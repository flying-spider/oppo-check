#!/usr/bin/env python3
"""分析验证码问题图和答案图的特征，测试匹配方案"""
import cv2
import numpy as np
import base64
import sys
from pathlib import Path

PROJECT = Path("/Users/a1-6/Documents/OPPO申诉")
HINT_PNG = PROJECT / ".runtime/captcha_debug/问题图.png"
ANSWER_PNG = PROJECT / ".runtime/captcha_debug/答案图.png"

def show_info(label, img):
    if img is None:
        print(f"  {label}: None")
        return
    h, w = img.shape[:2]
    ch = img.shape[2] if img.ndim == 3 else 1
    print(f"  {label}: {w}x{h} channels={ch} dtype={img.dtype}")
    if ch >= 3:
        b, g, r = cv2.mean(img[:,:,:3])[:3]
        print(f"    mean BGR=({b:.0f},{g:.0f},{r:.0f})")

def main():
    hint = cv2.imread(str(HINT_PNG), cv2.IMREAD_UNCHANGED)
    answer = cv2.imread(str(ANSWER_PNG), cv2.IMREAD_COLOR)

    print("=== 图片基本信息 ===")
    show_info("问题图(hint)", hint)
    show_info("答案图(answer)", answer)

    if hint is None:
        print("ERROR: 无法读取问题图")
        return

    # 检查 hint 是否有 alpha 通道
    print("\n=== 问题图特征分析 ===")
    if hint.ndim == 3 and hint.shape[2] == 4:
        alpha = hint[:,:,3]
        print(f"  有 alpha 通道, alpha 非零像素: {np.count_nonzero(alpha)}")
        print(f"  alpha 范围: [{alpha.min()},{alpha.max()}]")
        # 检查空心字特征
        fg_mask = alpha > 80
        fg_pixels = hint[fg_mask][:,:3]
        if len(fg_pixels) > 0:
            print(f"  前景像素数: {len(fg_pixels)}")
            print(f"  前景 BGR mean: ({fg_pixels[:,0].mean():.0f},{fg_pixels[:,1].mean():.0f},{fg_pixels[:,2].mean():.0f})")
        # 看中间区域是否透明（空心特征）
        hh, hw = hint.shape[:2]
        center_pixels = alpha[hh//4:3*hh//4, hw//4:3*hw//4]
        center_transparent = (center_pixels < 50).sum()
        center_total = center_pixels.size
        print(f"  中心区域透明比例: {center_transparent}/{center_total} = {center_transparent/center_total:.2%}")
        if center_transparent/center_total > 0.3:
            print("  >>> 确认为空心字体（中心大面积透明）")
    else:
        print(f"  通道数={hint.shape[2] if hint.ndim==3 else 1}, 无 alpha")

    # 答案图分析
    if answer is not None:
        print("\n=== 答案图特征分析 ===")
        ah, aw = answer.shape[:2]
        print(f"  尺寸: {aw}x{ah}")
        # 检测答案图中可能的文字区域（通过边缘密度）
        gray = cv2.cvtColor(answer, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = edges.sum() / edges.size
        print(f"  边缘密度: {edge_density:.4f}")

    # 检查 hint 中可能的字符
    print("\n=== Hint 字符提取 ===")
    if hint.ndim == 3 and hint.shape[2] == 4:
        # 从 alpha 通道生成 mask
        fg = hint[:,:,3] > 80
    else:
        gray = cv2.cvtColor(hint, cv2.COLOR_BGR2GRAY)
        _, fg = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY_INV)
        fg = fg > 0

    # 找连通组件
    fg_uint8 = fg.astype(np.uint8) * 255
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(fg_uint8)
    print(f"  连通组件数(含背景): {count}")
    for i in range(1, count):
        x, y, bw, bh, area = stats[i]
        if area < 20:
            continue
        cx, cy = centroids[i]
        # 裁剪这个字符
        char_img = fg_uint8[y:y+bh, x:x+bw]
        print(f"  char[{i}]: bbox=({x},{y},{bw},{bh}) area={area} centroid=({cx:.0f},{cy:.0f})")

    # 保存处理后的 hint 用于可视化
    hint_vis = hint.copy()
    if hint_vis.ndim == 3 and hint_vis.shape[2] == 4:
        hint_vis = cv2.cvtColor(hint_vis, cv2.COLOR_BGRA2BGR)
    cv2.imwrite(str(PROJECT / ".runtime/captcha_debug/hint_analysis.png"), hint_vis)
    print(f"\n分析结果已保存: .runtime/captcha_debug/hint_analysis.png")

if __name__ == "__main__":
    main()