import json
import logging
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
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import BooleanVar, Canvas, StringVar, Tk, Toplevel, messagebox, ttk

import keyboard
import keyring
import numpy as np
import pyautogui
import pyperclip
import pystray
import sounddevice as sd
from groq import Groq
from PIL import Image, ImageDraw

try:
    import winsound
except ImportError:  # pragma: no cover - Windows-only nicety
    winsound = None


APP_NAME = "Groq Insert Dictation"
APP_SLUG = "GroqInsertDictation"
APP_VERSION = "0.1.10"
GITHUB_REPO = "brvale97/groq-windows"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
KEYRING_SERVICE = APP_SLUG
KEYRING_USER = "groq_api_key"
_INSTANCE_MUTEX_HANDLE = None
MIN_TRANSCRIPTION_SECONDS = 1.0
MIN_TRANSCRIPTION_BYTES = 32_000

ANDROID_MIC_PATH = (
    "M12,14c1.66,0 3,-1.34 3,-3L15,5c0,-1.66 -1.34,-3 -3,-3S9,3.34 9,5v6c0,1.66 1.34,3 3,3z"
    "M17.3,11c0,3 -2.54,5.1 -5.3,5.1S6.7,14 6.7,11L5,11c0,3.41 2.72,6.23 6,6.72L11,21h2v-3.28"
    "c3.28,-0.48 6,-3.3 6,-6.72L17.3,11z"
)
BUBBLE_COLORS = {
    "idle": "#E81123",
    "recording": "#ff2e3d",
    "processing": "#E81123",
}


def app_data_dir() -> Path:
    root = os.getenv("APPDATA")
    if root:
        return Path(root) / APP_SLUG
    return Path.home() / f".{APP_SLUG}"


APP_DIR = app_data_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
LOG_PATH = APP_DIR / "app.log"
SOUNDS_DIR = APP_DIR / "sounds"


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def acquire_single_instance_lock() -> bool:
    if os.name != "nt":
        return True

    import ctypes

    global _INSTANCE_MUTEX_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INSTANCE_MUTEX_HANDLE = kernel32.CreateMutexW(None, False, f"Local\\{APP_SLUG}SingleInstance")
    return ctypes.get_last_error() != 183


@dataclass
class Config:
    api_key: str = ""
    model: str = "whisper-large-v3-turbo"
    language: str = "nl"
    prompt: str = ""
    shortcut: str = "insert"
    input_device: str = ""
    sample_rate: int = 16_000
    channels: int = 1
    paste_after_transcription: bool = True
    autostart: bool = True


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


def read_api_key_from_keyring() -> str:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception as exc:
        logging.warning("Could not read API key from keyring: %s", exc)
        return ""


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


def load_config() -> Config:
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logging.warning("Ignoring invalid settings file: %s", exc)

    env = load_dotenv_values(Path(".env"))
    config = Config(
        api_key="",
        model=data.get("model") or env.get("GROQ_MODEL") or "whisper-large-v3-turbo",
        language=data.get("language") if data.get("language") is not None else env.get("GROQ_LANGUAGE", "nl"),
        prompt=data.get("prompt") if data.get("prompt") is not None else env.get("GROQ_PROMPT", ""),
        shortcut=data.get("shortcut") or env.get("DICTATION_SHORTCUT") or "insert",
        input_device=data.get("input_device") if data.get("input_device") is not None else env.get("DICTATION_INPUT_DEVICE", ""),
        sample_rate=int(data.get("sample_rate") or env.get("DICTATION_SAMPLE_RATE") or 16000),
        channels=int(data.get("channels") or env.get("DICTATION_CHANNELS") or 1),
        paste_after_transcription=bool(
            data.get("paste_after_transcription")
            if "paste_after_transcription" in data
            else env.get("PASTE_AFTER_TRANSCRIPTION", "true").lower() in {"1", "true", "yes", "on"}
        ),
        autostart=bool(data.get("autostart", True)),
    )

    config.api_key = (
        read_api_key_from_keyring()
        or data.get("api_key", "")
        or env.get("GROQ_API_KEY", "")
        or os.getenv("GROQ_API_KEY", "")
    ).strip()
    return config


def save_config(config: Config) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    api_key = data.pop("api_key", "")
    if not write_api_key_to_keyring(api_key):
        data["api_key"] = api_key
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
        path.write_text(f"@echo off\n{current_launch_command()}\n", encoding="utf-8")
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
    request = urllib.request.Request(
        update.download_url,
        headers={"User-Agent": APP_SLUG},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())
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
                f"$PidToWait = {current_pid}",
                "function Log($Message) { Add-Content -LiteralPath $Log -Value \"$(Get-Date -Format o) $Message\" }",
                "try {",
                "  Log \"Waiting for process $PidToWait to exit\"",
                "  try { Wait-Process -Id $PidToWait -Timeout 30 -ErrorAction SilentlyContinue } catch {}",
                "  Start-Sleep -Milliseconds 700",
                "  Log \"Copying $Source to $Target\"",
                "  Copy-Item -LiteralPath $Source -Destination $Target -Force",
                "  Log \"Starting $Target\"",
                "  $Env:PYINSTALLER_RESET_ENVIRONMENT = '1'",
                "  Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target)",
                "  Remove-Item -LiteralPath $Source -Force -ErrorAction SilentlyContinue",
                "  Log \"Update complete\"",
                "} catch {",
                "  Log \"Update failed: $($_.Exception.Message)\"",
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


def hex_to_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def tokenize_svg_path(path_data: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(path_data):
        char = path_data[i]
        if char.isalpha():
            tokens.append(char)
            i += 1
            continue
        if char in " ,\n\r\t":
            i += 1
            continue

        start = i
        i += 1
        while i < len(path_data):
            current = path_data[i]
            previous = path_data[i - 1]
            if current.isalpha() or current in " ,\n\r\t":
                break
            if current in "+-" and previous not in "eE":
                break
            i += 1
        tokens.append(path_data[start:i])
    return tokens


def cubic_point(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    mt = 1 - t
    return (
        mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0],
        mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1],
    )


def render_svg_path_mask(path_data: str, size: int, viewport: float = 24.0) -> Image.Image:
    tokens = tokenize_svg_path(path_data)
    paths: list[list[tuple[float, float]]] = []
    path: list[tuple[float, float]] = []
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    last_control: tuple[float, float] | None = None
    command = ""
    index = 0

    def is_command(token: str) -> bool:
        return len(token) == 1 and token.isalpha()

    def read_float() -> float:
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    def add_point(point: tuple[float, float]) -> None:
        nonlocal current
        path.append(point)
        current = point

    while index < len(tokens):
        if is_command(tokens[index]):
            command = tokens[index]
            index += 1

        lower = command.lower()
        relative = command.islower()

        if lower == "m":
            first = True
            while index < len(tokens) and not is_command(tokens[index]):
                x, y = read_float(), read_float()
                point = (current[0] + x, current[1] + y) if relative else (x, y)
                if first:
                    if path:
                        paths.append(path)
                    path = [point]
                    current = point
                    start = point
                    first = False
                else:
                    add_point(point)
                last_control = None
            command = "l" if relative else "L"
        elif lower == "l":
            while index < len(tokens) and not is_command(tokens[index]):
                x, y = read_float(), read_float()
                add_point((current[0] + x, current[1] + y) if relative else (x, y))
            last_control = None
        elif lower == "h":
            while index < len(tokens) and not is_command(tokens[index]):
                x = read_float()
                add_point((current[0] + x, current[1]) if relative else (x, current[1]))
            last_control = None
        elif lower == "v":
            while index < len(tokens) and not is_command(tokens[index]):
                y = read_float()
                add_point((current[0], current[1] + y) if relative else (current[0], y))
            last_control = None
        elif lower == "c":
            while index < len(tokens) and not is_command(tokens[index]):
                x1, y1, x2, y2, x3, y3 = (read_float(), read_float(), read_float(), read_float(), read_float(), read_float())
                p1 = (current[0] + x1, current[1] + y1) if relative else (x1, y1)
                p2 = (current[0] + x2, current[1] + y2) if relative else (x2, y2)
                p3 = (current[0] + x3, current[1] + y3) if relative else (x3, y3)
                p0 = current
                for step in range(1, 25):
                    path.append(cubic_point(p0, p1, p2, p3, step / 24))
                current = p3
                last_control = p2
        elif lower == "s":
            while index < len(tokens) and not is_command(tokens[index]):
                x2, y2, x3, y3 = read_float(), read_float(), read_float(), read_float()
                if last_control is None:
                    p1 = current
                else:
                    p1 = (2 * current[0] - last_control[0], 2 * current[1] - last_control[1])
                p2 = (current[0] + x2, current[1] + y2) if relative else (x2, y2)
                p3 = (current[0] + x3, current[1] + y3) if relative else (x3, y3)
                p0 = current
                for step in range(1, 25):
                    path.append(cubic_point(p0, p1, p2, p3, step / 24))
                current = p3
                last_control = p2
        elif lower == "z":
            if path:
                path.append(start)
                paths.append(path)
                path = []
            current = start
            last_control = None
        else:
            raise ValueError(f"Unsupported SVG path command: {command}")

    if path:
        paths.append(path)

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    scale = size / viewport
    for subpath in paths:
        points = [(round(x * scale), round(y * scale)) for x, y in subpath]
        draw.polygon(points, fill=255)
    return mask


def create_bubble_image(state: str, size: int) -> Image.Image:
    render_scale = 4
    work_size = size * render_scale

    def p(value: float) -> int:
        return round((value / 60) * work_size)

    image = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = hex_to_rgba(BUBBLE_COLORS.get(state, BUBBLE_COLORS["idle"]))

    draw.ellipse((p(6), p(8), p(56), p(58)), fill=(0, 0, 0, 58))
    draw.ellipse((p(4), p(2), p(56), p(54)), fill=color)
    draw.ellipse((p(12), p(7), p(32), p(18)), fill=(255, 255, 255, 42))

    icon_size = p(30)
    icon = Image.new("RGBA", (icon_size, icon_size), (255, 255, 255, 0))
    icon.putalpha(render_svg_path_mask(ANDROID_MIC_PATH, icon_size))
    image.alpha_composite(icon, (p(15), p(13)))

    if state == "recording":
        draw.ellipse((p(43), p(10), p(52), p(19)), fill=(255, 255, 255, 235))

    resample = getattr(Image, "Resampling", Image).LANCZOS
    return image.resize((size, size), resample)


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
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    marker = SOUNDS_DIR / ".groqandroid-cues-v1"
    sounds = {
        "start.wav": [523.25, 659.25],
        "processing.wav": [587.33, 440.0],
        "success.wav": [587.33, 440.0],
        "error.wav": [246.94, 196.00],
    }

    for filename, notes in sounds.items():
        path = SOUNDS_DIR / filename
        if not path.exists() or not marker.exists():
            make_tone(path, notes)
    marker.write_text("Generated from GroqAndroid cue parameters.\n", encoding="utf-8")


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


def input_device_name(device_id: int | None) -> str:
    if device_id is None:
        default_input = sd.default.device[0]
        device_id = default_input if default_input is not None and default_input >= 0 else None

    if device_id is None:
        return "Windows default input"

    device = sd.query_devices(device_id)
    return f"{device_id}: {device['name']}"


def input_devices() -> list[tuple[str, str]]:
    devices = [("", "Windows default input")]
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            devices.append((str(index), f"{index}: {device['name']}"))
    return devices


def input_device_label(device_id: str) -> str:
    for candidate_id, label in input_devices():
        if candidate_id == device_id:
            return label
    return "Windows default input"


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
        self.size = 48
        self.window_width = self.size
        self.window_height = self.size
        self.hide_after_id = None
        self.spinner_after_id = None
        self.spinner_angle = 0
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
            width=self.size,
            height=self.size,
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

    def hide(self) -> None:
        self.hide_after_id = None
        try:
            self.window.withdraw()
        except Exception:
            pass

    def set_state(self, state: str, schedule_hide: bool = True) -> None:
        self.state = state
        self.stop_spinner()
        self.window_width = self.size
        self.window_height = self.size
        self.canvas.configure(width=self.window_width, height=self.window_height)
        if state != "idle":
            self.show()

        self.canvas.delete("all")

        tooltips = {
            "idle": "Klaar",
            "recording": "Opname",
            "processing": "Transcriptie",
        }
        if state == "processing":
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
        self.state = "notice"
        self.window_width = 176
        self.window_height = self.size
        self.canvas.configure(width=self.window_width, height=self.window_height)
        self.show()
        self.canvas.delete("all")

        self.draw_round_rect(0, 0, self.window_width - 1, self.window_height - 1, 14, BUBBLE_COLORS["idle"])
        self.canvas.create_oval(11, 11, 37, 37, fill="#ffffff", outline="")
        self.canvas.create_text(24, 24, text="!", fill=BUBBLE_COLORS["idle"], font=("Segoe UI", 16, "bold"))
        self.canvas.create_text(
            46,
            24,
            text=message,
            fill="#ffffff",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        self.window.title(f"{APP_NAME} - {message}")
        self.schedule_hide()

    def draw_mic_button(self, state: str) -> None:
        color = BUBBLE_COLORS["recording"] if state == "recording" else BUBBLE_COLORS["idle"]
        if state == "idle":
            self.draw_round_rect(5, 5, self.size - 5, self.size - 5, 11, color)
        else:
            self.canvas.create_oval(4, 4, self.size - 4, self.size - 4, fill=color, outline="")
        self.draw_mic_icon()

    def draw_processing_button(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_oval(4, 4, self.size - 4, self.size - 4, fill=BUBBLE_COLORS["recording"], outline="")
        self.canvas.create_oval(15, 15, 33, 33, outline="#ffffff", width=2)
        self.canvas.create_arc(
            14,
            14,
            34,
            34,
            start=self.spinner_angle,
            extent=105,
            style="arc",
            outline="#ffffff",
            width=4,
        )

    def draw_mic_icon(self) -> None:
        self.canvas.create_line(24, 14, 24, 27, fill="#ffffff", width=8, capstyle="round")
        self.canvas.create_line(
            14,
            25,
            14,
            28,
            16,
            34,
            24,
            37,
            32,
            34,
            34,
            28,
            34,
            25,
            fill="#ffffff",
            width=3,
            capstyle="round",
            joinstyle="round",
            smooth=True,
        )
        self.canvas.create_line(24, 37, 24, 42, fill="#ffffff", width=3, capstyle="round")
        self.canvas.create_line(19, 42, 29, 42, fill="#ffffff", width=3, capstyle="round")

    def draw_round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, fill: str) -> None:
        self.canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline="")
        self.canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline="")
        self.canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, fill=fill, outline="")
        self.canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, fill=fill, outline="")
        self.canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, fill=fill, outline="")
        self.canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, fill=fill, outline="")

    def destroy(self) -> None:
        self.stop_spinner()
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


class DictationEngine:
    def __init__(self, config: Config, status_callback=None, state_callback=None) -> None:
        self.config = config
        self.status_callback = status_callback or (lambda message: None)
        self.state_callback = state_callback or (lambda state: None)
        self.input_device = resolve_input_device(config.input_device)
        self.client = Groq(api_key=config.api_key) if config.api_key else None
        self.audio_queue: queue.Queue = queue.Queue()
        self.frames: list = []
        self.stream: sd.InputStream | None = None
        self.state = "idle"
        self.lock = threading.Lock()

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

    def set_state(self, state: str) -> None:
        with self.lock:
            self.state = state
        self.emit_state(state)

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
        if not self.config.api_key:
            self.notify("Open Instellingen en vul eerst je Groq API key in.")
            play_sound("error.wav")
            return

        with self.lock:
            if self.state != "idle":
                return
            self.state = "recording"
            self.frames = []
            self.audio_queue = queue.Queue()

        try:
            self.stream = sd.InputStream(
                device=self.input_device,
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                callback=self.audio_callback,
            )
            self.stream.start()
        except Exception:
            with self.lock:
                self.state = "idle"
            self.emit_state("idle")
            raise

        self.emit_state("recording")
        play_sound("start.wav")
        self.notify("Opname gestart. Druk nog eens op Insert om te stoppen.")

    def stop_recording(self) -> None:
        with self.lock:
            if self.state != "recording":
                return
            self.state = "processing"
        self.emit_state("processing")

        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        while True:
            try:
                self.frames.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break

        self.notify("Opname gestopt. Transcriberen...")
        threading.Thread(target=self.transcribe_and_output, daemon=True).start()

    def audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.notify(f"Audio waarschuwing: {status}")
        self.audio_queue.put(indata.copy())

    def write_wav(self) -> Path:
        if not self.frames:
            raise RuntimeError("Geen audio opgenomen.")

        temp = tempfile.NamedTemporaryFile(
            prefix="groq-insert-dictation-",
            suffix=".wav",
            delete=False,
        )
        temp_path = Path(temp.name)
        temp.close()

        with wave.open(str(temp_path), "wb") as wav:
            wav.setnchannels(self.config.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.config.sample_rate)
            for frame in self.frames:
                wav.writeframes(frame.tobytes())

        return temp_path

    def audio_stats(self) -> tuple[float, float, float]:
        if not self.frames:
            return 0.0, 0.0, 0.0

        audio = np.concatenate(self.frames, axis=0).astype(np.float32)
        samples = audio.reshape(-1)
        duration = len(audio) / self.config.sample_rate
        peak = float(np.max(np.abs(samples)) / 32768.0) if samples.size else 0.0
        rms = float(np.sqrt(np.mean((samples / 32768.0) ** 2))) if samples.size else 0.0
        return duration, peak, rms

    def transcribe_and_output(self) -> None:
        wav_path: Path | None = None
        started_at = time.perf_counter()
        show_idle_bubble = True
        try:
            wav_path = self.write_wav()
            duration, peak, rms = self.audio_stats()
            self.notify(f"Audio: {duration:.1f}s, piek {peak:.3f}, rms {rms:.3f}")
            if duration < MIN_TRANSCRIPTION_SECONDS or wav_path.stat().st_size < MIN_TRANSCRIPTION_BYTES:
                self.notify("Transcriptie te kort. Er is niets geplakt.")
                self.emit_state("too_short")
                play_sound("error.wav")
                show_idle_bubble = False
                return

            if peak < 0.01:
                self.notify("Waarschuwing: bijna geen inputvolume gemeten. Check microfoon/device.")

            text = remove_final_sentence_period(self.transcribe(wav_path).strip())
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

            if self.config.paste_after_transcription:
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
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
            with self.lock:
                self.state = "idle"
            if show_idle_bubble:
                self.emit_state("idle")
            self.notify("Klaar. Druk op Insert voor een nieuwe opname.")

    def transcribe(self, wav_path: Path) -> str:
        if self.client is None:
            raise RuntimeError("Groq API key ontbreekt.")

        kwargs = {
            "file": wav_path.open("rb"),
            "model": self.config.model,
            "response_format": "json",
            "temperature": 0.0,
        }
        if self.config.language:
            kwargs["language"] = self.config.language
        if self.config.prompt:
            kwargs["prompt"] = self.config.prompt

        with kwargs["file"] as audio_file:
            kwargs["file"] = audio_file
            transcription = self.client.audio.transcriptions.create(**kwargs)

        return getattr(transcription, "text", "") or ""


class TrayApp:
    def __init__(self) -> None:
        setup_logging()
        ensure_sounds()
        self.root = Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)

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
                pystray.MenuItem("Afsluiten", lambda: self.root.after(0, self.quit)),
            ),
        )

    def set_status(self, message: str) -> None:
        logging.info(message)
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
        save_config(self.config)
        set_autostart(self.config.autostart)
        self.install_hotkey()
        self.icon.run_detached()

        if not self.config.api_key:
            self.root.after(250, self.open_settings)
        self.root.after(2500, self.check_for_updates_auto)
        self.root.mainloop()

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
        window.geometry("560x430")
        window.resizable(False, False)

        api_key = StringVar(value=self.config.api_key)
        model = StringVar(value=self.config.model)
        language = StringVar(value=self.config.language)
        prompt = StringVar(value=self.config.prompt)
        shortcut = StringVar(value=self.config.shortcut)
        input_device = StringVar(value=input_device_label(self.config.input_device))
        paste = BooleanVar(value=self.config.paste_after_transcription)
        autostart_var = BooleanVar(value=self.config.autostart or autostart_enabled())

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

        ttk.Label(frame, text="Shortcut").grid(row=4, column=0, sticky="w", pady=6)
        shortcut_frame = ttk.Frame(frame)
        shortcut_frame.grid(row=4, column=1, sticky="ew", pady=6)
        shortcut_frame.columnconfigure(0, weight=1)
        ttk.Entry(shortcut_frame, textvariable=shortcut).grid(row=0, column=0, sticky="ew")
        capture_button = ttk.Button(shortcut_frame, text="Wijzig")
        capture_button.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(frame, text="Microfoon").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Combobox(
            frame,
            textvariable=input_device,
            values=[label for _, label in input_devices()],
            state="readonly",
        ).grid(row=5, column=1, sticky="ew", pady=6)

        ttk.Checkbutton(frame, text="Transcriptie automatisch plakken", variable=paste).grid(
            row=6, column=1, sticky="w", pady=6
        )
        ttk.Checkbutton(frame, text="Start automatisch met Windows", variable=autostart_var).grid(
            row=7, column=1, sticky="w", pady=6
        )

        status = StringVar(value=f"Instellingen: {SETTINGS_PATH}")
        ttk.Label(frame, textvariable=status, foreground="#555").grid(row=8, column=0, columnspan=2, sticky="w", pady=12)

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=16)
        capture_bind_id: list[str | None] = [None]

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
            for device_id, label in input_devices():
                if label == selected_device:
                    selected_device_id = device_id
                    break
            if selected_device and not selected_device_id and selected_device.split(":", 1)[0].isdigit():
                selected_device_id = selected_device.split(":", 1)[0]

            normalized_shortcut = normalize_hotkey_text(shortcut.get()) or "insert"
            try:
                validate_hotkey(normalized_shortcut)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Shortcut wordt niet herkend:\n{normalized_shortcut}\n\n{exc}")
                return

            new_config = Config(
                api_key=api_key.get().strip(),
                model=model.get().strip() or "whisper-large-v3-turbo",
                language=language.get().strip(),
                prompt=prompt.get().strip(),
                shortcut=normalized_shortcut,
                input_device=selected_device_id,
                paste_after_transcription=paste.get(),
                autostart=autostart_var.get(),
            )
            try:
                self.config = new_config
                save_config(self.config)
                set_autostart(self.config.autostart)
                self.engine.update_config(self.config)
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

    def quit(self) -> None:
        self.remove_hotkey()
        self.bubble.destroy()
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.quit()
        self.root.destroy()


def main() -> None:
    if "--version" in sys.argv:
        print(APP_VERSION)
        return

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
