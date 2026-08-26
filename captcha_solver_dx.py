#!/usr/bin/env python3
"""
DingXiang 验证码求解器 — 通过 page.evaluate 注入 JS 直接操控 captcha 组件。

借鉴其他项目的思路：不通过 Playwright 高级 API（mouse.click/tap）猜测，
而是在页面内直接 dispatch DOM 事件到 captcha 组件上的精确坐标。
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


def _debug_dir() -> str | None:
    d = os.environ.get("OPPO_CAPTCHA_DEBUG_DIR", "")
    return d or None


def _write_debug(path: str | None, name: str, content: bytes | str):
    if not path:
        return
    try:
        p = Path(path) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    except Exception:
        pass


# ====================== 滑块 — JS 注入式拖动 ======================

def _slider_gap_detect_dx(canvas_bytes: bytes, btn_w: float, cbox_w: float):
    """同步：cv2 图像处理检测滑块缺口，返回 (gap_x, gap_css, h, w, arr) 或 None"""
    arr = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return None
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    piece_w = max(1, int(round(btn_w * w / cbox_w)))
    roi_x = int(w * 0.15)
    col_mean = gray.mean(axis=0).astype(float)
    dark = col_mean.max() - col_mean
    dark[:roi_x] = 0
    dark[-5:] = 0
    gap_x = int(np.argmax(dark))
    css_per_img = cbox_w / w
    gap_css = (gap_x - piece_w * 0.92) * css_per_img
    gap_css = max(5, min(235, gap_css))
    return gap_x, gap_css, h, w, arr


_SLIDER_DRAG_JS = """
async ({gapCSS, btnBox, cbox}) => {
    const slider = document.querySelector('.dx_captcha_basic_slider, .dx_captcha_slider, [class*="slider"][class*="btn"]');
    if (!slider) return 'slider_not_found';
    const startX = btnBox.x + btnBox.width / 2;
    const startY = btnBox.y + btnBox.height / 2;
    const endX = startX + gapCSS;
    const steps = 30;
    const dispatch = (x, y, type) => {
        slider.dispatchEvent(new PointerEvent(type, {
            bubbles: true, cancelable: true,
            clientX: x, clientY: y, pointerType: 'mouse', isPrimary: true, pressure: 0.5,
        }));
        slider.dispatchEvent(new MouseEvent(type, {
            bubbles: true, cancelable: true,
            clientX: x, clientY: y, button: 0,
        }));
    };
    // pointerdown
    dispatch(startX, startY, 'pointerdown');
    dispatch(startX, startY, 'mousedown');
    await new Promise(r => setTimeout(r, 80 + Math.random() * 60));
    // 拖动
    for (let i = 1; i <= steps; i++) {
        const p = i / steps;
        const eased = p < 0.15 ? p * p * p : p < 0.7 ? 0.15**3 + (p-0.15)*1.2 : 1 - (1-p)*(1-p);
        const x = startX + gapCSS * Math.min(1, eased);
        const y = startY + (Math.random() - 0.5) * 2;
        dispatch(x, y, 'pointermove');
        dispatch(x, y, 'mousemove');
        await new Promise(r => setTimeout(r, 12 + Math.random() * 10));
    }
    // pointerup
    dispatch(endX, startY, 'pointerup');
    dispatch(endX, startY, 'mouseup');
    await new Promise(r => setTimeout(r, 300));
    return 'dragged';
}
"""


async def solve_slider_dx(page, max_attempts=4) -> bool:
    for attempt in range(max_attempts):
        try:
            await asyncio.sleep(1.0)

            slider = await page.query_selector(
                ".dx_captcha_basic_slider, .dx_captcha_slider, [class*='slider'][class*='btn']")
            canvas = await page.query_selector(
                ".dx_captcha_basic_box canvas, .dx_captcha_slider canvas, canvas")
            if not slider or not canvas:
                continue

            btn_box = await slider.bounding_box()
            cbox = await canvas.bounding_box()
            if not btn_box or not cbox:
                continue

            canvas_bytes = await canvas.screenshot(timeout=5000)
            result = await asyncio.to_thread(_slider_gap_detect_dx, canvas_bytes, btn_box["width"], cbox["width"])
            if result is None:
                continue
            gap_x, gap_css, h, w, arr = result

            debug_path = _debug_dir()
            if debug_path:
                dbg = arr.copy()
                cv2.circle(dbg, (gap_x, h // 2), 10, (0, 255, 0), 2)
                cv2.line(dbg, (gap_x, 0), (gap_x, h), (0, 255, 0), 1)
                _write_debug(debug_path, "canvas.png", canvas_bytes)
                _write_debug(debug_path, "gap.png",
                             cv2.imencode(".png", dbg)[1].tobytes())

            gap_css = max(5, min(235, gap_css))

            result = await page.evaluate(
                _SLIDER_DRAG_JS, {"gapCSS": gap_css, "btnBox": btn_box, "cbox": cbox})
            print(f"  [dx slider] gap={gap_x} css={gap_css:.0f} result={result} attempt={attempt}",
                  file=sys.stderr)

            await asyncio.sleep(2.0)
            if await _captcha_gone(page):
                return True
        except Exception as e:
            print(f"  [dx slider] err: {e}", file=sys.stderr)
    return False


# ====================== 点选 — JS 注入式点击 ======================

def _clickword_match_dx(hint_crops: list, candidate_boxes: list, canvas_img):
    """同步：cv2 模板匹配 hint 字符到候选框，返回 [(cx, cy), ...]"""
    points_canvas = []
    used_boxes = set()
    for hint_arr in hint_crops:
        hh, hw = hint_arr.shape[:2]
        hint_gray = cv2.cvtColor(hint_arr, cv2.COLOR_BGR2GRAY)
        best_score = -1.0
        best_center = None
        best_bi = -1
        for bi, (bx1, by1, bx2, by2) in enumerate(candidate_boxes):
            if bi in used_boxes:
                continue
            crop = canvas_img[by1:by2, bx1:bx2]
            if crop.size == 0:
                continue
            crop_h, crop_w = crop.shape[:2]
            scale = hh / max(crop_h, 1)
            new_w = int(crop_w * scale)
            if new_w < 3 or hh < 3:
                continue
            crop_resized = cv2.resize(crop, (new_w, hh))
            crop_gray = cv2.cvtColor(crop_resized, cv2.COLOR_BGR2GRAY)
            if crop_gray.shape[1] < hint_gray.shape[1]:
                res = cv2.matchTemplate(crop_gray, hint_gray, cv2.TM_CCOEFF_NORMED)
            else:
                res = cv2.matchTemplate(hint_gray, crop_gray, cv2.TM_CCOEFF_NORMED)
            _, score, _, _ = cv2.minMaxLoc(res)
            if score > best_score:
                best_score = score
                best_center = ((bx1 + bx2) / 2, (by1 + by2) / 2)
                best_bi = bi
        if best_center is not None:
            points_canvas.append(best_center)
            used_boxes.add(best_bi)
    return points_canvas


async def solve_clickword_dx(page, max_attempts=4) -> bool:
    for attempt in range(max_attempts):
        try:
            await asyncio.sleep(1.0)

            # 1. 截取 hint 容器里的所有 img 作为待匹配字符
            hint_imgs = await page.query_selector_all(
                ".dx_captcha_clickword_hint-cnt img, .dx_captcha_clickword_hint img")
            if not hint_imgs:
                print(f"  [dx clickword] no hint_imgs found", file=sys.stderr)
                continue

            hint_crops = []  # [(bytes, width, height)]
            for imgel in hint_imgs:
                try:
                    shot = await imgel.screenshot(timeout=3000)
                    if shot:
                        arr = cv2.imdecode(np.frombuffer(shot, np.uint8), cv2.IMREAD_COLOR)
                        if arr is not None:
                            hint_crops.append(arr)
                except Exception:
                    continue
            if len(hint_crops) < 3:
                print(f"  [dx clickword] only {len(hint_crops)} hint_crops", file=sys.stderr)
                continue

            # 2. 截取 canvas
            canvas_el = await page.query_selector(
                ".dx_captcha_clickword_pic canvas, canvas")
            if not canvas_el:
                continue
            cbox = await canvas_el.bounding_box()
            if not cbox:
                continue
            canvas_bytes = await canvas_el.screenshot(timeout=5000)
            canvas_img = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
            if canvas_img is None:
                continue
            ch, cw = canvas_img.shape[:2]
            sx = cbox["width"] / cw
            sy = cbox["height"] / ch

            # 3. AntiCAP YOLO 检测目标框（只用来做候选区域，不做 Siamese 匹配）
            from captcha_solver_anticap import _get_handler
            Atc = _get_handler()
            candidate_boxes = []  # [(x1,y1,x2,y2)] in canvas coords
            if Atc is not None:
                canvas_b64 = base64.b64encode(canvas_bytes).decode()
                boxes_raw = Atc.Detection_Text(img_base64=canvas_b64)
                if isinstance(boxes_raw, list):
                    for item in boxes_raw:
                        if isinstance(item, dict) and item.get("class") == "Text":
                            b = item.get("box")
                            if isinstance(b, (list, tuple)) and len(b) == 4:
                                x1, y1, x2, y2 = int(b[0]), int(b[1]), int(b[2]), int(b[3])
                                # 过滤掉四角边缘噪点
                                bw, bh = x2 - x1, y2 - y1
                                if x1 < 3 and y1 < 3:            # 左上
                                    continue
                                if x2 > cw - 3 and y1 < 3:       # 右上
                                    continue
                                if x1 < 5 and y1 > ch - 15:      # 左下
                                    continue
                                if x2 > cw - 5 and y2 > ch - 15: # 右下
                                    continue
                                candidate_boxes.append([x1, y1, x2, y2])

            if len(candidate_boxes) < 3:
                continue

            # 4. 每个 hint 字符用 OpenCV 模板匹配找最佳候选框（在线程池执行，不阻塞事件循环）
            points_canvas = await asyncio.to_thread(_clickword_match_dx, hint_crops, candidate_boxes, canvas_img)

            if len(points_canvas) < 3:
                continue

            # 5. 按 x 排序，转页面坐标
            points_canvas.sort(key=lambda p: p[0])
            page_points = [[p[0] * sx + cbox["x"], p[1] * sy + cbox["y"]]
                           for p in points_canvas]

            # 6. JS 注入点击
            click_js = """
            async (pts) => {
                const cv = document.querySelector('canvas');
                if (!cv) return 'no_canvas';
                for (const [px, py] of pts) {
                    const opts = {bubbles: true, cancelable: true, clientX: px, clientY: py};
                    cv.dispatchEvent(new PointerEvent('pointerdown', {...opts, pointerType:'mouse', isPrimary:true}));
                    cv.dispatchEvent(new MouseEvent('mousedown', opts));
                    cv.dispatchEvent(new PointerEvent('pointerup', {...opts, pointerType:'mouse', isPrimary:true}));
                    cv.dispatchEvent(new MouseEvent('mouseup', opts));
                    cv.dispatchEvent(new MouseEvent('click', opts));
                    await new Promise(r => setTimeout(r, 500));
                }
                return 'clicked';
            }
            """

            result = await page.evaluate(click_js, page_points)
            print(f"  [dx clickword] hint_chars={len(hint_crops)} candidates={len(candidate_boxes)} "
                  f"points={len(page_points)} result={result}",
                  file=sys.stderr)

            await asyncio.sleep(2.0)
            if await _captcha_gone(page):
                return True
        except Exception as e:
            print(f"  [dx clickword] err: {e}", file=sys.stderr)
    return False


# ====================== 辅助 ======================

async def _captcha_gone(page) -> bool:
    try:
        text = await page.evaluate("document.body?.innerText?.substring(0,500)||''")
        if any(k in text for k in ["验证通过", "验证成功", "选择账号", "当前绑定的账号",
                                     "绑定过的账号", "昵称", "注册时间", "账号密码错误"]):
            return True
        slider = await page.evaluate(
            "!!document.querySelector('.dx_captcha_slider, .dx_captcha_basic, .yidun_slider')")
        clickword = await page.evaluate(
            "!!document.querySelector('.dx_captcha_clickword, .yidun_tips')")
        return not slider and not clickword
    except Exception:
        return True


async def _captcha_type_dx(page) -> str:
    try:
        if await page.evaluate("!!document.querySelector('.dx_captcha_clickword, .yidun_tips')"):
            return "clickword"
        if await page.evaluate("!!document.querySelector('.dx_captcha_slider, .dx_captcha_basic, .yidun_slider')"):
            return "slider"
        text = await page.evaluate("document.body?.innerText?.substring(0,500)||''")
        if any(k in text for k in ["请依次点击", "请点击", "按顺序点击"]):
            return "clickword"
        if any(k in text for k in ["向右滑动", "拖动滑块", "滑动滑块", "完成拼图"]):
            return "slider"
        return "none"
    except Exception:
        return "none"


async def solve_captcha_dx(page, max_attempts=5) -> bool:
    """新统一入口：JS 注入式 DingXiang 验证码求解"""
    for attempt in range(max_attempts):
        ctype = await _captcha_type_dx(page)
        if ctype == "none":
            return True
        print(f"  [dx] type={ctype} attempt={attempt+1}/{max_attempts}", file=sys.stderr)
        if ctype == "slider":
            ok = await solve_slider_dx(page, 4)
        elif ctype == "clickword":
            ok = await solve_clickword_dx(page, 4)
        else:
            ok = await solve_slider_dx(page, 2) or await solve_clickword_dx(page, 2)
        if ok:
            return True
        # 刷新验证码 → 等 canvas 图片加载完成再重试
        try:
            refresh = await page.query_selector("[class*='refresh'], .dx-captcha-refresh")
            if refresh:
                await refresh.click()
                await _wait_canvas_ready(page, timeout_ms=8000)
        except Exception:
            pass
    return False


async def _wait_canvas_ready(page, timeout_ms: int = 8000) -> bool:
    """等 canvas 图片加载完成（采样 3 点验证非纯色），用于 refresh 后。"""
    import time as _time
    deadline = _time.time() + timeout_ms / 1000
    while _time.time() < deadline:
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
            return True
        await asyncio.sleep(0.2)
    return False