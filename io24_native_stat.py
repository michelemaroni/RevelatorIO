#!/usr/bin/env python3
"""Pure parser and encoder for firmware 1.28 native ``Stat`` records.

The io24 firmware keeps its four device-slot defaults in a version-2 chunk
container.  This is distinct from Universal Control's tagged ``{...}`` preset
archive used by the adjacent factory/library collection.  This module is
local-only: it has no transport, USB, audio, or device access.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import math
import struct


NATIVE_STAT_VERSION = 2
CHANNEL_STRIP_KEY = b"opt "
VOICEFX_COMPONENT_ID = 201
VOICEFX_COMPONENT_KEY = VOICEFX_COMPONENT_ID.to_bytes(4, "big")
VOICEFX_PARENT_VERSION = 6
VOICEFX_MODEL0 = 0
VOICEFX_MODEL_DELAY = 5

# A native model leaf is a four-byte length followed by exactly the live
# control blob's payload — everything after that message's 12-byte header.
# Proven byte-for-byte against the firmware's own default Channel-1 slot
# records, which ship a Delay: the record at firmware offset 0x68070 carries
# component 201 with selected_model 5 and the 16-byte state
# ``000000000000003fcdcccc3d5c8f423e``, which is exactly
# ``io24_fx.fx_delay(on=False, time_s=0.19, feedback=0.2, mix=0.5)[12:]``.
# Model 0's 20-byte leaf follows the same rule against ``godv``.
#
# Auxiliary blobs a model also pushes live (Transformer's two ``Bqdf`` shelves,
# the De-Tuner's low-pass, the Vocoder's ``inia`` bank) are NOT in the leaf.
NATIVE_MODEL_STATE_BYTES = {
    VOICEFX_MODEL0: 20,          # 'godv' 0x20 - 12
    VOICEFX_MODEL_DELAY: 16,     # 'vech' 0x1c - 12
}
CHANNEL_COMPONENT_KEYS = (b"filt", b"gate", b"comp", b"eq  ", b"lim ")

# All four firmware 1.28 defaults use these fixed stock component-state sizes.
# They are validation facts, not claimed storage maxima.
CHANNEL_COMPONENT_SIZES = {
    b"filt": 120,
    b"gate": 128,
    b"comp": 228,
    b"eq  ": 432,
    b"lim ": 28,
}

# Firmware 1.28's stock Channel-2 no-FX slot is the smallest complete neutral
# structural base.  Unknown structural bytes must be carried, not invented.
# Source: io24_fw.bin @ 0x69ab0, 0x404 bytes.
_STOCK_NATIVE_STAT_SHA256 = \
    "84201c25f28bbc2590e172ebc477392dcc2615c117bfcf565c6b4bd7453bd4be"
_STOCK_NATIVE_STAT_B64 = """
AgAAAPgDAAAAAAAAb3B0IOwDAAAAAAAA5AMAAAAAAABmaWx0eAAAAAAAAABnYXRlgAAAAAAAAABj
b21w5AAAAAAAAABlcSAgsAEAAAAAAABsaW0gHAAAAAAAAAAEAAAAAACAPwAAAAAAAAAAAAAAAAAA
AAABAEQsRwAAAAAAAIA/AAAAAAAAAAAAAAAAAAAAAAEAgDtHAAAAAAAAgD8AAAAAAAAAAAAAAAAA
AAAAAQBErEcAAAAAAACAPwAAAAAAAAAAAAAAAAAAAAABAIC7RwAAAAAAAAAAeAAAAOpaizu/UP4/
AAAAAJTSfb/qWou7bxKDOgAAekRCYGU8hesRP8XJdT+Cvn8/GTEAAAAAAAABAAAAAAAAAAAAgD8A
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAuAsAAAAAAAABAAAAAAAAAAQA
AAAlQ10/XEHivlxBYj5qoDQ+Y67nvgEARCxHAAAAADX8WT/FeKm+xXgpPk5MCj5+i9a+AQCAO0cA
AAAAGA9CPw3IDz8NyI++B78qvt3cMr4BAESsRwAAAABx0T4/ZMErP2TBq75Jm1S+H/gQvgEAgLtH
AAAAAGgAAAAlQ10/XUHivl1BYj5ooDQ+Y67nvoXrUT1cj8I+iCJVPwAAQEAAAPLBlsQXQAEAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgD8AAAAAAAAAAAQA
AAAWWHs/mIvqP4QO6b+Q8VW/ySxZPwAAgD8AAAAAAAAAAAAAAAAAAAAAAACAPwAAAAAAAAAAAAAA
AAAAAAAHAEQsRwEAAADskns/gC/sP2m/6r+UGFm/SPdbPwAAgD8AAAAAAAAAAAAAAAAAAAAAAACA
PwAAAAAAAAAAAAAAAAAAAAAHAIA7RwEAAAA05Xw/y+r0P+iq87+ODmq/QhFrPwAAgD8AAAAAAAAA
AAAAAAAAAAAAAACAPwAAAAAAAAAAAAAAAAAAAAAHAESsRwEAAABMCX0/Qsr1P02N9L/FxGu/Ypls
PwAAgD8AAAAAAAAAAAAAAAAAAAAAAACAPwAAAAAAAAAAAAAAAAAAAAAHAIC7RwEAAAAEAAAATaOD
P8KZsz4m+qu+JUYcPm52Ob7fviC+zoURPgEARCxHAAAAAPR/gz98z+o+nOnjvu1ECz7+Wie+lj8k
vrJ8Fj4BAIA7RwAAAAApcII/sHSUP4Tik79mgF2+ot5JPtPD9r2e2e09AQBErEcAAAAAxEuCPwWd
nz/IKp+/rAaVvlTGiz4UDNW9WSbOPQEAgLtHAAAAABgAAAABAAAAAACAP6nofz8AAAAA1bCqPwAA
AAA=
"""


class NativeStatFormatError(ValueError):
    """A byte string is not a structurally eligible native ``Stat`` record."""


def stock_native_stat_record():
    """Return the pinned complete no-FX firmware-native structural base."""
    raw = base64.b64decode(_STOCK_NATIVE_STAT_B64)
    if len(raw) != 0x404 or \
            hashlib.sha256(raw).hexdigest() != _STOCK_NATIVE_STAT_SHA256:
        raise NativeStatFormatError("embedded native Stat base failed its pin")
    return validate_native_stat_record(raw)


@dataclass(frozen=True)
class NativeChunk:
    """One four-byte-keyed payload in a native chunk group."""

    key: bytes
    payload: bytes


@dataclass(frozen=True)
class NativeStatRecord:
    """Decoded top-level native device-slot record."""

    version: int
    chunks: tuple[NativeChunk, ...]


def _as_bytes(name, value):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("%s must be bytes-like" % name)
    return bytes(value)


def _checked_chunks(chunks):
    checked = []
    for chunk in chunks:
        if isinstance(chunk, NativeChunk):
            key, payload = chunk.key, chunk.payload
        else:
            try:
                key, payload = chunk
            except (TypeError, ValueError) as error:
                raise TypeError(
                    "each native chunk must be NativeChunk or a key/payload pair"
                ) from error
        key = _as_bytes("native chunk key", key)
        payload = _as_bytes("native chunk payload", payload)
        if len(key) != 4:
            raise NativeStatFormatError("native chunk keys must be four bytes")
        checked.append(NativeChunk(key, payload))
    return tuple(checked)


def encode_chunk_group(chunks):
    """Encode one directory followed by its ordered payloads."""
    chunks = _checked_chunks(chunks)
    body_size = sum(12 + len(chunk.payload) for chunk in chunks)
    directory = b"".join(
        chunk.key + struct.pack("<Q", len(chunk.payload)) for chunk in chunks
    )
    return (struct.pack("<Q", body_size) + directory +
            b"".join(chunk.payload for chunk in chunks))


def decode_chunk_group(data):
    """Decode one exact native chunk group without interpreting leaf payloads.

    The format has no explicit directory-entry count.  The unique count is the
    one for which ``directory bytes + declared payload sizes`` consumes the
    group's declared extent exactly.
    """
    data = _as_bytes("native chunk group", data)
    if len(data) < 8:
        raise NativeStatFormatError("native chunk group is shorter than its header")
    declared_size, = struct.unpack_from("<Q", data)
    if declared_size != len(data) - 8:
        raise NativeStatFormatError(
            "native chunk group declares %d bytes, has %d" %
            (declared_size, len(data) - 8))

    candidates = []
    maximum = min(64, (len(data) - 8) // 12)
    for count in range(maximum + 1):
        directory_end = 8 + 12 * count
        sizes = [
            struct.unpack_from("<Q", data, 8 + 12 * index + 4)[0]
            for index in range(count)
        ]
        if directory_end + sum(sizes) == len(data):
            candidates.append((count, sizes))
    if len(candidates) != 1:
        raise NativeStatFormatError(
            "native chunk directory has %d possible entry counts" %
            len(candidates))

    count, sizes = candidates[0]
    payload_offset = 8 + 12 * count
    chunks = []
    for index, size in enumerate(sizes):
        key_offset = 8 + 12 * index
        key = data[key_offset:key_offset + 4]
        payload_end = payload_offset + size
        chunks.append(NativeChunk(key, data[payload_offset:payload_end]))
        payload_offset = payload_end
    return tuple(chunks)


def encode_native_stat_record(record):
    """Re-encode a decoded native record byte-for-byte."""
    if not isinstance(record, NativeStatRecord):
        raise TypeError("record must be a NativeStatRecord")
    if record.version != NATIVE_STAT_VERSION:
        raise NativeStatFormatError(
            "native Stat version must be %d" % NATIVE_STAT_VERSION)
    return struct.pack("<I", record.version) + encode_chunk_group(record.chunks)


def decode_native_stat_record(data):
    """Decode the version and top-level chunk directory of one record."""
    data = _as_bytes("native Stat record", data)
    if len(data) < 12:
        raise NativeStatFormatError("native Stat record is shorter than its header")
    version, = struct.unpack_from("<I", data)
    if version != NATIVE_STAT_VERSION:
        raise NativeStatFormatError(
            "native Stat record starts with version %d, expected %d" %
            (version, NATIVE_STAT_VERSION))
    return NativeStatRecord(version, decode_chunk_group(data[4:]))


def _unique_by_key(name, chunks):
    result = {}
    for chunk in chunks:
        if chunk.key in result:
            raise NativeStatFormatError(
                "%s repeats chunk key %s" % (name, chunk.key.hex()))
        result[chunk.key] = chunk.payload
    return result


def validate_native_stat_record(data, slot_index=None):
    """Return structurally valid firmware startup/default chunk bytes.

    Validation intentionally stops at the stock component-state boundary.  It
    proves the native version, chunk directories, component identities, and
    stock leaf sizes; it does not claim that the leaf values are safe, audible,
    accepted by a device, or nonvolatile.  ``slot_index`` checks only the
    four-slot index range.  The contents of four embedded defaults are
    observations, not a rule forbidding Voice FX in another channel's saved
    record.
    """
    if slot_index is not None:
        if (isinstance(slot_index, bool) or not isinstance(slot_index, int) or
                not 0 <= slot_index <= 3):
            raise ValueError("device slot index must be in the range 0..3")
    data = _as_bytes("native Stat record", data)
    record = decode_native_stat_record(data)
    top = _unique_by_key("native Stat top level", record.chunks)
    allowed = {CHANNEL_STRIP_KEY, VOICEFX_COMPONENT_KEY}
    unknown = sorted(set(top).difference(allowed))
    if unknown:
        raise NativeStatFormatError(
            "native Stat has unsupported top-level chunks: %s" %
            ", ".join(key.hex() for key in unknown))
    if CHANNEL_STRIP_KEY not in top:
        raise NativeStatFormatError("native Stat lacks the channel-strip chunk")

    channel_chunks = decode_chunk_group(top[CHANNEL_STRIP_KEY])
    channel = _unique_by_key("native channel strip", channel_chunks)
    missing = [key for key in CHANNEL_COMPONENT_KEYS if key not in channel]
    extra = sorted(set(channel).difference(CHANNEL_COMPONENT_KEYS))
    if missing or extra:
        details = []
        if missing:
            details.append("missing %s" % ",".join(
                key.decode("ascii") for key in missing))
        if extra:
            details.append("unsupported %s" % ",".join(
                key.hex() for key in extra))
        raise NativeStatFormatError(
            "native channel-strip chunks differ: %s" % "; ".join(details))
    for key, expected_size in CHANNEL_COMPONENT_SIZES.items():
        if len(channel[key]) != expected_size:
            raise NativeStatFormatError(
                "native %s chunk has %d bytes, expected %d" %
                (key.decode("ascii"), len(channel[key]), expected_size))

    if VOICEFX_COMPONENT_KEY in top:
        voicefx = top[VOICEFX_COMPONENT_KEY]
        if len(voicefx) < 16:
            raise NativeStatFormatError("native Voice FX chunk is truncated")
        # The first two words are the parent-state version and selected model.
        parent_version, selected_model = struct.unpack_from("<II", voicefx)
        if parent_version != VOICEFX_PARENT_VERSION:
            raise NativeStatFormatError(
                "native Voice FX parent version is %d, expected %d" %
                (parent_version, VOICEFX_PARENT_VERSION))
        if not 0 <= selected_model <= 5:
            raise NativeStatFormatError(
                "native Voice FX model %d is outside 0..5" % selected_model)
        model_chunks = _unique_by_key(
            "native Voice FX model", decode_chunk_group(voicefx[8:]))
        selected_key = selected_model.to_bytes(4, "big")
        if selected_key not in model_chunks:
            raise NativeStatFormatError(
                "native Voice FX selected model %d has no state chunk" %
                selected_model)
        if len(model_chunks) != 1:
            raise NativeStatFormatError(
                "native Voice FX must contain only its selected model state")
        model_state = model_chunks[selected_key]
        if len(model_state) < 4:
            raise NativeStatFormatError(
                "native Voice FX model state lacks its byte-count prefix")
        state_bytes, = struct.unpack_from("<I", model_state)
        if state_bytes != len(model_state) - 4:
            raise NativeStatFormatError(
                "native Voice FX model state declares %d bytes, has %d" %
                (state_bytes, len(model_state) - 4))

        expected_state = NATIVE_MODEL_STATE_BYTES.get(selected_model)
        if expected_state is not None and state_bytes != expected_state:
            raise NativeStatFormatError(
                "native Voice FX model %d state must contain %d bytes, has %d"
                % (selected_model, expected_state, state_bytes))

        if selected_model == VOICEFX_MODEL_DELAY:
            enabled, mix, half_feedback, time_s = struct.unpack_from(
                "<Ifff", model_state, 4)
            if enabled not in (0, 1):
                raise NativeStatFormatError(
                    "native Voice FX delay enable is not boolean")
            if not all(math.isfinite(value) for value in
                       (mix, half_feedback, time_s)):
                raise NativeStatFormatError(
                    "native Voice FX delay controls are not finite")
            if not 0.0 <= mix <= 1.0:
                raise NativeStatFormatError(
                    "native Voice FX delay mix is outside 0..1")
            # The blob stores half the feedback, so a full 1.0 is 0.5 here.
            if not 0.0 <= half_feedback <= 0.5:
                raise NativeStatFormatError(
                    "native Voice FX delay feedback is outside 0..1")
            if not 0.0 < time_s <= 0.25:
                raise NativeStatFormatError(
                    "native Voice FX delay time is outside 0..0.25 s")

        if selected_model == VOICEFX_MODEL0:
            enabled, lows, width, lows_mirror, mix = struct.unpack_from(
                "<Iffff", model_state, 4)
            if enabled not in (0, 1):
                raise NativeStatFormatError(
                    "native Voice FX model 0 enable is not boolean")
            if not all(math.isfinite(value) and 0.0 <= value <= 1.0
                       for value in (lows, width, lows_mirror, mix)):
                raise NativeStatFormatError(
                    "native Voice FX model 0 controls are outside 0..1")
            if struct.pack("<f", lows) != struct.pack("<f", lows_mirror):
                raise NativeStatFormatError(
                    "native Voice FX model 0 lows mirror differs")

    # Re-encoding at both levels makes the no-hidden-prefix claim executable.
    if encode_native_stat_record(record) != data:
        raise NativeStatFormatError("native Stat record does not round-trip")
    return data


def _unit_float(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("%s must be a real number" % name)
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("%s must be finite and in the range 0..1" % name)
    # Canonicalize now so the mirrored value is bit-identical on the wire.
    return struct.unpack("<f", struct.pack("<f", value))[0]


def encode_native_model0_state(enabled=True, lows=0.06, width=0.405,
                               mix=0.295):
    """Encode model 0's native leaf: byte count plus five stock dwords.

    Firmware serializer ``0x53680`` emits 20 bytes from ``model+0x340``.
    Those words are ``on, lows, width, lows-mirror, mix``—the same state words
    carried after the 12-byte header in the source-bound ``godv`` live-control
    message.  This function emits no transport framing and performs no I/O.
    """
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be bool")
    lows = _unit_float("lows", lows)
    width = _unit_float("width", width)
    mix = _unit_float("mix", mix)
    return struct.pack(
        "<IIffff", 20, int(enabled), lows, width, lows, mix)


def encode_native_delay_state(enabled=True, time_s=0.19, feedback=0.2,
                              mix=0.5):
    """Encode model 5's native leaf: byte count plus the live ``vech`` payload.

    Grounded byte-for-byte on the firmware's own Channel-1 default records,
    which ship exactly this component. The blob stores half the feedback, which
    ``io24_fx.fx_delay`` already handles, so this reuses that builder rather
    than repacking the fields and risking a different rounding.
    """
    import io24_fx
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be bool")
    time_s = float(time_s)
    if not math.isfinite(time_s) or not 0.0 < time_s <= 0.25:
        raise NativeStatFormatError("delay time must be in 0..0.25 s")
    payload = io24_fx.fx_delay(enabled, time_s, _unit_float("feedback",
                                                           feedback),
                               _unit_float("mix", mix))[12:]
    if len(payload) != NATIVE_MODEL_STATE_BYTES[VOICEFX_MODEL_DELAY]:
        raise NativeStatFormatError(
            "the 'vech' payload is %d bytes, expected %d"
            % (len(payload), NATIVE_MODEL_STATE_BYTES[VOICEFX_MODEL_DELAY]))
    return struct.pack("<I", len(payload)) + payload


def build_native_voicefx_stat_record(base_record, slot_index, model, state):
    """Replace or add component 201 carrying one model's encoded leaf.

    ``state`` is a complete leaf from one of the ``encode_native_*_state``
    functions. The complete Fat Channel state is retained byte for byte. This
    is an offline firmware-structure helper, not a live-write decision.
    """
    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or \
            not 0 <= slot_index <= 3:
        raise ValueError("device slot index must be in the range 0..3")
    if model not in NATIVE_MODEL_STATE_BYTES:
        raise NativeStatFormatError(
            "no decoded native leaf for Voice FX model %r" % (model,))
    base_record = validate_native_stat_record(
        base_record, slot_index=slot_index)
    record = decode_native_stat_record(base_record)
    voicefx = (
        struct.pack("<II", VOICEFX_PARENT_VERSION, model) +
        encode_chunk_group([(model.to_bytes(4, "big"), state)])
    )
    chunks = tuple(
        NativeChunk(chunk.key, voicefx)
        if chunk.key == VOICEFX_COMPONENT_KEY else chunk
        for chunk in record.chunks
    )
    if not any(chunk.key == VOICEFX_COMPONENT_KEY for chunk in record.chunks):
        chunks += (NativeChunk(VOICEFX_COMPONENT_KEY, voicefx),)
    result = encode_native_stat_record(NativeStatRecord(record.version, chunks))
    return validate_native_stat_record(result, slot_index=slot_index)


def build_native_delay_stat_record(base_record, slot_index, enabled=True,
                                   time_s=0.19, feedback=0.2, mix=0.5):
    """Put a native Delay (model 5) in a slot record, as the firmware does."""
    return build_native_voicefx_stat_record(
        base_record, slot_index, VOICEFX_MODEL_DELAY,
        encode_native_delay_state(enabled, time_s, feedback, mix))


def build_native_model0_stat_record(base_record, slot_index, enabled=True,
                                    lows=0.06, width=0.405, mix=0.295):
    """Replace or add component 201 in a firmware chunk fixture.

    The complete Fat Channel state is retained byte-for-byte.  This is an
    offline firmware-structure helper, not UC's source-bound tagged host slot
    writer and not a live-write eligibility decision.
    """
    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or \
            not 0 <= slot_index <= 3:
        raise ValueError("device slot index must be in the range 0..3")
    base_record = validate_native_stat_record(
        base_record, slot_index=slot_index)
    record = decode_native_stat_record(base_record)
    voicefx = (
        struct.pack("<II", VOICEFX_PARENT_VERSION, VOICEFX_MODEL0) +
        encode_chunk_group([(
            VOICEFX_MODEL0.to_bytes(4, "big"),
            encode_native_model0_state(enabled, lows, width, mix),
        )])
    )
    chunks = tuple(
        NativeChunk(chunk.key, voicefx)
        if chunk.key == VOICEFX_COMPONENT_KEY else chunk
        for chunk in record.chunks
    )
    if not any(chunk.key == VOICEFX_COMPONENT_KEY for chunk in record.chunks):
        chunks += (NativeChunk(VOICEFX_COMPONENT_KEY, voicefx),)
    result = encode_native_stat_record(NativeStatRecord(record.version, chunks))
    return validate_native_stat_record(result, slot_index=slot_index)


def replace_native_channel_component(base_record, slot_index, key, payload):
    """Replace one complete channel-strip leaf in a native record.

    This is the byte-preserving seam for offline component builders. The base
    supplies every leaf the caller is not replacing, including any Voice FX;
    partial leaves and unknown component keys are refused before encoding.
    """
    key = _as_bytes("native channel component key", key)
    payload = _as_bytes("native channel component payload", payload)
    if key not in CHANNEL_COMPONENT_SIZES:
        raise NativeStatFormatError(
            "unsupported native channel component %s" % key.hex())
    expected_size = CHANNEL_COMPONENT_SIZES[key]
    if len(payload) != expected_size:
        raise NativeStatFormatError(
            "native %s chunk has %d bytes, expected %d" %
            (key.decode("ascii"), len(payload), expected_size))

    base_record = validate_native_stat_record(
        base_record, slot_index=slot_index)
    record = decode_native_stat_record(base_record)
    top = []
    for chunk in record.chunks:
        if chunk.key != CHANNEL_STRIP_KEY:
            top.append(chunk)
            continue
        channel = tuple(
            NativeChunk(leaf.key, payload if leaf.key == key else leaf.payload)
            for leaf in decode_chunk_group(chunk.payload)
        )
        top.append(NativeChunk(CHANNEL_STRIP_KEY, encode_chunk_group(channel)))
    result = encode_native_stat_record(
        NativeStatRecord(record.version, tuple(top)))
    return validate_native_stat_record(result, slot_index=slot_index)


def build_native_standard_eq_stat_record(base_record, slot_index, eq):
    """Overlay a complete semantic Standard EQ on a native slot record.

    Every non-EQ leaf is retained from ``base_record``.  The EQ builder emits
    coefficients for all four supported sample rates and preserves every
    structural byte around those coefficients.
    """
    base_record = validate_native_stat_record(
        base_record, slot_index=slot_index)
    record = decode_native_stat_record(base_record)
    top = _unique_by_key("native Stat top level", record.chunks)
    channel = _unique_by_key(
        "native channel strip", decode_chunk_group(top[CHANNEL_STRIP_KEY]))

    import io24_native_strip
    payload = io24_native_strip.build_standard_eq_component(channel[b"eq  "], eq)
    return replace_native_channel_component(
        base_record, slot_index, b"eq  ", payload)


def _alternate_eq_coefficients(eq):
    """Use the shipped interpreter and the user's local UC 4.7.2 DLL."""
    import io24_alt_eq
    import io24_presets

    model = io24_presets.eq_model(eq)
    if model not in ("passive", "vintage"):
        raise NativeStatFormatError("alternate EQ must be Passive or Vintage")
    try:
        return io24_alt_eq.design_native_coefficients(eq)
    except Exception as error:
        raise NativeStatFormatError(
            "%s EQ native design failed: %s" %
            (model.title(), error)) from error


def build_native_slot_record(slot_record, slot_index, sample_rate_hz=96000.0):
    """Build one complete firmware-native slot body from a Host scene.

    The embedded base supplies structural bytes with no semantic equivalent.
    Every user-facing Fat Channel value is then replaced before the result is
    eligible for transport.  FX-off scenes remain FX-free; Transformer is the
    only currently decoded native FX component.
    """
    from io24_preset_record import complete_device_slot_record
    import io24
    import io24_dsp
    import io24_fx
    import io24_native_strip as strip
    import io24_presets

    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or \
            not 0 <= slot_index <= 3:
        raise ValueError("device slot index must be in the range 0..3")
    try:
        rate = float(sample_rate_hz)
    except (TypeError, ValueError):
        raise NativeStatFormatError("sample rate must be a supported clock")
    if rate not in strip.RATES:
        raise NativeStatFormatError("sample rate must be a supported clock")

    scene = complete_device_slot_record(slot_record)
    base = stock_native_stat_record()
    decoded = decode_native_stat_record(base)
    top = {chunk.key: chunk.payload for chunk in decoded.chunks}
    channel_chunks = decode_chunk_group(top[CHANNEL_STRIP_KEY])
    components = strip.decode_components(channel_chunks)

    filt = scene["filter"]
    try:
        hpf = float(filt["hpf"])
    except (KeyError, TypeError, ValueError):
        raise NativeStatFormatError("slot HPF must be a finite frequency")
    if not math.isfinite(hpf) or not 24.0 <= hpf <= 1000.0:
        raise NativeStatFormatError("slot HPF must be in 24..1000 Hz")
    identity = (1.0, 0.0, 0.0, 0.0, 0.0)
    components[b"filt"].set_filter_coefficients({
        clock: identity if hpf <= 24.001 else
        tuple(io24.Io24.highpass_coeffs(hpf, clock))
        for clock in strip.RATES
    })

    gate = scene["gate"]
    try:
        gate_args = {
            "on": io24_presets._toggle(gate, "on", "gate"),
            "threshold_db": float(gate["threshold"]),
            "range_db": float(gate["range"]),
            "attack_s": float(gate["attack"]),
            "release_s": float(gate["release"]),
            "keyfilter_hz": float(gate["keyfilter"]),
            "expander": io24_presets._toggle(
                gate, "expander", "gate"),
            "keylisten": io24_presets._toggle(
                gate, "keylisten", "gate"),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise NativeStatFormatError(
            "slot gate state is invalid: %s" % error) from error
    for index in (0, 1):
        components[b"gate"].set_gate_blob(io24_dsp.gate_blob(
            index, fs=rate, **gate_args))

    comp_index, comp_args = io24_presets._compressor_call(scene["comp"])
    comp_builder = (
        io24_dsp.cpxt_comp, io24_dsp.cpxt_tube, io24_dsp.cpxt_fet
    )[comp_index]
    for block in components[b"comp"].blocks:
        blob = comp_builder(index=0, fs=block.rate, **comp_args)
        block.set_biquad(0, struct.unpack_from("<5f", blob, 12))
    for index in (0, 1):
        components[b"comp"].set_compressor_blob(comp_builder(
            index=index, fs=rate, **comp_args))

    eq = scene["eq"]
    eq_model = io24_presets.eq_model(eq)
    if eq_model == "standard":
        eq_coefficients = strip.design_standard_eq(eq)
    elif eq_model in ("passive", "vintage"):
        eq_coefficients = _alternate_eq_coefficients(eq)
    else:
        raise NativeStatFormatError("slot EQ model is missing")
    components[b"eq  "].set_eq_coefficients(eq_coefficients)

    limiter = scene["limit"]
    try:
        limiter_on = limiter["limiteron"]
        if limiter_on not in (0, 1, False, True):
            raise ValueError
        limiter_threshold = float(limiter["threshold"])
    except (KeyError, TypeError, ValueError):
        raise NativeStatFormatError("slot limiter state is incomplete")
    components[b"lim "].set_limiter(
        on=bool(limiter_on),
        inverse_threshold=io24_dsp.limiter_inv_threshold(limiter_threshold),
        release_coefficient=io24_dsp.limiter_release_coef(0.4, rate),
    )

    channel = encode_chunk_group([
        NativeChunk(chunk.key, components[chunk.key].encode())
        for chunk in channel_chunks
    ])
    rebuilt = encode_native_stat_record(NativeStatRecord(
        decoded.version,
        tuple(NativeChunk(chunk.key, channel)
              if chunk.key == CHANNEL_STRIP_KEY else chunk
              for chunk in decoded.chunks),
    ))
    rebuilt = validate_native_stat_record(rebuilt, slot_index=slot_index)

    voicefx = scene.get("voicefx") or {}
    if voicefx:
        model, fx_args = io24_fx.voicefx_preset_call(voicefx)
        if fx_args["on"]:
            if model == "transformer":
                rebuilt = build_native_model0_stat_record(
                    rebuilt, slot_index, enabled=True,
                    lows=fx_args["lows"], width=fx_args["width"],
                    mix=fx_args["mix"])
            elif model == "delay":
                raise NativeStatFormatError(
                    "active Delay cannot be stored in a device-resident "
                    "block; save it in a Host setup so rate-safe placement "
                    "can be chosen when it loads")
            else:
                raise NativeStatFormatError(
                    "%s FX has no decoded native slot component" %
                    model.title())
    return validate_native_stat_record(rebuilt, slot_index=slot_index)


def native_stat_summary(data, slot_index=None):
    """Return a compact JSON-serializable structural summary."""
    data = validate_native_stat_record(data, slot_index=slot_index)
    record = decode_native_stat_record(data)
    top = _unique_by_key("native Stat top level", record.chunks)
    channel = decode_chunk_group(top[CHANNEL_STRIP_KEY])
    result = {
        "version": record.version,
        "bytes": len(data),
        "top_chunks": [
            {"key_hex": chunk.key.hex(), "bytes": len(chunk.payload)}
            for chunk in record.chunks
        ],
        "channel_chunks": [
            {"key": chunk.key.decode("ascii"), "bytes": len(chunk.payload)}
            for chunk in channel
        ],
        "voicefx": None,
    }
    if VOICEFX_COMPONENT_KEY in top:
        voicefx = top[VOICEFX_COMPONENT_KEY]
        parent_version, selected_model = struct.unpack_from("<II", voicefx)
        result["voicefx"] = {
            "component_id": VOICEFX_COMPONENT_ID,
            "parent_version": parent_version,
            "selected_model": selected_model,
            "bytes": len(voicefx),
        }
        model = decode_chunk_group(voicefx[8:])[0]
        state_bytes, = struct.unpack_from("<I", model.payload)
        result["voicefx"].update({
            "model_key_hex": model.key.hex(),
            "state_bytes": state_bytes,
            "state_hex": model.payload[4:].hex(),
        })
    return result
