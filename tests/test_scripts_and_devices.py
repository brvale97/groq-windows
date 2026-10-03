"""Tests that run on any OS: generated scripts and microphone resolution.

``app`` pulls in a handful of Windows-only libraries. They are stubbed here
when they are missing, so the platform independent helpers can be tested on a
development machine as well.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_missing_stubs() -> None:
    def stub(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        module.__getattr__ = lambda _attr: types.SimpleNamespace()  # type: ignore[attr-defined]
        sys.modules[name] = module
        return module

    for name in ("sounddevice", "pystray", "pyautogui", "pyperclip"):
        try:
            __import__(name)
        except Exception:
            stub(name)

    try:
        import groq  # noqa: F401
    except Exception:
        stub("groq").Groq = object  # type: ignore[attr-defined]

    try:
        import keyring  # noqa: F401
    except Exception:
        keyring_stub = stub("keyring")
        errors = types.ModuleType("keyring.errors")
        errors.KeyringError = Exception  # type: ignore[attr-defined]
        keyring_stub.errors = errors  # type: ignore[attr-defined]
        sys.modules["keyring.errors"] = errors


class GeneratedScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global app
        _install_missing_stubs()
        import app

    def test_paths_under_a_known_root_become_environment_variables(self) -> None:
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Zoë\AppData\Local"}, clear=False):
            self.assertEqual(
                app.path_with_env_var(r"C:\Users\Zoë\AppData\Local\Programs\x\x.exe"),
                r"%LOCALAPPDATA%\Programs\x\x.exe",
            )
            self.assertEqual(
                app.path_with_env_var(r"C:\Users\Zoë\AppData\Local\Programs\x\x.exe", style="powershell"),
                r"$Env:LOCALAPPDATA\Programs\x\x.exe",
            )

    def test_paths_outside_a_known_root_are_kept_verbatim(self) -> None:
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Zoë\AppData\Local"}, clear=False):
            self.assertEqual(app.path_with_env_var(r"D:\tools\x.exe"), r"D:\tools\x.exe")
            # A prefix must end on a separator, not halfway through a name.
            self.assertEqual(app.path_with_env_var(r"C:\Users\Zoë\AppData\Locally"), r"C:\Users\Zoë\AppData\Locally")

    def test_autostart_command_avoids_the_literal_user_name(self) -> None:
        exe = r"C:\Users\Zoë\AppData\Local\Programs\groq-insert-dictation\groq-insert-dictation.exe"
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Zoë\AppData\Local"}, clear=False):
            with mock.patch.object(app.sys, "frozen", True, create=True):
                with mock.patch.object(app.sys, "executable", exe):
                    command = app.current_launch_command()
        self.assertIn("%LOCALAPPDATA%", command)
        self.assertEqual(command, command.encode("ascii", "replace").decode("ascii"))

    def test_batch_bytes_use_crlf_and_survive_a_non_ascii_path(self) -> None:
        # No environment variable applies here, so the accented path stays in
        # the file; it must still be written instead of raising.
        payload = app.cmd_script_bytes(["@echo off", r'start "" "D:\Zoë\x.exe"', ""])
        self.assertTrue(payload.startswith(b"@echo off\r\n"))
        self.assertTrue(payload.endswith(b"\r\n"))

    def test_update_scripts_are_written_so_windows_can_read_the_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app_dir = Path(raw)
            with mock.patch.object(app, "APP_DIR", app_dir):
                with mock.patch.object(app.subprocess, "Popen") as popen:
                    app.launch_update_script(app_dir / "download.exe")
            self.assertTrue(popen.called)

            ps_bytes = (app_dir / "apply-update.ps1").read_bytes()
            # Windows PowerShell 5.1 falls back to ANSI without this BOM.
            self.assertTrue(ps_bytes.startswith(b"\xef\xbb\xbf"))

            cmd_bytes = (app_dir / "apply-update.cmd").read_bytes()
            self.assertIn(b"powershell.exe", cmd_bytes)
            self.assertIn(b'del "%~f0"', cmd_bytes)


class InputDeviceResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global app
        _install_missing_stubs()
        import app

    def _engine(self) -> "app.DictationEngine":
        engine = app.DictationEngine.__new__(app.DictationEngine)
        engine.input_device = None
        engine.input_device_error = None
        return engine

    def test_a_missing_microphone_does_not_raise(self) -> None:
        engine = self._engine()
        with mock.patch.object(app, "resolve_input_device", side_effect=RuntimeError("Geen input device gevonden voor 'Yeti'.")):
            engine._resolve_device(app.Config(input_device="Yeti"))
        self.assertIsNone(engine.input_device)
        self.assertIn("Yeti", engine.input_device_error or "")

    def test_a_working_microphone_clears_an_earlier_error(self) -> None:
        engine = self._engine()
        engine.input_device_error = "oud probleem"
        with mock.patch.object(app, "resolve_input_device", return_value=3):
            engine._resolve_device(app.Config(input_device="3"))
        self.assertEqual(engine.input_device, 3)
        self.assertIsNone(engine.input_device_error)


if __name__ == "__main__":
    unittest.main()
