"""Always-on-top, semi-transparent side window with a rolling feed of translations."""

import ctypes
import ctypes.util
import html
import json
import subprocess
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QApplication, QHBoxLayout, QLabel, QSizeGrip, QMenu, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget

# Default size; the window can be resized from its bottom-right corner and keeps that size.
WIDTH = 380
SPLIT_WIDTH = 600  # original on the left, translation on the right
HEIGHT = 380
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

RADIUS = 12
# Follows the macOS appearance (System Settings > Appearance), switching live.
THEMES = {
    "dark": {"background": QColor(30, 30, 32), "border": QColor(255, 255, 255, 30),
             "done": "#f2f2f7", "pending": "#98989d", "original": "#8e8e93"},
    "light": {"background": QColor(246, 246, 248), "border": QColor(0, 0, 0, 30),
              "done": "#1d1d1f", "pending": "#6e6e73", "original": "#86868b"},
}


class _TrafficLight(QAbstractButton):
    """A macOS-style window button: a coloured dot that shows its symbol on hover."""

    SIZE = 12

    def __init__(self, color: str, symbol: str, tooltip: str, parent: QWidget):
        super().__init__(parent)
        self._color = QColor(color)
        self._symbol = symbol
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setToolTip(tooltip)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.show_symbol = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._color.darker(115), 0.5))
        painter.setBrush(self._color.darker(115) if self.isDown() else self._color)
        painter.drawEllipse(self.rect().adjusted(0, 0, -1, -1))
        if not self.show_symbol:
            return
        pen = QPen(QColor(0, 0, 0, 150), 1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        c, d = self.SIZE / 2 - 0.5, 2.5
        if self._symbol == "close":
            painter.drawLine(int(c - d), int(c - d), int(c + d), int(c + d))
            painter.drawLine(int(c - d), int(c + d), int(c + d), int(c - d))
        else:
            painter.drawLine(int(c - d - 0.5), int(c), int(c + d + 0.5), int(c))


class _TitleBar(QWidget):
    """Close and minimise buttons in the top-left corner, symbols shown while hovering the group."""

    def __init__(self, on_close, on_minimise, parent: QWidget):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.close_button = _TrafficLight("#ff5f57", "close", "Quit transwhisper", self)
        self.minimise_button = _TrafficLight("#febc2e", "minimise", "Hide to the menu bar", self)
        self.close_button.clicked.connect(on_close)
        self.minimise_button.clicked.connect(on_minimise)
        self._buttons = QWidget(self)
        row = QHBoxLayout(self._buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.close_button)
        row.addWidget(self.minimise_button)
        self._buttons.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._buttons.installEventFilter(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 0)
        layout.addWidget(self._buttons)
        layout.addStretch(1)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave):
            hovering = event.type() == QEvent.Type.HoverEnter
            for button in (self.close_button, self.minimise_button):
                button.show_symbol = hovering
                button.update()
        return False


def _is_dark() -> bool:
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Unknown:
        return QGuiApplication.palette().window().color().lightness() < 128
    return scheme == Qt.ColorScheme.Dark


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
        # Rounded corners: the window itself is transparent, paintEvent draws the panel.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._theme = THEMES["dark" if _is_dark() else "light"]
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._apply_theme)

        self.setWindowTitle("transwhisper")
        self.max_entries = max_entries
        self.show_original = show_original
        self._layout_name = "split" if show_original else "single"
        self.resize(SPLIT_WIDTH if show_original else WIDTH, HEIGHT)
        self._restore_size()
        self.show_in_dock = show_in_dock
        self.follow_window = follow_window
        self._following = True
        self._followed_window = ""
        self._pending: QLabel | None = None  # line still being spoken, updated in place
        self._drag_offset = None
        self._bridge = _Bridge()
        self._bridge.entry.connect(self._update)

        self._feed = QVBoxLayout()
        self._feed.setContentsMargins(12, 4, 4, 8)  # + the 8px scrollbar on the right
        self._feed.setSpacing(8)
        # Reads like a document: from the top down, each new entry below the last one.
        self._feed.addStretch(1)
        container = QWidget()
        container.setLayout(self._feed)
        container.setStyleSheet("background: transparent;")

        self._scroll = QScrollArea()
        self._scroll.setWidget(container)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollBar:vertical { width: 8px; background: transparent; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(128, 128, 128, 110); border-radius: 3px;"
            " min-height: 24px; margin: 0 2px 0 0; }"
            "QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page"
            " { background: none; height: 0; }"
        )
        self._scroll.viewport().setAutoFillBackground(False)
        # Follow new text only while the view is at the bottom, so scrolling up to read
        # the history is not yanked back down by every new word.
        self._at_bottom = True
        bar = self._scroll.verticalScrollBar()
        bar.valueChanged.connect(lambda value: setattr(self, "_at_bottom", value >= bar.maximum() - 4))
        bar.rangeChanged.connect(lambda _min, max_: bar.setValue(max_) if self._at_bottom else None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(_TitleBar(QApplication.instance().quit, self.hide, self))
        layout.addWidget(self._scroll)
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        layout.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        # Save the size when the user lets go of the corner (not on resizes made by the app).
        grip.installEventFilter(self)
        self.setMinimumSize(240, 140)

        self._place(follow_window)

    def show(self):
        super().show()
        _configure_native_window(self, self.show_in_dock)
        if getattr(self, "_keep_floating", None) is not None:
            return  # shown again after being hidden: the timers are already running
        # Qt resets the native window when it is re-created (screen change, Space switch),
        # so keep re-applying: the call is cheap.
        self._keep_floating = QTimer(self)
        self._keep_floating.timeout.connect(lambda: _configure_native_window(self, self.show_in_dock))
        self._keep_floating.start(2000)
        # The call window may only appear, move or resize after the app was started.
        self._keep_following = QTimer(self)
        self._keep_following.timeout.connect(self._follow_tick)
        self._keep_following.start(3000)
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
        self._menu.addAction("Move to the call window", self._follow_again)
        self._menu.addAction("Reset the position and size", self._reset_position)
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

    def _follow_again(self):
        self._following = True
        self._followed_window = ""
        if not self._follow_tick():
            logger.warning("No call window found (looking for: {})", self.follow_window)
        self._bring_back()

    def _reset_position(self):
        STATE_FILE.unlink(missing_ok=True)
        self.resize(SPLIT_WIDTH if self.show_original else WIDTH, HEIGHT)
        self._following = True
        self._followed_window = ""
        self._place(self.follow_window)
        self._bring_back()

    # --- placement ---------------------------------------------------------

    def _place(self, follow_window: str):
        if self._follow_tick():
            return
        if self._restore_geometry():
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - MARGIN, screen.top() + MARGIN)

    def _follow_tick(self) -> bool:
        """Sit next to the call window and keep up when it appears later, moves or resizes.
        Dragging the window by hand switches this off until the menu bar item switches it back on."""
        if not self._following or not self.follow_window:
            return False
        call = find_window(self.follow_window)
        if not call:
            return False
        target = self._beside(QRect(call["x"], call["y"], call["width"], call["height"]))
        if target != self.geometry():
            self.setGeometry(target)
        if self._followed_window != call["title"]:
            logger.info("Window placed next to: {} — {}", call["owner"], call["title"])
            self._followed_window = call["title"]
        return True

    def _beside(self, call: QRect) -> QRect:
        """Next to the top-right corner of the call window: outside it if there is room,
        otherwise just inside its right edge. Only the position follows; the size stays."""
        screen = QGuiApplication.screenAt(call.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width, height = self.width(), min(self.height(), area.height() - 2 * GAP)
        top = max(area.top() + GAP, min(call.top() + GAP, area.bottom() - height - GAP))
        if area.right() - call.right() >= width + 2 * GAP:
            return QRect(call.right() + GAP, top, width, height)
        return QRect(min(call.right(), area.right()) - width - GAP, top, width, height)

    def _restore_geometry(self) -> bool:
        """Reuse wherever the window was dragged to last time."""
        saved = self._load_state()
        try:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            if screen.contains(saved["x"] + 40, saved["y"] + 40):
                self.move(saved["x"], saved["y"])
                return True
        except KeyError:
            pass
        return False

    def _restore_size(self):
        """The size the window was resized to, kept per layout (one or two columns)."""
        size = self._load_state().get("sizes", {}).get(self._layout_name)
        if size:
            self.resize(size[0], size[1])

    @staticmethod
    def _load_state() -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_geometry(self):
        state = self._load_state()
        rect = self.geometry()
        if not self._following:
            state.update(x=rect.x(), y=rect.y())
        state.setdefault("sizes", {})[self._layout_name] = [rect.width(), rect.height()]
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state))
        except OSError as exc:
            logger.debug("Could not save the window position: {}", exc)

    def eventFilter(self, watched, event):
        if isinstance(watched, QSizeGrip) and event.type() == QEvent.Type.MouseButtonRelease:
            self._save_geometry()
        return False

    # --- look --------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(0, 0, -1, -1), RADIUS, RADIUS)
        painter.fillPath(path, self._theme["background"])
        painter.setPen(QPen(self._theme["border"], 1))
        painter.drawPath(path)
        if self.show_original:
            # The divider between the original and the translation columns.
            x = self.width() // 2
            painter.drawLine(x, self._scroll.y() + 4, x, self.height() - RADIUS)

    def _apply_theme(self, *_):
        self._theme = THEMES["dark" if _is_dark() else "light"]
        for i in range(self._feed.count()):
            row = self._feed.itemAt(i).widget()
            if row is not None:
                self._fill(row, *row.property("entry"))
        self.update()

    # --- feed --------------------------------------------------------------

    def update_entry(self, original: str, done: str, pending: str, final: bool):
        """Thread-safe. `done` sentences will not change any more (white), `pending` is the
        sentence still being spoken (grey). Partial updates replace the current line; a final one commits it."""
        self._bridge.entry.emit(original, done, pending, final)

    def _new_row(self) -> QWidget:
        """One entry: the translation, with the original in a column to its left if shown."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        columns = QHBoxLayout(row)
        columns.setContentsMargins(0, 0, 0, 0)
        # Twice the feed margin, so the gap is centred on the divider drawn in paintEvent.
        columns.setSpacing(24)
        row.labels = []
        for _ in range(2 if self.show_original else 1):
            label = QLabel()
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            columns.addWidget(label, 1)
            row.labels.append(label)
        return row

    def _fill(self, row: QWidget, original: str, done: str, pending: str):
        theme = self._theme
        row.labels[-1].setText(
            f'<div style="font-size:15px;"><span style="color:{theme["done"]};">{html.escape(done)}</span> '
            f'<span style="color:{theme["pending"]};">{html.escape(pending)}</span></div>'
        )
        if self.show_original:
            row.labels[0].setText(
                f'<div style="font-size:15px; color:{theme["original"]};">{html.escape(original)}</div>'
            )

    def _update(self, original: str, done: str, pending: str, final: bool):
        if not (done + pending).strip():
            # The line turned out to be noise: drop whatever partial text was shown.
            if final and self._pending is not None:
                self._pending.deleteLater()
                self._pending = None
            return
        if self._pending is None:
            self._pending = self._new_row()
            self._feed.insertWidget(self._feed.count() - 1, self._pending)

        self._pending.setProperty("entry", (original, done, pending))
        self._fill(self._pending, original, done, pending)
        if final:
            self._pending = None

        # The last item is the stretch; the oldest entry is the first one.
        while self._feed.count() - 1 > self.max_entries:
            item = self._feed.takeAt(0)
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
            # Placed by hand: stop following the call window until asked again.
            self._following = False
            self._save_geometry()


def create_app(show_in_dock: bool = False) -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(True)
    # Must happen before the first window is created: a window born under the wrong
    # policy stays tied to its Space and never shows up over full-screen apps.
    try:
        objc = _ObjC()
        objc.send_long(objc.shared_app(), b"setActivationPolicy:",
                       REGULAR_POLICY if show_in_dock else ACCESSORY_POLICY)
    except Exception as exc:
        logger.warning("Could not set the application mode: {}", exc)
    return app
