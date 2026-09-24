#!/usr/bin/env python3
"""
io24.py — Linux userspace driver core for the PreSonus Revelator io24.

Implements the reverse-engineered native control protocol:
  USB iface 5 alt 1, bulk EP 0x01/0x81
  8-byte paesdk header + FourCC TLV payload ('GetP'/'SetP' -> 'Rply')

This module is the transport layer the daemon will build on, plus a CLI used to
map the device's state blob to named parameters.

  io24.py status            decoded, named parameters
  io24.py meters [secs]     live input levels + chain gain reduction
  io24.py gain 1 40         ...and the other control commands (see --help)

Investigation helpers:
  io24.py dump              one JaSt state read, hex + float view
  io24.py stable [n]        n reads; classify offsets as STABLE vs VOLATILE(meters)
  io24.py snap <file>       save a state snapshot (every read kept, not averaged)
  io24.py diff <a> <b>      diff two snapshots, ignoring volatile offsets
  io24.py watch [secs]      report stable-slot changes live (control mapping)

SAFETY: this file never sends 'FRst' (factory reset). set_param() refuses to
write wire ids it has no mapping for, because doing that crashed the device into
its bootloader on 2026-08-02 — recovered by a cold power cycle, but the DFU
interface cannot hand the firmware back, so there is no safety net. Probes that
genuinely need an unmapped id must pass unsafe=True and be ready to replug.
"""
import cmath
import hashlib
import inspect
import json
import math
import os
import struct
import sys
import tempfile
import time

import usb.core
import usb.util

VID = 0x194F
# io24 and its twin the io44 share one control protocol and control interface.
PIDS = (0x0422, 0x0424)
IFACE, EP_OUT, EP_IN = 5, 0x01, 0x81

# FourCC constants (C 'A'<<24|... ; stored little-endian => byte-reversed on wire)
GETP = 0x47657450
SETP = 0x53657450
RPLY = 0x52706C79
APPL = 0x4170706C
JAST = 0x4A615374   # state read (GetP only)
MEMP = 0x4D656D50
PRSM = 0x5072734D   # user preset record nested inside MemP
STAT = 0x53746174   # device-slot record nested inside MemP
PARA = 0x50617261   # float write (SetP only)
PARI = 0x50617269   # int write   (SetP only)
# 'FRst' deliberately not defined — factory reset.

STATE_BLOB_SIZE = 0x7EC
DATA_START = 0x1C          # payload offset where the float array begins

# UC 4.7.2 submits the block-201 materialization frames back-to-back.  _exec()
# already serializes each USB request/reply; do not add a second host delay.
FX_FRAME_INTERVAL_S = 0.0
# Firmware block 201 replaces its selected delegate only after a nominal
# 40 ms bypass transition and a second VoFx selector visit.  Quiescing before
# an upward clock change uses a deliberately larger old-rate barrier before
# that synchronous replacement.  After its transport reply, two live quanta
# provide the separate audio-frame visibility barrier.  Ordinary UC control
# edits still use no invented inter-frame delay.
FX_MODEL_TRANSITION_SETTLE_S = 0.060


def voicefx_audio_settle_seconds(rate_hz, quantum_frames):
    """Two old-rate blocks after a control-plane model replacement reply."""
    try:
        rate = float(rate_hz)
    except (TypeError, ValueError) as error:
        raise ValueError("Voice FX sample rate must be numeric") from error
    if not math.isfinite(rate) or not 8000.0 <= rate <= 192000.0:
        raise ValueError("Voice FX sample rate must be 8000..192000 Hz")
    if type(quantum_frames) is not int or not 1 <= quantum_frames <= 16384:
        raise ValueError("Voice FX quantum must be 1..16384 frames")
    return 2.0 * quantum_frames / rate


def fourcc(s):
    """C constant 'A'<<24|'B'<<16|'C'<<8|'D' — reverse the ASCII, read LE.

    Getting this backwards silently addresses a nonexistent block and the reply
    looks identical to "this block has no data".
    """
    return struct.unpack("<I", s.encode("ascii")[::-1])[0]


def fourcc_str(v):
    return struct.pack("<I", v).decode("ascii", "replace")


def _build_memp_frames(record_tag, record_index, record):
    """Build UC's common fragmented ``SetP/Appl/MemP`` record envelope."""
    if not isinstance(record, (bytes, bytearray, memoryview)):
        raise TypeError("preset record must be bytes-like")

    record = bytes(record)
    if not record:
        raise ValueError("preset record must not be empty")
    if len(record) > 0xFFFF:
        raise ValueError("preset record exceeds the captured 16-bit length")

    frames = []
    fragment_size = 0x7CE
    for offset in range(0, len(record), fragment_size):
        fragment = record[offset:offset + fragment_size]
        frame = bytearray(12 + STATE_BLOB_SIZE)
        struct.pack_into("<IIIIII", frame, 0,
                         SETP, APPL, 0, MEMP, STATE_BLOB_SIZE, record_tag)
        struct.pack_into("<I", frame, 24, record_index)
        struct.pack_into("<H", frame, 28, 0)
        struct.pack_into("<I", frame, 30, offset)
        frame[34] = int(offset + len(fragment) < len(record))
        struct.pack_into("<H", frame, 35, len(fragment))
        struct.pack_into("<I", frame, 37, len(record))
        frame[41:41 + len(fragment)] = fragment
        frames.append(bytes(frame))
    return frames


def _build_captured_memp_frames(user_index, record):
    """Encode UC's user-library update (``MemP/PrsM``, indexes 16..27).

    This remains a hardware-free wire fixture.  ``PrsM`` names the separate
    host/library collection; it is not one of the four device button slots.
    """
    if isinstance(user_index, bool) or not isinstance(user_index, int) or \
            not 16 <= user_index <= 27:
        raise ValueError("user preset index must be in the range 16..27")
    return _build_memp_frames(PRSM, user_index, record)


def _build_device_library_memp_frames(user_index, preset_record):
    """Build UC's explicit Device Presets write from one complete record.

    Unlike the four ``Stat`` button-selection records, this is the twelve-entry
    ``PrsM`` library used by UC's Store action: indexes 16..21 are input 1 and
    22..27 are input 2.  This helper is pure and performs no device I/O.
    """
    from io24_preset_record import (complete_device_slot_record,
                                    encode_preset_record)
    record = complete_device_slot_record(preset_record)
    return _build_captured_memp_frames(
        user_index, encode_preset_record(record))


def _build_uc_slot_memp_frames(slot_index, record):
    """Wrap caller-supplied bytes as ``MemP/Stat`` indexes 0..3.

    The envelope is shared by multiple record representations, so this low-
    level helper intentionally makes no claim about ``record``. This function
    only returns bytes and never opens USB.
    """
    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or \
            not 0 <= slot_index <= 3:
        raise ValueError("device slot index must be in the range 0..3")
    return _build_memp_frames(STAT, slot_index, record)


def _build_uc_slot_json_frames(slot_index, slot_record):
    """Build UC's tagged settled-state ``MemP/Stat`` fixture offline.

    UC 4.7.2's 500 ms settled-state synchronizer starts this physical record at
    the tagged mapping's opening ``{`` byte. The explicit Store action instead
    uses the separate ``PrsM`` library route. Firmware 1.28 preserves a stored
    ``Stat`` body byte-for-byte on recall and its io24 state loader requires a
    native little-endian version word ``2``. The 2026-09-14 inactive-slot test
    consequently found no applied body from this tagged representation.

    Keep the exact fixture for protocol comparison, but do not use it as the
    io24 live slot writer. Require a complete record so even offline callers
    cannot accidentally construct a misleading effect-only body.
    """
    from io24_preset_record import (complete_device_slot_record,
                                    encode_preset_record)
    record = complete_device_slot_record(slot_record)
    return _build_uc_slot_memp_frames(
        slot_index, encode_preset_record(record))


def _build_native_slot_memp_frames(slot_index, native_record):
    """Frame one validated firmware-native version-2 ``Stat`` record.

    Firmware 1.28's ``MemP/Stat`` ingress stores these bytes unchanged and its
    recall consumer requires this representation. Building frames performs no
    I/O and does not establish acceptance, persistence, or audibility.
    """
    from io24_native_stat import validate_native_stat_record
    record = validate_native_stat_record(native_record, slot_index=slot_index)
    return _build_uc_slot_memp_frames(slot_index, record)


def build_paired_doubler_private_reverb_frames(
        channel1_slot, channel1_base, channel2_slot, channel2_base,
        enabled=True, lows=0.06, width=0.405, mix=0.295,
        preset_name=None, wet_ch1=1.0, wet_ch2=1.0,
        bypass_ch1=False, bypass_ch2=False,
        custom_firmware_lane_controls=False):
    """Build a two-slot Voice-FX fixture for offline comparison only.

    The tagged archive is retained only as a UC settled-state fixture.
    Duplicating one settings object into two physical preset bodies is neither
    atomic nor part of UC's explicit one-input Voice FX assignment workflow.
    """
    from io24_preset_record import paired_doubler_private_reverb_slots
    records = paired_doubler_private_reverb_slots(
        channel1_slot, channel1_base, channel2_slot, channel2_base,
        enabled=enabled, lows=lows, width=width, mix=mix,
        preset_name=preset_name, wet_ch1=wet_ch1, wet_ch2=wet_ch2,
        bypass_ch1=bypass_ch1, bypass_ch2=bypass_ch2,
        custom_firmware_lane_controls=custom_firmware_lane_controls)
    return {slot: _build_uc_slot_json_frames(slot, record)
            for slot, record in records.items()}


def build_paired_doubler_private_reverb_frames_from_local_bases(
        channel1_slot, channel1_base_path, channel2_slot, channel2_base_path,
        **kwargs):
    """Build the superseded paired archive fixture from local JSON bases.

    This remains available only to reproduce prior offline evidence. It does
    not read the io24 and does not reproduce UC's single-channel assignment.
    """
    from io24_preset_record import load_paired_device_slot_bases
    channel1_base, channel2_base = load_paired_device_slot_bases(
        channel1_base_path, channel2_base_path)
    return build_paired_doubler_private_reverb_frames(
        channel1_slot, channel1_base, channel2_slot, channel2_base, **kwargs)


def build_native_model0_voicefx_frames(
        slot_index, base_native_record, enabled=True, lows=0.06,
        width=0.405, mix=0.295):
    """Frame a firmware-default chunk mutation for offline comparison."""
    from io24_native_stat import build_native_model0_stat_record
    record = build_native_model0_stat_record(
        base_native_record, slot_index, enabled=enabled, lows=lows,
        width=width, mix=mix)
    return _build_native_slot_memp_frames(slot_index, record)


# per-channel DSP chain (PROTOCOL.md §9a)
OPT = fourcc("opt ")
FILT = fourcc("filt")
GATE = fourcc("gate")
COMP = fourcc("comp")
EQ = fourcc("eq  ")
LIM = fourcc("lim ")
REDU = fourcc("Redu")
CHNP = fourcc("CHNP")   # channel names, block 0 — see PROTOCOL.md §13j
BQDF = fourcc("Bqdf")
LFDF = fourcc("Lfdf")

RELEASE_48K = 0x3F7FEA8F        # limiter release coefficient for fs=48000 (= 0.4 s)
LIM_OFF_THRESHOLD = 0x3FAAB0D5  # firmware power-on value (initialiser 0x60076c6c)

_DSP = None
_MIXER = None
_FX = None
_METERS = None


def _mixer_mod():
    """io24_mixer holds the fader taper and mixer blob builders."""
    global _MIXER
    if _MIXER is None:
        import io24_mixer
        _MIXER = io24_mixer
    return _MIXER


def _meters_mod():
    """io24_meters holds the reverb blob builder and the metering datagrams."""
    global _METERS
    if _METERS is None:
        import io24_meters
        _METERS = io24_meters
    return _METERS


def _fx_mod():
    """io24_fx holds the FX builders and several biquad designers."""
    global _FX
    if _FX is None:
        import io24_fx
        _FX = io24_fx
    return _FX


class HostActionError(RuntimeError):
    """A fail-closed error from an explicitly scoped native-host action."""


def _preset_is_enabled(params, channel):
    bit = 5 if channel == 1 else 6
    return not bool(int(params["flags"]) >> bit & 1)


def _channel1_guard(params):
    """Return only Channel-1 fields a Channel-2 action must preserve."""
    return {
        "gain": params["input1Gain"],
        "preset_enabled": _preset_is_enabled(params, 1),
        "processing_channel": params["input1ProcessingChannel"],
        "slot": params["input1SlotIndex"],
    }


def _linked_recall_state(params):
    """Project the readable fields needed by the bounded linked recall."""
    flags = int(params["flags"])
    return {
        "flags_word": flags,
        "channel_linked": bool(flags >> 12 & 1),
        "input1_slot_index": params["input1SlotIndex"],
        "input1_preset_enabled": _preset_is_enabled(params, 1),
        "input1_processing_channel": params["input1ProcessingChannel"],
        "input1_gain_db": params["input1Gain"],
        "input2_slot_index": params["input2SlotIndex"],
        "input2_preset_enabled": _preset_is_enabled(params, 2),
        "input2_processing_channel": params["input2ProcessingChannel"],
        "input2_gain_db": params["input2Gain"],
    }


FACTORY_SLAP_GATE = {
    "threshold_db": -47.5,
    "range_db": -60.0,
    "attack_s": 0.025,
    "release_s": 0.7,
    "keyfilter_hz": 730.000244,
    "expander": True,
    "keylisten": False,
    "instance": None,
    "fs": 48000.0,
}


def _set_factory_slap_gate(dev, on):
    """Send the byte-pinned factory Slap Echo gate state on Channel 2."""
    return dev.set_gate(2, on=bool(on), **FACTORY_SLAP_GATE)


def recall_channel2_slot3_while_linked(dev, sleep_fn=time.sleep,
                                        event_fn=lambda _event: None):
    """Run the CP34 linked positive-slot recall and always clear link.

    This deliberately does not write a preset record or any audio/routing
    control.  It starts from the pinned live baseline, moves Channel 2 to slot
    2 while unlinked, records a dry capture window, links the channel pair,
    recalls Channel-2 slot 3 once, records the active window, and clears the
    link in ``finally``.  The caller owns audio capture and device identity and
    battery gates.
    """
    before = dev.read_params()
    if before is None:
        raise HostActionError("unable to read the linked-recall baseline")
    before_state = _linked_recall_state(before)
    if before_state["channel_linked"]:
        raise HostActionError("linked recall must start unlinked")
    if before_state["input1_slot_index"] != 0 or \
            before_state["input1_preset_enabled"]:
        raise HostActionError("Channel 1 baseline must remain disabled on slot 0")
    if before_state["input2_slot_index"] != 3 or \
            not before_state["input2_preset_enabled"]:
        raise HostActionError("Channel 2 baseline must be enabled on slot 3")

    channel1_before = _channel1_guard(before)
    event_fn("baseline_confirmed")
    final = None
    try:
        dev.set_preset_slot(2, 2)
        sleep_fn(0.5)
        slot2_params = dev.read_params()
        if slot2_params is None:
            raise HostActionError("unable to confirm Channel-2 slot 2")
        slot2_state = _linked_recall_state(slot2_params)
        if slot2_state["channel_linked"] or \
                slot2_state["input2_slot_index"] != 2:
            raise HostActionError("Channel 2 did not reach unlinked slot 2")
        if _channel1_guard(slot2_params) != channel1_before:
            raise HostActionError("Channel 1 changed before the linked recall")
        event_fn("channel2_slot2_confirmed")
        sleep_fn(3.0)

        dev.set_channel_link(True)
        sleep_fn(0.5)
        linked_params = dev.read_params()
        if linked_params is None:
            raise HostActionError("unable to confirm stereo link")
        linked_state = _linked_recall_state(linked_params)
        if not linked_state["channel_linked"] or \
                linked_state["input2_slot_index"] != 2:
            raise HostActionError("stereo link did not engage on Channel-2 slot 2")
        event_fn("link_on_confirmed")

        dev.set_preset_slot(2, 3)
        sleep_fn(0.5)
        active_params = dev.read_params()
        if active_params is None:
            raise HostActionError("unable to confirm linked Channel-2 slot 3")
        active_state = _linked_recall_state(active_params)
        if not active_state["channel_linked"] or \
                active_state["input2_slot_index"] != 3:
            raise HostActionError("Channel 2 did not recall slot 3 while linked")
        event_fn("channel2_slot3_confirmed")
        sleep_fn(5.0)
    finally:
        dev.set_channel_link(False)
        sleep_fn(0.5)
        final_params = dev.read_params()
        if final_params is not None:
            final = _linked_recall_state(final_params)
            if not final["channel_linked"]:
                event_fn("link_off_confirmed")

    if final is None:
        raise HostActionError("unable to read state after clearing stereo link")
    if final["channel_linked"]:
        raise HostActionError("stereo link remained engaged after cleanup")
    if final["input2_slot_index"] != 3:
        raise HostActionError("Channel 2 did not finish on slot 3")
    return {
        "status": "LINKED_CHANNEL2_SLOT3_RECALL_COMPLETED",
        "audibility": "UNPROVED",
        "baseline_state": before_state,
        "final_state": final,
        "channel1_unchanged": _channel1_guard(final_params) == channel1_before,
    }


def probe_channel2_slot3_with_gate_transition(
        dev, sleep_fn=time.sleep, event_fn=lambda _event: None,
        input_ceiling=0.01):
    """Probe linked slot 3 with a low-level, exactly inverted gate transition.

    The gate is not readable.  This action therefore brackets the temporary
    off state with the exact factory Slap Echo gate-on write, restores that
    same write before clearing stereo link, and reports restoration only as a
    sent command.  It never changes gain, routing, output, or preset content.
    """
    before = dev.read_params()
    if before is None:
        raise HostActionError("unable to read the gate-transition baseline")
    before_state = _linked_recall_state(before)
    if before_state["flags_word"] != 0x1A1 or before_state["channel_linked"]:
        raise HostActionError("gate-transition probe requires flags 0x1a1")
    if before_state["input1_slot_index"] != 0 or \
            before_state["input1_preset_enabled"]:
        raise HostActionError("Channel 1 baseline must remain disabled on slot 0")
    if before_state["input2_slot_index"] != 3 or \
            not before_state["input2_preset_enabled"]:
        raise HostActionError("Channel 2 baseline must be enabled on slot 3")

    channel1_before = _channel1_guard(before)
    event_fn("baseline_confirmed")
    mutation_started = False
    gate_restore_required = False
    primary_error = None
    cleanup_errors = []
    final_params = None
    final_state = None
    try:
        mutation_started = True
        dev.set_preset_slot(2, 2)
        sleep_fn(0.5)
        slot2_params = dev.read_params()
        if slot2_params is None:
            raise HostActionError("unable to confirm Channel-2 slot 2")
        slot2_state = _linked_recall_state(slot2_params)
        if slot2_state["channel_linked"] or \
                slot2_state["input2_slot_index"] != 2:
            raise HostActionError("Channel 2 did not reach unlinked slot 2")
        if _channel1_guard(slot2_params) != channel1_before:
            raise HostActionError("Channel 1 changed before the linked probe")
        event_fn("channel2_slot2_confirmed")
        sleep_fn(1.0)

        dev.set_channel_link(True)
        sleep_fn(0.5)
        linked_params = dev.read_params()
        if linked_params is None:
            raise HostActionError("unable to confirm stereo link")
        linked_state = _linked_recall_state(linked_params)
        if not linked_state["channel_linked"] or \
                linked_state["input2_slot_index"] != 2:
            raise HostActionError("stereo link did not engage on Channel-2 slot 2")
        event_fn("link_on_confirmed")

        dev.set_preset_slot(2, 3)
        sleep_fn(0.5)
        active_params = dev.read_params()
        if active_params is None:
            raise HostActionError("unable to confirm linked Channel-2 slot 3")
        active_state = _linked_recall_state(active_params)
        if not active_state["channel_linked"] or \
                active_state["input2_slot_index"] != 3:
            raise HostActionError("Channel 2 did not recall slot 3 while linked")
        input_level = active_params.get("input2Level")
        if isinstance(input_level, bool) or not isinstance(input_level, (int, float)) \
                or not math.isfinite(input_level) or input_level < 0 \
                or input_level > input_ceiling:
            raise HostActionError("Input 2 exceeds or lacks the probe ceiling")
        event_fn("channel2_slot3_confirmed")

        gate_restore_required = True
        _set_factory_slap_gate(dev, True)
        event_fn("factory_gate_on_sent")
        sleep_fn(1.5)
        _set_factory_slap_gate(dev, False)
        event_fn("factory_gate_off_sent")
        sleep_fn(3.0)
    except BaseException as error:
        primary_error = error
    finally:
        if gate_restore_required:
            try:
                _set_factory_slap_gate(dev, True)
                event_fn("factory_gate_restored")
                sleep_fn(1.0)
            except BaseException as error:
                cleanup_errors.append(("factory gate restoration", error))
        if mutation_started:
            try:
                dev.set_channel_link(False)
                sleep_fn(0.5)
                final_params = dev.read_params()
                if final_params is None:
                    raise HostActionError("unable to read state after clearing link")
                final_state = _linked_recall_state(final_params)
                if final_state["channel_linked"]:
                    raise HostActionError("stereo link remained engaged after cleanup")
                event_fn("link_off_confirmed")
            except BaseException as error:
                cleanup_errors.append(("stereo-link cleanup", error))

    if cleanup_errors:
        detail = "; ".join("%s failed: %s" % (stage, error)
                           for stage, error in cleanup_errors)
        raise HostActionError(detail) from primary_error
    if primary_error is not None:
        raise primary_error
    if final_state["input2_slot_index"] != 3:
        raise HostActionError("Channel 2 did not finish on slot 3")
    return {
        "status": "LINKED_CHANNEL2_GATE_TRANSITION_COMPLETED",
        "audibility": "UNPROVED",
        "baseline_state": before_state,
        "final_state": final_state,
        "input2_probe_level": input_level,
        "input2_probe_ceiling": input_ceiling,
        "gate_restoration": "WRITE_SENT_NOT_READABLE",
        "channel1_unchanged": _channel1_guard(final_params) == channel1_before,
    }


def arm_channel2_delay(dev, sample_rate_hz=None):
    """Use the native host to arm the fixed shared delay after Ch-2 recall.

    Block 201 is a singleton, so only its exact existing delay parameters can
    be written here.  The recalled device slot and the preset-enable control
    are explicitly Channel 2.  A fresh state read must already show enabled
    Channel-2 slot 3; otherwise the function refuses before every write.

    A successful transport/state result is intentionally not an audibility
    claim: the DSP activation gate is not readable through this host API.
    """
    before = dev.read_params()
    if before is None:
        raise HostActionError("unable to read the native-host baseline")
    if before["input2SlotIndex"] != 3:
        raise HostActionError("Channel 2 must already select slot 3")
    if not _preset_is_enabled(before, 2):
        raise HostActionError("Channel 2 preset function must already be enabled")

    channel1_before = _channel1_guard(before)
    dev.set_preset_enabled(2, True)
    dev.set_preset_slot(2, 3)
    delay_kwargs = _fx_mod().voicefx_runtime_kwargs(
        "delay", {"on": True, "time_s": 0.173,
                  "feedback": 0.25, "mix": 0.5}, sample_rate_hz)
    delay_parameter_writes = dev.set_fx("delay", **delay_kwargs)

    after = dev.read_params()
    if after is None:
        raise HostActionError("unable to read the native-host post-state")
    if _channel1_guard(after) != channel1_before:
        raise HostActionError("Channel 1 guard changed during Channel-2 action")
    if after["input2SlotIndex"] != 3 or not _preset_is_enabled(after, 2):
        raise HostActionError("Channel 2 did not remain enabled on slot 3")
    return {
        "audibility": "UNPROVED",
        "channel1_unchanged": True,
        "channel2_slot": 3,
        "delay_parameter_writes": delay_parameter_writes,
        "status": "HOST_DELAY_ARMED_CH2_REASSERTED",
    }


FACTORY_SLAP_ECHO_BYTES = 850
FACTORY_SLAP_ECHO_SHA256 = (
    "a6c3294b62cfb3a46122a82c93514ea2aeb836280fb5d1e458707a78634f87eb"
)


def load_channel2_factory_slap_echo(dev, slot_record, sleep_fn=time.sleep):
    """Refuse the superseded Slap-only diagnostic before device access.

    Its tagged record is a valid UC library/settled-state representation, not
    a firmware-native io24 slot body. The earlier trial also omitted UC's Voice
    FX channel assignment and cannot answer the current question.
    """
    from io24_preset_record import (complete_device_slot_record,
                                    encode_preset_record)

    record = complete_device_slot_record(slot_record)
    encoded = encode_preset_record(record)
    if len(encoded) != FACTORY_SLAP_ECHO_BYTES or \
            hashlib.sha256(encoded).hexdigest() != FACTORY_SLAP_ECHO_SHA256:
        raise ValueError("record is not the canonical Slap Echo factory archive")

    _ = dev, sleep_fn
    raise HostActionError(
        "the superseded Slap Echo action omits Assign Voice FX; no device "
        "action was attempted")


def biquad_highshelf(freq, gain_db, fs=48000.0, Q=0.7):
    """Source-bound UC 4.7.2 Standard high-shelf transcription.

    ``eqtype`` UI value 2 maps through the Standard component table to designer
    selector 9.  The Standard band caller supplies three binary32 values; its
    designer widens them to binary64, computes selector 9 without intermediate
    single-precision rounding, then independently narrows the five object
    fields to binary32 for ``Bqdf``.  This follows the full-recompute order at
    0x18000ff44, 0x18001039d, and the shared tail at 0x18001033b.
    """
    names_and_values = (
        ("frequency", freq), ("gain", gain_db),
        ("sample rate", fs), ("Q", Q),
    )
    numeric = {}
    for name, value in names_and_values:
        try:
            value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("high-shelf %s must be numeric" % name) from error
        if not math.isfinite(value):
            raise ValueError("high-shelf %s must be finite" % name)
        numeric[name] = value
    freq = numeric["frequency"]
    gain_db = numeric["gain"]
    fs = numeric["sample rate"]
    Q = numeric["Q"]
    if not 20.0 <= freq <= 18000.0:
        raise ValueError("high-shelf frequency must be 20..18000 Hz")
    if not -15.0 <= gain_db <= 15.0:
        raise ValueError("high-shelf gain must be -15..15 dB")
    if not 0.1 <= Q <= 10.0:
        raise ValueError("high-shelf Q must be 0.1..10")
    if fs <= 0.0:
        raise ValueError("high-shelf sample rate must be positive")
    if freq >= fs * 0.5:
        raise ValueError("high-shelf frequency must be below Nyquist")

    def f32(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]

    # movss band fields followed by cvtss2sd in the Standard designer setter.
    freq = f32(freq)
    gain_db = f32(gain_db)
    Q = f32(Q)

    pi_over_fs = math.pi / fs
    w = (freq + freq) * pi_over_fs
    sw = math.sin(w)
    cw = math.cos(w)

    gain_exponent = gain_db * 0.025
    A = math.pow(10.0, gain_exponent)
    A_minus_one = A - 1.0
    sqrt_A = math.sqrt(A)
    inverse_Q = 1.0 / Q
    beta_base = sqrt_A * inverse_Q
    A_plus_one = A + 1.0
    A_minus_one_cos = A_minus_one * cw
    denominator_base = A_plus_one - A_minus_one_cos
    A_plus_one_cos = A_plus_one * cw
    numerator_base = A_plus_one + A_minus_one_cos
    beta = beta_base * sw
    a0 = denominator_base + beta
    normalization = 1.0 / a0

    neg_a1_base = A_minus_one - A_plus_one_cos
    b1_base = A_minus_one + A_plus_one_cos
    b1_base = b1_base * -2.0
    neg_a1_base = neg_a1_base * -2.0
    neg_a1 = neg_a1_base * normalization
    A_normalized = A * normalization
    neg_a2_base = denominator_base - beta
    b0_base = numerator_base + beta
    b2_base = numerator_base - beta
    b1 = b1_base * A_normalized
    neg_a2 = -(neg_a2_base * normalization)
    b2 = b2_base * A_normalized
    b0 = b0_base * A_normalized
    return [f32(value) for value in (b0, neg_a1, b1, neg_a2, b2)]


def biquad_peaking(freq, gain_db, fs=48000.0, Q=0.7):
    """Source-bound UC 4.7.2 Standard peaking-EQ transcription.

    Standard middle bands retain designer selector 6. The band caller passes
    binary32 frequency, gain, and Q; the designer widens those values, performs
    the selector-6 branch at ``0x18000fe4c`` entirely in binary64, and narrows
    only the five output fields.
    """
    names_and_values = (
        ("frequency", freq), ("gain", gain_db),
        ("sample rate", fs), ("Q", Q),
    )
    numeric = {}
    for name, value in names_and_values:
        try:
            value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("peaking %s must be numeric" % name) from error
        if not math.isfinite(value):
            raise ValueError("peaking %s must be finite" % name)
        numeric[name] = value
    freq = numeric["frequency"]
    gain_db = numeric["gain"]
    fs = numeric["sample rate"]
    Q = numeric["Q"]
    if not 20.0 <= freq <= 18000.0:
        raise ValueError("peaking frequency must be 20..18000 Hz")
    if not -15.0 <= gain_db <= 15.0:
        raise ValueError("peaking gain must be -15..15 dB")
    if not 0.1 <= Q <= 10.0:
        raise ValueError("peaking Q must be 0.1..10")
    if fs <= 0.0:
        raise ValueError("peaking sample rate must be positive")
    if freq >= fs * 0.5:
        raise ValueError("peaking frequency must be below Nyquist")

    def f32(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]

    freq = f32(freq)
    gain_db = f32(gain_db)
    Q = f32(Q)

    gain_power = math.pow(10.0, gain_db * 0.05)
    k = math.tan(freq * (math.pi / fs))
    k2 = k * k
    alpha_numerator = math.sqrt(gain_power) * k
    alpha_numerator = alpha_numerator / Q
    alpha_denominator = alpha_numerator / gain_power
    denominator = (alpha_denominator + k2) + 1.0

    neg_a2 = ((alpha_denominator - 1.0) - k2) / denominator
    neg_a1 = ((1.0 - k2) + (1.0 - k2)) / denominator
    b0 = ((k2 + alpha_numerator) + 1.0) / denominator
    b1 = (((k2 - 1.0) + k2) - 1.0) / denominator
    b2 = ((k2 - alpha_numerator) + 1.0) / denominator
    return [f32(value) for value in (b0, neg_a1, b1, neg_a2, b2)]


def biquad_lowshelf(freq, gain_db, fs=48000.0, Q=0.7):
    """Source-bound UC 4.7.2 Standard low-shelf transcription.

    An enabled first Standard band selects designer 8. Like the other Standard
    paths, its three semantic values arrive as binary32 and are widened before
    the binary64 branch at ``0x18001028a``.
    """
    names_and_values = (
        ("frequency", freq), ("gain", gain_db),
        ("sample rate", fs), ("Q", Q),
    )
    numeric = {}
    for name, value in names_and_values:
        try:
            value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("low-shelf %s must be numeric" % name) from error
        if not math.isfinite(value):
            raise ValueError("low-shelf %s must be finite" % name)
        numeric[name] = value
    freq = numeric["frequency"]
    gain_db = numeric["gain"]
    fs = numeric["sample rate"]
    Q = numeric["Q"]
    if not 20.0 <= freq <= 18000.0:
        raise ValueError("low-shelf frequency must be 20..18000 Hz")
    if not -15.0 <= gain_db <= 15.0:
        raise ValueError("low-shelf gain must be -15..15 dB")
    if not 0.1 <= Q <= 10.0:
        raise ValueError("low-shelf Q must be 0.1..10")
    if fs <= 0.0:
        raise ValueError("low-shelf sample rate must be positive")
    if freq >= fs * 0.5:
        raise ValueError("low-shelf frequency must be below Nyquist")

    def f32(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]

    freq = f32(freq)
    gain_db = f32(gain_db)
    Q = f32(Q)

    w = (freq + freq) * (math.pi / fs)
    sine = math.sin(w)
    cosine = math.cos(w)
    amplitude = math.pow(10.0, gain_db * 0.025)
    amplitude_minus_one = amplitude - 1.0
    sqrt_amplitude = math.sqrt(amplitude)
    inverse_Q = 1.0 / Q
    beta_base = sqrt_amplitude * inverse_Q
    amplitude_plus_one = amplitude + 1.0
    minus_cosine = amplitude_minus_one * cosine
    plus_cosine = amplitude_plus_one * cosine
    denominator_base = amplitude_plus_one + minus_cosine
    numerator_base = amplitude_plus_one - minus_cosine
    beta = beta_base * sine
    normalization = 1.0 / (denominator_base + beta)

    twice_amplitude_minus_one = amplitude_minus_one + amplitude_minus_one
    twice_plus_cosine = plus_cosine + plus_cosine
    neg_a1 = (twice_plus_cosine + twice_amplitude_minus_one) * normalization
    amplitude_normalized = amplitude * normalization
    b1_base = ((amplitude_minus_one - plus_cosine)
               + (amplitude_minus_one - plus_cosine))
    b1 = b1_base * amplitude_normalized
    neg_a2 = -((denominator_base - beta) * normalization)
    b2 = (numerator_base - beta) * amplitude_normalized
    b0 = (numerator_base + beta) * amplitude_normalized
    return [f32(value) for value in (b0, neg_a1, b1, neg_a2, b2)]


def _dsp_mod():
    """io24_dsp holds the recovered coefficient maths (compressor/gate/limiter).

    Imported lazily so the transport layer works even without it.
    """
    global _DSP
    if _DSP is None:
        import io24_dsp
        _DSP = io24_dsp
    return _DSP


# The device cannot report its DSP settings back (every DSP block is
# write-only), so the driver keeps a durable mirror of what it has written.
# Without it a CLI `savepreset` would capture nothing, since each command is its
# own process. The mirror is a claim about what we last sent, not a reading: a
# power cycle, or Universal Control on another host, will invalidate it.
SHADOW_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "io24", "shadow.json")


def _load_shadow():
    try:
        with open(SHADOW_PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _store_shadow(shadow):
    """Write via a temp file + rename so a crash cannot leave a half-file."""
    try:
        os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
        tmp = SHADOW_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(shadow, fh, indent=1)
        os.replace(tmp, SHADOW_PATH)
    except OSError:
        pass                      # the mirror is a convenience, never a blocker


def inspect_preset(path):
    """Return the effect state recorded in a host preset without opening USB."""
    with open(path) as fh:
        snapshot = json.load(fh)

    effects = {"reverb": None, "voice_fx": None,
               "processing_mix": {}, "fx_returns": {}}
    return_faders = {}
    return_assignments = {}
    bus_masters = {}
    for call in snapshot.get("calls", {}).values():
        name = call.get("fn")
        kwargs = dict(call.get("kwargs", {}))
        if name == "set_reverb":
            effects["reverb"] = kwargs
        elif name == "set_fx":
            effects["voice_fx"] = kwargs
        elif name == "set_fx_mix" and "channel" in kwargs:
            effects["processing_mix"][int(kwargs["channel"])] = \
                kwargs.get("value")
        elif name in ("set_send_db", "set_mix_db") and \
                kwargs.get("source") == "fxreturn/ch1":
            bus = str(kwargs.get("bus", "main")).lower()
            bus = Io24.BUS_ALIASES.get(bus, bus)
            return_faders[bus] = kwargs.get("gain_db")
        elif name == "set_send_assigned" and \
                kwargs.get("source") == "fxreturn/ch1":
            bus = str(kwargs.get("bus", "main")).lower()
            bus = Io24.BUS_ALIASES.get(bus, bus)
            return_assignments[bus] = bool(kwargs.get("on", True))
        elif name == "set_bus_master":
            bus = str(kwargs.get("bus", "main")).lower()
            bus = Io24.BUS_ALIASES.get(bus, bus)
            bus_masters[bus] = kwargs.get("gain_db", 0.0)

    for bus in sorted(set(return_faders) | set(return_assignments)):
        fader_saved = bus in return_faders
        fader = return_faders.get(bus)
        assigned = return_assignments.get(bus, True)
        master = bus_masters.get(bus, 0.0)
        effective = None
        if not assigned:
            effective_state = "off"
        elif not fader_saved:
            effective_state = "unknown"
        elif fader is None:
            effective_state = "off"
        else:
            effective_state = "level"
            effective = max(-144.0, min(10.0,
                                        float(fader) + float(master)))
        effects["fx_returns"][bus] = {
            "fader_db": fader,
            "fader_saved": fader_saved,
            "assigned": assigned,
            "master_db": master,
            "effective_db": effective,
            "effective_state": effective_state,
        }
    return effects


class Io24:
    def __init__(self, shadow=True):
        self.dev = usb.core.find(idVendor=VID,
                                 custom_match=lambda d: d.idProduct in PIDS)
        if self.dev is None:
            raise SystemExit("Revelator io24/io44 (194f:0422/0424) "
                             "not found — plugged in?")
        usb.util.claim_interface(self.dev, IFACE)
        self.dev.set_interface_altsetting(interface=IFACE, alternate_setting=1)
        intf = self.dev.get_active_configuration()[(IFACE, 1)]
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: e.bEndpointAddress == EP_IN)
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: e.bEndpointAddress == EP_OUT)
        self._uid = 0
        # Older GTK builds could save one leg of a stereo balance and could
        # file deprecated preset-enable names beside their canonical form.
        # Normalise before any write-only state is rebuilt from that cache: an
        # orphaned Channel-2 "pan" is precisely how L100 came to mean silence.
        self._shadow = _normalise_shadow(_load_shadow()) if shadow else {}
        self._shadow_dirty = False
        self._shadow_flushed = 0.0
        self._shadow_persist = bool(shadow)
        try:
            self.dev.clear_halt(EP_OUT)
            self.dev.clear_halt(EP_IN)
        except usb.core.USBError:
            pass
        # channel start (read-only vendor queries)
        self.proto = struct.unpack("<H", bytes(
            self.dev.ctrl_transfer(0xC1, 0x00, 0, IFACE, 2)))[0]
        self.max_cmd = struct.unpack("<I", bytes(
            self.dev.ctrl_transfer(0xC1, 0x01, 0, IFACE, 4)))[0]
        self.max_rsp = struct.unpack("<I", bytes(
            self.dev.ctrl_transfer(0xC1, 0x01, 1, IFACE, 4)))[0]

    def close(self):
        self._flush_shadow(force=True)
        try:
            usb.util.release_interface(self.dev, IFACE)
        except Exception:
            pass

    def _flush_shadow(self, force=False):
        """Persist the write mirror, at most once a second unless forced.

        Throttled because the shim moves faders at UI rate and each move would
        otherwise cost a file write; close() forces a final flush, so a normal
        exit never loses anything.
        """
        if not (self._shadow_persist and self._shadow_dirty):
            return
        now = time.monotonic()
        if force or now - self._shadow_flushed >= 1.0:
            _store_shadow(self._shadow)
            self._shadow_dirty = False
            self._shadow_flushed = now

    def _next_uid(self):
        self._uid = (self._uid + 1) & 0xFF
        if self._uid == 0:
            self._uid = 1
        return self._uid

    def _exec(self, payload, wait=1.5):
        """Strict synchronous request/response — one command outstanding.

        Returns as soon as the reply is complete: the paesdk header's
        totalLength field says how many bytes to expect, so there is no need to
        burn a further timeout cycle confirming nothing more is coming. That is
        worth roughly 400 ms per call, which is the difference between metering
        running at a useful rate and not.
        """
        uid = self._next_uid()
        frame = struct.pack("<H", len(payload) + 8) + \
            bytes([0x01, 0x01, uid, 0x00, 0x00, 0x00]) + payload
        try:
            self.ep_out.write(frame, timeout=2000)
        except usb.core.USBError:
            try:
                self.dev.clear_halt(EP_OUT)
            except usb.core.USBError:
                pass
            return b""
        buf = b""
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                chunk = self.ep_in.read(2048, timeout=400)
            except usb.core.USBTimeoutError:
                if buf:
                    break
                continue
            except usb.core.USBError:
                try:
                    self.dev.clear_halt(EP_IN)
                except usb.core.USBError:
                    pass
                break
            if chunk:
                buf += bytes(chunk)
                if len(buf) >= 2:
                    total, = struct.unpack_from("<H", buf, 0)
                    if 8 <= total <= len(buf):
                        break
        return buf[8:] if len(buf) > 8 else b""

    def read_state(self, blob_size=STATE_BLOB_SIZE):
        """GetP / 'Appl' / 'JaSt' -> raw payload of the reply (live DSP state)."""
        blob = struct.pack("<IIII", JAST, blob_size, 0, 0)
        blob += b"\x00" * (blob_size - len(blob))
        payload = struct.pack("<III", GETP, APPL, 0) + blob
        rsp = self._exec(payload)
        if len(rsp) < 16:
            return None
        tag, = struct.unpack_from("<I", rsp, 0)
        if tag != RPLY:
            return None
        return rsp

    def floats(self, rsp, start=DATA_START):
        n = (len(rsp) - start) // 4
        return list(struct.unpack_from("<%df" % n, rsp, start))

    # Wire ids this driver has established a meaning for. Everything else in the
    # accepted range is unmapped, and writing to unmapped ids is NOT safe --
    # see the warning on set_param below.
    # Recovered from the firmware's own 'Para' dispatcher (FW VA 0x6004e36e):
    #   subs r1,#1 ; cmp r1,#13 ; bhi <reject> ; tbb [pc,r1]
    # The 14-entry branch table sends wire ids 5-9 and 11-13 to one shared
    # `add sp,#0x34 ; pop {r4-r7,pc}` -- they are provable NO-OPS, not unknowns.
    # Wire 14 has its own arm, `movs r4,#3 ; movs r1,#24 ; bl setParam`, i.e.
    # kind 3 (float) internal id 24 = outputDelay. Measured on hardware.
    KNOWN_PARA = {1: "hpVolume", 2: "mainVolume", 3: "gain", 4: "fxMix",
                  10: "monitorBlend", 14: "outputDelay"}
    # From the firmware's 'Pari' dispatcher (FW VA 0x6004e484):
    #   subs r1,#4 ; cmp r1,#15 ; bhi <reject> ; tbh [pc,r1,lsl #1]
    # Indexed pairs load both internal ids and pick with the blob index, e.g.
    # wire 5 -> `movs r2,#15 ; movs r3,#16` (input1/2HighPassFilter) and
    # wire 7 -> `movs r2,#6 ; movs r3,#7` (input1/2Mute). An earlier version of
    # this table had 5 and 7 the wrong way round; the setters below were always
    # right, so only these labels were wrong.
    KNOWN_PARI = {0: "phantom", 4: "presetEnable", 5: "hpf", 6: "hpOutputMute",
                  7: "mute", 8: "muteMode", 9: "link", 11: "phonesSource",
                  12: "processingChannel",
                  13: "outputDelayBus",
                  15: "unknown, sets JaSt slot 42 bit 13", 16: "presetSlot",
                  17: "presetMode"}

    def set_param(self, param_id, value, index=0, as_int=False, block=APPL,
                  block_index=0, unsafe=False):
        """Raw SetP / 'Para'(f32) or 'Pari'(i32) on the WIRE paramId space.

        Wire ids are NOT the firmware's internal parameter ids — see PROTOCOL.md
        §6a. 'Para' accepts 1..14, 'Pari' accepts 0 and 4..19. SetP is
        fire-and-forget: it returns no reply, so a short wait is used.

        Unmapped ids require `unsafe=True`. The reason is NOT that they corrupt
        the device -- an earlier version of this docstring claimed that, and the
        firmware disassembly disproves it. The 'Para' dispatcher at FW VA
        0x6004e36e bounds-checks to 1..14 and its branch table sends ids 5-9 and
        11-13 to a shared `add sp,#0x34 ; pop {r4-r7,pc}`: writing them does
        literally nothing. The 2026-08-02 bootloader drop happened during a run
        that was also streaming audio hard, and the kernel logged the AUDIO
        interface failing first ("cannot get freq: err -110"), which points at
        the transport, not at any parameter value.

        The flag is kept because an id with no known meaning has no known range
        either, and because a probe should be a deliberate act. It is a
        speed bump, not a shield.
        """
        if not unsafe:
            table = self.KNOWN_PARI if as_int else self.KNOWN_PARA
            if param_id not in table:
                raise ValueError(
                    "wire id %d is not a mapped %s parameter. Writing unmapped "
                    "ids has crashed this device into its bootloader before; "
                    "pass unsafe=True if you are deliberately probing and can "
                    "power-cycle it. Mapped ids: %s"
                    % (param_id, "'Pari'" if as_int else "'Para'",
                       ", ".join("%d=%s" % kv for kv in sorted(table.items()))))
        tag = PARI if as_int else PARA
        val = struct.pack("<i", int(value)) if as_int else struct.pack("<f", float(value))
        blob = struct.pack("<IIII", tag, 0x14, index, param_id) + val
        payload = struct.pack("<III", SETP, block, block_index) + blob
        return self._exec(payload, wait=0.25)

    # ---------------------------------------------------------------- reading

    def read_params(self):
        """Read live state and decode it into named parameters.

        Slot map recovered from the firmware's 'JaSt' serializer and confirmed
        against hardware (see PROTOCOL.md §6).
        """
        rsp = self.read_state()
        if rsp is None:
            return None
        v = self.floats(rsp)

        def as_int(slot):
            return struct.unpack("<i", struct.pack("<f", v[slot]))[0]

        out = {
            "hpVolume":      v[43],          # 0..1
            "mainVolume":    v[44],          # 0..1
            "monitorMix":    v[45],          # -1..1  (blend)
            "input1Gain":    v[46],          # 0..60 dB
            "input2Gain":    v[47],          # 0..60 dB
            "input1SlotIndex": as_int(40),
            "input2SlotIndex": as_int(41),
            "input1ProcessingChannel": as_int(38) - 3,
            "input2ProcessingChannel": as_int(39) - 3,
            "flags":         as_int(42),
        }
        # slot 50: byte0 = ch1 phantom, byte1 = ch2 phantom
        raw50 = struct.pack("<f", v[50])
        out["input1PhantomPower"] = bool(raw50[0])
        out["input2PhantomPower"] = bool(raw50[1])
        # meters (linear); slot 4 = input1, slot 6 = input2
        out["input1Level"] = v[4]
        out["input2Level"] = v[6]
        return out

    # ---------------------------------------------------------------- writing
    # Wire paramIds below are all inside the firmware's accepted ranges and the
    # firmware clamps values, so these cannot drive a parameter out of range.

    def set_hp_volume(self, value):
        """Headphone volume, 0.0..1.0 ('Para' wire id 1)."""
        self.set_param(1, max(0.0, min(1.0, value)))

    def set_main_volume(self, value):
        """Main output volume, 0.0..1.0 ('Para' wire id 2)."""
        self.set_param(2, max(0.0, min(1.0, value)))

    def set_gain(self, channel, db):
        """Preamp gain in dB, 0..60. channel 1 or 2 ('Para' wire id 3)."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.set_param(3, max(0.0, min(60.0, db)), index=channel - 1)

    def set_fx_mix(self, channel, value):
        """Per-channel processing/effects mix, 0.0..1.0 (wire id 4).

        Zero bypasses the channel's Fat Channel/effects processing; any
        positive value enables it, and JaSt exposes only that zero/nonzero
        distinction.  The exact nonzero scalar is write-only.
        """
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.set_param(4, max(0.0, min(1.0, value)), index=channel - 1)

    # -------------------------------------------------------- output delay
    # Universal Control puts this on its sample-rate page. It exists to line a
    # local signal up with a delayed remote one -- a co-host on a call, a
    # stream's video path -- by holding one bus back.
    #
    # 'Para' wire 14 -> firmware internal 24, float seconds, 0..0.5 in 2 ms
    # steps. 'Pari' wire 13 -> outputDelayBus, which selects what gets held.
    # Both recovered from the firmware's dispatcher and then measured: asking
    # for 50 ms produced 49.50 ms on Mix A at bus 1 and 49.25 ms on Mix B at
    # bus 2, against a rig whose baseline is 0 samples at correlation 1.000.
    DELAY_MAX_S = 0.5
    DELAY_STEP_S = 0.002
    # Bus 1 delayed Mix A and 2/3/4 delayed Mix B; -1 and 0 delayed neither of
    # the two loopback buses. Only those two are observable without a cable, so
    # the names below are what was measured, not the full story -- the settings
    # that looked idle here are the candidates for the analog outputs.
    DELAY_BUSES = {"off": -1, "none": 0, "mixa": 1, "mixb": 2}

    def set_output_delay(self, seconds, bus=None):
        """Hold one output bus back, 0..0.5 s. See DELAY_BUSES for `bus`.

        Order matters: the bus is selected first, so the delay never lands on
        whichever bus was selected before.
        """
        if bus is not None:
            self.set_output_delay_bus(bus)
        s = max(0.0, min(self.DELAY_MAX_S, float(seconds)))
        s = round(s / self.DELAY_STEP_S) * self.DELAY_STEP_S
        return self.set_param(14, s)

    def set_output_delay_bus(self, bus):
        """Which output the delay applies to. Name from DELAY_BUSES, or an int."""
        if isinstance(bus, str):
            key = bus.strip().lower()
            key = self.BUS_ALIASES.get(key, key)
            if key not in self.DELAY_BUSES:
                raise ValueError("unknown delay bus %r (have %s)"
                                 % (bus, "/".join(self.DELAY_BUSES)))
            bus = self.DELAY_BUSES[key]
        return self.set_param(13, max(-1, min(4, int(bus))), as_int=True)

    def output_delay_off(self):
        """Clear the delay, then park the selector.

        Goes through `set_output_delay` rather than writing `'Para'` 14 directly:
        the direct write bypassed the shadow, so clearing the delay was invisible
        to a saved preset and loading one brought the old delay back.
        """
        self.set_output_delay(0.0)
        return self.set_output_delay_bus(0)

    def set_monitor_mix(self, value):
        """Monitor blend, -1.0..1.0 ('Para' wire id 10)."""
        self.set_param(10, max(-1.0, min(1.0, value)))

    def set_phantom(self, channel, on):
        """48V phantom power ('Pari' wire id 0)."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.set_param(0, 1 if on else 0, index=channel - 1, as_int=True)

    def set_highpass(self, channel, on):
        """Input high-pass filter ('Pari' wire id 5)."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.set_param(5, 1 if on else 0, index=channel - 1, as_int=True)

    def set_mute(self, channel, on):
        """Input mute ('Pari' wire id 7)."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.set_param(7, 1 if on else 0, index=channel - 1, as_int=True)

    def set_hp_mute(self, on):
        """Headphone output mute ('Pari' wire id 6)."""
        self.set_param(6, 1 if on else 0, as_int=True)

    def set_mute_mode(self, mode):
        """Channel Mute Sync ('Pari' wire id 8 -> firmware internal id 5).

        The owner's manual, section 7.1 item 5: "Channel Mute Sync. With this
        enabled, clicking Mute on a channel mutes the channel in all outputs:
        Main, Stream Mix A, and Stream Mix B."  So this changes what
        :meth:`set_mute` means, from one bus to every bus.

        The wire id is not a guess.  PROTOCOL.md's `'Pari'` dispatcher table
        has wire 8 -> `muteMode`, index ignored, recovered from the firmware
        table at fw 0x2e49a; an early probe wrote wire 8 believing it was
        `presetButtonMode` and the correction is recorded on
        :meth:`set_preset_mode`.

        WHAT IS NOT ESTABLISHED: which integer means enabled.  UC's name and
        the manual's wording make 1 the sync-on value, but no hardware run has
        confirmed it, and like `presetMode` this is write-only -- no slot in
        the 503-float state blob tracks it, so it cannot be read back.  The
        caller passes the integer, and a boolean is accepted only as the
        documented inference.  Do not report a value as verified from a
        successful transport; SetP is fire-and-forget.
        """
        if isinstance(mode, bool):
            mode = 1 if mode else 0
        if not isinstance(mode, int):
            raise TypeError("mute mode must be an integer")
        if mode not in (0, 1):
            raise ValueError("mute mode must be 0 or 1")
        self.set_param(8, mode, index=0, as_int=True)
        return {"wire_id": 8, "parameter": "muteMode", "value": mode,
                "value_mapping": "INFERRED_FROM_VENDOR_NAMING",
                "readback": "UNAVAILABLE"}

    PHONE_SOURCES = {"main": 0, "mixa": 1, "mixb": 2}
    PHONE_SOURCE_ALIASES = {
        "main": "main", "mainmix": "main",
        "mixa": "mixa", "mix a": "mixa", "aux1": "mixa", "stream mix a": "mixa",
        "mixb": "mixb", "mix b": "mixb", "aux2": "mixb", "stream mix b": "mixb",
    }

    def set_phones_source(self, source):
        """Select Main, Mix A, or Mix B for the headphones.

        This is the firmware's special write-only ``'Pari'`` wire id 11.  It
        stores the enum directly at ``device+0x1100`` rather than using the
        generic internal-parameter table, which is why it is absent from
        ``JaSt`` and cannot be confirmed by a software readback.  The Host
        shadows the requested value; an objective listening/capture test is
        still required to prove the acoustic result.
        """
        if isinstance(source, str):
            key = source.strip().lower()
            if key.lstrip("+-").isdigit():
                value = int(key)
            else:
                canonical = self.PHONE_SOURCE_ALIASES.get(key)
                if canonical is None:
                    raise ValueError(
                        "unknown headphones source %r (have main/mixa/mixb)" %
                        source)
                value = self.PHONE_SOURCES[canonical]
        elif isinstance(source, bool):
            raise ValueError("headphones source must be main/mixa/mixb or 0/1/2")
        else:
            try:
                numeric = float(source)
            except (TypeError, ValueError):
                raise ValueError(
                    "headphones source must be main/mixa/mixb or 0/1/2")
            if not math.isfinite(numeric) or not numeric.is_integer():
                raise ValueError(
                    "headphones source must be an exact enum 0, 1, or 2")
            value = int(numeric)
        if value not in (0, 1, 2):
            raise ValueError("headphones source must be main/mixa/mixb or 0/1/2")
        return self.set_param(11, value, as_int=True)

    def set_channel_link(self, on):
        """Stereo-link the two input channels ('Pari' wire id 9)."""
        self.set_param(9, 1 if on else 0, as_int=True)

    def set_processing_channel(self, channel, source_input):
        """Choose which physical input feeds a channel's DSP chain.

        'Pari' wire 12 -> firmware `input1ProcessingChannel` /
        `input2ProcessingChannel` (internal 19/20), selected by the blob index.
        `source_input` is 1 or 2.

        The firmware keeps the two a permutation of each other, so pointing
        channel 1 at input 2 also moves channel 2 to input 1 -- setting one is a
        swap, not an independent assignment. Observed in JaSt slots 38/39, which
        hold the mixer source id (3 = line/ch1, 4 = line/ch2) each chain is bound
        to, and measured as a 52 dB move on the bus carrying that chain.

        UC treats the channel object whose value is zero as the owner of the
        singleton VoiceFX model. Firmware 1.28 also exchanges the two exposed
        active-slot values when this changes the permutation. Neither behavior
        renumbers the physical preset-control APIs: ``set_preset_enabled(1)``
        and ``set_preset_slot(1, ...)`` always address physical Channel 1, and
        the corresponding channel-2 calls always address physical Channel 2.
        """
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        if source_input not in (1, 2):
            raise ValueError("source_input must be 1 or 2")
        result = self.set_param(12, source_input - 1, index=channel - 1,
                                as_int=True)
        # Changing the processing permutation moves the singleton VoiceFX
        # engine.  The destination cannot be assumed to retain the model that
        # was materialized on the previous input, even when the model name and
        # controls are unchanged.
        self._voicefx_selected_model = None
        self._voicefx_selected_state = {}
        return result

    def set_voicefx_channel(self, channel):
        """Assign the singleton VoiceFX layer to a physical input channel.

        UC exposes this as ``line/ch1/processingChannel``: value 0 gives the
        VoiceFX-owning DSP object physical Input 1 and value 1 gives it Input
        2. Firmware maintains the complementary mapping for the other DSP
        object. This is an assignment layer only; it never changes the
        physical channel namespace used by ``set_preset_enabled`` or
        ``set_preset_slot``.
        """
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        return self.set_processing_channel(1, channel)

    def processing_channel(self, channel):
        """Which input currently feeds a chain, read from JaSt slots 38/39."""
        rsp = self.read_state()
        if not rsp:
            return None
        f = self.floats(rsp)
        raw = struct.unpack("<I", struct.pack("<f", f[38 + (channel - 1)]))[0]
        return {3: 1, 4: 2}.get(raw)

    # ------------------------------------------------------- per-channel DSP
    # These blocks take pre-computed coefficient blobs rather than parameter
    # ids; the dB/Hz -> coefficient maths lives in the host driver and is
    # reimplemented here. See PROTOCOL.md §9a.
    #
    # NOTE the firmware does NOT bounds-check the blob index, so it is clamped
    # here. 'IFac' is never sent.

    def _dsp(self, cmd, block, block_index, blob, max_index=1):
        """Send a block command. `block_index` means different things per block.

        For the per-channel DSP blocks it selects the channel, and there are only
        two, so clamping to 0..1 is right and protects against writing past the
        sub-object. For the MIXER it selects the *bus*, of which there are at
        least three — clamping that to 0..1 silently redirected every `mixb`
        write into Mix A, which the mixer's lack of read-back made invisible.
        Hence the explicit bound rather than a single hardcoded one.
        """
        block_index = max(0, min(max_index, block_index))
        return self._exec(struct.pack("<III", cmd, block, block_index) + blob,
                          wait=0.25 if cmd == SETP else 0.8)

    def read_reduction(self, block, channel=1):
        """Gain-reduction metering. block: 'gate' | 'comp' | 'lim ' | 'opt '.

        Returns a list of linear gains (1.0 = no reduction); dB = 20*log10(v).
        'filt' and 'eq  ' do not implement it.
        """
        blob = struct.pack("<IIII", REDU, 0x4C, 0, 0) + b"\x00" * (0x4C - 16)
        rsp = self._dsp(GETP, fourcc(block), channel - 1, blob)
        if not rsp or len(rsp) < 12 + 0x4C:
            return []
        body = rsp[12:]
        count, = struct.unpack_from("<I", body, 0x48)
        count = max(0, min(count, 16))
        return list(struct.unpack_from("<%df" % count, body, 0x08))

    # ------------------------------------------------------- channel names
    # 'CHNP' on block 0. Found only because the firmware SYNTHESISES this tag
    # from 'APSP' by arithmetic (add.w r2, r2, #imm at fw 0x41c28) rather than
    # loading it as a literal — a literal scan of the image cannot see it at all.
    # See PROTOCOL.md §13i/§13j.
    #
    # Addressable by the blob index, unlike 'APSP' which just echoes it. The
    # device exposes two entries; indices past those return an empty string
    # rather than an error, so the caller stops at the first empty one.

    CHNP_MAX = 24               # scan bound; the device answers well short of it

    def channel_name(self, index):
        """One entry of the device's channel-name table, or '' if unset.

        index 0 is 'Input 1', 1 is 'Monitor Left'. `block_index` is ignored by
        this handler — only the blob index selects the entry.
        """
        blob = struct.pack("<IIII", CHNP, 0x1F0, int(index), 0) + b"\x00" * (0x1F0 - 16)
        rsp = self._exec(struct.pack("<III", GETP, 0, 0) + blob)
        if not rsp or len(rsp) < 0x40:
            return ""
        return bytes(rsp[0x20:0x40]).split(b"\x00")[0].decode("ascii", "replace")

    def channel_names(self):
        """The whole table as {index: name}, stopping at the first empty slot."""
        out = {}
        for i in range(self.CHNP_MAX):
            n = self.channel_name(i)
            if not n:
                break
            out[i] = n
        return out

    def set_limiter(self, channel, on, threshold_db=-28.0, instance=None,
                    release_s=0.4, fs=48000.0):
        """Limiter on a channel strip.

        threshold_db is dBFS. Both the threshold and the release coefficient are
        computed with the host's single-precision semantics (io24_dsp), so the
        bytes match what Universal Control sends. instance=None sets both
        sub-instances; on=False restores the firmware power-on coefficients.
        """
        targets = (0, 1) if instance is None else (max(0, min(1, instance)),)
        for idx in targets:
            if on:
                bits, = struct.unpack("<I", struct.pack(
                    "<f", _dsp_mod().limiter_inv_threshold(threshold_db)))
                rel, = struct.unpack("<I", struct.pack(
                    "<f", _dsp_mod().limiter_release_coef(release_s, fs)))
            else:
                bits, rel = LIM_OFF_THRESHOLD, 0
            blob = struct.pack("<IIIIII", LIM, 0x18, idx, 1 if on else 0, bits, rel)
            self._dsp(SETP, LIM, channel - 1, blob)

    def set_compressor(self, channel, model=0, instance=None, fs=48000.0, **kw):
        """Compressor on a channel strip ('comp' block, 'cpxt' blob).

        model 0 = Standard, 1 = Tube, 2 = FET — a purely host-side choice; the
        device has one compressor and the model only decides which maths fills
        the identical blob. Parameters differ per model:

          Standard: on, threshold_db, ratio, attack_s, release_s, gain_db,
                    softknee, automode, keyfilter_hz, keylisten
          Tube:     on, peak, gain, limit_mode, keyfilter_hz, keylisten
          FET:      on, input_db, output_db, attack_s, release_s, ratio_index,
                    keyfilter_hz, keylisten
        """
        D = _dsp_mod()
        builder = {0: D.cpxt_comp, 1: D.cpxt_tube, 2: D.cpxt_fet}[model]
        targets = (0, 1) if instance is None else (max(0, min(1, instance)),)
        for idx in targets:
            self._dsp(SETP, COMP, channel - 1, builder(idx, fs=fs, **kw))

    def compressor_off(self, channel, instance=None):
        """Disable the compressor without emitting infinite coefficients."""
        D = _dsp_mod()
        targets = (0, 1) if instance is None else (max(0, min(1, instance)),)
        for idx in targets:
            self._dsp(SETP, COMP, channel - 1, D.cpxt_safe_off(idx))

    def set_gate(self, channel, on=True, threshold_db=-40.0, range_db=-60.0,
                 attack_s=0.01, release_s=0.3, keyfilter_hz=0.0,
                 expander=True, keylisten=False, instance=None, fs=48000.0):
        """Noise gate / expander on a channel strip ('gate' block)."""
        D = _dsp_mod()
        targets = (0, 1) if instance is None else (max(0, min(1, instance)),)
        for idx in targets:
            self._dsp(SETP, GATE, channel - 1,
                      D.gate_blob(idx, on, threshold_db, range_db, attack_s,
                                  release_s, keyfilter_hz, expander, keylisten, fs))

    def gate_off(self, channel, instance=None):
        """Restore the gate's byte-exact firmware power-on state."""
        D = _dsp_mod()
        targets = (0, 1) if instance is None else (max(0, min(1, instance)),)
        for idx in targets:
            self._dsp(SETP, GATE, channel - 1, D.gate_blob_poweron(idx))

    @staticmethod
    def highpass_coeffs(freq_hz, fs=48000.0, q=0.70710678):
        """RBJ 2nd-order high-pass as the device's [b0, -a1, b1, -a2, b2]."""
        k = math.tan(math.pi * freq_hz / fs)
        norm = 1.0 + k / q + k * k
        return [1.0 / norm,
                2.0 * (1.0 - k * k) / norm,
                -2.0 / norm,
                (k / q - 1.0 - k * k) / norm,
                1.0 / norm]

    def set_biquad(self, block, channel, coeffs, band=0):
        """Load raw biquad coefficients [b0, -a1, b1, -a2, b2] ('Bqdf').

        block 'filt' ignores band; block 'eq  ' uses band 0..3 (firmware
        bounds-checks it). Both address one channel — measured, see PROTOCOL.md.

        Every biquad write in the driver funnels through here, so this is where
        stability is enforced: an unstable denominator is the one failure mode
        that produces an unbounded oscillation, and it reaches full scale from
        nothing in about 18 ms. The check is done on the float32 values actually
        sent, not the doubles handed in, because the rounding is what the DSP
        runs. The threshold is exactly 1.0 — the worst *legitimate* pole in the
        whole accepted EQ range is 0.99994 (peaking, 20 Hz, +15 dB, Q=10), so
        anything stricter would reject valid filters.
        """
        if len(coeffs) != 5:
            raise ValueError("need 5 coefficients [b0, -a1, b1, -a2, b2]")
        w = struct.unpack("<5f", struct.pack("<5f", *coeffs))
        if not all(math.isfinite(c) for c in w):
            raise ValueError("non-finite biquad coefficient: %r" % (w,))
        a1, a2 = -w[1], -w[3]
        disc = cmath.sqrt(complex(a1 * a1 - 4.0 * a2))
        pole = max(abs((-a1 + disc) / 2.0), abs((-a1 - disc) / 2.0))
        if pole >= 1.0:
            raise ValueError(
                "unstable biquad — pole magnitude %.6f >= 1. Refusing to send; "
                "this would oscillate. coefficients %r" % (pole, list(w)))
        blob = struct.pack("<IIII", BQDF, 0x24, 0, max(0, min(3, band)))
        blob += struct.pack("<5f", *w)
        return self._dsp(SETP, fourcc(block), channel - 1, blob)

    @staticmethod
    def _stable_denominator(coeffs):
        """Schur-test one UC wire-format second/third-order denominator.

        UC interleaves numerator coefficients with negated denominator
        coefficients.  For seven floats the polynomial is
        ``z^3 - na1*z^2 - na2*z - na3``.  The Schur recursion avoids adding a
        numerical package merely to guard the one wider section.
        """
        if len(coeffs) not in (5, 7):
            return False
        padded = tuple(coeffs) + (0.0,) * (7 - len(coeffs))
        polynomial = [1.0, -padded[1], -padded[3], -padded[5]]
        if len(coeffs) == 5:
            polynomial.pop()
        while len(polynomial) > 1:
            first, last = polynomial[0], polynomial[-1]
            if abs(last) >= abs(first):
                return False
            degree = len(polynomial) - 1
            polynomial = [
                first * polynomial[index] -
                last * polynomial[degree - index]
                for index in range(degree)
            ]
        return True

    def set_wide_eq(self, channel, coeffs, band=0):
        """Load UC's seven-float third-order EQ section (``Lfdf``).

        Passive and Vintage each use exactly one of these at live index 0.
        The float order extends Bqdf's convention:
        ``[b0, -a1, b1, -a2, b2, -a3, b3]``.
        """
        if len(coeffs) != 7:
            raise ValueError(
                "need 7 coefficients [b0, -a1, b1, -a2, b2, -a3, b3]")
        w = struct.unpack("<7f", struct.pack("<7f", *coeffs))
        if not all(math.isfinite(value) for value in w):
            raise ValueError("non-finite wide-EQ coefficient: %r" % (w,))
        if not self._stable_denominator(w):
            raise ValueError(
                "unstable wide EQ denominator; refusing to send coefficients %r"
                % (list(w),))
        blob = struct.pack("<IIII", LFDF, 0x2C, 0, max(0, min(3, band)))
        blob += struct.pack("<7f", *w)
        return self._dsp(SETP, EQ, channel - 1, blob)

    def set_highpass_freq(self, channel, freq_hz, fs=48000.0):
        """High-pass cutoff on the 'filt' block, 24..1000 Hz (24 = bypass)."""
        freq_hz = max(24.0, min(1000.0, freq_hz))
        if freq_hz <= 24.001:
            return self.set_biquad("filt", channel, [1.0, 0.0, 0.0, 0.0, 0.0])
        return self.set_biquad("filt", channel, self.highpass_coeffs(freq_hz, fs))

    # ---------------------------------------------------------------- mixer
    # Block 100, blockIndex 0/1/2 = main / Mix A / Mix B. Values are dB floats:
    # -145.0 is the off sentinel, -144.0 the floor, 0.0 unity. See PROTOCOL.md
    # §9c/§9d.
    #
    # NOTE there is NO read-back on this block. A level cannot be queried, so a
    # wrong write is invisible and cannot be precisely undone. Observe the mix-bus
    # meters (JaSt slots 12/13) instead.

    MIXER_BLOCK = 100
    MIXER_OFF_DB = -145.0
    # UCNET route -> mixer paramId (channel index within its route group)
    MIXER_SOURCES = {"line/ch1": 3, "line/ch2": 4, "line/ch3": 6,
                     "return/ch1": 0, "return/ch2": 1, "return/ch3": 2,
                     "fxreturn/ch1": 5}
    MIXER_BUSES = {"main": 0, "mixa": 1, "mixb": 2}

    # UC's writable ``username`` fields are component-model metadata, not an
    # io24 wire command.  Persist them in the Host shadow so presets/scenes and
    # the GTK surface behave like UC without pretending the labels were sent
    # to firmware.
    COMPONENT_PATHS = frozenset({
        "line/ch1", "line/ch2",
        "return/ch1", "return/ch2", "return/ch3",
        "fxreturn/ch1", "aux/ch1", "aux/ch2", "main/ch1",
    })

    # Universal Control's own vocabulary for the same three buses. Its object
    # model gives every channel a `volume`, an `aux1` and an `aux2` (descriptor
    # ids 106/124/125) — those are *host-side descriptor* ids, not wire paramIds,
    # and probing 124/125 as wire ids on block 100 moves nothing at any index
    # (measured 2026-08-01: tone in all three buses, no slot responded). The aux
    # sends reach the device as this same block at blockIndex 1 and 2. Accepting
    # the UC names as aliases means a UCNET client and this driver can speak
    # about the same control without a translation table.
    BUS_ALIASES = {"aux1": "mixa", "aux2": "mixb", "mix1": "mixa", "mix2": "mixb"}

    def _bus(self, bus):
        bus = self.BUS_ALIASES.get(str(bus).lower(), str(bus).lower())
        if bus not in self.MIXER_BUSES:
            raise ValueError("unknown bus %r (have %s, aliases %s)"
                             % (bus, "/".join(self.MIXER_BUSES),
                                "/".join(self.BUS_ALIASES)))
        return bus

    def _write_mix(self, source, bus, gain_db):
        """The raw block-100 write. Not shadowed — the send model above it is."""
        if source not in self.MIXER_SOURCES:
            raise ValueError("unknown mixer source %r" % source)
        db = self.MIXER_OFF_DB if gain_db is None else max(-144.0, min(10.0, gain_db))
        blob = _mixer_mod().para_blob(self.MIXER_SOURCES[source], db, index=0)
        return self._dsp(SETP, self.MIXER_BLOCK, self.MIXER_BUSES[bus], blob,
                         max_index=max(self.MIXER_BUSES.values()))

    # -- the send model ----------------------------------------------------
    # The device offers exactly one number per (source, bus): a level. UC shows
    # three controls on top of it — the send fader, an assign on/off, and a bus
    # master — and folds them together before it writes. We do the same, because
    # the alternative is losing the fader position every time a channel is
    # unassigned. What reaches the wire is:
    #
    #     off               if not assigned, source-muted, or bus-muted
    #     send + master     otherwise, clamped to the block's -144..+10
    #
    # None of it can be read back (block 100 is write-only), so the three parts
    # live in the shadow like every other write.

    DEFAULT_SEND_DB = 0.0

    def _sends(self):
        """The send model, rebuilt from the shadow on first use in a process."""
        if getattr(self, "_send_state", None) is None:
            self._send_state = {
                "level": {}, "assign": {}, "master": {}, "pan": {},
                "source_mute": {}, "bus_mute": {}, "mirror": {},
            }
            for ent in (self._shadow or {}).values():
                kw, fn = ent.get("kwargs", {}), ent.get("fn")
                src, bus = kw.get("source"), kw.get("bus")
                if isinstance(bus, str):        # entries may name a bus either way
                    bus = self.BUS_ALIASES.get(bus.lower(), bus.lower())
                if fn in ("set_send_db", "set_mix_db") and src and bus:
                    self._send_state["level"][(src, bus)] = kw.get("gain_db")
                elif fn == "set_send_assigned" and src and bus:
                    self._send_state["assign"][(src, bus)] = bool(kw.get("on", True))
                elif fn == "set_bus_master" and bus:
                    self._send_state["master"][bus] = kw.get("gain_db", 0.0)
                elif fn == "set_pan" and src and bus:
                    self._send_state["pan"][(src, bus)] = kw.get("pan")
                elif fn == "set_source_mute" and src:
                    self._send_state["source_mute"][src] = bool(
                        kw.get("on", True))
                elif fn == "set_bus_mute" and bus:
                    self._send_state["bus_mute"][bus] = bool(
                        kw.get("on", True))
                elif fn == "set_mirror_main" and bus in ("mixa", "mixb"):
                    self._send_state["mirror"][bus] = bool(
                        kw.get("on", True))
        return self._send_state

    def set_component_name(self, component, name):
        """Persist one UC ``username`` as Host component metadata.

        The io24 has no writable channel-name command.  UC stores this field in
        its component state, so the Linux Host does the same in its durable
        shadow and scene/preset files; this method deliberately performs no
        USB transfer.
        """
        if component not in self.COMPONENT_PATHS:
            raise ValueError("unknown named component %r" % component)
        if not isinstance(name, str):
            raise TypeError("component name must be text")
        name = name.strip()
        if len(name) > 64:
            raise ValueError("component name must be at most 64 characters")
        return name

    def component_name(self, component, default=""):
        """Return the last Host-stored UC component name."""
        if component not in self.COMPONENT_PATHS:
            raise ValueError("unknown named component %r" % component)
        for entry in reversed(list((self._shadow or {}).values())):
            if not isinstance(entry, dict) or \
                    entry.get("fn") != "set_component_name":
                continue
            kwargs = entry.get("kwargs") or {}
            if kwargs.get("component") == component:
                return kwargs.get("name", default)
        return default

    def pan(self, source, bus):
        """The pan position we last set for a source in a bus, or None."""
        return self._sends()["pan"].get((source, self._bus(bus)))

    def send_db(self, source, bus):
        """The last send value; None means either explicit off or not yet set."""
        return self._sends()["level"].get((source, self._bus(bus)))

    def has_send_level(self, source, bus):
        """Whether a concrete send state (including explicit off) is known."""
        return (source, self._bus(bus)) in self._sends()["level"]

    def send_assigned(self, source, bus):
        """Whether the source is assigned to the bus. Defaults to on."""
        return self._sends()["assign"].get((source, self._bus(bus)), True)

    def bus_master(self, bus):
        """The bus master trim in dB. Defaults to 0."""
        return self._sends()["master"].get(self._bus(bus), 0.0)

    def source_muted(self, source):
        """Whether the Host has muted this source across all three buses."""
        if source not in self.SOLO_SOURCES:
            raise ValueError("unknown io24 mixer source %r" % source)
        return self._sends()["source_mute"].get(source, False)

    def bus_muted(self, bus):
        """Whether the Host has muted this whole output bus."""
        return self._sends()["bus_mute"].get(self._bus(bus), False)

    # Universal Control's pan law, recovered from the host binary (PROTOCOL.md
    # §9d): g(x) = K*x^2 + (1-K)*x with K = -0.831783, giving g(0.5) = 0.7079458,
    # i.e. exactly -3.00 dB at centre. Mode 1 is the left leg, g(1-pan); mode 2 the
    # right, g(pan).
    PAN_K = -0.831783

    # Which mixer sources form a stereo pair, and which side each one is. The
    # device gives exactly ONE level per (source, bus) and the mixer blob's index
    # field is ignored (measured, §9c) — so a single mono source cannot be placed
    # in the stereo field at all. Panning here is therefore a BALANCE across a
    # pair, which is what pan means on a two-input interface and is how UC's own
    # two-mode law is defined. Panning a source with no partner only attenuates
    # it, and set_pan says so rather than pretending otherwise.
    STEREO_PAIRS = {"line/ch1": ("line/ch2", "left"),
                    "line/ch2": ("line/ch1", "right"),
                    "return/ch1": ("return/ch2", "left"),
                    "return/ch2": ("return/ch1", "right")}

    @classmethod
    def pan_gain(cls, pan, side):
        """UC's pan law as a linear gain. pan 0.0 = hard left, 1.0 = hard right."""
        p = max(0.0, min(1.0, float(pan)))
        x = (1.0 - p) if side == "left" else p
        return cls.PAN_K * x * x + (1.0 - cls.PAN_K) * x

    def pan_db(self, pan, side):
        """The pan law in dB, as it is folded into the send level."""
        g = self.pan_gain(pan, side)
        return -144.0 if g <= 1e-9 else 20.0 * math.log10(g)

    def _push_send(self, source, bus):
        """Fold level + assign + master + pan into the one number the device takes."""
        s = self._sends()
        state_bus = "main" if bus in ("mixa", "mixb") and \
            s["mirror"].get(bus, False) else bus
        if s["source_mute"].get(source, False) or \
                s["bus_mute"].get(bus, False):
            return self._write_mix(source, bus, None)
        if not s["assign"].get((source, state_bus), True):
            return self._write_mix(source, bus, None)
        soloed = self._solo_state().get(bus)
        if soloed and source not in soloed:
            # Another source is soloed in this bus. Only the wire value goes
            # off; level, assign and pan stay put, so releasing restores them.
            return self._write_mix(source, bus, None)
        if (source, state_bus) not in s["level"]:
            return None                       # never set: leave the device alone
        lvl = s["level"][(source, state_bus)]
        if lvl is None:
            # Explicitly off. This MUST reach the wire: testing `is None` alone
            # conflated "set to off" with "never set", so `set_send_db(.., None)`,
            # `mix_off()` and the CLI `send <src> <bus> off` were all silent
            # no-ops.
            return self._write_mix(source, bus, None)
        pan = s["pan"].get((source, state_bus))
        pan_db = 0.0
        if pan is not None and source in self.STEREO_PAIRS:
            pan_db = self.pan_db(pan, self.STEREO_PAIRS[source][1])
        return self._write_mix(source, bus,
                               lvl + s["master"].get(bus, 0.0) + pan_db)

    def _push_send_and_mirrors(self, source, bus):
        """Write one cell, then every aux currently following its Main cell."""
        result = self._push_send(source, bus)
        if bus == "main":
            for aux in ("mixa", "mixb"):
                if self._sends()["mirror"].get(aux, False):
                    result = self._push_send(source, aux)
        return result

    def set_send_db(self, source, bus, gain_db):
        """Move one source's send fader in one bus. -144..+10, None = off."""
        bus = self._bus(bus)
        if source not in self.MIXER_SOURCES:
            raise ValueError("unknown mixer source %r" % source)
        self._sends()["level"][(source, bus)] = gain_db
        return self._push_send_and_mirrors(source, bus)

    def set_send_assigned(self, source, bus, on=True):
        """Assign / unassign a source from a bus, keeping its fader position.

        UC calls this `assign_aux1` / `assign_aux2`. Unassigning writes the off
        sentinel; reassigning restores the level, which is why the fader has to
        be remembered host-side rather than read back.
        """
        bus = self._bus(bus)
        if source not in self.MIXER_SOURCES:
            raise ValueError("unknown mixer source %r" % source)
        sends = self._sends()
        key = (source, bus)
        sends["assign"][key] = bool(on)
        if on and key not in sends["level"]:
            # "Assigned" must never be a silent no-op.  The old Host could
            # show an active Mix-B route while _push_send returned without a
            # write because no fader value had ever been materialised.  Unity
            # is also the value the untouched routing fader visibly presents.
            # Call the public setter so the new concrete level is persisted in
            # the shadow alongside the assignment.
            return self.set_send_db(source, bus, self.DEFAULT_SEND_DB)
        return self._push_send_and_mirrors(source, bus)

    def set_bus_master(self, bus, gain_db=0.0):
        """A master trim for a whole bus: offsets every assigned send in it.

        The hardware has no bus master — block 100 holds per-source levels and
        nothing above them — so this is applied by rewriting each send. That
        means it costs one USB write per source, and it only moves sends this
        driver has set, since unset ones have no known position to offset.
        """
        bus = self._bus(bus)
        sends = self._sends()
        sends["master"][bus] = max(-96.0, min(10.0, float(gain_db)))
        state_bus = "main" if bus in ("mixa", "mixb") and \
            sends["mirror"].get(bus, False) else bus
        last = None
        for source in self.MIXER_SOURCES:
            if (source, state_bus) in sends["level"]:
                last = self._push_send(source, bus)
        return last

    def set_source_mute(self, source, on=True):
        """Mute one UC mixer source in Main, Mix A, and Mix B.

        Block 100 has no separate mute bit, so this is a Host abstraction just
        like UC's source mute: write each send off while retaining its fader and
        assign state. The first explicit mute action establishes the visible
        0 dB defaults before writing them off, so releasing it has a definite
        state to restore instead of guessing at unreadable mixer state.
        """
        if source not in self.SOLO_SOURCES:
            raise ValueError("unknown io24 mixer source %r" % source)
        sends = self._sends()
        sends["source_mute"][source] = bool(on)
        last = None
        for bus in self.MIXER_BUSES:
            if (source, bus) not in sends["level"]:
                last = self.set_send_db(source, bus, self.DEFAULT_SEND_DB)
            else:
                last = self._push_send(source, bus)
        return last

    def set_bus_mute(self, bus, on=True):
        """Mute a complete UC output bus while retaining all source levels."""
        bus = self._bus(bus)
        sends = self._sends()
        sends["bus_mute"][bus] = bool(on)
        last = None
        # ``line/ch3`` is part of the generic component model but is not an
        # analog input on the io24, so a whole-bus action must never invent it.
        for source in self.SOLO_SOURCES:
            if (source, bus) not in sends["level"]:
                last = self.set_send_db(source, bus, self.DEFAULT_SEND_DB)
            else:
                last = self._push_send(source, bus)
        return last

    # -- solo --------------------------------------------------------------
    # Block 100 has no solo, so the Host makes one, as UC does: every other
    # source in the bus is written off while their level, assign and pan are
    # left exactly where they were, and releasing the solo writes them back.
    # Solo is monitoring state, not mix state. It lives apart from the send
    # model (which a shadow reload rebuilds) and is never shadowed, so a preset
    # saved while soloing still holds the real mix.
    #
    # line/ch3 is the generic backend's third analog input. The io24 has two,
    # and nothing has shown that source id 6 exists here (the parameter matrix
    # blocks it on applicability), so solo never writes it.
    SOLO_SOURCES = tuple(s for s in MIXER_SOURCES if s != "line/ch3")

    def _solo_state(self):
        if getattr(self, "_solo", None) is None:
            self._solo = {}
        return self._solo

    def soloed(self, source, bus):
        """Whether a source is soloed in a bus."""
        return source in self._solo_state().get(self._bus(bus), ())

    def set_solo(self, source, bus, on=True):
        """Solo one source in one bus; several may be soloed at once.

        A source the Host has never placed in the bus is first given the unity
        send its fader already shows, so releasing the solo has a real level to
        return it to rather than leaving it off. Returns the number of sources
        rewritten; repeating the current state rewrites nothing.
        """
        bus = self._bus(bus)
        if source not in self.SOLO_SOURCES:
            raise ValueError("%r cannot be soloed (have %s)"
                             % (source, "/".join(self.SOLO_SOURCES)))
        solo = self._solo_state()
        before = set(solo.get(bus, ()))
        members = set(before)
        if on:
            members.add(source)
        else:
            members.discard(source)
        if members == before:
            return 0
        if members:
            solo[bus] = members
        else:
            solo.pop(bus, None)
        return self._push_solo_bus(bus, materialise=bool(members))

    def clear_solo(self, bus=None):
        """Release every solo, in one bus or in all of them."""
        solo = self._solo_state()
        buses = list(solo) if bus is None else [self._bus(bus)]
        n = 0
        for b in buses:
            if solo.pop(b, None):
                n += self._push_solo_bus(b, materialise=False)
        return n

    def _push_solo_bus(self, bus, materialise):
        sends = self._sends()
        n = 0
        for source in self.SOLO_SOURCES:
            if (source, bus) in sends["level"]:
                self._push_send(source, bus)
            elif materialise:
                self.set_send_db(source, bus, self.DEFAULT_SEND_DB)
            else:
                continue
            n += 1
        return n

    def set_pan(self, source, bus, pan):
        """Place a source in the stereo field of a bus. 0.0 left, 0.5 centre, 1.0 right.

        Host-side, exactly as Universal Control does it: UC computes
        `volume_dB + aux_dB + 20*log10(blend) + pan_dB` and sends the single number
        the mixer accepts (§9c). The pan law is UC's own, recovered from the host
        binary (§9d) — -3.00 dB at centre.

        IMPORTANT LIMITATION, and it is a property of the hardware rather than of
        this driver. Block 100 holds exactly one level per (source, bus), and the
        mixer blob's index field is ignored (measured, §9c), so there is no way to
        give one source different gains in the left and right legs. Panning is
        therefore a BALANCE across a stereo pair: pan `line/ch1` left and it gets
        louder while `line/ch2` — panned oppositely — gets quieter. On a source
        with no partner in STEREO_PAIRS this only attenuates, which is why that
        case raises rather than silently doing something useless.

        `pan=None` clears it.
        """
        bus = self._bus(bus)
        if source not in self.MIXER_SOURCES:
            raise ValueError("unknown mixer source %r" % source)
        if pan is not None and source not in self.STEREO_PAIRS:
            raise ValueError(
                "%r has no stereo partner, so pan would only attenuate it. "
                "This device has one level per (source, bus); pan is a balance "
                "across a pair. Pannable: %s"
                % (source, "/".join(sorted(self.STEREO_PAIRS))))
        sends = self._sends()
        key = (source, bus)
        sends["pan"][key] = (
            None if pan is None else max(0.0, min(1.0, float(pan))))
        if pan is not None and key not in sends["level"]:
            # A balance move is an explicit routing action.  Materialise the
            # displayed unity fader first instead of accepting the movement
            # into host bookkeeping while emitting no mixer write.
            return self.set_send_db(source, bus, self.DEFAULT_SEND_DB)
        return self._push_send_and_mirrors(source, bus)

    def set_pair_pan(self, source, bus, pan):
        """Pan a stereo pair together: one call, both legs, opposite sides.

        This is what a pan control on a linked channel should do — moving it left
        must lift the left member and drop the right one. Calling set_pan on a
        single member only moves that member, which is half a pan.
        """
        bus = self._bus(bus)
        if source not in self.STEREO_PAIRS:
            raise ValueError("%r is not part of a stereo pair" % source)
        partner = self.STEREO_PAIRS[source][0]
        self.set_pan(source, bus, pan)
        return self.set_pan(partner, bus, pan)

    def mirror_main_enabled(self, bus):
        """Whether a Host-persisted aux bus is latched to follow Main."""
        bus = self._bus(bus)
        if bus not in ("mixa", "mixb"):
            return False
        return bool(self._sends()["mirror"].get(bus, False))

    def set_mirror_main(self, bus, on=True):
        """Persistently latch an aux mix to Main, matching UC's semantics.

        The aux's own faders, assigns and balances remain stored while the
        latch is active.  Main changes are written through immediately; when
        the latch is cleared, the retained aux mix is restored.
        """
        bus = self._bus(bus)
        if bus not in ("mixa", "mixb"):
            raise ValueError("Mirror Main is available only for Mix A or Mix B")
        sends = self._sends()
        enabled = bool(on)
        changed = sends["mirror"].get(bus, False) != enabled
        sends["mirror"][bus] = enabled
        written = 0
        for source in self.MIXER_SOURCES:
            state_bus = "main" if enabled else bus
            if (source, state_bus) not in sends["level"]:
                continue
            self._push_send(source, bus)
            written += 1
        return {"bus": bus, "enabled": enabled, "changed": changed,
                "sources_written": written}

    def mirror_main(self, bus):
        """Compatibility alias: enable UC's persistent Mirror Main latch."""
        return self.set_mirror_main(bus, True)

    def set_mix_db(self, source, gain_db, bus="main"):
        """Set a source's level in a mix bus, in dB.

        source: 'line/ch1', 'return/ch2', 'fxreturn/ch1', ...
        bus:    'main' | 'mixa' (= 'aux1') | 'mixb' (= 'aux2')
        gain_db: -144..+10; pass None or -145 to switch the source off.
        """
        return self.set_send_db(source, self._bus(bus), gain_db)

    def set_mix_norm(self, source, norm, bus="main"):
        """Same, but taking a normalised 0..1 fader position.

        Uses the device's own `fader` taper (5-point piecewise linear,
        0.735 = unity), so a UCNET-style 0..1 control maps correctly.
        """
        return self.set_mix_db(source, _mixer_mod().taper_norm_to_db(
            max(0.0, min(1.0, norm))), bus=bus)

    def mix_off(self, source, bus="main"):
        """Switch a source out of a bus entirely (the -145 dB sentinel)."""
        return self.set_mix_db(source, None, bus=bus)

    def bus_summary(self, bus):
        """What this driver believes is in a bus. A claim, not a reading."""
        bus = self._bus(bus)
        return {"bus": bus, "master": self.bus_master(bus),
                "sends": {s: {"db": self.send_db(s, bus),
                              "assigned": self.send_assigned(s, bus)}
                          for s in self.MIXER_SOURCES
                          if self.send_db(s, bus) is not None}}

    # -------------------------------------------------------------- presets
    # Firmware 1.28 stores MemP/Stat payload bytes unchanged and its recalled
    # io24 state consumer requires a native version-2 record. UC 4.7.2's tagged
    # Stat sender belongs to its settled-state synchronizer; its explicit Store
    # action uses the separate PrsM library path. The 2026-09-14 inactive-slot
    # test confirmed that sending a tagged scene body does not apply that body.
    # Physical commit, front-panel recall and power-cycle survival remain
    # acceptance boundaries for the native writer too.
    #
    # The older savepreset/loadpreset files below remain host-side snapshots.
    # Because the DSP blocks are write-only, those files can contain only what
    # the driver itself set. A safe slot update instead starts from an explicit
    # complete native record so unrelated channel-strip state is preserved.

    # Keys of read_params() that describe settings rather than meters or
    # identity, i.e. the ones worth storing and restoring.
    LIVE_KEYS = ("input1Gain", "input2Gain", "hpVolume", "mainVolume",
                 "monitorMix", "input1PhantomPower", "input2PhantomPower")

    def _send_device_slot_frames(self, slot_index, frames):
        replies = []
        for frame in frames:
            replies.append(self._exec(frame))
            if len(frames) > 1:
                time.sleep(0.02)
        return {
            "slot": slot_index,
            "fragments_sent": len(frames),
            "replies_received": sum(bool(reply) for reply in replies),
        }

    def save_device_slot(self, slot_index, native_record):
        """Send one complete firmware-native version-2 ``Stat`` record.

        The returned report distinguishes frames sent from replies received;
        it does not claim parsing, nonvolatile commit, recall, or audibility
        merely because transport completed. The caller must supply the complete
        native body because the current body cannot be read back before an
        overwrite.
        """
        frames = _build_native_slot_memp_frames(slot_index, native_record)
        return self._send_device_slot_frames(slot_index, frames)

    def save_uc_device_slot(self, slot_index, slot_record):
        """Refuse the disproved tagged scene-body writer before transport."""
        _ = (slot_index, slot_record)
        raise HostActionError(
            "tagged UC Stat archives do not apply as io24 firmware 1.28 slot "
            "bodies; no device write was attempted")

    def _device_slot_identity(self):
        """Return the connected unit identity used to scope local slot evidence."""
        serial = getattr(self.dev, "serial_number", None)
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("connected io24 serial is unavailable")

        def descriptor(name):
            value = getattr(self.dev, name, None)
            if isinstance(value, bool) or not isinstance(value, int) or \
                    not 0 <= value <= 0xFFFF:
                raise ValueError("connected io24 %s is unavailable" % name)
            return "%04x" % value

        return {
            "serial": serial.strip(),
            "vendor_id": descriptor("idVendor"),
            "product_id": descriptor("idProduct"),
            "bcd_device": descriptor("bcdDevice"),
        }

    def save_known_device_slot(self, slot_index, slot_record, registry,
                               source, written_at=None,
                               sample_rate_hz=96000.0):
        """Build, send, and receipt one complete firmware-native slot body."""
        from io24_native_stat import build_native_slot_record

        native = build_native_slot_record(
            slot_record, slot_index, sample_rate_hz=sample_rate_hz)
        prepared = registry.prepare_write(
            slot_index, slot_record, source, written_at,
            device_identity=self._device_slot_identity(),
            native_record=native)
        transport = self.save_device_slot(slot_index, native)
        try:
            entry = registry.commit_sent(prepared, transport)
        except Exception as error:
            error.device_slot_transport = transport
            raise
        return {
            "transport": transport,
            "registry": entry,
            "native_sha256": hashlib.sha256(native).hexdigest(),
        }

    def save_device_library_preset(self, user_index, preset_record):
        """Send one complete UC ``MemP/PrsM`` Device Presets record.

        The reply proves only that the transport exchange returned.  The io24
        offers no command that reads the stored library body back, so callers
        must retain the returned report as ``WRITE_SENT_UNVERIFIED`` evidence.
        """
        frames = _build_device_library_memp_frames(user_index, preset_record)
        replies = []
        for frame in frames:
            replies.append(self._exec(frame))
            if len(frames) > 1:
                time.sleep(0.02)
        return {
            "user_index": user_index,
            "fragments_sent": len(frames),
            "replies_received": sum(bool(reply) for reply in replies),
        }

    def save_known_device_library_preset(
            self, user_index, preset_record, registry, source,
            written_at=None):
        """Send and locally receipt one UC Device Presets library entry."""
        prepared = registry.prepare_write(
            user_index, preset_record, source, written_at,
            device_identity=self._device_slot_identity())
        transport = self.save_device_library_preset(user_index, preset_record)
        try:
            entry = registry.commit_sent(prepared, transport)
        except Exception as error:
            error.device_preset_transport = transport
            raise
        return {"transport": transport, "registry": entry}

    def save_native_model0_voicefx_slot(
            self, slot_index, base_native_record, enabled=True, lows=0.06,
            width=0.405, mix=0.295):
        """Refuse an implicit unreadable-slot overwrite before transport."""
        _ = (slot_index, base_native_record, enabled, lows, width, mix)
        raise HostActionError(
            "construct the complete native record explicitly and acknowledge "
            "the unreadable current slot in a bounded runner; no device write "
            "was attempted")

    def save_paired_doubler_private_reverb(
            self, channel1_slot, channel1_base, channel2_slot, channel2_base,
            enabled=True, lows=0.06, width=0.405, mix=0.295,
            preset_name=None, wet_ch1=1.0, wet_ch2=1.0,
            bypass_ch1=False, bypass_ch2=False,
            custom_firmware_lane_controls=False):
        """Refuse a non-atomic duplicate write before either slot is touched."""
        raise HostActionError(
            "paired FX slot writes are not atomic and are unnecessary for the "
            "shared assigned engine; no device write was attempted")

    def snapshot(self, include_device_preset_state=False):
        """Return a Host snapshot without moving device slots on later load.

        Device preset mode and slot selection are a separate physical-device
        domain. They remain available only through the explicit opt-in so an
        ordinary Host snapshot cannot later move a channel's selected slot.
        The apparent enable is the zero/nonzero state of ``set_fx_mix`` and is
        therefore retained as ordinary processing state.
        """
        calls = _normalise_shadow(self._shadow)
        calls, quarantined = _quarantine_device_preset_calls(
            calls, include_device_preset_state)
        snap = {
            "version": 1,
            "live": {},
            "calls": calls,
            "device_preset_state_included": bool(include_device_preset_state),
            "quarantined_device_preset_calls": quarantined,
        }
        p = self.read_params()
        if p:
            for k in self.LIVE_KEYS:
                snap["live"][k] = p[k]
        return snap

    def clear_shadow(self):
        """Forget what we think we wrote — use after a power cycle."""
        self._shadow = {}
        self._send_state = None
        self._shadow_dirty = True
        self._flush_shadow(force=True)

    def replayable_shadow_count(self, include_device_preset_state=False):
        """Count cached calls that an explicit reapply would actually send."""
        calls = _normalise_shadow(self._shadow)
        replayable, _quarantined = _quarantine_device_preset_calls(
            calls, include_device_preset_state)
        return len(replayable)

    def reapply_shadow(self, include_device_preset_state=False, skip=(),
                       sample_rate_hz=None):
        """Explicitly replay cached write-only Host state after an attachment.

        Opening a USB handle is not proof that the device still contains the
        cache: it may have retained RAM under external power, or it may have
        cold-booted to defaults.  Since 2026-09-11 the GTK Host calls this on
        every connection, at the user's request, so it always resumes where
        it left off; ``skip`` names the setters the unit reports itself
        (mute, headphone mute, stereo link), so a change made on the
        hardware while the Host was closed is not undone.

        Device preset mode and selection are quarantined by default. They can
        be included only by an explicit backend opt-in; when included,
        selection runs before cached DSP state. Levels precede bus
        masters, pair balance and assignment so a route can never finish as
        "assigned but absent". Everything else retains its relative order.
        The DSP remains write-only, so a successful report means writes were
        emitted without a Host exception; it is not device readback.
        """
        before = dict(self._shadow)
        calls = _normalise_shadow(before)
        discarded = len(before) - len(calls)
        if calls != before:
            self._shadow = calls
            self._shadow_dirty = True
            self._flush_shadow(force=True)

        replay_calls, quarantined = _quarantine_device_preset_calls(
            calls, include_device_preset_state)

        # Rebuild from the normalised calls.  Otherwise a send model already
        # materialised from the pre-migration cache could retain an orphaned pan.
        self._send_state = None
        applied = 0
        by_request = 0
        skipped = []
        failed = []
        for key, call in _ordered_shadow_items(replay_calls):
            name = call.get("fn") or key.split("#", 1)[0]
            if name in skip:
                by_request += 1
                continue
            if name not in _SHADOWED:
                skipped.append(key)
                continue
            try:
                kwargs = _shadow_runtime_kwargs(
                    name, call.get("kwargs", {}), sample_rate_hz)
                getattr(self, name)(**kwargs)
                applied += 1
            except Exception as error:
                failed.append({"key": key,
                               "error": "%s: %s" %
                                        (type(error).__name__, error)})
        return {
            "requested": len(replay_calls),
            "cached": len(calls),
            "applied": applied,
            "skipped": skipped,
            "skipped_by_request": by_request,
            "failed": failed,
            "device_preset_state_included": bool(
                include_device_preset_state),
            "quarantined_device_preset_calls": quarantined,
            "discarded_legacy_entries": max(0, discarded),
            "readback": "UNAVAILABLE",
        }

    # Ordinary DSP writes do not survive a power cycle. Until the recovered
    # MemP/Stat path is validated on hardware under a separate live gate, the
    # practical Linux path remains a host preset re-applied on connection.
    STARTUP_PATH = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "io24", "startup.json")

    def save_startup(self, include_device_preset_state=False):
        """Store the current settings as the on-connect preset."""
        os.makedirs(os.path.dirname(self.STARTUP_PATH), exist_ok=True)
        return self.save_preset(
            self.STARTUP_PATH,
            include_device_preset_state=include_device_preset_state)

    def apply_startup(self, include_device_preset_state=False,
                      sample_rate_hz=None):
        """Apply the on-connect preset, if one has been saved."""
        if not os.path.exists(self.STARTUP_PATH):
            return None
        return self.load_preset(
            self.STARTUP_PATH,
            include_device_preset_state=include_device_preset_state,
            sample_rate_hz=sample_rate_hz)

    def save_preset(self, path, include_device_preset_state=False,
                    host_features=None):
        """Write device intent plus optional computer-only Host features."""
        host_features, _migrations = _normalise_host_features(host_features)
        # Keep old specialised snapshot providers source-compatible on the
        # safe default path; only the new explicit opt-in needs the keyword.
        snap = (self.snapshot(include_device_preset_state=True)
                if include_device_preset_state else self.snapshot())
        if host_features:
            snap["host_features"] = host_features
        target = os.path.abspath(os.fspath(path))
        directory = os.path.dirname(target)
        fd, temporary = tempfile.mkstemp(
            prefix=".%s." % os.path.basename(target), suffix=".tmp",
            dir=directory, text=True)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(snap, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return snap

    def load_preset(self, path, include_device_preset_state=False,
                    sample_rate_hz=None):
        """Apply a saved preset. Live 'Appl' values first, then replay writes."""
        with open(path) as fh:
            snap = json.load(fh)
        # Validate every Host-only extension before the first hardware setter.
        # A malformed computer-side effect must not leave the device half loaded.
        host_features, host_feature_migrations = _normalise_host_features(
            snap.get("host_features"))
        live = snap.get("live", {})
        if "input1Gain" in live:
            self.set_gain(1, live["input1Gain"])
        if "input2Gain" in live:
            self.set_gain(2, live["input2Gain"])
        if "hpVolume" in live:
            self.set_hp_volume(live["hpVolume"])
        if "mainVolume" in live:
            self.set_main_volume(live["mainVolume"])
        if "monitorMix" in live:
            self.set_monitor_mix(live["monitorMix"])
        for ch in (1, 2):
            key = "input%dPhantomPower" % ch
            if key in live:
                self.set_phantom(ch, bool(live[key]))
        applied = 0
        # Host files created by the broken per-input-pan build need the same
        # migration as the durable shadow; otherwise loading one can re-create
        # Channel 2 disappearing at L100 even after the live Host was repaired.
        calls = _normalise_shadow(snap.get("calls", {}))
        calls, quarantined = _quarantine_device_preset_calls(
            calls, include_device_preset_state)
        for key, call in _ordered_shadow_items(calls):
            name = call.get("fn") or key.split("#")[0]
            if name not in _SHADOWED:     # only replay setters we know we wrap
                print("  preset: skipping unknown call %r" % name)
                continue
            try:
                kwargs = _shadow_runtime_kwargs(
                    name, call.get("kwargs", {}), sample_rate_hz)
                getattr(self, name)(**kwargs)
                applied += 1
            except Exception as e:
                print("  preset: %s failed (%s)" % (key, e))
        self._last_preset_load_report = {
            "live": len(live),
            "applied": applied,
            "device_preset_state_included": bool(
                include_device_preset_state),
            "quarantined_device_preset_calls": quarantined,
            "host_features": host_features,
            "host_feature_migrations": host_feature_migrations,
        }
        return len(live), applied

    # ------------------------------------------------------------- insert FX
    # Block 201 is a singleton (blockIndex ignored). Its audio-root call,
    # selected-model delegate, and buffers are source-bound. UC 4.7.2 selects
    # and materializes a model once, then sends state-only frames for ordinary
    # control edits. Its USB capture has no separate activation command and no
    # host-invented inter-frame sleep.
    # Voice FX is distinct from shared reverb block 202. Transformer/Doubler
    # uses its own private reverb-core instance over two structural audio lanes.

    FX_MODELS = {"transformer": 0, "detuner": 1, "vocoder": 2,
                 "ringmod": 3, "filters": 4, "delay": 5}

    def send_fx(self, payloads, *, sleep_fn=time.sleep):
        """Send a list from io24_fx's set_fx_* helpers.

        Those helpers return payloads that ALREADY carry the 8-byte paesdk
        header, while _exec() prepends its own — so the header has to come off
        here. Passing one straight to _exec double-headers it, and the device
        answers normally while doing nothing at all.

        Preserve the builder order exactly. ``_exec`` is synchronous, so each
        next frame follows the prior USB reply just as in the UC 4.7.2 capture;
        the earlier 20 ms sleep was not vendor behavior.
        """
        if isinstance(payloads, (bytes, bytearray)):
            payloads = [payloads]
        frames = [bytes(payload) for payload in payloads]
        for frame in frames:
            self._exec(frame[8:])
            if FX_FRAME_INTERVAL_S:
                sleep_fn(FX_FRAME_INTERVAL_S)
        return len(frames)

    def set_fx(self, model, **kw):
        """Select/materialize a VoiceFX model or update its current state.

        UC sends ``VoFx`` only when the selected implementation changes.  A
        subsequent edit goes straight to that model's state tag. Transformer
        Lows and sample-rate edits additionally refresh its Bqdf/MBdf material.
        """
        X = _fx_mod()
        name = str(model).lower()
        if name not in self.FX_MODELS:
            raise ValueError("unknown FX model %r — one of %s"
                             % (model, "/".join(self.FX_MODELS)))
        if name == "delay":
            # Unlike coefficient-bearing models, Delay does not use ``fs`` in
            # its payload. It is still mandatory safety context: selecting the
            # model at 96 kHz caused a confirmed bootloader reset. Refuse an
            # unknown clock as well as the observed unsafe one before VoFx.
            if "fs" not in kw:
                X.validate_delay_sample_rate(None)
            X.validate_delay_sample_rate(kw["fs"])
        previous_model = getattr(self, "_voicefx_selected_model", None)
        previous_state = getattr(self, "_voicefx_selected_state", {})
        if previous_model != name:
            frames = getattr(X, "set_fx_" + name)(**kw)
        else:
            def changed(parameter, default):
                before = previous_state.get(parameter, default)
                after = kw.get(parameter, default)
                return X.f32(before) != X.f32(after)

            if name == "transformer":
                frames = X.update_fx_transformer(
                    refresh_tone=(changed("lows", 0.5) or
                                  changed("fs", 48000.0)), **kw)
            elif name == "detuner":
                frames = X.update_fx_detuner(
                    refresh_tone=changed("fs", 48000.0), **kw)
            elif name == "vocoder":
                frames = X.update_fx_vocoder(
                    refresh_filters=changed("fs", 48000.0), **kw)
            else:
                frames = getattr(X, "update_fx_" + name)(**kw)
        written = self.send_fx(frames)
        self._voicefx_selected_model = name
        self._voicefx_selected_state = dict(kw)
        return written

    def quiesce_voicefx_for_host_delay(
            self, fs, quantum=512, *, sleep_fn=time.sleep):
        """Leave block 201 on a lightweight, bypassed model.

        At 96 kHz the Linux Host runs Delay in PipeWire and must not select
        firmware model 5.  A previously selected hardware model also must not
        remain audible underneath that insert.  Transformer has no rate-scaled
        delay history, so materialize its exact UC state with its own On field
        clear.  This is an internal safety action and intentionally does not
        replace the user's shadowed Delay intent.
        """
        X = _fx_mod()
        rate = float(fs)
        if not math.isfinite(rate) or not 8000.0 <= rate <= 192000.0:
            raise ValueError("Voice FX sample rate must be 8000..192000 Hz")
        audio_settle = voicefx_audio_settle_seconds(rate, quantum)
        state = {
            "on": False, "lows": 0.5, "width": 0.5, "mix": 0.5,
            "fs": rate,
        }
        frames = X.set_fx_transformer(**state)
        selector, materialization = frames[:1], frames[1:]

        # A first VoFx request only starts block 201's 40 ms fade to bypass.
        # Once that old-rate transition is complete, replaying VoFx enters the
        # synchronous work slot that stores the model-0 delegate.  Its USB
        # reply is a transport acknowledgement, not claimed as an audio-frame
        # fence, so retain two complete old-rate audio quanta after sending the
        # complete Transformer-Off state.  Only the caller may then request the
        # new rate.
        written = self.send_fx(selector)
        sleep_fn(FX_MODEL_TRANSITION_SETTLE_S)
        written += self.send_fx(selector)
        written += self.send_fx(materialization)
        sleep_fn(audio_settle)
        self._voicefx_selected_model = "transformer"
        self._voicefx_selected_state = dict(state)
        return written

    def apply_voicefx_snapshot(self, state, sample_rate_hz=None):
        """Apply one complete UC ``voicefx`` component in source order.

        UC owns the active algorithm in its component tree: it resolves the
        saved ``__classid``, emits the block-201 ``VoFx`` selection, and only
        then pushes that selected model's state.  Reproduce that exact ordering
        without claiming the stock processing gate, persistence, or audibility.
        The device cannot supply this state through readback; ``state`` must be
        a complete known snapshot. Rate-aware models require the caller's
        current device rate. In particular, Delay never assumes 48 kHz because
        that could bypass the 96 kHz reset interlock.
        """
        X = _fx_mod()
        model, kwargs = X.voicefx_preset_call(state)
        kwargs = X.voicefx_runtime_kwargs(model, kwargs, sample_rate_hz)
        written = self.set_fx(model, **kwargs)
        return {
            "model": model,
            "enabled": kwargs["on"],
            "parameter_writes": written,
            "selection_order": "MODEL_BEFORE_STATE",
            "activation": "UNPROVED",
            "audibility": "UNPROVED",
        }

    def configure_voice_fx_intent(self, enabled, model, **kw):
        """Store model parameters without claiming that the insert is audible."""
        name = str(model).lower()
        if not enabled:
            return {"model": name, "parameter_writes": 0,
                    "audible": False, "activation": "unresolved"}
        written = self.set_fx(name, **kw)
        return {"model": name, "parameter_writes": written,
                "audible": False, "activation": "unresolved"}

    # Shared-reverb return routing. UC 4.7.2 does not call this transaction for
    # VoiceFX selection or state edits; block 201 and block 202 are independent.
    FX_RETURN_SOURCE = "fxreturn/ch1"

    def establish_effects_return(self, bus="main", return_db=0.0, channel=1,
                                 channel_mix=None):
        """Open the shared reverb feed and return.

        This is the block-202 transaction. VoiceFX block 201 does not mutate
        the mixer return in the captured UC 4.7.2 lifecycle.
        ``channel_mix`` of ``None`` leaves the channel's processing scalar
        alone, which is what a bypassed channel wants -- wire 4 is a single
        scalar, so writing it would take the channel out of bypass, and that
        changes what the main output carries.  The caller is expected to say
        why the effect stays silent instead.
        """
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        written = []
        if channel_mix is not None:
            self.set_fx_mix(channel, channel_mix)
            written.append("channel_mix")
        self.set_send_db(self.FX_RETURN_SOURCE, bus, return_db)
        self.set_send_assigned(self.FX_RETURN_SOURCE, bus, True)
        written += ["return_level", "return_assigned"]
        return {"source": self.FX_RETURN_SOURCE, "bus": self._bus(bus),
                "return_db": return_db, "channel": channel,
                "channel_mix": channel_mix, "written": written}

    def fx_on(self, channel=1, send=1.0, return_db=0.0, bus="main"):
        """Refuse an unproved standalone direct-gate write.

        No stock gate-opening path is currently established; a guessed direct
        gate write is deliberately not exposed here.
        """
        _ = channel, send, return_db, bus
        raise RuntimeError(
            "standalone direct Voice FX activation is not established; "
            "the processing gate remains unresolved")

    def fx_off(self, channel=1, bus="main"):
        """Refuse an unproved standalone direct-gate write."""
        _ = channel, bus
        raise RuntimeError(
            "standalone direct Voice FX deactivation is not established; "
            "shared reverb routing was left unchanged")

    # -------------------------------------------------------------- presets
    # The vendor model has FOUR global slot records: 0/1 belong to channel 1 and
    # 2/3 belong to channel 2 — two front-panel preset blocks per channel. Slot
    # selection and the per-channel enable are separately reachable parameters.
    #
    # Sweeping 0->1->2->3 changed only the readable slot index among the 503
    # state floats. That does not mean the device contains no preset data: its
    # DSP blocks are write-only, its factory content is independently recovered,
    # and slot records are not read back through the direct parameter resolver.

    # presetMode values, established on hardware 2026-08-10 by writing each
    # and reading the unit's own front panel:
    PRESET_MODE_SINGLE = 0        # one preset block per channel
    PRESET_MODE_DUAL = 1          # TWO blocks — what the manual documents
    PRESET_MODE_NONE = 2          # no blocks (buttons stay lit)

    def set_preset_mode(self, mode):
        """How many preset blocks the unit shows and its button cycles.

        `'Pari'` WIRE id 17 -> firmware INTERNAL id 2 (`presetMode`). Global,
        not per-channel: the handler at fw 0x2e7e2 takes no index and stores
        the raw integer (`str r5,[sp,#0x28]`), unlike the enable at wire 4
        which collapses to a boolean with `vseleq.f32`.

        Finding this took correcting a wrong assumption: internal ids are NOT
        wire ids. An earlier probe wrote wire 8 believing it was
        `presetButtonMode`(8); wire 8 is internal id 5, `muteMode`. The
        internal->wire map is only visible in the firmware's own 'Pari'
        dispatcher table (fw 0x2e49a).

        WRITE-ONLY: no slot in the 503-float state blob tracks it (swept
        both directions), so this driver cannot read the mode back — only
        command it.
        """
        if mode not in (0, 1, 2):
            raise ValueError("preset mode must be 0 (single), 1 (dual) or "
                             "2 (none)")
        self.set_param(17, mode, index=0, as_int=True)

    def set_preset_enabled(self, channel, on):
        """Boolean compatibility alias for the processing/effects mix.

        SETTLED ON HARDWARE 2026-08-10, by working the physical button while
        watching this parameter: press-and-hold on the unit toggles exactly
        this — the hold latched the bypass and the host-side switch followed
        on both channels. So id 4 is the per-channel ENABLE, as this driver
        originally said. (An intermediate rename to `set_preset_button_mode`
        followed a PROTOCOL 12K misreading; the real presetButtonMode is
        internal id 8 in the GLOBAL descriptor table and remains unprobed —
        it is the candidate for the one-versus-two-slot button mode, which
        the unit has never yet shown.)

        Firmware converts this integer wire-4 command to the same float state
        written by :meth:`set_fx_mix`: false is 0.0 (bypass), true is 1.0
        (fully processed). JaSt slot 42 bit 5 (ch1) / 6 (ch2) reports only the
        inverted zero/nonzero state, not an independent enable parameter.
        """
        return self.set_fx_mix(channel, 1.0 if on else 0.0)

    def set_preset_button_mode(self, channel, two_slots):
        """Deprecated alias for `set_preset_enabled` — kept so presets saved
        during 2026-08-10 (when the rename was briefly live) still load."""
        return self.set_preset_enabled(channel, two_slots)

    def preset_enabled(self, channel):
        """Read a channel's preset enable back from the device."""
        p = self.read_params()
        if not p:
            return None
        bit = 5 if channel == 1 else 6
        return not bool(p["flags"] >> bit & 1)

    PRESET_SLOTS = 4

    def set_preset_slot(self, channel, slot):
        """Select one of the four preset slots ('Pari' wire id 16)."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.set_param(16, max(0, min(self.PRESET_SLOTS - 1, int(slot))),
                       index=channel - 1, as_int=True)

    def device_slot_pair(self, channel):
        """The two global slot indexes a physical channel's button owns."""
        from io24_preset_record import DEVICE_SLOT_PAIRS
        if channel not in DEVICE_SLOT_PAIRS:
            raise ValueError("channel must be 1 or 2")
        return DEVICE_SLOT_PAIRS[channel]

    def recall_device_slot(self, channel, slot):
        """Select one stored slot and leave that channel processing it.

        This selects a slot index. It was measured on 2026-09-14 **not** to
        make the firmware reload the stored body: after forcing Channel 1's
        gate to its power-on state, selecting slot 0 left the Main/Input-1
        ratio at -0.294 dB, where a genuine re-apply of the stored ``MAIN``
        would have closed that gate to about -20 dB. The same session showed
        the device does apply its stored body at power-on, so storage works and
        it is the host-triggered re-apply that is missing. Do not describe this
        as a recall that reprograms the channel, and do not claim it as an FX
        activation path.

        A bypassed channel is still worth enabling, so the enable is asserted
        only when the device reports this channel bypassed.  A channel that is
        already processing keeps its exact mix, because wire id 4 is a float
        whose nonzero value is write-only: re-asserting it would silently
        force an arbitrary partial mix to full.

        The slot must be one of this channel's own two.  Pointing a channel at
        the other pair is what the 2026-08-10 button test showed produces
        half-broken states.

        Returns what was actually sent.  It does not claim audibility,
        nonvolatile commit, front-panel agreement, or that the stored body is
        what this Host believes it to be -- slot bodies have no readback.
        """
        pair = self.device_slot_pair(channel)
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise ValueError("device slot index must be an integer")
        if slot not in pair:
            raise HostActionError(
                "global slot %d is not one of Channel %d's slots %s; a "
                "cross-channel selection was refused before any write"
                % (slot, channel, " or ".join(str(index) for index in pair)))
        before = self.preset_enabled(channel)
        if before is None:
            raise HostActionError(
                "channel processing state is unreadable; refusing to recall "
                "a device slot without knowing whether the channel is bypassed")
        # Select first, then enable.  The other order would briefly run the
        # previously selected slot through a channel the caller asked to be
        # processing.
        self.set_preset_slot(channel, slot)
        if not before:
            self.set_preset_enabled(channel, True)
        return {
            "channel": channel,
            "slot": slot,
            "relative_slot": pair.index(slot),
            "slot_selected": True,
            "was_processing": bool(before),
            "enable_asserted": not before,
            "selection": "SENT_UNVERIFIED",
            "audibility": "UNPROVED",
        }

    # --------------------------------------------------------------- reverb
    # Block 202. Unlike the insert-FX slot (201) this one demonstrably works:
    # measured +2.26 dB on the FX return with the same rig that showed block 201
    # doing nothing at all. It needs the FX send open and the FX return up in a
    # bus, exactly like a classic send effect.
    REVERB_BLOCK = 202

    def set_reverb(self, on=True, size=0.5, mix=0.3, hp_freq=200.0,
                   predelay=0.02, fs=48000.0):
        """Reverb on block 202.

        size     0..1   room size
        mix      0..1   wet amount returned to the FX bus
        hp_freq  0..500 Hz high-pass on the reverb input (0 = off)
        predelay 0.0001..0.25 s
        """
        M = _meters_mod()
        blob = M.reverb_blob(on=on, size=size, mix=mix, hp_freq=hp_freq,
                             predelay=predelay, fs=fs)
        return self._dsp(SETP, self.REVERB_BLOCK, 0, blob)

    def reverb_off(self):
        return self.set_reverb(on=False, size=0.1, mix=0.0)

    # ------------------------------------------------------------------- EQ
    # The 'eq  ' block takes one 'Bqdf' blob per band (band index at blob+0x0c,
    # firmware bounds-checks 0..3). The band's *shape* is a host-side choice —
    # the device only ever sees coefficients — so this API takes the shape
    # directly.  UC 4.7.2's Standard mapper is [3, 8, 9, 6]; UI value 2 is the
    # source-bound high-shelf selector 9 used by biquad_highshelf above.
    EQ_BANDS = {"low": 0, "lowmid": 1, "himid": 2, "high": 3}

    def set_eq_band(self, channel, band, shape="peaking", freq_hz=1000.0,
                    gain_db=0.0, q=0.7, fs=48000.0):
        """Set one EQ band on a channel.

        band:  0..3, or 'low' / 'lowmid' / 'himid' / 'high'
        shape: 'peaking' | 'lowshelf' | 'highshelf' | 'hp' | 'lp' | 'off'
        Ranges follow the host descriptors: gain +-15 dB, Q 0.1..10,
        freq 20..18000 Hz.

        LOUDNESS WARNING on Q. Every combination in the accepted range is
        numerically stable (worst pole magnitude 0.99995, verified by sweeping
        the whole space in float32), but stability is not the same as sane gain:

            shape       peak gain at Q=0.7   at Q=2   at Q=10
            peaking          +15 dB          +15 dB    +15 dB
            lowshelf         +15 dB          +20 dB    +33 dB
            highshelf        +15 dB          +20 dB    +33 dB
            hp / lp            0 dB           +6 dB    +20 dB

        A shelf with Q above ~1 is a *resonant* shelf and overshoots the gain you
        asked for — at Q=10 a +15 dB shelf peaks at +33 dB. The range is kept as
        the host defines it rather than narrowed, but reach for Q<=0.7 on shelves
        and filters unless the resonance is what you want.
        """
        if isinstance(band, str):
            # the CLI hands everything over as a string, so "2" has to work as
            # well as "himid" — the usage text promises both
            band = self.EQ_BANDS.get(band.strip().lower(), band)
        try:
            band = int(band)
        except (TypeError, ValueError):
            raise ValueError("band must be 0..3 or one of %s, got %r"
                             % ("/".join(self.EQ_BANDS), band))
        band = max(0, min(3, band))
        freq_hz = max(20.0, min(18000.0, float(freq_hz)))
        gain_db = max(-15.0, min(15.0, float(gain_db)))
        q = max(0.1, min(10.0, float(q)))
        # fs is public and reaches every designer unchecked; at freq >= fs/2 the
        # poles leave the unit circle (measured: |p| = 1.019 at fs=32000,
        # freq=18000). This is the only clamp that depends on another argument.
        fs = float(fs)
        freq_hz = min(freq_hz, 0.45 * fs)
        X = _fx_mod()
        shape = shape.lower()
        if shape == "off":
            coeffs = [1.0, 0.0, 0.0, 0.0, 0.0]
        elif shape == "peaking":
            coeffs = biquad_peaking(freq_hz, gain_db, fs, q)
        elif shape == "lowshelf":
            coeffs = biquad_lowshelf(freq_hz, gain_db, fs, q)
        elif shape == "highshelf":
            coeffs = biquad_highshelf(freq_hz, gain_db, fs, q)
        elif shape == "hp":
            coeffs = X.biquad_hp2(freq_hz, fs, q)
        elif shape == "lp":
            coeffs = X.biquad_lp2(freq_hz, fs, q)
        else:
            raise ValueError("unknown shape %r" % shape)
        return self.set_biquad("eq  ", channel, coeffs, band=band)

    def set_alternate_eq(self, channel, eq, fs=48000.0, dll_path=None):
        """Apply one complete exact UC Passive or Vintage EQ state.

        All coefficients are designed and validated before the first write.
        Packet kind, width, and live index follow the corresponding UC 4.7.2
        recompute path, rather than the native preset's stored-band order.
        """
        import io24_alt_eq

        sections = io24_alt_eq.design_live_sections(
            eq, rate_hz=fs, dll_path=dll_path)
        # Validate every section before any transport.  The actual send methods
        # repeat these checks on the float32 wire values as a last-line guard.
        for kind, index, coefficients in sections:
            if kind == "wide":
                if len(coefficients) != 7 or not \
                        self._stable_denominator(coefficients):
                    raise ValueError("invalid UC alternate-EQ wide section")
            elif kind == "biquad":
                if len(coefficients) != 5 or not \
                        self._stable_denominator(coefficients):
                    raise ValueError("invalid UC alternate-EQ biquad section")
            else:
                raise ValueError("unknown UC alternate-EQ section %r" % kind)
            if index not in (0, 1, 2, 3):
                raise ValueError("alternate-EQ index must be 0..3")

        replies = []
        for kind, index, coefficients in sections:
            if kind == "wide":
                replies.append(self.set_wide_eq(
                    channel, coefficients, band=index))
            else:
                replies.append(self.set_biquad(
                    "eq  ", channel, coefficients, band=index))
        return replies

    def set_passive_eq(self, channel, eq, fs=48000.0, dll_path=None):
        """Validate and apply one complete Passive Program EQ record."""
        import io24_alt_eq
        if io24_alt_eq.model_of(eq) != "passive":
            raise ValueError("a Passive EQ record is required")
        return self.set_alternate_eq(channel, eq, fs=fs, dll_path=dll_path)

    def set_vintage_eq(self, channel, eq, fs=48000.0, dll_path=None):
        """Validate and apply one complete Vintage EQ record."""
        import io24_alt_eq
        if io24_alt_eq.model_of(eq) != "vintage":
            raise ValueError("a Vintage EQ record is required")
        return self.set_alternate_eq(channel, eq, fs=fs, dll_path=dll_path)

    def eq_off(self, channel):
        """Flatten all four EQ bands (identity biquads)."""
        for b in range(4):
            self.set_eq_band(channel, b, shape="off")

    def set_comp_eq_order(self, channel, eq_first):
        """'opt ' 'Pari' id 0 — the only wire parameter on the DSP chain.

        eq_first=False -> comp then eq (default); True -> eq then comp.
        """
        blob = struct.pack("<IIIII", PARI, 0x14, 0, 0, 1 if eq_first else 0)
        return self._dsp(SETP, OPT, channel - 1, blob)


def _fmt_floats(vals, lo=0, hi=None, per_row=6):
    hi = hi if hi is not None else len(vals)
    out = []
    for i in range(lo, hi, per_row):
        row = " ".join("[%3d]%-12.6g" % (j, vals[j])
                       for j in range(i, min(i + per_row, hi)))
        out.append("  " + row)
    return "\n".join(out)


def cmd_dump(dev):
    rsp = dev.read_state()
    if rsp is None:
        print("no valid Rply"); return
    vals = dev.floats(rsp)
    nz = [(i, v) for i, v in enumerate(vals) if v != 0.0]
    print("reply %d bytes, %d floats, %d non-zero\n" % (len(rsp), len(vals), len(nz)))
    print("non-zero float slots:")
    for i, v in nz:
        print("   [%3d] off=0x%03x  %-16.8g  raw=%s"
              % (i, DATA_START + i * 4, v,
                 struct.pack("<f", v).hex()))


def cmd_stable(dev, n=6):
    """Classify slots: VOLATILE (meters) vs STABLE (real params)."""
    runs = []
    for k in range(n):
        rsp = dev.read_state()
        if rsp is None:
            print("read %d failed" % k); continue
        runs.append(dev.floats(rsp))
        time.sleep(0.15)
    if len(runs) < 2:
        print("not enough reads"); return
    m = min(len(r) for r in runs)
    volatile, stable_nz = [], []
    for i in range(m):
        col = [r[i] for r in runs]
        if len(set(col)) > 1:
            volatile.append((i, min(col), max(col)))
        elif col[0] != 0.0:
            stable_nz.append((i, col[0]))
    print("%d reads, %d slots" % (len(runs), m))
    print("\nVOLATILE slots (live meters) — %d:" % len(volatile))
    for i, lo, hi in volatile:
        print("   [%3d] off=0x%03x  %.3e .. %.3e" % (i, DATA_START + i * 4, lo, hi))
    print("\nSTABLE non-zero slots (candidate parameters) — %d:" % len(stable_nz))
    for i, v in stable_nz:
        print("   [%3d] off=0x%03x  %-16.8g" % (i, DATA_START + i * 4, v))
    return {"volatile": [i for i, _, _ in volatile],
            "stable": {str(i): v for i, v in stable_nz}}


def cmd_snap(dev, path, n=4):
    runs = []
    for _ in range(n):
        rsp = dev.read_state()
        if rsp is not None:
            runs.append(dev.floats(rsp))
        time.sleep(0.12)
    if not runs:
        print("no reads"); return
    m = min(len(r) for r in runs)
    snap = {"n": len(runs),
            "slots": [[r[i] for r in runs] for i in range(m)]}
    json.dump(snap, open(path, "w"))
    print("snapshot -> %s (%d reads, %d slots)" % (path, len(runs), m))


def cmd_diff(a_path, b_path):
    a = json.load(open(a_path)); b = json.load(open(b_path))
    A, B = a["slots"], b["slots"]
    m = min(len(A), len(B))
    print("comparing %d slots\n" % m)
    hits = []
    for i in range(m):
        ca, cb = A[i], B[i]
        # volatile in either snapshot -> ignore (meters)
        if len(set(ca)) > 1 or len(set(cb)) > 1:
            continue
        if ca[0] != cb[0]:
            hits.append((i, ca[0], cb[0]))
    if not hits:
        print("no STABLE slot changed (only meters moved)")
    for i, va, vb in hits:
        print("  CHANGED [%3d] off=0x%03x : %-14.8g -> %-14.8g   (delta %+.6g)"
              % (i, DATA_START + i * 4, va, vb, vb - va))
    return hits


def cmd_watch(dev, seconds=90):
    """Poll state and report every STABLE slot change as it happens, so the user
    can walk the front panel and we log which slot each control drives."""
    print("watching %ds — change ONE control at a time, pausing between.\n" % seconds)
    hist = {}          # slot -> list of recent values (volatility detection)
    last = None
    seen = []
    t0 = time.monotonic()
    end = t0 + seconds
    while time.monotonic() < end:
        rsp = dev.read_state()
        if rsp is None:
            continue
        vals = dev.floats(rsp)
        for i, v in enumerate(vals):
            hist.setdefault(i, []).append(v)
            if len(hist[i]) > 6:
                hist[i].pop(0)
        if last is not None:
            m = min(len(vals), len(last))
            for i in range(m):
                if vals[i] == last[i]:
                    continue
                # ignore meters: slots that keep changing every sample
                h = hist.get(i, [])
                if len(h) >= 5 and len(set(h)) >= 4:
                    continue
                t = time.monotonic() - t0
                raw = struct.pack("<f", vals[i])
                iv, = struct.unpack("<i", raw)
                extra = "  (int %d)" % iv if 0 < abs(vals[i]) < 1e-30 else ""
                line = ("t=%5.1fs  slot[%3d] off=0x%03x : %-14.6g -> %-14.6g%s"
                        % (t, i, DATA_START + i * 4, last[i], vals[i], extra))
                print("  " + line)
                seen.append((i, last[i], vals[i]))
        last = vals
        time.sleep(0.12)
    print("\n=== slots that moved: %s ===" % sorted({i for i, _, _ in seen}))


# --- preset shadow -------------------------------------------------------
# The DSP blocks are write-only, so a preset can only replay what the driver
# itself wrote. Each stateful setter is wrapped to record its call, keyed by the
# thing it addresses — channel, band, mixer source/bus, compressor instance — so
# that per-channel and per-band settings do not overwrite one another while a
# repeated write to the same target replaces the earlier one.
#
# Only the *leaf* setters are wrapped. Convenience wrappers (mix_off, eq_off,
# compressor_off, set_mix_norm) call through to these, so the shadow always
# holds the canonical primitive call. Parameters that read back live via
# read_params() — gain, volumes, monitor mix, phantom — are deliberately not
# shadowed; snapshot() captures those from the device instead.

_ID_PARAMS = ("channel", "band", "source", "bus", "instance", "component")

# `set_mix_db` is deliberately absent: it now delegates to `set_send_db`, and
# shadowing both would file two entries for one write. Old preset files that
# still name it keep working — the method is still there, and _sends() reads
# either spelling back.
#
# Anything a user can set and cannot read back belongs here, or it silently
# disappears from every preset. `set_reverb` and the output-delay pair were
# missing until a round-trip test caught them: the effect was configured, the
# preset saved, and the reverb came back off.
#
# The same bug was found a third time (2026-08-07) in the "off" paths —
# gate_off, compressor_off and output_delay_off each wrote to the device without
# recording anything, so turning a processor OFF was invisible to a preset and
# loading one replayed the stale "on". They are shadowed now; gate_off and
# compressor_off share their setter's key via _SHADOW_ALIAS so the last call
# wins. An earlier version of this comment claimed the off paths "call through
# to these, so the shadow always holds the canonical primitive call" — that was
# never true of any of the three, and saying so is what stopped anyone checking.
_DEVICE_PRESET_STATE_CALLS = frozenset({
    "set_preset_mode", "set_preset_slot",
})


def _quarantine_device_preset_calls(calls, include_device_preset_state=False):
    """Partition physical preset selectors from ordinary Host processing."""
    if include_device_preset_state:
        return calls, 0
    replayable = {}
    quarantined = 0
    for key, call in calls.items():
        name = call.get("fn") or key.split("#", 1)[0]
        if name in _DEVICE_PRESET_STATE_CALLS:
            quarantined += 1
        else:
            replayable[key] = call
    return replayable, quarantined


_REPLAY_PHASES = {
    "set_preset_mode": 0,
    "set_preset_slot": 2,
    # UC chooses the Voice FX owner before selecting/materializing its model.
    "set_processing_channel": 10,
    "set_channel_link": 10,
    "set_send_db": 30,
    "set_mix_db": 30,
    "set_bus_master": 40,
    "set_pan": 50,
    "set_send_assigned": 60,
    "set_mirror_main": 65,
    "set_source_mute": 70,
    "set_bus_mute": 70,
}


def _ordered_shadow_items(calls):
    """Replay cached calls in dependency order while preserving peers."""
    indexed = list(enumerate(calls.items()))

    def order(item):
        position, (shadow_key, call) = item
        name = call.get("fn") or shadow_key.split("#", 1)[0]
        return _REPLAY_PHASES.get(name, 20), position

    return [entry for _position, entry in sorted(indexed, key=order)]


def _shadow_runtime_kwargs(name, raw_kwargs, sample_rate_hz):
    """Rebuild rate-sensitive FX state for the clock active during replay.

    A saved ``fs`` describes the clock when the state was written, not the
    clock after a reconnect. In particular it must never authorize a saved
    Delay selection on an unknown or newly selected 96 kHz clock.
    """
    kwargs = dict(raw_kwargs or {})
    if name != "set_fx":
        return kwargs
    model = str(kwargs.get("model", "")).lower()
    if not model:
        return kwargs
    state = dict(kwargs)
    state.pop("model", None)
    state.pop("fs", None)
    if sample_rate_hz is None:
        if model == "delay":
            _fx_mod().validate_delay_sample_rate(None)
        return kwargs
    runtime = _fx_mod().voicefx_runtime_kwargs(
        model, state, sample_rate_hz)
    return dict(runtime, model=model)


_SHADOWED = ("set_fx_mix", "set_highpass", "set_mute", "set_hp_mute",
             "set_mute_mode", "set_phones_source",
             "set_channel_link", "set_limiter", "set_compressor", "set_gate",
             "set_highpass_freq", "set_eq_band", "set_alternate_eq",
             "set_send_db",
             "set_send_assigned", "set_bus_master", "set_source_mute",
             "set_bus_mute", "set_comp_eq_order",
             "set_reverb", "set_output_delay", "set_output_delay_bus",
             "set_processing_channel", "set_pan", "set_mirror_main",
             "set_component_name",
             # the "off" paths: each bypassed its setter and so vanished
             # from saved presets, which then replayed the stale "on".
             "gate_off", "compressor_off", "output_delay_off",
             # Physical mode and slot selection remain a separate device
             # domain.  Preset enable is deliberately absent: it delegates to
             # set_fx_mix and is the same processing scalar, not a selector.
             "set_preset_mode", "set_preset_slot",
             # The insert-FX model. Its write-only state is recorded so a
             # preset saved now still describes what was asked for.
             "set_fx")


def _shadow_key(name, bound):
    """`name#<identity>` — the parameters that say *what* is being addressed."""
    parts = []
    for p in _ID_PARAMS:
        if p not in bound:
            continue
        # `source` normally identifies a mixer cell.  For the global
        # headphones selector it is the control's VALUE, so including it in
        # the key would retain Main, Mix A and Mix B as three independent
        # calls and replay all three on reconnect.
        if name == "set_phones_source" and p == "source":
            continue
        v = bound[p]
        if p == "band" and isinstance(v, str):
            v = Io24.EQ_BANDS.get(v.lower(), v)
        # Same reason as `band`: a bus reached by its UC name (`aux1`) and by
        # its native one (`mixa`) is one control, and must be one shadow entry.
        # Filing them separately loses the send model across processes.
        if p == "bus" and isinstance(v, str):
            v = Io24.BUS_ALIASES.get(v.lower(), v.lower())
        parts.append("%s=%s" % (p, v))
    return "%s#%s" % (name, ",".join(parts)) if parts else name


# An "off" method and its matching setter are ONE control, so they must share one
# shadow entry — otherwise `set_gate#channel=1` and `gate_off#channel=1` are filed
# separately, both replay on load, and the outcome depends on dict order rather
# than on which the user actually did last. Since re-calling a setter updates its
# entry in place (keeping its original position), an old "on" could replay after a
# newer "off" and switch the processor back on.
#
# Aliasing only the KEY, never the recorded `fn`, means the last call wins and
# replay still invokes the right method. These cannot simply delegate to their
# setters the way output_delay_off now does: gate_off and compressor_off send the
# firmware's byte-exact power-on blobs, which are not what set_gate(on=False) or
# set_compressor(on=False) produce.
_SHADOW_ALIAS = {"gate_off": "set_gate", "compressor_off": "set_compressor"}


def _normalise_shadow(shadow):
    """Return one canonical, internally coherent write-only Host snapshot.

    This is migration, not device inference.  It fixes two formats emitted by
    earlier Linux Host builds:

    * Boolean preset-enable calls and scalar FX-mix calls could coexist under
      two keys even though firmware maps both to one processing-mix control;
    * the GTK mixer exposed one "pan" per mono input, while the protocol only
      supports balance by attenuating the two members of a stereo pair.  A
      one-leg entry therefore means accidental mono attenuation, not a valid
      stereo balance, and must not be restored after a reconnect.

    Two matching leg entries are retained only alongside an enabled cached
    channel link.  That is the representation written by the repaired GTK
    pair-balance path and is the only state that can be replayed without
    silently attenuating independent mono inputs.
    """
    if not isinstance(shadow, dict):
        return {}

    # Old Hosts stored the Boolean alias and scalar under separate dictionary
    # keys. Updating an existing key does not change insertion order, so JSON
    # order cannot tell us which UI action happened last. Preserve explicit
    # bypass intent conservatively: false always means exact zero; true keeps a
    # known positive scalar, but overrules a conflicting stale zero as full
    # processing. New Hosts never need this arbitration because both public
    # methods land on the one scalar shadow key.
    legacy_enabled = {}
    scalar_channels = set()
    for old_key, raw_call in shadow.items():
        if not isinstance(raw_call, dict):
            continue
        kwargs = raw_call.get("kwargs", {})
        if not isinstance(kwargs, dict):
            continue
        name = raw_call.get("fn") or str(old_key).split("#", 1)[0]
        channel = kwargs.get("channel")
        if name == "set_fx_mix" and channel in (1, 2):
            scalar_channels.add(channel)
        elif name == "set_preset_enabled" and channel in (1, 2):
            legacy_enabled[channel] = bool(kwargs.get("on", True))
        elif name == "set_preset_button_mode" and channel in (1, 2):
            legacy_enabled[channel] = bool(kwargs.get("two_slots", True))

    canonical = {}
    for old_key, raw_call in shadow.items():
        if not isinstance(raw_call, dict):
            continue
        call = dict(raw_call)
        kwargs = call.get("kwargs", {})
        if not isinstance(kwargs, dict):
            kwargs = {}
        else:
            kwargs = dict(kwargs)
        name = call.get("fn") or str(old_key).split("#", 1)[0]

        if name in ("set_preset_enabled", "set_preset_button_mode"):
            channel = kwargs.get("channel")
            if channel not in (1, 2) or channel in scalar_channels:
                continue
            enabled = legacy_enabled.get(channel, True)
            call = {
                "fn": "set_fx_mix",
                "kwargs": {"channel": channel,
                           "value": 1.0 if enabled else 0.0},
            }
            canonical[_shadow_key("set_fx_mix", call["kwargs"])] = call
            continue

        if name == "set_fx_mix" and kwargs.get("channel") in legacy_enabled:
            channel = kwargs["channel"]
            enabled = legacy_enabled[channel]
            try:
                old_value = float(kwargs.get("value", 0.0))
            except (TypeError, ValueError):
                old_value = 0.0
            if not enabled:
                value = 0.0
                migration = "LEGACY_ENABLE_FALSE_FORCED_BYPASS"
            elif old_value > 0.0:
                value = old_value
                migration = None
            else:
                value = 1.0
                migration = \
                    "LEGACY_ENABLE_TRUE_OVERRULED_ZERO_MIX_INFERRED_FULL"
            call = {
                "fn": "set_fx_mix",
                "kwargs": {"channel": channel, "value": value},
            }
            if migration is not None:
                call["migration"] = migration
            canonical[_shadow_key("set_fx_mix", call["kwargs"])] = call
            continue

        key_name = _SHADOW_ALIAS.get(name, name)
        call["fn"] = name
        call["kwargs"] = kwargs
        canonical[_shadow_key(key_name, kwargs)] = call

    # Retain pan only when the partner leg has the same destination and value.
    # A value of None is a clear operation, but even that has no durable meaning
    # as a lone cached leg and can safely be discarded.
    pans = {}
    for key, call in canonical.items():
        if call.get("fn") != "set_pan":
            continue
        kwargs = call.get("kwargs", {})
        source = kwargs.get("source")
        bus = kwargs.get("bus")
        if isinstance(bus, str):
            bus = Io24.BUS_ALIASES.get(bus.lower(), bus.lower())
        if source in Io24.STEREO_PAIRS and bus in Io24.MIXER_BUSES:
            pans[(source, bus)] = (key, kwargs.get("pan"))

    link_call = canonical.get("set_channel_link", {})
    linked = bool(link_call.get("kwargs", {}).get("on", False))
    coherent = set()
    if linked:
        for (source, bus), (key, value) in pans.items():
            partner = Io24.STEREO_PAIRS[source][0]
            other = pans.get((partner, bus))
            if other is None:
                continue
            other_key, other_value = other
            if value is None and other_value is None:
                coherent.update((key, other_key))
            elif value is not None and other_value is not None:
                try:
                    if math.isclose(float(value), float(other_value),
                                    rel_tol=0.0, abs_tol=1e-9):
                        coherent.update((key, other_key))
                except (TypeError, ValueError):
                    pass

    for _pair, (key, _value) in pans.items():
        if key not in coherent:
            canonical.pop(key, None)
    return canonical


def _normalise_host_features(features):
    """Validate computer-side state and report explicit schema migrations."""
    if features is None:
        return {}, []
    if not isinstance(features, dict):
        raise ValueError("host_features must be an object")
    # reverb_character and autogain are the GTK Host's own; it checks their
    # contents when it adopts them and reports what it could not use. They
    # were saved but refused here, so a snapshot with either failed to save.
    unknown = set(features) - {"multiband", "multiband_insert", "pan",
                               "reverb_character", "autogain",
                               "standard_eq", "alternate_eq",
                               "spring_reverb"}
    if unknown:
        raise ValueError("unknown host feature%s: %s" %
                         ("" if len(unknown) == 1 else "s",
                          ", ".join(sorted(str(name) for name in unknown))))
    normalized = {}
    migrations = []
    for name in ("reverb_character", "autogain"):
        if name in features:
            if not isinstance(features[name], dict):
                raise ValueError("host feature %s must be an object" % name)
            normalized[name] = json.loads(json.dumps(features[name]))
    if "standard_eq" in features:
        import io24_presets
        normalized["standard_eq"] = \
            io24_presets.validate_standard_eq_host_state(
                features["standard_eq"])
    if "alternate_eq" in features:
        import io24_presets
        normalized["alternate_eq"] = \
            io24_presets.validate_alternate_eq_host_state(
                features["alternate_eq"])
    if "multiband" in features:
        # The retired computer-playback multiband. Still read, so its band
        # settings can seed the Multiband compressor type; never switched on.
        import io24_mbc
        legacy = (isinstance(features["multiband"], dict) and
                  features["multiband"].get("version") == 1)
        normalized["multiband"] = io24_mbc.validate_snapshot(
            features["multiband"])
        if legacy:
            migrations.append(
                "legacy Host multiband controls migrated to UC model schema v2")
    if "multiband_insert" in features:
        import io24_mbc
        raw_insert = features["multiband_insert"]
        raw_channels = (raw_insert.get("channels", {})
                        if isinstance(raw_insert, dict) else {})
        legacy = ((isinstance(raw_insert, dict) and
                   raw_insert.get("version") == 1) or
                  (isinstance(raw_channels, dict) and any(
                      isinstance(channel, dict) and channel.get("version") == 1
                      for channel in raw_channels.values())))
        normalized["multiband_insert"] = io24_mbc.validate_insert_state(
            raw_insert)
        if legacy:
            migrations.append(
                "legacy Host multiband insert migrated to UC model schema v2")
    if "spring_reverb" in features:
        import io24_spring
        legacy = (isinstance(features["spring_reverb"], dict) and
                  features["spring_reverb"].get("version") == 1)
        normalized["spring_reverb"] = io24_spring.validate_state(
            features["spring_reverb"])
        if legacy:
            migrations.append(
                "legacy Host spring return migrated to schema v2")
    if "pan" in features:
        migrations.append(
            "legacy Host bus pan was ignored; bus pan controls were removed")
    return normalized, migrations


def _shadowed(name):
    orig = getattr(Io24, name)
    sig = inspect.signature(orig)
    key_name = _SHADOW_ALIAS.get(name, name)

    def wrapper(self, *args, **kwargs):
        # Do the write FIRST. Recording before calling meant a rejected call —
        # a bad model name, an out-of-range channel — still landed in the shadow,
        # so the preset described something that never happened and failed the
        # same way on load. If the write raises, nothing is recorded.
        result = orig(self, *args, **kwargs)
        try:
            b = sig.bind(self, *args, **kwargs)
            b.apply_defaults()
            bound = dict(b.arguments)
            bound.pop("self", None)
            if name == "set_component_name":
                # The setter returns its normalized Host metadata value; keep
                # that exact value rather than the caller's surrounding space.
                bound["name"] = result
            # Standard and Passive/Vintage are mutually exclusive component
            # models on one channel. Keeping both call families would make a
            # reconnect replay whichever dictionary key happened to be older,
            # not the model the user selected last.
            channel = bound.get("channel")
            if name == "set_alternate_eq" and channel in (1, 2):
                for old_key in tuple(self._shadow):
                    if old_key.startswith(
                            "set_eq_band#channel=%s," % channel):
                        self._shadow.pop(old_key, None)
            elif name == "set_eq_band" and channel in (1, 2):
                self._shadow.pop(
                    "set_alternate_eq#channel=%s" % channel, None)
            self._shadow[_shadow_key(key_name, bound)] = {
                "fn": name, "kwargs": _jsonable(bound)}
            self._shadow_dirty = True
            self._flush_shadow()
        except Exception:
            pass                      # a preset is never worth failing a write
        return result

    wrapper.__name__ = name
    wrapper.__doc__ = orig.__doc__
    setattr(Io24, name, wrapper)


def _jsonable(d):
    """Flatten **kw catch-alls and drop anything json cannot represent."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):        # set_compressor(**kw)
            if k == "eq":
                # Alternate EQ is one semantic object. Flattening it into the
                # call kwargs would make a saved shadow unreplayable.
                out[k] = _jsonable(v)
            else:
                out.update(_jsonable(v))
        elif v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)
    return out


for _n in _SHADOWED:
    _shadowed(_n)


def _bool(s):
    v = str(s).strip().lower()
    if v in ("1", "on", "true", "yes", "y"):
        return True
    if v in ("0", "off", "false", "no", "n"):
        return False
    raise SystemExit("expected on/off, got %r" % s)


def _ch(s):
    c = int(s)
    if c not in (1, 2):
        raise SystemExit("channel must be 1 or 2")
    return c


def cmd_meters(dev, seconds=10.0):
    """Live input levels and chain gain-reduction."""
    print("  %-8s %-10s %-10s   %-22s" % ("", "in1", "in2", "reduction (gate/comp/lim)"))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        p = dev.read_params()
        if p is None:
            continue

        def dbfs(v):
            return "-inf " if v <= 1e-9 else "%6.1f" % (20 * math.log10(v))

        red = []
        for blk in ("gate", "comp", "lim "):
            r = dev.read_reduction(blk, 1)
            red.append("%s %s" % (blk.strip(), "-" if not r else "%.3f" % min(r)))
        print("  %-8s %-10s %-10s   %s" % (
            "ch1/ch2", dbfs(p["input1Level"]), dbfs(p["input2Level"]), "  ".join(red)))
        time.sleep(0.4)


CONTROL_USAGE = """control commands:
  status                       show all named parameters
  meters [secs]                live levels + gain reduction
  gain <ch> <dB>               preamp gain, 0..60
  hpvol <0..1>                 headphone volume
  mainvol <0..1>               main output volume
  blend <-1..1>                monitor blend
  phantom <ch> <on|off>        48V
  mute <ch> <on|off>           input mute
  hpmute <on|off>              headphone mute
  phonesrc <main|mixa|mixb>    source feeding the headphones (write-only)
  link <on|off>                stereo link the inputs
  hpf <ch> <on|off>            'Appl' high-pass enable
  hpfreq <ch> <Hz>             'filt' high-pass cutoff, 24..1000 (24 = bypass)
  limiter <ch> <on|off> [dBFS] limiter (default -28 dBFS)
  order <ch> <comp|eq>         which of comp/eq runs first
  eq <ch> <band> <shape> <Hz> [dB] [Q]
                               band: low|lowmid|himid|high (or 0..3)
                               shape: peaking|lowshelf|highshelf|hp|lp|off
  eqflat <ch>                  flatten all four EQ bands
  send <source> <bus> <dB|off> a source's send level in a bus
  assign <source> <bus> <on|off>
                               take a source in/out of a bus, keeping its level
  busmaster <bus> <dB>         trim every send in a bus at once
  pan <source> <bus> <pos>     balance a stereo pair in a bus
                               pos: 0..1, or left|centre|right, or off
                               (host-side, UC's pan law; pannable sources
                                are line/ch1+2 and return/ch1+2 only)
  mirror <bus>                 copy the main mix's levels into an aux bus
  procchan [<ch> <input>]      which input feeds a channel's DSP chain
                               (setting one swaps both — it is a permutation)
  delay <ms> [bus]             output delay 0..500 ms (bus: mixa|mixb|off)
  delay off                    clear the output delay
  bus <bus>                    what this driver believes is in a bus
                               source: line/ch1..3, return/ch1..3, fxreturn/ch1
                               bus:    main | mixa (aux1) | mixb (aux2)
  savepreset <file.json>       save current settings (see below)
  loadpreset <file.json>       apply a saved preset
  presetinfo <file.json>       inspect saved reverb/Voice FX without a device
  saveprivatereverb ...         retired safety stub; refuses the obsolete
                               paired tagged-record operation before writing
  startup [save|apply|clear]   a preset re-applied whenever the device
                               appears (host-side)
  names                        the device's channel-name table ('CHNP')
  shadow [clear]               show (or reset) the write mirror

savepreset/loadpreset files are host-side. The old saveprivatereverb command is
retained only as a fail-closed compatibility stub because it duplicated Voice
FX into two slots instead of reproducing UC's one-channel assignment workflow.
Tagged scene records are not sent as io24 slot bodies. The byte-oriented native
writer is available only to bounded tooling with a complete version-2 record.

A host preset holds the live 'Appl' values the device can report plus the
driver's mirror of the DSP writes it has made — kept in
~/.cache/io24/shadow.json so it survives commands.

Two consequences. Loading a preset applies what is in it; it does not reset
settings the preset never mentioned. And the mirror is a claim about what was
last sent, not a reading: after a power cycle, or after Universal Control has
touched the device from another host, run `shadow clear`.
"""


def run_control(dev, cmd, a):
    if cmd == "gain":
        dev.set_gain(_ch(a[0]), float(a[1]))
    elif cmd == "hpvol":
        dev.set_hp_volume(float(a[0]))
    elif cmd == "mainvol":
        dev.set_main_volume(float(a[0]))
    elif cmd == "blend":
        dev.set_monitor_mix(float(a[0]))
    elif cmd == "phantom":
        dev.set_phantom(_ch(a[0]), _bool(a[1]))
    elif cmd == "mute":
        dev.set_mute(_ch(a[0]), _bool(a[1]))
    elif cmd == "hpmute":
        dev.set_hp_mute(_bool(a[0]))
    elif cmd == "phonesrc":
        dev.set_phones_source(a[0])
    elif cmd == "link":
        dev.set_channel_link(_bool(a[0]))
    elif cmd == "hpf":
        dev.set_highpass(_ch(a[0]), _bool(a[1]))
    elif cmd == "hpfreq":
        dev.set_highpass_freq(_ch(a[0]), float(a[1]))
    elif cmd == "limiter":
        ch, on = _ch(a[0]), _bool(a[1])
        dev.set_limiter(ch, on, float(a[2]) if len(a) > 2 else -28.0)
    elif cmd == "order":
        dev.set_comp_eq_order(_ch(a[0]), str(a[1]).lower().startswith("eq"))
    elif cmd == "eq":
        dev.set_eq_band(_ch(a[0]), a[1], a[2], float(a[3]),
                        float(a[4]) if len(a) > 4 else 0.0,
                        float(a[5]) if len(a) > 5 else 0.7)
    elif cmd == "eqflat":
        dev.eq_off(_ch(a[0]))
    elif cmd == "send":
        dev.set_send_db(a[0], a[1], None if a[2].lower() == "off" else float(a[2]))
    elif cmd == "assign":
        dev.set_send_assigned(a[0], a[1], _bool(a[2]))
    elif cmd == "busmaster":
        dev.set_bus_master(a[0], float(a[1]))
    elif cmd == "pan":
        # `pan <source> <bus> <0..1|centre|left|right|off>` — pans the whole
        # stereo pair, which is what a pan control should do. Use the API's
        # set_pan directly to move one leg on its own.
        word = {"left": 0.0, "l": 0.0, "centre": 0.5, "center": 0.5, "c": 0.5,
                "right": 1.0, "r": 1.0}
        v = a[2].lower()
        pos = None if v in ("off", "none") else word.get(v)
        if pos is None and v not in ("off", "none"):
            pos = float(a[2])
        dev.set_pair_pan(a[0], a[1], pos)
        if pos is None:
            print("pan cleared on %s and its partner in %s" % (a[0], a[1]))
        else:
            side = dev.STEREO_PAIRS[a[0]][1]
            print("%s pan %.2f  (%s leg %+.2f dB, partner %+.2f dB)"
                  % (a[0], pos, side, dev.pan_db(pos, side),
                     dev.pan_db(pos, "right" if side == "left" else "left")))
    elif cmd == "mirror":
        print("copied %d send(s) from the main mix" % dev.mirror_main(a[0]))
    elif cmd == "procchan":
        if len(a) < 2:
            print("ch1 <- input %s   ch2 <- input %s"
                  % (dev.processing_channel(1), dev.processing_channel(2)))
        else:
            dev.set_processing_channel(_ch(a[0]), int(a[1]))
            time.sleep(0.4)
            print("ch1 <- input %s   ch2 <- input %s"
                  % (dev.processing_channel(1), dev.processing_channel(2)))
    elif cmd == "delay":
        if a and a[0].lower() in ("off", "none"):
            dev.output_delay_off(); print("output delay off")
        else:
            ms = float(a[0])
            dev.set_output_delay(ms / 1000.0, a[1] if len(a) > 1 else None)
            print("output delay %.0f ms on %s" % (ms, a[1] if len(a) > 1 else "current bus"))
    elif cmd == "bus":
        s = dev.bus_summary(a[0])
        print("%s  master %+.1f dB" % (s["bus"], s["master"]))
        for src, v in sorted(s["sends"].items()):
            print("  %-14s %8s  %s" % (src,
                  "off" if v["db"] is None else "%.1f dB" % v["db"],
                  "" if v["assigned"] else "(unassigned)"))
        if not s["sends"]:
            print("  (nothing set by this driver — the block cannot be read back)")
    else:
        return False
    return True


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__); print(CONTROL_USAGE); return
    cmd = args[0]
    if cmd == "diff":
        cmd_diff(args[1], args[2]); return
    if cmd == "presetinfo":
        if len(args) < 2:
            raise SystemExit("presetinfo needs a preset JSON file")
        effects = inspect_preset(args[1])
        reverb = effects["reverb"]
        if reverb is None:
            print("Reverb: not saved")
        else:
            state = "on" if reverb.get("on", True) else "off"
            values = " ".join("%s=%s" % item for item in reverb.items()
                              if item[0] != "on")
            print("Reverb: %s%s" % (state, "  " + values if values else ""))
        voice_fx = effects["voice_fx"]
        if voice_fx is None:
            print("Voice FX: not saved")
        else:
            model = voice_fx.get("model", "unknown")
            values = " ".join("%s=%s" % item for item in voice_fx.items()
                              if item[0] != "model")
            print("Voice FX: %s%s  (stored intent; direct playback not established)"
                  % (model, "  " + values if values else ""))
        mixes = effects["processing_mix"]
        print("Processing / effects mix: %s" % (
            " ".join("ch%d=%s" % item for item in sorted(mixes.items()))
            if mixes else "not saved"))
        returns = effects["fx_returns"]
        rendered_returns = []
        for bus, route in sorted(returns.items()):
            fader = route["fader_db"]
            effective = route["effective_db"]
            if not route["fader_saved"]:
                fader_text = "not saved"
            elif fader is None:
                fader_text = "off"
            else:
                fader_text = "%s dB" % fader
            effective_text = ("%s dB" % effective
                              if route["effective_state"] == "level"
                              else route["effective_state"])
            rendered_returns.append(
                "%s=fader %s, %s, effective %s, master %s dB" % (
                    bus,
                    fader_text,
                    "assigned" if route["assigned"] else "unassigned",
                    effective_text,
                    route["master_db"],
                ))
        print("FX returns: %s" % ("; ".join(rendered_returns)
                                   if rendered_returns else "not saved"))
        return
    dev = Io24()
    try:
        print("io24 connected: protocol=%d maxCmd=%d maxRsp=%d\n"
              % (dev.proto, dev.max_cmd, dev.max_rsp))
        if cmd == "dump":
            cmd_dump(dev)
        elif cmd == "status":
            p = dev.read_params()
            if p is None:
                print("read failed"); return
            print("  ch1 gain      %6.2f dB      ch2 gain      %6.2f dB"
                  % (p["input1Gain"], p["input2Gain"]))
            print("  headphone     %6.3f         main          %6.3f"
                  % (p["hpVolume"], p["mainVolume"]))
            print("  monitor blend %6.3f" % p["monitorMix"])
            print("  phantom       ch1=%-5s     ch2=%-5s"
                  % (p["input1PhantomPower"], p["input2PhantomPower"]))
            print("  levels        ch1=%.3e   ch2=%.3e"
                  % (p["input1Level"], p["input2Level"]))
        elif cmd == "stable":
            cmd_stable(dev, int(args[1]) if len(args) > 1 else 6)
        elif cmd == "snap":
            cmd_snap(dev, args[1])
        elif cmd == "watch":
            cmd_watch(dev, int(args[1]) if len(args) > 1 else 90)
        elif cmd == "meters":
            cmd_meters(dev, float(args[1]) if len(args) > 1 else 10.0)
        elif cmd == "startup":
            act = args[1] if len(args) > 1 else "show"
            if act == "save":
                snap = dev.save_startup()
                print("  startup preset saved: %d live values, %d settings"
                      % (len(snap["live"]), len(snap["calls"])))
                print("  -> %s" % dev.STARTUP_PATH)
                print("  install the unit to have it applied on connect:")
                print("     systemd/io24-startup.service")
            elif act == "apply":
                r = dev.apply_startup()
                print("  no startup preset saved" if r is None else
                      "  applied: %d live values, %d settings replayed" % r)
            elif act == "clear":
                if os.path.exists(dev.STARTUP_PATH):
                    os.unlink(dev.STARTUP_PATH); print("  cleared")
                else:
                    print("  nothing to clear")
            else:
                ex = os.path.exists(dev.STARTUP_PATH)
                print("  startup preset: %s" % (dev.STARTUP_PATH if ex else "none saved"))
                print("  save one with:  io24.py startup save")
        elif cmd == "names":
            names = dev.channel_names()
            if not names:
                print("  device returned no channel names")
            for i, n in sorted(names.items()):
                print("  %2d  %s" % (i, n))
        elif cmd == "savepreset":
            snap = dev.save_preset(args[1])
            print("  saved %s: %d live values, %d recorded settings"
                  % (args[1], len(snap["live"]), len(snap["calls"])))
            if not snap["calls"]:
                print("  (no replayable DSP writes are in the durable host mirror;")
                print("   adjust settings through this driver, then save again)")
        elif cmd == "loadpreset":
            n_live, n_calls = dev.load_preset(args[1])
            print("  loaded %s: %d live values, %d settings replayed"
                  % (args[1], n_live, n_calls))
        elif cmd == "saveprivatereverb":
            raise SystemExit(
                "saveprivatereverb is retired: a paired write does not "
                "reproduce UC's single-channel Voice FX assignment; no slot "
                "write attempted")
        elif cmd == "shadow":
            if len(args) > 1 and args[1] == "clear":
                dev.clear_shadow()
                print("  write mirror cleared (%s)" % SHADOW_PATH)
            elif not dev._shadow:
                print("  write mirror is empty — no DSP settings recorded")
            else:
                print("  DSP settings this driver believes it has written:")
                for key, call in dev._shadow.items():
                    kw = call.get("kwargs", {})
                    print("    %-36s %s" % (key, ", ".join(
                        "%s=%s" % (k, v) for k, v in kw.items()
                        if k not in _ID_PARAMS and k != "fs")))
        elif run_control(dev, cmd, args[1:]):
            # writes are fire-and-forget; read back what we can to confirm
            time.sleep(0.3)
            p = dev.read_params()
            if p:
                print("  ch1 gain %.1f dB  ch2 gain %.1f dB  hp %.3f  main %.3f  "
                      "blend %+.3f  48V %s/%s"
                      % (p["input1Gain"], p["input2Gain"], p["hpVolume"],
                         p["mainVolume"], p["monitorMix"],
                         p["input1PhantomPower"], p["input2PhantomPower"]))
        else:
            print("unknown command %r\n" % cmd); print(CONTROL_USAGE)
    finally:
        dev.close()


if __name__ == "__main__":
    main()
