#!/usr/bin/env python3
"""Run UC 4.7.2's own Passive Program EQ designers for the Linux Host.

The Passive class is the Pultec-style model registered by UC's pinned
``dspusbdevice.dll``.  Its recompute at ``0x180019f50`` calls two coefficient
designers:

* ``0x18001a610`` writes a seven-float combined high boost/attenuation section
  and sends it as ``Lfdf`` index 0;
* ``0x18001adb0`` writes a five-float combined low boost/attenuation section
  and sends it as ``Bqdf`` index 1.

Indexes 2 and 3 are identity biquads.  That tiles the native stored EQ body as
one five-float main section, two identity main sections, and the seven-float
wide section.  The routines are interpreted straight from the pinned binary
through ``uc472_vintage_eq.Machine``; vendor code is read as data and is never
loaded or executed by the operating system.  This module touches no device.
"""

import argparse
import json
import math
import os
import struct
import sys
from pathlib import Path
import io24_uc472_vintage_eq as _executor
AnalysisError = _executor.AnalysisError
Image = _executor.Image
Machine = _executor.Machine
_f32 = _executor._f32

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DLL = Path(os.environ.get(
    "IO24_UC472_DSPUSBDEVICE",
    Path.home() / ".cache" / "io24" / "re" / "dspusbdevice.dll",
))
BUFFER = _executor.BUFFER
NATIVE_RATES = _executor.NATIVE_RATES

PASSIVE_CLASS_ID = "{C0730CBB-5135-4558-9222-C40BDBA036ED}"
HIGH_DESIGNER = 0x18001A610
LOW_DESIGNER = 0x18001ADB0
HIGH_COUNT = 7
LOW_COUNT = 5
LOW_AT = 0x1C

# The actual UC enum order.  The first low label is literally ``20kHz`` in the
# pinned binary because the string is shared with the high-attenuation list;
# its circuit branch and the physical EQP-1A control make it the 20 Hz setting.
LOW_FREQUENCIES = (20.0, 30.0, 60.0, 100.0)
HIGH_BOOST_FREQUENCIES = (3000.0, 4000.0, 5000.0, 8000.0,
                          10000.0, 12000.0, 16000.0)
HIGH_ATTENUATION_FREQUENCIES = (5000.0, 10000.0, 20000.0)

IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0)
WIDE_IDENTITY = IDENTITY + (0.0, 0.0)


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError("Passive EQ sample rate must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise AnalysisError("Passive EQ sample rate must be positive")
    return value


def _run(image, entry, rate_hz, setup, output_at, count, trace=False):
    """Execute one designer and report its normalized output write range."""
    rate_hz = _rate(rate_hz)
    machine = Machine(image, trace=trace)
    machine.regs["rsp"] = Machine.STACK_TOP - 0x400
    machine.store_f32(BUFFER + 0x30, rate_hz)
    machine.regs["rcx"] = BUFFER
    setup(machine)
    before = set(machine.mem)
    machine.call(entry)
    touched = sorted(
        address - BUFFER for address in machine.mem
        if address not in before and BUFFER <= address < BUFFER + 0x200)
    expected = set(range(output_at, output_at + count * 4))
    if set(touched) != expected:
        raise AnalysisError(
            "Passive designer %#x wrote %r, expected %#x..%#x" %
            (entry,
             (touched[0], touched[-1]) if touched else None,
             output_at, output_at + count * 4 - 1))
    coefficients = [
        machine.load_f32(BUFFER + output_at + 4 * index)
        for index in range(count)
    ]
    return {
        "rate_hz": rate_hz,
        "coefficients": coefficients,
        "written": (0, count * 4 - 1),
        "steps": machine.steps,
    }


def design_low(image, frequency_index, boost, attenuation, rate_hz,
               trace=False):
    """Run the combined low boost/attenuation designer (five floats)."""
    index = _strict_integer(frequency_index, "bfreq", 0, 3)
    boost = _number(boost, "bboost", 0.0, 10.0)
    attenuation = _number(attenuation, "batten", 0.0, 10.0)

    def setup(machine):
        machine.regs["rdx"] = index
        machine.xmm["xmm2"] = [_f32(boost), 0.0, 0.0, 0.0]
        machine.xmm["xmm3"] = [_f32(attenuation), 0.0, 0.0, 0.0]
        machine.store_f32(machine.regs["rsp"] + 0x20, -1.0)

    result = _run(image, LOW_DESIGNER, rate_hz, setup, LOW_AT, LOW_COUNT,
                  trace=trace)
    result.update({"frequency_index": index, "boost": _f32(boost),
                   "attenuation": _f32(attenuation)})
    return result


def design_high(image, frequency_index, boost, attenuation,
                attenuation_frequency_index, bandwidth, rate_hz,
                trace=False):
    """Run the combined high boost/attenuation designer (seven floats)."""
    index = _strict_integer(frequency_index, "mfreq", 0, 6)
    boost = _number(boost, "mboost", 0.0, 10.0)
    attenuation = _number(attenuation, "hatten", 0.0, 10.0)
    attenuation_index = _strict_integer(
        attenuation_frequency_index, "hsfreq", 0, 2)
    bandwidth = _number(bandwidth, "bbwidth", 0.0, 10.0)

    def setup(machine):
        machine.regs["rdx"] = index
        machine.xmm["xmm2"] = [_f32(boost), 0.0, 0.0, 0.0]
        machine.xmm["xmm3"] = [_f32(attenuation), 0.0, 0.0, 0.0]
        machine.store(machine.regs["rsp"] + 0x20,
                      struct.pack("<I", attenuation_index))
        machine.store_f32(machine.regs["rsp"] + 0x28, bandwidth)
        machine.store_f32(machine.regs["rsp"] + 0x30, -1.0)

    result = _run(image, HIGH_DESIGNER, rate_hz, setup, 0, HIGH_COUNT,
                  trace=trace)
    result.update({"frequency_index": index, "boost": _f32(boost),
                   "attenuation": _f32(attenuation),
                   "attenuation_frequency_index": attenuation_index,
                   "bandwidth": _f32(bandwidth)})
    return result


def _number(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError("Passive EQ %s must be a finite number" % field)
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise AnalysisError(
            "Passive EQ %s must be in [%g, %g]" % (field, low, high))
    return value


def _strict_integer(value, field, low, high, allow_bool=False):
    if isinstance(value, bool):
        if not allow_bool:
            raise AnalysisError("Passive EQ %s must be an integer" % field)
        value = int(value)
    elif not isinstance(value, (int, float)):
        raise AnalysisError("Passive EQ %s must be an integer" % field)
    value = float(value)
    if not math.isfinite(value) or not value.is_integer() or not low <= value <= high:
        raise AnalysisError(
            "Passive EQ %s must be an integer in [%d, %d]" %
            (field, low, high))
    return int(value)


def _field(eq, name):
    try:
        return eq[name]
    except (KeyError, TypeError):
        raise AnalysisError("a complete Passive EQ record is required")


def design_eq(image, eq):
    """Design one tagged Passive EQ in native stored-band order."""
    if not isinstance(eq, dict) or eq.get("__classid") != PASSIVE_CLASS_ID:
        raise AnalysisError("a complete Passive EQ record is required")

    # Validate the complete semantic record before respecting the bypass.  A
    # disabled malformed preset must not become a latent unsafe write.
    on = _strict_integer(
        _field(eq, "eqallon"), "eqallon", 0, 1, allow_bool=True)
    bboost = _number(_field(eq, "bboost"), "bboost", 0.0, 10.0)
    batten = _number(_field(eq, "batten"), "batten", 0.0, 10.0)
    bfreq = _strict_integer(_field(eq, "bfreq"), "bfreq", 0, 3)
    mboost = _number(_field(eq, "mboost"), "mboost", 0.0, 10.0)
    bbwidth = _number(_field(eq, "bbwidth"), "bbwidth", 0.0, 10.0)
    mfreq = _strict_integer(_field(eq, "mfreq"), "mfreq", 0, 6)
    hatten = _number(_field(eq, "hatten"), "hatten", 0.0, 10.0)
    hsfreq = _strict_integer(_field(eq, "hsfreq"), "hsfreq", 0, 2)

    if not on:
        return {
            rate: {"low-mid": IDENTITY, "hi-mid": IDENTITY,
                   "high": IDENTITY, "wide": WIDE_IDENTITY}
            for rate in NATIVE_RATES
        }

    result = {}
    for rate in NATIVE_RATES:
        low = tuple(design_low(
            image, bfreq, bboost, batten, rate)["coefficients"])
        high = tuple(design_high(
            image, mfreq, mboost, hatten, hsfreq, bbwidth,
            rate)["coefficients"])
        result[rate] = {
            "low-mid": low,
            "hi-mid": IDENTITY,
            "high": IDENTITY,
            "wide": high,
        }
    return result


def build_eq_component(image, base_component, eq):
    """Overlay an exact Passive design on one complete native ``eq  `` body."""
    import io24_native_strip

    component = io24_native_strip.ComponentState(b"eq  ", base_component)
    component.set_eq_coefficients(design_eq(image, eq))
    return component.encode()


def build_native_stat_record(image, base_record, slot_index, eq):
    """Build a complete native record with one exact Passive EQ replacement."""
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
    parser.add_argument(
        "--presets", default=PROJECT_ROOT / "re/uc_factory_presets.json")
    parser.add_argument("--preset", default="Big Vocal")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    dll_path = Path(args.dll)
    if args.dll == DEFAULT_DLL:
        dll_path = PROJECT_ROOT / dll_path
    presets_path = Path(args.presets)
    if not presets_path.is_absolute():
        presets_path = PROJECT_ROOT / presets_path
    presets = json.loads(presets_path.read_text())
    try:
        eq = next(preset["eq"] for preset in presets
                  if preset.get("preset_name") == args.preset)
    except StopIteration:
        raise AnalysisError("preset %r was not found" % args.preset)
    designed = design_eq(Image(dll_path), eq)
    if args.json:
        print(json.dumps(designed, indent=1))
    else:
        for rate, bands in designed.items():
            print("%g Hz" % rate)
            for name, coefficients in bands.items():
                print("  %-7s %s" %
                      (name, " ".join("%.8g" % value
                                      for value in coefficients)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
