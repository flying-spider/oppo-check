#!/usr/bin/env python3
"""
验证码求解器 - AntiCAP 适配层

用 AntiCAP (https://github.com/81NewArk/AntiCAP) 做图像识别，
保留 Playwright 做页面坐标映射和实际交互（点击/拖动）。

AntiCAP 未安装时自动退化，不影响主链路启动。
pip install AntiCAP 后即可启用。
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

# ---------- lazy import AntiCAP ----------
_HANDLER = None
_HAS_ANTICAP = False


def _get_handler():
    """懒加载 AntiCAP Handler，安装失败不阻塞启动。"""
    global _HANDLER, _HAS_ANTICAP
    if _HANDLER is not None:
        return _HANDLER
    try:
        import AntiCAP  # type: ignore
        _HANDLER = AntiCAP.Handler(show_banner=False)
        _HAS_ANTICAP = True
        print("[AntiCAP] loaded", file=sys.stderr)
    except Exception as e:
        _HAS_ANTICAP = False
        print(f"[AntiCAP] not available: {e}", file=sys.stderr)
    return _HANDLER


# ---------- debug helpers ----------

def _debug_scope(debug_dir: Optional[str], label: str) -> Optional[str]:
    if not debug_dir:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scope = Path(debug_dir) / f"{stamp}_{label}_ac_{os.getpid()}_{random.randint(1000, 9999)}"
    try:
        (scope / "raw").mkdir(parents=True, exist_ok=True)
        (scope / "annotated").mkdir(parents=True, exist_ok=True)
        return str(scope)
    except Exception:
        return None


def _debug_file(debug_dir: Optional[str], group: str, name: str) -> Optional[Path]:
    return None  # debug 截图已禁用


def _write_json(path: Optional[Path], payload: dict) -> None:
    if not path:
        return
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def _write_bytes(path, payload) -> None:
    """诊断截图已禁用"""
    pass


async def _save_screenshot(page, path) -> None:
    """诊断截图已禁用"""
    pass


async def _page_metrics(page) -> dict:
    try:
        return await page.evaluate(
            """() => ({
                url: location.href,
                dpr: window.devicePixelRatio || 1,
                viewport: {width: window.innerWidth, height: window.innerHeight},
                scroll: {x: window.scrollX, y: window.scrollY},
            })"""
        )
    except Exception as exc:
        return {"error": repr(exc)}


async def _mark_page_points(page, points: list[dict]) -> None:
    safe = []
    for item in points:
        x, y = item.get("x"), item.get("y")
        if x is None or y is None:
            continue
        safe.append({
            "x": float(x), "y": float(y),
            "label": str(item.get("label", "")),
            "color": str(item.get("color", "#ff2d55")),
            "radius": float(item.get("radius", 10)),
        })
    if not safe:
        return
    try:
        await page.evaluate(
            """(points) => {
                const old = document.getElementById('__captcha_debug_overlay');
                if (old) old.remove();
                const layer = document.createElement('div');
                layer.id = '__captcha_debug_overlay';
                Object.assign(layer.style, {
                    position:'fixed', left:'0', top:'0', width:'0', height:'0',
                    zIndex:'2147483647', pointerEvents:'none'
                });
                for (const p of points) {
                    const r = p.radius || 10;
                    const dot = document.createElement('div');
                    dot.textContent = p.label || '';
                    Object.assign(dot.style, {
                        position:'fixed',
                        left:`${p.x - r}px`, top:`${p.y - r}px`,
                        width:`${r * 2}px`, height:`${r * 2}px`,
                        border:`3px solid ${p.color||'#ff2d55'}`,
                        borderRadius:'999px',
                        background:'rgba(255,45,85,0.12)',
                        color:p.color||'#ff2d55',
                        font:'bold 12px sans-serif',
                        lineHeight:`${r*2}px`, textAlign:'center',
                        boxSizing:'border-box',
                        textShadow:'0 1px 2px white',
                    });
                    layer.appendChild(dot);
                }
                document.documentElement.appendChild(layer);
            }""",
            safe,
        )
    except Exception:
        pass


def _save_clickword_debug_image(
    canvas_img,           # np.ndarray (BGR)
    sim_results: list[dict],
    target_boxes: list[list[int]],
    path,
) -> None:
    """在答案图上画匹配框和标签，保存为 PNG。"""
    if path is None:
        return
    try:
        import cv2
        vis = canvas_img.copy()
        colors = [(0, 255, 0), (255, 80, 0), (0, 100, 255)]
        for r in sim_results:
            bi = r.get("box_index", -1)
            if bi < 0 or bi >= len(target_boxes):
                continue
            x1, y1, x2, y2 = target_boxes[bi]
            hi = r.get("index", 0) - 1
            color = colors[hi % 3] if hi >= 0 else (0, 255, 0)
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
            cv2.circle(vis, ((x1 + x2) // 2, (y1 + y2) // 2), 6, color, -1)
            label = f'h{hi + 1}:{r.get("ocr_hint", "?")}→{r.get("ocr_answer", "?")}'
            cv2.putText(vis, label, (x1, max(y1 - 4, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
        cv2.imwrite(str(path), vis)
    except Exception:
        pass


# ====================== 滑块 ======================

async def _solve_slider_anticap(page, max_attempts: int = 5,
                                debug_dir: Optional[str] = None,
                                refresh_counter=None) -> bool:
    Atc = _get_handler()
    if Atc is None:
        return False

    for attempt in range(max_attempts):
        scope = _debug_scope(debug_dir, f"slider_ac_{attempt}")
        try:
            await asyncio.sleep(1.2)

            # 定位元素
            slider = await page.query_selector(
                ".dx_captcha_basic_slider, .dx_captcha_slider, "
                "[class*='slider'][class*='btn'], .yidun_slider")
            bar = await page.query_selector(
                ".dx_captcha_basic_bar, .dx_captcha_bar, .yidun_bgimg, "
                "[class*='slider'][class*='bar'], [class*='track']")
            canvas = await page.query_selector(
                ".dx_captcha_basic_box canvas, .dx_captcha_slider canvas, "
                ".dx_captcha_basic_bg, canvas")

            if not slider or not canvas:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False, "reason": "elements_not_found",
                    "attempt": attempt,
                })
                return False

            btn_box = await slider.bounding_box()
            cbox = await canvas.bounding_box()
            bar_box = await bar.bounding_box() if bar else None

            if not btn_box or not cbox:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False, "reason": "box_missing", "attempt": attempt,
                })
                return False

            canvas_bytes = await canvas.screenshot(timeout=5000)
            _write_bytes(_debug_file(scope, "raw", "canvas.png"), canvas_bytes)
            canvas_b64 = base64.b64encode(canvas_bytes).decode()

            # 截取滑块拖柄对应区域的图像（canvas 左侧滑块本体）
            import cv2
            import numpy as np
            decoded = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                return False
            canvas_h, canvas_w = decoded.shape[:2]
            css_per_img_x = cbox["width"] / canvas_w
            piece_w_img = max(1, int(round(btn_box["width"] / css_per_img_x)))
            piece_h_img = max(1, int(round(btn_box["height"] / css_per_img_x)))

            # 滑块图片 = canvas 左侧滑块区域
            slider_piece = decoded[:, :piece_w_img]
            slider_piece_bytes = cv2.imencode(".png", slider_piece)[1].tobytes()
            slider_piece_b64 = base64.b64encode(slider_piece_bytes).decode()
            _write_bytes(_debug_file(scope, "raw", "slider_piece.png"), slider_piece_bytes)

            # --- AntiCAP 识别缺口 ---
            result = Atc.Slider_Match(
                target_base64=slider_piece_b64,
                background_base64=canvas_b64,
            )

            _write_json(_debug_file(scope, "raw", "anticap_slider_result.json"), {
                "anticap_result": result,
                "canvas_w": canvas_w,
                "canvas_h": canvas_h,
                "piece_w_img": piece_w_img,
                "css_per_img_x": css_per_img_x,
            })

            # 解析返回坐标
            gap_x_img = _parse_slider_result(result, canvas_w)
            if gap_x_img is None:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False, "reason": "anticap_parse_failed",
                    "result": result, "attempt": attempt,
                })
                await _refresh(page, counter=refresh_counter)
                continue

            # 映射到页面坐标
            gap_css_x = gap_x_img * css_per_img_x

            # 滑块自身在 canvas 里的中心
            piece_img_cx = piece_w_img * 0.92
            piece_css_cx = piece_img_cx * css_per_img_x

            start_x = btn_box["x"] + btn_box["width"] / 2
            start_y = btn_box["y"] + btn_box["height"] / 2
            gap_page_x = cbox["x"] + gap_css_x
            gap_page_y = cbox["y"] + cbox["height"] / 2
            piece_page_x = cbox["x"] + piece_css_cx
            slide_dist = max(5.0, gap_page_x - piece_page_x)

            # 限制在轨道内（bar 可能选到零尺寸元素，退化到 canvas 宽度）
            track_w = (
                bar_box["width"] - btn_box["width"]
                if bar_box and bar_box["width"] > 10
                else cbox["width"] - btn_box["width"]
            )
            slide_dist = max(5.0, min(track_w, slide_dist * 1.0 - 5))

            end_x = start_x + slide_dist
            track = _human_track(slide_dist)

            # 标记点
            await _mark_page_points(page, [
                {"x": start_x, "y": start_y, "label": "S", "color": "#007aff", "radius": 11},
                {"x": piece_page_x, "y": start_y, "label": "P", "color": "#5856d6", "radius": 11},
                {"x": end_x, "y": start_y, "label": "E", "color": "#ff2d55", "radius": 11},
                {"x": gap_page_x, "y": gap_page_y, "label": "G", "color": "#34c759", "radius": 11},
            ])
            await _save_screenshot(page, _debug_file(scope, "annotated", "marked.png"))

            # 拖动
            await page.mouse.move(start_x, start_y)
            await asyncio.sleep(random.uniform(0.1, 0.2))
            await page.mouse.down()

            for dx in track:
                jitter_y = start_y + random.randint(-3, 3)
                await page.mouse.move(start_x + dx, jitter_y)
                await asyncio.sleep(random.uniform(0.012, 0.020))

            # 最终到位（不使用 steps 动画）
            await page.mouse.move(end_x, start_y + random.randint(-1, 1))
            await asyncio.sleep(0.12)
            await _save_screenshot(page, _debug_file(scope, "annotated", "before_release.png"))
            await page.mouse.up()

            await asyncio.sleep(5.0)
            result_type = await _captcha_type(page)

            _write_json(_debug_file(scope, "raw", "result.json"), {
                "ok": result_type == "none",
                "attempt": attempt,
                "gap_img_x": gap_x_img,
                "gap_page_x": gap_page_x,
                "piece_page_x": piece_page_x,
                "slide_dist": slide_dist,
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": start_y},
                "final": {"x": end_x, "y": start_y},
                "captchaTypeAfter": result_type,
                "track_points": len(track),
                "metrics": await _page_metrics(page),
            })

            if await _captcha_type(page) == "none":
                return True
            await _refresh(page, counter=refresh_counter)

        except Exception as e:
            _write_json(_debug_file(scope, "raw", "error.json"), {
                "error": repr(e), "attempt": attempt,
            })
            print(f"  [AntiCAP slider] attempt {attempt}: {e}", file=sys.stderr)

    return False


def _parse_slider_result(result, canvas_w: int) -> Optional[float]:
    """从 AntiCAP Slider_Match 返回值中提取缺口 x 坐标。"""
    if result is None:
        return None
    # 返回格式可能是 {'x': ..., 'y': ...} 或 {'distance': ...} 或直接数值
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, dict):
        for key in ("x", "gap_x", "distance", "dx"):
            val = result.get(key)
            if isinstance(val, (int, float)):
                return float(val)
        # 可能返回 box 格式 {'box': [x1, y1, x2, y2]}
        box = result.get("box")
        if isinstance(box, (list, tuple)) and len(box) >= 2:
            return float(box[0])
    return None


def _human_track(distance: float, steps: int | None = None) -> list[float]:
    """拟人滑动位移序列：慢启动 → 匀速 → 减速 → 末端微调。"""
    if steps is None:
        steps = max(35, min(55, int(distance * 0.45)))
    xs: list[float] = []
    for i in range(steps):
        p = (i + 1) / steps
        if p < 0.12:
            eased = p ** 1.6
        elif p < 0.65:
            eased = 0.12 ** 1.6 + (p - 0.12) * 0.85
        elif p < 0.88:
            r = (p - 0.65) / 0.23
            eased = 0.12 ** 1.6 + 0.53 * 0.85 + (1 - (1 - r) ** 1.5) * 0.35 * 0.85
        else:
            eased = 1.0 - (1.0 - p) * 0.015
        xs.append(distance * max(0.0, min(1.0, eased)))
    if xs:
        xs[-1] = distance
    return xs


# ====================== 点选 ======================

async def _solve_clickword_anticap(page, max_attempts: int = 5,
                                   debug_dir: Optional[str] = None,
                                   refresh_counter=None) -> bool:
    Atc = _get_handler()
    if Atc is None:
        return False

    for attempt in range(max_attempts):
        scope = _debug_scope(debug_dir, f"clickword_ac_{attempt}")
        try:
            await asyncio.sleep(1.8)

            # 读取提示区域整图：目标站点实际是一个包含 3 个字符的顺序提示条，
            # 不能按 img 数量拆，否则会被识别成 1 个目标。
            order_el = (
                await page.query_selector(".dx_captcha_clickword_hint-cnt")
                or await page.query_selector(".dx_captcha_clickword_hint")
                or await page.query_selector(".yidun_tips")
            )
            if not order_el:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False, "reason": "hint_container_not_found", "attempt": attempt,
                })
                return False

            order_bytes = await order_el.screenshot(timeout=5000)
            _write_bytes(_debug_file(scope, "raw", "order_img.png"), order_bytes)
            order_parts = _split_order_chars(order_bytes, expected=3)
            if len(order_parts) != 3:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False,
                    "reason": "order_split_failed",
                    "split_count": len(order_parts),
                    "attempt": attempt,
                })
                await _refresh(page, counter=refresh_counter)
                continue
            for i, part in enumerate(order_parts, start=1):
                _write_bytes(_debug_file(scope, "raw", f"order_char_{i}.png"), part["bytes"])
            _write_json(_debug_file(scope, "raw", "order_split.json"), {
                "count": len(order_parts),
                "parts": [
                    {k: v for k, v in part.items() if k != "bytes"}
                    for part in order_parts
                ],
            })

            # 目标 canvas
            canvas_el = (await page.query_selector(".dx_captcha_clickword_pic canvas")
                         or await page.query_selector("canvas"))
            if not canvas_el:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False, "reason": "canvas_not_found", "attempt": attempt,
                })
                return False

            cbox = await canvas_el.bounding_box()
            if not cbox or cbox["width"] < 10:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False, "reason": "canvas_box_invalid", "attempt": attempt,
                })
                return False

            canvas_bytes = await canvas_el.screenshot(timeout=5000)
            canvas_b64 = base64.b64encode(canvas_bytes).decode()
            _write_bytes(_debug_file(scope, "raw", "canvas.png"), canvas_bytes)

            # --- AntiCAP 识别：先用 YOLO 检测目标图中所有文字框，
            # 再对每个拆出的 hint 字符用 Siamese 相似度找最佳匹配 ---
            target_boxes_raw = Atc.Detection_Text(img_base64=canvas_b64)
            _write_json(_debug_file(scope, "raw", "detection_text_result.json"), {
                "raw": target_boxes_raw,
            })

            # 解析文字框：[{'class':'Text','box':[x1,y1,x2,y2]}, ...]
            target_boxes: list[list[int]] = []
            if isinstance(target_boxes_raw, list):
                for item in target_boxes_raw:
                    if isinstance(item, dict):
                        box = item.get("box")
                        if isinstance(box, (list, tuple)) and len(box) == 4:
                            target_boxes.append([int(v) for v in box])

            if len(target_boxes) < 3:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False,
                    "reason": "target_text_boxes_less_than_3",
                    "target_box_count": len(target_boxes),
                    "attempt": attempt,
                })
                await _refresh(page, counter=refresh_counter)
                continue

            # canvas 只解码一次
            import cv2
            import numpy as np
            decoded = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                return False
            img_h, img_w = decoded.shape[:2]

            # 过滤掉明显是噪音的框：画布底部、左边缘、过于窄小、刷新按钮区域
            filtered_boxes: list[list[int]] = []
            for box in target_boxes:
                x1, y1, x2, y2 = box
                bw, bh = x2 - x1, y2 - y1
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                # 左上角噪点
                if x1 < 20 and y1 < 20 and (bw < 25 or bh < 45):
                    continue
                # 右上角噪点（x2 贴近右边缘, y1 贴近顶部）
                if x2 > img_w - 20 and y1 < 20 and (bw < 25 or bh < 45):
                    continue
                # 左下角噪点（扩展：不限制 x1<5）
                if y1 > img_h - 20 and x1 < 15 and bw < 25:
                    continue
                # 右下角噪点（x2 贴近右边缘, y2 贴近底部）
                if x2 > img_w - 15 and y2 > img_h - 20 and bw < 30:
                    continue
                # 画布底部边缘
                if y1 > img_h - 8:
                    continue
                # 极小框（< 7×7 且面积 < 50）
                if bw < 7 and bh < 7 and bw * bh < 50:
                    continue
                # 右下角刷新按钮
                if cx > img_w * 0.82 and cy > img_h * 0.78:
                    aspect = bw / max(1, bh)
                    if 0.6 <= aspect <= 1.5:
                        continue
                filtered_boxes.append(box)
            target_boxes = filtered_boxes

            # 合并重叠框：IoU > 40% 的框取并集，避免同一字符被 YOLO 检测两次
            merged_boxes: list[list[int]] = []
            for box in target_boxes:
                x1, y1, x2, y2 = box
                merged = False
                for i, mb in enumerate(merged_boxes):
                    mx1, my1, mx2, my2 = mb
                    ix1 = max(x1, mx1)
                    iy1 = max(y1, my1)
                    ix2 = min(x2, mx2)
                    iy2 = min(y2, my2)
                    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                    area_a = (x2 - x1) * (y2 - y1)
                    area_b = (mx2 - mx1) * (my2 - my1)
                    union = area_a + area_b - inter
                    if union > 0 and inter / union > 0.35:
                        # 合并：取并集
                        merged_boxes[i] = [min(x1, mx1), min(y1, my1), max(x2, mx2), max(y2, my2)]
                        merged = True
                        break
                if not merged:
                    merged_boxes.append(box)
            target_boxes = merged_boxes

            # 3 个 hint 字符找最佳匹配目标框
            # 混合策略：OCR 识别字符 → 大小写不敏感匹配为主，Siamese 相似度为辅
            n_hints = len(order_parts)
            n_boxes = len(target_boxes)

            # OCR 辅助函数：多管线预处理 + 投票，优先选能匹配已知答案字符的结果
            def _best_ocr(img_bytes: bytes, known_chars: list[str] | None = None) -> str:
                """多管线 OCR：原始、反转、填充实心、2x 放大，投票取最佳。

                管线：
                  A) 原始彩色图（对大写/简洁字符有效）
                  B) 反转灰度（白底黑字，对空心字体有效）
                  C) 填充实心 + 裁剪 + pad（对空心 'a'/'e' 有效）
                  D) 填充实心反转 + 2x 放大（补偿小尺寸字符）
                """
                votes = {}  # char → count
                img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    return ""

                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

                # 管线 A: 原始
                ok, enc = cv2.imencode(".png", img)
                if ok:
                    r = Atc.OCR(img_base64=base64.b64encode(enc.tobytes()).decode())
                    if r:
                        c = str(r).strip().lower()
                        if c:
                            votes[c] = votes.get(c, 0) + 1

                # 管线 B: 反转灰度（空心字体关键管线，2x 权重）
                inv = cv2.bitwise_not(gray)
                ok, enc = cv2.imencode(".png", inv)
                if ok:
                    r = Atc.OCR(img_base64=base64.b64encode(enc.tobytes()).decode())
                    if r:
                        c = str(r).strip().lower()
                        if c:
                            votes[c] = votes.get(c, 0) + 2  # 反转管线权重加倍

                # 管线 C: 填充实心（闭运算填空心 → 裁剪 → pad）
                _, bin_img = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 5))
                filled = cv2.morphologyEx(bin_img, cv2.MORPH_CLOSE, k, iterations=2)
                ys, xs = np.where(filled > 0)
                if len(xs) > 0:
                    filled = filled[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
                padded = cv2.copyMakeBorder(filled, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=0)
                # 黑底白字方向
                ok, enc = cv2.imencode(".png", padded)
                if ok:
                    r = Atc.OCR(img_base64=base64.b64encode(enc.tobytes()).decode())
                    if r:
                        c = str(r).strip().lower()
                        if c:
                            votes[c] = votes.get(c, 0) + 1
                # 白底黑字方向
                padded_inv = cv2.bitwise_not(padded)
                ok2, enc2 = cv2.imencode(".png", padded_inv)
                if ok2:
                    r = Atc.OCR(img_base64=base64.b64encode(enc2.tobytes()).decode())
                    if r:
                        c = str(r).strip().lower()
                        if c:
                            votes[c] = votes.get(c, 0) + 1

                # 管线 D: 2x 放大填充实心（补偿小字符）
                if padded.shape[1] > 4 and padded.shape[0] > 4:
                    scaled = cv2.resize(padded, (padded.shape[1] * 2, padded.shape[0] * 2),
                                        interpolation=cv2.INTER_LANCZOS4)
                    ok, enc = cv2.imencode(".png", scaled)
                    if ok:
                        r = Atc.OCR(img_base64=base64.b64encode(enc.tobytes()).decode())
                        if r:
                            c = str(r).strip().lower()
                            if c:
                                votes[c] = votes.get(c, 0) + 1

                if not votes:
                    return ""

                # 投票策略：接受单字符（ASCII 字母/数字/符号优先，排除 CJK 中文）
                def _is_valid_char(c: str) -> bool:
                    if len(c) != 1:
                        return False
                    # ASCII 字母、数字、常见符号
                    if c in "abcdefghijklmnopqrstuvwxyz0123456789&@#\$%!?+-=<>()*;":
                        return True
                    # 排除 CJK 统一汉字 (U+4E00–U+9FFF) 和其他非 ASCII 非符号字符
                    cp = ord(c)
                    if 0x4E00 <= cp <= 0x9FFF:  # CJK
                        return False
                    if cp < 128:  # ASCII
                        return True
                    # 拉丁扩展等（如 é, ü）也接受
                    if cp < 0x300:
                        return True
                    return False

                letter_votes = {}
                for c, cnt in votes.items():
                    if _is_valid_char(c):
                        letter_votes[c] = cnt

                if letter_votes:
                    if known_chars:
                        for c, cnt in sorted(letter_votes.items(), key=lambda x: -x[1]):
                            if c in known_chars:
                                return c
                    # 点选从不出现数字，OCR 返回数字时降级为非数字最优候选
                    winner = max(letter_votes, key=letter_votes.get)
                    if winner.isdigit():
                        non_digit = {c: cnt for c, cnt in letter_votes.items() if not c.isdigit()}
                        if non_digit:
                            return max(non_digit, key=non_digit.get)
                    return winner

                # 无有效单字符，返回票数最高的原始结果
                return max(votes, key=votes.get)

            # OCR 所有 hint 字符（先不传 known_chars，后续有答案 OCR 后再纠偏）
            hint_ocr_chars = []
            for part in order_parts:
                hint_ocr_chars.append(_best_ocr(part["bytes"]))

            # OCR 所有答案框（同样双向）
            answer_ocr_chars = []
            for x1, y1, x2, y2 in target_boxes:
                cx1 = max(0, min(img_w - 1, x1))
                cy1 = max(0, min(img_h - 1, y1))
                cx2 = max(cx1 + 1, min(img_w, x2))
                cy2 = max(cy1 + 1, min(img_h, y2))
                crop = decoded[cy1:cy2, cx1:cx2]
                ok, enc = cv2.imencode(".png", crop)
                if ok:
                    answer_ocr_chars.append(_best_ocr(enc.tobytes()))
                else:
                    answer_ocr_chars.append("")

            # 用答案 OCR 结果纠偏 hint OCR（如原始 "d" 反转 "a"，后者匹配答案框中的 "a"）
            for hi in range(len(hint_ocr_chars)):
                corrected = _best_ocr(order_parts[hi]["bytes"], known_chars=answer_ocr_chars)
                if corrected:
                    hint_ocr_chars[hi] = corrected

            # 构建成本矩阵：OCR 匹配 = 0，否则用 Siamese 相似度
            sim_matrix = np.zeros((n_hints, n_boxes))
            ocr_cost = np.ones((n_hints, n_boxes))  # 1 = OCR mismatch
            for hi, part in enumerate(order_parts):
                char_b64 = base64.b64encode(part["bytes"]).decode()
                for bi, box in enumerate(target_boxes):
                    x1, y1, x2, y2 = box
                    cx1 = max(0, min(img_w - 1, x1))
                    cy1 = max(0, min(img_h - 1, y1))
                    cx2 = max(cx1 + 1, min(img_w, x2))
                    cy2 = max(cy1 + 1, min(img_h, y2))
                    crop = decoded[cy1:cy2, cx1:cx2]
                    ok, enc = cv2.imencode(".png", crop)
                    if not ok:
                        continue
                    crop_b64 = base64.b64encode(enc.tobytes()).decode()
                    sim = Atc.Compare_Image_Similarity(
                        image1_base64=char_b64,
                        image2_base64=crop_b64,
                    )
                    if sim is not None:
                        sim_matrix[hi, bi] = float(sim)
                    # OCR 匹配检查
                    if hint_ocr_chars[hi] and answer_ocr_chars[bi]:
                        if hint_ocr_chars[hi] == answer_ocr_chars[bi]:
                            ocr_cost[hi, bi] = 0.0  # 完全匹配

            # 匈牙利算法：OCR 匹配优先（cost=0），否则用 Siamese 成本
            from scipy.optimize import linear_sum_assignment
            # 成本 = OCR 匹配 ? 0 : (1 - sim) × 1.5（OCR 未匹配时给 Siamese 加权）
            cost = np.where(ocr_cost == 0, 0.0, (1.0 - sim_matrix) * 1.5)
            row_ind, col_ind = linear_sum_assignment(cost)

            points: list[tuple[float, float]] = []
            sim_results = []
            for hi, bi in zip(row_ind, col_ind):
                score = sim_matrix[hi, bi]
                ocr_matched = ocr_cost[hi, bi] == 0
                box = target_boxes[bi]
                x1, y1, x2, y2 = box
                sim_results.append({
                    "index": int(hi) + 1,
                    "best_similarity": float(score),
                    "best_box": box,
                    "box_index": int(bi),
                    "ocr_hint": hint_ocr_chars[hi],
                    "ocr_answer": answer_ocr_chars[bi],
                    "ocr_matched": ocr_matched,
                })
                points.append(((x1 + x2) / 2.0, (y1 + y2) / 2.0))

            _write_json(_debug_file(scope, "raw", "anticap_clickword_result.json"), {
                "order_split_count": len(order_parts),
                "order_split_parts": [
                    {k: v for k, v in p.items() if k != "bytes"}
                    for p in order_parts
                ],
                "target_box_count": len(target_boxes),
                "target_boxes": target_boxes,
                "sim_results": sim_results,
                "parsed_points": points,
                "parsed_count": len(points),
                "canvas_box": cbox,
                "ocr": {
                    "hint_chars": hint_ocr_chars,
                    "answer_chars": answer_ocr_chars,
                    "matched": [s["ocr_matched"] for s in sim_results],
                },
            })
            # 终端输出便于分析
            ocr_info = " ".join(
                f"{s['ocr_hint']}→{s['ocr_answer']}{'✓' if s['ocr_matched'] else '✗'}"
                for s in sim_results
            )
            # diagnostic muted: AntiCAP clickword detect details
            if len(points) < 2:    # 至少 2 个点才点击（部分 captcha 只有 2 字）
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False,
                    "reason": "anticap_points_not_3",
                    "parsed_count": len(points),
                    "sim_results": sim_results,
                    "attempt": attempt,
                })
                await _refresh(page, counter=refresh_counter)
                continue

            # 任意字符相似度过低则放弃本轮
            if any(s["best_similarity"] < 0.30 for s in sim_results):
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "ok": False,
                    "reason": "sim_too_low",
                    "sim_results": sim_results,
                    "attempt": attempt,
                })
                await _refresh(page, counter=refresh_counter)
                continue

            # 映射到页面坐标（canvas 已在上面解码，直接用 img_w, img_h）
            sx = cbox["width"] / img_w
            sy = cbox["height"] / img_h

            # 保存答案图标注（匹配框 + 点），方便排查点击位置是否正确
            _save_clickword_debug_image(decoded, sim_results, target_boxes,
                                        _debug_file(scope, "annotated", "answer_matched.png"))

            page_points = []
            for idx, (cx, cy) in enumerate(points):
                px = cbox["x"] + cx * sx
                py = cbox["y"] + cy * sy
                page_points.append({"x": px, "y": py, "label": str(idx + 1)})

            # 页面点已按 sim_results 的 hint 顺序排列，无需重排

            # 去重：相邻点 < 25px 则偏移
            for i in range(1, len(page_points)):
                prev = page_points[i - 1]
                curr = page_points[i]
                if abs(curr["x"] - prev["x"]) < 25 and abs(curr["y"] - prev["y"]) < 25:
                    curr["x"] += 8
                    curr["y"] += 6

            print(f"  [AntiCAP clickword] clicks: " +
                  " ".join(f"({p['x']:.0f},{p['y']:.0f})" for p in page_points),
                  file=sys.stderr)

            # 标记
            await _mark_page_points(page, [
                {"x": p["x"], "y": p["y"], "label": p["label"],
                 "color": "#ff2d55", "radius": 11}
                for p in page_points
            ])
            await _save_screenshot(page, _debug_file(scope, "annotated", "marked.png"))

            # 点击 — 每点一个截一次图，加长延迟便于肉眼观察
            for idx, p in enumerate(page_points):
                await page.mouse.move(p["x"] + random.randint(-5, 5),
                                      p["y"] + random.randint(-5, 5), steps=3)
                await asyncio.sleep(random.uniform(0.06, 0.15))
                await page.mouse.click(p["x"], p["y"])
                # diagnostic muted: click details
                await _save_screenshot(page, _debug_file(scope, "annotated", f"clicked_{idx+1}.png"))
                await asyncio.sleep(random.uniform(0.8, 1.8))

            await asyncio.sleep(1.8)

            _write_json(_debug_file(scope, "raw", "result.json"), {
                "ok": True,
                "attempt": attempt,
                "points": page_points,
                "metrics": await _page_metrics(page),
            })

            if await _captcha_type(page) == "none":
                return True
            await _refresh(page, counter=refresh_counter)

        except Exception as e:
            _write_json(_debug_file(scope, "raw", "error.json"), {
                "error": repr(e), "attempt": attempt,
            })
            print(f"  [AntiCAP clickword] attempt {attempt}: {e}", file=sys.stderr)

    return False

def _split_order_chars(order_bytes: bytes, expected: int = 3) -> list[dict]:
    """从提示条里拆出 expected 个待点击字符。

    目标站点只有一个提示区域图，里面包含 3 个字符；这里按暗色前景连通域切分，
    输出顺序从左到右，供 AntiCAP 单字符匹配。
    """
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(order_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 提示条背景浅，说明文字和目标字符都较深；先取所有暗色连通域，
        # 再从最右侧挑出 3 个字符，避免“请依次点击:”干扰。
        _, mask = cv2.threshold(gray, 145, 255, cv2.THRESH_BINARY_INV)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        count, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        boxes = []
        min_h = max(2, int(h * 0.10))  # 图片 20px 高时允许 2px 字符
        for label in range(1, count):
            x, y, bw, bh, area = stats[label]
            if area < 5 or bw < 1 or bh < min_h:
                continue
            if y > h * 0.88:
                continue
            boxes.append([int(x), int(y), int(bw), int(bh), int(area)])

        if not boxes:
            return []

        boxes.sort(key=lambda b: b[0])
        merged = []
        for box in boxes:
            x, y, bw, bh, area = box
            if not merged:
                merged.append(box)
                continue
            px, py, pw, ph, pa = merged[-1]
            gap = x - (px + pw)
            # 同一字符内部间隔小；双引号、i/j 的点允许更宽的合并距离。
            if gap <= max(6, min(12, int(h * 0.18))):
                nx = min(px, x)
                ny = min(py, y)
                nr = max(px + pw, x + bw)
                nb = max(py + ph, y + bh)
                merged[-1] = [nx, ny, nr - nx, nb - ny, pa + area]
            else:
                merged.append(box)

        candidates = [b for b in merged if b[2] >= 1 and b[3] >= max(2, int(h * 0.10)) and b[4] >= 4]
        if len(candidates) < expected:
            return []

        # 取最右侧 3 个目标字符，而不是面积最大的 3 个；说明文字总在左侧。
        candidates = sorted(candidates, key=lambda b: b[0])[-expected:]
        candidates.sort(key=lambda b: b[0])

        parts: list[dict] = []
        for x, y, bw, bh, area in candidates:
            pad = 8
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(w, x + bw + pad)
            y2 = min(h, y + bh + pad)
            crop = img[y1:y2, x1:x2]
            ok, enc = cv2.imencode(".png", crop)
            if ok:
                parts.append({
                    "bytes": enc.tobytes(),
                    "box": [int(x), int(y), int(bw), int(bh)],
                    "cropBox": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)],
                    "area": int(area),
                })
        return parts
    except Exception:
        return []


def _parse_clickword_result(result) -> list[tuple[float, float]]:
    """从 AntiCAP ClickText_Order 返回值提取点选坐标列表。"""
    points: list[tuple[float, float]] = []

    def _push(x, y):
        try:
            points.append((float(x), float(y)))
            return True
        except Exception:
            return False

    def _walk(obj):
        if obj is None:
            return
        if isinstance(obj, dict):
            # 常见: {'x': 1, 'y': 2}
            if obj.get("x") is not None and obj.get("y") is not None:
                _push(obj.get("x"), obj.get("y"))
                return
            # 常见: {'point': [x,y]} / {'center': [x,y]} / {'position': [x,y]}
            for key in ("point", "center", "position", "pos", "coordinate", "coord"):
                val = obj.get(key)
                if isinstance(val, (list, tuple)) and len(val) >= 2:
                    _push(val[0], val[1])
                    return
            # 常见: {'box': [x1,y1,x2,y2]}
            box = obj.get("box") or obj.get("bbox")
            if isinstance(box, (list, tuple)) and len(box) >= 2:
                cx = (box[0] + box[2]) / 2 if len(box) >= 3 else box[0]
                cy = (box[1] + box[3]) / 2 if len(box) >= 4 else box[1]
                _push(cx, cy)
                return
            # 常见: {'data': [...]}, {'result': [...]}
            for key in ("data", "result", "points", "list", "items"):
                if key in obj:
                    _walk(obj[key])
            return
        if isinstance(obj, (list, tuple)):
            # [x,y]
            if len(obj) >= 2 and all(isinstance(v, (int, float)) for v in obj[:2]):
                _push(obj[0], obj[1])
                return
            # [[x,y], [x,y]] 或 [{...}, {...}]
            for item in obj:
                _walk(item)

    _walk(result)

    # 去重，避免同一个点被多种字段重复解析
    dedup: list[tuple[float, float]] = []
    for x, y in points:
        if not any(abs(x - px) < 2 and abs(y - py) < 2 for px, py in dedup):
            dedup.append((x, y))
    return dedup


# ====================== 类型检测 ======================

async def _captcha_type(page) -> str:
    try:
        text = await page.evaluate("document.body?.innerText?.substring(0,500)||''")
        # 先检查验证成功标志，避免持续重试
        if any(k in text for k in ["验证通过", "验证成功", "校验通过", "校验成功", "✓"]):
            return "none"
        if await page.evaluate(
                "!!document.querySelector('.dx_captcha_clickword, .yidun_tips')"):
            return "clickword"
        if await page.evaluate(
                "!!document.querySelector('.dx_captcha_slider, .dx_captcha_basic, .yidun_slider')"):
            return "slider"
        if any(k in text for k in ["请依次点击", "请点击", "按顺序点击"]):
            return "clickword"
        if any(k in text for k in ["向右滑动", "拖动滑块", "滑动滑块", "完成拼图"]):
            return "slider"
        return "none"
    except Exception:
        return "none"


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


async def _refresh(page, counter=None):
    try:
        r = await page.query_selector(
            "[class*='refresh'], .dx-captcha-refresh, .yidun_refresh")
        if r:
            await r.click()
            await _wait_canvas_ready(page, timeout_ms=8000)
            if counter is not None:
                counter[0] += 1
    except Exception:
        pass


# ====================== 统一入口 ======================

async def solve_captcha_anticap(page, max_attempts: int = 5,
                                debug_dir: Optional[str] = None,
                                refresh_counter=None) -> bool:
    """统一验证码解决入口：AntiCAP 识别 + Playwright 交互。"""
    if _get_handler() is None:
        return False

    for attempt in range(max_attempts):
        ctype = await _captcha_type(page)
        if ctype == "none":
            return True
        print(f"  [AntiCAP] type={ctype} attempt={attempt+1}/{max_attempts}",
              file=sys.stderr)

        if ctype == "slider":
            # AntiCAP 的滑块匹配在当前页面会卡住；滑块交给 v7，AntiCAP 只处理点选。
            return False
        elif ctype == "clickword":
            ok = await _solve_clickword_anticap(page, 2, debug_dir, refresh_counter=refresh_counter)
        else:
            ok = await _solve_clickword_anticap(page, 2, debug_dir, refresh_counter=refresh_counter)

        if ok:
            await asyncio.sleep(1.5)
            if await _captcha_type(page) == "none":
                return True
        await _refresh(page, counter=refresh_counter)
    return False