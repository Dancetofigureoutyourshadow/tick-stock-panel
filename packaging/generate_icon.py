"""生成应用图标 icon.ico — 白底圆角 + 紫色 Logo。

设计 (与 frontend Logo.tsx / favicon.svg 一致):
  背景: 白色圆角方块
  内容: 紫色 #5B21B6 方括号 + K线 (上影短/下影长, bullish 站稳)

蜡烛几何 (32x32 viewBox):
  wick: y=7 ~ y=25
  body: y=9 ~ y=19 (偏上)
  → 上影 = 2 (短), 下影 = 6 (长)

每尺寸独立绘制: 小尺寸加粗线条, 大尺寸精细。
运行: python packaging/generate_icon.py
产物: packaging/icon.ico (16/32/48/64/128/256 多尺寸)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "icon.ico"

BRAND = (91, 33, 182, 255)        # #5B21B6
BRAND_WICK = (91, 33, 182, 153)   # 0.6 opacity


def _draw(size: int, sw_b: float, sw_w: float, body_w: float) -> Image.Image:
    """绘制单尺寸图标 (像素直接映射 32-viewBox)。"""
    factor = max(8, 2048 // size)
    s = size * factor
    img = Image.new("RGBA", (s, s), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)

    # 白色圆角背景
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.18), fill=(255, 255, 255, 255))

    def p(v):
        return v * s / 32

    swb = max(1, int(round(sw_b * factor)))
    sww = max(1, int(round(sw_w * factor)))

    # 方括号 [ ]
    for pts in [[(10, 4), (4, 4), (4, 28), (10, 28)], [(22, 4), (28, 4), (28, 28), (22, 28)]]:
        scaled = [(p(x), p(y)) for x, y in pts]
        for i in range(len(scaled) - 1):
            d.line([scaled[i], scaled[i + 1]], fill=BRAND, width=swb, joint="curve")

    # wick 影线 (上短下长)
    d.line([(p(16), p(7)), (p(16), p(25))], fill=BRAND_WICK, width=sww)
    wcap = sww // 2 + 1
    for cy in [7, 25]:
        d.ellipse([p(16) - wcap, p(cy) - wcap, p(16) + wcap, p(cy) + wcap], fill=BRAND_WICK)

    # body 实体 (偏上 → 上影短下影长)
    d.rounded_rectangle(
        [p(16 - body_w / 2), p(9), p(16 + body_w / 2), p(19)],
        radius=p(0.5), fill=BRAND,
    )

    return img.resize((size, size), Image.LANCZOS)


def draw_logo(size: int) -> Image.Image:
    """按尺寸选参数 (小尺寸加粗)。"""
    if size <= 16:
        return _draw(size, sw_b=4.0, sw_w=3.5, body_w=9)
    elif size <= 32:
        return _draw(size, sw_b=3.0, sw_w=2.5, body_w=8)
    elif size <= 48:
        return _draw(size, sw_b=2.5, sw_w=2.0, body_w=7)
    else:
        return _draw(size, sw_b=2.0, sw_w=1.5, body_w=6)


def main() -> None:
    sizes = [16, 32, 48, 64, 128, 256]
    images = [draw_logo(sz) for sz in sizes]
    # 主图 256 (大尺寸优先, 资源管理器大图清晰)
    images[-1].save(
        OUT, format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=images[:-1],
    )
    print(f"生成: {OUT} (尺寸 {sizes})")


if __name__ == "__main__":
    main()
