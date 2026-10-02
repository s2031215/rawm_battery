"""Component test for rawm_autostart.

Round-trips enable()/disable() against the real HKCU Run key, then restores
whatever was there before — plus the legacy Startup .lnk, which disable()
would otherwise delete. Run:  python test_autostart.py
"""
import os
import winreg

import rawm_autostart

saved_value = None
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rawm_autostart.RUN_KEY) as key:
        saved_value = winreg.QueryValueEx(key, rawm_autostart.VALUE_NAME)
except OSError:
    pass  # nothing registered yet

lnk = rawm_autostart.LEGACY_STARTUP_LNK
lnk_backup = lnk + ".test-backup"
had_lnk = os.path.isfile(lnk)
if had_lnk:  # keep enable()'s migration from eating the real shortcut
    os.replace(lnk, lnk_backup)

try:
    rawm_autostart.disable()
    assert not rawm_autostart.is_enabled(), "disable() left autostart behind"

    rawm_autostart.enable()
    assert rawm_autostart.is_enabled(), "enable() did not register autostart"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rawm_autostart.RUN_KEY) as key:
        cmd = winreg.QueryValueEx(key, rawm_autostart.VALUE_NAME)[0]
    print(f"registered command: {cmd}")
    assert cmd.strip('"'), "empty autostart command"

    # a leftover .lnk alone must also show as enabled
    open(lnk, "w").close()
    assert rawm_autostart.is_enabled(), "legacy .lnk not detected as autostart"
    rawm_autostart.enable()
    assert not os.path.isfile(lnk), "enable() did not migrate away the .lnk"

    rawm_autostart.disable()
    assert not rawm_autostart.is_enabled(), "disable() left autostart behind"
finally:
    if had_lnk:
        os.replace(lnk_backup, lnk)
    else:
        try:
            os.remove(lnk)
        except OSError:
            pass
    if saved_value is not None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, rawm_autostart.RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, rawm_autostart.VALUE_NAME, 0, saved_value[1], saved_value[0])

print("PASS: enable/disable round-trip; .lnk detection + migration OK")
