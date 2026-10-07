"""Release selection and safe staging of the folder build; runs on any OS."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from qt_support import install_missing_stubs

install_missing_stubs()

import updater  # noqa: E402


def release(*names, tag="v9.0.0"):
    return {
        "tag_name": tag, "html_url": "https://example.invalid/release",
        "assets": [{"name": name, "browser_download_url": f"https://example.invalid/{name}"} for name in names],
    }


def folder_zip(path: Path, *, exe=b"MZ" + b"\0" * 2000, wrap: str = "", extra: dict | None = None) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        prefix = f"{wrap}/" if wrap else ""
        bundle.writestr(prefix + "GroqInsertDictation.exe", exe)
        bundle.writestr(prefix + "_internal/python313.dll", b"dll")
        for name, data in (extra or {}).items():
            bundle.writestr(name, data)


class ReleaseSelectionTests(unittest.TestCase):
    def test_versions_compare_numerically(self):
        self.assertTrue(updater.is_newer_version("v0.2.0", "0.1.27"))
        self.assertTrue(updater.is_newer_version("0.1.10", "0.1.9"))
        self.assertFalse(updater.is_newer_version("v0.2.0", "0.2.0"))

    def test_folder_build_is_preferred_for_every_layout(self):
        for layout in ("folder", "onefile"):
            update = updater.select_update(release("GroqInsertDictation.exe", "GroqInsertDictation-win64.zip", "GroqInsertDictation.build.json"), layout)
            self.assertEqual((update.kind, update.asset_name), ("folder", "GroqInsertDictation-win64.zip"))
            self.assertTrue(update.manifest_url.endswith("GroqInsertDictation.build.json"))

    def test_folder_install_never_takes_a_single_exe(self):
        # Replacing only the launcher of a folder build would leave a broken app.
        self.assertIsNone(updater.select_update(release("GroqInsertDictation.exe"), "folder"))
        update = updater.select_update(release("GroqInsertDictation.exe"), "onefile")
        self.assertEqual(update.kind, "exe")

    def test_manifest_with_a_byte_order_mark_is_accepted(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        body = '\ufeff{"version": "9.0.0", "zip_sha256": "abc"}'.encode("utf-8")
        with mock.patch.object(updater.urllib.request, "urlopen", return_value=Response(body)):
            self.assertEqual(updater._get_json("https://x/manifest", 5)["zip_sha256"], "abc")

    def test_older_or_equal_releases_are_ignored(self):
        self.assertIsNone(updater.select_update(release("GroqInsertDictation-win64.zip", tag="v0.1.0"), "folder"))
        self.assertIsNone(updater.select_update(release("GroqInsertDictation-win64.zip", tag=""), "folder"))


class StagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_flat_and_wrapped_zips_are_staged(self):
        for wrap in ("", "GroqInsertDictation"):
            archive = self.root / f"build{wrap}.zip"
            folder_zip(archive, wrap=wrap)
            staged = updater.extract_folder_build(archive, self.root / f"staging{wrap}")
            self.assertTrue((staged / "GroqInsertDictation.exe").is_file())
            self.assertTrue((staged / "_internal").is_dir())

    def test_build_zip_uses_portable_names_and_stages(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("make_zip", Path(updater.__file__).with_name("packaging") / "make_zip.py")
        make_zip = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_zip)
        folder = self.root / "GroqInsertDictation"
        (folder / "_internal" / "PySide6").mkdir(parents=True)
        (folder / "GroqInsertDictation.exe").write_bytes(b"MZ" + b"\0" * 2000)
        (folder / "_internal" / "PySide6" / "Qt6Core.dll").write_bytes(b"dll")
        archive = self.root / "GroqInsertDictation-win64.zip"
        self.assertEqual(make_zip.main(folder, archive), 0)
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn("_internal/PySide6/Qt6Core.dll", bundle.namelist())
            self.assertFalse(any("\\" in name for name in bundle.namelist()))
        staged = updater.extract_folder_build(archive, self.root / "staged")
        self.assertTrue((staged / "_internal" / "PySide6" / "Qt6Core.dll").is_file())

    def test_zip_slip_and_invalid_apps_are_rejected(self):
        archive = self.root / "evil.zip"
        folder_zip(archive, extra={"../outside.txt": b"x"})
        with self.assertRaisesRegex(RuntimeError, "ongeldig pad"):
            updater.extract_folder_build(archive, self.root / "staging")
        self.assertFalse((self.root / "outside.txt").exists())
        archive = self.root / "fake.zip"
        folder_zip(archive, exe=b"not an exe" * 200)
        with self.assertRaisesRegex(RuntimeError, "geen geldige"):
            updater.extract_folder_build(archive, self.root / "staging2")
        incomplete = self.root / "incomplete.zip"
        with zipfile.ZipFile(incomplete, "w") as bundle:
            bundle.writestr("GroqInsertDictation.exe", b"MZ" + b"\0" * 2000)
        with self.assertRaisesRegex(RuntimeError, "geen geldige"):
            updater.extract_folder_build(incomplete, self.root / "staging3")

    def test_download_verifies_the_manifest_checksum_and_stages_beside_the_app(self):
        archive = self.root / "src.zip"
        folder_zip(archive)
        payload = archive.read_bytes()
        install = self.root / "Programs" / "GroqInsertDictation" / "GroqInsertDictation.exe"
        install.parent.mkdir(parents=True)

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        update = updater.UpdateInfo("9.0.0", "v9.0.0", "", "GroqInsertDictation-win64.zip", "https://x/zip", "folder", "https://x/manifest")
        with (
            mock.patch.object(updater.config_store, "APP_DIR", self.root / "appdata"),
            mock.patch.object(updater, "current_exe_path", return_value=install),
            mock.patch.object(updater.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(payload)),
        ):
            import hashlib

            digest = hashlib.sha256(payload).hexdigest()
            with mock.patch.object(updater, "_get_json", return_value={"version": "9.0.0", "zip_sha256": digest}):
                staged = updater.download_update(update)
            self.assertEqual(staged.parent, install.parent)
            self.assertTrue((staged / "_internal" / "python313.dll").exists())
            self.assertEqual(list((self.root / "appdata" / "updates").iterdir()), [])
            with mock.patch.object(updater, "_get_json", return_value={"version": "9.0.0", "zip_sha256": "0" * 64}):
                with self.assertRaisesRegex(RuntimeError, "controlesom"):
                    updater.download_update(update)

    def test_swap_script_backs_up_rolls_back_and_waits_for_confirmation(self):
        lines = "\n".join(updater.update_script_lines(
            Path(r"C:\Users\Zoë\AppData\Local\Programs\GroqInsertDictation\.staged-v9"), "folder",
            Path(r"C:\Users\Zoë\AppData\Local\Programs\GroqInsertDictation\GroqInsertDictation.exe"),
            1234, Path(r"C:\x\update.log"), Path(r"C:\x\update-confirmed.txt"),
        ))
        for snippet in (
            "Wait-Process -Id $PidToWait", "Move-Item -LiteralPath $Internal -Destination $InternalBackup",
            "Move-Item -LiteralPath $NewInternal -Destination $Internal", "Restore", "$Process.HasExited",
            "Test-Path -LiteralPath $Confirm", "Zoë",
        ):
            self.assertIn(snippet, lines)
        # Rollback is only attempted after the swap actually began.
        self.assertIn("if ($Swapped) {", lines)

    def test_scripts_are_written_so_windows_can_read_the_paths(self):
        with mock.patch.object(updater.config_store, "APP_DIR", self.root), \
                mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.launch_update_script(self.root / "staged", "folder")
        self.assertTrue(popen.called)
        ps_bytes = (self.root / "apply-update.ps1").read_bytes()
        self.assertTrue(ps_bytes.startswith(b"\xef\xbb\xbf"))  # PowerShell 5.1 needs the BOM.
        cmd_bytes = (self.root / "apply-update.cmd").read_bytes()
        self.assertIn(b"powershell.exe", cmd_bytes)
        self.assertIn(b'del "%~f0"', cmd_bytes)

    def test_confirmed_start_removes_backups_of_both_layouts(self):
        install = self.root / "GroqInsertDictation.exe"
        Path(f"{install}.bak").write_bytes(b"old")
        (self.root / "_internal.bak").mkdir()
        (self.root / "_internal.bak" / "x.dll").write_bytes(b"old")
        with (
            mock.patch.object(updater.sys, "frozen", True, create=True),
            mock.patch.object(updater, "current_exe_path", return_value=install),
            mock.patch.object(updater.config_store, "APP_DIR", self.root / "appdata"),
        ):
            (self.root / "appdata").mkdir()
            updater.confirm_startup_and_cleanup()
        self.assertFalse(Path(f"{install}.bak").exists())
        self.assertFalse((self.root / "_internal.bak").exists())
        self.assertTrue((self.root / "appdata" / "update-confirmed.txt").exists())


if __name__ == "__main__":
    unittest.main()
