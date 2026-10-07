"""Settings window for Groq Insert Dictation (Qt Widgets).

The window talks to the running application through a small controller
object (see :class:`SettingsController`) instead of importing ``app``, which
keeps it testable without a microphone, tray icon or Groq credentials.
Controller calls happen on the GUI thread; slow work (the Groq connection
test) runs on a worker thread and reports back through a Qt signal.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Protocol

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dictation_core import (
    DEFAULT_DEVICE_LABEL,
    MAX_CUSTOM_WORDS,
    MAX_WORD_REPLACEMENTS,
    DictionaryValidationError,
    compose_transcription_prompt,
    normalize_custom_word,
    normalize_custom_words,
    normalize_replacement_part,
    normalize_word_replacements,
)
from history import MAX_HISTORY_ENTRIES, MAX_RECORDING_ENTRIES, HistoryEntry, RecordingEntry
from hotkeys import HotkeyError, hotkey_from_key_press, normalize_hotkey_text, validate_hotkey
from microphone_test import MicrophoneTestStatus
from ui_theme import app_icon, current_theme, draw_glyph, icon, repolish

__all__ = ["DEFAULT_DEVICE_LABEL", "SettingsController", "SettingsWindow"]

MODEL_OPTIONS = (
    ("whisper-large-v3-turbo", "Turbo: snelste antwoord, uitstekend voor dagelijks dicteren."),
    ("whisper-large-v3", "V3: iets trager, hoogste nauwkeurigheid bij moeilijke audio."),
)
LANGUAGE_OPTIONS = (
    ("nl", "Nederlands"),
    ("en", "Engels"),
    ("de", "Duits"),
    ("fr", "Frans"),
    ("es", "Spaans"),
    ("", "Automatisch herkennen"),
)
MICROPHONE_TEST_KEY = "microphone-test"
SHORTCUT_TIP = "Tip: alt+z, ctrl+shift+space of een losse toets zoals insert of f9."
STATUS_LABELS = {
    "saved": ("Bewaard", "muted"),
    "processing": ("Transcriberen…", ""),
    "done": ("Getranscribeerd", ""),
    "failed": ("Mislukt", "danger"),
}


# --------------------------------------------------------------------------
# Controller protocol
# --------------------------------------------------------------------------


class SettingsController(Protocol):
    """What the settings window needs from the running application."""

    app_name: str
    app_version: str
    app_dir: str
    config: object

    def list_input_devices(self, *, refresh: bool = False) -> list[tuple[str, str]]: ...
    def start_microphone_test(self, selector: str) -> None: ...
    def microphone_test_status(self) -> MicrophoneTestStatus: ...
    def play_microphone_test(self) -> float: ...
    def stop_microphone_test(self) -> None: ...
    def autostart_enabled(self) -> bool: ...
    def apply_settings(self, new_config) -> None: ...
    def suspend_hotkey(self) -> None: ...
    def resume_hotkey(self) -> None: ...
    def test_api_key(self, api_key: str) -> str: ...
    def history_entries(self) -> tuple[HistoryEntry, ...]: ...
    def recording_entries(self) -> tuple[RecordingEntry, ...]: ...
    def recording_busy(self) -> bool: ...
    def retry_recording(self, recording_id: str) -> None: ...
    def play_recording(self, recording_id: str, offset: float = 0.0) -> float: ...
    def stop_playback(self) -> None: ...
    def playback_status(self): ...
    def copy_text(self, text: str) -> None: ...
    def clear_history(self) -> None: ...
    def check_for_updates_manual(self) -> None: ...
    def open_log(self) -> None: ...
    def restart(self) -> None: ...


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


def format_clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def label(text: str = "", name: str | None = None, *, wrap: bool = False, selectable: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button(text: str, *, variant: str | None = None, glyph: str | None = None, role: str = "text") -> QPushButton:
    widget = QPushButton(text)
    if variant:
        widget.setProperty("variant", variant)
    if glyph:
        icon_role = {"primary": "on_accent", "ghost": "accent_text"}.get(variant or "", role)
        widget.setIcon(icon(glyph, icon_role, icon_role))
        widget.setIconSize(QSize(16, 16))
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    # Focus rings only for keyboard users: a mouse click does not leave one behind.
    widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return widget


def hbox(*widgets, spacing: int = 8, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        elif isinstance(widget, int):
            layout.addSpacing(widget)
        else:
            layout.addWidget(widget)
    return layout


class ComboBox(QComboBox):
    """Styled combo box that paints its own crisp chevron."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        theme = current_theme()
        color = QColor(theme.muted if self.isEnabled() else theme.field_border)
        size = 16
        draw_glyph(painter, "chevron", QRectF(self.width() - size - 10, (self.height() - size) / 2, size, size), color)


class Card(QFrame):
    """Rounded section with a title, optional description and a body layout."""

    def __init__(self, title: str, description: str | None = None) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(4)
        self.title = label(title, "cardTitle", wrap=True)
        layout.addWidget(self.title)
        self.description = None
        if description:
            self.description = label(description, "cardDescription", wrap=True)
            layout.addWidget(self.description)
        layout.addSpacing(8)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        layout.addLayout(self.body, 1)


class ToggleSwitch(QAbstractButton):
    """A Windows 11 style switch with its label, drawn at the native scale."""

    TRACK_W = 40
    TRACK_H = 20
    GAP = 12

    def __init__(self, text: str, checked: bool = False) -> None:
        super().__init__()
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._offset = 1.0 if checked else 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(120)
        self._animation.valueChanged.connect(self._set_offset)
        self.toggled.connect(self._animate)

    def _set_offset(self, value) -> None:
        self._offset = float(value)
        self.update()

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(self.TRACK_W + self.GAP + metrics.horizontalAdvance(self.text()) + 6, max(30, metrics.height() + 10))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = (self.height() - self.TRACK_H) / 2
        track = QRectF(1, top, self.TRACK_W, self.TRACK_H)
        on = QColor(theme.accent)
        off = QColor(theme.field_border)
        mix = self._offset
        color = QColor(
            round(off.red() + (on.red() - off.red()) * mix),
            round(off.green() + (on.green() - off.green()) * mix),
            round(off.blue() + (on.blue() - off.blue()) * mix),
        )
        if not self.isEnabled():
            color.setAlpha(110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        if self.hasFocus():
            painter.setPen(QColor(theme.field_focus))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(-2.5, -2.5, 2.5, 2.5), self.TRACK_H / 2 + 2.5, self.TRACK_H / 2 + 2.5)
        knob = self.TRACK_H - 6
        x = track.left() + 3 + (self.TRACK_W - knob - 6) * mix
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(x, top + 3, knob, knob))
        painter.setPen(QColor(theme.text if self.isEnabled() else theme.muted))
        text_rect = self.rect().adjusted(self.TRACK_W + self.GAP, 0, 0, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class RecordingPlayer(QWidget):
    """Compact audio player: play/pause button, seekable progress bar and time.

    The widget only draws and reports input; the settings window owns the
    playback state and calls :meth:`render` as the position changes.
    """

    toggled = Signal()
    seek_requested = Signal(float)

    BUTTON = 30

    def __init__(self, duration: float, parent=None) -> None:
        super().__init__(parent)
        self.duration = max(0.0, duration)
        self.position = 0.0
        self.playing = False
        self.dragging = False
        self.setMinimumWidth(200)
        self.setFixedHeight(34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Opname afspelen")

    def _time_text(self) -> str:
        return f"{format_clock(self.position)} / {format_clock(self.duration + 0.5)}"

    def _time_width(self) -> int:
        sample = f"{format_clock(self.duration + 0.5)} / {format_clock(self.duration + 0.5)}".replace("1", "0")
        return QFontMetrics(self.font()).horizontalAdvance(sample) + 4

    def _track_bounds(self) -> tuple[float, float]:
        start = self.BUTTON + 14
        end = max(start + 20, self.width() - self._time_width() - 14)
        return start, end

    def render(self, position: float, playing: bool) -> None:
        self.position = min(max(0.0, position), self.duration)
        self.playing = playing
        self.update()

    def paintEvent(self, _event) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        middle = self.height() / 2
        circle = QRectF(0, middle - self.BUTTON / 2, self.BUTTON, self.BUTTON)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.accent))
        painter.drawEllipse(circle)
        draw_glyph(painter, "pause" if self.playing else "play", circle.adjusted(7, 7, -7, -7), QColor(theme.on_accent))
        start, end = self._track_bounds()
        fraction = self.position / self.duration if self.duration else 0.0
        current = start + (end - start) * fraction
        painter.setBrush(QColor(theme.field_border))
        painter.drawRoundedRect(QRectF(start, middle - 2, end - start, 4), 2, 2)
        if current > start:
            painter.setBrush(QColor(theme.accent))
            painter.drawRoundedRect(QRectF(start, middle - 2, current - start, 4), 2, 2)
        painter.setBrush(QColor(theme.accent))
        painter.drawEllipse(QRectF(current - 6, middle - 6, 12, 12))
        if self.hasFocus():
            painter.setPen(QColor(theme.field_focus))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(circle.adjusted(-2, -2, 2, 2))
        painter.setPen(QColor(theme.muted))
        painter.drawText(
            QRectF(self.width() - self._time_width(), 0, self._time_width(), self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self._time_text(),
        )

    def _fraction(self, x: float) -> float:
        start, end = self._track_bounds()
        return min(1.0, max(0.0, (x - start) / max(1.0, end - start)))

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        if x <= self.BUTTON + 4:
            self.toggled.emit()
            return
        self.dragging = True
        self.render(self._fraction(x) * self.duration, self.playing)

    def mouseMoveEvent(self, event) -> None:
        if self.dragging:
            self.render(self._fraction(event.position().x()) * self.duration, self.playing)

    def mouseReleaseEvent(self, event) -> None:
        if self.dragging:
            self.dragging = False
            self.seek_requested.emit(self._fraction(event.position().x()))

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggled.emit()
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.duration:
            step = -5.0 if event.key() == Qt.Key.Key_Left else 5.0
            self.seek_requested.emit(min(1.0, max(0.0, (self.position + step) / self.duration)))
        else:
            super().keyPressEvent(event)


class ListEditor(QWidget):
    """List + counter + remove button, backed by a Python list."""

    def __init__(self, items: list, render, counter_text, on_change) -> None:
        super().__init__()
        self.items = items
        self.render = render
        self.counter_text = counter_text
        self.on_change = on_change
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.listbox = QListWidget()
        self.listbox.setMinimumHeight(140)
        self.listbox.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.listbox, 1)
        self.counter = label("", "muted")
        self.remove_button = button("Verwijderen", variant="ghost", glyph="trash")
        self.remove_button.clicked.connect(self.remove_selected)
        layout.addLayout(hbox(self.counter, None, self.remove_button))
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.listbox, self.remove_selected, context=Qt.ShortcutContext.WidgetShortcut)
        self.refresh()

    def refresh(self, select_index: int | None = None) -> None:
        self.listbox.clear()
        for item in self.items:
            self.listbox.addItem(self.render(item))
        if select_index is not None and self.items:
            self.listbox.setCurrentRow(min(select_index, len(self.items) - 1))
        self.counter.setText(self.counter_text(len(self.items)))
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.remove_button.setEnabled(bool(self.items) and self.listbox.currentRow() >= 0)

    def append(self, item) -> None:
        self.items.append(item)
        self.refresh(len(self.items) - 1)
        self.on_change()

    def remove_selected(self) -> None:
        index = self.listbox.currentRow()
        if index < 0 or index >= len(self.items):
            return
        del self.items[index]
        self.refresh(index)
        self.on_change()


QT_KEY_NAMES = {
    Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end", Qt.Key.Key_PageUp: "page up", Qt.Key.Key_PageDown: "page down",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
    Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backtab: "tab", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Pause: "pause", Qt.Key.Key_Print: "print screen", Qt.Key.Key_ScrollLock: "scroll lock",
    Qt.Key.Key_CapsLock: "caps lock", Qt.Key.Key_NumLock: "num lock", Qt.Key.Key_Menu: "apps",
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Control: "ctrl", Qt.Key.Key_Shift: "shift", Qt.Key.Key_Alt: "alt", Qt.Key.Key_Meta: "windows",
    Qt.Key.Key_AltGr: "alt", Qt.Key.Key_Super_L: "windows", Qt.Key.Key_Super_R: "windows",
}


def qt_key_name(key: int, keypad: bool = False) -> str:
    try:
        enum = Qt.Key(key)
    except ValueError:
        enum = None
    if enum in QT_KEY_NAMES:
        return QT_KEY_NAMES[enum]
    if Qt.Key.Key_F1.value <= key <= Qt.Key.Key_F24.value:
        return f"f{key - Qt.Key.Key_F1.value + 1}"
    if 0x20 < key < 0x7F:
        character = chr(key).lower()
        return f"kp {character}" if keypad and character.isdigit() else character
    return ""


def hotkey_from_qt_event(event: QKeyEvent) -> str | None:
    modifiers = event.modifiers()
    return hotkey_from_key_press(
        key_name=qt_key_name(event.key(), bool(modifiers & Qt.KeyboardModifier.KeypadModifier)),
        virtual_key=int(event.nativeVirtualKey() or 0),
        ctrl=bool(modifiers & Qt.KeyboardModifier.ControlModifier),
        alt=bool(modifiers & Qt.KeyboardModifier.AltModifier),
        shift=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
        windows=bool(modifiers & Qt.KeyboardModifier.MetaModifier),
    )


def ask(parent, title: str, question: str, confirm: str, cancel: str = "Annuleren") -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, question, parent=parent)
    yes = box.addButton(confirm, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(cancel, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(yes)
    box.exec()
    return box.clickedButton() is yes


def show_error(parent, title: str, message: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=parent)
    box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


# --------------------------------------------------------------------------
# The settings window
# --------------------------------------------------------------------------


class SettingsWindow(QWidget):
    PAGES = (
        ("dictate", "Dicteren", "Jouw stem, direct op de juiste plek.", "mic"),
        ("history", "Geschiedenis", f"Je laatste {MAX_RECORDING_ENTRIES} opnames en {MAX_HISTORY_ENTRIES} transcripties.", "history"),
        ("recognition", "Herkenning", "Help Groq jouw taal en context beter te begrijpen.", "sparkles"),
        ("dictionary", "Woordenboek", "Eigen namen, vaktermen en vaste correcties.", "book"),
        ("connection", "Verbinding", "Je Groq API key en het transcriptiemodel.", "key"),
        ("about", "Over", "Versie, updates en hulpmiddelen.", "info"),
    )
    WIDTH = 980
    HEIGHT = 780
    MIN_WIDTH = 900
    MIN_HEIGHT = 600

    closed = Signal()
    _connection_result = Signal(str, bool)

    def __init__(self, controller: SettingsController, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.controller = controller
        self.original = controller.config
        self.setObjectName("settingsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{controller.app_name} instellingen")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)

        config = self.original
        self.dirty = False
        self.capturing = False
        self.disposed = False
        self.api_key_visible = False
        self.current_page = ""
        self.playback_positions: dict[str, float] = {}
        self.players: dict[str, RecordingPlayer] = {}
        self.microphone_test_active = False
        self.microphone_test_used = False
        self._loading = True
        self.custom_words: list[str] = list(config.custom_words)
        self.word_replacements: list[tuple[str, str]] = list(config.word_replacements)

        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(50)
        self.playback_timer.timeout.connect(self._playback_tick)
        self.microphone_timer = QTimer(self)
        self.microphone_timer.setInterval(50)
        self.microphone_timer.timeout.connect(self._microphone_tick)
        self.microphone_playback_timer = QTimer(self)
        self.microphone_playback_timer.setInterval(100)
        self.microphone_playback_timer.timeout.connect(self._microphone_playback_tick)
        self._connection_result.connect(self._show_connection_result)

        self._build_shell()
        self._build_pages()
        self._load_devices(controller.list_input_devices(), config.input_device)
        self._loading = False
        self.select_page("dictate" if config.api_key else "connection")
        self._place_on_screen()
        self.stack.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.stack.setFocus()

        QShortcut(QKeySequence.StandardKey.Save, self, self._save_shortcut)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._escape_shortcut)
        QApplication.instance().installEventFilter(self)

    # -- layout --------------------------------------------------------------

    def _place_on_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        width = min(self.WIDTH, max(self.MIN_WIDTH, area.width() - 40))
        height = min(self.HEIGHT, max(self.MIN_HEIGHT, area.height() - 60))
        self.resize(width, height)
        self.move(area.x() + (area.width() - width) // 2, area.y() + max(0, (area.height() - height) // 2 - 20))

    def _build_shell(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 22, 18, 18)
        side.setSpacing(4)
        brand_icon = QLabel()
        brand_icon.setPixmap(app_icon().pixmap(QSize(36, 36)))
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(label("Groq Dictation", "brandTitle"))
        brand_text.addWidget(label("Van stem naar tekst", "brandSubtitle"))
        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(brand_icon)
        brand.addLayout(brand_text, 1)
        side.addLayout(brand)
        side.addSpacing(22)
        self.navigation = QVBoxLayout()
        self.navigation.setSpacing(3)
        side.addLayout(self.navigation)
        side.addStretch(1)
        side.addWidget(label(f"Versie {self.controller.app_version}", "sideVersion"))
        root.addWidget(sidebar)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        header = QVBoxLayout()
        header.setContentsMargins(34, 26, 34, 14)
        header.setSpacing(2)
        self.page_title = label("", "pageTitle")
        self.page_subtitle = label("", "pageSubtitle", wrap=True)
        header.addWidget(self.page_title)
        header.addWidget(self.page_subtitle)
        main.addLayout(header)
        self.stack = QStackedWidget()
        self.stack.setObjectName("pageHost")
        main.addWidget(self.stack, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(34, 12, 34, 14)
        footer_layout.setSpacing(8)
        self.status_label = label("Wijzigingen worden bewaard zodra je op Opslaan klikt.", "status", wrap=True)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        footer_layout.addWidget(self.status_label, 1)
        self.cancel_button = button("Annuleren")
        self.cancel_button.clicked.connect(self.cancel)
        self.save_button = button("Opslaan", variant="primary", glyph="check")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        footer_layout.addWidget(self.cancel_button)
        footer_layout.addWidget(self.save_button)
        main.addWidget(footer)
        root.addLayout(main, 1)

    def _page(self, scroll: bool = True) -> tuple[QWidget, QVBoxLayout]:
        body = QWidget()
        body.setObjectName("pageBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(34, 4, 34, 22)
        layout.setSpacing(12)
        if not scroll:
            return body, layout
        area = QScrollArea()
        area.setObjectName("pageScroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(body)
        return area, layout

    def _build_pages(self) -> None:
        self.pages: dict[str, QWidget] = {}
        self.nav_buttons: dict[str, QPushButton] = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        builders = {
            "dictate": self._build_dictate_page,
            "history": self._build_history_page,
            "recognition": self._build_recognition_page,
            "dictionary": self._build_dictionary_page,
            "connection": self._build_connection_page,
            "about": self._build_about_page,
        }
        for key, text, _subtitle, glyph in self.PAGES:
            page, layout = self._page(scroll=key != "history")
            builders[key](layout)
            self.stack.addWidget(page)
            self.pages[key] = page
            nav = QPushButton(text)
            nav.setProperty("variant", "nav")
            nav.setCheckable(True)
            nav.setIcon(icon(glyph, "muted", "accent_text"))
            nav.setIconSize(QSize(20, 20))
            nav.setCursor(Qt.CursorShape.PointingHandCursor)
            nav.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            nav.clicked.connect(lambda _checked=False, k=key: self.select_page(k))
            self.nav_group.addButton(nav)
            self.navigation.addWidget(nav)
            self.nav_buttons[key] = nav

    def select_page(self, key: str) -> None:
        for page_key, text, subtitle, _glyph in self.PAGES:
            if page_key == key:
                self.stack.setCurrentWidget(self.pages[key])
                self.nav_buttons[key].setChecked(True)
                self.page_title.setText(text)
                self.page_subtitle.setText(subtitle)
        self.current_page = key

    # -- pages ---------------------------------------------------------------

    def _build_dictate_page(self, page: QVBoxLayout) -> None:
        config = self.original
        shortcut_card = Card(
            "Shortcut",
            "Eenmaal drukken start de opname, nogmaals drukken stopt. De combinatie komt niet in je tekst terecht.",
        )
        self.shortcut_entry = QLineEdit(config.shortcut)
        self.shortcut_entry.setObjectName("shortcutField")
        self.shortcut_entry.textEdited.connect(lambda _text: self.mark_dirty())
        self.capture_button = button("Wijzig", glyph="keyboard")
        self.capture_button.clicked.connect(self.toggle_capture)
        row = hbox(self.shortcut_entry, self.capture_button)
        row.setStretch(0, 1)
        shortcut_card.body.addLayout(row)
        self.shortcut_hint = label(SHORTCUT_TIP, "hint", wrap=True)
        shortcut_card.body.addWidget(self.shortcut_hint)
        page.addWidget(shortcut_card)

        mic_card = Card("Microfoon", "Standaard volgt de app de Windows-instelling voor opname.")
        self.device_combo = ComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(18)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        self.refresh_devices_button = button("Vernieuwen", glyph="refresh")
        self.refresh_devices_button.clicked.connect(self.refresh_devices)
        self.microphone_test_button = button("Microfoon testen", glyph="mic")
        self.microphone_test_button.clicked.connect(self.toggle_microphone_test)
        row = hbox(self.device_combo, self.refresh_devices_button, self.microphone_test_button)
        row.setStretch(0, 1)
        mic_card.body.addLayout(row)
        self.device_details_label = label("", "hint", wrap=True, selectable=True)
        mic_card.body.addWidget(self.device_details_label)

        self.microphone_panel = QWidget()
        panel = QVBoxLayout(self.microphone_panel)
        panel.setContentsMargins(0, 6, 0, 0)
        panel.setSpacing(6)
        self.microphone_level = QProgressBar()
        self.microphone_level.setRange(0, 100)
        self.microphone_level.setTextVisible(False)
        self.microphone_listen_button = button("Terugluisteren", variant="ghost", glyph="play")
        self.microphone_listen_button.setEnabled(False)
        self.microphone_listen_button.clicked.connect(self.listen_microphone_test)
        level_row = hbox(self.microphone_level, self.microphone_listen_button, spacing=12)
        level_row.setStretch(0, 1)
        panel.addLayout(level_row)
        self.microphone_result_label = label("", "hint", wrap=True)
        panel.addWidget(self.microphone_result_label)
        self.microphone_panel.hide()
        mic_card.body.addWidget(self.microphone_panel)
        page.addWidget(mic_card)

        behaviour_card = Card("Gedrag")
        self.paste_switch = ToggleSwitch("Transcriptie automatisch plakken", config.paste_after_transcription)
        self.remove_period_switch = ToggleSwitch("Punt aan het einde verwijderen", config.remove_final_period)
        self.autostart_switch = ToggleSwitch(
            "Start automatisch met Windows", config.autostart or self.controller.autostart_enabled(),
        )
        for switch in (self.paste_switch, self.remove_period_switch, self.autostart_switch):
            switch.toggled.connect(lambda _checked: self.mark_dirty())
            behaviour_card.body.addWidget(switch)
        page.addWidget(behaviour_card)
        page.addStretch(1)

    def _build_history_page(self, page: QVBoxLayout) -> None:
        self.history_tabs = QTabWidget()
        self.history_tabs.setDocumentMode(True)
        self.recording_scroller, self.recording_host = self._history_list()
        self.history_scroller, self.history_host = self._history_list()
        self.history_tabs.addTab(self.recording_scroller, icon("mic", "muted", "accent_text"), "Opnames")
        self.history_tabs.addTab(self.history_scroller, icon("document", "muted", "accent_text"), "Teksten")
        page.addWidget(self.history_tabs, 1)
        note = label(
            "Opnames blijven ook bij een fout bewaard. De oudste verdwijnen na 20 nieuwe opnames. "
            "Opnieuw proberen gebruikt je opgeslagen instellingen.",
            "muted", wrap=True,
        )
        self.clear_history_button = button("Geschiedenis wissen", glyph="trash")
        self.clear_history_button.clicked.connect(self.clear_history)
        footer = hbox(note, self.clear_history_button, spacing=16)
        footer.setStretch(0, 1)
        page.addLayout(footer)
        self.refresh_history()

    def _history_list(self) -> tuple[QScrollArea, QVBoxLayout]:
        host = QWidget()
        host.setObjectName("historyList")
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 10, 6, 4)
        layout.setSpacing(8)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(host)
        return area, layout

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def refresh_history(self) -> None:
        """Refresh both lists after saving audio or changing a request's status."""
        if self.disposed:
            return
        entries = self.controller.history_entries()
        recordings = self.controller.recording_entries()
        busy = self.controller.recording_busy()
        self.clear_history_button.setEnabled(bool(entries or recordings) and not busy)
        self.refresh_recordings(recordings, busy)
        self._clear_layout(self.history_host)
        if not entries:
            self.history_host.addWidget(Card(
                "Nog geen transcripties",
                "Dicteer iets met je shortcut. De tekst verschijnt hier zodra hij is geplakt.",
            ))
        for entry in entries:
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(16, 10, 12, 12)
            layout.setSpacing(6)
            copy_button = button("Kopiëren", variant="ghost", glyph="copy")
            copy_button.clicked.connect(lambda _checked=False, e=entry, b=copy_button: self.copy_history_entry(e, b))
            layout.addLayout(hbox(label(entry.label(), "muted"), None, copy_button))
            layout.addWidget(label(self._preview(entry.text), wrap=True, selectable=True))
            self.history_host.addWidget(card)
        self.history_host.addStretch(1)

    @staticmethod
    def _preview(text: str, limit: int = 600) -> str:
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def refresh_recordings(self, entries: tuple[RecordingEntry, ...], busy: bool) -> None:
        self._clear_layout(self.recording_host)
        self.players = {}
        status = self.controller.playback_status()
        if status.key and status.key != MICROPHONE_TEST_KEY and status.key not in {entry.id for entry in entries}:
            self.stop_recording_playback()
        if not entries:
            self.recording_host.addWidget(Card(
                "Nog geen opnames", "Nieuwe opnames worden hier bewaard, ook als de transcriptie mislukt.",
            ))
        for entry in entries:
            card = QFrame()
            card.setObjectName("card")
            grid = QGridLayout(card)
            grid.setContentsMargins(16, 10, 12, 12)
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(6)
            when = QVBoxLayout()
            when.setSpacing(4)
            when.addWidget(label(entry.label(), "muted"))
            text, tone = STATUS_LABELS[entry.status]
            badge = label(text, "badge")
            if tone:
                badge.setProperty("tone", tone)
            when.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
            grid.addLayout(when, 0, 0)
            grid.setColumnMinimumWidth(0, 136)
            player = RecordingPlayer(entry.duration)
            player.toggled.connect(lambda e=entry: self.toggle_recording_playback(e))
            player.seek_requested.connect(lambda fraction, e=entry: self.seek_recording(e, fraction))
            grid.addWidget(player, 0, 1)
            grid.setColumnStretch(1, 1)
            self.players[entry.id] = player
            retry = button("Opnieuw transcriberen", variant="ghost", glyph="retry")
            retry.setEnabled(not busy)
            retry.clicked.connect(lambda _checked=False, e=entry: self.retry_recording(e))
            grid.addWidget(retry, 0, 2, Qt.AlignmentFlag.AlignTop)
            row = 1
            if entry.text:
                grid.addWidget(label(self._preview(entry.text), wrap=True, selectable=True), row, 0, 1, 2)
                copy = button("Kopiëren", variant="ghost", glyph="copy")
                copy.clicked.connect(lambda _checked=False, e=entry, b=copy: self.copy_history_entry(e, b))
                grid.addWidget(copy, row, 2, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
                row += 1
            if entry.error:
                grid.addWidget(label(entry.error, "danger", wrap=True, selectable=True), row, 0, 1, 3)
            self.recording_host.addWidget(card)
            self.render_player(entry.id, status)
        self.recording_host.addStretch(1)

    # -- playback ------------------------------------------------------------

    def playback_position(self, recording_id: str, status=None) -> float:
        status = status or self.controller.playback_status()
        if status.key == recording_id and status.playing:
            return status.position
        return self.playback_positions.get(recording_id, 0.0)

    def render_player(self, recording_id: str, status=None) -> None:
        player = self.players.get(recording_id)
        if player is None or player.dragging:
            return
        status = status or self.controller.playback_status()
        playing = status.key == recording_id and status.playing
        player.render(self.playback_position(recording_id, status), playing)

    def toggle_recording_playback(self, entry: RecordingEntry) -> None:
        status = self.controller.playback_status()
        if status.key == entry.id and status.playing:
            self.stop_recording_playback()
            return
        position = self.playback_positions.get(entry.id, 0.0)
        self.start_recording_playback(entry, 0.0 if position >= entry.duration - 0.05 else position)

    def seek_recording(self, entry: RecordingEntry, fraction: float) -> None:
        position = fraction * entry.duration
        status = self.controller.playback_status()
        if status.key == entry.id and status.playing and position < entry.duration - 0.05:
            self.start_recording_playback(entry, position)
            return
        self.playback_positions[entry.id] = position
        self.render_player(entry.id)

    def start_recording_playback(self, entry: RecordingEntry, offset: float) -> None:
        self.stop_recording_playback()
        self._stop_microphone_playback()
        try:
            self.controller.play_recording(entry.id, offset)
        except Exception as exc:
            self.set_status(f"Afspelen mislukt: {exc}")
            return
        self.playback_positions[entry.id] = offset
        self.playback_timer.start()
        self._playback_tick()

    def stop_recording_playback(self) -> None:
        """Pause: keep the position so the next play resumes there."""
        status = self.controller.playback_status()
        if status.key and status.key != MICROPHONE_TEST_KEY and status.playing:
            self.playback_positions[status.key] = status.position
            try:
                self.controller.stop_playback()
            except Exception as exc:
                self.set_status(f"Stoppen mislukt: {exc}")
        self.playback_timer.stop()
        for recording_id in self.players:
            self.render_player(recording_id)

    def _playback_tick(self) -> None:
        status = self.controller.playback_status()
        if status.key in self.players and not status.playing:
            self.playback_positions[status.key] = 0.0 if status.finished else status.position
            self.playback_timer.stop()
        elif not status.playing:
            self.playback_timer.stop()
        for recording_id in self.players:
            self.render_player(recording_id, status)

    def retry_recording(self, entry: RecordingEntry) -> None:
        try:
            self.controller.retry_recording(entry.id)
        except Exception as exc:
            self.set_status(f"Opnieuw proberen mislukt: {exc}")
            return
        self.refresh_history()
        self.set_status("Opnieuw transcriberen… De tekst komt op je klembord en in Geschiedenis.")

    def copy_history_entry(self, entry: HistoryEntry, copy_button: QPushButton) -> None:
        try:
            self.controller.copy_text(entry.text)
        except Exception as exc:
            self.set_status(f"Kopiëren mislukt: {exc}")
            return
        copy_button.setText("Gekopieerd ✓")
        self.set_status("Transcriptie staat op je klembord.")

        def restore() -> None:
            try:
                copy_button.setText("Kopiëren")
            except RuntimeError:
                pass  # The list was rebuilt meanwhile.

        QTimer.singleShot(1500, copy_button, restore)

    def clear_history(self) -> None:
        if not ask(self, self.controller.app_name, "Alle bewaarde opnames en transcripties verwijderen?", "Verwijderen"):
            return
        self.stop_recording_playback()
        try:
            self.controller.clear_history()
        except Exception as exc:
            self.set_status(f"Geschiedenis wissen mislukt: {exc}")
            self.refresh_history()
            return
        self.playback_positions.clear()
        self.refresh_history()
        self.set_status("Geschiedenis gewist.")

    # -- remaining pages -----------------------------------------------------

    def _build_recognition_page(self, page: QVBoxLayout) -> None:
        language_card = Card(
            "Taal",
            "Een vaste taal maakt de herkenning sneller en betrouwbaarder. Laat leeg om Groq de taal te laten raden.",
        )
        self.language_combo = ComboBox()
        self.language_combo.setEditable(True)
        self.language_combo.addItems([code for code, _ in LANGUAGE_OPTIONS if code])
        self.language_combo.setEditText(self.original.language)
        self.language_combo.editTextChanged.connect(self._language_changed)
        self.language_combo.setMaximumWidth(260)
        language_card.body.addWidget(self.language_combo)
        self.language_hint = label("", "hint")
        language_card.body.addWidget(self.language_hint)
        page.addWidget(language_card)
        self._update_language_hint()

        prompt_card = Card(
            "Prompt",
            "Optionele context voor Whisper, bijvoorbeeld het onderwerp of de schrijfstijl. "
            "Prompt en woordenboek delen samen ongeveer 224 tokens.",
        )
        self.prompt_text = QPlainTextEdit(self.original.prompt)
        self.prompt_text.setMinimumHeight(140)
        self.prompt_text.setPlaceholderText("Bijvoorbeeld: Vergadering over de planning van het project Clinon.")
        self.prompt_text.textChanged.connect(self._prompt_changed)
        prompt_card.body.addWidget(self.prompt_text, 1)
        self.prompt_counter = label("", "hint")
        prompt_card.body.addWidget(self.prompt_counter)
        page.addWidget(prompt_card, 1)
        self._update_prompt_counter()

    def _build_dictionary_page(self, page: QVBoxLayout) -> None:
        columns = QHBoxLayout()
        columns.setSpacing(12)
        words_card = Card("Woorden", "Namen en vaktermen die Groq als spellinghint meekrijgt, zoals Groq of Clinon.")
        self.word_entry = QLineEdit()
        self.word_entry.setPlaceholderText("Nieuw woord")
        self.word_entry.returnPressed.connect(self.add_word)
        add_word = button("Toevoegen", glyph="plus")
        add_word.clicked.connect(self.add_word)
        row = hbox(self.word_entry, add_word)
        row.setStretch(0, 1)
        words_card.body.addLayout(row)
        self.words_editor = ListEditor(
            self.custom_words, str, lambda count: f"{count} van {MAX_CUSTOM_WORDS} woorden", self.mark_dirty,
        )
        words_card.body.addWidget(self.words_editor, 1)
        columns.addWidget(words_card, 1)

        replacements_card = Card(
            "Vervangingen", "Vaste correcties die na de transcriptie worden toegepast, bijvoorbeeld Grok → Groq.",
        )
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.source_entry = QLineEdit()
        self.source_entry.setPlaceholderText("Bijvoorbeeld Grok")
        self.target_entry = QLineEdit()
        self.target_entry.setPlaceholderText("Bijvoorbeeld Groq")
        form.addWidget(label("Verkeerd"), 0, 0)
        form.addWidget(self.source_entry, 0, 1)
        form.addWidget(label("Correct"), 1, 0)
        form.addWidget(self.target_entry, 1, 1)
        add_replacement = button("Toevoegen", glyph="plus")
        add_replacement.clicked.connect(self.add_replacement)
        form.addWidget(add_replacement, 2, 1, Qt.AlignmentFlag.AlignRight)
        form.setColumnStretch(1, 1)
        self.source_entry.returnPressed.connect(self.target_entry.setFocus)
        self.target_entry.returnPressed.connect(self.add_replacement)
        replacements_card.body.addLayout(form)
        self.replacements_editor = ListEditor(
            self.word_replacements, lambda pair: f"{pair[0]}  →  {pair[1]}",
            lambda count: f"{count} van {MAX_WORD_REPLACEMENTS} vervangingen", self.mark_dirty,
        )
        replacements_card.body.addWidget(self.replacements_editor, 1)
        columns.addWidget(replacements_card, 1)
        page.addLayout(columns, 1)

    def _build_connection_page(self, page: QVBoxLayout) -> None:
        key_card = Card(
            "Groq API key",
            "Maak een sleutel aan op console.groq.com/keys. De sleutel wordt veilig opgeslagen in Windows Credential Manager.",
        )
        self.api_key_entry = QLineEdit(self.original.api_key)
        self.api_key_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_entry.setPlaceholderText("gsk_…")
        self.api_key_entry.textEdited.connect(lambda _text: self.mark_dirty())
        self.reveal_button = button("Tonen", glyph="eye")
        self.reveal_button.clicked.connect(self.toggle_api_key_visibility)
        row = hbox(self.api_key_entry, self.reveal_button)
        row.setStretch(0, 1)
        key_card.body.addLayout(row)
        self.test_button = button("Verbinding testen", variant="ghost", glyph="refresh")
        self.test_button.clicked.connect(self.test_connection)
        self.connection_result = label("", "hint", wrap=True)
        result_row = hbox(self.test_button, self.connection_result, spacing=12)
        result_row.setStretch(1, 1)
        key_card.body.addLayout(result_row)
        page.addWidget(key_card)

        model_card = Card("Model", "Beide modellen draaien bij Groq. Wissel gerust; je instellingen blijven verder gelijk.")
        self.model_combo = ComboBox()
        names = [name for name, _ in MODEL_OPTIONS]
        if self.original.model not in names:
            names.append(self.original.model)
        self.model_combo.addItems(names)
        self.model_combo.setCurrentText(self.original.model)
        self.model_combo.currentTextChanged.connect(self._model_changed)
        self.model_combo.setMaximumWidth(360)
        model_card.body.addWidget(self.model_combo)
        self.model_hint = label("", "hint", wrap=True)
        model_card.body.addWidget(self.model_hint)
        page.addWidget(model_card)
        page.addStretch(1)
        self._update_model_hint()

    def _build_about_page(self, page: QVBoxLayout) -> None:
        version_card = Card(
            f"{self.controller.app_name} {self.controller.app_version}",
            "Dicteer overal in Windows met Groq Whisper. Updates vervangen alleen de app; je sleutel, instellingen "
            "en geschiedenis blijven staan.",
        )
        update_button = button("Controleren op updates", glyph="download")
        update_button.clicked.connect(self.controller.check_for_updates_manual)
        version_card.body.addLayout(hbox(update_button, None))
        page.addWidget(version_card)

        tools_card = Card("Hulpmiddelen", "Handig als iets niet doet wat je verwacht.")
        log_button = button("Logbestand openen", glyph="document")
        log_button.clicked.connect(self.controller.open_log)
        restart_button = button("App herstarten", glyph="power")
        restart_button.clicked.connect(self.controller.restart)
        tools_card.body.addLayout(hbox(log_button, restart_button, None))
        tools_card.body.addWidget(label(
            f"Instellingen en logboek staan in {self.controller.app_dir}", "hint", wrap=True, selectable=True,
        ))
        page.addWidget(tools_card)

        credits = Card(
            "Over deze app",
            "Gebouwd met Python en Qt for Python (PySide6), gebruikt onder de LGPLv3. "
            "De licentieteksten staan in de map licenses naast de app.",
        )
        page.addWidget(credits)
        page.addStretch(1)

    # -- helpers -------------------------------------------------------------

    def mark_dirty(self) -> None:
        if self._loading or self.dirty:
            return
        self.dirty = True
        self.set_status("Je hebt niet-opgeslagen wijzigingen.", dirty_style=True)

    def set_status(self, message: str, *, dirty_style: bool | None = None) -> None:
        self.status_label.setText(message)
        if dirty_style is not None:
            self.status_label.setProperty("dirty", "true" if dirty_style else "false")
            repolish(self.status_label)

    def _prompt_changed(self) -> None:
        self._update_prompt_counter()
        self.mark_dirty()

    def prompt_value(self) -> str:
        return self.prompt_text.toPlainText().strip()

    def _update_prompt_counter(self) -> None:
        length = len(self.prompt_value())
        self.prompt_counter.setText(f"{length} tekens" if length else "Nog geen prompt ingesteld.")

    def _language_changed(self, _text: str) -> None:
        self._update_language_hint()
        self.mark_dirty()

    def _update_language_hint(self) -> None:
        code = self.language_combo.currentText().strip().lower()
        names = dict(LANGUAGE_OPTIONS)
        if code in names:
            self.language_hint.setText(names[code] if code else "Automatisch herkennen (iets trager).")
        else:
            self.language_hint.setText("ISO-taalcode, bijvoorbeeld nl, en of de.")

    def _model_changed(self, _text: str) -> None:
        self._update_model_hint()
        self.mark_dirty()

    def _update_model_hint(self) -> None:
        self.model_hint.setText(dict(MODEL_OPTIONS).get(self.model_combo.currentText(), ""))

    # -- microphones ---------------------------------------------------------

    def _load_devices(self, options: list[tuple[str, str]], selected: str) -> None:
        self.device_options = list(options)
        self.device_labels = {device_id: text for device_id, text in self.device_options}
        if selected not in self.device_labels:
            text = f"Niet beschikbaar: {selected.removeprefix('wasapi:')}"
            self.device_options.append((selected, text))
            self.device_labels[selected] = text
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device_id, text in self.device_options:
            self.device_combo.addItem(text, device_id)
            self.device_combo.setItemData(self.device_combo.count() - 1, text, Qt.ItemDataRole.ToolTipRole)
        self.device_combo.setCurrentIndex(self.device_combo.findData(selected))
        self.device_combo.blockSignals(False)
        self._update_device_details()

    def selected_device_id(self) -> str:
        value = self.device_combo.currentData()
        return value if isinstance(value, str) else ""

    def selected_device_label(self) -> str:
        return self.device_combo.currentText()

    def _device_changed(self, _index: int) -> None:
        self._update_device_details()
        if self.microphone_test_used and not self.microphone_test_active:
            self._close_microphone_test()
            self.microphone_panel.hide()
        self.mark_dirty()

    def _update_device_details(self) -> None:
        self.device_details_label.setText(f"Geselecteerd: {self.selected_device_label()}")
        self.device_combo.setToolTip(self.selected_device_label())

    def refresh_devices(self) -> None:
        current_id = self.selected_device_id()
        try:
            options = self.controller.list_input_devices(refresh=True)
        except Exception as exc:
            self.set_status(f"Microfoons vernieuwen mislukt: {exc}")
            return
        self._load_devices(options, current_id)
        self.set_status(f"{max(0, len(options) - 1)} microfoon(s) gevonden.", dirty_style=self.dirty)

    def toggle_microphone_test(self) -> None:
        if self.microphone_test_active:
            self._close_microphone_test()
            self.microphone_result_label.setText("Test gestopt.")
            return
        self.stop_recording_playback()
        self._close_microphone_test()
        self.refresh_devices()
        self.microphone_panel.show()
        self.microphone_listen_button.setEnabled(False)
        self.microphone_test_used = True
        try:
            self.controller.start_microphone_test(self.selected_device_id())
        except Exception as exc:
            self.microphone_result_label.setText(f"Microfoon kon niet worden geopend: {exc}")
            return
        self.microphone_test_active = True
        self.microphone_test_button.setText("Stop test")
        self.microphone_test_button.setIcon(icon("stop", "danger", "danger"))
        self.device_combo.setEnabled(False)
        self.refresh_devices_button.setEnabled(False)
        self.microphone_timer.start()
        self._microphone_tick()

    def _microphone_tick(self) -> None:
        status = self.controller.microphone_test_status()
        self.microphone_level.setValue(round(status.level * 100))
        if status.state in ("opening", "recording"):
            self.microphone_result_label.setText(
                "Microfoon openen…" if status.state == "opening"
                else f"Zeg een paar woorden… {min(status.elapsed, status.duration):.1f} / {status.duration:.0f} seconden"
            )
            return
        self.microphone_timer.stop()
        self.microphone_test_active = False
        self.microphone_test_button.setText("Opnieuw testen")
        self.microphone_test_button.setIcon(icon("mic"))
        self.device_combo.setEnabled(True)
        self.refresh_devices_button.setEnabled(True)
        self.microphone_level.setValue(0)
        if status.state == "ready":
            self.microphone_listen_button.setEnabled(True)
            self.microphone_result_label.setText(
                "Geluid ontvangen. Luister je testopname terug."
                if status.heard_audio else "Geen geluid gemeten. Controleer aansluiting, dempen en microfoonvolume."
            )
        elif status.state == "error":
            self.microphone_result_label.setText(f"Microfoontest mislukt: {status.error}")
        else:
            self.microphone_result_label.setText("Test gestopt.")

    def listen_microphone_test(self) -> None:
        if self.microphone_playback_timer.isActive():
            self._stop_microphone_playback()
            return
        self.stop_recording_playback()
        try:
            self.controller.play_microphone_test()
        except Exception as exc:
            self.microphone_result_label.setText(f"Terugluisteren mislukt: {exc}")
            return
        self.microphone_listen_button.setText("Stop afspelen")
        self.microphone_listen_button.setIcon(icon("pause", "accent_text", "accent_text"))
        self.microphone_playback_timer.start()

    def _microphone_playback_tick(self) -> None:
        status = self.controller.playback_status()
        if status.key != MICROPHONE_TEST_KEY or not status.playing:
            self._reset_microphone_listen_button()

    def _reset_microphone_listen_button(self) -> None:
        self.microphone_playback_timer.stop()
        self.microphone_listen_button.setText("Terugluisteren")
        self.microphone_listen_button.setIcon(icon("play", "accent_text", "accent_text"))

    def _stop_microphone_playback(self) -> None:
        if self.microphone_playback_timer.isActive():
            if self.controller.playback_status().key == MICROPHONE_TEST_KEY:
                self.controller.stop_playback()
            self._reset_microphone_listen_button()

    def _close_microphone_test(self) -> None:
        self.microphone_timer.stop()
        self._stop_microphone_playback()
        if self.microphone_test_used:
            self.controller.stop_microphone_test()
        self.microphone_test_active = False
        self.microphone_test_used = False
        self.microphone_test_button.setText("Microfoon testen")
        self.microphone_test_button.setIcon(icon("mic"))
        self.microphone_listen_button.setEnabled(False)
        self.device_combo.setEnabled(True)
        self.refresh_devices_button.setEnabled(True)
        self.microphone_level.setValue(0)

    # -- shortcut capture ----------------------------------------------------

    def toggle_capture(self) -> None:
        if self.capturing:
            self.stop_capture(cancelled=True)
        else:
            self.start_capture()

    def start_capture(self) -> None:
        self.capturing = True
        try:
            self.controller.suspend_hotkey()
        except Exception as exc:  # pragma: no cover - defensive
            self.set_status(f"Kon de huidige shortcut niet pauzeren: {exc}")
        self.capture_button.setText("Luistert…")
        self.shortcut_entry.setReadOnly(True)
        self.shortcut_entry.setProperty("capturing", "true")
        repolish(self.shortcut_entry)
        self.shortcut_hint.setText("Druk nu de gewenste toetscombinatie. Esc annuleert.")
        self.set_status("Druk nu op de gewenste shortcut. Esc annuleert.")
        self.activateWindow()
        self.shortcut_entry.setFocus()

    def capture_key(self, event: QKeyEvent) -> bool:
        """Handle a key while capturing; returns True when it was consumed."""
        if event.key() == Qt.Key.Key_Escape and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            self.stop_capture(cancelled=True)
            return True
        value = hotkey_from_qt_event(event)
        if value is None:
            return True
        self.stop_capture(cancelled=False)
        self.shortcut_entry.setText(value)
        self.mark_dirty()
        self.set_status(f"Shortcut ingesteld op {value}. Klik op Opslaan om te bewaren.")
        return True

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self.capturing and event.type() in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            if isinstance(watched, QWidget) and watched.window() is self:
                if event.type() == QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                return self.capture_key(event)
        return False

    def stop_capture(self, *, cancelled: bool) -> None:
        self.capturing = False
        self.capture_button.setText("Wijzig")
        self.shortcut_entry.setReadOnly(False)
        self.shortcut_entry.setProperty("capturing", "false")
        repolish(self.shortcut_entry)
        self.shortcut_hint.setText(SHORTCUT_TIP)
        try:
            self.controller.resume_hotkey()
        except Exception as exc:
            self.set_status(f"Kon de oude shortcut niet terugzetten: {exc}")
            return
        if cancelled:
            self.set_status("Shortcut wijzigen geannuleerd.")

    def _save_shortcut(self) -> None:
        if not self.capturing:
            self.save()

    def _escape_shortcut(self) -> None:
        if not self.capturing:
            self.cancel()

    # -- dictionary ----------------------------------------------------------

    def add_word(self) -> None:
        try:
            word = normalize_custom_word(self.word_entry.text())
            if any(existing.casefold() == word.casefold() for existing in self.custom_words):
                raise DictionaryValidationError(f"'{word}' staat al in het woordenboek.")
            candidate = normalize_custom_words([*self.custom_words, word])
            compose_transcription_prompt(self.prompt_value(), candidate)
        except DictionaryValidationError as exc:
            show_error(self, self.controller.app_name, str(exc))
            return
        self.words_editor.append(word)
        self.word_entry.clear()
        self.word_entry.setFocus()

    def add_replacement(self) -> None:
        try:
            source = normalize_replacement_part(self.source_entry.text(), "verkeerd herkende")
            target = normalize_replacement_part(self.target_entry.text(), "correcte")
            candidate = normalize_word_replacements([*self.word_replacements, (source, target)])
            if len(candidate) == len(self.word_replacements):
                raise DictionaryValidationError(f"Voor '{source}' bestaat al een vervanging.")
        except DictionaryValidationError as exc:
            show_error(self, self.controller.app_name, str(exc))
            return
        self.replacements_editor.append((source, target))
        self.source_entry.clear()
        self.target_entry.clear()
        self.source_entry.setFocus()

    # -- connection ----------------------------------------------------------

    def toggle_api_key_visibility(self) -> None:
        self.api_key_visible = not self.api_key_visible
        self.api_key_entry.setEchoMode(QLineEdit.EchoMode.Normal if self.api_key_visible else QLineEdit.EchoMode.Password)
        self.reveal_button.setText("Verbergen" if self.api_key_visible else "Tonen")
        self.reveal_button.setIcon(icon("eye-off" if self.api_key_visible else "eye"))

    def _set_connection_result(self, message: str, tone: str) -> None:
        self.connection_result.setText(message)
        self.connection_result.setObjectName(tone)
        repolish(self.connection_result)

    def test_connection(self) -> None:
        api_key = self.api_key_entry.text().strip()
        if not api_key:
            self._set_connection_result("Vul eerst een API key in.", "danger")
            return
        self.test_button.setEnabled(False)
        self._set_connection_result("Verbinden met Groq…", "hint")

        def run() -> None:
            try:
                message, ok = self.controller.test_api_key(api_key), True
            except Exception as exc:
                message, ok = f"Verbinding mislukt: {exc}", False
            try:
                self._connection_result.emit(message, ok)
            except RuntimeError:
                pass  # Window closed while the request was in flight.

        threading.Thread(target=run, name="groq-connection-test", daemon=True).start()

    def _show_connection_result(self, message: str, ok: bool) -> None:
        if self.disposed:
            return
        self.test_button.setEnabled(True)
        self._set_connection_result(message, "success" if ok else "danger")

    # -- save / cancel -------------------------------------------------------

    def build_config(self):
        normalized_shortcut = normalize_hotkey_text(self.shortcut_entry.text()) or "insert"
        validate_hotkey(normalized_shortcut)
        normalized_words = normalize_custom_words(self.custom_words)
        normalized_replacements = normalize_word_replacements(self.word_replacements)
        prompt = self.prompt_value()
        compose_transcription_prompt(prompt, normalized_words)
        entered_api_key = self.api_key_entry.text().strip()
        return dataclasses.replace(
            self.original,
            api_key=entered_api_key,
            model=self.model_combo.currentText().strip() or MODEL_OPTIONS[0][0],
            language=self.language_combo.currentText().strip().lower(),
            prompt=prompt,
            custom_words=normalized_words,
            word_replacements=normalized_replacements,
            shortcut=normalized_shortcut,
            input_device=self.selected_device_id(),
            paste_after_transcription=self.paste_switch.isChecked(),
            remove_final_period=self.remove_period_switch.isChecked(),
            autostart=self.autostart_switch.isChecked(),
            # If Credential Manager could not be read at startup, an unchanged
            # fallback value must not overwrite a newer secret. Deliberately
            # editing the field still authorizes the change.
            keyring_read_succeeded=(self.original.keyring_read_succeeded or entered_api_key != self.original.api_key),
        )

    def save(self) -> None:
        if self.capturing:
            self.stop_capture(cancelled=True)
        try:
            new_config = self.build_config()
        except (DictionaryValidationError, HotkeyError) as exc:
            show_error(self, self.controller.app_name, str(exc))
            return
        except Exception as exc:
            show_error(self, self.controller.app_name, f"Instellingen zijn ongeldig:\n{exc}")
            return

        try:
            self.controller.apply_settings(new_config)
        except HotkeyError as exc:
            # Everything except the shortcut was stored. Re-baseline on what is
            # now on disk, so closing does not offer to discard saved changes.
            self.original = self.controller.config
            self.dirty = normalize_hotkey_text(self.shortcut_entry.text()) != self.original.shortcut
            if self.dirty:
                self.set_status("Alleen de shortcut is niet opgeslagen.", dirty_style=True)
            else:
                self.set_status("Instellingen zijn opgeslagen.", dirty_style=False)
            self.select_page("dictate")
            show_error(self, self.controller.app_name, str(exc))
            return
        except Exception as exc:
            show_error(self, self.controller.app_name, f"Instellingen konden niet worden opgeslagen:\n{exc}")
            return

        self.dirty = False
        self.close()

    def cancel(self) -> None:
        self.close()

    def dispose(self) -> None:
        """Release audio and the global key capture; safe to call twice."""
        if self.disposed:
            return
        if self.capturing:
            self.stop_capture(cancelled=True)
        self._close_microphone_test()
        self.stop_recording_playback()
        self.playback_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.disposed = True

    def closeEvent(self, event) -> None:
        if self.capturing:
            self.stop_capture(cancelled=True)
        if self.dirty and not ask(
            self, self.controller.app_name,
            "Je hebt wijzigingen die nog niet zijn opgeslagen. Wil je ze verwerpen?", "Verwerpen", "Blijven",
        ):
            event.ignore()
            return
        self.dispose()
        event.accept()
        self.closed.emit()
