"""生成 README 首页的横幅图片（中 / 英各一张）。

设计：深色渐变底 + 细网格 + 右上角光晕，左边是项目名与一句话说明，
右边是一张「终端窗口」卡片（用命令行的语气把能力列出来），右下角是仓库地址。

用法：
    python -m tools.make_banner            # 输出 docs/images/banner-{zh,en}.png
    python -m tools.make_banner --out DIR  # 指定输出目录

这个脚本只画图，不做任何网络请求；字体取 Windows 自带的微软雅黑与 Consolas，
缺字体时逐级回退，取不到就报错退出（宁可失败，也不要出一张缺字的横幅）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT = 1600, 400

# 背景：深海军蓝 → 青蓝，右上角加一团光晕
BG_LEFT = (11, 18, 32)
BG_RIGHT = (16, 42, 74)
GLOW = (34, 122, 158)
ACCENT = (56, 189, 248)

FONT_CANDIDATES = {
    "bold": [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"],
    "regular": [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"],
    "mono": [r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\msyh.ttc"],
    "mono_bold": [r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\msyhbd.ttc"],
}


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    raise SystemExit(f"找不到可用字体（{kind}），无法生成横幅")


def background() -> Image.Image:
    """渐变底 + 右上角光晕 + 细网格。"""
    ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
    t = (xs / WIDTH) * 0.55 + (ys / HEIGHT) * 0.45
    base = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
    for channel in range(3):
        base[:, :, channel] = BG_LEFT[channel] + (BG_RIGHT[channel] - BG_LEFT[channel]) * t

    # 光晕：离 (1210, 30) 越近越亮
    dist = np.sqrt(((xs - 1210) / 780.0) ** 2 + ((ys - 30) / 520.0) ** 2)
    glow = np.clip(1.0 - dist, 0.0, 1.0) ** 2
    for channel in range(3):
        base[:, :, channel] += (GLOW[channel] - base[:, :, channel] * 0.0) * glow * 0.55

    image = Image.fromarray(np.clip(base, 0, 255).astype("uint8"), mode="RGB")

    # 细网格
    grid = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(grid)
    step = 40
    for x in range(0, WIDTH, step):
        draw.line([(x, 0), (x, HEIGHT)], fill=(255, 255, 255, 10), width=1)
    for y in range(0, HEIGHT, step):
        draw.line([(0, y), (WIDTH, y)], fill=(255, 255, 255, 10), width=1)
    return Image.alpha_composite(image.convert("RGBA"), grid).convert("RGB")


def rounded_card(base: Image.Image, box: tuple[int, int, int, int], radius: int = 18) -> None:
    """在右下角画一张半透明的「终端窗口」卡片。"""
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(box, radius=radius, fill=(9, 14, 24, 232), outline=(70, 100, 140, 170), width=2)
    # 顶部三个「窗口按钮」
    cx, cy = box[0] + 26, box[1] + 26
    for index, color in enumerate([(248, 113, 113), (250, 204, 21), (74, 222, 128)]):
        draw.ellipse(
            [cx + index * 26, cy - 7, cx + index * 26 + 14, cy + 7],
            fill=color + (235,),
        )
    base.alpha_composite(overlay)


def draw_text_block(canvas: Image.Image, lang: str) -> None:
    draw = ImageDraw.Draw(canvas)
    scale = 2  # 先按 2 倍画，最后缩回去，边缘更干净
    if lang == "zh":
        title = "AutoOps 堡垒机"
        subtitle = "身份与权限审计 · 命令级策略管控 · SSH 网关"
        subtitle2 = "网页终端（SSH / WinRM）· WebRDP · SFTP · AI 运维助手"
        lines = [
            ("$ ssh -p 2222 ops@bastion", (148, 163, 184)),
            ("选择主机 › win-75   [远程桌面] [WinRM]", (226, 232, 240)),
            ("√ 已连接 win-75（192.168.0.75:5985）", (74, 222, 128)),
            ("√ 命令与输出已写入审计链", (74, 222, 128)),
        ]
    else:
        title = "AutoOps Bastion"
        subtitle = "Identity & permission audit · Command policy control"
        subtitle2 = "SSH gateway · Web terminal (SSH/WinRM) · WebRDP · SFTP · AI assistant"
        lines = [
            ("$ ssh -p 2222 ops@bastion", (148, 163, 184)),
            ("pick host > win-75   [RDP] [WinRM]", (226, 232, 240)),
            ("[ok] connected win-75 (192.168.0.75:5985)", (74, 222, 128)),
            ("[ok] command + output -> audit chain", (74, 222, 128)),
        ]

    title_font = load_font("bold", 74 * scale)
    sub_font = load_font("regular", 25 * scale)
    # 终端卡片里的中文（含 √ 和全角括号）要用带中日韩字形的字体，Consolas 画出来是方框；
    # 英文用 Consolas，但字号要小一点，否则长句会顶出卡片右边。
    mono_font = load_font("regular" if lang == "zh" else "mono", (23 if lang == "zh" else 20) * scale)
    badge_font = load_font("regular", 20 * scale)

    x = 78 * scale
    y = 96 * scale
    draw.text((x, y), title, font=title_font, fill=(255, 255, 255))
    title_w = draw.textlength(title, font=title_font)

    # 标题下的一道强调线
    draw.rounded_rectangle(
        [x, y + 96 * scale, x + int(title_w), y + 104 * scale],
        radius=4 * scale,
        fill=ACCENT,
    )

    draw.text((x, y + 132 * scale), subtitle, font=sub_font, fill=(203, 213, 225))
    draw.text((x, y + 168 * scale), subtitle2, font=sub_font, fill=(148, 163, 184))

    # 终端卡片里的四行：整体往上收，给卡片下沿留白（原来第四行正好压在边框上）
    card_x, card_y = 980 * scale, 78 * scale
    for index, (line, color) in enumerate(lines):
        draw.text(
            (card_x + 34 * scale, card_y + (42 + index * 46) * scale),
            line,
            font=mono_font,
            fill=color,
        )

    badge = "github.com/sywdn-001/AutoOps"
    badge_w = draw.textlength(badge, font=badge_font)
    draw.text(
        (WIDTH * scale - 78 * scale - badge_w, HEIGHT * scale - 54 * scale),
        badge,
        font=badge_font,
        fill=(125, 145, 175),
    )


def build(lang: str) -> Image.Image:
    canvas = background().resize((WIDTH * 2, HEIGHT * 2), Image.LANCZOS).convert("RGBA")
    rounded_card(canvas, (1960, 150, 3120, 620))
    draw_text_block(canvas, lang)
    return canvas.convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 README 横幅（中/英）")
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "images"), help="输出目录")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for lang, name in (("zh", "banner-zh.png"), ("en", "banner-en.png")):
        image = build(lang)
        path = out / name
        image.save(path, optimize=True)
        print(f"[banner] {path}  {image.size[0]}x{image.size[1]}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
