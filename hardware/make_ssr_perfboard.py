"""Pellevoisin: the pump SSR's 2N7000 driver on a scrap of perfboard, one printable page.

Regenerate with:  python hardware/make_ssr_perfboard.py hardware/ssr-perfboard.pdf   (needs reportlab)
                  add --png PREFIX to also write the views as PNG files for a quick look.

Holes are 0.1 in apart. The component side is drawn as seen from above; the solder
side is the same board turned over left to right, as you see it with the iron in
hand. Coordinates below are (column, row) on that grid, row numbers growing down.
"""
import sys
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String, Wedge
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

COLS = range(0, 15)          # board columns 0..14
ROWS = range(2, 9)           # board rows 2..8

IN = {"+5V": (1, 3), "SIG": (1, 5), "GND": (1, 7)}
OUT = {"SSR+": (13, 3), "SSR-": (13, 5)}
R1 = ((2, 5), (6, 5))                    # 100 ohm, lying flat
R2 = ((8, 5), (8, 6))                    # 100 kohm, standing up between adjacent holes, clear of the 2N7000
Q = {"D": (10, 4), "G": (10, 5), "S": (10, 6)}   # 2N7000, flat face towards the outputs (east)

BRIDGES = [                              # solder-side connections, as polylines of holes
    ("+5V", [(1, 3), (13, 3)]),
    ("signal", [(1, 5), (2, 5)]),
    ("gate", [(6, 5), (10, 5)]),
    ("source", [(10, 6), (10, 7)]),
    ("source", [(8, 6), (8, 7)]),
    ("ground", [(1, 7), (10, 7)]),
    ("drain", [(10, 4), (12, 4), (12, 5), (13, 5)]),
]
LEADS = set(IN.values()) | set(OUT.values()) | set(R1) | set(R2) | set(Q.values())

COPPER = colors.HexColor("#c87533")
NET_COLOUR = {"+5V": colors.HexColor("#d62728"), "signal": colors.HexColor("#1f77b4"),
              "gate": colors.HexColor("#1f77b4"), "source": colors.HexColor("#222222"),
              "ground": colors.HexColor("#222222"), "drain": colors.HexColor("#2ca02c")}


def view(p, solder_side=False, labels=True, small=False):
    """One view of the board, `p` points per hole pitch."""
    margin_l = 0 if small else 5.2 * p
    margin_r = 0 if small else 5.2 * p
    w = len(COLS) * p + margin_l + margin_r
    h = len(ROWS) * p
    d = Drawing(w, h)

    def X(col):
        c = (COLS[-1] - col) if solder_side else col
        return margin_l + (c - COLS[0] + 0.5) * p

    def Y(row):
        return h - (row - ROWS[0] + 0.5) * p

    d.add(Rect(margin_l, 0, len(COLS) * p, h, fillColor=colors.HexColor("#f4ecd8"),
               strokeColor=colors.HexColor("#8a7a55"), strokeWidth=0.8))
    for c in COLS:
        for r in ROWS:
            if solder_side:
                d.add(Circle(X(c), Y(r), p * 0.32, fillColor=COPPER, strokeColor=None))
            d.add(Circle(X(c), Y(r), p * 0.12, fillColor=colors.white if not solder_side else colors.HexColor("#f4ecd8"),
                         strokeColor=colors.HexColor("#8a7a55"), strokeWidth=0.4))

    if solder_side:
        for net, pts in BRIDGES:
            for (c0, r0), (c1, r1) in zip(pts[:-1], pts[1:]):
                d.add(Line(X(c0), Y(r0), X(c1), Y(r1), strokeColor=NET_COLOUR[net], strokeWidth=p * 0.34,
                           strokeLineCap=1))
        for (c, r) in LEADS:
            d.add(Circle(X(c), Y(r), p * 0.2, fillColor=colors.HexColor("#9a9a9a"), strokeColor=colors.black,
                         strokeWidth=0.6))
    else:
        # terminal blocks: two-tone boxes over their pins
        def block(pins, side):
            cs = [pc for pc, pr in pins]
            rs = [pr for pc, pr in pins]
            x0 = X(min(cs)) - 0.55 * p if side == "L" else X(min(cs)) - 0.45 * p
            d.add(Rect(x0, Y(max(rs)) - 0.95 * p, 1.0 * p, (max(rs) - min(rs)) * p + 1.9 * p,
                       fillColor=colors.HexColor("#3a8a3a"), strokeColor=colors.black, strokeWidth=0.6))
            for pc, pr in pins:
                d.add(Circle(X(pc), Y(pr), p * 0.28, fillColor=colors.HexColor("#d9d9d9"), strokeColor=colors.black,
                             strokeWidth=0.5))
        block(list(IN.values()), "L")
        block(list(OUT.values()), "R")
        # R1, lying flat
        (a, b) = R1
        d.add(Line(X(a[0]), Y(a[1]), X(b[0]), Y(b[1]), strokeColor=colors.HexColor("#777777"), strokeWidth=1.2))
        bx0, bx1 = sorted([X(a[0]) + 0.6 * p, X(b[0]) - 0.6 * p])
        d.add(Rect(bx0, Y(a[1]) - 0.3 * p, bx1 - bx0, 0.6 * p, rx=0.2 * p, ry=0.2 * p,
                   fillColor=colors.HexColor("#e8d3a8"), strokeColor=colors.black, strokeWidth=0.6))
        if labels:
            d.add(String((bx0 + bx1) / 2, Y(a[1]) - 0.95 * p, "100 ohm", fontName="Helvetica-Bold",
                         fontSize=max(5, p * 0.42), textAnchor="middle"))
        # R2, standing up: body over the gate-side hole, lead bent down into the source-side hole
        (g, s) = R2
        d.add(Line(X(g[0]), Y(g[1]), X(s[0]), Y(s[1]), strokeColor=colors.HexColor("#777777"), strokeWidth=1.2))
        d.add(Circle(X(g[0]), Y(g[1]), p * 0.36, fillColor=colors.HexColor("#b9d3ee"), strokeColor=colors.black,
                     strokeWidth=0.6))
        if labels:
            d.add(String(X(g[0]), Y(g[1]) + 0.55 * p, "100 k", fontName="Helvetica-Bold", fontSize=max(5, p * 0.42),
                         textAnchor="middle"))
            d.add(String(X(g[0]) - 0.6 * p, Y(s[1]) + 0.1 * p, "standing", fontName="Helvetica",
                         fontSize=max(4, p * 0.32), textAnchor="end"))
        # the 2N7000: round back towards the west, flat face towards the east
        import math
        cx = X(Q["G"][0])
        cy = Y(Q["G"][1])
        flat_x = cx + 0.5 * p                  # TO-92: 4.8 mm across the flat, 3.8 mm deep
        a, b = 1.5 * p, 0.95 * p
        pts = []
        for k in range(0, 41):
            ang = math.pi / 2 + math.pi * k / 40          # north, round the west, to south
            pts += [flat_x + a * math.cos(ang), cy + b * math.sin(ang)]
        d.add(Polygon(pts, fillColor=colors.HexColor("#333333"), strokeColor=colors.black, strokeWidth=0.6))
        rad = b
        for name, (c, r) in Q.items():
            d.add(Circle(X(c), Y(r), p * 0.17, fillColor=colors.HexColor("#c0c0c0"), strokeColor=None))
            if labels:
                d.add(String(X(c) + 0.95 * p, Y(r) - 0.17 * p, name, fontName="Helvetica-Bold",
                             fontSize=max(5, p * 0.42), fillColor=colors.black, textAnchor="middle"))
        if labels:
            d.add(String(flat_x + 0.15 * p, cy + rad + 0.35 * p, "flat face", fontName="Helvetica",
                         fontSize=max(4, p * 0.32)))

    if labels:
        fs = max(5, p * 0.4)
        texts_in = {"+5V": "+5 V in", "SIG": "SIG  (RJ45 pin 3)", "GND": "GND  (5 V -, RJ45 pin 6)"}
        texts_out = {"SSR+": "SSR+  (SSR 3)", "SSR-": "SSR-  (SSR 4)"}
        for name, (c, r) in IN.items():
            x = X(c)
            if solder_side:
                d.add(String(x + 1.5 * p, Y(r) - fs / 3, texts_in[name], fontName="Helvetica-Bold", fontSize=fs))
            else:
                d.add(String(x - 1.5 * p, Y(r) - fs / 3, texts_in[name], fontName="Helvetica-Bold", fontSize=fs,
                             textAnchor="end"))
        for name, (c, r) in OUT.items():
            x = X(c)
            if solder_side:
                d.add(String(x - 1.5 * p, Y(r) - fs / 3, texts_out[name], fontName="Helvetica-Bold", fontSize=fs,
                             textAnchor="end"))
            else:
                d.add(String(x + 1.5 * p, Y(r) - fs / 3, texts_out[name], fontName="Helvetica-Bold", fontSize=fs))
    return d


def main(argv):
    out = argv[0]
    png = argv[argv.index("--png") + 1] if "--png" in argv else None
    top, bottom, life = view(22), view(22, solder_side=True), view(0.1 * inch, labels=False, small=True)
    if png:
        from reportlab.graphics import renderPM
        renderPM.drawToFile(top, png + "-top.png", fmt="PNG", dpi=110)
        renderPM.drawToFile(bottom, png + "-bottom.png", fmt="PNG", dpi=110)

    c = canvas.Canvas(out, pagesize=letter)
    c.setTitle("Pellevoisin: pump SSR driver on perfboard")
    c.setAuthor("Massimo Pessino")
    W, H = letter
    L = 0.6 * inch
    y = H - 0.65 * inch
    c.setFont("Helvetica-Bold", 16)
    c.drawString(L, y, "Pellevoisin: pump SSR driver on perfboard")
    y -= 16
    c.setFont("Helvetica", 9.5)
    c.drawString(L, y, "2N7000, 100 ohm, 100 kohm, a 3-way and a 2-way screw terminal (5.08 mm). Board 15 x 7 holes, 0.1 in pitch.")

    y -= 22
    c.setFont("Helvetica-Bold", 11)
    c.drawString(L, y, "Component side (from above)")
    y -= top.height + 6
    renderPDF.draw(top, c, (W - top.width) / 2, y)

    y -= 26
    c.setFont("Helvetica-Bold", 11)
    c.drawString(L, y, "Solder side (board turned over left to right): coloured lines are solder bridges or bare wire")
    y -= bottom.height + 6
    renderPDF.draw(bottom, c, (W - bottom.width) / 2, y)

    y -= 22
    c.setFont("Helvetica-Bold", 11)
    c.drawString(L, y, "Actual size, component side")
    renderPDF.draw(life, c, L + 2.6 * inch, y - life.height + 8)
    c.setFont("Helvetica", 8.5)
    c.line(L, y - 12, L + 1 * inch, y - 12)
    c.line(L, y - 15, L, y - 9)
    c.line(L + 1 * inch, y - 15, L + 1 * inch, y - 9)
    c.drawString(L, y - 24, "this bar is 1 in / 25.4 mm")
    c.drawString(L, y - 34, "if not, the printer scaled it")

    y -= life.height + 28
    notes = [
        "Order of work: terminal blocks first, then the 2N7000 (legs short, flat face towards the output block), then the",
        "100 ohm lying flat, then the 100 k standing: body over the gate-row hole, its lead bent down into the hole below.",
        "Bridges on the solder side: +5 V straight along its row to SSR+; signal to the 100 ohm; the 100 ohm's far end, the",
        "100 k's top and the gate joined; the 100 k's foot, the source and the GND row joined; the drain across to SSR-.",
        "Keep the gate row apart from the drain: the hole between them stays empty.",
        "Check with the meter before mains: SSR+ to GND reads the 5 V; gate to source about 3.1 V with the pump on, 0 off.",
        "This layout is for the 2N7000 (S G D). The RFP30N06LE's legs are G D S: do not drop it into these holes.",
        "Mount on nylon standoffs, on the low-voltage side, near the SSR's input terminals and away from its mains ones.",
    ]
    c.setFont("Helvetica", 9)
    for ln in notes:
        c.drawString(L, y, ln)
        y -= 12
    c.showPage()
    c.save()
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1:])
