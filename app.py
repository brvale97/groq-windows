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
APP_VERSION = "0.1.6"
GITHUB_REPO = "brvale97/groq-windows"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
KEYRING_SERVICE = APP_SLUG
KEYRING_USER = "groq_api_key"
_INSTANCE_MUTEX_HANDLE = None


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
    script = APP_DIR / "apply-update.cmd"
    script.write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                f'set "SOURCE={downloaded_exe}"',
                f'set "TARGET={target}"',
                ":wait",
                f'tasklist /FI "IMAGENAME eq {APP_SLUG}.exe" | find /I "{APP_SLUG}.exe" >nul',
                "if not errorlevel 1 (",
                "  timeout /t 1 /nobreak >nul",
                "  goto wait",
                ")",
                'copy /Y "%SOURCE%" "%TARGET%" >nul',
                "if errorlevel 1 exit /b 1",
                'start "" "%TARGET%"',
                'del "%SOURCE%" >nul 2>nul',
                'del "%~f0" >nul 2>nul',
                "",
            ]
        ),
        encoding="ascii",
    )

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=creation_flags)


def create_icon_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=(18, 128, 92, 255))
    draw.rounded_rectangle((29, 16, 35, 43), radius=3, fill=(255, 255, 255, 255))
    draw.arc((22, 28, 42, 50), 0, 180, fill=(255, 255, 255, 255), width=4)
    draw.line((32, 50, 32, 56), fill=(255, 255, 255, 255), width=4)
    return image


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
        self.size = 74
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
        self.set_state("idle")
        self.window.deiconify()

    def position(self) -> None:
        try:
            screen_width = self.window.winfo_screenwidth()
            screen_height = self.window.winfo_screenheight()
            x = max(0, screen_width - self.size - 26)
            y = max(0, screen_height - self.size - 82)
            self.window.geometry(f"{self.size}x{self.size}+{x}+{y}")
        except Exception:
            pass

    def set_state(self, state: str) -> None:
        self.state = state
        self.canvas.delete("all")

        colors = {
            "idle": ("#0f766e", "#14b8a6", "Klaar"),
            "recording": ("#b91c1c", "#ef4444", "Opname"),
            "processing": ("#a16207", "#f59e0b", "Transcriptie"),
        }
        shadow = "#000000"
        outline, fill, tooltip = colors.get(state, colors["idle"])

        self.canvas.create_oval(8, 8, 68, 68, fill=shadow, outline="", stipple="gray50")
        self.canvas.create_oval(6, 4, 66, 64, fill=fill, outline=outline, width=2)
        self.canvas.create_oval(12, 8, 36, 26, fill="#ffffff", outline="", stipple="gray25")

        if state == "processing":
            self.draw_processing_icon()
        else:
            self.draw_mic_icon()
            if state == "recording":
                self.canvas.create_oval(47, 13, 57, 23, fill="#ffffff", outline="")

        self.window.title(f"{APP_NAME} - {tooltip}")
        self.position()

    def draw_mic_icon(self) -> None:
        self.canvas.create_rectangle(29, 25, 45, 35, fill="#ffffff", outline="")
        self.canvas.create_oval(29, 17, 45, 33, fill="#ffffff", outline="")
        self.canvas.create_oval(29, 31, 45, 47, fill="#ffffff", outline="")
        self.canvas.create_line(24, 34, 24, 36, 25, 42, 30, 47, 37, 49, 44, 47, 49, 42, 50, 36, 50, 34, fill="#ffffff", width=3, smooth=True)
        self.canvas.create_line(37, 49, 37, 56, fill="#ffffff", width=3)
        self.canvas.create_line(30, 56, 44, 56, fill="#ffffff", width=3, capstyle="round")

    def draw_processing_icon(self) -> None:
        self.canvas.create_line(24, 20, 50, 20, fill="#ffffff", width=4, capstyle="round")
        self.canvas.create_line(28, 24, 46, 42, fill="#ffffff", width=4, capstyle="round")
        self.canvas.create_line(46, 24, 28, 42, fill="#ffffff", width=4, capstyle="round")
        self.canvas.create_line(24, 46, 50, 46, fill="#ffffff", width=4, capstyle="round")
        for x in (28, 37, 46):
            self.canvas.create_oval(x - 2, 55, x + 2, 59, fill="#ffffff", outline="")

    def destroy(self) -> None:
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
        try:
            wav_path = self.write_wav()
            duration, peak, rms = self.audio_stats()
            self.notify(f"Audio: {duration:.1f}s, piek {peak:.3f}, rms {rms:.3f}")
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
