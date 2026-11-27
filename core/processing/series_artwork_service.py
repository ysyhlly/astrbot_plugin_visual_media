"""Cross-plugin series artwork style presets, Pillow fallback cards & meme thumbnails."""

from __future__ import annotations

import hashlib
import os
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

SERIES_ARTWORK_STYLES: dict[str, dict[str, str]] = {
    "polaroid": {
        "label": "纪念拍立得",
        "badge": "POLAROID MEMORY",
        "prompt": (
            "拍立得相纸纪念照风格，柔和暖色自然光与轻微胶片质感，四周保留优雅拍立得相框留白，"
            "温暖纪实回忆氛围，构图干净治愈，人物与场景细节生动。"
        ),
        "bg": "#F6F1E7",
        "frame": "#FFFDF9",
        "accent": "#C97B5A",
        "ink": "#2F2A26",
        "muted": "#7A6F66",
        "panel_top": "#EBD9C5",
        "panel_bottom": "#D4A88B",
    },
    "comic_strip": {
        "label": "故事连环画",
        "badge": "COMIC STORYBOARD",
        "prompt": (
            "精致日系手绘连环画插画分镜风格，流畅清晰的墨线勾勒与明快水彩上色，"
            "强叙事氛围、角色动作与表情张力十足，适合故事接龙与传话筒插画展示。"
        ),
        "bg": "#FAF6EC",
        "frame": "#FFFFFF",
        "accent": "#3B7A9E",
        "ink": "#1F2933",
        "muted": "#5C6B7A",
        "panel_top": "#D8EAF4",
        "panel_bottom": "#F3DFC1",
    },
    "pictionary": {
        "label": "你画我猜谜面",
        "badge": "PICTIONARY CLUE",
        "prompt": (
            "你画我猜游戏谜面插画：简洁明了的手绘彩铅/马克笔简笔画风格，纯白干净画纸背景，"
            "主体物象特征突出、辨识度极高；【严格禁止】在画面任何位置绘制文字、汉字、英文字母、数字、拼音或标签水印！"
        ),
        "bg": "#F8FAFC",
        "frame": "#FFFFFF",
        "accent": "#5A67D8",
        "ink": "#1E293B",
        "muted": "#64748B",
        "panel_top": "#E0E7FF",
        "panel_bottom": "#FCE7F3",
    },
    "meme_remix": {
        "label": "热梗专属表情",
        "badge": "MEME REMIX",
        "prompt": (
            "高辨识度 Q 版漫画热梗表情包贴纸风格，粗轮廓清晰描边，夸张搞笑神态与动作，"
            "简洁明亮浅色背景，构图紧凑居中、富有吐槽感与群聊传播力。"
        ),
        "bg": "#FFF7E8",
        "frame": "#FFFDF8",
        "accent": "#E05A47",
        "ink": "#2B1E1A",
        "muted": "#8A6557",
        "panel_top": "#FFE1A8",
        "panel_bottom": "#FFBCB0",
    },
    # Backward-compatible aliases from Wave 1
    "poster": {
        "label": "活动纪念海报",
        "badge": "EVENT POSTER",
        "prompt": "精致活动纪念海报构图，清晰主体与氛围光影，高级杂志排版感。",
        "bg": "#1B2230",
        "frame": "#242E42",
        "accent": "#E6A957",
        "ink": "#F8FAFC",
        "muted": "#B6C2D9",
        "panel_top": "#2D3B56",
        "panel_bottom": "#493B5C",
    },
    "postcard": {
        "label": "旅途明信片",
        "badge": "WORLD POSTCARD",
        "prompt": "旅途风景明信片插画质感，通透色彩与细腻光影，治愈系笔触。",
        "bg": "#F2F5F0",
        "frame": "#FFFFFF",
        "accent": "#4F8A6E",
        "ink": "#25332B",
        "muted": "#647A6E",
        "panel_top": "#CDE4D6",
        "panel_bottom": "#EAE2C6",
    },
    "sticker": {
        "label": "Q版贴纸",
        "badge": "STICKER",
        "prompt": "简洁 Q 版表情贴纸风格，清晰轮廓，高辨识度神态。",
        "bg": "#FFF8F0",
        "frame": "#FFFFFF",
        "accent": "#D97757",
        "ink": "#2B211B",
        "muted": "#7C685C",
        "panel_top": "#FDE6C4",
        "panel_bottom": "#FAD4C0",
    },
}


def resolve_artwork_style(style: str) -> tuple[str, dict[str, str]]:
    clean = str(style or "polaroid").strip().lower()
    if clean in SERIES_ARTWORK_STYLES:
        return clean, SERIES_ARTWORK_STYLES[clean]
    return "polaroid", SERIES_ARTWORK_STYLES["polaroid"]


def build_series_artwork_prompt(
    prompt: str,
    *,
    style: str = "polaroid",
    profile: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Build final prompt and canonical style key for ``generate_series_artwork``."""
    clean_prompt = str(prompt or "").strip()
    clean_style = str(style or "polaroid").strip()
    style_key = clean_style.lower() if clean_style.lower() in SERIES_ARTWORK_STYLES else "custom"
    preset = SERIES_ARTWORK_STYLES.get(style_key)

    parts: list[str] = []
    if isinstance(profile, dict):
        appearance = str(profile.get("appearance") or "").strip()
        profile_style = str(profile.get("style") or "").strip()
        if appearance:
            parts.append(appearance)
        if profile_style and style_key != "pictionary":
            parts.append(profile_style)

    if preset is not None:
        parts.append(f"画面风格：{preset['prompt']}")
    elif clean_style:
        parts.append(f"画面风格：{clean_style}")

    if style_key == "pictionary":
        parts.append(f"谜面绘制内容（仅用纯视觉图像表达，切勿写字）：{clean_prompt}")
    else:
        parts.append(clean_prompt)

    return "\n".join(p for p in parts if p), (style_key if preset else clean_style or "polaroid")


def _find_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: list[Path] = []
    repo_root = Path(__file__).resolve().parents[2]
    tl_dir = repo_root / "tl"
    if tl_dir.is_dir():
        for fp in tl_dir.iterdir():
            if fp.is_file() and fp.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                candidates.append(fp)

    system_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    candidates.extend(Path(p) for p in system_candidates)

    for path in candidates:
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _draw_vertical_gradient(
    img: Image.Image,
    box: tuple[int, int, int, int],
    top_hex: str,
    bottom_hex: str,
) -> None:
    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(img)
    r1, g1, b1 = _hex_to_rgb(top_hex)
    r2, g2, b2 = _hex_to_rgb(bottom_hex)
    height = max(1, y1 - y0)
    for dy in range(height):
        t = dy / max(1, height - 1)
        color = (
            int(r1 + (r2 - r1) * t),
            int(g1 + (g2 - g1) * t),
            int(b1 + (b2 - b1) * t),
        )
        draw.line([(x0, y0 + dy), (x1, y0 + dy)], fill=color)


def _wrap_cjk_text(text: str, width: int = 22, max_lines: int = 5) -> list[str]:
    clean = " ".join(str(text or "").split())
    if not clean:
        return []
    lines: list[str] = []
    buf = ""
    units = 0.0
    for ch in clean:
        w = 1.0 if ord(ch) > 127 else 0.55
        if units + w > width and buf:
            lines.append(buf)
            buf = ch
            units = w
            if len(lines) >= max_lines:
                break
        else:
            buf += ch
            units += w
    if buf and len(lines) < max_lines:
        lines.append(buf)
    if len(lines) == max_lines and len("".join(lines)) < len(clean):
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def render_fallback_artwork_card(
    prompt: str,
    *,
    style: str = "polaroid",
    output_dir: Path | str,
    persona_name: str = "",
    source_plugin: str = "",
    reason: str = "",
) -> str:
    """Render a clean Pillow commemorative card when AI image generation is unavailable."""
    style_key, theme = resolve_artwork_style(style)
    out_root = Path(output_dir) / "series_fallback"
    out_root.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(
        f"{style_key}|{prompt}|{persona_name}|{source_plugin}".encode("utf-8")
    ).hexdigest()[:16]
    dest = out_root / f"{style_key}_{digest}.png"

    width, height = (760, 920) if style_key == "polaroid" else (920, 640)
    img = Image.new("RGB", (width, height), _hex_to_rgb(theme["bg"]))
    draw = ImageDraw.Draw(img)

    # Outer commemorative card frame with subtle shadow
    pad = 36
    draw.rounded_rectangle(
        [pad + 6, pad + 8, width - pad + 6, height - pad + 8],
        radius=24,
        fill=(210, 205, 198),
    )
    draw.rounded_rectangle(
        [pad, pad, width - pad, height - pad],
        radius=24,
        fill=_hex_to_rgb(theme["frame"]),
        outline=_hex_to_rgb(theme["accent"]),
        width=3,
    )

    # Inner artwork stage with gradient + geometric botanical/comic motifs
    inner_x0 = pad + 32
    inner_y0 = pad + 78
    inner_x1 = width - pad - 32
    inner_y1 = height - pad - (210 if style_key == "polaroid" else 170)
    _draw_vertical_gradient(
        img,
        (inner_x0, inner_y0, inner_x1, inner_y1),
        theme["panel_top"],
        theme["panel_bottom"],
    )
    draw.rectangle(
        [inner_x0, inner_y0, inner_x1, inner_y1],
        outline=_hex_to_rgb(theme["accent"]),
        width=2,
    )

    # Decorative geometric elements inside the stage
    cx = (inner_x0 + inner_x1) // 2
    cy = (inner_y0 + inner_y1) // 2
    accent_rgb = _hex_to_rgb(theme["accent"])
    ink_rgb = _hex_to_rgb(theme["ink"])
    muted_rgb = _hex_to_rgb(theme["muted"])

    if style_key == "pictionary":
        # Sketchpad dot grid & mystery emblem without leaking plain answer text
        for gx in range(inner_x0 + 28, inner_x1 - 20, 36):
            for gy in range(inner_y0 + 28, inner_y1 - 20, 36):
                draw.ellipse([gx - 2, gy - 2, gx + 2, gy + 2], fill=(180, 190, 215))
        draw.ellipse([cx - 72, cy - 72, cx + 72, cy + 72], outline=accent_rgb, width=5)
        draw.ellipse([cx - 48, cy - 48, cx + 48, cy + 48], fill=_hex_to_rgb(theme["frame"]))
    elif style_key == "comic_strip":
        # Subtle 2-panel comic divider lines
        mid_x = inner_x0 + int((inner_x1 - inner_x0) * 0.56)
        draw.line([(mid_x, inner_y0 + 14), (mid_x, inner_y1 - 14)], fill=ink_rgb, width=3)
        draw.rounded_rectangle(
            [inner_x0 + 22, inner_y0 + 22, mid_x - 18, inner_y1 - 22],
            radius=12,
            outline=accent_rgb,
            width=2,
        )
        draw.rounded_rectangle(
            [mid_x + 18, inner_y0 + 22, inner_x1 - 22, inner_y1 - 22],
            radius=12,
            outline=accent_rgb,
            width=2,
        )
    elif style_key == "meme_remix":
        # Playful comic burst ring
        draw.rounded_rectangle(
            [inner_x0 + 30, inner_y0 + 28, inner_x1 - 30, inner_y1 - 28],
            radius=32,
            fill=_hex_to_rgb(theme["frame"]),
            outline=accent_rgb,
            width=4,
        )
    else:
        # Polaroid warm sun & rolling horizon silhouette
        sun_r = 46
        draw.ellipse(
            [inner_x1 - 140, inner_y0 + 36, inner_x1 - 140 + sun_r * 2, inner_y0 + 36 + sun_r * 2],
            fill=(255, 244, 214),
        )
        draw.polygon(
            [
                (inner_x0, inner_y1),
                (inner_x0 + 160, inner_y1 - 110),
                (inner_x0 + 340, inner_y1 - 45),
                (inner_x1 - 180, inner_y1 - 135),
                (inner_x1, inner_y1 - 55),
                (inner_x1, inner_y1),
            ],
            fill=accent_rgb,
        )

    # Header badge
    font_badge = _find_cjk_font(18)
    font_title = _find_cjk_font(28)
    font_body = _find_cjk_font(22)
    font_small = _find_cjk_font(16)

    draw.text(
        (inner_x0, pad + 26),
        f"{theme['badge']} · {theme['label']}",
        fill=accent_rgb,
        font=font_badge,
    )
    stamp = datetime.now().strftime("%Y.%m.%d")
    draw.text((inner_x1 - 115, pad + 26), stamp, fill=muted_rgb, font=font_small)

    # Center text inside stage or caption area
    if style_key == "pictionary":
        draw.text((cx - 16, cy - 22), "?", fill=accent_rgb, font=_find_cjk_font(44))
        caption_lines = ["画板暂歇 · 谜面卡片已生成", "请结合主持人提示猜出谜底"]
    else:
        caption_lines = _wrap_cjk_text(prompt, width=24 if style_key == "polaroid" else 30, max_lines=4)
        if not caption_lines:
            caption_lines = [theme["label"]]

    # Render caption lines in bottom paper margin
    cap_y = inner_y1 + 24
    for idx, line in enumerate(caption_lines):
        font_use = font_title if idx == 0 and len(caption_lines) <= 2 else font_body
        draw.text((inner_x0, cap_y), line, fill=ink_rgb, font=font_use)
        cap_y += 34 if font_use is font_title else 30

    # Footer metadata
    footer_bits = []
    if persona_name:
        footer_bits.append(f"角色：{persona_name}")
    if source_plugin:
        short_src = source_plugin.removeprefix("astrbot_plugin_")
        footer_bits.append(f"来自 {short_src}")
    footer_bits.append("视觉工坊纪念卡片")
    draw.text(
        (inner_x0, height - pad - 34),
        " · ".join(footer_bits),
        fill=muted_rgb,
        font=font_small,
    )

    img.save(dest, format="PNG")
    return str(dest)


def ensure_meme_thumbnail(
    image_path: str,
    *,
    thumb_dir: Path | str | None = None,
    max_size: int = 240,
) -> str:
    """Return a cached PNG thumbnail path for ``image_path``, falling back to original path."""
    src = Path(str(image_path or ""))
    if not src.is_file():
        return ""
    if thumb_dir is None:
        return str(src)
    try:
        tdir = Path(thumb_dir)
        tdir.mkdir(parents=True, exist_ok=True)
        stat = src.stat()
        key = hashlib.sha256(
            f"{src.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{max_size}".encode("utf-8")
        ).hexdigest()[:20]
        dest = tdir / f"thumb_{key}.png"
        if dest.is_file():
            return str(dest)
        with Image.open(src) as im:
            im.seek(0)
            copy = im.convert("RGBA")
            copy.thumbnail((max_size, max_size))
            copy.save(dest, format="PNG")
        return str(dest)
    except Exception:
        return str(src)
