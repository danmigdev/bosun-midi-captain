#!/usr/bin/env python3
"""Pre-render boot milestones; the Pi only copies pixels during startup."""
import argparse
import json
from pathlib import Path
import struct

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[3]
SIZE = (640, 360)
MILESTONES = (20, 45, 65, 80)


def render(output: Path, font: Path, *, preview: Path | None = None,
           percent: int = 20) -> None:
    if percent not in MILESTONES:
        raise ValueError("Unknown boot milestone")
    version = json.loads((REPO / "editor/package.json").read_text(encoding="utf-8"))["version"]
    picture = Image.new("RGB", SIZE, "#0f141a")
    icon = Image.open(REPO / "editor/src-tauri/icons/icon.png").convert("RGBA")
    icon = icon.resize((166, 166), Image.Resampling.LANCZOS)
    picture.paste(icon, ((SIZE[0] - 166) // 2, 28), icon)
    draw = ImageDraw.Draw(picture)
    for label, size, y, color in (("Bosun", 36, 207, "#e4e6eb"),
                                 (f"v{version}", 18, 258, "#9aa1ad")):
        draw.text((SIZE[0] // 2, y), label, font=ImageFont.truetype(str(font), size),
                  fill=color, anchor="mt")
    left, top, width, height = 210, 294, 220, 6
    draw.rounded_rectangle((left, top, left + width - 1, top + height - 1),
                           radius=3, fill="#2a2e36")
    draw.rounded_rectangle((left, top, left + width * percent // 100 - 1, top + height - 1),
                           radius=3, fill="#6fd99b")
    draw.text((SIZE[0] // 2, 310), f"{percent}%",
              font=ImageFont.truetype(str(font), 14), fill="#9aa1ad", anchor="mt")
    output.write_bytes(b"BSPLASH1" + struct.pack("<II", *SIZE) + picture.tobytes())
    if preview:
        picture.save(preview)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path,
                        default=Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--milestones", action="store_true",
                        help="Also write the later boot phase pictures alongside output")
    args = parser.parse_args()
    render(args.output, args.font, preview=args.preview)
    if args.milestones:
        for percent in MILESTONES[1:]:
            render(args.output.with_name(f"splash-{percent}.bin"), args.font, percent=percent)
