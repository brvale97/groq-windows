# Groq Windows Dictation

HTTPS requests use the Windows trusted certificate store, including trusted
enterprise inspection certificates. TLS certificate verification remains enabled.

A small dictation app for Windows:

- The configured shortcut starts recording.
- Pressing the shortcut again stops recording.
- Audio is sent to Groq Speech-to-Text using `whisper-large-v3-turbo`.
- A local speech detector skips recordings with no detected speech before sending
  audio to Groq. Nothing is copied or pasted; the original audio remains in History.
- The transcript is always copied to your clipboard.
- The text is then pasted automatically into the active window.
- Settings are managed from the system tray, including the Groq API key, microphone, customizable shortcut, and automatic startup.
- You can add names and terms such as `Groq` and `Clinon` with the correct spelling to your personal dictionary.
- Explicit word replacements can correct known variants such as `Grok` or `Grog` to `Groq` after transcription.
- AI cleanup on Groq (GPT-OSS 120B) removes filler words and repetitions and fixes punctuation, with your own system prompt if you like (see AI cleanup).
- The app checks GitHub Releases for updates and can update itself without deleting your API key or settings.
- A small status icon appears centered at the bottom of the screen while the app is in use: recording, transcribing, and then ready for another 3 seconds.
- While recording, a subtle waveform scrolls from right to left like a dictaphone: bar heights follow your microphone volume, rise quickly when you speak and fade out gently when you stop. During silence it settles into small dots; the timer keeps counting.

## Setup

<!-- Source: Bram's screenshot and correction, 2026-10-10. Scope: recording status bubble. -->
In **0.2.2**, the recording pill fits the timer text with a 10-pixel gap before
the stop button. It expands for recordings over 99 minutes without clipping
the timer; ordinary recordings keep a stable width.

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

<!-- Source: Bram's empty-audio report, 2026-10-10. Scope: dictation and recording retries. -->
In **0.2.3**, local Silero voice activity detection checks for speech, including
on retries. Quiet speech and short words can pass without requiring a large
speech-to-silence ratio. The bundled model runs on the CPU, with no detector
downloads or cloud requests. It distinguishes speech from microphone startup
transients, noise and the start cue; the regression checks include the two
actual silent laptop recordings reported on 2026-10-10 (kept outside Git).
Groq's `verbose_json` silence/confidence metadata provides a second
check. These checks reduce empty-audio hallucinations; speech detection is not
perfect. No transcript phrases are blacklisted, so spoken "Thank you" remains
valid. Rejected audio stays in History and the app shows **Geen spraak gedetecteerd**.

<!-- Source: Bram's request in T3, 2026-10-23, and the tests below. Scope: dictation and recording retries. -->
In **0.2.6**, a silence of 2 seconds or longer after the last word is not sent
to Groq; 0.4 seconds after the last word is kept. Whisper tends to invent a
closing line for such a silence: in a test with 60 recordings built from Bram's
own dictations, a single request with 8 seconds of trailing silence ended with
a made-up "Dank u wel." twice, the trimmed upload never. Pauses inside the
dictation and silence before it are not cut. Splitting the upload at every
pause of 2 seconds or longer was tested and rejected: Whisper broke sentences
at the cuts and misdetected the language of short pieces ("Hello?" for
"Hallo?"), raising word errors from 1.3% to 2.2%. The saved recording is never
altered, and retries from History apply the same rule.

Settings are stored in `%APPDATA%\GroqInsertDictation\settings.json`. The API key is stored in Windows Credential Manager whenever possible.

Open the personal dictionary from Settings to add names, jargon, and terms that are often transcribed with the wrong spelling. The app sends these terms as spelling context in the same Groq transcription request. Because Whisper treats that context as a hint rather than a guarantee, add known mistakes such as `Grok → Groq` or `Grog → Groq` in the **Replacements** section directly below the dictionary. Replacements are applied locally after transcription and before the text is copied or pasted. The existing free-form **Prompt** field continues to work alongside the dictionary.

Longer dictations are split into short, chat-style paragraphs: a few sentences per block, with a new block where a new thought starts ("Verder", "Daarnaast", "Trouwens", ...) and for a closing such as "Groetjes". Only the spacing between sentences changes; short messages stay one block. Turn off **Automatisch alinea's maken** in Settings to paste everything as one block.

The final period is preserved by default. Enable **Punt aan het einde verwijderen** in Settings if you prefer transcripts without one final period.

- `GROQ_MODEL=whisper-large-v3-turbo` for maximum speed.
- `GROQ_MODEL=whisper-large-v3` for higher accuracy.
- `GROQ_LANGUAGE=nl` for Dutch; leave it empty to use automatic language detection.
- `DICTATION_INPUT_DEVICE=11` to select a specific microphone from the startup list.
- `PASTE_AFTER_TRANSCRIPTION=false` to copy the transcript to the clipboard without pasting it automatically.

`.env` remains available as a fallback and migration path, but is no longer required for normal use.

Runtime and build dependencies are kept separately in `requirements.txt` and `requirements-build.txt`. The PowerShell scripts reinstall them only when the Python version or dependency files have changed.

## AI cleanup (0.2.7)

<!-- Source: Bram's request in T3, 2026-10-24 ("AI-opschoning ... het liefste dat model wat hier staat, als dat kan, met Groq ... dat je dus je eigen system prompt erin kan stoppen"), and the tests below. Scope: text after transcription. -->
After Whisper, the transcript goes to a chat model on Groq that removes
filler words ("eh", "uh"), stutters and repetitions, keeps only the corrected
version after "nee wacht" or "ik bedoel", and fixes punctuation, in the spirit
of Wispr Flow. It is on by default, also after updating, with
`openai/gpt-oss-120b` and the same API key. Settings has an **AI-opschoning**
page with the switch, the model (GPT-OSS 120B, GPT-OSS 20B or any Groq model
ID) and the system prompt. The built-in prompt is shown there; edit it to use
your own, or click **Standaardprompt herstellen**. Dictionary words are added
to the prompt as spellings.

The order is: Whisper, word replacements, AI cleanup, paragraphs, final
period. The dictation is sent between `<transcript>` tags, so a dictated
"ignore previous instructions" stays text. If the request fails, is rate
limited, takes longer than about 10 seconds, or the reply is much longer than
the dictation (an answer instead of a cleanup), the literal transcript is
pasted and the bubble shows **Niet opgeschoond**. Recordings and retries from
History use the same settings; the saved audio is never changed.

Tested on 2026-10-24 with 47 dictations (17 of Bram's real transcripts plus 30
hard cases in Dutch, English and mixed): GPT-OSS 120B kept the language of
every sentence, left all dictated questions and instructions as text (also
over three runs each), removed the repetitions and applied 5 of 6 spoken
corrections every time. It missed "The meeting is at 3 PM. Nee wacht, het is
om vier uur." and also drops a "Maar wacht," that corrects nothing. GPT-OSS
20B was about as fast but missed a correction, dropped an English "really"
and changed "transcribt" to "transcribeert". A cleanup took 0.1-0.6 s
(median 0.3 s). Bench scripts and results: `dist-release/v0.2.7/bench/`
(not in Git).

## Microphone priority (0.2.7.4 test)

<!-- Source: Bram's request in T3, 2026-10-24 (priority 1, 2, 3; switch automatically and show it; show clearly when there is no microphone), his approval of the mockup ("Ja, bouw maar in, dat is perfect"), and the tests below. Scope: microphone choice and the status bubble. -->
A friend had chosen a Bluetooth headset that was not connected; Alt+Z then
seemed to do nothing, because the app only played the error sound and put a
message in the tray tooltip. **Dicteren › Microfoons** is now a list in order of
preference: each recording uses the first microphone that is connected. Every
row says **In gebruik**, **Beschikbaar** or **Niet verbonden**, with buttons to
move it up or down or remove it; the last one cannot be removed.
**Microfoon toevoegen** offers the microphones that are connected now (Windows
only lists a Bluetooth headset while it is connected) and **Windows-standaard**,
which follows the microphone set in Windows.

<!-- Source: Bram in T3, 2026-10-24: "die vangnet optie moet nog weg. Dan moeten mensen gewoon instellen wat ze willen." -->
There is no hidden safety net: Windows-standaard is used only if it is in the
list, at its place. New installs and a previous "Windows-standaard" choice
start with just that entry; a previously chosen microphone becomes the only
entry.

**Microfoon testen** tests the microphone the next recording would use. It
records until you click **Stop test** (at most 10 seconds) and keeps what you
said for **Terugluisteren**; before 0.2.7.2 it always took 5 seconds.

The app reads the Windows audio devices again at every shortcut press
(re-initializing PortAudio took about 40 ms on AMD), so a headset that connected
or disconnected since the previous recording counts. A listed microphone that
cannot be opened is skipped too.

<!-- Source: Bram in T3, 2026-10-24: "ik wil dat die fallback ook werkt als TIJDENS het recorden de verbinding wegvalt" and "als de eerste mic weer terugkomt moet die daar ook weer fallback naar doen". -->
During a recording the app keeps watching. If the microphone disappears (its
stream stops, no audio arrives for 1 second, or Windows no longer lists it), the
same recording continues on the next microphone of the list; what was said
during the switch (about a second) is missing. If a higher priority comes back
(Windows lists it again for about two seconds), the recording switches back to
it, missing a split second. A microphone that Windows listed but that could not
be used at the start is not retried; one that does not open when it comes back
is retried twice, 10 seconds apart. The live list comes from Windows Core Audio
and takes about a millisecond. If no microphone is left, the recording stops
and what was captured is transcribed.

If the recording does not use priority 1, a card above the bubble says
**Overgeschakeld naar …** and which microphones were not connected (or
**Verbinding met … verbroken** after a switch during the recording) for 3
seconds, and an amber number stays in the bubble until the recording ends or
priority 1 is back; switching back shows a green card **Terug naar …**. Without
any working microphone in the list nothing is recorded: the error sound plays
and a red card **Geen microfoon gevonden** stays for 6 seconds; clicking it
opens Settings at the microphones.

`settings.json` keeps `input_device` as the first entry ("" for
Windows-standaard) next to the `input_devices` list, so an older version (after
a rollback) still uses the preferred microphone. Such a version saves the file
without the list, so after a rollback the list shrinks to that one microphone.

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

<!-- Source: Bram's request in T3, 2026-10-23 ("probeer t maar te fixen hoe jij denkt dat het t beste is"). Scope: automatic pasting. -->
Since **0.2.6** the app no longer presses `Ctrl+V` the instant the transcript
reaches the clipboard. Some apps sometimes pasted nothing: clipboard tools
(Windows clipboard history, cloud clipboard, clipboard managers) briefly lock
the clipboard to read every new text, longer texts for longer, and a key of the
shortcut that is still held turns `Ctrl+V` into another shortcut. The app now
waits until the shortcut keys are released (at most 1.5 s; after that it
releases them itself) and until the clipboard is free (at most 0.6 s), then
sends `Ctrl+V` with scan codes and 25 ms between the key events, as Handy,
OpenWhispr and VoiceInk do. This adds roughly 0.2 s. `app.log` records which
program received the paste, so an app that still misses it can be identified.
The text is still pasted in one go; none of these projects pastes in chunks by
default.

<!-- Source: Bram's report and observation in T3, 2026-10-24 ("als je tijdens recording in een tekstvak klikt dan plakt die daarna niet goed"). Scope: shortcuts with Alt or Windows. -->
Since **0.2.7** the shortcut no longer moves the focus. Windows
swallows the Z of `Alt+Z` but passes the Alt press and release on to the active
window, so releasing Alt after Z looked like a lone Alt. Electron apps with a
menu bar, such as Signal, then move the focus to that (often hidden) menu bar,
and the next Alt press gives it back. Starting and stopping usually cancelled
out, but after clicking into a text field during a recording, the stop press
took the focus away from that field and `Ctrl+V` pasted nothing. The app now
taps the unassigned key `0xE8` the moment the shortcut fires while Alt or
Windows is still held, as AutoHotkey does, so the release is never a lone Alt
or Windows key.

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
