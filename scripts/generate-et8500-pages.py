#!/usr/bin/env python3
"""Light-coverage maintenance pages for Epson's two-black printers (ET-8500/8550).

Page 1 (plain paper)  - Matte Black, Cyan, Magenta, Yellow, Gray
Page 2 (photo setting) - Photo Black and Gray only, so the photo-paper job doesn't
                        reprint the colours page 1 already exercised. A4 (for plain
                        paper sent with a photo paper type) and 4x6 versions.

Each ink gets one narrow solid bar. The bars are taller than the print head's
nozzle rows (about an inch), so every nozzle fires on each pass while only a
few percent of the page is covered. A white line across a bar means a blocked
nozzle.

Usage: python3 generate-et8500-pages.py <output_dir>
(writes preset-et8500-plain.png, preset-et8500-photo.png and preset-et8500-photo-4x6.png)
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

DPI = 300
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TEXT = (0, 0, 0)
MUTED = (110, 110, 110)


def mm(v):
    return round(v / 25.4 * DPI)


def font(path, pt):
    try:
        return ImageFont.truetype(path, round(pt / 72 * DPI))
    except OSError:
        return ImageFont.load_default()


def page(size_mm, title, subtitle, bars, bar_w_mm, bar_h_mm, gap_mm, note, title_pt, body_pt):
    w, h = mm(size_mm[0]), mm(size_mm[1])
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    margin = mm(12)

    y = margin
    d.text((margin, y), title, font=font(FONT_BOLD, title_pt), fill=TEXT)
    y += round(title_pt / 72 * DPI * 1.45)
    d.text((margin, y), subtitle, font=font(FONT, body_pt), fill=MUTED)
    y += round(body_pt / 72 * DPI * 1.4) + mm(8)

    bw, bh, gap = mm(bar_w_mm), mm(bar_h_mm), mm(gap_mm)
    total = len(bars) * bw + (len(bars) - 1) * gap
    x = (w - total) // 2
    label_font = font(FONT_BOLD, body_pt)
    for label, colour in bars:
        d.rectangle([x, y, x + bw - 1, y + bh - 1], fill=colour)
        tw = d.textlength(label, font=label_font)
        d.text((x + (bw - tw) / 2, y + bh + mm(2.5)), label, font=label_font, fill=TEXT)
        x += bw + gap
    y += bh + mm(2.5) + round(body_pt / 72 * DPI * 1.4) + mm(6)

    note_font = font(FONT, body_pt * 0.85)
    for line in note:
        d.text((margin, y), line, font=note_font, fill=MUTED)
        y += round(body_pt * 0.85 / 72 * DPI * 1.45)

    hist = img.convert("L").histogram()
    ink = 1 - hist[255] / (w * h)
    return img, ink


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)

    # Page 1: A4, plain paper. sRGB values close to each process ink.
    p1, ink1 = page(
        (210, 297),
        "NozzleWake maintenance page 1 of 2",
        "Epson ET-8500 · plain paper · Matte Black, Cyan, Magenta, Yellow, Gray",
        [("MB", (0, 0, 0)), ("C", (0, 174, 239)), ("M", (236, 0, 140)),
         ("Y", (255, 242, 0)), ("Gray", (150, 150, 150))],
        bar_w_mm=11, bar_h_mm=50, gap_mm=13,
        note=["A white line across a bar means a blocked nozzle. If you see one,",
              "run Nozzle Check / Head Cleaning from the printer's Maintenance menu.",
              "Photo Black is exercised by page 2 (sent as photo paper)."],
        title_pt=14, body_pt=9,
    )
    p1.save(os.path.join(out_dir, "preset-et8500-plain.png"), dpi=(DPI, DPI), optimize=True)

    # Page 2: Photo Black + Gray. The job is sent with a photo-paper type so the
    # printer switches to Photo Black; it can be printed on plain A4 (photo modes
    # lay down more ink, so the bars are narrower) or on 4x6 photo paper.
    pb_bars = [("PB", (0, 0, 0)), ("Gray", (110, 110, 110)), ("Light", (185, 185, 185))]
    p2, ink2 = page(
        (210, 297),
        "NozzleWake maintenance page 2 of 2",
        "Epson ET-8500 \u00b7 sent as photo paper (plain A4 is fine) \u00b7 Photo Black + Gray",
        pb_bars, bar_w_mm=7, bar_h_mm=50, gap_mm=14,
        note=["A white line across a bar means a blocked nozzle.",
              "The photo-paper setting makes the printer use Photo Black;",
              "Cyan, Magenta, Yellow and Matte Black are covered by page 1."],
        title_pt=14, body_pt=9,
    )
    p2.save(os.path.join(out_dir, "preset-et8500-photo.png"), dpi=(DPI, DPI), optimize=True)

    p3, ink3 = page(
        (101.6, 152.4),
        "NozzleWake page 2 of 2",
        "ET-8500 \u00b7 photo paper \u00b7 Photo Black + Gray",
        pb_bars, bar_w_mm=8, bar_h_mm=45, gap_mm=10,
        note=["White lines across a bar = blocked nozzles.",
              "Colours are covered by page 1 (plain paper)."],
        title_pt=11, body_pt=7.5,
    )
    p3.save(os.path.join(out_dir, "preset-et8500-photo-4x6.png"), dpi=(DPI, DPI), optimize=True)

    print(f"page 1: {p1.size[0]}x{p1.size[1]} px, {ink1:.1%} of the page inked")
    print(f"page 2 (A4): {p2.size[0]}x{p2.size[1]} px, {ink2:.1%} of the page inked")
    print(f"page 2 (4x6): {p3.size[0]}x{p3.size[1]} px, {ink3:.1%} of the page inked")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
