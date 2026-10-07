"""Shared test helpers: one QApplication, and stubs for libraries a dev box may lack."""
import os
import sys
import types


def install_missing_stubs() -> None:
    """Stub Windows-only or display-bound libraries so pure logic can be tested anywhere."""

    def stub(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        module.__getattr__ = lambda _attr: types.SimpleNamespace()  # type: ignore[attr-defined]
        sys.modules[name] = module
        return module

    for name in ("sounddevice", "pyautogui", "pyperclip"):
        try:
            __import__(name)
        except Exception:
            stub(name)

    try:
        import groq  # noqa: F401
    except Exception:
        stub("groq").Groq = object  # type: ignore[attr-defined]

    try:
        import keyring  # noqa: F401
    except Exception:
        keyring_stub = stub("keyring")
        errors = types.ModuleType("keyring.errors")
        errors.KeyringError = Exception  # type: ignore[attr-defined]
        errors.PasswordDeleteError = Exception  # type: ignore[attr-defined]
        keyring_stub.errors = errors  # type: ignore[attr-defined]
        sys.modules["keyring.errors"] = errors

    try:
        import truststore  # noqa: F401
    except Exception:
        stub("truststore").inject_into_ssl = lambda: None  # type: ignore[attr-defined]


def qt_app():
    """Return the process-wide QApplication with the app theme applied."""
    if os.name != "nt":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    install_missing_stubs()
    from PySide6.QtWidgets import QApplication

    import ui_theme

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        ui_theme.apply_theme(app, ui_theme.LIGHT)
    return app
