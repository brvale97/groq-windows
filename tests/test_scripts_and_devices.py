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

    SOUNDCORE = "wasapi:Headset Microphone (Soundcore Motion Boom)"
    RODE = "wasapi:Microfoon (Rode NT-USB Mini)"

    def open_input(self, selectors, *, connected=(), broken=(), default="Microfoonmatrix (Realtek(R) Audio)",
                   default_opens=True):
        """Run _open_input against fake devices; returns (choice or error, opened devices, calls)."""
        indices = {selector: index for index, selector in enumerate(connected, start=10)}
        calls = []
        opened = []

        def resolve(selector):
            calls.append(("resolve", selector))
            if selector not in indices:
                raise RuntimeError(f"Microfoon {selector!r} is niet aangesloten of niet beschikbaar.")
            return indices[selector]

        class FakeStream:
            def __init__(self, *, device, **kwargs):
                self.device, self.kwargs, self.closed = device, kwargs, False

            def start(self):
                broken_devices = {indices[selector] for selector in broken}
                if self.device in broken_devices or (self.device is None and not default_opens):
                    raise RuntimeError("Device unavailable [PaErrorCode -9985]")
                opened.append(self.device)

            def close(self):
                self.closed = True

        dictation = engine.DictationEngine.__new__(engine.DictationEngine)
        dictation.refresh_inputs = lambda: calls.append(("refresh",))
        dictation.audio_callback = lambda *args: None
        session = engine.RecordingSession(
            input_device=None, sample_rate=16_000, channels=1, model="m", language="nl", prompt="",
            word_replacements=(), paste_after_transcription=True, remove_final_period=False, client=object(),
        )
        with (
            mock.patch.object(engine, "resolve_input_device", side_effect=resolve),
            mock.patch.object(engine, "default_input_name", return_value=default),
            mock.patch.object(engine.sd, "InputStream", FakeStream, create=True),
            mock.patch.object(engine.sd, "WasapiSettings", lambda **kwargs: kwargs, create=True),
        ):
            try:
                stream, choice = dictation._open_input(tuple(selectors), session)
            except engine.NoMicrophoneError as exc:
                return exc, opened, calls
        self.assertEqual(stream.device, choice.device)
        return choice, opened, calls

    def test_the_first_connected_microphone_wins_and_the_list_is_read_again_first(self) -> None:
        choice, opened, calls = self.open_input([self.SOUNDCORE, self.RODE], connected=[self.SOUNDCORE, self.RODE])
        self.assertEqual(calls[0], ("refresh",))
        self.assertEqual((choice.priority, choice.name, choice.wasapi, choice.fallback), (1, "Headset Microphone (Soundcore Motion Boom)", True, False))
        self.assertEqual(opened, [10])

    def test_a_disconnected_headset_falls_back_to_the_next_priority(self) -> None:
        choice, opened, _calls = self.open_input([self.SOUNDCORE, self.RODE], connected=[self.RODE])
        self.assertEqual((choice.priority, choice.name), (2, "Microfoon (Rode NT-USB Mini)"))
        self.assertEqual(choice.skipped, ("Headset Microphone (Soundcore Motion Boom)",))
        self.assertTrue(choice.skipped_missing)
        self.assertTrue(choice.fallback)
        self.assertEqual(opened, [10])

    def test_a_microphone_that_cannot_be_opened_is_skipped_too(self) -> None:
        choice, opened, _calls = self.open_input(
            [self.SOUNDCORE, self.RODE], connected=[self.SOUNDCORE, self.RODE], broken=[self.SOUNDCORE],
        )
        self.assertEqual(choice.priority, 2)
        self.assertFalse(choice.skipped_missing)
        self.assertEqual(opened, [11])

    def test_without_a_safety_net_missing_microphones_mean_no_microphone(self) -> None:
        error, opened, _calls = self.open_input([self.SOUNDCORE, self.RODE])
        self.assertIsInstance(error, engine.NoMicrophoneError)
        self.assertFalse(error.found)
        self.assertEqual(opened, [])

    def test_windows_standaard_is_an_entry_like_any_other(self) -> None:
        choice, opened, _calls = self.open_input([self.SOUNDCORE, "", self.RODE], connected=[self.RODE])
        self.assertEqual((choice.priority, choice.device, choice.wasapi, choice.windows_default), (2, None, False, True))
        self.assertEqual(choice.name, "Microfoonmatrix (Realtek(R) Audio)")
        self.assertEqual(choice.skipped, ("Headset Microphone (Soundcore Motion Boom)",))
        self.assertEqual(opened, [None])
        # Without a Windows default microphone the entry is skipped like a missing one.
        choice, opened, _calls = self.open_input(["", self.RODE], connected=[self.RODE], default=None)
        self.assertEqual((choice.priority, choice.skipped), (2, ("Windows-standaard",)))

    def test_an_empty_list_just_uses_the_windows_default(self) -> None:
        choice, opened, calls = self.open_input([])
        self.assertEqual((choice.priority, choice.fallback, choice.windows_default), (1, False, True))
        self.assertEqual(calls, [("refresh",)])
        self.assertEqual(opened, [None])

    def test_no_microphone_at_all_is_reported(self) -> None:
        error, opened, _calls = self.open_input([self.SOUNDCORE, ""], default=None, default_opens=False)
        self.assertIsInstance(error, engine.NoMicrophoneError)
        self.assertFalse(error.found)
        self.assertIn("Geen microfoon gevonden", str(error))
        self.assertEqual(opened, [])
        error, _opened, _calls = self.open_input([self.SOUNDCORE, ""], default_opens=False)
        self.assertTrue(error.found)  # Windows has a default microphone, it just would not open.
        self.assertIn("kon worden geopend", str(error))

    def test_a_failing_refresh_does_not_block_the_recording(self) -> None:
        dictation = engine.DictationEngine.__new__(engine.DictationEngine)
        dictation.refresh_inputs = mock.Mock(side_effect=RuntimeError("PortAudio"))
        dictation._open_stream = mock.Mock(return_value="stream")
        with mock.patch.object(engine, "default_input_name", return_value="Realtek"):
            stream, choice = dictation._open_input((), mock.Mock())
        self.assertEqual((stream, choice.name), ("stream", "Realtek"))

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
