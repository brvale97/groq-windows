"""Check the native icon handles Windows uses for the title bar and taskbar."""
import os
import unittest


@unittest.skipUnless(os.name == "nt", "Windows icon test")
class WindowIconTests(unittest.TestCase):
    def test_window_has_native_small_and_large_icons_at_the_monitor_dpi(self):
        import ctypes
        from ctypes import wintypes

        from qt_support import qt_app

        qt_app()
        from PySide6.QtWidgets import QApplication, QWidget

        import ui_theme

        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP),
            ]

        class BITMAP(ctypes.Structure):
            _fields_ = [
                ("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
        user32.GetSystemMetricsForDpi.restype = ctypes.c_int
        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
        user32.GetIconInfo.restype = wintypes.BOOL
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]

        window = QWidget()
        window.setWindowIcon(ui_theme.app_icon())
        window.show()
        QApplication.processEvents()
        try:
            hwnd = int(window.winId())
            dpi = user32.GetDpiForWindow(hwnd)
            # ICON_SMALL uses SM_CXSMICON (49), ICON_BIG SM_CXICON (11): 16/32 px at 100 %, 20/40 at 125 %.
            for kind, metric in ((0, 49), (1, 11)):
                size = user32.GetSystemMetricsForDpi(metric, dpi)
                handle = user32.SendMessageW(hwnd, 0x007F, kind, 0)  # WM_GETICON
                self.assertTrue(handle, f"Missing {size}px window icon")
                info = ICONINFO()
                self.assertTrue(user32.GetIconInfo(handle, ctypes.byref(info)))
                try:
                    bitmap = BITMAP()
                    self.assertTrue(gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bitmap), ctypes.byref(bitmap)))
                    self.assertEqual((bitmap.bmWidth, bitmap.bmHeight), (size, size))
                finally:
                    gdi32.DeleteObject(info.hbmColor)
                    gdi32.DeleteObject(info.hbmMask)
        finally:
            window.close()

    def test_every_native_icon_size_is_rendered_directly(self):
        from qt_support import qt_app

        qt_app()
        from PySide6.QtCore import QSize

        import ui_theme
        from branding import ICO_SIZES

        icon = ui_theme.app_icon()
        available = {(size.width(), size.height()) for size in icon.availableSizes()}
        for size in ICO_SIZES:
            self.assertIn((size, size), available)
            self.assertEqual(icon.pixmap(QSize(size, size), 1.0).width(), size)


if __name__ == "__main__":
    unittest.main()
