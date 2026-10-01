"""Power-event watcher: fires a callback when the PC resumes or the screen
turns on, so the tray monitor can re-read the battery immediately.

Implemented with a hidden top-level window that receives WM_POWERBROADCAST:
  - PBT_APMRESUMEAUTOMATIC / PBT_APMRESUMESUSPEND  -> system resumed
  - PBT_POWERSETTINGCHANGE with GUID_MONITOR_POWER_ON == 1 -> display woke
RegisterPowerSettingNotification is used to subscribe to monitor power-on.
"""

import ctypes
import threading
import uuid
from ctypes import wintypes

WM_DESTROY = 0x0002
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007
PBT_POWERSETTINGCHANGE = 0x8017

MONITOR_POWER_ON = uuid.UUID("{0BA15600-2167-4701-AC6A-2757B2A7EF1B}").bytes_le

GUID = ctypes.c_ubyte * 16
WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", GUID),
        ("DataLength", wintypes.DWORD),
        ("Data", ctypes.c_ubyte * 1),
    ]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.DefWindowProcW.restype = wintypes.LPARAM
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterPowerSettingNotification.restype = ctypes.c_void_p

_wndproc_ref = None  # keep the ctypes callback alive
last_reg_handle = None  # HPOWERNOTIFY from the last watch() run (for diagnostics)


def monitor_on_from_lparam(lparam):
    """Decode a PBT_POWERSETTINGCHANGE lParam: True if the screen just turned on."""
    if not lparam:
        return False
    setting = ctypes.cast(
        ctypes.c_void_p(lparam), ctypes.POINTER(POWERBROADCAST_SETTING)
    ).contents
    return bytes(setting.PowerSetting) == MONITOR_POWER_ON and bool(setting.Data[0])


def watch(on_event, window_title="RAWM power watch"):
    """Run the message loop until the process exits. Blocks the caller."""
    global _wndproc_ref

    def on_wakeup(reason):
        try:
            on_event(reason)
        except Exception:
            pass  # a listener crash must not take the message loop down

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
        elif msg == WM_POWERBROADCAST:
            if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                on_wakeup("resume")
            elif wparam == PBT_POWERSETTINGCHANGE and monitor_on_from_lparam(lparam):
                on_wakeup("monitor-on")
            return 1  # processed
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _wndproc_ref = WNDPROC(wndproc)
    hinstance = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(
        style=0,
        lpfnWndProc=_wndproc_ref,
        hInstance=hinstance,
        lpszClassName="RAWM_PowerWatch_" + str(id(wndproc)),
    )
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError()
    hwnd = user32.CreateWindowExW(
        0, wc.lpszClassName, window_title, 0,
        0, 0, 0, 0, None, None, hinstance, None,
    )
    if not hwnd:
        raise ctypes.WinError()
    global last_reg_handle
    last_reg_handle = user32.RegisterPowerSettingNotification(
        hwnd, GUID.from_buffer_copy(MONITOR_POWER_ON), 0
    )
    if not last_reg_handle:
        # resume events still arrive as broadcasts; only screen-on is lost
        import sys
        if sys.stderr:
            print("RegisterPowerSettingNotification failed; screen-on detection off", file=sys.stderr)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def start(on_event, window_title="RAWM power watch"):
    """Run the watcher on a daemon thread."""
    threading.Thread(target=watch, args=(on_event, window_title), daemon=True).start()
