import dataclasses
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from config_store import APP_VERSION

if __name__ == "__main__" and "--version" in sys.argv:
    print(APP_VERSION)
    raise SystemExit(0)

import engine  # noqa: E402,F401 - enables the Windows certificate store before Groq loads
import pyperclip  # noqa: E402
import sounddevice as sd  # noqa: E402
from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal  # noqa: E402
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import config_store  # noqa: E402
import updater  # noqa: E402
from audio_player import AudioPlayer, PlaybackStatus  # noqa: E402
from config_store import APP_NAME, Config, load_config, save_config, setup_logging  # noqa: E402
from dictation_core import DEFAULT_DEVICE_LABEL  # noqa: E402
from engine import (  # noqa: E402
    WAVE_BAR_COUNT,
    WAVE_TICK_MS,
    DictationEngine,
    Groq,
    ensure_sounds,
    input_devices,
    resolve_input_device,
    smooth_audio_level,
    stable_input_selector,
)
from history import HistoryEntry, RecordingEntry, RecordingHistory, TranscriptionHistory  # noqa: E402
from hotkeys import HotkeyError, HotkeyListener  # noqa: E402
from microphone_test import MicrophoneTest, MicrophoneTestStatus  # noqa: E402
from settings_ui import MICROPHONE_TEST_KEY, SettingsWindow  # noqa: E402
from ui_theme import app_icon, apply_theme, current_theme, draw_glyph  # noqa: E402
from windows_services import (  # noqa: E402
    acquire_single_instance_lock,
    autostart_enabled,
    release_single_instance_lock,
    set_autostart,
)

BUBBLE_MARGIN = 12  # Room for the soft shadow around the pill.
BUBBLE_BOTTOM_OFFSET = 28  # Distance from the taskbar / bottom of the work area.
RECORD_RED = "#e5484d"
WAVE_FADED_BARS = 4  # The oldest bars fade into the pill background.
SPLASH_MIN_VISIBLE_MS = 900
SPLASH_READY_VISIBLE_MS = 350
SPLASH_TRAY_TIMEOUT_MS = 10_000
NOTICE_VISIBLE_MS = 3000


def bottom_centered_position(width: int, height: int, area: QRect, bottom_offset: int = BUBBLE_BOTTOM_OFFSET) -> tuple[int, int]:
    x = area.x() + max(0, (area.width() - width) // 2)
    y = area.y() + max(0, area.height() - height - bottom_offset)
    return x, y


def process_age_ms() -> float | None:
    """Milliseconds since this app was launched; includes a onefile bootloader."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]

        def created(handle) -> int:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return 0
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

        start = created(kernel32.GetCurrentProcess())
        if updater.install_layout() == "onefile":
            parent = kernel32.OpenProcess(0x1000, False, os.getppid())  # PROCESS_QUERY_LIMITED_INFORMATION
            if parent:
                try:
                    parent_start = created(parent)
                    if parent_start:
                        start = min(start, parent_start)
                finally:
                    kernel32.CloseHandle(parent)
        now = wintypes.FILETIME()
        kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
        current = (now.dwHighDateTime << 32) | now.dwLowDateTime
        return (current - start) / 10_000 if start else None
    except Exception:
        return None


class EngineBridge(QObject):
    """Moves engine callbacks from worker threads to the GUI thread."""

    status = Signal(str)
    state = Signal(str)
    recordings_changed = Signal()
    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(lambda function: function())

    def run_on_main(self, function) -> None:
        self.invoke.emit(function)


def make_non_activating(widget: QWidget) -> None:
    """Never take keyboard focus away from the window you are dictating into."""
    if os.name != "nt":
        return
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_style.restype = ctypes.c_ssize_t
    get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
    set_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    hwnd = ctypes.c_void_p(int(widget.winId()))
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
    style = get_style(hwnd, GWL_EXSTYLE)
    set_style(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)


def is_mouse_activate(message) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
    return msg.message == 0x0021  # WM_MOUSEACTIVATE


class StatusBubble(QWidget):
    """Translucent status pill at the bottom of the screen.

    It is a tool window that never accepts focus (Qt.WindowDoesNotAcceptFocus,
    WS_EX_NOACTIVATE and MA_NOACTIVATE), so showing or clicking it keeps the
    target window active and Ctrl+V lands where you were typing.
    """

    SIZES = {"recording": (196, 48), "processing": (176, 48)}

    def __init__(self, on_click) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.on_click = on_click
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.state = "idle"
        self.message = ""
        self.wave_levels = [0.0] * WAVE_BAR_COUNT
        self.wave_envelope = 0.0
        self.audio_level_provider = lambda: 0.0
        self.recording_started_at = 0.0
        self.spinner_started_at = 0.0
        self.pill_size = QSize(44, 44)
        self.wave_timer = QTimer(self)
        self.wave_timer.setInterval(WAVE_TICK_MS)
        self.wave_timer.timeout.connect(self.wave_tick)
        self.spinner_timer = QTimer(self)
        self.spinner_timer.setInterval(16)
        self.spinner_timer.timeout.connect(self.update)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self.setWindowTitle(f"{APP_NAME} - Klaar")
        self.winId()  # Create the native window now so its styles are in place before the first show.
        make_non_activating(self)

    # -- window --------------------------------------------------------------

    def nativeEvent(self, event_type, message):
        if is_mouse_activate(message):
            return True, 3  # MA_NOACTIVATE
        return super().nativeEvent(event_type, message)

    def resize_pill(self, width: int, height: int) -> None:
        self.pill_size = QSize(width, height)
        self.setFixedSize(width + 2 * BUBBLE_MARGIN, height + 2 * BUBBLE_MARGIN)

    def position(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        x, y = bottom_centered_position(self.width(), self.height(), screen.availableGeometry())
        self.move(x, y)

    def show_bubble(self) -> None:
        self.hide_timer.stop()
        self.position()
        if not self.isVisible():
            self.show()
        self.raise_()

    def schedule_hide(self, delay_ms: int = NOTICE_VISIBLE_MS) -> None:
        self.hide_timer.start(delay_ms)

    def stop_animations(self) -> None:
        self.wave_timer.stop()
        self.spinner_timer.stop()

    # -- states --------------------------------------------------------------

    def set_state(self, state: str, schedule_hide: bool = True) -> None:
        self.state = state
        self.stop_animations()
        titles = {"idle": "Klaar", "recording": "Opname", "processing": "Transcriptie"}
        self.setWindowTitle(f"{APP_NAME} - {titles.get(state, titles['idle'])}")
        if state == "idle":
            if schedule_hide:
                self.hide()
            return
        self.resize_pill(*self.SIZES.get(state, (44, 44)))
        if state == "recording":
            self.start_wave()
        elif state == "processing":
            self.spinner_started_at = time.perf_counter()
            self.spinner_timer.start()
        self.show_bubble()
        self.update()

    def show_notice(self, message: str) -> None:
        self.stop_animations()
        self.state = "notice"
        self.message = message
        font = self._font(10, QFont.Weight.DemiBold)
        width = 58 + QFontMetrics(font).horizontalAdvance(message) + 20
        self.resize_pill(max(200, width), 48)
        self.setWindowTitle(f"{APP_NAME} - {message}")
        self.show_bubble()
        self.update()
        self.schedule_hide()

    def start_wave(self) -> None:
        self.recording_started_at = time.perf_counter()
        self.wave_levels = [0.0] * WAVE_BAR_COUNT
        self.wave_envelope = 0.0
        self.wave_tick()
        self.wave_timer.start()

    def wave_tick(self) -> None:
        if self.state != "recording":
            self.wave_timer.stop()
            return
        self.wave_envelope = smooth_audio_level(self.wave_envelope, self.audio_level_provider())
        self.wave_levels = self.wave_levels[1:] + [self.wave_envelope]
        self.update()

    def elapsed_label(self) -> str:
        elapsed = max(0, int(time.perf_counter() - self.recording_started_at))
        return f"{elapsed // 60:02d}:{elapsed % 60:02d}"

    def bar_heights(self) -> list[float]:
        return [3 + level * 18 for level in self.wave_levels]

    @staticmethod
    def bar_alpha(index: int) -> float:
        return min(1.0, 0.3 + 0.7 * index / WAVE_FADED_BARS)

    # -- input ---------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.on_click()

    # -- painting ------------------------------------------------------------

    @staticmethod
    def _font(points: float, weight=QFont.Weight.Normal) -> QFont:
        font = QFont("Segoe UI")
        font.setPointSizeF(points)
        font.setWeight(weight)
        return font

    def _pill_rect(self) -> QRectF:
        return QRectF(BUBBLE_MARGIN, BUBBLE_MARGIN, self.pill_size.width(), self.pill_size.height())

    def paintEvent(self, _event) -> None:
        if self.state == "idle":
            return
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pill = self._pill_rect()
        radius = pill.height() / 2
        # Soft shadow: stacked translucent outlines, cheaper than a blur effect.
        for step in range(BUBBLE_MARGIN, 0, -2):
            shadow = QColor(0, 0, 0, round(30 * (1 - step / BUBBLE_MARGIN) ** 2) + 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shadow)
            grown = pill.adjusted(-step * 0.6, -step * 0.35, step * 0.6, step * 0.75)
            painter.drawRoundedRect(grown, radius + step * 0.5, radius + step * 0.5)
        fill = QColor("#202624") if theme.dark else QColor("#ffffff")
        fill.setAlpha(246)
        border = QColor(255, 255, 255, 28) if theme.dark else QColor(0, 0, 0, 22)
        painter.setPen(border)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, radius, radius)
        text_color = QColor(theme.text)
        if self.state == "recording":
            self._paint_recording(painter, pill, fill, text_color)
        elif self.state == "processing":
            self._paint_processing(painter, pill, theme, text_color)
        elif self.state == "notice":
            self._paint_notice(painter, pill, theme)

    def _paint_recording(self, painter: QPainter, pill: QRectF, fill: QColor, text_color: QColor) -> None:
        middle = pill.center().y()
        left = pill.left() + 20
        red = QColor(RECORD_RED)
        for index, height in enumerate(self.bar_heights()):
            color = QColor(red)
            color.setAlphaF(self.bar_alpha(index))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            x = left + index * 5
            painter.drawRoundedRect(QRectF(x, middle - height / 2, 2.6, height), 1.3, 1.3)
        painter.setPen(text_color)
        painter.setFont(self._font(10.5, QFont.Weight.DemiBold))
        painter.drawText(QRectF(left + WAVE_BAR_COUNT * 5 + 8, pill.top(), 60, pill.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.elapsed_label())
        stop = QRectF(pill.right() - 40, middle - 15, 30, 30)
        soft = QColor(red)
        soft.setAlpha(38)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(soft)
        painter.drawEllipse(stop)
        draw_glyph(painter, "stop", stop.adjusted(7, 7, -7, -7), red)

    def _paint_processing(self, painter: QPainter, pill: QRectF, theme, text_color: QColor) -> None:
        middle = pill.center().y()
        ring = QRectF(pill.left() + 18, middle - 9, 18, 18)
        track = QColor(theme.accent)
        track.setAlpha(50)
        pen = painter.pen()
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setColor(track)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(ring)
        pen.setColor(QColor(theme.accent_text if theme.dark else theme.accent))
        painter.setPen(pen)
        angle = ((time.perf_counter() - self.spinner_started_at) * 400) % 360
        painter.drawArc(ring, int(-angle * 16), 110 * 16)
        painter.setPen(text_color)
        painter.setFont(self._font(10, QFont.Weight.DemiBold))
        painter.drawText(QRectF(ring.right() + 12, pill.top(), pill.width(), pill.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Transcriberen")

    def _paint_notice(self, painter: QPainter, pill: QRectF, theme) -> None:
        middle = pill.center().y()
        badge = QRectF(pill.left() + 14, middle - 13, 26, 26)
        tone = QColor(theme.danger)
        soft = QColor(tone)
        soft.setAlpha(36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(soft)
        painter.drawEllipse(badge)
        draw_glyph(painter, "warning", badge.adjusted(5, 5, -5, -5), tone)
        painter.setPen(QColor(theme.text))
        painter.setFont(self._font(10, QFont.Weight.DemiBold))
        painter.drawText(QRectF(badge.right() + 10, pill.top(), pill.width() - 60, pill.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.message)

    def destroy_bubble(self) -> None:
        self.stop_animations()
        self.hide_timer.stop()
        self.hide()
        self.deleteLater()


class StartupSplash(QWidget):
    width_px = 440
    height_px = 196

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(APP_NAME)
        self.destroyed_flag = False
        self.started_at = time.monotonic()
        self.setFixedSize(self.width_px, self.height_px)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        brand = QHBoxLayout()
        brand.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(QSize(40, 40)))
        brand.addWidget(logo)
        title = QLabel("Groq Windows Dictation")
        title.setObjectName("cardTitle")
        title.setStyleSheet("font-size: 14pt;")
        brand.addWidget(title, 1)
        layout.addLayout(brand)
        self.status = QLabel("Wordt geladen in het systeemvak...")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        hint = QLabel("Daarna blijft de app beschikbaar via het icoon rechtsonder.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.x() + (area.width() - self.width()) // 2, area.y() + (area.height() - self.height()) // 2)
        self.winId()
        make_non_activating(self)
        self.show()
        QApplication.processEvents()

    def minimum_time_has_elapsed(self) -> bool:
        return (time.monotonic() - self.started_at) * 1000 >= SPLASH_MIN_VISIBLE_MS

    def show_ready(self) -> None:
        if self.destroyed_flag:
            return
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.status.setText("Klaar — actief in het systeemvak")

    def destroy_splash(self) -> None:
        if self.destroyed_flag:
            return
        self.destroyed_flag = True
        self.hide()
        self.deleteLater()


def message(kind: str, text: str, parent=None) -> None:
    icon = {"info": QMessageBox.Icon.Information, "error": QMessageBox.Icon.Warning}[kind]
    box = QMessageBox(icon, APP_NAME, text, parent=parent)
    box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
    box.setWindowIcon(app_icon())
    box.exec()


class UpdateDialog(QDialog):
    def __init__(self, app: "TrayApp", update: updater.UpdateInfo) -> None:
        super().__init__(None)
        self.app = app
        self.update_info = update
        self.setWindowTitle("Nieuwe versie beschikbaar")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)
        title = QLabel(f"Er is een nieuwe versie beschikbaar: {update.tag}")
        title.setObjectName("cardTitle")
        title.setStyleSheet("font-size: 13pt;")
        layout.addWidget(title)
        body = QLabel(
            f"Je gebruikt nu v{APP_VERSION}. Klik op Update om de nieuwe versie te downloaden, "
            "de app te vervangen en opnieuw te starten. Je Groq API key, instellingen en geschiedenis blijven behouden."
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        later = QPushButton("Later")
        later.clicked.connect(self.close)
        self.update_button = QPushButton(f"Update naar {update.tag}")
        self.update_button.setProperty("variant", "primary")
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(self.start_update)
        buttons.addWidget(later)
        buttons.addWidget(self.update_button)
        layout.addLayout(buttons)

    def start_update(self) -> None:
        self.update_button.setEnabled(False)
        self.status.setText("Downloaden en controleren...")
        update = self.update_info

        def run() -> None:
            try:
                staged = updater.download_update(update)
                updater.launch_update_script(staged, update.kind)
            except Exception as exc:
                logging.exception("Update failed")
                text = f"Update mislukt:\n{exc}"

                def failed() -> None:
                    try:
                        self.update_button.setEnabled(True)
                        self.status.setText("Update mislukt.")
                    except RuntimeError:
                        pass
                    message("error", text)

                self.app.bridge.run_on_main(failed)
                return
            self.app.bridge.run_on_main(self.app.quit)

        threading.Thread(target=run, name="update-download", daemon=True).start()


class TrayApp:
    app_name = APP_NAME
    app_version = APP_VERSION

    def __init__(self) -> None:
        setup_logging()
        self.qt = QApplication.instance() or QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)
        self.qt.setApplicationName(APP_NAME)
        self.qt.setApplicationVersion(APP_VERSION)
        self.qt.setWindowIcon(app_icon())
        apply_theme(self.qt)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.on_color_scheme_changed)
        self.bridge = EngineBridge()
        self.splash = StartupSplash()
        self.fatal_startup_error: Exception | None = None
        self.startup_finished = False
        self.startup_timer = QTimer()
        self.startup_timer.setInterval(25)
        self.startup_timer.timeout.connect(self._poll_startup_ready)

        try:
            ensure_sounds()
            self.config = load_config()
            self.config = dataclasses.replace(self.config, input_device=stable_input_selector(self.config.input_device))
            self.bubble = StatusBubble(self.on_bubble_click)
            self.history = TranscriptionHistory(config_store.HISTORY_PATH)
            self.recordings = RecordingHistory(config_store.RECORDINGS_DIR)
            self.player = AudioPlayer(sd)
            self.bridge.status.connect(self.set_status)
            self.bridge.state.connect(self.set_engine_state)
            self.bridge.recordings_changed.connect(self._refresh_history_view)
            self.engine = DictationEngine(
                self.config, self.bridge.status.emit, self.bridge.state.emit, self.on_transcript,
                self.recordings, self.bridge.recordings_changed.emit,
            )
            self.bubble.audio_level_provider = self.engine.get_audio_level
            self.microphone_test: MicrophoneTest | None = None
            self.hotkeys = HotkeyListener(self.engine.on_shortcut)
            self.hotkey_error: str | None = None
            self.settings_window: SettingsWindow | None = None
            self.update_window: UpdateDialog | None = None
            self.tray = QSystemTrayIcon(app_icon())
            self.tray.setToolTip(f"{APP_NAME} v{APP_VERSION}")
            self.tray.activated.connect(self.on_tray_activated)
            self.menu = QMenu()
            for text, action in (
                ("Instellingen", self.open_settings),
                ("Geschiedenis", lambda: self.open_settings("history")),
                (None, None),
                ("Controleren op updates", self.check_for_updates_manual),
                ("Logbestand openen", self.open_log),
                ("App herstarten", self.restart),
                (None, None),
                ("Afsluiten", self.quit),
            ):
                if text is None:
                    self.menu.addSeparator()
                else:
                    self.menu.addAction(text, action)
            self.tray.setContextMenu(self.menu)
        except Exception:
            self.splash.destroy_splash()
            raise

    # -- engine adapter (GUI thread) -----------------------------------------

    def set_status(self, text: str) -> None:
        self.tray.setToolTip(f"{APP_NAME} - {text[:80]}")

    def set_engine_state(self, state: str) -> None:
        if state == "too_short":
            self.bubble.show_notice("Transcriptie te kort")
        else:
            if state == "recording":
                self.player.stop()  # Never mix playback into a dictation.
            self.bubble.set_state(state)
        self._refresh_history_view()

    def on_transcript(self, text: str) -> None:
        """Runs on the transcription worker; store first, then refresh any open window."""
        self.history.add(text)
        self.bridge.recordings_changed.emit()

    def on_bubble_click(self) -> None:
        """The floating bubble stops a running recording; otherwise it opens settings."""
        state = self.engine.state
        if state == "recording":
            self.engine.on_shortcut()
        elif state == "idle":
            self.open_settings()

    def on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_settings()

    def on_color_scheme_changed(self, *_args) -> None:
        apply_theme(self.qt)
        for widget in self.qt.topLevelWidgets():
            widget.update()

    def install_hotkey(self) -> None:
        self.hotkeys.set_hotkey(self.config.shortcut)
        self.hotkey_error = None

    def remove_hotkey(self) -> None:
        try:
            self.hotkeys.clear()
        except Exception as exc:
            logging.warning("Could not release hotkey: %s", exc)

    # -- SettingsController protocol -----------------------------------------

    @property
    def app_dir(self) -> str:
        return str(config_store.APP_DIR)

    def _refresh_inputs_locked(self) -> None:
        # PortAudio enumerates once at initialization. Reset it only while no
        # dictation/test/playback stream owns it; callers hold both engine locks.
        if self.engine.state != "idle":
            raise RuntimeError("Stop eerst de opname of microfoontest voordat je apparaten vernieuwt.")
        self.player.release()
        sd._terminate()
        sd._initialize()
        self.engine._resolve_device(self.config)

    def list_input_devices(self, *, refresh: bool = False) -> list[tuple[str, str]]:
        try:
            with self.engine.stream_transition_lock, self.engine.lock:
                if refresh or self.engine.state == "idle":
                    self._refresh_inputs_locked()
                return input_devices()
        except Exception as exc:
            if refresh:
                raise
            logging.warning("Could not list input devices: %s", exc)
            return [("", f"{DEFAULT_DEVICE_LABEL} (apparaat niet beschikbaar)")]

    def start_microphone_test(self, selector: str) -> None:
        self.stop_microphone_test()
        with self.engine.stream_transition_lock, self.engine.lock:
            self._refresh_inputs_locked()
            device = resolve_input_device(selector)
            self.engine.state = "testing"

            def finished():
                with self.engine.lock:
                    if self.engine.state == "testing":
                        self.engine.state = "idle"

            self.microphone_test = MicrophoneTest(sd, device, wasapi=selector.startswith("wasapi:"), finished=finished)
            self.microphone_test.start()

    def microphone_test_status(self) -> MicrophoneTestStatus:
        return self.microphone_test.snapshot() if self.microphone_test else MicrophoneTestStatus()

    def play_microphone_test(self) -> float:
        if self.microphone_test is None:
            raise RuntimeError("Maak eerst een testopname.")
        if self.recording_busy():
            raise RuntimeError("Wacht tot de opname klaar is.")
        return self.player.play(MICROPHONE_TEST_KEY, self.microphone_test.playback_path())

    def stop_microphone_test(self) -> None:
        if self.player.status().key == MICROPHONE_TEST_KEY:
            self.player.release()
        if self.microphone_test is not None:
            self.microphone_test.close()
            self.microphone_test = None

    def autostart_enabled(self) -> bool:
        return autostart_enabled()

    def suspend_hotkey(self) -> None:
        self.remove_hotkey()

    def resume_hotkey(self) -> None:
        self.install_hotkey()

    def apply_settings(self, new_config: Config) -> None:
        previous = self.config
        if new_config.input_device != previous.input_device:
            # Validate before writing, so a device that cannot be resolved is
            # never stored while the user is told saving failed.
            resolve_input_device(new_config.input_device)
        save_config(new_config)
        set_autostart(new_config.autostart)
        self.engine.update_config(new_config)
        self.config = new_config
        try:
            self.install_hotkey()
        except HotkeyError:
            # Keep everything else, but fall back to the shortcut that worked.
            self.config = dataclasses.replace(new_config, shortcut=previous.shortcut)
            save_config(self.config)
            try:
                self.install_hotkey()
            except Exception as exc:
                logging.warning("Could not restore previous hotkey: %s", exc)
            raise

    def _refresh_history_view(self) -> None:
        if self.settings_window is not None:
            self.settings_window.refresh_history()

    def history_entries(self) -> tuple[HistoryEntry, ...]:
        return self.history.entries

    def recording_entries(self) -> tuple[RecordingEntry, ...]:
        return self.recordings.entries

    def recording_busy(self) -> bool:
        with self.engine.lock:
            return self.engine.state != "idle"

    def retry_recording(self, recording_id: str) -> None:
        self.engine.retry_recording(recording_id)

    def play_recording(self, recording_id: str, offset: float = 0.0) -> float:
        entry = self.recordings.get(recording_id)
        return self.player.play(recording_id, self.recordings.audio_path(entry), offset)

    def stop_playback(self) -> None:
        self.player.stop()

    def playback_status(self) -> PlaybackStatus:
        return self.player.status()

    def copy_text(self, text: str) -> None:
        pyperclip.copy(text)

    def clear_history(self) -> None:
        with self.engine.lock:
            if self.engine.state != "idle":
                raise RuntimeError("Wacht tot de huidige opname of transcriptie klaar is.")
            self.player.release()
            self.recordings.clear()
            self.history.clear()

    def test_api_key(self, api_key: str) -> str:
        client = Groq(api_key=api_key)
        models = client.models.list()
        names = [str(getattr(model, "id", "")) for model in getattr(models, "data", [])]
        whisper = sorted(name for name in names if "whisper" in name.lower())
        if not whisper:
            return "Verbonden met Groq, maar er is geen Whisper-model beschikbaar voor deze sleutel."
        return f"Verbonden. Beschikbare spraakmodellen: {', '.join(whisper)}."

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> None:
        # A startup save migrates a non-empty legacy/.env key to Credential
        # Manager, but must never interpret a temporary keyring read failure as
        # an explicit request to delete an existing secret.
        try:
            save_config(self.config)
            set_autostart(self.config.autostart)
            try:
                self.install_hotkey()
            except HotkeyError as exc:
                # Start anyway so the user can pick another shortcut in Settings.
                self.hotkey_error = str(exc)
                logging.warning("Hotkey unavailable at startup: %s", exc)
            self.tray.show()
            self.startup_timer.start()
        except Exception:
            self._cleanup_failed_startup()
            raise
        self.qt.exec()
        if self.fatal_startup_error is not None:
            raise self.fatal_startup_error

    def tray_ready(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible()

    def _poll_startup_ready(self) -> None:
        if self.startup_finished:
            self.startup_timer.stop()
            return
        elapsed_ms = (time.monotonic() - self.splash.started_at) * 1000
        ready = self.tray_ready()
        if not ready and elapsed_ms >= SPLASH_TRAY_TIMEOUT_MS:
            self.startup_timer.stop()
            self._fail_startup(RuntimeError("Het systeemvak kon niet op tijd worden gestart."))
            return
        if ready and self.splash.minimum_time_has_elapsed():
            self.startup_timer.stop()
            self.splash.show_ready()
            QTimer.singleShot(SPLASH_READY_VISIBLE_MS, self._finish_startup)

    def _fail_startup(self, error: Exception) -> None:
        self.fatal_startup_error = error
        self._cleanup_failed_startup()

    def _cleanup_failed_startup(self) -> None:
        self.startup_finished = True
        self.splash.destroy_splash()
        self.remove_hotkey()
        try:
            self.tray.hide()
        except Exception:
            pass
        self.qt.quit()

    def _finish_startup(self) -> None:
        if self.startup_finished:
            return
        self.startup_finished = True
        self.splash.destroy_splash()
        age = process_age_ms()
        logging.info(
            "Startup ready: version %s, layout %s%s", APP_VERSION, updater.install_layout(),
            f", {age:.0f} ms after launch" if age is not None else "",
        )

        if not self.config.api_key or "--settings" in sys.argv or self.hotkey_error:
            QTimer.singleShot(250, self.open_settings)
        if self.hotkey_error:
            error = self.hotkey_error
            QTimer.singleShot(400, lambda: message("error", error))
        QTimer.singleShot(2500, self.check_for_updates_auto)
        QTimer.singleShot(5000, updater.confirm_startup_and_cleanup)

    def check_for_updates_auto(self) -> None:
        if not getattr(sys, "frozen", False) or config_store.profile_name():
            return
        self.check_for_updates(show_no_update=False)

    def check_for_updates_manual(self) -> None:
        self.check_for_updates(show_no_update=True)

    def check_for_updates(self, show_no_update: bool) -> None:
        def run() -> None:
            try:
                update = updater.fetch_latest_update()
            except Exception as exc:
                logging.warning("Update check failed: %s", exc)
                if show_no_update:
                    # Bind the message now: `exc` is unbound once the except block ends.
                    text = f"Update-check mislukt:\n{exc}"
                    self.bridge.run_on_main(lambda: message("error", text))
                return

            if update is None:
                if show_no_update:
                    self.bridge.run_on_main(lambda: message("info", f"Je gebruikt de nieuwste versie: v{APP_VERSION}."))
                return

            self.bridge.run_on_main(lambda: self.show_update_window(update))

        threading.Thread(target=run, name="update-check", daemon=True).start()

    def show_update_window(self, update: updater.UpdateInfo) -> None:
        if self.update_window is not None:
            try:
                self.update_window.raise_()
                self.update_window.activateWindow()
                return
            except RuntimeError:
                self.update_window = None
        self.update_window = UpdateDialog(self, update)
        self.update_window.destroyed.connect(lambda *_: setattr(self, "update_window", None))
        self.update_window.show()

    def open_settings(self, page: str | None = None) -> None:
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
            self.settings_window.closed.connect(self._settings_closed)
            self.settings_window.show()
        else:
            self.settings_window.showNormal()
        self.settings_window.raise_()
        self.settings_window.activateWindow()
        if page:
            self.settings_window.select_page(page)

    def _settings_closed(self) -> None:
        self.settings_window = None

    def open_log(self) -> None:
        config_store.LOG_PATH.touch(exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_store.LOG_PATH))):
            message("error", f"Logbestand kon niet worden geopend:\n{config_store.LOG_PATH}")

    def restart(self) -> None:
        env = os.environ.copy()
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Hand over what only one process can own before the successor starts:
        # it checks the single-instance claim right away (and would otherwise
        # exit silently) and registers the same global shortcut.
        self.remove_hotkey()
        self.hotkeys.stop()
        release_single_instance_lock()
        try:
            if getattr(sys, "frozen", False):
                executable = Path(sys.executable)
                subprocess.Popen([str(executable)], cwd=str(executable.parent), env=env, creationflags=creation_flags)
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                launcher = pythonw if pythonw.exists() else Path(sys.executable)
                subprocess.Popen([str(launcher), str(Path(__file__).resolve())], cwd=str(Path(__file__).parent), env=env, creationflags=creation_flags)
        except Exception as exc:
            acquire_single_instance_lock()
            try:
                self.install_hotkey()
            except Exception as hotkey_exc:
                logging.warning("Could not restore hotkey after a failed restart: %s", hotkey_exc)
            message("error", f"App kon niet worden herstart:\n{exc}")
            return

        self.quit()

    def quit(self) -> None:
        self.startup_finished = True
        self.splash.destroy_splash()
        if self.settings_window is not None:
            self.settings_window.dirty = False
            self.settings_window.close()
        self.hotkeys.stop()
        self.stop_microphone_test()
        self.player.release()
        self.engine.shutdown()
        self.bubble.destroy_bubble()
        try:
            self.tray.hide()
        except Exception:
            pass
        self.qt.quit()


def main() -> None:
    if not acquire_single_instance_lock():
        return

    try:
        TrayApp().run()
    except Exception as exc:
        setup_logging()
        logging.exception("Fatal startup error")
        try:
            QApplication.instance() or QApplication(sys.argv)
            message("error", f"Startfout:\n{exc}\n\nLog: {config_store.LOG_PATH}")
        except Exception:
            pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
