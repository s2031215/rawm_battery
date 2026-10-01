"""Component test for rawm_power.

- resume path: posts a synthetic WM_POWERBROADCAST(PBT_APMRESUMEAUTOMATIC) to
  the watcher window (PostMessageW allows that variant) and expects a callback.
- screen-on path: Windows rejects posting PBT_POWERSETTINGCHANGE with a
  user-supplied payload (system-reserved message), so the lParam parsing is
  unit-tested directly; OS delivery is covered by the live registration check.
"""
import ctypes
import threading
import time
from ctypes import wintypes

import rawm_power

fired = []


def callback(reason):
    fired.append(reason)
    print(f"EVENT: {reason}", flush=True)


threading.Thread(target=rawm_power.watch, args=(callback,), daemon=True).start()
time.sleep(1.5)

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
hwnd = user32.FindWindowW(None, "RAWM power watch")
print(f"watcher window hwnd: {hwnd}")
assert hwnd, "watcher window not found"
assert rawm_power.last_reg_handle, "RegisterPowerSettingNotification did not return a handle"
print(f"monitor power-on registration handle: {rawm_power.last_reg_handle:#x}")

# 1) resume event, end to end through the message loop
user32.PostMessageW(hwnd, rawm_power.WM_POWERBROADCAST, rawm_power.PBT_APMRESUMEAUTOMATIC, 0)
deadline = time.monotonic() + 3
while not fired and time.monotonic() < deadline:
    time.sleep(0.05)
assert fired and fired[-1] == "resume", fired

# 2) screen-on lParam parsing, unit test
class PBS(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", ctypes.c_ubyte * 16),
        ("DataLength", wintypes.DWORD),
        ("Data", ctypes.c_ubyte * 1),
    ]

buf = PBS()
buf.PowerSetting = (ctypes.c_ubyte * 16).from_buffer_copy(rawm_power.MONITOR_POWER_ON)
buf.DataLength = 1
buf.Data[0] = 1
assert rawm_power.monitor_on_from_lparam(ctypes.addressof(buf)) is True, "monitor-on parse failed"
buf.Data[0] = 0
assert rawm_power.monitor_on_from_lparam(ctypes.addressof(buf)) is False, "monitor-off must not fire"
assert rawm_power.monitor_on_from_lparam(0) is False, "null lParam must not fire"

print("PASS: resume end-to-end fires; screen-on parsing + registration OK")
