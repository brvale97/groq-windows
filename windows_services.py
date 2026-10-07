"""Windows integration without UI: single instance, autostart and launch commands."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import config_store
from config_store import APP_SLUG, profile_name, profile_suffix

_INSTANCE_MUTEX_HANDLE = None
_INSTANCE_LOCK_FILE = None
ERROR_ALREADY_EXISTS = 183


def instance_mutex_name() -> str:
    return f"Local\\{APP_SLUG}SingleInstance{profile_suffix()}"


def acquire_single_instance_lock() -> bool:
    if os.name != "nt":
        return True

    import ctypes
    import ctypes.wintypes
    import msvcrt

    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE

    config_store.APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = config_store.APP_DIR / "instance.lock"
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
    _INSTANCE_MUTEX_HANDLE = kernel32.CreateMutexW(None, True, instance_mutex_name())
    if not _INSTANCE_MUTEX_HANDLE:
        _INSTANCE_LOCK_FILE.close()
        _INSTANCE_LOCK_FILE = None
        return False

    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        _INSTANCE_MUTEX_HANDLE = None
        _INSTANCE_LOCK_FILE.close()
        _INSTANCE_LOCK_FILE = None
        return False

    return True


def release_single_instance_lock() -> None:
    """Hand the single-instance claim back so a successor can take it."""
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE

    if os.name != "nt":
        return

    import ctypes
    import ctypes.wintypes
    import msvcrt

    if _INSTANCE_MUTEX_HANDLE is not None:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = ctypes.wintypes.BOOL
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
            kernel32.ReleaseMutex(_INSTANCE_MUTEX_HANDLE)
            kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        except Exception as exc:
            logging.warning("Could not release single-instance mutex: %s", exc)
        _INSTANCE_MUTEX_HANDLE = None

    if _INSTANCE_LOCK_FILE is not None:
        try:
            _INSTANCE_LOCK_FILE.seek(0)
            msvcrt.locking(_INSTANCE_LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception as exc:
            logging.warning("Could not unlock single-instance file: %s", exc)
        try:
            _INSTANCE_LOCK_FILE.close()
        except Exception:
            pass
        _INSTANCE_LOCK_FILE = None


# Prefixes that are swapped for an environment variable so generated scripts
# stay ASCII even when the Windows user name contains accented characters.
ENV_PATH_VARIABLES = ("LOCALAPPDATA", "APPDATA", "USERPROFILE")


def path_with_env_var(path: Path | str, style: str = "cmd") -> str:
    """Render *path* with a %VAR% / $Env:VAR prefix when one applies."""
    text = str(path)
    for name in ENV_PATH_VARIABLES:
        base = (os.getenv(name) or "").rstrip("\\/")
        if not base or len(text) <= len(base):
            continue
        if text[: len(base)].lower() != base.lower() or text[len(base)] not in "\\/":
            continue
        remainder = text[len(base) :]
        return f"$Env:{name}{remainder}" if style == "powershell" else f"%{name}%{remainder}"
    return text


def cmd_script_encoding() -> str:
    """cmd.exe reads batch files in the console OEM code page, not in UTF-8."""
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "mbcs"


def cmd_script_bytes(lines: list[str]) -> bytes:
    text = "\r\n".join(lines)
    try:
        return text.encode(cmd_script_encoding())
    except (LookupError, UnicodeEncodeError):
        # Better a path cmd.exe may mangle than no script at all.
        return text.encode("utf-8", errors="replace")


def startup_cmd_path() -> Path:
    return (
        Path(os.getenv("APPDATA", str(Path.home())))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / f"{APP_SLUG}{profile_suffix()}.cmd"
    )


def entry_script() -> Path:
    return Path(__file__).resolve().with_name("app.py")


def current_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'start "" "{path_with_env_var(sys.executable)}"'

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else Path(sys.executable)
    return f'start "" "{path_with_env_var(launcher)}" "{path_with_env_var(entry_script())}"'


def set_autostart(enabled: bool) -> None:
    if profile_name():
        # An isolated test profile must never start itself at the next logon.
        return
    path = startup_cmd_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = cmd_script_bytes(["@echo off", current_launch_command(), ""])
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
    else:
        path.unlink(missing_ok=True)


def autostart_enabled() -> bool:
    return startup_cmd_path().exists()
