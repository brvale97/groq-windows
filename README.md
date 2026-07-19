# Groq Windows Dictation

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
- A small status icon appears in the bottom-right corner while the app is in use: recording, transcribing, and then ready for another 3 seconds.

## Setup

For development:

```powershell
.\run.ps1
```

For normal use, build the Windows app:

```powershell
.\build-app.ps1
```

The resulting app is written to `dist\GroqInsertDictation.exe`.

To install it in your user profile and enable automatic startup:

```powershell
.\install-app.ps1
```

This copies the app to `%LOCALAPPDATA%\Programs\GroqInsertDictation\GroqInsertDictation.exe`.

The Settings window opens the first time you run the app. Enter your Groq API key, optionally select a microphone, configure the shortcut if desired, and leave automatic startup enabled.

To run the tests:

```powershell
.\bootstrap.ps1 -Profile runtime
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Settings

Settings are stored in `%APPDATA%\GroqInsertDictation\settings.json`. The API key is stored in Windows Credential Manager whenever possible.

Open the personal dictionary from Settings to add names, jargon, and terms that are often transcribed with the wrong spelling. The app sends these terms as spelling context in the same Groq transcription request. Because Whisper treats that context as a hint rather than a guarantee, add known mistakes such as `Grok → Groq` or `Grog → Groq` in the **Replacements** section directly below the dictionary. Replacements are applied locally after transcription and before the text is copied or pasted. The existing free-form **Prompt** field continues to work alongside the dictionary.

- `GROQ_MODEL=whisper-large-v3-turbo` for maximum speed.
- `GROQ_MODEL=whisper-large-v3` for higher accuracy.
- `GROQ_LANGUAGE=nl` for Dutch; leave it empty to use automatic language detection.
- `DICTATION_INPUT_DEVICE=11` to select a specific microphone from the startup list.
- `PASTE_AFTER_TRANSCRIPTION=false` to copy the transcript to the clipboard without pasting it automatically.

`.env` remains available as a fallback and migration path, but is no longer required for normal use.

Runtime and build dependencies are kept separately in `requirements.txt` and `requirements-build.txt`. The PowerShell scripts reinstall them only when the Python version or dependency files have changed.

## Updates

The app checks for a newer GitHub Release when it starts. If an update is available, a window with an update button appears. The updater replaces only the executable; your settings and API key remain in `%APPDATA%` and Windows Credential Manager.

## Note

The app pastes text using `Ctrl+V` instead of typing it character by character. This is faster and works better with Dutch characters, punctuation, and longer text. Because the transcript is copied to the clipboard first, you can always paste it manually if automatic pasting fails.
