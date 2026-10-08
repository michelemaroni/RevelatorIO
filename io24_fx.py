"""io24_fx.py -- the six insert-FX models of the PreSonus Revelator io24.

Runnable and self-tested (python3 io24_fx.py -> "OK -- all six models").
Also saved at

Block 201 is the insert-FX slot (dev+0x11e0, a singleton -- blockIndex ignored).
UC 4.7.2 selects a model once, immediately materializes that model's complete
runtime state, and then sends state-only writes for ordinary On/Off and control
edits.  Models 0, 1 and 2 also push sample-rate-dependent filter material on
the same block when the model or its filter configuration changes.
Everything here is static analysis of the Windows host driver dspusbdevice.dll
(PE32+, imagebase 0x180000000) cross-checked against the io24's own Thumb-2
firmware embedded in that DLL (file 0x216030..0x323a70, load VA 0x60020000;
file_off = 0x216030 + VA - 0x60020000).  Only model 5 (Delay) is
hardware-verified; block 201 is write-only, so nothing else can be read back.

model -> class binding (proved, not guessed):
  factory 0x180049960 does `lea eax,[r13+1]; cmp eax,6; ja default;
  jmp [0x180049f34 + eax*4]`, and the SAME r13d is stored into the 'VoFx'
  payload at 0x180049e69 (with block 0xc9 = 201 at 0x180049e4f).  So the
  jump-table index IS the VoFx model number.  Each of the six tags occurs
  exactly ONCE as an immediate in the whole .text, inside exactly one push:

    m  name            case        ctor/inline   vtable       setParam     push         tag    size
    0  Transformer     0x1800499e0 0x18004ae90   0x1804362a0  0x1800222d0  0x180022370  godv   0x20
    1  De-Tuner        0x180049be3 0x18004b430   0x180435f30  0x1800236b0  0x180023740  may4   0x24
    2  Vocoder         0x180049a0f 0x18004b080   0x180436400  0x180022ce0  0x180022dc0  bota   0x28
    3  Ring Modulator  0x180049a3e 0x18004b280   0x180436090  0x180023390  0x1800234b0  botb   0x28
    4  Filters         0x180049a6d inline        0x180436350  0x180023500  0x180023600  botc   0x28
    5  Delay           0x180049c12 inline        0x180435bb8  0x180020920  0x1800209c0  vech   0x1c

  Index 6 (= model 5) lands on the class whose setParam/push are the
  already hardware-verified delay pair -- that is the anchor that rules out
  any off-by-one.  The firmware agrees: each tag also occurs exactly once
  there, as the literal a SetP handler compares against.
"""

# --- CALL SHAPES (bit me once, noted here) -------------------------------
# fx_transformer_blobs(...) -> LIST of blobs   -> for b in ...: send(b)
# fx_vocoder_filters(fs)    -> a SINGLE blob   -> send(...)      (not a list)
# All other fx_*() builders -> a SINGLE blob.
# Block 201 is channel-insert state.  Firmware model 0 owns a private instance
# of the same reverb-core implementation used by block 202; it does not use the
# block-202 send/return object.  Native Stat recall stores the high-level model
# state and configures that private core.  These direct blob builders are a
# separate live-control path. Model selection uses a deferred bypass/rebuild
# transition.  UC does not add a host-side delay or replay the selector.


import math
import struct

# ---------------------------------------------------------------- helpers
def f32(x):
    """round to IEEE single, as every host computation does"""
    return struct.unpack('<f', struct.pack('<f', float(x)))[0]


def hdr(payload, uid=1):
    """8-byte paesdk bulk header (unchanged, from io24_meters.py)"""
    return struct.pack('<HBBBBH', len(payload) + 8, 1, 1, uid, 0, 0) + payload


def setp(block, blob, blockindex=0, uid=1):
    return hdr(struct.pack('<III', 0x53657450, block, blockindex) + blob, uid)


BLOCK_FX = 201          # 0xc9, literal at 0x180049e4f

TAG_VOFX = 0x566f4678   # 'VoFx'
TAG_GODV = 0x676f6476   # 'godv'  model 0
TAG_MAY4 = 0x6d617934   # 'may4'  model 1
TAG_BOTA = 0x626f7461   # 'bota'  model 2
TAG_BOTB = 0x626f7462   # 'botb'  model 3
TAG_BOTC = 0x626f7463   # 'botc'  model 4
TAG_VECH = 0x76656368   # 'vech'  model 5  (verified)
TAG_INIA = 0x696e6961   # 'inia'  model 2 filter bank
TAG_BQDF = 0x42716466   # 'Bqdf'  generic single biquad (models 0 and 1)
TAG_MBDF = 0x4d426466   # 'MBdf'  sample-rate-indexed biquad table (model 0)

MODEL_NAMES = ['Transformer', 'De-Tuner', 'Vocoder',
               'Ring Modulator', 'Filters', 'Delay']

# 48 kHz is the highest rate with direct physical Delay acceptance. The same
# private histories grow with the clock, so 88.2 kHz is kept off the firmware
# path along with the observed 96 kHz reset case.
DELAY_NATIVE_MAX_RATE_HZ = 48000.0
RATE_AWARE_MODELS = frozenset((
    "transformer", "detuner", "vocoder", "delay",
))


class UnsafeDelayRate(RuntimeError):
    """A Delay operation whose device-clock safety is not established."""


def delay_needs_host_fallback(fs):
    """Whether the hardware Delay must be replaced by the Host processor."""
    try:
        rate = float(fs)
    except (TypeError, ValueError):
        return False
    return math.isfinite(rate) and rate > DELAY_NATIVE_MAX_RATE_HZ


def validate_delay_sample_rate(fs):
    """Return a safe runtime rate or refuse before a Delay frame is built.

    Delay was waveform-verified at 48 kHz. On 2026-09-21, selecting it while
    the io24 was clocked at 96 kHz immediately re-enumerated the interface as
    its bootloader. Its private histories also grow substantially at 88.2 kHz,
    which has no physical Delay acceptance. The exact firmware failure is not
    inferred here; rates above the accepted boundary use the Host insert.
    """
    if fs is None:
        raise UnsafeDelayRate(
            "Delay needs the current sample rate before it can be sent")
    try:
        rate = float(fs)
    except (TypeError, ValueError) as error:
        raise UnsafeDelayRate(
            "Delay needs a valid current sample rate before it can be sent") \
            from error
    if not math.isfinite(rate) or not 8000.0 <= rate <= 192000.0:
        raise UnsafeDelayRate(
            "Delay needs a valid current sample rate before it can be sent")
    if rate > DELAY_NATIVE_MAX_RATE_HZ:
        raise UnsafeDelayRate(
            "hardware Delay is blocked above 48 kHz; 88.2 kHz has no safe "
            "acceptance and selecting it at 96 kHz reset the io24. The "
            "Linux Host must use its safe Delay insert")
    return rate


def voicefx_runtime_kwargs(model, kwargs, fs):
    """Add only the runtime clock data a selected Voice FX model needs."""
    name = str(model).lower()
    result = dict(kwargs)
    if name == "delay":
        result["fs"] = validate_delay_sample_rate(fs)
    elif name in RATE_AWARE_MODELS:
        try:
            rate = float(fs)
        except (TypeError, ValueError) as error:
            raise ValueError("Voice FX sample rate must be numeric") from error
        if not math.isfinite(rate) or not 8000.0 <= rate <= 192000.0:
            raise ValueError("Voice FX sample rate must be 8000..192000 Hz")
        result["fs"] = rate
    return result

# UC persists the selected VoiceFX implementation as the mutable component's
# class id.  Parameter 450 is the host-side selector that materializes this
# choice; the io24 does not expose a readable block-201 selector.  Keep the
# mapping next to the exact wire builders so scene/factory importers cannot
# silently drift from the model numbers sent by set_fx_model().
VOICEFX_CLASS_IDS = {
    'transformer': '{66A10093-D461-4CAC-A80C-91F6A1BB37E5}',
    'detuner': '{509018B0-0DE1-4D26-9DDF-D782476D3C98}',
    'vocoder': '{981F8B8F-D1D8-4634-BB04-2148AF2123E3}',
    'ringmod': '{4B4CAD90-7709-451F-A7DD-AF6F0E954F57}',
    'filters': '{491FED97-6761-4FF6-91E1-E155891291AA}',
    'delay': '{98A527BA-2D6E-4B35-BB26-251EC081A067}',
}

_VOICEFX_MODEL_BY_CLASS_ID = {
    class_id.upper(): model
    for model, class_id in VOICEFX_CLASS_IDS.items()
}

# A runtime-safe transcription of the Voice FX schema documented in
# ``PROTOCOL.md`` under "The per-module On is already on the wire".
# The retained XML is the authority for order, names, type, bounds, defaults,
# curve, midpoint, units, flags and list labels.  ``builder`` is the one local
# addition: it names the exact keyword accepted by the recovered wire builder.
# Keeping that adapter beside the XML field prevents the Host UI, preset JSON
# and packet builders from acquiring three independently maintained maps.
VOICEFX_XML_SCHEMA = {
    'transformer': {
        'paramlist': 'VoiceOfGod',
        'title': 'Doubler / Transformer',
        'parameters': (
            {'id': 'on', 'name': 'On', 'type': 'toggle', 'builder': 'on',
             'flags': ('storable',)},
            {'id': 'lows', 'name': 'Lows', 'type': 'float', 'builder': 'lows',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
            {'id': 'width', 'name': 'Width', 'type': 'float', 'builder': 'width',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
            {'id': 'mix', 'name': 'WetDry', 'type': 'float', 'builder': 'mix',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
        ),
    },
    'detuner': {
        'paramlist': 'DarthVoice',
        'title': 'Detuner',
        'parameters': (
            {'id': 'on', 'name': 'On', 'type': 'toggle', 'builder': 'on',
             'flags': ('storable',)},
            {'id': 'detune', 'name': 'Detune', 'type': 'list',
             'builder': 'detune', 'default': 4,
             'units': 'TuneList',
             'choices': ('-8', '-7', '-6', '-5', '-4', '-3', '-2', '-1', '0'),
             'flags': ('storable',)},
            {'id': 'mix', 'name': 'WetDry', 'type': 'float', 'builder': 'mix',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
        ),
    },
    'vocoder': {
        'paramlist': 'RobotVoiceA',
        'title': 'Vocoder',
        'parameters': (
            {'id': 'on', 'name': 'On', 'type': 'toggle', 'builder': 'on',
             'flags': ('storable',)},
            {'id': 'avol', 'name': 'Volume', 'type': 'float', 'builder': 'vol',
             'min': 0.0, 'max': 1.0, 'default': 1.0, 'units': 'percent',
             'flags': ('storable',)},
            {'id': 'acarriertype', 'name': 'Carrier Type', 'type': 'list',
             'builder': 'carrier_type', 'default': 1, 'units': 'Carriers',
             'choices': ('Noise', 'Sawtooth', 'Rect'),
             'flags': ('storable',)},
            {'id': 'acarrierfreq', 'name': 'Carrier Frequency', 'type': 'float',
             'builder': 'carrier_freq', 'min': 50.0, 'max': 500.0,
             'curve': 'skew', 'mid': 100.0, 'default': 80.0, 'units': 'freq',
             'flags': ('storable',)},
            {'id': 'avoiced', 'name': 'Voiced', 'type': 'float', 'builder': None,
             'min': 0.0, 'max': 1.0, 'flags': ('readonly',)},
            {'id': 'mix', 'name': 'WetDry', 'type': 'float', 'builder': 'mix',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
        ),
    },
    'ringmod': {
        'paramlist': 'RobotVoiceB',
        'title': 'Ring Modulator',
        'parameters': (
            {'id': 'on', 'name': 'On', 'type': 'toggle', 'builder': 'on',
             'flags': ('storable',)},
            {'id': 'bcarrierfreq', 'name': 'Frequency', 'type': 'float',
             'builder': 'carrier_hz', 'min': 0.1, 'max': 2000.0,
             'curve': 'skew', 'mid': 30.0, 'default': 30.0, 'units': 'freq',
             'flags': ('storable',)},
            {'id': 'bcarrier2', 'name': 'Sub Carrier', 'type': 'toggle',
             'builder': 'carrier2', 'flags': ('storable',)},
            {'id': 'bcarrier2freq', 'name': 'Sub Carrier Frequency',
             'type': 'float', 'builder': 'carrier2_hz', 'min': 0.1,
             'max': 2000.0, 'curve': 'skew', 'mid': 30.0, 'default': 50.0,
             'units': 'freq', 'flags': ('storable',)},
            {'id': 'bdist', 'name': 'Distortion', 'type': 'float',
             'builder': 'dist', 'min': 0.0, 'max': 1.0, 'default': 0.5,
             'units': 'percent', 'flags': ('storable',)},
            {'id': 'bvol', 'name': 'Volume', 'type': 'float', 'builder': 'vol',
             'min': 0.0, 'max': 1.0, 'default': 1.0, 'units': 'percent',
             'flags': ('storable',)},
            {'id': 'mix', 'name': 'WetDry', 'type': 'float', 'builder': 'mix',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
        ),
    },
    'filters': {
        'paramlist': 'RobotVoiceC',
        'title': 'Filters',
        'parameters': (
            {'id': 'on', 'name': 'On', 'type': 'toggle', 'builder': 'on',
             'flags': ('storable',)},
            {'id': 'ctune', 'name': 'Pitch', 'type': 'float', 'builder': 'pitch',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
            {'id': 'cfb', 'name': 'Regeneration', 'type': 'float',
             'builder': 'regeneration', 'min': 0.0, 'max': 1.0,
             'default': 0.5, 'units': 'percent', 'flags': ('storable',)},
            {'id': 'cdamp', 'name': 'Damping', 'type': 'float',
             'builder': 'damping', 'min': 0.0, 'max': 1.0,
             'default': 0.5, 'units': 'percent', 'flags': ('storable',)},
            {'id': 'cdist', 'name': 'Distortion', 'type': 'float',
             'builder': 'distortion', 'min': 0.0, 'max': 1.0,
             'default': 0.5, 'units': 'percent', 'flags': ('storable',)},
            {'id': 'cvol', 'name': 'Volume', 'type': 'float',
             'builder': 'volume', 'min': 0.0, 'max': 1.0, 'default': 1.0,
             'units': 'percent', 'flags': ('storable',)},
            {'id': 'mix', 'name': 'WetDry', 'type': 'float', 'builder': 'mix',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
        ),
    },
    'delay': {
        'paramlist': 'VocalEcho',
        'title': 'Delay',
        'parameters': (
            {'id': 'on', 'name': 'On', 'type': 'toggle', 'builder': 'on',
             'flags': ('storable',)},
            {'id': 'time', 'name': 'Time', 'type': 'float', 'builder': 'time_s',
             'min': 0.0001, 'max': 0.25, 'mid': 0.125, 'default': 0.125,
             'units': 'time', 'flags': ('storable',)},
            {'id': 'feedback', 'name': 'Feedback', 'type': 'float',
             'builder': 'feedback', 'min': 0.0, 'max': 1.0,
             'default': 0.5, 'units': 'percent', 'flags': ('storable',)},
            {'id': 'mix', 'name': 'WetDry', 'type': 'float', 'builder': 'mix',
             'min': 0.0, 'max': 1.0, 'default': 0.5, 'units': 'percent',
             'flags': ('storable',)},
        ),
    },
}


def _voicefx_conversion(parameter):
    if parameter['type'] == 'toggle':
        return 'bool'
    if parameter['type'] == 'list':
        return 'int'
    return 'float'


# (serialized UC field, io24 builder keyword, conversion kind), derived from
# the schema above. Read-only XML fields such as Vocoder ``avoiced`` have no
# builder and therefore cannot accidentally become a SetP argument.
_VOICEFX_PRESET_FIELDS = {
    model: tuple((parameter['id'], parameter['builder'],
                  _voicefx_conversion(parameter))
                 for parameter in component['parameters']
                 if parameter['builder'] is not None)
    for model, component in VOICEFX_XML_SCHEMA.items()
}


def _preset_value(value, field, kind):
    """Validate one JSON component value without inventing a default."""
    if kind == 'bool':
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError('VoiceFX field %s must be 0/1 or boolean' % field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('VoiceFX field %s must be numeric' % field)
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError('VoiceFX field %s must be finite' % field)
    if kind == 'int':
        if not numeric.is_integer():
            raise ValueError('VoiceFX field %s must be an integer' % field)
        return int(numeric)
    return numeric


def voicefx_preset_call(state):
    """Translate one UC ``voicefx`` object to ``Io24.set_fx`` arguments.

    This is deliberately strict.  A missing field in a supposedly complete
    saved component is not replaced with a builder default, because doing that
    would apply a state the source record never specified.  The returned call
    selects/instantiates the model first; ``Io24.set_fx`` then sends that
    model's exact parameter payloads in the UC order.
    """
    if not isinstance(state, dict):
        raise ValueError('VoiceFX state must be a mapping')
    class_id = state.get('__classid')
    if not isinstance(class_id, str):
        raise ValueError('VoiceFX state is missing __classid')
    model = _VOICEFX_MODEL_BY_CLASS_ID.get(class_id.upper())
    if model is None:
        raise ValueError('unknown VoiceFX class id %r' % class_id)

    kwargs = {}
    for source, target, kind in _VOICEFX_PRESET_FIELDS[model]:
        if source not in state:
            raise ValueError('missing VoiceFX field: %s' % source)
        kwargs[target] = _preset_value(state[source], source, kind)
    return model, kwargs


def voicefx_preset_state(model, **kwargs):
    """Build one complete UC slot component from live builder arguments.

    This is the inverse of :func:`voicefx_preset_call`.  It is intentionally
    strict so a device-slot save cannot silently fill a missing model field or
    retain fields belonging to a different Voice FX implementation.
    """
    if model not in _VOICEFX_PRESET_FIELDS:
        raise ValueError("unknown VoiceFX model %r" % model)
    schema = _VOICEFX_PRESET_FIELDS[model]
    expected = {target for _source, target, _kind in schema}
    if set(kwargs) != expected:
        missing = sorted(expected.difference(kwargs))
        unknown = sorted(set(kwargs).difference(expected))
        details = []
        if missing:
            details.append("missing %s" % ", ".join(missing))
        if unknown:
            details.append("unknown %s" % ", ".join(unknown))
        raise ValueError("VoiceFX builder arguments are incomplete: %s" %
                         "; ".join(details))
    state = {"__classid": VOICEFX_CLASS_IDS[model]}
    for source, target, kind in schema:
        value = _preset_value(kwargs[target], source, kind)
        state[source] = int(value) if kind == "bool" else value
    # Exercise the same strict decoder used by direct apply before returning a
    # body that may later be serialized into a complete device slot.
    voicefx_preset_call(state)
    return state


def set_fx_model(model):
    """SetP | 201 | 'VoFx' size 0x10 | {u32 model}.  Unchanged, verified."""
    assert 0 <= model <= 5
    return setp(201, struct.pack('<IIII', TAG_VOFX, 0x10, 0, model))


# ======================================================================
# 0. the shared biquad designer  (dspusbdevice 0x180010d30)
# ======================================================================
# obj+0x2c type | obj+0x34 fs | obj+0x38 gain_db | obj+0x3c freq | obj+0x40 Q
# obj+0x64 = pi/fs  (setSampleRate 0x18000fce0, pi f32 const at 0x1804819d0)
# Results land in obj+0x44,+0x48,+0x4c,+0x50,+0x54 and every caller copies
# those five dwords straight to the wire, so the wire order is
#     [b0, -a1, b1, -a2, b2]
# That order is triple-anchored:
#   * the hardware-verified reverb 'vrvb'+0x24 reads designer+0x44.. the same way
#     (reverb push 0x1800207f0: obj+0xa8..+0xb8 == designer_base(obj+0x64)+0x44..)
#   * io24 firmware 'Bqdf' handler for model 1 (0x6007ba60) scatters
#     blob c0->dev+0x340(b0) c1->+0x34c(-a1) c2->+0x344(b1) c3->+0x350(-a2)
#     c4->+0x348(b2)
#   * io24 firmware 'inia' loop (0x600779a0) does the identical scatter.
PI_F = f32(3.1415927410125732)      # 0x1804819d0


def biquad_lp2(freq, fs=48000.0, Q=0.7):
    """designer type 2, full-recompute handler 0x180011ed1 (+ epilogue
       0x1800127f6 which stores b2 = b0):
         K = tanf(freq * pi/fs) ; n = 1 + K/Q + K*K
         [b0,-a1,b1,-a2,b2] = [K^2/n, 2(1-K^2)/n, 2K^2/n, (K/Q-1-K^2)/n, K^2/n]
    """
    K = f32(math.tan(f32(f32(freq) * f32(PI_F / f32(fs)))))
    KK = f32(K * K)
    n = f32(f32(f32(K / f32(Q)) + KK) + 1.0)
    b0 = f32(KK / n)
    return [b0,
            f32(f32(f32(1.0 - KK) + f32(1.0 - KK)) / n),
            f32(b0 + b0),
            f32(f32(f32(f32(K / f32(Q)) - 1.0) - KK) / n),
            b0]


def biquad_hp2(freq, fs=48000.0, Q=0.7):
    """designer type 3, handler 0x180011f5d.  Identical to the already
       hardware-verified rbj_hp2() in io24_meters.py:
         [b0,-a1,b1,-a2,b2] = [1/n, 2(1-K^2)/n, -2/n, (K/Q-1-K^2)/n, 1/n]"""
    K = f32(math.tan(f32(f32(freq) * f32(PI_F / f32(fs)))))
    KK = f32(K * K)
    n = f32(f32(f32(K / f32(Q)) + KK) + 1.0)
    b0 = f32(1.0 / n)
    return [b0,
            f32(f32(f32(1.0 - KK) + f32(1.0 - KK)) / n),
            f32(-2.0 / n),
            f32(f32(f32(f32(K / f32(Q)) - 1.0) - KK) / n),
            b0]


def biquad_lowshelf(freq, gain_db, fs=48000.0, Q=0.7):
    """designer type 8, full-recompute handler 0x1800125de.  Textbook RBJ
       low shelf; transcribed instruction for instruction:
         A    = powf(10.0, gain_db * 0.025)      consts 0x180481a10 / 0x18048185c
         w    = 2*freq * (pi/fs)                 (class-7 preamble 0x1800122b4)
         beta = (sqrt(A)/Q) * sin(w)
         a0   = (A+1) + (A-1)cos(w) + beta
         b0   =  A*((A+1) - (A-1)cos + beta)/a0     -> obj+0x44
         -a1  =  2*((A-1) + (A+1)cos)/a0            -> obj+0x48
         b1   =  2*A*((A-1) - (A+1)cos)/a0          -> obj+0x4c
         -a2  = -((A+1) + (A-1)cos - beta)/a0       -> obj+0x50
         b2   =  A*((A+1) - (A-1)cos - beta)/a0     -> obj+0x54
    """
    A = f32(10.0 ** f32(f32(gain_db) * f32(0.025)))
    w = f32(f32(f32(freq) + f32(freq)) * f32(PI_F / f32(fs)))
    s, c = math.sin(w), math.cos(w)
    beta = f32(f32(math.sqrt(A) / f32(Q)) * s)
    a0 = (A + 1.0) + (A - 1.0) * c + beta
    return [f32(A * ((A + 1.0) - (A - 1.0) * c + beta) / a0),
            f32(2.0 * ((A - 1.0) + (A + 1.0) * c) / a0),
            f32(2.0 * A * ((A - 1.0) - (A + 1.0) * c) / a0),
            f32(-((A + 1.0) + (A - 1.0) * c - beta) / a0),
            f32(A * ((A + 1.0) - (A - 1.0) * c - beta) / a0)]


def bqdf_blob(band, coeffs, index=0):
    """'Bqdf', size 0x24.  Emitted by model 0 (bands 0 and 1) and model 1
       (band 0 only -- firmware 0x6007ba62 does `cbnz r3, reject`).
       +0x00 tag | +0x04 0x24 | +0x08 component index | +0x0c band
       +0x10..+0x23  5 * f32 [b0, -a1, b1, -a2, b2]"""
    b = struct.pack('<IIII', TAG_BQDF, 0x24, index, band) + \
        struct.pack('<5f', *coeffs)
    assert len(b) == 0x24
    return b


def mbdf_blob(table, rate_coefficients, index=0):
    """Build UC's 0x1f4-byte sample-rate-indexed ``MBdf`` table.

    ``rate_coefficients`` is an iterable of ``(sample_rate, five_coeffs)``.
    UC 4.7.2 allocates room for 20 entries, writes the active entry count at
    +0x1f0, and currently supplies 44.1/48/88.2/96 kHz.  Its unused stack
    slots contain indeterminate bytes; zeroing them is deterministic and safe
    because firmware consumes only ``count`` entries.
    """
    entries = list(rate_coefficients)
    if not 0 <= int(table) <= 0xFFFFFFFF:
        raise ValueError("MBdf table must fit u32")
    if not 0 < len(entries) <= 20:
        raise ValueError("MBdf requires 1..20 sample-rate entries")
    blob = bytearray(0x1F4)
    struct.pack_into('<IIII', blob, 0, TAG_MBDF, 0x1F4, index, int(table))
    for position, (sample_rate, coefficients) in enumerate(entries):
        coefficients = tuple(coefficients)
        if len(coefficients) != 5:
            raise ValueError("each MBdf entry requires five coefficients")
        struct.pack_into('<6f', blob, 0x10 + position * 0x18,
                         *coefficients, f32(sample_rate))
    struct.pack_into('<I', blob, 0x1F0, len(entries))
    return bytes(blob)


# ======================================================================
# model 5 -- DELAY  (reference; hardware-verified, reproduced UNCHANGED
#                    from io24_meters.py)
# ======================================================================
def fx_delay(on, time_s, feedback, mix):
    """model 5, tag 'vech', size 0x1c. Fully traced: setParam 0x180020920,
       push 0x1800209c0 -> payload [on, mix, feedback*0.5, time]."""
    return struct.pack('<III', 0x76656368, 0x1c, 0) + struct.pack(
        '<Ifff', 1 if on else 0, mix, f32(feedback * 0.5), time_s)


# ======================================================================
# model 0 -- TRANSFORMER   tag 'godv' 0x20
#                         (+ two 'Bqdf' 0x24, two 'MBdf' 0x1f4)
# ======================================================================
# The factory preset titled "Reverb" selects this model.  Its scene/Stat JSON
# contains only class id + on/lows/width/mix.  In firmware, model 0 constructs
# a private reverb core at model+0x384 using the same 0x5af14 constructor and
# 0x5a6a8 configurator as shared reverb block 202.  Do not add vrvb/FXA state
# when storing this model; sample-rate-dependent configuration is runtime state.
# setParam 0x1800222d0: id0 on -> obj+0x60 (u32, (v>=1.0f))
#                       id1 lows -> obj+0x64 AND obj+0x6c, sets dirty obj+0x110
#                       id2 width -> obj+0x68     id3 mix -> obj+0x70
# push 0x180022370:  if dirty: Bqdf(band 0, 600 Hz) ; Bqdf(band 1, 550 Hz)
#                              MBdf(table 0, 600 Hz); MBdf(table 1, 550 Hz)
#                    always : 'godv' = movups obj+0x60..0x6f then obj+0x70
# firmware 'godv' handler 0x6007bc0a copies exactly 5 dwords blob+0x0c..+0x1c
#   -> dev+0x340..+0x350, and dev+0x358 = 0.2 + blob[0x14]*0.2 (width).
# firmware 'Bqdf' handler 0x6007bbfa accepts band <= 1 (two filter slots).
TRANSFORMER_SHELF_FREQ = (f32(600.0), f32(550.0))   # 0x180481aa8 / 0x180481aa4
TRANSFORMER_SHELF_Q = f32(0.7)                      # 0x1804818f0
TRANSFORMER_SHELF_MAXDB = f32(12.0)                 # 0x180481a14
TRANSFORMER_SAMPLE_RATES = (44100.0, 48000.0, 88200.0, 96000.0)


def fx_transformer(on=True, lows=0.5, width=0.5, mix=0.5, index=0):
    """Return the Transformer model-state ``godv`` blob."""
    lows = min(max(f32(lows), 0.0), 1.0)
    width = min(max(f32(width), 0.0), 1.0)
    mix = min(max(f32(mix), 0.0), 1.0)
    blob = struct.pack('<III', TAG_GODV, 0x20, index) + struct.pack(
        '<Iffff', 1 if on else 0, lows, width, lows, mix)
    assert len(blob) == 0x20
    return blob


def fx_transformer_tone_blobs(lows=0.5, fs=48000.0, index=0):
    """Return UC's two current-rate filters and two all-rate tables."""
    lows = min(max(f32(lows), 0.0), 1.0)
    gain_db = f32(lows * TRANSFORMER_SHELF_MAXDB)
    current = [
        bqdf_blob(table, biquad_lowshelf(frequency, gain_db, fs,
                                         TRANSFORMER_SHELF_Q), index)
        for table, frequency in enumerate(TRANSFORMER_SHELF_FREQ)
    ]
    tables = []
    for table, frequency in enumerate(TRANSFORMER_SHELF_FREQ):
        entries = [
            (sample_rate,
             biquad_lowshelf(frequency, gain_db, sample_rate,
                             TRANSFORMER_SHELF_Q))
            for sample_rate in TRANSFORMER_SAMPLE_RATES
        ]
        tables.append(mbdf_blob(table, entries, index))
    return current + tables


def fx_transformer_blobs(on=True, lows=0.5, width=0.5, mix=0.5,
                         fs=48000.0, index=0):
    """Return UC's five materialization blobs in their captured push order.

    The order is Bqdf 0, Bqdf 1, MBdf 0, MBdf 1, then godv.

    'godv', size 0x20:
       +0x0c u32 on | +0x10 f32 lows | +0x14 f32 width
       +0x18 f32 lows AGAIN | +0x1c f32 mix
    +0x18 is mirror obj+0x6c, which setParam id1 writes together with obj+0x64,
    so on the wire it is always byte-identical to +0x10.  getParam never reads
    it and the firmware stores it to dev+0x34c without any visible consumer in
    the code inspected -- its independent meaning is UNKNOWN.  Do not invent
    a separate value for it; send lows.

    Both Bqdf blobs are RBJ low shelves at Q 0.7 with gain = lows * 12 dB,
    at 600 Hz (band 0) and 550 Hz (band 1)."""
    return (fx_transformer_tone_blobs(lows, fs, index) +
            [fx_transformer(on, lows, width, mix, index)])


# ======================================================================
# model 1 -- DE-TUNER   tag 'may4' 0x24  (+ one 'Bqdf' 0x24)
# ======================================================================
# setParam 0x1800236b0: id0 on -> obj+0x60 | id1 detune -> (int)(v+0.5f)
#                       -> obj+0x114 (the INDEX, not the payload)
#                       id2 mix -> obj+0x74
# push 0x180023740: obj+0x64 = DETUNE_TABLE[obj+0x114]; blob = obj+0x60..0x77.
#                   THEN, if obj+0x118 (sample-rate dirty): a 'Bqdf' band 0
#                   holding a 2nd-order LP at 6000 Hz / Q 0.7.
# firmware 'may4' handler 0x6007ba98 copies exactly 6 dwords blob+0x0c..+0x20
#   -> dev+0x2c8..+0x2dc, then apply 0x6007a378 does
#      vcvt.f32.S32 on dev+0x2cc, / 12.0, powf(2.0, .) -> PROVES semitones.
DETUNE_TABLE = [-8, -7, -6, -5, -4, -3, -2, -1, 0,   # 0x180481f30 (UC's table)
                1, 2, 3, 4, 5, 6, 7, 8]              # host extension: upward
DETUNER_LP_FREQ = f32(6000.0)       # 0x180481ae0
DETUNER_LP_Q = f32(0.7)             # 0x1804818f0


def fx_detuner(on=True, detune=4, mix=0.5, index=0):
    """model 1, tag 'may4', size 0x24.

    detune -- host enum index 0..8 (descriptor min 0 max 8, reset 4, UI labels
              "-8".."0").  Payload = DETUNE_TABLE[idx] = idx - 8 SEMITONES.
              8 means no shift.  The push has NO bounds check.

    +0x14/+0x18/+0x1c are emitted as zero because the ctor zeroes mirror
    obj+0x68/+0x6c/+0x70 and nothing ever writes them.  Their MEANING IS
    UNKNOWN; note the device's own power-on default for +0x1c (dev+0x2d8,
    reset routine 0x6007c6b6) is 0.5f, so sending 0 is not a no-op."""
    idx = int(f32(f32(detune) + 0.5))          # setParam: cvttss2si(v + 0.5f)
    # The wire field is a SIGNED i32 semitone count (idx - 8), and the push has
    # no bounds check — the 0..8 limit was UC's UI, not the device's. 9..16
    # sends +1..+8 semitones: accepted by the firmware's setParam, but an
    # upward shift has not been heard on hardware yet, so treat it as a
    # host-extended range rather than a documented one.
    if not 0 <= idx <= 16:
        raise ValueError('detune index must be 0..16 (8 = no shift)')
    b = struct.pack('<III', TAG_MAY4, 0x24, index) + struct.pack(
        '<IiIIIf', 1 if on else 0, DETUNE_TABLE[idx], 0, 0, 0,
        min(max(f32(mix), 0.0), 1.0))
    assert len(b) == 0x24
    return b


def fx_detuner_biquad(fs=48000.0, index=0):
    """The De-Tuner's fixed tone filter -- 'Bqdf' band 0, LP 6000 Hz Q 0.7.
       The host emits it only after a sample-rate change (obj+0x118); the
       ctor leaves that flag CLEAR (0x18004b515), so a fresh instance may
       never send it.  The device's power-on biquad is the identity
       [1,0,0,0,0] (reset 0x6007c690..0x6007c6f0), i.e. tone filter bypassed.
       Send it once to match the Windows host."""
    return bqdf_blob(0, biquad_lp2(DETUNER_LP_FREQ, fs, DETUNER_LP_Q), index)


# ======================================================================
# model 2 -- VOCODER   tag 'bota' 0x28  (+ one 'inia' 0x1c4)
# ======================================================================
# setParam 0x180022ce0: id0 on -> obj+0x60 | id1 avol -> obj+0x6c
#                       id2 acarriertype -> (int)(v+0.5f) -> obj+0x64
#                       id3 acarrierfreq -> obj+0x68 | id5 mix -> obj+0x78
#                       id4 avoiced is READ-ONLY (setParam falls through to
#                       ret; getParam 0x180022d93 reads obj+0x2d4) -- a meter,
#                       NOT part of the blob.
# push 0x180022dc0: if obj+0x2d0 (ctor sets it to 1, srhook 0x1800238e0 re-sets
#                   it): design 22 biquads into obj+0x7c..+0x233, emit 'inia',
#                   AND force obj+0x6c=1.0f, obj+0x70=1, obj+0x74=1.
#                   always: 'bota' = obj+0x60..0x6f, obj+0x70..0x77, obj+0x78.
# firmware dispatcher 0x6007779c accepts 'bota','inia','Bqdf','MBdf','Setu';
#   'bota' handler 0x60077b66 copies exactly 7 dwords blob+0x0c..+0x24
#   -> dev+0x18..+0x30; apply 0x600773a0 branches on dev+0x1c == 0/1/else
#   (Noise/Sawtooth/Rect) and turns dev+0x20 into a phase increment (Hz).
VOC_HP = [85.0, 141.0, 230.0, 378.0, 622.0, 1024.0, 1680.0, 2730.0,
          4450.0, 7250.0]                     # 0x180481ea0/0x180481ec0 + 2 imm
VOC_LP = [141.0, 230.0, 378.0, 622.0, 1024.0, 1690.0, 2750.0, 4482.0,
          7300.0, 11900.0]                    # 0x180481eb0/0x180481ed0 + 2 imm
VOC_BANK_Q = f32(2.0)                         # 0x180481980
VOC_TAIL = [('lp', 600.0), ('hp', 2500.0)]    # 0x180481aa8 / 0x180481ad0, Q 0.7


def fx_vocoder_filters(fs=48000.0, index=0):
    """'inia', size 0x1c4 -- the analysis/synthesis filter bank.
       22 biquads x 5 f32 = 110 floats at +0x0c (0x0c + 440 = 0x1c4).
       Order: 10 high-pass Q 2.0, then 10 low-pass Q 2.0, then LP 600 Q 0.7,
       then HP 2500 Q 0.7.  Send once, BEFORE the first fx_vocoder(), and
       again after any sample-rate change.
       Corroborated device-side: firmware 0x600779a0 walks 20 biquads with
       stride 0x14 and then handles blob+0x19c.. and blob+0x1b0.. explicitly
       -- i.e. exactly indices 20 and 21, ending at blob+0x1c4."""
    c = []
    for fr in VOC_HP:
        c += biquad_hp2(fr, fs, VOC_BANK_Q)
    for fr in VOC_LP:
        c += biquad_lp2(fr, fs, VOC_BANK_Q)
    for kind, fr in VOC_TAIL:
        c += (biquad_lp2 if kind == 'lp' else biquad_hp2)(fr, fs, f32(0.7))
    assert len(c) == 110
    b = struct.pack('<III', TAG_INIA, 0x1c4, index) + struct.pack('<110f', *c)
    assert len(b) == 0x1c4
    return b


def fx_vocoder(on=True, carrier_type=1, carrier_freq=80.0, vol=1.0, mix=0.5,
               index=0):
    """model 2, tag 'bota', size 0x28.
       carrier_type: 0 Noise, 1 Sawtooth, 2 Rect (descriptor labels, and the
       firmware's own 0/1/else branch at 0x600773c6).
       carrier_freq in Hz, descriptor 50..500 (default 100, reset 80).

       +0x1c and +0x20 are emitted as integer 1 because that is the only value
       the host ever puts there (push 0x1800231d3/0x1800231da).  No consumer
       for dev+0x28/+0x2c was located in the firmware -- MEANING UNKNOWN."""
    return struct.pack('<III', TAG_BOTA, 0x28, index) + struct.pack(
        '<IiffIIf',
        1 if on else 0,
        int(f32(f32(carrier_type) + 0.5)),
        f32(carrier_freq),
        min(max(f32(vol), 0.0), 1.0),
        1, 1,
        min(max(f32(mix), 0.0), 1.0))


# ======================================================================
# model 3 -- RING MODULATOR   tag 'botb' 0x28
# ======================================================================
# setParam 0x180023390 (jt 0x18002340c): id0 on -> obj+0x38 |
#   id1 bcarrierfreq -> obj+0x3c | id2 bcarrier2 -> (int)(v+0.5f) -> obj+0x48 |
#   id3 bcarrier2freq -> obj+0x4c | id4 bdist -> obj+0x40 |
#   id5 bvol -> obj+0x44 | id6 mix -> obj+0x50
# push 0x1800234b0: movups obj+0x38..0x47, movsd obj+0x48..0x4f, obj+0x50.
#   NO host-side arithmetic on any float (contrast the delay's feedback*0.5).
# firmware handler 0x60078dce copies 7 dwords blob+0x0c..+0x24 -> dev+0x14..+0x2c;
#   apply 0x60078b80 turns dev+0x18 into (f*256/fs)*2**24 (257-point sine
#   table) and dev+0x1c into drive 1+100x with makeup 1/(1+20x).
def fx_ringmod(on=True, carrier_hz=30.0, dist=0.5, vol=1.0,
               carrier2=False, carrier2_hz=50.0, mix=0.5, index=0):
    """model 3, tag 'botb', size 0x28.  Blob field order is NOT parameter
       order -- it is param ids 0,1,4,5,2,3,6:
         +0x0c u32 on | +0x10 f32 carrier Hz | +0x14 f32 dist | +0x18 f32 vol
         +0x1c u32 carrier2 enable | +0x20 f32 carrier2 Hz | +0x24 f32 mix
       Descriptor ranges (0.1..2000 Hz, 0..1) are UI clamps only; neither the
       push nor the firmware clamps."""
    return struct.pack('<III', TAG_BOTB, 0x28, index) + struct.pack(
        '<IfffIff',
        1 if on else 0, f32(carrier_hz), f32(dist), f32(vol),
        1 if carrier2 else 0, f32(carrier2_hz), f32(mix))


# ======================================================================
# model 4 -- FILTERS   tag 'botc' 0x28
# ======================================================================
# setParam 0x180023500 (jt 0x180023574): id0 on -> obj+0x44 |
#   id1 ctune -> obj+0x38 | id2 cfb -> obj+0x40 | id3 cdamp -> obj+0x3c |
#   id4 cdist -> obj+0x54 | id5 cvol -> obj+0x58 | id6 mix -> obj+0x5c
#   getParam 0x180023590 is the exact inverse with NO scaling -> the mirror
#   holds raw host units and the conversions below live only in the push.
# push 0x180023600 converts ctune/cfb/cdamp, packs via three shufps and emits.
# firmware handler 0x60079f6e copies 7 dwords -> dev+0x14..+0x2c; apply
#   0x60079eac compares dev+0x18 as a SIGNED INT against the delay-line
#   capacity at dev+0xd18 and reallocs -> dev+0x18 is a SAMPLE COUNT.
FILTERS_TUNE_SCALE = f32(1300.0)    # 0x180481ac0
FILTERS_TUNE_BIAS = f32(250.5)      # 0x180481a8c
FILTERS_FB_SCALE = f32(0.35)        # 0x1804818b0
FILTERS_FB_BIAS = f32(0.5)          # 0x1804818c4
FILTERS_DAMP_SCALE = f32(0.6)       # 0x1804818d8
FILTERS_DAMP_BIAS = f32(0.3)        # 0x1804818a8


def fx_filters(on=True, pitch=0.5, regeneration=0.5, damping=0.5,
               distortion=0.5, volume=1.0, mix=0.5, index=0):
    """model 4, tag 'botc', size 0x28.  A tuned feedback comb/resonator --
       no biquads and no LFO anywhere in this model.
         +0x0c u32 on
         +0x10 i32 delay length in SAMPLES = (int)(pitch*1300 + 250.5)
                                             -> 250 .. 1550
         +0x14 f32 feedback   = regeneration*0.35 + 0.5   -> 0.50 .. 0.85
         +0x18 f32 damping    = damping*0.6 + 0.3         -> 0.30 .. 0.90
         +0x1c f32 distortion (raw)   +0x20 f32 volume (raw)   +0x24 f32 mix
       UI labels: On / Pitch / Regeneration / Damping / Distortion / Volume /
       WetDry, all linear 0..1 with no taper."""
    def clamp(x):
        return min(max(f32(x), 0.0), 1.0)
    tune = int(f32(f32(clamp(pitch) * FILTERS_TUNE_SCALE) + FILTERS_TUNE_BIAS))
    fb = f32(f32(clamp(regeneration) * FILTERS_FB_SCALE) + FILTERS_FB_BIAS)
    damp = f32(f32(clamp(damping) * FILTERS_DAMP_SCALE) + FILTERS_DAMP_BIAS)
    b = struct.pack('<III', TAG_BOTC, 0x28, index) + struct.pack(
        '<Iifffff', 1 if on else 0, tune, fb, damp,
        clamp(distortion), clamp(volume), clamp(mix))
    assert len(b) == 0x28
    return b


# ======================================================================
# convenience: full wire sequence per model
# ======================================================================
def set_fx_transformer(**kw):
    return [set_fx_model(0)] + [setp(201, b) for b in fx_transformer_blobs(**kw)]


def set_fx_detuner(fs=48000.0, **kw):
    return [set_fx_model(1), setp(201, fx_detuner(**kw)),
            setp(201, fx_detuner_biquad(fs))]


def set_fx_vocoder(fs=48000.0, **kw):
    return [set_fx_model(2), setp(201, fx_vocoder_filters(fs)),
            setp(201, fx_vocoder(**kw))]


def set_fx_ringmod(**kw):
    return [set_fx_model(3), setp(201, fx_ringmod(**kw))]


def set_fx_filters(**kw):
    return [set_fx_model(4), setp(201, fx_filters(**kw))]


def set_fx_delay(on=True, time_s=0.125, feedback=0.5, mix=0.5, fs=48000.0):
    validate_delay_sample_rate(fs)
    return [set_fx_model(5), setp(201, fx_delay(on, time_s, feedback, mix))]


# ======================================================================
# convenience: steady-state updates after a model has been materialized
# ======================================================================
def update_fx_transformer(on=True, lows=0.5, width=0.5, mix=0.5,
                          fs=48000.0, index=0, refresh_tone=False):
    """Update Transformer without replaying ``VoFx``.

    On, Width and WetDry edits are one ``godv`` write.  Lows (or sample-rate)
    changes refresh the same four filter payloads UC emits before ``godv``.
    """
    blobs = []
    if refresh_tone:
        blobs.extend(fx_transformer_tone_blobs(lows, fs, index))
    blobs.append(fx_transformer(on, lows, width, mix, index))
    return [setp(BLOCK_FX, blob) for blob in blobs]


def update_fx_detuner(on=True, detune=4, mix=0.5, fs=48000.0, index=0,
                      refresh_tone=False):
    blobs = [fx_detuner(on, detune, mix, index)]
    if refresh_tone:
        blobs.append(fx_detuner_biquad(fs, index))
    return [setp(BLOCK_FX, blob) for blob in blobs]


def update_fx_vocoder(on=True, carrier_type=1, carrier_freq=80.0, vol=1.0,
                      mix=0.5, fs=48000.0, index=0,
                      refresh_filters=False):
    blobs = []
    if refresh_filters:
        blobs.append(fx_vocoder_filters(fs, index))
    blobs.append(fx_vocoder(on, carrier_type, carrier_freq, vol, mix, index))
    return [setp(BLOCK_FX, blob) for blob in blobs]


def update_fx_ringmod(**kw):
    return [setp(BLOCK_FX, fx_ringmod(**kw))]


def update_fx_filters(**kw):
    return [setp(BLOCK_FX, fx_filters(**kw))]


def update_fx_delay(on=True, time_s=0.125, feedback=0.5, mix=0.5,
                    fs=48000.0):
    validate_delay_sample_rate(fs)
    return [setp(BLOCK_FX, fx_delay(on, time_s, feedback, mix))]


# ======================================================================
# self-test -- each push routine is re-simulated from its disassembly
# ======================================================================
if __name__ == '__main__':

    # -- reference: the verified delay ------------------------------------
    d = fx_delay(True, 0.60, 0.8, 1.0)
    assert len(d) == 0x1c and d[:4] == b'hcev'
    assert struct.unpack_from('<Ifff', d, 0x0c) == (1, 1.0, f32(0.4), f32(0.60))

    # -- model 4: transcribe setParam 0x180023500 + push 0x180023600 -------
    def m4_sim(on, pitch, regen, damp, dist, vol, mix):
        m = bytearray(0x60)
        struct.pack_into('<I', m, 0x44, 1 if on >= 1.0 else 0)     # id0
        struct.pack_into('<f', m, 0x38, pitch)                     # id1
        struct.pack_into('<f', m, 0x40, regen)                     # id2
        struct.pack_into('<f', m, 0x3c, damp)                      # id3
        struct.pack_into('<f', m, 0x54, dist)                      # id4
        struct.pack_into('<f', m, 0x58, vol)                       # id5
        struct.pack_into('<f', m, 0x5c, mix)                       # id6
        x0 = f32(f32(struct.unpack_from('<f', m, 0x38)[0] * 1300.0) + 250.5)
        x2 = f32(f32(struct.unpack_from('<f', m, 0x40)[0] * 0.35) + 0.5)
        x1 = f32(f32(struct.unpack_from('<f', m, 0x3c)[0] * 0.6) + 0.3)
        struct.pack_into('<i', m, 0x48, int(x0))                   # cvttss2si
        # movups xmm0,[m+0x44]; shufps d2; movss xmm2; shufps 27; movss xmm1;
        # shufps 39  ==  {on, tune, fb, damp}
        lanes = [m[0x44:0x48], m[0x48:0x4c],
                 struct.pack('<f', x2), struct.pack('<f', x1)]
        buf = bytearray(0x28)
        struct.pack_into('<II', buf, 0, TAG_BOTC, 0x28)
        buf[0x08:0x0c] = m[0x08:0x0c]
        buf[0x0c:0x1c] = b''.join(lanes)
        buf[0x1c:0x24] = m[0x54:0x5c]                              # movsd
        buf[0x24:0x28] = m[0x5c:0x60]
        return bytes(buf)

    for args in [(1.0, 0.5, 0.5, 0.5, 0.5, 1.0, 0.5),
                 (0.0, 0.0, 1.0, 0.0, 0.25, 0.0, 1.0),
                 (1.0, 1.0, 0.0, 1.0, 1.0, 0.5, 0.0)]:
        want = fx_filters(args[0] >= 1.0, *args[1:])
        assert m4_sim(*args) == want, (m4_sim(*args).hex(), want.hex())
    assert struct.unpack_from('<i', fx_filters(pitch=0.0), 0x10)[0] == 250
    assert struct.unpack_from('<i', fx_filters(pitch=1.0), 0x10)[0] == 1550
    assert fx_filters().hex() == ('63746f62' '28000000' '00000000' '01000000'
                                  '84030000' 'cdcc2c3f' '9a99193f' '0000003f'
                                  '0000803f' '0000003f')

    # -- model 3: transcribe setParam 0x180023390 + push 0x1800234b0 -------
    def m3_sim(on, cf, c2, c2f, dist, vol, mix):
        m = bytearray(0x58)
        struct.pack_into('<I', m, 0x38, 1 if on >= 1.0 else 0)
        struct.pack_into('<f', m, 0x3c, cf)
        struct.pack_into('<i', m, 0x48, int(f32(c2 + 0.5)))
        struct.pack_into('<f', m, 0x4c, c2f)
        struct.pack_into('<f', m, 0x40, dist)
        struct.pack_into('<f', m, 0x44, vol)
        struct.pack_into('<f', m, 0x50, mix)
        buf = bytearray(0x28)
        struct.pack_into('<II', buf, 0, TAG_BOTB, 0x28)
        buf[0x08:0x0c] = m[0x08:0x0c]
        buf[0x0c:0x1c] = m[0x38:0x48]        # movups
        buf[0x1c:0x24] = m[0x48:0x50]        # movsd
        buf[0x24:0x28] = m[0x50:0x54]
        return bytes(buf)

    assert m3_sim(1.0, 30.0, 1.0, 50.0, 0.5, 1.0, 0.5) == \
        fx_ringmod(True, 30.0, 0.5, 1.0, True, 50.0, 0.5)
    assert m3_sim(0.0, 440.0, 0.0, 0.1, 0.0, 0.25, 1.0) == \
        fx_ringmod(False, 440.0, 0.0, 0.25, False, 0.1, 1.0)
    assert fx_ringmod()[:4] == b'btob' and len(fx_ringmod()) == 0x28

    # -- model 2: push 0x180022dc0 tail ------------------------------------
    def m2_sim(on, vol, ctype, cfreq, mix):
        m = bytearray(0x80)
        struct.pack_into('<I', m, 0x60, 1 if on >= 1.0 else 0)
        struct.pack_into('<f', m, 0x6c, vol)
        struct.pack_into('<i', m, 0x64, int(f32(ctype + 0.5)))
        struct.pack_into('<f', m, 0x68, cfreq)
        struct.pack_into('<f', m, 0x78, mix)
        struct.pack_into('<f', m, 0x6c, 1.0)          # 0x1800231e1 (dirty path)
        struct.pack_into('<II', m, 0x70, 1, 1)        # 0x1800231d3/0x1800231da
        buf = bytearray(0x28)
        struct.pack_into('<II', buf, 0, TAG_BOTA, 0x28)
        buf[0x08:0x0c] = m[0x08:0x0c]
        buf[0x0c:0x1c] = m[0x60:0x70]        # movups
        buf[0x1c:0x24] = m[0x70:0x78]        # movsd
        buf[0x24:0x28] = m[0x78:0x7c]
        return bytes(buf)

    assert m2_sim(1.0, 0.3, 1.0, 80.0, 0.5) == \
        fx_vocoder(True, 1, 80.0, 1.0, 0.5)      # dirty path forces avol=1.0

    def m2_sim_clean(on, vol, ctype, cfreq, mix):   # steady state, no rebuild
        m = bytearray(0x80)
        struct.pack_into('<I', m, 0x60, 1 if on >= 1.0 else 0)
        struct.pack_into('<f', m, 0x6c, vol)
        struct.pack_into('<i', m, 0x64, int(f32(ctype + 0.5)))
        struct.pack_into('<f', m, 0x68, cfreq)
        struct.pack_into('<f', m, 0x78, mix)
        struct.pack_into('<II', m, 0x70, 1, 1)
        buf = bytearray(0x28)
        struct.pack_into('<II', buf, 0, TAG_BOTA, 0x28)
        buf[0x0c:0x1c] = m[0x60:0x70]
        buf[0x1c:0x24] = m[0x70:0x78]
        buf[0x24:0x28] = m[0x78:0x7c]
        return bytes(buf)

    assert m2_sim_clean(1.0, 0.3, 2.0, 250.0, 0.75) == \
        fx_vocoder(True, 2, 250.0, 0.3, 0.75)
    v = fx_vocoder(True, 1, 80.0, 1.0, 0.5)
    assert v.hex() == ('61746f62' '28000000' '00000000' '01000000' '01000000'
                       '0000a042' '0000803f' '01000000' '01000000' '0000003f')
    fb = fx_vocoder_filters(48000.0)
    assert len(fb) == 0x1c4 and fb[:4] == b'aini'
    assert struct.unpack_from('<I', fb, 4)[0] == 0x1c4

    # -- model 1: push 0x180023740 -----------------------------------------
    def m1_sim(on, detune, mix):
        m = bytearray(0x120)
        struct.pack_into('<I', m, 0x60, 1 if on >= 1.0 else 0)
        struct.pack_into('<i', m, 0x114, int(f32(detune + 0.5)))
        struct.pack_into('<f', m, 0x74, mix)
        idx = struct.unpack_from('<i', m, 0x114)[0]
        struct.pack_into('<i', m, 0x64, DETUNE_TABLE[idx])   # 0x180023784
        buf = bytearray(0x24)
        struct.pack_into('<II', buf, 0, TAG_MAY4, 0x24)
        buf[0x08:0x0c] = m[0x08:0x0c]
        buf[0x0c:0x1c] = m[0x60:0x70]        # movups
        buf[0x1c:0x24] = m[0x70:0x78]        # movsd
        return bytes(buf)

    for i in range(9):
        assert m1_sim(1.0, float(i), 0.5) == fx_detuner(True, i, 0.5)
        assert struct.unpack_from('<i', fx_detuner(True, i, 0.0), 0x10)[0] == i - 8
    assert fx_detuner(True, 4, 0.5).hex() == (
        '3479616d' '24000000' '00000000' '01000000' 'fcffffff'
        '00000000' '00000000' '00000000' '0000003f')
    assert struct.unpack_from('<III', fx_detuner(True, 8, 0.3), 0x14) == (0, 0, 0)

    # -- model 0: push 0x180022370 -----------------------------------------
    def m0_sim(on, lows, width, mix):
        m = bytearray(0x120)
        struct.pack_into('<I', m, 0x60, 1 if on >= 1.0 else 0)
        struct.pack_into('<f', m, 0x64, lows)
        struct.pack_into('<f', m, 0x6c, lows)        # id1 writes BOTH
        struct.pack_into('<f', m, 0x68, width)
        struct.pack_into('<f', m, 0x70, mix)
        buf = bytearray(0x20)
        struct.pack_into('<II', buf, 0, TAG_GODV, 0x20)
        buf[0x08:0x0c] = m[0x08:0x0c]
        buf[0x0c:0x1c] = m[0x60:0x70]        # movups
        buf[0x1c:0x20] = m[0x70:0x74]
        return bytes(buf)

    bl = fx_transformer_blobs(True, 0.75, 0.8, 0.9)
    assert [len(x) for x in bl] == [0x24, 0x24, 0x1f4, 0x1f4, 0x20]
    assert (bl[0][:4] == b'fdqB' and bl[1][:4] == b'fdqB' and
            bl[2][:4] == b'fdBM' and bl[3][:4] == b'fdBM' and
            bl[4][:4] == b'vdog')
    assert struct.unpack_from('<I', bl[0], 0x0c)[0] == 0
    assert struct.unpack_from('<I', bl[1], 0x0c)[0] == 1
    assert struct.unpack_from('<I', bl[2], 0x1f0)[0] == 4
    assert struct.unpack_from('<I', bl[3], 0x1f0)[0] == 4
    assert m0_sim(1.0, 0.75, 0.8, 0.9) == bl[4]
    assert struct.unpack_from('<f', bl[4], 0x10)[0] == \
           struct.unpack_from('<f', bl[4], 0x18)[0]

    # -- biquads: independent textbook cross-checks ------------------------
    def ref_hp(f, fs, Q):
        K = math.tan(math.pi * f / fs); n = 1 + K / Q + K * K
        return [1 / n, 2 * (1 - K * K) / n, -2 / n, (K / Q - 1 - K * K) / n, 1 / n]

    def ref_lp(f, fs, Q):
        K = math.tan(math.pi * f / fs); n = 1 + K / Q + K * K
        return [K * K / n, 2 * (1 - K * K) / n, 2 * K * K / n,
                (K / Q - 1 - K * K) / n, K * K / n]

    def ref_ls(f, gdb, fs, Q):
        A = 10 ** (gdb / 40.0); w = 2 * math.pi * f / fs
        s, c = math.sin(w), math.cos(w); b = math.sqrt(A) / Q * s
        a0 = (A + 1) + (A - 1) * c + b
        return [A * ((A + 1) - (A - 1) * c + b) / a0,
                2 * ((A - 1) + (A + 1) * c) / a0,
                2 * A * ((A - 1) - (A + 1) * c) / a0,
                -((A + 1) + (A - 1) * c - b) / a0,
                A * ((A + 1) - (A - 1) * c - b) / a0]

    for fs in (44100.0, 48000.0, 88200.0, 96000.0):
        for fr in (85.0, 600.0, 6000.0, 11900.0):
            for Q in (0.7, 2.0):
                assert max(abs(a - b) for a, b in
                           zip(biquad_hp2(fr, fs, Q), ref_hp(fr, fs, Q))) < 3e-6
                assert max(abs(a - b) for a, b in
                           zip(biquad_lp2(fr, fs, Q), ref_lp(fr, fs, Q))) < 3e-6
            for g in (0.0, 6.0, 12.0):
                assert max(abs(a - b) for a, b in
                           zip(biquad_lowshelf(fr, g, fs, 0.7),
                               ref_ls(fr, g, fs, 0.7))) < 3e-6

    # DC gain of the shelf must equal the requested dB (catches sin/cos swaps)
    for g in (0.0, 3.0, 6.0, 12.0):
        b0, na1, b1, na2, b2 = biquad_lowshelf(600.0, g, 48000.0, 0.7)
        dc = (b0 + b1 + b2) / (1.0 - na1 - na2)
        assert abs(20 * math.log10(dc) - g) < 2e-3, (g, dc)
    # unity DC gain for the low-passes, unity Nyquist gain for the high-passes
    for fs in (44100.0, 48000.0, 96000.0):
        c = biquad_lp2(6000.0, fs, 0.7)
        assert abs((c[0] + c[2] + c[4]) / (1 - c[1] - c[3]) - 1.0) < 5e-6
        c = biquad_hp2(2500.0, fs, 0.7)
        assert abs((c[0] - c[2] + c[4]) / (1 + c[1] - c[3]) - 1.0) < 5e-6

    # -- wire framing -------------------------------------------------------
    for msgs in (set_fx_transformer(), set_fx_detuner(), set_fx_vocoder(),
                 set_fx_ringmod(), set_fx_filters(), set_fx_delay()):
        for m in msgs:
            assert struct.unpack_from('<H', m, 0)[0] == len(m)
            assert m[8:12] == b'PteS' and struct.unpack_from('<I', m, 12)[0] == 201

    print('OK -- all six models')
