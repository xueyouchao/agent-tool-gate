#!/usr/bin/env python3
"""Render the three-band table as a PNG.

LinkedIn cannot render a table — not in a post, and not in the article editor, which offers
headings, bold, lists, images and links but no tables. A document post is made of images, so this
draws the table instead of describing it. Colours come from the viewer's own palette so the asset
matches the screenshots it sits beside.

    python3 docs/make_band_table.py            # writes docs/band-table.png
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path(__file__).with_name("band-table.png")

# viewer.py's palette, so this matches the UI it explains
BG, PANEL, LINE = "#0d1117", "#161b22", "#2b323f"
TEXT, MUTED, DIM = "#e6e9ef", "#8b95a7", "#6e7681"
GREEN, RED, AMBER = "#3fb950", "#f85149", "#d29922"

S = 2  # 2x, so it survives being upscaled by a feed or a slide
W = 1760

HEADER = ("WHAT MATCHED", "CEDAR RETURNS", "THE BAND")
ROWS = [
    ("A permit matched,\nand nothing forbade it", "Allow",
     "ALLOW", "the model never sees it", GREEN),
    ("A forbid matched", "Deny\n+ the policy id",
     "BLOCK", "it's dangerous", RED),
    ("Nothing matched at all", "Deny\n+ an empty list",
     "GRAY", "ask the model, then a human", AMBER),
]
FOOTER = ('Cedar reports which policy decided it. So a Deny with no policy named\n'
          'is "no opinion" — not a refusal.')


def font(px: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    base = "/usr/share/fonts/truetype/dejavu/"
    name = "DejaVuSansMono.ttf" if mono else ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    return ImageFont.truetype(base + name, px * S)


F_HEAD = font(15, bold=True)
F_CELL = font(21)
F_MONO = font(19, mono=True)
F_BAND = font(23, bold=True)
F_NOTE = font(16)
F_FOOT = font(16)

PAD, HEAD_H, ROW_H, FOOT_H = 48, 92, 168, 124
COL1, COL2, COL3 = 96, 620, 1150
H = PAD + HEAD_H + ROW_H * len(ROWS) + FOOT_H + PAD

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
# (column index, text, bbox) — column 0 spans the full width, and is checked as such
boxes: list[tuple[int, str, tuple[int, int, int, int]]] = []
d.rounded_rectangle([PAD - 24, PAD - 24, W - PAD + 24, H - PAD + 24], 18, fill=PANEL, outline=LINE)


def put(col: int, text: str, x: int, y: int, f: ImageFont.FreeTypeFont, fill: str) -> None:
    d.text((x, y), text, font=f, fill=fill, anchor="lm")
    boxes.append((col, text, d.textbbox((x, y), text, font=f, anchor="lm")))


def block(col: int, text: str, x: int, mid: int, f: ImageFont.FreeTypeFont, fill: str,
          line_h: int) -> None:
    """Draw a left-aligned block of lines vertically centred on ``mid``.

    Each line is placed by hand rather than passing the newlines to Pillow: with multi-line text the
    vertical anchor applies to the first line, which silently shifts every cell off-centre.
    """
    lines = text.split("\n")
    y = mid - (line_h * len(lines)) // 2
    for line in lines:
        put(col, line, x, y, f, fill)
        y += line_h


for title, x in zip(HEADER, (COL1, COL2, COL3)):
    put(1 if x == COL1 else 2 if x == COL2 else 3, title, x, PAD + HEAD_H // 2, F_HEAD, DIM)

y = PAD + HEAD_H
d.line([(PAD - 24, y), (W - PAD + 24, y)], fill=LINE)

for c1, c2, band, note, colour in ROWS:
    mid = y + ROW_H // 2
    block(1, c1, COL1, mid, F_CELL, TEXT, 56)
    block(2, c2, COL2, mid, F_MONO, MUTED, 48)

    # a dot rather than an emoji: DejaVu has no colour emoji, and this stays on-palette
    r, band_y = 14, mid - 26
    d.ellipse([COL3, band_y - r, COL3 + 2 * r, band_y + r], fill=colour)
    bx = COL3 + 2 * r + 18
    put(3, band, bx, band_y, F_BAND, colour)
    put(3, note, bx, mid + 24, F_NOTE, DIM)

    y += ROW_H
    d.line([(PAD - 24, y), (W - PAD + 24, y)], fill=LINE)

block(0, FOOTER, COL1, y + FOOT_H // 2, F_FOOT, MUTED, 44)

# Drawn, then checked. A table whose third column has overrun into the second still looks fine in
# the code that produced it, so assert the geometry rather than trusting the constants above.
limits = {1: COL2, 2: COL3, 3: W - PAD}
problems = [f"clipped: col {c} {t!r} {b}" for c, t, b in boxes
            if b[0] < 0 or b[1] < 0 or b[2] > W or b[3] > H]
for col, text, b in boxes:
    if b[2] > limits.get(col, W - PAD):
        problems.append(f"col {col} overruns: {text!r} right={b[2]} > {limits.get(col, W - PAD)}")
if problems:
    raise SystemExit("layout: FAIL\n  " + "\n  ".join(problems))

img.save(OUT)
print(f"  {OUT}  {img.width}×{img.height}  {OUT.stat().st_size / 1024:.0f} KB")
print(f"  layout: PASS — {len(boxes)} text runs, none clipped, no column collisions")
