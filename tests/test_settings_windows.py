"""Exercise real Tk widgets without microphone, credentials, or tray changes."""
import os
import time
import unittest
from unittest import mock


@unittest.skipUnless(os.name == 'nt', 'Windows UI test')
class SettingsWindowTests(unittest.TestCase):
    def test_navigation_dictionary_and_cancel_preserve_configuration(self):
        import app
        root = app.Tk()
        root.withdraw()
        app.configure_ui(root)
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.root = root
        tray.config = app.Config(api_key='test-only')
        tray.install_hotkey = mock.Mock()
        tray.test_sounds = mock.Mock()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        def button(text):
            return next(w for w in descendants(tray.settings_window)
                        if isinstance(w, app.ttk.Button) and w.cget('text') == text)

        try:
            with mock.patch.object(app, 'input_devices', return_value=[('', 'Windows default input')]), mock.patch.object(app, 'autostart_enabled', return_value=False):
                tray.open_settings()
            window = tray.settings_window
            window.deiconify()
            for _ in range(50):
                root.update()
                if window.winfo_viewable():
                    break
                time.sleep(0.02)
            if not window.winfo_viewable():
                self.skipTest('An interactive Windows desktop is required')
            for page in ('Dicteren', 'Herkenning', 'Verbinding'):
                button(page).invoke()
                root.update()
                self.assertIn('selected', button(page).state())
                self.assertTrue(button('Opslaan').winfo_viewable())
                # Check each visible widget fits inside its immediate parent.
                for w in descendants(window):
                    if w.winfo_viewable() and not isinstance(w, app.Toplevel):
                        self.assertLessEqual(w.winfo_x() + w.winfo_width(), w.master.winfo_width() + 2, str(w))
                        self.assertLessEqual(w.winfo_y() + w.winfo_height(), w.master.winfo_height() + 2, str(w))
            button('Herkenning').invoke()
            button('Woordenboek openen').invoke()
            root.update()
            dialog = next(w for w in window.winfo_children() if isinstance(w, app.Toplevel))
            self.assertTrue(dialog.winfo_viewable())
            close = next(w for w in descendants(dialog) if isinstance(w, app.ttk.Button) and w.cget('text') == 'Sluiten')
            close.invoke()
            with mock.patch.object(app, 'save_config') as save:
                button('Annuleren').invoke()
                save.assert_not_called()
            self.assertEqual(tray.config.api_key, 'test-only')
        finally:
            root.destroy()
