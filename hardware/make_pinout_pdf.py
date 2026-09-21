"""Pellevoisin: the Pi's header and the box, on two printable pages.
Regenerate with:  python hardware/make_pinout_pdf.py hardware/pi-header-and-box.pdf   (needs reportlab)"""
import sys
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

out = sys.argv[1]
doc = SimpleDocTemplate(out, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                        title="Pellevoisin: Pi header and box wiring", author="Massimo Pessino")
ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, spaceAfter=4, alignment=TA_LEFT)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)
P = ParagraphStyle("p", parent=ss["Normal"], fontSize=9, leading=12)
SMALL = ParagraphStyle("s", parent=P, fontSize=8, leading=10, textColor=colors.HexColor("#444444"))
MONO = ParagraphStyle("m", parent=ss["Code"], fontName="Courier", fontSize=7.6, leading=9.2)   # 115 columns across 7.3 in

# (pin, function, ours) for the left column (odd) and right column (even)
pins = {
    1: ("3.3 V", "MIDI: 6N137 module IN+"), 2: ("5 V", "free (Pi fed by USB-C from the SD-25A-5)"),
    3: ("GPIO 2 / SDA", "free; I2C stays off"), 4: ("5 V", "free"),
    5: ("GPIO 3 / SCL", "ON/OFF SWITCH, one side"), 6: ("GND", "ON/OFF SWITCH, other side"),
    7: ("GPIO 4", ""), 8: ("GPIO 14 / TXD", "MIDI: 6N137 module IN-"),
    9: ("GND", "RESET opto cathode (PC817 pin 2)"), 10: ("GPIO 15 / RXD", "free"),
    11: ("GPIO 17", "RESET: 330 ohm to PC817 anode"), 12: ("GPIO 18", ""),
    13: ("GPIO 27", "STATUS LED: 330 ohm to LED anode"), 14: ("GND", "STATUS LED cathode"),
    15: ("GPIO 22", ""), 16: ("GPIO 23", ""),
    17: ("3.3 V", ""), 18: ("GPIO 24", "PUMP: SSR terminal 3 (+)"),
    19: ("GPIO 10", ""), 20: ("GND", "PUMP: SSR terminal 4 (-)"),
    21: ("GPIO 9", ""), 22: ("GPIO 25", ""),
    23: ("GPIO 11", ""), 24: ("GPIO 8", ""),
    25: ("GND", ""), 26: ("GPIO 7", ""),
    27: ("ID_SD", "never"), 28: ("ID_SC", "never"),
    29: ("GPIO 5", ""), 30: ("GND", ""),
    31: ("GPIO 6", ""), 32: ("GPIO 12", ""),
    33: ("GPIO 13", ""), 34: ("GND", ""),
    35: ("GPIO 19", ""), 36: ("GPIO 16", ""),
    37: ("GPIO 26", ""), 38: ("GPIO 20", ""),
    39: ("GND", ""), 40: ("GPIO 21", ""),
}
rows = [["ours", "function", "pin", "pin", "function", "ours"]]
for odd in range(1, 40, 2):
    fo, uo = pins[odd]
    fe, ue = pins[odd + 1]
    rows.append([uo, fo, str(odd), str(odd + 1), fe, ue])

table = Table(rows, colWidths=[2.25 * inch, 1.05 * inch, 0.4 * inch, 0.4 * inch, 1.05 * inch, 2.25 * inch], repeatRows=1)
style = [
    ("FONT", (0, 0), (-1, -1), "Helvetica", 8),
    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
    ("FONT", (2, 1), (3, -1), "Helvetica-Bold", 9),
    ("ALIGN", (2, 0), (3, -1), "CENTER"),
    ("ALIGN", (0, 1), (0, -1), "RIGHT"),
    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ("BOX", (2, 1), (3, -1), 1.2, colors.black),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
    ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
]
used = {1, 5, 6, 8, 9, 11, 13, 14, 18, 20}
for r, odd in enumerate(range(1, 40, 2), start=1):
    for pin, cols in ((odd, (0, 2)), (odd + 1, (3, 5))):
        if pin in used:
            style.append(("BACKGROUND", (cols[0], r), (cols[1], r), colors.HexColor("#ffe9a8")))
            style.append(("FONT", (cols[0], r), (cols[1], r), "Helvetica-Bold", 8))
        elif pin in (27, 28):
            style.append(("TEXTCOLOR", (cols[0], r), (cols[1], r), colors.HexColor("#aa0000")))
        f = pins[pin][0]
        if f == "GND":
            style.append(("TEXTCOLOR", (cols[1] if cols == (0, 2) else cols[1] - 1, r),
                          (cols[1] if cols == (0, 2) else cols[1] - 1, r), colors.HexColor("#333333")))
table.setStyle(TableStyle(style))

story = [
    Paragraph("Pellevoisin: the Raspberry Pi 4 header", H1),
    Paragraph("Pin 1 top left with the board's edge at the top: odd pins in the inner row, even pins in the outer "
              "row next to the edge. Highlighted cells are this organ's connections. Every GND pin is the Pi's ground, "
              "and nothing on the organ's side ever touches this header.", P),
    Spacer(1, 8),
    table,
    Spacer(1, 8),
    Paragraph("Each line is paired with its nearest ground so every cable is a short twisted pair: 5 and 6 for the "
              "switch, 11 and 9 for reset, 13 and 14 for the status LED, 18 and 20 for the pump. The MIDI pair is pins 1 and 8 "
              "and carries no ground: TX is its return. Pins 3 and 5 are the I2C bus, which stays disabled so GPIO 3 is "
              "free for the switch and wakes a halted Pi when pulled low. The DSI screen uses its own ribbon connector.", P),
    Spacer(1, 6),
    Paragraph("Part pinouts: PC817 pin 1 anode, 2 cathode, 3 emitter, 4 collector (dot at pin 1). SSR-xx DA: 1 and 2 "
              "load (mains), 3 positive and 4 negative input, 3 to 32 V DC. 6N137 module: IN+ and IN- marked on the "
              "terminals; VCC, OUT, GND on the other side. SD-25A-5: +Vin/-Vin from the 12 V bus through a 2 A fuse, +V/-V trimmed to 5.1 V at "
              "the Pi into its USB-C; 1500 V isolation inside. organ_web: --pump 24 --power-switch 3; reset on 17. "
              "Status LED: config.txt dtoverlay=gpio-led,gpio=27,label=pellevoisin,trigger=heartbeat; it beats while Linux runs and is dark when halted.", SMALL),
    PageBreak(),
    Paragraph("Pellevoisin: the box, wired", H1),
    Paragraph("Two halves in one aluminium case, and exactly four things cross between them, three by light and one by a "
              "transformer: the MIDI opto, the reset opto, the SSR, and the isolated converter that makes the Pi's 5 V "
              "from the 12 V bus.", P),
    Spacer(1, 6),
]
schematic = r"""
MAINS -- IEC inlet with fuse -+- E ---- earth stud on the case ---- motor frame
                              +- N ---------------+-------------------------------- motor N
                              |                  ATX N
                              +- L -- PANEL SWITCH, 2 poles, 3 positions, centre off, motor rated
                                       pole A common -+- BOOK -----------------------> motor L
                                                      +- AUTO -> SSR 1 -- SSR 2 ------> motor L
                                       pole B common --- AUTO -> ATX L   (PS_ON tied to ATX GND: on with Auto)
                                       (OFF: neither pole connected; the ATX fan cools the box in Auto)

ORGAN SIDE (the ATX's ground)                        PI SIDE (the converter's output ground)
ATX 12 V, several yellows -> 12 V bus bar            SD-25A-5 +V / -V, trimmed to 5.1 V --USB-C--> Pi 4
ATX GND, several blacks --> organ GND bus bar        DSI ribbon --> touchscreen
ATX PS_ON (green) --------> ATX GND: on with Auto    GPIO 3 (pin 5) -- antique switch -- Pi GND (pin 6)   on/off
12 V bus / GND bus -------> boards 1-4, 5 A fuses    GPIO 14 TX (pin 8) -- 6N137 IN-             MIDI
12 V bus -[2 A fuse]-> SD-25A-5 +Vin, GND bus > -Vin 3.3 V (pin 1) ------- 6N137 IN+
         ----- transformer inside the SD-25A-5 ----- GPIO 24 (pin 18) -- SSR 3 (+)                pump
last board ISP pin 2 -----> RJ12 pin 6 = 5 V bus     Pi GND (pin 20) --- SSR 4 (-)
RJ12 pin 1 = /RESET bus (jumpered board to board)    GPIO 27 (pin 13) -[330 ohm]- LED - Pi GND (pin 14)  alive
RJ12: MIDI signal, GND, as shipped                   GPIO 17 (pin 11) -[330 ohm]- PC817 anode         reset
6N137 module: VCC < RJ12 pin 6, GND < RJ12 GND,      Pi GND (pin 9) ------------ PC817 cathode
              OUT > boards' MIDI in
PC817: collector - RJ12 pin 1, emitter - RJ12 GND
SSR 1 / 2: on the mains side, above; MOV across them
""".strip("\n")
story += [
    Preformatted(schematic, MONO),
    Spacer(1, 10),
    Paragraph("Rules", H2),
]
rules = [
    "No wire, bar or chassis lug between the organ's GND bus and the Pi's ground: the Pi's 5 V comes through the converter's "
    "transformer, never from a buck. The earth stud carries earth only, and no DC negative goes to it.",
    "Mains bus bars shrouded, or covered DIN terminal blocks instead. Low-voltage bars may be bare.",
    "Mains and low voltage on opposite sides of a partition: inlet, switch, supplies' inputs, SSR 1 and 2, motor lead on "
    "one side; Pi, RJ12 bus, optos, SSR 3 and 4 on the other. A wire that must cross does so once, at right angles.",
    "The SSR's heat sink in the ATX fan's stream, thermal paste under the module. A varistor across SSR 1 and 2.",
    "Only the live is switched, ever. Neutral and earth run straight through. The panel switch is motor rated: "
    "1 HP at 125 V, or 20 A. The antique on/off switch carries microamps and may be anything.",
    "Book: the motor runs, nothing else has power. Auto: the ATX starts at once (PS_ON tied to its ground) and the 12 V bus, "
    "the fan, the boards and the Pi through its converter come up together; the motor runs only when the SSR says so. "
    "Off: everything isolated (the SSR alone leaks a milliamp).",
    "Before the panel goes from Auto to Off: the antique switch off, or Shut down on the Service tab, and wait for the status LED to go dark.",
]
for r in rules:
    story.append(Paragraph("- " + r, P))
doc.build(story)
print("wrote", out)
