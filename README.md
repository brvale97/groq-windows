# Groq Windows Dictation

HTTPS requests use the Windows trusted certificate store, including trusted
enterprise inspection certificates. TLS certificate verification remains enabled.

A small dictation app for Windows:

- The configured shortcut starts recording.
- Pressing the shortcut again stops recording.
- Audio is sent to Groq Speech-to-Text using `whisper-large-v3-turbo`.
- The transcript is always copied to your clipboard.
- The text is then pasted automatically into the active window.
- Settings are managed from the system tray, including the Groq API key, microphone, customizable shortcut, and automatic startup.
- You can add names and terms such as `Groq` and `Clinon` with the correct spelling to your personal dictionary.
- Explicit word replacements can correct known variants such as `Grok` or `Grog` to `Groq` after transcription.
- The app checks GitHub Releases for updates and can update itself without deleting your API key or settings.
- A small status icon appears centered at the bottom of the screen while the app is in use: recording, transcribing, and then ready for another 3 seconds.
- While recording, a subtle waveform scrolls from right to left like a dictaphone: bar heights follow your microphone volume, rise quickly when you speak and fade out gently when you stop. During silence it settles into small dots; the timer keeps counting.

## Setup

For development:

```powershell
.\run.ps1
```

For normal use, build the Windows app:

```powershell
.\build-app.ps1
```

The resulting app is a folder build in `dist\GroqInsertDictation\`
(`GroqInsertDictation.exe` plus `_internal`), with the update package
`dist\GroqInsertDictation-win64.zip` and the manifest
`dist\GroqInsertDictation.build.json`. `.\build-app.ps1 -Bridge` also builds the
single-file `dist\GroqInsertDictation.exe` for release (see Updates).

To install it in your user profile and enable automatic startup (quit the
running app first):

```powershell
.\install-app.ps1
```

This copies the folder build to `%LOCALAPPDATA%\Programs\GroqInsertDictation\`
and keeps the previous version in a `backup-<date>` folder next to it.

The Settings window opens the first time you run the app. Enter your Groq API key, optionally select a microphone, configure the shortcut if desired, and leave automatic startup enabled.

To run the tests:

```powershell
.\bootstrap.ps1 -Profile runtime
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Settings

Settings are stored in `%APPDATA%\GroqInsertDictation\settings.json`. The API key is stored in Windows Credential Manager whenever possible.

Open the personal dictionary from Settings to add names, jargon, and terms that are often transcribed with the wrong spelling. The app sends these terms as spelling context in the same Groq transcription request. Because Whisper treats that context as a hint rather than a guarantee, add known mistakes such as `Grok → Groq` or `Grog → Groq` in the **Replacements** section directly below the dictionary. Replacements are applied locally after transcription and before the text is copied or pasted. The existing free-form **Prompt** field continues to work alongside the dictionary.

The final period is preserved by default. Enable **Punt aan het einde verwijderen** in Settings if you prefer transcripts without one final period.

- `GROQ_MODEL=whisper-large-v3-turbo` for maximum speed.
- `GROQ_MODEL=whisper-large-v3` for higher accuracy.
- `GROQ_LANGUAGE=nl` for Dutch; leave it empty to use automatic language detection.
- `DICTATION_INPUT_DEVICE=11` to select a specific microphone from the startup list.
- `PASTE_AFTER_TRANSCRIPTION=false` to copy the transcript to the clipboard without pasting it automatically.

`.env` remains available as a fallback and migration path, but is no longer required for normal use.

Runtime and build dependencies are kept separately in `requirements.txt` and `requirements-build.txt`. The PowerShell scripts reinstall them only when the Python version or dependency files have changed.

## Updates

The app checks for a newer GitHub Release when it starts. If an update is available, a window with an update button appears. Your settings, history and API key remain in `%APPDATA%` and Windows Credential Manager.

From 0.2.0 the updater installs the folder build: it downloads
`GroqInsertDictation-win64.zip`, verifies its SHA-256 against
`GroqInsertDictation.build.json`, unpacks it next to the installed app, swaps
`GroqInsertDictation.exe` and `_internal` after the app has exited, and restores
the previous version if the new one exits before confirming its start. A folder
install never accepts an exe-only release. Releases also carry a single-file
`GroqInsertDictation.exe`, because updaters up to 0.1.27 replace exactly one
file; that bridge build runs on its own and moves to the folder layout on its
next update.

## Note

The app pastes text using `Ctrl+V` instead of typing it character by character. This is faster and works better with Dutch characters, punctuation, and longer text. Because the transcript is copied to the clipboard first, you can always paste it manually if automatic pasting fails.

## Interface and reliability (0.1.19)

The settings window was rebuilt around five pages in a fixed sidebar: **Dicteren**
(shortcut, microphone, behaviour switches), **Herkenning** (language, multi-line
prompt), **Woordenboek** (words and replacements side by side, no separate dialog),
**Verbinding** (API key with show/hide and a "Verbinding testen" button, model) and
**Over** (version, update check, log file, restart). Unsaved edits are flagged in the
footer and confirmed before closing. Launch `GroqInsertDictation.exe --settings` to
open the settings after startup. The UI code lives in `settings_ui.py`.

The global shortcut no longer uses a low-level keyboard hook (`keyboard` package).
Windows silently removes such a hook when its callback is slow, which is why the
shortcut could stop working until the app was restarted. `hotkeys.py` now registers
the shortcut with the Win32 `RegisterHotKey` API on its own message loop: the OS
consumes the key combination, delivers it as a message, and recording starts and
stops on a worker thread. Saved shortcut strings such as `alt+z`, `insert` or
`ctrl+shift+f9` keep working. If another program already owns the combination the
app starts anyway and asks you to pick a different shortcut.

Clicking the floating status bubble now stops a running recording; when idle it
opens the settings.

**Geschiedenis** keeps the last twenty transcriptions (newest first) with a copy button
per entry and a "Geschiedenis wissen" button. It is stored locally in
`%APPDATA%\GroqInsertDictation\history.json` and is also reachable from the tray menu.

The app now ships its own icon (`branding.py`): embedded in the executable, used by the
tray and shown on every window and in the taskbar instead of the default Tk feather.

<!-- Source: Bram's requests, 2026-10-06. Scope: Groq Windows Dictation. Sharp taskbar icon and visible selected microphone. -->
Version **0.1.26** supplies native icon sizes for the title bar and taskbar, with
each ICO frame rendered separately. The microphone selector shows the actual
device name even when following **Windows-standaard**. The complete selection is
also shown below the selector so long names remain readable. **Vernieuwen** keeps
your selection; a missing selected device is shown as unavailable.

<!-- Source: Bram's request, 2026-10-06. Scope: Groq Windows Dictation. Replace sound cues preview with a microphone test and remove unplugged microphones from the device list. -->
In **0.1.27**, **Microfoon testen** records five seconds from the selected input,
including an unsaved selection, with a live input meter and **Terugluisteren**.
The test stays local, does not contact Groq or enter History, and is discarded
when you close Settings or start another test. Stop the test at any time with
**Stop test**. Silence and disconnected/unavailable devices are reported.
The former **Geluiden testen** preview has been removed.

Opening Settings or clicking **Vernieuwen** now reinitializes PortAudio while
idle, so unplugged USB microphones and changed Windows defaults are detected.
The device list uses Windows audio endpoints without duplicate backend entries,
and explicit choices use names instead of volatile PortAudio indices. Legacy
indices are migrated when a unique matching Windows endpoint is available.

The Windows widget tests require an interactive desktop. They verify navigation,
visible control bounds, dictionary edits, saving, shortcut capture and cancelling
without saving. (Since 0.2.0 these are Qt tests that also run headless with
`QT_QPA_PLATFORM=offscreen`; see the 0.2.0 section.)

## Voice recording recovery (0.1.23)

<!-- Source: Bram's request, 2026-10-03. Scope: Groq Windows Dictation. Keep twenty recordings and allow transcription retries in the app. -->

Geschiedenis also keeps the last **twenty voice recordings**, independently of
the twenty transcripts. The **Opnames** tab stores the original audio locally in
`%APPDATA%\GroqInsertDictation\recordings` before contacting Groq, including short
recordings and failed or empty transcription responses. Recordings and their
status survive app restarts. The oldest audio and its metadata are removed when
the twenty-first recording is saved.

Use **Opnieuw transcriberen** on a recording to try again with your currently
saved API key, model, language, prompt, dictionary and replacements. The result
is copied to the clipboard and added to **Teksten**; a retry keeps the original
recording and does not automatically paste into the Settings window. You can
retry while idle, including after correcting your connection or settings.
**Geschiedenis wissen** removes both the saved audio and the transcripts.
Recordings deleted by older app versions cannot be recovered.

## Listening back to recordings (0.1.24, player 0.1.25)

<!-- Source: Bram's request, 2026-10-04. Scope: Groq Windows Dictation. Play saved recordings from the history. -->

Each recording in the **Opnames** tab has a small player: a play/pause button,
a progress bar you can click or drag to jump to another moment, and the elapsed
and total time. Audio plays through the default Windows output device. Pausing
keeps your position; playing another recording, starting a dictation, closing the
window or clearing the history stops playback. (0.1.24 copied the rest of the WAV
to a temporary file to seek with `winsound`; 0.2.0 replaces that, see below.)

## Modern Qt interface (0.2.0)

<!-- Source: Bram's explicit instruction "Oke laat opus maar bouwen", 2026-10-06, confirming the Python + PySide6/Qt Widgets modernization. Scope: Groq Windows Dictation. -->

Bram decided on 2026-10-06 to keep Python and replace Tkinter/ttk, pystray and
the winsound-based player with **PySide6 (Qt for Python) using Qt Widgets**, not
Qt Quick/QML and not Electron/Tauri. The settings are mostly forms and lists,
which map directly to Qt Widgets in plain Python.

**Architecture.** UI-independent code moved out of `app.py`:

- `config_store.py`: version, data paths, settings file and Credential Manager.
- `engine.py`: recording (sounddevice/PortAudio, WASAPI with per-thread COM
  initialization and `WasapiSettings(auto_convert=True)` for explicit inputs),
  device list, cues, Groq transcription and pasting. The code is the 0.1.27 code,
  moved unchanged apart from paths.
- `audio_player.py`: seekable playback for History and the microphone test.
- `windows_services.py`: single instance, autostart and launch commands.
- `updater.py`: release selection, verified download and the swap script.
- `ui_theme.py`: light/dark palettes with the existing green accent, the style
  sheet and vector icons painted at the exact size and DPI Windows requests.
- `settings_ui.py`: the settings window (same six pages and the same
  `SettingsController` boundary), `app.py`: tray, status bubble, splash, update
  dialog and the Qt adapter that turns engine callbacks from worker threads into
  signals handled on the GUI thread.

**Behaviour.**

- The interface follows the Windows light/dark setting, uses Segoe UI at a
  readable size, scales per monitor (100/125/150 %) and draws icons sharply at
  every size. The tray icon opens Settings on a click; the menu is unchanged.
- The status bubble is a translucent, anti-aliased pill with the scrolling
  waveform, timer and stop button, a spinner while transcribing, and notices.
  It is a tool window that never accepts focus (`WindowDoesNotAcceptFocus`,
  `WS_EX_NOACTIVATE`, `MA_NOACTIVATE`), so showing it or clicking it to stop
  keeps the window you dictate into active and Ctrl+V lands there.
- History playback and **Terugluisteren** use PortAudio output with the PCM in
  memory: the position shown is the position actually played, seeking needs no
  temporary files, and a dictation, refresh or clearing history releases the
  output device first. Cues still use `winsound`.
- Settings, API key, history and recordings from 0.1.x are read unchanged; an
  unchanged settings file stays byte-identical.

**Distribution.** The app ships as a folder build (onedir). On AMD (Ryzen,
NVMe, Windows 11, 125 %) the folder build reported "ready" 2.0-2.1 s after launch
versus 3.2-3.4 s for the single-file build (three runs each, both including the
deliberate 1.25 s splash minimum); the single file unpacks ~100 MB into a
temporary folder on every autostart. The folder build also keeps the LGPL-licensed Qt libraries as
replaceable DLLs. The update package is a 46 MB zip (107 MB unpacked); Qt parts
the app does not use (software OpenGL, QML/Quick, PDF, image-format and spare
platform plugins, translations) and Tk are left out. See `THIRD_PARTY_NOTICES.md`
for Qt for Python's license (PyPI: LGPLv3/GPLv2/GPLv3 or commercial; used under
the LGPLv3) and the bundled license texts.

**Testing.** `python -m unittest discover -s tests` runs everywhere; on Linux
Qt uses the offscreen platform. `tests/test_desktop_windows.py`
(`GROQ_DESKTOP_E2E=1`) and `packaging/desktop-e2e.ps1` (for a built app) drive a
real, unlocked Windows desktop: shortcut via `SendInput`, real microphone, a
click on the bubble and Ctrl+V into a separate window, with offline QA
transcripts instead of Groq. Both use the isolated profile
`GROQ_DICTATION_PROFILE=qa` (own settings folder, Credential Manager entry and
mutex, never autostarts), so they can run next to an installed copy.
`packaging/desktop-e2e-apps.ps1` repeats the paste check in Word, Edge and
Chrome (new document closed without saving, throwaway browser profiles). On
2026-10-07 all of these passed on AMD main; Teams was not available to test.
