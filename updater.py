"""GitHub release check and self-update for the folder (onedir) build.

Release assets
--------------
``GroqInsertDictation-win64.zip``
    The folder build: ``GroqInsertDictation.exe`` plus ``_internal``. Versions
    from 0.2.0 install only this asset.
``GroqInsertDictation.exe``
    A self-contained single-file build. Updaters up to 0.1.27 replace just one
    exe; they must never receive the folder build's small launcher, which
    cannot run without ``_internal``. This bridge build keeps them working and
    moves them to the folder layout on their next update.
``GroqInsertDictation.build.json``
    Manifest with the SHA-256 of both assets; verified when present.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import config_store
from config_store import APP_SLUG, APP_VERSION, GITHUB_REPO
from windows_services import cmd_script_bytes, path_with_env_var

LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
EXE_NAME = f"{APP_SLUG}.exe"
ZIP_NAME = f"{APP_SLUG}-win64.zip"
MANIFEST_NAME = f"{APP_SLUG}.build.json"
INTERNAL_DIR = "_internal"
CONFIRM_FILE = "update-confirmed.txt"


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    url: str
    asset_name: str
    download_url: str
    kind: str = "exe"  # "folder" (zip) or "exe" (legacy single file)
    manifest_url: str = ""


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


def current_exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_SLUG / EXE_NAME


def install_layout() -> str:
    """``folder`` for the onedir build, ``onefile`` for the single exe, else ``source``."""
    if not getattr(sys, "frozen", False):
        return "source"
    bundle = Path(getattr(sys, "_MEIPASS", "")).resolve()
    internal = (Path(sys.executable).parent / INTERNAL_DIR).resolve()
    return "folder" if bundle == internal else "onefile"


def select_update(release: dict, layout: str) -> UpdateInfo | None:
    tag = str(release.get("tag_name", "")).strip()
    if not tag or not is_newer_version(tag):
        return None
    assets = {str(asset.get("name", "")).lower(): asset for asset in release.get("assets") or []}
    manifest = assets.get(MANIFEST_NAME.lower(), {})
    common = dict(
        version=tag.lstrip("v"), tag=tag, url=str(release.get("html_url", "")),
        manifest_url=str(manifest.get("browser_download_url", "")),
    )
    folder = assets.get(ZIP_NAME.lower())
    if folder and folder.get("browser_download_url"):
        return UpdateInfo(asset_name=folder["name"], download_url=folder["browser_download_url"], kind="folder", **common)
    single = assets.get(EXE_NAME.lower())
    if layout == "onefile" and single and single.get("browser_download_url"):
        return UpdateInfo(asset_name=single["name"], download_url=single["browser_download_url"], kind="exe", **common)
    if single:
        # Replacing only the launcher of a folder build would break it.
        logging.warning("Release %s has no folder build; skipping the update for layout %s.", tag, layout)
    return None


def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": APP_SLUG},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # utf-8-sig: Windows PowerShell 5.1 writes JSON files with a byte order mark.
        return json.loads(response.read().decode("utf-8-sig"))


def fetch_latest_update() -> UpdateInfo | None:
    return select_update(_get_json(LATEST_RELEASE_API, 12), install_layout())


def updates_dir() -> Path:
    return config_store.APP_DIR / "updates"


def _download(url: str, destination: Path) -> str:
    """Download to ``destination`` and return its SHA-256."""
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": APP_SLUG})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            expected_length = response.headers.get("Content-Length")
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if expected_length is not None and downloaded != int(expected_length):
            raise RuntimeError(f"Onvolledige update-download: {downloaded} van {expected_length} bytes ontvangen.")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return digest.hexdigest()


def _expected_hash(update: UpdateInfo) -> str:
    if not update.manifest_url:
        return ""
    manifest = _get_json(update.manifest_url, 20)
    if str(manifest.get("version", "")).lstrip("v") not in {"", update.version}:
        raise RuntimeError("Het updatemanifest hoort bij een andere versie.")
    return str(manifest.get("zip_sha256" if update.kind == "folder" else "exe_sha256", "")).lower()


def _is_windows_executable(path: Path) -> bool:
    with path.open("rb") as handle:
        return path.stat().st_size >= 1024 and handle.read(2) == b"MZ"


def extract_folder_build(archive: Path, staging: Path) -> Path:
    """Unpack a folder build safely; return the staging folder that holds the exe."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    root = staging.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (staging / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("Het updatebestand bevat een ongeldig pad.")
        bundle.extractall(staging)
    # Accept both a flat zip and one wrapped in a top-level folder.
    candidates = [staging, staging / APP_SLUG]
    for folder in candidates:
        if (folder / EXE_NAME).is_file() and (folder / INTERNAL_DIR).is_dir():
            if not _is_windows_executable(folder / EXE_NAME):
                break
            return folder
    raise RuntimeError("Het updatebestand bevat geen geldige Windows-app.")


def download_update(update: UpdateInfo) -> Path:
    """Download and verify; return the staged exe (legacy) or staged folder."""
    folder = updates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    expected = _expected_hash(update)
    if update.kind == "folder":
        archive = folder / f"{APP_SLUG}-{update.tag}.zip"
        actual = _download(update.download_url, archive)
        try:
            if expected and actual != expected:
                raise RuntimeError("De controlesom van de update klopt niet.")
            # Stage beside the installed app, so swapping is a same-volume rename.
            staging = current_exe_path().parent / f".staged-{update.tag}"
            return extract_folder_build(archive, staging)
        finally:
            archive.unlink(missing_ok=True)

    destination = folder / f"{APP_SLUG}-{update.tag}.exe"
    actual = _download(update.download_url, destination)
    if (expected and actual != expected) or not _is_windows_executable(destination):
        destination.unlink(missing_ok=True)
        raise RuntimeError("Het gedownloade updatebestand is geen geldige Windows-app.")
    return destination


def _ps_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def update_script_lines(source: Path, kind: str, target: Path, pid: int, log_path: Path, confirm: Path) -> list[str]:
    install_dir = target.parent
    return [
        "$ErrorActionPreference = 'Stop'",
        f"$Source = {_ps_literal(source)}",
        f"$Kind = {_ps_literal(kind)}",
        f"$Target = {_ps_literal(target)}",
        f"$InstallDir = {_ps_literal(install_dir)}",
        f"$Log = {_ps_literal(log_path)}",
        f"$Confirm = {_ps_literal(confirm)}",
        f"$PidToWait = {pid}",
        "$Internal = Join-Path $InstallDir '_internal'",
        "$Backup = \"$Target.bak\"",
        "$InternalBackup = \"$Internal.bak\"",
        "function Log($Message) { Add-Content -LiteralPath $Log -Value \"$(Get-Date -Format o) $Message\" }",
        "function Restore {",
        "  if (Test-Path -LiteralPath $Backup) {",
        "    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue",
        "    Move-Item -LiteralPath $Backup -Destination $Target -Force",
        "  }",
        "  if (Test-Path -LiteralPath $InternalBackup) {",
        "    Remove-Item -LiteralPath $Internal -Recurse -Force -ErrorAction SilentlyContinue",
        "    Move-Item -LiteralPath $InternalBackup -Destination $Internal -Force",
        "  } elseif ($Kind -eq 'folder') {",
        "    Remove-Item -LiteralPath $Internal -Recurse -Force -ErrorAction SilentlyContinue",
        "  }",
        "  Log 'Previous version restored'",
        "}",
        "$Swapped = $false",
        "try {",
        "  Log \"Waiting for process $PidToWait to exit\"",
        "  try { Wait-Process -Id $PidToWait -Timeout 30 -ErrorAction SilentlyContinue } catch {}",
        "  Start-Sleep -Milliseconds 700",
        "  Remove-Item -LiteralPath $Confirm -Force -ErrorAction SilentlyContinue",
        "  if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Force }",
        "  if (Test-Path -LiteralPath $InternalBackup) { Remove-Item -LiteralPath $InternalBackup -Recurse -Force }",
        "  if ($Kind -eq 'folder') {",
        "    $NewExe = Join-Path $Source 'GroqInsertDictation.exe'",
        "    $NewInternal = Join-Path $Source '_internal'",
        "    if (-not (Test-Path -LiteralPath $NewExe) -or -not (Test-Path -LiteralPath $NewInternal)) { throw 'Staged folder build is incomplete' }",
        "    Log \"Installing folder build from $Source\"",
        "    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $Backup -Force }",
        "    if (Test-Path -LiteralPath $Internal) { Move-Item -LiteralPath $Internal -Destination $InternalBackup -Force }",
        "    $Swapped = $true",
        "    Move-Item -LiteralPath $NewInternal -Destination $Internal -Force",
        "    Move-Item -LiteralPath $NewExe -Destination $Target -Force",
        "    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {",
        "      $Destination = Join-Path $InstallDir $_.Name",
        "      Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue",
        "      Move-Item -LiteralPath $_.FullName -Destination $Destination -Force",
        "    }",
        "  } else {",
        "    Log \"Installing single-file build from $Source\"",
        "    $Staged = \"$Target.new\"",
        "    Copy-Item -LiteralPath $Source -Destination $Staged -Force",
        "    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $Backup -Force }",
        "    $Swapped = $true",
        "    Move-Item -LiteralPath $Staged -Destination $Target -Force",
        "  }",
        "  Log \"Starting $Target\"",
        "  $Env:PYINSTALLER_RESET_ENVIRONMENT = '1'",
        "  $Process = Start-Process -FilePath $Target -WorkingDirectory $InstallDir -PassThru",
        "  $Deadline = (Get-Date).AddSeconds(45)",
        "  while ((Get-Date) -lt $Deadline -and -not (Test-Path -LiteralPath $Confirm)) {",
        "    if ($Process.HasExited) { throw \"New version exited with code $($Process.ExitCode) before confirming startup\" }",
        "    Start-Sleep -Milliseconds 500",
        "  }",
        "  if (Test-Path -LiteralPath $Confirm) { Log 'Update complete' } else { Log 'Update started; confirmation still pending' }",
        "} catch {",
        "  Log \"Update failed: $($_.Exception.Message)\"",
        "  if ($Swapped) {",
        "    Restore",
        "    $Env:PYINSTALLER_RESET_ENVIRONMENT = '1'",
        "    Start-Process -FilePath $Target -WorkingDirectory $InstallDir",
        "  }",
        "} finally {",
        "  if ($Kind -eq 'folder') { Remove-Item -LiteralPath $Source -Recurse -Force -ErrorAction SilentlyContinue }",
        "  else { Remove-Item -LiteralPath $Source -Force -ErrorAction SilentlyContinue }",
        "}",
        "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue",
        "",
    ]


def launch_update_script(staged: Path, kind: str = "exe") -> None:
    app_dir = config_store.APP_DIR
    target = current_exe_path()
    ps_script = app_dir / "apply-update.ps1"
    cmd_script = app_dir / "apply-update.cmd"
    ps_script.write_text(
        "\n".join(update_script_lines(
            staged, kind, target, os.getpid(), app_dir / "update.log", app_dir / CONFIRM_FILE,
        )),
        # Windows PowerShell 5.1 reads a BOM-less file as ANSI, which corrupts
        # non-ASCII paths; the BOM makes it decode the script as UTF-8.
        encoding="utf-8-sig",
    )
    cmd_script.write_bytes(
        cmd_script_bytes(
            [
                "@echo off",
                f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{path_with_env_var(ps_script)}"',
                'del "%~f0" >nul 2>nul',
                "",
            ]
        )
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd.exe", "/c", str(cmd_script)], creationflags=creation_flags)


def confirm_startup_and_cleanup() -> None:
    """Tell a waiting updater that this version started, then drop the backups."""
    if not getattr(sys, "frozen", False):
        return
    try:
        (config_store.APP_DIR / CONFIRM_FILE).write_text(APP_VERSION + "\n", encoding="utf-8")
    except OSError as exc:
        logging.warning("Could not confirm update start: %s", exc)
    target = current_exe_path()
    for backup in (Path(f"{target}.bak"), target.parent / f"{INTERNAL_DIR}.bak"):
        try:
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink(missing_ok=True)
        except OSError as exc:
            logging.warning("Could not remove confirmed update backup %s: %s", backup, exc)
    # Leftovers of an update that never ran; recent ones may belong to a download in progress.
    for staged in target.parent.glob(".staged-*"):
        try:
            if time.time() - staged.stat().st_mtime > 3600:
                shutil.rmtree(staged, ignore_errors=True)
        except OSError:
            pass
