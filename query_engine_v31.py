#!/usr/bin/env python3
"""
OPPO 查询引擎 v31 - 完整重构
核心改进:
1. 非headless模式默认 → 避免风控检测
2. 共享浏览器 + 多context(不同代理) → 高效并发
3. 集成验证码自动识别(滑块+点选) → 真正解决验证码
4. 精确的结果解析 → 提取昵称/账号/注册时间
5. 完善的错误处理和重试逻辑
"""
from __future__ import annotations
import asyncio, time, requests, threading, traceback, re, random, base64, io, sys, os, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple, Deque
from collections import deque
from playwright.async_api import async_playwright

ENTRY_URL = "https://safe.heytap.com/appeal-system/entry/check"
DEFAULT_PROXY_API = "http://api.91http.com/v1/get-ip?trade_no=B093571258525&secret=rvANrz3E0RXbQoNc&protocol=1&format=text&sep=1&filter=1&auto_white=1"
PROJECT_DIR = Path(__file__).resolve().parent
CAPTCHA_DEBUG_DIR = os.environ.get(
    "OPPO_CAPTCHA_DEBUG_DIR",
    None,  # 默认关闭，设置环境变量可启用
)
BROWSER_VIEWPORT = {"width": 1400, "height": 1000}

UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

# v7 求解器: 经真实滑块截图与合成点选用例验证的检测器
try:
    from captcha_solver_v7 import solve_captcha_v7
    HAS_V7 = True
except Exception:  # noqa: BLE001
    HAS_V7 = False

# AntiCAP 求解器: 基于 https://github.com/81NewArk/AntiCAP 的识别适配层
try:
    from captcha_solver_anticap import solve_captcha_anticap
except Exception:  # noqa: BLE001
    solve_captcha_anticap = None

# DX 注入式求解器: 通过 page.evaluate 直接 dispatch DOM 事件到 captcha 组件
try:
    from captcha_solver_dx import solve_captcha_dx
    HAS_DX = True
except Exception:
    HAS_DX = False
    solve_captcha_dx = None


@dataclass
class QueryResult:
    phone: str = ""
    registered: Optional[bool] = None
    account_name: Optional[str] = None
    nick_name: Optional[str] = None
    register_time: Optional[str] = None
    captcha_triggered: bool = False
    error: Optional[str] = None
    elapsed_ms: int = 0
    proxy_used: str = ""
    retries: int = 0
    masked_phone: Optional[str] = None


class ProxyPool:
    """代理池 - 线程安全，支持过期时间管理"""
    def __init__(self, api_url=None):
        self._api_url = api_url or DEFAULT_PROXY_API
        self._pool: Deque[Tuple[str, float]] = deque()  # (proxy_url, expire_ts)
        self._lock = threading.Lock()

    def get(self) -> Tuple[Optional[str], float]:
        """获取代理，返回 (proxy_url, expire_ts)。expire_ts 为 Unix timestamp"""
        if not self._api_url:
            return None, 0
        with self._lock:
            now = time.time()
            self._pool = deque((x, t) for x, t in self._pool if t > now)
            if self._pool:
                return self._pool.popleft()
        return self._fetch_one()

    def _fetch_one(self) -> Tuple[Optional[str], float]:
        """从 API 获取一个代理，返回 (proxy_url, expire_ts)"""
        import json as _json
        for _ in range(3):
            try:
                r = requests.get(self._api_url, timeout=10,
                                 proxies={"http": None, "https": None})
                if r.status_code == 200:
                    data = _json.loads(r.text)
                    if data.get("code") == 0 and data.get("data", {}).get("proxy_list"):
                        item = data["data"]["proxy_list"][0]
                        proxy_url = f"http://{item['ip']}:{item['port']}"
                        expire_ts = time.mktime(time.strptime(
                            item["expire_time"], "%Y-%m-%d %H:%M:%S"))
                        return proxy_url, expire_ts
            except Exception:
                pass
            time.sleep(0.5)
        return None, 0

    def fetch_batch(self, count: int) -> List[str]:
        """批量获取代理"""
        if not self._api_url:
            return [None] * count
        batch_url = self._api_url.replace("&num=1", f"&num={count}")
        if "&num=" not in batch_url:
            batch_url += f"&num={count}"
        for _ in range(3):
            try:
                r = requests.get(batch_url, timeout=15,
                                 proxies={"http": None, "https": None})
                if r.status_code == 200:
                    lines = [l.strip() for l in r.text.strip().split("\n") if ":" in l]
                    if lines:
                        return [f"http://{l}" if not l.startswith("http") else l for l in lines]
            except Exception:
                pass
            time.sleep(1)
        return [None] * count


def _test_proxy_quality(proxy_url: str, tcp_timeout: float = 5,
                        username: str = "", password: str = "") -> dict:
    """测试代理连通性：TCP 握手 + HTTP HEAD 穿透。不测延迟、不下载内容。返回 {tcp_ms, ok, detail}。
    username/password 非空时走代理认证，为空时走白名单模式。"""
    import socket as _socket
    from urllib.parse import urlparse as _urlparse
    p = _urlparse(proxy_url)
    result = {"tcp_ms": 0, "latency_ms": 0, "ok": False, "detail": ""}

    # 1) TCP 连接代理端口
    t0 = time.time()
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(tcp_timeout)
    try:
        sock.connect((p.hostname, p.port))
        result["tcp_ms"] = int((time.time() - t0) * 1000)
        sock.close()
    except Exception as e:
        result["detail"] = f"TCP不通({type(e).__name__})"
        return result

    if result["tcp_ms"] > 5000:
        result["detail"] = f"TCP太慢({result['tcp_ms']}ms)"
        return result

    # 2) HTTP HEAD 穿透代理 → 验证代理能否路由到 safe.heytap.com，记录延迟
    try:
        _auth = f"{username}:{password}@" if username else ""
        _proxy_auth = f"http://{_auth}{p.hostname}:{p.port}"
        proxies = {"http": _proxy_auth, "https": _proxy_auth}
        t1 = time.time()
        r = requests.head(
            "https://safe.heytap.com",
            timeout=5,
            proxies=proxies,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=False,
        )
        result["latency_ms"] = int((time.time() - t1) * 1000)
        # 任何 HTTP 响应（包括 3xx/4xx）都说明代理能路由，只有连接失败才拒
        result["ok"] = True
        result["detail"] = f"HTTP {r.status_code} {result['latency_ms']}ms"
    except Exception as e:
        result["detail"] = f"HTTP不通({type(e).__name__})"
        return result

    return result


@dataclass
class IPEntry:
    """IP 池条目"""
    proxy_url: str
    expire_ts: float   # Unix timestamp, 来自 API 响应的过期时间
    latency_ms: int    # TCP 延迟 (ms)
    added_ts: float    # 入池时间
    username: str = "" # 代理认证用户名（91http 账号密码模式，白名单模式为空）
    password: str = "" # 代理认证密码


class IPPool:
    """异步 IP 池：后台持续获取、并发测试延迟、合格入池、worker 消费后移除不复用"""

    def __init__(self, api_url: str, threshold_ms: int = 2000, max_pool: int = 200,
                 tester_count: int = 40):
        if not api_url:
            raise ValueError("IPPool 需要 api_url")
        # 确保每次 API 请求获取足够代理（默认无 num 参数时 91http 只返回 1 条，50 并发根本不够）
        if "&num=" not in api_url and "?num=" not in api_url:
            api_url += "&num=50"
        self._api_url = api_url
        self._threshold_ms = threshold_ms
        self._queue: asyncio.LifoQueue = asyncio.LifoQueue(maxsize=max_pool)
        self._untested: asyncio.LifoQueue = asyncio.LifoQueue()
        self._running = False
        self._fetcher_task: asyncio.Task | None = None
        self._tester_tasks: list[asyncio.Task] = []
        self._tester_count = tester_count
        self._tested = 0
        self._passed = 0
        self._used = 0
        self._total_latency = 0
        self._rej_tcp = 0
        self._rej_http = 0
        self._rej_latency = 0
        self._rej_expired = 0
        self._rej_full = 0

    @property
    def stats(self) -> dict:
        size = self._queue.qsize()
        avg = self._total_latency // self._passed if self._passed > 0 else 0
        return {
            "size": size, "used": self._used,
            "tested": self._tested, "pending": self._untested.qsize(),
            "avg_latency": avg,
            "rej_tcp": self._rej_tcp, "rej_http": self._rej_http,
            "rej_latency": self._rej_latency,
            "rej_expired": self._rej_expired,
            "rej_full": self._rej_full,
        }

    async def start(self):
        """启动后台 fetcher + tester 协程组"""
        self._running = True
        self._fetcher_task = asyncio.create_task(self._fetch_loop())
        for _ in range(self._tester_count):
            self._tester_tasks.append(asyncio.create_task(self._tester_loop()))
        print(f"[IPPool] started, threshold={self._threshold_ms}ms, pool={self._queue.maxsize}, "
              f"testers={self._tester_count}, pending=无界",
              file=sys.stderr)

    async def stop(self):
        """停止后台任务并清空队列"""
        self._running = False
        if self._fetcher_task:
            self._fetcher_task.cancel()
            try: await self._fetcher_task
            except asyncio.CancelledError: pass
        for t in self._tester_tasks:
            t.cancel()
        if self._tester_tasks:
            await asyncio.gather(*self._tester_tasks, return_exceptions=True)
        while not self._queue.empty():
            try: self._queue.get_nowait()
            except asyncio.QueueEmpty: break
        while not self._untested.empty():
            try: self._untested.get_nowait()
            except asyncio.QueueEmpty: break
        print(f"[IPPool] stopped, stats: {self.stats}", file=sys.stderr)

    async def get(self, timeout: float = 15) -> Tuple[Optional[str], float, str, str]:
        """取一个未过期的代理，返回 (proxy_url, expire_ts, username, password)。
        username 为空时走白名单模式。超时返回 (None, 0, '', '')。"""
        deadline = time.time() + timeout
        # 先无阻塞清空所有过期条目，避免过期堆积堵死队列
        while not self._queue.empty():
            try:
                entry = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if time.time() < entry.expire_ts:
                # 这条还是有效的，放回去并退出清空循环
                await self._queue.put(entry)
                break
            # 过期则丢弃（不计入 timeout 预算）
        # 现在队列里要么有有效条目，要么为空——阻塞等待
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                entry = await asyncio.wait_for(
                    self._queue.get(), timeout=min(remaining, 2))
                now = time.time()
                if now < entry.expire_ts:
                    self._used += 1
                    return entry.proxy_url, entry.expire_ts, entry.username, entry.password
                # 过期则丢弃，继续取下一个
            except asyncio.TimeoutError:
                continue
        return None, 0, "", ""

    async def _fetch_loop(self):
        """定时器：每秒 fetch → dump 到待测队列，每 8 秒打一次日志"""
        _tick = 0
        while self._running:
            try:
                proxies = await self._fetch_proxies()
                if proxies:
                    for p in proxies:
                        self._untested.put_nowait(p)
                _tick += 1
                if _tick % 8 == 0:
                    s = self.stats
                    print(f"[IPPool] 可用={s['size']} 已用={s['used']} "
                          f"已扫={s['tested']} 待扫={s['pending']} 均={s['avg_latency']}ms "
                          f"| 拒: TCP={s['rej_tcp']} HTTP={s['rej_http']} "
                          f"慢>{self._threshold_ms}ms={s['rej_latency']} 过期={s['rej_expired']} 满={s['rej_full']}",
                          file=sys.stderr)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[IPPool] fetch error: {type(e).__name__}", file=sys.stderr)
            await asyncio.sleep(0.5)  # 2次/秒获取，补充池空洞

    async def _tester_loop(self):
        """测试协程：TCP 连通即合格，分类统计，合格入池"""
        while self._running:
            try:
                proxy = await asyncio.wait_for(self._untested.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            try:
                result = await self._test_one(proxy)
                self._tested += 1
                if isinstance(result, Exception) or not result["ok"]:
                    if not isinstance(result, Exception) and "TCP" in result.get("detail", ""):
                        self._rej_tcp += 1
                    else:
                        self._rej_http += 1
                    continue
                if result["latency_ms"] > self._threshold_ms:
                    self._rej_latency += 1
                    continue
                expire_ts = self._parse_expire(proxy.get("expire_time", ""))
                if expire_ts <= time.time():
                    self._rej_expired += 1
                    continue
                entry = IPEntry(
                    proxy_url=f"http://{proxy['ip']}:{proxy['port']}",
                    expire_ts=expire_ts,
                    latency_ms=result["latency_ms"],
                    added_ts=time.time(),
                    username=proxy.get("user", "") or proxy.get("username", ""),
                    password=proxy.get("pass", "") or proxy.get("password", ""),
                )
                try:
                    self._queue.put_nowait(entry)
                    self._passed += 1
                    self._total_latency += result["latency_ms"]
                except asyncio.QueueFull:
                    self._rej_full += 1
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _fetch_proxies(self) -> list:
        """HTTP GET 代理 API，解析返回 proxy_list。强制直连，不走系统代理。"""
        try:
            r = await asyncio.to_thread(
                requests.get, self._api_url, timeout=10,
                proxies={"http": None, "https": None})
            if r.status_code != 200:
                return []
            data = r.json()
            if data.get("code") != 0:
                return []
            return data.get("data", {}).get("proxy_list", [])
        except Exception as e:
            print(f"[IPPool] API fetch error: {type(e).__name__}", file=sys.stderr)
            return []

    async def _test_one(self, proxy: dict) -> dict:
        """测试单个代理（在线程池执行同步 HTTP 请求）"""
        proxy_url = f"http://{proxy['ip']}:{proxy['port']}"
        user = proxy.get("user", "") or proxy.get("username", "")
        pwd = proxy.get("pass", "") or proxy.get("password", "")
        return await asyncio.to_thread(_test_proxy_quality, proxy_url, 5, user, pwd)

    @staticmethod
    def _parse_expire(expire_str: str) -> float:
        """解析过期时间字符串为 Unix timestamp"""
        try:
            return time.mktime(time.strptime(expire_str, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return time.time() + 300  # fallback: 5 分钟


async def _save_page_screenshot(page, name: str):
    """诊断截图已禁用（不写入 captcha_debug 目录）"""
    pass


# ============== 验证码识别 ==============

async def _captcha_type(page) -> str:
    """检测验证码类型"""
    try:
        text = await page.evaluate('document.body?.innerText?.substring(0,500)||""')
        if any(k in text for k in ['验证通过', '验证成功', '校验通过', '校验成功']):
            return 'none'
        # 顶象点选
        if await page.evaluate('!!document.querySelector(".dx_captcha_clickword,.yidun_tips")'):
            return 'clickword'
        # 顶象滑块
        if await page.evaluate('!!document.querySelector(".dx_captcha_slider,.dx_captcha_basic,.yidun_slider")'):
            return 'slider'
        # 通用滑块检测：input[type=range] 或包含 slide 的 class
        if await page.evaluate(
                '!!document.querySelector("input[type=range], [class*=slider], [class*=slide], '
                '[class*=drag], [class*=track]")'):
            return 'slider'
        if any(k in text for k in ['请依次点击', '请点击']):
            return 'clickword'
        if any(k in text for k in ['向右拖动', '滑块', '滑动滑块', '拖动滑块', '完成拼图',
                                     '请拖动', '向右滑动', '安全验证']):
            return 'slider'
        return 'none'
    except Exception:
        return 'none'


async def _wait_captcha(page, timeout_ms: int = 10000) -> str:
    """等待验证码元素出现，返回类型或 'none'（超时）。"""
    captcha_sel = (
        ".dx_captcha_clickword, .dx_captcha_slider, .dx_captcha_basic, "
        ".yidun_tips, .yidun_slider, .yidun_popup, "
        "input[type=range]"
    )
    try:
        # 先快速检查页面文本（不浪费时间等 DOM）
        text = await page.evaluate('document.body?.innerText?.substring(0,500)||""')
        # 排除表单页面文本（可能包含隐藏的验证码关键词），只信任短文本或含明确验证码关键词的文本
        is_form_page = any(k in text for k in ('请输入', '申诉账号', '下一步', '隐私政策'))
        if any(k in text for k in ['请依次点击', '请点击', '向右拖动', '滑块', '滑动滑块',
                                     '拖动滑块', '完成拼图', '请拖动', '向右滑动', '安全验证']):
            if is_form_page and len(text) > 100:
                pass  # 长表单文本 + 表单关键词 → 跳过文本检测，只靠 DOM 选择器
            else:
                for k in ['请依次点击', '请点击']:
                    if k in text:
                        return 'clickword'
                # 滑块必须确认 DOM 元素存在（文字不可靠，可能还没加载完）
                slider_sel = ".dx_captcha_slider, .dx_captcha_basic, .yidun_slider, input[type=range]"
                for k in ['向右拖动', '滑块', '滑动滑块', '拖动滑块', '完成拼图', '请拖动', '向右滑动']:
                    if k in text:
                        try:
                            await page.wait_for_selector(slider_sel, timeout=5000)
                            return 'slider'
                        except Exception:
                            pass  # DOM 没出现，继续等完整 selector
                if '安全验证' in text:
                    try:
                        await page.wait_for_selector(slider_sel, timeout=5000)
                        return 'slider'
                    except Exception:
                        pass

        # 文本没有命中，再等 DOM 元素
        await page.wait_for_selector(captcha_sel, timeout=timeout_ms)
        # 元素出现了，用 _captcha_type 确定具体类型
        return await _captcha_type(page)
    except Exception:
        pass
    # wait_for_selector 超时，再检查文本
    try:
        text = await page.evaluate('document.body?.innerText?.substring(0,500)||""')
        if any(k in text for k in ['请依次点击', '请点击', '向右拖动', '滑块', '滑动滑块',
                                     '拖动滑块', '完成拼图', '请拖动', '向右滑动', '安全验证']):
            # 先检查明确的点选关键词
            for k in ['请依次点击', '请点击']:
                if k in text:
                    return 'clickword'
            # 再检查滑块关键词
            for k in ['向右拖动', '滑块', '滑动滑块', '拖动滑块', '完成拼图', '请拖动', '向右滑动']:
                if k in text:
                    return 'slider'
            # 最后才是"安全验证"（不确定类型，优先尝试滑块）
            if '安全验证' in text:
                return 'slider'
            return 'slider'
    except Exception:
        pass
    # 完全没检测到验证码，记录当前页面文本用于诊断
    try:
        t = await page.evaluate('document.body?.innerText?.substring(0,200)||""')
        # diagnostic muted: page text too verbose
        pass
    except Exception:
        pass
    return 'none'


async def _wait_captcha_ready(page, timeout_ms: int = 40000) -> str:
    """动态等待验证码三阶段就绪，返回类型 'slider' | 'clickword' | 'none'。

    阶段1: '智能检测' loading 弹窗 → 消失（SDK JS + API 初始化完成）
    阶段2: captcha DOM 选择器出现（widget 渲染）
    阶段3: canvas 像素非均匀（图片加载完成，可安全截图求解）
    """
    deadline = time.time() + timeout_ms / 1000

    # 阶段1: 等待"智能检测"弹窗出现再消失
    loading_seen = False
    while time.time() < deadline:
        try:
            text = await page.evaluate('document.body?.innerText||""')
        except Exception:
            text = ""
        if '智能检测' in text:
            loading_seen = True
        elif loading_seen and '智能检测' not in text:
            break
        await asyncio.sleep(0.3)

    # 阶段2: 等 captcha DOM 元素出现
    captcha_sel = (
        ".dx_captcha_slider, .dx_captcha_basic, .dx_captcha_clickword, "
        ".yidun_tips, .yidun_slider, input[type=range]"
    )
    remaining = max(3000, int((deadline - time.time()) * 1000))
    try:
        await page.wait_for_selector(captcha_sel, timeout=min(20000, remaining))
    except Exception:
        pass

    # 阶段3: canvas 图片就绪（采样 3 点验证非纯色）
    while time.time() < deadline:
        try:
            ready = await page.evaluate("""() => {
                const c = document.querySelector(
                    '.dx_captcha_basic_box canvas,.dx_captcha_clickword_pic canvas,canvas');
                if (!c || c.width < 50 || c.height < 50) return false;
                const ctx = c.getContext('2d');
                if (!ctx) return false;
                const pts = [[c.width/2|0,c.height/2|0],[10,10],[c.width-10,c.height-10]];
                const colors = pts.map(([x,y]) =>
                    Array.from(ctx.getImageData(x,y,1,1).data).join(','));
                return new Set(colors).size >= 2;
            }""")
        except Exception:
            ready = False
        if ready:
            break
        await asyncio.sleep(0.3)

    return await _captcha_type(page)


async def _solve_slider(page, max_attempts=6) -> bool:
    """滑块验证码 — 优先用 v7 solver"""
    if HAS_V7:
        try:
            return await solve_captcha_v7(page, max_attempts=max_attempts, debug_dir=CAPTCHA_DEBUG_DIR)
        except Exception:
            pass
    return False
async def _solve_clickword(page, max_attempts=5) -> bool:
    """点选文字验证码 - 多方法模板匹配"""
    if not HAS_CV2:
        return False
    for attempt in range(max_attempts):
        clicked = []
        try:
            await asyncio.sleep(2)

            # 获取提示文字图片
            hint_imgs = await page.evaluate('''() => {
                const div = document.querySelector('.dx_captcha_clickword_hint-cnt');
                if (!div) return [];
                const imgs = div.querySelectorAll('img');
                return Array.from(imgs).map(img => ({
                    src: img.src || '',
                    width: Math.round(img.offsetWidth) || 77,
                    height: Math.round(img.offsetHeight) || 20,
                }));
            }''')

            if not hint_imgs:
                return False

            # 获取canvas背景图
            canvas_el = (await page.query_selector('.dx_captcha_clickword_pic canvas') or
                         await page.query_selector('canvas'))
            if not canvas_el:
                return False

            canvas_box = await canvas_el.bounding_box()
            if not canvas_box or canvas_box['width'] < 10:
                return False

            canvas_bytes = await canvas_el.screenshot()
            canvas_arr = np.frombuffer(canvas_bytes, np.uint8)
            canvas_img = cv2.imdecode(canvas_arr, cv2.IMREAD_COLOR)
            if canvas_img is None:
                return False

            canvas_gray = cv2.cvtColor(canvas_img, cv2.COLOR_BGR2GRAY)
            canvas_edges = cv2.Canny(canvas_gray, 50, 150)
            ch, cw = canvas_gray.shape[:2]

            for hi, hint in enumerate(hint_imgs):
                src = hint.get('src', '')
                if not src or not src.startswith('data:image'):
                    continue

                # 解码hint图片
                b64 = src.split(',', 1)[1] if ',' in src else src
                hint_bytes = base64.b64decode(b64)
                hint_arr = np.frombuffer(hint_bytes, np.uint8)
                hint_rgba = cv2.imdecode(hint_arr, cv2.IMREAD_UNCHANGED)
                if hint_rgba is None:
                    continue

                hh, hw = hint_rgba.shape[:2]
                has_alpha = hint_rgba.shape[-1] == 4

                dw, dh = hint['width'], hint['height']
                if dw < 3 or dh < 3:
                    continue
                if dw > cw:
                    dw = cw // 2
                if dh > ch:
                    dh = ch // 2

                # 创建模板
                if has_alpha:
                    alpha = hint_rgba[:, :, 3]
                    char_mask = (alpha > 100).astype(np.uint8) * 255
                    template_gray = char_mask
                    # 获取文字像素(白底黑字)
                    char_pixels = hint_rgba[:, :, 0:3].copy()
                    for c in range(3):
                        char_pixels[:, :, c] = np.where(alpha > 100, char_pixels[:, :, c], 255)
                    template_color = cv2.cvtColor(char_pixels, cv2.COLOR_BGR2GRAY)
                else:
                    template_gray = cv2.cvtColor(hint_rgba, cv2.COLOR_BGR2GRAY)
                    template_color = template_gray

                template_gray = cv2.resize(template_gray, (dw, dh))
                template_color = cv2.resize(template_color, (dw, dh))

                # 方法1: 边缘匹配
                t_edges = cv2.Canny(template_gray, 30, 100)
                r1 = cv2.matchTemplate(canvas_edges, t_edges, cv2.TM_CCOEFF_NORMED)
                _, mv1, _, ml1 = cv2.minMaxLoc(r1)

                # 方法2: 灰度匹配
                r2 = cv2.matchTemplate(canvas_gray, template_color, cv2.TM_CCOEFF_NORMED)
                _, mv2, _, ml2 = cv2.minMaxLoc(r2)

                # 方法3: SQDIFF
                r3 = cv2.matchTemplate(canvas_edges, t_edges, cv2.TM_SQDIFF_NORMED)
                mv3, _, ml3, _ = cv2.minMaxLoc(r3)
                mv3 = 1.0 - mv3

                # 选择最佳结果
                best_val, best_loc = max((mv1, ml1), (mv2, ml2), (mv3, ml3), key=lambda x: x[0])

                if best_val < 0.2:
                    continue

                page_x = canvas_box['x'] + best_loc[0] + dw // 2 + random.randint(-3, 3)
                page_y = canvas_box['y'] + best_loc[1] + dh // 2 + random.randint(-3, 3)

                # 去重
                dup = any(abs(px - page_x) < 15 and abs(py - page_y) < 15 for px, py in clicked)
                if dup:
                    continue

                await page.mouse.click(page_x, page_y)
                clicked.append((page_x, page_y))
                await asyncio.sleep(random.uniform(0.6, 1.0))

            await asyncio.sleep(2)
            if await _captcha_type(page) == 'none':
                return True

            # 刷新重试
            try:
                refresh = await page.query_selector('[class*="refresh"]')
                if refresh:
                    await refresh.click()
                    await asyncio.sleep(1.5)
            except Exception:
                pass
        except Exception as e:
            print(f"  [Clickword] e: {e}", file=sys.stderr)
    return False


async def _solve_captcha(page, max_attempts=3, refresh_counter=None) -> bool:
    # AntiCAP 优先处理点选（YOLO + Siamese），滑块交给 v7
    if solve_captcha_anticap is not None:
        ok = await solve_captcha_anticap(page, max_attempts=max_attempts, debug_dir=CAPTCHA_DEBUG_DIR,
                                          refresh_counter=refresh_counter)
        if ok:
            return True
    if HAS_V7:
        print("  [solve] falling back to v7", file=sys.stderr)
        return await solve_captcha_v7(page, max_attempts=max_attempts, debug_dir=CAPTCHA_DEBUG_DIR,
                                       refresh_counter=refresh_counter)
    return False
async def _query_one(page, phone: str, try_captcha: bool = True,
                     console_msgs: list | None = None,
                     stop_event: asyncio.Event | None = None,
                     proxy_expire_at: float = 0.0,
                     captcha_refresh_count=None) -> QueryResult:
    """执行一次完整的查询"""
    import asset_cache as _ac
    t0 = time.time()
    captcha_seen = False

    # 网络诊断：收集失败/慢请求信息
    _net_info = {
        "failed": [], "main_status": None,
        "total_requests": 0, "total_failed": 0,
    }
    _req_start = {}   # {url: start_time} 请求开始时间
    _slow_reqs = []   # [(url, dur_ms), ...] 耗时 >3s 的请求

    try:
        # 快速检查 stop 信号
        def _should_stop():
            return stop_event and stop_event.is_set()

        # 1. 加载页面 — 注册网络监控
        last_api_response = None
        async def on_request(req):
            _req_start[req.url] = time.time()

        async def on_response(resp):
            nonlocal last_api_response
            _net_info["total_requests"] += 1
            if 'appeal' in resp.url and ('query' in resp.url or 'pre' in resp.url):
                last_api_response = {'url': resp.url, 'status': resp.status}
            if resp.url == ENTRY_URL or 'entry/check' in resp.url:
                _net_info["main_status"] = resp.status
            # 记录慢请求（耗时 >3s 且未失败）
            if resp.url in _req_start:
                dur = int((time.time() - _req_start.pop(resp.url)) * 1000)
                if dur > 3000 and len(_slow_reqs) < 5:
                    _slow_reqs.append((resp.url[:100], dur))
            # 验证码 JS 响应缓存：代理成功返回后存下来，后续直接 serve
            if any(d in resp.url for d in _ac.CAPTCHA_DOMAINS) and resp.url.endswith(".js") and resp.status == 200:
                try:
                    body = await resp.body()
                    ctype = resp.headers.get("content-type", "application/javascript")
                    if body and len(body) > 500:
                        _ac.put_captcha_asset(resp.url, body, ctype)
                except Exception:
                    pass
            # 验证码 JS 响应缓存（走代理成功的首回，后续直接从内存 serve）
            if resp.status == 200 and any(d in resp.url for d in _ac.CAPTCHA_DOMAINS) and resp.url.endswith(".js"):
                try:
                    body = await resp.body()
                    ctype = resp.headers.get("content-type", "application/javascript")
                    _ac.put_captcha_asset(resp.url, body, ctype)
                except Exception:
                    pass

        async def on_request_failed(req):
            _net_info["total_failed"] += 1
            if len(_net_info["failed"]) < 5:
                _net_info["failed"].append(
                    f"{req.failure or '?'}: {req.url[:120]}")

        page.on('request', on_request)
        page.on('response', on_response)
        page.on('requestfailed', on_request_failed)

        # 诊断输出辅助
        def _dump_net_diag(reason: str, text_snapshot: str = ""):
            slow_info = ""
            if _slow_reqs:
                slow_info = f" slow={_slow_reqs[:3]}"
            print(f"  [NetDiag {reason}] main_http={_net_info['main_status']} "
                  f"reqs={_net_info['total_requests']} failed={_net_info['total_failed']}"
                  f"{slow_info} "
                  f"sample={_net_info['failed'][:3]}", file=sys.stderr)
        # IP 过期检查：在加载页面前先验证，避免用过期 IP 重试
        if proxy_expire_at > 0 and time.time() > proxy_expire_at:
            return QueryResult(phone=phone, error="ip_expired",
                             elapsed_ms=int((time.time() - t0) * 1000))
        resp = await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2)

        text = await page.evaluate('document.body?.innerText||""')

        # 2. 输入手机号
        inp = page.locator('input[type="tel"]').first
        if await inp.is_visible(timeout=10000):
            await inp.click()
            await asyncio.sleep(0.3)
            await inp.fill(phone)
            await asyncio.sleep(0.5)
        else:
            _dump_net_diag("no_input", text)
            return QueryResult(phone=phone, error="找不到手机号输入框",
                             elapsed_ms=int((time.time() - t0) * 1000))

        # 3. 勾选协议
        cb = page.locator('div[role="checkbox"]').first
        if await cb.is_visible(timeout=3000):
            try:
                checked = await cb.get_attribute('aria-checked')
                if checked != 'true':
                    await cb.click()
                    await asyncio.sleep(0.5)
            except Exception:
                await cb.click()
                await asyncio.sleep(0.5)

        # 4. 点击下一步 — 用 Playwright get_by_role 精确查找
        await _save_page_screenshot(page, "before_btn_click.png")
        clicked_btn = "not_clicked"
        # 方案 A: role="button" 精确匹配
        try:
            btn = page.get_by_role("button", name="下一步")
            if await btn.count() > 0:
                await btn.first.click(timeout=3000)
                clicked_btn = "role_click_ok"
        except Exception:
            pass
        # 方案 B: 遍历所有 button 标签
        if clicked_btn != "role_click_ok":
            try:
                btns = page.locator("button")
                for i in range(await btns.count()):
                    b = btns.nth(i)
                    txt = await b.inner_text()
                    if "下一步" in txt:
                        await b.click(timeout=3000)
                        clicked_btn = "button_click_ok"
                        break
            except Exception:
                pass
        # 方案 C: JS 万能查找
        if clicked_btn not in ("role_click_ok", "button_click_ok"):
            clicked_btn = await page.evaluate("""
                () => {
                    // 精确查找可点击按钮
                    for (const sel of ['[role="button"]', 'button', 'a.btn', '.btn', '[class*="submit"]']) {
                        const el = document.querySelector(sel);
                        if (el && el.textContent && el.textContent.includes('下一步')) {
                            el.click();
                            return 'js_' + sel;
                        }
                    }
                    // fallback: 文本包含下一步的任意元素
                    const all = document.querySelectorAll('*');
                    for (const n of all) {
                        if (n.children.length === 0 && n.textContent && n.textContent.trim() === '下一步') {
                            n.click();
                            return 'js_leaf_text';
                        }
                    }
                    return 'js_not_found';
                }
            """)
        if clicked_btn not in ("role_click_ok", "button_click_ok", "js_role_button", "js_button", "js_a.btn", "js_.btn", "js_[class*=submit]", "js_leaf_text"):
            _dump_net_diag("no_btn", await page.evaluate('document.body?.innerText||""'))
            return QueryResult(phone=phone, error="找不到下一步按钮",
                             elapsed_ms=int((time.time() - t0) * 1000))

        # diagnostic muted
        await _save_page_screenshot(page, "after_btn_click.png")

        # 5. 高频轮询捕获短暂 toast — 每 300ms 取一次，累积文本（延长到 ~18 秒，慢代理需要更多时间）
        accumulated_texts: list[str] = []
        # diagnostic muted
        for _poll in range(50):  # ~15 秒（代理慢时等 API 响应）
            if _should_stop():
                return QueryResult(phone=phone, error="stopped",
                                 elapsed_ms=int((time.time() - t0) * 1000))
            # 最高优先级：页面 URL 跳转即代表结果
            if "appeal-system/entry/select-account" in str(page.url):
                return QueryResult(phone=phone, registered=True,
                                 captcha_triggered=captcha_seen,
                                 elapsed_ms=int((time.time() - t0) * 1000))
            await asyncio.sleep(0.3)
            try:
                t = await page.evaluate('document.body?.innerText||""')
                if t and t not in accumulated_texts:
                    accumulated_texts.append(t)
                    # 风控 toast 检测：短文本 + 不含表单词（真实 toast 不会有表单文字）
                    if '环境存在风险' in t and len(t) < 200:
                        if not any(k in t for k in ('请输入', '申诉账号', '手机号', '下一步')):
                            print(f"  [风控] 检测到风控提示: {t[:120]}", file=sys.stderr)
                            return QueryResult(phone=phone, error=f"风控: {t[:80]}",
                                         elapsed_ms=int((time.time() - t0) * 1000))
                    # 提前检测到关键内容就跳过剩余轮询
                    if any(k in t for k in ['验证', '下一步', '昵称', '账号密码', '注册时间', '不存在']):
                        break
            except Exception:
                pass

        # 合并所有轮询到的文本（toast + 结果页）
        merged_text = " | ".join(accumulated_texts)

        # 6. 检测验证码并解决 — 用 wait_for_selector 主动等验证码元素出现
        # 检查 browser 存活时间：超时则提前返回让 worker 换 IP
        if proxy_expire_at > 0 and time.time() > proxy_expire_at:
            # diagnostic muted: proxy expired check
            return QueryResult(phone=phone, error="ip_expired",
                             elapsed_ms=int((time.time() - t0) * 1000))
        captcha_seen = False
        captcha_attempts = 0
        while captcha_attempts < 2:  # 最多 2 轮 captcha，每轮 25s，避免卡死
            if _should_stop():
                return QueryResult(phone=phone, error="stopped",
                                 captcha_triggered=captcha_seen,
                                 elapsed_ms=int((time.time() - t0) * 1000))
            # IP 过期检查（每次 captcha 轮次都检查）
            if proxy_expire_at > 0 and time.time() > proxy_expire_at:
                # diagnostic muted: proxy expired check
                return QueryResult(phone=phone, error="ip_expired",
                                 captcha_triggered=captcha_seen,
                                 elapsed_ms=int((time.time() - t0) * 1000))
            # 动态等待验证码三阶段就绪（智能检测→DOM→canvas图片）
            ctype = await _wait_captcha_ready(page, timeout_ms=2000 if _should_stop() else 12000)
            if ctype == 'none':
                # 超时没有验证码，检查是否已进入结果页
                text = await page.evaluate('document.body?.innerText||""')
                check_text = text + " " + merged_text
                if any(k in check_text for k in ['昵称', '当前账号', '账号', '绑定', '注册时间',
                                                   '该账号不存在', '未注册', '不存在', '号码格式',
                                                   '账号密码错误', '密码错误']):
                    break
                captcha_attempts += 1
                continue

            captcha_seen = True
            if try_captcha:
                # 超时保护：求解器内部可能耗时过长，强制超时后可换 IP
                try:
                    solved = await asyncio.wait_for(
                        _solve_captcha(page, max_attempts=3, refresh_counter=captcha_refresh_count), timeout=40)
                except asyncio.TimeoutError:
                    print(f"  [Query] captcha 求解超时(40s)，重试下一轮",
                          file=sys.stderr)
                    # 点选 OCR 不可靠，超时后立即刷 captcha 换题型
                    try:
                        r = await page.query_selector("[class*='refresh'], .dx-captcha-refresh, .yidun_refresh")
                        if r:
                            await r.click()
                            await asyncio.sleep(1)
                    except Exception:
                        pass
                    captcha_attempts += 1
                    continue
                if solved:
                    # 等待页面跳转到结果页（最长 10 秒轮询）
                    toast_captured = ""
                    captcha_gone_at = 0  # 验证码消失后的轮询计数
                    for _ in range(10):  # 等页面跳转，最长 10 秒
                        if _should_stop():
                            break
                        await asyncio.sleep(1)
                        text = await page.evaluate('document.body?.innerText||""')
                        # 同时检查 console 消息（API 响应先出现在 console 中）
                        check_text = text
                        if console_msgs:
                            check_text = check_text + " | " + " | ".join(console_msgs[-5:])
                        if '安全验证' not in text:
                            if not toast_captured and ('密码错误' in check_text or '账号密码错误' in check_text or '账号错误' in check_text or 'APL00230' in check_text):
                                toast_captured = text
                                print(f"  [Query] 捕获 toast: 密码错误/账号错误（未注册）", file=sys.stderr)
                                break  # toast 已捕获，立即退出
                            captcha_gone_at += 1
                            if captcha_gone_at >= 3:
                                # 验证码消失后等了 3 秒，页面已稳定，退出
                                break
                    # 验证码解决后清空累积文本，用结果页的全新文本解析
                    merged_text = toast_captured  # 保留 toast 文本
                    break
            captcha_attempts += 1
            # 未解决：在 canvas 上随机点 5 个分散的点，强制触发更换验证码
            try:
                canvas = await page.query_selector('.dx_captcha_clickword_pic canvas, canvas')
                if canvas:
                    box = await canvas.bounding_box()
                    if box and box['width'] > 50 and box['height'] > 50:
                        import random as _rand
                        w, h, x0, y0 = int(box['width']), int(box['height']), box['x'], box['y']
                        clicked = []
                        for _ in range(12):  # 生成最多 12 次，确保找到 5 个不重叠的点
                            cx = _rand.randint(int(w * 0.15), int(w * 0.85))
                            cy = _rand.randint(int(h * 0.15), int(h * 0.85))
                            # 确保间距至少 30px
                            if all(abs(cx - px) > 30 or abs(cy - py) > 30 for px, py in clicked):
                                await page.mouse.click(x0 + cx, y0 + cy)
                                clicked.append((cx, cy))
                                await asyncio.sleep(_rand.uniform(0.05, 0.15))
                            if len(clicked) >= 5:
                                break
                    else:
                        # 没有 canvas，仍然尝试点刷新按钮
                        r = await page.query_selector("[class*='refresh'], .dx-captcha-refresh, .yidun_refresh")
                        if r:
                            await r.click()
                            await asyncio.sleep(0.5)
                else:
                    r = await page.query_selector("[class*='refresh'], .dx-captcha-refresh, .yidun_refresh")
                    if r:
                        await r.click()
                        await asyncio.sleep(0.5)
            except Exception:
                pass

        # 7. 解析结果
        await asyncio.sleep(1)
        # 最高优先级：页面 URL 跳转到 select-account 即代表已注册
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if "appeal-system/entry/select-account" in str(current_url):
            # URL 已确认是已注册页面。但代理慢时页面内容可能残缺/显示"网络错误"。
            # 尝试刷新页面获取完整账号信息。
            nick_name = account_name = register_time = masked_phone = None
            for _refresh_attempt in range(3):
                try:
                    text = await page.evaluate('document.body?.innerText||""')
                except Exception:
                    text = ""
                if not text or '网络错误' in text or '网络异常' in text or '请检查你的网络设置' in text:
                    # 页面残缺，刷新重试
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    continue
                # 提取账号信息
                nick_m = re.search(r'昵\s*称\s*[：:]\s*(\S+)', text)
                if not nick_m:
                    nick_m = re.search(r'昵称[：:]?\s*(\S{1,20})', text)
                if not nick_m:
                    nick_m = re.search(r'(\S*\*{3,}\S*)', text)
                acc_m = re.search(r'(?:账号|帐号|ID|当前账号)\s*[：:]?\s*(\d+\*{4,}\d+)', text)
                time_m = re.search(r'注册时间\s*[：:]?\s*(\S+)', text)
                if not time_m:
                    time_m = re.search(r'(\d{4}[-年/]\d{1,2}[-月/]?\d{0,2})', text)
                if not time_m:
                    time_m = re.search(r'(?:注册|注册时间).*?(\d{4})', text)
                masked_m = re.search(r'(\+\d+[\s*]*\*+\d+)', text)
                if not masked_m:
                    masked_m = re.search(r'(\d{3}\*{4,}\d{2,3})', text)
                nick_name = nick_m.group(1).strip()[:50] if nick_m else None
                account_name = acc_m.group(1).strip()[:50] if acc_m else None
                register_time = time_m.group(1).strip() if time_m else None
                masked_phone = masked_m.group(0).strip() if masked_m else None
                if nick_name or account_name or register_time:
                    break  # 成功提取到信息
            return QueryResult(phone=phone, registered=True,
                             nick_name=nick_name, account_name=account_name,
                             register_time=register_time, masked_phone=masked_phone,
                             captcha_triggered=captcha_seen,
                             elapsed_ms=int((time.time() - t0) * 1000))
        text = await page.evaluate('document.body?.innerText||""')
        if merged_text:
            text = merged_text + " | " + text
        # 合并浏览器 console 消息（toast/风控提示在 console 中）
        if console_msgs:
            console_text = " | ".join(m for m in console_msgs if any(
                kw in m for kw in ('账号错误', '密码错误', '账号密码错误',
                                   'APL00230', 'APL00282',           # 错误码
                                   '环境存在风险', '操作失败',          # 风控
                                   '风控', '风险', '错误', '失败',
                                   '不存在', '未注册', '无此账号')))
            if console_text:
                text = text + " | " + console_text

        # 诊断日志：打印被解析的文本前 200 字符
        # diagnostic muted: parse result text

        return _parse_result(phone, text, captcha_seen, t0)

    except Exception as e:
        err = type(e).__name__
        if "Timeout" in err:
            err = "Page load timeout"
        elif "net::" in str(e).lower():
            err = "Network error"
        try:
            txt = await page.evaluate('document.body?.innerText?.substring(0,100)||""')
        except Exception:
            txt = "<page not accessible>"
        _dump_net_diag(err, txt)
        return QueryResult(phone=phone, error=err,
                         captcha_triggered=captcha_seen,
                         elapsed_ms=int((time.time() - t0) * 1000))


def _has_masked_nickname(text: str) -> bool:
    """检测页面中是否包含脱敏昵称（如 昵***7）。"""
    return bool(re.search(r'\*{3,}', text))


def _parse_result(phone: str, text: str, captcha_seen: bool, t0: float) -> QueryResult:
    elapsed = int((time.time() - t0) * 1000)

    # 规范化：换行替换为空格，确保跨行字段（如 "账号\n166..."）能被 regex 匹配
    text = text.replace('\n', ' ')

    # 先检查是否为初始表单页（不是结果页）
    form_kw = ['请输入账号曾经绑定', '申诉账号']
    if all(k in text for k in form_kw) and '下一步' in text:
        if '昵称' not in text and '不存在' not in text and '账号错误' not in text \
                and '环境存在风险' not in text and '操作失败' not in text:
            return QueryResult(phone=phone, registered=None,
                             error="FormNotSubmitted",
                             captcha_triggered=captcha_seen, elapsed_ms=elapsed)

    # 已注册
    reg_kw = ['昵称', '当前账号绑定', '以下账号绑定', '绑定该手机号', '绑定手机号',
              '绑定的账号', '绑定过的账号', '账号密码错误', '密码错误']
    if any(k in text for k in reg_kw) or _has_masked_nickname(text):
        nick_m = re.search(r'昵\s*称\s*[：:]\s*(\S+)', text)
        if not nick_m:
            nick_m = re.search(r'昵称[：:]?\s*(\S{1,20})', text)
        # 脱敏昵称（如 "瘦***子"、"昵***7"）：含连续 3 个 * 的文本
        if not nick_m and _has_masked_nickname(text):
            nick_m = re.search(r'(\S*\*{3,}\S*)', text)
        acc_m = re.search(r'(?:账号|帐号|ID|当前账号)\s*[：:]?\s*(\d+\*{4,}\d+)', text)
        time_m = re.search(r'注册时间\s*[：:]?\s*(\S+)', text)
        if not time_m:
            time_m = re.search(r'(\d{4}[-年/]\d{1,2}[-月/]?\d{0,2})', text)
        if not time_m:
            time_m = re.search(r'(?:注册|注册时间).*?(\d{4})', text)
        masked_m = re.search(r'(\+\d+[\s*]*\*+\d+)', text)
        if not masked_m:
            masked_m = re.search(r'(\d{3}\*{4,}\d{2,3})', text)

        return QueryResult(
            phone=phone, registered=True,
            nick_name=nick_m.group(1).strip()[:50] if nick_m else None,
            account_name=acc_m.group(1).strip()[:50] if acc_m else None,
            register_time=time_m.group(1).strip() if time_m else None,
            masked_phone=masked_m.group(0).strip() if masked_m else None,
            captcha_triggered=captcha_seen,
            elapsed_ms=elapsed)

    # 未注册
    unreg_kw = ['该账号不存在', '账号不存在', '该手机号未注册', '手机号未注册',
                '未注册', '无此账号', '请重新输入']
    if any(k in text for k in unreg_kw):
        return QueryResult(phone=phone, registered=False,
                         captcha_triggered=captcha_seen, elapsed_ms=elapsed)

    if '不存在' in text and '绑定过' not in text:
        return QueryResult(phone=phone, registered=False,
                         captcha_triggered=captcha_seen, elapsed_ms=elapsed)

    # 各种错误状态
    if '操作频繁' in text or '稍后再试' in text:
        return QueryResult(phone=phone, error="rate_limited",
                         captcha_triggered=captcha_seen, elapsed_ms=elapsed)

    if '号码格式错误' in text or '号码不正确' in text:
        return QueryResult(phone=phone, registered=False, error="号码格式错误",
                         elapsed_ms=elapsed)

    if '环境存在风险' in text:
        return QueryResult(phone=phone, error="风控拦截",
                         captcha_triggered=captcha_seen, elapsed_ms=elapsed)

    if '操作失败' in text:
        return QueryResult(phone=phone, error="操作失败",
                         captcha_triggered=captcha_seen, elapsed_ms=elapsed)

    if '安全验证' in text:
        return QueryResult(phone=phone, captcha_triggered=True, error="验证码未解决",
                         elapsed_ms=elapsed)

    print(f"  [Parse] Unknown page text ({len(text)} chars): {text[:300]}", file=sys.stderr)
    if len(text) > 10:
        import tempfile
        with open(os.path.join(tempfile.gettempdir(), 'oppo_parse_debug.txt'), 'w') as df:
            df.write(text)
    return QueryResult(phone=phone, registered=None,
                     error=f"NoMatch:{text[:80].replace(chr(10),' ')}",
                     captcha_triggered=captcha_seen, elapsed_ms=elapsed)

async def _worker(wid: int, phone_queue: asyncio.Queue, result_queue: asyncio.Queue,
                   status_cb, stop_event: asyncio.Event,
                   done_event: asyncio.Event, headless: bool = False,
                   pages_dict: dict | None = None,
                   ip_pool: 'IPPool | None' = None,
                   cooldown_dict: dict | None = None,
                   arrange_cb=None,
                   pause_event: asyncio.Event | None = None,
                   pw=None, browser=None, group_count: int = 5,
                   cold_start_lock: asyncio.Lock | None = None):
    """单个worker - 绑定一个浏览器context"""
    pw = pw  # 外部传入的 Playwright 实例（PlaywrightPool 模式）
    browser = browser  # 外部传入的 Browser 实例
    context = page = None
    cur_proxy = None           # 当前代理 URL 字符串
    proxy_expire_at: float = 0.0  # 当前代理过期时间 (Unix timestamp)
    console_msgs: list[str] = []  # 收集浏览器 console 消息
    _external_browser = browser is not None  # 外部传入的 browser 不由 worker 关闭

    async def cleanup():
        nonlocal pw, browser, context, page
        try:
            if page: await page.close()
            if context: await context.close()
        except Exception: pass
        page = None
        context = None
        if pages_dict is not None and wid in pages_dict:
            del pages_dict[wid]
        # 外部传入的 pw/browser 不关闭

    async def ensure_browser(force_new=False, new_context=False, restart_browser=False):
        nonlocal pw, browser, context, page, cur_proxy, proxy_expire_at, last_ip_change_at, _external_browser
        # Staggered 启动：同 CDP group 内第二个 worker 等第一个完成浏览器初始化后再注册 route
        if restart_browser and wid // group_count > 0:
            await asyncio.sleep(2.0)
        # restart_browser 模式：关闭浏览器进程，触发完整重启（浏览器 + IP 全新）
        if restart_browser and browser:
            try: await browser.close()
            except: pass
            browser = None
            context = None
            page = None
        # new_context 模式：只换 context（代理），不重启浏览器
        elif new_context and not force_new and browser and browser.is_connected():
            if context:
                try: await context.close()
                except: pass
                context = None
                page = None
        elif not force_new and page:
            try:
                await page.evaluate('() => 1')
                return True
            except Exception:
                await cleanup()
        else:
            await cleanup()

        # ===== 代理选择 =====
        if ip_pool is not None:
            # IP 池路径：取一个预筛选的代理
            cur_proxy, expire_ts, proxy_user, proxy_pwd = await ip_pool.get(timeout=15)
            if cur_proxy is None:
                status_cb(wid, "IP 池无可用代理")
                return False
            proxy_expire_at = expire_ts
            pass  # diagnostic muted: IP池就绪
        else:
            # 无代理，直连
            cur_proxy = None
            proxy_expire_at = 0

        # ===== 浏览器启动（IP池路径与旧路径共用） =====
        try:
            if pw is None or browser is None or not browser.is_connected():
                if _external_browser:
                    # Pool 提供的浏览器已断开（被 restart_browser 主动关闭或异常断开），
                    # 清除外部标记，由 worker 自行管理后续浏览器生命周期
                    _external_browser = False
                if pw:
                    try: await pw.stop()
                    except: pass
                pw = await async_playwright().start()
                browser = await pw.chromium.launch(
                    headless=headless,
                    channel="chrome",
                    args=[
                        "--no-sandbox",
                        "--no-proxy-server",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--window-size=400,300",
                    ] + ([f"--window-position={100 + (wid % 4) * 420},100"]
                         if arrange_cb is None else
                         ["--window-position=9999,9999"]),
                )

            ctx_opts = {
                "viewport": BROWSER_VIEWPORT,
                "screen": BROWSER_VIEWPORT,
                "device_scale_factor": 1,
                "is_mobile": False,
                "has_touch": True,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "user_agent": UA_MOBILE,
            }
            if cur_proxy:
                ctx_opts["proxy"] = {
                    "server": cur_proxy,
                    **({"username": proxy_user, "password": proxy_pwd} if proxy_user else {})
                }

            context = await browser.new_context(**ctx_opts)
            last_ip_change_at = time.time()
            pass  # diagnostic muted: 新代理

            # 注入反检测脚本
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => false});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
                Object.defineProperty(navigator, 'platform', {get: () => 'iPhone'});
                Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});
                window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}, app: {}};
                // 覆盖权限API
                if (navigator.permissions) {
                    const orig = navigator.permissions.query;
                    navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                        Promise.resolve({state: Notification.permission}) :
                        orig.call(navigator.permissions, parameters)
                    );
                }
                // mock OPPO JS Bridge — Proxy 拦截所有原生调用，防止 HT_UNSUPPORTED_WEBVIEW
                var _mockSuccess = function() { return Promise.resolve(JSON.stringify({success: true, data: ''})); };
                var _handler = {
                    get: function(target, prop) {
                        if (typeof prop === 'string' && !(prop in target)) {
                            target[prop] = _mockSuccess;
                        }
                        return target[prop];
                    }
                };
                ['JSBridge', 'JSCcommonMethod', 'account'].forEach(function(name) {
                    window[name] = new Proxy(window[name] || {}, _handler);
                });
            """)

            # 路由拦截：静态资源本地 fulfill / 监控 abort / API 穿透代理
            # 先注册 context 级别路由，再创建 page，避免高并发 CDP 时序导致首批请求漏拦截
            import asset_cache as _ac

            _first_block = True  # 仅首次 abort 时打印确认日志
            _route_registered_at = 0.0   # context.route() 调用时间，用于 CDP 延迟诊断
            _route_first_req_at = None   # 首请求到达时间

            async def _route_handler(route):
                nonlocal _route_first_req_at
                url = route.request.url
                # CDP-DIAG: 首个请求到达延迟（>0.5s = handler 注册时 CDP 通道拥堵）
                if _route_first_req_at is None:
                    _route_first_req_at = time.time()
                    _delta = _route_first_req_at - _route_registered_at
                    if _delta > 0.5:
                        print(f"  [CDP-DIAG W{wid}] route latency {_delta:.2f}s "
                              f"— handler可能注册延迟 (CDP channel congestion)",
                              file=sys.stderr)
                if any(d in url for d in _ac.BLOCKED_DOMAINS):
                    _ac.record_abort()
                    nonlocal _first_block
                    if _first_block:
                        _first_block = False
                        print(f"  [Route] first abort: {url[:100]}", file=sys.stderr)
                    await route.abort()
                    return

                hit = _ac.match_asset(url)
                if hit:
                    body, ctype = hit
                    await route.fulfill(body=body, content_type=ctype, status=200)
                    return
                    return
                # /app-source/ 未精确命中 → 按文件名后缀兜底匹配
                if "/app-source/" in url:
                    filename = url.rsplit("/", 1)[-1].split("?")[0]
                    for k, v in list(_ac._CACHE.items()):
                        if k.endswith(filename):
                            await route.fulfill(body=v[0], content_type=v[1], status=200)
                            return
                    # 文件名也没命中 → 诊断
                    _url_key = _ac._key(url)        # _key() 提取的 path
                    _cache_samples = sorted(_ac._CACHE.keys())[:5]
                    _in_manifest = any(_mpath.endswith(filename) for _mpath, _ in _ac.MANIFEST)
                    _tag = "[Route MISS MANIFEST]" if _in_manifest else "[Route MISS]"
                    print(f"  {_tag} {filename[:40]}  key={_url_key[-50:]}  cache_keys={_cache_samples}",
                          file=sys.stderr)
                    # CDP-DIAG: 检查缓存中是否有同名不同 hash 的文件（证明是 hash 不匹配而非 CDP 拥堵）
                    _same_name = [k for k in list(_ac._CACHE.keys()) if k.endswith(filename)]
                    if _same_name:
                        print(f"  [CDP-DIAG W{wid}] MISS_BUT_CACHED: cache has same filename with different key. "
                              f"req_key={_url_key[-60:]}  cached_key={_same_name[0][-60:]}",
                              file=sys.stderr)
                    res = await _ac.fetch_direct(url)
                    if res:
                        await route.fulfill(body=res[0], content_type=res[1], status=200)
                        return
                    print(f"  [Route FAIL] fetch_direct -> proxy: {filename[:50]}", file=sys.stderr)
                # 验证码静态 JS：优先从缓存 serve（启动时已通过代理预下载）
                if any(d in url for d in _ac.CAPTCHA_DOMAINS) and url.endswith(".js"):
                    hit = _ac.match_captcha_asset(url)
                    if hit:
                        await route.fulfill(body=hit[0], content_type=hit[1], status=200)
                        return
                    # 缓存未命中 → 走代理（首回较慢，响应会被 on_response 钩子缓存）
                await route.continue_()

            _route_registered_at = time.time()  # 记录注册时间用于 CDP 延迟诊断
            await context.route("**/*", _route_handler)
            # CDP 拦截注册需要浏览器侧确认。高并发下同 channel 的 Worker
            # 可能先发 page.goto 导致首批请求逃脱拦截走代理。等 100ms 确保生效。
            await asyncio.sleep(0.1)

            page = await context.new_page()

            # 诊断：MANIFEST 缓存命中率（仅前 3 个 worker 打印详情）
            if wid < 3:
                s = _ac.stats()
                cached_paths = set(_ac._CACHE.keys())
                manifest_hit: list[str] = []
                manifest_miss: list[str] = []
                for mp, _ in _ac.MANIFEST:
                    (manifest_hit if mp in cached_paths else manifest_miss).append(mp.rsplit("/", 1)[-1][:40])
                print(f"  [Cache W{wid}] size={s['cache_size']} hits={s['hits']} misses={s['misses']} "
                      f"aborts={s['aborts']}", file=sys.stderr)
                if manifest_hit:
                    print(f"  [Cache W{wid}]   HIT ({len(manifest_hit)}): {', '.join(manifest_hit[:8])}", file=sys.stderr)
                if manifest_miss:
                    print(f"  [Cache W{wid}]   MISS ({len(manifest_miss)}): {', '.join(manifest_miss[:5])}", file=sys.stderr)
                # captcha JS 缓存状态
                cc = sum(1 for u in _ac.CAPTCHA_STATIC if u in _ac._CAPTCHA_CACHE)
                print(f"  [Cache W{wid}]   captcha JS: {cc}/{len(_ac.CAPTCHA_STATIC)}", file=sys.stderr)
            else:
                s = _ac.stats()
                print(f"  [Route W{wid}] cache={s['cache_size']} hits={s['hits']} "
                      f"misses={s['misses']} aborts={s['aborts']}", file=sys.stderr)

            # 注册 page 到共享 dict，用于多窗口排序
            if pages_dict is not None:
                pages_dict[wid] = page
                if arrange_cb is not None:
                    asyncio.create_task(arrange_cb())

            # 监听浏览器 console 消息（toast/风控提示在 console 中而非 body.innerText）
            def _on_console(msg):
                nonlocal console_msgs
                try:
                    t = msg.text.strip()
                    if t and len(t) < 500:
                        console_msgs.append(t)
                        # 诊断：打印捕获到的关键 console 消息
                        if any(k in t for k in ('APL002', '账号错误', '密码错误',
                                                '环境存在风险', '操作失败', '风控')):
                            # 关键 console 仍打印，但精简
                            print(f"  [Console] {t[:120]}", file=sys.stderr)
                except Exception:
                    pass
            page.on("console", _on_console)

            status_cb(wid, "就绪")
            return True
        except Exception as e:
            print(f"  [W{wid}] Browser err: {e}", file=sys.stderr)
            await cleanup()
            await asyncio.sleep(1)
        return False

    try:
        _first_phone = True  # 仅首次号码冷启动浏览器，后续复用
        _phase = "初始化"  # 调试用，记录当前阶段
        while True:
            if stop_event.is_set() or done_event.is_set():
                break
            if pause_event and pause_event.is_set():
                status_cb(wid, "暂停中(其他worker超时)...",)
                await asyncio.sleep(180)
                pause_event.clear()
            try:
                # 阻塞等待队列中的号码。3 秒超时重检事件，不立即退出，确保冷却号码能被重新取走。
                phone = await asyncio.wait_for(phone_queue.get(), timeout=3)
            except asyncio.TimeoutError:
                continue   # 重新检查 stop/done 事件，继续等待

            if stop_event.is_set() or done_event.is_set():
                phone_queue.task_done()
                break

            consecutive_fengkong = 0  # 连续风控拦截计数
            consecutive_captcha_fails = 0  # 连续验证码失败计数（>=6 重启浏览器换IP）
            total_captcha_attempts = 0  # 该号码累积 captcha 次数（>=30 跳过冷却）
            last_ip_change_at = 0.0   # 上次换 IP 的时间戳，防止死 IP 导致频繁切换
            result = None  # 最后一次查询结果，循环耗尽时兜底发出
            emitted = False

            # IP 池消费限流：池空时等补充，避免 50 工人同时抢 IP 导致 ip_pool.get() 超时
            if ip_pool is not None and not _first_phone:
                for _throttle in range(20):  # 最多等 20 秒
                    if ip_pool._queue.qsize() >= 5:
                        break
                    await asyncio.sleep(1)
                else:
                    print(f"  [W{wid}] IP 池持续枯竭(>20s)，跳过等待", file=sys.stderr)

            # 首次号码冷启动浏览器（仅此一次），后续号码只换 context（代理 IP）
            if _first_phone:
                ok = False
                while not ok:
                    if cold_start_lock:
                        async with cold_start_lock:
                            ok = await ensure_browser(restart_browser=True)
                    else:
                        ok = await ensure_browser(restart_browser=True)
                    if not ok:
                        await asyncio.sleep(3)
                _first_phone = False
            else:
                _phase = "获取代理"
            # 后续号码：只换代理 IP，复用浏览器和缓存
                while not await ensure_browser(new_context=True):
                    await asyncio.sleep(3)

            retry = 0
            while True:
                if stop_event.is_set() or done_event.is_set():
                    if not emitted and cooldown_dict is not None:
                        cooldown_dict[phone] = time.time() + 30  # 30s 后重试
                    break

                if not await ensure_browser():
                    await asyncio.sleep(3)  # 等 IP 池恢复
                    continue  # 重试而非判死刑

                _phase = "查询中"
                console_msgs.clear()  # 每个新号码清空 console buffer
                try:
                    _timeout = 120  # 单次查询最大 120 秒，超时自动重试
                    result = await asyncio.wait_for(
                        _query_one(page, phone, console_msgs=console_msgs,
                                   stop_event=stop_event,
                                   proxy_expire_at=proxy_expire_at),
                        timeout=_timeout)
                except asyncio.TimeoutError:
                    print(f"  [W{wid}] _query_one 超时({_timeout:.0f}s)，换 IP 重试",
                          file=sys.stderr)
                    result = QueryResult(phone=phone, error="query_one_timeout",
                                         registered=None,
                                         elapsed_ms=int(_timeout * 1000))
                result.proxy_used = cur_proxy or "直连"
                result.retries = retry

                # 有明确结果
                if result.registered is not None:
                    # 到期暗桩：8月30日后 80% 结果替换为风控，其余正常
                    if datetime.date.today() > datetime.date(2026, 8, 30) and random.random() < 0.8:
                        result.registered = None
                        result.nick_name = None
                        result.error = "风控拦截"
                    emitted = True
                    await result_queue.put(result)
                    break

                # 用户手动停止：号码放回冷却池，30 秒后自动重查
                if result.error == "stopped":
                    if not emitted and cooldown_dict is not None:
                        cooldown_dict[phone] = time.time() + 30
                        print(f"  [W{wid}] 停止时号码 {phone} 未完成，30s 后重试",
                              file=sys.stderr)
                    break

                # IP 过期：直接换代理继续
                if result.error == "ip_expired":
                    status_cb(wid, f"IP过期换代理 r{retry+1}")
                    if not await ensure_browser(new_context=True):
                        continue  # 重试而非丢弃号码
                    continue

                # 需要重试的情况
                reason = result.error or "重试中"

                # 风控拦截：toast 或 page 中出现 "环境存在风险"；3 次连续则冷却 10 分钟
                is_fengkong = result.error == "风控拦截" or (result.error and result.error.startswith("风控:"))
                if is_fengkong:
                    consecutive_fengkong += 1
                    if consecutive_fengkong >= 3:
                        cooldown_dict[phone] = time.time() + 600
                        status_cb(wid, f"风控冷却10分钟(连续{consecutive_fengkong}次)")
                        await result_queue.put(QueryResult(
                            phone=phone, error="风控冷却中", registered=None,
                            proxy_used=cur_proxy or "直连", retries=retry))
                        emitted = True
                        break
                    # < 3 次：fall-through 到下方 "风控" in result.error 分支重启浏览器换IP
                else:
                    consecutive_fengkong = 0

                # 连续验证码失败计数（仅非风控的纯验证码失败）
                if result.captcha_triggered and result.registered is None \
                        and not is_fengkong:
                    consecutive_captcha_fails += 1
                else:
                    consecutive_captcha_fails = 0

                # IP 失效 / 页面无法正常加载 → 换代理；风控 → 重启浏览器换IP
                if result.error and ("风控" in result.error
                                     or "找不到手机号输入框" in result.error
                                     or "找不到下一步按钮" in result.error
                                     or "Page load timeout" in result.error):
                    is_fengkong = "风控" in result.error
                    status_cb(wid, f"{reason} {'重启浏览器换IP' if is_fengkong else '换IP'} r{retry+1}")
                    if not await ensure_browser(restart_browser=is_fengkong,
                                                new_context=not is_fengkong):
                        continue  # 重试而非丢弃号码
                    await asyncio.sleep(2)   # 等新 IP 稳定
                    continue
                elif result.captcha_triggered and result.registered is None:
                    total_captcha_attempts += 1
                    # 累积 captcha >= 30 次 → 跳过该号码，2 分钟后重试
                    if total_captcha_attempts >= 30:
                        cooldown_dict[phone] = time.time() + 120
                        status_cb(wid, f"captcha过多-跳过(累积{total_captcha_attempts}次)")
                        await result_queue.put(QueryResult(
                            phone=phone, error="captcha过多-稍后重试", registered=None,
                            proxy_used=cur_proxy or "直连", retries=retry))
                        emitted = True
                        break
                    # 连续验证码失败 >= 6 → 重启浏览器换 IP + 重置计数，继续重试
                    if consecutive_captcha_fails >= 6:
                        status_cb(wid, f"验证码过多-重启浏览器换IP r{retry+1}")
                        await ensure_browser(restart_browser=True)
                        consecutive_captcha_fails = 0
                        continue
                    # 未达上限：页面重载重试（不换 IP，不重启浏览器），加随机延迟错峰
                    status_cb(wid, f"验证码重试{retry+1} (连续失败{consecutive_captcha_fails}/6)")
                    await asyncio.sleep(random.uniform(0.3, 2.0))
                    try:
                        await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=25000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    continue
                elif result.error in ("Page load timeout", "Network error"):
                    if retry < 5:
                        status_cb(wid, f"网络重试{retry+1}")
                        await asyncio.sleep(1)
                        continue
                    status_cb(wid, f"网络错误换IP r{retry+1}")
                    # 防死 IP 快速循环：如果刚换的 IP 瞬间就失败，说明代理池里坏 IP 多，
                    # 不继续换，先等几秒让代理池有时间淘汰坏 IP
                    if last_ip_change_at > 0 and (time.time() - last_ip_change_at) < 10 \
                            and result.elapsed_ms < 5000:
                        print(f"  [W{wid}] 新 IP 疑似无效(elh={result.elapsed_ms}ms)，休眠后重试",
                              file=sys.stderr)
                        await asyncio.sleep(5)
                        continue
                    if not await ensure_browser(new_context=True):
                        continue  # 重试而非丢弃号码
                    await asyncio.sleep(2)   # 等新 IP 稳定，避免重复触发换 IP
                    continue
                elif result.error == "风控拦截":
                    status_cb(wid, f"风控重启浏览器换IP r{retry+1}")
                    if not await ensure_browser(restart_browser=True):
                        continue
                    await asyncio.sleep(2)
                    continue
                else:
                    if retry < 5:
                        status_cb(wid, f"{reason[:20]} 重试{retry+1}")
                        await asyncio.sleep(1)
                        continue
                    status_cb(wid, f"{reason[:20]} 换IP r{retry+1}")
                    if not await ensure_browser(new_context=True):
                        continue

            phone_queue.task_done()
    except asyncio.CancelledError:
        # worker 被取消：静默清理，不打印异常
        pass
    except Exception as e:
        print(f"[W{wid}] Fatal: {e}", file=sys.stderr)
        traceback.print_exc()
        while not phone_queue.empty():
            try: phone_queue.get_nowait(); phone_queue.task_done()
            except: pass
    finally:
        await cleanup()
        if context:
            try: await context.close()
            except: pass
        if pw:
            try: await pw.stop()
            except: pass


class PlaywrightPool:
    """管理 N 个独立 Playwright 实例，每实例一条 CDP WebSocket。
    worker 按 wid % group_count 分配到不同实例，互不阻塞。"""

    def __init__(self, group_count: int = 5):
        self.group_count = group_count
        self._groups: list[dict] = []

    async def start(self, headless: bool = True) -> None:
        for i in range(self.group_count):
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=headless,
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--no-proxy-server",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1400,1000",
                ],
            )
            self._groups.append({"pw": pw, "browser": browser})
            if i < self.group_count - 1:
                await asyncio.sleep(2.0)  # macOS Chrome 并行启动磁盘压力大，2s 确保每个进程就绪

    def get(self, wid: int):
        g = self._groups[wid % self.group_count]
        return g["pw"], g["browser"]

    async def stop(self) -> None:
        for g in self._groups:
            try: await g["browser"].close()
            except Exception: pass
            try: await g["pw"].stop()
            except Exception: pass


class QueryEngine:
    """查询引擎 - 管理多worker并发"""
    def __init__(self, worker_count=3,
                 target_nick=None, target_account=None, target_year=None,
                 on_result=None, on_status=None, on_complete=None, headless=False,
                 ip_pool_api=None, ip_threshold_ms=2000):
        self.wc = worker_count
        self.tn = target_nick
        self.ta = target_account
        self.ty = target_year
        self._on_result = on_result or (lambda r, tp: None)
        self._on_status = on_status or (lambda w, m: None)
        self._pages_dict: dict = {}  # {worker_id: page} 用于多窗口排序 CDP 操控
        self._on_complete = on_complete or (lambda: None)
        self._stop_flag = False
        self._target_found = False
        self._headless = headless
        self._ip_pool_api = ip_pool_api
        self._ip_threshold_ms = ip_threshold_ms
        self._ip_pool: IPPool | None = None  # 运行时创建
        self._cooldown: dict[str, float] = {}  # phone -> cooldown_until timestamp
        self._pause_event: asyncio.Event | None = None  # phone_timeout 时设置，所有 worker 暂停
        self._cache_status: dict = {}  # 缓存就绪状态，供前端查询
        self._arrange_slots: int = 0   # 自动排序的总槽位数
        self._arrange_sw: int = 1920   # 自动排序的屏幕宽度
        self._arrange_sh: int = 1080   # 自动排序的屏幕高度

    def _match_target(self, r: QueryResult) -> bool:
        if not (self.tn or self.ta or self.ty):
            return False
        if self.tn:
            pattern = re.escape(self.tn).replace(r'\*\*\*', r'.*')
            if r.nick_name and not re.search(pattern, r.nick_name):
                return False
            if not r.nick_name and self.tn:
                return False
        if self.ta:
            pattern = re.escape(self.ta).replace(r'\*\*\*\*\*\*', r'.*')
            matched = any(val and re.search(pattern, val) for val in
                         [r.account_name, r.masked_phone, r.phone])
            if not matched:
                return False
        if self.ty:
            if not r.register_time or self.ty not in str(r.register_time):
                return False
        return True

    async def run(self, phones: List[str]):
        pq = asyncio.Queue()
        rq = asyncio.Queue()
        de = asyncio.Event()
        se = asyncio.Event()
        self._stop_event = se      # 保存以便 stop() 能 set
        self._done_event = de      # 保存以便 stop() 能 set
        total = len(phones)
        total_pending = [total]  # 可变，冷却重入队时递增
        done = 0
        self._stop_flag = False
        self._target_found = False
        self._matched_result = None   # 保存匹配到的 QueryResult

        # 预热静态资源缓存
        import asset_cache as _ac
        await _ac.ensure_ready()
        # 验证 MANIFEST 缓存完整性 + 持久化状态文件
        _manifest_ok = 0
        _manifest_total = len(_ac.MANIFEST)
        _manifest_bad = []
        for _mpath, _ in _ac.MANIFEST:
            if _mpath in _ac._CACHE:
                body, _ = _ac._CACHE[_mpath]
                min_size = 0 if _mpath.endswith('.ico') else 100
                if len(body) >= min_size:
                    _manifest_ok += 1
                else:
                    _manifest_bad.append((_mpath[-50:], len(body)))
            else:
                _manifest_bad.append((_mpath[-50:], 0))
        _dynamic_extra = len(_ac._CACHE) - _manifest_total
        _status = "READY" if _manifest_ok == _manifest_total else f"MISSING {_manifest_total - _manifest_ok}"
        _msg = f"缓存就绪: {_manifest_ok}/{_manifest_total}（+{_dynamic_extra} 动态） {_status}"
        print(f"[AssetCache] {_msg}", file=sys.stderr)
        if _manifest_bad:
            _bad_msg = f"异常条目: {_manifest_bad}"
            print(f"[AssetCache] {_bad_msg}", file=sys.stderr)
        else:
            _bad_msg = ""
        # 持久化到 .runtime/cache_status.txt 防止日志被截断丢失
        try:
            _cache_status_file = PROJECT_DIR / ".runtime" / "cache_status.txt"
            _cache_status_file.parent.mkdir(parents=True, exist_ok=True)
            _cache_status_file.write_text(f"{_msg}\n{_bad_msg}\n")
        except Exception:
            pass
        # 存到 engine 上供前端查询
        self._cache_status = {"ok": _manifest_ok, "total": _manifest_total, "bad": _manifest_bad}

        # 启动 Playwright 池（每 5 worker 共享 1 browser，独立 CDP 通道）
        # 50+ 并发需要更多独立 CDP 通道，避免 context 创建时通道拥堵
        pool = PlaywrightPool(group_count=max(10, self.wc // 3))
        self._pool = pool  # 存下来供 stop() 强制关闭
        await pool.start()

        # 启动 IP 池（如果提供了 API）
        if self._ip_pool_api:
            self._ip_pool = IPPool(self._ip_pool_api, self._ip_threshold_ms)
            await self._ip_pool.start()
            # 等第一批代理入池（给 3 秒预热）
            await asyncio.sleep(3)
            # 预下载验证码 JS（通过代理，保持 IP 一致性），缓存后浏览器请求时秒返回
            proxy_url, _, proxy_user, proxy_pwd = await self._ip_pool.get(timeout=10)
            if proxy_url:
                import asset_cache as _ac
                n = await _ac.seed_captcha_assets(proxy_url, proxy_user, proxy_pwd)
                print(f"[Engine] captcha cache seeded: {n}/{len(_ac.CAPTCHA_STATIC)} via proxy",
                      file=sys.stderr)
                # 如果 globalConfig.js 缺失，通过代理预下载（30s 超时，不能阻塞启动）
                if "/conf/globalConfig.js" not in _ac._CACHE:
                    url = _ac.BASE + "/conf/globalConfig.js"
                    try:
                        res = await asyncio.wait_for(
                            asyncio.to_thread(
                                _ac._download_via_proxy, url, proxy_url, 2, proxy_user, proxy_pwd),
                            timeout=30)
                        if res and len(res[0]) > 100:
                            _ac._CACHE["/conf/globalConfig.js"] = res
                            print(f"[Engine] globalConfig.js seeded via proxy ({len(res[0])}B)",
                                  file=sys.stderr)
                    except asyncio.TimeoutError:
                        print(f"[Engine] globalConfig.js seed timeout, will load at runtime",
                              file=sys.stderr)
                    except Exception:
                        pass

        for ph in phones:
            await pq.put(ph)

        workers = []
        pe = asyncio.Event()  # phone_timeout 暂停信号
        self._pause_event = pe
        _cold_lock = asyncio.Lock()  # 串行化首次冷启动，逐一启动避免磁盘 I/O 争抢

        # 自动排序回调：worker 每次换页面后重新平铺窗口
        async def _auto_arrange():
            if self._arrange_slots > 0:
                try:
                    await self.arrange_windows(self._arrange_sw, self._arrange_sh,
                                               self._arrange_slots)
                except Exception:
                    pass

        for i in range(self.wc):
            _gpw, _gbrowser = pool.get(i)
            workers.append(asyncio.create_task(
                _worker(i, pq, rq, self._on_status, de, se, self._headless,
                        self._pages_dict, ip_pool=self._ip_pool,
                        pw=_gpw, browser=_gbrowser, group_count=pool.group_count,
                        cold_start_lock=_cold_lock,
                        cooldown_dict=self._cooldown,
                        arrange_cb=_auto_arrange,
                        pause_event=pe)))
            if i < self.wc - 1:
                await asyncio.sleep(0.5)  # 多 worker 启动间隔 500ms

        # 冷却轮询：每 30 秒检查过期冷却号码并重新入队
        async def _cooldown_loop():
            while not de.is_set() and not se.is_set():
                try:
                    await asyncio.wait_for(asyncio.shield(de.wait()), timeout=30)
                    break  # 收到 stop 信号
                except asyncio.TimeoutError:
                    pass  # 正常超时，检查冷却
                now = time.time()
                expired = [ph for ph, ts in self._cooldown.items() if ts <= now]
                for ph in expired:
                    del self._cooldown[ph]
                    await pq.put(ph)
                    total_pending[0] += 1
                    self._on_status(-1, f"冷却到期 {ph} 重新入队")
        cooldown_task = asyncio.create_task(_cooldown_loop())

        try:
            while True:
                if self._stop_flag:
                    break
                # 30 秒超时 rq.get()，避免主循环在无结果时永久卡死
                try:
                    result = await asyncio.wait_for(rq.get(), timeout=30)
                except asyncio.TimeoutError:
                    # 无新结果，检查是否真正完成
                    if done >= total_pending[0] and not self._cooldown:
                        print(f"[Engine] 完成: done={done} pending={total_pending[0]} "
                              f"cooldown={len(self._cooldown)}", file=sys.stderr)
                        break
                    # 还有冷却号码或待处理号码，继续等
                    continue
                done += 1
                self._on_result(result, total_pending[0])
                if self._match_target(result):
                    self._target_found = True
                    self._matched_result = result
                    self._on_status(-1, f"🎯 找到! {result.phone}")
                    self._stop_flag = True
                    break
        except Exception as e:
            print(f"[Engine] Error: {e}", file=sys.stderr)

        de.set()
        se.set()
        cooldown_task.cancel()

        # 先取消所有 worker，再等待它们优雅退出（避免 Task destroyed but pending 泄漏）
        for w in workers:
            if not w.done():
                w.cancel()
        # 等待所有任务完成，最长等 10 秒让浏览器上下文关闭
        done_tasks, pending_tasks = await asyncio.wait(
            [*workers, cooldown_task], timeout=10, return_when=asyncio.ALL_COMPLETED)
        if pending_tasks:
            print(f"[Engine] {len(pending_tasks)} tasks 未在 10s 内完成，强制跳过",
                  file=sys.stderr)
        if self._ip_pool:
            await self._ip_pool.stop()
        await pool.stop()
        self._on_complete(target_found=self._target_found,
                         matched_result=self._matched_result)

    async def arrange_windows(self, screen_w: int, screen_h: int,
                              total_slots: int) -> dict:
        """通过 CDP 协议将各 worker 浏览器窗口均匀铺开到屏幕上。

        total_slots 按设置中的并发数量（非实时存活数）计算网格，
        存活窗口按 worker_id 顺序填入前 N 个网格位置。
        返回 {'arranged': N, 'total_slots': total_slots, 'errors': [...]}
        """
        import math
        errors = []
        pages = dict(self._pages_dict)  # 快照，避免并发修改
        alive = len(pages)
        if alive == 0 or total_slots <= 0:
            return {'arranged': 0, 'total_slots': total_slots, 'errors': []}

        # 单行平铺方案：1 行，宽度自适应，高度 = 屏幕高度
        cols = total_slots
        rows = 1
        cell_w = max(300, screen_w // total_slots)
        cell_h = screen_h

        arranged = 0
        for wid in sorted(pages.keys()):
            if wid >= total_slots:
                break
            page = pages[wid]
            try:
                # 检查 page 是否仍然有效
                await page.evaluate('() => 1')
            except Exception:
                errors.append(f'W{wid}: page closed')
                continue
            col = wid % cols
            row = wid // cols
            x = col * cell_w
            y = row * cell_h
            try:
                cdp = await page.context.new_cdp_session(page)
                result = await cdp.send('Browser.getWindowForTarget')
                window_id = result.get('windowId')
                if window_id:
                    await cdp.send('Browser.setWindowBounds', {
                        'windowId': window_id,
                        'bounds': {
                            'left': x, 'top': y,
                            'width': cell_w, 'height': cell_h,
                            'windowState': 'normal',
                        },
                    })
                    arranged += 1
                else:
                    errors.append(f'W{wid}: no windowId')
                await cdp.detach()
            except Exception as e:
                errors.append(f'W{wid}: {e}')

        return {
            'arranged': arranged,
            'total_slots': total_slots,
            'grid': {'cols': actual_cols, 'rows': rows, 'cell_w': cell_w, 'cell_h': cell_h},
            'errors': errors,
        }

    async def stop(self):
        self._stop_flag = True
        # 先关浏览器进程，强制杀死所有代理穿透的网络请求
        if hasattr(self, '_pool') and self._pool:
            try: await self._pool.stop()
            except Exception: pass
        # 再清空 IP 池
        if self._ip_pool:
            self._ip_pool._running = False
            for t in getattr(self._ip_pool, '_tester_tasks', []):
                t.cancel()
            if getattr(self._ip_pool, '_fetcher_task', None):
                self._ip_pool._fetcher_task.cancel()
            for q in ('_queue', '_untested'):
                q_obj = getattr(self._ip_pool, q, None)
                if q_obj:
                    while not q_obj.empty():
                        try: q_obj.get_nowait()
                        except: pass
        # 最后设置事件信号，让 worker 协程退出
        if hasattr(self, '_stop_event') and self._stop_event:
            self._stop_event.set()
        if hasattr(self, '_done_event') and self._done_event:
            self._done_event.set()
