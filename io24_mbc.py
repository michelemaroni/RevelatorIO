"""Host-side multiband compressor for the io24.

The device cannot do this: the firmware capability sweep in PROTOCOL.md is a
clean, measured negative — no `multiband`, `crossover` or `fft` vocabulary
exists anywhere in it. So this is not a device feature and never pretends to
be one. It is a PipeWire filter-chain running on the computer. Since
2026-09-11 it is the Fat Channel's fourth compressor type, Multiband: the
input insert below takes each input from USB capture, right after the unit's
Fat Channel, and plays it back into the unit in place of the input's own feed.
The older sink form (`Chain`, "io24 Multiband" for application playback) is
kept for the command line; the GTK Host no longer offers it. This module also
owns two distinct passive capture-bus sources; those paths never route
themselves to playback or pretend to be device DSP.

Topology of one channel's graph, all standard pieces:

    in -> [LR4 low     ] -> UC comp -> \\
       -> [LR4 low-mid ] -> UC comp ->  mixer -> out
       -> [LR4 high-mid] -> UC comp ->
       -> [LR4 high    ] -> UC comp -> /

* The crossover is Linkwitz-Riley 4th order, built from PipeWire's builtin
  biquads (two cascaded Butterworth 2nd-order sections per leg — that IS an
  LR4), and each band also passes the all-pass of the splits it does not
  use, so the four bands sum flat (CROSSOVER_LEGS).
* Each band uses the project's small LADSPA processor. Its controls are the
  common `cpxt` tuple produced by the exact UC-derived Standard, Tube and FET
  builders, including the model's side-chain biquad. PipeWire 1.6's
  filter-chain hosts LADSPA and builtins only (no LV2), so the Host builds this
  self-contained C processor into its user cache without sudo.
* The chain runs in PipeWire's own process graph (C, realtime scheduled), not
  in this Python process — enabling it costs the app nothing.

Runtime model: the chain is hosted by a child `pipewire -c <ourconf>` process,
which is the documented way to run an on-demand filter-chain; killing the
child removes the sink cleanly. Parameters are changed live with
`pw-cli set-param <node> Props`.
"""

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time

import io24_uc_comp
import io24_voicefx_delay

SINK_NAME = "io24-multiband"
SINK_DESC = "io24 Multiband"
# Which of the io24's six capture channels each bus is, 1-based. A PipeWire
# node's index i is always device capture channel i+1; what changes with the
# card profile is how many of them the node exposes. `analog-surround-21`
# publishes three (FL, FR, LFE), so Mix A and Mix B are simply not there;
# `pro-audio` publishes all six. Reading the node is the only way to know.
BUS_DEVICE_CHANNELS = {"mixa": (3, 4), "mixb": (5, 6)}
BUS_CAPTURE_PAIRS = {bus: (left - 1, right - 1)
                     for bus, (left, right) in BUS_DEVICE_CHANNELS.items()}
_POSITION_CACHE = {}
BUS_SOURCE_NAMES = {
    "mixa": "io24-host-mixa",
    "mixb": "io24-host-mixb",
}
BUS_SOURCE_DESCRIPTIONS = {
    "mixa": "io24 Host Mix A",
    "mixb": "io24 Host Mix B",
}
CAPTURE_POSITIONS = ("FL", "FR", "FC", "LFE", "RL", "RR")

BANDS = ("low", "lowmid", "highmid", "high")
MODEL_NAMES = ("standard", "tube", "fet")

DEFAULTS = {
    "xovers": [150.0, 800.0, 4500.0],
    "low":     {"threshold": -18.0, "ratio": 3.0, "attack": 20.0,
                "release": 200.0, "knee": 6.0, "makeup": 0.0, "key": 100.0},
    "lowmid":  {"threshold": -20.0, "ratio": 2.5, "attack": 12.0,
                "release": 150.0, "knee": 6.0, "makeup": 0.0, "key": 400.0},
    "highmid": {"threshold": -20.0, "ratio": 2.5, "attack": 6.0,
                "release": 100.0, "knee": 6.0, "makeup": 0.0, "key": 2000.0},
    "high":    {"threshold": -22.0, "ratio": 3.0, "attack": 3.0,
                "release": 80.0, "knee": 6.0, "makeup": 0.0, "key": 8000.0},
}

SNAPSHOT_VERSION = 2
BUS_SOURCE_KEYS = ("mixa", "mixb")
_MODEL_FIELDS = {
    "standard": frozenset({
        "threshold_db", "ratio", "attack_s", "release_s", "gain_db",
        "softknee", "automode",
    }),
    "tube": frozenset({"peak", "gain", "limit_mode"}),
    "fet": frozenset({
        "input_db", "output_db", "attack_s", "release_s", "ratio_index",
    }),
}
_MODEL_RANGES = {
    "standard": {
        "threshold_db": (-56.0, 0.0), "ratio": (1.0, 20.0),
        "attack_s": (0.0002, 0.15), "release_s": (0.0025, 0.9),
        "gain_db": (0.0, 28.0),
    },
    "tube": {"peak": (0.0, 100.0), "gain": (0.0, 100.0)},
    "fet": {
        "input_db": (-56.0, 0.0), "output_db": (-56.0, 0.0),
        "attack_s": (0.000021, 0.0008), "release_s": (0.05, 1.1),
    },
}
_MODEL_BOOLEANS = {
    "standard": ("softknee", "automode"),
    "tube": ("limit_mode",),
    "fet": (),
}
_BAND_FIELDS = frozenset({
    "type", "standard", "tube", "fet", "key_filter", "key", "listen",
})

_LAST_PLUGIN_ERROR = None
_LAST_DELAY_PLUGIN_ERROR = None


def _finite_number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError("multiband %s must be numeric" % name)
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("multiband %s must be numeric" % name) from error
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(
            "multiband %s must be in [%s, %s]" % (name, low, high))
    return number


def default_band(band):
    """All three exact UC model vocabularies retained for one band."""
    values = DEFAULTS[band]
    return {
        "type": "standard",
        "standard": {
            "threshold_db": float(values["threshold"]),
            "ratio": float(values["ratio"]),
            "attack_s": float(values["attack"]) / 1000.0,
            "release_s": float(values["release"]) / 1000.0,
            "gain_db": float(values["makeup"]),
            "softknee": True,
            "automode": False,
        },
        "tube": {"peak": 0.0, "gain": 40.0, "limit_mode": False},
        "fet": {
            "input_db": -43.0,
            "output_db": 0.0,
            "attack_s": 0.0001,
            "release_s": 0.25,
            "ratio_index": 0,
        },
        "key_filter": False,
        "key": float(values["key"]),
        "listen": False,
    }


def default_snapshot(enabled=False):
    """Return the complete computer-side state shown by the GTK controls."""
    return {
        "version": SNAPSHOT_VERSION,
        "enabled": bool(enabled),
        "xovers": list(DEFAULTS["xovers"]),
        "bands": {band: default_band(band) for band in BANDS},
    }


def _legacy_fet_ratio_index(ratio):
    ratio = float(ratio)
    if ratio >= 19.5:
        return 4
    if ratio < 8.0:
        return 0
    if ratio < 12.0:
        return 1
    if ratio < 18.0:
        return 2
    return 3


def _migrate_legacy_snapshot(state):
    """Version 1 used generic sc3 controls; preserve its audible intent."""
    legacy_fields = {
        "type", "threshold", "ratio", "attack", "release", "knee",
        "makeup", "auto", "limit", "key_filter", "key", "listen",
    }
    raw_bands = state.get("bands")
    if not isinstance(raw_bands, dict) or set(raw_bands) != set(BANDS):
        raise ValueError("multiband bands must contain low/lowmid/highmid/high")
    migrated = {
        "version": SNAPSHOT_VERSION,
        "enabled": state.get("enabled"),
        "xovers": state.get("xovers"),
        "bands": {},
    }
    for band in BANDS:
        raw = raw_bands[band]
        if not isinstance(raw, dict) or set(raw) != legacy_fields:
            raise ValueError("multiband %s fields are incomplete or unknown" % band)
        kind = raw["type"]
        if kind not in MODEL_NAMES:
            raise ValueError("multiband %s type is invalid" % band)
        for key in ("auto", "limit", "key_filter", "listen"):
            if not isinstance(raw[key], bool):
                raise ValueError("multiband %s %s must be Boolean" % (band, key))
        threshold = _finite_number(raw["threshold"], band + " threshold", -40.0, 0.0)
        ratio = _finite_number(raw["ratio"], band + " ratio", 1.0, 20.0)
        attack_ms = _finite_number(raw["attack"], band + " attack", 1.0, 100.0)
        release_ms = _finite_number(raw["release"], band + " release", 10.0, 800.0)
        knee = _finite_number(raw["knee"], band + " knee", 0.0, 12.0)
        makeup = _finite_number(raw["makeup"], band + " makeup", 0.0, 24.0)
        key = _finite_number(raw["key"], band + " key", 30.0, 16000.0)
        new = default_band(band)
        new["type"] = kind
        new["key_filter"] = raw["key_filter"]
        new["key"] = max(40.0, key)
        new["listen"] = raw["listen"]
        new["standard"] = {
            "threshold_db": max(-56.0, threshold),
            "ratio": ratio,
            "attack_s": max(0.0002, min(0.15, attack_ms / 1000.0)),
            "release_s": max(0.0025, min(0.9, release_ms / 1000.0)),
            "gain_db": min(28.0, makeup),
            "softknee": knee >= 1.5,
            "automode": bool(raw["auto"]),
        }
        new["tube"] = {
            "peak": max(0.0, min(100.0, 2.0 * (1.0 - threshold))),
            "gain": max(0.0, min(100.0, 40.0 + 2.0 * makeup)),
            "limit_mode": bool(raw["limit"]),
        }
        new["fet"] = {
            "input_db": max(-56.0, min(0.0, threshold)),
            "output_db": 0.0,
            "attack_s": max(0.000021, min(0.0008, attack_ms / 1000.0)),
            "release_s": max(0.05, min(1.1, release_ms / 1000.0)),
            "ratio_index": _legacy_fet_ratio_index(ratio),
        }
        migrated["bands"][band] = new
    return migrated


def _validate_model(band, model, raw):
    if not isinstance(raw, dict) or set(raw) != _MODEL_FIELDS[model]:
        raise ValueError("multiband %s %s fields are incomplete or unknown" %
                         (band, model))
    normalized = {}
    for key, limits in _MODEL_RANGES[model].items():
        normalized[key] = _finite_number(
            raw[key], "%s %s %s" % (band, model, key), *limits)
    for key in _MODEL_BOOLEANS[model]:
        if not isinstance(raw[key], bool):
            raise ValueError("multiband %s %s %s must be Boolean" %
                             (band, model, key))
        normalized[key] = raw[key]
    if model == "fet":
        index = raw["ratio_index"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 4:
            raise ValueError("multiband %s fet ratio_index must be 0..4" % band)
        normalized["ratio_index"] = index
    return normalized


def validate_snapshot(state):
    """Validate and normalize one complete Host-only multiband snapshot."""
    if not isinstance(state, dict):
        raise ValueError("multiband state must be an object")
    expected = {"version", "enabled", "xovers", "bands"}
    if set(state) != expected:
        raise ValueError("multiband state fields must be %s" %
                         ", ".join(sorted(expected)))
    if state["version"] == 1:
        state = _migrate_legacy_snapshot(state)
    elif state["version"] != SNAPSHOT_VERSION:
        raise ValueError("multiband snapshot version must be 1 or 2")
    if not isinstance(state["enabled"], bool):
        raise ValueError("multiband enabled must be Boolean")
    xovers = state["xovers"]
    if not isinstance(xovers, list) or len(xovers) != 3:
        raise ValueError("multiband xovers must contain three values")
    ranges = ((40.0, 400.0), (300.0, 2500.0), (2000.0, 12000.0))
    xovers = [_finite_number(value, "xover %d" % (index + 1), *ranges[index])
              for index, value in enumerate(xovers)]
    if not xovers[0] < xovers[1] < xovers[2]:
        raise ValueError("multiband xovers must be strictly increasing")

    raw_bands = state["bands"]
    if not isinstance(raw_bands, dict) or set(raw_bands) != set(BANDS):
        raise ValueError("multiband bands must contain low/lowmid/highmid/high")
    bands = {}
    for band in BANDS:
        raw = raw_bands[band]
        if not isinstance(raw, dict) or set(raw) != _BAND_FIELDS:
            raise ValueError("multiband %s fields are incomplete or unknown" % band)
        kind = raw["type"]
        if kind not in MODEL_NAMES:
            raise ValueError("multiband %s type is invalid" % band)
        normalized = {"type": kind}
        for model in MODEL_NAMES:
            normalized[model] = _validate_model(band, model, raw[model])
        normalized["key"] = _finite_number(
            raw["key"], "%s key" % band, 40.0, 16000.0)
        for key in ("key_filter", "listen"):
            if not isinstance(raw[key], bool):
                raise ValueError("multiband %s %s must be Boolean" % (band, key))
            normalized[key] = raw[key]
        bands[band] = normalized
    return {
        "version": SNAPSHOT_VERSION,
        "enabled": state["enabled"],
        "xovers": xovers,
        "bands": bands,
    }


def uc_comp_available(cache_dir=None):
    """Built plugin path, or None with a retained user-facing build error."""
    global _LAST_PLUGIN_ERROR
    try:
        path = io24_uc_comp.build_plugin(cache_dir)
    except io24_uc_comp.PluginBuildError as error:
        _LAST_PLUGIN_ERROR = str(error)
        return None
    _LAST_PLUGIN_ERROR = None
    return str(path)


def uc_comp_error():
    return _LAST_PLUGIN_ERROR


def voicefx_delay_available(cache_dir=None):
    """Built Host Delay path, or None with a retained build error."""
    global _LAST_DELAY_PLUGIN_ERROR
    try:
        path = io24_voicefx_delay.build_plugin(cache_dir)
    except io24_voicefx_delay.PluginBuildError as error:
        _LAST_DELAY_PLUGIN_ERROR = str(error)
        return None
    _LAST_DELAY_PLUGIN_ERROR = None
    return str(path)


def voicefx_delay_error():
    return _LAST_DELAY_PLUGIN_ERROR


def pipewire_available():
    return (shutil.which("pipewire") is not None
            and shutil.which("pw-cli") is not None)


# Q of one Butterworth section, and of the all-pass a Linkwitz-Riley pair's
# low and high halves sum to. Set on every crossover biquad rather than left to
# the builtin's default.
BUTTERWORTH_Q = 1.0 / math.sqrt(2.0)

# Each band as a cascade of (biquad, split index). A Linkwitz-Riley half is
# two Butterworth sections at its split, and LP + HP of one pair is an
# all-pass A. So low = LP0.A1.A2, low-mid = HP0.LP1.A2, high-mid = HP0.HP1.LP2
# and high = HP0.HP1.HP2 sum to A0.A1.A2: a flat magnitude. The earlier
# parallel legs (LP0 | HP0.LP1 | HP1.LP2 | HP2) lacked those phase terms and
# summed to a dip of about 1.2 dB near the middle split at the defaults.
CROSSOVER_LEGS = (
    (("bq_lowpass", 0), ("bq_lowpass", 0),
     ("bq_allpass", 1), ("bq_allpass", 2)),
    (("bq_highpass", 0), ("bq_highpass", 0),
     ("bq_lowpass", 1), ("bq_lowpass", 1), ("bq_allpass", 2)),
    (("bq_highpass", 0), ("bq_highpass", 0),
     ("bq_highpass", 1), ("bq_highpass", 1),
     ("bq_lowpass", 2), ("bq_lowpass", 2)),
    (("bq_highpass", 0), ("bq_highpass", 0),
     ("bq_highpass", 1), ("bq_highpass", 1),
     ("bq_highpass", 2), ("bq_highpass", 2)),
)


def crossover_nodes():
    """For each split, every biquad tuned to it (its filters and the
    all-passes that compensate for it), so a split moves as one."""
    nodes = ([], [], [])
    for band, leg in enumerate(CROSSOVER_LEGS):
        for index, (_label, split) in enumerate(leg):
            nodes[split].append("b%d_%d" % (band, index))
    return tuple(tuple(names) for names in nodes)


def _bq(name, label, freq):
    return {"type": "builtin", "name": name, "label": label,
            "control": {"Freq": float(freq), "Q": BUTTERWORTH_Q}}


def compressor_controls(values, sample_rate=48000.0):
    """The exact common controls for one validated per-band model state."""
    model = values["type"]
    key = values["key"] if values["key_filter"] else 0.0
    return io24_uc_comp.model_controls(
        model, values[model], key, values["listen"], sample_rate)


def _uc_comp(name, band, values=None, sample_rate=48000.0, plugin=None):
    values = values or default_band(band)
    return {
        "type": "ladspa",
        "name": name,
        "plugin": plugin or io24_uc_comp.plugin_name(),
        "label": io24_uc_comp.PLUGIN_LABEL,
        "control": compressor_controls(values, sample_rate),
    }


def _lin(name, mult):
    return {"type": "builtin", "name": name, "label": "linear",
            "control": {"Mult": float(mult), "Add": 0.0}}


def build_graph(xovers, with_comp, state=None, sample_rate=48000.0,
                plugin=None):
    """One channel of the 4-band graph; the module clones it per channel.

    Per band i: band signal -> io24 UC compressor -> master mixer. The plugin
    owns the exact model side-chain biquad and key-listen switch represented by
    the common cpxt tuple, so no approximate external key filter is involved.

    with_comp=False: crossover legs straight into the mixer (transparent).
    """
    state = validate_snapshot(state) if state is not None else None
    f0, f1, f2 = state["xovers"] if state is not None else xovers
    nodes = [{"type": "builtin", "name": "split", "label": "copy"}]
    links = []

    def leg(prefix, kinds):
        """A cascade of biquads; returns (entry_port, exit_name)."""
        prev = None
        for i, (label, freq) in enumerate(kinds):
            nm = "%s%d" % (prefix, i)
            nodes.append(_bq(nm, label, freq))
            if prev is not None:
                links.append({"output": prev + ":Out", "input": nm + ":In"})
            prev = nm
        return "%s0:In" % prefix, prev

    # Linkwitz-Riley crossover, all-pass compensated (CROSSOVER_LEGS)
    band_tails = []
    splits = (f0, f1, f2)
    legs = [[(label, splits[split]) for label, split in leg]
            for leg in CROSSOVER_LEGS]
    for bi, kinds in enumerate(legs):
        entry, tail = leg("b%d_" % bi, kinds)
        links.append({"output": "split:Out", "input": entry})
        band_tails.append(tail)

    nodes.append({"type": "builtin", "name": "sum", "label": "mixer"})
    mix_in = 1

    for bi, band in enumerate(BANDS):
        tail = band_tails[bi]
        if with_comp:
            values = (state["bands"][band] if state is not None
                      else default_band(band))
            comp = "c%d" % bi
            nodes.append(_uc_comp(comp, band, values, sample_rate, plugin))
            links.append({"output": tail + ":Out",
                          "input": comp + ":Input"})
            links.append({"output": comp + ":Output",
                          "input": "sum:In %d" % mix_in})
            mix_in += 1
        else:
            links.append({"output": tail + ":Out",
                          "input": "sum:In %d" % mix_in})
            mix_in += 1

    return {"nodes": nodes, "links": links,
            "inputs": ["split:In"], "outputs": ["sum:Out"]}


def build_conf(target=None, xovers=None, with_comp=None, state=None,
               sample_rate=48000.0, plugin=None):
    """A complete pipewire.conf hosting the chain as a sink."""
    if with_comp is None:
        with_comp = uc_comp_available() is not None
    graph = build_graph(xovers or DEFAULTS["xovers"], with_comp, state=state,
                        sample_rate=sample_rate, plugin=plugin)
    playback = {"node.passive": True}
    if target:
        playback["target.object"] = target
    args = {
        "node.description": SINK_DESC,
        "media.name": SINK_DESC,
        "filter.graph": graph,
        "audio.channels": 2,
        "audio.position": ["FL", "FR"],
        "capture.props": {
            "node.name": SINK_NAME,
            "media.class": "Audio/Sink",
        },
        "playback.props": playback,
    }
    return ("context.properties = { log.level = 2 }\n"
            "context.modules = [\n"
            "  { name = libpipewire-module-filter-chain\n"
            "    args = %s\n"
            "  }\n"
            "]\n" % json.dumps(args, indent=2))


def _validate_bus(bus):
    if bus not in BUS_SOURCE_KEYS:
        raise ValueError("bus source must be mixa or mixb")
    return bus


def capture_positions(node_name):
    """The capture node's channel positions in order, or () when unknown.

    Cached per node name, which is safe because changing the card profile
    changes the name (`...analog-surround-21` vs `...pro-audio`).
    """
    if not node_name:
        return ()
    cached = _POSITION_CACHE.get(node_name)
    if cached is not None:
        return cached
    try:
        output = subprocess.run(["pw-dump"], capture_output=True, text=True,
                                timeout=10).stdout
        objects = json.loads(output)
    except Exception:
        return ()
    positions = ()
    for obj in objects:
        props = ((obj.get("info") or {}).get("props") or {})
        if props.get("node.name") != node_name:
            continue
        raw = props.get("audio.position")
        if isinstance(raw, str):
            raw = [part.strip()
                   for part in raw.strip("[] ").split(",") if part.strip()]
        if raw:
            positions = tuple(raw)
        elif isinstance(props.get("audio.channels"), int):
            positions = tuple("CH%d" % index
                              for index in range(props["audio.channels"]))
        break
    if positions:
        _POSITION_CACHE[node_name] = positions
    return positions


def bus_source_indices(bus, positions=CAPTURE_POSITIONS):
    """Node indices carrying one bus's pair, or None when this profile does
    not expose them. Publishing silence from absent channels is the bug this
    replaces: `analog-surround-21` has three channels, so Mix A took the LFE
    channel plus one that did not exist and Mix B took two that did not."""
    left, right = BUS_DEVICE_CHANNELS[_validate_bus(bus)]
    if len(positions) < right:
        return None
    return (left - 1, right - 1)


def build_bus_source_graph(bus, positions=CAPTURE_POSITIONS):
    """Select one exact capture pair and expose it at fixed unity gain."""
    bus = _validate_bus(bus)
    indices = bus_source_indices(bus, positions)
    if indices is None:
        raise ValueError(
            "%s needs device capture channel %d; this profile exposes %d"
            % (BUS_SOURCE_DESCRIPTIONS[bus], BUS_DEVICE_CHANNELS[bus][1],
               len(positions)))
    nodes = [
        {"type": "builtin", "name": "src%d" % index, "label": "copy"}
        for index in range(len(positions))
    ]
    nodes.extend((_lin("source_l", 1.0), _lin("source_r", 1.0)))
    left_index, right_index = indices
    return {
        "nodes": nodes,
        "links": [
            {"output": "src%d:Out" % left_index,
             "input": "source_l:In"},
            {"output": "src%d:Out" % right_index,
             "input": "source_r:In"},
        ],
        "inputs": ["src%d:In" % index for index in range(len(positions))],
        "outputs": ["source_l:Out", "source_r:Out"],
    }


def build_bus_source_conf(bus, capture_target, positions=CAPTURE_POSITIONS):
    """PipeWire config for one passive Mix A or Mix B virtual source."""
    bus = _validate_bus(bus)
    if not capture_target:
        raise ValueError("bus source needs a capture target")
    name = BUS_SOURCE_NAMES[bus]
    description = BUS_SOURCE_DESCRIPTIONS[bus]
    args = {
        "node.description": description,
        "media.name": description,
        "filter.graph": build_bus_source_graph(bus, positions),
        "capture.props": {
            "node.name": name + "-capture",
            "media.class": "Stream/Input/Audio",
            "target.object": capture_target,
            "node.passive": True,
            "stream.dont-remix": True,
            "audio.channels": len(positions),
            "audio.position": list(positions),
        },
        "playback.props": {
            "node.name": name,
            "node.description": description,
            "media.class": "Audio/Source",
            "node.passive": True,
            "stream.dont-remix": True,
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
        },
    }
    return ("context.properties = { log.level = 2 }\n"
            "context.modules = [\n"
            "  { name = libpipewire-module-filter-chain\n"
            "    args = %s\n"
            "  }\n"
            "]\n" % json.dumps(args, indent=2))


# --------------------------------------------------------------------------
# Multiband as a compressor type: the input insert.
#
# Asked for on 2026-09-11: the multiband as "just another type of compression
# that can be selected, replacing the normal compressor block", and, since the
# unit's own chain cannot be split, "immediately" after the Fat Channel. The
# io24 delivers each input to USB capture after the whole Fat Channel, limiter
# included (its manual: it records "just as you hear it in your headphones,
# complete with the Fat Channel preset"; firmware 0128 has no parameter that
# changes that). So the insert takes the input there:
#
#   capture 1 -> [in1_ 4-band] -> out1 \  Audio/Source "io24 Input 1+2
#   capture 2 -> [in2_ 4-band] -> out2 /  Multiband" (left Input 1, right 2)
#   that source -> [mixer: channels with Multiband] -> USB playback 1-2
#
# An input without Multiband passes through untouched, so the published source
# stands in for capture 1+2 whichever channels are processed. The return is
# centred, both legs at unity, as a mono input sits in the unit's mixer. It
# arrives on USB playback 1-2, the user's choice, so it shares that fader with
# computer audio. route_insert then takes the input's own feed out of each bus
# it was in and makes USB playback 1-2 audible there; unroute_insert undoes
# exactly that.
# --------------------------------------------------------------------------
INSERT_VERSION = 2
INSERT_CHANNELS = (1, 2)
INSERT_CAPTURE_INDEX = {1: 0, 2: 1}      # USB capture channel of each input
INSERT_PROCESS_NAME = "io24-input-multiband-process"
INSERT_SOURCE_NAME = "io24-input-multiband"
INSERT_SOURCE_DESC = "io24 Input 1+2 Multiband"
INSERT_RETURN_NAME = "io24-input-multiband-return"
INSERT_RETURN_SOURCE = "return/ch1"      # the mixer's USB playback 1-2
INSERT_BUSES = ("main", "mixa", "mixb")
# Monitoring now goes to the computer and back, so the buffer is held small
# while an insert runs (GUIDE.md: latency is quantum / rate).
INSERT_QUANTUM = 128


def insert_prefix(channel):
    """Node-name prefix of one channel's 4-band graph inside the insert."""
    return "in%d_" % channel


def _prefixed(graph, prefix):
    """The same graph with every node renamed, so two fit in one chain."""
    def port(ref):
        node, _sep, rest = ref.partition(":")
        return "%s%s:%s" % (prefix, node, rest)
    return {
        "nodes": [dict(node, name=prefix + node["name"])
                  for node in graph["nodes"]],
        "links": [{"output": port(link["output"]),
                   "input": port(link["input"])} for link in graph["links"]],
        "inputs": [port(name) for name in graph["inputs"]],
        "outputs": [port(name) for name in graph["outputs"]],
    }


def _insert_states(states, allow_empty=False):
    """{channel: snapshot} for the channels with Multiband, validated."""
    if not isinstance(states, dict) or (not states and not allow_empty):
        raise ValueError("the multiband insert needs at least one channel")
    normalized = {}
    for channel, state in states.items():
        if isinstance(channel, bool) or channel not in INSERT_CHANNELS:
            raise ValueError("multiband insert channel must be 1 or 2")
        normalized[channel] = validate_snapshot(state)
    return normalized


def _insert_delays(delays):
    """{channel: VocalEcho state} for safe Host processing, validated."""
    if delays is None:
        return {}
    if not isinstance(delays, dict):
        raise ValueError("the Host Delay insert state must be an object")
    normalized = {}
    for channel, state in delays.items():
        if isinstance(channel, bool) or channel not in INSERT_CHANNELS:
            raise ValueError("Host Delay insert channel must be 1 or 2")
        normalized[channel] = io24_voicefx_delay.validate_state(state)
    return normalized


def _insert_effects(states, delays):
    states = _insert_states(states, allow_empty=bool(delays))
    delays = _insert_delays(delays)
    channels = tuple(sorted(set(states) | set(delays)))
    if not channels:
        raise ValueError("the Host insert needs at least one channel")
    return states, delays, channels


def build_insert_graph(states, with_comp=True, positions=CAPTURE_POSITIONS,
                       sample_rate=48000.0, plugin=None, delays=None,
                       delay_plugin=None):
    """Both inputs, each through its requested computer-side processors.

    Multiband remains first, directly after the unit's Fat Channel. At 96 kHz
    the safe Voice FX Delay follows it in the same graph, so both features can
    be selected without two processes fighting over the input's mixer route.

    Only device capture channels 1 and 2 are needed, so every profile that
    exposes a stereo pair carries the insert.
    """
    states, delays, _channels = _insert_effects(states, delays)
    if len(positions) < max(INSERT_CAPTURE_INDEX.values()) + 1:
        raise ValueError(
            "the Multiband insert needs both inputs; this profile exposes %d "
            "capture channel%s" % (len(positions),
                                   "" if len(positions) == 1 else "s"))
    nodes = [{"type": "builtin", "name": "src%d" % index, "label": "copy"}
             for index in range(len(positions))]
    links = []
    for channel in INSERT_CHANNELS:
        tap = "src%d:Out" % INSERT_CAPTURE_INDEX[channel]
        tail = tap
        out = "out%d" % channel
        nodes.append({"type": "builtin", "name": out, "label": "copy"})
        if channel in states:
            sub = _prefixed(build_graph(
                None, with_comp, state=states[channel],
                sample_rate=sample_rate, plugin=plugin),
                            insert_prefix(channel))
            nodes.extend(sub["nodes"])
            links.extend(sub["links"])
            links.append({"output": tail, "input": sub["inputs"][0]})
            tail = sub["outputs"][0]
        if channel in delays:
            name = insert_prefix(channel) + "delay"
            nodes.append({
                "type": "ladspa",
                "name": name,
                "plugin": delay_plugin or io24_voicefx_delay.plugin_name(),
                "label": io24_voicefx_delay.PLUGIN_LABEL,
                "control": io24_voicefx_delay.plugin_controls(delays[channel]),
            })
            links.append({"output": tail, "input": name + ":Input"})
            tail = name + ":Output"
        links.append({"output": tail, "input": out + ":In"})
    return {"nodes": nodes, "links": links,
            "inputs": ["src%d:In" % index for index in range(len(positions))],
            "outputs": ["out%d:Out" % channel for channel in INSERT_CHANNELS]}


def build_insert_return_graph(channels):
    """Only the processed inputs, centred, for USB playback 1-2."""
    channels = tuple(channels)
    if not channels or any(isinstance(channel, bool) or
                           channel not in INSERT_CHANNELS
                           for channel in channels):
        raise ValueError("the multiband return needs channel 1 and/or 2")
    gains = {"Gain %d" % (index + 1): 1.0 if channel in channels else 0.0
             for index, channel in enumerate(INSERT_CHANNELS)}
    return {
        "nodes": [
            {"type": "builtin", "name": "take1", "label": "copy"},
            {"type": "builtin", "name": "take2", "label": "copy"},
            {"type": "builtin", "name": "mix", "label": "mixer",
             "control": gains},
            {"type": "builtin", "name": "ret_l", "label": "copy"},
            {"type": "builtin", "name": "ret_r", "label": "copy"},
        ],
        "links": [
            {"output": "take1:Out", "input": "mix:In 1"},
            {"output": "take2:Out", "input": "mix:In 2"},
            {"output": "mix:Out", "input": "ret_l:In"},
            {"output": "mix:Out", "input": "ret_r:In"},
        ],
        "inputs": ["take1:In", "take2:In"],
        "outputs": ["ret_l:Out", "ret_r:Out"],
    }


def build_insert_conf(states, capture_target, playback_target,
                      with_comp=None, positions=CAPTURE_POSITIONS,
                      sample_rate=48000.0, plugin=None, delays=None,
                      delay_plugin=None):
    """PipeWire config for the insert: the processing, published as a source,
    and its return into the io24's USB playback 1-2.

    Every device-facing stream names its node and may not fall back to another
    device: an insert that lost the io24 must go quiet, not process the
    laptop's microphone into its speakers.
    """
    states, delays, channels = _insert_effects(states, delays)
    if not capture_target or not playback_target:
        raise ValueError("the multiband insert needs the io24's capture and "
                         "playback nodes")
    if with_comp is None and states:
        with_comp = uc_comp_available() is not None
    process = {
        "node.description": INSERT_SOURCE_DESC,
        "media.name": INSERT_SOURCE_DESC,
        "filter.graph": build_insert_graph(
            states, with_comp, positions, sample_rate, plugin,
            delays=delays, delay_plugin=delay_plugin),
        "capture.props": {
            "node.name": INSERT_PROCESS_NAME,
            "media.class": "Stream/Input/Audio",
            "target.object": capture_target,
            "node.dont-fallback": True,
            "stream.dont-remix": True,
            "audio.channels": len(positions),
            "audio.position": list(positions),
        },
        "playback.props": {
            "node.name": INSERT_SOURCE_NAME,
            "node.description": INSERT_SOURCE_DESC,
            "media.class": "Audio/Source",
            "stream.dont-remix": True,
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
        },
    }
    ret = {
        "node.description": "io24 Multiband return",
        "media.name": "io24 Multiband return",
        "filter.graph": build_insert_return_graph(channels),
        "capture.props": {
            "node.name": INSERT_RETURN_NAME + "-in",
            "media.class": "Stream/Input/Audio",
            "target.object": INSERT_SOURCE_NAME,
            "node.dont-fallback": True,
            "stream.dont-remix": True,
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
        },
        "playback.props": {
            "node.name": INSERT_RETURN_NAME,
            "media.class": "Stream/Output/Audio",
            "target.object": playback_target,
            "node.dont-fallback": True,
            "channelmix.upmix": False,
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
        },
    }
    modules = "\n".join(
        "  { name = libpipewire-module-filter-chain\n"
        "    args = %s\n"
        "  }" % json.dumps(args, indent=2) for args in (process, ret))
    return ("context.properties = { log.level = 2 }\n"
            "context.modules = [\n%s\n]\n" % modules)


def default_insert_state():
    """Both inputs' Multiband settings, neither selected, nothing rerouted."""
    return {"version": INSERT_VERSION,
            "channels": {str(channel): default_snapshot()
                         for channel in INSERT_CHANNELS},
            "routing": None, "quantum_before": None}


def _validate_routing(routing):
    """A copy of what route_insert changed in the mixer, or None."""
    if routing is None:
        return None
    if not isinstance(routing, dict) or \
            set(routing) != {"moved", "return_prior"}:
        raise ValueError("multiband routing must hold moved and return_prior")
    moved = routing["moved"]
    if not isinstance(moved, dict) or \
            not set(moved) <= {str(c) for c in INSERT_CHANNELS}:
        raise ValueError("multiband routing moved channels must be 1 or 2")
    normalized = {"moved": {}, "return_prior": {}}
    for key, buses in moved.items():
        if not isinstance(buses, list) or len(set(buses)) != len(buses) or \
                not set(buses) <= set(INSERT_BUSES):
            raise ValueError("multiband routing buses must be main/mixa/mixb")
        normalized["moved"][key] = [bus for bus in INSERT_BUSES if bus in buses]
    prior = routing["return_prior"]
    if not isinstance(prior, dict) or not set(prior) <= set(INSERT_BUSES):
        raise ValueError("multiband routing return buses must be main/mixa/mixb")
    for bus, was in prior.items():
        if not isinstance(was, dict) or set(was) != {"assigned", "off"} or \
                not all(isinstance(was[k], bool) for k in was):
            raise ValueError("multiband routing return state is invalid")
        normalized["return_prior"][bus] = dict(was)
    return normalized


def validate_insert_state(state):
    """Validate and normalize the Host-only Multiband compressor state."""
    if not isinstance(state, dict):
        raise ValueError("multiband insert state must be an object")
    expected = {"version", "channels", "routing", "quantum_before"}
    if set(state) != expected:
        raise ValueError("multiband insert fields must be %s" %
                         ", ".join(sorted(expected)))
    if state["version"] not in (1, INSERT_VERSION):
        raise ValueError("multiband insert version must be 1 or 2")
    channels = state["channels"]
    if not isinstance(channels, dict) or \
            set(channels) != {str(c) for c in INSERT_CHANNELS}:
        raise ValueError("multiband insert channels must be 1 and 2")
    quantum = state["quantum_before"]
    if quantum is not None and (isinstance(quantum, bool) or
                                not isinstance(quantum, int) or
                                not 0 <= quantum <= 8192):
        raise ValueError("multiband insert quantum_before must be 0..8192")
    return {"version": INSERT_VERSION,
            "channels": {key: validate_snapshot(channels[key])
                         for key in sorted(channels)},
            "routing": _validate_routing(state["routing"]),
            "quantum_before": quantum}


def _audible(dev, source, bus):
    return (dev.send_assigned(source, bus) and dev.has_send_level(source, bus)
            and dev.send_db(source, bus) is not None)


def route_insert(dev, channel, routing=None):
    """Put one input's Multiband return in place of its own feed.

    `dev` is the Io24 driver. In every bus the input is assigned to, USB
    playback 1-2 is made audible (and how it was is noted when it was
    unassigned or off), then the input's send is switched off with its fader
    position kept. Returns the new routing record; `routing` is not changed.
    """
    routing = _validate_routing(routing) or {"moved": {}, "return_prior": {}}
    key = str(channel)
    if key in routing["moved"]:
        return routing
    source = "line/ch%d" % channel
    ret = INSERT_RETURN_SOURCE
    moved = []
    for bus in INSERT_BUSES:
        if not dev.send_assigned(source, bus):
            continue
        if bus not in routing["return_prior"] and not _audible(dev, ret, bus):
            was = {"assigned": bool(dev.send_assigned(ret, bus)),
                   "off": bool(dev.has_send_level(ret, bus) and
                               dev.send_db(ret, bus) is None)}
            if not was["assigned"] or was["off"]:
                routing["return_prior"][bus] = was
            if was["off"] or not dev.has_send_level(ret, bus):
                dev.set_send_db(ret, bus, dev.DEFAULT_SEND_DB)
            if not was["assigned"]:
                dev.set_send_assigned(ret, bus, True)
        dev.set_send_assigned(source, bus, False)
        moved.append(bus)
    routing["moved"][key] = moved
    return routing


def unroute_insert(dev, channel, routing):
    """Undo route_insert for one input. USB playback 1-2 goes back to how it
    was in a bus only once no other input's return still needs it there."""
    routing = _validate_routing(routing) or {"moved": {}, "return_prior": {}}
    for bus in routing["moved"].pop(str(channel), []):
        dev.set_send_assigned("line/ch%d" % channel, bus, True)
    still = {bus for buses in routing["moved"].values() for bus in buses}
    for bus in INSERT_BUSES:
        if bus in still or bus not in routing["return_prior"]:
            continue
        was = routing["return_prior"].pop(bus)
        if was["off"]:
            dev.set_send_db(INSERT_RETURN_SOURCE, bus, None)
        if not was["assigned"]:
            dev.set_send_assigned(INSERT_RETURN_SOURCE, bus, False)
    return routing


def _find_io24_node(direction):
    """Find a PipeWire ALSA input/output node for the connected io24."""
    token = "alsa_%s" % direction
    try:
        out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    # pw-cli indents real global-id lines with a tab. Split after stripping
    # each line instead of assuming an unindented "\nid "; otherwise the
    # whole listing becomes one block and a laptop ALSA node before the io24
    # can be returned merely because the later io24 marker is also present.
    blocks, current = [], []
    for line in out.splitlines():
        value = line.strip()
        if value.startswith("id ") and "," in value:
            if current:
                blocks.append("\n".join(current))
            current = [value]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    for block in blocks:
        folded = block.casefold()
        if token not in folded or not any(
                marker in folded for marker in ("presonus", "revelator", "io24")):
            continue
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("node.name"):
                name = line.split("=", 1)[1].strip().strip('"')
                if token in name.casefold():
                    return name
    return None


def find_io24_sink():
    """Node name of the io24's playback sink, or None while unplugged."""
    return _find_io24_node("output")


def find_io24_capture_source():
    """Node name of the io24's physical capture source, or None."""
    return _find_io24_node("input")


def node_id(node_name):
    """PipeWire global id for an exact node name, or None."""
    if not node_name:
        return None
    try:
        out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    current = None
    for line in out.splitlines():
        value = line.strip()
        if value.startswith("id ") and "," in value:
            current = value.split()[1].rstrip(",")
        elif value == 'node.name = "%s"' % node_name:
            return current
    return None


def _playback_state(node_name):
    """The exact PipeWire playback mute/volume state for ``node_name``.

    The Host insert returns through the io24's playback sink. A filter graph
    can be fully alive while that sink is muted, which leaves the device mixer
    receiving silence. Read the state before moving any hardware mixer feed so
    that case fails closed instead of wearing an "On" label.
    """
    nid = node_id(node_name)
    if nid is None or shutil.which("wpctl") is None:
        raise RuntimeError("the io24 playback sink is unavailable")
    try:
        result = subprocess.run(
            ["wpctl", "get-volume", str(nid)], capture_output=True,
            text=True, timeout=5)
    except Exception as error:
        raise RuntimeError("the io24 playback state could not be read") from error
    if result.returncode != 0:
        raise RuntimeError("the io24 playback state could not be read")
    match = re.search(r"\bVolume:\s*([0-9]+(?:\.[0-9]+)?)", result.stdout)
    if match is None:
        raise RuntimeError("the io24 playback volume could not be read")
    return {"node": node_name, "id": str(nid),
            "volume": float(match.group(1)),
            "muted": "[MUTED]" in result.stdout}


def _set_playback_mute(node_name, muted):
    nid = node_id(node_name)
    if nid is None or shutil.which("wpctl") is None:
        return False
    try:
        result = subprocess.run(
            ["wpctl", "set-mute", str(nid), "1" if muted else "0"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return result.returncode == 0


def acquire_playback(node_name):
    """Open the io24 playback return and return what must be restored.

    Only mute is borrowed.  The user's output volume is never changed: doing
    so would also turn up ordinary computer playback on USB 1-2.  Zero volume
    is rejected before the device input is rerouted, since unmuting it would
    still produce a silent Multiband return.
    """
    state = _playback_state(node_name)
    if state["volume"] <= 0.0:
        raise RuntimeError("the io24 playback volume is zero")
    restore_muted = state["muted"]
    if restore_muted:
        if not _set_playback_mute(node_name, False):
            raise RuntimeError("the io24 playback sink could not be unmuted")
        try:
            observed = _playback_state(node_name)
            if observed["muted"]:
                raise RuntimeError("the io24 playback sink stayed muted")
        except RuntimeError:
            # We changed system state but cannot prove the return is usable.
            # Put the original mute back before failing closed.
            _set_playback_mute(node_name, True)
            raise
    return {"node": node_name, "restore_muted": restore_muted}


def release_playback(guard):
    """Restore only the mute state borrowed by :func:`acquire_playback`."""
    if not guard or not guard.get("restore_muted"):
        return True
    return _set_playback_mute(guard.get("node"), True)


def set_default_output(node_name):
    """Make an existing PipeWire sink the default for new playback."""
    nid = node_id(node_name)
    if nid is None or shutil.which("wpctl") is None:
        return False
    try:
        result = subprocess.run(["wpctl", "set-default", str(nid)],
                                capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return result.returncode == 0


class Chain:
    """Owns the child pipewire process hosting the sink."""

    NODE_NAME = SINK_NAME
    CONF_PREFIX = "io24-mbc-"
    FRAGMENT = "io24-mbc.conf"
    START_TIMEOUT_S = 2.0

    def __init__(self):
        self.proc = None
        self.conf_path = None
        self.last_error = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def ladspa_paths(self):
        """Plugin search roots required by this particular chain."""
        return (io24_uc_comp.default_cache_dir(),)

    def start(self, target=None, with_comp=None, state=None,
              sample_rate=48000.0, plugin=None):
        configured = target is not None or with_comp is not None or state is not None
        # Validate and render before creating any private filesystem state.
        # Invalid saved settings must fail closed without leaving a directory.
        configuration = build_conf(
            target=target, with_comp=with_comp, state=state,
            sample_rate=sample_rate, plugin=plugin)
        self.last_error = None
        if not self._launch(configuration, configured):
            self.last_error = "PipeWire process could not start"
            return False
        deadline = time.monotonic() + self.START_TIMEOUT_S
        while self.running:
            if self.node_id() is not None:
                return True
            if time.monotonic() >= deadline:
                self.last_error = "io24 Multiband output node did not appear"
                break
            time.sleep(0.05)
        if self.last_error is None:
            self.last_error = "PipeWire process exited before its output node appeared"
        self.stop()
        return False

    def _launch(self, configuration, configured=True):
        if self.running and not configured:
            return True
        # A configured start means "run this exact graph", not merely "some
        # graph is alive". Replace an existing or crashed child so a True
        # return never hides stale target/settings, and collect stale temp state.
        if self.proc is not None or self.conf_path is not None:
            self.stop()
        # A bare conf with only the filter module dies at startup: a standalone
        # `pipewire -c` needs the whole client stack (protocol-native,
        # client-node, adapter...). Rather than maintain a copy of that
        # boilerplate, use the mechanism PipeWire documents for exactly this:
        # the shipped filter-chain.conf as the base, our chain as a .conf.d
        # fragment, and PIPEWIRE_CONFIG_DIR pointing at a private config dir so
        # nothing leaks into the user's real configuration.
        base = "/usr/share/pipewire/filter-chain.conf"
        if not os.path.exists(base):
            return False
        self.conf_path = tempfile.mkdtemp(prefix=self.CONF_PREFIX)
        try:
            shutil.copy(base, os.path.join(self.conf_path, "filter-chain.conf"))
            frag_dir = os.path.join(self.conf_path, "filter-chain.conf.d")
            os.mkdir(frag_dir)
            with open(os.path.join(frag_dir, self.FRAGMENT), "w") as f:
                f.write(configuration)
            env = dict(os.environ)
            env["PIPEWIRE_CONFIG_DIR"] = self.conf_path
            ladspa = os.pathsep.join(str(path) for path in self.ladspa_paths())
            if env.get("LADSPA_PATH"):
                ladspa += os.pathsep + env["LADSPA_PATH"]
            env["LADSPA_PATH"] = ladspa
            self.proc = subprocess.Popen(
                ["pipewire", "-c", "filter-chain.conf"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        except (OSError, subprocess.SubprocessError):
            self.proc = None
            shutil.rmtree(self.conf_path, ignore_errors=True)
            self.conf_path = None
            return False
        return True

    def stop(self):
        proc, self.proc = self.proc, None
        try:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except (OSError, subprocess.SubprocessError):
                        pass
                except (OSError, subprocess.SubprocessError):
                    pass
        finally:
            if self.conf_path:
                shutil.rmtree(self.conf_path, ignore_errors=True)
                self.conf_path = None

    def node_id(self):
        """The chain sink's node id, for set-param. None until it appears."""
        return node_id(self.NODE_NAME)

    def set_controls(self, controls):
        """Live parameter changes: {'c_low:Ratio (1:n)': 4.0, ...}.

        Silently no-ops when the chain is down — a slider moved while the
        sink is off should not raise, it should just have nothing to do.
        """
        nid = self.node_id()
        if nid is None:
            return False
        params = []
        for k, v in controls.items():
            params += [json.dumps(k), str(float(v))]
        body = "{ params = [ %s ] }" % " ".join(params)
        try:
            r = subprocess.run(["pw-cli", "set-param", nid, "Props", body],
                               capture_output=True, text=True, timeout=5)
            # returncode checked: an earlier version returned True whenever
            # pw-cli merely ran, which reported success for writes that never
            # landed — the exact failure mode the audio test had to unmask
            return r.returncode == 0
        except Exception:
            return False


class BusSourceChain(Chain):
    """Own one independent passive Mix A or Mix B source process."""

    def __init__(self, bus):
        super().__init__()
        self.bus = _validate_bus(bus)
        self.NODE_NAME = BUS_SOURCE_NAMES[bus]
        self.CONF_PREFIX = self.NODE_NAME + "-"
        self.FRAGMENT = self.NODE_NAME + ".conf"
        self._configuration = None

    def start(self, capture_target=None, positions=None):
        if positions is None:
            positions = capture_positions(capture_target) or CAPTURE_POSITIONS
        configuration = build_bus_source_conf(
            self.bus, capture_target, positions)
        if self.running and configuration == self._configuration:
            return True
        started = self._launch(configuration, configured=True)
        if started:
            self._configuration = configuration
        return started

    def node_id(self):
        """Return only the public Audio/Source node, never its capture side."""
        try:
            output = subprocess.run(
                ["pw-cli", "ls", "Node"], capture_output=True,
                text=True, timeout=5).stdout
        except Exception:
            return None
        node_id = None
        expected = 'node.name = "%s"' % self.NODE_NAME
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("id ") and "," in stripped:
                node_id = stripped.split()[1].rstrip(",")
            elif stripped == expected and node_id is not None:
                return node_id
        return None


class BusSourceManager:
    """Reconcile two separately owned bus sources without coupled failure."""

    START_TIMEOUT_S = 5.0

    def __init__(self, chains=None):
        self.chains = chains or {
            bus: BusSourceChain(bus) for bus in BUS_SOURCE_KEYS
        }
        if set(self.chains) != set(BUS_SOURCE_KEYS):
            raise ValueError("bus source manager needs mixa and mixb chains")
        self.statuses = {bus: "unavailable" for bus in BUS_SOURCE_KEYS}
        self._started_at = {bus: None for bus in BUS_SOURCE_KEYS}

    def reconcile(self, capture_target, now=None, positions=None):
        """Start, observe, and independently classify both virtual sources.

        A bus whose capture channels this card profile does not expose is
        reported `profile` and left stopped: publishing a source fed by absent
        channels is silence wearing the name of a mix.
        """
        now = time.monotonic() if now is None else float(now)
        if not capture_target:
            self.stop()
            return dict(self.statuses)
        if positions is None:
            positions = capture_positions(capture_target) or CAPTURE_POSITIONS

        for bus in BUS_SOURCE_KEYS:
            chain = self.chains[bus]
            if bus_source_indices(bus, positions) is None:
                chain.stop()
                self.statuses[bus] = "profile"
                self._started_at[bus] = None
                continue
            if not chain.running:
                if not chain.start(capture_target, positions):
                    self.statuses[bus] = "failed"
                    self._started_at[bus] = None
                    continue
                self._started_at[bus] = now

            if chain.node_id() is not None:
                self.statuses[bus] = "ready"
                self._started_at[bus] = None
                continue

            started_at = self._started_at[bus]
            if started_at is None:
                self._started_at[bus] = now
                started_at = now
            if now - started_at >= self.START_TIMEOUT_S:
                chain.stop()
                self.statuses[bus] = "failed"
                self._started_at[bus] = None
            else:
                self.statuses[bus] = "starting"
        return dict(self.statuses)

    def stop(self):
        for bus in BUS_SOURCE_KEYS:
            self.chains[bus].stop()
            self.statuses[bus] = "unavailable"
            self._started_at[bus] = None


def set_default_input(node_name):
    """Make an existing PipeWire source the default for new recording.
    `wpctl set-default` picks sink or source from the node itself."""
    return set_default_output(node_name)


def _present_nodes():
    """Every PipeWire node name present now, from one listing."""
    try:
        out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return set()
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("node.name = "):
            names.add(line.split("=", 1)[1].strip().strip('"'))
    return names


class InsertChain(Chain):
    """Owns the input insert: one child process with the processing and its
    return. Multiband and the 96 kHz Voice FX Delay share this graph and one
    mixer route. Changing processor membership restarts it; ordinary control
    moves are live."""

    NODE_NAME = INSERT_PROCESS_NAME
    CONF_PREFIX = "io24-insert-"
    FRAGMENT = "io24-insert.conf"
    START_TIMEOUT_S = 3.0
    READY_NODES = (INSERT_PROCESS_NAME, INSERT_SOURCE_NAME, INSERT_RETURN_NAME)

    def __init__(self):
        super().__init__()
        self.channels = ()
        self.multiband_channels = ()
        self.delay_channels = ()
        self.playback_guard = None
        self._configuration = None

    def start(self, states, capture_target=None, playback_target=None,
              with_comp=None, positions=None, sample_rate=48000.0,
              plugin=None, delays=None, delay_plugin=None):
        """Run the insert for exactly these channels. True once all three of
        its nodes are present; anything short of that stops it again."""
        if positions is None:
            positions = capture_positions(capture_target) or CAPTURE_POSITIONS
        states, delays, channels = _insert_effects(states, delays)
        configuration = build_insert_conf(
            states, capture_target, playback_target, with_comp=with_comp,
            positions=positions, sample_rate=sample_rate, plugin=plugin,
            delays=delays, delay_plugin=delay_plugin)
        if self.running and configuration == self._configuration:
            return True
        self.last_error = None
        if not self._launch(configuration, configured=True):
            self.channels = ()
            self.multiband_channels = ()
            self.delay_channels = ()
            self.last_error = "PipeWire process could not start"
            return False
        self._configuration = configuration
        deadline = time.monotonic() + self.START_TIMEOUT_S
        while self.running:
            if set(self.READY_NODES) <= _present_nodes():
                try:
                    self.playback_guard = acquire_playback(playback_target)
                except RuntimeError as error:
                    self.last_error = str(error)
                    break
                self.channels = channels
                self.multiband_channels = tuple(sorted(states))
                self.delay_channels = tuple(sorted(delays))
                return True
            if time.monotonic() >= deadline:
                self.last_error = "the io24 Input Multiband nodes did not appear"
                break
            time.sleep(0.05)
        if self.last_error is None:
            self.last_error = "PipeWire process exited before its nodes appeared"
        self.stop()
        return False

    def stop(self, restore_playback=True):
        super().stop()
        self.channels = ()
        self.multiband_channels = ()
        self.delay_channels = ()
        self._configuration = None
        if restore_playback:
            self.restore_playback()

    def restore_playback(self):
        """Put back a mute borrowed for the return, after mixer restoration."""
        guard = self.playback_guard
        if guard is None:
            return True
        if not release_playback(guard):
            return False
        self.playback_guard = None
        return True

    def set_channel_controls(self, channel, controls):
        """Live moves for one channel's graph, keyed as for Chain."""
        if channel not in self.channels:
            return False
        prefix = insert_prefix(channel)
        return self.set_controls(
            {prefix + key: value for key, value in controls.items()})

    def set_delay_controls(self, channel, state):
        """Move VocalEcho controls without rebuilding the PipeWire graph."""
        if channel not in self.delay_channels:
            return False
        controls = io24_voicefx_delay.plugin_controls(state)
        prefix = insert_prefix(channel) + "delay:"
        return self.set_controls(
            {prefix + key: value for key, value in controls.items()})


if __name__ == "__main__":
    import sys
    processor = uc_comp_available()
    print("UC compressor:", processor or uc_comp_error())
    print("pipewire:", pipewire_available())
    print("io24 sink:", find_io24_sink() or "not present")
    if "--run" in sys.argv:
        import time
        c = Chain()
        c.start(with_comp=processor is not None)
        time.sleep(2)
        print("node id:", c.node_id())
        print("running:", c.running)
        c.stop()
        print("stopped")
