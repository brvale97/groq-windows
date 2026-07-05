# Groq Insert Dictation

Kleine Windows dictation app:

- `Insert` start opname.
- `Insert` stopt opname.
- Audio gaat naar Groq Speech-to-Text met `whisper-large-v3-turbo`.
- Transcriptie wordt altijd naar je klembord gezet.
- Daarna wordt de tekst automatisch geplakt in het actieve venster.
- Instellingen zitten in een tray-app, inclusief Groq API key, microfoon en autostart.

## Setup

Voor development:

```powershell
.\run.ps1
```

Voor normaal gebruik bouw je een Windows app:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconsole --onefile --name GroqInsertDictation app.py
```

Daarna staat de app in `dist\GroqInsertDictation.exe`.

Installeren naar je gebruikersprofiel en autostart instellen:

```powershell
.\install-app.ps1
```

Dat kopieert de app naar `%LOCALAPPDATA%\Programs\GroqInsertDictation\GroqInsertDictation.exe`.

De eerste keer opent de app Instellingen. Vul je Groq API key in, kies eventueel je microfoon, en laat autostart aan staan.

## Instellingen

Instellingen worden opgeslagen in `%APPDATA%\GroqInsertDictation\settings.json`. De API key wordt in Windows Credential Manager gezet als dat lukt.

- `GROQ_MODEL=whisper-large-v3-turbo` voor maximale snelheid.
- `GROQ_MODEL=whisper-large-v3` voor hogere nauwkeurigheid.
- `GROQ_LANGUAGE=nl` voor Nederlands; laat leeg voor autodetect.
- `DICTATION_INPUT_DEVICE=11` om een specifieke microfoon te kiezen uit de startup-lijst.
- `PASTE_AFTER_TRANSCRIPTION=false` als je alleen het klembord wilt vullen.

`.env` werkt nog als fallback/migratie, maar is niet meer nodig voor normaal gebruik.

## Opmerking

De app gebruikt plakken via `Ctrl+V` in plaats van letter-voor-letter typen. Dat is sneller en werkt beter met Nederlandse tekens, interpunctie en langere tekst. Omdat het transcript eerst naar het klembord gaat, kun je altijd handmatig plakken als automatisch plakken niet lukt.
