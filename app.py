import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_VERSION = "0.1.15"
if __name__ == "__main__" and "--version" in sys.argv:
    print(APP_VERSION)
    raise SystemExit(0)

from tkinter import END, BooleanVar, Canvas, Listbox, StringVar, Tk, Toplevel, messagebox, ttk

import keyboard
import keyring
import numpy as np
import pyautogui
import pyperclip
import pystray
import sounddevice as sd
from groq import Groq
from PIL import Image, ImageDraw

from dictation_core import (
    DictionaryValidationError,
    apply_word_replacements,
    compose_transcription_prompt,
    normalize_custom_word,
    normalize_custom_words,
    normalize_replacement_part,
    normalize_word_replacements,
)

try:
    import winsound
except ImportError:  # pragma: no cover - Windows-only nicety
    winsound = None


APP_NAME = "Groq Insert Dictation"
APP_SLUG = "GroqInsertDictation"
GITHUB_REPO = "brvale97/groq-windows"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
KEYRING_SERVICE = APP_SLUG
KEYRING_USER = "groq_api_key"
_INSTANCE_MUTEX_HANDLE = None
_INSTANCE_LOCK_FILE = None
MIN_TRANSCRIPTION_SECONDS = 1.0
MIN_TRANSCRIPTION_BYTES = 32_000
POST_STOP_RECORDING_SECONDS = 0.15
_SOUNDS_READY = False
_SOUNDS_LOCK = threading.Lock()
BUBBLE_COLORS = {
    "idle": "#E81123",
    "recording": "#ff2e3d",
    "processing": "#E81123",
}
SPLASH_MIN_VISIBLE_MS = 900
SPLASH_READY_VISIBLE_MS = 350
SPLASH_TRAY_TIMEOUT_MS = 10_000


def app_data_dir() -> Path:
    root = os.getenv("APPDATA")
    if root:
        return Path(root) / APP_SLUG
    return Path.home() / f".{APP_SLUG}"


def centered_window_geometry(width: int, height: int, screen_width: int, screen_height: int) -> str:
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return f"{width}x{height}+{x}+{y}"


APP_DIR = app_data_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
LOG_PATH = APP_DIR / "app.log"
SOUNDS_DIR = APP_DIR / "sounds"


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    if any(getattr(handler, "_groq_dictation_handler", False) for handler in root_logger.handlers):
        return

    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    handler._groq_dictation_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def acquire_single_instance_lock() -> bool:
    if os.name != "nt":
        return True

    import ctypes
    import ctypes.wintypes
    import msvcrt

    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE

    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = APP_DIR / "instance.lock"
    _INSTANCE_LOCK_FILE = lock_path.open("a+b")
    try:
        _INSTANCE_LOCK_FILE.seek(0)
        msvcrt.locking(_INSTANCE_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        _INSTANCE_LOCK_FILE.close()
        _INSTANCE_LOCK_FILE = None
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetLastError.argtypes = [ctypes.wintypes.DWORD]
    kernel32.SetLastError.restype = None
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    kernel32.SetLastError(0)
    _INSTANCE_MUTEX_HANDLE = kernel32.CreateMutexW(None, True, f"Local\\{APP_SLUG}SingleInstance")
    if not _INSTANCE_MUTEX_HANDLE:
        _INSTANCE_LOCK_FILE.close()
        _INSTANCE_LOCK_FILE = None
        return False

    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        _INSTANCE_MUTEX_HANDLE = None
        _INSTANCE_LOCK_FILE.close()
        _INSTANCE_LOCK_FILE = None
        return False

    return True


@dataclass
class Config:
    api_key: str = ""
    model: str = "whisper-large-v3-turbo"
    language: str = "nl"
    prompt: str = ""
    custom_words: tuple[str, ...] = ()
    word_replacements: tuple[tuple[str, str], ...] = ()
    shortcut: str = "insert"
    input_device: str = ""
    sample_rate: int = 16_000
    channels: int = 1
    paste_after_transcription: bool = True
    autostart: bool = True
    keyring_read_succeeded: bool = field(default=True, repr=False, compare=False)


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def try_read_api_key_from_keyring() -> tuple[bool, str]:
    try:
        return True, keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception as exc:
        logging.warning("Could not read API key from keyring: %s", exc)
        return False, ""


def write_api_key_to_keyring(api_key: str) -> bool:
    try:
        if api_key:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, api_key)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except keyring.errors.PasswordDeleteError:
                pass
        return True
    except Exception as exc:
        logging.warning("Could not write API key to keyring: %s", exc)
        return False


def positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def load_config() -> Config:
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
            else:
                logging.warning("Ignoring settings file because its root is not an object.")
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Ignoring invalid settings file: %s", exc)

    env = load_dotenv_values(Path(".env"))
    raw_custom_words = data.get("custom_words", ())
    if not isinstance(raw_custom_words, (list, tuple)):
        raw_custom_words = ()
    raw_word_replacements = data.get("word_replacements", ())
    if not isinstance(raw_word_replacements, (list, tuple)):
        raw_word_replacements = ()
    keyring_read_succeeded, keyring_api_key = try_read_api_key_from_keyring()
    config = Config(
        api_key="",
        model=str(data.get("model") or env.get("GROQ_MODEL") or "whisper-large-v3-turbo"),
        language=str(data.get("language") if data.get("language") is not None else env.get("GROQ_LANGUAGE", "nl")),
        prompt=str(data.get("prompt") if data.get("prompt") is not None else env.get("GROQ_PROMPT", "")),
        custom_words=normalize_custom_words(raw_custom_words, strict=False),
        word_replacements=normalize_word_replacements(raw_word_replacements, strict=False),
        shortcut=str(data.get("shortcut") or env.get("DICTATION_SHORTCUT") or "insert"),
        input_device=str(
            data.get("input_device") if data.get("input_device") is not None else env.get("DICTATION_INPUT_DEVICE", "")
        ),
        sample_rate=positive_int(data.get("sample_rate") or env.get("DICTATION_SAMPLE_RATE"), 16_000),
        channels=positive_int(data.get("channels") or env.get("DICTATION_CHANNELS"), 1),
        paste_after_transcription=bool(
            data.get("paste_after_transcription")
            if "paste_after_transcription" in data
            else env.get("PASTE_AFTER_TRANSCRIPTION", "true").lower() in {"1", "true", "yes", "on"}
        ),
        autostart=bool(data.get("autostart", True)),
        keyring_read_succeeded=keyring_read_succeeded,
    )

    config.api_key = (
        keyring_api_key
        or data.get("api_key", "")
        or env.get("GROQ_API_KEY", "")
        or os.getenv("GROQ_API_KEY", "")
    ).strip()
    return config


def save_config(config: Config, *, allow_keyring_mutation: bool | None = None) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if allow_keyring_mutation is None:
        allow_keyring_mutation = config.keyring_read_succeeded
    data = asdict(config)
    api_key = data.pop("api_key", "")
    data.pop("keyring_read_succeeded", None)
    if allow_keyring_mutation:
        keyring_available, stored_api_key = try_read_api_key_from_keyring()
    else:
        keyring_available, stored_api_key = False, ""
    should_write_keyring = (
        allow_keyring_mutation
        and bool(api_key)
        and (not keyring_available or stored_api_key != api_key)
    )
    should_delete_keyring = (
        allow_keyring_mutation
        and not api_key
        and (not keyring_available or bool(stored_api_key))
    )
    if (should_write_keyring or should_delete_keyring) and not write_api_key_to_keyring(api_key):
        if api_key:
            data["api_key"] = api_key
    elif not allow_keyring_mutation and api_key:
        # Preserve a legacy/settings fallback until a later startup can verify
        # Credential Manager. It may be the only recoverable copy of the key.
        data["api_key"] = api_key
    serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if SETTINGS_PATH.exists() and SETTINGS_PATH.read_text(encoding="utf-8") == serialized:
        return

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=APP_DIR,
            prefix="settings-",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp.write(serialized)
            temp.flush()
            os.fsync(temp.fileno())
            temp_path = Path(temp.name)
        os.replace(temp_path, SETTINGS_PATH)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def startup_cmd_path() -> Path:
    return (
        Path(os.getenv("APPDATA", str(Path.home())))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / f"{APP_SLUG}.cmd"
    )


def current_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'start "" "{sys.executable}"'

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else Path(sys.executable)
    return f'start "" "{launcher}" "{Path(__file__).resolve()}"'


def set_autostart(enabled: bool) -> None:
    path = startup_cmd_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"@echo off\r\n{current_launch_command()}\r\n".encode("utf-8")
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
    else:
        path.unlink(missing_ok=True)


def autostart_enabled() -> bool:
    return startup_cmd_path().exists()


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    url: str
    asset_name: str
    download_url: str


def parse_version(value: str) -> tuple[int, ...]:
    clean = value.strip().lower().lstrip("v")
    parts: list[int] = []
    for part in clean.split("."):
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits or "0"))
    return tuple(parts)


def is_newer_version(candidate: str, current: str = APP_VERSION) -> bool:
    left = parse_version(candidate)
    right = parse_version(current)
    max_len = max(len(left), len(right))
    return left + (0,) * (max_len - len(left)) > right + (0,) * (max_len - len(right))


def fetch_latest_update() -> UpdateInfo | None:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": APP_SLUG,
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        release = json.loads(response.read().decode("utf-8"))

    tag = str(release.get("tag_name", "")).strip()
    if not tag or not is_newer_version(tag):
        return None

    assets = release.get("assets") or []
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.lower() == f"{APP_SLUG}.exe".lower():
            download_url = str(asset.get("browser_download_url", ""))
            if download_url:
                return UpdateInfo(
                    version=tag.lstrip("v"),
                    tag=tag,
                    url=str(release.get("html_url", "")),
                    asset_name=name,
                    download_url=download_url,
                )

    return None


def download_update(update: UpdateInfo) -> Path:
    update_dir = APP_DIR / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    destination = update_dir / f"{APP_SLUG}-{update.tag}.exe"
    partial = destination.with_suffix(".exe.part")
    request = urllib.request.Request(
        update.download_url,
        headers={"User-Agent": APP_SLUG},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            expected_length = response.headers.get("Content-Length")
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
            output.flush()
            os.fsync(output.fileno())

        if expected_length is not None and downloaded != int(expected_length):
            raise RuntimeError(f"Onvolledige update-download: {downloaded} van {expected_length} bytes ontvangen.")
        with partial.open("rb") as downloaded_file:
            header = downloaded_file.read(2)
        if downloaded < 1024 or header != b"MZ":
            raise RuntimeError("Het gedownloade updatebestand is geen geldige Windows-app.")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def current_exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_SLUG / f"{APP_SLUG}.exe"


def launch_update_script(downloaded_exe: Path) -> None:
    target = current_exe_path()
    current_pid = os.getpid()
    log_path = APP_DIR / "update.log"
    ps_script = APP_DIR / "apply-update.ps1"
    cmd_script = APP_DIR / "apply-update.cmd"
    ps_script.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$Source = '{str(downloaded_exe).replace("'", "''")}'",
                f"$Target = '{str(target).replace("'", "''")}'",
                f"$Log = '{str(log_path).replace("'", "''")}'",
                "$Backup = \"$Target.bak\"",
                "$Staged = \"$Target.new\"",
                f"$PidToWait = {current_pid}",
                "function Log($Message) { Add-Content -LiteralPath $Log -Value \"$(Get-Date -Format o) $Message\" }",
                "try {",
                "  Log \"Waiting for process $PidToWait to exit\"",
                "  try { Wait-Process -Id $PidToWait -Timeout 30 -ErrorAction SilentlyContinue } catch {}",
                "  Start-Sleep -Milliseconds 700",
                "  Log \"Staging $Source for $Target\"",
                "  Copy-Item -LiteralPath $Source -Destination $Staged -Force",
                "  if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Force }",
                "  if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $Backup -Force }",
                "  Move-Item -LiteralPath $Staged -Destination $Target -Force",
                "  Log \"Starting $Target\"",
                "  $Env:PYINSTALLER_RESET_ENVIRONMENT = '1'",
                "  Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target)",
                "  Remove-Item -LiteralPath $Source -Force -ErrorAction SilentlyContinue",
                "  Log \"Update complete\"",
                "} catch {",
                "  Log \"Update failed: $($_.Exception.Message)\"",
                "  Remove-Item -LiteralPath $Staged -Force -ErrorAction SilentlyContinue",
                "  if (Test-Path -LiteralPath $Backup) {",
                "    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue",
                "    Move-Item -LiteralPath $Backup -Destination $Target -Force",
                "    Log \"Previous version restored\"",
                "  }",
                "}",
                "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cmd_script.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{ps_script}"',
                'del "%~f0" >nul 2>nul',
                "",
            ]
        ),
        encoding="ascii",
    )

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd.exe", "/c", str(cmd_script)], creationflags=creation_flags)


def create_icon_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=(18, 128, 92, 255))
    draw.rounded_rectangle((29, 16, 35, 43), radius=3, fill=(255, 255, 255, 255))
    draw.arc((22, 28, 42, 50), 0, 180, fill=(255, 255, 255, 255), width=4)
    draw.line((32, 50, 32, 56), fill=(255, 255, 255, 255), width=4)
    return image


def cleanup_confirmed_update_backup() -> None:
    if not getattr(sys, "frozen", False):
        return
    backup = Path(f"{current_exe_path()}.bak")
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        logging.warning("Could not remove confirmed update backup %s: %s", backup, exc)


def make_tone(path: Path, notes: list[float]) -> None:
    sample_rate = 44_100
    note_duration = 0.09
    note_gap = 0.025
    attack_time = 0.015
    max_gain = 0.2
    note_samples = int(note_duration * sample_rate)
    gap_samples = int(note_gap * sample_rate)
    samples: list[int] = []

    for note_index, freq in enumerate(notes):
        decay_duration = note_duration - attack_time
        for i in range(note_samples):
            t = i / sample_rate
            sine = math.sin(2 * math.pi * freq * t)
            if t < attack_time:
                envelope = (t / attack_time) * max_gain
            else:
                decay_progress = (t - attack_time) / decay_duration
                envelope = max_gain * math.pow(0.0001 / max_gain, decay_progress)
            samples.append(int(sine * envelope * 32767))

        if note_index < len(notes) - 1:
            samples.extend([0] * gap_samples)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples))


def ensure_sounds() -> None:
    global _SOUNDS_READY
    if _SOUNDS_READY:
        return

    with _SOUNDS_LOCK:
        if _SOUNDS_READY:
            return
        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        marker = SOUNDS_DIR / ".groqandroid-cues-v1"
        sounds = {
            "start.wav": [523.25, 659.25],
            "success.wav": [587.33, 440.0],
            "error.wav": [246.94, 196.00],
        }
        generated = False
        for filename, notes in sounds.items():
            path = SOUNDS_DIR / filename
            if not path.exists() or not marker.exists():
                make_tone(path, notes)
                generated = True
        if generated or not marker.exists():
            marker.write_text("Generated from GroqAndroid cue parameters.\n", encoding="utf-8")
        _SOUNDS_READY = True


def play_sound(name: str) -> None:
    if winsound is None:
        return
    ensure_sounds()
    path = SOUNDS_DIR / name
    try:
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except RuntimeError as exc:
        logging.warning("Could not play sound %s: %s", name, exc)


def resolve_input_device(input_device: str) -> int | None:
    if not input_device:
        return None

    if input_device.isdigit():
        return int(input_device)

    devices = sd.query_devices()
    needle = input_device.lower()
    for index, device in enumerate(devices):
        if device["max_input_channels"] > 0 and needle in device["name"].lower():
            return index

    raise RuntimeError(f"Geen input device gevonden voor {input_device!r}.")


def input_devices() -> list[tuple[str, str]]:
    devices = [("", "Windows default input")]
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            devices.append((str(index), f"{index}: {device['name']}"))
    return devices


def normalize_tk_key(keysym: str) -> str:
    aliases = {
        "Return": "enter",
        "Escape": "esc",
        "BackSpace": "backspace",
        "Delete": "delete",
        "Insert": "insert",
        "Tab": "tab",
        "space": "space",
        "Prior": "page up",
        "Next": "page down",
        "Control_L": "ctrl",
        "Control_R": "ctrl",
        "Shift_L": "shift",
        "Shift_R": "shift",
        "Alt_L": "alt",
        "Alt_R": "alt",
        "Win_L": "windows",
        "Win_R": "windows",
    }
    return aliases.get(keysym, keysym).lower().replace("_", " ")


def hotkey_from_tk_event(event) -> str | None:
    key = normalize_tk_key(event.keysym)
    if key in {"ctrl", "shift", "alt", "windows"}:
        return None

    parts: list[str] = []
    if event.state & 0x0004:
        parts.append("ctrl")
    if event.state & 0x0001:
        parts.append("shift")
    # Tk on Windows uses extra state bits for extended keys like Delete.
    # Treat only Mod1 as Alt; otherwise bare Delete can be misread as Alt+Delete.
    if event.state & 0x0008:
        parts.append("alt")

    parts.append(key)
    return "+".join(dict.fromkeys(parts))


def normalize_hotkey_text(value: str) -> str:
    aliases = {
        "del": "delete",
        "esc": "esc",
        "escape": "esc",
        "control": "ctrl",
        "ctl": "ctrl",
        "option": "alt",
        "win": "windows",
        "cmd": "windows",
        "return": "enter",
        "pgup": "page up",
        "pgdn": "page down",
    }
    parts = [
        aliases.get(part.strip().lower(), part.strip().lower())
        for part in value.replace("-", "+").split("+")
        if part.strip()
    ]
    return "+".join(dict.fromkeys(parts))


def validate_hotkey(value: str) -> None:
    keyboard.parse_hotkey(value)


def remove_final_sentence_period(text: str) -> str:
    if text.endswith(".") and not text.endswith("..."):
        return text[:-1]
    return text


class StatusBubble:
    def __init__(self, root: Tk, on_click) -> None:
        self.root = root
        self.on_click = on_click
        self.state = "idle"
        self.button_size = 44
        self.window_width = self.button_size
        self.window_height = self.button_size
        self.hide_after_id = None
        self.spinner_after_id = None
        self.spinner_angle = 0
        self.wave_after_id = None
        self.wave_phase = 0
        self.recording_started_at = 0.0
        self.window = Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-toolwindow", True)
        except Exception:
            pass

        self.transparent_color = "#101011"
        self.window.configure(bg=self.transparent_color)
        try:
            self.window.attributes("-transparentcolor", self.transparent_color)
        except Exception:
            pass

        self.canvas = Canvas(
            self.window,
            width=self.window_width,
            height=self.window_height,
            highlightthickness=0,
            bd=0,
            bg=self.transparent_color,
            cursor="hand2",
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", lambda _event: self.on_click())
        self.window.bind("<Button-1>", lambda _event: self.on_click())
        self.root.bind("<Configure>", lambda _event: self.position(), add="+")

        self.position()
        self.set_state("idle", schedule_hide=False)

    def position(self) -> None:
        try:
            screen_width = self.window.winfo_screenwidth()
            screen_height = self.window.winfo_screenheight()
            x = max(0, screen_width - self.window_width - 26)
            y = max(0, screen_height - self.window_height - 82)
            self.window.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")
        except Exception:
            pass

    def show(self) -> None:
        if self.hide_after_id is not None:
            try:
                self.root.after_cancel(self.hide_after_id)
            except Exception:
                pass
            self.hide_after_id = None
        self.position()
        self.window.deiconify()
        self.window.lift()

    def schedule_hide(self) -> None:
        if self.hide_after_id is not None:
            try:
                self.root.after_cancel(self.hide_after_id)
            except Exception:
                pass
        self.hide_after_id = self.root.after(3000, self.hide)

    def stop_spinner(self) -> None:
        if self.spinner_after_id is not None:
            try:
                self.root.after_cancel(self.spinner_after_id)
            except Exception:
                pass
            self.spinner_after_id = None

    def stop_wave(self) -> None:
        if self.wave_after_id is not None:
            try:
                self.root.after_cancel(self.wave_after_id)
            except Exception:
                pass
            self.wave_after_id = None

    def start_spinner(self) -> None:
        self.stop_spinner()

        def tick() -> None:
            if self.state != "processing":
                self.spinner_after_id = None
                return
            self.spinner_angle = (self.spinner_angle + 32) % 360
            self.draw_processing_button()
            self.spinner_after_id = self.root.after(70, tick)

        tick()

    def start_wave(self) -> None:
        self.stop_wave()
        self.recording_started_at = time.perf_counter()

        def tick() -> None:
            if self.state != "recording":
                self.wave_after_id = None
                return
            self.wave_phase = (self.wave_phase + 1) % 1000
            self.draw_recording_pill()
            self.wave_after_id = self.root.after(120, tick)

        tick()

    def hide(self) -> None:
        self.hide_after_id = None
        try:
            self.window.withdraw()
        except Exception:
            pass

    def set_state(self, state: str, schedule_hide: bool = True) -> None:
        self.state = state
        self.stop_spinner()
        self.stop_wave()
        if state == "idle" and schedule_hide:
            self.hide()
            return

        self.window_width = 176 if state == "recording" else 162 if state == "processing" else self.button_size
        self.window_height = 48 if state in {"recording", "processing"} else self.button_size
        self.canvas.configure(width=self.window_width, height=self.window_height)
        if state != "idle":
            self.show()

        self.canvas.delete("all")

        tooltips = {
            "idle": "Klaar",
            "recording": "Opname",
            "processing": "Transcriptie",
        }
        if state == "recording":
            self.draw_recording_pill()
            self.start_wave()
        elif state == "processing":
            self.draw_processing_button()
            self.start_spinner()
        else:
            self.draw_mic_button(state)
        tooltip = tooltips.get(state, tooltips["idle"])
        self.window.title(f"{APP_NAME} - {tooltip}")
        self.position()
        if state == "idle" and schedule_hide:
            self.schedule_hide()

    def show_notice(self, message: str) -> None:
        self.stop_spinner()
        self.stop_wave()
        self.state = "notice"
        self.window_width = 218
        self.window_height = 48
        self.canvas.configure(width=self.window_width, height=self.window_height)
        self.show()
        self.canvas.delete("all")

        self.draw_round_rect(1, 1, self.window_width - 2, self.window_height - 2, 12, "#fff6f7")
        self.draw_round_rect_outline(1, 1, self.window_width - 2, self.window_height - 2, 12, "#f3b6bd")
        self.canvas.create_oval(11, 11, 37, 37, fill="#ffffff", outline="")
        self.canvas.create_text(24, 24, text="!", fill=BUBBLE_COLORS["idle"], font=("Segoe UI", 16, "bold"))
        self.canvas.create_text(
            46,
            24,
            text=message,
            fill="#a11b28",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        self.window.title(f"{APP_NAME} - {message}")
        self.schedule_hide()

    def draw_mic_button(self, state: str) -> None:
        self.draw_round_rect(1, 1, self.button_size - 2, self.button_size - 2, 22, "#fff6f7")
        self.draw_round_rect_outline(1, 1, self.button_size - 2, self.button_size - 2, 22, "#f3b6bd")
        self.draw_mic_icon()

    def draw_processing_button(self) -> None:
        self.canvas.delete("all")
        self.draw_glass_pill()
        self.canvas.create_oval(16, 15, 33, 32, outline="#f3b6bd", width=2)
        self.canvas.create_arc(
            15,
            14,
            34,
            33,
            start=self.spinner_angle,
            extent=105,
            style="arc",
            outline=BUBBLE_COLORS["idle"],
            width=3,
        )
        self.canvas.create_text(
            43,
            24,
            text="Transcriberen",
            fill="#a11b28",
            anchor="w",
            font=("Segoe UI", 8, "bold"),
        )

    def draw_recording_pill(self) -> None:
        self.canvas.delete("all")
        self.draw_glass_pill()
        for index in range(7):
            phase = (self.wave_phase + index * 2) % 12
            distance = abs(phase - 6)
            height = 8 + (6 - distance) * 2
            x = 18 + index * 5
            y_mid = 24
            self.canvas.create_line(
                x,
                y_mid - height / 2,
                x,
                y_mid + height / 2,
                fill="#d92c3a",
                width=3,
                capstyle="round",
            )

        elapsed = max(0, int(time.perf_counter() - self.recording_started_at))
        elapsed_label = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        self.canvas.create_text(70, 24, text=elapsed_label, fill="#a11b28", anchor="w", font=("Segoe UI", 9, "bold"))
        self.draw_round_rect(128, 10, 156, 38, 8, "#ffe4e7")
        self.draw_round_rect_outline(128, 10, 156, 38, 8, "#f3b6bd")
        self.canvas.create_rectangle(138, 20, 146, 28, fill=BUBBLE_COLORS["idle"], outline="")

    def draw_glass_pill(self) -> None:
        self.draw_round_rect(1, 1, self.window_width - 2, self.window_height - 2, 12, "#fff6f7")
        self.draw_round_rect_outline(1, 1, self.window_width - 2, self.window_height - 2, 12, "#f3b6bd")

    def draw_mic_icon(self) -> None:
        center = self.button_size // 2
        self.canvas.create_line(center, 12, center, 25, fill=BUBBLE_COLORS["idle"], width=7, capstyle="round")
        self.canvas.create_line(
            center - 10,
            23,
            center - 10,
            27,
            center - 8,
            32,
            center,
            35,
            center + 8,
            32,
            center + 10,
            27,
            center + 10,
            23,
            fill=BUBBLE_COLORS["idle"],
            width=3,
            capstyle="round",
            joinstyle="round",
            smooth=True,
        )
        self.canvas.create_line(center, 35, center, 40, fill=BUBBLE_COLORS["idle"], width=3, capstyle="round")
        self.canvas.create_line(center - 5, 40, center + 5, 40, fill=BUBBLE_COLORS["idle"], width=3, capstyle="round")

    def draw_round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, fill: str) -> None:
        self.canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline="")
        self.canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline="")
        self.canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, fill=fill, outline="")
        self.canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, fill=fill, outline="")
        self.canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, fill=fill, outline="")
        self.canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, fill=fill, outline="")

    def draw_round_rect_outline(self, x1: int, y1: int, x2: int, y2: int, radius: int, outline: str) -> None:
        self.canvas.create_line(x1 + radius, y1, x2 - radius, y1, fill=outline)
        self.canvas.create_line(x1 + radius, y2, x2 - radius, y2, fill=outline)
        self.canvas.create_line(x1, y1 + radius, x1, y2 - radius, fill=outline)
        self.canvas.create_line(x2, y1 + radius, x2, y2 - radius, fill=outline)
        self.canvas.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90, outline=outline, style="arc")
        self.canvas.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90, outline=outline, style="arc")
        self.canvas.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90, outline=outline, style="arc")
        self.canvas.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90, outline=outline, style="arc")

    def destroy(self) -> None:
        self.stop_spinner()
        self.stop_wave()
        if self.hide_after_id is not None:
            try:
                self.root.after_cancel(self.hide_after_id)
            except Exception:
                pass
            self.hide_after_id = None
        try:
            self.window.destroy()
        except Exception:
            pass


@dataclass(frozen=True)
class RecordingSession:
    input_device: int | None
    sample_rate: int
    channels: int
    model: str
    language: str
    prompt: str
    word_replacements: tuple[tuple[str, str], ...]
    paste_after_transcription: bool
    client: Groq


class StartupSplash:
    width = 380
    height = 170

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.destroyed = False
        self.started_at = time.monotonic()
        self.window = Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(
            centered_window_geometry(
                self.width,
                self.height,
                self.window.winfo_screenwidth(),
                self.window.winfo_screenheight(),
            )
        )

        frame = ttk.Frame(self.window, padding=24, relief="solid", borderwidth=1)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Groq Windows Dictation", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.status = StringVar(value="Wordt geladen in het systeemvak...")
        ttk.Label(frame, textvariable=self.status, font=("Segoe UI", 10)).pack(anchor="w", pady=(12, 8))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x")
        self.progress.start(12)
        ttk.Label(
            frame,
            text="Daarna blijft de app beschikbaar via het icoon rechtsonder.",
            foreground="#555555",
        ).pack(anchor="w", pady=(9, 0))

        # Paint once before synchronous configuration and sound initialization.
        self.root.update_idletasks()
        self.root.update()

    def minimum_time_has_elapsed(self) -> bool:
        elapsed_ms = (time.monotonic() - self.started_at) * 1000
        return elapsed_ms >= SPLASH_MIN_VISIBLE_MS

    def show_ready(self) -> None:
        if self.destroyed:
            return
        self.progress.stop()
        self.status.set("Klaar — actief in het systeemvak")

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self.progress.stop()
        try:
            self.window.destroy()
        except Exception:
            pass


class DictationEngine:
    def __init__(self, config: Config, status_callback=None, state_callback=None) -> None:
        self.config = config
        self.status_callback = status_callback or (lambda message: None)
        self.state_callback = state_callback or (lambda state: None)
        self.input_device = resolve_input_device(config.input_device)
        self.client = Groq(api_key=config.api_key) if config.api_key else None
        self.audio_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.audio_warning: str | None = None
        self.stream: sd.InputStream | None = None
        self.active_session: RecordingSession | None = None
        self.state = "idle"
        self.lock = threading.Lock()
        self.stream_transition_lock = threading.Lock()

        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0

    def update_config(self, config: Config) -> None:
        with self.lock:
            self.config = config
            self.input_device = resolve_input_device(config.input_device)
            self.client = Groq(api_key=config.api_key) if config.api_key else None

    def notify(self, message: str) -> None:
        logging.info(message)
        self.status_callback(message)

    def emit_state(self, state: str) -> None:
        self.state_callback(state)

    def on_shortcut(self) -> None:
        try:
            with self.lock:
                state = self.state

            if state == "idle":
                self.start_recording()
            elif state == "recording":
                self.stop_recording()
            else:
                self.notify("Nog bezig met transcriberen; shortcut genegeerd.")
        except Exception as exc:
            with self.lock:
                self.state = "idle"
            self.emit_state("idle")
            self.notify(f"Kon opname niet starten/stoppen: {exc}")
            play_sound("error.wav")

    def start_recording(self) -> None:
        with self.stream_transition_lock:
            self._start_recording()

    def _start_recording(self) -> None:
        with self.lock:
            if self.state != "idle":
                return
            config = self.config
            if not config.api_key or self.client is None:
                session = None
            else:
                session = RecordingSession(
                    input_device=self.input_device,
                    sample_rate=config.sample_rate,
                    channels=config.channels,
                    model=config.model,
                    language=config.language,
                    prompt=compose_transcription_prompt(config.prompt, config.custom_words),
                    word_replacements=config.word_replacements,
                    paste_after_transcription=config.paste_after_transcription,
                    client=self.client,
                )

            if session is None:
                self.notify("Open Instellingen en vul eerst je Groq API key in.")
                play_sound("error.wav")
                return
            self.state = "recording"
            self.active_session = session
            self.audio_queue = queue.SimpleQueue()
            self.audio_warning = None

        stream: sd.InputStream | None = None
        try:
            stream = sd.InputStream(
                device=session.input_device,
                samplerate=session.sample_rate,
                channels=session.channels,
                dtype="int16",
                callback=self.audio_callback,
            )
            stream.start()
            self.stream = stream
        except Exception:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logging.exception("Could not close failed input stream")
            with self.lock:
                self.state = "idle"
                self.active_session = None
                self.stream = None
            self.emit_state("idle")
            raise

        self.emit_state("recording")
        play_sound("start.wav")
        self.notify("Opname gestart. Gebruik je shortcut opnieuw om te stoppen.")

    def stop_recording(self) -> None:
        with self.stream_transition_lock:
            self._stop_recording()

    def _stop_recording(self) -> None:
        with self.lock:
            if self.state != "recording":
                return
            self.state = "processing"
            stream = self.stream
            self.stream = None
            session = self.active_session
            self.active_session = None
        self.emit_state("processing")

        if stream is not None:
            time.sleep(POST_STOP_RECORDING_SECONDS)
            try:
                stream.stop()
            except Exception as exc:
                logging.warning("Could not stop input stream cleanly: %s", exc)
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    logging.warning("Could not close input stream cleanly: %s", exc)

        captured_frames: list[np.ndarray] = []
        while True:
            try:
                captured_frames.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break

        if self.audio_warning:
            self.notify(f"Audio waarschuwing: {self.audio_warning}")
        self.notify("Opname gestopt. Transcriberen...")
        if session is None:
            raise RuntimeError("Opnamesessie ontbreekt.")
        threading.Thread(
            target=self.transcribe_and_output,
            args=(session, captured_frames),
            daemon=True,
        ).start()

    def audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.audio_warning = str(status)
        self.audio_queue.put(indata.copy())

    def write_wav_and_stats(
        self,
        session: RecordingSession,
        frames: list[np.ndarray],
    ) -> tuple[Path, float, float, float]:
        if not frames:
            raise RuntimeError("Geen audio opgenomen.")

        temp = tempfile.NamedTemporaryFile(
            prefix="groq-insert-dictation-",
            suffix=".wav",
            delete=False,
        )
        temp_path = Path(temp.name)
        temp.close()

        frame_count = 0
        sample_count = 0
        peak_value = 0.0
        sum_of_squares = 0.0
        try:
            with wave.open(str(temp_path), "wb") as wav:
                wav.setnchannels(session.channels)
                wav.setsampwidth(2)
                wav.setframerate(session.sample_rate)
                for frame in frames:
                    wav.writeframesraw(frame.tobytes())
                    values = np.asarray(frame, dtype=np.float64)
                    frame_count += len(frame)
                    sample_count += values.size
                    if values.size:
                        peak_value = max(peak_value, float(np.max(np.abs(values))))
                        sum_of_squares += float(np.sum(np.square(values)))
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        duration = frame_count / session.sample_rate
        peak = peak_value / 32768.0 if sample_count else 0.0
        rms = math.sqrt(sum_of_squares / sample_count) / 32768.0 if sample_count else 0.0
        return temp_path, duration, peak, rms

    def transcribe_and_output(self, session: RecordingSession, frames: list[np.ndarray]) -> None:
        wav_path: Path | None = None
        started_at = time.perf_counter()
        show_idle_bubble = True
        try:
            wav_path, duration, peak, rms = self.write_wav_and_stats(session, frames)
            frames.clear()
            self.notify(f"Audio: {duration:.1f}s, piek {peak:.3f}, rms {rms:.3f}")
            if duration < MIN_TRANSCRIPTION_SECONDS or wav_path.stat().st_size < MIN_TRANSCRIPTION_BYTES:
                self.notify("Transcriptie te kort. Er is niets geplakt.")
                self.emit_state("too_short")
                play_sound("error.wav")
                show_idle_bubble = False
                return

            if peak < 0.01:
                self.notify("Waarschuwing: bijna geen inputvolume gemeten. Check microfoon/device.")

            text = apply_word_replacements(
                self.transcribe(session, wav_path).strip(),
                session.word_replacements,
            )
            text = remove_final_sentence_period(text)
            elapsed = time.perf_counter() - started_at

            if not text:
                self.notify("Geen tekst herkend.")
                play_sound("error.wav")
                return

            if set(text) == {"*"}:
                pyperclip.copy(text)
                self.notify("Groq gaf alleen sterretjes terug. Meestal is dit stilte of de verkeerde microfoon.")
                play_sound("error.wav")
                return

            pyperclip.copy(text)
            self.notify(f"Transcriptie klaar in {elapsed:.1f}s. Tekst staat op je klembord.")

            if session.paste_after_transcription:
                try:
                    pyautogui.hotkey("ctrl", "v")
                    self.notify("Geplakt in het actieve venster.")
                except Exception as exc:
                    self.notify(f"Automatisch plakken mislukte, maar de tekst staat op je klembord: {exc}")

            play_sound("success.wav")
        except Exception as exc:
            self.notify(f"Fout: {exc}")
            play_sound("error.wav")
        finally:
            frames.clear()
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
            with self.lock:
                self.state = "idle"
            if show_idle_bubble:
                self.emit_state("idle")
            self.notify("Klaar. Gebruik je shortcut voor een nieuwe opname.")

    def transcribe(self, session: RecordingSession, wav_path: Path) -> str:
        kwargs = {
            "file": wav_path.open("rb"),
            "model": session.model,
            "response_format": "json",
            "temperature": 0.0,
        }
        if session.language:
            kwargs["language"] = session.language
        if session.prompt:
            kwargs["prompt"] = session.prompt

        with kwargs["file"] as audio_file:
            kwargs["file"] = audio_file
            try:
                transcription = session.client.audio.transcriptions.create(**kwargs)
            except Exception as exc:
                error_text = str(exc).lower()
                if "prompt" in error_text and ("224" in error_text or "token" in error_text):
                    raise RuntimeError(
                        "Prompt en woordenboek zijn samen te lang voor Groq. "
                        "Maak de Prompt korter of verwijder enkele woorden."
                    ) from exc
                raise

        return getattr(transcription, "text", "") or ""

    def shutdown(self) -> None:
        with self.stream_transition_lock:
            self._shutdown()

    def _shutdown(self) -> None:
        with self.lock:
            stream = self.stream
            self.stream = None
            self.active_session = None
            self.state = "idle"
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                try:
                    stream.stop()
                except Exception:
                    pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass


class TrayApp:
    def __init__(self) -> None:
        setup_logging()
        self.root = Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.splash = StartupSplash(self.root)
        self.tray_startup_complete = threading.Event()
        self.tray_startup_error: Exception | None = None
        self.fatal_startup_error: Exception | None = None
        self.startup_finished = False

        try:
            ensure_sounds()
            self.config = load_config()
            self.bubble = StatusBubble(self.root, self.open_settings)
            self.engine = DictationEngine(self.config, self.set_status, self.set_engine_state)
            self.hotkey_handle = None
            self.update_window = None
            self.icon = pystray.Icon(
                APP_SLUG,
                create_icon_image(),
                f"{APP_NAME} v{APP_VERSION}",
                menu=pystray.Menu(
                    pystray.MenuItem("Instellingen", lambda: self.root.after(0, self.open_settings)),
                    pystray.MenuItem("Controleren op updates", lambda: self.root.after(0, self.check_for_updates_manual)),
                    pystray.MenuItem("Geluiden testen", lambda: self.root.after(0, self.test_sounds)),
                    pystray.MenuItem("Logbestand openen", lambda: self.root.after(0, self.open_log)),
                    pystray.MenuItem("App herstarten", lambda: self.root.after(0, self.restart)),
                    pystray.MenuItem("Afsluiten", lambda: self.root.after(0, self.quit)),
                ),
            )
        except Exception:
            self.splash.destroy()
            self.root.destroy()
            raise

    def set_status(self, message: str) -> None:
        try:
            self.icon.title = f"{APP_NAME} - {message[:50]}"
        except Exception:
            pass

    def set_engine_state(self, state: str) -> None:
        if state == "too_short":
            self.root.after(0, lambda: self.bubble.show_notice("Transcriptie te kort"))
        else:
            self.root.after(0, lambda: self.bubble.set_state(state))

    def install_hotkey(self) -> None:
        self.remove_hotkey()
        self.hotkey_handle = keyboard.add_hotkey(self.config.shortcut, self.engine.on_shortcut, suppress=True)
        logging.info("Hotkey installed: %s", self.config.shortcut)

    def remove_hotkey(self) -> None:
        if self.hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_handle)
            except Exception as exc:
                logging.warning("Could not remove previous hotkey: %s", exc)
            self.hotkey_handle = None

    def run(self) -> None:
        # A startup save migrates a non-empty legacy/.env key to Credential
        # Manager, but must never interpret a temporary keyring read failure as
        # an explicit request to delete an existing secret.
        try:
            save_config(self.config)
            set_autostart(self.config.autostart)
            self.install_hotkey()
            self.icon.run_detached(self._setup_tray)
            self.root.after(25, self._poll_startup_ready)
        except Exception:
            self._cleanup_failed_startup()
            raise
        self.root.mainloop()
        if self.fatal_startup_error is not None:
            raise self.fatal_startup_error

    def _setup_tray(self, icon: pystray.Icon) -> None:
        try:
            icon.visible = True
        except Exception as exc:
            self.tray_startup_error = exc
        finally:
            self.tray_startup_complete.set()

    def _poll_startup_ready(self) -> None:
        if self.startup_finished:
            return
        elapsed_ms = (time.monotonic() - self.splash.started_at) * 1000
        if not self.tray_startup_complete.is_set() and elapsed_ms >= SPLASH_TRAY_TIMEOUT_MS:
            self._fail_startup(RuntimeError("Het systeemvak kon niet op tijd worden gestart."))
            return
        if self.tray_startup_complete.is_set() and self.tray_startup_error is not None:
            self._fail_startup(RuntimeError(f"Het systeemvak kon niet worden gestart: {self.tray_startup_error}"))
            return
        if self.tray_startup_complete.is_set() and self.splash.minimum_time_has_elapsed():
            self.splash.show_ready()
            self.root.after(SPLASH_READY_VISIBLE_MS, self._finish_startup)
            return
        self.root.after(25, self._poll_startup_ready)

    def _fail_startup(self, error: Exception) -> None:
        self.fatal_startup_error = error
        self._cleanup_failed_startup()

    def _cleanup_failed_startup(self) -> None:
        self.startup_finished = True
        self.splash.destroy()
        self.remove_hotkey()
        try:
            self.icon.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def _finish_startup(self) -> None:
        if self.startup_finished:
            return
        self.startup_finished = True
        self.splash.destroy()

        if not self.config.api_key:
            self.root.after(250, self.open_settings)
        self.root.after(2500, self.check_for_updates_auto)
        self.root.after(5000, cleanup_confirmed_update_backup)

    def check_for_updates_auto(self) -> None:
        if not getattr(sys, "frozen", False):
            return
        self.check_for_updates(show_no_update=False)

    def check_for_updates_manual(self) -> None:
        self.check_for_updates(show_no_update=True)

    def check_for_updates(self, show_no_update: bool) -> None:
        def run() -> None:
            try:
                update = fetch_latest_update()
            except Exception as exc:
                logging.warning("Update check failed: %s", exc)
                if show_no_update:
                    self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"Update-check mislukt:\n{exc}"))
                return

            if update is None:
                if show_no_update:
                    self.root.after(
                        0,
                        lambda: messagebox.showinfo(
                            APP_NAME,
                            f"Je gebruikt de nieuwste versie: v{APP_VERSION}.",
                        ),
                    )
                return

            self.root.after(0, lambda: self.show_update_window(update))

        threading.Thread(target=run, daemon=True).start()

    def show_update_window(self, update: UpdateInfo) -> None:
        if self.update_window is not None and self.update_window.winfo_exists():
            self.update_window.lift()
            self.update_window.focus_force()
            return

        window = Toplevel(self.root)
        self.update_window = window
        window.title("Nieuwe versie beschikbaar")
        window.geometry("480x220")
        window.resizable(False, False)

        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=f"Er is een nieuwe versie beschikbaar: {update.tag}",
            font=("", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(
            frame,
            text=(
                f"Je gebruikt nu v{APP_VERSION}. Klik op Update om de nieuwe versie te downloaden, "
                "de app te vervangen en opnieuw te starten. Je Groq API key en instellingen blijven behouden."
            ),
            wraplength=430,
        ).grid(row=1, column=0, sticky="w")

        status = StringVar(value="")
        ttk.Label(frame, textvariable=status, foreground="#555").grid(row=2, column=0, sticky="w", pady=14)

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="e", pady=(8, 0))

        update_button = ttk.Button(buttons, text=f"Update naar {update.tag}")
        update_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="Later", command=window.destroy).pack(side="left", padx=6)

        def start_update() -> None:
            update_button.configure(state="disabled")
            status.set("Downloaden...")

            def run() -> None:
                try:
                    downloaded = download_update(update)
                    launch_update_script(downloaded)
                except Exception as exc:
                    logging.exception("Update failed")
                    self.root.after(
                        0,
                        lambda: (
                            update_button.configure(state="normal"),
                            status.set("Update mislukt."),
                            messagebox.showerror(APP_NAME, f"Update mislukt:\n{exc}"),
                        ),
                    )
                    return

                self.root.after(0, self.quit)

            threading.Thread(target=run, daemon=True).start()

        update_button.configure(command=start_update)

    def open_settings(self) -> None:
        if hasattr(self, "settings_window") and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        window = Toplevel(self.root)
        self.settings_window = window
        window.title(f"{APP_NAME} instellingen")
        window.geometry("560x470")
        window.resizable(False, False)

        api_key = StringVar(value=self.config.api_key)
        model = StringVar(value=self.config.model)
        language = StringVar(value=self.config.language)
        prompt = StringVar(value=self.config.prompt)
        custom_words = list(self.config.custom_words)
        word_replacements = list(self.config.word_replacements)
        dictionary_summary = StringVar()
        shortcut = StringVar(value=self.config.shortcut)
        device_options = input_devices()
        device_labels = {device_id: label for device_id, label in device_options}
        input_device = StringVar(value=device_labels.get(self.config.input_device, "Windows default input"))
        paste = BooleanVar(value=self.config.paste_after_transcription)
        autostart_var = BooleanVar(value=self.config.autostart or autostart_enabled())

        def update_dictionary_summary() -> None:
            word_count = len(custom_words)
            replacement_count = len(word_replacements)
            words_label = f"{word_count} woord" if word_count == 1 else f"{word_count} woorden"
            replacements_label = (
                f"{replacement_count} vervanging"
                if replacement_count == 1
                else f"{replacement_count} vervangingen"
            )
            dictionary_summary.set(f"{words_label}, {replacements_label}")

        update_dictionary_summary()

        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Groq API key").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=api_key, show="*", width=46).grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="Model").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Combobox(
            frame,
            textvariable=model,
            values=("whisper-large-v3-turbo", "whisper-large-v3"),
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="Taal").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=language).grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="Prompt").grid(row=3, column=0, sticky="nw", pady=6)
        ttk.Entry(frame, textvariable=prompt).grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="Woordenboek").grid(row=4, column=0, sticky="w", pady=6)
        dictionary_frame = ttk.Frame(frame)
        dictionary_frame.grid(row=4, column=1, sticky="ew", pady=6)
        dictionary_frame.columnconfigure(0, weight=1)
        ttk.Label(dictionary_frame, textvariable=dictionary_summary, foreground="#555").grid(
            row=0, column=0, sticky="w"
        )
        dictionary_button = ttk.Button(dictionary_frame, text="Beheren...")
        dictionary_button.grid(row=0, column=1)

        ttk.Label(frame, text="Shortcut").grid(row=5, column=0, sticky="w", pady=6)
        shortcut_frame = ttk.Frame(frame)
        shortcut_frame.grid(row=5, column=1, sticky="ew", pady=6)
        shortcut_frame.columnconfigure(0, weight=1)
        ttk.Entry(shortcut_frame, textvariable=shortcut).grid(row=0, column=0, sticky="ew")
        capture_button = ttk.Button(shortcut_frame, text="Wijzig")
        capture_button.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(frame, text="Microfoon").grid(row=6, column=0, sticky="w", pady=6)
        ttk.Combobox(
            frame,
            textvariable=input_device,
            values=[label for _, label in device_options],
            state="readonly",
        ).grid(row=6, column=1, sticky="ew", pady=6)

        ttk.Checkbutton(frame, text="Transcriptie automatisch plakken", variable=paste).grid(
            row=7, column=1, sticky="w", pady=6
        )
        ttk.Checkbutton(frame, text="Start automatisch met Windows", variable=autostart_var).grid(
            row=8, column=1, sticky="w", pady=6
        )

        status = StringVar(value=f"Instellingen: {SETTINGS_PATH}")
        ttk.Label(frame, textvariable=status, foreground="#555").grid(row=9, column=0, columnspan=2, sticky="w", pady=12)

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=16)
        capture_bind_id: list[str | None] = [None]
        dictionary_window: list[Toplevel | None] = [None]

        def open_dictionary() -> None:
            existing_dialog = dictionary_window[0]
            if existing_dialog is not None and existing_dialog.winfo_exists():
                existing_dialog.lift()
                existing_dialog.focus_force()
                return

            dialog = Toplevel(window)
            dictionary_window[0] = dialog
            dialog.title("Persoonlijk woordenboek")
            dialog.geometry("600x620")
            dialog.resizable(False, False)
            dialog.transient(window)

            def close_dialog() -> None:
                dictionary_window[0] = None
                try:
                    dialog.grab_release()
                except Exception:
                    pass
                dialog.destroy()

            dialog_frame = ttk.Frame(dialog, padding=16)
            dialog_frame.pack(fill="both", expand=True)
            dialog_frame.columnconfigure(0, weight=1)
            dialog_frame.rowconfigure(2, weight=1)
            dialog_frame.rowconfigure(4, weight=1)

            ttk.Label(
                dialog_frame,
                text="Voeg namen, jargon of woorden toe die Groq helpen bij herkenning en spelling.",
                wraplength=440,
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

            word_value = StringVar()
            word_entry = ttk.Entry(dialog_frame, textvariable=word_value)
            word_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))

            list_frame = ttk.Frame(dialog_frame)
            list_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(12, 10))
            list_frame.columnconfigure(0, weight=1)
            list_frame.rowconfigure(0, weight=1)
            word_list = Listbox(list_frame, height=6, exportselection=False)
            word_list.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=word_list.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            word_list.configure(yscrollcommand=scrollbar.set)

            def refresh_words(select_index: int | None = None) -> None:
                word_list.delete(0, END)
                for item in custom_words:
                    word_list.insert(END, item)
                update_dictionary_summary()
                if select_index is not None and custom_words:
                    index = min(select_index, len(custom_words) - 1)
                    word_list.selection_set(index)
                    word_list.see(index)

            def add_word() -> None:
                try:
                    word = normalize_custom_word(word_value.get())
                    if any(existing.casefold() == word.casefold() for existing in custom_words):
                        raise DictionaryValidationError(f"'{word}' staat al in het woordenboek.")
                    candidate = normalize_custom_words([*custom_words, word])
                    compose_transcription_prompt(prompt.get(), candidate)
                except DictionaryValidationError as exc:
                    messagebox.showerror(APP_NAME, str(exc), parent=dialog)
                    return
                custom_words.append(word)
                word_value.set("")
                refresh_words(len(custom_words) - 1)
                word_entry.focus_set()

            def remove_word() -> None:
                selection = word_list.curselection()
                if not selection:
                    return
                index = int(selection[0])
                del custom_words[index]
                refresh_words(index)

            def create_replacements_section() -> None:
                replacements_frame = ttk.LabelFrame(dialog_frame, text="Vervangingen", padding=12)
                replacements_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(12, 10))
                replacements_frame.columnconfigure(1, weight=1)
                replacements_frame.rowconfigure(3, weight=1)

                ttk.Label(
                    replacements_frame,
                    text=(
                        "Vervang bekende transcriptiefouten altijd door de gewenste spelling, "
                        "bijvoorbeeld Grok → Groq."
                    ),
                    wraplength=530,
                ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

                source_value = StringVar()
                target_value = StringVar()
                ttk.Label(replacements_frame, text="Verkeerd").grid(row=1, column=0, sticky="w", padx=(0, 8))
                source_entry = ttk.Entry(replacements_frame, textvariable=source_value)
                source_entry.grid(row=1, column=1, sticky="ew", pady=3)
                ttk.Label(replacements_frame, text="Correct").grid(row=2, column=0, sticky="w", padx=(0, 8))
                target_entry = ttk.Entry(replacements_frame, textvariable=target_value)
                target_entry.grid(row=2, column=1, sticky="ew", pady=3)

                replacements_list_frame = ttk.Frame(replacements_frame)
                replacements_list_frame.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(12, 10))
                replacements_list_frame.columnconfigure(0, weight=1)
                replacements_list_frame.rowconfigure(0, weight=1)
                replacements_list = Listbox(replacements_list_frame, height=6, exportselection=False)
                replacements_list.grid(row=0, column=0, sticky="nsew")
                replacements_scrollbar = ttk.Scrollbar(
                    replacements_list_frame,
                    orient="vertical",
                    command=replacements_list.yview,
                )
                replacements_scrollbar.grid(row=0, column=1, sticky="ns")
                replacements_list.configure(yscrollcommand=replacements_scrollbar.set)

                def refresh_replacements(select_index: int | None = None) -> None:
                    replacements_list.delete(0, END)
                    for source, target in word_replacements:
                        replacements_list.insert(END, f"{source} → {target}")
                    update_dictionary_summary()
                    if select_index is not None and word_replacements:
                        index = min(select_index, len(word_replacements) - 1)
                        replacements_list.selection_set(index)
                        replacements_list.see(index)

                def add_replacement() -> None:
                    try:
                        source = normalize_replacement_part(source_value.get(), "verkeerd herkende")
                        target = normalize_replacement_part(target_value.get(), "correcte")
                        candidate = normalize_word_replacements([*word_replacements, (source, target)])
                        if len(candidate) == len(word_replacements):
                            raise DictionaryValidationError(f"Voor '{source}' bestaat al een vervanging.")
                    except DictionaryValidationError as exc:
                        messagebox.showerror(APP_NAME, str(exc), parent=dialog)
                        return
                    word_replacements.append((source, target))
                    source_value.set("")
                    target_value.set("")
                    refresh_replacements(len(word_replacements) - 1)
                    source_entry.focus_set()

                def remove_replacement() -> None:
                    selection = replacements_list.curselection()
                    if not selection:
                        return
                    index = int(selection[0])
                    del word_replacements[index]
                    refresh_replacements(index)

                ttk.Button(replacements_frame, text="Toevoegen", command=add_replacement).grid(
                    row=1,
                    column=2,
                    rowspan=2,
                    padx=(8, 0),
                )
                replacement_buttons = ttk.Frame(replacements_frame)
                replacement_buttons.grid(row=4, column=0, columnspan=3, sticky="e")
                ttk.Button(replacement_buttons, text="Verwijderen", command=remove_replacement).pack(
                    side="left",
                    padx=6,
                )
                target_entry.bind("<Return>", lambda _event: (add_replacement(), "break")[1])
                refresh_replacements()

            ttk.Button(dialog_frame, text="Toevoegen", command=add_word).grid(row=1, column=1)
            dialog_buttons = ttk.Frame(dialog_frame)
            dialog_buttons.grid(row=3, column=0, columnspan=2, sticky="e")
            ttk.Button(dialog_buttons, text="Verwijderen", command=remove_word).pack(side="left", padx=6)
            create_replacements_section()
            close_buttons = ttk.Frame(dialog_frame)
            close_buttons.grid(row=5, column=0, columnspan=2, sticky="e")
            ttk.Button(close_buttons, text="Sluiten", command=close_dialog).pack(side="left", padx=6)
            word_entry.bind("<Return>", lambda _event: (add_word(), "break")[1])
            refresh_words()
            dialog.protocol("WM_DELETE_WINDOW", close_dialog)
            dialog.grab_set()
            word_entry.focus_set()

        dictionary_button.configure(command=open_dictionary)

        def stop_capture(restore_hotkey: bool) -> None:
            if capture_bind_id[0] is not None:
                window.unbind("<KeyPress>", capture_bind_id[0])
                capture_bind_id[0] = None
            capture_button.configure(text="Wijzig")
            if restore_hotkey:
                try:
                    self.install_hotkey()
                except Exception as exc:
                    status.set(f"Kon oude shortcut niet terugzetten: {exc}")

        def start_capture() -> None:
            stop_capture(restore_hotkey=False)
            self.remove_hotkey()
            status.set("Druk nu op de gewenste shortcut. Esc annuleert.")
            capture_button.configure(text="Luistert...")
            window.focus_force()

            def capture(event) -> str:
                if event.keysym == "Escape":
                    status.set("Shortcut wijzigen geannuleerd.")
                    stop_capture(restore_hotkey=True)
                    return "break"

                value = hotkey_from_tk_event(event)
                if value is None:
                    return "break"

                shortcut.set(value)
                status.set(f"Shortcut ingesteld op: {value}")
                stop_capture(restore_hotkey=True)
                return "break"

            capture_bind_id[0] = window.bind("<KeyPress>", capture, add="+")

        capture_button.configure(command=start_capture)

        def save() -> None:
            stop_capture(restore_hotkey=False)
            selected_device = input_device.get()
            selected_device_id = ""
            for device_id, label in device_options:
                if label == selected_device:
                    selected_device_id = device_id
                    break
            if selected_device and not selected_device_id and selected_device.split(":", 1)[0].isdigit():
                selected_device_id = selected_device.split(":", 1)[0]

            normalized_shortcut = normalize_hotkey_text(shortcut.get()) or "insert"
            try:
                validate_hotkey(normalized_shortcut)
                normalized_words = normalize_custom_words(custom_words)
                normalized_replacements = normalize_word_replacements(word_replacements)
                compose_transcription_prompt(prompt.get(), normalized_words)
            except DictionaryValidationError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Shortcut wordt niet herkend:\n{normalized_shortcut}\n\n{exc}")
                return

            entered_api_key = api_key.get().strip()
            new_config = Config(
                api_key=entered_api_key,
                model=model.get().strip() or "whisper-large-v3-turbo",
                language=language.get().strip(),
                prompt=prompt.get().strip(),
                custom_words=normalized_words,
                word_replacements=normalized_replacements,
                shortcut=normalized_shortcut,
                input_device=selected_device_id,
                sample_rate=self.config.sample_rate,
                channels=self.config.channels,
                paste_after_transcription=paste.get(),
                autostart=autostart_var.get(),
                # If Credential Manager could not be read at startup, an
                # unchanged fallback value must not overwrite a newer secret.
                # Deliberately editing the field still authorizes the change.
                keyring_read_succeeded=(
                    self.config.keyring_read_succeeded
                    or entered_api_key != self.config.api_key
                ),
            )
            try:
                save_config(new_config)
                set_autostart(new_config.autostart)
                self.engine.update_config(new_config)
                self.config = new_config
                self.install_hotkey()
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Instellingen konden niet worden opgeslagen:\n{exc}")
                return

            status.set("Opgeslagen.")
            window.destroy()

        ttk.Button(buttons, text="Geluiden testen", command=self.test_sounds).pack(side="left", padx=6)
        ttk.Button(buttons, text="Annuleren", command=lambda: (stop_capture(restore_hotkey=True), window.destroy())).pack(side="left", padx=6)
        ttk.Button(buttons, text="Opslaan", command=save).pack(side="left", padx=6)
        window.protocol("WM_DELETE_WINDOW", lambda: (stop_capture(restore_hotkey=True), window.destroy()))

    def test_sounds(self) -> None:
        def run() -> None:
            for sound in ("start.wav", "success.wav"):
                play_sound(sound)
                time.sleep(0.32)

        threading.Thread(target=run, daemon=True).start()

    def open_log(self) -> None:
        LOG_PATH.touch(exist_ok=True)
        try:
            os.startfile(LOG_PATH)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Logbestand kon niet worden geopend:\n{exc}")

    def restart(self) -> None:
        env = os.environ.copy()
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if getattr(sys, "frozen", False):
                executable = Path(sys.executable)
                subprocess.Popen([str(executable)], cwd=str(executable.parent), env=env, creationflags=creation_flags)
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                launcher = pythonw if pythonw.exists() else Path(sys.executable)
                subprocess.Popen([str(launcher), str(Path(__file__).resolve())], cwd=str(Path(__file__).parent), env=env, creationflags=creation_flags)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"App kon niet worden herstart:\n{exc}")
            return

        self.quit()

    def quit(self) -> None:
        self.startup_finished = True
        self.splash.destroy()
        self.remove_hotkey()
        self.engine.shutdown()
        self.bubble.destroy()
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.quit()
        self.root.destroy()


def main() -> None:
    if not acquire_single_instance_lock():
        return

    try:
        TrayApp().run()
    except Exception as exc:
        setup_logging()
        logging.exception("Fatal startup error")
        try:
            messagebox.showerror(APP_NAME, f"Startfout:\n{exc}\n\nLog: {LOG_PATH}")
        except Exception:
            pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
