"""App icon drawing shared by the tray, the Tk windows and the PyInstaller build."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICON_GREEN = (23, 107, 83, 255)
ICON_GREEN_DARK = (17, 79, 61, 255)
ICON_WHITE = (255, 255, 255, 255)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def draw_icon(size: int = 256) -> Image.Image:
    """Green rounded square with a white microphone, drawn at any pixel size."""
    # Draw oversampled and downscale for smooth edges at small sizes.
    canvas = 512
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = canvas / 64

    def box(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float, float]:
        return (x1 * unit, y1 * unit, x2 * unit, y2 * unit)

    draw.rounded_rectangle(box(4, 4, 60, 60), radius=15 * unit, fill=ICON_GREEN)
    draw.rounded_rectangle(box(4, 4, 60, 60), radius=15 * unit, outline=ICON_GREEN_DARK, width=int(1.2 * unit))

    # Microphone capsule
    draw.rounded_rectangle(box(26, 13, 38, 37), radius=6 * unit, fill=ICON_WHITE)
    # Cradle
    draw.arc(box(19.5, 22, 44.5, 46), start=0, end=180, fill=ICON_WHITE, width=int(4 * unit))
    # Stem and base
    draw.line((32 * unit, 46 * unit, 32 * unit, 52 * unit), fill=ICON_WHITE, width=int(4 * unit))
    draw.rounded_rectangle(box(24, 50.5, 40, 54.5), radius=2 * unit, fill=ICON_WHITE)

    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_icon(path: str | Path) -> Path:
    """Write a multi-resolution Windows .ico file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(target, format="ICO", sizes=[(n, n) for n in ICO_SIZES])
    return target


if __name__ == "__main__":  # pragma: no cover - build helper
    import sys

    print(write_icon(sys.argv[1] if len(sys.argv) > 1 else "build/GroqInsertDictation.ico"))
