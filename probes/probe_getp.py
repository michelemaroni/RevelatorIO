#!/usr/bin/env python3
"""
THE test: native 'GetP' (READ-ONLY) using the reverse-engineered FourCC TLV
protocol, proven from both the host serializer (dspusbdevice.dll x86) and the
io24's own embedded Thumb-2 firmware dispatcher.

Request payload (goes after the 8-byte paesdk bulk header):
  0   u32 cmdTag      'GetP'=0x47657450 ('SetP'=0x53657450)
  4   u32 blockFourCC 'Appl'=0x4170706c
  8   u32 blockIndex
  12  BLOB: +0x00 u32 blobTag ('Para'=f32, 'Pari'=i32)
            +0x04 u32 blobSize (total incl 8-byte blob hdr; 0x14 for Para/Pari)
            +0x08 u32 index
            +0x0c u32 paramId
            +0x10 value (f32/i32)
  total = 12 + blobSize = 32 ; firmware rejects total <= 19
Response: 'Rply'=0x52706c79 + echoed selector + blob (value at 0x1c..0x1f)

FourCCs are C 'A'<<24|... constants stored little-endian => byte-reversed on wire.
SAFETY: only GetP (reads) here. Never sends 'FRst' (factory reset).
"""
import struct, time, sys
import usb.core, usb.util

VID, PIDS, IFACE, EP_OUT, EP_IN = 0x194F, (0x0422, 0x0424), 5, 0x01, 0x81

GETP = 0x47657450
SETP = 0x53657450
RPLY = 0x52706c79
APPL = 0x4170706c
PARA = 0x50617261
PARI = 0x50617269

def fourcc_bytes(v): return struct.pack("<I", v)
def tag_str(v):
    return struct.pack("<I", v).decode("ascii", "replace")

def build(cmd, block, blockIndex, blobTag, index, paramId, value=0.0, as_int=False):
    blob = struct.pack("<II", blobTag, 0x14)
    blob += struct.pack("<II", index, paramId)
    blob += struct.pack("<i", int(value)) if as_int else struct.pack("<f", float(value))
    return struct.pack("<III", cmd, block, blockIndex) + blob

def hexd(b, limit=64):
    b = bytes(b); s = b[:limit]
    return " ".join("%02x" % x for x in s) + (" +%d" % (len(b)-limit) if len(b) > limit else "")

class Dev:
    def __init__(s):
        s.dev = usb.core.find(idVendor=VID,
                              custom_match=lambda d: d.idProduct in PIDS)
        if s.dev is None: sys.exit("io24 not found — plugged in?")
        usb.util.claim_interface(s.dev, IFACE)
        s.dev.set_interface_altsetting(interface=IFACE, alternate_setting=1)
        i = s.dev.get_active_configuration()[(IFACE, 1)]
        s.i = usb.util.find_descriptor(i, custom_match=lambda e: e.bEndpointAddress == EP_IN)
        s.o = usb.util.find_descriptor(i, custom_match=lambda e: e.bEndpointAddress == EP_OUT)
        s.uid = 0
        try: s.dev.clear_halt(EP_OUT); s.dev.clear_halt(EP_IN)
        except Exception: pass
        # channel start (read-only control queries)
        s.dev.ctrl_transfer(0xC1, 0, 0, IFACE, 2)
        s.dev.ctrl_transfer(0xC1, 1, 0, IFACE, 4)
        s.dev.ctrl_transfer(0xC1, 1, 1, IFACE, 4)
    def _u(s):
        s.uid = (s.uid + 1) & 0xff
        return s.uid if s.uid else s._u()
    def cmd(s, payload, wait=1.0):
        u = s._u()
        w = struct.pack("<H", len(payload) + 8) + bytes([1, 1, u, 0, 0, 0]) + payload
        try:
            s.o.write(w, timeout=1500)
        except usb.core.USBError as e:
            try: s.dev.clear_halt(EP_OUT)
            except Exception: pass
            return None, "OUT error %s" % e
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try: d = s.i.read(2048, timeout=400)
            except usb.core.USBTimeoutError: continue
            except usb.core.USBError as e:
                try: s.dev.clear_halt(EP_IN)
                except Exception: pass
                return None, "IN error %s" % e
            if not d: continue
            d = bytes(d)
            pl = d[8:] if len(d) > 8 else b""
            if pl: return pl, None
            # empty ack: keep waiting briefly for the real reply
        return b"", None

def main():
    dv = Dev()
    print("channel started. Sending READ-ONLY GetP probes.\n")
    # firmware-side param ids seen in the io24 image (input1Gain=13, hpVolume=10)
    probes = [
        ("Appl/Para paramId=13 (input1Gain)", build(GETP, APPL, 0, PARA, 0, 13)),
        ("Appl/Para paramId=10 (hpVolume)",   build(GETP, APPL, 0, PARA, 0, 10)),
        ("Appl/Pari paramId=0  (int probe)",  build(GETP, APPL, 0, PARI, 0, 0, 0, as_int=True)),
    ]
    hit = False
    for name, payload in probes:
        print("== %s ==" % name)
        print("   TX [%d] %s" % (len(payload), hexd(payload)))
        pl, err = dv.cmd(payload, wait=1.2)
        if err:
            print("   %s" % err); continue
        if not pl:
            print("   (empty ack only — no reply blob)"); continue
        print("   RX [%d] %s" % (len(pl), hexd(pl)))
        if len(pl) >= 4:
            tag, = struct.unpack_from("<I", pl, 0)
            print("   reply tag = 0x%08x %r" % (tag, tag_str(tag)))
            if tag == RPLY:
                hit = True
                print("   *** 'Rply' — PROTOCOL CONFIRMED ***")
                if len(pl) >= 0x20:
                    f, = struct.unpack_from("<f", pl, 0x1c)
                    i, = struct.unpack_from("<i", pl, 0x1c)
                    print("   value @0x1c: float=%r  int=%d" % (f, i))
        print()
    usb.util.release_interface(dv.dev, IFACE)
    print("=== %s ===" % ("NATIVE PROTOCOL WORKING" if hit else "no 'Rply' yet"))

if __name__ == "__main__":
    main()
