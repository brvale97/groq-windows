"""The maintained wheels use the webrtcvad module but a different dist name."""

from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
hiddenimports = ["_webrtcvad"]
