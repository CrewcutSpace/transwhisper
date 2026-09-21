"""Always-on-top, semi-transparent side window with a rolling feed of translations."""

import ctypes
import ctypes.util
import html
import json
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget

WIDTH = 420
MARGIN = 16
STATE_FILE = Path.home() / ".local/share/transwhisper/overlay.json"

# NSWindowCollectionBehavior: show the window in every Space, including the one a
# full-screen app (Meet in full screen, Zoom, Keynote) creates for itself.
CAN_JOIN_ALL_SPACES = 1 << 0
STATIONARY = 1 << 4
FULL_SCREEN_AUXILIARY = 1 << 8
# NSStatusWindowLevel: above the full-screen app's own window.
STATUS_WINDOW_LEVEL = 25


def _show_over_fullscreen_apps(widget: QWidget):
    """Qt cannot set this, so talk to the underlying NSWindow directly."""
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        window = objc.objc_msgSend(ctypes.c_void_p(int(widget.winId())), objc.sel_registerName(b"window"))
        send_long = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long))
        send_long(window, objc.sel_registerName(b"setCollectionBehavior:"),
                  CAN_JOIN_ALL_SPACES | STATIONARY | FULL_SCREEN_AUXILIARY)
        send_long(window, objc.sel_registerName(b"setLevel:"), STATUS_WINDOW_LEVEL)
    except Exception as exc:  # not fatal: the window still works on the normal desktop
        logger.warning("Could not make the window appear over full-screen apps: {}", exc)


class _Bridge(QObject):
    """Carries entries from worker threads to the GUI thread."""

    entry = Signal(str, str, str, bool)


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

        self._restore_geometry()

    def show(self):
        super().show()
        _show_over_fullscreen_apps(self)

    # --- placement ---------------------------------------------------------

    def _restore_geometry(self):
        """Reuse wherever the window was dragged to last time."""
        try:
            saved = json.loads(STATE_FILE.read_text())
            screen = QGuiApplication.primaryScreen().availableGeometry()
            if screen.contains(saved["x"] + 40, saved["y"] + 40):
                self.setGeometry(saved["x"], saved["y"], saved["width"], saved["height"])
                return
        except (OSError, ValueError, KeyError):
            pass
        screen = QGuiApplication.primaryScreen().availableGeometry()
        height = screen.height() - 2 * MARGIN
        self.setGeometry(screen.right() - WIDTH - MARGIN, screen.top() + MARGIN, WIDTH, height)

    def _save_geometry(self):
        rect = self.geometry()
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()}))
        except OSError as exc:
            logger.debug("Could not save the window position: {}", exc)

    # --- feed --------------------------------------------------------------

    def update_entry(self, original: str, done: str, pending: str, final: bool):
        """Thread-safe. `done` sentences will not change any more (white), `pending` is the
        sentence still being spoken (grey). Partial updates replace the current line; a final one commits it."""
        self._bridge.entry.emit(original, done, pending, final)

    def _render(self, original: str, done: str, pending: str) -> str:
        text = (
            f'<div style="font-size:17px;"><span style="color:#f2f2f2;">{html.escape(done)}</span> '
            f'<span style="color:#9a9a9a;">{html.escape(pending)}</span></div>'
        )
        if self.show_original:
            text = f'<div style="color:#8a8a8a; font-size:13px; margin-bottom:3px;">{html.escape(original)}</div>' + text
        return text

    def _update(self, original: str, done: str, pending: str, final: bool):
        if not (done + pending).strip():
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

        self._pending.setText(self._render(original, done, pending))
        if final:
            self._pending = None

        # Index 0 is the stretch that keeps entries pinned to the bottom.
        while self._feed.count() - 1 > self.max_entries:
            item = self._feed.takeAt(1)
            item.widget().deleteLater()

    # --- dragging ----------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_geometry()


def create_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(True)
    return app
