"""Always-on-top, semi-transparent side window with a rolling feed of translations."""

import ctypes
import ctypes.util
import html
import json
import subprocess
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget

WIDTH = 420
MARGIN = 16
GAP = 8
STATE_FILE = Path.home() / ".local/share/transwhisper/overlay.json"
TAP_BINARY = Path(__file__).resolve().parent / "audiotap" / "audiotap"

# NSWindowCollectionBehavior: show the window in every Space, including the one a
# full-screen app (Meet in full screen, Zoom, Keynote) creates for itself.
CAN_JOIN_ALL_SPACES = 1 << 0
STATIONARY = 1 << 4
FULL_SCREEN_AUXILIARY = 1 << 8
# NSStatusWindowLevel: above the full-screen app's own window.
STATUS_WINDOW_LEVEL = 25
# NSApplicationActivationPolicy: Regular shows a Dock icon, Accessory hides it.
REGULAR_POLICY = 0
ACCESSORY_POLICY = 1


class _ObjC:
    """Just enough of the Objective-C runtime to configure the native window."""

    def __init__(self):
        self.lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_msgSend.restype = ctypes.c_void_p
        self.lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def send(self, target, selector: bytes):
        return self.lib.objc_msgSend(ctypes.c_void_p(target), self.lib.sel_registerName(selector))

    def send_long(self, target, selector: bytes, value: int):
        function = ctypes.cast(
            self.lib.objc_msgSend, ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)
        )
        function(ctypes.c_void_p(target), self.lib.sel_registerName(selector), value)

    def shared_app(self):
        return self.send(self.lib.objc_getClass(b"NSApplication"), b"sharedApplication")


def _configure_native_window(widget: QWidget, show_in_dock: bool = True):
    """Qt cannot express this, so talk to the underlying NSWindow (and NSApp) directly."""
    try:
        objc = _ObjC()
        objc.send_long(objc.shared_app(), b"setActivationPolicy:", REGULAR_POLICY if show_in_dock else ACCESSORY_POLICY)
        window = objc.send(int(widget.winId()), b"window")
        objc.send_long(window, b"setCollectionBehavior:", CAN_JOIN_ALL_SPACES | STATIONARY | FULL_SCREEN_AUXILIARY)
        objc.send_long(window, b"setLevel:", STATUS_WINDOW_LEVEL)
    except Exception as exc:  # not fatal: the window still works on the normal desktop
        logger.warning("Could not make the window float over full-screen apps: {}", exc)


def find_window(patterns: str) -> dict | None:
    """Ask the helper for the on-screen window of the call (e.g. the Chrome window with Meet)."""
    if not TAP_BINARY.exists():
        return None
    for pattern in (p.strip() for p in patterns.split(",") if p.strip()):
        try:
            found = subprocess.run(
                [str(TAP_BINARY), "--window", pattern], capture_output=True, text=True, timeout=5
            )
            window = json.loads(found.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.debug("Window lookup for {!r} failed: {}", pattern, exc)
            continue
        if window.get("width"):
            logger.info("Placing the window next to: {} — {}", window["owner"], window["title"])
            return window
    return None


class _Bridge(QObject):
    """Carries entries from worker threads to the GUI thread."""

    entry = Signal(str, str, str, bool)


class Overlay(QWidget):
    def __init__(self, max_entries: int, opacity: float, show_original: bool, follow_window: str = "",
                 show_in_dock: bool = True):
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

        self.setWindowTitle("transwhisper")
        self.max_entries = max_entries
        self.show_original = show_original
        self.show_in_dock = show_in_dock
        self.follow_window = follow_window
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

        self._place(follow_window)

    def show(self):
        super().show()
        _configure_native_window(self, self.show_in_dock)
        # Qt resets the native window when it is re-created (screen change, Space switch),
        # so keep re-applying: the call is cheap.
        self._keep_floating = QTimer(self)
        self._keep_floating.timeout.connect(lambda: _configure_native_window(self, self.show_in_dock))
        self._keep_floating.start(2000)
        self._menu_bar_icon()

    def _menu_bar_icon(self):
        """A menu bar item, so the window can always be found again even if it ends up
        somewhere awkward or behind a full-screen app."""
        if getattr(self, "_tray", None) is not None:
            return
        icon = QPixmap(22, 22)
        icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon)
        painter.setPen(Qt.GlobalColor.black)
        font = painter.font()
        font.setPixelSize(16)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(icon.rect(), Qt.AlignmentFlag.AlignCenter, "T")
        painter.end()
        image = QIcon(icon)
        image.setIsMask(True)  # follows the light/dark menu bar

        self._menu = QMenu()
        self._menu.addAction("Show the window", self._bring_back)
        self._menu.addAction("Reset the position", self._reset_position)
        self._menu.addSeparator()
        self._menu.addAction("Quit", QApplication.instance().quit)

        self._tray = QSystemTrayIcon(image, self)
        self._tray.setToolTip("transwhisper")
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(lambda _reason: self._bring_back())
        self._tray.show()

    def _bring_back(self):
        if not self.isVisible():
            self.show()
        self.raise_()
        _configure_native_window(self, self.show_in_dock)

    def _reset_position(self):
        STATE_FILE.unlink(missing_ok=True)
        self._place(self.follow_window)
        self._bring_back()

    # --- placement ---------------------------------------------------------

    def _place(self, follow_window: str):
        call = find_window(follow_window) if follow_window else None
        if call:
            self.setGeometry(self._beside(QRect(call["x"], call["y"], call["width"], call["height"])))
            return
        if self._restore_geometry():
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(screen.right() - WIDTH - MARGIN, screen.top() + MARGIN, WIDTH, screen.height() - 2 * MARGIN)

    def _beside(self, call: QRect) -> QRect:
        """Right next to the call window: outside it if there is room, otherwise along its right edge."""
        screen = QGuiApplication.screenAt(call.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        height = min(call.height() - 2 * GAP, area.height() - 2 * GAP)
        top = max(area.top() + GAP, min(call.top() + GAP, area.bottom() - height - GAP))
        if area.right() - call.right() >= WIDTH + 2 * GAP:
            return QRect(call.right() + GAP, top, WIDTH, height)
        return QRect(min(call.right(), area.right()) - WIDTH - GAP, top, WIDTH, height)

    def _restore_geometry(self) -> bool:
        """Reuse wherever the window was dragged to last time."""
        try:
            saved = json.loads(STATE_FILE.read_text())
            screen = QGuiApplication.primaryScreen().availableGeometry()
            if screen.contains(saved["x"] + 40, saved["y"] + 40):
                self.setGeometry(saved["x"], saved["y"], saved["width"], saved["height"])
                return True
        except (OSError, ValueError, KeyError):
            pass
        return False

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
