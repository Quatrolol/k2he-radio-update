#!/usr/bin/env python3
"""
monitor.py: live view of a Keychron K2 HE and its Keychron Link receiver.

Prints a line whenever something changes:
  * keyboard transport as seen by its own firmware (USB / BLUETOOTH / 2.4G).
    Needs the cable. Plug it in with the switch in USB position; in our tests
    the raw HID channel kept answering after moving the switch to BT / 2.4G.
  * receiver slots: which keyboard VID:PID is stored and whether it is ON
  * number of keyboard HID nodes coming from Bluetooth

Useful for checking the mode switch and for watching 2.4G pairing attempts.
Stop with Ctrl+C.

Requirements: Python 3.8+, `pip install hidapi`.
"""
import argparse
import time

try:
    import hid
except ImportError:
    raise SystemExit("hidapi is not installed: pip install hidapi")

VID = 0x3434
PID_K2HE_ANSI = 0x0E20
PID_LINK = 0xD030
RAW_USAGE_PAGE = 0xFF60
TRANSPORT = {0: "NONE", 1: "USB", 2: "BLUETOOTH", 4: "2.4G"}


def open_raw(pid):
    t = [d for d in hid.enumerate(VID, pid) if d["usage_page"] == RAW_USAGE_PAGE]
    if not t:
        return None
    h = hid.device()
    h.open_path(t[0]["path"])
    h.set_nonblocking(1)
    return h


def factory_packet(cmd):
    p = [0xAB, cmd] + [0] * 30
    cs = sum(p[1:29])
    p[30], p[31] = cs & 0xFF, cs >> 8
    return p


def query(h, packet, pred, timeout=0.4):
    while h.read(64):
        pass
    h.write([0] + packet + [0] * (32 - len(packet)))
    t = time.time()
    while time.time() - t < timeout:
        r = h.read(64)
        if r and pred(r):
            return r
        time.sleep(0.005)
    return None


def keyboard_state(kb):
    if kb is None:
        return "kb: no USB raw HID"
    r = query(kb, factory_packet(0x05), lambda r: r[0] == 0xAB and r[1] == 0x05)
    if r is None:
        return "kb: no reply"
    return f"kb: {TRANSPORT.get(r[2], hex(r[2]))}"


def receiver_state(dg):
    if dg is None:
        return "receiver: absent"
    r = query(dg, [0xB2], lambda r: r[0] == 0xB2)
    if r is None:
        return "receiver: no reply"
    slots = []
    for i, o in enumerate((2, 7, 12)):
        vid = (r[o + 1] << 8) | r[o]
        pid = (r[o + 3] << 8) | r[o + 2]
        if vid or pid:
            slots.append(f"slot{i} {vid:04x}:{pid:04x} {'ON' if r[o + 4] else 'OFF'}")
    return "receiver: " + (", ".join(slots) or "empty")


def main():
    ap = argparse.ArgumentParser(description="K2 HE / Keychron Link live monitor")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=PID_K2HE_ANSI)
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--duration", type=float, default=0, help="seconds to run, 0 = until Ctrl+C")
    args = ap.parse_args()

    kb = dg = None
    prev = None
    end = time.time() + args.duration if args.duration else float("inf")
    print("Monitoring, Ctrl+C to stop", flush=True)
    try:
        while time.time() < end:
            try:
                if kb is None:
                    kb = open_raw(args.pid)
                if dg is None:
                    dg = open_raw(PID_LINK)
                bt = len([d for d in hid.enumerate(VID, args.pid) if d["interface_number"] == -1])
                s = f"{keyboard_state(kb)} | {receiver_state(dg)} | BT HID nodes: {bt}"
                if s != prev:
                    print(time.strftime("%X"), s, flush=True)
                    prev = s
            except (OSError, ValueError) as e:
                print(time.strftime("%X"), "device lost:", e, flush=True)
                kb = dg = None
                time.sleep(0.5)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
