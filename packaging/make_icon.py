"""Draw PorxPy's program icon (packaging/porxpy.ico).

Why a script rather than only the .ico: the icon is derived from the
app's own colours (``--surface``, ``--accent``, ``--accent2`` in
fund_explorer.html) and the dot that leads the page header, so if
those change, the icon is redrawn rather than repainted by hand.
Pillow comes with reportlab, which the app already needs.

    python packaging/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024                                   # drawn large, scaled down
SURFACE, ACCENT, ACCENT2 = "#181c27", "#4f8ef7", "#7b61ff"


def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill=SURFACE)
    c, r = S // 2, 300
    # A portfolio "x-ray": a ring in two segments around the header's dot.
    box = (c - r, c - r, c + r, c + r)
    d.arc(box, start=-90, end=150, fill=ACCENT, width=110)
    d.arc(box, start=160, end=260, fill=ACCENT2, width=110)
    d.ellipse((c - 110, c - 110, c + 110, c + 110), fill=ACCENT)
    return im


if __name__ == "__main__":
    out = Path(__file__).with_name("porxpy.ico")
    draw().save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
    print(f"wrote {out}")
