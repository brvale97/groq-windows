"""Settings file and Credential Manager handling; runs on any OS."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qt_support import install_missing_stubs

install_missing_stubs()

import config_store  # noqa: E402


class ConfigStoreTests(unittest.TestCase):
    def patched(self, directory: str, **patches):
        settings_path = Path(directory) / "settings.json"
        managers = [
            mock.patch.object(config_store, "APP_DIR", Path(directory)),
            mock.patch.object(config_store, "SETTINGS_PATH", settings_path),
            mock.patch.object(config_store, "load_dotenv_values", return_value={}),
        ]
        managers += [mock.patch.object(config_store, name, value) for name, value in patches.items()]
        for manager in managers:
            manager.start()
            self.addCleanup(manager.stop)
        return settings_path

    def test_config_roundtrip_preserves_dictionary_replacements_and_audio_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = self.patched(
                directory, try_read_api_key_from_keyring=mock.Mock(return_value=(True, "secret")),
            )
            config = config_store.Config(
                api_key="secret",
                prompt="Nederlandse vergadering",
                custom_words=("Groq", "Clinon"),
                word_replacements=(("Grok", "Groq"), ("Grog", "Groq")),
                sample_rate=48_000,
                channels=2,
                remove_final_period=True,
                input_devices=("wasapi:Headset Microphone (Soundcore Motion Boom)", "wasapi:Microphone (Realtek(R) Audio)"),
            )
            config_store.save_config(config)
            stored = json.loads(settings_path.read_text(encoding="utf-8"))
            loaded = config_store.load_config()
            self.assertEqual(stored["input_devices"], list(config.input_devices))
            # Older versions (after a rollback) read one microphone: the first priority.
            self.assertEqual(stored["input_device"], "wasapi:Headset Microphone (Soundcore Motion Boom)")
            self.assertEqual(stored["custom_words"], ["Groq", "Clinon"])
            self.assertEqual(stored["word_replacements"], [["Grok", "Groq"], ["Grog", "Groq"]])
            self.assertEqual(stored["sample_rate"], 48_000)
            self.assertEqual(stored["channels"], 2)
            self.assertTrue(stored["remove_final_period"])
            self.assertNotIn("api_key", stored)
            self.assertEqual(loaded, config)

    LEGACY_0_2_7 = {
        "model": "whisper-large-v3", "language": "nl", "prompt": "Overleg",
        "custom_words": ["Groq"], "word_replacements": [["Grok", "Groq"]], "shortcut": "insert",
        "input_device": "wasapi:Microphone (Realtek(R) Audio)", "sample_rate": 16000, "channels": 1,
        "paste_after_transcription": True, "remove_final_period": False, "auto_paragraphs": True,
        "cleanup_enabled": True, "cleanup_model": "openai/gpt-oss-120b", "cleanup_prompt": "",
        "autostart": True,
    }

    def load_settings(self, data: dict) -> tuple[Path, str, "config_store.Config"]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_path = self.patched(
            directory.name, try_read_api_key_from_keyring=mock.Mock(return_value=(True, "secret")),
        )
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        settings_path.write_text(text, encoding="utf-8")
        return settings_path, text, config_store.load_config()

    def test_unchanged_settings_stay_byte_identical(self) -> None:
        current = {}
        for key, value in self.LEGACY_0_2_7.items():
            current[key] = value
            if key == "input_device":
                current["input_devices"] = [value, "wasapi:Headset Microphone (Soundcore Motion Boom)"]
        settings_path, text, loaded = self.load_settings(current)
        self.assertEqual(loaded.shortcut, "insert")
        self.assertEqual(loaded.input_devices, tuple(current["input_devices"]))
        config_store.save_config(loaded)
        # Startup re-saves settings; an unchanged file must stay byte-identical.
        self.assertEqual(settings_path.read_text(encoding="utf-8"), text)

    def test_the_single_microphone_of_0_2_7_becomes_priority_one(self) -> None:
        settings_path, _text, loaded = self.load_settings(self.LEGACY_0_2_7)
        self.assertEqual(loaded.input_devices, ("wasapi:Microphone (Realtek(R) Audio)",))
        config_store.save_config(loaded)
        stored = json.loads(settings_path.read_text(encoding="utf-8"))
        # Every existing setting keeps its value; only the list is added.
        self.assertEqual({k: v for k, v in stored.items() if k != "input_devices"}, self.LEGACY_0_2_7)
        self.assertEqual(stored["input_devices"], ["wasapi:Microphone (Realtek(R) Audio)"])
        keys = list(stored)
        self.assertEqual(keys.index("input_devices"), keys.index("input_device") + 1)

    def test_windows_default_of_0_2_7_becomes_an_empty_list(self) -> None:
        _path, _text, loaded = self.load_settings({**self.LEGACY_0_2_7, "input_device": ""})
        self.assertEqual(loaded.input_devices, ())

    def test_a_microphone_chosen_in_an_older_version_moves_to_the_top(self) -> None:
        # After a rollback 0.2.7 only rewrites "input_device".
        _path, _text, loaded = self.load_settings({
            "input_device": "wasapi:B", "input_devices": ["wasapi:A", "wasapi:B", "wasapi:C"],
        })
        self.assertEqual(loaded.input_devices, ("wasapi:B", "wasapi:A", "wasapi:C"))
        _path, _text, loaded = self.load_settings({"input_device": "wasapi:D", "input_devices": ["wasapi:A"]})
        self.assertEqual(loaded.input_devices, ("wasapi:D", "wasapi:A"))

    def test_invalid_priority_entries_are_dropped(self) -> None:
        _path, _text, loaded = self.load_settings({
            "input_device": "wasapi:A", "input_devices": ["wasapi:A", "", 3, None, "wasapi:A", "wasapi:B"],
        })
        self.assertEqual(loaded.input_devices, ("wasapi:A", "wasapi:B"))

    def test_legacy_settings_load_with_an_empty_dictionary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = self.patched(directory, try_read_api_key_from_keyring=mock.Mock(return_value=(True, "")))
            settings_path.write_text(json.dumps({"prompt": "Bestaande prompt"}), encoding="utf-8")
            config = config_store.load_config()
            self.assertEqual(config.prompt, "Bestaande prompt")
            self.assertEqual(config.custom_words, ())
            self.assertEqual(config.word_replacements, ())
            self.assertFalse(config.remove_final_period)
            self.assertTrue(config.auto_paragraphs)
            # AI cleanup is on for existing installs too, with the built-in prompt.
            self.assertTrue(config.cleanup_enabled)
            self.assertEqual(config.cleanup_model, "openai/gpt-oss-120b")
            self.assertEqual(config.cleanup_prompt, "")

    def test_corrupt_settings_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = self.patched(directory, try_read_api_key_from_keyring=mock.Mock(return_value=(True, "")))
            settings_path.write_text("{not json", encoding="utf-8")
            self.assertEqual(config_store.load_config().shortcut, "insert")

    def test_clearing_api_key_attempts_keyring_delete_after_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            write_key = mock.Mock(return_value=True)
            self.patched(
                directory, try_read_api_key_from_keyring=mock.Mock(return_value=(False, "")),
                write_api_key_to_keyring=write_key,
            )
            config_store.save_config(config_store.Config(api_key=""))
            write_key.assert_called_once_with("")

    def test_startup_save_never_deletes_secret_after_temporary_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            read_key = mock.Mock(return_value=(False, ""))
            write_key = mock.Mock()
            self.patched(directory, try_read_api_key_from_keyring=read_key, write_api_key_to_keyring=write_key)
            config = config_store.load_config()
            config_store.save_config(config, allow_keyring_mutation=config.keyring_read_succeeded)
            write_key.assert_not_called()
            self.assertEqual(read_key.call_count, 1)

    def test_startup_save_never_overwrites_secret_with_stale_fallback_after_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            read_key = mock.Mock(return_value=(False, ""))
            write_key = mock.Mock()
            settings_path = self.patched(directory, try_read_api_key_from_keyring=read_key, write_api_key_to_keyring=write_key)
            settings_path.write_text(json.dumps({"api_key": "old-fallback"}), encoding="utf-8")
            config = config_store.load_config()
            config_store.save_config(config, allow_keyring_mutation=config.keyring_read_succeeded)
            self.assertEqual(config.api_key, "old-fallback")
            write_key.assert_not_called()
            self.assertEqual(read_key.call_count, 1)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["api_key"], "old-fallback")

    def test_keyring_failure_keeps_the_key_in_the_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = self.patched(
                directory, try_read_api_key_from_keyring=mock.Mock(return_value=(True, "")),
                write_api_key_to_keyring=mock.Mock(return_value=False),
            )
            config_store.save_config(config_store.Config(api_key="new"))
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["api_key"], "new")

    def test_isolated_profile_uses_its_own_folder_and_secret(self) -> None:
        with mock.patch.dict(os.environ, {"GROQ_DICTATION_PROFILE": "qa", "APPDATA": r"C:\Users\x\AppData\Roaming"}):
            self.assertEqual(config_store.profile_suffix(), "-qa")
            self.assertEqual(config_store.app_data_dir().name, "GroqInsertDictation-qa")
        with mock.patch.dict(os.environ, {"GROQ_DICTATION_PROFILE": "../evil"}):
            self.assertEqual(config_store.profile_suffix(), "")
        with mock.patch.dict(os.environ, {"GROQ_DICTATION_PROFILE": ""}):
            self.assertEqual(config_store.profile_suffix(), "")


if __name__ == "__main__":
    unittest.main()
