"""Draw the KEAT logo.

    python tools/make_logo.py

Writes src/kindle_acquire/assets/keat.png (512 px) and keat.ico. The
drawing is original: a round badge with three rings of segments, like the
tracks of a disk, around the letter K. One segment is tinted, as a
highlight is on a page. Needs PySide6 or PyQt6. Pillow, if installed,
writes a multi-size icon.
"""

import math
import sys
from pathlib import Path

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    from PyQt6 import QtCore, QtGui

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kindle_acquire" / "assets"

ACCENT = "#f39a1e"
DARK = "#221402"
TINT = "#ffdca3"
SIZE = 1024
CENTRE = SIZE / 2.0

# (radius, width, [(start angle, span), ...]) in degrees, counter-clockwise from 3 o'clock.
RINGS = (
    (398, 38, [(96, 70), (180, 36), (226, 64), (300, 46), (356, 34), (38, 44)]),
    (322, 36, [(58, 56), (128, 84), (224, 42), (276, 26), (312, 62), (22, 26)]),
    (248, 34, [(84, 112), (208, 50), (268, 66), (346, 58)]),
)
TINTED = (1, 1)            # ring index, segment index
SPOKES = ((398, 322, 142), (322, 248, 252), (398, 322, 330), (248, 176, 96), (322, 248, 296))
NODES = ((398, 166), (322, 48), (248, 334))


def point(radius, angle):
    rad = math.radians(angle)
    return QtCore.QPointF(CENTRE + radius * math.cos(rad), CENTRE - radius * math.sin(rad))


def draw(size=SIZE):
    image = QtGui.QImage(SIZE, SIZE, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(image)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    accent, dark, tint = QtGui.QColor(ACCENT), QtGui.QColor(DARK), QtGui.QColor(TINT)

    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(accent)
    p.drawEllipse(point(0, 0), 496, 496)
    p.setBrush(dark)
    p.drawEllipse(point(0, 0), 440, 440)

    def pen(colour, width):
        return QtGui.QPen(colour, width, QtCore.Qt.PenStyle.SolidLine,
                          QtCore.Qt.PenCapStyle.FlatCap)

    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    for ring_index, (radius, width, segments) in enumerate(RINGS):
        box = QtCore.QRectF(CENTRE - radius, CENTRE - radius, 2 * radius, 2 * radius)
        for seg_index, (start, span) in enumerate(segments):
            p.setPen(pen(tint if (ring_index, seg_index) == TINTED else accent, width))
            p.drawArc(box, int(start * 16), int(span * 16))
    p.setPen(pen(accent, 30))
    for outer, inner, angle in SPOKES:
        p.drawLine(point(outer, angle), point(inner, angle))
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(accent)
    for radius, angle in NODES:
        p.drawEllipse(point(radius, angle), 31, 31)

    # Centre: a ring and the letter.
    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    p.setPen(pen(accent, 30))
    p.drawEllipse(point(0, 0), 176, 176)
    font = QtGui.QFont("Arial")
    font.setPixelSize(272)
    font.setWeight(QtGui.QFont.Weight.Black)
    path = QtGui.QPainterPath()
    path.addText(0, 0, font, "K")
    bounds = path.boundingRect()
    path.translate(CENTRE - bounds.center().x(), CENTRE - bounds.center().y())
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(accent)
    p.drawPath(path)
    p.end()
    if size != SIZE:
        image = image.scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                             QtCore.Qt.TransformationMode.SmoothTransformation)
    return image


def main():
    app = QtGui.QGuiApplication(sys.argv)  # fonts need an application object
    ASSETS.mkdir(parents=True, exist_ok=True)
    png = ASSETS / "keat.png"
    draw(512).save(str(png))
    ico = ASSETS / "keat.ico"
    try:
        from PIL import Image
        master = ASSETS / "_master.png"
        draw(SIZE).save(str(master))
        Image.open(master).save(ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
        master.unlink()
    except ImportError:
        draw(256).save(str(ico))
    for path in (png, ico):
        print("%s  %d bytes" % (path.name, path.stat().st_size))
    del app


if __name__ == "__main__":
    main()
