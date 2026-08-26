#!/usr/bin/env python3
"""
CDP + Playwright 真浏览器滑块验证 - 修正版

基于 OPPO_Checker.exe 逆向分析：使用真浏览器运行 greenseer SDK
"""
from __future__ import annotations
import asyncio, json, random, re, sys, time, urllib.parse
from io import BytesIO
from pathlib import Path
from typing import Optional

import cv2, numpy as np

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from playwright.async_api import async_playwright, Page, Browser

CAPTCHA_APP_KEY = "b960842d69fec0920579b4ec213809a3"
CAP_SEC_BASE = "https://cap-sec-gl.heytapmobi.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

class CaptchaError(RuntimeError): pass

def _read_sdk() -> str:
    for name in ["greenseer.js", "js_sdk_16.js", "js_index.js"]:
        p = SCRIPT_DIR / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise CaptchaError("No SDK file found")

class BrowserPool:
    """单例浏览器管理"""
    _instance: Optional["BrowserPool"] = None
    
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._ready = False
    
    @classmethod
    async def get(cls) -> "BrowserPool":
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._start()
        return cls._instance
    
    @classmethod
    async def close(cls):
        if cls._instance:
            await cls._instance._close()
            cls._instance = None
    
    async def _start(self):
        print("[Browser] Starting headless Chrome...", file=sys.stderr)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--no-sandbox", "--no-proxy-server", "--disable-gpu", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await self._browser.new_context(user_agent=USER_AGENT, viewport={"width": 1920, "height": 1080})
        self._page = await ctx.new_page()
        
        # Navigate and inject SDK
        await self._page.goto("https://safe.heytap.com/appeal", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(0.5)
        
        sdk = _read_sdk()
        print(f"[Browser] Injecting SDK ({len(sdk)} bytes)...", file=sys.stderr)
        await self._page.evaluate(f"eval({json.dumps(sdk)});")
        await asyncio.sleep(0.5)
        
        # Verify
        status = await self._page.evaluate("""
            (() => ({
                hasUA: typeof _dx !== 'undefined' && typeof _dx.UA !== 'undefined',
                hasInit: typeof _dx !== 'undefined' && typeof _dx.UA !== 'undefined',
            }))()
        """)
        self._ready = bool(status.get("hasUA"))
        print(f"[Browser] Ready: {self._ready} status={status}", file=sys.stderr)
    
    async def _close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
    
    @property
    def page(self) -> Page:
        if not self._page:
            raise CaptchaError("Browser not started")
        return self._page
    
    @property
    def ready(self) -> bool:
        return self._ready
    
    async def gen_ac(self, sid: str, x: int, y: int) -> str:
        js = f"""
        (() => {{
            try {{
                const ua = _dx.UA.init({{ token: {json.dumps(sid)} }});
                
                // Generate mouse tracks
                const tracks = [];
                let cx = 0, ct = Date.now();
                const gapX = {x};
                while (cx < gapX && tracks.length < 120) {{
                    const a = cx < gapX * 0.6 ? 0.4 + Math.random() * 0.6 : -(0.15 + Math.random() * 0.25);
                    const v = Math.max(0.1, Math.min(8, 3 + a));
                    const dt = 6 + Math.floor(Math.random() * 10);
                    let dx = v * dt / 10 + (Math.random() - 0.5) * 1.5;
                    if (cx + dx >= gapX) {{ dx = gapX - cx; cx = gapX; }}
                    else {{ cx = Math.round((cx + dx) * 100) / 100; }}
                    ct += dt;
                    tracks.push({{ x: Math.round(cx), y: Math.round(50 + (Math.random()-0.5)*2), t: ct }});
                }}
                if (tracks.length > 1) tracks[tracks.length-1].x = gapX;
                
                for (const t of tracks) {{
                    document.dispatchEvent(new MouseEvent('mousemove', {{
                        clientX: t.x, clientY: t.y, screenX: t.x, screenY: t.y,
                        timeStamp: t.t, button: 0, buttons: 1
                    }}));
                }}
                
                if (typeof ua.sendTemp === 'function')
                    ua.sendTemp('x=' + gapX + '&y=' + {y});
                
                const ac = ua.getUA();
                return {{ success: true, ac: ac, len: ac.length }};
            }} catch(e) {{
                return {{ success: false, error: e.message }};
            }}
        }})()
        """
        result = await self.page.evaluate(js)
        if result.get("success") and result.get("ac"):
            return result["ac"]
        raise CaptchaError(f"AC gen failed: {result}")


# ===== HTTP helpers =====

async def _http_get_json(url: str) -> dict:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        h = {"User-Agent": USER_AGENT, "Referer": "https://safe.heytap.com/"}
        async with s.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

async def _http_get_bytes(url: str) -> bytes:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        h = {"User-Agent": USER_AGENT, "Referer": "https://safe.heytap.com/"}
        async with s.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.read()

async def _http_post_form(url: str, data: bytes) -> dict:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        h = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT, "Referer": "https://safe.heytap.com/"}
        async with s.post(url, data=data, headers=h, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()


# ===== Gap detection =====

def find_gap_x(bg_bytes: bytes, slider_bytes: bytes) -> int:
    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    slider = cv2.imdecode(np.frombuffer(slider_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or slider is None:
        raise CaptchaError("Image decode failed")
    
    from captcha_solver import find_gap_x_coord
    return find_gap_x_coord(bg, slider, img_width=280, img_height=144)


# ===== Main solver =====

async def _solve_once(browser: BrowserPool) -> Optional[str]:
    # 1. Get c value
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{CAP_SEC_BASE}/udid/c1", headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
    m = re.search(r"_callback\((.*)\)", text)
    if not m:
        raise CaptchaError("Failed to get c value")
    c_value = json.loads(m.group(1)).get("data", "")
    
    # 2. Get slider challenge
    aid = f"dx-{random.randint(10**18, 10**19-1)}-{random.randint(10**7, 10**8-1)}-{random.randint(1, 5)}"
    challenge_url = (
        f"{CAP_SEC_BASE}/api/a?w=280&h=144&s=50"
        f"&ak={urllib.parse.quote(CAPTCHA_APP_KEY)}&c={urllib.parse.quote(c_value)}"
        f"&jsv=v1.3.41(610)&aid={urllib.parse.quote(aid)}"
        f"&wp=1&de=0&uid=&lf=1&tpc=&isDark=false&wtf=false&_r={random.random()}"
    )
    
    for _ in range(10):
        ch = await _http_get_json(challenge_url)
        if ch.get("success") and ch.get("type") == 0:
            break
        await asyncio.sleep(0.3)
    else:
        raise CaptchaError("No slider challenge")
    
    sid, gap_y = ch["sid"], ch.get("y", 0)
    print(f"  c={c_value[:20]}... sid={sid[:20]}... y={gap_y}", file=sys.stderr)
    
    # 3. Get images and detect gap
    bg_bytes = await _http_get_bytes(f"{CAP_SEC_BASE}{ch['p1']}&_r={random.random()}")
    slider_bytes = await _http_get_bytes(f"{CAP_SEC_BASE}{ch['p2']}&_r={random.random()}")
    gap_x = find_gap_x(bg_bytes, slider_bytes)
    print(f"  gap_x={gap_x}", file=sys.stderr)
    
    # 4. Scan x values around gap
    for dx in range(-30, 31, 1):
        x = max(5, min(250, gap_x + dx))
        for retry in range(3):
            try:
                ac = await browser.gen_ac(sid, x, gap_y)
            except Exception as e:
                if retry == 0:
                    print(f"  x={x}: AC err: {e}", file=sys.stderr)
                continue
            
            if not ac or len(ac) < 20:
                continue
            
            # 5. Submit
            body = (
    f"ac={urllib.parse.quote(ac)}"
    f"&ak={urllib.parse.quote(CAPTCHA_APP_KEY)}"
    f"&c={urllib.parse.quote(c_value)}"
    f"&jsv=v1.3.41(610)"
    f"&sid={urllib.parse.quote(sid)}"
    f"&aid={urllib.parse.quote(aid)}"
    f"&x={x}"
    f"&y={gap_y}"
    f"&isDark=false"
)
            
            try:
                result = await _http_post_form(f"{CAP_SEC_BASE}/api/v1", body.encode())
            except Exception as e:
                print(f"  POST error: {e}", file=sys.stderr)
                continue
            
            if result.get("success") and result.get("data"):
                token = result["data"]
                print(f"  ✅ x={x} token={token[:25]}...", file=sys.stderr)
                return token
            
            code = result.get("code", "")
            if code == 4012 or code == 1:  # MISMATCH or REQUEST_REPEAT → next x
                break
            elif code == 4011:  # HIGH_RISK → retry
                continue
            elif code == 502:
                continue
            else:
                if retry == 0:
                    print(f"  x={x}: code={code}", file=sys.stderr)
                break
    
    return None


async def auto_solve_captcha(max_attempts: int = 5) -> Optional[str]:
    print("=== CDP Captcha Solver ===", file=sys.stderr)
    browser = await BrowserPool.get()
    
    for attempt in range(max_attempts):
        print(f"\n--- Attempt {attempt+1}/{max_attempts} ---", file=sys.stderr)
        try:
            token = await _solve_once(browser)
            if token:
                return token
        except CaptchaError as e:
            print(f"  Error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  Exception: {e}", file=sys.stderr)
        await asyncio.sleep(0.5)
    
    return None


async def cleanup():
    await BrowserPool.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        CAPTCHA_APP_KEY = sys.argv[1]
    
    async def main():
        try:
            token = await auto_solve_captcha(max_attempts=3)
            if token:
                print(f"\n✅ captchaTicket: {token}")
            else:
                print("\n❌ Failed")
                sys.exit(1)
        finally:
            await cleanup()
    
    asyncio.run(main())
