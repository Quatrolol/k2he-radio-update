#!/usr/bin/env python3
"""
k2he_radio_update.py: update the wireless (BT + 2.4 GHz) module firmware
of a Keychron K2 HE over USB raw HID.

Unofficial tool, use at your own risk. Not affiliated with Keychron.

What it does:
  info     show main firmware, current transport and radio module model/version
  list     list module firmware versions published on the Keychron Launcher API
  update   download the latest (or --version / --file) firmware and flash it

Safety checks before flashing:
  * keyboard must be connected by cable AND the mode switch set to USB (cable)
  * the module model reported by the keyboard must be present inside the file
  * the version string must be present inside the downloaded file
  * no downgrade / reflash of the same version without --force
  * explicit confirmation unless --yes
  * every data packet is acknowledged, CRC32 is verified by the module before
    switching to the new firmware

If flashing is interrupted, the module bootloader stays intact: Bluetooth and
2.4G stop working, the keyboard keeps working by cable, and you can rerun the
tool. Still: do not unplug the cable while flashing.

Requirements: Python 3.8+, `pip install hidapi`.
Linux: needs read/write access to /dev/hidraw* (udev rule or sudo).
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
import zlib

try:
    import hid
except ImportError:
    sys.exit("hidapi is not installed: pip install hidapi")

VID = 0x3434
PID_K2HE_ANSI = 0x0E20
RAW_USAGE_PAGE = 0xFF60

# Launcher API ids of radio modules: VID 0xFF99 and module PID glued into one int
MODULE_API_IDS = {
    b"KCH_KB1": 4288282521,  # 0xFF99FF99
    b"CKBT51": 4288282520,   # 0xFF99FF98
    b"KCH_KB2": 4288282519,  # 0xFF99FF97
}
API = "https://launcher.keychron.com/vapi/v2"

TRANSPORT = {0: "NONE", 1: "USB", 2: "BLUETOOTH", 4: "2.4G"}

HDR, NOACK, ACK = 0xAA, 0x55, 0x56


def nor(x):
    return (~x) & 0xFF


def hx(r):
    return " ".join("%02x" % x for x in r)


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


# ---------------------------------------------------------------- HID layer
class Keyboard:
    def __init__(self, pid):
        devs = [d for d in hid.enumerate(VID, pid) if d["usage_page"] == RAW_USAGE_PAGE]
        if not devs:
            sys.exit(
                "Keyboard raw HID interface not found.\n"
                "Connect the keyboard by cable and set the mode switch to USB (cable)."
            )
        self.h = hid.device()
        self.h.open_path(devs[0]["path"])
        self.h.set_nonblocking(1)

    def close(self):
        self.h.close()

    def drain(self):
        while self.h.read(64):
            pass

    def send(self, p):
        self.h.write([0] + p + [0] * (32 - len(p)))

    def rd(self, timeout, pred=lambda r: True):
        t = time.time()
        while time.time() - t < timeout:
            r = self.h.read(64)
            if r and pred(r):
                return r
            if not r:
                time.sleep(0.002)
        return None

    # --- factory test channel (0xAB), see keychron/common/factory_test.c
    def factory(self, cmd, timeout=0.8):
        p = [0xAB, cmd] + [0] * 30
        cs = sum(p[1:29])
        p[30], p[31] = cs & 0xFF, cs >> 8
        self.drain()
        self.send(p)
        return self.rd(timeout, lambda r: r[0] == 0xAB and r[1] == cmd)

    def transport(self):
        r = self.factory(0x05)
        return None if r is None else r[2]

    def build(self):
        r = self.factory(0x08)
        if r is None:
            return None
        return bytes(b for b in r[2:30] if 32 <= b < 127).decode().strip()

    # --- radio module passthrough (0xAA, KC_WIRELESS_DFU), see lkbt51.c
    def module_info(self):
        self.drain()
        self.send([HDR, NOACK, 3, nor(3), 1, 0x60, 0x60, 0])
        y, M, t = [], None, time.time()
        while time.time() - t < 2.5:
            r = self.rd(0.5, lambda r: r[0] == 3)
            if r is None:
                continue
            r = r[1:]
            if M is None:
                if r[0] == 0xAA and r[1] == 0x57:
                    M = r[2]
                    n = r[5:]
                    y += n
                    M -= len(n)
            else:
                n = r if M > len(r) else r[:M]
                y += n
                M -= len(n)
            if M is not None and M <= 0:
                break
        if not y:
            return None
        model = bytes(b for b in y[4:14] if b)
        fw = bytes(b for b in y[16:25] if b).decode(errors="replace")
        hw = bytes(b for b in y[26:36] if b).decode(errors="replace")
        return model, fw, hw

    def dfu_version(self):
        self.drain()
        self.send([HDR, NOACK, 3, nor(3), 2, 0x61, 0x61, 0])
        r = self.rd(2)
        if r is None:
            sys.exit("Radio module bootloader did not answer (0x61).")
        return r[10]

    def flash(self, data):
        dv = self.dfu_version()
        print(f"Module bootloader (DFU) version: {dv}")

        self.drain()
        self.send([HDR, NOACK, 4, nor(4), 2, 0x62, 0, 0x62, 0])
        if self.rd(5) is None:
            sys.exit("No reply to START_DFU (0x62). Nothing was written.")

        self.drain()
        cs = 0x63 + dv
        self.send([HDR, ACK, 4, nor(4), 3, 0x63, dv, cs & 0xFF, cs >> 8])
        if self.rd(10) is None:
            sys.exit("No reply to PREPARE (0x63). Nothing was written.")

        packets, g = [], 1
        for off in range(0, len(data), 16):
            w = list(data[off:off + 16])
            n = len(w) + 5
            b = [HDR, ACK, n, nor(n), g, 0x64, 0, g] + w
            s = 0x64 + g + sum(w)
            packets.append(b + [s & 0xFF, (s >> 8) & 0xFF])
            g = 0 if g >= 255 else g + 1

        total, t0 = len(packets), time.time()
        for i, p in enumerate(packets):
            for _ in range(5):
                self.send(p)
                if self.rd(2, lambda r: r[1] == 0xAA and r[2] == 0x57):
                    break
            else:
                sys.exit(f"FLASH ERROR at packet {i}/{total}. Do not unplug, rerun the tool.")
            if i % 200 == 0 or i == total - 1:
                pct = (i + 1) * 100 / total
                bar = "#" * int(pct // 4)
                print(f"\r  [{bar:<25}] {pct:5.1f}%  {time.time() - t0:4.0f}s", end="", flush=True)
        print()

        crc = zlib.crc32(data) ^ 0xFFFFFFFF  # module expects CRC32 without final XOR
        if crc >= 0xFFFFFFFF:
            crc = 0
        cb = list(crc.to_bytes(4, "little"))
        pkt = [HDR, NOACK, 11, nor(11), 1, 0x65] + cb + cb
        j = sum(pkt[5:14])
        pkt += [j & 0xFF, (j >> 8) & 0xFF]
        self.drain()
        self.send(pkt)
        r = self.rd(3)
        s = hx(r).replace(" ", "").upper() if r else ""
        m = re.search(r"AA5704FB[0-9A-F]{2}A3[0-9A-F]{2}65([0-9A-F]{4})", s)
        if not r or (m and m.group(1) != "0000"):
            sys.exit("CRC verification FAILED, new firmware NOT activated. Rerun the tool.")
        print("CRC32 verified, switching module to new firmware (0x66)...")
        self.send([HDR, NOACK, 3, nor(3), 1, 0x66, 0x66, 0])
        time.sleep(3)


# ---------------------------------------------------------------- API layer
def api_get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "k2he-radio-update"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.load(r)
    if j.get("code") != 200:
        sys.exit(f"Launcher API error: {j}")
    return j["data"]


def firmware_list(api_id):
    items = api_get(f"/firmware/{api_id}")["list"]
    return sorted(items, key=lambda x: vtuple(x["version"]), reverse=True)


def changelog(item):
    for d in item.get("languageDesc") or []:
        if d.get("language") == "en-US":
            return d.get("description", "")
    return ""


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "k2he-radio-update"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


# ---------------------------------------------------------------- commands
def read_state(kb):
    tr = kb.transport()
    print(f"Main firmware : {kb.build() or '?'}")
    print(f"Transport     : {TRANSPORT.get(tr, tr)}")
    mi = kb.module_info()
    if mi is None:
        sys.exit("Radio module did not answer (0x60).")
    model, fw, hw = mi
    print(f"Radio module  : {model.decode(errors='replace')}  fw {fw}  hw {hw}")
    return tr, model, fw


def cmd_info(args):
    kb = Keyboard(args.pid)
    try:
        read_state(kb)
    finally:
        kb.close()


def cmd_list(args):
    model = args.model.encode()
    if model not in MODULE_API_IDS:
        sys.exit(f"Unknown module model {args.model}; known: {[m.decode() for m in MODULE_API_IDS]}")
    for it in firmware_list(MODULE_API_IDS[model]):
        note = changelog(it)
        print(f"{it['version']:8} {it['update_time'][:10]}  {note}")


def cmd_update(args):
    kb = Keyboard(args.pid)
    try:
        tr, model, cur = read_state(kb)
        if tr != 1:
            sys.exit("\nSet the mode switch to USB (cable) and keep the cable connected, then rerun.")

        if args.file:
            data = open(args.file, "rb").read()
            target = "?"
            m = re.search(rb"\d+\.\d+\.\d+", data[data.find(model):] if model in data else b"")
            if m:
                target = m.group().decode()
        else:
            if model not in MODULE_API_IDS:
                sys.exit(f"No Launcher API id known for module {model!r}; use --file.")
            items = firmware_list(MODULE_API_IDS[model])
            if args.version:
                items = [i for i in items if i["version"] == args.version]
                if not items:
                    sys.exit(f"Version {args.version} not found on the Launcher API.")
            it = items[0]
            target = it["version"]
            print(f"Target        : {target} ({it['update_time'][:10]}) {changelog(it)}")
            print(f"Downloading   : {it['path']}")
            data = download(it["path"])
            if target.encode() not in data:
                sys.exit("ABORT: version string not found inside downloaded file.")

        print(f"File          : {len(data)} bytes, SHA-256 {hashlib.sha256(data).hexdigest()}")
        if model not in data:
            sys.exit(f"ABORT: module model {model!r} not found in file, wrong firmware.")

        if target != "?" and vtuple(target) <= vtuple(cur) and not args.force:
            sys.exit(f"Module already on {cur} (target {target}). Use --force to reflash/downgrade.")

        if args.dry_run:
            print("\nDry run: all checks passed, nothing was flashed.")
            return

        if not args.yes:
            a = input(f"\nFlash radio module {cur} -> {target}? Do not unplug the cable. [y/N] ")
            if a.strip().lower() not in ("y", "yes", "д", "да"):
                sys.exit("Cancelled.")

        kb.flash(data)
        mi = kb.module_info()
        if mi:
            print(f"Done. Radio module now reports fw {mi[1]}")
        else:
            print("Done. Module did not answer yet; replug the cable and run `info`.")
        print("Now set the switch to 2.4G, hold Fn+4 for ~4 s and replug the receiver.")
    finally:
        kb.close()


def main():
    ap = argparse.ArgumentParser(description="Keychron K2 HE radio module updater (unofficial)")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=PID_K2HE_ANSI,
                    help="keyboard USB PID (default 0x0E20, K2 HE ANSI). Other models: at your own risk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show keyboard and radio module state")
    p = sub.add_parser("list", help="list module firmware on the Launcher API")
    p.add_argument("--model", default="KCH_KB1")
    p = sub.add_parser("update", help="download and flash module firmware")
    p.add_argument("--version", help="specific version instead of the latest")
    p.add_argument("--file", help="local firmware .bin instead of downloading")
    p.add_argument("--force", action="store_true", help="allow reflash of same/older version")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--dry-run", action="store_true", help="run all checks and download, but do not flash")
    args = ap.parse_args()
    {"info": cmd_info, "list": cmd_list, "update": cmd_update}[args.cmd](args)


if __name__ == "__main__":
    main()
