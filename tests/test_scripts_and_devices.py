"""Tests that run on any OS: generated scripts and microphone resolution.

Windows-only libraries are stubbed when they are missing, so the platform
independent helpers can be tested on a development machine as well.
"""

import os
import unittest
from unittest import mock

from qt_support import install_missing_stubs


class GeneratedScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global windows_services
        install_missing_stubs()
        import windows_services

    def test_paths_under_a_known_root_become_environment_variables(self) -> None:
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Zoë\AppData\Local"}, clear=False):
            self.assertEqual(
                windows_services.path_with_env_var(r"C:\Users\Zoë\AppData\Local\Programs\x\x.exe"),
                r"%LOCALAPPDATA%\Programs\x\x.exe",
            )
            self.assertEqual(
                windows_services.path_with_env_var(r"C:\Users\Zoë\AppData\Local\Programs\x\x.exe", style="powershell"),
                r"$Env:LOCALAPPDATA\Programs\x\x.exe",
            )

    def test_paths_outside_a_known_root_are_kept_verbatim(self) -> None:
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Zoë\AppData\Local"}, clear=False):
            self.assertEqual(windows_services.path_with_env_var(r"D:\tools\x.exe"), r"D:\tools\x.exe")
            # A prefix must end on a separator, not halfway through a name.
            self.assertEqual(windows_services.path_with_env_var(r"C:\Users\Zoë\AppData\Locally"), r"C:\Users\Zoë\AppData\Locally")

    def test_autostart_command_avoids_the_literal_user_name(self) -> None:
        exe = r"C:\Users\Zoë\AppData\Local\Programs\groq-insert-dictation\groq-insert-dictation.exe"
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Zoë\AppData\Local"}, clear=False):
            with mock.patch.object(windows_services.sys, "frozen", True, create=True):
                with mock.patch.object(windows_services.sys, "executable", exe):
                    command = windows_services.current_launch_command()
        self.assertIn("%LOCALAPPDATA%", command)
        self.assertEqual(command, command.encode("ascii", "replace").decode("ascii"))

    def test_batch_bytes_use_crlf_and_survive_a_non_ascii_path(self) -> None:
        # No environment variable applies here, so the accented path stays in
        # the file; it must still be written instead of raising.
        payload = windows_services.cmd_script_bytes(["@echo off", r'start "" "D:\Zoë\x.exe"', ""])
        self.assertTrue(payload.startswith(b"@echo off\r\n"))
        self.assertTrue(payload.endswith(b"\r\n"))

    def test_isolated_test_profile_never_touches_autostart(self) -> None:
        with mock.patch.dict(os.environ, {"GROQ_DICTATION_PROFILE": "qa"}), \
                mock.patch.object(windows_services, "startup_cmd_path") as path:
            windows_services.set_autostart(True)
            windows_services.set_autostart(False)
        path.assert_not_called()
        with mock.patch.dict(os.environ, {"GROQ_DICTATION_PROFILE": "qa"}):
            self.assertTrue(windows_services.instance_mutex_name().endswith("SingleInstance-qa"))


class InputDeviceResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global engine, Config
        install_missing_stubs()
        import engine
        from config_store import Config

    def _engine(self) -> "engine.DictationEngine":
        dictation = engine.DictationEngine.__new__(engine.DictationEngine)
        dictation.input_device = None
        dictation.input_device_error = None
        return dictation

    def test_a_missing_microphone_does_not_raise(self) -> None:
        dictation = self._engine()
        with mock.patch.object(engine, "resolve_input_device", side_effect=RuntimeError("Geen input device gevonden voor 'Yeti'.")):
            dictation._resolve_device(Config(input_device="Yeti"))
        self.assertIsNone(dictation.input_device)
        self.assertIn("Yeti", dictation.input_device_error or "")

    def test_a_working_microphone_clears_an_earlier_error(self) -> None:
        dictation = self._engine()
        dictation.input_device_error = "oud probleem"
        with mock.patch.object(engine, "resolve_input_device", return_value=3):
            dictation._resolve_device(Config(input_device="3"))
        self.assertEqual(dictation.input_device, 3)
        self.assertIsNone(dictation.input_device_error)

    def test_windows_default_shows_the_real_endpoint_instead_of_sound_mapper(self):
        devices = [
            {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2},
            {"name": "Microfoonarray (Realtek High Definition Audio)", "max_input_channels": 2},
            {"name": "Luidsprekers", "max_input_channels": 0},
        ]
        with (
            mock.patch.object(engine.sd, "query_devices", return_value=devices),
            mock.patch.object(engine.sd, "query_hostapis", return_value=[
                {"name": "MME", "default_input_device": 0, "devices": [0]},
                {"name": "Windows WASAPI", "default_input_device": 1, "devices": [1, 2]},
            ]),
        ):
            options = engine.input_devices()
        self.assertEqual(options[0], ("", "Windows-standaard: Microfoonarray (Realtek High Definition Audio)"))
        self.assertEqual([device_id for device_id, _ in options], ["", "wasapi:Microfoonarray (Realtek High Definition Audio)"])

    def test_absent_default_input_is_explicit(self):
        with (
            mock.patch.object(engine.sd, "query_devices", return_value=[]),
            mock.patch.object(engine.sd, "query_hostapis", return_value=[
                {"name": "Windows WASAPI", "default_input_device": -1, "devices": []},
            ]),
        ):
            self.assertEqual(engine.input_devices(), [("", "Windows-standaard (geen microfoon beschikbaar)")])

    def test_named_selection_survives_renumbering_and_never_falls_back_after_unplug(self):
        devices = [
            {'name': 'Realtek', 'max_input_channels': 1},
            {'name': 'TONOR', 'max_input_channels': 1},
        ]
        with (
            mock.patch.object(engine.sd, 'query_devices', return_value=devices),
            mock.patch.object(engine.sd, 'query_hostapis', return_value=[{'name': 'Windows WASAPI', 'devices': [0, 1]}]),
        ):
            self.assertEqual(engine.resolve_input_device('wasapi:TONOR'), 1)
            devices.reverse()
            self.assertEqual(engine.resolve_input_device('wasapi:TONOR'), 0)
            devices[0]['name'] = 'Andere microfoon'
            with self.assertRaisesRegex(RuntimeError, 'niet aangesloten'):
                engine.resolve_input_device('wasapi:TONOR')


if __name__ == "__main__":
    unittest.main()
