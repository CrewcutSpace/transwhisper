"""Always-on-top, semi-transparent side window with a rolling feed of translations."""

import html

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget

WIDTH = 420
MARGIN = 16


class _Bridge(QObject):
    """Carries entries from worker threads to the GUI thread."""

    entry = Signal(str, str)


class Overlay(QWidget):
    def __init__(self, max_entries: int, opacity: float):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        # Never take focus away from the call window.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowOpacity(opacity)
        self.setStyleSheet("background-color: #151515;")

        self.max_entries = max_entries
        self._drag_offset = None
        self._bridge = _Bridge()
        self._bridge.entry.connect(self._append)

        self._feed = QVBoxLayout()
        self._feed.setContentsMargins(12, 12, 12, 12)
        self._feed.setSpacing(10)
        self._feed.addStretch(1)
        container = QWidget()
        container.setLayout(self._feed)

        self._scroll = QScrollArea()
        self._scroll.setWidget(container)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.verticalScrollBar().rangeChanged.connect(
            lambda _min, max_: self._scroll.verticalScrollBar().setValue(max_)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll)

        self._place_on_right_edge()

    def _place_on_right_edge(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        height = screen.height() - 2 * MARGIN
        self.setGeometry(screen.right() - WIDTH - MARGIN, screen.top() + MARGIN, WIDTH, height)

    def add_entry(self, original: str, translated: str):
        """Thread-safe: may be called from any thread."""
        self._bridge.entry.emit(original, translated)

    def _append(self, original: str, translated: str):
        label = QLabel(
            f'<div style="color:#9a9a9a; font-size:13px;">{html.escape(original)}</div>'
            f'<div style="color:#f2f2f2; font-size:17px; margin-top:3px;">{html.escape(translated)}</div>'
        )
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._feed.addWidget(label)

        # Index 0 is the stretch that keeps entries pinned to the bottom.
        while self._feed.count() - 1 > self.max_entries:
            item = self._feed.takeAt(1)
            item.widget().deleteLater()

    # Frameless window: drag it with the mouse.
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None


def create_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(True)
    return app
