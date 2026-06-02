"""Generate whisper-ptt.ico — a white microphone on the green brand circle.

Matches the running tray icon's active-state colour (see _STATE_COLORS in ptt.py).
Run standalone or via install-desktop-icon.ps1; only needs Pillow (already a dep).
"""

from pathlib import Path

from PIL import Image, ImageDraw

BRAND_GREEN = (50, 200, 50)
WHITE = (255, 255, 255)
ICON_SIZES = [16, 32, 48, 64, 128, 256]
RENDER_SIZE = 256


def make_image() -> Image.Image:
    """Draw the icon at RENDER_SIZE; downscaling to the .ico sizes stays crisp."""
    s = RENDER_SIZE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Brand circle
    draw.ellipse([8, 8, s - 8, s - 8], fill=BRAND_GREEN)

    # Microphone capsule (body) — vertical, taller than wide
    cx = s // 2
    body_w, body_top, body_bottom = s * 0.15, s * 0.20, s * 0.55
    draw.rounded_rectangle(
        [cx - body_w, body_top, cx + body_w, body_bottom],
        radius=body_w,
        fill=WHITE,
    )

    # Mic stand: arc cradle hugging the capsule + post + base
    cradle = [cx - body_w * 1.9, body_top + s * 0.14,
              cx + body_w * 1.9, body_bottom + s * 0.12]
    draw.arc(cradle, start=15, end=165, fill=WHITE, width=max(2, s // 26))
    post_top, post_bottom = body_bottom + s * 0.11, s * 0.78
    draw.line([cx, post_top, cx, post_bottom], fill=WHITE, width=max(2, s // 22))
    base_w = s * 0.13
    draw.line([cx - base_w, post_bottom, cx + base_w, post_bottom],
              fill=WHITE, width=max(2, s // 24))

    return img


def main() -> None:
    out = Path(__file__).resolve().parent / "whisper-ptt.ico"
    img = make_image()
    img.save(out, format="ICO", sizes=[(n, n) for n in ICON_SIZES])
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
