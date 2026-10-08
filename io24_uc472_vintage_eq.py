#!/usr/bin/env python3
"""Run UC 4.7.2's own Vintage EQ designer for the Linux Host.

PROTOCOL.md §12N recorded the Vintage model as a Neve 1073 whose switch
frequencies were "not recovered", and the Host had been applying 1073 values as
a labelled guess. The retained investigation recovered the switch table and
the designer with it:

    0x18001c140   the recompute; loads the frequency index and calls
    0x18001d470   the band designer            <- what this module executes
    0x18001b270   helper, no calls
    0x18001b110   helper, no calls
    0x180010610   the s-plane to z-plane stage, via powf and tanf

The designer is not a shelf formula. It is an analog model: the switch
positions are capacitors (2e-7, 1e-7, 4.7e-8, 1.5e-8 F - 200, 100, 47 and 15
nF, a 1073's LF switch), combined with resistor-scale constants into s-domain
polynomials whose corner and Q move with the gain. Transcribing that by hand
would be hundreds of floating-point steps with no way to notice a slip, so this
module interprets the vendor's instructions directly: same inputs, same
arithmetic, same float32 rounding, by construction.

It reads the retained DLL and computes. It never loads or executes vendor code
as code, and touches no device.
"""

import argparse
import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

from capstone import Cs, CS_ARCH_X86, CS_MODE_64

DSP_SHA256 = "de685e89c88b191b9709145056aa37d58a76f141a53a9c1533dff917f611fd28"
DSP_SIZE = 4_979_672
IMAGE_BASE = 0x180000000

DESIGNER = 0x18001D470
MATH_THUNKS = {0x1800D355C: "powf", 0x1800D356E: "tanf", 0x1800D3556: "pow"}

# The four band designers the recompute at 0x18001c140 calls, in the order they
# write one shared buffer at `+0xc4`. The widths are what the designers were
# observed to write, not an assumption: the low band is a third-order section
# of seven coefficients, the other three are ordinary five-float biquads.
#
#     entry, the index field the recompute reads, the register carrying the
#     smoothed gain, the band's offset in the buffer, and its coefficient count
BAND_DESIGNERS = {
    "low":     (0x18001D470, 0x60, "xmm2", 0x00, 7),
    "high":    (0x18001CF30, None, "xmm1", 0x1C, 5),
    "hi-mid":  (0x18001CC20, 0x88, "xmm2", 0x30, 5),
    "low-mid": (0x18001C910, 0x9C, "xmm2", 0x44, 5),
}

# 7 + 5 + 5 + 5. A stored `eq  ` unit holds exactly this many floats per sample
# rate - three five-float bands plus one seven-float band - because the record
# is a copy of this buffer, not a conversion of it.
BAND_COEFFICIENT_TOTAL = 22
NATIVE_RATES = (44100.0, 48000.0, 88200.0, 96000.0)
VINTAGE_CLASS_ID = "{E1C5E024-C5CD-473C-B08A-6EC177812E01}"

# The switch positions, read from the label lists in .data (see the run record).
LOW_FREQUENCIES = (35.0, 60.0, 110.0, 220.0)
LOW_MID_FREQUENCIES = (360.0, 700.0, 1600.0)
HI_MID_FREQUENCIES = (3200.0, 4800.0, 7200.0)


class AnalysisError(ValueError):
    """The binary is not the pinned one, or the trace left the known path."""


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


class Image:
    """The retained DLL, addressed the way the loader would map it."""

    def __init__(self, path):
        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != DSP_SIZE or digest != DSP_SHA256:
            raise AnalysisError(
                "not the pinned UC 4.7.2 dspusbdevice.dll (size %d, sha %s)"
                % (len(raw), digest[:16]))
        self.raw = raw
        pe = struct.unpack_from("<I", raw, 0x3C)[0]
        count = struct.unpack_from("<H", raw, pe + 6)[0]
        opt = struct.unpack_from("<H", raw, pe + 20)[0]
        self.sections = []
        for index in range(count):
            at = pe + 24 + opt + index * 40
            _name = raw[at:at + 8].rstrip(b"\0").decode()
            vsize, va, rsize, rawptr = struct.unpack_from("<IIII", raw, at + 8)
            self.sections.append((va, max(vsize, rsize), rawptr, rsize))

    def offset(self, va):
        rva = va - IMAGE_BASE
        for va_start, size, rawptr, rsize in self.sections:
            if va_start <= rva < va_start + size:
                at = rawptr + (rva - va_start)
                return at if at < len(self.raw) else None
        return None

    def read(self, va, size):
        at = self.offset(va)
        if at is None:
            return b"\0" * size
        return self.raw[at:at + size]


class Machine:
    """Enough of x86-64 and SSE to run these routines and nothing more."""

    def __init__(self, image, trace=False):
        self.image = image
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)
        self.md.detail = True
        self.trace = trace
        self.regs = {name: 0 for name in (
            "rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
            "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")}
        self.xmm = {"xmm%d" % i: [0.0, 0.0, 0.0, 0.0] for i in range(16)}
        self.mem = {}
        self.flags = {}
        self.steps = 0

    # -- memory ----------------------------------------------------------
    def load(self, address, size):
        """Written bytes win; anything untouched reads from the image.

        Merging byte by byte matters: a 64-bit read over a 32-bit store has to
        see the stored half, not fall back to the image for the whole range.
        """
        backing = self.image.read(address, size) or (b"\0" * size)
        return bytes(self.mem.get(address + i, backing[i]) for i in range(size))

    def store(self, address, payload):
        for index, byte in enumerate(payload):
            self.mem[address + index] = byte

    def load_f32(self, address):
        return struct.unpack("<f", self.load(address, 4))[0]

    def store_f32(self, address, value):
        self.store(address, struct.pack("<f", _f32(value)))

    def load_u64(self, address):
        return struct.unpack("<Q", self.load(address, 8))[0]

    # -- operands --------------------------------------------------------
    def _reg(self, name):
        if name in self.regs:
            return self.regs[name]
        if name in self.xmm:
            return self.xmm[name]
        wide = "r" + name[1:]
        if name.startswith("e") and wide in self.regs:
            return self.regs[wide] & 0xFFFFFFFF
        if name.endswith("d") and name[:-1] in self.regs:
            return self.regs[name[:-1]] & 0xFFFFFFFF
        if name in ("sil", "al", "bl", "cl", "dl"):
            base = {"sil": "rsi", "al": "rax", "bl": "rbx",
                    "cl": "rcx", "dl": "rdx"}[name]
            return self.regs[base] & 0xFF
        raise AnalysisError("register %r is outside the modelled set" % name)

    def _set_reg(self, name, value):
        if name in self.regs:
            self.regs[name] = value & 0xFFFFFFFFFFFFFFFF
            return
        if name in self.xmm:
            self.xmm[name] = value
            return
        wide = "r" + name[1:]
        if name.startswith("e") and wide in self.regs:
            self.regs[wide] = value & 0xFFFFFFFF        # 32-bit writes zero-extend
            return
        if name.endswith("d") and name[:-1] in self.regs:
            self.regs[name[:-1]] = value & 0xFFFFFFFF
            return
        if name in ("sil", "al", "bl", "cl", "dl"):
            base = {"sil": "rsi", "al": "rax", "bl": "rbx",
                    "cl": "rcx", "dl": "rdx"}[name]
            self.regs[base] = (self.regs[base] & ~0xFF) | (value & 0xFF)
            return
        raise AnalysisError("register %r is outside the modelled set" % name)

    def _address(self, insn, op):
        mem = op.mem
        address = mem.disp
        if mem.base:
            name = insn.reg_name(mem.base)
            address += self.regs["rip"] if name == "rip" else self._reg(name)
        if mem.index:
            address += self._reg(insn.reg_name(mem.index)) * mem.scale
        return address & 0xFFFFFFFFFFFFFFFF

    def read_op(self, insn, op, size=4):
        from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM
        if op.type == X86_OP_REG:
            return self._reg(insn.reg_name(op.reg))
        if op.type == X86_OP_IMM:
            return op.imm
        if op.type == X86_OP_MEM:
            return self.load(self._address(insn, op), size)
        raise AnalysisError("operand type %r" % op.type)

    def read_f32(self, insn, op):
        from capstone.x86 import X86_OP_REG
        if op.type == X86_OP_REG:
            return self._reg(insn.reg_name(op.reg))[0]
        return struct.unpack("<f", self.read_op(insn, op, 4))[0]

    def write_f32(self, insn, op, value):
        from capstone.x86 import X86_OP_REG
        value = _f32(value)
        if op.type == X86_OP_REG:
            name = insn.reg_name(op.reg)
            lanes = list(self.xmm[name])
            lanes[0] = value
            self.xmm[name] = lanes
        else:
            self.store_f32(self._address(insn, op), value)

    def read_xmm(self, insn, op):
        from capstone.x86 import X86_OP_REG
        if op.type == X86_OP_REG:
            return list(self._reg(insn.reg_name(op.reg)))
        raw = self.read_op(insn, op, 16)
        return list(struct.unpack("<4f", raw))

    def write_xmm(self, insn, op, lanes):
        from capstone.x86 import X86_OP_REG
        lanes = [_f32(v) for v in lanes]
        if op.type == X86_OP_REG:
            self.xmm[insn.reg_name(op.reg)] = lanes
        else:
            self.store(self._address(insn, op), struct.pack("<4f", *lanes))

    # -- execution -------------------------------------------------------
    STACK_TOP = 0x7FF000000000

    def _decode(self, va):
        code = self.image.read(va, 16)
        for insn in self.md.disasm(code, va):
            return insn
        raise AnalysisError("cannot decode at %#x" % va)

    def _math_thunk(self, target):
        name = MATH_THUNKS[target]
        if name == "powf":
            value = math.pow(self.xmm["xmm0"][0], self.xmm["xmm1"][0])
        elif name == "tanf":
            value = math.tan(self.xmm["xmm0"][0])
        else:                                   # pow, double precision
            value = math.pow(self.xmm["xmm0"][0], self.xmm["xmm1"][0])
        self.xmm["xmm0"] = [_f32(value), 0.0, 0.0, 0.0]

    def call(self, entry, limit=400000):
        """Run one routine to its `ret`, with a return marker on the stack."""
        marker = 0xDEADBEEF00000000
        self.regs["rsp"] -= 8
        self.store(self.regs["rsp"], struct.pack("<Q", marker))
        self.regs["rip"] = entry
        while True:
            self.steps += 1
            if self.steps > limit:
                raise AnalysisError("step limit at %#x" % self.regs["rip"])
            insn = self._decode(self.regs["rip"])
            self.regs["rip"] = insn.address + insn.size
            if self.step(insn) == "ret":
                returned = struct.unpack("<Q", self.load(self.regs["rsp"], 8))[0]
                self.regs["rsp"] += 8
                if returned == marker:
                    return
                self.regs["rip"] = returned

    def step(self, insn):
        from capstone.x86 import X86_OP_REG, X86_OP_MEM
        name, ops = insn.mnemonic, insn.operands
        if self.trace:
            print("%08x  %-9s %s" % (insn.address, name, insn.op_str))

        if name in ("nop", "int3"):
            return None
        if name == "ret":
            return "ret"
        if name == "call":
            target = ops[0].imm if ops[0].type != X86_OP_REG else \
                self._reg(insn.reg_name(ops[0].reg))
            if target in MATH_THUNKS:
                self._math_thunk(target)
                return None
            self.regs["rsp"] -= 8
            self.store(self.regs["rsp"], struct.pack("<Q", self.regs["rip"]))
            self.regs["rip"] = target
            return None
        if name == "jmp":
            self.regs["rip"] = ops[0].imm if ops[0].type != X86_OP_REG else \
                self._reg(insn.reg_name(ops[0].reg))
            return None
        if name.startswith("j"):
            if self._branch(name):
                self.regs["rip"] = ops[0].imm
            return None

        if name in ("movss", "movsd"):
            self.write_f32(insn, ops[0], self.read_f32(insn, ops[1]))
        elif name in ("movaps", "movups", "movdqa", "movdqu"):
            self.write_xmm(insn, ops[0], self.read_xmm(insn, ops[1]))
        elif name == "movd":
            if ops[0].type == X86_OP_REG and insn.reg_name(ops[0].reg) in self.xmm:
                bits = self.read_op(insn, ops[1], 4)
                bits = struct.pack("<I", bits & 0xFFFFFFFF) if isinstance(bits, int) else bits
                self.xmm[insn.reg_name(ops[0].reg)] = [
                    struct.unpack("<f", bits)[0], 0.0, 0.0, 0.0]
            else:
                lanes = self.read_xmm(insn, ops[1])
                value = struct.unpack("<I", struct.pack("<f", lanes[0]))[0]
                self._set_reg(insn.reg_name(ops[0].reg), value)
        elif name in ("mulss", "addss", "subss", "divss", "maxss", "minss"):
            left = self.read_f32(insn, ops[0])
            right = self.read_f32(insn, ops[1])
            result = {"mulss": lambda: left * right,
                      "addss": lambda: left + right,
                      "subss": lambda: left - right,
                      "divss": lambda: left / right if right else math.inf,
                      "maxss": lambda: right if right > left else left,
                      "minss": lambda: right if right < left else left}[name]()
            self.write_f32(insn, ops[0], result)
        elif name == "xorps":
            same = (ops[0].type == X86_OP_REG and ops[1].type == X86_OP_REG and
                    ops[0].reg == ops[1].reg)
            if same:
                self.write_xmm(insn, ops[0], [0.0] * 4)
            else:
                a = self.read_xmm(insn, ops[0]); b = self.read_xmm(insn, ops[1])
                out = []
                for x, y in zip(a, b):
                    xi, = struct.unpack("<I", struct.pack("<f", x))
                    yi, = struct.unpack("<I", struct.pack("<f", y))
                    out.append(struct.unpack("<f", struct.pack("<I", xi ^ yi))[0])
                self.write_xmm(insn, ops[0], out)
        elif name == "andps":
            a = self.read_xmm(insn, ops[0]); b = self.read_xmm(insn, ops[1])
            out = []
            for x, y in zip(a, b):
                xi, = struct.unpack("<I", struct.pack("<f", x))
                yi, = struct.unpack("<I", struct.pack("<f", y))
                out.append(struct.unpack("<f", struct.pack("<I", xi & yi))[0])
            self.write_xmm(insn, ops[0], out)
        elif name in ("comiss", "ucomiss"):
            left = self.read_f32(insn, ops[0]); right = self.read_f32(insn, ops[1])
            self.flags = {"zf": left == right, "cf": left < right,
                          "pf": left != left or right != right}
        elif name in ("cvtps2pd", "cvtpd2ps", "cvtss2sd", "cvtsd2ss"):
            self.write_f32(insn, ops[0], self.read_f32(insn, ops[1]))
        elif name == "cvtdq2ps":
            lanes = self.read_xmm(insn, ops[1])
            bits = struct.unpack("<i", struct.pack("<f", lanes[0]))[0]
            self.write_f32(insn, ops[0], float(bits))
        elif name == "cvttss2si":
            self._set_reg(insn.reg_name(ops[0].reg),
                          int(self.read_f32(insn, ops[1])))
        elif name in ("cvtsi2ss", "cvtsi2sd"):
            self.write_f32(insn, ops[0], float(self.read_op(insn, ops[1], 4)))
        elif name == "lea":
            self._set_reg(insn.reg_name(ops[0].reg), self._address(insn, ops[1]))
        elif name == "mov":
            size = ops[0].size if ops[0].type == X86_OP_MEM else 8
            value = self.read_op(insn, ops[1], size)
            if isinstance(value, bytes):
                value = int.from_bytes(value, "little")
            if ops[0].type == X86_OP_MEM:
                self.store(self._address(insn, ops[0]),
                           (value & ((1 << (size * 8)) - 1)).to_bytes(size, "little"))
            else:
                self._set_reg(insn.reg_name(ops[0].reg), value)
        elif name == "movsxd":
            value = self.read_op(insn, ops[1], 4)
            if isinstance(value, bytes):
                value = int.from_bytes(value, "little")
            value &= 0xFFFFFFFF
            if value >> 31:
                value -= 1 << 32
            self._set_reg(insn.reg_name(ops[0].reg), value)
        elif name == "push":
            self.regs["rsp"] -= 8
            self.store(self.regs["rsp"],
                       struct.pack("<Q", self.read_op(insn, ops[0], 8) & (2**64 - 1)))
        elif name == "pop":
            self._set_reg(insn.reg_name(ops[0].reg),
                          struct.unpack("<Q", self.load(self.regs["rsp"], 8))[0])
            self.regs["rsp"] += 8
        elif name in ("sub", "add", "inc", "dec"):
            target = insn.reg_name(ops[0].reg)
            left = self._reg(target)
            right = 1 if name in ("inc", "dec") else self.read_op(insn, ops[1], 8)
            value = left + right if name in ("add", "inc") else left - right
            self._set_reg(target, value)
            self.flags = {"zf": (value & 0xFFFFFFFFFFFFFFFF) == 0,
                          "cf": value < 0, "sf": value < 0}
        elif name == "cmp":
            left = self.read_op(insn, ops[0], 4)
            right = self.read_op(insn, ops[1], 4)
            if isinstance(left, bytes):
                left = int.from_bytes(left, "little")
            if isinstance(right, bytes):
                right = int.from_bytes(right, "little")
            value = left - right
            self.flags = {"zf": value == 0, "cf": left < right, "sf": value < 0}
        elif name == "test":
            left = self.read_op(insn, ops[0], 4)
            right = self.read_op(insn, ops[1], 4)
            if isinstance(left, bytes):
                left = int.from_bytes(left, "little")
            if isinstance(right, bytes):
                right = int.from_bytes(right, "little")
            self.flags = {"zf": (left & right) == 0, "cf": False,
                          "sf": ((left & right) >> 31) & 1 == 1}
        elif name in ("setae", "sete", "setne"):
            wanted = {"setae": not self.flags.get("cf"),
                      "sete": self.flags.get("zf"),
                      "setne": not self.flags.get("zf")}[name]
            self._set_reg(insn.reg_name(ops[0].reg), 1 if wanted else 0)
        elif name == "cdq":
            pass
        else:
            raise AnalysisError("instruction %r at %#x is not modelled"
                                % (name, insn.address))
        return None

    def _branch(self, name):
        zf, cf = self.flags.get("zf", False), self.flags.get("cf", False)
        sf = self.flags.get("sf", False)
        return {"je": zf, "jz": zf, "jne": not zf, "jnz": not zf,
                "jae": not cf, "jnb": not cf, "jb": cf, "jc": cf,
                "ja": not cf and not zf, "jbe": cf or zf,
                "jl": sf, "jge": not sf, "jg": not sf and not zf,
                "jle": sf or zf, "js": sf, "jns": not sf}[name]


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DLL = Path(os.environ.get(
    "IO24_UC472_DSPUSBDEVICE",
    Path.home() / ".cache" / "io24" / "re" / "dspusbdevice.dll",
))
BUFFER = 0x10000000


def design(image, band, index, gain_db, rate_hz, trace=False):
    """Run the vendor's designer for one band and return what it wrote.

    The call is set up exactly as the recompute does it: the band-state buffer
    in rcx, the switch index in edx, the smoothed gain in the register that
    band's call site uses, and -1 as the fifth argument, which is what makes
    the designer take the sample rate from the buffer rather than the caller.

    `written` is the byte range the designer actually touched, so a caller can
    see the coefficient count was observed rather than assumed.
    """
    try:
        entry, _, gain_register, at, count = BAND_DESIGNERS[band]
    except KeyError:
        raise AnalysisError("unknown band %r" % (band,))
    machine = Machine(image, trace=trace)
    machine.regs["rsp"] = Machine.STACK_TOP - 0x400
    machine.store_f32(BUFFER + 0x58, float(rate_hz))
    machine.store(machine.regs["rsp"] + 0x20, struct.pack("<f", -1.0))
    machine.regs["rcx"] = BUFFER
    machine.regs["rdx"] = int(index)
    machine.xmm[gain_register] = [_f32(gain_db), 0.0, 0.0, 0.0]
    machine.xmm["xmm3"] = [-1.0, 0.0, 0.0, 0.0]
    before = set(machine.mem)
    machine.call(entry)
    touched = sorted(address - BUFFER for address in machine.mem
                     if address not in before
                     and BUFFER <= address < BUFFER + 0x200)
    coefficients = [machine.load_f32(BUFFER + at + 4 * i) for i in range(count)]
    return {"band": band, "index": int(index), "gain_db": _f32(gain_db),
            "rate_hz": float(rate_hz), "coefficients": coefficients,
            "written": (touched[0], touched[-1]) if touched else None,
            "steps": machine.steps}


def design_band(image, index, gain_db, rate_hz, trace=False):
    """The low band, which is the one whose table PROTOCOL.md was missing."""
    return design(image, "low", index, gain_db, rate_hz, trace)


def _preset_number(eq, field, low, high, integer=False):
    try:
        value = float(eq[field])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise AnalysisError("Vintage EQ %s must be a finite number" % field)
    if not math.isfinite(value) or not low <= value <= high:
        raise AnalysisError(
            "Vintage EQ %s must be in [%g, %g]" % (field, low, high))
    if integer and not value.is_integer():
        raise AnalysisError("Vintage EQ %s must be an integer switch index" % field)
    return int(value) if integer else value


def design_eq(image, eq):
    """Design one tagged Vintage EQ in the native stored-band order.

    UC's four routines write ``low, high, hi-mid, low-mid`` into one work
    buffer. Firmware stores the same 22 floats as three five-float sections in
    ascending-frequency order plus the seven-float section: ``low-mid,
    hi-mid, high, wide``. For Vintage, ``wide`` is the low shelf.
    """
    if not isinstance(eq, dict) or eq.get("__classid") != VINTAGE_CLASS_ID:
        raise AnalysisError("a complete Vintage EQ record is required")
    on = _preset_number(eq, "eqallon", 0, 1, integer=True)
    low_index = _preset_number(eq, "lowfreq", 0, 3, integer=True)
    low_mid_index = _preset_number(eq, "lowmidfreq", 0, 2, integer=True)
    hi_mid_index = _preset_number(eq, "himidfreq", 0, 2, integer=True)
    gains = {
        "low": _preset_number(eq, "lowgain", -16, 16),
        "low-mid": _preset_number(eq, "lowmidgain", -16, 16),
        "hi-mid": _preset_number(eq, "himidgain", -16, 16),
        "high": _preset_number(eq, "higain", -16, 16),
    }
    if not on:
        return {
            rate: {
                "low-mid": (1.0, 0.0, 0.0, 0.0, 0.0),
                "hi-mid": (1.0, 0.0, 0.0, 0.0, 0.0),
                "high": (1.0, 0.0, 0.0, 0.0, 0.0),
                "wide": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            }
            for rate in NATIVE_RATES
        }

    result = {}
    for rate in NATIVE_RATES:
        result[rate] = {
            "low-mid": tuple(design(
                image, "low-mid", low_mid_index, gains["low-mid"], rate
            )["coefficients"]),
            "hi-mid": tuple(design(
                image, "hi-mid", hi_mid_index, gains["hi-mid"], rate
            )["coefficients"]),
            "high": tuple(design(
                image, "high", 0, gains["high"], rate
            )["coefficients"]),
            "wide": tuple(design(
                image, "low", low_index, gains["low"], rate
            )["coefficients"]),
        }
    return result


def build_eq_component(image, base_component, eq):
    """Overlay an exact Vintage design on one complete native ``eq  `` body."""
    import io24_native_strip

    component = io24_native_strip.ComponentState(b"eq  ", base_component)
    component.set_eq_coefficients(design_eq(image, eq))
    return component.encode()


def build_native_stat_record(image, base_record, slot_index, eq):
    """Build a complete native record with one exact Vintage EQ replacement."""
    import io24_native_stat

    record = io24_native_stat.decode_native_stat_record(
        io24_native_stat.validate_native_stat_record(
            base_record, slot_index=slot_index))
    top = {chunk.key: chunk.payload for chunk in record.chunks}
    channel = {chunk.key: chunk.payload for chunk in
               io24_native_stat.decode_chunk_group(top[b"opt "])}
    rebuilt_eq = build_eq_component(image, channel[b"eq  "], eq)
    return io24_native_stat.replace_native_channel_component(
        base_record, slot_index, b"eq  ", rebuilt_eq)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dll", default=DEFAULT_DLL)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--gain", type=float, default=0.0)
    parser.add_argument("--rate", type=float, default=48000.0)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    dll_path = Path(args.dll)
    if args.dll == DEFAULT_DLL:
        dll_path = PROJECT_ROOT / dll_path
    image = Image(dll_path)
    result = design_band(image, args.index, args.gain, args.rate, args.trace)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print("index %d  gain %+.2f dB  rate %g" %
              (result["index"], result["gain_db"], result["rate_hz"]))
        print("  coefficients:", ["%.8g" % c for c in result["coefficients"]])
        print("  steps:", result["steps"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
