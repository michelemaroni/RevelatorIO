#!/usr/bin/env python3
"""
ucnet_shim — speak UCNET on loopback so existing PreSonus plugins drive the io24
on Linux.

On Windows, plugins such as oddbear's Stream Deck / Touch Portal integrations talk
UCNET over TCP to PreSonusHardwareAccessService.exe, which bridges to USB. This
serves the same conversation and translates to our native USB protocol, so those
plugins work unmodified.

  ucnet_shim.py [--port N] [--device-id N] [--serial S] [--dry-run]

Derived from the UCNET compatibility contract documented in PROTOCOL.md §9.
Key constraints honoured here:
  * UDP 'DA' announce is SENT to 127.0.0.1:47809 once a second — the port is
    never bound, because the client binds it and only listens.
  * Every UCNET message goes out in exactly ONE write(): the client splits reads
    on the "UC\\0\\x01" magic and never reassembles.
  * Message bodies stay under 0x4000; the real service drops the link otherwise.
  * Booleans are emitted as 0.0/1.0 — JSON true/false is silently dropped by the
    client's Traverse.
  * Every accepted PV/PS is echoed back; the client does not update its own cache.

SAFETY: 'line/chN/pan' and 'line/chN/dawpostdsp' are host-bound to block 0, which
resolves to the device object where wire id 2 is mainVolume. Forwarding them would
slam the main output, so they are shadowed and never sent. See BLOCKED below.
"""
import json
import math
import socket
import struct
import sys
import threading
import time

try:
    from io24 import Io24
except Exception:
    Io24 = None

try:
    import io24_meters as METERS
except Exception:
    METERS = None

MAGIC = b"UC\x00\x01"
DISCOVERY_ADDR = ("127.0.0.1", 47809)
CLIENT_ID = 104
MAX_BODY = 0x4000


def _shadowed_phones_source(dev):
    """Return UCNET's normalized route only for an identified Host shadow."""
    shadow = getattr(dev, "_shadow", {})
    if not isinstance(shadow, dict):
        return None
    aliases = getattr(Io24, "PHONE_SOURCE_ALIASES", {}) if Io24 else {}
    sources = getattr(Io24, "PHONE_SOURCES", {}) if Io24 else {}
    for call in shadow.values():
        if not isinstance(call, dict) or call.get("fn") != "set_phones_source":
            continue
        source = (call.get("kwargs") or {}).get("source")
        try:
            if isinstance(source, str):
                key = source.strip().lower()
                value = sources[aliases.get(key, key)]
            elif isinstance(source, bool):
                continue
            else:
                numeric = float(source)
                if not math.isfinite(numeric) or not numeric.is_integer():
                    continue
                value = int(numeric)
            if value in (0, 1, 2):
                return value / 2.0
        except (KeyError, TypeError, ValueError):
            continue
    return None

# ---------------------------------------------------------------- route table
# route -> (blob, wire id, uses channel index, encoder from normalised 0..1)
def _b(v):
    return 1 if v > 0.5 else 0


NATIVE = {
    "global/phonesVolume":   ("Para", 1,  False, lambda v: v),
    "global/mainOutVolume":  ("Para", 2,  False, lambda v: v),
    "global/monitorBlend":   ("Para", 10, False, lambda v: 2 * v - 1),
    "global/outputDelay":    ("Para", 14, False, lambda v: 0.5 * v),
    "global/phonesSrc":      ("Pari", 11, False, lambda v: round(2 * v)),
    "global/phonesMute":     ("Pari", 6,  False, _b),
    "global/presetButtonMode": ("Pari", 17, False, lambda v: round(2 * v)),
    "global/outputDelayBus": ("Pari", 13, False, lambda v: round(4 * v)),
    "line/ch{}/preampgain":  ("Para", 3,  True,  lambda v: 60 * v),
    "line/ch{}/48v":         ("Pari", 0,  True,  _b),
    "line/ch{}/hardwareMute": ("Pari", 7, True,  _b),
    "line/ch{}/link":        ("Pari", 9,  True,  _b),
    "line/ch{}/processingChannel": ("Pari", 12, True, lambda v: round(2 * v - 1)),
    "line/ch{}/activePresetSlotIndex": ("Pari", 16, True, lambda v: round(3 * v)),
}

# Never forward: host binds these to block 0 == the device object, where
# wire 2 = mainVolume and wire 3 = input gain.
BLOCKED = {"pan", "dawpostdsp"}

# JaSt read-back: route -> (slot, normaliser)
READBACK = {
    "global/phonesVolume":  (43, lambda x: x),
    "global/mainOutVolume": (44, lambda x: x),
    "global/monitorBlend":  (45, lambda x: (x + 1) / 2),
    "line/ch1/preampgain":  (46, lambda x: x / 60.0),
    "line/ch2/preampgain":  (47, lambda x: x / 60.0),
    "line/ch1/activePresetSlotIndex": (40, lambda x: x / 3.0),
    "line/ch2/activePresetSlotIndex": (41, lambda x: x / 3.0),
}


def route_lookup(route):
    """Return (blob, wireid, index, encoder) or None. Handles line/chN templating."""
    if route in NATIVE:
        blob, wid, _, enc = NATIVE[route]
        return blob, wid, 0, enc
    parts = route.split("/")
    if len(parts) == 3 and parts[0] == "line" and parts[1].startswith("ch"):
        try:
            ch = int(parts[1][2:])
        except ValueError:
            return None
        key = "line/ch{}/" + parts[2]
        if key in NATIVE and ch in (1, 2):
            blob, wid, _, enc = NATIVE[key]
            return blob, wid, ch - 1, enc
    return None


# ---------------------------------------------------------------- UCNET frames
def frame(mtype, payload, src, dst):
    body = mtype.encode("ascii") + struct.pack("<HH", src, dst) + payload
    if len(body) > MAX_BODY:
        raise ValueError("message body %d exceeds 0x4000" % len(body))
    return MAGIC + struct.pack("<H", len(body)) + body


def parse_frames(buf):
    """Split on the magic; return (messages, remainder). Never reassembles across
    the magic, matching the client's own chunker."""
    out, i = [], 0
    while True:
        j = buf.find(MAGIC, i)
        if j < 0:
            break
        if len(buf) < j + 8:
            break
        blen, = struct.unpack_from("<H", buf, j + 4)
        end = j + 6 + blen
        if len(buf) < end:
            break
        body = buf[j + 6:end]
        out.append((body[:2].decode("ascii", "replace"),
                    struct.unpack_from("<H", body, 2)[0],
                    struct.unpack_from("<H", body, 4)[0],
                    body[6:]))
        i = end
    return out, buf[i:]


def jm(obj, src, dst):
    js = json.dumps(obj, separators=(",", ":")).encode("ascii")
    return frame("JM", struct.pack("<I", len(js)) + js, src, dst)


def pv(route, value, src, dst):
    return frame("PV", route.encode("ascii") + b"\x00\x00\x00"
                 + struct.pack("<f", float(value)), src, dst)


def ps(route, value, src, dst):
    return frame("PS", route.encode("ascii") + b"\x00\x00\x00"
                 + value.encode("ascii") + b"\x00", src, dst)


def discovery_datagram(tcp_port, device_id, serial, model="Revelator IO 24",
                       firmware=296, display="Revelator io24 (Linux)"):
    d = bytearray(MAGIC)
    d += struct.pack("<H", tcp_port)          # NOT a length on the UDP path
    d += b"DA"
    d += struct.pack("<I", device_id)
    d += b"\x00" * 20                          # address/flags block
    for s in ("%s/%d" % (model, firmware), "AUD", serial, display):
        d += s.encode("ascii") + b"\x00"
    return bytes(d)


# ---------------------------------------------------------------- state model
class State:
    """Shadow state for every route, plus device read-back where possible."""

    def __init__(self, dev, usb_lock=None):
        self.dev = dev
        self.usb_lock = usb_lock or threading.Lock()
        self.values = {}
        self.strings = {}
        self.lock = threading.Lock()
        self._defaults()
        phones_source = _shadowed_phones_source(dev)
        if phones_source is not None:
            self.values["global/phonesSrc"] = phones_source

    def _defaults(self):
        g = {"phonesVolume": 0.5, "mainOutVolume": 0.5, "monitorBlend": 0.5,
             "phonesMute": 0.0, "outputDelay": 0.0,
             "outputDelayBus": 0.0, "presetButtonMode": 1.0,
             "aux1_mirror_main": 0.0, "aux2_mirror_main": 0.0,
             "auxMuteMode": 0.0, "enableChannelAssign": 1.0}
        for k, v in g.items():
            self.values["global/" + k] = v
        for ch in (1, 2):
            base = "line/ch%d/" % ch
            for k, v in {"preampgain": 0.0, "48v": 0.0, "hardwareMute": 0.0,
                         "mute": 0.0, "volume": 0.75, "solo": 0.0, "lr": 1.0,
                         "assign_aux1": 1.0, "assign_aux2": 1.0, "aux1": 0.75,
                         "aux2": 0.75, "FXA": 0.0, "link": 0.0, "pan": 0.5,
                         "processingChannel": 0.5, "activePresetSlotIndex": 0.0,
                         "autogain": 0.0, "autogainmode": 0.0}.items():
                self.values[base + k] = v
            self.strings[base + "chnum"] = str(ch)
            self.strings[base + "username"] = "Mic %d" % ch
            for sub, vals in (("filter", {"hpf": 0.0}),
                              ("limit", {"limiteron": 0.0, "threshold": 1.0}),
                              ("opt", {"swapcompeq": 0.0, "eqmodel": 0.0,
                                       "compmodel": 0.0})):
                for k, v in vals.items():
                    self.values["%s%s/%s" % (base, sub, k)] = v

    def refresh(self):
        """Pull what the device can actually tell us into the shadow state."""
        if self.dev is None:
            return
        try:
            with self.usb_lock:
                rsp = self.dev.read_state()
            if rsp is None:
                return
            f = self.dev.floats(rsp)
        except Exception:
            return
        with self.lock:
            for route, (slot, norm) in READBACK.items():
                if slot < len(f):
                    try:
                        self.values[route] = max(0.0, min(1.0, float(norm(f[slot]))))
                    except Exception:
                        pass
            raw50 = struct.pack("<f", f[50]) if len(f) > 50 else b"\0\0\0\0"
            self.values["line/ch1/48v"] = 1.0 if raw50[0] else 0.0
            self.values["line/ch2/48v"] = 1.0 if raw50[1] else 0.0

    def sync_tree(self):
        with self.lock:
            vals = dict(self.values)
            strs = dict(self.strings)
        tree = {"id": "Synchronize", "children": {},
                "shared": {"strings": [[""]]}}

        def put(path, key, value, kind):
            node = tree["children"]
            parts = path.split("/")
            for i, p in enumerate(parts):
                node = node.setdefault(p, {})
                if i < len(parts) - 1:
                    node = node.setdefault("children", {})
            node.setdefault(kind, {})[key] = value

        for route, v in vals.items():
            head, _, leaf = route.rpartition("/")
            put(head, leaf, float(v), "values")
        for route, s in strs.items():
            head, _, leaf = route.rpartition("/")
            put(head, leaf, s, "strings")
        return tree


# ---------------------------------------------------------------- the server
class Shim:
    def __init__(self, dev, device_id=107, serial="LINUXIO24001", port=0,
                 dry_run=False):
        self.dev = dev
        self.device_id = device_id
        self.serial = serial
        self.dry_run = dry_run
        # The native protocol is STRICTLY SYNCHRONOUS: one command outstanding at
        # a time. This shim has several threads (metering, state refresh, client
        # writes), so every single device access must go through this lock.
        # Without it the pipe stalls, reads come back empty, and libusb can crash.
        self.usb_lock = threading.Lock()
        self.state = State(dev, self.usb_lock)
        self.clients = []
        self.clients_lock = threading.Lock()
        # metering: client-advertised UDP endpoints from their UM welcome
        self.monitors = {}                  # (host, port) -> last seen
        self.monitors_lock = threading.Lock()
        self.hold = METERS.MeterHold() if METERS else None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.running = True

    # --- discovery ---
    def announce_loop(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        dg = discovery_datagram(self.port, self.device_id, self.serial)
        while self.running:
            try:
                udp.sendto(dg, DISCOVERY_ADDR)
            except Exception:
                pass
            time.sleep(1.0)

    def refresh_loop(self):
        while self.running:
            self.state.refresh()
            time.sleep(1.0)

    # --- metering ---
    def meter_loop(self, poll_hz=20.0, frame_hz=10.0):
        """Feed MeterHold from JaSt reads and emit alternating 'levl'/'redu'
        datagrams to every client that advertised a monitor port in its UM.

        Levels peak-hold and reduction min-holds between frames, matching the
        real service; values go on the wire as linear u16 (unity = 0xFFFF).
        """
        if METERS is None or self.dev is None:
            return
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        want_levels = True
        next_frame = time.monotonic()
        while self.running:
            try:
                with self.usb_lock:
                    rsp = self.dev.read_state(0x100)
                if rsp is not None:
                    self.hold.feed(METERS.JaSt(self.dev.floats(rsp)))
            except Exception:
                pass
            now = time.monotonic()
            if now >= next_frame:
                next_frame = now + 1.0 / frame_hz
                with self.monitors_lock:
                    targets = list(self.monitors)
                if targets:
                    try:
                        if want_levels:
                            dg = METERS.meter_datagram(
                                b"levl", METERS.levl_groups(self.hold.take_levels()),
                                src=self.device_id, dst=CLIENT_ID)
                        else:
                            dg = METERS.meter_datagram(
                                b"redu", METERS.redu_groups(self.hold.take_reduction()),
                                src=self.device_id, dst=CLIENT_ID)
                        want_levels = not want_levels
                        for t in targets:
                            udp.sendto(dg, t)
                    except Exception as e:
                        print("  meter frame failed: %s" % e)
            time.sleep(max(0.0, 1.0 / poll_hz))

    def register_monitor(self, host, port):
        if not port:
            return
        with self.monitors_lock:
            new = (host, port) not in self.monitors
            self.monitors[(host, port)] = time.time()
        if new:
            print("  metering -> %s:%d%s" % (host, port,
                  "" if METERS and self.dev else "  (unavailable)"))

    # --- per-client ---
    def serve(self, conn, addr):
        buf = b""
        me, peer = self.device_id, CLIENT_ID
        with self.clients_lock:
            self.clients.append(conn)
        try:
            while self.running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                msgs, buf = parse_frames(buf)
                for mtype, src, dst, payload in msgs:
                    self.handle(conn, mtype, payload, me, peer, addr[0])
        except Exception as e:
            print("  client %s: %s" % (addr, e))
        finally:
            with self.clients_lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def handle(self, conn, mtype, payload, me, peer, peer_host="127.0.0.1"):
        if mtype == "UM":
            port = struct.unpack_from("<H", payload, 0)[0] if len(payload) >= 2 else 0
            print("  UM welcome, client monitor port %d" % port)
            self.register_monitor(peer_host, port)
        elif mtype == "JM":
            n, = struct.unpack_from("<I", payload, 0)
            obj = json.loads(payload[4:4 + n].decode("ascii", "replace"))
            if obj.get("id") == "Subscribe":
                print("  Subscribe from %r" % obj.get("clientName"))
                conn.sendall(jm({"id": "SubscriptionReply",
                                 "clientName": obj.get("clientName", ""),
                                 "result": "OK"}, me, peer))
                self.state.refresh()
                tree = self.state.sync_tree()
                blob = jm(tree, me, peer)
                print("  Synchronize: %d bytes" % len(blob))
                conn.sendall(blob)
        elif mtype == "KA":
            pass                       # no reply expected
        elif mtype == "PV":
            route = payload[:-7].decode("ascii", "replace")
            value, = struct.unpack_from("<f", payload, len(payload) - 4)
            self.apply(route, value)
            conn.sendall(pv(route, self.state.values.get(route, value), me, peer))
        elif mtype == "PS":
            body = payload.decode("ascii", "replace").split("\x00")
            route, value = body[0], (body[3] if len(body) > 3 else "")
            self.state.strings[route] = value
            conn.sendall(ps(route, value, me, peer))
        else:
            print("  unhandled message type %r" % mtype)

    def apply(self, route, value):
        leaf = route.rsplit("/", 1)[-1]
        self.state.values[route] = value
        if leaf in BLOCKED:
            print("  BLOCKED %s (host binds block 0 — would hit main volume)" % route)
            return
        hit = route_lookup(route)
        if hit is None:
            return                      # shadow-only or inert route
        blob, wid, index, enc = hit
        try:
            encoded = enc(value)
        except Exception:
            return
        print("  %s = %.4f -> %s id=%d index=%d value=%s"
              % (route, value, blob, wid, index, encoded))
        if self.dry_run or self.dev is None:
            return
        with self.usb_lock:
            # Use the public setter for the one write-only global the Linux
            # Host shadows.  Generic set_param would reach the same bytes but
            # would disappear from Host JSON persistence and GTK adoption.
            if route == "global/phonesSrc":
                self.dev.set_phones_source(encoded)
            else:
                self.dev.set_param(wid, encoded, index=index,
                                   as_int=(blob == "Pari"))

    def run(self):
        threading.Thread(target=self.announce_loop, daemon=True).start()
        threading.Thread(target=self.refresh_loop, daemon=True).start()
        threading.Thread(target=self.meter_loop, daemon=True).start()
        print("ucnet shim on 127.0.0.1:%d  deviceId=%d serial=%s%s"
              % (self.port, self.device_id, self.serial,
                 "  [DRY RUN]" if self.dry_run else ""))
        print("announcing to %s:%d once a second" % DISCOVERY_ADDR)
        try:
            while self.running:
                conn, addr = self.sock.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print("client connected: %s" % (addr,))
                threading.Thread(target=self.serve, args=(conn, addr),
                                 daemon=True).start()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.sock.close()


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    def opt(name, default, cast=str):
        return cast(argv[argv.index(name) + 1]) if name in argv else default
    dry = "--dry-run" in argv
    dev = None
    if not dry:
        if Io24 is None:
            sys.exit("io24 driver unavailable; use --dry-run")
        dev = Io24()
    shim = Shim(dev, device_id=opt("--device-id", 107, int),
                serial=opt("--serial", "LINUXIO24001"),
                port=opt("--port", 0, int), dry_run=dry)
    try:
        shim.run()
    finally:
        if dev is not None:
            dev.close()


if __name__ == "__main__":
    main()
