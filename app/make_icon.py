"""Draws the app icon into an .iconset folder (turned into AppIcon.icns by iconutil)."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath  # noqa: E402


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # The macOS icon grid: a rounded square with a margin around it.
    margin = size * 0.1
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, QColor("#3a7bfd"))
    gradient.setColorAt(1, QColor("#7b3afd"))
    path = QPainterPath()
    path.addRoundedRect(rect, rect.width() * 0.225, rect.width() * 0.225)
    painter.fillPath(path, gradient)
    font = QFont("Helvetica Neue")
    font.setPixelSize(int(rect.height() * 0.62))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "T")
    painter.end()
    return image


def main(folder: str):
    app = QGuiApplication([])  # noqa: F841  (fonts need it)
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        draw(size).save(str(out / f"icon_{size}x{size}.png"))
        draw(size * 2).save(str(out / f"icon_{size}x{size}@2x.png"))


if __name__ == "__main__":
    main(sys.argv[1])
