#!/usr/bin/env python3
"""OPPO 查询 Web 界面 v5 - 使用 v31 引擎"""
from __future__ import annotations
import asyncio, csv, datetime, io, json, os, socket, sys, threading, time, traceback
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO

sys.path.insert(0, str(Path(__file__).parent))
from query_engine_v31 import QueryEngine, QueryResult

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_RESULTS_FILE = Path(__file__).parent / ".runtime" / "results_backup.json"
_PHONES_FILE = Path(__file__).parent / ".runtime" / "phones_backup.json"


def _load_results():
    """启动时加载持久化的查询结果"""
    results = []
    try:
        if _RESULTS_FILE.exists():
            data = json.loads(_RESULTS_FILE.read_text(encoding="utf-8"))
            for item in data:
                r = QueryResult(phone=item.get("phone", ""))
                r.registered = item.get("registered")
                r.nick_name = item.get("nick_name")
                r.account_name = item.get("account_name")
                r.masked_phone = item.get("masked_phone")
                r.register_time = item.get("register_time")
                r.error = item.get("error")
                r.retries = item.get("retries", 0)
                r.captcha_triggered = item.get("captcha_triggered", False)
                r.elapsed_ms = item.get("elapsed_ms", 0)
                r.proxy_used = item.get("proxy_used")
                results.append(r)
    except Exception as e:
        print(f"[persist] load error: {e}", file=sys.stderr)
    return results


def _save_results(results: list):
    """每次结果更新后持久化到本地"""
    try:
        _RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = [{
            "phone": r.phone,
            "registered": r.registered,
            "nick_name": r.nick_name,
            "account_name": r.account_name,
            "masked_phone": r.masked_phone,
            "register_time": r.register_time,
            "error": r.error,
            "retries": r.retries,
            "captcha_triggered": r.captcha_triggered,
            "elapsed_ms": r.elapsed_ms,
            "proxy_used": r.proxy_used,
        } for r in results]
        _RESULTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[persist] save error: {e}", file=sys.stderr)

_state = {
    "loop": None,
    "total": 0,
    "done": 0,
    "results": _load_results(),  # 启动时恢复上次的查询结果
    "running": False,
    "engine": None,
}
_lock = threading.Lock()


def _get_loop():
    with _lock:
        if _state["loop"] is None or _state["loop"].is_closed():
            loop = asyncio.new_event_loop()
            _state["loop"] = loop
            t = threading.Thread(target=loop.run_forever, daemon=True)
            t.start()
            print("[Web] Event loop started", file=sys.stderr)
        return _state["loop"]


def _log(msg: str):
    print(f"[Web {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
    sys.stderr.flush()


async def _push_pool_status(engine, _log_fn):
    """每 3 秒推送 IP 池状态到前端"""
    import asyncio as _asyncio
    while True:
        await _asyncio.sleep(3)
        try:
            pool = engine._ip_pool
            if pool is None:
                continue
            stats = pool.stats
            stats["active_workers"] = len(engine._pages_dict)
            socketio.emit("pool_status", stats)
        except Exception:
            pass


@app.route("/")
def index():
    # 崩溃恢复：页面加载时把未完成的号码直接注入输入框
    pending_phones = []
    try:
        if _PHONES_FILE.exists():
            phones = json.loads(_PHONES_FILE.read_text(encoding="utf-8"))
            completed = {r.phone for r in _state["results"]}
            pending_phones = [p for p in phones if p not in completed]
    except Exception:
        pass
    return render_template("index.html", pending_phones=pending_phones)


@app.route("/api/generate_phones", methods=["POST"])
def generate_phones():
    """生成指定数量的随机中国手机号（仅移动/联通老号段，不含电信/虚拟/新号段）"""
    import random as _random
    data = request.get_json() or {}
    count = max(1, min(int(data.get("count", 10)), 2000))
    # 移动老号段
    prefixes = [
        "134","135","136","137","138","139",
        "150","151","152","157","158","159",
        "182","183","184","187","188",
        # 联通老号段
        "130","131","132",
        "155","156",
        "185","186",
    ]
    phones = []
    for _ in range(count * 2):  # 生成 2x 确保去重后有足够数量
        p = _random.choice(prefixes) + "".join(_random.choices("0123456789", k=8))
        if p not in phones:
            phones.append(p)
        if len(phones) >= count:
            break
    _log(f"Generated {len(phones)} random phones")
    return jsonify({"count": len(phones), "phones": phones})


@app.route("/api/import", methods=["POST"])
def import_phones():
    if "file" not in request.files:
        return jsonify({"error": "没有文件"}), 400
    file = request.files["file"]
    try:
        content = file.read().decode("utf-8")
        phones = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for p in line.replace("\t", ",").split(","):
                p = p.strip().strip('"\'').strip()
                if p.isdigit() and len(p) >= 10 and p not in phones:
                    phones.append(p)
        _log(f"Imported {len(phones)} phones")
        return jsonify({"count": len(phones), "phones": phones})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/start", methods=["POST"])
def start_query():
    if _state.get("running"):
        return jsonify({"error": "查询正在进行中"}), 400

    data = request.get_json()
    phones = data.get("phones", [])
    concurrency = int(data.get("concurrency", 3))
    target_nick = data.get("target_nick", "").strip() or None
    target_account = data.get("target_account", "").strip() or None
    target_year = data.get("target_year", "").strip() or None
    headless = data.get("headless", False)  # default: 非无头
    ip_pool_api = data.get("ip_pool_api", "").strip() or None
    ip_threshold_ms = int(data.get("ip_threshold_ms", 2000))

    if not phones:
        return jsonify({"error": "没有手机号"}), 400

    _state["total"] = len(phones)
    _state["done"] = 0
    _state["results"].clear()
    _save_results(_state["results"])  # 持久化空列表
    # 保存原始号码列表，用于崩溃后恢复
    try:
        _PHONES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PHONES_FILE.write_text(json.dumps(phones, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    _state["running"] = True

    _log(f"Start: {len(phones)} phones, {concurrency} workers, headless={headless}")

    def emit_result(r: QueryResult, total_pending: int):
        _state["results"].append(r)
        _state["done"] += 1
        _save_results(_state["results"])  # 每完成一个就持久化
        try:
            socketio.emit("result", {
                "phone": r.phone,
                "registered": r.registered,
                "masked": r.masked_phone or "",
                "captcha": r.captcha_triggered,
                "error": r.error or "",
                "elapsed_ms": r.elapsed_ms,
                "proxy": r.proxy_used[-40:] if r.proxy_used else "直连",
                "retries": r.retries,
                "account_name": r.account_name or "",
                "nick_name": r.nick_name or "",
                "register_time": r.register_time or "",
                "done": _state["done"],
                "total_pending": total_pending,
            })
        except Exception as e:
            print(f"[Web] emit error: {e}", file=sys.stderr)

    def emit_status(wid: int, msg: str):
        try:
            socketio.emit("status", {"worker": wid, "msg": msg[:80]})
        except Exception:
            pass
        _log(f"W{wid}: {msg}")

    def emit_complete(target_found=False, matched_result=None):
        reg = sum(1 for r in _state["results"] if r.registered is True)
        unreg = sum(1 for r in _state["results"] if r.registered is False)
        cap = sum(1 for r in _state["results"] if r.captcha_triggered)
        _log(f"Done: {_state['total']} phones, {reg} reg, {unreg} unreg, {cap} captcha" + 
             (" 🎯 目标已找到!" if target_found else ""))
        _state["running"] = False
        try:
            if target_found and matched_result:
                socketio.emit("found", {
                    "phone": matched_result.phone,
                    "nick_name": matched_result.nick_name or "",
                    "register_time": matched_result.register_time or "",
                    "account_name": matched_result.account_name or "",
                    "masked_phone": matched_result.masked_phone or "",
                    "done": _state["done"],
                    "total": _state["total"],
                })
            socketio.emit("complete", {
                "total": _state["total"],
                "registered": reg,
                "unregistered": unreg,
                "captcha": cap,
                "target_found": target_found,
            })
        except Exception:
            pass

    def emit_error(msg: str):
        _state["running"] = False
        try:
            socketio.emit("error", {"msg": msg})
        except Exception:
            pass

    async def _run():
        try:
            engine = QueryEngine(
                worker_count=concurrency,
                target_nick=target_nick,
                target_account=target_account,
                target_year=target_year,
                on_result=emit_result,
                on_status=emit_status,
                on_complete=emit_complete,
                headless=headless,
                ip_pool_api=ip_pool_api,
                ip_threshold_ms=ip_threshold_ms,
            )
            _state["engine"] = engine
            # 启动 IP 池状态推送（后台任务，不阻塞主流程）
            _pool_pusher = asyncio.create_task(
                _push_pool_status(engine, _log))
            await engine.run(phones)
            _pool_pusher.cancel()
            try: await _pool_pusher
            except asyncio.CancelledError: pass
        except Exception as e:
            _log(f"Engine error: {e}")
            traceback.print_exc()
            emit_error(str(e))

    loop = _get_loop()
    asyncio.run_coroutine_threadsafe(_run(), loop)
    return jsonify({"total": _state["total"], "message": f"开始查询 {_state['total']} 个号码"})


@app.route("/api/stop", methods=["POST"])
def stop_query():
    _log("Stop requested")
    engine = _state.get("engine")
    _state["running"] = False
    if engine:
        loop = _get_loop()
        asyncio.run_coroutine_threadsafe(engine.stop(), loop)
    return jsonify({"ok": True})


@app.route("/api/arrange", methods=["POST"])
def arrange_windows():
    """将各 worker 浏览器窗口均匀铺开到屏幕上。
    接收: {total_slots, screen_width, screen_height}
    total_slots = 并发窗口数（设置值），用于计算网格
    """
    engine = _state.get("engine")
    if not engine:
        return jsonify({"error": "没有正在运行的查询引擎"}), 400

    data = request.get_json() or {}
    total_slots = int(data.get("total_slots", 3))
    screen_w = int(data.get("screen_width", 1920))
    screen_h = int(data.get("screen_height", 1080))

    # 存到引擎供 worker 自动排序使用
    engine._arrange_slots = total_slots
    engine._arrange_sw = screen_w
    engine._arrange_sh = screen_h

    async def _do():
        return await engine.arrange_windows(screen_w, screen_h, total_slots)

    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(_do(), loop)
    try:
        result = future.result(timeout=15)
        _log(f"Arrange: {result['arranged']}/{result['total_slots']} windows"
             + (f", errors: {result['errors']}" if result['errors'] else ""))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "arranged": 0}), 500


@app.route("/api/export", methods=["GET"])
def export_csv():
    output = io.BytesIO()
    output.write(b'\xef\xbb\xbf')  # UTF-8 BOM，确保 Excel/浏览器正确识别中文
    # 先用 StringIO 构建 CSV 内容，再编码为 UTF-8 写入 BytesIO
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["手机号", "状态", "脱敏账号", "昵称", "注册时间", "耗时(s)", "代理", "重试", "错误"])
    for r in _state["results"]:
        st = "已注册" if r.registered is True else ("未注册" if r.registered is False else (r.error or "未知")[:24])
        w.writerow([r.phone, st, r.masked_phone or r.account_name or "",
                    r.nick_name or "", r.register_time or "",
                    round(r.elapsed_ms / 1000, 1),
                    r.proxy_used[-40:] if r.proxy_used else "",
                    r.retries, r.error or ""])
    output.write(buf.getvalue().encode("utf-8"))
    resp = Response(output.getvalue(), content_type="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''oppo_results.csv"
    return resp


def _first_available_port(preferred_port: int, host: str = "127.0.0.1") -> int:
    """Pick the configured port when possible; otherwise fall forward to avoid duplicate-start crashes."""
    for port in range(preferred_port, preferred_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port found from {preferred_port} to {preferred_port + 99}")


def main():
    host = "127.0.0.1"
    preferred_port = int(os.environ.get("OPPO_WEB_PORT", "5000"))
    port = _first_available_port(preferred_port, host=host)
    if port != preferred_port:
        print(f"[Web] Port {preferred_port} is in use, switched to {port}", file=sys.stderr)
    url = f"http://{host}:{port}"
    print(f"\n{'='*50}")
    print(f"  OPPO 查询工具 v5 (v31引擎 / AntiCAP点选拆分版)")
    print(f"  浏览器打开: {url}")
    print(f"  模式: {'无头(headless)'} - 查看首行复选框")
    print(f"{'='*50}\n")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
