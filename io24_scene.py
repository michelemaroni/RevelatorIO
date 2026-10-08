#!/usr/bin/env python3
"""Save or apply a Universal Control `.scene` with the native Linux Host.

The importer follows UC 4.7.2's component model rather than treating a scene
as a channel preset. It restores global settings, both Fat Channels, the full
mixer matrix, output masters and mutes, shared reverb, and the one shared
VoiceFX processor. Passive and Vintage EQ use the exact retained UC 4.7.2
coefficient designers already used by the Host.

    python3 io24_scene.py --sample-rate 48000 "B A S E.scene"
    python3 io24_scene.py --dry-run --sample-rate 96000 Main.scene
    python3 io24_scene.py --export io24-host.scene

Device-resident preset records are retained in scene files when their complete
bodies are known to this Host, but scene load never overwrites the device
library. A failed load compensates from a pre-write checkpoint and reports when
unknown prior state prevents a complete rollback.
Fields that the io24 protocol cannot faithfully vary are reported with a
specific reason. Their fixed ordinary values are accepted as satisfied:
centred mono pan, FXA off, post-DSP capture and stereo output. UC component
names and Mirror Main are persisted as Host state because neither requires
inventing a missing device command.
"""
import argparse
import copy
import json
import math
import os
import tempfile

import io24_alt_eq
import io24_dsp
import io24_fx
import io24_presets
import io24_voicefx_delay


SOURCE = {1: "line/ch1", 2: "line/ch2"}

# Public compatibility constants used by protocol tests and by people
# inspecting scene files. Model dispatch itself is centralized in
# ``io24_presets`` so the importer cannot drift from the preset loader.
COMP_MODELS = {
    "{870D04F7-212E-4F9C-ADBB-39A97216433F}": 0,
    "{7F8A4262-D377-48E3-9D48-15D82C400A71}": 1,
    "{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}": 2,
}
EQ_PARAMETRIC = "{A0A8A068-14F0-4B04-BB6F-AF8329D0E8EE}"
EQ_PASSIVE = "{C0730CBB-5135-4558-9222-C40BDBA036ED}"
EQ_VINTAGE = "{E1C5E024-C5CD-473C-B08A-6EC177812E01}"

VINTAGE_LOW_HZ = (35.0, 60.0, 110.0, 220.0)
VINTAGE_LOWMID_HZ = (360.0, 700.0, 1600.0)
VINTAGE_HIMID_HZ = (3200.0, 4800.0, 7200.0)
VINTAGE_HIGH_HZ = 12000.0
PASSIVE_LOW_HZ = (20.0, 30.0, 60.0, 100.0)
PASSIVE_HIGH_BOOST_HZ = (3000.0, 4000.0, 5000.0, 8000.0,
                         10000.0, 12000.0, 16000.0)
PASSIVE_HIGH_ATTEN_HZ = (5000.0, 10000.0, 20000.0)


def load(path):
    """Read one UC scene, accepting the BOM emitted by some UC versions."""
    with open(path, encoding="utf-8-sig") as handle:
        scene = json.load(handle)
    if not isinstance(scene, dict):
        raise ValueError("scene root must be an object")
    return scene


def _object(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % label)
    return value


def _number(value, label, low=None, high=None):
    if isinstance(value, bool):
        raise ValueError("%s must be a finite number" % label)
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a finite number" % label)
    if not math.isfinite(value):
        raise ValueError("%s must be finite" % label)
    if (low is not None and value < low) or \
            (high is not None and value > high):
        raise ValueError("%s must be in [%s, %s]" % (label, low, high))
    return value


def _integer(value, label, low=None, high=None):
    number = _number(value, label, low, high)
    if not number.is_integer():
        raise ValueError("%s must be an integer" % label)
    return int(number)


def _toggle(value, label):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and \
            math.isfinite(float(value)) and float(value) in (0.0, 1.0):
        return bool(value)
    raise ValueError("%s must be boolean or 0/1" % label)


def _text(value, label):
    if not isinstance(value, str):
        raise ValueError("%s must be text" % label)
    value = value.strip()
    if len(value) > 64:
        raise ValueError("%s must be at most 64 characters" % label)
    return value


def _call(calls, method, args, kwargs, description):
    calls.append((method, tuple(args), dict(kwargs), description))


def _unknown_fields(component, known, prefix, skips):
    for field in sorted(set(component) - set(known)):
        skips.append("%s.%s is not in the UC 4.7.2 io24 component model" %
                     (prefix, field))


def _routing_calls(component, source, prefix, calls):
    for field, bus, assign in (
            ("volume", "main", "lr"),
            ("aux1", "mixa", "assign_aux1"),
            ("aux2", "mixb", "assign_aux2")):
        if field in component:
            level = _number(component[field], "%s.%s" % (prefix, field),
                            -145.0, 10.0)
            _call(calls, "set_send_db", (source, bus, level), {},
                  "%s -> %s = %.1f dB" % (prefix, bus, level))
        if assign in component:
            on = _toggle(component[assign], "%s.%s" % (prefix, assign))
            _call(calls, "set_send_assigned", (source, bus, on), {},
                  "%s -> %s assigned = %s" % (prefix, bus, on))


def _plan_standard_eq(channel, eq, prefix, fs, calls):
    bands = io24_presets._standard_eq_bands(eq)
    for index, values in enumerate(bands):
        kwargs = {
            "shape": values["shape"], "freq_hz": values["freq"],
            "gain_db": values["gain"], "q": values["q"], "fs": fs,
        }
        _call(calls, "set_eq_band", (channel, index), kwargs,
              "%s Standard EQ band %d %s %.0f Hz %+.1f dB Q%.2f" %
              (prefix, index + 1, values["shape"], values["freq"],
               values["gain"], values["q"]))


def _plan_processing(channel, component, prefix, fs, calls, voicefx, skips,
                     allow_host_delay=False):
    opt = _object(component.get("opt"), "%s.opt" % prefix)
    if "swapcompeq" in opt:
        eq_first = _toggle(opt["swapcompeq"], "%s.opt.swapcompeq" % prefix)
        _call(calls, "set_comp_eq_order", (channel, eq_first), {},
              "%s order = %s" %
              (prefix, "EQ then compressor" if eq_first
               else "compressor then EQ"))
    _unknown_fields(opt, {"swapcompeq"}, "%s.opt" % prefix, skips)

    filt = _object(component.get("filter"), "%s.filter" % prefix)
    if "hpf" in filt:
        hpf = _number(filt["hpf"], "%s.filter.hpf" % prefix, 24.0, 1000.0)
        _call(calls, "set_highpass_freq", (channel, hpf), {"fs": fs},
              "%s Fat Channel HPF = %.0f Hz" % (prefix, hpf))
    _unknown_fields(filt, {"hpf"}, "%s.filter" % prefix, skips)

    gate = _object(component.get("gate"), "%s.gate" % prefix)
    if gate:
        kwargs = {
            "on": _toggle(gate.get("on", 1), "%s.gate.on" % prefix),
            "threshold_db": _number(gate.get("threshold", -40.0),
                                    "%s.gate.threshold" % prefix),
            "range_db": _number(gate.get("range", -60.0),
                                "%s.gate.range" % prefix),
            "attack_s": _number(gate.get("attack", 0.01),
                                "%s.gate.attack" % prefix, 0.0),
            "release_s": _number(gate.get("release", 0.3),
                                 "%s.gate.release" % prefix, 0.0),
            "keyfilter_hz": _number(gate.get("keyfilter", 0.0),
                                    "%s.gate.keyfilter" % prefix, 0.0),
            "expander": _toggle(gate.get("expander", 1),
                                "%s.gate.expander" % prefix),
            "keylisten": _toggle(gate.get("keylisten", 0),
                                 "%s.gate.keylisten" % prefix),
            "fs": fs,
        }
        io24_dsp.gate_blob(
            0, kwargs["on"], kwargs["threshold_db"], kwargs["range_db"],
            kwargs["attack_s"], kwargs["release_s"], kwargs["keyfilter_hz"],
            kwargs["expander"], kwargs["keylisten"], fs)
        _call(calls, "set_gate", (channel,), kwargs,
              "%s gate on=%s threshold=%.1f dB" %
              (prefix, kwargs["on"], kwargs["threshold_db"]))
    _unknown_fields(gate, {
        "on", "threshold", "range", "attack", "release", "keyfilter",
        "expander", "keylisten",
    }, "%s.gate" % prefix, skips)

    comp = _object(component.get("comp"), "%s.comp" % prefix)
    if comp:
        decoded = io24_presets._compressor_call(comp)
        if decoded is not None:
            model, kwargs = decoded
            kwargs = dict(kwargs, model=model, fs=fs)
            _call(calls, "set_compressor", (channel,), kwargs,
                  "%s %s compressor on=%s" %
                  (prefix, io24_presets.compressor_model(comp).title(),
                   kwargs["on"]))

    limiter = _object(component.get("limit"), "%s.limit" % prefix)
    if limiter:
        on = _toggle(limiter.get("limiteron", 0),
                     "%s.limit.limiteron" % prefix)
        threshold = _number(limiter.get("threshold", -28.0),
                            "%s.limit.threshold" % prefix, -56.0, 0.0)
        _call(calls, "set_limiter", (channel, on, threshold), {"fs": fs},
              "%s limiter on=%s threshold=%.1f dBFS" %
              (prefix, on, threshold))
    _unknown_fields(limiter, {"limiteron", "threshold"},
                    "%s.limit" % prefix, skips)

    eq = _object(component.get("eq"), "%s.eq" % prefix)
    if eq:
        model = io24_presets.eq_model(eq)
        if model == "standard":
            _plan_standard_eq(channel, eq, prefix, fs, calls)
        elif model in ("passive", "vintage"):
            normalized = io24_alt_eq.validate_eq(eq)
            # Missing retained source material or invalid coefficients fail
            # here, before the first device call can be made.
            io24_alt_eq.design_live_sections(normalized, rate_hz=fs)
            _call(calls, "set_alternate_eq", (channel, normalized), {"fs": fs},
                  "%s exact %s EQ on=%s" %
                  (prefix, model.title(), bool(normalized["eqallon"])))
        else:
            raise ValueError("%s EQ has no selected model" % prefix)

    fx = _object(component.get("voicefx"), "%s.voicefx" % prefix)
    if fx:
        model, kwargs = io24_fx.voicefx_preset_call(fx)
        host_only = (allow_host_delay and model == "delay" and
                     io24_fx.delay_needs_host_fallback(fs))
        if host_only:
            # Validate the exact saved controls, but deliberately do not build
            # a device model-5 transaction. The GTK Host will adopt this
            # semantic record into its PipeWire Delay after the device calls
            # in the rest of the scene have succeeded.
            io24_fx.fx_delay(**kwargs)
        else:
            kwargs = io24_fx.voicefx_runtime_kwargs(model, kwargs, fs)
            # Materialize the exact device transaction offline, including On.
            getattr(io24_fx, "set_fx_" + model)(**kwargs)
        voicefx.append((prefix, model, kwargs, host_only))


def plan(scene, vintage=False, sample_rate_hz=48000.0,
         allow_host_delay=False):
    """Return ``(calls, skips)`` without touching USB or the device.

    ``vintage`` is accepted for source compatibility. Alternate EQ no longer
    needs an opt-in: exact Passive and Vintage replay is the normal path.
    """
    _ = vintage
    scene = _object(scene, "scene")
    fs = _number(sample_rate_hz, "sample rate", 8000.0, 192000.0)
    calls, skips, mirrors, solos, mutes, voicefx = [], [], [], [], [], []

    global_state = _object(scene.get("global"), "global")
    if "outputDelayBus" in global_state:
        bus = _integer(global_state["outputDelayBus"],
                       "global.outputDelayBus", -1, 4)
        _call(calls, "set_output_delay_bus", (bus,), {},
              "output delay bus = %d" % bus)
    if "outputDelay" in global_state:
        seconds = _number(global_state["outputDelay"],
                          "global.outputDelay", 0.0, 0.5)
        _call(calls, "set_output_delay", (seconds,), {},
              "output delay = %.0f ms" % (seconds * 1000.0))
    if "phonesSrc" in global_state:
        source = _integer(global_state["phonesSrc"], "global.phonesSrc", 0, 2)
        _call(calls, "set_phones_source", (source,), {},
              "headphones source = %d" % source)
    if "auxMuteMode" in global_state:
        mode = _toggle(global_state["auxMuteMode"], "global.auxMuteMode")
        _call(calls, "set_mute_mode", (mode,), {},
              "Channel Mute Sync = %s (UC value mapping)" % mode)
    if "presetButtonMode" in global_state:
        mode = _integer(global_state["presetButtonMode"],
                        "global.presetButtonMode", 0, 2)
        _call(calls, "set_preset_mode", (mode,), {},
              "preset button mode = %d" % mode)
    for field, bus in (("aux1_mirror_main", "mixa"),
                       ("aux2_mirror_main", "mixb")):
        if field in global_state:
            enabled = _toggle(global_state[field], "global.%s" % field)
            mirrors.append(("set_mirror_main", (bus, enabled), {},
                            "%s follows Main = %s" % (bus, enabled)))
    _unknown_fields(global_state, {
        "phonesSrc", "aux1_mirror_main", "aux2_mirror_main",
        "outputDelay", "outputDelayBus", "presetButtonMode", "auxMuteMode",
    }, "global", skips)

    line = _object(scene.get("line"), "line")
    ch1 = _object(line.get("ch1"), "line.ch1")
    if "link" in ch1:
        linked = _toggle(ch1["link"], "line.ch1.link")
        _call(calls, "set_channel_link", (linked,), {},
              "input stereo link = %s" % linked)
    for channel in (1, 2):
        key = "ch%d" % channel
        component = _object(line.get(key), "line.%s" % key)
        if not component:
            continue
        prefix, source = "line.%s" % key, SOURCE[channel]
        if "username" in component:
            name = _text(component["username"], "%s.username" % prefix)
            _call(calls, "set_component_name", (source, name), {},
                  "%s name = %r" % (prefix, name))
        if "preampgain" in component:
            gain = _number(component["preampgain"],
                           "%s.preampgain" % prefix, 0.0, 60.0)
            _call(calls, "set_gain", (channel, gain), {},
                  "%s preamp gain = %.1f dB" % (prefix, gain))
        if "48v" in component:
            phantom = _toggle(component["48v"], "%s.48v" % prefix)
            _call(calls, "set_phantom", (channel, phantom), {},
                  "%s phantom = %s" % (prefix, phantom))
        if "mute" in component:
            muted = _toggle(component["mute"], "%s.mute" % prefix)
            _call(calls, "set_mute", (channel, muted), {},
                  "%s input mute = %s" % (prefix, muted))
        if "processingChannel" in component:
            processing = _integer(component["processingChannel"],
                                  "%s.processingChannel" % prefix, 0, 1) + 1
            _call(calls, "set_processing_channel", (channel, processing), {},
                  "%s DSP source = input %d" % (prefix, processing))
        _routing_calls(component, source, prefix, calls)
        if "dspAmount" in component:
            amount = _number(component["dspAmount"],
                             "%s.dspAmount" % prefix, 0.0, 1.0)
            _call(calls, "set_fx_mix", (channel, amount), {},
                  "%s DSP amount = %.3f" % (prefix, amount))
        if "solo" in component:
            on = _toggle(component["solo"], "%s.solo" % prefix)
            for bus in ("main", "mixa", "mixb"):
                solos.append(("set_solo", (source, bus, on), {},
                              "%s solo in %s = %s" % (prefix, bus, on)))
        _plan_processing(channel, component, prefix, fs, calls, voicefx, skips,
                         allow_host_delay=allow_host_delay)
        # These four UC fields exist in its generic component/scene schema but
        # the io24 fixes their ordinary state in hardware: mono inputs are
        # centred, an absent independent reverb send is off, and capture is
        # post-DSP.  A scene already asking for that state is satisfied, not an
        # omission.  Only a request that differs remains explicit.
        fixed_fields = {
            "pan": (
                _number(component["pan"], "%s.pan" % prefix, 0.0, 1.0)
                if "pan" in component else None,
                0.5, "mono-source pan is fixed at centre by block 100"),
            "FXA": (
                _number(component["FXA"], "%s.FXA" % prefix, -96.0, 10.0)
                if "FXA" in component else None,
                -96.0, "independent FXA send is fixed off"),
            "dawpostdsp": (
                int(_toggle(component["dawpostdsp"],
                            "%s.dawpostdsp" % prefix))
                if "dawpostdsp" in component else None,
                1, "DAW capture tap is fixed post-DSP"),
        }
        for field, (value, fixed, reason) in fixed_fields.items():
            if field in component and not math.isclose(
                    float(value), float(fixed), abs_tol=1e-7):
                skips.append("%s.%s = %r (%s)" %
                             (prefix, field, component[field], reason))
        for field, reason in (
                ("stereopan", "stereo width/mono collapse is not representable"),
                ("preset_name", "display metadata is not a live parameter"),
                ("linkmaster", "UC component-tree metadata")):
            if field in component:
                if field == "stereopan":
                    _number(component[field], "%s.%s" % (prefix, field),
                            0.0, 1.0)
                skips.append("%s.%s = %r (%s)" %
                             (prefix, field, component[field], reason))
        known = {
            "username", "mute", "volume", "link", "linkmaster",
            "preset_name", "solo", "lr", "assign_aux1", "assign_aux2",
            "aux1", "aux2", "preampgain", "pan", "stereopan", "FXA",
            "dawpostdsp", "dspAmount", "processingChannel", "48v", "opt",
            "filter", "gate", "comp", "eq", "limit", "voicefx",
        }
        _unknown_fields(component, known, prefix, skips)

    if len(voicefx) > 1:
        first = voicefx[0][1:]
        if any(item[1:] != first for item in voicefx[1:]):
            raise ValueError(
                "scene contains conflicting per-channel VoiceFX records, but "
                "the io24 has one shared VoiceFX processor")
    if voicefx:
        prefix, model, kwargs, host_only = voicefx[0]
        if not host_only:
            _call(calls, "set_fx", (model,), kwargs,
                  "shared VoiceFX from %s = %s, on=%s" %
                  (prefix, model, kwargs["on"]))

    for section in ("return", "fxreturn"):
        group = _object(scene.get(section), section)
        for key, component in group.items():
            component = _object(component, "%s.%s" % (section, key))
            source = "%s/%s" % (section, key)
            prefix = "%s.%s" % (section, key)
            if source not in ("return/ch1", "return/ch2", "return/ch3",
                              "fxreturn/ch1"):
                skips.append("%s has no io24 mixer source" % prefix)
                continue
            if "username" in component:
                name = _text(component["username"], "%s.username" % prefix)
                _call(calls, "set_component_name", (source, name), {},
                      "%s name = %r" % (prefix, name))
            _routing_calls(component, source, prefix, calls)
            if "mute" in component:
                on = _toggle(component["mute"], "%s.mute" % prefix)
                mutes.append(("set_source_mute", (source, on), {},
                              "%s source mute = %s" % (prefix, on)))
            if "solo" in component:
                on = _toggle(component["solo"], "%s.solo" % prefix)
                for bus in ("main", "mixa", "mixb"):
                    solos.append(("set_solo", (source, bus, on), {},
                                  "%s solo in %s = %s" %
                                  (prefix, bus, on)))
            for field, reason in (
                    ("preset_name", "display metadata is not live state"),
                    ("link", "USB-return stereo linking has no exact wire"),
                    ("linkmaster", "UC component-tree metadata")):
                if field in component:
                    skips.append("%s.%s = %r (%s)" %
                                 (prefix, field, component[field], reason))
            _unknown_fields(component, {
                "username", "mute", "volume", "link", "linkmaster",
                "preset_name", "solo", "lr", "assign_aux1", "assign_aux2",
                "aux1", "aux2",
            }, prefix, skips)

    output_map = {("aux", "ch1"): "mixa", ("aux", "ch2"): "mixb",
                  ("main", "ch1"): "main"}
    for section in ("aux", "main"):
        group = _object(scene.get(section), section)
        for key, component in group.items():
            prefix = "%s.%s" % (section, key)
            component = _object(component, prefix)
            bus = output_map.get((section, key))
            if bus is None:
                skips.append("%s has no io24 output bus" % prefix)
                continue
            component_path = "%s/%s" % (section, key)
            if "username" in component:
                name = _text(component["username"], "%s.username" % prefix)
                _call(calls, "set_component_name", (component_path, name), {},
                      "%s name = %r" % (prefix, name))
            if "volume" in component:
                gain = _number(component["volume"], "%s.volume" % prefix,
                               -96.0, 10.0)
                _call(calls, "set_bus_master", (bus, gain), {},
                      "%s master = %.1f dB" % (prefix, gain))
            if "mute" in component:
                on = _toggle(component["mute"], "%s.mute" % prefix)
                mutes.append(("set_bus_mute", (bus, on), {},
                              "%s output mute = %s" % (prefix, on)))
            if "mono" in component:
                mono = _toggle(component["mono"], "%s.mono" % prefix)
                if mono:
                    skips.append(
                        "%s.mono = %r (output mono fold-down is fixed off)" %
                        (prefix, component["mono"]))
            for field, reason in (
                    ("preset_name", "display metadata is not live state"),
                    ("link", "output link is component-tree metadata"),
                    ("linkmaster", "UC component-tree metadata")):
                if field in component:
                    skips.append("%s.%s = %r (%s)" %
                                 (prefix, field, component[field], reason))
            _unknown_fields(component, {
                "username", "mute", "volume", "link", "linkmaster",
                "preset_name", "mono",
            }, prefix, skips)

    fx_group = _object(scene.get("fx"), "fx")
    fx_ch1 = _object(fx_group.get("ch1"), "fx.ch1")
    reverb = _object(fx_ch1.get("reverb"), "fx.ch1.reverb")
    if reverb:
        on = _toggle(reverb.get("on", 1), "fx.ch1.reverb.on")
        size = _number(reverb.get("size", 0.5),
                       "fx.ch1.reverb.size", 0.0, 1.0)
        mix = _number(reverb.get("mix", 0.3),
                      "fx.ch1.reverb.mix", 0.0, 1.0)
        hp = _number(reverb.get("hp_freq", 200.0),
                     "fx.ch1.reverb.hp_freq", 0.0, 500.0)
        predelay = _number(reverb.get("predelay", 0.02),
                           "fx.ch1.reverb.predelay", 0.0001, 0.25)
        _call(calls, "set_reverb", (), {
            "on": on, "size": size, "mix": mix, "hp_freq": hp,
            "predelay": predelay, "fs": fs,
        }, "shared reverb on=%s size=%.3f mix=%.3f" % (on, size, mix))
    _unknown_fields(reverb, {"on", "size", "mix", "hp_freq", "predelay"},
                    "fx.ch1.reverb", skips)
    _unknown_fields(fx_ch1, {"reverb"}, "fx.ch1", skips)
    _unknown_fields(fx_group, {"ch1"}, "fx", skips)

    if "presets" in scene:
        slots, users = describe_presets(scene)
        skips.append(
            "presets library retained and reported (%d device block%s, %d user "
            "preset%s); scene load never overwrites device-resident blocks" %
            (len(slots), "" if len(slots) == 1 else "s", len(users),
             "" if len(users) == 1 else "s"))

    _unknown_fields(scene, {
        "global", "line", "return", "fxreturn", "aux", "main", "fx",
        "presets",
    }, "scene", skips)

    # Routing first, then the persistent Mirror Main latch, then solo/mute gates.
    # This matches UC's resulting state and ensures the final gate always wins.
    calls.extend(mirrors)
    calls.extend(solos)
    calls.extend(mutes)
    return calls, skips


def describe_presets(scene):
    presets = _object(scene.get("presets"), "presets")
    slots = _object(presets.get("slots"), "presets.slots")
    output = []
    for key in sorted(slots, key=lambda value: int(value)):
        record = _object(slots[key], "presets.slots.%s" % key)
        fx = _object(record.get("voicefx"),
                     "presets.slots.%s.voicefx" % key)
        output.append("  slot %s (%s): %-18r voicefx.on=%s" %
                      (key, "ch1" if int(key) < 2 else "ch2",
                       record.get("preset_name"), fx.get("on")))
    users = list(_object(presets.get("userpresets"),
                         "presets.userpresets"))
    return output, users


_BUS_TO_SCENE = {
    "main": ("volume", "lr"),
    "mixa": ("aux1", "assign_aux1"),
    "mixb": ("aux2", "assign_aux2"),
}
_BUS_ALIASES = {"aux1": "mixa", "aux2": "mixb",
                "mix1": "mixa", "mix2": "mixb"}
_OUTPUT_TO_SCENE = {
    "main": ("main", "ch1"),
    "mixa": ("aux", "ch1"),
    "mixb": ("aux", "ch2"),
}
_SOURCE_TO_SCENE = {
    "line/ch1": ("line", "ch1"),
    "line/ch2": ("line", "ch2"),
    "return/ch1": ("return", "ch1"),
    "return/ch2": ("return", "ch2"),
    "return/ch3": ("return", "ch3"),
    "fxreturn/ch1": ("fxreturn", "ch1"),
}
_COMPONENT_TO_SCENE = dict(_SOURCE_TO_SCENE, **{
    "aux/ch1": ("aux", "ch1"),
    "aux/ch2": ("aux", "ch2"),
    "main/ch1": ("main", "ch1"),
})


def _scene_bus(value):
    value = str(value).lower()
    return _BUS_ALIASES.get(value, value)


def _export_component(scene, source):
    section, key = _COMPONENT_TO_SCENE[source]
    return scene[section].setdefault(key, {})


def _export_compressor(kwargs):
    """Invert ``Io24.set_compressor`` into UC's selected model object."""
    try:
        model = int(kwargs["model"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("compressor shadow is missing model 0, 1, or 2")
    if model not in (0, 1, 2):
        raise ValueError("compressor shadow model must be 0, 1, or 2")
    if kwargs.get("instance") is not None:
        raise ValueError("one compressor sub-instance is not a UC scene model")
    record = {
        "__classid": next(key for key, value in COMP_MODELS.items()
                          if value == model),
        "on": int(bool(kwargs["on"])),
        "keyfilter": float(kwargs["keyfilter_hz"]),
        "keylisten": int(bool(kwargs["keylisten"])),
    }
    if model == 0:
        record.update({
            "threshold": float(kwargs["threshold_db"]),
            "ratio": float(kwargs["ratio"]),
            "attack": float(kwargs["attack_s"]),
            "release": float(kwargs["release_s"]),
            "gain": float(kwargs["gain_db"]),
            "softknee": int(bool(kwargs["softknee"])),
            "automode": int(bool(kwargs["automode"])),
        })
    elif model == 1:
        record.update({
            "peak": float(kwargs["peak"]),
            "gain": float(kwargs["gain"]),
            "mode": int(bool(kwargs["limit_mode"])),
        })
    else:
        record.update({
            "input": float(kwargs["input_db"]),
            "output": float(kwargs["output_db"]),
            "attack": float(kwargs["attack_s"]),
            "release": float(kwargs["release_s"]),
            "ratio": int(kwargs["ratio_index"]),
        })
    # Reuse the import validator so export cannot manufacture an invalid model.
    io24_presets._compressor_call(record)
    return record


def _export_standard_eq(calls, channel, omissions):
    bands = {}
    for call in calls:
        if call.get("fn") != "set_eq_band":
            continue
        kwargs = call.get("kwargs") or {}
        if kwargs.get("channel") != channel:
            continue
        try:
            band = int(kwargs["band"])
        except (KeyError, TypeError, ValueError):
            continue
        if band not in range(4):
            continue
        shape = str(kwargs.get("shape", "off")).lower()
        bands[band] = {
            "shape": shape,
            "mode": None if shape == "off" else shape,
            "on": shape != "off",
            "freq": kwargs.get("freq_hz", 1000.0),
            "gain": kwargs.get("gain_db", 0.0),
            "q": kwargs.get("q", 0.7),
        }
    if not bands:
        return None
    if set(bands) != set(range(4)):
        omissions.append(
            "line.ch%d.eq was omitted because only %d of 4 Standard bands "
            "are Host-known" % (channel, len(bands)))
        return None
    # An identity coefficient does not retain the hidden shelf/peak selector.
    # The vendor defaults are the only non-invented reconstruction available.
    defaults = io24_presets.default_standard_eq_bands()
    ordered = []
    for index in range(4):
        band = bands[index]
        if band["mode"] is None:
            band["mode"] = defaults[index]["mode"]
        ordered.append(band)
    return io24_presets._standard_slot_eq(
        ordered, eq_on=any(band["on"] for band in ordered))


def export_snapshot(snapshot, host_features=None, solo=None, presets=None):
    """Build a UC-vocabulary scene from exact Host-known state.

    This is deliberately not device readback. Readable input gain/phantom
    values come from ``snapshot.live``; DSP, mixer and global values come from
    the Host's durable write mirror. Anything not known exactly is omitted and
    returned in ``omissions`` rather than filled with a guessed UC default.
    Device-library bodies are included only when supplied from the Host's
    identity-scoped sent-record registries; nothing is inferred from a selected
    button slot or a USB reply.
    """
    snapshot = _object(snapshot, "snapshot")
    live = _object(snapshot.get("live"), "snapshot.live")
    call_map = _object(snapshot.get("calls"), "snapshot.calls")
    calls = []
    for key, raw in call_map.items():
        raw = _object(raw, "snapshot.calls.%s" % key)
        name = raw.get("fn") or str(key).split("#", 1)[0]
        kwargs = _object(raw.get("kwargs"),
                         "snapshot.calls.%s.kwargs" % key)
        calls.append({"fn": name, "kwargs": dict(kwargs)})

    scene = {
        "global": {},
        "line": {"ch1": {}, "ch2": {}},
        "return": {},
        "fxreturn": {},
        "aux": {"ch1": {}, "ch2": {}},
        "main": {"ch1": {}},
        "fx": {"ch1": {}},
    }
    omissions = []

    for channel in (1, 2):
        component = scene["line"]["ch%d" % channel]
        gain_key = "input%dGain" % channel
        phantom_key = "input%dPhantomPower" % channel
        if gain_key in live:
            component["preampgain"] = _number(
                live[gain_key], "snapshot.live.%s" % gain_key, 0.0, 60.0)
        if phantom_key in live:
            component["48v"] = int(_toggle(
                live[phantom_key], "snapshot.live.%s" % phantom_key))
    for key in sorted(set(live) - {
            "input1Gain", "input2Gain", "input1PhantomPower",
            "input2PhantomPower", "input1ProcessingChannel",
            "input2ProcessingChannel", "input1SlotIndex", "input2SlotIndex",
            "input1Level", "input2Level", "flags"}):
        omissions.append(
            "snapshot.live.%s has no supported UC scene export field" % key)

    alternate_from_shadow = {}
    standard_calls = []
    for call in calls:
        name, kwargs = call["fn"], call["kwargs"]
        channel = kwargs.get("channel")
        component = scene["line"].get("ch%d" % channel) \
            if channel in (1, 2) else None
        try:
            if name == "set_mute_mode":
                scene["global"]["auxMuteMode"] = int(bool(kwargs["mode"]))
            elif name == "set_phones_source":
                source = kwargs["source"]
                if isinstance(source, str):
                    aliases = {"main": 0, "mixa": 1, "aux1": 1,
                               "mix a": 1, "mixb": 2, "aux2": 2,
                               "mix b": 2}
                    source = aliases.get(source.strip().lower(), source)
                scene["global"]["phonesSrc"] = _integer(
                    source, "set_phones_source.source", 0, 2)
            elif name == "set_output_delay":
                scene["global"]["outputDelay"] = _number(
                    kwargs["seconds"], "set_output_delay.seconds", 0.0, 0.5)
            elif name == "set_output_delay_bus":
                bus = kwargs["bus"]
                if isinstance(bus, str):
                    bus = {"off": -1, "none": 0, "mixa": 1, "aux1": 1,
                           "mixb": 2, "aux2": 2}.get(
                               bus.strip().lower(), bus)
                scene["global"]["outputDelayBus"] = _integer(
                    bus, "set_output_delay_bus.bus", -1, 4)
            elif name == "output_delay_off":
                scene["global"]["outputDelay"] = 0.0
                scene["global"]["outputDelayBus"] = 0
            elif name == "set_preset_mode":
                scene["global"]["presetButtonMode"] = _integer(
                    kwargs["mode"], "set_preset_mode.mode", 0, 2)
            elif name == "set_mirror_main":
                bus = _scene_bus(kwargs["bus"])
                field = {"mixa": "aux1_mirror_main",
                         "mixb": "aux2_mirror_main"}.get(bus)
                if field is None:
                    raise ValueError("Mirror Main is only valid for aux buses")
                scene["global"][field] = int(bool(kwargs.get("on", True)))
            elif name == "set_component_name":
                path = kwargs["component"]
                if path not in _COMPONENT_TO_SCENE:
                    raise ValueError("unknown named scene component %r" % path)
                _export_component(scene, path)["username"] = _text(
                    kwargs["name"], "set_component_name.name")
            elif name == "set_channel_link":
                scene["line"]["ch1"]["link"] = int(bool(kwargs["on"]))
            elif name == "set_mute" and component is not None:
                component["mute"] = int(bool(kwargs["on"]))
            elif name == "set_processing_channel" and component is not None:
                component["processingChannel"] = _integer(
                    kwargs["source_input"], "processing source", 1, 2) - 1
            elif name == "set_fx_mix" and component is not None:
                component["dspAmount"] = _number(
                    kwargs["value"], "DSP amount", 0.0, 1.0)
            elif name == "set_comp_eq_order" and component is not None:
                component.setdefault("opt", {})["swapcompeq"] = int(
                    bool(kwargs["eq_first"]))
            elif name == "set_highpass_freq" and component is not None:
                component.setdefault("filter", {})["hpf"] = _number(
                    kwargs["freq_hz"], "high-pass frequency", 24.0, 1000.0)
            elif name == "set_gate" and component is not None:
                if kwargs.get("instance") is not None:
                    raise ValueError(
                        "one gate sub-instance is not a UC scene model")
                component["gate"] = {
                    "on": int(bool(kwargs["on"])),
                    "threshold": float(kwargs["threshold_db"]),
                    "range": float(kwargs["range_db"]),
                    "attack": float(kwargs["attack_s"]),
                    "release": float(kwargs["release_s"]),
                    "keyfilter": float(kwargs["keyfilter_hz"]),
                    "expander": int(bool(kwargs["expander"])),
                    "keylisten": int(bool(kwargs["keylisten"])),
                }
            elif name == "gate_off" and component is not None:
                component["gate"] = {
                    "on": 0, "threshold": -40.0, "range": -60.0,
                    "attack": 0.01, "release": 0.3, "keyfilter": 0.0,
                    "expander": 1, "keylisten": 0,
                }
            elif name == "set_compressor" and component is not None:
                component["comp"] = _export_compressor(kwargs)
            elif name == "compressor_off" and component is not None:
                omissions.append(
                    "line.ch%d.comp was omitted because compressor_off does "
                    "not retain a selected UC model" % channel)
            elif name == "set_limiter" and component is not None:
                if kwargs.get("instance") is not None:
                    raise ValueError(
                        "one limiter sub-instance is not a UC scene model")
                component["limit"] = {
                    "limiteron": int(bool(kwargs["on"])),
                    "threshold": float(kwargs["threshold_db"]),
                }
            elif name == "set_eq_band":
                standard_calls.append(call)
            elif name == "set_alternate_eq" and component is not None:
                alternate_from_shadow[channel] = \
                    io24_alt_eq.validate_eq(kwargs["eq"])
            elif name == "set_fx":
                model = kwargs.get("model")
                state_args = {key: value for key, value in kwargs.items()
                              if key not in ("model", "fs")}
                state = io24_fx.voicefx_preset_state(model, **state_args)
                # Block 201 is shared. UC repeats the same component under both
                # lines; one canonical copy is sufficient for this Host.
                scene["line"]["ch1"]["voicefx"] = state
            elif name == "set_reverb":
                scene["fx"]["ch1"]["reverb"] = {
                    key: kwargs[key]
                    for key in ("on", "size", "mix", "hp_freq", "predelay")
                }
            elif name in ("set_send_db", "set_send_assigned"):
                source = kwargs.get("source")
                bus = _scene_bus(kwargs.get("bus"))
                if source not in _SOURCE_TO_SCENE or bus not in _BUS_TO_SCENE:
                    raise ValueError("unknown mixer source or bus")
                field, assign = _BUS_TO_SCENE[bus]
                target = _export_component(scene, source)
                if name == "set_send_db":
                    value = kwargs.get("gain_db")
                    target[field] = -145.0 if value is None else float(value)
                else:
                    target[assign] = int(bool(kwargs.get("on", True)))
            elif name == "set_source_mute":
                source = kwargs.get("source")
                if source not in _SOURCE_TO_SCENE:
                    raise ValueError("unknown mixer source")
                _export_component(scene, source)["mute"] = int(
                    bool(kwargs.get("on", True)))
            elif name in ("set_bus_master", "set_bus_mute"):
                bus = _scene_bus(kwargs.get("bus"))
                if bus not in _OUTPUT_TO_SCENE:
                    raise ValueError("unknown output bus")
                section, key = _OUTPUT_TO_SCENE[bus]
                target = scene[section][key]
                if name == "set_bus_master":
                    target["volume"] = float(kwargs.get("gain_db", 0.0))
                else:
                    target["mute"] = int(bool(kwargs.get("on", True)))
            elif name == "set_pan":
                omissions.append(
                    "set_pan was not exported because Host stereo-pair balance "
                    "is not UC mono-source pan")
            elif name == "set_preset_slot":
                omissions.append(
                    "device preset selection was not exported")
            elif name in ("set_highpass", "set_hp_mute"):
                omissions.append(
                    "%s has no supported UC scene export field" % name)
            else:
                omissions.append(
                    "%s has no supported UC scene export mapping" % name)
        except (KeyError, TypeError, ValueError) as error:
            omissions.append("%s was not exported: %s" % (name, error))

    features = _object(host_features, "host_features")
    standard = features.get("standard_eq")
    alternate = features.get("alternate_eq")
    host_delay = features.get("voicefx_delay")
    if standard is not None:
        standard = io24_presets.validate_standard_eq_host_state(standard)
    if alternate is not None:
        alternate = io24_presets.validate_alternate_eq_host_state(alternate)
    if host_delay is not None:
        host_delay = io24_voicefx_delay.validate_host_feature(host_delay)
        # The device shadow deliberately retains the user's last native Delay
        # intent while hardware block 201 is quiesced. Above 48 kHz this Host
        # feature is newer and authoritative, including its input owner.
        for channel in (1, 2):
            scene["line"]["ch%d" % channel].pop("voicefx", None)
        scene["line"]["ch%d" % host_delay["target"]]["voicefx"] = \
            io24_fx.voicefx_preset_state("delay", **host_delay["state"])
    for name in sorted(set(features) - {
            "standard_eq", "alternate_eq", "voicefx_delay"}):
        omissions.append(
            "host_features.%s has no UC scene representation" % name)
    alternate_channels = (alternate or {}).get("channels", {})
    standard_channels = (standard or {}).get("channels", {})
    for channel in (1, 2):
        key = str(channel)
        if key in alternate_channels:
            scene["line"]["ch%d" % channel]["eq"] = \
                alternate_channels[key]
        elif channel in alternate_from_shadow:
            scene["line"]["ch%d" % channel]["eq"] = \
                alternate_from_shadow[channel]
        elif key in standard_channels:
            body = standard_channels[key]
            scene["line"]["ch%d" % channel]["eq"] = \
                io24_presets._standard_slot_eq(
                    body["bands"], eq_on=body["on"])
        else:
            eq = _export_standard_eq(standard_calls, channel, omissions)
            if eq is not None:
                scene["line"]["ch%d" % channel]["eq"] = eq

    # Readable state wins over the write mirror: physical mute/link changes can
    # happen while the Host is closed, and processingChannel is device state.
    if "flags" in live:
        flags = _integer(live["flags"], "snapshot.live.flags", 0)
        scene["line"].setdefault("ch1", {})["link"] = int(
            bool(flags >> 12 & 1))
        for channel, mute_bit, bypass_bit in ((1, 3, 5), (2, 4, 6)):
            component = scene["line"].setdefault("ch%d" % channel, {})
            component["mute"] = int(bool(flags >> mute_bit & 1))
            if flags >> bypass_bit & 1:
                component["dspAmount"] = 0.0
            elif "dspAmount" not in component:
                omissions.append(
                    "line.ch%d.dspAmount was omitted because the device only "
                    "reports nonzero, not the exact processing amount" %
                    channel)
    for channel in (1, 2):
        key = "input%dProcessingChannel" % channel
        if key in live:
            source = _integer(live[key], "snapshot.live.%s" % key, 1, 2)
            scene["line"].setdefault("ch%d" % channel, {})[
                "processingChannel"] = source - 1
    if "input1SlotIndex" in live or "input2SlotIndex" in live:
        omissions.append(
            "readable device preset selections were not exported")

    if solo is not None:
        solo = _object(solo, "solo")
        buses = ("main", "mixa", "mixb")
        members = {bus: set(solo.get(bus) or ()) for bus in buses}
        for source in _SOURCE_TO_SCENE:
            states = [source in members[bus] for bus in buses]
            if all(state == states[0] for state in states):
                _export_component(scene, source)["solo"] = int(states[0])
            else:
                omissions.append(
                    "%s solo differs by bus and cannot be one UC scene field" %
                    source)

    if presets is not None:
        presets = copy.deepcopy(_object(presets, "presets"))
        # Validate the library structure and each VoiceFX subobject before the
        # data becomes part of an export. Scene load remains report-only.
        from io24_preset_record import complete_device_slot_record
        for collection in ("slots", "userpresets"):
            records = _object(presets.get(collection),
                              "presets.%s" % collection)
            for key, record in records.items():
                records[key] = complete_device_slot_record(record)
        describe_presets({"presets": presets})
        scene["presets"] = presets

    # Keep recognized top-level sections but remove empty child objects. This
    # produces stable, readable vendor-vocabulary JSON without claiming state
    # that the Host never observed.
    for section in ("line", "return", "fxreturn", "aux", "main", "fx"):
        scene[section] = {key: value for key, value in scene[section].items()
                          if value}
    return scene, omissions


def capture(dev, host_features=None, presets=None):
    """Capture one exportable scene from readable state and the Host mirror."""
    live = dev.read_params() if callable(getattr(dev, "read_params", None)) \
        else {}
    calls = copy.deepcopy(getattr(dev, "_shadow", None) or {})
    # A scene is not a physical-button selection record.
    calls = {key: value for key, value in calls.items()
             if (value.get("fn") if isinstance(value, dict) else None) !=
             "set_preset_slot"}
    solo = copy.deepcopy(getattr(dev, "_solo", None) or {})
    return export_snapshot(
        {"version": 1, "live": live or {}, "calls": calls},
        host_features=host_features, solo=solo, presets=presets)


def save(path, scene, sample_rate_hz=48000.0):
    """Validate and atomically write a Host-known UC-vocabulary scene."""
    plan(scene, sample_rate_hz=sample_rate_hz, allow_host_delay=True)
    target = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(target)
    fd, temporary = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(target), suffix=".tmp",
        dir=directory, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(scene, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
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
    return scene


_POSITIONAL = {
    "set_gain": ("channel", "db"),
    "set_phantom": ("channel", "on"),
    "set_mute": ("channel", "on"),
    "set_channel_link": ("on",),
    "set_processing_channel": ("channel", "source_input"),
    "set_fx_mix": ("channel", "value"),
    "set_highpass_freq": ("channel", "freq_hz"),
    "set_comp_eq_order": ("channel", "eq_first"),
    "set_limiter": ("channel", "on", "threshold_db"),
    "set_compressor": ("channel",),
    "set_gate": ("channel",),
    "set_eq_band": ("channel", "band"),
    "set_alternate_eq": ("channel", "eq"),
    "set_send_db": ("source", "bus", "gain_db"),
    "set_send_assigned": ("source", "bus", "on"),
    "set_bus_master": ("bus", "gain_db"),
    "set_source_mute": ("source", "on"),
    "set_bus_mute": ("bus", "on"),
    "set_solo": ("source", "bus", "on"),
    "set_fx": ("model",),
    "set_preset_mode": ("mode",),
    "set_output_delay": ("seconds",),
    "set_output_delay_bus": ("bus",),
    "set_phones_source": ("source",),
    "set_mute_mode": ("mode",),
    "set_mirror_main": ("bus", "on"),
    "mirror_main": ("bus",),
    "set_component_name": ("component", "name"),
}


def _call_values(name, args=(), kwargs=None):
    values = dict(kwargs or {})
    for field, value in zip(_POSITIONAL.get(name, ()), args):
        values.setdefault(field, value)
    if "bus" in values:
        values["bus"] = _scene_bus(values["bus"])
    return values


def _control_target(name, args=(), kwargs=None):
    values = _call_values(name, args, kwargs)
    if name in ("gate_off",):
        name = "set_gate"
    elif name in ("compressor_off",):
        name = "set_compressor"
    if name in ("set_eq_band", "set_alternate_eq"):
        band = values.get("band")
        if isinstance(band, str):
            band = {"low": 0, "lowmid": 1, "himid": 2, "high": 3}.get(
                band.strip().lower(), band)
        return ("eq", values.get("channel"),
                band if name == "set_eq_band" else "all")
    identities = tuple((field, values.get(field))
                       for field in ("channel", "band", "source", "bus",
                                     "instance", "component")
                       if field in values)
    return (name,) + identities


def _checkpoint(dev):
    live = {}
    read = getattr(dev, "read_params", None)
    if callable(read):
        try:
            live = read() or {}
        except Exception as error:
            live = {"_read_error": "%s: %s" %
                    (type(error).__name__, error)}
    shadow = copy.deepcopy(getattr(dev, "_shadow", None) or {})
    # Scene apply never moves physical slots; rollback must not either.
    shadow = {key: value for key, value in shadow.items()
              if (value.get("fn") if isinstance(value, dict) else None) !=
              "set_preset_slot"}
    solo = copy.deepcopy(getattr(dev, "_solo", None) or {})
    return {"live": live, "shadow": shadow, "solo": solo}


def _cached_fx_mix(checkpoint, channel):
    for raw in checkpoint["shadow"].values():
        if not isinstance(raw, dict) or raw.get("fn") != "set_fx_mix":
            continue
        kwargs = raw.get("kwargs") or {}
        if kwargs.get("channel") == channel:
            return kwargs.get("value")
    return None


def _prior_targets(checkpoint):
    targets = set()
    eq_bands = {}
    for key, raw in checkpoint["shadow"].items():
        if not isinstance(raw, dict):
            continue
        name = raw.get("fn") or str(key).split("#", 1)[0]
        kwargs = raw.get("kwargs") or {}
        target = _control_target(name, kwargs=kwargs)
        targets.add(target)
        if target[:1] == ("eq",) and target[-1] != "all":
            eq_bands.setdefault(target[1], set()).add(target[-1])
    for channel, bands in eq_bands.items():
        if bands == set(range(4)):
            targets.add(("eq", channel, "all"))
    live = checkpoint["live"]
    if "flags" in live:
        flags = int(live["flags"])
        targets.add(("set_channel_link",))
        targets.update(("set_mute", ("channel", channel))
                       for channel in (1, 2))
        for channel, bit in ((1, 5), (2, 6)):
            target = ("set_fx_mix", ("channel", channel))
            if flags >> bit & 1:  # exact bypass is known; nonzero mix is not
                targets.add(target)
                continue
            # An enabled device contradicts a cached zero. The readable bit
            # proves only "some positive scalar", so stale zero is not exact
            # rollback state and must not make recovery look complete.
            cached = _cached_fx_mix(checkpoint, channel)
            try:
                exact_positive = float(cached) > 0.0
            except (TypeError, ValueError):
                exact_positive = False
            if not exact_positive:
                targets.discard(target)
    for channel in (1, 2):
        if "input%dGain" % channel in live:
            targets.add(("set_gain", ("channel", channel)))
        if "input%dPhantomPower" % channel in live:
            targets.add(("set_phantom", ("channel", channel)))
        if "input%dProcessingChannel" % channel in live:
            targets.add(("set_processing_channel", ("channel", channel)))
    return targets


def _rollback_unresolved(checkpoint, attempted):
    prior = _prior_targets(checkpoint)
    unresolved = []
    for name, args, kwargs, description in attempted:
        if name == "set_solo":
            values = _call_values(name, args, kwargs)
            bus = values.get("bus")
            cells = {
                ("set_send_db", ("source", source), ("bus", bus))
                for source in _SOURCE_TO_SCENE
            }
            if not cells.issubset(prior):
                unresolved.append(description)
            continue
        target = _control_target(name, args, kwargs)
        if target[:1] == ("eq",):
            full = ("eq", target[1], "all")
            if target not in prior and full not in prior:
                unresolved.append(description)
        elif target not in prior:
            unresolved.append(description)
    return list(dict.fromkeys(unresolved))


def _restore_checkpoint(dev, checkpoint, attempted):
    errors = []
    restored = 0
    if hasattr(dev, "_shadow"):
        # Rebuild the mirror only from successful restore calls. Preloading the
        # old dictionary would leave a false "restored" claim behind whenever
        # one compensating transport write itself failed.
        dev._shadow = {}
    if hasattr(dev, "_send_state"):
        dev._send_state = None
    if hasattr(dev, "_solo"):
        dev._solo = {}

    live = checkpoint["live"]
    bypassed_channels = set()
    enabled_without_exact_mix = set()
    attempted_targets = {
        _control_target(name, args, kwargs)
        for name, args, kwargs, _description in attempted
    }

    def restore(name, *args, **kwargs):
        nonlocal restored
        try:
            getattr(dev, name)(*args, **kwargs)
            restored += 1
        except Exception as error:
            errors.append({"control": name,
                           "error": "%s: %s" %
                                    (type(error).__name__, error)})

    for channel in (1, 2):
        key = "input%dGain" % channel
        if key in live and \
                ("set_gain", ("channel", channel)) in attempted_targets:
            restore("set_gain", channel, live[key])
        key = "input%dPhantomPower" % channel
        if key in live and \
                ("set_phantom", ("channel", channel)) in attempted_targets:
            restore("set_phantom", channel, bool(live[key]))
    if "flags" in live:
        flags = int(live["flags"])
        for channel, bit in ((1, 3), (2, 4)):
            if ("set_mute", ("channel", channel)) in attempted_targets:
                restore("set_mute", channel, bool(flags >> bit & 1))
        if ("set_channel_link",) in attempted_targets:
            restore("set_channel_link", bool(flags >> 12 & 1))
        for channel, bit in ((1, 5), (2, 6)):
            if flags >> bit & 1 and \
                    ("set_fx_mix", ("channel", channel)) in attempted_targets:
                bypassed_channels.add(channel)
            elif not (flags >> bit & 1):
                cached = _cached_fx_mix(checkpoint, channel)
                try:
                    exact_positive = float(cached) > 0.0
                except (TypeError, ValueError):
                    exact_positive = False
                if not exact_positive:
                    enabled_without_exact_mix.add(channel)
    for channel in (1, 2):
        key = "input%dProcessingChannel" % channel
        if key in live and \
                ("set_processing_channel", ("channel", channel)) in \
                attempted_targets:
            restore("set_processing_channel", channel, int(live[key]))

    phases = {
        "set_preset_mode": 0,
        "set_processing_channel": 10,
        "set_channel_link": 10,
        "set_send_db": 30,
        "set_bus_master": 40,
        "set_pan": 50,
        "set_send_assigned": 60,
        "set_mirror_main": 65,
        "set_source_mute": 70,
        "set_bus_mute": 70,
    }
    skip_live = {"set_gain", "set_phantom", "set_mute",
                 "set_channel_link", "set_processing_channel"}
    entries = []
    for position, (key, raw) in enumerate(checkpoint["shadow"].items()):
        if not isinstance(raw, dict):
            continue
        name = raw.get("fn") or str(key).split("#", 1)[0]
        if name == "set_preset_slot" or name in skip_live:
            continue
        if name == "set_fx_mix" and \
                (raw.get("kwargs") or {}).get("channel") in \
                (bypassed_channels | enabled_without_exact_mix):
            continue
        entries.append((phases.get(name, 20), position, name,
                        raw.get("kwargs") or {}))
    for _phase, _position, name, kwargs in sorted(entries):
        restore(name, **kwargs)

    # A readable bypass is exact zero and overrides a stale positive Host
    # scalar. Apply it after shadow replay so rollback finishes at device truth.
    for channel in sorted(bypassed_channels):
        restore("set_fx_mix", channel, 0.0)

    for bus, members in checkpoint["solo"].items():
        for source in members:
            restore("set_solo", source, bus, True)

    if hasattr(dev, "_shadow_dirty"):
        dev._shadow_dirty = True
    flush = getattr(dev, "_flush_shadow", None)
    if callable(flush):
        try:
            flush(force=True)
        except Exception as error:
            errors.append({"control": "shadow persistence",
                           "error": "%s: %s" %
                                    (type(error).__name__, error)})
    return restored, errors


def _check_setters(dev, calls):
    missing = sorted({name for name, _args, _kwargs, _desc in calls
                      if not callable(getattr(dev, name, None))})
    if missing:
        raise AttributeError("device backend is missing setter%s: %s" %
                             ("" if len(missing) == 1 else "s",
                              ", ".join(missing)))


def apply_transactional(dev, calls):
    """Apply a plan and compensate from exact prestate after a failure.

    The io24 protocol has no device-side transaction. This therefore restores
    every readable or Host-known prior value and reports any attempted control
    whose old value was unknowable. It never moves a physical preset slot.
    """
    _check_setters(dev, calls)
    checkpoint = _checkpoint(dev)
    done = 0
    for position, (name, args, kwargs, description) in enumerate(calls):
        try:
            getattr(dev, name)(*args, **kwargs)
            done += 1
        except Exception as error:
            attempted = calls[:position + 1]
            unresolved = _rollback_unresolved(checkpoint, attempted)
            restored, rollback_errors = _restore_checkpoint(
                dev, checkpoint, attempted)
            return {
                "applied": done,
                "failed": 1,
                "failed_setting": description,
                "error": "%s: %s" % (type(error).__name__, error),
                "rollback": {
                    "attempted": True,
                    "restored": restored,
                    "errors": rollback_errors,
                    "unresolved": unresolved,
                    "complete": not rollback_errors and not unresolved,
                },
            }
    return {
        "applied": done,
        "failed": 0,
        "failed_setting": None,
        "error": None,
        "rollback": {
            "attempted": False,
            "restored": 0,
            "errors": [],
            "unresolved": [],
            "complete": True,
        },
    }


def apply(dev, calls):
    """Apply a validated plan, stopping on the first transport/runtime error."""
    _check_setters(dev, calls)
    done = 0
    for name, args, kwargs, description in calls:
        try:
            getattr(dev, name)(*args, **kwargs)
            done += 1
        except Exception as error:
            print("    ! %s: %s" % (description, error))
            return done, 1
    return done, 0


def _parser():
    parser = argparse.ArgumentParser(
        description="Save or apply a Universal Control `.scene` with the io24")
    parser.add_argument("scene", nargs="?", help="path to the .scene JSON file")
    parser.add_argument("--export", metavar="PATH",
                        help="save readable and Host-known state as a .scene")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="validate and print the complete plan without USB")
    parser.add_argument(
        "--sample-rate", type=float,
        help="current device rate; required when applying a scene")
    parser.add_argument("--vintage-eq", action="store_true",
                        help=argparse.SUPPRESS)
    return parser


def _cli_sample_rate(requested, live_apply=False):
    """Never let a live scene assume 48 kHz behind the user's back."""
    if requested is not None:
        return _number(requested, "sample rate", 8000.0, 192000.0)
    if live_apply:
        raise SystemExit(
            "--sample-rate is required when applying a scene; use the "
            "current io24 rate (for Delay, choose 48000 rather than 96000)")
    return 48000.0


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.export and args.scene:
        raise SystemExit("choose either a scene to load or --export, not both")
    if args.export and args.dry_run:
        raise SystemExit("--dry-run applies to loading; --export writes PATH")
    if args.export:
        from io24 import Io24
        device = Io24()
        try:
            scene, omissions = capture(device)
            rate = _cli_sample_rate(args.sample_rate)
            save(args.export, scene, sample_rate_hz=rate)
            calls, skips = plan(scene, sample_rate_hz=rate)
            print("saved %s (%d Host-known settings)" %
                  (os.path.basename(args.export), len(calls)))
            for omitted in omissions + skips:
                print("    - %s" % omitted)
            return 0
        finally:
            device.close()
    if not args.scene:
        _parser().print_help()
        return 0
    if not os.path.exists(args.scene):
        raise SystemExit("no such scene file: %s" % args.scene)
    scene = load(args.scene)
    rate = _cli_sample_rate(
        args.sample_rate, live_apply=not args.dry_run)
    calls, skips = plan(scene, vintage=args.vintage_eq,
                        sample_rate_hz=rate)

    print("scene: %s" % os.path.basename(args.scene))
    slots, users = describe_presets(scene)
    if slots:
        print("\ndevice preset blocks recorded in this scene:")
        for description in slots:
            print(description)
    if users:
        print("user presets in the library: %s" % ", ".join(users))
    print("\n%d setting(s) to apply:" % len(calls))
    for _name, _args, _kwargs, description in calls:
        print("    %s" % description)
    if skips:
        print("\n%d not applied:" % len(skips))
        for skipped in skips:
            print("    - %s" % skipped)
    if args.dry_run:
        print("\n(dry run — nothing sent)")
        return 0

    from io24 import Io24
    device = Io24()
    try:
        print("\napplying...")
        report = apply_transactional(device, calls)
        print("applied %d, failed %d" %
              (report["applied"], report["failed"]))
        if report["failed"]:
            rollback = report["rollback"]
            print("failure: %s (%s)" %
                  (report["failed_setting"], report["error"]))
            if rollback["complete"]:
                print("rollback: complete (%d setting(s) restored)" %
                      rollback["restored"])
            else:
                print("rollback: partial (%d unknown prior value(s), %d "
                      "restore error(s))" %
                      (len(rollback["unresolved"]),
                       len(rollback["errors"])))
        return 1 if report["failed"] else 0
    finally:
        device.close()


if __name__ == "__main__":
    raise SystemExit(main())
