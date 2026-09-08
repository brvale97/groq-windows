"""Settings window for Groq Insert Dictation.

The window is a plain Tk/ttk implementation so the app stays a single small
PyInstaller executable. It talks to the tray application through a small
"controller" object (see :class:`SettingsController`) instead of importing
``app`` directly, which keeps the UI testable without a microphone, tray icon
or Groq credentials.
"""

from __future__ import annotations

import dataclasses
import threading
from tkinter import END, BooleanVar, Canvas, Listbox, StringVar, Text, Tk, Toplevel, messagebox, ttk
from typing import Protocol

from PIL import Image, ImageDraw, ImageTk

from dictation_core import (
    MAX_CUSTOM_WORDS,
    MAX_WORD_REPLACEMENTS,
    DictionaryValidationError,
    compose_transcription_prompt,
    normalize_custom_word,
    normalize_custom_words,
    normalize_replacement_part,
    normalize_word_replacements,
)
from history import MAX_HISTORY_ENTRIES, HistoryEntry
from hotkeys import HotkeyError, hotkey_from_tk_event, normalize_hotkey_text, validate_hotkey


# --------------------------------------------------------------------------
# Visual language
# --------------------------------------------------------------------------

FONT_FAMILY = "Segoe UI"
COLORS = {
    "shell": "#f3f6f5",
    "content": "#ffffff",
    "card": "#f8faf9",
    "card_border": "#e1e8e5",
    "text": "#1f2a33",
    "muted": "#66756e",
    "accent": "#176b53",
    "accent_hover": "#1f8064",
    "accent_pressed": "#114f3d",
    "accent_soft": "#dceee8",
    "accent_text": "#145c48",
    "field_border": "#d3dcd8",
    "field_focus": "#208065",
    "danger": "#b3261e",
    "success": "#1b7f5a",
    "warning_bg": "#fff6e5",
    "warning_text": "#8a5a00",
}

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
DEFAULT_DEVICE_LABEL = "Windows default input"


def ui_scale(widget) -> float:
    """Pixels per 96-DPI logical pixel; Tk's ``scaling`` is points-based (1.333 at 96 DPI)."""
    try:
        return max(0.75, float(widget.tk.call("tk", "scaling")) / (96 / 72))
    except Exception:
        return 1.0


def apply_theme(root: Tk) -> None:
    """One shared, DPI-aware visual language for every window of the app."""
    root.option_add("*Listbox.font", (FONT_FAMILY, 10))
    root.option_add("*Listbox.background", "#ffffff")
    root.option_add("*Listbox.foreground", COLORS["text"])
    root.option_add("*Listbox.selectBackground", COLORS["accent_soft"])
    root.option_add("*Listbox.selectForeground", COLORS["accent_text"])
    root.option_add("*Listbox.relief", "flat")
    root.option_add("*Listbox.borderWidth", 0)
    root.option_add("*Listbox.highlightThickness", 1)
    root.option_add("*Listbox.highlightBackground", COLORS["field_border"])
    root.option_add("*Listbox.highlightColor", COLORS["field_focus"])
    root.option_add("*Listbox.activeStyle", "none")
    root.option_add("*Text.font", (FONT_FAMILY, 10))
    root.option_add("*TCombobox*Listbox.font", (FONT_FAMILY, 10))

    style = ttk.Style(root)
    style.theme_use("clam")
    base_font = (FONT_FAMILY, 10)
    style.configure(".", font=base_font, background=COLORS["content"], foreground=COLORS["text"])

    # Surfaces
    style.configure("TFrame", background=COLORS["content"])
    style.configure("Shell.TFrame", background=COLORS["shell"])
    style.configure(
        "Card.TFrame",
        background=COLORS["card"],
        relief="solid",
        borderwidth=1,
        bordercolor=COLORS["card_border"],
        lightcolor=COLORS["card_border"],
        darkcolor=COLORS["card_border"],
    )
    style.configure("CardBody.TFrame", background=COLORS["card"])
    style.configure("Footer.TFrame", background=COLORS["content"])
    style.configure("TSeparator", background=COLORS["card_border"])

    # Text
    style.configure("TLabel", background=COLORS["content"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"])
    style.configure("Title.TLabel", font=(FONT_FAMILY, 22, "bold"))
    style.configure("Subtitle.TLabel", foreground=COLORS["muted"], font=(FONT_FAMILY, 10))
    style.configure("Brand.TLabel", font=(FONT_FAMILY, 16, "bold"), background=COLORS["shell"], foreground=COLORS["accent_text"])
    style.configure("Side.TLabel", background=COLORS["shell"], foreground=COLORS["muted"])
    style.configure("Card.TLabel", background=COLORS["card"])
    style.configure("CardTitle.TLabel", background=COLORS["card"], font=(FONT_FAMILY, 11, "bold"))
    style.configure("CardMuted.TLabel", background=COLORS["card"], foreground=COLORS["muted"])
    style.configure("CardSuccess.TLabel", background=COLORS["card"], foreground=COLORS["success"])
    style.configure("CardDanger.TLabel", background=COLORS["card"], foreground=COLORS["danger"])
    style.configure("Status.TLabel", foreground=COLORS["muted"])
    style.configure("StatusDirty.TLabel", foreground=COLORS["warning_text"])

    # Buttons
    style.configure(
        "TButton",
        padding=(14, 8),
        background="#e9efec",
        foreground=COLORS["text"],
        borderwidth=0,
        focusthickness=2,
        focuscolor="#a0c9bb",
    )
    style.map("TButton", background=[("active", "#dde6e2"), ("pressed", "#d0dbd6"), ("disabled", "#eef2f0")], foreground=[("disabled", "#9aa8a2")])
    style.configure("Accent.TButton", background=COLORS["accent"], foreground="#ffffff")
    style.map(
        "Accent.TButton",
        background=[("pressed", COLORS["accent_pressed"]), ("active", COLORS["accent_hover"]), ("disabled", "#a8c7bc")],
        foreground=[("disabled", "#eef5f2"), ("!disabled", "#ffffff")],
    )
    style.configure("Ghost.TButton", background="#e6f1ed", foreground=COLORS["accent_text"], padding=(12, 7))
    style.map("Ghost.TButton", background=[("active", COLORS["accent_soft"]), ("pressed", "#c9e2d8"), ("disabled", "#eef3f1")], foreground=[("disabled", "#9aa8a2")])
    style.configure("Nav.TButton", anchor="w", padding=(16, 11), background=COLORS["shell"], foreground="#4f6058", borderwidth=0)
    style.map(
        "Nav.TButton",
        background=[("selected", COLORS["accent_soft"]), ("active", "#e6edea")],
        foreground=[("selected", COLORS["accent_text"])],
    )

    # Inputs
    style.configure("TEntry", padding=8, fieldbackground="#ffffff", bordercolor=COLORS["field_border"], lightcolor="#ffffff", darkcolor="#ffffff", insertcolor=COLORS["text"])
    style.map("TEntry", bordercolor=[("focus", COLORS["field_focus"])], lightcolor=[("focus", COLORS["field_focus"])], darkcolor=[("focus", COLORS["field_focus"])])
    style.configure("TCombobox", padding=7, bordercolor=COLORS["field_border"], arrowsize=16, arrowcolor=COLORS["muted"], background="#ffffff", lightcolor="#ffffff", darkcolor="#ffffff")
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", "#ffffff"), ("!readonly", "#ffffff")],
        selectbackground=[("readonly", "#ffffff")],
        selectforeground=[("readonly", COLORS["text"])],
        bordercolor=[("focus", COLORS["field_focus"])],
        background=[("active", "#f3f6f5")],
    )
    style.layout(
        "Vertical.TScrollbar",
        [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})],
    )
    style.configure(
        "Vertical.TScrollbar",
        width=8,
        gripcount=0,
        background="#c9d4cf",
        troughcolor="#ffffff",
        bordercolor="#ffffff",
        lightcolor="#c9d4cf",
        darkcolor="#c9d4cf",
        relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", "#aebcb6"), ("pressed", "#9aaba4")])
    style.configure("Horizontal.TProgressbar", background=COLORS["accent"], troughcolor="#e7eeeb", borderwidth=0)

    # Switches: drawn at the active Tk scale, images kept alive on the root.
    switch_scale = float(root.tk.call("tk", "scaling")) / (96 / 72)
    switch_width = max(34, round(38 * switch_scale))
    switch_height = max(18, round(21 * switch_scale))
    root._switch_images = []  # type: ignore[attr-defined]
    for enabled in (False, True):
        bitmap = Image.new("RGBA", (switch_width * 3, switch_height * 3), (255, 255, 255, 0))
        draw = ImageDraw.Draw(bitmap)
        width, height = bitmap.size
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=height // 2, fill=COLORS["accent"] if enabled else "#b6c2bd")
        knob_x = width - height if enabled else 0
        draw.ellipse((knob_x + 7, 7, knob_x + height - 8, height - 8), fill="#ffffff")
        bitmap = bitmap.resize((switch_width, switch_height), Image.Resampling.LANCZOS)
        padded = Image.new("RGBA", (switch_width + round(10 * switch_scale), switch_height), (255, 255, 255, 0))
        padded.paste(bitmap, (0, 0))
        root._switch_images.append(ImageTk.PhotoImage(padded, master=root))  # type: ignore[attr-defined]
    style.element_create(
        "Switch.indicator",
        "image",
        root._switch_images[0],  # type: ignore[attr-defined]
        ("selected", root._switch_images[1]),  # type: ignore[attr-defined]
        sticky="w",
        border=0,
    )
    style.layout(
        "Switch.TCheckbutton",
        [
            (
                "Checkbutton.padding",
                {
                    "sticky": "nswe",
                    "children": [
                        ("Switch.indicator", {"side": "left", "sticky": "w"}),
                        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]}),
                    ],
                },
            )
        ],
    )
    style.configure("Switch.TCheckbutton", padding=(0, 5), background=COLORS["card"], space=10)
    style.map("Switch.TCheckbutton", background=[("active", COLORS["card"])])


# --------------------------------------------------------------------------
# Controller protocol
# --------------------------------------------------------------------------


class SettingsController(Protocol):
    """What the settings window needs from the running application."""

    app_name: str
    app_version: str
    app_dir: str
    config: object

    def list_input_devices(self) -> list[tuple[str, str]]: ...
    def autostart_enabled(self) -> bool: ...
    def apply_settings(self, new_config) -> None: ...
    def suspend_hotkey(self) -> None: ...
    def resume_hotkey(self) -> None: ...
    def test_api_key(self, api_key: str) -> str: ...
    def history_entries(self) -> tuple[HistoryEntry, ...]: ...
    def copy_text(self, text: str) -> None: ...
    def clear_history(self) -> None: ...
    def test_sounds(self) -> None: ...
    def check_for_updates_manual(self) -> None: ...
    def open_log(self) -> None: ...
    def restart(self) -> None: ...


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


class Card(ttk.Frame):
    """Bordered section with a title, optional description and a body frame."""

    PADDING = (16, 12, 16, 14)

    def __init__(self, parent, title: str, description: str | None = None) -> None:
        super().__init__(parent, style="Card.TFrame", padding=self.PADDING)
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        next_row = 1
        if description:
            self.description = ttk.Label(self, text=description, style="CardMuted.TLabel", justify="left")
            self.description.grid(row=1, column=0, sticky="w", pady=(2, 0))
            self.bind("<Configure>", self._rewrap, add="+")
            next_row = 2
        self.body = ttk.Frame(self, style="CardBody.TFrame")
        self.body.grid(row=next_row, column=0, sticky="nsew", pady=(10, 0))
        self.body.columnconfigure(0, weight=1)
        self.rowconfigure(next_row, weight=1)

    def _rewrap(self, event) -> None:
        horizontal_padding = self.PADDING[0] + self.PADDING[2] + 4
        self.description.configure(wraplength=max(160, event.width - horizontal_padding))


def bordered_text(parent, height: int) -> Text:
    widget = Text(
        parent,
        height=height,
        wrap="word",
        relief="flat",
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=COLORS["field_border"],
        highlightcolor=COLORS["field_focus"],
        padx=10,
        pady=8,
        background="#ffffff",
        foreground=COLORS["text"],
        insertbackground=COLORS["text"],
        undo=True,
    )
    return widget


class ListEditor(ttk.Frame):
    """Listbox + scrollbar + remove button, backed by a Python list."""

    def __init__(self, parent, items: list, render, counter_text, on_change, *, height: int = 9) -> None:
        super().__init__(parent, style="CardBody.TFrame")
        self.items = items
        self.render = render
        self.counter_text = counter_text
        self.on_change = on_change
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.listbox = Listbox(self, height=height, exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<Delete>", lambda _event: self.remove_selected())
        self.listbox.bind("<BackSpace>", lambda _event: self.remove_selected())

        toolbar = ttk.Frame(self, style="CardBody.TFrame")
        toolbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        toolbar.columnconfigure(0, weight=1)
        self.counter = ttk.Label(toolbar, text="", style="CardMuted.TLabel")
        self.counter.grid(row=0, column=0, sticky="w")
        self.remove_button = ttk.Button(toolbar, text="Verwijderen", style="Ghost.TButton", command=self.remove_selected)
        self.remove_button.grid(row=0, column=1, sticky="e")
        self.refresh()

    def refresh(self, select_index: int | None = None) -> None:
        self.listbox.delete(0, END)
        for item in self.items:
            self.listbox.insert(END, self.render(item))
        if select_index is not None and self.items:
            index = min(select_index, len(self.items) - 1)
            self.listbox.selection_set(index)
            self.listbox.see(index)
        self.remove_button.state(["!disabled"] if self.items else ["disabled"])
        self.counter.configure(text=self.counter_text(len(self.items)))

    def append(self, item) -> None:
        self.items.append(item)
        self.refresh(len(self.items) - 1)
        self.on_change()

    def remove_selected(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        index = int(selection[0])
        del self.items[index]
        self.refresh(index)
        self.on_change()


class ScrollableFrame(ttk.Frame):
    """Vertical scrolling container; ``self.inner`` holds the content."""

    def __init__(self, parent, background: str) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.inner = ttk.Frame(self.canvas)
        self.inner.columnconfigure(0, weight=1)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.canvas, self.inner):
            widget.bind("<Enter>", lambda _event: self._bind_wheel())
            widget.bind("<Leave>", lambda _event: self._unbind_wheel())

    def _on_inner_configure(self, _event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_scrollbar()

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        needs_scroll = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        if needs_scroll:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


# --------------------------------------------------------------------------
# The settings window
# --------------------------------------------------------------------------


class SettingsWindow(Toplevel):
    PAGES = (
        ("dictate", "Dicteren", "Jouw stem, direct op de juiste plek."),
        ("history", "Geschiedenis", f"Je laatste {MAX_HISTORY_ENTRIES} transcripties, direct te kopiëren."),
        ("recognition", "Herkenning", "Help Groq jouw taal en context beter te begrijpen."),
        ("dictionary", "Woordenboek", "Eigen namen, vaktermen en vaste correcties."),
        ("connection", "Verbinding", "Je Groq API key en het transcriptiemodel."),
        ("about", "Over", "Versie, updates en hulpmiddelen."),
    )
    # Logical (96-DPI) sizes; scaled with the display so text always fits.
    WIDTH = 940
    HEIGHT = 700
    MIN_WIDTH = 900
    MIN_HEIGHT = 660

    def __init__(self, root: Tk, controller: SettingsController) -> None:
        super().__init__(root)
        self.controller = controller
        self.original = controller.config
        self.scale = ui_scale(self)
        self.title(f"{controller.app_name} instellingen")
        self.minsize(self.px(self.MIN_WIDTH), self.px(self.MIN_HEIGHT))
        self.resizable(True, True)
        self.configure(background=COLORS["content"])
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(self.px(self.WIDTH), max(320, screen_width - 40))
        height = min(self.px(self.HEIGHT), max(240, screen_height - 120))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - self.px(30))
        self.geometry(f"{width}x{height}+{x}+{y}")

        config = self.original
        self.dirty = False
        self.capturing = False
        self.capture_bind_id: str | None = None
        self.api_key_visible = False

        # State that is edited by the pages and read back by save().
        self.api_key = StringVar(value=config.api_key)
        self.model = StringVar(value=config.model)
        self.language = StringVar(value=config.language)
        self.shortcut = StringVar(value=config.shortcut)
        self.custom_words: list[str] = list(config.custom_words)
        self.word_replacements: list[tuple[str, str]] = list(config.word_replacements)
        self.device_options = controller.list_input_devices()
        self.device_labels = {device_id: label for device_id, label in self.device_options}
        self.input_device = StringVar(value=self.device_labels.get(config.input_device, DEFAULT_DEVICE_LABEL))
        self.paste = BooleanVar(value=config.paste_after_transcription)
        self.remove_period = BooleanVar(value=config.remove_final_period)
        self.autostart = BooleanVar(value=config.autostart or controller.autostart_enabled())
        self.status = StringVar(value="Wijzigingen worden bewaard zodra je op Opslaan klikt.")

        self._build_shell()
        self._build_pages()
        self.select_page("dictate" if config.api_key else "connection")
        for variable in (self.api_key, self.model, self.language, self.shortcut, self.input_device, self.paste, self.remove_period, self.autostart):
            variable.trace_add("write", lambda *_args: self.mark_dirty())
        self.prompt_text.bind("<<Modified>>", self._on_prompt_modified)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        # Specific bindings win over the generic <KeyPress> capture binding, so
        # route them back into the capture while it is listening.
        self.bind("<Escape>", lambda event: self._capture_key(event) if self.capturing else self.cancel())
        self.bind("<Control-s>", lambda event: self._capture_key(event) if self.capturing else self.save())

    def px(self, logical: int) -> int:
        return max(1, round(logical * self.scale))

    # -- layout --------------------------------------------------------------

    def _build_shell(self) -> None:
        shell = ttk.Frame(self, style="Shell.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(shell, style="Shell.TFrame", padding=(22, 26, 22, 22), width=self.px(232))
        sidebar.grid(row=0, column=0, rowspan=2, sticky="ns")
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="groq / dictation", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="Van stem naar tekst", style="Side.TLabel").pack(anchor="w", pady=(2, 28))
        self.navigation = ttk.Frame(sidebar, style="Shell.TFrame")
        self.navigation.pack(fill="x")
        ttk.Label(sidebar, text=f"Versie {self.controller.app_version}", style="Side.TLabel").pack(side="bottom", anchor="w")

        content = ttk.Frame(shell, padding=(32, 26, 32, 8))
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        self.page_title = StringVar()
        self.page_subtitle = StringVar()
        ttk.Label(content, textvariable=self.page_title, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(content, textvariable=self.page_subtitle, style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 18))
        self.page_host = ttk.Frame(content)
        self.page_host.grid(row=2, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)

        footer_wrap = ttk.Frame(shell, style="Footer.TFrame")
        footer_wrap.grid(row=1, column=1, sticky="ew")
        footer_wrap.columnconfigure(0, weight=1)
        ttk.Separator(footer_wrap).grid(row=0, column=0, sticky="ew")
        footer = ttk.Frame(footer_wrap, style="Footer.TFrame", padding=(32, 14, 32, 16))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(footer, textvariable=self.status, style="Status.TLabel", wraplength=self.px(400), justify="left")
        self.status_label.grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(footer, style="Footer.TFrame")
        buttons.grid(row=0, column=1, sticky="e")
        ttk.Button(buttons, text="Annuleren", command=self.cancel).pack(side="left", padx=(0, 8))
        self.save_button = ttk.Button(buttons, text="Opslaan", style="Accent.TButton", command=self.save)
        self.save_button.pack(side="left")

    def _build_pages(self) -> None:
        self.pages: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, ttk.Button] = {}
        builders = {
            "dictate": self._build_dictate_page,
            "history": self._build_history_page,
            "recognition": self._build_recognition_page,
            "dictionary": self._build_dictionary_page,
            "connection": self._build_connection_page,
            "about": self._build_about_page,
        }
        for key, label, _subtitle in self.PAGES:
            page = ttk.Frame(self.page_host)
            page.grid(row=0, column=0, sticky="nsew")
            page.columnconfigure(0, weight=1)
            builders[key](page)
            self.pages[key] = page
            button = ttk.Button(self.navigation, text=label, style="Nav.TButton", command=lambda k=key: self.select_page(k))
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button

    def select_page(self, key: str) -> None:
        for page_key, page in self.pages.items():
            if page_key == key:
                page.grid()
            else:
                page.grid_remove()
            self.nav_buttons[page_key].state(["selected"] if page_key == key else ["!selected"])
        for page_key, label, subtitle in self.PAGES:
            if page_key == key:
                self.page_title.set(label)
                self.page_subtitle.set(subtitle)
        self.current_page = key

    # -- pages ---------------------------------------------------------------

    def _build_dictate_page(self, page: ttk.Frame) -> None:
        shortcut_card = Card(
            page,
            "Shortcut",
            "Eenmaal drukken start de opname, nogmaals drukken stopt. De combinatie komt niet in je tekst terecht.",
        )
        shortcut_card.grid(row=0, column=0, sticky="ew")
        body = shortcut_card.body
        body.columnconfigure(0, weight=1)
        self.shortcut_entry = ttk.Entry(body, textvariable=self.shortcut, font=("Consolas", 11))
        self.shortcut_entry.grid(row=0, column=0, sticky="ew")
        self.capture_button = ttk.Button(body, text="Wijzig", command=self.toggle_capture)
        self.capture_button.grid(row=0, column=1, padx=(10, 0))
        self.shortcut_hint = ttk.Label(
            body,
            text="Tip: alt+z, ctrl+shift+space of een losse toets zoals insert of f9.",
            style="CardMuted.TLabel",
        )
        self.shortcut_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        mic_card = Card(page, "Microfoon", "Standaard volgt de app de Windows-instelling voor opname.")
        mic_card.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        body = mic_card.body
        body.columnconfigure(0, weight=1)
        self.device_combo = ttk.Combobox(body, textvariable=self.input_device, values=[label for _, label in self.device_options], state="readonly")
        self.device_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(body, text="Vernieuwen", command=self.refresh_devices).grid(row=0, column=1, padx=(10, 0))
        ttk.Button(body, text="Geluiden testen", command=self.controller.test_sounds).grid(row=0, column=2, padx=(8, 0))

        behaviour_card = Card(page, "Gedrag")
        behaviour_card.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        body = behaviour_card.body
        ttk.Checkbutton(body, text="Transcriptie automatisch plakken", variable=self.paste, style="Switch.TCheckbutton").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(body, text="Punt aan het einde verwijderen", variable=self.remove_period, style="Switch.TCheckbutton").grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(body, text="Start automatisch met Windows", variable=self.autostart, style="Switch.TCheckbutton").grid(row=2, column=0, sticky="w")

    def _build_history_page(self, page: ttk.Frame) -> None:
        page.rowconfigure(0, weight=1)
        self.history_scroller = ScrollableFrame(page, COLORS["content"])
        self.history_scroller.grid(row=0, column=0, sticky="nsew")
        self.history_host = self.history_scroller.inner

        footer = ttk.Frame(page)
        footer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Alleen op deze pc bewaard, in de map met je instellingen. Oudere transcripties verdwijnen automatisch.",
            style="Muted.TLabel",
            wraplength=self.px(430),
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        self.clear_history_button = ttk.Button(footer, text="Geschiedenis wissen", command=self.clear_history)
        self.clear_history_button.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.refresh_history()

    def refresh_history(self) -> None:
        """Rebuild the history list; called after every new transcription."""
        if not self.winfo_exists():
            return
        for child in self.history_host.winfo_children():
            child.destroy()
        entries = self.controller.history_entries()
        self.clear_history_button.state(["!disabled"] if entries else ["disabled"])
        if not entries:
            empty = Card(self.history_host, "Nog geen transcripties", "Dicteer iets met je shortcut. De tekst verschijnt hier zodra hij is geplakt.")
            empty.grid(row=0, column=0, sticky="ew")
            return

        for index, entry in enumerate(entries):
            row = ttk.Frame(self.history_host, style="Card.TFrame", padding=(14, 10, 14, 12))
            row.grid(row=index, column=0, sticky="ew", pady=(0 if index == 0 else 8, 0))
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=entry.label(), style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
            copy_button = ttk.Button(row, text="Kopiëren", style="Ghost.TButton")
            copy_button.configure(command=lambda e=entry, b=copy_button: self.copy_history_entry(e, b))
            copy_button.grid(row=0, column=1, sticky="e")
            lines = max(1, min(3, (len(entry.text) // 80) + 1 + entry.text.count("\n")))
            text = bordered_text(row, height=lines)
            text.insert("1.0", entry.text)
            text.configure(state="disabled", cursor="arrow")
            text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def copy_history_entry(self, entry: HistoryEntry, button: ttk.Button) -> None:
        try:
            self.controller.copy_text(entry.text)
        except Exception as exc:
            self.set_status(f"Kopiëren mislukt: {exc}")
            return
        button.configure(text="Gekopieerd ✓")
        self.set_status("Transcriptie staat op je klembord.")

        def restore() -> None:
            if button.winfo_exists():
                button.configure(text="Kopiëren")

        self.after(1500, restore)

    def clear_history(self) -> None:
        if not messagebox.askyesno(self.controller.app_name, "Alle bewaarde transcripties verwijderen?", parent=self):
            return
        self.controller.clear_history()
        self.refresh_history()
        self.set_status("Geschiedenis gewist.")

    def _build_recognition_page(self, page: ttk.Frame) -> None:
        language_card = Card(page, "Taal", "Een vaste taal maakt de herkenning sneller en betrouwbaarder. Laat leeg om Groq de taal te laten raden.")
        language_card.grid(row=0, column=0, sticky="ew")
        body = language_card.body
        body.columnconfigure(0, weight=1)
        language_values = [code for code, _ in LANGUAGE_OPTIONS if code]
        self.language_combo = ttk.Combobox(body, textvariable=self.language, values=language_values)
        self.language_combo.grid(row=0, column=0, sticky="ew")
        self.language_hint = ttk.Label(body, text="", style="CardMuted.TLabel")
        self.language_hint.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.language.trace_add("write", lambda *_args: self._update_language_hint())
        self._update_language_hint()

        prompt_card = Card(
            page,
            "Prompt",
            "Optionele context voor Whisper, bijvoorbeeld het onderwerp of de schrijfstijl. "
            "Prompt en woordenboek delen samen ongeveer 224 tokens.",
        )
        prompt_card.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        page.rowconfigure(1, weight=1)
        body = prompt_card.body
        body.rowconfigure(0, weight=1)
        self.prompt_text = bordered_text(body, height=6)
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        self.prompt_text.insert("1.0", self.original.prompt)
        self.prompt_text.edit_modified(False)
        self.prompt_counter = ttk.Label(body, text="", style="CardMuted.TLabel")
        self.prompt_counter.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._update_prompt_counter()

    def _build_dictionary_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1, uniform="dictionary")
        page.columnconfigure(1, weight=1, uniform="dictionary")
        page.rowconfigure(0, weight=1)

        words_card = Card(
            page,
            "Woorden",
            "Namen en vaktermen die Groq als spellinghint meekrijgt, zoals Groq of Clinon.",
        )
        words_card.grid(row=0, column=0, sticky="nsew")
        body = words_card.body
        body.rowconfigure(1, weight=1)
        entry_row = ttk.Frame(body, style="CardBody.TFrame")
        entry_row.grid(row=0, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        self.word_value = StringVar()
        self.word_entry = ttk.Entry(entry_row, textvariable=self.word_value)
        self.word_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(entry_row, text="Toevoegen", command=self.add_word).grid(row=0, column=1, padx=(8, 0))
        self.word_entry.bind("<Return>", lambda _event: (self.add_word(), "break")[1])
        self.words_editor = ListEditor(
            body,
            self.custom_words,
            str,
            lambda count: f"{count} van {MAX_CUSTOM_WORDS} woorden",
            self.mark_dirty,
        )
        self.words_editor.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        replacements_card = Card(
            page,
            "Vervangingen",
            "Vaste correcties die na de transcriptie worden toegepast, bijvoorbeeld Grok → Groq.",
        )
        replacements_card.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        body = replacements_card.body
        body.rowconfigure(1, weight=1)
        form = ttk.Frame(body, style="CardBody.TFrame")
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.source_value = StringVar()
        self.target_value = StringVar()
        ttk.Label(form, text="Verkeerd", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.source_entry = ttk.Entry(form, textvariable=self.source_value)
        self.source_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(form, text="Correct", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        target_entry = ttk.Entry(form, textvariable=self.target_value)
        target_entry.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(form, text="Toevoegen", command=self.add_replacement).grid(row=2, column=1, sticky="e", pady=(8, 0))
        self.source_entry.bind("<Return>", lambda _event: (target_entry.focus_set(), "break")[1])
        target_entry.bind("<Return>", lambda _event: (self.add_replacement(), "break")[1])
        self.replacements_editor = ListEditor(
            body,
            self.word_replacements,
            lambda pair: f"{pair[0]}  →  {pair[1]}",
            lambda count: f"{count} van {MAX_WORD_REPLACEMENTS} vervangingen",
            self.mark_dirty,
        )
        self.replacements_editor.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

    def _build_connection_page(self, page: ttk.Frame) -> None:
        key_card = Card(
            page,
            "Groq API key",
            "Maak een sleutel aan op console.groq.com/keys. De sleutel wordt veilig opgeslagen in Windows Credential Manager.",
        )
        key_card.grid(row=0, column=0, sticky="ew")
        body = key_card.body
        body.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(body, textvariable=self.api_key, show="•")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.reveal_button = ttk.Button(body, text="Tonen", command=self.toggle_api_key_visibility)
        self.reveal_button.grid(row=0, column=1, padx=(10, 0))
        actions = ttk.Frame(body, style="CardBody.TFrame")
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        actions.columnconfigure(1, weight=1)
        self.test_button = ttk.Button(actions, text="Verbinding testen", style="Ghost.TButton", command=self.test_connection)
        self.test_button.grid(row=0, column=0, sticky="w")
        self.connection_result = ttk.Label(actions, text="", style="CardMuted.TLabel", wraplength=self.px(380), justify="left")
        self.connection_result.grid(row=0, column=1, sticky="w", padx=(12, 0))

        model_card = Card(page, "Model", "Beide modellen draaien bij Groq. Wissel gerust; je instellingen blijven verder gelijk.")
        model_card.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        body = model_card.body
        body.columnconfigure(0, weight=1)
        self.model_combo = ttk.Combobox(body, textvariable=self.model, values=[name for name, _ in MODEL_OPTIONS], state="readonly")
        self.model_combo.grid(row=0, column=0, sticky="ew")
        self.model_hint = ttk.Label(body, text="", style="CardMuted.TLabel", wraplength=self.px(540), justify="left")
        self.model_hint.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.model.trace_add("write", lambda *_args: self._update_model_hint())
        self._update_model_hint()

    def _build_about_page(self, page: ttk.Frame) -> None:
        version_card = Card(
            page,
            f"{self.controller.app_name} {self.controller.app_version}",
            "Dicteer overal in Windows met Groq Whisper. Updates vervangen alleen de app; je sleutel en instellingen blijven staan.",
        )
        version_card.grid(row=0, column=0, sticky="ew")
        body = version_card.body
        ttk.Button(body, text="Controleren op updates", command=self.controller.check_for_updates_manual).grid(row=0, column=0, sticky="w")

        tools_card = Card(page, "Hulpmiddelen", "Handig als iets niet doet wat je verwacht.")
        tools_card.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        body = tools_card.body
        row = ttk.Frame(body, style="CardBody.TFrame")
        row.grid(row=0, column=0, sticky="w")
        ttk.Button(row, text="Logbestand openen", command=self.controller.open_log).pack(side="left")
        ttk.Button(row, text="App herstarten", command=self.controller.restart).pack(side="left", padx=(8, 0))
        ttk.Label(
            body,
            text=f"Instellingen en logboek staan in {self.controller.app_dir}",
            style="CardMuted.TLabel",
            wraplength=self.px(540),
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(12, 0))

    # -- helpers -------------------------------------------------------------

    def mark_dirty(self) -> None:
        if self.dirty:
            return
        self.dirty = True
        self.status.set("Je hebt niet-opgeslagen wijzigingen.")
        self.status_label.configure(style="StatusDirty.TLabel")

    def set_status(self, message: str, *, dirty_style: bool | None = None) -> None:
        self.status.set(message)
        if dirty_style is not None:
            self.status_label.configure(style="StatusDirty.TLabel" if dirty_style else "Status.TLabel")

    def _on_prompt_modified(self, _event=None) -> None:
        if self.prompt_text.edit_modified():
            self.prompt_text.edit_modified(False)
            self._update_prompt_counter()
            self.mark_dirty()

    def prompt_value(self) -> str:
        return self.prompt_text.get("1.0", "end-1c").strip()

    def _update_prompt_counter(self) -> None:
        length = len(self.prompt_value())
        self.prompt_counter.configure(text=f"{length} tekens" if length else "Nog geen prompt ingesteld.")

    def _update_language_hint(self) -> None:
        code = self.language.get().strip().lower()
        names = dict(LANGUAGE_OPTIONS)
        if code in names:
            self.language_hint.configure(text=names[code] if code else "Automatisch herkennen (iets trager).")
        else:
            self.language_hint.configure(text="ISO-taalcode, bijvoorbeeld nl, en of de.")

    def _update_model_hint(self) -> None:
        self.model_hint.configure(text=dict(MODEL_OPTIONS).get(self.model.get(), ""))

    def refresh_devices(self) -> None:
        current_label = self.input_device.get()
        self.device_options = self.controller.list_input_devices()
        self.device_labels = {device_id: label for device_id, label in self.device_options}
        labels = [label for _, label in self.device_options]
        self.device_combo.configure(values=labels)
        if current_label not in labels:
            self.input_device.set(DEFAULT_DEVICE_LABEL)
        self.set_status(f"{max(0, len(labels) - 1)} microfoon(s) gevonden.")

    def selected_device_id(self) -> str:
        selected = self.input_device.get()
        for device_id, label in self.device_options:
            if label == selected:
                return device_id
        prefix = selected.split(":", 1)[0]
        return prefix if selected and prefix.isdigit() else ""

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
        self.capture_button.configure(text="Luistert…")
        self.shortcut_entry.state(["disabled"])
        self.shortcut_hint.configure(text="Druk nu de gewenste toetscombinatie. Esc annuleert.")
        self.set_status("Druk nu op de gewenste shortcut. Esc annuleert.")
        self.capture_bind_id = self.bind("<KeyPress>", self._capture_key, add="+")
        self.focus_force()

    def _capture_key(self, event) -> str:
        if event.keysym == "Escape":
            self.stop_capture(cancelled=True)
            return "break"
        value = hotkey_from_tk_event(event)
        if value is None:
            return "break"
        self.stop_capture(cancelled=False)
        self.shortcut.set(value)
        self.set_status(f"Shortcut ingesteld op {value}. Klik op Opslaan om te bewaren.")
        return "break"

    def stop_capture(self, *, cancelled: bool) -> None:
        if self.capture_bind_id is not None:
            self.unbind("<KeyPress>", self.capture_bind_id)
            self.capture_bind_id = None
        self.capturing = False
        self.capture_button.configure(text="Wijzig")
        self.shortcut_entry.state(["!disabled"])
        self.shortcut_hint.configure(text="Tip: alt+z, ctrl+shift+space of een losse toets zoals insert of f9.")
        try:
            self.controller.resume_hotkey()
        except Exception as exc:
            self.set_status(f"Kon de oude shortcut niet terugzetten: {exc}")
            return
        if cancelled:
            self.set_status("Shortcut wijzigen geannuleerd.")

    # -- dictionary ----------------------------------------------------------

    def add_word(self) -> None:
        try:
            word = normalize_custom_word(self.word_value.get())
            if any(existing.casefold() == word.casefold() for existing in self.custom_words):
                raise DictionaryValidationError(f"'{word}' staat al in het woordenboek.")
            candidate = normalize_custom_words([*self.custom_words, word])
            compose_transcription_prompt(self.prompt_value(), candidate)
        except DictionaryValidationError as exc:
            messagebox.showerror(self.controller.app_name, str(exc), parent=self)
            return
        self.words_editor.append(word)
        self.word_value.set("")
        self.word_entry.focus_set()

    def add_replacement(self) -> None:
        try:
            source = normalize_replacement_part(self.source_value.get(), "verkeerd herkende")
            target = normalize_replacement_part(self.target_value.get(), "correcte")
            candidate = normalize_word_replacements([*self.word_replacements, (source, target)])
            if len(candidate) == len(self.word_replacements):
                raise DictionaryValidationError(f"Voor '{source}' bestaat al een vervanging.")
        except DictionaryValidationError as exc:
            messagebox.showerror(self.controller.app_name, str(exc), parent=self)
            return
        self.replacements_editor.append((source, target))
        self.source_value.set("")
        self.target_value.set("")
        self.source_entry.focus_set()

    # -- connection ----------------------------------------------------------

    def toggle_api_key_visibility(self) -> None:
        self.api_key_visible = not self.api_key_visible
        self.api_key_entry.configure(show="" if self.api_key_visible else "•")
        self.reveal_button.configure(text="Verbergen" if self.api_key_visible else "Tonen")

    def test_connection(self) -> None:
        api_key = self.api_key.get().strip()
        if not api_key:
            self.connection_result.configure(text="Vul eerst een API key in.", style="CardDanger.TLabel")
            return
        self.test_button.state(["disabled"])
        self.connection_result.configure(text="Verbinden met Groq…", style="CardMuted.TLabel")

        def run() -> None:
            try:
                message = self.controller.test_api_key(api_key)
                style = "CardSuccess.TLabel"
            except Exception as exc:
                message = f"Verbinding mislukt: {exc}"
                style = "CardDanger.TLabel"

            def show() -> None:
                if not self.winfo_exists():
                    return
                self.test_button.state(["!disabled"])
                self.connection_result.configure(text=message, style=style)

            self.after(0, show)

        threading.Thread(target=run, daemon=True).start()

    # -- save / cancel -------------------------------------------------------

    def build_config(self):
        normalized_shortcut = normalize_hotkey_text(self.shortcut.get()) or "insert"
        validate_hotkey(normalized_shortcut)
        normalized_words = normalize_custom_words(self.custom_words)
        normalized_replacements = normalize_word_replacements(self.word_replacements)
        prompt = self.prompt_value()
        compose_transcription_prompt(prompt, normalized_words)
        entered_api_key = self.api_key.get().strip()
        return dataclasses.replace(
            self.original,
            api_key=entered_api_key,
            model=self.model.get().strip() or MODEL_OPTIONS[0][0],
            language=self.language.get().strip().lower(),
            prompt=prompt,
            custom_words=normalized_words,
            word_replacements=normalized_replacements,
            shortcut=normalized_shortcut,
            input_device=self.selected_device_id(),
            paste_after_transcription=self.paste.get(),
            remove_final_period=self.remove_period.get(),
            autostart=self.autostart.get(),
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
            messagebox.showerror(self.controller.app_name, str(exc), parent=self)
            return
        except Exception as exc:
            messagebox.showerror(self.controller.app_name, f"Instellingen zijn ongeldig:\n{exc}", parent=self)
            return

        try:
            self.controller.apply_settings(new_config)
        except HotkeyError as exc:
            self.select_page("dictate")
            messagebox.showerror(self.controller.app_name, str(exc), parent=self)
            return
        except Exception as exc:
            messagebox.showerror(self.controller.app_name, f"Instellingen konden niet worden opgeslagen:\n{exc}", parent=self)
            return

        self.dirty = False
        self.destroy()

    def cancel(self) -> None:
        if self.capturing:
            self.stop_capture(cancelled=True)
        if self.dirty and not messagebox.askyesno(
            self.controller.app_name,
            "Je hebt wijzigingen die nog niet zijn opgeslagen. Wil je ze verwerpen?",
            parent=self,
        ):
            return
        self.destroy()
