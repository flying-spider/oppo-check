#!/usr/bin/env python3
"""用现有 captcha_solver_v7 的 match_hint_v7 测试匹配效果"""
import sys, base64, cv2, numpy as np
from pathlib import Path

PROJECT = Path("/Users/a1-6/Documents/OPPO申诉")
sys.path.insert(0, str(PROJECT))

from captcha_solver_v7 import match_hint_v7, _write_clickword_debug_image

HINT = PROJECT / ".runtime/captcha_debug/问题图.png"
ANSWER = PROJECT / ".runtime/captcha_debug/答案图.png"

def main():
    # 读取图片
    hint_img = cv2.imread(str(HINT), cv2.IMREAD_UNCHANGED)
    answer_img = cv2.imread(str(ANSWER), cv2.IMREAD_COLOR)

    print(f"Hint: {hint_img.shape}")
    print(f"Answer: {answer_img.shape}")

    # 把 hint 编码为 PNG bytes (模拟 canvas screenshot)
    _, hint_bytes = cv2.imencode('.png', hint_img)
    _, answer_bytes = cv2.imencode('.png', answer_img)

    # 读回用于分析
    hint_decoded = cv2.imdecode(hint_bytes, cv2.IMREAD_UNCHANGED)
    answer_decoded = cv2.imdecode(answer_bytes, cv2.IMREAD_COLOR)
    print(f"Hint decoded: {hint_decoded.shape}")
    print(f"Answer decoded: {answer_decoded.shape}")

    # 检查 hint 的 alpha 通道
    if hint_decoded.ndim == 3 and hint_decoded.shape[2] == 4:
        alpha = hint_decoded[:,:,3]
        print(f"Alpha 255 像素: {np.count_nonzero(alpha == 255)}")
        print(f"Alpha 0 像素: {np.count_nonzero(alpha == 0)}")

    # === 测试匹配 ===
    # hint 的显示尺寸（和原始一样大）
    disp_w = hint_decoded.shape[1]
    disp_h = hint_decoded.shape[0]

    print(f"\n=== 测试 match_hint_v7 ===")
    cx, cy, score, method = match_hint_v7(
        answer_bytes,
        hint_bytes,
        disp_w, disp_h,
        exclude=None
    )
    print(f"结果: cx={cx}, cy={cy}, score={score:.4f}, method={method}")

    # 多个 hint（同一个 hint 重复来模拟多个文字）
    print(f"\n=== 测试多个 hint 匹配 ===")
    hints = [
        {"src": "data:image/png;base64," + base64.b64encode(hint_bytes).decode(),
         "width": disp_w, "height": disp_h},
        {"src": "data:image/png;base64," + base64.b64encode(hint_bytes).decode(),
         "width": disp_w, "height": disp_h},
    ]

    # 模拟 cbox
    canvas_h, canvas_w = answer_decoded.shape[:2]
    sx, sy = 1.0, 1.0  # 原始尺寸

    occupied = []
    results = []
    for idx, hint_info in enumerate(hints, start=1):
        b64 = base64.b64encode(hint_bytes).decode()
        cx, cy, score, method = match_hint_v7(
            answer_bytes, hint_bytes,
            hint_info["width"], hint_info["height"],
            exclude=occupied)
        print(f"  Hint {idx}: cx={cx}, cy={cy}, score={score:.4f}, method={method}")
        if cx >= 0:
            occupied.append((cx, cy, max(disp_w, disp_h)))
            results.append({
                "index": idx, "label": str(idx),
                "score": score, "method": method,
                "image": {"x": cx, "y": cy},
            })

    # 可视化
    from captcha_solver_v7 import _write_clickword_debug_image
    output = PROJECT / ".runtime/captcha_debug/match_v7_result.png"
    _write_clickword_debug_image(answer_bytes, results, output)
    print(f"\n可视化: .runtime/captcha_debug/match_v7_result.png")

if __name__ == "__main__":
    main()