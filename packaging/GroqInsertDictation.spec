# PyInstaller spec for Groq Insert Dictation (PySide6 / Qt Widgets).
#
#   GROQ_BUILD_MODE=onedir   folder build: GroqInsertDictation.exe + _internal (default, installed)
#   GROQ_BUILD_MODE=onefile  single exe: bridge for updaters up to 0.1.27 that replace one file
#
# Qt parts the app never uses are left out: the software OpenGL renderer, the
# D3D shader compiler, QML/Quick, PDF and Qt's translations.
import os
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
MODE = os.environ.get("GROQ_BUILD_MODE", "onedir")
VERSION = re.search(r'^APP_VERSION = "([^"]+)"', (ROOT / "config_store.py").read_text(encoding="utf-8"), re.M).group(1)
numbers = tuple(int(part) for part in (VERSION.split(".") + ["0"] * 4)[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "brvale97"),
            StringStruct("FileDescription", "Groq Insert Dictation"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "GroqInsertDictation"),
            StringStruct("OriginalFilename", "GroqInsertDictation.exe"),
            StringStruct("ProductName", "Groq Insert Dictation"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

EXCLUDED_BINARIES = re.compile(
    r"(opengl32sw|d3dcompiler_\d+|qsvg|qsvgicon|qpdf|Qt6(Pdf|Qml\w*|Quick\w*|VirtualKeyboard|OpenGL|Network|Svg))\.dll$"
    # Only the native "windows" platform is used, and icons are drawn in code, not loaded from image files.
    r"|[\\/]plugins[\\/](imageformats|generic)[\\/]|[\\/](qdirect2d|qminimal|qoffscreen)\.dll$",
    re.I,
)
EXCLUDED_DATA = re.compile(r"[\\/](translations|qml)[\\/]", re.I)

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    excludes=[
        "tkinter", "_tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "unittest.mock",
        "PIL._avif", "PIL.AvifImagePlugin",
    ],
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if not EXCLUDED_BINARIES.search(entry[0])]
a.datas = [entry for entry in a.datas if not EXCLUDED_DATA.search(entry[0])]
pyz = PYZ(a.pure)
icon = str(ROOT / "build" / "GroqInsertDictation.ico")

if MODE == "onefile":
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="GroqInsertDictation", icon=icon, version=version_info,
        console=False, upx=False, runtime_tmpdir=None,
    )
else:
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name="GroqInsertDictation", icon=icon, version=version_info,
        console=False, upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="GroqInsertDictation", upx=False)
