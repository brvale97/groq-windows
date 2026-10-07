"""Shared Qt look: light/dark palettes, the style sheet and crisp vector icons.

Everything is drawn with QPainter at the size and device pixel ratio Windows
asks for, so icons stay sharp at 100, 125, 150 or 200 % scaling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QIconEngine,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication

from branding import ICO_SIZES, draw_icon

FONT_FAMILY = "Segoe UI"
BASE_POINT_SIZE = 10


@dataclass(frozen=True)
class Theme:
    dark: bool
    shell: str
    content: str
    card: str
    card_border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    accent_text: str
    on_accent: str
    field: str
    field_border: str
    field_focus: str
    button: str
    button_hover: str
    button_pressed: str
    danger: str
    success: str
    warning_text: str
    selection: str


LIGHT = Theme(
    dark=False,
    shell="#eef3f1",
    content="#f8faf9",
    card="#ffffff",
    card_border="#dfe7e3",
    text="#1c2730",
    muted="#5b6a63",
    accent="#176b53",
    accent_hover="#1f8064",
    accent_pressed="#114f3d",
    accent_soft="#dbede6",
    accent_text="#135a46",
    on_accent="#ffffff",
    field="#ffffff",
    field_border="#c9d5cf",
    field_focus="#1f8064",
    button="#e8efec",
    button_hover="#dce6e1",
    button_pressed="#cfdcd6",
    danger="#b3261e",
    success="#1b7f5a",
    warning_text="#8a5a00",
    selection="#cfe8de",
)

DARK = Theme(
    dark=True,
    shell="#121715",
    content="#171d1b",
    card="#1f2724",
    card_border="#2f3b37",
    text="#e5ece9",
    muted="#9aaaa3",
    accent="#1f8064",
    accent_hover="#26957a",
    accent_pressed="#176b53",
    accent_soft="#1d3a31",
    accent_text="#7dd3b2",
    on_accent="#ffffff",
    field="#151b19",
    field_border="#3a4843",
    field_focus="#3cb98d",
    button="#28322f",
    button_hover="#313d39",
    button_pressed="#3a4743",
    danger="#ff8a80",
    success="#6fd3a8",
    warning_text="#f0c36b",
    selection="#245244",
)

_current: Theme = LIGHT


def current_theme() -> Theme:
    return _current


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return False


def build_palette(theme: Theme) -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: theme.content,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.field,
        QPalette.ColorRole.AlternateBase: theme.card,
        QPalette.ColorRole.ToolTipBase: theme.card,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.button,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.BrightText: theme.on_accent,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: theme.on_accent,
        QPalette.ColorRole.Link: theme.accent_text,
        QPalette.ColorRole.Mid: theme.card_border,
        QPalette.ColorRole.Midlight: theme.card_border,
        QPalette.ColorRole.Dark: theme.field_border,
        QPalette.ColorRole.Light: theme.card,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, value in colors.items():
        palette.setColor(role, QColor(value))
    disabled = QColor(theme.muted)
    disabled.setAlpha(150)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return palette


STYLE_SHEET = """
* {{ font-family: "{font}"; }}
QWidget {{ color: {text}; }}
QWidget#settingsRoot, QWidget#pageHost, QScrollArea#pageScroll, QWidget#pageBody,
QWidget#historyList {{ background: {content}; }}
QFrame#sidebar {{ background: {shell}; border: none; border-right: 1px solid {card_border}; }}
QLabel#brandTitle {{ font-size: 13pt; font-weight: 600; color: {accent_text}; background: transparent; }}
QLabel#brandSubtitle, QLabel#sideVersion {{ color: {muted}; background: transparent; }}
QLabel#pageTitle {{ font-size: 20pt; font-weight: 600; background: transparent; }}
QLabel#pageSubtitle {{ color: {muted}; font-size: 10.5pt; background: transparent; }}
QLabel#muted, QLabel#cardDescription, QLabel#hint {{ color: {muted}; background: transparent; }}
QLabel#cardTitle {{ font-size: 11pt; font-weight: 600; background: transparent; }}
QLabel#success {{ color: {success}; background: transparent; }}
QLabel#danger {{ color: {danger}; background: transparent; }}
QLabel#status {{ color: {muted}; }}
QLabel#status[dirty="true"] {{ color: {warning_text}; font-weight: 600; }}
QFrame#card QLabel#badge {{ color: {accent_text}; background: {accent_soft}; border-radius: 9px; padding: 1px 9px; font-size: 9pt; font-weight: 600; }}
QFrame#card QLabel#badge[tone="danger"] {{ color: {danger}; background: transparent; border: 1px solid {danger}; }}
QFrame#card QLabel#badge[tone="muted"] {{ color: {muted}; background: {button}; }}
QFrame#card {{ background: {card}; border: 1px solid {card_border}; border-radius: 10px; }}
QFrame#card QLabel {{ background: transparent; }}
QFrame#footer {{ background: {content}; border: none; border-top: 1px solid {card_border}; }}

QPushButton {{
    background: {button}; color: {text}; border: 1px solid transparent; border-radius: 7px;
    padding: 7px 16px; min-height: 22px;
}}
QPushButton:hover {{ background: {button_hover}; }}
QPushButton:pressed {{ background: {button_pressed}; }}
QPushButton:disabled {{ color: {disabled_text}; background: {button}; }}
QPushButton:focus {{ border: 1px solid {field_focus}; }}
QPushButton[variant="primary"] {{ background: {accent}; color: {on_accent}; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {accent_soft}; color: {disabled_text}; }}
QPushButton[variant="primary"]:focus {{ border: 1px solid {on_accent}; }}
QPushButton[variant="ghost"] {{ background: transparent; color: {accent_text}; padding: 6px 12px; }}
QPushButton[variant="ghost"]:hover {{ background: {accent_soft}; }}
QPushButton[variant="ghost"]:pressed {{ background: {selection}; }}
QPushButton[variant="ghost"]:disabled {{ background: transparent; color: {disabled_text}; }}
QPushButton[variant="nav"] {{
    background: transparent; color: {muted}; text-align: left; padding: 9px 12px;
    border-radius: 8px; font-size: 10.5pt;
}}
QPushButton[variant="nav"]:hover {{ background: {button_hover}; color: {text}; }}
QPushButton[variant="nav"]:checked {{ background: {accent_soft}; color: {accent_text}; font-weight: 600; }}
QPushButton[variant="nav"]:focus {{ border: 1px solid {field_focus}; }}

QLineEdit, QPlainTextEdit, QComboBox {{
    background: {field}; color: {text}; border: 1px solid {field_border}; border-radius: 7px;
    padding: 6px 9px; selection-background-color: {selection}; selection-color: {text};
}}
QLineEdit, QComboBox {{ min-height: 22px; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border: 1px solid {field_focus}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {disabled_text}; background: {content}; }}
QLineEdit#shortcutField {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 11pt; }}
QLineEdit#shortcutField[capturing="true"] {{ border: 2px solid {accent}; background: {accent_soft}; }}
QPlainTextEdit[readOnly="true"] {{ background: {content}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox QAbstractItemView {{
    background: {card}; color: {text}; border: 1px solid {card_border}; border-radius: 6px;
    selection-background-color: {accent_soft}; selection-color: {accent_text}; padding: 4px; outline: none;
}}
QListWidget {{
    background: {field}; color: {text}; border: 1px solid {field_border}; border-radius: 7px;
    padding: 4px; outline: none;
}}
QListWidget::item {{ padding: 6px 8px; border-radius: 5px; }}
QListWidget::item:selected {{ background: {accent_soft}; color: {accent_text}; }}
QListWidget::item:hover:!selected {{ background: {button}; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent; color: {muted}; padding: 7px 16px; margin-right: 4px;
    border: none; border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:selected {{ color: {accent_text}; border-bottom: 2px solid {accent}; }}
QTabBar::tab:hover:!selected {{ color: {text}; }}

QProgressBar {{ background: {button}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {field_border}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}

QMenu {{ background: {card}; color: {text}; border: 1px solid {card_border}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {accent_soft}; color: {accent_text}; }}
QMenu::separator {{ height: 1px; background: {card_border}; margin: 4px 8px; }}
QToolTip {{ background: {card}; color: {text}; border: 1px solid {card_border}; padding: 5px 8px; }}
QMessageBox, QDialog {{ background: {content}; }}
"""


def style_sheet(theme: Theme) -> str:
    disabled = QColor(theme.muted)
    disabled.setAlpha(140)
    values = {name: getattr(theme, name) for name in Theme.__dataclass_fields__ if name != "dark"}
    return STYLE_SHEET.format(font=FONT_FAMILY, disabled_text=disabled.name(QColor.NameFormat.HexArgb), **values)


def apply_theme(app: QApplication, theme: Theme | None = None) -> Theme:
    global _current
    _current = theme or (DARK if system_prefers_dark() else LIGHT)
    app.setStyle("Fusion")
    font = QFont(FONT_FAMILY, BASE_POINT_SIZE)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    app.setPalette(build_palette(_current))
    app.setStyleSheet(style_sheet(_current))
    return _current


def repolish(widget) -> None:
    """Re-evaluate dynamic-property selectors such as ``[dirty="true"]``."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------


def _pen(painter: QPainter, color: QColor, unit: float, width: float = 1.8) -> None:
    pen = QPen(color, width * unit)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def draw_glyph(painter: QPainter, name: str, rect: QRectF, color: QColor) -> None:
    """Line icons on a 24-unit grid, in the spirit of Fluent/Lucide outlines."""
    unit = min(rect.width(), rect.height()) / 24.0
    ox = rect.x() + (rect.width() - 24 * unit) / 2
    oy = rect.y() + (rect.height() - 24 * unit) / 2

    def p(x: float, y: float) -> QPointF:
        return QPointF(ox + x * unit, oy + y * unit)

    def r(x: float, y: float, w: float, h: float) -> QRectF:
        return QRectF(ox + x * unit, oy + y * unit, w * unit, h * unit)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _pen(painter, color, unit)
    if name == "mic":
        painter.drawRoundedRect(r(9, 3, 6, 11.5), 3 * unit, 3 * unit)
        path = QPainterPath(p(5.5, 11))
        path.cubicTo(p(5.5, 19), p(18.5, 19), p(18.5, 11))
        painter.drawPath(path)
        painter.drawLine(p(12, 17.5), p(12, 21))
        painter.drawLine(p(8.5, 21), p(15.5, 21))
    elif name == "history":
        painter.drawArc(r(3.5, 3.5, 17, 17), 200 * 16, -310 * 16)
        painter.drawLine(p(4.3, 8.2), p(4.6, 12.0))
        painter.drawLine(p(4.3, 8.2), p(8.0, 8.9))
        painter.drawLine(p(12, 7.5), p(12, 12))
        painter.drawLine(p(12, 12), p(15, 14))
    elif name == "sparkles":
        star = QPainterPath(p(10, 3))
        star.cubicTo(p(10.8, 8.5), p(11.5, 9.2), p(17, 10))
        star.cubicTo(p(11.5, 10.8), p(10.8, 11.5), p(10, 17))
        star.cubicTo(p(9.2, 11.5), p(8.5, 10.8), p(3, 10))
        star.cubicTo(p(8.5, 9.2), p(9.2, 8.5), p(10, 3))
        painter.drawPath(star)
        painter.drawLine(p(18, 15), p(18, 21))
        painter.drawLine(p(15, 18), p(21, 18))
    elif name == "book":
        painter.drawRoundedRect(r(5, 3, 14, 18), 2 * unit, 2 * unit)
        painter.drawLine(p(8.5, 3), p(8.5, 21))
        painter.drawLine(p(11.5, 8), p(16, 8))
        painter.drawLine(p(11.5, 11.5), p(15, 11.5))
    elif name == "key":
        painter.drawEllipse(r(3, 9, 8, 8))
        painter.drawLine(p(11, 13), p(21, 13))
        painter.drawLine(p(18, 13), p(18, 16.5))
        painter.drawLine(p(15, 13), p(15, 15.5))
    elif name == "info":
        painter.drawEllipse(r(3, 3, 18, 18))
        painter.drawLine(p(12, 11), p(12, 16.5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(p(12, 7.6), 1.2 * unit, 1.2 * unit)
    elif name == "refresh":
        painter.drawArc(r(4, 4, 16, 16), 40 * 16, 280 * 16)
        painter.drawLine(p(18.3, 3.8), p(18.3, 8.2))
        painter.drawLine(p(18.3, 8.2), p(13.9, 8.2))
    elif name == "copy":
        painter.drawRoundedRect(r(8, 8, 12, 12), 2 * unit, 2 * unit)
        path = QPainterPath(p(16, 8))
        path.lineTo(p(16, 6))
        path.quadTo(p(16, 4), p(14, 4))
        path.lineTo(p(6, 4))
        path.quadTo(p(4, 4), p(4, 6))
        path.lineTo(p(4, 14))
        path.quadTo(p(4, 16), p(6, 16))
        path.lineTo(p(8, 16))
        painter.drawPath(path)
    elif name == "check":
        path = QPainterPath(p(4.5, 12.5))
        path.lineTo(p(9.5, 17.5))
        path.lineTo(p(19.5, 6.5))
        painter.drawPath(path)
    elif name == "trash":
        painter.drawLine(p(4, 6.5), p(20, 6.5))
        painter.drawLine(p(9.5, 6.5), p(10, 3.5))
        painter.drawLine(p(10, 3.5), p(14, 3.5))
        painter.drawLine(p(14, 3.5), p(14.5, 6.5))
        path = QPainterPath(p(6, 6.5))
        path.lineTo(p(7, 19))
        path.quadTo(p(7.2, 20.5), p(8.7, 20.5))
        path.lineTo(p(15.3, 20.5))
        path.quadTo(p(16.8, 20.5), p(17, 19))
        path.lineTo(p(18, 6.5))
        painter.drawPath(path)
    elif name in {"eye", "eye-off"}:
        path = QPainterPath(p(2.5, 12))
        path.cubicTo(p(6, 5.5), p(18, 5.5), p(21.5, 12))
        path.cubicTo(p(18, 18.5), p(6, 18.5), p(2.5, 12))
        painter.drawPath(path)
        painter.drawEllipse(r(9, 9, 6, 6))
        if name == "eye-off":
            painter.drawLine(p(4, 4), p(20, 20))
    elif name == "retry":
        painter.drawArc(r(4, 4, 16, 16), 90 * 16, 290 * 16)
        painter.drawLine(p(12, 4), p(15, 1.8))
        painter.drawLine(p(12, 4), p(15, 6.4))
    elif name == "document":
        path = QPainterPath(p(14, 3))
        path.lineTo(p(7, 3))
        path.quadTo(p(5, 3), p(5, 5))
        path.lineTo(p(5, 19))
        path.quadTo(p(5, 21), p(7, 21))
        path.lineTo(p(17, 21))
        path.quadTo(p(19, 21), p(19, 19))
        path.lineTo(p(19, 8))
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(p(14, 3), p(14, 8))
        painter.drawLine(p(14, 8), p(19, 8))
        painter.drawLine(p(8.5, 13), p(15.5, 13))
        painter.drawLine(p(8.5, 16.5), p(13.5, 16.5))
    elif name == "power":
        painter.drawArc(r(4, 4.5, 16, 16), 55 * 16, -290 * 16)
        painter.drawLine(p(12, 2.5), p(12, 11))
    elif name == "download":
        painter.drawLine(p(12, 3.5), p(12, 15))
        painter.drawLine(p(7.5, 10.5), p(12, 15))
        painter.drawLine(p(16.5, 10.5), p(12, 15))
        painter.drawLine(p(4.5, 20), p(19.5, 20))
    elif name == "stop":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(r(6, 6, 12, 12), 2.5 * unit, 2.5 * unit)
    elif name == "play":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        path = QPainterPath(p(8, 5.5))
        path.lineTo(p(19, 12))
        path.lineTo(p(8, 18.5))
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "pause":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(r(7, 5.5, 3.6, 13), 1.2 * unit, 1.2 * unit)
        painter.drawRoundedRect(r(13.4, 5.5, 3.6, 13), 1.2 * unit, 1.2 * unit)
    elif name == "keyboard":
        painter.drawRoundedRect(r(2.5, 6, 19, 12), 2.5 * unit, 2.5 * unit)
        for x in (6, 9.5, 13, 16.5):
            painter.drawPoint(p(x + 0.5, 10))
        painter.drawLine(p(8, 14.5), p(16, 14.5))
    elif name == "chevron":
        path = QPainterPath(p(6.5, 9.5))
        path.lineTo(p(12, 15))
        path.lineTo(p(17.5, 9.5))
        painter.drawPath(path)
    elif name == "plus":
        painter.drawLine(p(12, 5), p(12, 19))
        painter.drawLine(p(5, 12), p(19, 12))
    elif name == "warning":
        path = QPainterPath(p(12, 3.5))
        path.lineTo(p(21, 19.5))
        path.lineTo(p(3, 19.5))
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(p(12, 9.5), p(12, 14))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(p(12, 16.8), 1.1 * unit, 1.1 * unit)
    else:  # pragma: no cover - programming error
        raise ValueError(f"Unknown icon {name!r}")
    painter.restore()


class VectorIconEngine(QIconEngine):
    """Paints a glyph at the exact requested size; colors follow the theme."""

    def __init__(self, name: str, role: str = "text", checked_role: str = "accent_text") -> None:
        super().__init__()
        self.name = name
        self.role = role
        self.checked_role = checked_role

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> QColor:
        theme = current_theme()
        if mode == QIcon.Mode.Disabled:
            color = QColor(theme.muted)
            color.setAlpha(130)
            return color
        role = self.checked_role if state == QIcon.State.On else self.role
        return QColor(getattr(theme, role))

    def paint(self, painter: QPainter, rect, mode, state) -> None:
        draw_glyph(painter, self.name, QRectF(rect), self._color(mode, state))

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode, state, scale: float) -> QPixmap:
        image = QImage(
            max(1, round(size.width() * scale)), max(1, round(size.height() * scale)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        draw_glyph(painter, self.name, QRectF(0, 0, image.width(), image.height()), self._color(mode, state))
        painter.end()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def clone(self) -> "VectorIconEngine":
        return VectorIconEngine(self.name, self.role, self.checked_role)


def icon(name: str, role: str = "text", checked_role: str = "accent_text") -> QIcon:
    return QIcon(VectorIconEngine(name, role, checked_role))


def pil_to_pixmap(image) -> QPixmap:
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


def app_icon() -> QIcon:
    """The branded icon with a separately rendered frame for every native size."""
    result = QIcon()
    for size in (*ICO_SIZES, 512):
        result.addPixmap(pil_to_pixmap(draw_icon(size)))
    return result


def ring_angle(elapsed_ms: float) -> float:
    """Spinner angle; one turn per 0.9 s."""
    return (elapsed_ms / 900.0 * 360.0) % 360.0


def ease(value: float) -> float:
    return 0.5 - math.cos(math.pi * max(0.0, min(1.0, value))) / 2
