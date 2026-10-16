"""Pellevoisin: the pump SSR's MOSFET driver, one printable page.
Regenerate with:  python hardware/make_ssr_driver_sheet.py hardware/ssr-driver.pdf   (needs reportlab)"""
import sys
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

out = sys.argv[1]
doc = SimpleDocTemplate(out, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                        title="Pellevoisin: pump SSR driver", author="Massimo Pessino")
ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, spaceAfter=6, alignment=TA_LEFT)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4)
P = ParagraphStyle("p", parent=ss["Normal"], fontSize=10, leading=13)
MONO = ParagraphStyle("m", parent=ss["Code"], fontName="Courier", fontSize=9.5, leading=12)


def table(rows, widths, bold_first_col=False):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if bold_first_col:
        style.append(("FONT", (0, 1), (0, -1), "Helvetica-Bold", 10))
    t.setStyle(TableStyle(style))
    return t


story = [
    Paragraph("Pellevoisin: pump SSR driver", H1),
    Paragraph("The Pi's GPIO 24 reaches the box at 3.1 V, too little to fire the SSR. A logic-level MOSFET beside the "
              "SSR switches the box's 5 V into it instead. GPIO high = pump on, as before.", P),
    Paragraph("Wiring", H2),
    table([
        ["From", "Through", "To"],
        ["box +5 V", "", "SSR terminal 3 (+)"],
        ["SSR terminal 4 (-)", "", "MOSFET drain"],
        ["RJ45 pin 3 (GPIO 24)", "100 ohm", "MOSFET gate"],
        ["MOSFET gate", "100 kohm", "MOSFET source"],
        ["MOSFET source", "", "RJ45 pin 6 (5 V return = Pi GND)"],
    ], [2.2 * inch, 1.0 * inch, 3.6 * inch]),
    Spacer(1, 8),
    Preformatted("""box +5 V ---------------------------- SSR 3 (+)
                                     SSR 4 (-) --- drain
RJ45 pin 3 --[100 ohm]--+------------ gate
                        +--[100 kohm]--+
RJ45 pin 6 ----------------------------+-- source""", MONO),
    Paragraph("Pinouts", H2),
    table([
        ["MOSFET", "Hold it", "Pins, left to right"],
        ["2N7000 (TO-92)", "flat face towards you, legs down", "source - gate - drain"],
        ["RFP30N06LE (TO-220)", "printed face towards you, legs down", "gate - drain - source"],
    ], [1.7 * inch, 2.7 * inch, 2.4 * inch], bold_first_col=True),
    Paragraph("Check", H2),
    Paragraph("Pump commanded on: measure DC across SSR terminals 3 and 4. <b>4.8 V or more</b> = well driven. "
              "Noticeably lower with the 2N7000 = that part's threshold is high; fit the RFP30N06LE.", P),
    Spacer(1, 4),
    Paragraph("<b>Never put the 12 V on the SSR in this circuit.</b> The MOSFET's source is on the Pi's ground; the 12 V "
              "supply's negative is the organ's, and 12 V here would join the two grounds.", P),
    Spacer(1, 4),
    Paragraph("SSR terminals 1 and 2 (mains) are interchangeable. The 100 kohm keeps the pump off while the Pi boots.", P),
]
doc.build(story)
print("wrote", out)
