#!/usr/bin/env python3
"""Render the post's tables as PNGs.

LinkedIn cannot render a table — not in a post, and not in the article editor, which offers
headings, bold, lists, images and links but no tables. A document post is made of page images, so
these are drawn instead of described. Colours come from the viewer's own palette, so the assets match
the screenshots they sit beside.

    python3 docs/make_tables.py

Two tables, one renderer and one geometry check:

  band-table.png     what matched → what Cedar hands back → the band. Three columns, and the whole
                     rule in one image.
  command-table.png  the same rule over three real commands from the demo, including the one that
                     matched nothing at all. Wider, because it carries the policy ids verbatim.
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).parent

# viewer.py's palette, so these match the UI they explain
BG, PANEL, LINE = "#0d1117", "#161b22", "#2b323f"
TEXT, MUTED, DIM = "#e6e9ef", "#8b95a7", "#6e7681"
GREEN, RED, AMBER = "#3fb950", "#f85149", "#d29922"

S = 2  # 2x, so the assets survive being upscaled by a feed or a slide


def font(px: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    base = "/usr/share/fonts/truetype/dejavu/"
    name = "DejaVuSansMono.ttf" if mono else ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    return ImageFont.truetype(base + name, px * S)


F_HEAD = font(15, bold=True)
F_BAND = font(23, bold=True)
F_FOOT = font(16)

PAD, HEAD_H, ROW_H, GAP, DOT_R, LINE_H = 48, 92, 168, 52, 14, 58


def render(out_name: str, header: tuple[str, ...], rows: list, col_fonts: tuple, col_fills: tuple,
           note_font: ImageFont.FreeTypeFont,
           footer: str | None = None) -> None:
    """Draw one table, auto-sized to its content, and fail if any cell escapes its column.

    Auto-sizing rather than fixed columns: a hardcoded width that is one character too narrow clips
    silently, and a table can look perfectly reasonable in the code that produced it while the image
    is wrong.
    """
    n = len(header)
    # 4px of slack per column: getlength() returns the advance width, while textbbox() measures the
    # ink extent, which can sit a fraction of a pixel wider because of side bearings. Without the
    # slack a cell that exactly fills its column reports as an overrun, and the check becomes noise.
    SLACK = 4
    widths = []
    for i in range(n - 1):
        w = F_HEAD.getlength(header[i])
        for cells, *_ in rows:
            for line in cells[i].split("\n"):
                w = max(w, col_fonts[i].getlength(line))
        widths.append(w + SLACK)
    band_w = F_HEAD.getlength(header[-1])
    for _cells, band, note, _colour in rows:
        band_w = max(band_w, 2 * DOT_R + 18 + F_BAND.getlength(band))
        if note:
            band_w = max(band_w, 2 * DOT_R + 18 + note_font.getlength(note))
    widths.append(band_w + SLACK)

    xs, x = [], PAD + 24
    for w in widths:
        xs.append(x)
        x += w + GAP
    W = int(x - GAP + PAD + 24)
    FOOT_H = 124 if footer else 0
    H = PAD + HEAD_H + ROW_H * len(rows) + FOOT_H + PAD

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    boxes: list[tuple[int, str, tuple[int, int, int, int]]] = []
    d.rounded_rectangle([PAD - 24, PAD - 24, W - PAD + 24, H - PAD + 24], 18, fill=PANEL, outline=LINE)

    def put(col: int, text: str, px: int, py: int, f: ImageFont.FreeTypeFont, fill: str) -> None:
        d.text((px, py), text, font=f, fill=fill, anchor="lm")
        boxes.append((col, text, d.textbbox((px, py), text, font=f, anchor="lm")))

    def block(col: int, text: str, px: int, mid: int, f: ImageFont.FreeTypeFont, fill: str) -> None:
        """Left-aligned lines centred on ``mid``.

        Lines are placed by hand rather than handed to Pillow as one string: with multi-line text the
        vertical anchor applies to the first line, which silently shifts every cell off-centre.
        """
        lines = text.split("\n")
        y = mid - (LINE_H * len(lines)) // 2
        for line in lines:
            put(col, line, px, y, f, fill)
            y += LINE_H

    for i, title in enumerate(header):
        put(i + 1, title, xs[i], PAD + HEAD_H // 2, F_HEAD, DIM)

    y = PAD + HEAD_H
    d.line([(PAD - 24, y), (W - PAD + 24, y)], fill=LINE)

    for cells, band, note, colour in rows:
        mid = y + ROW_H // 2
        for i in range(n - 1):
            block(i + 1, cells[i], xs[i], mid, col_fonts[i], col_fills[i])

        # a dot rather than an emoji: DejaVu has no colour emoji, and this stays on-palette
        band_y = mid - 26
        d.ellipse([xs[-1], band_y - DOT_R, xs[-1] + 2 * DOT_R, band_y + DOT_R], fill=colour)
        bx = xs[-1] + 2 * DOT_R + 18
        put(n, band, bx, band_y, F_BAND, colour)
        if note:
            put(n, note, bx, mid + 24, note_font, DIM)

        y += ROW_H
        d.line([(PAD - 24, y), (W - PAD + 24, y)], fill=LINE)

    if footer:
        block(0, footer, PAD + 24, y + FOOT_H // 2, F_FOOT, MUTED)

    limits = {i + 1: xs[i + 1] - GAP for i in range(n - 1)}
    limits[n] = limits[0] = W - PAD - 24
    problems = [f"clipped: col {c} {t!r} {b}" for c, t, b in boxes
                if b[0] < 0 or b[1] < 0 or b[2] > W or b[3] > H]
    problems += [f"col {c} overruns: {t!r} right={b[2]} > {limits[c]}"
                 for c, t, b in boxes if b[2] > limits[c]]
    if problems:
        raise SystemExit(f"  {out_name}: layout FAIL\n    " + "\n    ".join(problems))

    path = HERE / out_name
    img.save(path)
    print(f"  {path.name:22s} {img.width}×{img.height}  {path.stat().st_size / 1024:4.0f} KB  "
          f"layout PASS ({len(boxes)} runs)")


# ---------------------------------------------------------------- band-table.png
render(
    "band-table.png",
    ("WHAT MATCHED", "CEDAR RETURNS", "THE BAND"),
    [
        (("A permit matched,\nand nothing forbade it", "Allow"),
         "ALLOW", "the model never sees it", GREEN),
        (("A forbid matched", "Deny\n+ the policy id"),
         "BLOCK", "it's dangerous", RED),
        (("Nothing matched at all", "Deny\n+ an empty list"),
         "GRAY", "ask the model, then a human", AMBER),
    ],
    col_fonts=(font(21), font(19, mono=True)),
    col_fills=(TEXT, MUTED),
    note_font=font(16),
    footer=('Cedar reports which policy decided it. So a Deny with no policy named\n'
            'is "no opinion" — not a refusal.'),
)

# ------------------------------------------------------------ command-table.png
render(
    "command-table.png",
    ("COMMAND", "WHAT MATCHED", "CEDAR'S ANSWER", "THE BAND"),
    [
        (("helm delete prod-db -n prod", "forbid\nprod-delete-class-v1",
          'Deny\nids ["prod-delete-class-v1"]'),
         "BLOCK", "it's dangerous", RED),
        (("git status", "permit\nread-only-repo-permit-v1", "Allow"),
         "ALLOW", "the model never sees it", GREEN),
        (("git remote -v", "nothing", "Deny\nids []"),
         "GRAY", "ask the model, then a human", AMBER),
    ],
    col_fonts=(font(16, mono=True), font(18), font(16, mono=True)),
    col_fills=(TEXT, MUTED, MUTED),
    note_font=font(16),
    footer=('Cedar hands back the id of the policy that decided it, so a Deny with an\n'
            'empty list is a call no policy mentioned at all.'),
)
