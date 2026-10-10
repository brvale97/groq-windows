"""App identity, data paths, settings and the Groq API key in Credential Manager.

Nothing in here imports a UI toolkit, so the engine, the updater and the tests
can use it without a desktop session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

import keyring

from dictation_core import normalize_custom_words, normalize_word_replacements

APP_VERSION = "0.2.2"
APP_NAME = "Groq Insert Dictation"
APP_SLUG = "GroqInsertDictation"
GITHUB_REPO = "brvale97/groq-windows"


def profile_name() -> str:
    """Optional isolated profile for automated runs next to a real installation.

    ``GROQ_DICTATION_PROFILE=qa`` gives the process its own settings folder,
    Credential Manager entry, autostart file and single-instance lock, so a
    test build can run on a PC where the installed app keeps working.
    """
    raw = os.getenv("GROQ_DICTATION_PROFILE", "").strip()
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", raw) else ""


def profile_suffix() -> str:
    name = profile_name()
    return f"-{name}" if name else ""


KEYRING_SERVICE = APP_SLUG + profile_suffix()
KEYRING_USER = "groq_api_key"


def app_data_dir() -> Path:
    root = os.getenv("APPDATA")
    if root:
        return Path(root) / (APP_SLUG + profile_suffix())
    return Path.home() / f".{APP_SLUG}{profile_suffix()}"


APP_DIR = app_data_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
HISTORY_PATH = APP_DIR / "history.json"
RECORDINGS_DIR = APP_DIR / "recordings"
LOG_PATH = APP_DIR / "app.log"
SOUNDS_DIR = APP_DIR / "sounds"
ICON_PATH = APP_DIR / "app.ico"


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
    remove_final_period: bool = False
    auto_paragraphs: bool = True
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
        remove_final_period=bool(data.get("remove_final_period", False)),
        auto_paragraphs=bool(data.get("auto_paragraphs", True)),
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
