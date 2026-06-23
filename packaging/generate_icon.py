"""生成应用图标 .ico — 与前端 favicon.svg / Logo.tsx 保持一致。

几何 (32x32 基准, 按比例缩放到各尺寸):
  - 紫色方括号 [ ] (brand color #5B21B6)
  - 中间一根带 wick 的 K 线 body (实心矩形)

用途:
  - exe 图标 (PyInstaller icon=)
  - 安装包图标 (Inno Setup SetupIconFile=)
  - 快捷方式图标

运行: python packaging/generate_icon.py
产物: packaging/icon.ico (含 16/32/48/64/128/256 多尺寸)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# 品牌色 (与 frontend/public/favicon.svg 一致)
BRAND = (91, 33, 182, 255)        # #5B21B6
BRAND_DIM = (91, 33, 182, 166)    # stroke-opacity 0.65 (wick)

OUTPUT = Path(__file__).parent / "icon.ico"


def draw_logo(size: int) -> Image.Image:
    """按 favicon.svg 的几何绘制指定尺寸的图标。

    坐标基于 32x32 viewBox, 按比例缩放到 size。
    """
    # 用 4x 超采样再缩放, 抗锯齿
    scale = max(4, 256 // size)
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 把 32-viewBox 坐标缩放到 s
    def x(v: float) -> float:
        return v * s / 32

    def y(v: float) -> float:
        return v * s / 32

    sw = 2.5  # 括号线宽
    bw = 2.0  # wick 线宽
    w = max(1, int(round(sw * scale)))
    ww = max(1, int(round(bw * scale)))

    # 左方括号 [ : M10,4 L4,4 L4,28 L10,28
    d.line([(x(10), y(4)), (x(4), y(4))], fill=BRAND, width=w)
    d.line([(x(4), y(4)), (x(4), y(28))], fill=BRAND, width=w)
    d.line([(x(4), y(28)), (x(10), y(28))], fill=BRAND, width=w)
    # 括号转角处用圆点补强 (line 转角有缝隙)
    r = w // 2 + 1
    for cx, cy in [(x(4), y(4)), (x(4), y(28))]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BRAND)

    # 右方括号 ] : M22,4 L28,4 L28,28 L22,28
    d.line([(x(22), y(4)), (x(28), y(4))], fill=BRAND, width=w)
    d.line([(x(28), y(4)), (x(28), y(28))], fill=BRAND, width=w)
    d.line([(x(28), y(28)), (x(22), y(28))], fill=BRAND, width=w)
    for cx, cy in [(x(28), y(4)), (x(28), y(28))]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BRAND)

    # K 线 wick: x=16, y=7..25
    d.line([(x(16), y(7)), (x(16), y(25))], fill=BRAND_DIM, width=ww)

    # K 线 body: rect (12.5, 11.5) 7x11, rx=0.8
    bx0, by0 = x(12.5), y(11.5)
    bx1, by1 = x(12.5) + x(7), y(11.5) + y(11)
    radius = x(0.8)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=radius, fill=BRAND)

    # 缩放到目标尺寸 (高质量抗锯齿)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes = [16, 32, 48, 64, 128, 256]
    images = [draw_logo(s) for s in sizes]
    # 第一个作为主图, 其余作为变体
    images[0].save(
        OUTPUT,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"生成图标: {OUTPUT} (尺寸 {sizes})")


if __name__ == "__main__":
    main()
