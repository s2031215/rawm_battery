"""Battery-level reader for the RAWM SA-MH01 mouse (RAWM Receiver dongle).

The HID protocol below was reverse-engineered from the behavior of RAWM's
official web hub (https://www.rawmtech.com/hub.html). The receiver (Nordic
VID 0x1915) exposes a vendor HID collection (usage page 0xFF00) that speaks a
framed event protocol over report-ID-0 input/output reports.

Wire format
-----------
All host->device writes are 64-byte output reports (hidapi needs a leading 0x00
report-ID byte on Windows, so pass 65 bytes).

Wired / receiver-directed event:
    [0x80|len] [cmd|(len>>8)<<4] [len&0xFF] [body...]

Mouse-directed (forwarded over EB/2.4G by the receiver):
    [0xC0|channel] [0x80|len] [event...]

Battery query = CMD_QUERY (0x01) on ESB channel 0:
    [0x00] [0xC0] [0x8D] [01 0D 03 00 00 <unix-seconds LE64>]
    (~ = 00 C0 8D 01 0D 03 00 00 xx xx xx xx 00 00 00 00 00 00 00 00 + zero padding)

Device->host input reports (64 bytes) carry up to two 32-byte records:
    [0xC0|ch] [0x80|len] [payload...]  -- event data record
    [0xC0|ch] [0x40|..] [free-buf-size announce -- ignored]
The mouse's CMD_QUERY_RESULT (0x02) frame inside a record stream:
    FF FF FF FF [cmd|hi_len] [lo_len] <JSON of declared length>
The JSON contains "dn" (device name), "battery" (percent), "chr" (charging).
The receiver's own query answer arrives unframed-directly the same way with
channel-less records.

Usage
-----
    python rawm_battery.py            # one-shot read
    python rawm_battery.py --watch    # keep listening, print every change
    python rawm_battery.py --json     # dump the full device JSON
    python rawm_battery.py --debug    # dump raw HID reports
"""

import argparse
import json
import re
import sys
import time

import hid

RAWM_VENDOR_ID = 0x1915

PACKET_TYPE_MASK = 0xC0
PACKET_TYPE_EVENT_DATA = 0x80
PACKET_TYPE_BUFFER_SIZE = 0x40
PACKET_TYPE_EXTENDED = 0xC0
PACKET_SIZE_MASK = 0x3F

CMD_QUERY = 0x01
CMD_QUERY_RESULT = 0x02
CMD_NOTIFY = 0x0B
OS_PC = 0x03

ESB_CHANNEL_MOUSE = 0x00

NOTIFY_TYPE_MOUSE_BATTERY = 0x17
NOTIFY_TYPE_MOUSE_RSSI = 0x1C

REPORT_SIZE = 64
QUERY_RETRY_SEC = 2.0


class RawmReceiverError(RuntimeError):
    pass


def _find_receiver_paths():
    """Return HID paths of the receiver's vendor (0xFF00) collections."""
    return [d["path"] for d in hid.enumerate(vendor_id=RAWM_VENDOR_ID) if d["usage_page"] == 0xFF00]


def _query_event():
    ts = int(time.time())
    body = [CMD_QUERY, 0x00, OS_PC, 0x00, 0x00] + [(ts >> s) & 0xFF for s in range(0, 64, 8)]
    body[1] = len(body) & 0xFF  # crc_process() stamps the length into byte 1
    return body


def _wrap_wired(event):
    packet = bytes([0x00, PACKET_TYPE_EVENT_DATA | len(event)]) + bytes(event)
    return packet + bytes(REPORT_SIZE + 1 - len(packet))


def _wrap_esb(event, channel=ESB_CHANNEL_MOUSE):
    """[reportID 0x00][0xC0|ch][0x80|len][event...] padded to 64+1 bytes."""
    packet = bytes([0x00, PACKET_TYPE_EXTENDED | channel, PACKET_TYPE_EVENT_DATA | len(event)]) + bytes(event)
    return packet + bytes(REPORT_SIZE + 1 - len(packet))


def _iter_records(report):
    """Yield (kind, payload) records packed into one 64-byte input report.

    Records are [0xC0|ch][0x80|len][data...] or buffer-size announcements.
    """
    i = 0
    data = bytes(report)
    while i + 2 <= len(data):
        head = data[i]
        if (head & PACKET_TYPE_MASK) == PACKET_TYPE_EXTENDED:
            i += 1  # strip channel byte
            head = data[i] if i < len(data) else 0
        if (head & PACKET_TYPE_MASK) == PACKET_TYPE_EVENT_DATA:
            length = head & PACKET_SIZE_MASK
            if i + 1 + length > len(data):
                return
            yield "data", data[i + 1 : i + 1 + length]
            i += 1 + length
        elif (head & PACKET_TYPE_MASK) == PACKET_TYPE_BUFFER_SIZE:
            return  # flow-control announcement, rest of report is padding
        else:
            return


def _frame_events(stream):
    """Parse complete frames out of a reassembled record stream.

    Frame: FF FF FF FF [cmd|len_hi] [len_lo] body...  (length covers cmd..end)
    Yields (cmd, body_bytes).
    """
    i = 0
    data = stream
    n = len(data)
    while i + 6 <= n:
        if data[i : i + 4] == b"\xff\xff\xff\xff":
            cmd = data[i + 4] & 0x0F
            length = ((data[i + 4] >> 4) << 8) | data[i + 5]
            end = i + 4 + length
            if 2 <= length <= n - (i + 4):
                yield cmd, data[i + 6 : min(end, n)]
                i = end
                continue
        i += 1


def _extract_json(streams):
    """Find and parse the query-result JSON from reassembled streams."""
    candidates = []
    for stream in streams:
        for cmd, body in _frame_events(stream):
            if cmd == CMD_QUERY_RESULT and body[:1] == b"{":
                candidates.append(body)
        # fallback: raw substring scan for a JSON object with a battery key
        for m in re.finditer(rb'\{"[^{}]*"battery"\s*:', stream):
            end = stream.find(b"}", m.start())
            if end != -1:
                candidates.append(stream[m.start() : end + 1])
    for blob in candidates:
        try:
            info = json.loads(blob.decode("utf-8", errors="replace").rstrip("\x00"))
        except json.JSONDecodeError:
            continue
        if "battery" in info or "dn" in info:
            return info
    return None


def read_battery(timeout=15.0, debug=False):
    """Query the mouse through the receiver and return its status dict.

    Raises RawmReceiverError if the receiver is missing or the mouse does not
    answer before ``timeout`` (it may be asleep -- move/click it and retry).
    """
    paths = _find_receiver_paths()
    if not paths:
        raise RawmReceiverError(
            "RAWM Receiver not found (no HID device with VID 0x1915 and a "
            "0xFF00 vendor collection). Is the dongle plugged in?"
        )

    dev = hid.device()
    dev.open_path(paths[0])
    try:
        dev.set_nonblocking(True)
        query, last_send = _wrap_esb(_query_event()), 0.0
        streams = [bytearray(), bytearray()]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if time.monotonic() - last_send >= QUERY_RETRY_SEC:
                dev.write(query)
                last_send = time.monotonic()
                if debug:
                    print("OUT:", query[1:].hex(" "))
            report = dev.read(REPORT_SIZE)
            if not report:
                time.sleep(0.01)
                continue
            if debug:
                print("IN :", bytes(report).hex(" "))
            for index, (kind, payload) in enumerate(_iter_records(report)):
                if kind == "data" and index < len(streams):
                    streams[index] += payload
            info = _extract_json(streams)
            if info and "battery" in info:
                return info
        partial = _extract_json(streams)
        if partial:
            raise RawmReceiverError(
                f"device answered but reported no battery field: {partial!r}"
            )
        raise RawmReceiverError(
            "No answer from the mouse within {:.0f}s -- asleep or off? "
            "Move/click it and retry.".format(timeout)
        )
    finally:
        dev.close()


def watch(debug=False, interval_min=30):
    """Re-query the battery every ``interval_min`` minutes and print changes."""
    printed = None
    try:
        while True:
            try:
                info = read_battery(timeout=10.0, debug=debug)
            except RawmReceiverError as e:
                print(f"[{time.strftime('%H:%M:%S')}] {e}", file=sys.stderr)
                time.sleep(min(interval_min * 60, 60))
                continue
            current = (info.get("battery"), bool(info.get("chr", 0)))
            if current != printed:
                printed = current
                state = "charging" if current[1] else "discharging"
                name = info.get("dn", "RAWM mouse")
                print(f"[{time.strftime('%H:%M:%S')}] {name}: battery {current[0]}% ({state})")
            time.sleep(interval_min * 60)
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(description="Read RAWM SA-MH01 mouse battery level.")
    parser.add_argument("--watch", action="store_true", help="keep polling for updates")
    parser.add_argument("--json", action="store_true", help="dump full device JSON")
    parser.add_argument("--debug", action="store_true", help="dump raw HID reports")
    parser.add_argument(
        "--interval", type=float, default=30, metavar="MIN",
        help="with --watch: minutes between queries (default: 30)",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for an answer")
    args = parser.parse_args()

    if args.watch:
        watch(debug=args.debug, interval_min=args.interval)
        return

    try:
        info = read_battery(timeout=args.timeout, debug=args.debug)
    except RawmReceiverError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(info, indent=2))
        return

    state = "charging" if info.get("chr") else "discharging"
    line = f"{info.get('dn', 'RAWM mouse')} battery: {info.get('battery')}% ({state})"
    print(line)


if __name__ == "__main__":
    main()
