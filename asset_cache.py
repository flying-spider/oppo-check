#!/usr/bin/env python3
"""静态资源内存/磁盘缓存 + 路由拦截辅助。

设计目标：让 Playwright 页面渲染所需的 JS/CSS/HTML 不走代理，
直接从本地内存 fulfill；只让业务 API 穿透代理。这样：
- 页面渲染 100% 成功（不受代理质量影响）
- 代理只承载关键 API 流量（KB 级），测速更快、更省带宽

对外接口：
- ensure_ready()      启动预热：磁盘缓存 → 内存，缺失则直连下载
- match_asset(url)    同步命中查询，返回 (body, content_type) 或 None
- fetch_direct(url)   直连下载（不经代理），用于未知 webpack chunk 的 fallback
- seed_from_har(path) 从 HAR 导入资源作为缓存种子（可选）
- refresh()           强制重新下载 manifest 资源（应对发版更新 hash）
- stats()             返回当前缓存统计
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

BASE = "https://safe.heytap.com"
PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / ".runtime" / "asset_cache"
BLOB_DIR = PROJECT_DIR / ".runtime" / "asset_blobs"

# 监控/埋点域名：路由层直接 abort，不产生任何流量
# 注意：obus-jssdk-cn.heytapmobi.com 不能 abort——页面 JS 初始化依赖它，
# 阻断会导致验证码加载链路断裂。
BLOCKED_DOMAINS = (
    "ums-telemetry-cn.heytapmobi.com",
    "obus-cn.dc.heytapmobi.com",
    "report.uc.heytapmobi.com",
)

# 验证码 SDN 域名：不走代理，直连下载后 fulfill 给浏览器。
# 这些域名的请求如果走代理可能不通（代理 IP 质量参差不齐），
# 导致动态 <script> 加载超时（5s），验证码 widget 永远不出现。
CAPTCHA_DOMAINS = (
    "cap-sec-gl.heytapmobi.com",
    "captcha-sec.heytapmobi.com",
    "captcha-sgp-sec.heytapmobile.com",
    "captcha-eu-sec.heytapmobile.com",
    "captcha-ind-sec.heytapmobile.com",
    "sec-captcha-us.heytapmobile.com",
)

# 页面渲染所需的静态资源清单（path → content-type）
# 来自正常渲染出输入框时的抓包；hash 变化时跑 refresh() 重新拉取
MANIFEST: list[tuple[str, str]] = [
    ("/appeal-system/entry/check", "text/html; charset=utf-8"),
    ("/conf/globalConfig.js", "application/javascript"),
    ("/favicon.ico", "image/x-icon"),
    ("/app-source/js/vendors_business.b3aa21a0dbc31cdf05d2.js", "application/javascript"),
    ("/app-source/js/appeal-system.f8ee4a9979d86d7e72c8.js", "application/javascript"),
    ("/app-source/js/lang-zh-CN-index.e8b1cafd7809945fd838.js", "application/javascript"),
    ("/app-source/js/56.81f9182f01847d6769ec.js", "application/javascript"),
    ("/app-source/js/2.f26ae8e685f4074a5ae8.js", "application/javascript"),
    ("/app-source/js/1.4632afbe19b61a2d38d7.js", "application/javascript"),
    ("/app-source/css/appeal-system.22885468b33f55766c8f.css", "text/css"),
    ("/app-source/css/vendors_business.bf672592af10c7cca025.css", "text/css"),
    ("/app-source/css/56.7f4a532bd1364aecc0ed.css", "text/css"),
    ("/app-source/css/2.2bc3d663ce9266be5cca.css", "text/css"),
]

# 验证码静态 JS（不走直连，通过代理预下载一次后缓存 serve，保持 IP 一致性）
CAPTCHA_STATIC: list[str] = [
    "https://cap-sec-gl.heytapmobi.com/dx-captcha/index.js",
    "https://cap-sec-gl.heytapmobi.com/dx-captcha/libs/const-id.js",
    "https://cap-sec-gl.heytapmobi.com/dx-captcha/libs/greenseer.js",
]
_CAPTCHA_CACHE: dict[str, tuple[bytes, str]] = {}  # url → (body, content_type)

# path -> (body, content_type)
_CACHE: dict[str, tuple[bytes, str]] = {}
# 下载失败过的 URL，避免重复直连尝试
_NEGATIVE: set[str] = set()
_lock = threading.Lock()
# 未知 chunk 的并发下载去重：url -> Future
_inflight: dict[str, "asyncio.Future"] = {}
_ready = False

# 诊断计数器（线程安全）
_hits = 0
_aborts = 0
_misses = 0
_first_misses: list[str] = []  # 最多记录 10 个首次 miss 的 URL


def _log(msg: str) -> None:
    print(f"[AssetCache] {msg}", file=sys.stderr)


def _key(url: str) -> str:
    """从完整 URL 或 path 提取缓存 key（path 部分）。"""
    if url.startswith(BASE):
        return url[len(BASE):] or "/"
    if url.startswith("http://") or url.startswith("https://"):
        return ""
    return url


def _disk_paths(path: str) -> Tuple[Path, Path]:
    h = hashlib.sha1(path.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.bin", CACHE_DIR / f"{h}.meta"


def _put(path: str, body: bytes, ctype: str, persist: bool = True) -> None:
    with _lock:
        _CACHE[path] = (body, ctype)
    if persist:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            bin_p, meta_p = _disk_paths(path)
            bin_p.write_bytes(body)
            # meta 格式：第 1 行 = path，第 2 行 = content-type
            meta_p.write_text(f"{path}\n{ctype}", encoding="utf-8")
        except Exception as e:
            _log(f"persist failed {path}: {e}")


def _load_disk() -> int:
    """从磁盘恢复缓存。meta 文件第 1 行是 path，第 2 行是 content-type。"""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for bin_p in CACHE_DIR.glob("*.bin"):
        meta_p = bin_p.with_suffix(".meta")
        if not meta_p.exists():
            continue
        try:
            lines = meta_p.read_text(encoding="utf-8").strip().split("\n", 2)
            if len(lines) < 2:
                # 旧格式（只有 content-type，无 path），跳过
                continue
            path = lines[0].strip()
            ctype = lines[1].strip()
            if not path.startswith("/"):
                continue  # 旧格式
            body = bin_p.read_bytes()
            with _lock:
                _CACHE[path] = (body, ctype)
            n += 1
        except Exception:
            continue
    return n


def _direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _download_sync(url: str, retries: int = 2) -> Optional[Tuple[bytes, str]]:
    """直连下载，带重试。连接超时 10s，读取超时 15s。"""
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(2)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
            },
        )
        try:
            with _direct_opener().open(req, timeout=15) as resp:
                body = resp.read()
                ctype = resp.headers.get("content-type", "application/octet-stream")
                return body, ctype.split(";")[0].strip() or "application/octet-stream"
        except Exception as e:
            if attempt < retries:
                _log(f"download retry {attempt+1}/{retries} {url[-60:]}: {type(e).__name__}")
            else:
                _log(f"direct download failed {url[-80:]}: {type(e).__name__}")
    return None


async def fetch_direct(url: str) -> Optional[Tuple[bytes, str]]:
    """直连下载并缓存（对未知 chunk 做并发去重）。"""
    path = _key(url)
    if not path:
        return None
    with _lock:
        if path in _CACHE:
            return _CACHE[path]
        if url in _NEGATIVE:
            return None

    loop = asyncio.get_event_loop()
    fut = _inflight.get(url)
    if fut is None:
        fut = loop.create_future()
        _inflight[url] = fut
        try:
            res = await asyncio.to_thread(_download_sync, url)
            if res:
                _put(path, res[0], res[1])
            else:
                with _lock:
                    _NEGATIVE.add(url)
            fut.set_result(res)
            return res
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            _inflight.pop(url, None)
    else:
        return await fut


async def fetch_external(url: str) -> Optional[Tuple[bytes, str]]:
    """直连下载任意域名的资源（不走代理、不缓存）。
    用于验证码域名等不能走代理的请求。"""
    return await asyncio.to_thread(_download_sync, url)


def match_captcha_asset(url: str) -> Optional[Tuple[bytes, str]]:
    """验证码静态 JS 缓存命中查询。"""
    with _lock:
        return _CAPTCHA_CACHE.get(url)


def put_captcha_asset(url: str, body: bytes, ctype: str) -> None:
    """存入验证码静态 JS 缓存。由 on_response 钩子调用。"""
    with _lock:
        if url not in _CAPTCHA_CACHE:
            _CAPTCHA_CACHE[url] = (body, ctype)


async def seed_captcha_assets(proxy_url: str, username: str = "", password: str = "") -> int:
    """通过指定代理预下载验证码静态 JS 并缓存。
    在 IP 池就绪后调用，使用代理 IP 下载（与页面同 IP 环境），
    缓存后浏览器请求时直接 serve，不再走代理。
    username/password 非空时走代理认证。
    返回成功缓存的条目数。"""
    import urllib.request as _ur
    n = 0
    for url in CAPTCHA_STATIC:
        # 已缓存则跳过
        with _lock:
            if url in _CAPTCHA_CACHE:
                n += 1
                continue
        res = await asyncio.to_thread(_download_via_proxy, url, proxy_url, 2, username, password)
        if res and len(res[0]) > 500:  # JS 至少 500 字节
            with _lock:
                _CAPTCHA_CACHE[url] = res
            n += 1
            _log(f"captcha seeded: {url[-50:]} ({len(res[0])}B)")
        else:
            _log(f"captcha seed FAIL: {url[-60:]}")
    return n


def _download_via_proxy(url: str, proxy_url: str, retries: int = 2,
                        username: str = "", password: str = "") -> Optional[Tuple[bytes, str]]:
    """通过指定代理下载资源（代理侧做 DNS）。username/password 非空时走认证。"""
    import time as _time
    from urllib.parse import urlparse as _urlparse
    _p = _urlparse(proxy_url)
    _auth = f"{username}:{password}@" if username else ""
    _full_proxy = f"http://{_auth}{_p.hostname}:{_p.port}"
    proxy = urllib.request.ProxyHandler({"http": _full_proxy, "https": _full_proxy})
    opener = urllib.request.build_opener(proxy)
    for attempt in range(retries + 1):
        if attempt > 0:
            _time.sleep(2)
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                    ),
                    "Accept": "*/*",
                },
            )
            with opener.open(req, timeout=15) as resp:
                body = resp.read()
                ctype = resp.headers.get("content-type", "application/javascript")
                return body, ctype.split(";")[0].strip()
        except Exception:
            continue
    return None


def match_asset(url: str) -> Optional[Tuple[bytes, str]]:
    """同步命中查询。只查内存，记录首次 miss。"""
    global _hits, _misses, _first_misses
    path = _key(url)
    if not path:
        _misses += 1
        return None
    with _lock:
        hit = _CACHE.get(path)
        if hit is not None:
            _hits += 1
            return hit
    _misses += 1
    if len(_first_misses) < 10 and "/app-source/" in url:
        _first_misses.append(url[:120])
    return None


def record_abort() -> None:
    global _aborts
    _aborts += 1


def stats() -> dict:
    """返回缓存统计 + 路由拦截诊断计数。"""
    with _lock:
        size = len(_CACHE)
    return {
        "cache_size": size,
        "ready": _ready,
        "hits": _hits,
        "misses": _misses,
        "aborts": _aborts,
        "first_misses": list(_first_misses),
    }


def seed_from_har(har_path: str | Path) -> int:
    """从 HAR 的 response.content.text 导入 safe.heytap.com 静态资源。"""
    try:
        with open(har_path, encoding="utf-8") as f:
            har = json.load(f)
    except Exception as e:
        _log(f"seed_from_har open failed: {e}")
        return 0
    n = 0
    for entry in har.get("log", {}).get("entries", []):
        url = entry.get("request", {}).get("url", "")
        if not url.startswith(BASE):
            continue
        path = _key(url)
        if not path:
            continue
        with _lock:
            if path in _CACHE:
                continue
        content = entry.get("response", {}).get("content", {})
        text = content.get("text")
        mime = content.get("mimeType", "application/octet-stream")
        if not text:
            continue
        if "image" in mime:
            continue
        _put(path, text.encode("utf-8"), mime.split(";")[0].strip(), persist=True)
        n += 1
    return n


async def ensure_ready(force: bool = False) -> dict:
    """预热缓存。返回统计信息。幂等，可重复调用。"""
    global _ready
    if _ready and not force:
        return {"status": "already_ready", "cache_size": len(_CACHE)}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk = _load_disk()
    _log(f"disk load: {disk} entries from {CACHE_DIR}")

    # HAR 种子（可选，best-effort）
    har_seeded = 0
    har_env = os.environ.get("OPPO_ASSET_HAR")
    candidates = [har_env] if har_env else glob.glob(str(PROJECT_DIR / "*.har"))
    for har in candidates:
        if har and Path(har).exists():
            n = seed_from_har(har)
            har_seeded += n
            if n:
                _log(f"HAR seed: {n} entries from {Path(har).name}")

    # manifest 缺失项直连下载（每项最多 3 次尝试，间隔 2s）
    downloaded = 0
    failed = []
    for path, ctype in MANIFEST:
        with _lock:
            if path in _CACHE:
                body, _ = _CACHE[path]
                # 校验已有缓存完整性：JS/CSS/HTML 应 > 100 字节
                min_size = 0 if path.endswith('.ico') else 100
                if len(body) >= min_size:
                    continue
                # 残缺 → 删除
                del _CACHE[path]
                # globalConfig.js 直连经常被阻断，跳过重试，运行时走代理加载
                if path == "/conf/globalConfig.js":
                    continue
                _log(f"re-downloading corrupt cache: {path[-60:]} ({len(body)} bytes)")
        url = BASE + path
        min_size = 0 if path.endswith('.ico') else 100
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2)
            res = await asyncio.to_thread(_download_sync, url, retries=1)
            if res and len(res[0]) >= min_size:
                _put(path, res[0], res[1])
                downloaded += 1
                _log(f"cached {path[-60:]} ({len(res[0])} bytes)")
                break
            if attempt == 2:
                size = len(res[0]) if res else 0
                failed.append(f"{path[-50:]} ({size}B)")
    if failed:
        _log(f"WARNING: {len(failed)} manifest files NOT cached: {failed}")
    if downloaded:
        _log(f"downloaded {downloaded} missing/corrupt manifest entries")

    _ready = True
    s = {
        "disk": disk,
        "har_seeded": har_seeded,
        "downloaded": downloaded,
        "cache_size": len(_CACHE),
    }
    _log(f"ensure_ready: {s}")
    return {"status": "ready", **s}


async def refresh() -> dict:
    """强制重新下载 manifest 全部资源（应对发版 hash 更新）。"""
    with _lock:
        for path, _ in MANIFEST:
            _CACHE.pop(path, None)
    return await ensure_ready(force=True)


# ========== 本地 HTTP server（绕开 CDP） ==========

import http.server as _http_server
import socket as _socket
import threading as _threading

_cache_server_port: int = 0
_cache_server: "http.server.HTTPServer | None" = None


class _CacheHandler(_http_server.BaseHTTPRequestHandler):
    """极简 HTTP handler：只用 HEAD/GET，从 _CACHE 内存 serve"""

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()

    def _serve(self):
        path = self.path.split("?")[0]        # strip query string
        with _lock:
            entry = _CACHE.get(path)
        if entry:
            body, ctype = entry
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # 静默


def start_cache_server() -> int:
    """启动本地 HTTP cache server，返回监听端口。幂等。"""
    global _cache_server_port, _cache_server
    if _cache_server is not None:
        return _cache_server_port
    # 找可用端口
    for port in range(18000, 18100):
        try:
            _cache_server = _http_server.HTTPServer(
                ("127.0.0.1", port), _CacheHandler)
            _cache_server_port = port
            break
        except OSError:
            continue
    if _cache_server is None:
        raise RuntimeError("无法启动本地 cache server：端口范围 18000-18099 全被占用")
    t = _threading.Thread(target=_cache_server.serve_forever, daemon=True)
    t.start()
    _log(f"local cache server started on http://127.0.0.1:{_cache_server_port}")
    return _cache_server_port


def stop_cache_server():
    global _cache_server
    if _cache_server:
        _cache_server.shutdown()
        _cache_server = None