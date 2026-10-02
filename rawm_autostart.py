r"""Autostart-at-login support for the RAWM SA-MH01 tray app.

Backs the tray menu's "Start with Windows" toggle with a per-user entry in
the Windows "Run" registry key
(HKCU\Software\Microsoft\Windows\CurrentVersion\Run) — no admin rights
needed, and it works for both the packaged exe (quoted path to itself) and
a source run ("pythonw" + script path).

The toggle also cleans up the Startup-folder shortcut that older releases of
this repo installed (LEGACY_STARTUP_LNK): enabling consolidates autostart into
the registry entry (removing the .lnk), disabling removes both, and
is_enabled() reports the union so the menu never lies about a leftover
shortcut.
"""

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RAWM SA-MH01"
# legacy: older releases autostarted via a shortcut here, which the toggle
# cleans up when used
LEGACY_STARTUP_LNK = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup\RAWM SA-MH01.lnk",
)


def launch_command():
    """Command line that restarts the app the way it is running now."""
    if getattr(sys, "frozen", False):  # packaged PyInstaller exe
        return f'"{sys.executable}"'
    script = os.path.abspath(sys.argv[0])
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interpreter = pythonw if os.path.isfile(pythonw) else sys.executable
    return f'"{interpreter}" "{script}"'


def is_enabled():
    """True if anything would start the app at login (Run entry or .lnk)."""
    if os.path.isfile(LEGACY_STARTUP_LNK):
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        return False


def enable():
    """Register the Run entry; migrate away the legacy Startup shortcut."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
    _remove_legacy_lnk()


def disable():
    """Remove the Run entry and the legacy Startup shortcut, if present."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass  # nothing registered — counts as disabled
    _remove_legacy_lnk()


def _remove_legacy_lnk():
    try:
        os.remove(LEGACY_STARTUP_LNK)
    except OSError:
        pass  # no legacy shortcut, or already gone
