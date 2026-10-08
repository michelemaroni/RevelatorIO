#!/usr/bin/env python3
"""
io24_presets — Universal Control's own factory Fat Channel presets.

These are not imitations. They are the real preset records, recovered from the
Universal Control installer (see re/extract_presets.py) and applied through the
reverse-engineered DSP calls. Every value lands inside the ranges recovered
independently from the host's parameter descriptors, which is what says the
decode is right rather than plausible-looking.

    python3 io24_presets.py                 list them
    python3 io24_presets.py "Vocal" 1       apply one to channel 1
    python3 io24_presets.py --voicefx "Reverb" 2

The preset data itself is PreSonus's, not this project's original work and not
covered by its GPL. The private research tree retains it at
re/uc_factory_presets.json, while the runtime wheel deliberately excludes it.
See README.md and PUBLICATION.md before redistributing a source tree. The Host
continues without the file and reports the factory catalog as unavailable; a
user can regenerate it from a local Universal Control installer with
re/extract_presets.py.

A preset names seven modules. Six map onto DSP this driver drives:

    opt      swapcompeq            -> set_comp_eq_order
    filter   hpf                   -> set_highpass_freq
    gate     8 parameters          -> set_gate
    comp     input/output/ratio    -> set_compressor, FET model
    eq       4 bands x 5 fields    -> set_eq_band
    limit    limiteron, threshold  -> set_limiter

The seventh, `voicefx`, is record-resident Voice FX state for block 201. The
ordinary live setter replay omits it unless ``with_fx=True`` is explicitly
requested. That opt-in assigns the shared processor to the requested physical
input, resolves UC's saved class id, selects/instantiates the model, and then
applies only that model's state. The corrected
UC 4.7.2 transaction is audibly verified for all six models on physical Input 1;
saved state still does not prove standalone VoiceFX recall without the Host.
Tagged scene records remain useful for Host-side replay, but the 2026-09-14
inactive-slot test showed that they do not install as io24 firmware 1.28 slot
bodies. ``Io24.save_device_slot()`` now accepts only a complete firmware-native
version-2 record; this module does not synthesize one from a tagged preset.
"""
import json
import math
import os
import sys

import io24_dsp
import io24_fx

_CACHE = None
PRESET_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "re", "uc_factory_presets.json")

# Exact Standard-EQ presentation contract from UC 4.7.2's embedded
# ``fatchannelxt.xml``.  ``eqallon`` is independent of the four
# ``eqbandonN`` toggles; the old Host collapsed both into ``shape=off`` and
# therefore destroyed the band state every time the whole EQ was bypassed.
# Bands 2 and 3 have no usable option field and are always parametric.  Only
# the two outer bands expose the XML's shelf-vs-parametric switch.
STANDARD_EQ_BAND_SPECS = (
    {"name": "Low", "default_freq": 130.0, "default_shape": "lowshelf",
     "shapes": ("peaking", "lowshelf")},
    {"name": "Low mid", "default_freq": 320.0, "default_shape": "peaking",
     "shapes": ("peaking",)},
    {"name": "High mid", "default_freq": 1400.0, "default_shape": "peaking",
     "shapes": ("peaking",)},
    {"name": "High", "default_freq": 5000.0, "default_shape": "highshelf",
     "shapes": ("peaking", "highshelf")},
)
STANDARD_EQ_FREQ_RANGE = (36.0, 18000.0)
STANDARD_EQ_GAIN_RANGE = (-15.0, 15.0)
STANDARD_EQ_Q_RANGE = (0.1, 10.0)
STANDARD_EQ_DEFAULT_Q = 0.6

_SHELF = {1: "lowshelf", 4: "highshelf"}

EQ_CLASSES = {
    "{A0A8A068-14F0-4B04-BB6F-AF8329D0E8EE}": "standard",
    "{C0730CBB-5135-4558-9222-C40BDBA036ED}": "passive",
    "{E1C5E024-C5CD-473C-B08A-6EC177812E01}": "vintage",
}

COMP_CLASSES = {
    "{870D04F7-212E-4F9C-ADBB-39A97216433F}": "standard",
    "{7F8A4262-D377-48E3-9D48-15D82C400A71}": "tube",
    "{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}": "fet",
}


class UnsupportedPresetModel(ValueError):
    """A stored model has no exact direct-live implementation."""


def _model(component, classes, signatures, label):
    """Resolve a UC class id, with field inference only when it is absent."""
    if not component:
        return None
    class_id = component.get("__classid")
    if class_id is not None:
        try:
            return classes[class_id]
        except KeyError:
            raise ValueError("unknown %s class id: %s" % (label, class_id))
    matches = [name for name, fields in signatures.items()
               if any(field in component for field in fields)]
    if len(matches) != 1:
        raise ValueError("%s model is missing an unambiguous class id" % label)
    return matches[0]


def eq_model(eq):
    return _model(eq, EQ_CLASSES, {
        "standard": ("eqfreq1", "eqgain1", "eqq1"),
        "passive": ("bboost", "batten", "bfreq", "mboost", "bbwidth"),
        "vintage": ("lowgain", "lowfreq", "lowmidgain", "himidgain"),
    }, "EQ")


def compressor_model(comp):
    return _model(comp, COMP_CLASSES, {
        "standard": ("threshold", "softknee", "automode"),
        "tube": ("peak", "mode"),
        "fet": ("input", "output"),
    }, "compressor")


def _require(component, fields, label):
    missing = [field for field in fields if field not in component]
    if missing:
        raise ValueError("%s missing field%s: %s" %
                         (label, "s" if len(missing) != 1 else "",
                          ", ".join(missing)))


def _finite_in_range(component, field, low, high, label):
    value = component[field]
    if isinstance(value, bool):
        raise ValueError("%s %s must be a finite number" % (label, field))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s %s must be a finite number" % (label, field))
    if not math.isfinite(number):
        raise ValueError("%s %s must be finite" % (label, field))
    # UC serializes descriptor values as float32 and then writes their full
    # decimal expansion to JSON. A value selected at the exact 0.0002 lower
    # bound therefore appears as 0.00019999999494757503. Treat a float32-sized
    # boundary error as that boundary; rejecting UC's own saved scene is not a
    # stricter interpretation of the descriptor, just a precision bug.
    if number < low and math.isclose(number, low, rel_tol=1e-6, abs_tol=1e-12):
        number = float(low)
    if number > high and math.isclose(number, high, rel_tol=1e-6, abs_tol=1e-12):
        number = float(high)
    if not low <= number <= high:
        raise ValueError("%s %s must be in [%s, %s]" %
                         (label, field, low, high))
    return number


def _toggle(component, field, label):
    value = component[field]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and \
            float(value) in (0.0, 1.0):
        return bool(value)
    raise ValueError("%s %s must be boolean or 0/1" % (label, field))


def _index(component, field, low, high, label):
    value = _finite_in_range(component, field, low, high, label)
    if not value.is_integer():
        raise ValueError("%s %s must be an integer in [%d, %d]" %
                         (label, field, low, high))
    return int(value)


def _keyfilter(component, off_sentinel, label):
    """Accept only this model's exact off value or its documented live range."""
    value = _finite_in_range(component, "keyfilter", 0.0, 16000.0, label)
    if value != off_sentinel and value < 40.0:
        raise ValueError(
            "%s keyfilter must equal the %.1f Hz off sentinel or in "
            "[40.0, 16000.0]" % (label, off_sentinel))
    return value


def _compressor_call(comp):
    """Validate and serialize the exact selected compressor before any write."""
    model = compressor_model(comp)
    if model is None:
        return None
    common = ("on", "keyfilter", "keylisten")
    if model == "standard":
        label = "Standard compressor"
        _require(comp, common + ("threshold", "ratio", "attack", "release",
                                 "gain", "softknee", "automode"),
                 label)
        index = 0
        kwargs = dict(
            on=_toggle(comp, "on", label),
            threshold_db=_finite_in_range(comp, "threshold", -56.0, 0.0, label),
            ratio=_finite_in_range(comp, "ratio", 1.0, 20.0, label),
            attack_s=_finite_in_range(comp, "attack", 0.0002, 0.15, label),
            release_s=_finite_in_range(comp, "release", 0.0025, 0.9, label),
            gain_db=_finite_in_range(comp, "gain", 0.0, 28.0, label),
            softknee=_toggle(comp, "softknee", label),
            automode=_toggle(comp, "automode", label),
            keyfilter_hz=_keyfilter(comp, 20.0, label),
            keylisten=_toggle(comp, "keylisten", label))
        builder = io24_dsp.cpxt_comp
    elif model == "tube":
        label = "Tube compressor"
        _require(comp, common + ("peak", "gain", "mode"), label)
        index = 1
        kwargs = dict(
            on=_toggle(comp, "on", label),
            peak=_finite_in_range(comp, "peak", 0.0, 100.0, label),
            gain=_finite_in_range(comp, "gain", 0.0, 100.0, label),
            limit_mode=_toggle(comp, "mode", label),
            keyfilter_hz=_keyfilter(comp, 0.0, label),
            keylisten=_toggle(comp, "keylisten", label))
        builder = io24_dsp.cpxt_tube
    else:
        label = "FET compressor"
        _require(comp, common + ("input", "output", "attack", "release", "ratio"),
                 label)
        index = 2
        kwargs = dict(
            on=_toggle(comp, "on", label),
            input_db=_finite_in_range(comp, "input", -56.0, 0.0, label),
            output_db=_finite_in_range(comp, "output", -56.0, 0.0, label),
            attack_s=_finite_in_range(comp, "attack", 0.000021, 0.0008, label),
            release_s=_finite_in_range(comp, "release", 0.05, 1.1, label),
            ratio_index=_index(comp, "ratio", 0, 4, label),
            keyfilter_hz=_keyfilter(comp, 20.0, label),
            keylisten=_toggle(comp, "keylisten", label))
        builder = io24_dsp.cpxt_fet

    blob = builder(index=0, fs=48000.0, **kwargs)
    if not isinstance(blob, bytes) or len(blob) != 0x40:
        raise ValueError("%s builder did not serialize a 64-byte cpxt blob" % label)
    return index, kwargs


def _standard_eq_bands(eq):
    """Validate and decode all four Standard bands before any device write.

    ``shape`` is the coefficient shape to send now, so it is ``off`` while
    either the complete EQ or that band is bypassed.  ``mode`` and ``on`` keep
    the two independent XML controls intact for the GTK editor and preset
    round trip.
    """
    _require(eq, ("eqallon",) +
             tuple("eqbandon%d" % band for band in range(1, 5)) +
             tuple("eqfreq%d" % band for band in range(1, 5)) +
             tuple("eqgain%d" % band for band in range(1, 5)) +
             tuple("eqq%d" % band for band in range(1, 5)) +
             ("eqbandop1", "eqbandop4"), "Standard EQ")
    eq_all_on = _toggle(eq, "eqallon", "Standard EQ")
    band_on = {
        band: _toggle(eq, "eqbandon%d" % band, "Standard EQ")
        for band in range(1, 5)
    }
    shelf_on = {
        band: _toggle(eq, "eqbandop%d" % band, "Standard EQ")
        for band in (1, 4)
    }
    bands = []
    for band in range(1, 5):
        spec = STANDARD_EQ_BAND_SPECS[band - 1]
        freq = _finite_in_range(
            eq, "eqfreq%d" % band, *STANDARD_EQ_FREQ_RANGE, "Standard EQ")
        gain = _finite_in_range(
            eq, "eqgain%d" % band, *STANDARD_EQ_GAIN_RANGE, "Standard EQ")
        q = _finite_in_range(
            eq, "eqq%d" % band, *STANDARD_EQ_Q_RANGE, "Standard EQ")
        mode = _SHELF[band] if band in _SHELF and \
            shelf_on[band] else "peaking"
        if mode not in spec["shapes"]:
            raise ValueError("Standard EQ band %d has invalid mode %s" %
                             (band, mode))
        shape = mode if eq_all_on and band_on[band] else "off"
        bands.append({
            "shape": shape,
            "mode": mode,
            "on": bool(band_on[band]),
            "freq": freq,
            "gain": gain,
            "q": q,
        })
    return bands


def standard_eq_enabled(preset):
    """The independent ``eqallon`` state of a Standard preset."""
    eq = preset.get("eq") or {}
    model = eq_model(eq)
    if model is None:
        return False
    if model != "standard":
        raise UnsupportedPresetModel(
            "%s EQ does not use the Standard power control" % model.title())
    _require(eq, ("eqallon",), "Standard EQ")
    return bool(_toggle(eq, "eqallon", "Standard EQ"))


def standard_band_mode(band, index):
    """Return one band's stored Standard shape, independent of its power."""
    try:
        spec = STANDARD_EQ_BAND_SPECS[int(index)]
    except (IndexError, TypeError, ValueError):
        raise ValueError("Standard EQ band index must be 0..3")
    if not isinstance(band, dict):
        raise ValueError("Standard EQ band %d must be an object" % (index + 1))
    mode = band.get("mode")
    if mode is None:
        mode = band.get("shape")
    if mode in (None, "off"):
        mode = spec["default_shape"]
    if mode not in spec["shapes"]:
        raise UnsupportedPresetModel(
            "EQ band %d shape %s has no Standard-EQ XML mapping" %
            (index + 1, mode))
    return mode


def standard_band_enabled(band):
    """Return the XML band-power state, accepting the older shape-only form."""
    if not isinstance(band, dict):
        raise ValueError("Standard EQ band must be an object")
    if "on" in band:
        value = band["on"]
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError("Standard EQ band on must be a toggle")
    return band.get("shape") != "off"


def default_standard_eq_bands():
    """UC 4.7.2's four band defaults; the complete EQ defaults to bypassed."""
    return [
        {"shape": spec["default_shape"], "mode": spec["default_shape"],
         "on": True, "freq": spec["default_freq"], "gain": 0.0,
         "q": STANDARD_EQ_DEFAULT_Q}
        for spec in STANDARD_EQ_BAND_SPECS
    ]


def default_passive_eq(on=False):
    """UC 4.7.2's exact Passive Program EQ defaults."""
    import io24_alt_eq
    return io24_alt_eq.default_eq("passive", on=on)


def default_vintage_eq(on=False):
    """UC 4.7.2's exact Vintage EQ defaults."""
    import io24_alt_eq
    return io24_alt_eq.default_eq("vintage", on=on)


def validate_alternate_eq(eq):
    """Validate and normalize a complete Passive or Vintage section."""
    import io24_alt_eq
    return io24_alt_eq.validate_eq(eq)


def direct_apply_support(preset):
    """Whether a factory record has an exact direct-live Host mapping."""
    try:
        comp = preset.get("comp") or {}
        if comp:
            _compressor_call(comp)
        eq = preset.get("eq") or {}
        model = eq_model(eq)
        if model in ("passive", "vintage"):
            import io24_alt_eq
            normalized = io24_alt_eq.validate_eq(eq)
            if normalized["eqallon"]:
                available, detail = io24_alt_eq.designer_status()
                if not available:
                    return False, detail
        if model == "standard":
            _standard_eq_bands(eq)
    except ValueError as error:
        return False, str(error)
    return True, "exact direct-live mapping"


def alternate_eq_view(preset):
    """Editable semantic view of a Passive/Vintage body, else ``None``.

    Labels, defaults, and switch positions come from UC 4.7.2's embedded
    component XML.  The returned ``eq`` is strict and normalized so UI edits,
    direct application, and preset round trips share one representation.
    """
    eq = preset.get("eq") or {}
    if not eq:
        return None
    try:
        model = eq_model(eq)
    except ValueError:
        return None
    if model not in ("passive", "vintage"):
        return None
    import io24_alt_eq
    eq = io24_alt_eq.validate_eq(eq)

    def switch(field, table):
        pos = int(round(float(eq.get(field, 0))))
        if not 0 <= pos < len(table):
            return "position %d (outside the %d-way switch)" % (
                pos + 1, len(table))
        return "position %d of %d (%g Hz)" % (
            pos + 1, len(table), table[pos])

    if model == "vintage":

        def gain(field):
            return "%+.1f dB" % float(eq.get(field, 0.0))

        rows = [
            ("Low shelf", switch("lowfreq", io24_alt_eq.VINTAGE_LOW_HZ),
             gain("lowgain")),
            ("Low mid", switch("lowmidfreq", io24_alt_eq.VINTAGE_LOWMID_HZ),
             gain("lowmidgain")),
            ("High mid", switch("himidfreq", io24_alt_eq.VINTAGE_HIMID_HZ),
             gain("himidgain")),
            ("High shelf", "fixed frequency", gain("higain")),
        ]
    else:
        def amount(field):
            return "%.1f / 10" % float(eq.get(field, 0.0))

        low = switch("bfreq", io24_alt_eq.PASSIVE_LOW_HZ)
        rows = [
            ("Low boost", low, amount("bboost")),
            ("Low attenuation", low, amount("batten")),
            ("High boost",
             switch("mfreq", io24_alt_eq.PASSIVE_HIGH_BOOST_HZ),
             "%s; bandwidth %s" %
             (amount("mboost"), amount("bbwidth"))),
            ("High attenuation",
             switch("hsfreq", io24_alt_eq.PASSIVE_HIGH_ATTEN_HZ),
             amount("hatten")),
        ]
    return {"model": model, "on": bool(eq.get("eqallon", 1)),
            "rows": rows, "eq": dict(eq)}


def load(path=None):
    """Every factory preset, de-duplicated by content, keyed by name."""
    global _CACHE
    if _CACHE is not None and path is None:
        return _CACHE
    p = path or PRESET_JSON
    if not os.path.exists(p):
        raise SystemExit(
            "no preset data at %s\n"
            "Generate it from your own installer:\n"
            "  python3 re/extract_presets.py <nsis_payload.bin> %s" % (p, p))
    with open(p) as stream:
        raw = json.load(stream)
    out, seen = {}, set()
    for r in raw:
        body = json.dumps({k: v for k, v in r.items() if k != "_offset"},
                          sort_keys=True)
        if body in seen:
            continue
        seen.add(body)
        name = r["preset_name"]
        n, base = name, name
        k = 2
        while n in out:                       # a few names recur per product
            n = "%s (%d)" % (base, k)
            k += 1
        out[n] = r
    if path is None:
        _CACHE = out
    return out


def names():
    return sorted(load())


# ---------------------------------------------------------------- user presets
# The user's own presets live on this computer, in the same list-of-records
# format as the factory file, so one loader and one apply path serve both.
USER_PRESETS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "io24", "user-presets.json")


def _user_path(path):
    return str(path) if path else USER_PRESETS_PATH


def _preset_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a preset needs a name")
    return name.strip()


def _read_user_records(path=None):
    p = _user_path(path)
    if not os.path.exists(p):
        return []
    with open(p) as stream:
        text = stream.read()
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise ValueError("the saved presets in %s are damaged: %s"
                         % (p, error)) from error
    if not isinstance(raw, list) or not all(
            isinstance(r, dict) and isinstance(r.get("preset_name"), str)
            for r in raw):
        raise ValueError("the saved presets in %s are not a list of named "
                         "presets" % p)
    return raw


def _write_user_records(records, path=None):
    """Write via a temp file and rename, so a crash never leaves half a file."""
    p = _user_path(path)
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as stream:
        json.dump(records, stream, indent=1)
    os.replace(tmp, p)


def load_user_presets(path=None):
    """The user's own presets, keyed by name, in the order they were saved.

    A missing file is simply no presets; a damaged one is refused rather than
    read as empty, so a later save cannot silently overwrite it.
    """
    return {r["preset_name"]: r for r in _read_user_records(path)}


def save_user_preset(name, record, path=None):
    """Store one preset under ``name``, replacing a same-named one in place."""
    name = _preset_name(name)
    records = _read_user_records(path)
    record = dict(record)
    record["preset_name"] = name
    for index, existing in enumerate(records):
        if existing["preset_name"] == name:
            records[index] = record
            break
    else:
        records.append(record)
    _write_user_records(records, path)


def delete_user_preset(name, path=None):
    records = _read_user_records(path)
    kept = [r for r in records if r["preset_name"] != name]
    if len(kept) == len(records):
        raise KeyError(name)
    _write_user_records(kept, path)


def rename_user_preset(old, new, path=None):
    new = _preset_name(new)
    records = _read_user_records(path)
    names = [r["preset_name"] for r in records]
    if old not in names:
        raise KeyError(old)
    if new != old and new in names:
        raise ValueError("a preset named %r already exists" % new)
    for record in records:
        if record["preset_name"] == old:
            record["preset_name"] = new
    _write_user_records(records, path)


def describe(p):
    """A one-line summary, for a UI list."""
    bits = []
    if p.get("gate", {}).get("on"):
        bits.append("Gate")
    if p.get("comp", {}).get("on"):
        try:
            bits.append("%s Comp" % compressor_model(p["comp"]).title())
        except ValueError:
            bits.append("Compressor")
    if p.get("eq", {}).get("eqallon"):
        try:
            model = eq_model(p["eq"])
            bits.append("%s EQ" % model.title())
        except ValueError:
            bits.append("EQ")
    if p.get("limit", {}).get("limiteron"):
        bits.append("Limiter")
    if p.get("voicefx", {}).get("on"):
        bits.append("Voice FX")
    return ", ".join(bits) or "Flat"


def apply_preset(dev, preset, channel=1, with_fx=False,
                 establish_return=True, return_db=0.0, fs=None):
    """Send a factory preset to a channel. Returns a list of what was applied.

    ``with_fx`` is an explicit opt-in to assign the one block-201 processor to
    the selected physical input, then restore its model state. The assignment
    uses UC's ``processingChannel`` route and remains separate from the preset
    control namespace. ``establish_return`` is retained for call compatibility
    but ignored: the UC 4.7.2 capture changes no mixer return as part of a
    VoiceFX transaction.
    """
    if isinstance(preset, str):
        preset = load()[preset]
    # Historical Fat Channel callers used the 48 kHz default. Keep that
    # compatibility for coefficient design, but never let an omitted rate
    # authorize a live Voice FX transaction. Delay at an assumed 48 kHz could
    # otherwise reach a unit that is actually running at 96 kHz.
    design_fs = 48000.0 if fs is None else fs
    rate_kw = {} if fs is None else {"fs": design_fs}
    fx = preset.get("voicefx") or {}
    # Validate the complete VoiceFX object before changing any Fat Channel
    # state. A malformed opt-in must fail atomically at the host boundary.
    fx_call = io24_fx.voicefx_preset_call(fx) if with_fx and fx else None
    if fx_call is not None:
        model, kwargs = fx_call
        fx_call = (model, io24_fx.voicefx_runtime_kwargs(
            model, kwargs, fs))
    comp = preset.get("comp") or {}
    comp_call = _compressor_call(comp) if comp else None
    eq = preset.get("eq") or {}
    eq_kind = eq_model(eq)
    alternate_eq = validate_alternate_eq(eq) \
        if eq_kind in ("passive", "vintage") else None
    if alternate_eq is not None and alternate_eq["eqallon"]:
        import io24_alt_eq
        available, detail = io24_alt_eq.designer_status()
        if not available:
            raise UnsupportedPresetModel(detail)
    eq_bands = _standard_eq_bands(eq) if eq_kind == "standard" else None
    done = []

    opt = preset.get("opt") or {}
    if "swapcompeq" in opt:
        dev.set_comp_eq_order(channel, bool(opt["swapcompeq"]))
        done.append("order=%s" % ("eq-first" if opt["swapcompeq"] else "comp-first"))

    filt = preset.get("filter") or {}
    if "hpf" in filt:
        dev.set_highpass_freq(channel, float(filt["hpf"]), **rate_kw)
        done.append("hpf=%.0fHz" % filt["hpf"])

    g = preset.get("gate") or {}
    if g:
        if g.get("on"):
            dev.set_gate(channel, on=True,
                         threshold_db=float(g.get("threshold", -40)),
                         range_db=float(g.get("range", -60)),
                         attack_s=float(g.get("attack", 0.005)),
                         release_s=float(g.get("release", 0.3)),
                         keyfilter_hz=float(g.get("keyfilter", 0.0)),
                         expander=bool(g.get("expander", 1)),
                         keylisten=bool(g.get("keylisten", 0)),
                         **rate_kw)
            done.append("gate")
        else:
            dev.gate_off(channel)

    if comp_call is not None:
        comp_index, comp_kwargs = comp_call
        if comp_kwargs["on"]:
            dev.set_compressor(
                channel, model=comp_index, **comp_kwargs, **rate_kw)
            done.append("comp(%s)" % compressor_model(comp).title())
        else:
            dev.compressor_off(channel)

    if eq_bands is not None:
        if bool(eq["eqallon"]):
            for band, values in enumerate(eq_bands):
                dev.set_eq_band(channel, band, values["shape"],
                                freq_hz=values["freq"],
                                gain_db=values["gain"], q=values["q"],
                                **rate_kw)
            done.append("eq(4)")
        else:
            dev.eq_off(channel)
    elif alternate_eq is not None:
        dev.set_alternate_eq(channel, alternate_eq, fs=design_fs)
        done.append("eq(%s)" % eq_kind)

    lm = preset.get("limit") or {}
    if lm:
        dev.set_limiter(channel, bool(lm.get("limiteron")),
                        float(lm.get("threshold", -28)), **rate_kw)
        if lm.get("limiteron"):
            done.append("limiter")

    if fx_call is not None:
        done.append(_apply_voicefx_call(
            dev, fx_call, establish_return, channel=channel))
    elif fx.get("on"):
        done.append("[voicefx retained for device-slot save; direct replay omitted]")
    return done


def _apply_voicefx_call(dev, fx_call, establish_return, return_db=0.0,
                        bus="main", channel_mix=None, channel=1):
    """Apply the captured UC VoiceFX transaction without mixer side effects."""
    _ = establish_return, return_db, bus, channel_mix
    model, kwargs = fx_call
    dev.set_voicefx_channel(channel)
    writes = dev.set_fx(model, **kwargs)
    state = "" if kwargs["on"] else " off"
    how = "UC 4.7.2 state transaction" if kwargs["on"] else "off"
    return "voicefx=%s%s (%d writes; %s)" % (model, state, writes, how)


def apply_voicefx(dev, preset, establish_return=True, return_db=0.0,
                  bus="main", channel_mix=None, channel=1, fs=None):
    """Assign and apply only a preset's Voice FX state.

    Deliberately separate from the Fat Channel replay, so an ordinary preset
    load still completes if the VoiceFX transaction fails. Malformed FX and a
    missing runtime rate fail before any write. Legacy return arguments are
    accepted but ignored.
    """
    if isinstance(preset, str):
        preset = load()[preset]
    fx = preset.get("voicefx") or {}
    if not fx:
        return None
    model, kwargs = io24_fx.voicefx_preset_call(fx)
    fx_call = (model, io24_fx.voicefx_runtime_kwargs(model, kwargs, fs))
    return _apply_voicefx_call(
        dev, fx_call, establish_return,
        return_db=return_db, bus=bus, channel_mix=channel_mix,
        channel=channel)


def to_bands(preset):
    """The preset's four EQ bands in this driver's own shape, for the UI."""
    eq = preset.get("eq") or {}
    model = eq_model(eq)
    if model in ("passive", "vintage"):
        raise UnsupportedPresetModel(
            "%s EQ cannot be represented as four Standard bands" % model.title())
    if model is None:
        return default_standard_eq_bands()
    bands = _standard_eq_bands(eq)
    # The editor keeps band power independent of complete-EQ power.  Restore
    # the stored band shape here; coefficient builders continue to consume the
    # effective ``shape`` returned directly by _standard_eq_bands().
    for band in bands:
        band["shape"] = band["mode"] if band["on"] else "off"
    return bands


def _standard_slot_eq(bands, eq_on=None):
    """Four Host bands as one complete Standard EQ record section."""
    if not isinstance(bands, (list, tuple)) or len(bands) != 4:
        raise ValueError("current slot EQ must contain four bands")
    band_states = [standard_band_enabled(band) for band in bands]
    eq = {
        "__classid": next(key for key, value in EQ_CLASSES.items()
                          if value == "standard"),
        "eqallon": int(any(band_states) if eq_on is None else bool(eq_on)),
    }
    for index, band in enumerate(bands, 1):
        if not isinstance(band, dict) or not {
                "shape", "freq", "gain", "q"}.issubset(band):
            raise ValueError("current slot EQ band %d is incomplete" % index)
        mode = standard_band_mode(band, index - 1)
        shape = band["shape"]
        if shape != "off" and shape != mode:
            raise ValueError(
                "EQ band %d shape and stored mode disagree" % index)
        eq["eqbandon%d" % index] = int(band_states[index - 1])
        eq["eqfreq%d" % index] = float(band["freq"])
        eq["eqgain%d" % index] = float(band["gain"])
        eq["eqq%d" % index] = float(band["q"])
        if index in (1, 4):
            shelf = "lowshelf" if index == 1 else "highshelf"
            eq["eqbandop%d" % index] = int(mode == shelf)
    _standard_eq_bands(eq)
    return eq


def validate_standard_eq_host_state(state):
    """Validate the GTK-only state needed for independent EQ/band bypass.

    The device shadow records the coefficients actually sent.  When the EQ is
    bypassed those are four identities and cannot remember the four settings
    behind the switch, so this small semantic layer belongs in Host snapshots.
    """
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("standard_eq must be a version-1 object")
    channels = state.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("standard_eq channels must be an object")
    unknown = set(channels) - {"1", "2"}
    if unknown:
        raise ValueError("standard_eq has unknown channel%s" %
                         ("s" if len(unknown) != 1 else ""))
    normalized = {"version": 1, "channels": {}}
    for channel in ("1", "2"):
        if channel not in channels:
            continue
        body = channels[channel]
        if not isinstance(body, dict) or set(body) != {"on", "bands"}:
            raise ValueError(
                "standard_eq channel %s must contain on and bands" % channel)
        if not isinstance(body["on"], bool):
            raise ValueError("standard_eq channel %s on must be boolean" % channel)
        eq = _standard_slot_eq(body["bands"], eq_on=body["on"])
        normalized["channels"][channel] = {
            "on": body["on"],
            "bands": to_bands({"eq": eq}),
        }
    return normalized


def validate_alternate_eq_host_state(state):
    """Validate Host semantic state for independently selected alt EQ models."""
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("alternate_eq must be a version-1 object")
    channels = state.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("alternate_eq channels must be an object")
    unknown = set(channels) - {"1", "2"}
    if unknown:
        raise ValueError("alternate_eq has unknown channel%s" %
                         ("s" if len(unknown) != 1 else ""))
    normalized = {"version": 1, "channels": {}}
    for channel in ("1", "2"):
        if channel in channels:
            normalized["channels"][channel] = validate_alternate_eq(
                channels[channel])
    return normalized


def current_slot_record(base, preset_name, *, bands, hpf_hz, eq_first,
                        gate, compressor_model, compressor, limiter,
                        voicefx_model, voicefx, alternate_eq=None, eq_on=None):
    """Overlay current Host controls onto one explicit complete slot base.

    The base supplies archive sections that the live UI does not model.  Every
    Fat Channel and Voice FX field that the UI *does* own is replaced, never
    merged with a stale model.  The result remains a complete-body candidate;
    it does not imply device readback, selection, recall, or audibility.

    ``alternate_eq`` is an editable Passive/Vintage semantic section.  When
    given it is validated and stored instead of the Standard bands.
    """
    from io24_preset_record import complete_device_slot_record

    record = complete_device_slot_record(base)
    if not isinstance(preset_name, str) or not preset_name.strip():
        raise ValueError("current slot preset name must be non-empty text")
    record["preset_name"] = preset_name.strip()
    record["opt"] = {"swapcompeq": int(bool(eq_first))}
    record["filter"] = {"hpf": float(hpf_hz)}

    if alternate_eq is not None:
        record["eq"] = validate_alternate_eq(alternate_eq)
    else:
        record["eq"] = _standard_slot_eq(bands, eq_on=eq_on)

    gate_fields = {
        "on", "threshold_db", "range_db", "attack_s", "release_s",
        "keyfilter_hz", "keylisten", "expander",
    }
    if not isinstance(gate, dict) or set(gate) != gate_fields:
        raise ValueError("current slot gate state is incomplete")
    record["gate"] = {
        "on": int(bool(gate["on"])),
        "keylisten": int(bool(gate["keylisten"])),
        "expander": int(bool(gate["expander"])),
        "keyfilter": float(gate["keyfilter_hz"]),
        "threshold": float(gate["threshold_db"]),
        "range": float(gate["range_db"]),
        "attack": float(gate["attack_s"]),
        "release": float(gate["release_s"]),
    }

    try:
        comp_name = ("standard", "tube", "fet")[int(compressor_model)]
    except (IndexError, TypeError, ValueError):
        raise ValueError("current slot compressor model must be 0, 1, or 2")
    if not isinstance(compressor, dict):
        raise ValueError("current slot compressor state must be an object")
    common = {
        "on": int(bool(compressor["on"])),
        "keyfilter": float(compressor["keyfilter_hz"]),
        "keylisten": int(bool(compressor["keylisten"])),
        "__classid": next(key for key, value in COMP_CLASSES.items()
                          if value == comp_name),
    }
    if common["keyfilter"] <= 0.0 and comp_name in ("standard", "fet"):
        common["keyfilter"] = 20.0
    if comp_name == "standard":
        common.update({
            "threshold": float(compressor["threshold_db"]),
            "ratio": float(compressor["ratio"]),
            "attack": float(compressor["attack_s"]),
            "release": float(compressor["release_s"]),
            "gain": float(compressor["gain_db"]),
            "softknee": int(bool(compressor["softknee"])),
            "automode": int(bool(compressor["automode"])),
        })
    elif comp_name == "tube":
        common.update({
            "peak": float(compressor["peak"]),
            "gain": float(compressor["gain"]),
            "mode": int(bool(compressor["limit_mode"])),
        })
    else:
        common.update({
            "input": float(compressor["input_db"]),
            "output": float(compressor["output_db"]),
            "attack": float(compressor["attack_s"]),
            "release": float(compressor["release_s"]),
            "ratio": int(compressor["ratio_index"]),
        })
    _compressor_call(common)
    record["comp"] = common

    if not isinstance(limiter, dict) or set(limiter) != {"on", "threshold_db"}:
        raise ValueError("current slot limiter state is incomplete")
    record["limit"] = {
        "limiteron": int(bool(limiter["on"])),
        "threshold": float(limiter["threshold_db"]),
    }
    record["voicefx"] = io24_fx.voicefx_preset_state(
        voicefx_model, **voicefx)
    return complete_device_slot_record(record)


def main():
    ps = load()
    argv = sys.argv[1:]
    with_fx = "--voicefx" in argv
    argv = [value for value in argv if value != "--voicefx"]
    if not argv:
        print("%d factory presets:\n" % len(ps))
        for n in names():
            print("  %-22s %s" % (n, describe(ps[n])))
        print("\n  apply strip:  python3 io24_presets.py \"Vocal\" [channel]")
        print("  apply strip plus explicit VoiceFX model/state:")
        print("                python3 io24_presets.py --voicefx \"Reverb\" [channel]")
        return
    name = argv[0]
    ch = int(argv[1]) if len(argv) > 1 else 1
    if name not in ps:
        raise SystemExit("no preset %r — try: %s" % (name, ", ".join(names()[:8])))
    from io24 import Io24
    d = Io24()
    try:
        applied = apply_preset(d, ps[name], ch, with_fx=with_fx)
        print("applied %r to channel %d: %s" % (name, ch, ", ".join(applied)))
    finally:
        d.close()


if __name__ == "__main__":
    main()
