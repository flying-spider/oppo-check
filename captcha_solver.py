"""
DingXiang (顶象) 滑块验证码脱机识别模块

本模块实现顶象滑块验证码的离线缺口识别功能：
1. 获取滑块挑战（背景图 + 滑块图 + 缺口 y 坐标）
2. 使用 OpenCV 识别缺口 x 坐标

注意：滑块验证的提交（api/v1）涉及前端 JS 加密（ac 参数），
本模块不做逆向，仅提供缺口坐标识别。
验证提交需通过浏览器或 curl_cffi 模拟完成。

依赖: opencv-python, numpy, Pillow, requests
"""

from __future__ import annotations

import base64
import hashlib
import json as json_module
import math
import random
import sys
import time
import urllib.parse
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests as http_requests

try:
    import cv2
except ModuleNotFoundError:
    cv2 = None  # type: ignore

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

CAP_SEC_BASE = "https://cap-sec-gl.heytapmobi.com"


class CaptchaError(RuntimeError):
    """滑块验证相关错误"""
    pass


def require_cv2() -> None:
    if cv2 is None:
        raise CaptchaError(
            "缺少 OpenCV 依赖。请运行：pip install opencv-python numpy Pillow requests"
        )


def _download_image(url: str, session: http_requests.Session,
                    referer: str = "https://safe.heytap.com/",
                    grayscale: bool = False) -> np.ndarray:
    """下载图片并解码为 OpenCV 数组"""
    resp = session.get(url, headers={
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    resp.raise_for_status()
    img_bytes = BytesIO(resp.content)
    img_array = np.frombuffer(img_bytes.read(), dtype=np.uint8)
    flag = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR  # type: ignore
    return cv2.imdecode(img_array, flag)  # type: ignore


def _download_slider_alpha(url: str, session: http_requests.Session,
                           referer: str = "https://safe.heytap.com/") -> Tuple[np.ndarray, np.ndarray]:
    """下载滑块图（带 alpha 通道），返回 (BGR图像, alpha通道)"""
    resp = session.get(url, headers={
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    resp.raise_for_status()
    img_bytes = BytesIO(resp.content)
    img_array = np.frombuffer(img_bytes.read(), dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_UNCHANGED)  # type: ignore
    if img is None:
        raise CaptchaError("滑块图片解码失败")
    if img.shape[2] == 4:
        bgr = img[:, :, :3]
        alpha = img[:, :, 3]
    else:
        bgr = img
        alpha = np.ones(bgr.shape[:2], dtype=np.uint8) * 255
    return bgr, alpha


def find_gap_x_coord(
    bg_img: np.ndarray,
    slider_img: np.ndarray,
    slider_alpha: Optional[np.ndarray] = None,
    known_gap_y: Optional[int] = None,
    img_width: int = 280,
    img_height: int = 144,
) -> int:
    """
    识别滑块缺口在背景图中的 x 坐标。

    使用多种方法组合评分：
    1. 模板匹配（带掩码，限制y范围）
    2. 垂直 Sobel 梯度分析
    3. 缺口左右颜色差异
    4. Canny 边缘检测

    参数:
        bg_img: 背景图 (BGR)
        slider_img: 滑块图 (BGR)
        slider_alpha: 滑块 alpha 通道
        known_gap_y: api/a 返回的缺口 y 坐标（原始尺寸）
        img_width: 原始图片宽度 (api/a 的 w 参数)
        img_height: 原始图片高度 (api/a 的 h 参数)

    返回: 缺口 x 坐标（原始尺寸，相对于 img_width）
    """
    require_cv2()

    bg_h, bg_w = bg_img.shape[:2]
    scale_x = bg_w / img_width
    scale_y = bg_h / img_height

    gap_y_scaled = int(known_gap_y * scale_y) if known_gap_y is not None else bg_h // 2

    bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    slider_gray = cv2.cvtColor(slider_img, cv2.COLOR_BGR2GRAY)

    # 创建滑块掩码
    if slider_alpha is not None:
        _, mask = cv2.threshold(slider_alpha, 10, 255, cv2.THRESH_BINARY)
        # 移除滑块白色背景
        white = np.all(slider_img > 220, axis=2)
        mask[white] = 0
    else:
        _, mask = cv2.threshold(slider_gray, 240, 255, cv2.THRESH_BINARY_INV)

    # 对掩码进行轻微腐蚀以减少边缘噪声
    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.erode(mask, kernel, iterations=1)

    # 预处理：CLAHE 增强对比度
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    bg_enhanced = clahe.apply(bg_gray)

    # ============ 1. 模板匹配 ============
    tmpl_result = cv2.matchTemplate(
        bg_enhanced, slider_gray, cv2.TM_CCOEFF_NORMED, mask=mask
    )

    # ============ 2. Sobel 垂直梯度 ============
    blurred = cv2.GaussianBlur(bg_gray, (5, 5), 0)
    sobel_x = cv2.Sobel(blurred, cv2.CV_64F, 1, 0, ksize=3)
    sobel_x_abs = np.abs(sobel_x)

    # ============ 综合评分 ============
    y_start = max(0, gap_y_scaled - 25)
    y_end = min(bg_h, gap_y_scaled + 25)

    best_x = None
    best_score = -999.0

    for x in range(10, bg_w - 68):
        # 模板匹配分：取 y 范围内的最大值
        tmpl_scores = tmpl_result[y_start:y_end, x] if (
            y_start < tmpl_result.shape[0] and x < tmpl_result.shape[1]
        ) else [-1]
        tmpl_score = np.max(tmpl_scores) if len(tmpl_scores) > 0 else -1

        # 垂直梯度分
        grad_roi = sobel_x_abs[gap_y_scaled - 20:gap_y_scaled + 20, x:x + 58]
        grad_score = np.sum(grad_roi) / (58 * 40) if grad_roi.size > 0 else 0

        # 颜色差异分（缺口左右两侧）
        window = 15
        if x >= window and x + 58 + window < bg_w:
            left = np.mean(blurred[y_start:y_end, x - window:x])
            right = np.mean(blurred[y_start:y_end, x + 58:x + 58 + window])
            color_diff = abs(float(left) - float(right))
        else:
            color_diff = 0

        # Canny 边缘密度
        edges = cv2.Canny(blurred[y_start:y_end, x:x + 58], 50, 150)
        edge_density = np.sum(edges > 0) / edges.size if edges.size > 0 else 0

        # 综合评分
        score = (
            max(tmpl_score, 0) * 0.30
            + (grad_score / 20) * 0.15
            + color_diff * 0.30
            + edge_density * 0.25
        )
        if score > best_score:
            best_score = score
            best_x = x

    if best_x is None:
        raise CaptchaError("无法识别滑块缺口位置")

    # 缩放到原始尺寸
    final_x = int(round(best_x / scale_x))
    return max(0, min(final_x, img_width - 58))


def fetch_challenge(
    app_key: str,
    session: Optional[http_requests.Session] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    referer: str = "https://safe.heytap.com/",
    max_retries: int = 10,
) -> Dict[str, Any]:
    """
    从顶象服务端获取滑块挑战。

    返回挑战信息字典，包含 sid、背景图 p1、滑块图 p2、缺口 y、type 等。
    仅当 type=0（滑块验证）时返回成功，否则自动重试。

    参数:
        app_key: 应用 key (ak 参数)
        session: HTTP 会话
        user_agent: UA
        referer: Referer
        max_retries: 最大重试次数（服务端可能返回非滑块验证）

    返回: 挑战信息字典
    """
    if session is None:
        session = http_requests.Session()

    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Origin": "https://safe.heytap.com",
        "Accept": "application/json, text/plain, */*",
    }

    aid = f"dx-{random.randint(1000000000000000000, 9999999999999999999)}-{random.randint(10000000, 99999999)}-{random.randint(1, 5)}"

    for attempt in range(max_retries):
        rand = random.random()
        challenge_url = (
            f"{CAP_SEC_BASE}/api/a"
            f"?w=280&h=144&s=50"
            f"&ak={urllib.parse.quote(app_key)}"
            f"&c=&jsv=v1.3.41(610)"
            f"&aid={urllib.parse.quote(aid)}"
            f"&wp=1&de=0&uid=&lf=1&tpc=&isDark=false"
            f"&wtf=false&_r={rand}"
        )

        resp = session.get(challenge_url, headers=headers, timeout=15)
        resp.raise_for_status()
        challenge = resp.json()

        if not challenge.get("success"):
            err_msg = challenge.get("msg", "")
            if err_msg == "REQUEST_REPEAT" and attempt < max_retries - 1:
                # 请求重复，重新生成参数重试
                print(f"服务端返回 REQUEST_REPEAT，第 {attempt + 1} 次重试...", file=sys.stderr)
                time.sleep(0.3 + random.random() * 0.5)
                continue
            raise CaptchaError(f"获取滑块挑战失败: {challenge}")

        verify_type = challenge.get("type")
        if verify_type == 0:
            # 滑块验证
            return challenge

        if attempt < max_retries - 1:
            print(
                f"当前验证类型 type={verify_type}，非滑块验证，第 {attempt + 1} 次重试...",
                file=sys.stderr,
            )
            time.sleep(0.3 + random.random() * 0.5)

    raise CaptchaError(
        f"重试 {max_retries} 次后仍为非滑块验证(type={challenge.get('type')})，"
        f"无法自动处理"
    )


def solve_slider(
    app_key: str,
    session: Optional[http_requests.Session] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    referer: str = "https://safe.heytap.com/",
) -> Dict[str, Any]:
    """
    获取滑块挑战并识别缺口坐标。

    返回字典:
        sid: 会话 ID
        gap_x: 缺口 x 坐标（原始尺寸 280 宽）
        gap_y: 缺口 y 坐标
        challenge: 原始挑战信息
        以及图片 URL 方便调试

    参数:
        app_key: 应用 key
        session: HTTP 会话
        user_agent: UA
        referer: Referer
    """
    require_cv2()

    if session is None:
        session = http_requests.Session()

    challenge = fetch_challenge(app_key, session, user_agent, referer)

    sid = challenge["sid"]
    gap_y = challenge.get("y")
    p1_path = challenge.get("p1") or ""
    p2_path = challenge.get("p2") or ""

    # 下载图片
    bg_url = f"{CAP_SEC_BASE}{p1_path}&_r={random.random()}"
    slider_url = f"{CAP_SEC_BASE}{p2_path}&_r={random.random()}"

    bg_img = _download_image(bg_url, session, referer)
    slider_img, slider_alpha = _download_slider_alpha(slider_url, session, referer)

    gap_x = find_gap_x_coord(
        bg_img, slider_img, slider_alpha,
        known_gap_y=gap_y,
        img_width=280, img_height=144,
    )

    return {
        "sid": sid,
        "gap_x": gap_x,
        "gap_y": gap_y,
        "bg_url": bg_url,
        "slider_url": slider_url,
        "challenge": challenge,
    }


# ========== 命令行测试入口 ==========
if __name__ == "__main__":
    print("=" * 60)
    print("顶象滑块验证码脱机识别测试")
    print("=" * 60)

    app_key = sys.argv[1] if len(sys.argv) > 1 else "b960842d69fec0920579b4ec213809a3"

    try:
        result = solve_slider(app_key)
        print(f"\n✅ 缺口识别成功!")
        print(f"   SID: {result['sid']}")
        print(f"   缺口 x: {result['gap_x']}")
        print(f"   缺口 y: {result['gap_y']}")
        print(f"   背景图: {result['bg_url']}")
        print(f"   滑块图: {result['slider_url']}")
    except CaptchaError as e:
        print(f"\n❌ 识别失败: {e}")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
