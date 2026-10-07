# Third-party notices

Groq Insert Dictation bundles Python and the packages listed in
`requirements.txt`. The license files that ship with each package are copied
next to this file in the Windows build (`licenses\<package>-<version>`).

## Qt for Python (PySide6) and Qt

The user interface uses Qt for Python (PySide6 Essentials 6.11.2) and the Qt 6
libraries it contains. PyPI lists PySide6 as "available under both Open Source
(LGPLv3 or GPLv2 or GPLv3) and commercial license"
(https://pypi.org/project/PySide6/, checked 2026-10-06). This app uses it
under the GNU Lesser General Public License version 3; the full texts of the
LGPLv3 and the GPLv3 it refers to are included as `LGPL-3.0.txt` and
`GPL-3.0.txt`.

The app is distributed as a folder build: the Qt and PySide6 libraries are
separate, unmodified DLLs and Python extension modules in `_internal\PySide6`
and `_internal\shiboken6`, so they can be replaced with compatible versions.
Source code for Qt for Python is available from https://code.qt.io/ and
https://download.qt.io/official_releases/QtForPython/.

## Other components

Python (PSF License), NumPy, Pillow, sounddevice/PortAudio, groq, keyring,
pyautogui, pyperclip and truststore are bundled under their own licenses;
see the `licenses` folder.
