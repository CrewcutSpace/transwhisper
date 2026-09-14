"""Always-on-top, semi-transparent side window with a rolling feed of translations."""

import html

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget

WIDTH = 420
MARGIN = 16


class _Bridge(QObject):
    """Carries entries from worker threads to the GUI thread."""

    entry = Signal(str, str, bool)


class Overlay(QWidget):
    def __init__(self, max_entries: int, opacity: float, show_original: bool):
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
        self.show_original = show_original
        self._pending: QLabel | None = None  # line still being spoken, updated in place
        self._drag_offset = None
        self._bridge = _Bridge()
        self._bridge.entry.connect(self._update)

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

    def update_entry(self, original: str, translated: str, final: bool):
        """Thread-safe. Partial updates replace the current line; a final one commits it."""
        self._bridge.entry.emit(original, translated, final)

    def _render(self, original: str, translated: str, final: bool) -> str:
        color = "#f2f2f2" if final else "#b8b8b8"
        text = f'<div style="color:{color}; font-size:17px;">{html.escape(translated)}</div>'
        if self.show_original:
            text = f'<div style="color:#8a8a8a; font-size:13px; margin-bottom:3px;">{html.escape(original)}</div>' + text
        return text

    def _update(self, original: str, translated: str, final: bool):
        if not translated.strip():
            # The line turned out to be noise: drop whatever partial text was shown.
            if final and self._pending is not None:
                self._pending.deleteLater()
                self._pending = None
            return
        if self._pending is None:
            self._pending = QLabel()
            self._pending.setWordWrap(True)
            self._pending.setTextFormat(Qt.TextFormat.RichText)
            self._pending.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._feed.addWidget(self._pending)

        self._pending.setText(self._render(original, translated, final))
        if final:
            self._pending = None

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
