r"""System-tray battery monitor for the RAWM SA-MH01 mouse.

Shows the battery percentage as the tray icon (color-coded: green >= 40%,
yellow >= 30%, orange >= 20%, red below; blue while charging) and refreshes it
by querying the mouse through the RAWM Receiver dongle every 30 minutes
(see rawm_battery.py for the reverse-engineered protocol).

Requirements:
    pip install hidapi pystray Pillow

Run:
    pythonw rawm_tray.py                 # refresh every 30 minutes, no console
    pythonw rawm_tray.py --interval 15   # refresh every 15 minutes

Tray menu: status line, "Refresh now", "Exit". A low-battery balloon fires
once per app run when the battery is below the alert threshold (default 20%;
change it in the tray menu under "Notify below" — the choice persists in
%APPDATA%\RAWM\tray_config.json).
"""

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from pystray import Icon, Menu, MenuItem

import rawm_battery
import rawm_power

DEFAULT_INTERVAL_MIN = 30
RETRY_INTERVAL_MIN = 5  # after a failed refresh, retry sooner than the full interval
DEFAULT_NOTIFY_LEVEL = 20
NOTIFY_LEVEL_CHOICES = (10, 15, 20, 25, 30)

CONFIG_PATH = os.path.join(os.environ.get("APPDATA", ""), "RAWM", "tray_config.json")


def _load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            level = int(json.load(f).get("notify_level", DEFAULT_NOTIFY_LEVEL))
        if 1 <= level <= 100:
            return {"notify_level": level}
    except (OSError, ValueError, AttributeError):
        pass
    return {"notify_level": DEFAULT_NOTIFY_LEVEL}


def _save_config(config):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f)
    except OSError:
        pass  # persistence is best-effort; defaults still apply this run
APP_USER_MODEL_ID = "RAWM.SAMH01.Tray"  # matches RAWM SA-MH01.lnk in Start Menu


def _set_app_id():
    """Claim our AppUserModelID so notifications show 'RAWM SA-MH01', not
    'Python'. The matching Start Menu shortcut (setup_shortcut.ps1) carries
    the same System.AppUserModel.ID property."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass  # cosmetic only; falls back to the process description
READ_TIMEOUT_SEC = 12.0
UNKNOWN_COLOR = "#9E9E9E"
CHARGING_COLOR = "#29B6F6"
LEVEL_COLORS = ((40, "#43D17A"), (30, "#FFB300"), (20, "#FF7043"), (-1, "#F44336"))
# small lightning bolt, top-right of the icon
BOLT_POINTS = [(58, 2), (47, 18), (53, 18), (48, 33), (61, 13), (54, 13)]


def _level_color(percent):
    for floor, color in LEVEL_COLORS:
        if percent >= floor:
            return color
    return LEVEL_COLORS[-1][1]


def _font(size):
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(f"C:\\Windows\\Fonts\\{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(percent=None, charging=False):
    """Render the 64x64 tray icon: big percent digits, bolt overlay if charging."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    text = "?" if percent is None else str(percent)
    stroke = 2
    size = 56 if len(text) <= 2 else 36
    font = _font(size)
    # shrink until digits + stroke fill but fit the canvas
    while size > 10:
        font = _font(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left + 2 * stroke <= 64 and bottom - top + 2 * stroke <= 64:
            break
        size -= 2
    x = (64 - (right - left)) / 2 - left
    y = (64 - (bottom - top)) / 2 - top
    if charging:
        color = CHARGING_COLOR
    elif percent is None:
        color = UNKNOWN_COLOR
    else:
        color = _level_color(percent)
    draw.text(
        (x, y), text, font=font, fill=color,
        stroke_width=stroke, stroke_fill=(0, 0, 0, 210),
    )
    if charging:
        draw.polygon(BOLT_POINTS, fill="#FFEB3B", outline=(0, 0, 0, 210))
    return image


class TrayMonitor:
    """Owns the device state; safe to drive from a worker thread."""

    def __init__(self, interval_min=DEFAULT_INTERVAL_MIN):
        self.interval_min = interval_min
        self.interval_sec = interval_min * 60
        self.notify_level = _load_config()["notify_level"]
        self._busy = False
        self.info = None
        self.error = None
        self.updated = None
        self._low_notified = False  # low-battery alert fires once per app run

    def set_notify_level(self, level):
        """Persist the user's alert threshold and re-arm the alert."""
        self.notify_level = int(level)
        self._low_notified = False
        _save_config({"notify_level": self.notify_level})

    # ----- device access -----

    def refresh(self, icon, notify_on_done=False):
        if self._busy:
            return
        self._busy = True
        try:
            try:
                info = rawm_battery.read_battery(timeout=READ_TIMEOUT_SEC)
            except rawm_battery.RawmReceiverError as e:
                self.error = str(e)
            except OSError as e:  # dongle vanished / HID error: keep the app alive
                self.error = f"HID error: {e}"
            else:
                self.info, self.error = info, None
                self.updated = datetime.now()
                self._notify_low(icon, info)
        finally:
            self._busy = False
        self.apply(icon)
        if notify_on_done:
            try:
                icon.notify(self.status_text(), "RAWM battery")
            except Exception:
                pass  # notification is best-effort

    def _notify_low(self, icon, info):
        percent = info.get("battery")
        if percent is None:
            return
        charging = bool(info.get("chr"))
        if percent < self.notify_level and not charging and not self._low_notified:
            self._low_notified = True  # once per app run, even if it keeps dropping
            try:
                icon.notify(
                    f"RAWM {info.get('dn', 'mouse')} battery is at {percent}% — time to charge.",
                    "RAWM battery low",
                )
            except Exception:
                pass  # notification is best-effort

    # ----- presentation -----

    def apply(self, icon):
        percent, charging, name = None, False, "RAWM SA-MH01"
        if self.info:
            percent = self.info.get("battery")
            charging = bool(self.info.get("chr"))
            name = f"RAWM {self.info.get('dn', 'mouse')}"
        if percent is None:
            title = f"{name}: no data"
        else:
            title = f"{name}: {percent}% ({'charging' if charging else 'discharging'})"
        if self.updated:
            title += f" · updated {self.updated:%H:%M}"
        if self.error:
            title += f" · retrying in {RETRY_INTERVAL_MIN} min"
        icon.title = title[:127]  # Windows tooltip limit
        icon.icon = make_icon(percent, charging)

    def status_text(self):
        if self.info and self.info.get("battery") is not None:
            text = "{}% ({})".format(
                self.info["battery"],
                "charging" if self.info.get("chr") else "discharging",
            )
        else:
            text = "no data yet"
        if self.updated:
            text += f" · updated {self.updated:%H:%M}"
        if self.error:
            text += f" · retrying in {RETRY_INTERVAL_MIN} min · {self.error}"
        return text

    # ----- worker loop -----

    def worker(self, icon):
        first = True
        while True:
            try:
                self.refresh(icon)
            except Exception as e:  # never let the loop die silently
                if sys.stderr:
                    print(f"refresh failed: {e}", file=sys.stderr)
                if self.info is None:
                    self.error = str(e)
                    self.apply(icon)
            if first:
                first = False
                self._startup_notify(icon)
            # after a failed read, try again in 5 min instead of waiting a full interval
            wait_min = self.interval_min if self.error is None else RETRY_INTERVAL_MIN
            time.sleep(wait_min * 60)

    def _startup_notify(self, icon):
        try:
            icon.notify(f"Monitoring started — {self.status_text()}", "RAWM battery")
        except Exception:
            pass  # notification is best-effort


def build(interval_min=DEFAULT_INTERVAL_MIN):
    """Return the configured (icon, monitor) pair, ready for icon.run()."""
    monitor = TrayMonitor(interval_min)

    def start_refresh(icon, item):
        threading.Thread(
            target=monitor.refresh, args=(icon,), kwargs={"notify_on_done": True}, daemon=True
        ).start()

    def set_level(level):
        def action(icon, item):
            monitor.set_notify_level(level)
        return action

    notify_menu = Menu(*[
        MenuItem(
            f"{level}%", set_level(level), radio=True,
            checked=lambda item, lvl=level: monitor.notify_level == lvl,
        )
        for level in NOTIFY_LEVEL_CHOICES
    ])

    menu = Menu(
        MenuItem(lambda item: monitor.status_text(), None, enabled=False),
        MenuItem("Refresh now", start_refresh),
        MenuItem("Notify below", notify_menu),
        Menu.SEPARATOR,
        MenuItem("Exit", lambda icon, item: icon.stop()),
    )
    icon = Icon("rawm_battery", make_icon(), "RAWM SA-MH01 battery", menu)
    return icon, monitor


def _already_running():
    """True if another tray instance holds our mutex (single-instance guard)."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "RawmSaMh01TrayMonitor")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    _set_app_id()
    if _already_running():
        if sys.stderr:
            print("RAWM tray monitor is already running.", file=sys.stderr)
        return
    parser = argparse.ArgumentParser(description="RAWM SA-MH01 battery in the system tray.")
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_MIN, metavar="MIN",
        help="minutes between battery queries (default: 30)",
    )
    args = parser.parse_args()

    icon, monitor = build(args.interval)

    def setup(icon):
        icon.visible = True  # required when passing a custom setup to run()
        threading.Thread(target=monitor.worker, args=(icon,), daemon=True).start()
        # re-read the battery right away when the PC resumes or the screen turns on
        rawm_power.start(lambda reason: threading.Thread(
            target=monitor.refresh, args=(icon,), daemon=True).start())

    icon.run(setup=setup)


if __name__ == "__main__":
    main()
