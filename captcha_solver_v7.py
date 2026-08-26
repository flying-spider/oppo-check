#!/usr/bin/env python3
"""
验证码求解器 v7 - 经真实截图验证

滑块(滑动拼图):
  - 缺口检测以"暗色凹槽 + 边缘强度"为主, 明确排除左侧滑块本体区域,
    避免旧版把缺口误判到滑块自身(旧版 gap_x=58, 真实缺口在 ~205)。
  - 已用 diag_captcha/slider_canvas_v2.png 验证: gap_x=208, 正中缺口。
  - 计算滑动距离时用 canvas 图像宽度与轨道宽度做等比换算, 拟人轨迹拖动。

点选(点选文字):
  - 提示图去透明背景后, 多方法(边缘/灰度/多尺度)模板匹配定位, 按顺序点击。

两个检测函数都是纯函数, 可脱机对图片单测; 见文件末尾 self-test。
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

import numpy as np
import cv2

HAS_SIFT = hasattr(cv2, "SIFT_create")


def _debug_scope(debug_dir: str | None, label: str) -> str | None:
    """为一次验证码处理生成独立诊断路径。目录在真正写入时才创建。"""
    if not debug_dir:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scope = Path(debug_dir) / f"{stamp}_{label}_{os.getpid()}_{random.randint(1000, 9999)}"
    return str(scope)


def _debug_file(debug_dir: str | None, group: str, name: str) -> Path | None:
    return None  # debug 截图已禁用


def _write_json(path: Path | None, payload: dict) -> None:
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


async def _save_viewport_screenshot(page, path) -> None:
    """诊断截图已禁用"""
    pass


async def _page_metrics(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const vv = window.visualViewport;
                return {
                    url: location.href,
                    dpr: window.devicePixelRatio || 1,
                    viewport: {width: window.innerWidth, height: window.innerHeight},
                    visualViewport: vv ? {
                        width: vv.width, height: vv.height,
                        offsetLeft: vv.offsetLeft, offsetTop: vv.offsetTop,
                        scale: vv.scale,
                    } : null,
                    scroll: {x: window.scrollX, y: window.scrollY},
                    document: {
                        width: document.documentElement.scrollWidth,
                        height: document.documentElement.scrollHeight,
                    },
                };
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)}


async def _normalize_captcha_view(page) -> dict:
    """只做可视域修正，不改验证码坐标系；坐标仍以 bounding_box 为准。"""
    try:
        return await page.evaluate(
            """() => {
                const styleId = '__captcha_visible_debug_style';
                if (!document.getElementById(styleId)) {
                    const style = document.createElement('style');
                    style.id = styleId;
                    style.textContent = `
                        html, body { overflow: visible !important; }
                        [class*="captcha"], [class*="Captcha"], [class*="dx_captcha"], [class*="yidun"] {
                            max-width: none !important;
                            max-height: none !important;
                        }
                    `;
                    document.documentElement.appendChild(style);
                }
                const selectors = [
                    '.dx_captcha_clickword', '.dx_captcha_slider', '.dx_captcha_basic',
                    '.dx_captcha', '.yidun_popup', '.yidun_panel', '.yidun',
                    '[class*="captcha"]', '[class*="yidun"]'
                ];
                let target = null;
                for (const selector of selectors) {
                    target = document.querySelector(selector);
                    if (target) break;
                }
                if (target && target.scrollIntoView) {
                    target.scrollIntoView({block: 'center', inline: 'center'});
                }
                const rect = target ? target.getBoundingClientRect() : null;
                return {
                    target: target ? String(target.className || target.id || target.tagName) : '',
                    rect: rect ? {
                        x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                        top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left,
                    } : null,
                    viewport: {width: window.innerWidth, height: window.innerHeight},
                    dpr: window.devicePixelRatio || 1,
                };
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)}


async def _mark_page_points(page, points: list[dict]) -> None:
    safe_points = []
    for item in points:
        x = item.get("x")
        y = item.get("y")
        if x is None or y is None:
            continue
        safe_points.append({
            "x": float(x),
            "y": float(y),
            "label": str(item.get("label", "")),
            "color": str(item.get("color", "#ff2d55")),
            "radius": float(item.get("radius", 10)),
        })
    if not safe_points:
        return
    try:
        await page.evaluate(
            """(points) => {
                const old = document.getElementById('__captcha_debug_overlay');
                if (old) old.remove();
                const layer = document.createElement('div');
                layer.id = '__captcha_debug_overlay';
                Object.assign(layer.style, {
                    position: 'fixed', left: '0', top: '0', width: '0', height: '0',
                    zIndex: '2147483647', pointerEvents: 'none'
                });
                for (const p of points) {
                    const r = p.radius || 10;
                    const dot = document.createElement('div');
                    dot.textContent = p.label || '';
                    Object.assign(dot.style, {
                        position: 'fixed',
                        left: `${p.x - r}px`, top: `${p.y - r}px`,
                        width: `${r * 2}px`, height: `${r * 2}px`,
                        border: `3px solid ${p.color || '#ff2d55'}`,
                        borderRadius: '999px',
                        background: 'rgba(255,45,85,0.12)',
                        color: p.color || '#ff2d55',
                        font: 'bold 12px sans-serif',
                        lineHeight: `${r * 2}px`, textAlign: 'center',
                        boxSizing: 'border-box',
                        textShadow: '0 1px 2px white',
                    });
                    layer.appendChild(dot);
                }
                document.documentElement.appendChild(layer);
            }""",
            safe_points,
        )
    except Exception:
        pass


def _write_clickword_debug_image(canvas_bytes: bytes, matches: list[dict], path: Path | None) -> None:
    if not path:
        return
    try:
        img = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        for item in matches:
            image = item.get("image", {})
            cx = int(round(image.get("x", -1)))
            cy = int(round(image.get("y", -1)))
            if cx < 0 or cy < 0:
                continue
            label = str(item.get("label", ""))
            cv2.circle(img, (cx, cy), 12, (0, 0, 255), 2)
            cv2.drawMarker(img, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 18, 2)
            cv2.putText(img, label, (cx + 8, max(15, cy - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        cv2.imwrite(str(path), img)
    except Exception:
        pass


# ====================== 滑块缺口检测 ======================

def _detect_slider_piece(gray: np.ndarray, piece_w: int) -> dict:
    """识别左侧待拖动拼图片中心；失败时用尺寸估算给出稳定兜底。"""
    h, w = gray.shape[:2]
    search_r = min(w, max(int(piece_w * 2.4), int(w * 0.30)))
    roi = cv2.GaussianBlur(gray[:, :search_r], (5, 5), 0)
    candidates: list[dict] = []

    min_radius = max(8, int(piece_w * 0.25))
    max_radius = max(min_radius + 4, int(piece_w * 0.72))
    try:
        circles = cv2.HoughCircles(
            roi,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(12, piece_w * 0.6),
            param1=90,
            param2=16,
            minRadius=min_radius,
            maxRadius=max_radius,
        )
    except Exception:
        circles = None

    if circles is not None:
        for x, y, r in np.round(circles[0]).astype(int):
            if x < r * 0.35 or y < r * 0.35 or y > h - r * 0.35:
                continue
            if x > search_r - r * 0.15:
                continue
            expected_x = piece_w * 0.92
            expected_r = piece_w * 0.45
            x_score = max(0.0, 1.0 - abs(x - expected_x) / max(1.0, expected_x))
            r_score = max(0.0, 1.0 - abs(r - expected_r) / max(1.0, expected_r))
            y_score = max(0.0, 1.0 - abs(y - h / 2.0) / max(1.0, h / 2.0))
            candidates.append({
                "x": float(x), "y": float(y), "radius": float(r),
                "score": float(x_score * 1.2 + r_score + y_score * 0.3),
                "method": "hough_circle",
            })

    if candidates:
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[0]

    return {
        "x": float(min(max(piece_w * 0.92, piece_w * 0.5), w * 0.32)),
        "y": float(h / 2.0),
        "radius": float(piece_w * 0.45),
        "score": 0.0,
        "method": "size_estimate",
    }


def find_gap_v7(canvas_bytes: bytes, piece_w: int | None = None,
                debug_path: str | None = None) -> dict:
    """检测滑块缺口在 canvas 图像中的中心坐标。

    返回: {gap_x, gap_y, confidence, canvas_w, canvas_h, method}
    当前页面里的缺口是暗色圆形块，优先走组件形状识别；只有找不到圆形块时才回退到列信号。
    """
    arr = np.frombuffer(canvas_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"gap_x": -1, "gap_y": -1, "confidence": 0.0}

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    pw = piece_w if piece_w else max(int(w * 0.12), 40)
    piece = _detect_slider_piece(gray, pw)
    lm, rm = max(int(piece["x"] + pw * 1.2), int(w * 0.18)), w - 8
    if rm <= lm + 5:
        lm, rm = int(w * 0.15), w - 5

    roi = blur[:, lm:rm]
    candidates: list[dict] = []
    if roi.size:
        levels = np.percentile(roi, [4, 7, 10, 14, 18])
        thresholds = sorted({
            int(max(25, min(150, levels[0] + 16))),
            int(max(25, min(150, levels[1] + 12))),
            int(max(25, min(150, levels[2] + 8))),
            int(max(25, min(150, levels[3]))),
            int(max(25, min(150, levels[4] - 4))),
        })

        min_d = max(18, int(pw * 0.55))
        max_d = max(min_d + 8, int(pw * 2.3))
        min_area = max(180, int(pw * pw * 0.22))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

        for thr in thresholds:
            mask = np.zeros_like(blur, dtype=np.uint8)
            mask[(blur <= thr)] = 255
            mask[:, :lm] = 0
            mask[:, rm:] = 0
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

            count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
            for label in range(1, count):
                x, y, bw, bh, area = stats[label]
                if area < min_area:
                    continue
                if bw < min_d or bh < min_d or bw > max_d or bh > max_d:
                    continue
                if x < lm or x + bw > rm:
                    continue

                fill = area / max(1, bw * bh)
                roundness = min(bw, bh) / max(bw, bh)
                if fill < 0.34 or roundness < 0.58:
                    continue

                cx, cy = centroids[label]
                pixels = gray[labels == label]
                darkness = 1.0 - float(pixels.mean()) / 255.0 if pixels.size else 0.0
                expected_d = max(float(pw), w * 0.09)
                actual_d = (bw + bh) / 2.0
                size_score = max(0.0, 1.0 - abs(actual_d - expected_d) / expected_d)
                mid_y_score = max(0.0, 1.0 - abs(cy - h / 2.0) / max(1.0, h / 2.0))
                score = (
                    darkness * 2.2
                    + roundness * 1.1
                    + fill * 0.7
                    + size_score * 0.6
                    + mid_y_score * 0.25
                )
                # 缺口应在 canvas 右侧，左侧候选降权（防止 piece 区暗色组件被误判）
                rightness = max(0.0, (cx - w * 0.35) / max(1.0, w * 0.35))
                score += rightness * 0.5
                candidates.append({
                    "x": float(cx), "y": float(cy),
                    "bbox": [int(x), int(y), int(bw), int(bh)],
                    "area": int(area),
                    "threshold": int(thr),
                    "darkness": float(darkness),
                    "roundness": float(roundness),
                    "fill": float(fill),
                    "score": float(score),
                })

    candidates.sort(key=lambda item: item["score"], reverse=True)
    if candidates:
        best = candidates[0]
        gap_x = int(round(best["x"]))
        gap_y = int(round(best["y"]))
        conf = float(best["score"])
        method = "dark_round_component"
        # 诊断：打印 top-3 candidates
        print(f"  [find_gap] detection: gap=({gap_x},{gap_y}) conf={conf:.2f} "
              f"lm={lm} rm={rm} n_cands={len(candidates)}",
              file=sys.stderr)
        for i, c in enumerate(candidates[:3]):
            bx, by, bw, bh = c["bbox"]
            print(f"  [find_gap]   #{i+1}: ({c['x']:.0f},{c['y']:.0f}) "
                  f"bbox=[{bx},{by},{bw}x{bh}] "
                  f"dark={c['darkness']:.3f} round={c['roundness']:.3f} "
                  f"fill={c['fill']:.3f} score={c['score']:.2f}",
                  file=sys.stderr)
    else:
        col_mean = gray.mean(axis=0).astype(float)
        dark = col_mean.max() - col_mean
        dark[:lm] = 0
        dark[rm:] = 0
        dark = np.convolve(dark, np.ones(7) / 7, mode="same")

        edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 40, 120)
        col_edges = edges.sum(axis=0).astype(float)
        col_edges[:lm] = 0
        col_edges[rm:] = 0

        def _norm(a):
            m = a.max()
            return a / m if m > 0 else a

        dark_n = _norm(dark)
        edge_n = _norm(col_edges)
        score = 0.75 * dark_n + 0.25 * edge_n
        k = max(5, w // 45)
        score = np.convolve(score, np.ones(k) / k, mode="same")
        score[:lm] = 0
        score[rm:] = 0
        gap_x = int(np.argmax(score))
        gap_y = h // 2
        conf = float(score[gap_x])
        method = "column_fallback"

    if debug_path:
        dbg = img.copy()
        for idx, item in enumerate(candidates[:5], start=1):
            x, y, bw, bh = item["bbox"]
            color = (0, 255, 0) if idx == 1 else (0, 180, 255)
            cv2.rectangle(dbg, (x, y), (x + bw, y + bh), color, 1)
            cv2.putText(dbg, f"{idx}:{item['score']:.2f}", (x, max(12, y - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        cv2.circle(dbg, (gap_x, gap_y), max(8, int(pw * 0.45)), (0, 255, 0), 2)
        cv2.drawMarker(dbg, (gap_x, gap_y), (0, 0, 255), cv2.MARKER_CROSS, 18, 2)
        piece_x = int(round(piece.get("x", pw * 0.92)))
        piece_y = int(round(piece.get("y", h / 2.0)))
        piece_r = int(round(piece.get("radius", pw * 0.45)))
        cv2.circle(dbg, (piece_x, piece_y), max(6, piece_r), (255, 0, 0), 2)
        cv2.drawMarker(dbg, (piece_x, piece_y), (255, 0, 0), cv2.MARKER_CROSS, 16, 2)
        cv2.line(dbg, (gap_x, 0), (gap_x, h), (0, 255, 0), 1)
        cv2.putText(dbg, f"gap=({gap_x},{gap_y}) {method} c={conf:.2f}", (5, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imwrite(debug_path, dbg)

    return {
        "gap_x": gap_x,
        "gap_y": gap_y,
        "confidence": conf,
        "canvas_w": w,
        "canvas_h": h,
        "method": method,
        "piece": piece,
        "candidates": candidates[:5],
    }


def _human_track(distance: float, steps: int | None = None) -> list[float]:
    """拟人滑动：纯 smoothstep (3p²-2p³)，无抖动。"""
    if steps is None:
        steps = max(45, min(60, int(distance * 0.5)))
    xs: list[float] = []
    for i in range(steps):
        p = (i + 1) / steps
        eased = 3 * p * p - 2 * p * p * p
        xs.append(distance * eased)
    if xs:
        xs[-1] = distance
    return xs


async def solve_slider_v7(page, max_attempts: int = 5,
                          debug_dir: str | None = None,
                          refresh_counter=None) -> bool:
    """完成滑块验证码: 检测缺口 -> 页面坐标映射 -> 拟人拖动 -> 校验。"""
    for attempt in range(max_attempts):
        scope = _debug_scope(debug_dir, f"slider_{attempt}")
        try:
            await asyncio.sleep(1.2)
            view_fix = await _normalize_captcha_view(page)
            await _save_viewport_screenshot(page, _debug_file(scope, "raw", "01_before.png"))

            slider = await page.query_selector(
                ".dx_captcha_basic_slider, .dx_captcha_slider, "
                "[class*='slider'][class*='btn'], .yidun_slider")
            bar = await page.query_selector(
                ".dx_captcha_basic_bar, .dx_captcha_bar, .yidun_bgimg, "
                "[class*='slider'][class*='bar'], [class*='track']")
            canvas = await page.query_selector(
                ".dx_captcha_basic_box canvas, .dx_captcha_slider canvas, "
                ".dx_captcha_basic_bg, canvas")
            if not slider:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "type": "slider", "attempt": attempt, "ok": False,
                    "reason": "slider_not_found", "viewFix": view_fix,
                    "metrics": await _page_metrics(page),
                })
                return False

            btn_box = await slider.bounding_box()
            if not btn_box:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "type": "slider", "attempt": attempt, "ok": False,
                    "reason": "slider_box_missing", "viewFix": view_fix,
                    "metrics": await _page_metrics(page),
                })
                return False

            bar_box = await bar.bounding_box() if bar else None
            canvas_bytes = await canvas.screenshot(timeout=5000) if canvas else None
            cbox = await canvas.bounding_box() if canvas else None
            if not canvas_bytes or not cbox:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "type": "slider", "attempt": attempt, "ok": False,
                    "reason": "canvas_missing", "viewFix": view_fix,
                    "sliderBox": btn_box, "barBox": bar_box,
                    "metrics": await _page_metrics(page),
                })
                return False

            _write_bytes(_debug_file(scope, "raw", "canvas.png"), canvas_bytes)

            decoded = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                return False
            canvas_h, canvas_w = decoded.shape[:2]
            css_per_image_x = cbox["width"] / canvas_w
            css_per_image_y = cbox["height"] / canvas_h
            piece_w_img = max(1, int(round(btn_box["width"] / css_per_image_x)))

            gap_debug = _debug_file(scope, "annotated", "gap_detection.png")
            info = await asyncio.to_thread(find_gap_v7, canvas_bytes, piece_w=piece_w_img,
                               debug_path=str(gap_debug) if gap_debug else None)
            gap_x = info["gap_x"]
            gap_y = info.get("gap_y", int(canvas_h * 0.5))
            gap_method = info.get("method", "?")
            gap_conf = info.get("confidence", 0.0)
            gap_cands = len(info.get("candidates", []))

            # 检测质量门控：极低置信度跳过本轮（但尝试拖动，失败会触发新验证码类型）
            if gap_conf < 0.3:
                print(f"  [slider v7] ⚠ 检测质量极低: method={gap_method} conf={gap_conf:.2f} "
                      f"cands={gap_cands}，放弃本次 attempt",
                      file=sys.stderr)
                print(f"  [slider v7]   DIAG: gap_detection details — gap=({gap_x},{gap_y}) "
                      f"piece=({info.get('piece',{}).get('x',-1):.0f},{info.get('piece',{}).get('y',-1):.0f})",
                      file=sys.stderr)
                return False
            print(f"  [slider v7] 检测: method={gap_method} conf={gap_conf:.2f} "
                  f"cands={gap_cands} gap=({gap_x},{gap_y})", file=sys.stderr)
            if gap_x < 0:
                gap_x = int(canvas_w * 0.5)
            if gap_y < 0:
                gap_y = int(canvas_h * 0.5)

            track_w = (bar_box["width"] - btn_box["width"]) if bar_box and bar_box["width"] > 10 else (cbox["width"] - btn_box["width"])
            gap_css_x = gap_x * css_per_image_x
            gap_css_y = gap_y * css_per_image_y
            # 用检测到的圆形组件中心 + 0.92 乘子
            piece_info = info.get("piece") or {}
            piece_img_x = float(piece_info.get("x", piece_w_img * 0.92))
            piece_img_y = float(piece_info.get("y", canvas_h * 0.5))
            piece_css_x = piece_img_x * css_per_image_x
            piece_css_y = piece_img_y * css_per_image_y
            start_x = btn_box["x"] + btn_box["width"] / 2
            start_y = btn_box["y"] + btn_box["height"] / 2
            gap_page_x = cbox["x"] + gap_css_x
            gap_page_y = cbox["y"] + gap_css_y
            piece_page_x = cbox["x"] + piece_css_x
            piece_page_y = cbox["y"] + piece_css_y
            raw_slide_dist = gap_page_x - piece_page_x
            slide_dist = max(5.0, min(track_w, raw_slide_dist))   # 直接使用 gap-piece 距离，不预设 ratio

            end_x = start_x + slide_dist
            end_y = start_y
            track = _human_track(slide_dist)

            print(f"  [slider v7] gap=({gap_x},{gap_y}) piece=({piece_img_x},{piece_img_y}) "
                  f"raw={raw_slide_dist:.0f} slide={slide_dist:.1f} "
                  f"[{gap_method} conf={gap_conf:.2f} cands={gap_cands}] "
                  f"start=({start_x:.0f},{start_y:.0f}) end=({end_x:.0f},{end_y:.0f}) "
                  f"cbox=({cbox['x']:.0f},{cbox['y']:.0f},{cbox['width']:.0f}x{cbox['height']:.0f}) "
                  f"attempt={attempt+1}",
                  file=sys.stderr)

            debug_payload = {
                "type": "slider",
                "attempt": attempt,
                "viewFix": view_fix,
                "metricsBefore": await _page_metrics(page),
                "boxes": {"slider": btn_box, "bar": bar_box, "canvas": cbox},
                "image": {"width": canvas_w, "height": canvas_h},
                "scale": {
                    "cssPerImageX": css_per_image_x,
                    "cssPerImageY": css_per_image_y,
                    "pieceWidthImage": piece_w_img,
                },
                "detection": info,
                "mapping": {
                    "gapImageX": gap_x,
                    "gapImageY": gap_y,
                    "gapCssX": gap_css_x,
                    "gapCssY": gap_css_y,
                    "gapPageX": gap_page_x,
                    "gapPageY": gap_page_y,
                    "pieceImageX": piece_img_x,
                    "pieceImageY": piece_img_y,
                    "pieceCssX": piece_css_x,
                    "pieceCssY": piece_css_y,
                    "piecePageX": piece_page_x,
                    "piecePageY": piece_page_y,
                    "rawSlideDistance": raw_slide_dist,
                    "trackWidth": track_w,
                    "slideDistance": slide_dist,
                    "start": {"x": start_x, "y": start_y},
                    "end": {"x": end_x, "y": end_y},
                },
                "track": track,
            }

            await _mark_page_points(page, [
                {"x": start_x, "y": start_y, "label": "S", "color": "#007aff", "radius": 11},
                {"x": piece_page_x, "y": piece_page_y, "label": "P", "color": "#5856d6", "radius": 11},
                {"x": end_x, "y": end_y, "label": "E", "color": "#ff2d55", "radius": 11},
                {"x": gap_page_x, "y": gap_page_y, "label": "G", "color": "#34c759", "radius": 11},
            ])
            await _save_viewport_screenshot(page, _debug_file(scope, "annotated", "02_before_drag_marked.png"))

            await page.mouse.move(start_x, start_y)
            await asyncio.sleep(random.uniform(0.1, 0.2))
            await page.mouse.down()

            # 第一阶段：粗略拖动到计算位置
            for dx in track:
                jitter_y = start_y + random.randint(-3, 3)
                await page.mouse.move(start_x + dx, jitter_y)
                await asyncio.sleep(random.uniform(0.012, 0.020))
            await page.mouse.move(end_x, start_y + random.randint(-1, 1))
            await asyncio.sleep(0.2)

            # ===== 修正：按钮坐标循环检测 + 分段瞬时 ratio 修正 =====
            if canvas:
                cb = await canvas.screenshot(timeout=5000)
                _write_bytes(_debug_file(scope, "annotated", "修正前_canvas.png"), cb)
            correction = 0.0

            # 分段 ratio 状态：记录上轮按钮/鼠标位置，用于计算瞬时效率
            btn_start_val = btn_box["x"] + btn_box["width"] / 2
            prev_btn_cur = btn_start_val
            prev_end_x = end_x
            prev_piece_x = None
            piece_dom_valid = True   # 假设 piece DOM 有效，首次修正后验证

            for corr_round in range(4):
                await asyncio.sleep(0.08)  # 给 DOM 稳定时间
                slider2 = await page.query_selector(
                    ".dx_captcha_basic_slider, .dx_captcha_slider, "
                    "[class*='slider'][class*='btn'], .yidun_slider")
                btn_box2 = await slider2.bounding_box() if slider2 else None

                # 直接查询 piece（拼图块）的 DOM 位置，比推算更准确
                piece_el = await page.query_selector(
                    "img[class*='slider'], [class*='dx_captcha'] img, "
                    "[class*='slider-sub'], [class*='slider_mask']")
                piece_box = await piece_el.bounding_box() if piece_el else None

                if not (btn_box2 and gap_page_x and btn_box):
                    break
                btn_start = btn_box["x"] + btn_box["width"] / 2
                btn_cur = btn_box2["x"] + btn_box2["width"] / 2
                btn_move = btn_cur - btn_start
                mouse_move = end_x - start_x
                ratio_total = btn_move / max(1, mouse_move)

                # 分段瞬时 ratio：只统计当前段的行为，比累计平均更准
                delta_btn = btn_cur - prev_btn_cur
                delta_mouse = end_x - prev_end_x
                seg_ratio = delta_btn / max(1, delta_mouse) if delta_mouse > 0 else ratio_total

                # 修正用 ratio：优先分段 ratio（首轮回退累计 ratio）
                use_ratio = seg_ratio if seg_ratio > 0.05 else ratio_total

                # 修正目标：piece 对齐 gap
                # 优先使用 piece 元素的实测 DOM 位置（精确），回退到 btn 推算
                if piece_box and piece_dom_valid:
                    piece_cur_dom = piece_box["x"] + piece_box["width"] / 2

                    # 验证 piece DOM 是否真的在移动（防止选错静态元素）
                    if prev_piece_x is not None and abs(delta_btn) > 0.5:
                        delta_piece = piece_cur_dom - prev_piece_x
                        if abs(delta_piece) < 0.5:
                            piece_dom_valid = False
                            print(f"  [slider v7]   DIAG: piece DOM stale (delta_piece={delta_piece:.1f}, "
                                  f"delta_btn={delta_btn:.1f}) → fallback to calc",
                                  file=sys.stderr)
                    if piece_dom_valid:
                        piece_cur = piece_cur_dom
                        prev_piece_x = piece_cur_dom
                        piece_from = "DOM"
                    else:
                        piece_cur = piece_page_x + (btn_cur - btn_start_val)
                        piece_from = "calc"
                else:
                    piece_cur = piece_page_x + (btn_cur - btn_start_val)
                    piece_from = "calc"
                remaining = gap_page_x - piece_cur
                # 诊断日志: btn_box2 原始值 + 全精度浮点数 + piece 来源
                print(f"  [slider v7] 修正轮{corr_round+1}: btn_move={btn_move:.1f} "
                      f"mouse={mouse_move:.1f} ratio={ratio_total:.3f} seg={seg_ratio:.3f} "
                      f"gap={gap_page_x:.1f} btn={btn_cur:.1f} piece={piece_cur:.1f}({piece_from}) rem={remaining:.1f}",
                      file=sys.stderr)
                print(f"  [slider v7]   DIAG: btn_box2=(x={btn_box2['x']:.1f}, y={btn_box2['y']:.1f}, "
                      f"w={btn_box2['width']:.1f}, h={btn_box2['height']:.1f})"
                      f"{' piece_box=(x=' + str(piece_box['x']) + ')' if piece_box else ' piece_box=None'}  "
                      f"delta_btn={delta_btn:.1f} delta_mouse={delta_mouse:.1f}"
                      f"{' REVERSE' if delta_mouse < 0 else ''}  "
                      f"end_x={end_x:.1f} prev_end_x={prev_end_x:.1f}",
                      file=sys.stderr)
                if abs(remaining) <= 1.0:    # piece 与 gap 对齐误差 ≤ 1px 即到位
                    break

                # 修正量：分段 ratio 推算 + 限幅（防止死区段推算过度）
                raw_correction = remaining / max(0.3, abs(use_ratio))
                MAX_REVERSE = 60
                MAX_FORWARD = track_w * 0.5    # 单轮正向修正不超过轨道宽度的 50%（防死区段过度推算）
                if raw_correction < -MAX_REVERSE:
                    print(f"  [slider v7] 修正轮{corr_round+1}: "
                          f"反向修正过大({raw_correction:.0f}px < -{MAX_REVERSE}px)，放弃修正",
                          file=sys.stderr)
                    break
                if raw_correction > MAX_FORWARD:
                    print(f"  [slider v7] 修正轮{corr_round+1}: "
                          f"正向修正缩减 {raw_correction:.0f} → {MAX_FORWARD:.0f}",
                          file=sys.stderr)
                    raw_correction = MAX_FORWARD
                correction = max(-track_w, min(track_w, raw_correction))

                # 最小修正振幅：确保滑块能响应（<2px 鼠标移动可能不触发按钮位移）
                MIN_CORR = 2.0
                if 0 < abs(correction) < MIN_CORR:
                    correction = MIN_CORR * (1 if correction > 0 else -1)

                corr_steps = max(10, int(abs(correction) * 0.6))
                for i in range(corr_steps):
                    p = (i + 1) / corr_steps
                    await page.mouse.move(end_x + correction * p,
                                          start_y + random.randint(-2, 2))
                    await asyncio.sleep(random.uniform(0.020, 0.035))

                # 保存本轮状态，供下轮计算 seg_ratio
                prev_btn_cur = btn_cur
                prev_end_x = end_x
                end_x += correction
                print(f"  [slider v7]   DIAG: correction={correction:.1f} steps={corr_steps} "
                      f"end_x {prev_end_x:.1f}→{end_x:.1f}",
                      file=sys.stderr)
                await asyncio.sleep(0.05)

            if canvas:
                cb = await canvas.screenshot(timeout=5000)
                _write_bytes(_debug_file(scope, "annotated", "修正后_canvas.png"), cb)

            # 最终释放
            # 释放前诊断：对比 piece 参考位置
            slider_final = await page.query_selector(
                ".dx_captcha_basic_slider, .dx_captcha_slider, "
                "[class*='slider'][class*='btn'], .yidun_slider")
            if slider_final:
                fb = await slider_final.bounding_box()
                if fb:
                    btn_final = fb["x"] + fb["width"] / 2
                    piece_final = piece_page_x + (btn_final - btn_start_val)
                    print(f"  [slider v7]   DIAG: before release — btn={btn_final:.1f} "
                          f"btn_x={fb['x']:.0f} btn_w={fb['width']:.0f} "
                          f"piece={piece_final:.1f} gap={gap_page_x:.1f} "
                          f"err={gap_page_x-piece_final:+.1f} "
                          f"ref_piecePageX={piece_page_x:.0f} (from detection piece_x={piece_img_x:.1f})",
                          file=sys.stderr)
            await _save_viewport_screenshot(page, _debug_file(scope, "annotated", "02c_before_release.png"))
            await page.mouse.up()

            await asyncio.sleep(8.0)
            result_type = await captcha_type_v7(page)
            # 如果第一次检查还是 slider，再等 5 秒（慢代理下服务器可能还没响应完）
            if result_type == "slider":
                await asyncio.sleep(5)
                result_type = await captcha_type_v7(page)
            debug_payload["action"] = {
                "final": {"x": end_x + correction, "y": start_y},
                "captchaTypeAfter": result_type,
                "ok": result_type == "none",
            }
            debug_payload["metricsAfter"] = await _page_metrics(page)
            _write_json(_debug_file(scope, "raw", "result.json"), debug_payload)
            await _save_viewport_screenshot(page, _debug_file(scope, "raw", "03_after_drag.png"))

            if result_type == "none":
                return True

            await _refresh(page, counter=refresh_counter)
        except Exception as e:  # noqa: BLE001
            _write_json(_debug_file(scope, "raw", "error.json"), {
                "type": "slider", "attempt": attempt, "error": repr(e),
            })
            print(f"  [slider v7] attempt {attempt}: {e}", file=sys.stderr)
    return False


# ====================== 点选文字检测 ======================

def _hint_to_solid_binary(hint_gray: np.ndarray) -> np.ndarray:
    """把空心粗体字的 hint 转成实心二值模板。

    流程：
      gray → 二值化 → 形态学闭运算填充空心 → 裁剪到字符 bbox

    对极宽字符（如空心粗体 'W'/'w'），闭运算核用椭圆+横向收窄，
    避免过度膨胀。
    """
    h, w = hint_gray.shape[:2]

    # 多策略二值化：先试 Otsu，再试自适应，取最好的
    _, bin1 = cv2.threshold(hint_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    r1 = np.sum(bin1 > 0) / bin1.size

    bin2 = cv2.adaptiveThreshold(
        hint_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, blockSize=11, C=4,
    )
    r2 = np.sum(bin2 > 0) / bin2.size

    bin3 = cv2.adaptiveThreshold(
        hint_gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV, blockSize=11, C=4,
    )
    r3 = np.sum(bin3 > 0) / bin3.size

    # 选择暗像素比例最合理的（10%~50% 为正常字符范围）
    # 对于空心粗体字，暗像素通常是轮廓线，占比不应该太高
    candidates = [(r1, bin1, "otsu"), (r2, bin2, "gauss"), (r3, bin3, "mean")]
    # 偏好 10%~40%
    best_bin = bin1
    best_score = 999
    for ratio, b, name in candidates:
        dist = abs(ratio - 0.25)  # 目标 ~25%
        # 但完全不合理（<3% 或 >60%）要惩罚
        if ratio < 0.03 or ratio > 0.60:
            dist += 10
        if dist < best_score:
            best_score = dist
            best_bin = b

    binary = best_bin

    # 形态学闭运算填充空心：核的垂直方向可以大（跨越空心线宽），
    # 水平方向保持小（避免把相邻字符连起来）
    # 对极宽字符（w > 3h），垂直核加大
    if w > 3 * h:
        k_h = min(9, max(3, h // 2))
        k_w = max(2, k_h // 3)
    else:
        k_h = min(7, max(2, h // 3))
        k_w = k_h
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_w, k_h))

    # 第一次膨胀：填充空心
    filled = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=2)

    # 裁剪到字符 bbox
    ys, xs = np.where(filled > 0)
    if len(xs) == 0:
        return filled
    x1 = max(0, xs.min() - 2)
    x2 = min(filled.shape[1] - 1, xs.max() + 2)
    y1 = max(0, ys.min() - 1)
    y2 = min(filled.shape[0] - 1, ys.max() + 1)
    return filled[y1:y2 + 1, x1:x2 + 1]


def _answer_to_binary(answer_gray: np.ndarray) -> np.ndarray:
    """把答案图转成二值图，白色(255)=字符区域。

    使用自适应阈值而非全局 Otsu，因为答案图亮度不均匀。
    自适应阈值后再做形态学去噪。
    """
    blur = cv2.GaussianBlur(answer_gray, (3, 3), 0)
    # CLAHE 对比度增强
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(blur)
    # 自适应高斯阈值：局部对比度提取字符
    binary = cv2.adaptiveThreshold(
        enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, blockSize=15, C=6,
    )
    # 形态学去噪：小连通域视为噪声
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return cleaned


def match_hint_v9(canvas_bytes: bytes, hint_bytes: bytes,
                  disp_w: int, disp_h: int,
                  exclude: list | None = None) -> tuple:
    """v9: 空心粗体字匹配，大小写不敏感。

    策略（按优先级）：
      1. 距离变换匹配（对形状变化最鲁棒）
      2. 二值形状匹配（大小写不敏感）
      3. 梯度匹配（fallback）

    如果 distance + binary 在同一区域（中心距离 <30px），置信度加倍。
    """
    ca = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
    hi = cv2.imdecode(np.frombuffer(hint_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if ca is None or hi is None:
        return (-1, -1, 0.0, "decode_error")

    ch, cw = ca.shape[:2]

    # ---- hint 预处理 ----
    if hi.ndim == 3 and hi.shape[2] == 4:
        alpha = hi[:, :, 3]
        rgb = hi[:, :, :3].copy()
        for cc in range(3):
            rgb[:, :, cc] = np.where(alpha > 80, rgb[:, :, cc], 255)
        hint_gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    elif hi.ndim == 3:
        hint_gray = cv2.cvtColor(hi, cv2.COLOR_BGR2GRAY)
    else:
        hint_gray = hi

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
    hint_gray = clahe.apply(hint_gray)

    # 空心填充
    hint_solid = _hint_to_solid_binary(hint_gray)

    # ---- answer 预处理 ----
    answer_gray = cv2.cvtColor(ca, cv2.COLOR_BGR2GRAY)
    if exclude:
        for ex, ey, er in exclude:
            cv2.circle(answer_gray, (int(ex), int(ey)), int(er), 255, -1)

    answer_binary = _answer_to_binary(answer_gray)

    # ---- 距离变换（主要策略） ----
    hint_dt = cv2.distanceTransform(hint_solid, cv2.DIST_L2, 5)
    hint_dt = (hint_dt / max(1, hint_dt.max())).astype(np.float32)

    answer_dt = cv2.distanceTransform(answer_binary, cv2.DIST_L2, 5)
    answer_dt = (answer_dt / max(1, answer_dt.max())).astype(np.float32)

    # ---- 梯度（fallback） ----
    answer_blur = cv2.GaussianBlur(answer_gray, (3, 3), 0)
    ax = cv2.Sobel(answer_blur, cv2.CV_64F, 1, 0, ksize=3)
    ay = cv2.Sobel(answer_blur, cv2.CV_64F, 0, 1, ksize=3)
    answer_grad = np.sqrt(ax ** 2 + ay ** 2)
    answer_grad = np.clip(answer_grad, 0, 255).astype(np.uint8)

    hint_blur = cv2.GaussianBlur(hint_gray, (3, 3), 0)
    hx = cv2.Sobel(hint_blur, cv2.CV_64F, 1, 0, ksize=3)
    hy = cv2.Sobel(hint_blur, cv2.CV_64F, 0, 1, ksize=3)
    hint_grad_map = np.sqrt(hx ** 2 + hy ** 2)
    hint_grad_map = np.clip(hint_grad_map, 0, 255).astype(np.uint8)
    hint_grad_map = cv2.dilate(hint_grad_map,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                               iterations=1)

    # ---- 基准尺寸 ----
    dw = max(5, min(disp_w, cw // 2))
    dh = max(5, min(disp_h, ch // 2))

    # ---- 扫描函数 ----
    def _search(templ, target, scales, ars):
        base = cv2.resize(templ, (dw, dh))
        best_val, best_loc, best_nw, best_nh = -1.0, (0, 0), 0, 0
        for s in scales:
            for ar in ars:
                nw = max(3, min(target.shape[1] - 1, int(dw * s * ar)))
                nh = max(3, min(target.shape[0] - 1, int(dh * s)))
                if nw >= target.shape[1] or nh >= target.shape[0]:
                    continue
                var = cv2.resize(base, (nw, nh))
                res = cv2.matchTemplate(target, var, cv2.TM_CCOEFF_NORMED)
                _, val, _, loc = cv2.minMaxLoc(res)
                if val > best_val:
                    best_val = val
                    best_loc = loc
                    best_nw, best_nh = nw, nh
        cx = best_loc[0] + best_nw // 2
        cy = best_loc[1] + best_nh // 2
        return cx, cy, best_val

    # ---- 策略1：距离变换 ----
    dt_cx, dt_cy, dt_score = _search(
        hint_dt, answer_dt,
        scales=(0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30),
        ars=(0.80, 0.90, 1.00, 1.10, 1.20),
    )

    # ---- 策略2：二值 ----
    bin_cx, bin_cy, bin_score = _search(
        hint_solid.astype(np.float32) / 255.0,
        answer_binary.astype(np.float32) / 255.0,
        scales=(0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20),
        ars=(0.80, 0.90, 1.00, 1.10, 1.20),
    )

    # ---- 策略3：梯度 ----
    grad_cx, grad_cy, grad_score = _search(
        hint_grad_map,
        answer_grad,
        scales=(0.75, 0.88, 1.00, 1.12, 1.25),
        ars=(0.88, 1.00, 1.12),
    )

    # ---- 融合决策 ----
    # 距离变换和二值匹配如果位置接近则置信度加成
    dt_bin_dist = np.sqrt((dt_cx - bin_cx) ** 2 + (dt_cy - bin_cy) ** 2)
    agreement_bonus = 0.15 if dt_bin_dist < 30 else 0.0

    dt_adj = dt_score + agreement_bonus
    bin_adj = bin_score + agreement_bonus

    # 选择最佳策略
    candidates = [
        (dt_cx, dt_cy, dt_adj, "dt"),
        (bin_cx, bin_cy, bin_adj, "binary"),
        (grad_cx, grad_cy, grad_score, "grad"),
    ]
    candidates.sort(key=lambda x: -x[2])
    best = candidates[0]

    if best[2] < 0.08:
        return (-1, -1, best[2], best[3])
    return (int(best[0]), int(best[1]), float(best[2]), best[3])


def _validate_match(answer_bin: np.ndarray, cx: int, cy: int, tw: int, th: int,
                   answer_gray: np.ndarray | None = None) -> bool:
    """校验匹配位置是否是真实字符区域而非背景均匀暗区。

    检查：
      1. 灰度标准差足够（>20 = 有对比度；真实字符 ~50，假阳性 ~13）
      2. 存在明显亮像素（至少 2% > 192 或 5% > 128）
      3. 区域内存在显著连通组件
    """
    ah, aw = answer_bin.shape[:2]
    hw, hh = max(3, tw), max(3, th)
    x1 = max(0, cx - hw // 2)
    y1 = max(0, cy - hh // 2)
    x2 = min(aw, x1 + hw)
    y2 = min(ah, y1 + hh)

    # ---- 灰度检查 ----
    if answer_gray is not None:
        gh, gw = answer_gray.shape[:2]
        gx1 = max(0, cx - hw // 2)
        gy1 = max(0, cy - hh // 2)
        gx2 = min(gw, gx1 + hw)
        gy2 = min(gh, gy1 + hh)
        gray_region = answer_gray[gy1:gy2, gx1:gx2]
        if gray_region.size >= 25:
            gstd = float(np.std(gray_region.astype(np.float32)))
            if gstd < 20:
                return False  # 均匀背景，无对比度
            bright_192 = np.sum(gray_region > 192) / gray_region.size
            bright_128 = np.sum(gray_region > 128) / gray_region.size
            if bright_192 < 0.02 and bright_128 < 0.05:
                return False  # 几乎没有亮像素，应该是背景暗区

    # ---- 二值检查 ----
    region = answer_bin[y1:y2, x1:x2]
    if region.size < 9:
        return False

    if np.std(region.astype(np.float32)) < 15:
        return False

    white_ratio = np.sum(region > 0) / region.size
    if white_ratio < 0.03 or white_ratio > 0.70:
        return False

    n, _, stats, _ = cv2.connectedComponentsWithStats(region)
    min_area = max(9, region.size * 0.03)
    for i in range(1, n):
        if stats[i][4] >= min_area:
            return True

    return False


# 向后兼容别名 — 升级到 v9
match_hint_v8 = match_hint_v9
match_hint_v7 = match_hint_v9


# ====================== 多字符 hint 拆分 + 匹配 ======================

def _split_hint_chars(hint_bytes: bytes) -> list[np.ndarray]:
    """将含多个字符的 hint 图片沿水平方向拆分为独立字符。

    用列投影找字符间隙（gap > 3px），拆分后返回每个字符的灰度图。
    如果只有 1 个字符则返回单元素列表。
    """
    hi = cv2.imdecode(np.frombuffer(hint_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if hi is None:
        return []

    # 透明底处理
    if hi.ndim == 3 and hi.shape[2] == 4:
        alpha = hi[:, :, 3]
        rgb = hi[:, :, :3].copy()
        for cc in range(3):
            rgb[:, :, cc] = np.where(alpha > 80, rgb[:, :, cc], 255)
        gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    elif hi.ndim == 3:
        gray = cv2.cvtColor(hi, cv2.COLOR_BGR2GRAY)
    else:
        gray = hi

    h, w = gray.shape[:2]

    # CLAHE 增强
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
    gray = clahe.apply(gray)

    # 二值化 + 列投影
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dark_ratio = np.sum(binary > 0) / binary.size
    if dark_ratio > 0.6 or dark_ratio < 0.02:
        _, binary = cv2.threshold(gray, 165, 255, cv2.THRESH_BINARY_INV)

    col_dark = np.sum(binary > 0, axis=0)

    # 找连续暗像素段 = 字符区域
    chars = []
    in_char = False
    cs = 0
    for i in range(len(col_dark)):
        if col_dark[i] > 0:
            if not in_char:
                cs = i
                in_char = True
        else:
            if in_char and i - cs > 3:
                chars.append(cs)
            in_char = False
    if in_char and len(col_dark) - cs > 3:
        chars.append(cs)

    # 找字符间隙来分割
    if len(chars) <= 1:
        return [gray]  # 单字符，返回整图

    # 计算间隙中点作为分割线
    splits = []
    for i in range(len(chars) - 1):
        mid = (chars[i + 1] + chars[i]) // 2
        splits.append(mid)

    # 分割灰度图
    parts = []
    prev = 0
    for sp in splits:
        parts.append(gray[:, prev:sp])
        prev = sp
    parts.append(gray[:, prev:])

    return [p for p in parts if p.shape[1] > 3]


def match_hint_multi_v9(
    canvas_bytes: bytes,
    hint_bytes: bytes,
    disp_w: int,
    disp_h: int,
    exclude: list | None = None,
) -> list[tuple]:
    """多字符形状匹配：拆分 hint → 填充空心 → Hu 矩 + 宽高比匹配 answer 组件。

    流程：
      1. _split_hint_chars 拆分 hint 并 _hint_to_solid_binary 填充空心
      2. answer 自适应二值化 → 连通组件 → 提取 Hu 矩 + aspect
      3. 贪心匹配（每次选全局最佳 hint↔component 对，排除已用组件）

    返回 [(cx, cy, score, method), ...] 列表，长度等于 hint 字符数。
    """
    # 1. 拆分 hint 并填充
    char_grays = _split_hint_chars(hint_bytes)
    if not char_grays:
        return []

    hint_chars = []
    for cg in char_grays:
        solid = _hint_to_solid_binary(cg)
        hint_chars.append(solid)

    if len(hint_chars) < 2:
        # 单字符回退到 v9
        results = []
        if hint_chars:
            ok, buf = cv2.imencode(".png", char_grays[0])
            if ok:
                cx, cy, score, method = match_hint_v9(canvas_bytes, buf.tobytes(), disp_w, disp_h, exclude)
                results.append((cx, cy, score, method))
        return results

    # 2. 提取 hint Hu 矩（归一化到 32×32）
    def _hu_features(binary_img):
        h, w = binary_img.shape[:2]
        pad_h = max(0, 32 - h)
        pad_w = max(0, 32 - w)
        padded = np.pad(binary_img, ((0, pad_h), (0, pad_w)), mode='constant')
        padded = padded[:32, :32]
        m = cv2.moments(padded)
        hu = cv2.HuMoments(m)
        hu_log = -np.sign(hu) * np.log10(np.abs(hu) + 1e-10)
        return hu_log.flatten()

    hint_features = []
    for hc in hint_chars:
        ys, xs = np.where(hc > 0)
        hf = {
            'w': hc.shape[1], 'h': hc.shape[0],
            'aspect': hc.shape[1] / max(1, hc.shape[0]),
            'area': len(xs),
            'hu': _hu_features(hc),
        }
        hint_features.append(hf)

    # 3. 提取 answer 组件
    ca = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
    if ca is None:
        return [(-1, -1, 0.0, "decode_error")] * len(hint_chars)

    ag = cv2.cvtColor(ca, cv2.COLOR_BGR2GRAY)
    ab = cv2.adaptiveThreshold(ag, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 15, 6)
    n, _, stats, centroids = cv2.connectedComponentsWithStats(ab)

    comps = []
    for i in range(1, n):
        x, y, bw, bh, a = stats[i]
        if a < 120 or bw < 5 or bh < 5 or bw > ca.shape[1] * 0.85:
            continue
        # 提取组件区域并做形态学清理
        roi = ab[y:y + bh, x:x + bw]
        k2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        roi_c = cv2.morphologyEx(roi, cv2.MORPH_CLOSE, k2)
        conts, _ = cv2.findContours(roi_c, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros_like(roi_c)
        cv2.drawContours(mask, conts, -1, 255, -1)

        comps.append({
            'x': x, 'y': y, 'w': bw, 'h': bh, 'area': a,
            'cx': int(x + bw / 2), 'cy': int(y + bh / 2),
            'aspect': bw / max(1, bh),
            'hu': _hu_features(mask),
        })

    if len(comps) < len(hint_chars):
        # 不够组件，回退到单字符匹配
        results = []
        for cg, hc in zip(char_grays, hint_chars):
            ok, buf = cv2.imencode(".png", cg)
            cx, cy, score, method = (-1, -1, 0.0, "no_comp")
            if ok:
                cx, cy, score, method = match_hint_v9(canvas_bytes, buf.tobytes(), disp_w, disp_h)
            results.append((cx, cy, score, method))
        return results

    # 4. 计算 Hu + aspect 距离矩阵
    def _hu_dist(hu1, hu2):
        return float(np.sqrt(np.sum((hu1 - hu2) ** 2)))

    scores = np.zeros((len(hint_chars), len(comps)))
    for hi in range(len(hint_chars)):
        for ci in range(len(comps)):
            hf = hint_features[hi]
            cf = comps[ci]
            hu_d = _hu_dist(hf['hu'], cf['hu'])
            ar_d = abs(hf['aspect'] - cf['aspect']) / max(hf['aspect'], cf['aspect'], 0.1)
            scores[hi, ci] = -(hu_d * 0.55 + ar_d * 0.45)

    # 5. 贪心匹配：每次选全局最高分，排除已用组件
    used_comp = set()
    results = []
    for hi in range(len(hint_chars)):
        best_score = -999.0
        best_ci = -1
        for ci in range(len(comps)):
            if ci in used_comp:
                continue
            if scores[hi, ci] > best_score:
                best_score = scores[hi, ci]
                best_ci = ci
        if best_ci >= 0:
            used_comp.add(best_ci)
            cf = comps[best_ci]
            # 将负分映射回 [0, 1] 范围，越接近 0 越好 → 越接近 1 越好
            # Hu 距离典型值 < 5，取 max(-5, score) 然后 normalize
            norm_score = max(0.0, min(1.0, (best_score + 5.0) / 5.0))
            results.append((cf['cx'], cf['cy'], norm_score, "shape_hu"))
        else:
            results.append((-1, -1, 0.0, "shape_no_match"))

    return results


async def solve_clickword_v7(page, max_attempts: int = 5,
                             debug_dir: str | None = None,
                             refresh_counter=None) -> bool:
    """完成点选文字验证码: 读取提示图 -> 图像匹配 -> 页面坐标点击 -> 校验。"""
    for attempt in range(max_attempts):
        scope = _debug_scope(debug_dir, f"clickword_{attempt}")
        clicked = []
        matches = []
        try:
            await asyncio.sleep(1.8)
            view_fix = await _normalize_captcha_view(page)
            await _save_viewport_screenshot(page, _debug_file(scope, "raw", "01_before.png"))

            hints = await page.evaluate(
                """() => {
                    const sels = ['.dx_captcha_clickword_hint-cnt img',
                                  '.dx_captcha_clickword_hint img',
                                  '.yidun_tips__img'];
                    for (const s of sels) {
                        const imgs = document.querySelectorAll(s);
                        if (imgs.length) return Array.from(imgs).map((im, idx) => ({
                            index: idx,
                            src: im.src || '',
                            width: Math.round(im.offsetWidth) || 30,
                            height: Math.round(im.offsetHeight) || 30,
                            naturalWidth: Math.round(im.naturalWidth) || 0,
                            naturalHeight: Math.round(im.naturalHeight) || 0,
                        }));
                    }
                    return [];
                }""")
            if not hints:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "type": "clickword", "attempt": attempt, "ok": False,
                    "reason": "hints_not_found", "viewFix": view_fix,
                    "metrics": await _page_metrics(page),
                })
                return False

            canvas_el = (await page.query_selector(".dx_captcha_clickword_pic canvas")
                         or await page.query_selector("canvas"))
            if not canvas_el:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "type": "clickword", "attempt": attempt, "ok": False,
                    "reason": "canvas_not_found", "viewFix": view_fix,
                    "hints": [{k: v for k, v in h.items() if k != "src"} for h in hints],
                    "metrics": await _page_metrics(page),
                })
                return False

            cbox = await canvas_el.bounding_box()
            if not cbox or cbox["width"] < 10:
                _write_json(_debug_file(scope, "raw", "result.json"), {
                    "type": "clickword", "attempt": attempt, "ok": False,
                    "reason": "canvas_box_invalid", "viewFix": view_fix,
                    "canvasBox": cbox,
                    "metrics": await _page_metrics(page),
                })
                return False

            canvas_bytes = await canvas_el.screenshot(timeout=5000)
            _write_bytes(_debug_file(scope, "raw", "canvas.png"), canvas_bytes)
            img = cv2.imdecode(np.frombuffer(canvas_bytes, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return False
            canvas_h, canvas_w = img.shape[:2]
            sx = cbox["width"] / canvas_w
            sy = cbox["height"] / canvas_h

            occupied = []
            for idx, hint in enumerate(hints, start=1):
                src = hint.get("src", "")
                if not src.startswith("data:image"):
                    continue
                b64 = src.split(",", 1)[1] if "," in src else src
                hint_bytes = base64.b64decode(b64)
                _write_bytes(_debug_file(scope, "raw", f"hint_{idx}.png"), hint_bytes)

                # 用多字符匹配：自动拆分 hint 为独立字符，逐个匹配
                multi_results = await asyncio.to_thread(
                    match_hint_multi_v9,
                    canvas_bytes, hint_bytes,
                    hint["width"], hint["height"],
                    exclude=occupied,
                )

                if not multi_results:
                    matches.append({
                        "index": idx, "score": 0.0, "method": "multi_empty",
                        "skipped": True,
                    })
                    continue

                for ci, (cx, cy, score, method) in enumerate(multi_results):
                    if cx < 0 or score < 0.08:
                        matches.append({
                            "index": idx, "sub": ci + 1, "score": score,
                            "method": method, "skipped": True,
                        })
                        continue

                    occupied.append((cx, cy, max(hint["width"] // max(1, len(multi_results)), 10)))
                    jitter_x = random.randint(-3, 3)
                    jitter_y = random.randint(-3, 3)
                    px = cbox["x"] + cx * sx + jitter_x
                    py = cbox["y"] + cy * sy + jitter_y
                    item = {
                        "index": idx,
                        "sub": ci + 1 if len(multi_results) > 1 else 0,
                        "label": str(len(clicked) + 1),
                        "score": score,
                        "method": method,
                        "hint": {k: v for k, v in hint.items() if k != "src"},
                        "image": {"x": cx, "y": cy},
                        "page": {"x": px, "y": py},
                        "jitter": {"x": jitter_x, "y": jitter_y},
                    }
                    matches.append(item)
                    clicked.append((px, py))

            _write_clickword_debug_image(
                canvas_bytes, matches,
                _debug_file(scope, "annotated", "canvas_targets.png"),
            )
            await _mark_page_points(page, [
                {"x": item["page"]["x"], "y": item["page"]["y"],
                 "label": str(item["index"]), "color": "#ff2d55", "radius": 11}
                for item in matches if not item.get("skipped")
            ])
            await _save_viewport_screenshot(page, _debug_file(scope, "annotated", "02_before_click_marked.png"))

            for px, py in clicked:
                await page.mouse.click(px, py)
                await asyncio.sleep(random.uniform(0.5, 0.9))

            await asyncio.sleep(1.8)
            result_type = await captcha_type_v7(page)
            result = {
                "type": "clickword",
                "attempt": attempt,
                "viewFix": view_fix,
                "metricsBefore": await _page_metrics(page),
                "canvasBox": cbox,
                "image": {"width": canvas_w, "height": canvas_h},
                "scale": {"cssPerImageX": sx, "cssPerImageY": sy},
                "matches": matches,
                "clicked": [{"x": px, "y": py} for px, py in clicked],
                "captchaTypeAfter": result_type,
                "ok": result_type == "none",
                "metricsAfter": await _page_metrics(page),
            }
            _write_json(_debug_file(scope, "raw", "result.json"), result)
            await _save_viewport_screenshot(page, _debug_file(scope, "raw", "03_after_click.png"))

            if result_type == "none":
                return True
            await _refresh(page, counter=refresh_counter)
        except Exception as e:  # noqa: BLE001
            _write_json(_debug_file(scope, "raw", "error.json"), {
                "type": "clickword", "attempt": attempt, "error": repr(e),
            })
            print(f"  [clickword v7] attempt {attempt}: {e}", file=sys.stderr)
    return False


# ====================== 统一入口 ======================

async def captcha_type_v7(page) -> str:
    try:
        text = await page.evaluate("document.body?.innerText?.substring(0,500)||''")
        if any(k in text for k in ["验证通过", "验证成功", "校验通过", "校验成功", "✓"]):
            return "none"
        if await page.evaluate("!!document.querySelector('.dx_captcha_clickword, .yidun_tips')"):
            return "clickword"
        if await page.evaluate(
                "!!document.querySelector('.dx_captcha_slider, .dx_captcha_basic, .yidun_slider')"):
            return "slider"
        if any(k in text for k in ["请依次点击", "请点击", "按顺序点击"]):
            return "clickword"
        if any(k in text for k in ["向右滑动", "拖动滑块", "滑动滑块", "完成拼图"]):
            return "slider"
        if "安全验证" in text:
            return "unknown"
        return "none"
    except Exception:  # noqa: BLE001
        return "none"


async def _wait_canvas_ready(page, timeout_ms: int = 8000) -> bool:
    """等 canvas 图片加载完成（采样 3 点验证非纯色），用于 refresh 后。"""
    deadline = time.time() + timeout_ms / 1000
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
            return True
        await asyncio.sleep(0.2)
    return False


async def _refresh(page, counter=None):
    try:
        r = await page.query_selector("[class*='refresh'], .dx-captcha-refresh, .yidun_refresh")
        if r:
            await r.click()
            await _wait_canvas_ready(page, timeout_ms=8000)
            if counter is not None:
                counter[0] += 1
    except Exception:  # noqa: BLE001
        pass


async def solve_captcha_v7(page, max_attempts: int = 3,
                           debug_dir: str | None = None,
                           refresh_counter=None) -> bool:
    """v7 只处理滑块验证码。点选验证码由 AntiCAP YOLO+OCR 方案处理。"""
    for attempt in range(max_attempts):
        # 清除上轮验证码残留的 debug 覆盖层
        try:
            await page.evaluate(
                """() => {
                    const old = document.getElementById('__captcha_debug_overlay');
                    if (old) old.remove();
                }"""
            )
        except Exception:
            pass

        ctype = await captcha_type_v7(page)
        if ctype == "none":
            return True
        print(f"  [captcha v7] type={ctype} attempt={attempt+1}/{max_attempts}",
              file=sys.stderr)
        if ctype == "clickword":
            return False  # 点选交给 AntiCAP
        if ctype == "slider":
            ok = await solve_slider_v7(page, 2, debug_dir, refresh_counter=refresh_counter)  # 减少修正轮数
        else:
            ok = await solve_slider_v7(page, 2, debug_dir, refresh_counter=refresh_counter)  # unknown 也尝试滑块
        if ok:
            await asyncio.sleep(1.5)
            if await captcha_type_v7(page) == "none":
                return True
        await _refresh(page, counter=refresh_counter)
    return False


# ====================== 脱机自测 ======================

def _selftest():
    import os
    d = os.path.join(os.path.dirname(__file__), "diag_captcha")
    cases = [("slider_canvas_v2.png", 45, (190, 220))]
    ok = True
    for fn, pw, (lo, hi) in cases:
        p = os.path.join(d, fn)
        if not os.path.exists(p):
            print(f"skip {fn} (missing)")
            continue
        res = find_gap_v7(open(p, "rb").read(), piece_w=pw,
                          debug_path=os.path.join(tempfile.gettempdir(), f"selftest_{fn}"))
        g = res["gap_x"]
        good = lo <= g <= hi
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {fn}: gap_x={g} "
              f"(expect {lo}-{hi}) conf={res['confidence']:.2f}")
    print("== selftest", "OK" if ok else "FAILED", "==")
    return ok


if __name__ == "__main__":
    sys.exit(0 if _selftest() else 1)
