#!/usr/bin/env python3
"""
io24gtk — a native GTK4 / libadwaita mixer for the PreSonus Revelator io24.

The Linux replacement for Universal Control's window. Needs the PyGObject GTK4
and libadwaita bindings.

    io24gtk.py [--wait]

USB is exclusive: run this OR io24d OR ucnet_shim, not several.

Two things this is built around, both learned the hard way:

  * REAL TIME, BOTH WAYS. One 0x7EC state read costs 0.33 ms and carries every
    meter *and* every knob position, so the worker polls it at 30 fps and the
    controls follow the hardware. Turn the gain knob on the box and the fader
    moves. A widget the user is currently touching is left alone for a moment
    afterwards, so the device can never fight your hand.

  * DRAWN METERS, NOT PROGRESS BARS. Gtk.LevelBar has no ballistics, no peak
    hold and no scale. These are Cairo, with segment colouring, a 1.5 s peak
    hold and a dB scale, plus gain-reduction bars that run backwards the way
    every compressor display does.
"""
import math
import json
import os
import queue
import re
import struct
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
from gi.repository import (Adw, GLib, Gtk, Gio, Gdk, Gsk, Graphene,  # noqa: E402
                           Pango)

import io24                                # noqa: E402  (STEREO_PAIRS, pan law)
import io24_alt_eq                         # noqa: E402  (exact Passive/Vintage EQ)
import io24_dsp                            # noqa: E402  (exact compressor models)
import io24_fx                             # noqa: E402  (vendor VoiceFX schema/builders)
import io24_mbc                            # noqa: E402  (host multiband)
import io24_spring                         # noqa: E402  (host spring reverb)
import io24_voicefx_delay                  # noqa: E402  (safe 96 kHz Delay)
import io24_presets                        # noqa: E402  (shared preset model resolver)
import io24_scene                          # noqa: E402  (UC scene import)
from io24_preset_record import (           # noqa: E402
    DevicePresetLibraryRegistry,
    DeviceSlotRegistry,
    complete_device_slot_record,
)
from io24d import Device, wait_for_device  # noqa: E402

# JaSt slots (PROTOCOL.md §6)
M_IN1, M_IN2 = 4, 6
M_MAIN = (12, 13)
M_MIXA = (14, 15)
M_MIXB = (16, 17)
P_HP, P_MAIN, P_BLEND, P_G1, P_G2 = 43, 44, 45, 46, 47

PROFILE = bool(os.environ.get('IO24_PROFILE'))
BAND_NAMES = [spec["name"] for spec in io24_presets.STANDARD_EQ_BAND_SPECS]
# Kept as a public compatibility constant for tests/extensions.  The GTK page
# no longer offers one free-form shape menu: UC 4.7.2 exposes only Low Shelf on
# band 1 and High Shelf on band 4; the two middle bands are always parametric.
SHAPES = ["off", "peaking", "lowshelf", "highshelf"]
FLOOR = -60.0
HPF_BASIC_HZ = (24.0, 40.0, 80.0, 160.0)
DEVICE_SLOT_REGISTRY_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "io24", "device-slots.json")
DEVICE_PRESET_LIBRARY_REGISTRY_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "io24", "device-presets.json")

# What the Host last had open, restored at launch. Added 2026-09-11 at the
# user's request: pick up where the last session left off, as Universal Control
# did. The device half is the driver's own write mirror, ~/.cache/io24/shadow.json.
LAST_SESSION_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "io24", "last-session.json")
# Setters the unit reports itself. A resume takes these from the unit, so a
# change made on the hardware while the Host was closed stands.
RESUME_SKIP = ("set_mute", "set_hp_mute", "set_channel_link",
               "set_processing_channel")

# The shared effects return row bottoms out here; at its floor an established
# return is inaudible, which is worth saying rather than leaving the user with
# an armed effect and silence.
RETURN_EFFECTIVELY_OFF_DB = -59.9

# Compressor models on the Fat Channel page. The first three are the unit's;
# Multiband runs on the computer, right after the Fat Channel (io24_mbc).
COMP_MODELS = ("standard", "tube", "fet", "multiband")
MULTIBAND_MODEL = 3

# The preset menu routes through UC's Device Presets path
# (``Io24.save_known_device_library_preset``, MemP/PrsM 16..27). ``None``
# means the action is available.
PUT_ON_UNIT_UNAVAILABLE = None


# How wide a page's content may get. Adw.PreferencesPage clamps itself at
# 600sp, which left roughly a third of the window empty on either side at the
# sizes this Host opens at; the user asked (2026-09-12) that the window be
# used. Pages that build their own clamp use the same number, so every page
# widens together.
PAGE_WIDTH = 900
PAGE_TIGHTEN = 640
# The routing matrix is wider than any other page: one label plus three
# bus cells, each a fader, its value, a mute and a solo. Clamping it to
# PAGE_WIDTH squeezed the faders, so it gets its own ceiling.
ROUTING_WIDTH = 1500


def widen_page(root, maximum=PAGE_WIDTH, tighten=PAGE_TIGHTEN):
    """Widen every clamp inside a built page and return how many were found.

    Adw.PreferencesPage keeps its clamp as an internal child and exposes no
    width API, so the page is walked instead. Anything that takes both a
    maximum size and a tightening threshold is a clamp; nothing else in these
    pages does.
    """
    widened, stack = 0, [root]
    while stack:
        widget = stack.pop()
        if widget is None:
            continue
        if hasattr(widget, "set_maximum_size") and \
                hasattr(widget, "set_tightening_threshold"):
            widget.set_maximum_size(maximum)
            widget.set_tightening_threshold(tighten)
            widened += 1
        child = (widget.get_first_child()
                 if hasattr(widget, "get_first_child") else None)
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return widened


def wide_preferences_page():
    """An Adw.PreferencesPage that uses the window instead of a centre strip."""
    page = Adw.PreferencesPage()
    widen_page(page)
    return page


def wheel_scroll_value(value, lower, upper, page_size, delta, step=24.0):
    """Clamp one page-wheel movement without letting a child fader consume it."""
    end = max(float(lower), float(upper) - float(page_size))
    return max(float(lower), min(end, float(value) + float(delta) * float(step)))


def effects_wheel_step(page_increment):
    """A precise Effects-page wheel step for both short and tall windows."""
    increment = float(page_increment)
    if increment <= 0.0:
        return 24.0
    return max(18.0, min(32.0, increment * 0.04))


def first_descendant(root, widget_type):
    """Find the first descendant of ``widget_type`` in a GTK widget tree."""
    pending = [root]
    while pending:
        widget = pending.pop()
        if isinstance(widget, widget_type):
            return widget
        child = (widget.get_first_child()
                 if hasattr(widget, "get_first_child") else None)
        while child is not None:
            pending.append(child)
            child = child.get_next_sibling()
    return None


def keep_page_wheel_scrolling(page):
    """Make vertical wheel/trackpad input scroll the page, even over faders.

    Gtk.Scale handles wheel input itself.  On a long Effects page that meant a
    pointer resting over any parameter moved that parameter and left the page
    stranded at its old offset.  Capturing at the page is intentional here:
    drag and keyboard adjustment still belong to the fader, while vertical
    wheel input consistently belongs to the document under it.
    """
    controller = Gtk.EventControllerScroll(
        flags=Gtk.EventControllerScrollFlags.VERTICAL)
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def scroll(_controller, _dx, dy):
        if abs(float(dy)) < 1e-9:
            return False
        scroller = first_descendant(page, Gtk.ScrolledWindow)
        if scroller is None:
            return False
        adjustment = scroller.get_vadjustment()
        step = effects_wheel_step(adjustment.get_page_increment())
        adjustment.set_value(wheel_scroll_value(
            adjustment.get_value(), adjustment.get_lower(),
            adjustment.get_upper(), adjustment.get_page_size(), dy, step))
        # Claim at the bounds too; otherwise the fader underneath receives the
        # same wheel event exactly when the page reaches its top or bottom.
        return True

    controller.connect("scroll", scroll)
    page.add_controller(controller)
    return controller


def load_last_session(path=None):
    """The last session's Host-only state, or None when there is none."""
    try:
        with open(str(path) if path else LAST_SESSION_PATH) as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("version") == 1:
        return data
    return None


def db(x):
    return -99.0 if x is None or x <= 1e-12 else 20.0 * math.log10(x)


def factory_preset_matches(query, name, description=""):
    """Case-insensitive factory-sound filtering for the Presets page."""
    terms = str(query or "").casefold().split()
    haystack = "%s %s" % (name, description)
    haystack = haystack.casefold()
    return all(term in haystack for term in terms)


def preset_indicator_opacity(active, slot_index, now, animations=True):
    """Breathing intensity for the Mixer preset marker.

    User-facing Slot 1 (index 0) breathes slowly; Slot 2 (index 1) breathes
    twice as quickly. Reduced-motion mode keeps a steady lit marker, and bypass
    is an unlit ring rather than motion/color.
    """
    if not active:
        return 0.0
    if not animations:
        return 1.0
    period = 1.8 if slot_index == 0 else 0.9
    phase = (float(now) % period) / period * 2.0 * math.pi
    return 0.25 + 0.75 * (0.5 + 0.5 * math.sin(phase - math.pi / 2.0))


def preset_indicator_visual(active, slot_index, now, animations=True):
    """Return the dot-only preset cue, including its reduced-motion fallback."""
    if not active:
        return {"glyph": "○", "opacity": 0.28, "radius": 5.0}
    # With animation enabled, cadence alone distinguishes the slots as the user
    # requested. Reduced motion deliberately removes cadence, so a small radius
    # difference preserves slot identity without bringing S1/S2 text back.
    opacity = preset_indicator_opacity(
        True, slot_index, now, animations)
    radius = 5.0 if animations else (4.25 if slot_index == 0 else 5.75)
    return {
        "glyph": "●",
        "opacity": opacity,
        "radius": radius,
    }


def bus_meter_columns():
    """Mixer bus identity, compact visible label, and retained detail."""
    return (
        ("main", "Main", "Main output"),
        ("mixa", "Mix A", "USB capture 3–4"),
        ("mixb", "Mix B", "USB capture 5–6"),
    )


def effects_path_plan(win):
    """What opening the shared reverb path would take, or ``None``.

    The values are the user's existing channel-processing scalar and reverb
    return level. A bypassed channel keeps its bypass. ``None`` means the Host
    has not built those controls yet, and nothing is established. VoiceFX does
    not use this helper; UC 4.7.2 changes no mixer return for block 201.
    """
    controls = getattr(win, "processing_mix_controls", None)
    returns = getattr(win, "rev_return_controls", None)
    if not controls or not returns or "main" not in returns:
        return None
    control = controls.get(1)
    if control is None:
        return None
    bypassed = control.bypassed()
    return_db = returns["main"].get_value()
    return {
        "channel_mix": None if bypassed else control.get_value(),
        "return_db": return_db,
        "bypassed": bypassed,
        # The row bottoms out at -60 dB. Establishing a return at its own floor
        # is indistinguishable from not establishing it, so the Host says so
        # rather than presenting an armed effect nobody can hear.
        "return_off": bool(return_db <= RETURN_EFFECTIVELY_OFF_DB),
    }


def establish_effects_path(dev, channel_mix, return_db, bus="main"):
    """Open the shared effects feed and return before arming reverb.

    The transaction itself lives in ``Io24.establish_effects_return`` so the
    driver and Host cannot drift apart. The UC 4.7.2 capture proves VoiceFX
    edits do not perform this mixer-return transaction.
    """
    return dev.establish_effects_return(
        bus=bus, return_db=return_db, channel_mix=channel_mix)


def append_host_migration_notices(completion_message, migrations):
    """Disclose migration only as part of a completed Host-file load."""
    if completion_message is None:
        return None
    if migrations:
        completion_message += "; " + "; ".join(migrations)
    return completion_message


def shadow_ui_state(shadow):
    """Translate the Host's write-only call mirror into display state.

    This is deliberately not device readback.  It exists so loading a Host JSON
    preset cannot leave the controls showing the pre-load values while the DSP
    is running the loaded ones.
    """
    state = {
        "hpf_toggle": {}, "hpf_freq": {}, "eq": {1: {}, 2: {}},
        "gate": {}, "compressor": {}, "limiter": {}, "order": {},
        "reverb": None, "processing_mix": {}, "reverb_return": {},
        "voicefx": None, "voicefx_target": None,
        "preset_mode": None, "mute_mode": None,
        "preset_slot": {}, "output_delay": None,
        "output_delay_bus": None, "phones_source": None,
        "link": None, "balance": None, "component_names": {},
    }
    for call in (shadow or {}).values():
        if not isinstance(call, dict):
            continue
        name = call.get("fn")
        kw = call.get("kwargs") or {}
        try:
            ch = int(kw.get("channel", 0))
        except (TypeError, ValueError):
            ch = 0
        if name == "set_highpass" and ch in (1, 2):
            state["hpf_toggle"][ch] = bool(kw.get("on"))
        elif name == "set_highpass_freq" and ch in (1, 2):
            state["hpf_freq"][ch] = float(kw.get("freq_hz", 24.0))
        elif name == "set_eq_band" and ch in (1, 2):
            band = max(0, min(3, int(kw.get("band", 0))))
            state["eq"][ch][band] = {
                "shape": str(kw.get("shape", "off")),
                "freq": float(kw.get("freq_hz", 1000.0)),
                "gain": float(kw.get("gain_db", 0.0)),
                "q": float(kw.get("q", 0.7)),
            }
        elif name in ("set_gate", "gate_off") and ch in (1, 2):
            state["gate"][ch] = {"on": name == "set_gate" and
                                  bool(kw.get("on", True)), **kw}
        elif name in ("set_compressor", "compressor_off") and ch in (1, 2):
            state["compressor"][ch] = {
                "on": name == "set_compressor" and bool(kw.get("on", True)),
                **kw,
            }
        elif name == "set_limiter" and ch in (1, 2):
            state["limiter"][ch] = dict(kw)
        elif name == "set_comp_eq_order" and ch in (1, 2):
            state["order"][ch] = bool(kw.get("eq_first"))
        elif name == "set_reverb":
            state["reverb"] = dict(kw)
        elif name == "set_fx_mix" and ch in (1, 2):
            state["processing_mix"][ch] = float(kw.get("value", 0.0))
        elif name in ("set_send_db", "set_mix_db") and \
                kw.get("source") == "fxreturn/ch1":
            bus = str(kw.get("bus", "main")).lower()
            bus = io24.Io24.BUS_ALIASES.get(bus, bus)
            if bus in io24.Io24.MIXER_BUSES:
                state["reverb_return"][bus] = kw.get("gain_db")
        elif name == "set_fx":
            state["voicefx"] = dict(kw)
        elif name == "set_processing_channel" and ch == 1:
            source = int(kw.get("source_input", 1))
            if source in (1, 2):
                state["voicefx_target"] = source
        elif name == "set_preset_mode":
            state["preset_mode"] = int(kw.get("mode", 1))
        elif name == "set_mute_mode":
            state["mute_mode"] = bool(kw.get("mode", False))
        elif name == "set_preset_slot" and ch in (1, 2):
            state["preset_slot"][ch] = int(kw.get("slot", 0))
        elif name == "set_output_delay":
            state["output_delay"] = float(kw.get("seconds", 0.0))
            if kw.get("bus") is not None:
                state["output_delay_bus"] = str(kw["bus"])
        elif name == "output_delay_off":
            state["output_delay"] = 0.0
            state["output_delay_bus"] = "off"
        elif name == "set_output_delay_bus":
            state["output_delay_bus"] = str(kw.get("bus", "off"))
        elif name == "set_phones_source":
            source = kw.get("source")
            try:
                if isinstance(source, str):
                    key = source.strip().lower()
                    canonical = io24.Io24.PHONE_SOURCE_ALIASES.get(key, key)
                    state["phones_source"] = io24.Io24.PHONE_SOURCES[canonical]
                else:
                    value = int(source)
                    if value in (0, 1, 2):
                        state["phones_source"] = value
            except (KeyError, TypeError, ValueError):
                pass
        elif name == "set_channel_link":
            state["link"] = bool(kw.get("on"))
        elif name == "set_component_name":
            component = kw.get("component")
            if component in io24.Io24.COMPONENT_PATHS:
                state["component_names"][component] = str(
                    kw.get("name", ""))
        elif name == "set_pan" and kw.get("source") in \
                ("line/ch1", "line/ch2") and kw.get("bus") == "main":
            state["balance"] = kw.get("pan")
    return state


def voicefx_target_from_processing(processing):
    """Return the physical input that owns Voice FX in a live permutation."""
    if not isinstance(processing, (list, tuple)) or len(processing) != 2:
        return None
    values = tuple(processing)
    if values == (0, 1):
        return 1
    if values == (1, 0):
        return 2
    return None


# --------------------------------------------------------------------------
# device worker: one thread, one read per frame, everything else is a snapshot
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Sample rate and buffer size.
#
# These are NOT device parameters. There is no clock, rate, buffer or latency
# descriptor among the 117 the host exposes, and no rate vocabulary in the
# device firmware — the io24 is class-compliant, so it advertises its rates
# through standard USB Audio Class descriptors and the AUDIO SERVER picks one.
# On Windows the ASIO driver owns this, which is why Universal Control can
# present it as a device setting; on Linux PipeWire owns it. So this page reads
# the truth from ALSA and drives PipeWire, rather than pretending to write to
# the device.
# --------------------------------------------------------------------------
CARD = "R24"
DEFAULT_SAMPLE_RATE = 96000
DEFAULT_QUANTUM = 512
SUPPORTED_SAMPLE_RATES = (44100, 48000, 88200, 96000)
SUPPORTED_QUANTA = (32, 64, 128, 256, 512, 1024, 2048)


def alsa_rates(card=CARD):
    """Rates the DEVICE advertises, straight from its USB descriptors."""
    try:
        for n in os.listdir("/proc/asound"):
            if n == card or (n.startswith("card") and
                             os.path.exists("/proc/asound/%s/id" % n) and
                             open("/proc/asound/%s/id" % n).read().strip() == card):
                for line in open("/proc/asound/%s/stream0" % n):
                    if "Rates:" in line:
                        return [int(x) for x in
                                line.split("Rates:")[1].replace(" ", "").split(",")
                                if x.strip().isdigit()]
    except Exception:
        pass
    return list(SUPPORTED_SAMPLE_RATES)


def alsa_live(card=CARD):
    """The rate/period actually in use, if a stream is open."""
    out = {}
    try:
        base = None
        for n in os.listdir("/proc/asound"):
            if os.path.exists("/proc/asound/%s/id" % n) and \
               open("/proc/asound/%s/id" % n).read().strip() == card:
                base = "/proc/asound/%s" % n
                break
        if not base:
            return out
        for sub in ("pcm0c", "pcm0p"):
            f = "%s/%s/sub0/hw_params" % (base, sub)
            if not os.path.exists(f):
                continue
            txt = open(f).read()
            if "closed" in txt:
                continue
            d = {}
            for line in txt.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    d[k.strip()] = v.strip()
            out["capture" if sub.endswith("c") else "playback"] = d
    except Exception:
        pass
    return out


def pw_settings():
    """Current PipeWire clock settings."""
    import subprocess
    out = {}
    try:
        r = subprocess.run(["pw-metadata", "-n", "settings"], capture_output=True,
                           text=True, timeout=4).stdout
        for line in r.splitlines():
            if "key:'" in line and "value:'" in line:
                k = line.split("key:'")[1].split("'")[0]
                v = line.split("value:'")[1].split("'")[0]
                out[k] = v
    except Exception:
        pass
    return out


def pw_set(key, value):
    """Ask PipeWire to force a rate or quantum. Returns (ok, message)."""
    import subprocess
    try:
        r = subprocess.run(["pw-metadata", "-n", "settings", "0", key, str(value)],
                           capture_output=True, text=True, timeout=4)
        return (r.returncode == 0, (r.stderr or r.stdout).strip()[:120])
    except FileNotFoundError:
        return (False, "pw-metadata not installed")
    except Exception as e:
        return (False, str(e)[:120])


def audio_clock_preference(session):
    """Return the saved Host clock, or the first-launch defaults.

    This is the user's requested base clock. Multiband may temporarily lower
    the live quantum to 128, but that borrowed value is not a new selection.
    """
    selected = (session or {}).get("audio_clock")
    if not isinstance(selected, dict) or selected.get("version") != 1:
        selected = {}
    try:
        rate = int(selected.get("sample_rate", DEFAULT_SAMPLE_RATE))
    except (TypeError, ValueError):
        rate = DEFAULT_SAMPLE_RATE
    try:
        quantum = int(selected.get("quantum", DEFAULT_QUANTUM))
    except (TypeError, ValueError):
        quantum = DEFAULT_QUANTUM
    if rate not in SUPPORTED_SAMPLE_RATES:
        rate = DEFAULT_SAMPLE_RATE
    if quantum not in SUPPORTED_QUANTA:
        quantum = DEFAULT_QUANTUM
    return {"version": 1, "sample_rate": rate, "quantum": quantum}


class Ctl:
    def __init__(self, dev):
        self.dev = dev
        cached = self._cached_shadow_count(dev)
        self.snap = {
            "in": [-99.0, -99.0], "main": [-99.0, -99.0],
            "mixa": [-99.0, -99.0], "mixb": [-99.0, -99.0],
            "gain": [0.0, 0.0], "hp": 0.0, "mainvol": 0.0, "blend": 0.0,
            "phantom": [False, False], "gr": {1: {"gate": 0.0, "comp": 0.0, "lim": 0.0},
                   2: {"gate": 0.0, "comp": 0.0, "lim": 0.0}},
            "sel": [0, 2], "chsel": [True, False],
            "preset_off": [False, False], "preset_slot": [0, 0],
            "mute": [False, False], "hpmute": False, "link": False,
            "mainmute": False,
            "processing_channel": [None, None],
            # DSP/mixer blocks cannot be read back.  A newly opened handle may
            # follow a cold boot, so a disk cache is *pending* until the window
            # resumes the last session, which re-sends it on every connection
            # (2026-09-11, at the user's request).
            "shadow_pending": bool(cached), "shadow_calls": cached,
            "attach_generation": 1 if dev is not None else 0,
            "alive": False, "error": None,
        }
        self.q = queue.Queue()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, fn):
        self.q.put(fn)

    @staticmethod
    def _cached_shadow_count(dev):
        backend = getattr(dev, "dev", None)
        count = getattr(backend, "replayable_shadow_count", None)
        if callable(count):
            return count()
        raw = getattr(backend, "_shadow", {})
        return len(raw) if isinstance(raw, dict) else 0

    def _mark_attached(self):
        cached = self._cached_shadow_count(self.dev)
        self.snap["shadow_calls"] = cached
        self.snap["shadow_pending"] = bool(cached)
        self.snap["attach_generation"] = \
            int(self.snap.get("attach_generation", 0)) + 1

    def _loop(self):
        import struct
        n = 0
        self._errs = 0
        self._next_probe = 0.0
        while self.running:
            if self.dev is None:
                # OFFLINE: the app runs without the interface now — the UI
                # itself never needed it. Queued control
                # writes are dropped (there is no hardware to take them), and
                # every couple of seconds we try to attach, so plugging the
                # device in brings everything live without a restart.
                try:
                    while True:
                        self.q.get_nowait()
                except queue.Empty:
                    pass
                self.snap["alive"] = False
                now = time.monotonic()
                if now >= self._next_probe:
                    self._next_probe = now + 2.0
                    try:
                        self.dev = Device(poll_interval=0.5)
                        self._mark_attached()
                        self._errs = 0
                        self.snap["error"] = None
                    except SystemExit:
                        self.dev = None          # genuinely absent
                    except Exception as e:
                        # present but unopenable is a DIFFERENT state than
                        # absent — "Resource busy" here meant a forgotten
                        # instance held the device for six hours while this
                        # loop reported nothing at all. Keep probing (the
                        # holder may exit), but say what is happening.
                        self.dev = None
                        if "busy" in str(e).lower():
                            self.snap["error"] = ("device is held by another "
                                                  "program (Resource busy)")
                time.sleep(0.2)
                continue
            try:
                while True:
                    fn = self.q.get_nowait()
                    try:
                        with self.dev.lock:
                            fn(self.dev.dev)
                    except Exception as e:
                        self.snap["error"] = "%s: %s" % (type(e).__name__, e)
            except queue.Empty:
                pass
            try:
                with self.dev.lock:
                    rsp = self.dev.dev.read_state(0x7EC)
                    f = self.dev.dev.floats(rsp) if rsp else None
                    if n % 6 == 0 and f:
                        gr = {}
                        for ch in (1, 2):
                            g = {}
                            for blk in ("gate", "comp", "lim "):
                                r = self.dev.dev.read_reduction(blk, ch)
                                g[blk.strip()] = db(r[0]) if r else 0.0
                            gr[ch] = g
                        self.snap["gr"] = gr
                if f:
                    s = self.snap
                    s["in"] = [db(f[M_IN1]), db(f[M_IN2])]
                    s["main"] = [db(f[i]) for i in M_MAIN]
                    s["mixa"] = [db(f[i]) for i in M_MIXA]
                    s["mixb"] = [db(f[i]) for i in M_MIXB]
                    s["gain"] = [f[P_G1], f[P_G2]]
                    s["hp"], s["mainvol"], s["blend"] = f[P_HP], f[P_MAIN], f[P_BLEND]
                    raw = struct.pack("<f", f[50])
                    s["phantom"] = [bool(raw[0]), bool(raw[1])]
                    # slot 42 is a bitfield. Every assignment below was found by
                    # driving the control and diffing the whole state blob:
                    #   bit 1  MAIN OUT mute — the physical front-panel button.
                    #          READ ONLY: no 'Pari' id 5..19 moves it, so the app
                    #          can mirror the button but cannot press it.
                    #   bit 2  the software mute on 'Pari' id 6 (a DIFFERENT
                    #          control from the front-panel button)
                    #   bit 3  ch1 mute      bit 4  ch2 mute      ('Pari' id 7)
                    #   bit 12 stereo link   ('Pari' id 9)
                    #   bit 13 'Pari' id 15  (PROTOCOL.md calls id 15 a no-op;
                    #          it is not — it sets this bit. Purpose unknown.)
                    # The low cut is NOT here: nothing stable reflects it, so it
                    # cannot be mirrored at all.
                    fl = struct.unpack("<i", struct.pack("<f", f[42]))[0]
                    s["mainmute"] = bool(fl >> 1 & 1)
                    s["hpmute"] = bool(fl >> 2 & 1)
                    s["mute"] = [bool(fl >> 3 & 1), bool(fl >> 4 & 1)]
                    s["link"] = bool(fl >> 12 & 1)
                    # slots 40/41 move when a front-panel button is pressed
                    # (re/state_map.md). Surfaced so the UI can follow whichever
                    # channel the hardware has selected.
                    s["sel"] = [struct.unpack("<i", struct.pack("<f", f[40]))[0],
                                struct.unpack("<i", struct.pack("<f", f[41]))[0]]
                    # bits 5 and 6 are the front-panel CHANNEL SELECT buttons,
                    # found by watching the blob while they were pressed. They
                    # are independent — the device happily selects both at once,
                    # which is why the UI highlights a set rather than one.
                    # bits 5/6 latch when press-and-hold DISABLES that
                    # channel's preset function. A short preset press only
                    # blips them, so treat the steady state as the meaning.
                    s["preset_off"] = [bool(fl >> 5 & 1), bool(fl >> 6 & 1)]
                    s["preset_slot"] = [
                        struct.unpack("<i", struct.pack("<f", f[40]))[0],
                        struct.unpack("<i", struct.pack("<f", f[41]))[0]]
                    s["processing_channel"] = [
                        struct.unpack("<i", struct.pack("<f", f[38]))[0] - 3,
                        struct.unpack("<i", struct.pack("<f", f[39]))[0] - 3]
                    s["alive"] = True
                    s["error"] = None
                    self._errs = 0        # a good poll ends any error streak
                else:
                    # A dead poll usually RETURNS None rather than raising —
                    # the driver's read path swallows USB errors — so the
                    # unplug detector must count this branch too. It counted
                    # only exceptions at first, and a real cable-pull test
                    # showed the app clutching its stale handle forever.
                    s = self.snap
                    s["alive"] = False
                    self._errs += 1
                    if self._errs >= 5:
                        try:
                            self.dev.close()
                        except Exception:
                            pass
                        self.dev = None
                        self._errs = 0
                        continue
            except Exception as e:
                self.snap["alive"] = False
                self._errs += 1
                if self._errs >= 5:
                    # five consecutive failures is an unplug, not a hiccup:
                    # release the stale handle and let the offline machinery
                    # re-attach when the device comes back
                    try:
                        self.dev.close()
                    except Exception:
                        pass
                    self.dev = None
                    self._errs = 0
                    continue
                self.snap["error"] = "%s: %s" % (type(e).__name__, e)
            n += 1
            time.sleep(0.020)

    def close(self):
        self.running = False
        try:
            self.dev.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# drawn widgets
#
# These use GTK4's own snapshot API (Gsk paths + Graphene rects + Pango
# layouts) rather than Cairo. PyGObject can only pass a cairo.Context to Python
# when the separate `python3-gi-cairo` bridge is installed, and it usually is
# not — the failure is silent per-frame ("Couldn't find foreign struct
# converter"), which shows up as a window with no graphics in it at all. The
# snapshot API is built into GTK4, so this draws everywhere.
# --------------------------------------------------------------------------
# -- palette ---------------------------------------------------------------
# The canvases used to derive every colour from the theme foreground via
# tint(), which made them monochrome and flat. They now carry their own
# palette so the instrument reads as an instrument: a near-black blue ground,
# a cyan trace, and violet for the second channel so the two are separable at
# a glance rather than by position alone.
#
# Meter colours stay conventional — green / amber / red is what every desk in
# the world uses and is not the place to be inventive — but they are pushed
# toward neon so they sit with the rest.
GREEN = (0.16, 0.85, 0.62)
AMBER = (0.98, 0.72, 0.20)
RED = (0.98, 0.29, 0.36)

ACCENT = (0.24, 0.84, 0.87)          # cyan — channel 1, curves, active state
ACCENT2 = (0.66, 0.55, 0.98)         # violet — channel 2
GRID = (0.35, 0.62, 0.78)            # graticule, used at low alpha

# The instrument palette for the field displays: a phosphor-cyan → thermal-amber
# duotone over a deep field, with gold reserved for nodal lines. The point of a
# duotone is that VALUE maps to HUE — a surface reads its own sign without a
# legend, and the one third colour is spent on the one thing worth singling out:
# where the field cancels.
PHOS = (0.349, 0.839, 0.788)         # #59D6C9  phosphor cyan — positive
THERM = (0.961, 0.659, 0.235)        # #F5A83C  thermal amber — negative
IVORY = (0.929, 0.910, 0.863)        # #EDE8DC
GOLD = (1.0, 0.843, 0.0)             # #FFD700  nodal contours only
FIELD = (0.027, 0.043, 0.078)        # #070B14  the deep field


def duo(mix, a=1.0):
    """Phosphor→thermal across 0..1. The workhorse: every field value is a hue."""
    m = 0.0 if mix < 0.0 else 1.0 if mix > 1.0 else mix
    return rgba(PHOS[0] + (THERM[0] - PHOS[0]) * m,
                PHOS[1] + (THERM[1] - PHOS[1]) * m,
                PHOS[2] + (THERM[2] - PHOS[2]) * m, a)
INK = (0.79, 0.85, 0.93)             # primary text on canvas
GROUND = (0.031, 0.043, 0.071)       # canvas ground, near-black with a blue cast


def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def pal(col, a=1.0):
    """A palette tuple as a Gdk.RGBA. Companion to tint() for theme colours.

    Named `pal` rather than `c` on purpose: Meter.paint already binds `c` as a
    loop variable for zone colours, and a one-letter global would shadow into
    exactly the kind of bug this file has had enough of."""
    return rgba(col[0], col[1], col[2], a)


def chan_col(channel):
    """Channel 1 reads cyan, channel 2 violet, everywhere in the app."""
    return ACCENT if channel == 1 else ACCENT2


def tint(col, a):
    return rgba(col.red, col.green, col.blue, a)


def glow(sn, draw, radius=7.0, passes=1):
    """Run `draw` blurred underneath itself, then crisp on top.

    GTK 4.10+ has a real blur node, so this is an actual optical glow rather
    than the usual trick of stroking a fat translucent line under a thin one —
    which reads as a fat line, not as light. `draw` is called once per pass, so
    it must be cheap and side-effect free.
    """
    for _ in range(max(1, passes)):
        sn.push_blur(radius)
        draw()
        sn.pop()
    draw()


def box(sn, x, y, w, h, color):
    if w > 0 and h > 0:
        sn.append_color(color, Graphene.Rect().init(x, y, w, h))


def polyline(sn, pts, color, width=2.0):
    if len(pts) < 2:
        return
    pb = Gsk.PathBuilder()
    pb.move_to(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        pb.line_to(x, y)
    sn.append_stroke(pb.to_path(), Gsk.Stroke.new(width), color)


def label(widget, sn, text, x, y, color, size=9):
    lay = widget.create_pango_layout(text)
    fd = Pango.FontDescription()
    fd.set_size(int(size * Pango.SCALE))
    lay.set_font_description(fd)
    sn.save()
    sn.translate(Graphene.Point().init(x, y))
    sn.append_layout(lay, color)
    sn.restore()


def vgrad(sn, x, y, w, h, stops):
    """Vertical gradient. stops = [(offset 0..1, (r,g,b,a)), ...]"""
    if w <= 0 or h <= 0:
        return
    cs = []
    for off, c in stops:
        st = Gsk.ColorStop()
        st.offset = off
        st.color = rgba(*c)
        cs.append(st)
    sn.append_linear_gradient(Graphene.Rect().init(x, y, w, h),
                              Graphene.Point().init(x, y),
                              Graphene.Point().init(x, y + h), cs)


def fill_poly(sn, pts, color):
    if len(pts) < 3:
        return
    pb = Gsk.PathBuilder()
    pb.move_to(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        pb.line_to(x, y)
    pb.close()
    sn.append_fill(pb.to_path(), Gsk.FillRule.WINDING, color)


def dot(sn, x, y, r, color):
    pb = Gsk.PathBuilder()
    pb.add_circle(Graphene.Point().init(x, y), r)
    sn.append_fill(pb.to_path(), Gsk.FillRule.WINDING, color)


class Spectrum:
    """Real-time spectrum from the device's own capture stream.

    The control interface tells us levels but not spectrum, so this taps the
    class-compliant audio side instead: `arecord` on the same card, FFT in
    numpy. The two interfaces are independent, so this runs happily while the
    control interface is held. If arecord or numpy is missing it simply stays
    silent and the EQ page loses its backdrop, nothing else.
    """

    BINS = 256
    FFT = 8192
    HOP = 1024
    RELEASE_TAU_S = 0.16

    # If a capture generation produces no data for this long while it is
    # supposed to be running, treat it as wedged rather than waiting forever.
    STALL_S = 6.0

    # The io24 captures SIX channels (FL FR FC LFE RL RR in ALSA's naming): the
    # two inputs first, then Mix A on 3-4 and Mix B on 5-6 (PROTOCOL.md §12F).
    # Ask for the device's native width and slice the pair we want explicitly.
    #
    # Asking `plughw` for 2 channels instead, as this did, leaves it to ALSA's
    # plug layer to reduce 6 to 2 — and that is a channel *conversion*, which may
    # downmix rather than truncate. A downmix would fold Mix A and Mix B into the
    # backdrop, so the EQ curve would be drawn over the whole monitor mix rather
    # than over the channel being edited: entirely plausible on screen, and wrong.
    # Slicing named columns is deterministic and costs nothing.
    CAPTURE_CHANNELS = 6
    ANALYSE = (0, 1)                     # capture channels 1 and 2 = the inputs

    def __init__(self, card="R24", channels=None, rate=48000, autostart=True):
        self.rate = rate
        self.channels = self.CAPTURE_CHANNELS if channels is None else channels
        self.card = card
        self.mag = [None, None]          # dB per log-spaced bin, per channel
        self.ok = False
        self.error = None
        self.running = False
        self.proc = None
        self.gen = 0                     # bumped on every start/stop
        self.last_data = 0.0
        self._lock = threading.Lock()
        self.freqs = [20.0 * (24000 / 20.0) ** (i / (self.BINS - 1))
                      for i in range(self.BINS)]
        if autostart:
            self.start()

    def frequency_resolution_hz(self):
        """Native FFT-bin width at the configured sample rate."""
        return float(self.rate) / self.FFT

    def update_interval_seconds(self):
        """Analysis update cadence; independent of the longer FFT window."""
        return float(self.HOP) / self.rate

    # -- lifecycle ---------------------------------------------------------
    # The capture thread spends nearly all its time blocked in
    # proc.stdout.read(), so a flag alone cannot stop it -- it would not be
    # noticed until the next buffer arrived, and if arecord wedges, never. So
    # stopping means KILLING THE PIPE, which makes the blocked read return b""
    # immediately and the thread fall out of its loop on its own.
    #
    # Nothing here joins the thread. This is called from the GTK main thread on
    # page switches, and a join is exactly how a UI locks up when the thing it
    # waits for is stuck. The generation counter makes an outgoing thread
    # harmless instead: it can finish whenever it likes, and its results are
    # discarded because its generation is stale.

    def start(self):
        with self._lock:
            if self.running:
                return
            self.running = True
            self.gen += 1
            gen = self.gen
            self.last_data = time.time()
        threading.Thread(target=self._run, args=(gen,), daemon=True).start()

    def pause(self):
        """Stop capturing and release the card. Safe from the UI thread."""
        with self._lock:
            if not self.running:
                return
            self.running = False
            self.gen += 1
            proc, self.proc = self.proc, None
            # Cleared INSIDE the lock, with the generation bump, so a worker
            # cannot slip a frame in between the bump and the clear.
            self.mag = [None, None]
            self.ok = False
        if proc is not None:
            # terminate() is what unblocks the reader. kill() is the backstop if
            # arecord ignores SIGTERM; both are non-blocking, and the reaping
            # happens on a throwaway thread so the UI never waits on either.
            def reap():
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            threading.Thread(target=reap, daemon=True).start()

    def stalled(self):
        """True if a running capture has gone quiet for too long."""
        return (self.running and self.last_data
                and (time.time() - self.last_data) > self.STALL_S)

    @staticmethod
    def _take_window(buf, need, hop):
        """Return one overlapping analysis window and its retained tail."""
        if len(buf) < need:
            return None, buf
        return buf[:need], buf[hop:]

    @classmethod
    def _release_alpha(cls, elapsed):
        return math.exp(-max(0.0, float(elapsed)) / cls.RELEASE_TAU_S)

    def _run(self, gen):
        try:
            import numpy as np
            import subprocess
        except Exception as e:
            self.error = str(e)
            return
        cmd = ["arecord", "-D", "plughw:CARD=%s" % self.card, "-f", "S32_LE",
               "-c", str(self.channels), "-r", str(self.rate), "-t", "raw",
               "--period-size=1024", "-q"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        except Exception as e:
            self.error = "arecord: %s" % e
            return
        with self._lock:
            if gen != self.gen:              # paused while we were starting up
                try:
                    proc.terminate()
                except Exception:
                    pass
                return
            self.proc = proc
        win = np.hanning(self.FFT).astype(np.float32)
        fft_f = np.fft.rfftfreq(self.FFT, 1.0 / self.rate)
        # Each display bin must cover its whole frequency SPAN. A fixed +-k bin
        # window is wrong at both ends: down low several display bins share one
        # FFT bin, and up high the display bins are hundreds of Hz apart, so a
        # narrow window steps straight over the peaks (a 10 kHz tone read 74 dB
        # low before this was fixed).
        ratio = (24000 / 20.0) ** (1.0 / (self.BINS - 1))
        spans = []
        for f in self.freqs:
            lo = int(np.searchsorted(fft_f, f / math.sqrt(ratio)))
            hi = int(np.searchsorted(fft_f, f * math.sqrt(ratio)))
            lo = max(1, lo)
            spans.append((lo, max(lo + 1, hi)))
        frame_bytes = self.channels * 4
        need = self.FFT * frame_bytes
        hop_bytes = self.HOP * frame_bytes
        buf = b""
        smooth = [None, None]
        last_frame_at = None
        try:
            while self.running and gen == self.gen:
                chunk = proc.stdout.read(hop_bytes)
                if not chunk:
                    break
                buf += chunk
                self.last_data = time.time()
                while len(buf) >= need:
                    frame, buf = self._take_window(buf, need, hop_bytes)
                    a = np.frombuffer(frame, dtype="<i4").astype(
                        np.float32) / 2147483648.0
                    a = a.reshape(-1, self.channels)
                    frame_at = time.monotonic()
                    elapsed = ((self.HOP / self.rate) if last_frame_at is None
                               else frame_at - last_frame_at)
                    last_frame_at = frame_at
                    alpha = self._release_alpha(elapsed)
                    for out, src_col in enumerate(self.ANALYSE):
                        if src_col >= self.channels:
                            continue
                        sp = np.abs(np.fft.rfft(
                            a[:, src_col] * win)) / (self.FFT / 4)
                        d = 20.0 * np.log10(np.maximum(sp, 1e-9))
                        binned = np.array(
                            [d[lo:hi].max() for lo, hi in spans])
                        if smooth[out] is None:
                            smooth[out] = binned
                        else:
                            rise = binned > smooth[out]
                            smooth[out] = np.where(
                                rise, binned,
                                smooth[out] * alpha + binned * (1.0 - alpha))
                        with self._lock:
                            if gen == self.gen:
                                self.mag[out] = smooth[out].tolist()
                    with self._lock:
                        if gen == self.gen:
                            self.ok = True
        except Exception as e:
            if gen == self.gen:
                self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            with self._lock:
                if self.proc is proc:
                    self.proc = None

    def stop(self):
        self.pause()


class Canvas(Gtk.Widget):
    """A widget that paints itself with the snapshot API."""

    def __init__(self, width=-1, height=-1):
        super().__init__()
        self.set_size_request(width, height)

    def paint(self, sn, w, h):
        raise NotImplementedError

    def do_snapshot(self, sn):
        w, h = self.get_width(), self.get_height()
        if w > 0 and h > 0:
            self.paint(sn, w, h)


class Meter(Canvas):
    """Flat console-style meter: discrete zone colours, fine LED ruling, a
    held peak line and dB ticks. No gradient — real desk meters do not have
    one, and a gradient reads as decoration rather than information."""

    HOLD, DECAY = 1.6, 26.0
    TICKS = (0, -6, -12, -20, -30, -45)

    def __init__(self, width=22, scale=True):
        super().__init__(width, 120)
        self.value = -99.0
        self.peak = -99.0
        self.peak_at = 0.0
        self.show_scale = scale
        self.set_vexpand(True)

    def set_db(self, v):
        self.value = v
        now = time.monotonic()
        if v >= self.peak:
            self.peak, self.peak_at = v, now
        elif now - self.peak_at > self.HOLD:
            self.peak = max(v, self.peak - self.DECAY * (now - self.peak_at - self.HOLD))
            self.peak_at = now - self.HOLD
        self.queue_draw()

    def _y(self, d, h):
        return h - (max(FLOOR, min(0.0, d)) - FLOOR) / (-FLOOR) * h

    def _y_inv(self, y, h):
        """Pixel row back to dB — which zone a segment belongs to."""
        return FLOOR + (h - y) / max(h, 1.0) * (-FLOOR)

    def paint(self, sn, w, h):
        bw = max(7, w - (18 if self.show_scale else 0))
        box(sn, 0, 0, bw, h, pal(GROUND))
        box(sn, 0, 0, bw, h, pal(GRID, 0.06))
        if self.value > FLOOR:
            # three flat zones, each clipped to how far the level has risen.
            # Still flat, still discrete: a gradient here would read as
            # decoration and make the zone boundaries impossible to judge.
            top = self._y(self.value, h)
            hot = self.value > -9.0
            # Discrete LED segments rather than a continuous bar. Real ladder
            # meters are segmented, it reads as an instrument, and — the part
            # that actually matters — a segment boundary is a fixed dB landmark,
            # so you can judge level by counting rather than by estimating a
            # length. Drawn as segments directly instead of painting a bar and
            # punching gaps out of it, which left the gaps a different colour
            # from the ground wherever the zones met.
            # A smooth column with a real vertical gradient, clipped to the
            # current level. The segmented version read as blocky and, worse,
            # quantised the level to 7 px steps — the whole point of a meter is
            # fine movement. Gradient stops sit exactly on the zone boundaries,
            # so green/amber/red still land at the same dB as before.
            def stop(db, col):
                # Gsk.ColorStop is a plain struct with no .new() constructor —
                # calling one raised inside the draw callback, which GTK
                # swallows, so the meter silently drew nothing at all.
                st = Gsk.ColorStop()
                st.offset = (h - self._y(db, h)) / max(h, 1.0)
                st.color = pal(col)
                return st
            bar = lambda: sn.append_linear_gradient(
                Graphene.Rect().init(0, top, bw, h - top),
                Graphene.Point().init(0, h), Graphene.Point().init(0, 0),
                [stop(FLOOR, GREEN), stop(-12.0, GREEN),
                 stop(-9.0, AMBER), stop(-3.0, AMBER),
                 stop(-1.5, RED), stop(0.0, RED)])
            # only the loud zones glow, so the glow carries information rather
            # than being ambient decoration
            glow(sn, bar, radius=5.0) if hot else bar()
            # a bright cap at the top of the column gives the level an edge to
            # track, which a plain gradient loses at low levels
            box(sn, 0, max(0, top - 1), bw, 2,
                pal(RED if self.value > -1.5 else
                    AMBER if self.value > -9 else GREEN, 0.95))
        if self.peak > FLOOR:
            py = self._y(self.peak, h)
            zone = RED if self.peak > -1.5 else AMBER if self.peak > -9 else GREEN
            glow(sn, lambda: box(sn, 0, max(0, py - 1), bw, 2, pal(zone)), radius=4.0)
        if self.show_scale:
            for d in self.TICKS:
                yy = self._y(d, h)
                box(sn, bw, yy, 3, 1, pal(GRID, 0.35))
                label(self, sn, str(d), bw + 5,
                      min(h - 11, max(0, yy - 5)), pal(INK, 0.42), 7)


class PresetIndicator(Canvas):
    """Compact slot identity plus an active/bypassed processing marker."""

    def __init__(self):
        super().__init__(28, 20)
        self.slot = None
        self.active = False

    def set_state(self, slot, active):
        slot = slot if slot in (0, 1) else None
        active = bool(active and slot is not None)
        if (slot, active) != (self.slot, self.active):
            self.slot, self.active = slot, active
        self.queue_draw()

    @staticmethod
    def _animations_enabled():
        settings = Gtk.Settings.get_default()
        return bool(settings is None or
                    settings.get_property("gtk-enable-animations"))

    def paint(self, sn, w, h):
        visual = preset_indicator_visual(
            self.active, self.slot, time.monotonic(),
            self._animations_enabled())
        size = int(round(visual["radius"] * 2.6))
        x = max(0, (w - size) / 2.0)
        if visual["glyph"] == "○":
            label(self, sn, visual["glyph"], x, 0,
                  pal(INK, visual["opacity"]), size)
            return
        draw = lambda: label(
            self, sn, visual["glyph"], x, 0,
            pal(ACCENT, visual["opacity"]), size)
        glow(sn, draw, radius=4.0)


class GRBar(Canvas):
    """Gain reduction, filling right-to-left as every compressor display does."""

    def __init__(self, span=24.0):
        super().__init__(-1, 11)
        self.value = 0.0
        self.span = span
        self.set_hexpand(True)

    def set_db(self, v):
        self.value = v
        self.queue_draw()

    def paint(self, sn, w, h):
        box(sn, 0, 0, w, h, pal(GROUND))
        box(sn, 0, 0, w, h, pal(GRID, 0.07))
        amt = min(1.0, max(0.0, -self.value / self.span))
        if amt > 0.002:
            x = w * (1 - amt)
            glow(sn, lambda: box(sn, x, 0, w * amt, h, pal(AMBER, 0.95)), radius=4.0)
            # a bright leading edge gives the bar a direction to read
            box(sn, x, 0, 1.5, h, pal(AMBER))


class _SwitchValue:
    """Adapts a SwitchRow to the .get_value()/.set_value() a fader offers, so
    the parameter plumbing does not have to care which widget it got."""

    def __init__(self, row):
        self.row = row

    def get_value(self):
        return 1.0 if self.row.get_active() else 0.0

    def set_value(self, v):
        self.row.set_active(bool(v))


class _ComboValue:
    """Same idea for a ComboRow over an evenly-spaced numeric range."""

    def __init__(self, row, lo, step):
        self.row, self.lo, self.step = row, lo, step

    def get_value(self):
        return self.lo + self.row.get_selected() * self.step

    def set_value(self, v):
        self.row.set_selected(int(round((v - self.lo) / self.step)))


class _SkewValue:
    """Expose an XML ``curve=skew`` fader in its real parameter units.

    GTK's scale is linear, so it carries a normalized 0..1 position.  The
    exponent is solved from UC's declared midpoint: position 0.5 maps exactly
    to ``mid`` while both endpoints remain exact.
    """

    def __init__(self, scale, lo, hi, mid):
        self.scale = scale
        self.lo = float(lo)
        self.hi = float(hi)
        fraction = (float(mid) - self.lo) / (self.hi - self.lo)
        if not 0.0 < fraction < 1.0:
            raise ValueError("skew midpoint must lie inside the parameter range")
        self.exponent = math.log(fraction) / math.log(0.5)

    def value_from_position(self, position):
        position = max(0.0, min(1.0, float(position)))
        return self.lo + (self.hi - self.lo) * position ** self.exponent

    def position_from_value(self, value):
        fraction = ((max(self.lo, min(self.hi, float(value))) - self.lo) /
                    (self.hi - self.lo))
        return fraction ** (1.0 / self.exponent)

    def get_value(self):
        return self.value_from_position(self.scale.get_value())

    def set_value(self, value):
        self.scale.set_value(self.position_from_value(value))


class _SelectedFxPower:
    """Compatibility view of the selected model's own XML ``On`` control.

    Universal Control does not define a container-level Voice FX enable. Each
    mutable model component owns a storable ``on`` parameter instead. Keeping
    this adapter lets preset/session plumbing address the selected model
    without collapsing the six controls back into one shared switch.
    """

    def __init__(self, selector, controls, order):
        self.selector = selector
        self.controls = controls
        self.order = order

    def _selected(self):
        index = max(0, min(len(self.order) - 1,
                           self.selector.get_selected()))
        return self.controls[self.order[index]]

    def get_active(self):
        return bool(self._selected().get_value())

    def set_active(self, value):
        self._selected().set_value(bool(value))


# --------------------------------------------------------------------------
# A very small 3D wireframe renderer.
#
# GTK4's drawing API is 2D — there is no mesh node and no shader stage — so the
# geometry is transformed here and emitted as line segments. That is a real
# projection, not a fake isometric look: vertices are rotated, divided through
# by depth, and drawn back-to-front with depth cueing, so parallax and
# foreshortening behave correctly when a parameter changes the shape.
#
# Wireframe surfaces, not polyhedra. An earlier pass drew boxes and lattices —
# a cube tells you nothing about what an effect does, and at this size a shaded
# blob tells you less. What is drawn now is the response itself: a curved
# surface over frequency and time, interpolated with Catmull-Rom in both axes,
# so the shape IS the information and every parameter deforms it.
# --------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The displays are 2D.
#
# An earlier pass rendered these as projected 3D surfaces. It was dropped, and
# the reason is worth keeping: the third axis was carrying PARAMETER HISTORY —
# how the response had changed while you moved a slider. That is not an axis the
# effect has. It looked like data without being data, and it made every model
# read as the same generic landscape when the whole point is that a detuner and
# a comb filter do visibly different things.
#
# What replaced it is what each effect actually IS, drawn flat and lit: the
# detuner's two voices as two traces, the comb's notches as notches. If a real
# 3D instrument is wanted later — a spectrogram of live audio, where time is a
# genuine axis — that is a different program with a different job.
# ---------------------------------------------------------------------------


def voicefx_visual_profile(model, params, phase=0.0, points=161):
    """Return the normalized geometry drawn for one Voice FX component.

    This is deliberately separate from GTK painting.  Both the large editor
    and the six-module rack consume the same profile, so a parameter cannot be
    wired to the device while silently being omitted from one of its displays.
    Values are illustrative response geometry, not live device telemetry.
    """
    P = params or {}

    def value(name, default):
        try:
            return float(P.get(name, default))
        except (TypeError, ValueError):
            return float(default)

    def clamp(v, lo=0.0, hi=1.0):
        return max(lo, min(hi, float(v)))

    count = max(9, int(points))
    positions = tuple(i / float(count - 1) for i in range(count))
    mix = clamp(value("mix", 0.5))

    if model == "transformer":
        lows = clamp(value("lows", 0.5))
        width = clamp(value("width", 0.5))
        curve = []
        for t in positions:
            frequency = 20.0 * (1000.0 ** t)
            shelf = 1.0 / (1.0 + (frequency / 575.0) ** 2.0)
            wet = 0.10 + shelf * (0.10 + lows * 0.78)
            curve.append((1.0 - mix) * 0.16 + mix * wet)
        return {
            "curve": tuple(curve),
            "spread": (0.02 + width * 0.22) * (0.18 + mix * 0.82),
        }

    if model == "detuner":
        semitones = int(round(value("detune", 4.0))) - 8
        ratio = 2.0 ** (semitones / 12.0)
        dry_gain = 0.22 + (1.0 - mix) * 0.70
        wet_gain = 0.06 + mix * 0.86
        return {
            "dry": tuple(math.sin(t * 22.0 + phase) * dry_gain
                         for t in positions),
            "wet": tuple(math.sin(t * 22.0 * ratio + phase * ratio) * wet_gain
                         for t in positions),
            "semitones": semitones,
        }

    if model == "vocoder":
        carrier = max(0, min(2, int(round(value("carrier_type", 1.0)))))
        frequency = clamp(value("carrier_freq", 80.0), 50.0, 500.0)
        frequency_position = (frequency - 50.0) / 450.0
        volume = clamp(value("vol", 1.0))
        bars = []
        for index in range(22):
            harmonic = index + 1
            if carrier == 0:
                amplitude = 0.50 + 0.22 * math.sin(
                    index * 2.3 + frequency_position * math.tau)
            elif carrier == 1:
                amplitude = 1.0 / (
                    1.0 + index * (0.34 + frequency_position * 0.34))
            else:
                amplitude = (1.0 / (1.0 + index * 0.42)
                             if harmonic % 2 else 0.055)
                amplitude *= 0.88 + frequency_position * 0.24
            bars.append(clamp(amplitude * volume * mix))
        return {
            "bars": tuple(bars),
            "dry": tuple(math.sin(t * 18.0 + phase * 0.25)
                         * (1.0 - mix) * 0.30 for t in positions),
        }

    if model == "ringmod":
        frequency = clamp(value("carrier_hz", 30.0), 0.1, 2000.0)
        sub_frequency = clamp(value("carrier2_hz", 50.0), 0.1, 2000.0)
        sub_on = value("carrier2", 0.0) >= 0.5
        distortion = clamp(value("dist", 0.5))
        volume = clamp(value("vol", 1.0))
        frequency_position = math.log(frequency / 0.1) / math.log(20000.0)
        sub_position = math.log(sub_frequency / 0.1) / math.log(20000.0)
        carrier_cycles = 1.5 + frequency_position * 12.0
        sub_cycles = 1.5 + sub_position * 12.0
        dry, output = [], []
        drive = 1.0 + distortion * 6.0
        drive_scale = math.tanh(drive)
        for t in positions:
            source = math.sin(t * math.tau * 3.0 + phase * 0.3)
            wet = source * math.sin(
                t * math.tau * carrier_cycles + phase * 2.0)
            if sub_on:
                sub = source * math.sin(
                    t * math.tau * sub_cycles + phase)
                wet = wet * 0.72 + sub * 0.28
            wet = math.tanh(wet * drive) / drive_scale
            dry.append(source * 0.82)
            output.append(volume * ((1.0 - mix) * source + mix * wet))
        return {
            "dry": tuple(dry),
            "curve": tuple(output),
            # The marker shows the configured sub frequency even while that
            # carrier is off; its intensity communicates whether it is active.
            "sub_marker": sub_position,
            "sub_on": sub_on,
        }

    if model == "filters":
        pitch = clamp(value("pitch", 0.5))
        regeneration = clamp(value("regeneration", 0.5))
        damping = clamp(value("damping", 0.5))
        distortion = clamp(value("distortion", 0.5))
        volume = clamp(value("volume", 1.0))
        samples = int(pitch * 1300.0 + 250.5)
        feedback = regeneration * 0.35 + 0.5
        damp = damping * 0.6 + 0.3
        comb_frequency = 48000.0 / max(1, samples)
        sharpness = 2.0 + feedback * 12.0
        curve = []
        for t in positions:
            frequency = 20.0 * (1000.0 ** t)
            wet = abs(math.cos(
                math.pi * frequency / comb_frequency)) ** sharpness
            wet *= 1.0 / (
                1.0 + (frequency / (20000.0 * (1.06 - damp))) ** 2.0)
            if distortion > 0.0001:
                drive = 1.0 + distortion * 7.0
                wet = 0.5 + 0.5 * (
                    math.tanh((wet * 2.0 - 1.0) * drive) / math.tanh(drive))
            curve.append(volume * ((1.0 - mix) * 0.52 + mix * wet))
        return {
            "curve": tuple(curve),
            "samples": samples,
            "feedback": feedback,
            "damp": damp,
        }

    # Delay uses a fixed 2.75 s window.  A dynamic window made Time mostly
    # cancel itself out: moving the control changed the legend while the echo
    # bars stayed in almost the same places.
    delay_time = clamp(value("time_s", 0.125), 0.0001, 0.25)
    feedback = clamp(value("feedback", 0.5))
    repeats = max(2, min(10, int(2 + feedback * 9)))
    span_s = 2.75
    events = [(0.0, max(0.08, 1.0 - mix), 0.0)]
    for index in range(1, repeats + 1):
        seconds = delay_time * index
        level = mix * (0.25 + feedback * 0.72) ** index
        events.append((min(1.0, seconds / span_s), level, seconds))
    return {
        "events": tuple(events),
        "span_s": span_s,
        "playhead": (phase * 0.05) % 1.0,
    }


class FXVisual(Canvas):
    """A drawn signature for each Voice FX model, in two dimensions.

    Each one is a picture of the transformation that model performs, plotted
    from its OWN decoded parameters — so the display says what the effect is
    and moves when you change it. The rack above is the compact six-component
    selector; this is the selected component's larger parameter-driven view.
    Neither transport success nor an animated view is presented as proof that
    block 201 is audible.
    """

    ANIMATED = ("detuner", "ringmod", "delay")

    def __init__(self, model="transformer", params=None, height=190):
        super().__init__(-1, height)
        self.model = model
        self.params = params or (lambda: {})
        self._key = None
        self.phase = 0.0
        self.set_hexpand(True)

    def set_model(self, name):
        self.model = name
        self._key = None
        self.queue_draw()

    def tick(self):
        """Animated models advance every tick (the page gate in _fx_anim keeps
        this from running unseen); static ones redraw only on a change."""
        if self.model in self.ANIMATED:
            self.phase = (self.phase + 0.09) % (math.pi * 2000)
            if self.get_mapped():
                self.queue_draw()
            return
        try:
            P = self.params() or {}
        except Exception:
            return
        key = (self.model,) + tuple(round(float(v), 4) for _, v in sorted(P.items()))
        if key == self._key:
            return
        self._key = key
        if self.get_mapped():
            self.queue_draw()

    # -- helpers ------------------------------------------------------------
    def _frame(self, sn, w, h):
        box(sn, 0, 0, w, h, pal(FIELD))
        for i in range(1, 8):
            polyline(sn, [(w * i / 8.0, 0), (w * i / 8.0, h)], pal(PHOS, 0.055), 1.0)
        polyline(sn, [(0, h * 0.5), (w, h * 0.5)], pal(PHOS, 0.10), 1.0)

    def _curve(self, sn, w, fn, colour, width=2.2, n=200, glow_r=7.0):
        pts = [(w * i / n, fn(i / float(n))) for i in range(n + 1)]
        glow(sn, lambda: polyline(sn, pts, pal(colour, 0.95), width), radius=glow_r)

    def paint(self, sn, w, h):
        self._frame(sn, w, h)
        try:
            P = self.params() or {}
        except Exception:
            P = {}

        def q(name, default):
            try:
                return float(P.get(name, default))
            except (TypeError, ValueError):
                return default

        m = self.model
        pad = 16.0
        base = h - pad
        profile = voicefx_visual_profile(m, P, self.phase, 321)

        def sample(values, position):
            index = max(0, min(len(values) - 1,
                               int(round(position * (len(values) - 1)))))
            return values[index]

        if m == "transformer":
            lows, width_, mix = q("lows", 0.5), q("width", 0.5), q("mix", 0.5)
            curve = profile["curve"]
            self._curve(
                sn, w,
                lambda t: base - sample(curve, t) * (h - 2 * pad), PHOS)
            # Width and Wet/Dry both determine how far the two sides separate.
            spread = h * profile["spread"]
            for sgn, tone in ((-1, PHOS), (1, THERM)):
                y = h * 0.30 + sgn * spread
                glow(sn, lambda y=y, tone=tone: polyline(
                    sn, [(w * 0.63, y), (w - 14, y)], pal(tone, 0.85), 2.0), radius=5.0)
            label(self, sn, "%+.1f dB shelf  ·  width %.0f%%  ·  wet %.0f%%"
                  % (lows * 12.0, width_ * 100, mix * 100), 10, h - 15,
                  pal(INK, 0.45), 8)

        elif m == "detuner":
            # Two voices, a whole number of semitones apart. Wet/Dry crossfades
            # their heights, so it is visible instead of merely printed below.
            semis = profile["semitones"]
            mix = q("mix", 0.5)
            amp = (h * 0.5 - pad)
            dry, wet = profile["dry"], profile["wet"]
            self._curve(sn, w, lambda t: h * 0.5 + sample(dry, t) * amp,
                        PHOS, 2.0, n=320)
            self._curve(sn, w, lambda t: h * 0.5 + sample(wet, t) * amp,
                        THERM, 1.8, n=320)
            label(self, sn, "%s  ·  wet %.0f%%"
                  % ("no shift" if semis == 0 else "%d semitones" % semis, mix * 100),
                  10, h - 15, pal(INK, 0.45), 8)

        elif m == "vocoder":
            kind = max(0, min(2, int(q("carrier_type", 1))))
            fr = q("carrier_freq", 80.0)
            vol, mix = q("vol", 1.0), q("mix", 0.5)
            dry, bars = profile["dry"], profile["bars"]
            amp = h * 0.5 - pad
            self._curve(sn, w, lambda t: h * 0.5 + sample(dry, t) * amp,
                        GRID, 1.2, n=240, glow_r=0.0)
            n = len(bars)
            for i, amount in enumerate(bars):
                bh = max(1.0, (h - 2 * pad) * amount)
                x = 12 + i * (w - 26) / n
                bw = max(3.0, (w - 26) / n - 5)
                glow(sn, lambda x=x, bh=bh, bw=bw, a=amount:
                     box(sn, x, base - bh, bw, bh, duo(min(1.0, a), 0.9)),
                     radius=5.0)
            label(self, sn, "%s  ·  %.0f Hz  ·  vol %.0f%%  ·  wet %.0f%%"
                  % (["Noise", "Sawtooth", "Rect"][kind], fr,
                     vol * 100, mix * 100), 10, h - 15,
                  pal(INK, 0.45), 8)

        elif m == "ringmod":
            fr, dist = q("carrier_hz", 30.0), q("dist", 0.5)
            sub = q("carrier2", 0) >= 0.5
            vol, mix = q("vol", 1.0), q("mix", 0.5)
            amp = h * 0.5 - pad
            dry, curve = profile["dry"], profile["curve"]
            self._curve(sn, w, lambda t: h * 0.5 + sample(dry, t) * amp,
                        GRID, 1.2, n=200, glow_r=0.0)
            self._curve(sn, w, lambda t: h * 0.5 + sample(curve, t) * amp,
                        PHOS, 2.0, n=380)
            marker_x = 12 + (w - 24) * profile["sub_marker"]
            polyline(sn, [(marker_x, pad), (marker_x, base)],
                     pal(THERM, 0.52 if sub else 0.16), 1.2)
            label(self, sn, "%.0f Hz%s  ·  dist %.0f%%  ·  vol %.0f%%  ·  wet %.0f%%"
                  % (fr, "  + sub %.0f Hz" % q("carrier2_hz", 50.0) if sub else "",
                     dist * 100, vol * 100, mix * 100),
                  10, h - 15, pal(INK, 0.45), 8)

        elif m == "filters":
            curve = profile["curve"]
            self._curve(
                sn, w,
                lambda t: base - sample(curve, t) * (h - 2 * pad) * 0.92,
                PHOS, 2.0, n=420)
            label(self, sn,
                  "%d samples  ·  fb %.2f  ·  damp %.2f  ·  dist %.0f%%  ·  vol %.0f%%  ·  wet %.0f%%"
                  % (profile["samples"], profile["feedback"], profile["damp"],
                     q("distortion", 0.5) * 100, q("volume", 1.0) * 100,
                     q("mix", 0.5) * 100),
                  10, h - 15, pal(INK, 0.45), 8)

        else:                                       # delay
            t_s, fb = q("time_s", 0.125), q("feedback", 0.5)
            mix = q("mix", 0.5)
            events, span_s = profile["events"], profile["span_s"]
            x_of = lambda position: 14 + (w - 28) * position
            # time axis with real millisecond ticks
            polyline(sn, [(12, base), (w - 12, base)], pal(GRID, 0.35), 1.0)
            tick_ms = 500
            sec = tick_ms / 1000.0
            while sec <= span_s:
                x = x_of(sec / span_s)
                polyline(sn, [(x, base), (x, base + 4)], pal(GRID, 0.45), 1.0)
                label(self, sn, "%d" % round(sec * 1000), x - 8, base + 4,
                      pal(INK, 0.30), 6)
                sec += tick_ms / 1000.0
            for i, (position, level, _seconds) in enumerate(events):
                x = x_of(position)
                bh = max(1.0, (h - 2 * pad - 10) * level)
                glow(sn, lambda x=x, bh=bh, i=i:
                     box(sn, x - 1.5, base - bh, 3.0, bh,
                         rgba(*IVORY, 0.9) if i == 0 else duo(i / len(events), 0.85)),
                     radius=5.0)
                if i + 1 < len(events):
                    nx = x_of(events[i + 1][0])
                    top = base - bh - 10
                    arc = [(x + (nx - x) * t2 / 14.0,
                            top + 8 - math.sin(math.pi * t2 / 14.0) * 7)
                           for t2 in range(15)]
                    polyline(sn, arc, pal(THERM, 0.30), 1.0)
            # the playhead: one pulse travelling the tail, wrapping each pass
            px = x_of(profile["playhead"])
            glow(sn, lambda: polyline(sn, [(px, pad), (px, base)],
                                      rgba(*GOLD, 0.35), 1.2), radius=4.0)
            label(self, sn, "%.0f ms  ·  feedback %.0f%%  ·  wet %.0f%%  ·  %d repeats"
                  % (t_s * 1000, fb * 100, mix * 100, len(events) - 1),
                  10, h - 15, pal(INK, 0.45), 8)

        if q("on", 1.0) < 0.5:
            # On is part of each model component, so its visual should say
            # when that model is bypassed instead of animating as if active.
            # Keep the configured shape legible while bypassed; the explicit
            # OFF label and rack lamp carry the state without hiding edits.
            box(sn, 0, 0, w, h, pal(GROUND, 0.30))
            label(self, sn, "%s  ·  OFF" % MODEL_LABELS.get(m, m),
                  10, 7, pal(INK, 0.62), 8)
        else:
            label(self, sn, MODEL_LABELS.get(m, m), 10, 7,
                  pal(INK, 0.55), 8)


class VoiceFxRack(Canvas):
    """Six VoiceFX components as the same live rack language as Fat Channel.

    A click selects the XML mutable component. A double-click operates that
    component's own ``on`` parameter. The full Model row remains immediately
    below as the keyboard/screen-reader selector; this canvas is the compact
    visual navigator and never invents a second model state.
    """

    SHORT_TITLES = ("Doubler", "Detuner", "Vocoder", "Ring Mod", "Filters",
                    "Delay")

    def __init__(self, win):
        super().__init__(-1, 126)
        self.win = win
        self.set_hexpand(True)
        self.set_tooltip_text(
            "Click to select; double-click to turn on or off")
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)

    def _slot(self, x):
        width = self.get_width() or 1
        return max(0, min(len(self.win.FX_ORDER) - 1,
                          int(x / (width / len(self.win.FX_ORDER)))))

    def _clicked(self, _gesture, n_press, x, _y):
        index = self._slot(x)
        self.win.fx_model.set_selected(index)
        if n_press >= 2:
            model = self.win.FX_ORDER[index]
            power = self.win.fx_power[model]
            power.set_value(not bool(power.get_value()))
        self.queue_draw()

    def _state(self, model):
        controls = getattr(self.win, "fx_params", {}).get(model, {})
        state = {name: control.get_value() for name, control in controls.items()}
        power = getattr(self.win, "fx_power", {}).get(model)
        state["on"] = bool(power.get_value()) if power is not None else False
        return state

    @staticmethod
    def _mini(sn, model, state, x, y, w, h, colour):
        """Compact forms of the six parameter-driven FXVisual signatures."""
        active = bool(state.get("on"))
        tone = colour if active else INK
        acc = pal(tone, 0.9 if active else 0.28)
        mid = y + h * 0.5
        profile = voicefx_visual_profile(model, state, points=49)

        def trace(values, amplitude=0.42, colour_=None, alpha=None):
            pts = [(x + i / float(len(values) - 1) * w,
                    mid + value * h * amplitude)
                   for i, value in enumerate(values)]
            ink = (pal(colour_ or tone,
                       alpha if alpha is not None else (0.9 if active else 0.28)))
            polyline(sn, pts, ink, 1.3)

        if model == "transformer":
            values = profile["curve"]
            pts = [(x + i / float(len(values) - 1) * w,
                    y + h - amount * h * 0.86)
                   for i, amount in enumerate(values)]
            polyline(sn, pts, acc, 1.6)
            spread = profile["spread"] * h
            for direction, line_tone in ((-1, tone), (1, THERM)):
                yy = mid + direction * spread
                polyline(sn, [(x + w * 0.58, yy), (x + w, yy)],
                         pal(line_tone, 0.68 if active else 0.22), 1.0)
        elif model == "detuner":
            trace(profile["dry"], 0.42, tone, 0.82 if active else 0.24)
            trace(profile["wet"], 0.42, THERM, 0.72 if active else 0.22)
        elif model == "vocoder":
            trace(profile["dry"], 0.42, GRID, 0.42 if active else 0.18)
            bars = profile["bars"]
            for i, amount in enumerate(bars):
                bh = max(1.0, h * amount)
                step = w / len(bars)
                box(sn, x + i * step, y + h - bh,
                    max(1.0, step - 1.5), bh, acc)
        elif model == "ringmod":
            trace(profile["dry"], 0.34, GRID, 0.38 if active else 0.16)
            trace(profile["curve"], 0.42)
            marker_x = x + w * profile["sub_marker"]
            polyline(sn, [(marker_x, y), (marker_x, y + h)],
                     pal(THERM, 0.48 if profile["sub_on"] else 0.14), 0.9)
        elif model == "filters":
            values = profile["curve"]
            pts = [(x + i / float(len(values) - 1) * w,
                    y + h - amount * h * 0.88)
                   for i, amount in enumerate(values)]
            polyline(sn, pts, acc, 1.4)
        else:
            for i, (position, level, _seconds) in enumerate(profile["events"]):
                xx = x + position * w
                polyline(sn, [(xx, y + h), (xx, y + h - h * level)], acc, 1.5)

    def paint(self, sn, w, h):
        count = len(self.win.FX_ORDER)
        unit_width = w / count
        selected = max(0, min(count - 1, self.win.fx_model.get_selected()))
        for index, model in enumerate(self.win.FX_ORDER):
            x = index * unit_width
            state = self._state(model)
            on = bool(state["on"])
            face = pal(ACCENT, 0.24) if on else pal(GRID, 0.13)
            header = pal(ACCENT, 0.34) if on else pal(GRID, 0.20)
            box(sn, x + 4, 3, unit_width - 8, h - 6, face)
            box(sn, x + 4, 3, unit_width - 8, 24, header)
            if selected == index:
                for ex, ey, ew, eh in ((x + 3, 2, unit_width - 6, 1.5),
                                       (x + 3, h - 3.5, unit_width - 6, 1.5),
                                       (x + 3, 2, 1.5, h - 4),
                                       (x + unit_width - 4.5, 2, 1.5, h - 4)):
                    box(sn, ex, ey, ew, eh, pal(ACCENT, 0.92))
            if on:
                dot(sn, x + 14, 15, 7, pal(ACCENT, 0.24))
            dot(sn, x + 14, 15, 4, pal(ACCENT) if on else pal(INK, 0.25))
            label(self, sn, self.SHORT_TITLES[index], x + 24, 8,
                  pal(INK, 0.95 if on else 0.62), 8)
            self._mini(sn, model, state, x + 11, 35, unit_width - 22,
                       h - 49, ACCENT)


class ReverbVisual(Canvas):
    """The reverb as its own decay: pre-delay gap, then the tail dying away.

    Time runs left to right, energy is height — the two things every reverb
    control actually changes. Size sets how long the tail survives, Wet mix its
    height, Pre-delay the silent run-in before the first reflection, and the
    input high-pass how much low end is under it (drawn as the shaded body).
    """

    def __init__(self, params, height=190):
        super().__init__(-1, height)
        self.params = params
        self.t = 0.0
        self._key = None
        self._moving = False
        self.set_hexpand(True)

    def tick(self):
        try:
            P = self.params() or {}
        except Exception:
            return
        key = tuple(round(float(P.get(k, 0) or 0), 4)
                    for k in ("size", "mix", "predelay", "hp")) + (bool(P.get("on")),)
        moving = bool(P.get("on")) and self._moving
        if key == self._key and not moving:
            return
        self._key = key
        self.t += 0.05
        if self.get_mapped():
            self.queue_draw()

    def paint(self, sn, w, h):
        box(sn, 0, 0, w, h, pal(FIELD))
        try:
            P = self.params() or {}
        except Exception:
            P = {}
        size = max(0.02, min(1.0, float(P.get("size", 0.5))))
        mix = max(0.0, min(1.0, float(P.get("mix", 0.35))))
        pre = max(0.0, min(0.25, float(P.get("predelay", 0.02))))
        hp = max(0.0, min(500.0, float(P.get("hp", 200.0))))
        on = bool(P.get("on", False))
        pad = 16.0
        base = h - pad
        for i in range(1, 8):
            polyline(sn, [(w * i / 8.0, 0), (w * i / 8.0, h)], pal(PHOS, 0.055), 1.0)

        gap = pre / 0.25 * 0.34
        decay = 0.10 + size * 0.72
        lit = 1.0 if on else 0.30

        def env(t):
            if t < gap:
                return 0.0
            return math.exp(-(t - gap) / max(0.03, decay)) * (0.12 + mix * 0.88)

        # the dry hit, then the tail
        polyline(sn, [(14, base), (14, base - (h - 2 * pad) * 0.92)],
                 pal(IVORY, 0.55 * lit), 2.0)
        pts = [(14 + (w - 28) * (i / 240.0), base - env(i / 240.0) * (h - 2 * pad) * 0.92)
               for i in range(241)]
        if on:
            glow(sn, lambda: polyline(sn, pts, pal(PHOS, 0.95), 2.2), radius=7.0)
        else:
            polyline(sn, pts, pal(PHOS, 0.30), 1.6)

        # the body under the tail: how much low end survives the input high-pass
        low = 1.0 - min(1.0, hp / 500.0)
        if low > 0.03:
            step = 8
            for i in range(0, 241, step):
                t = i / 240.0
                y = base - env(t) * (h - 2 * pad) * 0.92 * low
                if base - y > 1.0:
                    x = 14 + (w - 28) * t
                    polyline(sn, [(x, base), (x, y)], duo(1.0 - t, 0.18 * lit), 1.0)

        # the pre-delay gap, marked where it is
        if gap > 0.004:
            gx = 14 + (w - 28) * gap
            polyline(sn, [(gx, 22), (gx, base)], pal(GOLD, 0.45 * lit), 1.2)
            # beside its own line, below the readout — at the old spot it sat
            # directly on top of the "pre N ms" text
            label(self, sn, "pre-delay", gx + 5, 24, pal(GOLD, 0.5 * lit), 7)

        polyline(sn, [(12, base), (w - 12, base)], pal(GRID, 0.30), 1.0)
        label(self, sn, "pre %.0f ms   size %.0f%%   wet %.0f%%   hp %s"
              % (pre * 1000, size * 100, mix * 100,
                 "off" if hp < 1 else "%.0f Hz" % hp),
              10, 7, pal(INK, 0.48), 8)
        if not on:
            label(self, sn, "reverb off", w - 74, h - 15, pal(INK, 0.35), 8)


def fx_response(model, P, bins=34):
    """The effect's spectral response across the audible band, 0..1 per bin.

    Real characterisations rather than ornament — each is the shape that model
    actually imposes, computed from its own decoded parameters:

        Transformer  a low shelf at 600/550 Hz, gain = Lows * 12 dB, plus the
                     stereo spread Width opens up
        Detuner      the source partial and its shifted copy, Tune semitones apart
        Vocoder      the carrier's harmonic series — 1/n for sawtooth, odd-only
                     for rectangle, flat and noisy for noise
        Ring Mod     sidebands at signal +/- carrier, and again for the sub carrier
        Filters      the comb produced by a 250-1550 sample feedback delay
        Delay        the comb produced by the delay time, depth set by Feedback

    Log frequency, 20 Hz to 20 kHz, so the low end gets the room it deserves.
    """
    out = []
    for i in range(bins):
        f = 20.0 * (1000.0 ** (i / (bins - 1.0)))          # 20 Hz .. 20 kHz
        v = 0.0
        if model == "transformer":
            lows = P.get("lows", 0.5)
            width = P.get("width", 0.5)
            shelf = 1.0 / (1.0 + (f / 575.0) ** 2.0)
            v = 0.16 + shelf * (0.10 + lows * 0.78) + width * 0.10 * (f > 2000)
        elif model == "detuner":
            semis = int(P.get("detune", 4)) - 8
            r = 2.0 ** (semis / 12.0)
            for k in range(1, 7):
                for f0, amp in ((220.0 * k, 0.85 / k), (220.0 * k * r, 0.85 / k)):
                    v += amp * math.exp(-((math.log(f / f0)) ** 2) / 0.006)
        elif model == "vocoder":
            kind = int(P.get("carrier_type", 1))
            f0 = max(20.0, P.get("carrier_freq", 80.0))
            if kind == 0:
                v = 0.42 + 0.18 * math.sin(f * 0.013)
            else:
                for k in range(1, 26):
                    if kind == 2 and k % 2 == 0:
                        continue
                    v += (0.95 / k) * math.exp(-((math.log(f / (f0 * k))) ** 2) / 0.004)
        elif model == "ringmod":
            c = max(0.1, P.get("carrier_hz", 30.0))
            sig = 300.0
            peaks = [(sig - c, 0.8), (sig + c, 0.8)]
            if P.get("carrier2", 0) >= 0.5:
                c2 = max(0.1, P.get("carrier2_hz", 50.0))
                peaks += [(sig - c2, 0.5), (sig + c2, 0.5)]
            for f0, amp in peaks:
                if f0 > 1:
                    v += amp * math.exp(-((math.log(f / f0)) ** 2) / 0.004)
            v += P.get("dist", 0.5) * 0.30 * (1.0 / (1.0 + (f / 4000.0)))
        elif model == "filters":
            samples = int(P.get("pitch", 0.5) * 1300 + 250.5)
            fb = P.get("regeneration", 0.5) * 0.35 + 0.5
            damp = P.get("damping", 0.5) * 0.6 + 0.3
            f_comb = 48000.0 / max(1, samples)
            v = abs(math.cos(math.pi * f / f_comb)) ** (2 + fb * 12)
            v *= 1.0 / (1.0 + (f / (20000.0 * (1.05 - damp))) ** 2)
        else:                                              # delay
            t = max(0.001, P.get("time_s", 0.125))
            fb = P.get("feedback", 0.5)
            v = 0.5 + 0.5 * math.cos(2 * math.pi * f * t) * (0.25 + fb * 0.7)
        out.append(max(0.0, min(1.0, v)))
    return out


# NAMING, and PreSonus itself is inconsistent about it. Checked against the
# io24's OWN owner's manual (EN_08042022, fetched from PreSonus; cached in
# ~/.cache/io24/re/), not just the io44's: BOTH manuals call model 0 "Doubler"
# — the io24 spec sheet lists "Doubler, Vocoder, Ring Modulator, Comb Filter,
# Detuner, Delay, Reverb". "Transformer" appears only in Universal Control,
# which is nevertheless what this device's owner saw and used, so it stays.
# That same spec sheet's "Comb Filter" independently confirms the decode of
# model 4 as a tuned feedback comb — identified from the binary alone before
# the manual was read.
MODEL_LABELS = {
    "transformer": "TRANSFORMER",
    "detuner": "DETUNER",
    "vocoder": "VOCODER",
    "ringmod": "RING MODULATOR",
    "filters": "FILTERS",
    "delay": "DELAY",
}

MODEL_TITLES = [component["title"]
                for component in io24_fx.VOICEFX_XML_SCHEMA.values()]


def _voicefx_value_text(parameter, value):
    """Format one value with the units or list labels declared by UC XML."""
    if parameter["type"] == "list":
        choices = parameter["choices"]
        return choices[max(0, min(len(choices) - 1, int(round(value))))]
    units = parameter.get("units")
    if units == "percent":
        return "%.0f %%" % (value * 100.0)
    if units == "freq":
        return ("%.1f Hz" if value < 100.0 else "%.0f Hz") % value
    if units in ("time", "time/off"):
        return "%.0f ms" % (value * 1000.0)
    return "%.3g" % value


def _voicefx_ui_fields():
    """Build the editable Host rows directly from the retained XML schema."""
    fields = {}
    for model, component in io24_fx.VOICEFX_XML_SCHEMA.items():
        rows = []
        for parameter in component["parameters"]:
            builder = parameter.get("builder")
            if builder in (None, "on"):
                continue
            if parameter["type"] == "list":
                lo, hi, step = 0, len(parameter["choices"]) - 1, 1
            elif parameter["type"] == "toggle":
                lo, hi, step = 0, 1, 1
            else:
                lo, hi = parameter["min"], parameter["max"]
                step = 0.0001 if parameter.get("units") == "time" else 0.005
            formatter = lambda value, p=parameter: _voicefx_value_text(p, value)
            rows.append((builder, parameter["name"], lo, hi, step,
                         parameter.get("default", 0), formatter))
        fields[model] = rows
    return fields


VOICEFX_UI_FIELDS = _voicefx_ui_fields()


class Knob(Canvas):
    """A rotary control. Drag vertically to turn, double-click to centre.

    Drawn rather than themed because a Gtk.Scale cannot be made to look like a
    knob, and pan is the one control on this desk that is genuinely rotary — a
    left/right position reads instantly on a dial and poorly on a slider.

    Vertical drag, not circular: circular tracking feels clever for a second and
    then fights you at the top of the sweep, where a small hand movement crosses
    the discontinuity. Every DAW that tried it went back to vertical.
    """

    SWEEP = 270.0                        # degrees of travel, centred on 12 o'clock
    TRAVEL = 140.0                       # pixels of drag for the full sweep

    def __init__(self, value=0.5, size=46, on_change=None, centre=0.5, label=""):
        super().__init__(size, size)
        self.value = value
        self.centre = centre
        self.on_change = on_change
        self.label = label
        self._start = value
        # While you are turning this, the device's own reported value lags —
        # the write is throttled and the state poll is 30 fps behind. Without a
        # hold, _sync yanks the knob back to that stale value on every frame,
        # which is what made it feel like it was fighting the mouse. Any touch
        # claims the control for a moment; the device drives it again after.
        self._hold_until = 0.0
        self.set_tooltip_text("Drag to turn, double-click to centre")

        dr = Gtk.GestureDrag()
        dr.connect("drag-begin", self._begin)
        dr.connect("drag-update", self._update)
        dr.connect("drag-end", self._end)
        self.add_controller(dr)
        cl = Gtk.GestureClick()
        cl.connect("pressed", self._clicked)
        self.add_controller(cl)

    def set_value(self, v, notify=True):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self.value) < 1e-9:
            return
        self.value = v
        self.queue_draw()
        if notify and self.on_change:
            self.on_change(v)

    HOLD_S = 0.6

    def _touch(self):
        self._hold_until = time.monotonic() + self.HOLD_S

    def _begin(self, *_a):
        self._start = self.value
        self._touch()

    def _update(self, _g, _dx, dy):
        self._touch()
        self.set_value(self._start - dy / self.TRAVEL)

    def _end(self, *_a):
        self._touch()

    def _clicked(self, g, n_press, _x, _y):
        if n_press == 2:
            self._touch()
            self.set_value(self.centre)
            g.set_state(Gtk.EventSequenceState.CLAIMED)

    def adopt(self, v):
        """Device-driven update. Ignored while the user is working the knob."""
        if time.monotonic() < self._hold_until:
            return
        self.set_value(v, notify=False)

    def _angle(self, v):
        return math.radians(-90 - self.SWEEP / 2 + v * self.SWEEP)

    def paint(self, sn, w, h):
        cx, cy = w / 2.0, h / 2.0
        r = min(cx, cy) - 6.0
        # track
        track = [(cx + r * math.cos(self._angle(t / 40.0)),
                  cy + r * math.sin(self._angle(t / 40.0))) for t in range(41)]
        polyline(sn, track, pal(GRID, 0.38), 3.0)
        # travel from centre to the current position, so the eye reads offset
        a, b = sorted((self.centre, self.value))
        if b - a > 0.004:
            n = max(2, int((b - a) * 40))
            arc = [(cx + r * math.cos(self._angle(a + (b - a) * i / n)),
                    cy + r * math.sin(self._angle(a + (b - a) * i / n)))
                   for i in range(n + 1)]
            glow(sn, lambda: polyline(sn, arc, pal(ACCENT), 3.0), radius=5.0)
        # pointer
        ang = self._angle(self.value)
        p0 = (cx + r * 0.30 * math.cos(ang), cy + r * 0.30 * math.sin(ang))
        p1 = (cx + r * 0.92 * math.cos(ang), cy + r * 0.92 * math.sin(ang))
        glow(sn, lambda: polyline(sn, [p0, p1], pal(INK, 0.95), 2.4), radius=4.0)
        dot(sn, cx, cy, 3.0, pal(GRID, 0.75))
        if self.label:
            label(self, sn, self.label, 2, h - 11, pal(INK, 0.5), 7)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _fet_input_db(threshold_db):
    """Map the Threshold slider (-56..0) onto the FET model's INPUT drive.

    Inverted and anchored, in two segments, because the FET has no threshold —
    input drive is what compresses, so the control runs the other way, and its
    auto-makeup is a steep function of the operating point (+0.58 dB at -43,
    +27.5 dB at -24). The anchor puts the slider's own default of -24 dB onto
    Universal Control's default input of -43 dB, which is unity makeup.

        cth -56 (max compression) -> input   0 dB
        cth -24 (slider default)  -> input -43 dB   (UC default, ~unity)
        cth   0 (min compression) -> input -56 dB
    """
    t = _clamp(float(threshold_db), -56.0, 0.0)
    if t <= -24.0:                       # -56..-24  ->  0..-43
        f = (t + 24.0) / -32.0           # 0 at -24, 1 at -56
        return _clamp(-43.0 * (1.0 - f), -56.0, 0.0)
    f = (t + 24.0) / 24.0                # 0 at -24, 1 at 0
    return _clamp(-43.0 - 13.0 * f, -56.0, 0.0)


def _fet_ratio_index(ratio):
    """Ratio slider (1..20) -> the FET's five fixed positions.

    FET_RATIO is [6.5, 9.9, 14.0, 22.0, 20.0]; index 4 is the 1176's "All
    buttons in" mode. The old code did min(3, int(ratio/5)), which capped at 3 —
    index 4 was unreachable — and put everything below 5:1 on index 0.
    """
    r = _clamp(float(ratio), 1.0, 20.0)
    if r >= 19.5:
        return 4                          # top of the slider = All
    for i, edge in enumerate((8.0, 12.0, 18.0)):
        if r < edge:
            return i
    return 3


class ChannelBound:
    """Mixin for canvases that draw exactly ONE channel's state.

    The fat channel became per-channel — two independent columns, `Win.w[ch]` for
    the widgets and `bands_by_ch` / `cur_band_by_ch` / `dyn_by_ch` for the state —
    but these canvases went on reading the single-channel names the refactor
    removed: `win.bands`, `win.cur_band`, `win.dyn`, `win.fat_ch` and the
    `win.s_*` widget handles. None of them has existed since.

    Nothing crashed visibly because GTK swallows exceptions raised inside a draw
    callback: every `paint()` simply died partway through, so the EQ curve, the
    compressor transfer curve and three of the five rack units silently never
    drew, and all EQ mouse interaction — click-to-select, drag, scroll-for-Q,
    hover — was dead. A silent half-drawn frame is why this survived so long.

    Each canvas is already constructed with its channel, so the fix is to read
    through to the per-channel state rather than to reintroduce global handles.
    """

    @property
    def bands(self):
        """This channel's band DATA. Note `w[ch]["bands"]` is the button row."""
        return self.win.bands_by_ch[self.channel]

    @property
    def cur_band(self):
        return self.win.cur_band_by_ch[self.channel]

    @cur_band.setter
    def cur_band(self, value):
        self.win.cur_band_by_ch[self.channel] = value

    @property
    def dyn(self):
        return self.win.dyn_by_ch[self.channel]

    def wid(self, key):
        """One of this channel's widgets, e.g. wid("cth") for the comp threshold."""
        return self.win.w[self.channel][key]


class EQCurve(ChannelBound, Canvas):
    """Combined response with a marker per band; click a marker to select it."""

    def __init__(self, win, channel=1):
        super().__init__(-1, 230)
        self.win = win
        self.channel = channel
        self.set_hexpand(True)
        self.drag_band = None
        self._drag_thr = Throttle(
            lambda _value: self.win._push_band(self.channel), 0.012)
        self._drag_push = lambda: self._drag_thr(0.0)
        g = Gtk.GestureClick()
        g.connect("pressed", self._clicked)
        self.add_controller(g)
        # grab a node and move it, the way any DAW equaliser works:
        # horizontal = frequency, vertical = gain
        dr = Gtk.GestureDrag()
        dr.connect("drag-begin", self._drag_begin)
        dr.connect("drag-update", self._drag_update)
        dr.connect("drag-end", self._drag_end)
        self.add_controller(dr)
        # the scroll wheel over a node adjusts its Q, again as DAWs do
        scr = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scr.connect("scroll", self._scroll)
        self.add_controller(scr)
        mo = Gtk.EventControllerMotion()
        mo.connect("motion", self._motion)
        self.add_controller(mo)
        self.hover = None

    def _x(self, f, w):
        return math.log10(max(f, 20) / 20.0) / math.log10(24000 / 20.0) * w

    def _nearest(self, x, y):
        w, h = self.get_width() or 1, self.get_height() or 1
        best, bd = 0, 1e18
        for i, b in enumerate(self.bands):
            bx = self._x(b["freq"], w)
            by = h / 2 - self.win.response_for(
                self.channel, b["freq"], fs=self.win._fs,
                enabled=True) / 18.0 * (h / 2)
            d = (bx - x) ** 2 + ((by - y) * 0.65) ** 2
            if d < bd:
                best, bd = i, d
        return best

    def _clicked(self, _g, _n, x, y):
        if self.win._alt_eq(self.channel) is not None:
            return
        self.win.select_band(self._nearest(x, y), self.channel)

    def _motion(self, _c, x, y):
        if self.win._alt_eq(self.channel) is not None:
            self.hover = None
            return
        n = self._nearest(x, y)
        if n != self.hover:
            self.hover = n
            self.queue_draw()

    def _f_from_x(self, x, w):
        t = max(0.0, min(1.0, x / max(w, 1)))
        low, high = io24_presets.STANDARD_EQ_FREQ_RANGE
        return max(low, min(high, 20.0 * (24000 / 20.0) ** t))

    def _drag_begin(self, g, x, y):
        if self.win._alt_eq(self.channel) is not None:
            self.drag_band = None
            return
        self.drag_band = self._nearest(x, y)
        self.win.select_band(self.drag_band, self.channel)
        if not self.win.eq_enabled(self.channel):
            self.win._set_eq_enabled(self.channel, True)
        b = self.bands[self.drag_band]
        if b["shape"] == "off":            # grabbing an unused band switches it on
            b["shape"] = self.win._standard_band_mode(
                self.channel, self.drag_band)
            b["on"] = True
            prior = self.win._adopt_mute
            self.win._adopt_mute = True
            try:
                self.wid("band_on").set_active(True)
            finally:
                self.win._adopt_mute = prior
        self._start = (x, y, b["freq"], b["gain"])

    def _drag_update(self, g, dx, dy):
        if self.drag_band is None:
            return
        self.win.invalidate_curve()
        w, h = self.get_width() or 1, self.get_height() or 1
        x0, y0, f0, g0 = self._start
        b = self.bands[self.drag_band]
        b["freq"] = self._f_from_x(x0 + dx, w)
        b["gain"] = max(-15.0, min(15.0, g0 - dy / (h / 2) * 18.0))
        prior = self.win._adopt_mute
        self.win._adopt_mute = True
        try:
            self.wid("freq").set_value(b["freq"])
            self.wid("gain").set_value(b["gain"])
        finally:
            self.win._adopt_mute = prior
        self._drag_push()
        self.queue_draw()
        for rack in self.win.racks.values():
            rack.queue_draw()

    def _drag_end(self, g, dx, dy):
        self.drag_band = None

    def _scroll(self, _c, _dx, dy):
        if self.win._alt_eq(self.channel) is not None:
            return True
        i = self.hover if self.hover is not None else self.cur_band
        b = self.bands[i]
        b["q"] = max(0.1, min(10.0, b["q"] * (1.12 if dy > 0 else 1 / 1.12)))
        self.win.invalidate_curve()
        if i == self.cur_band:
            self.wid("q").set_value(b["q"])
        else:
            prior = self.cur_band
            self.cur_band = i
            try:
                self.win._push_band(self.channel)
            finally:
                self.cur_band = prior
        self.queue_draw()
        return True

    def paint(self, sn, w, h):
        col = self.get_color()
        acc = chan_col(self.channel)
        box(sn, 0, 0, w, h, pal(GROUND))
        # zero line reads brighter than the rest of the graticule, so the eye
        # finds unity without hunting for a label
        for d in (-12, -6, 0, 6, 12):
            y = h / 2 - d / 18.0 * (h / 2)
            polyline(sn, [(0, y), (w, y)],
                     pal(GRID, 0.30 if d == 0 else 0.11), 1.0)
        for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
            x = self._x(f, w)
            polyline(sn, [(x, 0), (x, h)], pal(GRID, 0.11), 1.0)
        for f, t in ((100, "100"), (1000, "1k"), (10000, "10k")):
            label(self, sn, t, self._x(f, w) + 3, h - 13, pal(INK, 0.45), 8)
        for d in (12, 0, -12):
            label(self, sn, "%+d" % d, 3, h / 2 - d / 18.0 * (h / 2) - 6,
                  pal(INK, 0.45), 8)
        # spectrum backdrop, straight off the device's capture stream.
        # Always say what it is doing: a silent input used to draw nothing at
        # all, which is indistinguishable from the feature being missing.
        sp = self.win.spectrum
        mag = sp.mag[self.channel - 1] if sp else None
        if sp is None:
            label(self, sn, "spectrum: analyser not started", 8, 6, pal(INK, 0.45), 8)
        elif sp.error:
            label(self, sn, "spectrum: %s" % sp.error[:44], 8, 6, pal(RED, 0.75), 8)
        elif not mag:
            label(self, sn, "spectrum: starting capture…", 8, 6, pal(INK, 0.45), 8)
        else:
            peak = max(mag)
            label(self, sn, "spectrum: input %d   peak %.0f dB%s"
                  % (self.channel, peak,
                     "   (no signal on this input)" if peak < -90 else ""),
                  8, 6, pal(INK, 0.45), 8)
        if mag:
            top, bot = -6.0, -108.0
            poly = [(0, h)]
            for f, d in zip(sp.freqs, mag):
                x = self._x(f, w)
                y = h - (max(bot, min(top, d)) - bot) / (top - bot) * h
                poly.append((x, y))
            poly.append((w, h))
            # the analyser sits UNDER the EQ curve, so it is deliberately dim
            # and desaturated — it is context, not the thing being edited
            fill_poly(sn, poly, pal(acc, 0.13))
            polyline(sn, poly[1:-1], pal(acc, 0.42), 1.0)

        alt = self.win._alt_eq(self.channel)
        if alt is not None:
            active = self.win.eq_enabled(self.channel)
            pts = self.win.curve_points(w, h, self.channel)
            if active:
                fill_poly(sn, pts + [(w, h / 2), (0, h / 2)],
                          pal(acc, 0.14))
                glow(sn, lambda: polyline(sn, pts, pal(acc), 2.2), radius=8.0)
            else:
                polyline(sn, pts, pal(acc, 0.32), 1.4)
            label(self, sn, "%s EQ" % alt["model"].title(),
                  8, 20, pal(INK, 0.72), 8)
            if alt.get("error"):
                label(self, sn, alt["error"][:64], 8, h / 2,
                      pal(RED, 0.75), 8)
            return
        active = self.win.eq_enabled(self.channel)
        pts = self.win.curve_points(w, h, self.channel)
        if active and any(b["shape"] != "off" for b in self.bands):
            fill_poly(sn, pts + [(w, h / 2), (0, h / 2)], pal(acc, 0.14))
        if active:
            glow(sn, lambda: polyline(sn, pts, pal(acc), 2.2), radius=8.0)
        else:
            polyline(sn, pts, pal(acc, 0.32), 1.4)
        for i, b in enumerate(self.bands):
            if b["shape"] == "off":
                continue
            x = self._x(b["freq"], w)
            y = max(8, min(h - 8, h / 2 - self.win.response_for(
                self.channel, b["freq"], fs=self.win._fs,
                enabled=True) / 18.0 * (h / 2)))
            sel = (i == self.cur_band)
            hov = (i == self.hover)
            r = 8 if sel else (7 if hov else 5)
            polyline(sn, [(x, 0), (x, h)], pal(acc, 0.32 if sel else 0.12), 1.0)
            dot(sn, x, y, r + 3, rgba(0, 0, 0, 0.35))
            if sel:
                glow(sn, lambda x=x, y=y, r=r: dot(sn, x, y, r, pal(AMBER)), radius=6.0)
            else:
                dot(sn, x, y, r, pal(acc, 0.95 if hov else 0.8))
            dot(sn, x, y, max(1.5, r - 3), rgba(1, 1, 1, 0.9))
            if sel or hov:
                label(self, sn, "%s  %.0f Hz  %+.1f dB  Q %.2f"
                      % (BAND_NAMES[i], b["freq"], b["gain"], b["q"]),
                      min(w - 168, x + 11), max(2, y - 20), pal(INK, 0.92), 8)
            else:
                label(self, sn, BAND_NAMES[i], x + 10, y - 7, pal(INK, 0.7), 8)


class CompCurve(ChannelBound, Canvas):
    """Compressor transfer curve with a live dot at the current input level."""

    def __init__(self, win, channel=1):
        super().__init__(-1, 180)
        self.win = win
        self.channel = channel
        self.set_hexpand(True)

    def paint(self, sn, w, h):
        col = self.get_color()
        acc = chan_col(self.channel)
        lo = -60.0
        box(sn, 0, 0, w, h, pal(GROUND))
        X = lambda d: (d - lo) / (-lo) * w
        Y = lambda d: h - (d - lo) / (-lo) * h
        for d in range(-60, 1, 12):
            polyline(sn, [(X(d), 0), (X(d), h)], pal(GRID, 0.11), 1.0)
            polyline(sn, [(0, Y(d)), (w, Y(d))], pal(GRID, 0.11), 1.0)
        # unity: dashed-looking guide so the compression is visible as departure
        polyline(sn, [(X(lo), Y(lo)), (X(0), Y(0))], pal(GRID, 0.34), 1.0)
        # Use the exact builder inputs for whichever hardware compressor is
        # selected.  Tube and FET do not have Standard's threshold/ratio/makeup
        # controls, so drawing them from those hidden sliders was as misleading
        # as sending those sliders to the DSP.
        thr, ratio, mk, model_name = self.win.compressor_transfer(self.channel)
        on = self.dyn["comp"]
        pts = []
        for i in range(0, int(w) + 1, 3):
            din = lo + i / max(w, 1) * (-lo)
            dout = din if din <= thr else thr + (din - thr) / ratio
            if on:
                dout = min(0.0, dout + mk)
            pts.append((X(din), Y(dout)))
        # The area between unity and the transfer curve IS the gain change —
        # shading it makes the compressor's effect the biggest thing on the
        # graph instead of something you infer from two nearly-parallel lines.
        # Above unity is makeup (added), below is reduction (taken away), so the
        # two are coloured differently rather than both reading as "activity".
        if on:
            above = [(X(lo), Y(lo))]
            below = [(X(lo), Y(lo))]
            for (px, py), i in zip(pts, range(0, int(w) + 1, 3)):
                din = lo + i / max(w, 1) * (-lo)
                (above if py < Y(din) else below).append((px, py))
            for poly, tone in ((below, RED), (above, GREEN)):
                if len(poly) > 2:
                    closed = poly + [(poly[-1][0], Y(lo)), (X(lo), Y(lo))]
                    fill_poly(sn, closed, pal(tone, 0.13))

        # threshold: a vertical marker, so the knee is locatable at a glance
        polyline(sn, [(X(thr), 0), (X(thr), h)],
                 pal(AMBER, 0.45 if on else 0.18), 1.0)

        if on:
            glow(sn, lambda: polyline(sn, pts, pal(acc), 2.2), radius=7.0)
        else:
            polyline(sn, pts, pal(INK, 0.30), 2.0)
        label(self, sn, "%s  threshold %.0f dB" % (model_name, thr),
              X(thr) + 4, 4, pal(INK, 0.5), 8)
        label(self, sn, "in", w - 18, h - 14, pal(INK, 0.45), 8)
        label(self, sn, "%.1f:1" % ratio, 6, h - 14, pal(INK, 0.45), 8)

        lvl = self.win.ctl.snap["in"][self.channel - 1]
        if lvl > lo:
            dout = lvl if lvl <= thr else thr + (lvl - thr) / ratio
            if on:
                dout = min(0.0, dout + mk)
            # drop lines to both axes: the working point is only useful if you
            # can read what it maps FROM and TO without measuring by eye
            polyline(sn, [(X(lvl), Y(dout)), (X(lvl), h)], pal(acc, 0.30), 1.0)
            polyline(sn, [(0, Y(dout)), (X(lvl), Y(dout))], pal(acc, 0.30), 1.0)
            glow(sn, lambda: dot(sn, X(lvl), Y(dout), 4.5, pal(acc)), radius=6.0)
            dot(sn, X(lvl), Y(dout), 2.0, rgba(1, 1, 1, 0.92))
            gr = dout - lvl
            if on and abs(gr) > 0.05:
                label(self, sn, "%+.1f dB" % gr,
                      min(w - 52, X(lvl) + 8), max(2, Y(dout) - 16),
                      pal(RED if gr < 0 else GREEN, 0.95), 8)


class Rack(ChannelBound, Canvas):
    """The fat channel drawn as rack units, one per module, live.

    Each unit shows what it is actually doing right now — the gate its
    threshold against the input, the compressor its transfer curve and the
    working point, the EQ a miniature of its own response, the limiter its
    ceiling. Drag a unit to reorder.

    An honest note about reordering: the hardware exposes exactly ONE ordering
    choice, `'opt '` `'Pari'` id 0 = swapcompeq, which swaps the compressor and
    the EQ. Filter, gate and limiter sit at fixed points in the DSP chain and no
    wire parameter moves them. So those units are drawn with a lock and refuse
    the drop rather than pretending.
    """

    FIXED = {"HPF", "GATE", "LIM"}
    TITLES = {"HPF": "HPF", "GATE": "Gate", "COMP": "Compressor",
              "EQ": "Equaliser", "LIM": "Limiter"}

    def __init__(self, win, channel):
        super().__init__(-1, 132)
        self.win = win
        self.channel = channel
        self.set_hexpand(True)
        self.order = ["HPF", "GATE", "COMP", "EQ", "LIM"]
        self.drag = None
        self.drag_x = 0.0
        self.hover = None
        self.preview = None
        self._dx = 0.0
        d = Gtk.GestureDrag()
        d.connect("drag-begin", self._begin)
        d.connect("drag-update", self._update)
        d.connect("drag-end", self._end)
        self.add_controller(d)
        c = Gtk.GestureClick()
        c.connect("released", self._clicked)
        self.add_controller(c)
        # scrolling over the low-cut unit sets its corner frequency, the way
        # the original software let you dial it straight on the module
        sc = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", self._scroll)
        self.add_controller(sc)
        self.moved = False
        m = Gtk.EventControllerMotion()
        m.connect("motion", self._motion)
        m.connect("leave", lambda *_a: (setattr(self, "hover", None),
                                        self.queue_draw()))
        self.add_controller(m)

    # ---- geometry -----------------------------------------------------
    def _slot(self, x):
        w = self.get_width() or 1
        return max(0, min(len(self.order) - 1, int(x / (w / len(self.order)))))

    def _motion(self, _c, x, _y):
        i = self._slot(x)
        if i != self.hover:
            self.hover = i
            self.queue_draw()

    def _begin(self, _g, x, _y):
        self.moved = False
        i = self._slot(x)
        self.drag = i if self.order[i] not in self.FIXED else None
        self.drag_x = x
        if self.drag is None and self.order[i] in self.FIXED:
            self.win.say("%s sits at a fixed point in the hardware chain"
                         % self.TITLES[self.order[i]])
        self.queue_draw()

    def _update(self, _g, dx, _dy):
        if self.drag is None:
            return
        if abs(dx) > 4:
            self.moved = True
        self._dx = dx
        # live preview: work out where it would land and let the other units
        # slide aside, so the drag reads as continuous rather than snapping
        # only at the moment of release
        tgt = self._slot(max(0.0, self.drag_x + dx))
        self.preview = tgt if self.order[tgt] not in self.FIXED else None
        self.queue_draw()

    def _end(self, _g, dx, _dy):
        if self.drag is None:
            return
        src = self.drag
        dst = self._slot(max(0, self.drag_x + dx))
        self.drag, self._dx, self.preview = None, 0, None
        if dst != src and self.order[dst] not in self.FIXED:
            a, b = sorted((src, dst))
            if set(self.order[a:b + 1]) <= {"COMP", "EQ"} or \
               {self.order[src], self.order[dst]} == {"COMP", "EQ"}:
                self.order[src], self.order[dst] = self.order[dst], self.order[src]
                eq_first = self.order.index("EQ") < self.order.index("COMP")
                self.win.set_order(self.channel, eq_first)
            else:
                self.win.say("Only the compressor and EQ can trade places — "
                             "that is the one ordering the hardware exposes")
        self.queue_draw()

    def _clicked(self, _g, n, x, y):
        """Single click REVEALS the unit's controls; double click switches it.

        Reveal is the primary action because the rack is a navigator: with every
        section stacked on screen at once there was nothing for a click to show,
        which is what made the rack decorative. Toggling moved to double-click
        rather than disappearing — it is still the fastest way to A/B a module.
        """
        if self.moved:
            return
        i = self._slot(x)
        mod = self.order[i]
        w = self.get_width() or 1
        uw = w / len(self.order)
        if mod == "HPF":
            # clicking inside the response area dials the corner frequency,
            # left edge = bypassed, right edge = 1 kHz
            rel = (x - i * uw - 12) / max(1.0, uw - 24)
            if 0.0 <= rel <= 1.0 and y > 30:
                hz = 24.0 * (1000.0 / 24.0) ** max(0.0, min(1.0, rel))
                self.win.set_hpf(self.channel, hz)
                return
        if n >= 2:
            self.win.toggle_module(self.channel, mod)
        else:
            self.win.show_module(self.channel, mod)

    def _scroll(self, _c, _dx, dy):
        i = self.hover
        if i is None or self.order[i] != "HPF":
            return False
        hz = self.win.hpf_hz(self.channel)
        hz = max(24.0, min(1000.0, hz * (1.15 if dy < 0 else 1 / 1.15)))
        self.win.set_hpf(self.channel, hz)
        return True

    # ---- per-module miniature ------------------------------------------
    def _mini(self, sn, mod, x, y, w, h, col, on):
        gr = self.win.ctl.snap["gr"].get(self.channel, {})
        lvl = self.win.ctl.snap["in"][self.channel - 1]
        acc = pal(chan_col(self.channel)) if on else pal(INK, 0.30)
        alt = self.win._alt_eq(self.channel) if mod == "EQ" else None
        if alt is not None:
            label(self, sn, alt["model"].upper(), x + 4, y + h / 2 - 6, acc, 8)
        elif mod == "EQ":
            bands = tuple(dict(b) for b in self.win.bands_by_ch[self.channel])
            pts = []
            for i in range(0, int(w) + 1, 3):
                f = 20.0 * (24000 / 20.0) ** (i / max(w, 1))
                mag = self.win.response_for(self.channel, f, bands=bands)
                pts.append((x + i, y + h / 2 - mag / 18.0 * (h / 2)))
            polyline(sn, [(x, y + h / 2), (x + w, y + h / 2)], pal(GRID, 0.18), 1.0)
            polyline(sn, pts, acc, 1.6)
        elif mod == "COMP":
            if self.win._multiband_selected(self.channel):
                # Four independent selected UC characters, not the hidden
                # Standard controls. Each miniature is drawn from the same
                # compiled tuple that drives its realtime band processor.
                gap = 3.0
                cell = (w - 3.0 * gap) / 4.0
                short = ("L", "LM", "HM", "H")
                for bi in range(4):
                    bx = x + bi * (cell + gap)
                    values = self.win._mbc_band_state(self.channel, bi)
                    control = io24_mbc.compressor_controls(
                        values, getattr(self.win, "_fs", 48000.0))
                    thr = max(-60.0, min(0.0,
                        control["Threshold level (dB)"]))
                    slope = max(0.0, min(1.0, control["Slope"]))
                    makeup = 20.0 * math.log10(max(
                        control["Makeup gain (linear)"], 1e-12))
                    X = lambda d, left=bx: left + (d + 60.0) / 60.0 * cell
                    Y = lambda d: y + h - (d + 60.0) / 60.0 * h
                    polyline(sn, [(X(-60), Y(-60)), (X(0), Y(0))],
                             pal(GRID, 0.16), 1.0)
                    points = []
                    for step in range(0, max(2, int(cell)) + 1, 2):
                        din = -60.0 + step / max(cell, 1.0) * 60.0
                        dout = din - slope * max(0.0, din - thr)
                        if on:
                            dout = min(0.0, dout + makeup)
                        points.append((X(din), Y(dout)))
                    polyline(sn, points, acc, 1.3)
                    model = values["type"][0].upper()
                    label(self, sn, short[bi] + model, bx + 2, y + 1,
                          pal(INK, 0.62), 6)
            else:
                thr = self.wid("cth").get_value()
                ratio = max(1.0, self.wid("rat").get_value())
                X = lambda d: x + (d + 60) / 60.0 * w
                Y = lambda d: y + h - (d + 60) / 60.0 * h
                polyline(sn, [(X(-60), Y(-60)), (X(0), Y(0))],
                         pal(GRID, 0.18), 1.0)
                pts = []
                for i in range(0, int(w) + 1, 3):
                    din = -60 + i / max(w, 1) * 60
                    dout = din if din <= thr else thr + (din - thr) / ratio
                    pts.append((X(din), Y(min(0.0, dout))))
                polyline(sn, pts, acc, 1.6)
                if lvl > -60:
                    dout = lvl if lvl <= thr else thr + (lvl - thr) / ratio
                    dot(sn, X(lvl), Y(min(0.0, dout)), 3, pal(RED))
        elif mod == "GATE":
            thr = self.wid("gth").get_value()
            box(sn, x, y + h - 6, w, 6, pal(GRID, 0.12))
            f = max(0.0, min(1.0, (lvl + 84) / 84.0))
            box(sn, x, y + h - 6, w * f, 6, acc)
            tx = x + max(0.0, min(1.0, (thr + 84) / 84.0)) * w
            box(sn, tx - 1, y + h - 11, 2, 11, pal(AMBER))
            a = min(1.0, -gr.get("gate", 0.0) / 24.0)
            if on and a > 0.01:
                box(sn, x + w * (1 - a), y, w * a, h - 9, pal(AMBER, 0.55))
        elif mod == "LIM":
            thr = self.wid("lth").get_value()
            box(sn, x, y + h - 6, w, 6, pal(GRID, 0.12))
            tx = x + max(0.0, min(1.0, (thr + 40) / 40.0)) * w
            box(sn, tx - 1, y + h - 11, 2, 11, pal(RED))
            a = min(1.0, -gr.get("lim", 0.0) / 12.0)
            if on and a > 0.01:
                box(sn, x + w * (1 - a), y, w * a, h - 9, pal(RED, 0.5))
        else:                                     # HPF
            hz = self.win.hpf_hz(self.channel)
            pts = []
            for i in range(0, int(w) + 1, 3):
                f = 20.0 * (24000 / 20.0) ** (i / max(w, 1))
                # 2nd-order response, so the drawn slope matches the real filter
                r = (f / hz) ** 2
                mag = 20 * math.log10(max(r / math.sqrt(1 + r * r), 1e-3))
                pts.append((x + i, y + h - max(0.0, min(1.0, (mag + 24) / 24.0)) * h))
            polyline(sn, pts, acc, 1.8)
            t = "bypassed" if hz <= 25 else "%.0f Hz" % hz
            label(self, sn, t, x + w - len(t) * 5.2, y - 1,
                  pal(INK, 0.75 if on else 0.4), 8)

    def paint(self, sn, w, h):
        col = self.get_color()
        n = len(self.order)
        uw = w / n
        gr = self.win.ctl.snap["gr"].get(self.channel, {})
        state = {"HPF": self.win.hpf_hz(self.channel) > 25,
                 "GATE": self.win.dyn_for(self.channel)["gate"],
                 "COMP": self.win.dyn_for(self.channel)["comp"],
                 "EQ": (self.win._alt_eq(self.channel)["on"]
                        if self.win._alt_eq(self.channel) is not None
                        else self.win.eq_enabled(self.channel)),
                 "LIM": self.win.dyn_for(self.channel)["lim"]}
        for i, mod in enumerate(self.order):
            x = i * uw
            drag = (i == self.drag)
            dx = getattr(self, "_dx", 0) if drag else 0
            px = x + dx
            on = state.get(mod, False)
            # An active unit glows and an inactive one does not — the glow is
            # the state readout, so it must never appear on something that is
            # switched off. Real blur rather than stacked translucent boxes:
            # the halos were hard-coded GREEN and no longer matched the
            # channel's own colour once the palette arrived.
            # Only ONE module is on screen now, so the outline must match the
            # channel too — otherwise both racks would claim to be the one you
            # are looking at.
            sel = getattr(self.win, "current_module", None) == (self.channel, mod)
            if on:
                glow(sn, lambda px=px: box(sn, px + 5, 4, uw - 10, h - 8,
                                           pal(chan_col(self.channel), 0.22)),
                     radius=9.0)
            # the section currently on screen gets an outline, so the rack also
            # says where you are, not just what is on
            if sel:
                for ex, ey, ew, eh in ((px + 4, 3, uw - 8, 1),
                                       (px + 4, h - 4, uw - 8, 1),
                                       (px + 4, 3, 1, h - 6),
                                       (px + uw - 5, 3, 1, h - 6)):
                    box(sn, ex, ey, ew, eh, pal(ACCENT, 0.55))
            # Faceplates must read as hardware at a glance: at 6% alpha the
            # rack was a ghost of itself and the page's main navigator looked
            # disabled. Off units are clearly present, on units clearly lit.
            box(sn, px + 5, 4, uw - 10, h - 8,
                pal(chan_col(self.channel), 0.24) if on else pal(GRID, 0.13))
            box(sn, px + 5, 4, uw - 10, 22,
                pal(chan_col(self.channel), 0.34) if on else pal(GRID, 0.20))
            # a hairline frame so each unit holds its own edge
            for ex, ey, ew, eh in ((px + 5, 4, uw - 10, 1),
                                   (px + 5, h - 5, uw - 10, 1),
                                   (px + 5, 4, 1, h - 8),
                                   (px + uw - 6, 4, 1, h - 8)):
                box(sn, ex, ey, ew, eh,
                    pal(chan_col(self.channel), 0.38) if on else pal(GRID, 0.24))
            if i == self.hover and not drag:
                box(sn, px + 5, 4, uw - 10, 2, pal(chan_col(self.channel), 0.9))
                box(sn, px + 5, h - 6, uw - 10, 2, pal(chan_col(self.channel), 0.9))
            # power LED, with its own halo when lit
            if on:
                dot(sn, px + 15, 15, 7, pal(chan_col(self.channel), 0.25))
            dot(sn, px + 15, 15, 4, pal(chan_col(self.channel)) if on else pal(INK, 0.28))
            label(self, sn, self.TITLES[mod], px + 25, 8,
                  pal(INK, 0.95 if on else 0.65), 9)
            if mod in self.FIXED:
                label(self, sn, "\u25ac", px + uw - 20, 8, pal(INK, 0.25), 8)
            self._mini(sn, mod, px + 12, 32, uw - 24, h - 60, col, on)
            # gain reduction read-out
            key = {"GATE": "gate", "COMP": "comp", "LIM": "lim"}.get(mod)
            if key and on and gr.get(key, 0.0) < -0.1:
                t = "%.1f dB" % gr[key]
                label(self, sn, t, px + uw / 2 - len(t) * 2.6, h - 20,
                      pal(AMBER), 8)
            if i < n - 1:
                polyline(sn, [(x + uw - 5, h / 2), (x + uw + 5, h / 2)],
                         pal(INK, 0.28), 1.5)


# --------------------------------------------------------------------------
# a control that follows the hardware without fighting the user's hand
# --------------------------------------------------------------------------
class AutoGain:
    """Continuous Host-side automatic preamp gain for one input or a linked pair.

    Universal Control's Automatic Preamp Gain is a per-input switch, but the
    io24 firmware has no descriptor behind `autogain`, so writing it does
    nothing (re/UCNET_SHIM_SPEC.md 4c): automatic gain is a host feature.
    While the switch is on, this watches the JaSt input meter (slots 4 and 6)
    and moves the preamp toward the target by itself, with no listening step
    and nothing to confirm.

    It steers the 95th percentile of the last few seconds of readings, so the
    gaps between words do not count and one stray peak does not pull it
    around. The meter reads the converter, after the analog preamp, so a dB of
    gain moves it by a dB. It corrects only outside a small deadband, at most
    once a second, rising slowly and falling faster, and a reading at clip
    drops the gain at once. It never lifts silence or a steady noise floor: a
    rise needs the spread between loud and quiet moments that real playing or
    speech has. Every change clears the window, because those readings
    describe the old gain. Linked inputs share one controller, and the louder
    one sets the single gain for both.
    """

    TARGET_DB = -12.0        # about 12 dB of headroom above the performance
    DEADBAND_DB = 3.0
    WINDOW_S = 3.0
    MIN_READINGS = 20
    SETTLE_S = 0.4           # a change takes a moment to reach the meter
    HOLD_S = 1.0             # minimum spacing between corrections
    MAX_RISE_DB = 2.0
    MAX_FALL_DB = 4.0
    CLIP_DB = -0.5
    CLIP_FALL_DB = 6.0
    SILENT_DB = -60.0
    MIN_SPREAD_DB = 10.0     # loud-versus-quiet spread a rise requires
    GAIN_MIN_DB, GAIN_MAX_DB = 0.0, 60.0

    def __init__(self, channels, target_db=TARGET_DB):
        self.channels = tuple(int(c) for c in channels)
        if not self.channels:
            raise ValueError("auto gain needs at least one channel")
        self.target_db = float(target_db)
        self.readings = {c: [] for c in self.channels}     # (time, dBFS)
        self.settle_until = float("-inf")
        self.next_change = float("-inf")

    @staticmethod
    def _percentile(values, fraction):
        ordered = sorted(values)
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]

    def step(self, now, levels, gains):
        """Take one meter reading; return the gain to set now, or None."""
        if now < self.settle_until:
            return None
        heard = {c: float(levels[c]) for c in self.channels
                 if levels.get(c) is not None and math.isfinite(levels[c])}
        if not heard:
            return None
        loudest = max(sorted(heard), key=lambda c: heard[c])
        if heard[loudest] >= self.CLIP_DB:
            return self._change(now, gains[loudest], -self.CLIP_FALL_DB)
        horizon = now - self.WINDOW_S
        for c, value in heard.items():
            window = self.readings[c]
            window.append((now, value))
            while window and window[0][0] < horizon:
                window.pop(0)
        if now < self.next_change:
            return None
        levels95 = {c: self._percentile([v for _t, v in window], 0.95)
                    for c, window in self.readings.items()
                    if len(window) >= self.MIN_READINGS}
        if not levels95:
            return None
        loud = max(sorted(levels95), key=lambda c: levels95[c])
        level = levels95[loud]
        if level < self.SILENT_DB:
            return None
        error = self.target_db - level
        if abs(error) <= self.DEADBAND_DB:
            return None
        if error > 0:
            quiet = self._percentile([v for _t, v in self.readings[loud]], 0.10)
            if level - quiet < self.MIN_SPREAD_DB:
                return None                  # steady noise or hum: never lift it
        delta = max(-self.MAX_FALL_DB, min(self.MAX_RISE_DB, error))
        return self._change(now, gains[loud], delta)

    def _change(self, now, gain, delta):
        new = round(max(self.GAIN_MIN_DB, min(self.GAIN_MAX_DB, gain + delta)), 1)
        if new == round(gain, 1):
            return None                      # already at the end of the range
        self.settle_until = now + self.SETTLE_S
        self.next_change = now + self.HOLD_S
        for window in self.readings.values():
            window.clear()
        return new


class ProcessingMix:
    """One channel's DSP amount and Bypass, composed onto one device value.

    The firmware keeps a single processing scalar per channel, wire id 4
    (`input1FxMix` / `input2FxMix`): exact zero bypasses the whole channel
    chain and any positive value is the amount. Universal Control's schema
    shows the same thing as two controls -- `dspAmount`, whose minimum sits just
    above zero, and a `bypassDSP` toggle -- and neither has a device binding of
    its own (re/UCNET_SHIM_SPEC.md 4f). So the Host composes them: zero is
    written while bypassed and the amount otherwise, and the amount survives a
    bypass instead of being lost to it.

    Callers read and adopt the composed value exactly as they did the single
    slider this replaces. Adopting only moves the widgets; the window raises
    `_adopt_mute` around it so nothing is sent.
    """

    AMOUNT_MIN = 0.001

    def __init__(self, amount, bypass):
        self.amount = amount
        self.bypass = bypass

    @staticmethod
    def label(value):
        pct = value * 100.0
        return "%.0f %%" % pct if pct >= 9.95 else "%.1f %%" % pct

    def bypassed(self):
        return bool(self.bypass.get_active())

    def get_value(self):
        return 0.0 if self.bypassed() else float(self.amount.get_value())

    def set_value(self, value):
        value = max(0.0, min(1.0, float(value)))
        if value > 0.0:
            self.amount.set_value(max(self.AMOUNT_MIN, value))
        self.bypass.set_active(value == 0.0)


class Throttle:
    """Leading-edge throttle with a guaranteed trailing send.

    A debounce was used here first and it was the whole reason the hardware felt
    slow and choppy: while a fader is moving, every new value cancels the
    pending write, so exactly ONE write goes out — after the gesture ends. A
    1 s drag produced one device write at t=1.04 s. This sends immediately and
    then at most every `interval`, so the hardware tracks the fader live, and
    the trailing timer makes sure the final resting value is never lost.
    """

    def __init__(self, fn, interval=0.002):
        self.fn = fn
        self.interval = interval
        self.last = 0.0
        self.pending = 0
        self.latest = None

    def __call__(self, v):
        self.latest = v
        now = time.monotonic()
        if now - self.last >= self.interval:
            self.last = now
            self.fn(v)
            return
        if not self.pending:
            wait = int(max(1, (self.interval - (now - self.last)) * 1000))
            self.pending = GLib.timeout_add(wait, self._fire)

    def _fire(self):
        self.pending = 0
        self.last = time.monotonic()
        self.fn(self.latest)
        return False


class Live:
    GRACE = 0.35                                 # seconds after a touch

    def __init__(self, widget, push, prop="value"):
        self.w = widget
        self.push = push
        self.prop = prop
        self.touched = 0.0
        self.mute = False
        self.pending = 0
        self._thr = Throttle(self.push, 0.002)
        if prop == "value":
            widget.connect("value-changed", self._changed)
        else:
            widget.connect("notify::active", self._changed)

    def _changed(self, *_a):
        if self.mute:
            return
        self.touched = time.monotonic()
        v = self.w.get_value() if self.prop == "value" else self.w.get_active()
        self._thr(v)

    def pull(self, v):
        """Adopt a value the device reports, unless the user is mid-gesture."""
        if time.monotonic() - self.touched < self.GRACE:
            return
        cur = self.w.get_value() if self.prop == "value" else self.w.get_active()
        if self.prop == "value":
            if abs(cur - v) < 1e-4:
                return
        elif cur == v:
            return
        self.mute = True
        (self.w.set_value if self.prop == "value" else self.w.set_active)(v)
        self.mute = False


def editable_db(scale, width_chars=5):
    """A value label you can click and type into.

    Gtk.EditableLabel: shows the number, click to edit, Enter commits. The
    scale stays the fast path; typing is for exact values. Guarded both ways
    so slider->label updates don't re-enter as label->slider writes.
    """
    lbl = Gtk.EditableLabel(text="%.1f" % scale.get_value())
    lbl.set_width_chars(width_chars)
    lbl.add_css_class("numeric")
    lbl.add_css_class("caption")
    state = {"mute": False}

    def scale_moved(w):
        if not lbl.get_editing():
            state["mute"] = True
            lbl.set_text("%.1f" % w.get_value())
            state["mute"] = False
    scale.connect("value-changed", scale_moved)

    def committed(l, _p):
        if state["mute"] or l.get_editing():
            return
        try:
            v = float(l.get_text().replace(",", "."))
        except ValueError:
            state["mute"] = True
            l.set_text("%.1f" % scale.get_value())
            state["mute"] = False
            return
        adj = scale.get_adjustment()
        scale.set_value(max(adj.get_lower(), min(adj.get_upper(), v)))
    lbl.connect("notify::editing", committed)
    return lbl


def reset_on_double_click(scale, default, label_fn=None):
    """Double-click a fader to return it to its designed default.

    Lives here rather than inside _srow because five faders are built directly
    — preamp gain, bus master, the routing sends, the output delay — and those
    are the ones people ride hardest. Having the gesture on some sliders and not
    others is worse than not having it at all, because the ones that ignore you
    read as broken.
    """
    g = Gtk.GestureClick()
    g.set_button(1)
    # CAPTURE phase: in BUBBLE (the default) the Scale's own click-to-jump
    # handling claims the sequence first and the second click never arrives —
    # which is why double-click reset silently did nothing on the matrix.
    g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def hit(gesture, n_press, x, y):
        if n_press == 2:
            scale.set_value(default)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
    g.connect("pressed", hit)
    scale.add_controller(g)
    try:
        scale.set_tooltip_text("Double-click to reset to %s"
                               % (label_fn(default) if label_fn else default))
    except Exception:
        pass
    return scale


def fader(lo, hi, step, vertical=True):
    sc = Gtk.Scale.new_with_range(
        Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
        lo, hi, step)
    sc.set_draw_value(False)
    if vertical:
        sc.set_inverted(True)
        sc.set_vexpand(True)
    else:
        sc.set_hexpand(True)
    return sc


class Win(Adw.ApplicationWindow):
    def __init__(self, app, ctl):
        super().__init__(application=app, title="Revelator io24",
                         default_width=1040, default_height=800)
        # Ties the window to org.io24.Mixer.desktop, so the shell shows the
        # installed icon and groups the window under that entry rather than
        # inventing a second generic one.
        self.set_icon_name("org.io24.Mixer")
        self.ctl = ctl
        # The fat channel is per channel on the hardware, so keep a full set of
        # state for each and let the page switch between them — or drive both at
        # once, which the original software never offered.
        self.bands_by_ch = {
            c: io24_presets.default_standard_eq_bands() for c in (1, 2)
        }
        # fatchannelxt.xml has one EQ On toggle plus four independent band On
        # toggles.  Keeping this separately is what lets bypass preserve the
        # complete curve instead of flattening it destructively.
        self.eq_on_by_ch = {1: False, 2: False}
        # ``None`` means Standard. Alternate entries hold the normalized UC XML
        # state plus its exact cached live sections for graphing and replay.
        self.alt_eq_by_ch = {1: None, 2: None}
        self._alt_eq_warned = None
        self.dyn_by_ch = {c: {"gate": False, "comp": False, "lim": False}
                          for c in (1, 2)}
        self.cur_band_by_ch = {1: 0, 2: 0}
        self.link_both = False
        # Multiband, the compressor type that runs on the computer
        # (io24_mbc's insert): its process, per-channel controls, and what it
        # changed in the unit's mixer and in PipeWire, so it can be undone.
        self.insert = io24_mbc.InsertChain()
        self.mbc_ctl = {}
        self.mbc_status = {}
        self._mbc_mute = False
        self._mbc_prev = {}
        self._insert_routing = None
        self._insert_quantum_before = None
        self._insert_default_input = False
        self._insert_stale = False
        self._insert_pending = 0
        # A distinct Host spring tank: wet-only Input 1/2 capture returned on
        # the best stereo pair exposed by the active io24 playback profile.
        # This does not replace or relabel the unit's block-202 reverb.
        self.spring = io24_spring.SpringChain()
        self._spring_routing = None
        self._spring_mute = False
        self._spring_route_pending = False
        # Widget adoption is not a user edit.  Factory loads, band selection,
        # and per-channel page changes all populate controls programmatically;
        # without one shared guard those GTK signals enqueue fresh DSP writes
        # and can overwrite the state that was just loaded.
        self._adopt_mute = False
        self._rev_mute = False
        self._order_mute = False
        self._fx_mute = False
        self._phones_source_mute = False
        self.device_slot_registry = DeviceSlotRegistry(
            DEVICE_SLOT_REGISTRY_PATH)
        self.device_preset_library_registry = DevicePresetLibraryRegistry(
            DEVICE_PRESET_LIBRARY_REGISTRY_PATH)
        self._fx_push_id = None
        # Block 201 is one shared processor, assigned to one physical input by
        # processingChannel. The assignment exchanges the device's processing
        # permutation, so remember the confirmed target and do not resend it
        # for every slider movement.
        self._fx_last_sent_device = None
        self._fx_last_sent_target = None
        # Firmware model 5 is never selected at 96 kHz.  At that rate the
        # visible Delay state is hosted by the same PipeWire insert as
        # Multiband; this remembers which attached unit has already had its
        # hardware Voice FX block safely bypassed.
        self._host_delay_quiesced_device = None
        # A saved or newly selected 96 kHz clock is held at a known-safe old
        # rate while the interface is absent. On attach, block 201 is
        # quiesced before the requested clock is allowed to move.
        self._audio_clock_restore_deferred = False
        self._audio_clock_restore_inflight = False
        # Every coefficient this Host computes — biquads, gate/comp/limiter
        # time constants, reverb, the rate-dependent Voice FX filters — is a
        # function of the device clock. Nothing used to pass one, so all of it
        # was built for 48 kHz whatever the interface was actually running at.
        clock = audio_clock_preference(load_last_session())
        self._selected_rate = clock["sample_rate"]
        self._selected_quantum = clock["quantum"]
        self._fs = float(self._selected_rate)
        self._fs_seen = False
        # The unit's own Preset button runs the same Stat load, so a slot the
        # device reports moving is a recall this Host did not perform. Seed it
        # empty: the resting index at attach says which slot is selected, not
        # that a load has been run this session.
        self._observed_preset_slot = {}
        self.order_by_ch = {1: False, 2: False}
        self.spectrum = None
        self._frames = []
        self._curve = None
        self._curve_key = None
        self.hpf_by_ch = {1: 24.0, 2: 24.0}
        self.bus_sources = io24_mbc.BusSourceManager()
        self._bus_source_status = {
            bus: "unavailable" for bus in io24_mbc.BUS_SOURCE_KEYS
        }
        self.live = []
        self.meters = {}

        stack = Adw.ViewStack()
        self.stack = stack
        stack.add_titled_with_icon(self._mixer_page(), "mix", "Mixer",
                                   "audio-volume-high-symbolic")
        stack.add_titled_with_icon(self._channel_page(), "ch", "Fat channel",
                                   "applications-utilities-symbolic")
        stack.add_titled_with_icon(self._fx_page(), "fx", "Effects",
                                   "media-optical-symbolic")
        stack.add_titled_with_icon(self._routing_page(), "route", "Routing",
                                   "network-workgroup-symbolic")
        stack.add_titled_with_icon(self._device_page(), "dev", "Device",
                                   "preferences-system-symbolic")
        stack.add_titled_with_icon(self._presets_page(), "pre", "Presets",
                                   "view-list-symbolic")

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(
            stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        self.status = Gtk.Label(label="")
        self.status.add_css_class("dim-label")
        header.pack_start(self.status)
        menu = Gio.Menu()
        menu.append("Save snapshot…", "win.save")
        menu.append("Load snapshot…", "win.load")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                       menu_model=menu))
        for nm, fn in (("save", self.on_save), ("load", self.on_load)):
            a = Gio.SimpleAction.new(nm, None)
            a.connect("activate", fn)
            self.add_action(a)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(stack)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        # Offline is a state, not an error: the app opens without the
        # interface and attaches live when it is plugged in. The banner is
        # the one place that says which mode you are in.
        self.offline_banner = Adw.Banner(
            title="Revelator io24 not connected")
        self.offline_banner.set_revealed(self.ctl.dev is None)
        tv.add_top_bar(self.offline_banner)
        self._was_offline = self.ctl.dev is None
        # Reconnect recovery is intentionally not presented as a standing
        # question. The durable write-only shadow is replayed once per attach;
        # Host snapshots remain the explicit named save/load path.
        tv.set_content(self.toasts)
        self.set_content(tv)
        # The spectrum is the only part of this app that opens an audio stream,
        # and sustained capture alongside control traffic is what has twice put
        # the device into its bootloader (PROTOCOL.md 12G/12I). So it runs only
        # while a page that draws it is actually on screen.
        self.stack = stack
        try:
            self.spectrum = Spectrum(autostart=False)
        except Exception:
            self.spectrum = None
        stack.connect("notify::visible-child-name", self._page_changed)
        GLib.timeout_add(33, self._tick)                 # 30 fps
        GLib.timeout_add(2000, self._spectrum_watchdog)
        GLib.timeout_add(50, self._fx_anim)             # 20 fps, page-gated
        GLib.timeout_add(700, self._watch_attach)
        GLib.timeout_add(2000, self._bus_source_watchdog)
        GLib.timeout_add(2000, self._insert_watchdog)
        GLib.timeout_add(2000, self._spring_watchdog)
        GLib.idle_add(self._restore_audio_clock)
        GLib.timeout_add(5000, self._autosave_session)
        GLib.idle_add(self._sync_mix_widgets)           # show cache/unknown honestly
        self._page_changed(stack, None)                  # set the initial state

    # Pages whose drawing actually uses the spectrum backdrop.
    SPECTRUM_PAGES = ("ch",)

    def _watch_attach(self):
        """Track hotplug and report only connection-state transitions."""
        off = self.ctl.dev is None
        if off:
            err = self.ctl.snap.get("error")
            self.offline_banner.set_title(
                "io24 is connected but held by another program"
                if err and "busy" in err else
                "Revelator io24 not connected")
        if off != self._was_offline:
            self._was_offline = off
            self.offline_banner.set_revealed(off)
            if off:
                self.say("Revelator io24 disconnected")
            else:
                self.say("Revelator io24 connected")
                GLib.idle_add(self._sync_mix_widgets)
        return True

    def _fx_anim(self):
        """Advance the FX signature. Only while its page is visible — an
        animation running behind a hidden page is pure battery drain."""
        if self.stack.get_visible_child_name() != "fx":
            return True
        for name in ("fx_visual", "rev_visual"):
            v = getattr(self, name, None)
            if v is not None:
                v.tick()
        return True

    def _page_changed(self, stack, _p):
        """Run the capture only on pages that draw it."""
        if not self.spectrum:
            return
        name = stack.get_visible_child_name()
        if name in self.SPECTRUM_PAGES:
            if not getattr(self, "_spectrum_blocked", False):
                self.spectrum.start()
        else:
            self.spectrum.pause()

    def _spectrum_watchdog(self):
        """Kill a capture that has gone quiet, and do not silently retry forever.

        A wedged arecord would otherwise hold the card open with nothing to show
        for it. One restart is worth trying; a second stall means something is
        actually wrong, so it stays off and says so rather than fighting the
        device in a loop.
        """
        s = self.spectrum
        if not s or not s.stalled():
            return True
        s.pause()
        n = getattr(self, "_spectrum_stalls", 0) + 1
        self._spectrum_stalls = n
        if n >= 2:
            self._spectrum_blocked = True
            self.say("Spectrum stopped — the capture stream stalled twice")
        else:
            self.say("Spectrum stalled, restarting")
            self._page_changed(self.stack, None)
        return True

    def say(self, msg):
        """One message at a time.

        Tolerates being called before the toast overlay exists: a status message
        is never worth aborting a page build for, and construction-time callers
        (a widget seeding its initial state) legitimately arrive early.

        Adw.ToastOverlay queues by default, so rapid feedback (dragging a rack,
        clicking modules) piled up into a backlog that kept popping long after
        the action. Dismiss the outgoing one and keep the timeout short.
        """
        if getattr(self, "toasts", None) is None:
            return
        prev = getattr(self, "_toast", None)
        if prev is not None:
            try:
                prev.dismiss()
            except Exception:
                pass
        t = Adw.Toast(title=msg)
        t.set_timeout(2)
        self._toast = t
        self.toasts.add_toast(t)

    def set_hpf(self, ch, hz):
        """Set the digital Fat Channel HPF and keep its one UI in sync."""
        hz = max(24.0, min(1000.0, hz))
        targets = (1, 2) if self.link_both else (ch,)
        send = not self._adopt_mute
        for c in targets:
            self.hpf_by_ch[c] = hz
            if send:
                self.ctl.submit(lambda dev, x=c, fs=self._fs:
                                dev.set_highpass_freq(x, hz, fs=fs))
        prior = self._adopt_mute
        self._adopt_mute = True
        try:
            for c in targets:
                self._adopt_hpf_controls(c, hz)
        finally:
            self._adopt_mute = prior
        for r in self.racks.values():
            r.queue_draw()

    @staticmethod
    def _hpf_basic_index(hz):
        for index, basic_hz in enumerate(HPF_BASIC_HZ):
            if abs(float(hz) - basic_hz) < 0.02:
                return index
        return len(HPF_BASIC_HZ)

    def _adopt_hpf_controls(self, ch, hz):
        """Adopt one intended digital cutoff without crossing channels."""
        hz = max(24.0, min(1000.0, float(hz)))
        self.hpf_by_ch[ch] = hz
        controls = self.w.get(ch, {})
        if "hpf" not in controls:
            return
        controls["hpf"].set_value(hz)
        index = self._hpf_basic_index(hz)
        mode = controls.get("hpf_mode")
        if mode is not None:
            mode.set_selected(index)
        controls["hpf"].set_sensitive(index == len(HPF_BASIC_HZ))

    def _hpf_mode_changed(self, row, _param, ch):
        """Choose Off/40/80/160 or unlock the one advanced cutoff control."""
        if self._adopt_mute:
            return
        index = row.get_selected()
        if index < len(HPF_BASIC_HZ):
            self.set_hpf(ch, HPF_BASIC_HZ[index])
        elif "hpf" in self.w.get(ch, {}):
            self.w[ch]["hpf"].set_sensitive(True)

    def show_module(self, ch, mod):
        """Show one module, for one channel — switching both stacks together."""
        st = getattr(self, "mod_stacks", {}).get(ch)
        if st is None:
            return
        if getattr(self, "chan_stack", None) is not None:
            self.chan_stack.set_visible_child_name(str(ch))
            self._sync_order_row(ch)
        if st.get_child_by_name(mod) is not None:
            st.set_visible_child_name(mod)
        if getattr(self, "mod_title", None) is not None:
            self.mod_title.set_text("Channel %d — %s"
                                    % (ch, Rack.TITLES.get(mod, mod)))
        self.current_module = (ch, mod)
        for r in self.racks.values():
            r.queue_draw()

    def eq_enabled(self, ch):
        """The complete EQ switch, independent of every band's switch."""
        alternate = self._alt_eq(ch)
        if alternate is not None:
            return bool(alternate["on"])
        states = getattr(self, "eq_on_by_ch", None)
        if states is not None and ch in states:
            return bool(states[ch])
        # Compatibility for small test doubles and older in-process state.
        return any(band.get("shape") != "off"
                   for band in getattr(self, "bands_by_ch", {}).get(ch, ()))

    def _standard_band_mode(self, ch, index):
        return io24_presets.standard_band_mode(
            self.bands_by_ch[ch][index], index)

    def _effective_eq_band(self, ch, index):
        """One stored band reduced to the coefficient state sent right now."""
        band = dict(self.bands_by_ch[ch][index])
        if not self.eq_enabled(ch) or not \
                io24_presets.standard_band_enabled(band):
            band["shape"] = "off"
        else:
            band["shape"] = io24_presets.standard_band_mode(band, index)
        return band

    def _set_eq_enabled(self, ch, on):
        """Switch the selected EQ model without changing its hidden controls."""
        on = bool(on)
        targets = self._eq_write_targets(ch)
        if not hasattr(self, "eq_on_by_ch"):
            self.eq_on_by_ch = {}
        for target in targets:
            controls = getattr(self, "w", {}).get(target, {})
            switch = controls.get("eq_on")
            if switch is not None and switch.get_active() != on:
                prior = self._adopt_mute
                self._adopt_mute = True
                try:
                    switch.set_active(on)
                finally:
                    self._adopt_mute = prior
            alternate = self._alt_eq(target)
            if alternate is not None:
                alternate["eq"]["eqallon"] = int(on)
                alternate["on"] = on
                self._queue_alternate_eq(target)
            elif on:
                self.eq_on_by_ch[target] = True
                for index in range(4):
                    band = self._effective_eq_band(target, index)
                    self.ctl.submit(
                        lambda dev, x=target, j=index, bb=band, fs=self._fs:
                        dev.set_eq_band(x, j, bb["shape"], bb["freq"],
                                        bb["gain"], bb["q"], fs=fs))
            else:
                self.eq_on_by_ch[target] = False
                self.ctl.submit(lambda dev, x=target: dev.eq_off(x))
            curve = controls.get("curve")
            if curve is not None:
                curve.queue_draw()
        self.invalidate_curve()
        for rack in getattr(self, "racks", {}).values():
            rack.queue_draw()

    def _eq_power_changed(self, row, _param, ch):
        if self._adopt_mute:
            return
        self._set_eq_enabled(ch, row.get_active())

    def toggle_module(self, ch, mod):
        """Click a rack unit to switch that module in or out."""
        if mod == "HPF":
            cur = self.hpf_by_ch.get(ch, 24.0)
            self.set_hpf(ch, 24.0 if cur > 25 else 120.0)
            return
        if mod == "EQ":
            self._set_eq_enabled(ch, not self.eq_enabled(ch))
            return
        key = {"GATE": "gate", "COMP": "comp", "LIM": "lim"}[mod]
        new = not self.dyn_by_ch[ch][key]
        self.dyn_by_ch[ch][key] = new
        self.w[ch][key + "_on"].set_active(new)
        for r in self.racks.values():
            r.queue_draw()

    def dyn_for(self, ch):
        return self.dyn_by_ch[ch]

    def hpf_hz(self, ch):
        return self.hpf_by_ch.get(ch, 24.0)

    def set_order(self, ch, eq_first):
        for c in ((1, 2) if self.link_both else (ch,)):
            self.ctl.submit(lambda dev, x=c: dev.set_comp_eq_order(x, eq_first))

    def response_for(self, ch, f, fs=48000.0, bands=None, enabled=None):
        if enabled is None:
            enabled = self.eq_enabled(ch)
        if not enabled:
            return 0.0
        alternate = self._alt_eq(ch)
        if alternate is not None:
            sections = alternate.get("sections")
            if not sections or alternate.get("error"):
                return 0.0
            return io24_alt_eq.response_db(
                sections, f, alternate.get("rate_hz", fs))
        re, im = 1.0, 0.0
        if bands is None:
            bands = tuple(dict(b) for b in self.bands_by_ch[ch])
        for b in bands:
            c = self._biquad(b, fs)
            if not c:
                continue
            b0, b1, b2, a1, a2 = c
            om = 2 * math.pi * f / fs
            c1, s1 = math.cos(-om), math.sin(-om)
            c2, s2 = math.cos(-2 * om), math.sin(-2 * om)
            nr, ni = b0 + b1 * c1 + b2 * c2, b1 * s1 + b2 * s2
            dr, di = 1 + a1 * c1 + a2 * c2, a1 * s1 + a2 * s2
            dd = dr * dr + di * di or 1e-12
            hr, hi = (nr * dr + ni * di) / dd, (ni * dr - nr * di) / dd
            re, im = re * hr - im * hi, re * hi + im * hr
        return 20 * math.log10(max(math.hypot(re, im), 1e-6))


    # ---------------------------------------------------------- mixer page
    def _strip(self, title=None, title_suffix=None):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.add_css_class("card")
        b.set_margin_top(10); b.set_margin_bottom(10)
        b.set_margin_start(3); b.set_margin_end(3)
        if title:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                           halign=Gtk.Align.CENTER)
            head.set_margin_top(10)
            lab = Gtk.Label(label=title)
            lab.add_css_class("heading")
            head.append(lab)
            if title_suffix is not None:
                head.append(title_suffix)
            b.append(head)
            self._strip_titles = getattr(self, "_strip_titles", {})
            self._strip_titles[title] = lab
        return b

    def _input_strip(self, ch):
        indicator = PresetIndicator()
        self.preset_indicators = getattr(self, "preset_indicators", {})
        self.preset_indicators[ch] = indicator
        box = self._strip("Channel %d" % ch, title_suffix=indicator)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.CENTER)
        row.set_size_request(-1, 260)      # a floor, not the height:
        row.set_vexpand(True)              # meters grow with the window
        m = Meter()
        self.meters["in%d" % ch] = m
        row.append(m)
        f = fader(0, 60, 0.05)
        reset_on_double_click(f, 0.0, lambda v: "%.1f dB" % v)
        row.append(f)
        box.append(row)

        val = Gtk.Label(label="0.0 dB")
        val.add_css_class("numeric")
        box.append(val)
        f.connect("value-changed", lambda w: val.set_text("%.1f dB" % w.get_value()))
        self.live.append(Live(f, lambda v, c=ch: self._set("gain", c, v)))
        self.gain_faders = getattr(self, "gain_faders", {})
        self.gain_faders[ch] = self.live[-1]
        auto = Gtk.ToggleButton(label="Auto")
        auto.set_margin_start(6); auto.set_margin_end(6)
        auto.set_tooltip_text("Set preamp gain automatically")
        auto.connect("toggled", self._autogain_toggled, ch)
        box.append(auto)
        self.autogain_toggles = getattr(self, "autogain_toggles", {})
        self.autogain_toggles[ch] = auto

        for label, param in (("48V", "phantom"), ("Mute", "mute"),
                             ("HPF", "hpf")):
            t = Gtk.ToggleButton(label=label)
            t.set_margin_start(6); t.set_margin_end(6)
            self.live.append(Live(t, lambda v, p=param, c=ch: self._set(p, c, v),
                                  prop="active"))
            if param == "phantom":
                self.phantom_live = getattr(self, "phantom_live", {})
                self.phantom_live[ch] = self.live[-1]
            elif param == "mute":
                self.mute_live = getattr(self, "mute_live", {})
                self.mute_live[ch] = self.live[-1]
            elif param == "hpf":
                t.set_tooltip_text("80 Hz input filter")
                self.hpf_live = getattr(self, "hpf_live", {})
                self.hpf_live[ch] = self.live[-1]
            box.append(t)

        g = Gtk.Label(label="Gain reduction")
        g.add_css_class("caption"); g.add_css_class("dim-label")
        g.set_margin_top(6)
        box.append(g)
        if not hasattr(self, "gr_bars"):
            self.gr_bars = {1: {}, 2: {}}
        if True:
            for key, nm in (("gate", "Gate"), ("comp", "Comp"), ("lim", "Lim")):
                r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                r.set_margin_start(10); r.set_margin_end(10)
                l = Gtk.Label(label=nm, xalign=0)
                l.add_css_class("caption"); l.set_size_request(38, -1)
                bar = GRBar()
                self.gr_bars[ch][key] = bar
                r.append(l); r.append(bar)
                box.append(r)
        box.append(Gtk.Box(height_request=8))   # keep the card's
        return box                             # edge off the rows

    def _bus_strip(self):
        box = self._strip("Buses")
        grid = Gtk.Grid(column_spacing=10, row_spacing=6,
                        column_homogeneous=True,
                        halign=Gtk.Align.CENTER)
        grid.set_size_request(228, 260)    # a floor, not the height
        grid.set_vexpand(True)
        for column, (key, visible_name, detail) in enumerate(
                bus_meter_columns()):
            header = Gtk.Label(label=visible_name, halign=Gtk.Align.CENTER)
            header.add_css_class("caption")
            header.add_css_class("dim-label")
            header.set_tooltip_text(detail)
            grid.attach(header, column, 0, 1, 1)
            pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            pair.set_halign(Gtk.Align.CENTER)
            pair.set_vexpand(True)
            for side in (0, 1):
                m = Meter(width=14, scale=(side == 1 and key == "mixb"))
                self.meters["%s%d" % (key, side)] = m
                pair.append(m)
            grid.attach(pair, column, 1, 1, 1)
        box.append(grid)
        box.append(Gtk.Box(height_request=8))
        return box

    def _set_bus_source_status(self, bus, status, notify=False):
        previous = self._bus_source_status.get(bus, "unavailable")
        self._bus_source_status[bus] = status
        if notify and status != previous:
            if status == "failed":
                self.say("%s source failed to start" %
                         io24_mbc.BUS_SOURCE_DESCRIPTIONS[bus])
            elif status == "profile":
                # not a failure: this card profile has no such capture channel
                self.say("%s needs the io24's Pro Audio profile; this one has "
                         "no such capture channel" %
                         io24_mbc.BUS_SOURCE_DESCRIPTIONS[bus])

    def _bus_source_watchdog(self):
        """Slowly reconcile source visibility; ordinary transitions are quiet."""
        capture = io24_mbc.find_io24_capture_source()
        statuses = self.bus_sources.reconcile(capture)
        for bus in io24_mbc.BUS_SOURCE_KEYS:
            self._set_bus_source_status(
                bus, statuses[bus],
                notify=statuses[bus] in ("failed", "profile"))
        return True

    def _master_strip(self):
        box = self._strip("Monitoring")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14,
                      halign=Gtk.Align.CENTER)
        row.set_size_request(-1, 260)
        self.mon = {}
        for key, nm in (("mainvol", "Main"), ("hp", "Phones")):
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            f = fader(0, 1, 0.002)
            reset_on_double_click(f, 0.75, lambda v: "%.0f %%" % (v * 100))
            v = Gtk.Label(label="0.00"); v.add_css_class("numeric")
            f.connect("value-changed", lambda w, l=v: l.set_text("%.2f" % w.get_value()))
            param = "mainvol" if key == "mainvol" else "hpvol"
            self.live.append(Live(f, lambda x, p=param: self._set(p, None, x)))
            self.mon[key] = self.live[-1]
            c.append(f); c.append(v)
            lab = Gtk.Label(label=nm)
            lab.add_css_class("caption"); lab.add_css_class("dim-label")
            c.append(lab)
            row.append(c)
        box.append(row)

        source_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        source_row.set_margin_start(10); source_row.set_margin_end(10)
        source_label = Gtk.Label(label="Phones source", xalign=0, hexpand=True)
        source_label.add_css_class("caption")
        self.phones_source_row = Gtk.DropDown.new_from_strings(
            ["Main", "Mix A", "Mix B"])
        self.phones_source_row.set_selected(0)
        self.phones_source_row.set_tooltip_text("Choose the headphone mix")
        self.phones_source_row.connect(
            "notify::selected", self._phones_source_changed)
        source_row.append(source_label); source_row.append(self.phones_source_row)
        box.append(source_row)

        # Blend is a balance between two sources, not a level — the same shape of
        # control as pan, so it gets the same knob. Centre is an equal mix, which
        # a dial shows at a glance and a horizontal slider does not.
        br = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        br.set_margin_start(10); br.set_margin_end(10)
        bl = Gtk.Label(label="Blend  playback ←→ direct")
        bl.add_css_class("caption"); bl.add_css_class("dim-label")
        br.append(bl)
        krow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                       halign=Gtk.Align.CENTER)
        blbl = Gtk.Label(label="even")
        blbl.add_css_class("numeric"); blbl.add_css_class("dim-label")
        blbl.set_size_request(58, -1)

        def blend_text(v):                       # v is 0..1 on the knob
            d = v * 2.0 - 1.0                    # device takes -1..+1
            if abs(d) < 0.01:
                return "even"
            return ("play %d%%" if d < 0 else "direct %d%%") % round(abs(d) * 100)

        bthr = Throttle(lambda v: self._set("blend", None, v * 2.0 - 1.0), 0.010)

        def blend_moved(v, l=blbl):
            l.set_text(blend_text(v))
            if not getattr(self, "_dev_mute", False):
                bthr(v)

        self.blend_knob = Knob(0.5, 60, blend_moved, label="blend")
        krow.append(self.blend_knob); krow.append(blbl)
        br.append(krow)
        box.append(br)
        self.mainmute_row = Gtk.Label(
            label="Main output: On", xalign=0.5)
        self.mainmute_row.add_css_class("caption")
        self.mainmute_row.set_margin_top(4)
        self.mainmute_row.set_tooltip_text(
            "Follows the interface's Mute button")
        box.append(self.mainmute_row)
        for nm, p in (("Output mute", "hpmute"), ("Stereo link", "link")):
            t = Gtk.ToggleButton(label=nm)
            t.set_margin_start(10); t.set_margin_end(10)
            self.live.append(Live(t, lambda v, pp=p: self._set(pp, None, v),
                                  prop="active"))
            self.mon[p] = self.live[-1]
            box.append(t)
        box.append(Gtk.Box(vexpand=True))
        return box

    def _mixer_page(self):
        b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                    homogeneous=False)
        b.set_margin_start(6); b.set_margin_end(6)
        strips = (self._input_strip(1), self._input_strip(2),
                  self._bus_strip(), self._master_strip())
        for index, w in enumerate(strips):
            w.set_hexpand(index < 2)
            b.append(w)
        sc = Gtk.ScrolledWindow(); sc.set_child(b)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        return sc

    # -------------------------------------------------------- channel page
    def _channel_page(self):
        """Both channels' fat channels, side by side and independent.

        The hardware has a complete strip per input, so showing one at a time
        was an artificial limit of the original software rather than of the
        device. Linking makes channel 2 mirror channel 1 and collapses the two
        racks into one.
        """
        self.w = {1: {}, 2: {}}
        self.columns = {}
        self.col_titles = {}
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # ONE continuous page. The old build nested Adw.PreferencesPages —
        # each carrying its own ScrolledWindow — inside the outer scroll, so
        # the module area scrolled separately and the page read as two pages
        # stitched together. That was an artifact of the original two-column
        # bichannel layout. Now: one clamp, one column, one scroll.
        self.chain_group = Adw.PreferencesGroup(title="Signal chain")

        # Stereo link and chain order live in the Signal chain header instead
        # of their own full-height group — two rows of chrome became one line.
        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lk_lab = Gtk.Label(label="Stereo link")
        lk_lab.add_css_class("caption"); lk_lab.add_css_class("dim-label")
        self.link_row = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.link_row.set_tooltip_text("Link the inputs as a stereo pair")
        self.link_row.connect("notify::active", self._link_changed)
        order = Gtk.DropDown.new_from_strings(["Compressor → EQ",
                                               "EQ → Compressor"])
        order.set_tooltip_text("Set compressor and EQ order")
        order.connect("notify::selected", self._order_changed)
        self.order_row = order
        suffix.append(lk_lab); suffix.append(self.link_row); suffix.append(order)
        self.chain_group.set_header_suffix(suffix)

        self.chain_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.racks = {}
        self.chain_group.add(self.chain_box)
        outer.append(self.chain_group)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.set_margin_top(10)
        self.mod_title = Gtk.Label(label="Channel 1 — Equaliser", xalign=0)
        self.mod_title.add_css_class("title-4")
        head.append(self.mod_title)
        outer.append(head)

        self.chan_stack = Gtk.Stack()
        # not vhomogeneous: a Stack reserves its tallest child's height, which
        # left a page-sized void under short modules
        self.chan_stack.set_vhomogeneous(False)
        self.chan_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.chan_stack.set_transition_duration(110)
        for ch in (1, 2):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            title = Gtk.Label(label="Channel %d" % ch)      # kept: _sync marks
            title.add_css_class("caption")                  # the preset state on it
            title.add_css_class("dim-label")
            title.set_margin_top(2)
            self.col_titles[ch] = title
            col.append(title)
            col.append(self._channel_column(ch))
            self.columns[ch] = col
            self.chan_stack.add_named(col, str(ch))
        self.chan_stack.set_visible_child_name("1")
        outer.append(self.chan_stack)
        self._rebuild_chains()
        outer.set_margin_top(10); outer.set_margin_bottom(24)
        clamp = Adw.Clamp(maximum_size=PAGE_WIDTH,
                          tightening_threshold=PAGE_TIGHTEN)
        clamp.set_child(outer)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_child(clamp)
        sc.set_vexpand(True)
        return sc

    def _mbc_group(self, ch):
        """Channel `ch`'s Multiband, the compressor type that runs on the
        computer right after the unit's Fat Channel (io24_mbc's insert).

        Choosing it switches the unit's single-band compressor off and
        processes the input from USB capture, where it arrives after the
        limiter; GUIDE.md has the details. Each band carries the exact public
        control vocabulary of UC's Standard, Tube and FET models. Their existing
        io24_dsp builders compile those controls into one common tuple consumed
        by the Host's realtime LADSPA processor.
        """
        ctl = self.mbc_ctl.setdefault(ch, {})
        g = Adw.PreferencesGroup()
        status = Adw.ActionRow(title="Multiband", subtitle="Off")
        self.mbc_status[ch] = status
        g.add(status)
        missing = self._insert_missing()
        if missing:
            note = Adw.ActionRow(title="Unavailable", subtitle=missing)
            note.add_prefix(Gtk.Image.new_from_icon_name(
                "dialog-warning-symbolic"))
            g.add(note)

        def slider(parent, cid, title, lo, hi, step, dv, fmt, keys):
            r, sc = self._srow(title, lo, hi, step, dv, fmt,
                               lambda v, i=cid, k=keys:
                               self._mbc_set(ch, i, k, v))
            if isinstance(parent, Adw.ExpanderRow):
                parent.add_row(r)
            else:
                parent.add(r)
            ctl[cid] = sc

        def model_slider(parent, bi, cid, title, lo, hi, step, dv, fmt,
                         subtitle=None):
            r, sc = self._srow(
                title, lo, hi, step, dv, fmt,
                lambda _v, c=ch, b=bi, i=cid:
                    self._mbc_model_edit(c, b, i), subtitle)
            if isinstance(parent, Adw.ExpanderRow):
                parent.add_row(r)
            else:
                parent.add(r)
            ctl[(bi, cid)] = sc

        def model_switch(parent, bi, cid, title, subtitle=None, active=False):
            row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
            row.set_active(active)
            row.connect("notify::active", self._mbc_model_notify, ch, bi, cid)
            if isinstance(parent, Adw.ExpanderRow):
                parent.add_row(row)
            else:
                parent.add(row)
            ctl[(bi, cid)] = row

        # crossovers: each split drives every biquad and all-pass tuned to it
        XNODES = io24_mbc.crossover_nodes()
        for i, ttl in enumerate(("Split low / low-mid",
                                 "Split low-mid / high-mid",
                                 "Split high-mid / high")):
            lo, hi = ((40, 400), (300, 2500), (2000, 12000))[i]
            slider(g, "x%d" % i, ttl, lo, hi, 1,
                   io24_mbc.DEFAULTS["xovers"][i], lambda v: "%.0f Hz" % v,
                   tuple(n + ":Freq" for n in XNODES[i]))

        for bi, (band, bname) in enumerate(zip(
                io24_mbc.BANDS, ("Low", "Low mid", "High mid", "High"))):
            d = io24_mbc.DEFAULTS[band]
            ex = Adw.ExpanderRow(title=bname)
            ctl[(bi, "expander")] = ex

            trow = Adw.ComboRow(title="Character",
                                model=Gtk.StringList.new(
                                    ["Standard", "Tube", "FET"]))
            trow.connect("notify::selected", self._mbc_type, ch, bi)
            ex.add_row(trow)
            ctl[(bi, "type")] = trow

            stack = Gtk.Stack()
            stack.set_vhomogeneous(False)
            ctl[(bi, "stack")] = stack

            standard = Adw.PreferencesGroup()
            model_slider(standard, bi, "s_thr", "Threshold", -56, 0, 0.01,
                         d["threshold"], lambda v: "%.2f dB" % v)
            model_slider(standard, bi, "s_ratio", "Ratio", 1, 20, 0.01,
                         d["ratio"], lambda v: "%.2f:1" % v)
            model_slider(standard, bi, "s_attack", "Attack", 0.0002, 0.15,
                         0.0001, d["attack"] / 1000.0,
                         lambda v: "%.2f ms" % (v * 1000.0))
            model_slider(standard, bi, "s_release", "Release", 0.0025, 0.9,
                         0.001, d["release"] / 1000.0,
                         lambda v: "%.1f ms" % (v * 1000.0))
            model_slider(standard, bi, "s_gain", "Gain", 0, 28, 0.01, 0.0,
                         lambda v: "%+.2f dB" % v)
            model_switch(standard, bi, "s_knee", "Soft knee", active=True)
            model_switch(standard, bi, "s_auto", "Auto mode")
            stack.add_named(standard, "standard")

            tube = Adw.PreferencesGroup()
            model_slider(tube, bi, "t_peak", "Peak reduction", 0, 100, 0.1,
                         0.0, lambda v: "%.1f" % v)
            model_slider(tube, bi, "t_gain", "Gain", 0, 100, 0.1, 40.0,
                         lambda v: "%.1f" % v)
            model_switch(tube, bi, "t_limit", "Limit mode")
            stack.add_named(tube, "tube")

            fet = Adw.PreferencesGroup()
            model_slider(fet, bi, "f_input", "Input", -56, 0, 0.01, -43.0,
                         lambda v: "%.2f dB" % v)
            model_slider(fet, bi, "f_output", "Output", -56, 0, 0.01, 0.0,
                         lambda v: "%.2f dB" % v)
            model_slider(fet, bi, "f_attack", "Attack", 0.000021, 0.0008,
                         0.000001, 0.0001,
                         lambda v: "%.3f ms" % (v * 1000.0))
            model_slider(fet, bi, "f_release", "Release", 0.05, 1.1, 0.001,
                         0.25, lambda v: "%.0f ms" % (v * 1000.0))
            f_ratio = Adw.ComboRow(
                title="Ratio", model=Gtk.StringList.new(io24_dsp.FET_RATIO_NAMES))
            f_ratio.connect("notify::selected", self._mbc_model_notify,
                            ch, bi, "f_ratio")
            fet.add(f_ratio)
            ctl[(bi, "f_ratio")] = f_ratio
            stack.add_named(fet, "fet")
            stack.set_visible_child_name("standard")
            ex.add_row(stack)

            model_switch(ex, bi, "keyon", "Key filter")
            model_slider(ex, bi, "keyf", "Key frequency", 40, 16000, 1,
                         d["key"], lambda v: "%.0f Hz" % v)
            model_switch(ex, bi, "listen", "Key listen")
            g.add(ex)
        return g

    def _mbc_type(self, row, _p, ch, bi):
        """Select one exact UC model while retaining the other two states."""
        if self._mbc_mute:
            return
        self._mbc_mirror(ch, (bi, "type"))
        model = io24_mbc.MODEL_NAMES[row.get_selected()]
        self.mbc_ctl[ch][(bi, "stack")].set_visible_child_name(model)
        if self.link_both:
            other = 2 if ch == 1 else 1
            self.mbc_ctl[other][(bi, "stack")].set_visible_child_name(model)
        self._mbc_push_band(ch, bi)

    def _mbc_model_notify(self, _widget, _p, ch, bi, cid):
        self._mbc_model_edit(ch, bi, cid)

    def _mbc_model_edit(self, ch, bi, cid):
        if self._mbc_mute:
            return
        self._mbc_mirror(ch, (bi, cid))
        self._mbc_push_band(ch, bi)

    def _mbc_band_state(self, ch, bi):
        ctl = self.mbc_ctl[ch]
        band = io24_mbc.default_band(io24_mbc.BANDS[bi])
        band["type"] = io24_mbc.MODEL_NAMES[
            max(0, min(2, ctl[(bi, "type")].get_selected()))]
        band["standard"] = {
            "threshold_db": ctl[(bi, "s_thr")].get_value(),
            "ratio": ctl[(bi, "s_ratio")].get_value(),
            "attack_s": ctl[(bi, "s_attack")].get_value(),
            "release_s": ctl[(bi, "s_release")].get_value(),
            "gain_db": ctl[(bi, "s_gain")].get_value(),
            "softknee": ctl[(bi, "s_knee")].get_active(),
            "automode": ctl[(bi, "s_auto")].get_active(),
        }
        band["tube"] = {
            "peak": ctl[(bi, "t_peak")].get_value(),
            "gain": ctl[(bi, "t_gain")].get_value(),
            "limit_mode": ctl[(bi, "t_limit")].get_active(),
        }
        band["fet"] = {
            "input_db": ctl[(bi, "f_input")].get_value(),
            "output_db": ctl[(bi, "f_output")].get_value(),
            "attack_s": ctl[(bi, "f_attack")].get_value(),
            "release_s": ctl[(bi, "f_release")].get_value(),
            "ratio_index": ctl[(bi, "f_ratio")].get_selected(),
        }
        band["key_filter"] = ctl[(bi, "keyon")].get_active()
        band["key"] = ctl[(bi, "keyf")].get_value()
        band["listen"] = ctl[(bi, "listen")].get_active()
        return band

    def _mbc_push_band(self, ch, bi):
        values = self._mbc_band_state(ch, bi)
        controls = io24_mbc.compressor_controls(
            values, getattr(self, "_fs", 48000.0))
        insert = getattr(self, "insert", None)
        if insert is None or not insert.running:
            return
        update = {"c%d:%s" % (bi, key): value
                  for key, value in controls.items()}
        for target in self._mbc_targets(ch):
            insert.set_channel_controls(target, update)

    def _mbc_set(self, ch, cid, keys, value):
        """Push one crossover move to every graph that carries it."""
        if self._mbc_mute:
            return
        self._mbc_mirror(ch, cid)
        self._mbc_push(ch, keys, value)

    def _mbc_targets(self, ch):
        """Channels a Multiband edit reaches: both while linked."""
        return (1, 2) if self.link_both else (ch,)

    def _mbc_push(self, ch, keys, value):
        insert = getattr(self, "insert", None)
        if insert is None or not insert.running:
            return
        for c in self._mbc_targets(ch):
            insert.set_channel_controls(c, {key: value for key in keys})

    def _mbc_mirror(self, ch, cid):
        """While linked, copy one control to the other channel without an
        echo; _mbc_push then sends the move to both graphs."""
        if not self.link_both:
            return
        other = 2 if ch == 1 else 1
        src = self.mbc_ctl.get(ch, {}).get(cid)
        dst = self.mbc_ctl.get(other, {}).get(cid)
        if src is None or dst is None:
            return
        prior, self._mbc_mute = self._mbc_mute, True
        try:
            if isinstance(src, Adw.SwitchRow):
                dst.set_active(src.get_active())
            elif isinstance(src, Adw.ComboRow):
                dst.set_selected(src.get_selected())
            else:
                dst.set_value(src.get_value())
        finally:
            self._mbc_mute = prior

    def _mbc_snapshot_state(self, ch):
        """Capture one channel's visible Multiband controls."""
        ctl = getattr(self, "mbc_ctl", {}).get(ch)
        if not ctl:
            return None
        state = io24_mbc.default_snapshot(
            enabled=ch in self._multiband_insert_wanted())
        state["xovers"] = [ctl["x%d" % index].get_value() for index in range(3)]
        for index, band in enumerate(io24_mbc.BANDS):
            state["bands"][band] = self._mbc_band_state(ch, index)
        return io24_mbc.validate_snapshot(state)

    def _adopt_mbc_controls(self, ch, state):
        """Show one channel's saved Multiband settings without pushing them."""
        state = io24_mbc.validate_snapshot(state)
        ctl = self.mbc_ctl[ch]
        prior, self._mbc_mute = self._mbc_mute, True
        try:
            for index, value in enumerate(state["xovers"]):
                ctl["x%d" % index].set_value(value)
            for index, band in enumerate(io24_mbc.BANDS):
                values = state["bands"][band]
                model = values["type"]
                ctl[(index, "type")].set_selected(
                    io24_mbc.MODEL_NAMES.index(model))
                ctl[(index, "stack")].set_visible_child_name(model)
                standard = values["standard"]
                for key, field in (
                        ("s_thr", "threshold_db"), ("s_ratio", "ratio"),
                        ("s_attack", "attack_s"), ("s_release", "release_s"),
                        ("s_gain", "gain_db")):
                    ctl[(index, key)].set_value(standard[field])
                ctl[(index, "s_knee")].set_active(standard["softknee"])
                ctl[(index, "s_auto")].set_active(standard["automode"])
                tube = values["tube"]
                ctl[(index, "t_peak")].set_value(tube["peak"])
                ctl[(index, "t_gain")].set_value(tube["gain"])
                ctl[(index, "t_limit")].set_active(tube["limit_mode"])
                fet = values["fet"]
                for key, field in (
                        ("f_input", "input_db"), ("f_output", "output_db"),
                        ("f_attack", "attack_s"), ("f_release", "release_s")):
                    ctl[(index, key)].set_value(fet[field])
                ctl[(index, "f_ratio")].set_selected(fet["ratio_index"])
                ctl[(index, "keyf")].set_value(values["key"])
                ctl[(index, "keyon")].set_active(values["key_filter"])
                ctl[(index, "listen")].set_active(values["listen"])
        finally:
            self._mbc_mute = prior

    def _mbc_copy(self, source, target):
        """Linked channels share one Multiband setting."""
        state = self._mbc_snapshot_state(source)
        if state is not None and self.mbc_ctl.get(target):
            self._adopt_mbc_controls(target, state)

    def _adopt_legacy_multiband(self, state):
        """The retired computer-playback multiband. Its bands seed both
        inputs' Multiband settings; it is never switched on by a load."""
        if state is None:
            return None
        state = io24_mbc.validate_snapshot(state)
        if not getattr(self, "mbc_ctl", None):
            return "multiband state retained in file; controls unavailable here"
        for ch in io24_mbc.INSERT_CHANNELS:
            self._adopt_mbc_controls(ch, state)
        self._insert_stale = True
        return ("the playback multiband is now the compressor type Multiband; "
                "its settings were loaded into both inputs, switched off")

    def _insert_state(self):
        """The Multiband compressor type's Host-only state: both inputs'
        settings (enabled = selected with the Compressor on), what the Host
        changed in the unit's mixer, and the buffer to give back."""
        if not getattr(self, "mbc_ctl", None):
            return None
        channels = {}
        for ch in io24_mbc.INSERT_CHANNELS:
            snapshot = self._mbc_snapshot_state(ch)
            if snapshot is None:
                return None
            channels[str(ch)] = snapshot
        return io24_mbc.validate_insert_state({
            "version": io24_mbc.INSERT_VERSION, "channels": channels,
            "routing": self._insert_routing,
            "quantum_before": self._insert_quantum_before})

    def _adopt_insert_state(self, state):
        """Restore both inputs' Multiband: settings, and the selection with
        the Compressor on where it was. Only a Host with nothing rerouted of
        its own takes the saved routing, which is how a crashed session's
        changes to the unit's mixer are undone or carried on."""
        if state is None:
            return None
        state = io24_mbc.validate_insert_state(state)
        if not getattr(self, "mbc_ctl", None):
            return "multiband state retained in file; controls unavailable here"
        insert = getattr(self, "insert", None)
        if self._insert_routing is None and not (insert and insert.running):
            self._insert_routing = state["routing"]
            if self._insert_quantum_before is None:
                self._insert_quantum_before = state["quantum_before"]
        on = []
        prior, self._adopt_mute = self._adopt_mute, True
        try:
            for ch in io24_mbc.INSERT_CHANNELS:
                snapshot = state["channels"][str(ch)]
                self._adopt_mbc_controls(ch, snapshot)
                if snapshot["enabled"]:
                    on.append(ch)
                    W = self.w[ch]
                    W["model"].set_selected(MULTIBAND_MODEL)
                    self.dyn_by_ch[ch]["comp"] = True
                    W["comp_on"].set_active(True)
                    W["comp_curve"].queue_draw()
        finally:
            self._adopt_mute = prior
        self._insert_stale = True
        if not on:
            return None
        return "Multiband restored on Input %s" % " and ".join(map(str, on))

    @staticmethod
    def _insert_missing(multiband=(), delays=()):
        """What this computer lacks for the requested Host insert."""
        if not io24_mbc.pipewire_available():
            return "Audio service unavailable"
        if multiband and io24_mbc.uc_comp_available() is None:
            return "Compressor unavailable: %s" % (
                io24_mbc.uc_comp_error() or "unknown compiler error")
        if delays and io24_mbc.voicefx_delay_available() is None:
            return "Delay unavailable: %s" % (
                io24_mbc.voicefx_delay_error() or "unknown compiler error")
        return None

    def _multiband_selected(self, ch):
        model = (getattr(self, "w", {}).get(ch) or {}).get("model")
        return model is not None and model.get_selected() == MULTIBAND_MODEL

    def _multiband_insert_wanted(self):
        """Inputs with Multiband selected and the Compressor switched on."""
        return tuple(ch for ch in io24_mbc.INSERT_CHANNELS
                     if self._multiband_selected(ch)
                     and self.dyn_by_ch[ch]["comp"])

    def _host_delay_states(self):
        """The selected 96 kHz VocalEcho state, keyed by its input."""
        model_row = getattr(self, "fx_model", None)
        target_row = getattr(self, "fx_target", None)
        if model_row is None or target_row is None or not \
                io24_fx.delay_needs_host_fallback(
                    Win._voicefx_effective_rate(self)):
            return {}
        index = max(0, min(len(self.FX_ORDER) - 1,
                           model_row.get_selected()))
        if self.FX_ORDER[index] != "delay":
            return {}
        state = self._fx_live_params()
        if not state.get("on"):
            return {}
        target = 2 if target_row.get_selected() == 1 else 1
        return {target: io24_voicefx_delay.validate_state(state)}

    def _insert_wanted(self):
        """Union of Multiband and the safe 96 kHz Delay insert channels."""
        return tuple(sorted(set(self._multiband_insert_wanted()) |
                            set(self._host_delay_states())))

    def _insert_reconcile(self, restart=False):
        """Bring the running insert, the unit's mixer and PipeWire into line
        with the controls.

        The processing starts before an input's own feed leaves the mixer,
        and the feed goes back whenever the processing is not running, so a
        failure never leaves an input silent.
        """
        insert = getattr(self, "insert", None)
        if insert is None:
            return
        multiband = self._multiband_insert_wanted()
        delays = self._host_delay_states()
        wanted = tuple(sorted(set(multiband) | set(delays)))
        waiting = None
        if wanted:
            missing = self._insert_missing(multiband, delays)
            if missing:
                self._insert_give_up(multiband, "Host insert %s" % missing)
                return
            active_multiband = getattr(
                insert, "multiband_channels", insert.channels)
            active_delays = getattr(insert, "delay_channels", ())
            if restart or self._insert_stale or not insert.running or \
                    tuple(active_multiband) != tuple(multiband) or \
                    tuple(active_delays) != tuple(sorted(delays)):
                capture = io24_mbc.find_io24_capture_source()
                sink = io24_mbc.find_io24_sink()
                if not capture or not sink:
                    insert.stop()
                    waiting = "Waiting for audio"
                else:
                    states = {ch: self._mbc_snapshot_state(ch)
                              for ch in multiband}
                    if not insert.start(
                            states, capture, sink,
                            sample_rate=getattr(self, "_fs", 48000.0),
                            delays=delays):
                        self._insert_give_up(
                            multiband, "Host insert did not start: %s"
                            % insert.last_error)
                        return
                    self._insert_stale = False
                    self._insert_lower_quantum()
                    if io24_mbc.set_default_input(io24_mbc.INSERT_SOURCE_NAME):
                        self._insert_default_input = True
        elif insert.running:
            insert.stop()
        running = insert.channels if insert.running else ()
        self._insert_sync_mixer(running)
        if not running and waiting is None:
            self._insert_restore_system()
        self._insert_show(waiting)

    def _insert_give_up(self, channels, message):
        """The insert cannot run: those Compressor switches go off, the
        inputs' own feeds come back, and the user is told why."""
        prior, self._adopt_mute = self._adopt_mute, True
        try:
            for ch in channels:
                self.dyn_by_ch[ch]["comp"] = False
                self.w[ch]["comp_on"].set_active(False)
                self.w[ch]["comp_curve"].queue_draw()
        finally:
            self._adopt_mute = prior
        self.insert.stop()
        self._insert_sync_mixer(())
        self._insert_restore_system()
        self._insert_show()
        self.say(message)

    def _insert_sync_mixer(self, running):
        """Queue one job that makes the unit's mixer match `running`: an input
        whose insert runs plays from USB playback 1-2, every other input from
        its own feed. The job reads the routing record when it runs, so jobs
        queued back to back cannot undo each other."""
        target = tuple(running)
        if not target and self._insert_routing is None and \
                not self._insert_pending:
            return
        self._insert_pending += 1

        def work(dev, target=target):
            routing = self._insert_routing
            try:
                moved = {int(key) for key in
                         ((routing or {}).get("moved") or {})}
                for ch in sorted(moved - set(target)):
                    routing = io24_mbc.unroute_insert(dev, ch, routing)
                for ch in sorted(set(target) - moved):
                    routing = io24_mbc.route_insert(dev, ch, routing)
            except Exception as error:
                GLib.idle_add(self.say, "Multiband could not reroute the "
                              "mixer: %s" % error)
            finally:
                self._insert_routing = routing if routing and (
                    routing["moved"] or routing["return_prior"]) else None
                self._insert_pending -= 1
            GLib.idle_add(self._sync_mix_widgets)
        self.ctl.submit(work)

    def _insert_lower_quantum(self):
        """Hold PipeWire's buffer at INSERT_QUANTUM while an insert runs,
        unless it is already that small, remembering what to give back."""
        if self._insert_quantum_before is not None:
            return
        forced = int(pw_settings().get("clock.force-quantum", "0") or 0)
        if forced and forced <= io24_mbc.INSERT_QUANTUM:
            return
        ok, _message = pw_set("clock.force-quantum", io24_mbc.INSERT_QUANTUM)
        if ok:
            self._insert_quantum_before = forced

    def _insert_restore_system(self):
        """Give PipeWire back the buffer and default input the insert took."""
        if self._insert_quantum_before is not None:
            pw_set("clock.force-quantum", self._insert_quantum_before)
            self._insert_quantum_before = None
        if self._insert_default_input:
            io24_mbc.set_default_input(io24_mbc.find_io24_capture_source())
            self._insert_default_input = False

    def _insert_show(self, waiting=None):
        insert = getattr(self, "insert", None)
        running = getattr(insert, "multiband_channels", insert.channels) \
            if insert is not None and insert.running else ()
        wanted = self._multiband_insert_wanted()
        for ch, row in getattr(self, "mbc_status", {}).items():
            if ch in running:
                text = "On"
            elif ch in wanted:
                text = "Waiting for audio" if waiting else "Starting"
            else:
                text = "Off"
            row.set_subtitle(text)

    def _insert_watchdog(self):
        """Put right what the controls want but is not running: a process
        that died, an io24 whose audio came back."""
        insert = getattr(self, "insert", None)
        if insert is not None:
            multiband = self._multiband_insert_wanted()
            delays = self._host_delay_states()
            wanted = self._insert_wanted()
            active_multiband = getattr(
                insert, "multiband_channels", insert.channels)
            active_delays = getattr(insert, "delay_channels", ())
            if (wanted and (not insert.running or
                            tuple(active_multiband) != tuple(multiband) or
                            tuple(active_delays) != tuple(sorted(delays)))) \
                    or (not wanted and insert.running):
                self._insert_reconcile()
        return True

    def _insert_shutdown(self):
        """On the way out the unit gets its own feeds back and PipeWire its
        buffer and default input. Which inputs had Multiband stays in the
        session, so the next launch puts it back."""
        insert = getattr(self, "insert", None)
        if insert is None:
            return
        insert.stop()
        self._insert_routing = release_insert_routing(
            getattr(self, "ctl", None), self._insert_routing)
        self._insert_restore_system()

    def _channel_column(self, ch):
        """One channel's fat channel. The rack above selects which module's
        controls are shown; only one section is on screen at a time."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        if not hasattr(self, "mod_stacks"):
            self.mod_stacks = {}
        W = self.w[ch]

        e = Adw.PreferencesGroup(title="Equaliser")
        W["curve"] = EQCurve(self, ch)
        # PreferencesGroup lays out PreferencesRows as full-width list rows.
        # Adding a bare custom widget was accepted but allocated it no height,
        # which silently hid both the response graph and the band selector.
        curve_row = Adw.PreferencesRow()
        curve_row.set_child(W["curve"])
        e.add(curve_row)
        W["eq_model"] = Adw.ComboRow(
            title="EQ model",
            model=Gtk.StringList.new(["Standard", "Passive", "Vintage"]))
        W["eq_model"].connect(
            "notify::selected", self._eq_model_changed, ch)
        e.add(W["eq_model"])
        W["eq_on"] = Adw.SwitchRow(title="EQ")
        W["eq_on"].connect("notify::active", self._eq_power_changed, ch)
        e.add(W["eq_on"])
        bb = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=4)
        bb.add_css_class("linked")
        W["bands"] = []
        for i, nm in enumerate(BAND_NAMES):
            t = Gtk.ToggleButton(label=nm)
            t.connect("toggled", self._band_toggled, i, ch)
            bb.append(t)
            W["bands"].append(t)
        band_selector_row = Adw.PreferencesRow()
        band_selector_row.set_child(bb)
        e.add(band_selector_row)
        W["_standard_eq_rows"] = [band_selector_row]
        W["band_on"] = Adw.SwitchRow(title="Low band")
        W["band_on"].connect("notify::active", self._band_power_changed, ch)
        e.add(W["band_on"])
        W["_standard_eq_rows"].append(W["band_on"])
        W["shelf"] = Adw.SwitchRow(title="Low shelf")
        W["shelf"].connect("notify::active", self._shelf_changed, ch)
        e.add(W["shelf"])
        W["_standard_eq_rows"].append(W["shelf"])
        for key, title, lo, hi, st, dv, fmt, sub in (
                ("freq", "Frequency", 36, 18000, 1, 130,
                 lambda v: "%.0f Hz" % v, None),
                ("gain", "Gain", -15, 15, 0.1, 0,
                 lambda v: "%+.1f dB" % v, None),
                ("q", "Q", 0.1, 10, 0.05, 0.6,
                 lambda v: "%.2f" % v, None)):
            r, W[key] = self._srow(title, lo, hi, st, dv, fmt,
                                   lambda v, k=key, c=ch: self._band_set(k, v, c),
                                   sub)
            e.add(r)
            W["_standard_eq_rows"].append(r)
        flat = Gtk.Button(label="Flatten", margin_top=6)
        flat.connect("clicked", lambda _b, c=ch: self._eq_flat(c))
        e.add(flat)
        W["eq_flat"] = flat
        W["_standard_eq_rows"].append(flat)

        # Passive Program EQ: exact fields, order, ranges, and defaults from
        # UC 4.7.2's embedded PassiveEQ parameter list.
        W["_passive_eq_rows"] = []
        for key, title, lo, hi, step, default in (
                ("p_bboost", "Low Boost", 0, 10, 0.1, 0.0),
                ("p_batten", "Low Atten", 0, 10, 0.1, 0.0)):
            field = key[2:]
            row, W[key] = self._srow(
                title, lo, hi, step, default, lambda v: "%.1f / 10" % v,
                lambda v, c=ch, f=field: self._alternate_eq_set(c, f, v))
            e.add(row); W["_passive_eq_rows"].append(row)
        row = Adw.ComboRow(
            title="Low Frequency",
            model=Gtk.StringList.new(["20 Hz", "30 Hz", "60 Hz", "100 Hz"]))
        row.connect("notify::selected", self._alternate_eq_switch_changed,
                    ch, "bfreq")
        W["p_bfreq"] = row; e.add(row); W["_passive_eq_rows"].append(row)
        for key, title, default, subtitle in (
                ("p_mboost", "High Boost", 1.5, None),
                ("p_bbwidth", "High Bandwidth", 10.0, None)):
            field = key[2:]
            row, W[key] = self._srow(
                title, 0, 10, 0.1, default, lambda v: "%.1f / 10" % v,
                lambda v, c=ch, f=field: self._alternate_eq_set(c, f, v),
                subtitle)
            e.add(row); W["_passive_eq_rows"].append(row)
        row = Adw.ComboRow(
            title="High Freq",
            model=Gtk.StringList.new(
                ["3 kHz", "4 kHz", "5 kHz", "8 kHz", "10 kHz",
                 "12 kHz", "16 kHz"]))
        row.connect("notify::selected", self._alternate_eq_switch_changed,
                    ch, "mfreq")
        W["p_mfreq"] = row; e.add(row); W["_passive_eq_rows"].append(row)
        row, W["p_hatten"] = self._srow(
            "High Atten", 0, 10, 0.1, 1.5, lambda v: "%.1f / 10" % v,
            lambda v, c=ch: self._alternate_eq_set(c, "hatten", v))
        e.add(row); W["_passive_eq_rows"].append(row)
        row = Adw.ComboRow(
            title="Attenuation Select",
            model=Gtk.StringList.new(["5 kHz", "10 kHz", "20 kHz"]))
        row.connect("notify::selected", self._alternate_eq_switch_changed,
                    ch, "hsfreq")
        W["p_hsfreq"] = row; e.add(row); W["_passive_eq_rows"].append(row)

        # Vintage EQ: the three switched bands and fixed high shelf declared
        # by UC's VintageEQ component.
        W["_vintage_eq_rows"] = []
        vintage_rows = (
            ("lowgain", "Low Gain", 0.0, None),
            ("lowmidgain", "Low-Mid Gain", 0.0, None),
            ("himidgain", "Hi-Mid Gain", 0.0, None),
            ("higain", "High Gain", 0.0, None),
        )
        vintage_switches = {
            "lowgain": ("lowfreq", "Low Frequency",
                        ["35 Hz", "60 Hz", "110 Hz", "220 Hz"], 1),
            "lowmidgain": ("lowmidfreq", "Low-Mid Freq",
                           ["360 Hz", "700 Hz", "1.6 kHz"], 2),
            "himidgain": ("himidfreq", "Hi-Mid Freq",
                          ["3.2 kHz", "4.8 kHz", "7.2 kHz"], 2),
        }
        for field, title, default, subtitle in vintage_rows:
            key = "v_" + field
            row, W[key] = self._srow(
                title, -16, 16, 0.1, default, lambda v: "%+.1f dB" % v,
                lambda v, c=ch, f=field: self._alternate_eq_set(c, f, v),
                subtitle)
            e.add(row); W["_vintage_eq_rows"].append(row)
            switch = vintage_switches.get(field)
            if switch is not None:
                switch_field, switch_title, labels, default_index = switch
                combo = Adw.ComboRow(
                    title=switch_title, model=Gtk.StringList.new(labels))
                combo.set_selected(default_index)
                combo.connect(
                    "notify::selected", self._alternate_eq_switch_changed,
                    ch, switch_field)
                W["v_" + switch_field] = combo
                e.add(combo); W["_vintage_eq_rows"].append(combo)

        for row in W["_passive_eq_rows"] + W["_vintage_eq_rows"]:
            row.set_visible(False)

        gt = Adw.PreferencesGroup(
            title="Gate / expander",)
        W["gate_on"] = Adw.SwitchRow(title="Gate")
        W["gate_on"].connect("notify::active", self._dyn_toggle, "gate", ch)
        gt.add(W["gate_on"])
        for key, title, lo, hi, st, dv, fmt, sub in (
                ("gth", "Threshold", -84, 0, 0.01, -40.0,
                 lambda v: "%.2f dB" % v, None),
                ("grange", "Range", -84, 0, 0.01, -60.0,
                 lambda v: "%.2f dB" % v, None),
                ("gatk", "Attack", 0.00002, 0.5, 0.0001, 0.005,
                 lambda v: "%.1f ms" % (v * 1000), None),
                ("grel", "Release", 0.05, 2.0, 0.001, 0.3,
                 lambda v: "%.0f ms" % (v * 1000), None),
                ("gkey", "Key filter", 40, 16000, 1, 40,
                 lambda v: ("off" if v <= 41 else "%.0f Hz" % v), None)):
            r, W[key] = self._srow(title, lo, hi, st, dv, fmt,
                                   lambda v, c=ch: self._dyn_push("gate", c), sub)
            gt.add(r)
        W["gklisten"] = Adw.SwitchRow(title="Key listen")
        W["gklisten"].connect("notify::active",
                              lambda *_a, c=ch: self._dyn_push("gate", c))
        gt.add(W["gklisten"])
        W["gexp"] = Adw.SwitchRow(title="Expander mode")
        W["gexp"].set_active(True)
        W["gexp"].connect("notify::active",
                          lambda *_a, c=ch: self._dyn_push("gate", c))
        gt.add(W["gexp"])

        d = Adw.PreferencesGroup(title="Compressor")
        W["comp_curve"] = CompCurve(self, ch)
        d.add(W["comp_curve"])
        W["comp_on"] = Adw.SwitchRow(title="Compressor")
        W["comp_on"].connect("notify::active", self._dyn_toggle, "comp", ch)
        d.add(W["comp_on"])
        W["model"] = Adw.ComboRow(
            title="Model", model=Gtk.StringList.new(
                ["Standard", "Tube", "FET", "Multiband"]))
        W["model"].connect("notify::selected",
                           lambda *_a, c=ch: self._comp_model_changed(c))
        d.add(W["model"])

        # The three device models share one cpxt blob format, not one parameter
        # vocabulary.  Presenting a single set of Standard sliders and mapping
        # it heuristically made Tube controls misleading and made FET output
        # literally unreachable (0..28 was clamped into its -56..0 range).
        # Each stack child below is therefore the actual descriptor set.
        W["comp_param_stack"] = Gtk.Stack()
        W["comp_param_stack"].set_vhomogeneous(False)

        standard = Adw.PreferencesGroup()
        for key, title, lo, hi, st, dv, fmt, sub in (
                ("cth", "Threshold", -56, 0, 0.01, 0.0,
                 lambda v: "%.2f dB" % v, None),
                ("rat", "Ratio", 1, 20, 0.01, 2.0,
                 lambda v: "%.2f:1" % v, None),
                ("catk", "Attack", 0.0002, 0.15, 0.0001, 0.02,
                 lambda v: "%.2f ms" % (v * 1000), None),
                ("crel", "Release", 0.0025, 0.9, 0.001, 0.15,
                 lambda v: "%.1f ms" % (v * 1000), None),
                ("mk", "Gain", 0, 28, 0.01, 0.0,
                 lambda v: "%+.2f dB" % v, None)):
            r, W[key] = self._srow(
                title, lo, hi, st, dv, fmt,
                lambda v, c=ch: self._dyn_push("comp", c), sub)
            standard.add(r)
        for key, title, sub, default in (
                ("knee", "Soft knee", "", False),
                ("auto", "Auto mode", "", False)):
            W[key] = Adw.SwitchRow(title=title, subtitle=sub)
            W[key].set_active(default)
            W[key].connect("notify::active",
                           lambda *_a, c=ch: self._dyn_push("comp", c))
            standard.add(W[key])
        W["comp_param_stack"].add_named(standard, "standard")

        tube = Adw.PreferencesGroup()
        for key, title, lo, hi, st, dv, fmt in (
                ("tpeak", "Peak reduction", 0, 100, 0.1, 0.0,
                 lambda v: "%.1f" % v),
                ("tgain", "Gain", 0, 100, 0.1, 40.0,
                 lambda v: "%.1f" % v)):
            r, W[key] = self._srow(
                title, lo, hi, st, dv, fmt,
                lambda v, c=ch: self._dyn_push("comp", c))
            tube.add(r)
        W["climit"] = Adw.SwitchRow(title="Limit mode")
        W["climit"].connect("notify::active",
                            lambda *_a, c=ch: self._dyn_push("comp", c))
        tube.add(W["climit"])
        W["comp_param_stack"].add_named(tube, "tube")

        fet = Adw.PreferencesGroup()
        for key, title, lo, hi, st, dv, fmt in (
                ("finput", "Input", -56, 0, 0.01, -43.0,
                 lambda v: "%.2f dB" % v),
                ("foutput", "Output", -56, 0, 0.01, 0.0,
                 lambda v: "%.2f dB" % v),
                ("fatk", "Attack", 0.000021, 0.0008, 0.000001, 0.0001,
                 lambda v: "%.3f ms" % (v * 1000)),
                ("frel", "Release", 0.05, 1.1, 0.001, 0.25,
                 lambda v: "%.0f ms" % (v * 1000))):
            r, W[key] = self._srow(
                title, lo, hi, st, dv, fmt,
                lambda v, c=ch: self._dyn_push("comp", c))
            fet.add(r)
        W["fratio"] = Adw.ComboRow(
            title="Ratio", model=Gtk.StringList.new(io24_dsp.FET_RATIO_NAMES))
        W["fratio"].connect("notify::selected",
                            lambda *_a, c=ch: self._dyn_push("comp", c))
        fet.add(W["fratio"])
        W["comp_param_stack"].add_named(fet, "fet")
        W["comp_param_stack"].add_named(self._mbc_group(ch), "multiband")
        W["comp_param_stack"].set_visible_child_name("standard")
        d.add(W["comp_param_stack"])

        # These are genuine common inputs on all three device builders;
        # Multiband has its own, per band.
        r, W["ckey"] = self._srow(
            "Key filter", 40, 16000, 1, 40,
            lambda v: ("off" if v <= 41 else "%.0f Hz" % v),
            lambda v, c=ch: self._dyn_push("comp", c))
        W["ckey_row"] = r
        d.add(r)
        W["cklisten"] = Adw.SwitchRow(title="Key listen")
        W["cklisten"].connect("notify::active",
                              lambda *_a, c=ch: self._dyn_push("comp", c))
        d.add(W["cklisten"])

        lm = Adw.PreferencesGroup(title="Limiter")
        W["lim_on"] = Adw.SwitchRow(title="Limiter")
        W["lim_on"].connect("notify::active", self._dyn_toggle, "lim", ch)
        lm.add(W["lim_on"])
        r, W["lth"] = self._srow("Limiter threshold", -40, 0, 0.01, -28.0,
                                 lambda v: "%.2f dB" % v,
                                 lambda v, c=ch: self._dyn_push("lim", c))
        lm.add(r)
        # The device has always taken a release coefficient and the driver has
        # always computed it exactly; the page simply never offered the
        # control, so every limiter write in this Host's history used the 0.4 s
        # default. UC keeps no release field in its preset record, so this is
        # live Host state and rides the snapshot shadow, not the slot body.
        r, W["lrel"] = self._srow("Limiter release", 0.05, 1.5, 0.01, 0.4,
                                  lambda v: "%.0f ms" % (v * 1000.0),
                                  lambda v, c=ch: self._dyn_push("lim", c))
        lm.add(r)

        # The digital HPF gets its own section: it is its own rack unit and is
        # separate from the fixed 80 Hz preamp switch on the mixer strip.
        # Sharing a group with the limiter meant clicking either one landed you
        # in a page about the other.
        hp = Adw.PreferencesGroup(title="HPF")
        W["hpf_mode"] = Adw.ComboRow(
            title="HPF",
            model=Gtk.StringList.new(
                ["Off", "40 Hz", "80 Hz", "160 Hz", "Advanced"]))
        W["hpf_mode"].set_selected(0)
        W["hpf_mode"].connect(
            "notify::selected", self._hpf_mode_changed, ch)
        hp.add(W["hpf_mode"])
        r, W["hpf"] = self._srow(
            "Exact cutoff", 24, 1000, 0.5, 24,
            lambda v: ("bypassed" if v <= 25 else "%.0f Hz" % v),
            lambda v, c=ch: self.set_hpf(c, v))
        W["hpf"].set_sensitive(False)
        hp.add(r)

        # One section visible at a time, selected by the rack above. Stacking
        # every group defeated the rack: if all four are on screen already,
        # clicking a unit has nothing to reveal.
        stack = Gtk.Stack()
        stack.set_vhomogeneous(False)     # same: size to the visible module
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(120)
        for key, group in (("EQ", e), ("GATE", gt), ("COMP", d),
                           ("LIM", lm), ("HPF", hp)):
            group.set_title("")           # the header line names the module
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.set_margin_top(6)
            holder.append(group)          # no nested PreferencesPage: it owns
            stack.add_named(holder, key)  # a ScrolledWindow, which is exactly
                                          # the split-page artifact
        self.mod_stacks[ch] = stack
        stack.set_visible_child_name("EQ")
        page.append(stack)
        W["bands"][0].set_active(True)
        return page


    def _rebuild_chains(self):
        """One rack when the channels are linked, one per channel when not."""
        child = self.chain_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.chain_box.remove(child)
            child = nxt
        self.racks = {}
        chans = (1,) if self.link_both else (1, 2)
        for ch in chans:
            if not self.link_both:
                lab = Gtk.Label(label="Channel %d" % ch, xalign=0)
                lab.add_css_class("caption")
                lab.add_css_class("dim-label")
                self.chain_box.append(lab)
            r = Rack(self, ch)
            self.racks[ch] = r
            self.chain_box.append(r)
        if self.link_both:
            lab = Gtk.Label(label="Channels 1 and 2 linked — one chain", xalign=0)
            lab.add_css_class("caption")
            lab.add_css_class("dim-label")
            self.chain_box.append(lab)
        self.chain = next(iter(self.racks.values()))

    def _fx_page(self):
        """Reverb, plus the send/return it needs, plus an honest note on the
        insert-FX models."""
        page = wide_preferences_page()
        self.fx_scroll_controller = keep_page_wheel_scrolling(page)

        g = Adw.PreferencesGroup(
            title="Shared reverb",)
        self.rev_visual = ReverbVisual(self._reverb_live_params)
        g.add(self.rev_visual)

        self.rev_on = Adw.SwitchRow(
            title="On")
        self.rev_on.connect(
            "notify::active",
            lambda *_a: (self._push_reverb(establish_path=True),
                         self._repaint_reverb()))
        g.add(self.rev_on)

        # Character presets. The device exposes one algorithm with size, input
        # high-pass and pre-delay, so a "type" is a position in that space rather
        # than a different reverb — which is worth saying plainly instead of
        # implying the unit has a spring tank in it. Wet mix stays independent so
        # switching character does not change how much you hear.
        self.rev_type = Adw.ComboRow(
            title="Character",
            model=Gtk.StringList.new([n for n, _ in self.REVERB_TYPES]))
        self.rev_type.connect("notify::selected", self._reverb_type_changed)
        g.add(self.rev_type)

        # Host-side augmentation. The device has one fixed algorithm, but the
        # host owns the parameters and can move them over time — which is a real
        # extra process, not a relabelling. Slowly modulating room size is how a
        # static digital tail is made to breathe; on a spring it is most of the
        # character. Costs one small control write per step, well under the rate
        # the mixer sustains.
        self.rev_mod = Adw.SwitchRow(title="Movement")
        self.rev_mod.connect("notify::active", self._reverb_mod_toggled)
        g.add(self.rev_mod)
        r, self.s_rmoddep = self._srow(
            "Movement depth", 0.0, 0.30, 0.005, 0.08,
            lambda v: ("off" if v < 0.005 else "±%.0f %%" % (v * 100)),
            lambda v: None)
        g.add(r)
        r, self.s_rsize = self._srow("Room size", 0, 1, 0.005, 0.5,
                                     lambda v: "%.0f %%" % (v * 100),
                                     lambda v: (self._reverb_touched(), self._push_reverb(),
                                              self._repaint_reverb()))
        g.add(r)
        # 100 %, as every Universal Control scene of the user's keeps it: the
        # FX return is a send return, so a dry share doubles the input that
        # is already in the bus. The return level sets how much reverb.
        r, self.s_rmix = self._srow("Reverb return blend", 0, 1, 0.005, 1.0,
                                    lambda v: "%.0f %%" % (v * 100),
                                    lambda v: (self._reverb_touched(), self._push_reverb(),
                                              self._repaint_reverb()))
        g.add(r)
        r, self.s_rhp = self._srow("Input high-pass", 0, 500, 1, 200,
                                   lambda v: ("off" if v < 1 else "%.0f Hz" % v),
                                    lambda v: (self._reverb_touched(), self._push_reverb(),
                                              self._repaint_reverb()))
        g.add(r)
        r, self.s_rpre = self._srow("Pre-delay", 0.0001, 0.25, 0.0005, 0.02,
                                    lambda v: "%.0f ms" % (v * 1000),
                                    lambda v: (self._reverb_touched(), self._push_reverb(),
                                              self._repaint_reverb()))
        g.add(r)

        page.add(g)          # added ONCE — adding it twice raised a GTK critical

        sg = Adw.PreferencesGroup(
            title="Shared effects returns",)
        self.rev_return_controls = {}
        for bus, bname in (("main", "Main out"), ("mixa", "Mix A"), ("mixb", "Mix B")):
            r, sc = self._srow("FX return → %s" % bname, -60, 10, 0.1, 0.0,
                               lambda v: "%.1f dB" % v,
                               lambda v, b=bus: self.ctl.submit(
                                   lambda dev: dev.set_mix_db(
                                       "fxreturn/ch1", v, bus=b)))
            self.rev_return_controls[bus] = sc
            sg.add(r)
        page.add(sg)

        # A real Host-side spring tank, not another character preset for the
        # device's one shared digital reverb. The PipeWire graph is wet-only:
        # Inputs 1/2 feed it after the Fat Channel and its stereo return joins
        # the physical Main playback path.
        spring = Adw.PreferencesGroup(title="Spring reverb")
        self.spring_on = Adw.SwitchRow(
            title="On", subtitle="Off")
        self.spring_on.connect("notify::active", self._spring_toggled)
        spring.add(self.spring_on)

        lane = Adw.ActionRow(
            title="Output", subtitle="Main 1–2")
        spring.add(lane)

        defaults = io24_spring.default_state()
        self.spring_controls = {}
        for key, title, lower, upper, step, formatter, note in (
                ("input1_db", "Input 1 send", -60.0, 0.0, 0.1,
                 lambda v: "off" if v <= -59.9 else "%.1f dB" % v, None),
                ("input2_db", "Input 2 send", -60.0, 0.0, 0.1,
                 lambda v: "off" if v <= -59.9 else "%.1f dB" % v, None),
                ("dwell", "Dwell", 0.0, 1.0, 0.005,
                 lambda v: "%.0f %%" % (v * 100), None),
                ("tone", "Tone", 0.0, 1.0, 0.005,
                 lambda v: "%.0f %%" % (v * 100), None),
                ("drip", "Drip", 0.0, 1.0, 0.005,
                 lambda v: "%.0f %%" % (v * 100), None),
                ("width", "Stereo width", 0.0, 1.0, 0.005,
                 lambda v: "%.0f %%" % (v * 100), None),
                ("predelay_s", "Pre-delay", 0.0, 0.1, 0.0005,
                 lambda v: "%.0f ms" % (v * 1000), None),
                ("output_db", "Spring return → Main 1–2", -60.0, 10.0, 0.1,
                 lambda v: "%.1f dB" % v, None)):
            row, control = self._srow(
                title, lower, upper, step, defaults[key], formatter,
                lambda _value: self._spring_controls_changed(), note)
            self.spring_controls[key] = control
            if key == "output_db":
                self.spring_return_row = row
                self.spring_return_control = control
                control.set_sensitive(False)
            spring.add(row)
        page.add(spring)

        # UC exposes one block-201 processor and an explicit input assignment.
        ig = Adw.PreferencesGroup(title="Voice FX")

        self.fx_visual = FXVisual("transformer", self._fx_live_params)
        self.fx_target = Adw.ComboRow(
            title="Voice FX input",
            subtitle="Choose which input receives the shared processor",
            model=Gtk.StringList.new(["Input 1", "Input 2"]))
        self.fx_target.set_selected(0)
        self.fx_target.connect(
            "notify::selected", lambda *_a: self._fx_target_changed())
        self.fx_model = Adw.ComboRow(title="Model",
                                     model=Gtk.StringList.new(MODEL_TITLES))
        self.fx_model.connect("notify::selected", lambda *_a: self._fx_model_changed())

        # Each mutable model owns its own storable `on` parameter as well as its
        # own exact XML-ordered controls. There is no container-level Voice FX
        # enable in the UC component model.
        self.fx_params = {}
        self.fx_power = {}
        self.fx_param_stack = Gtk.Stack()
        self.fx_param_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        # Short models must not reserve the height of Ring Mod or Filters.  The
        # homogeneous default created a blank scroll region and shifted every
        # model change to an apparently unrelated page offset.
        self.fx_param_stack.set_vhomogeneous(False)
        for key, component in io24_fx.VOICEFX_XML_SCHEMA.items():
            grp = Adw.PreferencesGroup()
            self.fx_params[key] = {}
            for parameter in component["parameters"]:
                builder = parameter.get("builder")

                if builder == "on":
                    power = Adw.SwitchRow(title=parameter["name"])
                    power.set_active(bool(parameter.get("default", False)))
                    power.connect("notify::active", self._fx_power_changed)
                    self.fx_power[key] = _SwitchValue(power)
                    grp.add(power)
                elif builder is None:
                    # UC declares Vocoder ``avoiced`` read-only, but the io24
                    # exposes no readable block-201 state to populate it.  A
                    # permanently disabled pseudo-control was misleading, so
                    # retain it in the exact schema and omit it from the Host.
                    continue
                elif parameter["type"] == "toggle":
                    control = Adw.SwitchRow(title=parameter["name"])
                    control.set_active(bool(parameter.get("default", False)))
                    control.connect("notify::active", lambda *_a: self._push_fx())
                    self.fx_params[key][builder] = _SwitchValue(control)
                    grp.add(control)
                elif parameter["type"] == "list":
                    choices = parameter["choices"]
                    control = Adw.ComboRow(
                        title=parameter["name"],
                        model=Gtk.StringList.new(list(choices)))
                    control.set_selected(int(parameter["default"]))
                    control.connect("notify::selected", lambda *_a: self._push_fx())
                    self.fx_params[key][builder] = _ComboValue(control, 0, 1)
                    grp.add(control)
                else:
                    formatter = lambda value, p=parameter: _voicefx_value_text(p, value)
                    step = (0.0001 if parameter.get("units") == "time"
                            else 0.005)
                    row, control = self._srow(
                        parameter["name"], parameter["min"], parameter["max"],
                        step, parameter["default"], formatter,
                        lambda _value: self._queue_fx_push(),
                        curve=parameter.get("curve"), mid=parameter.get("mid"))
                    self.fx_params[key][builder] = control
                    grp.add(row)
            self.fx_param_stack.add_named(grp, key)
        self.fx_param_stack.set_visible_child_name("transformer")

        # Older call sites use ``fx_arm`` for the selected model. It is an
        # adapter now, not a seventh/master switch.
        self.fx_arm = _SelectedFxPower(
            self.fx_model, self.fx_power, self.FX_ORDER)

        self.fx_rack = VoiceFxRack(self)
        ig.add(self.fx_rack)
        ig.add(self.fx_visual)
        ig.add(self.fx_target)
        ig.add(self.fx_model)
        ig.add(self.fx_param_stack)

        page.add(ig)
        return page

    FX_ORDER = tuple(io24_fx.VOICEFX_XML_SCHEMA)
    # Compatibility/public inspection table: every row is derived from the
    # exact XML transcription rather than maintained as a second UI schema.
    FX_PARAMS = VOICEFX_UI_FIELDS

    def _fx_live_params(self):
        """Current selected component state, including its own XML On value."""
        idx = max(0, min(len(self.FX_ORDER) - 1, self.fx_model.get_selected()))
        model = self.FX_ORDER[idx]
        state = {n: sc.get_value()
                 for n, sc in getattr(self, "fx_params", {}).get(model, {}).items()}
        power = getattr(self, "fx_power", {}).get(model)
        state["on"] = (power.get_value() if power is not None
                       else bool(self.fx_arm.get_active()))
        return state

    def _voicefx_effective_rate(self):
        """Conservative clock for deciding whether hardware Delay is safe.

        During a rate transition ``_selected_rate`` changes before ALSA can
        report the new hardware clock. Taking the higher of requested and
        observed rates moves Delay to the Host immediately on 48 -> 96, and
        keeps it there until 96 -> 48 has actually been observed.
        """
        rates = []
        for value in (getattr(self, "_fs", None),
                      getattr(self, "_selected_rate", None)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and 8000.0 <= value <= 192000.0:
                rates.append(value)
        return max(rates) if rates else DEFAULT_SAMPLE_RATE

    def _fx_model_changed(self):
        idx = max(0, min(len(self.FX_ORDER) - 1, self.fx_model.get_selected()))
        name = self.FX_ORDER[idx]
        v = getattr(self, "fx_visual", None)
        if v is not None:
            v.set_model(name)
        if getattr(self, "fx_param_stack", None) is not None:
            self.fx_param_stack.set_visible_child_name(name)
        rack = getattr(self, "fx_rack", None)
        if rack is not None:
            rack.queue_draw()
        self._push_fx()

    def _fx_target_changed(self):
        """Apply the selected UC Voice FX assignment before model state."""
        self._push_fx()

    def _remember_voicefx_target(self, target):
        """Adopt a confirmed live/preset assignment without echoing a write."""
        if target not in (1, 2):
            return False
        prior = self._fx_mute
        self._fx_mute = True
        try:
            target_row = getattr(self, "fx_target", None)
            if target_row is not None:
                target_row.set_selected(target - 1)
        finally:
            self._fx_mute = prior
        holder = getattr(getattr(self, "ctl", None), "dev", None)
        backend = getattr(holder, "dev", holder)
        self._fx_last_sent_device = backend
        self._fx_last_sent_target = target
        return True

    def _fx_power_changed(self, *_args):
        rack = getattr(self, "fx_rack", None)
        if rack is not None:
            rack.queue_draw()
        visual = getattr(self, "fx_visual", None)
        if visual is not None:
            visual.queue_draw()
        self._push_fx()

    def _queue_fx_push(self):
        """Coalesce an expensive multi-packet VoiceFX slider gesture."""
        rack = getattr(self, "fx_rack", None)
        if rack is not None:
            rack.queue_draw()
        visual = getattr(self, "fx_visual", None)
        if visual is not None:
            visual.queue_draw()
        if self._fx_mute:
            return
        if self._fx_push_id is not None:
            GLib.source_remove(self._fx_push_id)
        self._fx_push_id = GLib.timeout_add(80, self._fire_fx_push)

    def _fire_fx_push(self):
        self._fx_push_id = None
        self._push_fx()
        return False

    def _push_fx(self):
        """Apply the selected Voice FX on its safe processing path.

        Models 0-4 and Delay below 96 kHz use block 201 in the unit. At 96 kHz
        Delay uses the Host insert and block 201 is left on a bypassed,
        lightweight model. This boundary is what prevents a model-5 selection
        from resetting the interface.
        """
        if self._fx_mute:
            return
        rack = getattr(self, "fx_rack", None)
        if rack is not None:
            rack.queue_draw()
        visual = getattr(self, "fx_visual", None)
        if visual is not None:
            visual.queue_draw()
        on = self.fx_arm.get_active()
        model = self.FX_ORDER[max(0, min(len(self.FX_ORDER) - 1,
                                         self.fx_model.get_selected()))]
        target = 2 if self.fx_target.get_selected() == 1 else 1
        params = {name: control.get_value()
                  for name, control in self.fx_params[model].items()}
        for name in ("detune", "carrier_type"):
            if name in params:
                params[name] = int(params[name])
        if "carrier2" in params:
            params["carrier2"] = bool(params["carrier2"])

        host_delay = model == "delay" and io24_fx.delay_needs_host_fallback(
            Win._voicefx_effective_rate(self))
        if host_delay:
            state = io24_voicefx_delay.validate_state(dict(params, on=on))
            subtitle = getattr(self.fx_model, "set_subtitle", None)
            if callable(subtitle):
                subtitle("Host processing at 96 kHz")

            # Queue the hardware bypass before the mixer-route job generated by
            # _insert_reconcile. The processed return cannot become audible on
            # top of a stale hardware effect, even briefly.
            if self.ctl.dev is not None:
                def quiesce(
                        dev, fs=self._fs,
                        quantum=getattr(
                            self, "_selected_quantum", DEFAULT_QUANTUM)):
                    if getattr(self, "_host_delay_quiesced_device", None) is dev:
                        return
                    try:
                        dev.quiesce_voicefx_for_host_delay(
                            fs, quantum=quantum)
                    except Exception as error:
                        GLib.idle_add(
                            self.say, "Voice FX safety bypass failed: %s" % error)
                        return
                    self._host_delay_quiesced_device = dev
                self.ctl.submit(quiesce)

            self._insert_reconcile()
            insert = getattr(self, "insert", None)
            if on and insert is not None and insert.running:
                insert.set_delay_controls(target, state)
            return

        subtitle = getattr(self.fx_model, "set_subtitle", None)
        if callable(subtitle):
            subtitle("")
        # Moving away from the 96 kHz fallback removes only its Delay node; a
        # Multiband node on either channel remains in the shared insert.
        if Win._host_delay_states(self) or getattr(
                getattr(self, "insert", None), "delay_channels", ()):
            self._insert_reconcile()
        self._host_delay_quiesced_device = None
        if self.ctl.dev is None:
            return
        try:
            params = io24_fx.voicefx_runtime_kwargs(
                model, params, getattr(self, "_fs", DEFAULT_SAMPLE_RATE))
        except (TypeError, ValueError, RuntimeError) as error:
            self.say(str(error))
            return
        processing = getattr(self, "processing_mix_controls", {}).get(target)
        bypassed = bool(on and processing is not None and
                        processing.bypassed())

        def work(dev, ch=target, name=model, enabled=on, kw=dict(params),
                 channel_bypassed=bypassed):
            try:
                if self._fx_last_sent_device is not dev or \
                        self._fx_last_sent_target != ch:
                    dev.set_voicefx_channel(ch)
                    # Keep a successful assignment even if the following
                    # model write fails. Retrying must not exchange the route
                    # again when the target is already correct.
                    self._fx_last_sent_device = dev
                    self._fx_last_sent_target = ch
                dev.set_fx(name, on=enabled, **kw)
            except Exception as error:
                GLib.idle_add(self.say, "Voice FX send failed: %s" % error)
                return
            if channel_bypassed:
                GLib.idle_add(
                    self.say,
                    "Voice FX is on, but Input %d processing is bypassed; "
                    "clear that bypass on the Device page to hear it." % ch)

        self.ctl.submit(work)


    # name -> (size, input high-pass Hz, pre-delay s)
    # Chosen for what each name means acoustically: a small bright box, a large
    # slow space, a plate's dense low-mid-shy tail, a spring's tight bandpassed
    # boing, and a cathedral's long pre-delay and huge size.
    # (size, input high-pass, pre-delay, movement depth or None). Movement is
    # part of a character, not an extra: a static tail is what makes Spring or
    # Cathedral sound like a preset instead of a space, so the characters that
    # live on modulation bring it with them.
    REVERB_TYPES = [
        ("Custom",     None),
        ("Room",       (0.28, 220.0, 0.008, None)),
        ("Plate",      (0.52, 320.0, 0.014, None)),
        ("Spring",     (0.34, 480.0, 0.004, 0.12)),
        ("Hall",       (0.72, 160.0, 0.032, 0.05)),
        ("Cathedral",  (0.94, 110.0, 0.070, 0.08)),
    ]

    def _reverb_type_changed(self, row, _p):
        spec = self.REVERB_TYPES[row.get_selected()][1]
        if spec is None:
            return                       # "Custom" leaves the sliders alone
        size, hp, pre, move = spec
        self._rev_mute = True            # moving these must not re-select Custom
        prior_adopt = self._adopt_mute    # and must not send three partial states
        self._adopt_mute = True
        try:
            self.s_rsize.set_value(size)
            self.s_rhp.set_value(hp)
            self.s_rpre.set_value(pre)
            self.s_rmix.set_value(1.0)     # every character is fully wet
            if move is not None:
                self.s_rmoddep.set_value(move)
                self.rev_mod.set_active(True)
            else:
                self.rev_mod.set_active(False)
        finally:
            self._rev_mute = False
            self._adopt_mute = prior_adopt
        self._push_reverb()

    def _reverb_live_params(self):
        """Current reverb state for the room display."""
        return {"on": self.rev_on.get_active(),
                "size": self.s_rsize.get_value(),
                "mix": self.s_rmix.get_value(),
                "predelay": self.s_rpre.get_value(),
                "hp": self.s_rhp.get_value()}

    REV_MOD_HZ = 0.07                 # a slow drift, not a wobble

    def _reverb_mod_toggled(self, *_a):
        on = self.rev_mod.get_active()
        if on and getattr(self, "_rev_mod_id", None) is None:
            self._rev_mod_base = self.s_rsize.get_value()
            self._rev_mod_t = 0.0
            self._rev_mod_id = GLib.timeout_add(120, self._reverb_mod_step)
            if getattr(self, "rev_visual", None) is not None:
                self.rev_visual._moving = True
        elif not on and getattr(self, "_rev_mod_id", None) is not None:
            GLib.source_remove(self._rev_mod_id)
            self._rev_mod_id = None
            if getattr(self, "rev_visual", None) is not None:
                self.rev_visual._moving = False
            # put the size back where the user left it, or the control lies
            self._rev_mute = True
            prior_adopt = self._adopt_mute
            self._adopt_mute = True
            try:
                self.s_rsize.set_value(getattr(self, "_rev_mod_base",
                                               self.s_rsize.get_value()))
            finally:
                self._rev_mute = False
                self._adopt_mute = prior_adopt
            self._push_reverb()

    def _reverb_mod_step(self):
        if not self.rev_mod.get_active():
            self._rev_mod_id = None
            return False
        self._rev_mod_t += 0.12
        dep = self.s_rmoddep.get_value()
        base = getattr(self, "_rev_mod_base", 0.5)
        v = max(0.02, min(1.0, base + math.sin(
            2 * math.pi * self.REV_MOD_HZ * self._rev_mod_t) * dep))
        self._rev_mute = True             # a drift is not the user choosing Custom
        try:
            self.s_rsize.set_value(v)
        finally:
            self._rev_mute = False
        return True

    def _repaint_reverb(self):
        v = getattr(self, "rev_visual", None)
        if v is not None:
            v.queue_draw()

    def _reverb_touched(self):
        """Any manual move drops the character back to Custom, so the label
        never claims a preset that is no longer what is loaded."""
        if getattr(self, "_rev_mute", False):
            return
        if getattr(self, "rev_type", None) is not None and self.rev_type.get_selected() != 0:
            self.rev_type.set_selected(0)

    def _push_reverb(self, establish_path=False):
        if self._adopt_mute:
            return
        on = self.rev_on.get_active()
        size, mix = self.s_rsize.get_value(), self.s_rmix.get_value()
        hp, pre = self.s_rhp.get_value(), self.s_rpre.get_value()
        # Read only when it is about to be sent: the Effects page is built
        # before the Device page that owns these controls.
        processing_mix = (self.processing_mix_controls[1].get_value()
                          if establish_path else None)
        main_return = self.rev_return_controls["main"].get_value()

        def work(dev, enable=on, establish=establish_path,
                 channel_mix=processing_mix, return_db=main_return,
                 fs=self._fs):
            if not enable:
                return dev.reverb_off()
            if establish:
                # Establish the feed and return before arming the engine. This
                # is the device order that produces an audible shared reverb.
                # UC 4.7.2 does not open this path for block 201 VoiceFX.
                establish_effects_path(dev, channel_mix, return_db)
            return dev.set_reverb(
                on=True, size=size, mix=mix, hp_freq=hp,
                predelay=max(0.0001, pre), fs=fs)

        self.ctl.submit(work)

    def _reverb_character_state(self):
        """The Host-only half of the reverb, which the device has no parameter
        for: the named character, and the slow size drift the Host applies.

        Saving only the device parameters lost these on every load, and the
        character combo was reset to Custom because exact numbers cannot imply
        a name. Saving them explicitly is what makes a restored reverb the one
        the user built.
        """
        if getattr(self, "rev_type", None) is None:
            return None
        return {
            "version": 1,
            "type": int(self.rev_type.get_selected()),
            "movement": bool(self.rev_mod.get_active()),
            "movement_depth": float(self.s_rmoddep.get_value()),
        }

    def _adopt_reverb_character(self, state):
        """Restore the Host-only reverb character. Sends nothing to the device.

        Returns a completed-load notice when saved values are rejected, so a
        bad file cannot quietly leave a control describing something else.
        """
        if state is None or getattr(self, "rev_type", None) is None:
            return None
        if not isinstance(state, dict):
            return "reverb character was not restored; it was not a mapping"
        try:
            index = int(state["type"])
            depth = float(state["movement_depth"])
            movement = bool(state["movement"])
        except (KeyError, TypeError, ValueError):
            return "reverb character was not restored; the saved values were " \
                   "incomplete or unreadable"
        if not 0 <= index < len(self.REVERB_TYPES):
            return "reverb character was not restored; unknown character"
        lower, upper = self.s_rmoddep.get_adjustment().get_lower(), \
            self.s_rmoddep.get_adjustment().get_upper()
        if not lower <= depth <= upper:
            return "reverb character was not restored; movement depth was " \
                   "outside the control's range"
        prior_rev = self._rev_mute
        prior_adopt = self._adopt_mute
        # Selecting a character normally rewrites size/HP/pre-delay and sends.
        # Adoption must move the controls only.
        self._rev_mute = True
        self._adopt_mute = True
        try:
            self.rev_type.set_selected(index)
            self.rev_mod.set_active(movement)
            self.s_rmoddep.set_value(depth)
        finally:
            self._rev_mute = prior_rev
            self._adopt_mute = prior_adopt
        return None

    # ------------------------------------------------------ Host spring tank
    def _spring_state(self):
        """Complete Host-only spring settings plus this session's route loan."""
        controls = getattr(self, "spring_controls", None)
        switch = getattr(self, "spring_on", None)
        if not controls or switch is None:
            return None
        state = io24_spring.default_state(switch.get_active())
        for name, control in controls.items():
            state[name] = float(control.get_value())
        state["routing"] = getattr(self, "_spring_routing", None)
        return io24_spring.validate_state(state)

    def _spring_show(self, detail=None):
        row = getattr(self, "spring_on", None)
        if row is None:
            return False
        wanted = bool(row.get_active())
        chain = getattr(self, "spring", None)
        if detail is not None:
            subtitle = detail
        elif wanted and chain is not None and chain.running and \
                self._spring_routing is not None:
            subtitle = "On"
        elif wanted:
            subtitle = "Starting"
        else:
            subtitle = "Off"
        row.set_subtitle(subtitle)
        control = getattr(self, "spring_return_control", None)
        if control is not None:
            control.set_sensitive(wanted)
        return False

    def _spring_toggled(self, *_args):
        if self._spring_mute or self._adopt_mute:
            return
        self._spring_show()
        self._spring_reconcile()

    def _spring_controls_changed(self):
        if self._spring_mute or self._adopt_mute:
            return
        chain = getattr(self, "spring", None)
        state = self._spring_state()
        if chain is not None and chain.running and state is not None and \
                not chain.set_state(state):
            self.say("Spring reverb update failed")

    def _spring_return_changed(self, value):
        _ = value
        self._spring_controls_changed()

    def _spring_route_main(self):
        if self._spring_route_pending:
            return
        self._spring_route_pending = True

        def work(dev):
            try:
                lane = getattr(self.spring, "return_lane", None)
                if lane is None:
                    raise RuntimeError("Spring reverb has no playback lane")
                self._spring_routing = io24_spring.route_main_only(
                    dev, self._spring_routing, lane=lane)
                message = None
            except Exception as error:
                message = "Spring reverb output failed: %s" % error
            finally:
                self._spring_route_pending = False
            GLib.idle_add(self._sync_mix_widgets)
            GLib.idle_add(self._spring_show)
            if message:
                GLib.idle_add(self.say, message)

        self.ctl.submit(work)

    def _spring_restore_routes(self):
        if self._spring_route_pending or self._spring_routing is None:
            return
        self._spring_route_pending = True

        def work(dev):
            try:
                self._spring_routing = io24_spring.restore_routes(
                    dev, self._spring_routing)
                message = None
            except Exception as error:
                message = "Spring reverb cleanup failed: %s" % error
            finally:
                self._spring_route_pending = False
            GLib.idle_add(self._sync_mix_widgets)
            GLib.idle_add(self._spring_show)
            if message:
                GLib.idle_add(self.say, message)

        self.ctl.submit(work)

    def _spring_give_up(self, message):
        prior, self._spring_mute = self._spring_mute, True
        try:
            self.spring_on.set_active(False)
        finally:
            self._spring_mute = prior
        self.spring.stop()
        self._spring_restore_routes()
        self._spring_show("Off · %s" % message)
        self.say(message)

    def _spring_reconcile(self, restart=False):
        """Match the PipeWire tank and borrowed Main-only route to the switch."""
        chain = getattr(self, "spring", None)
        switch = getattr(self, "spring_on", None)
        if chain is None or switch is None:
            return
        wanted = bool(switch.get_active())
        if not wanted:
            chain.stop()
            self._spring_restore_routes()
            self._spring_show()
            return
        if self.ctl.dev is None:
            chain.stop()
            self._spring_route_pending = False
            self._spring_show("Waiting for the io24")
            return
        missing = io24_spring.available()
        if missing:
            self._spring_give_up("Spring reverb %s" % missing)
            return
        capture = io24_mbc.find_io24_capture_source()
        playback = io24_mbc.find_io24_sink()
        if not capture or not playback:
            chain.stop()
            self._spring_show("Waiting for audio")
            return
        state = self._spring_state()
        try:
            if restart:
                chain.stop()
            started = chain.start(state, capture, playback)
        except (OSError, ValueError, io24_spring.PluginBuildError) as error:
            self._spring_give_up("Spring reverb did not start: %s" % error)
            return
        if not started:
            self._spring_give_up(
                "Spring reverb did not start: %s" %
                (chain.last_error or "unknown PipeWire error"))
            return
        self._spring_route_main()
        self._spring_show()

    def _spring_watchdog(self):
        chain = getattr(self, "spring", None)
        switch = getattr(self, "spring_on", None)
        if chain is None or switch is None:
            return True
        wanted = bool(switch.get_active())
        if self.ctl.dev is None:
            if chain.running:
                chain.stop()
            self._spring_route_pending = False
            if wanted:
                self._spring_show("Waiting for the io24")
            return True
        if (wanted and (not chain.running or self._spring_routing is None)) or \
                (not wanted and (chain.running or
                                 self._spring_routing is not None)):
            self._spring_reconcile()
        return True

    def _adopt_spring_state(self, state):
        if state is None:
            return None
        try:
            state = io24_spring.validate_state(state)
        except (TypeError, ValueError) as error:
            return "Spring reverb was not restored: %s" % error
        if not getattr(self, "spring_controls", None):
            return "Spring reverb controls are unavailable"
        if self._spring_routing is None and state["routing"] is not None:
            self._spring_routing = state["routing"]
        prior_adopt, prior_spring = self._adopt_mute, self._spring_mute
        self._adopt_mute = True
        self._spring_mute = True
        try:
            for name, control in self.spring_controls.items():
                control.set_value(state[name])
            self.spring_on.set_active(state["enabled"])
        finally:
            self._spring_mute = prior_spring
            self._adopt_mute = prior_adopt
        self._spring_reconcile()
        return "Spring reverb restored%s" % (
            " on Main 1–2" if state["enabled"] else " switched off")

    def _spring_shutdown(self):
        chain = getattr(self, "spring", None)
        if chain is not None:
            chain.stop()
        self._spring_routing = release_spring_routing(
            getattr(self, "ctl", None), self._spring_routing)

    def _mark_processing_mix_known(self, channel):
        self.processing_mix_rows[channel].set_subtitle("")

    def _set_processing_mix(self, channel, value):
        """Set the unified channel processing scalar, never an isolated send.

        The old page showed a 0 dB Main return without ever writing that value.
        When shared reverb is on, raising the unified processing mix can feed
        that engine, so only an *unknown* return is initialized. An explicitly
        muted/off return remains respected.
        """
        value = max(0.0, min(1.0, float(value)))
        shared_reverb_on = self.rev_on.get_active()

        def work(dev, ch=channel, amount=value, reverb_on=shared_reverb_on):
            dev.set_fx_mix(ch, amount)
            GLib.idle_add(self._mark_processing_mix_known, ch)
            if reverb_on and amount > 0.0 and not dev.has_send_level(
                    "fxreturn/ch1", "main"):
                dev.set_send_db("fxreturn/ch1", "main", dev.DEFAULT_SEND_DB)

        self.ctl.submit(work)

    def _processing_group(self):
        """UC's DSP Amount and Bypass for both channels.

        Both controls drive the channel's one wire-4 processing scalar (see
        ProcessingMix). The amount row is greyed out while bypassed, so the
        slider never looks live while it can change nothing.
        """
        g = Adw.PreferencesGroup(title="Channel processing")
        self.processing_mix_controls = {}
        self.processing_mix_rows = {}
        for ch in (1, 2):
            bypass = Adw.SwitchRow(
                title="Channel %d bypass" % ch)
            row, amount = self._srow(
                "Channel %d DSP amount" % ch, ProcessingMix.AMOUNT_MIN, 1,
                0.005, 1.0, ProcessingMix.label,
                lambda v, c=ch: self._set_processing_amount(c, v),
                "Move to set")
            self.processing_mix_controls[ch] = ProcessingMix(amount, bypass)
            self.processing_mix_rows[ch] = row
            bypass.connect(
                "notify::active",
                lambda _row, _p, c=ch: self._processing_bypass_toggled(c))
            g.add(bypass)
            g.add(row)
        return g

    def _set_processing_amount(self, channel, value):
        """Send a moved DSP amount, but only while the channel is processing."""
        if self.processing_mix_controls[channel].bypassed():
            return
        self._set_processing_mix(channel, value)

    def _processing_bypass_toggled(self, channel):
        """Bypass writes zero; releasing it writes the remembered amount."""
        control = self.processing_mix_controls[channel]
        self.processing_mix_rows[channel].set_sensitive(not control.bypassed())
        if self._adopt_mute:
            return
        self._set_processing_mix(channel, control.get_value())

    def _adopt_processing_mix(self, channel, value):
        """Show a wire-4 value the device already holds, sending nothing."""
        control = getattr(self, "processing_mix_controls", {}).get(channel)
        if control is None:
            return
        prior = self._adopt_mute
        self._adopt_mute = True
        try:
            control.set_value(value)
        finally:
            self._adopt_mute = prior
        self._mark_processing_mix_known(channel)

    def _follow_recall_processing(self, channel, report):
        """A recall on a bypassed channel enabled it at full amount; show that."""
        if report.get("enable_asserted"):
            self._adopt_processing_mix(channel, 1.0)

    DEVICE_BYPASS_HOLD_S = 0.5

    def _follow_device_bypass(self, poff, now=None):
        """Follow the unit's own press-and-hold on the Bypass switches.

        JaSt bits 5/6 latch while press-and-hold has bypassed a channel, but a
        short Preset press only blips them, so a device state counts once it
        has held for DEVICE_BYPASS_HOLD_S. Only a *change* in that state moves
        a switch, never a mere disagreement: a poll taken before a Host write
        lands shows the old state unchanged, so it cannot undo the write. The
        switch is adopted, never sent. A channel the unit re-enables is at
        full amount, because press-and-hold writes the Boolean form of wire 4.
        """
        now = time.monotonic() if now is None else now
        seen = getattr(self, "_device_bypass", None)
        if seen is None:
            seen = self._device_bypass = {}
        for ch in (1, 2):
            off = bool(poff[ch - 1])
            accepted, candidate, since = seen.get(ch, (None, None, now))
            if off != candidate:
                seen[ch] = (accepted, off, now)
                continue
            if off == accepted or now - since < self.DEVICE_BYPASS_HOLD_S:
                continue
            seen[ch] = (off, off, since)
            control = getattr(self, "processing_mix_controls", {}).get(ch)
            if control is not None and control.bypassed() != off:
                self._adopt_processing_mix(ch, 0.0 if off else 1.0)

    SOURCES = [("line/ch1", "Input 1"), ("line/ch2", "Input 2"),
               ("return/ch1", "USB playback 1-2"), ("return/ch2", "USB playback 3-4"),
               ("return/ch3", "USB playback 5-6"), ("fxreturn/ch1", "FX return")]
    BUSES = [("main", "Main out"), ("mixa", "Mix A"), ("mixb", "Mix B")]

    def _routing_page(self):
        """The mixer matrix — every source into every bus, including the two
        loopback mixes.

        Mix A and Mix B are not just extra headphone mixes: they are the
        loopback buses that feed the computer's own record channels. Measured on
        this device by routing a tone and watching the capture stream:
            Mix A  -> USB capture channels 3-4
            Mix B  -> USB capture channels 5-6
        Capture 1-2 are the analog inputs. (Corrected 2026-08-02: this said
        A->5-6 and B->1-2, which was wrong in both entries. Re-measured by
        routing a tone into one bus at a time and reading the level on all six
        capture channels.)
        So this page is what decides what a streaming or recording application
        actually hears.
        """
        self.mix_widgets = {}
        self.bus_master_widgets = {}
        self.bus_mute_widgets = {}
        self.mirror_widgets = {}
        self.source_mute_widgets = {}
        self.solo_widgets = {}
        self._mix_mute = False

        # A matrix, not three stacked lists. Sources are rows, buses are
        # columns — 7 rows instead of 24, every source's whole routing on one
        # line, and the page stops repeating itself. The widget contract
        # (mix_widgets[(src, bus)] = (scale, mute)) is unchanged, so
        # _sync_mix_widgets and preset loads keep working untouched.
        g = Adw.PreferencesGroup(title="Routing matrix")
        grid = Gtk.Grid(column_spacing=16, row_spacing=8)
        grid.set_margin_top(6); grid.set_margin_bottom(4)

        def cell_label(text, cls=("caption", "dim-label"), xal=0.0):
            l = Gtk.Label(label=text, xalign=xal)
            for c in cls:
                l.add_css_class(c)
            return l

        # column headers
        for col, (bus, bname) in enumerate(self.BUSES):
            head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            t = Gtk.Label(label=bname)
            t.add_css_class("heading")
            head.append(t)
            sub = {"mixa": "USB 3–4", "mixb": "USB 5–6"}.get(bus)
            if sub:
                head.append(cell_label(sub, xal=0.5))
            head.set_halign(Gtk.Align.CENTER)
            grid.attach(head, 1 + col, 0, 1, 1)

        # bus master row: one offset fader per column, mirror on the mixes
        grid.attach(cell_label("Master", ("caption",)), 0, 1, 1, 1)
        for col, (bus, bname) in enumerate(self.BUSES):
            cellbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            msc = fader(-40, 10, 0.1, vertical=False)
            reset_on_double_click(msc, 0.0, lambda v: "%.1f dB" % v)
            msc.set_value(0.0)
            msc.set_size_request(110, -1)   # a floor; hexpand does the rest
            msc.set_hexpand(True)
            if bus != "main":
                msc.add_css_class("bus-" + bus)
            mlbl = editable_db(msc)
            mthr = Throttle(
                lambda v, b=bus: self.ctl.submit(
                    lambda dev: dev.set_bus_master(b, v)), 0.010)
            msc.connect("value-changed", lambda w, t=mthr: t(w.get_value()))
            cellbox.append(msc); cellbox.append(mlbl)
            bus_mute = Gtk.ToggleButton(
                icon_name="audio-volume-muted-symbolic",
                valign=Gtk.Align.CENTER)
            bus_mute.add_css_class("flat")
            bus_mute.set_tooltip_text("Mute output")
            bus_mute.connect("toggled", self._bus_mute_toggled, bus)
            cellbox.append(bus_mute)
            if bus != "main":
                mir = Gtk.ToggleButton(icon_name="view-refresh-symbolic",
                                       valign=Gtk.Align.CENTER)
                mir.add_css_class("flat")
                mir.set_tooltip_text("Follow Main")
                mir.connect("toggled", self._mirror_main_toggled, bus)
                cellbox.append(mir)
                self.mirror_widgets[bus] = mir
            grid.attach(cellbox, 1 + col, 1, 1, 1)
            self.bus_master_widgets[bus] = msc
            self.bus_mute_widgets[bus] = bus_mute

        # a hairline under the master row, so sends read as their own block
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(2); sep.set_margin_bottom(2)
        grid.attach(sep, 0, 2, 4, 1)

        for rown, (src, sname) in enumerate(self.SOURCES):
            source_head = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            source_head.append(cell_label(sname, ()))
            if src.startswith("return/") or src.startswith("fxreturn/"):
                source_mute = Gtk.ToggleButton(
                    icon_name="audio-volume-muted-symbolic",
                    valign=Gtk.Align.CENTER)
                source_mute.add_css_class("flat")
                source_mute.set_tooltip_text("Mute source")
                source_mute.connect(
                    "toggled", self._source_mute_toggled, src)
                source_head.append(source_mute)
                self.source_mute_widgets[src] = source_mute
            grid.attach(source_head, 0, 3 + rown, 1, 1)
            for col, (bus, bname) in enumerate(self.BUSES):
                cellbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=4)
                sc = fader(-60, 10, 0.1, vertical=False)
                reset_on_double_click(sc, 0.0, lambda v: "%.1f dB" % v)
                sc.set_value(0.0)
                sc.set_size_request(110, -1)
                sc.set_hexpand(True)
                if bus != "main":
                    sc.add_css_class("bus-" + bus)
                lbl = editable_db(sc)
                thr = Throttle(
                    lambda v, s2=src, b=bus: self.ctl.submit(
                        lambda dev: dev.set_send_db(s2, b, v)), 0.002)

                def moved(w, t=thr):
                    if not self._mix_mute:
                        t(w.get_value())
                sc.connect("value-changed", moved)
                off = Gtk.ToggleButton(icon_name="audio-volume-muted-symbolic",
                                       valign=Gtk.Align.CENTER)
                off.add_css_class("flat")
                off.set_tooltip_text("Remove from mix")

                def cut(b_, s2=src, b=bus, sl=sc):
                    on = not b_.get_active()
                    sl.set_sensitive(on)
                    if not self._mix_mute:
                        self.ctl.submit(
                            lambda dev: dev.set_send_assigned(s2, b, on))
                off.connect("toggled", cut)
                solo = Gtk.ToggleButton(label="S", valign=Gtk.Align.CENTER)
                solo.add_css_class("flat")
                solo.set_tooltip_text("Solo in this mix")
                solo.connect("toggled", self._solo_toggled, src, bus)
                cellbox.append(sc); cellbox.append(lbl); cellbox.append(off)
                cellbox.append(solo)
                grid.attach(cellbox, 1 + col, 3 + rown, 1, 1)
                self.mix_widgets[(src, bus)] = (sc, off)
                self.solo_widgets[(src, bus)] = solo

        g.add(grid)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_margin_top(10); outer.set_margin_bottom(24)
        outer.set_margin_start(16); outer.set_margin_end(16)
        outer.append(g)
        clamp = Adw.Clamp(maximum_size=ROUTING_WIDTH,
                          tightening_threshold=PAGE_TIGHTEN)
        clamp.set_child(outer)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_child(clamp)
        sc.set_vexpand(True)
        return sc

    def _source_mute_toggled(self, button, source):
        if self._mix_mute:
            return
        self.ctl.submit(
            lambda dev: dev.set_source_mute(source, button.get_active()))
        GLib.timeout_add(140, self._sync_mix_widgets)

    def _bus_mute_toggled(self, button, bus):
        if self._mix_mute:
            return
        self.ctl.submit(
            lambda dev: dev.set_bus_mute(bus, button.get_active()))
        GLib.timeout_add(140, self._sync_mix_widgets)

    def _mirror_main_toggled(self, button, bus):
        if self._mix_mute:
            return
        enabled = button.get_active()
        self.ctl.submit(lambda dev: dev.set_mirror_main(bus, enabled))
        GLib.timeout_add(140, self._sync_mix_widgets)
        self.say("%s %s Main" % (
            bus.replace("mix", "Mix ").title(),
            "now follows" if enabled else "restored its retained mix from"))

    def _solo_toggled(self, button, src, bus):
        if self._mix_mute:
            return
        on = button.get_active()
        self.ctl.submit(lambda dev: dev.set_solo(src, bus, on))
        # a first solo can materialise unset sends; show them afterwards
        GLib.timeout_add(140, self._sync_mix_widgets)

    def _sync_mix_widgets(self):
        """Pull the sliders back into line with the driver's send model.

        Needed after anything that changes several sends at once — Mirror main,
        or loading a preset — because those move the model without going through
        the widgets. Reads no USB: the send model is host-side bookkeeping, and
        block 100 could not be queried even if we wanted to.
        """
        if self.ctl.dev is None:
            return False                  # offline: nothing to mirror from
        dev = self.ctl.dev.dev
        self._mix_mute = True
        try:
            for (src, bus), (sc, off) in self.mix_widgets.items():
                mirrored = bus in ("mixa", "mixb") and \
                    dev.mirror_main_enabled(bus)
                display_bus = "main" if mirrored else bus
                lvl = dev.send_db(src, display_bus)
                known = dev.has_send_level(src, display_bus)
                if lvl is not None:
                    sc.set_value(max(-60.0, min(10.0, lvl)))
                assigned = dev.send_assigned(
                    src, display_bus) if known else False
                off.set_active(not assigned)
                sc.set_sensitive(assigned and not mirrored)
                off.set_sensitive(not mirrored)
                if known:
                    off.set_tooltip_text("Remove from mix")
                else:
                    off.set_tooltip_text("Add to mix at 0 dB")
            for bus, msc in self.bus_master_widgets.items():
                msc.set_value(max(-40.0, min(10.0, dev.bus_master(bus))))
            for source, mute in getattr(
                    self, "source_mute_widgets", {}).items():
                mute.set_active(dev.source_muted(source))
            for bus, mute in getattr(self, "bus_mute_widgets", {}).items():
                mute.set_active(dev.bus_muted(bus))
            for bus, mirror in getattr(self, "mirror_widgets", {}).items():
                mirror.set_active(dev.mirror_main_enabled(bus))
            for (src, bus), solo in getattr(self, "solo_widgets", {}).items():
                solo.set_active(dev.soloed(src, bus))
        finally:
            self._mix_mute = False
        return False

    def _load_host_slot_entries(self):
        """Read both Host-known device record registries."""
        identity = None
        ctl = getattr(self, "ctl", None)
        if ctl is not None and ctl.dev is not None:
            try:
                identity = ctl.dev.dev._device_slot_identity()
            except (AttributeError, TypeError, ValueError):
                pass
        try:
            entries = (
                self.device_slot_registry.entries()
                if ctl is None else
                self.device_slot_registry.entries(device_identity=identity)
                if identity is not None else [])
        except Exception:
            entries = []
        self._host_slot_entries = entries
        try:
            library = (
                self.device_preset_library_registry.entries()
                if ctl is None else
                self.device_preset_library_registry.entries(
                    device_identity=identity)
                if identity is not None else [])
        except Exception:
            library = []
        self._host_device_preset_entries = library
        self._slot_names_by_channel = {
            channel: [entry["record"]["preset_name"] for entry in entries
                      if entry["physical_channel"] == channel]
            for channel in (1, 2)
        }
        return entries

    def _refresh_host_slot_entries(self):
        """Re-read the registry after a write so the new body is listed.

        The list used to be built once when the window opened, so a preset the
        user had just saved did not appear until the Host was restarted — and
        the gate row could not name it either.
        """
        self._load_host_slot_entries()
        self._populate_user_presets()

    def _preset_mode_changed(self, row, _p):
        if getattr(self, "_preset_sync", False):
            return
        mode = row.get_selected()
        self.ctl.submit(lambda dev: dev.set_preset_mode(mode))

    def _mute_mode_changed(self, row, _p):
        if getattr(self, "_adopt_mute", False):
            return
        self.ctl.submit(lambda dev: dev.set_mute_mode(row.get_active()))

    PRESET_BASE = {1: 0, 2: 2}           # each channel's first global slot

    def _slot_changed(self, ch, slot):
        """Select a block and Host-load its known body when one is available.

        Measured on 2026-09-14: the selector alone does not make the firmware
        replay the stored body. For a block this Host wrote, Load therefore
        selects it and directly sends the registry's Fat Channel plus VoiceFX
        state. An unknown block can only be selected and is reported as such.
        """
        def work(dev):
            try:
                report = self._select_and_load_slot(dev, ch, slot)
            except Exception as error:
                GLib.idle_add(self.say,
                              "Channel %d block selection failed: %s" %
                              (ch, error))
                return
            GLib.idle_add(self._slot_recalled, ch, slot, report, None)

        self.ctl.submit(work)

    def _observe_device_slot_recalls(self, pslot):
        """Notice a front-panel slot change without inferring FX activation."""
        observed = getattr(self, "_observed_preset_slot", {})
        changed = []
        for ch in (1, 2):
            slot = pslot[ch - 1]
            previous = observed.get(ch)
            observed[ch] = slot
            if previous is not None and previous != slot:
                changed.append(ch)
        self._observed_preset_slot = observed
        return changed

    def _host_slot_entry(self, slot):
        """The Host's own receipt for one global slot, when it wrote it."""
        for entry in getattr(self, "_host_slot_entries", []):
            if entry["slot"] == slot:
                return entry
        return None

    def _slot_voicefx_intent(self, slot):
        """What this Host knows about the Voice FX stored in one device slot.

        Slot bodies have no readback, so an entry the Host never wrote is
        reported as unknown rather than assumed to be off.
        """
        entry = self._host_slot_entry(slot)
        if entry is not None:
            fx = entry["record"].get("voicefx") or {}
            return {"name": entry["record"].get("preset_name", "that slot"),
                    "on": bool(fx.get("on"))}
        # Name it the way the user sees it. The wire index is global, but a
        # channel's blocks are numbered within its own pair, so reporting
        # "Slot 3" for Channel 2's first block would be a plain lie.
        base = self.PRESET_BASE[1 if slot < 2 else 2]
        return {"name": "Block %d" % (slot - base + 1), "on": None}

    def _host_apply_slot_record(self, dev, channel, record, report):
        """Directly apply one Host-known block after physical selection."""
        report = dict(report)
        apply_preset = getattr(getattr(self, "PR", None), "apply_preset", None)
        if not callable(apply_preset):
            report["host_applied"] = False
            return report
        host_delay = Win._record_uses_host_delay(self, record)
        try:
            report["host_parts"] = apply_preset(
                dev, record, channel, with_fx=not host_delay,
                fs=getattr(self, "_fs", DEFAULT_SAMPLE_RATE))
        except Exception as error:
            report["host_applied"] = False
            report["host_apply_error"] = str(error)
        else:
            report["host_applied"] = True
            report["host_delay"] = host_delay
        return report

    def _select_and_load_slot(self, dev, channel, slot):
        """Select a physical block, then replay its Host receipt if known."""
        report = dev.recall_device_slot(channel, slot)
        entry = self._host_slot_entry(slot)
        if entry is None:
            report = dict(report)
            report["host_applied"] = False
            return report
        return self._host_apply_slot_record(
            dev, channel, entry["record"], report)

    def _slot_recalled(self, ch, slot, report, name=None):
        """Follow a completed selection/Host-assisted load in the UI."""
        intent = self._slot_voicefx_intent(slot)
        pair = getattr(self, "preset_rows", {}).get(ch)
        if pair is not None:
            local = slot - self.PRESET_BASE[ch]
            pair[1].set_selected(local if 0 <= local < 2 else -1)
        if report.get("host_applied"):
            self._adopt_recalled_slot(ch, slot)
            if report.get("host_delay"):
                self._push_fx()
        self._follow_recall_processing(ch, report)
        label = name or intent["name"]
        enabled = (" · Processing enabled"
                   if report.get("enable_asserted") else "")
        if report.get("host_applied"):
            self.say("Loaded %s on Input %d%s" % (label, ch, enabled))
        elif report.get("host_apply_error"):
            self.say("Could not load %s on Input %d: %s" %
                     (label, ch, report["host_apply_error"]))
        else:
            self.say("Selected %s on Input %d%s" % (label, ch, enabled))
        return False

    def _adopt_recalled_slot(self, ch, slot):
        """Show a Host-applied known body on the controls.

        The physical selector does not replace the live chain. This method is
        called only after direct Host replay succeeded; a body the Host never
        wrote cannot be shown because device blocks have no readback.
        """
        entry = self._host_slot_entry(slot)
        if entry is None or getattr(self, "PR", None) is None:
            return False
        record = entry["record"]
        # Alternate models retain their own semantic controls; they are never
        # coerced into four Standard bands.
        view = self.PR.alternate_eq_view(record)
        supported, reason = self.PR.direct_apply_support(record)
        if not supported:
            self.say("Recalled body is not shown on the controls: %s. They "
                     "still show the previous sound." % reason)
            return False
        try:
            self._adopt_preset_record(record, (ch,), fx_channel=ch)
        except Exception as error:
            self.say("Recalled body could not be shown on the controls: %s"
                     % error)
            return False
        if view is not None:
            self.say("Input %d · %s EQ" % (ch, view["model"].title()))
        return True

    # ------------------------------------------------- alternate-model EQ
    EQ_MODELS = ("standard", "passive", "vintage")

    def _alt_eq(self, ch):
        """The editable Passive/Vintage state on a channel, or ``None``."""
        return getattr(self, "alt_eq_by_ch", {}).get(ch)

    def _eq_model_name(self, ch):
        alternate = self._alt_eq(ch)
        return "standard" if alternate is None else alternate["model"]

    def _show_alternate_eq(self, ch, view):
        """Adopt an alternate model into its exact controls without sending."""
        if not hasattr(self, "alt_eq_by_ch"):
            self.alt_eq_by_ch = {1: None, 2: None}
        if view is not None:
            eq = io24_alt_eq.validate_eq(view["eq"])
            view = io24_presets.alternate_eq_view({"eq": eq})
            try:
                view["sections"] = io24_alt_eq.design_live_sections(
                    eq, getattr(self, "_fs", 48000.0))
                view["error"] = None
            except Exception as error:
                view["sections"] = None
                view["error"] = str(error)
            view["rate_hz"] = float(getattr(self, "_fs", 48000.0))
        self.alt_eq_by_ch[ch] = view
        self._alt_eq_warned = None

        W = getattr(self, "w", {}).get(ch)
        if not W:
            return
        model = "standard" if view is None else view["model"]
        prior_mute = getattr(self, "_adopt_mute", False)
        self._adopt_mute = True
        try:
            selector = W.get("eq_model")
            if selector is not None:
                selector.set_selected(self.EQ_MODELS.index(model))
            for row in W.get("_standard_eq_rows", ()):
                row.set_visible(model == "standard")
            for row in W.get("_passive_eq_rows", ()):
                row.set_visible(model == "passive")
            for row in W.get("_vintage_eq_rows", ()):
                row.set_visible(model == "vintage")
            switch = W.get("eq_on")
            if switch is not None:
                switch.set_active(
                    self.eq_on_by_ch.get(ch, False) if view is None
                    else bool(view["eq"]["eqallon"]))
            if view is not None:
                eq = view["eq"]
                prefix = "p_" if model == "passive" else "v_"
                for field, value in eq.items():
                    control = W.get(prefix + field)
                    if control is None:
                        continue
                    if field.endswith("freq"):
                        control.set_selected(int(value))
                    else:
                        control.set_value(float(value))
        finally:
            self._adopt_mute = prior_mute
        self.invalidate_curve()
        curve = W.get("curve")
        if curve is not None:
            curve.queue_draw()
        for rack in getattr(self, "racks", {}).values():
            rack.queue_draw()

    @staticmethod
    def _alt_eq_text(view):
        """Compact diagnostic text retained for tests and non-GTK clients."""
        status = "on" if view["eq"]["eqallon"] else "off"
        return "%s EQ (%s), editable with exact UC 4.7.2 controls" % (
            view["model"].title(), status)

    def _queue_alternate_eq(self, ch):
        """Design once for UI feedback, then queue the complete UC live route."""
        view = self._alt_eq(ch)
        if view is None:
            return False
        try:
            eq = io24_alt_eq.validate_eq(view["eq"])
            sections = io24_alt_eq.design_live_sections(eq, self._fs)
        except Exception as error:
            view["error"] = str(error)
            view["sections"] = None
            self.say("%s EQ was not sent: %s" %
                     (view["model"].title(), error))
            return False
        view.update(eq=eq, on=bool(eq["eqallon"]), sections=sections,
                    rate_hz=float(self._fs), error=None)
        self.ctl.submit(
            lambda dev, x=ch, state=dict(eq), fs=self._fs:
            dev.set_alternate_eq(x, state, fs=fs))
        self.invalidate_curve()
        controls = getattr(self, "w", {}).get(ch, {})
        if controls.get("curve") is not None:
            controls["curve"].queue_draw()
        return True

    def _alternate_eq_set(self, ch, field, value):
        if getattr(self, "_adopt_mute", False):
            return
        source = self._alt_eq(ch)
        if source is None or field not in source["eq"]:
            return
        source["eq"][field] = float(value)
        for target in self._eq_write_targets(ch):
            if target != ch:
                mirrored = io24_presets.alternate_eq_view(
                    {"eq": dict(source["eq"])})
                self._show_alternate_eq(target, mirrored)
            self._queue_alternate_eq(target)

    def _alternate_eq_switch_changed(self, row, _param, ch, field):
        if not getattr(self, "_adopt_mute", False):
            self._alternate_eq_set(ch, field, int(row.get_selected()))

    def _eq_model_changed(self, row, _param, ch):
        if getattr(self, "_adopt_mute", False):
            return
        index = max(0, min(2, int(row.get_selected())))
        model = self.EQ_MODELS[index]
        targets = (1, 2) if getattr(self, "link_both", False) else (ch,)
        for target in targets:
            was_on = self.eq_enabled(target)
            if model == "standard":
                self.eq_on_by_ch[target] = was_on
                self._show_alternate_eq(target, None)
                if was_on:
                    for band in range(4):
                        state = self._effective_eq_band(target, band)
                        self.ctl.submit(
                            lambda dev, x=target, j=band, bb=state, fs=self._fs:
                            dev.set_eq_band(x, j, bb["shape"], bb["freq"],
                                            bb["gain"], bb["q"], fs=fs))
                else:
                    self.ctl.submit(lambda dev, x=target: dev.eq_off(x))
            else:
                eq = io24_alt_eq.default_eq(model, on=was_on)
                self._show_alternate_eq(
                    target, io24_presets.alternate_eq_view({"eq": eq}))
                self._queue_alternate_eq(target)
        self.say("%s EQ selected on Channel %s" %
                 (model.title(), " and ".join(str(c) for c in targets)))

    def _release_alternate_eq(self, ch):
        """Compatibility action: select and immediately apply Standard EQ."""
        controls = getattr(self, "w", {}).get(ch, {})
        selector = controls.get("eq_model")
        if selector is not None:
            selector.set_selected(0)
        else:
            class Selection:
                @staticmethod
                def get_selected():
                    return 0
            self._eq_model_changed(Selection(), None, ch)

    def _eq_write_targets(self, ch):
        """Linked edits reach only channels currently using the same model."""
        chans = (1, 2) if getattr(self, "link_both", False) else (ch,)
        source_model = self._eq_model_name(ch)
        mismatched = tuple(c for c in chans
                           if self._eq_model_name(c) != source_model)
        if mismatched and getattr(self, "_alt_eq_warned", None) != mismatched:
            self._alt_eq_warned = mismatched
            self.say("Linked EQ edit skipped Channel %s because its EQ model "
                     "differs" % " and ".join(str(c) for c in mismatched))
        return tuple(c for c in chans if c not in mismatched)

    def _recall_host_preset(self, _button, entry):
        """Select one Host-known block and directly load its complete record."""
        slot = entry["slot"]
        channel = entry["physical_channel"]
        name = entry["record"].get("preset_name", "that slot")

        def work(dev):
            try:
                report = self._select_and_load_slot(dev, channel, slot)
            except Exception as error:
                GLib.idle_add(self.say, "%s load failed: %s" % (name, error))
                return
            GLib.idle_add(self._slot_recalled, channel, slot, report, name)

        self.ctl.submit(work)

    def _device_page(self):
        """Sample rate, buffer size, channel processing and device identity.

        Rate and buffer are host-side on Linux: the io24 is class-compliant, so
        it advertises rates over USB Audio Class and PipeWire chooses. There is
        no device parameter for either — no clock/rate/buffer/latency descriptor
        exists among the 117 the host exposes, and the firmware has no rate
        vocabulary at all. So this page reads the real state from ALSA and
        drives PipeWire, instead of pretending to write to the device.
        """
        page = wide_preferences_page()

        g = Adw.PreferencesGroup(
            title="Sample rate",)
        rates = alsa_rates()
        self.rate_row = Adw.ComboRow(
            title="Rate", model=Gtk.StringList.new(["%d Hz" % r for r in rates]))
        self._rates = rates
        if self._selected_rate in rates:
            self.rate_row.set_selected(rates.index(self._selected_rate))
        self.rate_row.connect("notify::selected", self._rate_changed)
        g.add(self.rate_row)
        self.rate_note = Adw.ActionRow(title="Available rates")
        self.rate_note_lbl = Gtk.Label(label="…")
        self.rate_note_lbl.add_css_class("dim-label")
        self.rate_note.add_suffix(self.rate_note_lbl)
        g.add(self.rate_note)
        page.add(g)

        b = Adw.PreferencesGroup(
            title="Buffer size",)
        self._quanta = list(SUPPORTED_QUANTA)
        self.quantum_row = Adw.ComboRow(
            title="Size",
            model=Gtk.StringList.new(["%d frames" % q for q in self._quanta]))
        self.quantum_row.set_selected(
            self._quanta.index(self._selected_quantum))
        self.quantum_row.connect("notify::selected", self._quantum_changed)
        b.add(self.quantum_row)
        self.latency_row = Adw.ActionRow(title="Latency")
        self.latency_lbl = Gtk.Label(label="…")
        self.latency_lbl.add_css_class("numeric")
        self.latency_lbl.add_css_class("dim-label")
        self.latency_row.add_suffix(self.latency_lbl)
        b.add(self.latency_row)
        page.add(b)

        # Output delay. Universal Control puts this on the same page as the
        # sample rate, and this is the one control here that lives in the
        # DEVICE rather than in PipeWire — the two rows above only ask the
        # sound server for something, this one writes hardware.
        od = Adw.PreferencesGroup(
            title="Output delay",)
        self._delay_buses = [("off", "Off"), ("mixa", "Mix A"), ("mixb", "Mix B")]
        self.delay_bus_row = Adw.ComboRow(
            title="Apply to",
            model=Gtk.StringList.new([t for _, t in self._delay_buses]))
        self.delay_bus_row.connect("notify::selected", self._delay_bus_changed)
        od.add(self.delay_bus_row)

        drow = Adw.ActionRow(title="Delay")
        self.delay_scale = fader(0, 500, 2, vertical=False)
        reset_on_double_click(self.delay_scale, 0.0, lambda v: "%.0f ms" % v)
        self.delay_scale.set_value(0)
        self.delay_scale.set_size_request(260, -1)
        self.delay_lbl = Gtk.Label(label="0 ms")
        self.delay_lbl.add_css_class("numeric")
        self.delay_lbl.add_css_class("dim-label")
        self.delay_lbl.set_size_request(72, -1)
        # 10 ms throttle, not 2: this is a hardware write behind a coarse
        # control, and there is no reason to flood the bus from a drag.
        self._delay_thr = Throttle(
            lambda ms: self.ctl.submit(
                lambda dev: dev.set_output_delay(ms / 1000.0)), 0.010)

        def delay_moved(w):
            ms = int(round(w.get_value() / 2.0) * 2)
            self.delay_lbl.set_text("%d ms" % ms)
            if not getattr(self, "_delay_mute", False):
                self._delay_thr(ms)
        self.delay_scale.connect("value-changed", delay_moved)
        drow.add_suffix(self.delay_scale)
        drow.add_suffix(self.delay_lbl)
        od.add(drow)
        # Starts off, so the slider starts inert. There is no read-back for this
        # parameter — like the mixer, these widgets show what the app last sent,
        # not what the hardware holds.
        self.delay_scale.set_sensitive(False)
        page.add(od)

        page.add(self._processing_group())
        page.add(self._preset_button_group())

        mute_group = Adw.PreferencesGroup(title="Mute behavior")
        self.mute_sync_row = Adw.SwitchRow(title="Channel Mute Sync")
        self.mute_sync_row.connect(
            "notify::active", self._mute_mode_changed)
        mute_group.add(self.mute_sync_row)
        page.add(mute_group)

        lv = Adw.PreferencesGroup(title="Live stream")
        self.live_rows = {}
        for k, t in (("rate", "Rate"), ("period", "Period size"),
                     ("buffer", "Buffer size"), ("format", "Format"),
                     ("channels", "Channels")):
            row = Adw.ActionRow(title=t)
            l = Gtk.Label(label="—")
            l.add_css_class("numeric")
            l.add_css_class("dim-label")
            row.add_suffix(l)
            lv.add(row)
            self.live_rows[k] = l
        page.add(lv)

        idg = Adw.PreferencesGroup(title="Device")
        self._usb_product = self._usb_sysattr("product") or "Revelator io24"
        self._usb_id = ""
        for p in self._usb_entry():
            self._usb_id = (open(p + "/idVendor").read().strip() + ":" +
                            open(p + "/idProduct").read().strip())
            break
        for t, v in (("Model", self._usb_product),
                     ("USB ID", self._usb_id),
                     ("Firmware", self._bcd_device()),
                     ("Serial", self._usb_serial())):
            row = Adw.ActionRow(title=t)
            l = Gtk.Label(label=v)
            l.add_css_class("dim-label")
            row.add_suffix(l)
            idg.add(row)
        page.add(idg)

        host_names = Adw.PreferencesGroup(title="Names")
        stored_names = {}
        for call in io24._load_shadow().values():
            if isinstance(call, dict) and \
                    call.get("fn") == "set_component_name":
                kwargs = call.get("kwargs") or {}
                stored_names[kwargs.get("component")] = kwargs.get("name", "")
        self.component_name_rows = {}
        for component, title in (
                ("line/ch1", "Input 1"), ("line/ch2", "Input 2"),
                ("return/ch1", "USB playback 1–2"),
                ("return/ch2", "USB playback 3–4"),
                ("return/ch3", "USB playback 5–6"),
                ("fxreturn/ch1", "FX return"),
                ("aux/ch1", "Mix A"), ("aux/ch2", "Mix B"),
                ("main/ch1", "Main")):
            row = Adw.EntryRow(title=title, text=stored_names.get(component, ""))
            row.set_show_apply_button(True)
            row.connect("apply", self._component_name_applied, component)
            host_names.add(row)
            self.component_name_rows[component] = row
        page.add(host_names)

        # Channel names, read from the device's 'CHNP' table (PROTOCOL.md §13j).
        # Read ONCE, not polled: the table is static, and this device dislikes
        # control traffic during streaming (§12G).
        #
        # It has to go through ctl.submit like every other device access — Win
        # holds no device handle of its own, only `ctl`. Reading it directly here
        # was wrong twice over: Win has no `.dev`, and even if it did, a
        # synchronous USB read on the GTK main thread would block the UI. The
        # group is created hidden and revealed only if the device answers, so a
        # device that returns nothing simply shows no section.
        self._names_group = Adw.PreferencesGroup(
            title="Device names")
        self._names_group.set_visible(False)
        page.add(self._names_group)
        self.ctl.submit(
            lambda dev: self.ctl.snap.__setitem__("names", dev.channel_names()))
        self._names_tries = 0
        GLib.timeout_add(700, self._fill_channel_names)

        GLib.timeout_add(1500, self._refresh_device_page)
        return page

    def _component_name_applied(self, row, component):
        name = row.get_text().strip()
        self.ctl.submit(lambda dev: dev.set_component_name(component, name))
        self.say("Saved %s name" % row.get_title())

    def _fill_channel_names(self):
        """Populate the channel-name group once the worker thread has read it.

        Returns True to be called again while the answer is still outstanding.
        Gives up after ~7 s rather than retrying forever: no names is a normal
        outcome (only two entries exist on this device, and a future firmware
        may expose none), not a condition worth spinning on.
        """
        names = self.ctl.snap.get("names")
        if not names:
            self._names_tries += 1
            return self._names_tries < 10
        for i, n in sorted(names.items()):
            self._names_group.add(Adw.ActionRow(title=n))
        self._names_group.set_visible(True)
        return False

    def _usb_serial(self):
        for p in self._usb_entry():
            return open(p + "/serial").read().strip()
        return "unknown"

    def _bcd_device(self):
        for p in self._usb_entry():
            return open(p + "/bcdDevice").read().strip()
        return "unknown"

    def _usb_sysattr(self, name):
        for p in self._usb_entry():
            try:
                return open(p + "/" + name).read().strip()
            except Exception:
                pass
        return ""

    def _usb_entry(self):
        """First sysfs entry for the io24/io44 control interface parent."""
        try:
            for d in os.listdir("/sys/bus/usb/devices"):
                b = "/sys/bus/usb/devices/" + d
                if os.path.exists(b + "/idProduct") and \
                   open(b + "/idProduct").read().strip() in ("0422", "0424"):
                    yield b
        except Exception:
            return

    def _apply_rate_change(self, want, previous=None):
        """Pin PipeWire after any required hardware-Delay preflight."""
        if previous is None:
            previous = getattr(self, "_selected_rate", DEFAULT_SAMPLE_RATE)
        # Establish the conservative Delay guard before PipeWire can begin the
        # transition. On failure the old preference is restored.
        self._selected_rate = want
        ok, msg = pw_set("clock.force-rate", want)
        if not ok:
            self._selected_rate = previous
            self._audio_clock_restore_deferred = False
            self.say("Rate change failed: %s" % msg)
            return False
        self._audio_clock_restore_deferred = False
        save = getattr(self, "_save_last_session", None)
        if callable(save):
            save()
        return False

    def _rate_changed(self, row, _p):
        if getattr(self, "_dev_mute", False):
            return
        want = self._rates[row.get_selected()]
        settings = pw_settings()
        allowed = settings.get("clock.allowed-rates", "")
        available = set(int(value) for value in re.findall(r"\d+", allowed))
        if available and want not in available:
            self.say("Available rates: %s" % allowed.strip("[] "))
            return

        # Model 5 must not survive a 96 kHz clock transition in the unit. Do
        # the device transaction first, and only then ask PipeWire to change
        # the clock. This also cleans a stale/dirty model selected outside the
        # Host; the visible model is re-applied on the new safe path when ALSA
        # reports the rate change.
        if io24_fx.delay_needs_host_fallback(want) and self.ctl.dev is None:
            previous = getattr(
                self, "_selected_rate", DEFAULT_SAMPLE_RATE)
            safe_rate = self._safe_delay_transition_rate(available)
            self._selected_rate = want
            ok, message = pw_set("clock.force-rate", safe_rate)
            if not ok:
                self._selected_rate = previous
                self._audio_clock_restore_deferred = False
                self.say("Rate change failed: %s" % message)
                return
            self._audio_clock_restore_deferred = True
            save = getattr(self, "_save_last_session", None)
            if callable(save):
                save()
            self.say(
                "%d kHz will be applied after the io24 connects safely" %
                (want // 1000))
            return

        if io24_fx.delay_needs_host_fallback(want) and self.ctl.dev is not None:
            previous = getattr(
                self, "_selected_rate", DEFAULT_SAMPLE_RATE)
            current_rate, current_quantum = self._transition_clock(
                settings, alsa_live())
            # Route any Voice FX edit that arrives during the old-rate settle
            # window to the Host. Otherwise a queued slider edit could select
            # model 5 again between the safety transaction and the rate call.
            self._selected_rate = want

            def quiesce(
                    dev, rate=current_rate,
                    quantum=current_quantum):
                try:
                    dev.quiesce_voicefx_for_host_delay(
                        rate, quantum=quantum)
                except Exception as error:
                    def failed(problem=error):
                        self._selected_rate = previous
                        self.say(
                            "Rate change stopped: Voice FX safety bypass "
                            "failed: %s" % problem)
                        return False
                    GLib.idle_add(
                        failed)
                    return
                self._host_delay_quiesced_device = dev
                GLib.idle_add(
                    self._apply_rate_change, want, previous)

            self.ctl.submit(quiesce)
            return
        self._apply_rate_change(want)

    def _delay_bus_changed(self, row, _p):
        """Pick the bus, then re-apply the delay to it.

        Order matters, and getting it wrong is visible: if the delay is left set
        while the selector moves, the old bus keeps its delay until the next
        write lands. So select first, then push the current slider value.
        """
        if getattr(self, "_delay_mute", False):
            return
        key = self._delay_buses[row.get_selected()][0]
        ms = int(round(self.delay_scale.get_value() / 2.0) * 2)
        self.delay_scale.set_sensitive(key != "off")
        if key == "off":
            self.ctl.submit(lambda dev: dev.output_delay_off())
            return
        self.ctl.submit(lambda dev: dev.set_output_delay(ms / 1000.0, key))

    def _quantum_changed(self, row, _p):
        if getattr(self, "_dev_mute", False):
            return
        q = self._quanta[row.get_selected()]
        ok, msg = pw_set("clock.force-quantum", q)
        if ok:
            self._selected_quantum = q
            # the user's buffer now stands; Multiband will not put back its own
            self._insert_quantum_before = None
            save = getattr(self, "_save_last_session", None)
            if callable(save):
                save()
        else:
            self.say("Buffer change failed: %s" % msg)

    def _audio_clock_state(self):
        """The user's base clock selection, independent of live overrides."""
        return audio_clock_preference({
            "audio_clock": {
                "version": 1,
                "sample_rate": getattr(
                    self, "_selected_rate", DEFAULT_SAMPLE_RATE),
                "quantum": getattr(
                    self, "_selected_quantum", DEFAULT_QUANTUM),
            },
        })

    def _safe_delay_transition_rate(self, available):
        """Choose a below-96 kHz staging clock, preferring verified 48 kHz."""
        candidates = sorted(
            int(rate) for rate in (available or SUPPORTED_SAMPLE_RATES)
            if 8000 <= int(rate) < io24_fx.DELAY_BLOCKED_RATE_HZ)
        if 48000 in candidates:
            return 48000
        if not candidates:
            return 48000
        return candidates[-1]

    def _transition_clock(self, settings, live=None):
        """Return the old live rate and quantum for a guarded transition."""
        stream = ((live or {}).get("capture") or
                  (live or {}).get("playback") or {})
        try:
            live_rate = int(str(stream.get("rate", "0")).split()[0])
        except (TypeError, ValueError, IndexError):
            live_rate = 0
        try:
            live_quantum = int(stream.get("period_size", "0") or 0)
        except (TypeError, ValueError):
            live_quantum = 0
        try:
            forced_rate = int(settings.get("clock.force-rate", "0") or 0)
        except (TypeError, ValueError):
            forced_rate = 0
        try:
            default_rate = int(settings.get("clock.rate", "0") or 0)
        except (TypeError, ValueError):
            default_rate = 0
        observed_rate = getattr(self, "_fs", 0) \
            if getattr(self, "_fs_seen", False) else 0
        rate = live_rate or observed_rate or forced_rate or default_rate or \
            getattr(self, "_fs", DEFAULT_SAMPLE_RATE)
        if not 8000 <= rate <= 192000:
            rate = DEFAULT_SAMPLE_RATE

        try:
            quantum = int(
                settings.get("clock.force-quantum", "0") or 0) or int(
                settings.get("clock.quantum", "0") or 0)
        except (TypeError, ValueError):
            quantum = 0
        quantum = live_quantum or quantum or getattr(
            self, "_selected_quantum", DEFAULT_QUANTUM)
        if not 1 <= quantum <= 16384:
            quantum = DEFAULT_QUANTUM
        return float(rate), quantum

    def _apply_saved_quantum(self, quantum, settings):
        """Restore the saved PipeWire quantum without changing a rate."""
        try:
            forced_quantum = int(
                settings.get("clock.force-quantum", "0") or 0)
        except (TypeError, ValueError):
            forced_quantum = 0
        if forced_quantum != quantum:
            quantum_ok, message = pw_set("clock.force-quantum", quantum)
            if not quantum_ok:
                self.say("Saved buffer could not be restored: %s" % message)
                return False
            self._insert_quantum_before = None
        return True

    def _apply_saved_audio_clock(self, rate, quantum, settings):
        """Finish startup clock restore after its optional safety preflight."""
        try:
            forced_rate = int(settings.get("clock.force-rate", "0") or 0)
        except (TypeError, ValueError):
            forced_rate = 0
        rate_ok = True
        if forced_rate != rate:
            rate_ok, message = pw_set("clock.force-rate", rate)
            if not rate_ok:
                self.say("Saved rate could not be restored: %s" % message)
        if rate_ok:
            self._set_device_fs(rate)
        self._apply_saved_quantum(quantum, settings)
        self._audio_clock_restore_inflight = False
        self._audio_clock_restore_deferred = False
        return False

    def _restore_audio_clock(self):
        """Apply the last selected base clock once when the Host opens."""
        if getattr(self, "_audio_clock_restore_inflight", False):
            return False
        clock = self._audio_clock_state()
        rate, quantum = clock["sample_rate"], clock["quantum"]
        settings = pw_settings()
        available = set(int(value) for value in re.findall(
            r"\d+", settings.get("clock.allowed-rates", "")))

        if available and rate not in available:
            self.say("Saved rate %d Hz is unavailable" % rate)
            return False

        try:
            forced_rate = int(settings.get("clock.force-rate", "0") or 0)
        except (TypeError, ValueError):
            forced_rate = 0
        dev = getattr(getattr(self, "ctl", None), "dev", None)
        if dev is None and io24_fx.delay_needs_host_fallback(rate):
            safe_rate = self._safe_delay_transition_rate(available)
            self._audio_clock_restore_deferred = True
            rate_ok, message = pw_set("clock.force-rate", safe_rate)
            if not rate_ok:
                self.say(
                    "Saved rate is waiting for the io24, but the safe "
                    "staging clock could not be set: %s" % message)
                return False
            self._apply_saved_quantum(quantum, settings)
            return False

        if dev is not None and (forced_rate != rate or getattr(
                self, "_audio_clock_restore_deferred", False)) and \
                io24_fx.delay_needs_host_fallback(rate):
            old_rate, old_quantum = self._transition_clock(
                settings, alsa_live())
            self._audio_clock_restore_inflight = True

            def quiesce(backend):
                try:
                    backend.quiesce_voicefx_for_host_delay(
                        old_rate, quantum=old_quantum)
                except Exception as error:
                    def failed(problem=error):
                        self._audio_clock_restore_inflight = False
                        self._audio_clock_restore_deferred = False
                        self.say(
                            "Saved rate restore stopped: Voice FX safety "
                            "bypass failed: %s" % problem)
                        return False
                    GLib.idle_add(
                        failed)
                    return
                self._host_delay_quiesced_device = backend
                GLib.idle_add(
                    self._apply_saved_audio_clock,
                    rate, quantum, settings)

            self.ctl.submit(quiesce)
            return False
        return self._apply_saved_audio_clock(rate, quantum, settings)

    def _set_device_fs(self, rate):
        """Adopt the clock the device is actually running at.

        Returns True only when a *change* needs re-sending. The first sighting
        is adopted silently: at that point the Host has pushed nothing, so
        there is nothing on the device computed at the wrong rate yet.
        """
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return False
        if not 8000.0 <= rate <= 192000.0:
            return False
        if rate == self._fs:
            self._fs_seen = True
            return False
        self._fs = rate
        if not self._fs_seen:
            self._fs_seen = True
            return False
        return True

    def _resend_rate_dependent_state(self):
        """Re-send every write whose coefficients are a function of the clock.

        The device stores coefficients, not the Hz and seconds they came from,
        so a rate change silently moves every filter and time constant until
        the Host recomputes them — at 96 kHz a 1 kHz band was landing at 2 kHz.
        Nothing here changes what the user asked for; it restates it at the
        new rate.
        """
        if self.ctl.dev is None:
            return False
        # set_hpf and _dyn_push already fan out across a linked pair, so
        # driving both channels through them would send everything twice.
        fanned = (1,) if self.link_both else (1, 2)
        for ch in fanned:
            self.set_hpf(ch, self.hpf_by_ch[ch])
            for key in ("gate", "comp", "lim"):
                self._dyn_push(key, ch)
        for ch in (1, 2):
            if self._alt_eq(ch) is not None:
                self._queue_alternate_eq(ch)
                continue
            for index in range(4):
                band = self._effective_eq_band(ch, index)
                self.ctl.submit(
                    lambda dev, x=ch, j=index, bb=band, fs=self._fs:
                    dev.set_eq_band(x, j, bb["shape"], bb["freq"],
                                    bb["gain"], bb["q"], fs=fs))
        self._push_reverb()
        self._push_fx()
        self.say("Sample rate changed to %.4g kHz" % (self._fs / 1000.0))
        return True

    def _refresh_device_page(self):
        st = pw_settings()
        allowed = st.get("clock.allowed-rates", "").strip("[] ").strip()
        live = alsa_live()
        d = live.get("capture") or live.get("playback") or {}
        # Which number is the truth? Not `clock.rate` — that is the *default*
        # the server falls back to, and it stays at 48000 no matter what the
        # graph is actually doing. In order of authority:
        #   1. the rate the device is genuinely clocking at, from hw_params
        #   2. clock.force-rate, if a rate has been pinned but nothing runs yet
        #   3. clock.rate, the idle default
        live_rate = 0
        try:
            live_rate = int((d.get("rate") or "0").split(" ")[0])
        except ValueError:
            pass
        forced = int(st.get("clock.force-rate", "0") or 0)
        cur_rate = live_rate or forced or int(
            st.get("clock.rate", str(DEFAULT_SAMPLE_RATE))
            or DEFAULT_SAMPLE_RATE)
        if self._set_device_fs(cur_rate):
            self._resend_rate_dependent_state()
        cur_q = int(st.get("clock.force-quantum", "0") or 0) or \
            int(st.get("clock.quantum", str(DEFAULT_QUANTUM))
                or DEFAULT_QUANTUM)
        self._dev_mute = True
        if cur_rate in self._rates:
            self.rate_row.set_selected(self._rates.index(cur_rate))
        if cur_q in self._quanta:
            self.quantum_row.set_selected(self._quanta.index(cur_q))
        self._dev_mute = False
        # Compare SETS of numbers, not strings. PipeWire prints this list
        # comma-separated — "[ 44100, 48000, 88200, 96000 ]" — while this
        # compared it against a space-joined string, so the test could never
        # succeed and every machine was told its rates were restricted, including
        # while it was demonstrably clocking at 96 kHz. The question is only ever
        # "is anything we offer missing", so ask exactly that.
        offered = set(self._rates)
        available = set(int(x) for x in re.findall(r"\d+", allowed))
        missing = sorted(offered - available) if available else []
        if missing:
            self.rate_note_lbl.set_text(
                "%s  (missing %s)" % (", ".join(str(r) for r in sorted(available)),
                                      ", ".join(str(r) for r in missing)))
            self.rate_note.set_subtitle("")
        else:
            self.rate_note_lbl.set_text(
                ", ".join(str(r) for r in sorted(available)) if available else "any")
            self.rate_note.set_subtitle("")
        self.latency_lbl.set_text("%.2f ms" % (cur_q / max(cur_rate, 1) * 1000.0))
        self.live_rows["rate"].set_text(
            (d.get("rate") or "—").split(" ")[0] if d else
            ("%d (pinned, idle)" % forced if forced else "— (idle)"))
        self.live_rows["period"].set_text(d.get("period_size") or "—")
        self.live_rows["buffer"].set_text(d.get("buffer_size") or "—")
        self.live_rows["format"].set_text(d.get("format") or "—")
        self.live_rows["channels"].set_text(d.get("channels") or "—")
        return True

    def _presets_page(self):
        """User presets and factory presets, as two drop-downs.

        One list per origin and one way to load: click Load and the preset's
        Fat Channel and Voice FX go to the channel chosen in Load into, or to
        both while the channels are linked. Presets the user saves live on this
        computer; storing a Fat Channel candidate in a device block is an
        action on the preset, not a section of its own.
        """
        page = wide_preferences_page()
        g = Adw.PreferencesGroup(title="Presets")

        self.factory_target = Adw.ComboRow(
            title="Load into",
            model=Gtk.StringList.new(["Channel 1", "Channel 2"]))
        self.factory_target.set_selected(0)
        g.add(self.factory_target)

        self.device_preset_slot = Adw.ComboRow(
            title="Device preset",
            model=Gtk.StringList.new([
                "Preset 1", "Preset 2", "Preset 3",
                "Preset 4", "Preset 5", "Preset 6",
            ]))
        self.device_preset_slot.set_selected(0)
        g.add(self.device_preset_slot)

        search_row = Adw.ActionRow(title="Find a preset")
        self.factory_search = Gtk.SearchEntry(
            placeholder_text="Search presets…", valign=Gtk.Align.CENTER)
        self.factory_search.set_size_request(240, -1)
        self.factory_search.connect(
            "search-changed", lambda _entry: self._filter_presets())
        search_row.add_suffix(self.factory_search)
        g.add(search_row)

        # A preset you cannot name is a preset you cannot find again.
        self.slot_name_row = Adw.EntryRow(title="Preset name")
        save = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        save.add_css_class("suggested-action")
        save.set_tooltip_text("Save the current sound")
        save.connect("clicked", self._save_user_preset_clicked)
        self.slot_name_row.add_suffix(save)
        g.add(self.slot_name_row)

        self.user_presets_row = Adw.ExpanderRow(
            title="User Presets", expanded=True)
        g.add(self.user_presets_row)
        self.factory_presets_row = Adw.ExpanderRow(title="Factory Presets")
        g.add(self.factory_presets_row)
        page.add(g)

        scenes = Adw.PreferencesGroup(title="Scenes")
        scene_row = Adw.ActionRow(title="Scene")
        scene_save = Gtk.Button(label="Save scene…", valign=Gtk.Align.CENTER)
        scene_save.add_css_class("flat")
        scene_save.connect("clicked", self._save_scene_clicked)
        scene_row.add_suffix(scene_save)
        scene_load = Gtk.Button(label="Load scene…", valign=Gtk.Align.CENTER)
        scene_load.add_css_class("flat")
        scene_load.connect("clicked", self._load_scene_clicked)
        scene_row.add_suffix(scene_load)
        scenes.add(scene_row)
        page.add(scenes)

        # The module always serves the user's own presets; only the factory
        # list depends on the recovered installer data being present.
        self.PR = io24_presets
        self._factory_unavailable = None
        try:
            self.factory_names = list(io24_presets.names())
        except SystemExit as error:
            self.factory_names = []
            self._factory_unavailable = str(error).split("\n")[0]
        except Exception as error:
            self.factory_names = []
            self._factory_unavailable = str(error)
        self._preset_rows_by_origin = {"user": [], "factory": []}
        self._load_host_slot_entries()
        self._populate_user_presets()
        self._populate_factory_presets()
        return page

    def _scene_preset_state(self, dev):
        """Return every complete device record this Host knows for this unit."""
        identity = dev._device_slot_identity()
        slots = {
            str(entry["slot"]): entry["record"]
            for entry in self.device_slot_registry.entries(
                device_identity=identity)
        }
        userpresets = {
            "%d.%s.channel" % (
                entry["user_index"], entry["record"]["preset_name"]):
            entry["record"]
            for entry in self.device_preset_library_registry.entries(
                device_identity=identity)
        }
        return json.loads(json.dumps({
            "slots": slots, "userpresets": userpresets,
        }))

    def _save_scene_clicked(self, _button):
        if self.ctl.dev is None:
            self.say("Scene not saved: io24 is not connected")
            return
        try:
            host_features = snapshot_host_features(
                self._host_features_state())
        except Exception as error:
            self.say("Scene was not saved: %s" % error)
            return
        dialog = Gtk.FileDialog(
            title="Save scene",
            initial_name="io24-host.scene")

        def selected(picker, result):
            try:
                chosen = picker.save_finish(result)
            except GLib.Error:
                return
            path = chosen.get_path()

            def work(dev, destination=path, features=host_features):
                try:
                    preset_state = self._scene_preset_state(dev)
                    scene, omitted = io24_scene.capture(
                        dev, host_features=features, presets=preset_state)
                    scene_rate = Win._voicefx_effective_rate(self)
                    io24_scene.save(
                        destination, scene, sample_rate_hz=scene_rate)
                    _calls, _skips = io24_scene.plan(
                        scene, sample_rate_hz=scene_rate,
                        allow_host_delay=True)
                except Exception as error:
                    GLib.idle_add(
                        self.say, "Scene was not saved: %s" % error)
                    return
                GLib.idle_add(
                    self.say,
                    "Saved %s" % os.path.basename(destination))

            self.ctl.submit(work)

        dialog.save(self, None, selected)

    def _load_scene_clicked(self, _button):
        if self.ctl.dev is None:
            self.say("Scene not loaded: io24 is not connected")
            return
        dialog = Gtk.FileDialog(title="Load scene")

        def selected(picker, result):
            try:
                chosen = picker.open_finish(result)
            except GLib.Error:
                return
            path = chosen.get_path()
            try:
                scene = io24_scene.load(path)
                calls, skips = io24_scene.plan(
                    scene,
                    sample_rate_hz=Win._voicefx_effective_rate(self),
                    allow_host_delay=True)
            except Exception as error:
                self.say("Scene was not loaded: %s" % error)
                return

            def work(dev, source=scene, plan=calls, omitted=skips):
                try:
                    report = io24_scene.apply_transactional(dev, plan)
                except Exception as error:
                    GLib.idle_add(
                        self.say, "Scene was not loaded: %s" % error)
                    return
                if report["failed"]:
                    rollback = report["rollback"]
                    if rollback["complete"]:
                        recovery = "Previous state was restored."
                    else:
                        recovery = (
                            "Rollback was partial: %d prior value%s were "
                            "unknown and %d restore operation%s failed." %
                            (len(rollback["unresolved"]),
                             "" if len(rollback["unresolved"]) == 1 else "s",
                             len(rollback["errors"]),
                             "" if len(rollback["errors"]) == 1 else "s"))
                    GLib.idle_add(
                        self.say,
                        "Scene load failed at %s after %d setting%s. %s" %
                        (report["failed_setting"], report["applied"],
                         "" if report["applied"] == 1 else "s", recovery))
                    return
                mirror = json.loads(json.dumps(
                    getattr(dev, "_shadow", None) or {}))
                message = "Loaded %s" % os.path.basename(path)
                if omitted:
                    message += " · %d setting%s skipped" % (
                        len(omitted), "" if len(omitted) == 1 else "s")
                GLib.idle_add(
                    self._after_scene_load, source, mirror, message)

            self.ctl.submit(work)

        dialog.open(self, None, selected)

    def _after_scene_load(self, scene, mirror, message):
        """Adopt exact semantic scene controls after all writes succeeded."""
        self._after_load(mirror)
        line = scene.get("line") or {}
        fx_owner = next((ch for ch in (1, 2)
                         if (line.get("ch%d" % ch) or {}).get("voicefx")), None)
        for ch in (1, 2):
            record = line.get("ch%d" % ch)
            if not isinstance(record, dict):
                continue
            try:
                self._adopt_preset_record(
                    record, (ch,), fx_channel=ch if ch == fx_owner else None)
            except Exception as error:
                self.say("Scene was applied, but Channel %d controls could not "
                         "be adopted: %s" % (ch, error))
        if fx_owner is not None and Win._record_uses_host_delay(
                self,
                line.get("ch%d" % fx_owner) or {}):
            self._push_fx()
        self.say(message)
        return False

    def _populate_user_presets(self):
        """Fill User Presets: the unit's known blocks, then presets saved here."""
        expander = getattr(self, "user_presets_row", None)
        if expander is None:
            return
        by_origin = self._preset_rows_by_origin
        for row in by_origin["user"]:
            expander.remove(row)
        rows = by_origin["user"] = []
        self._unit_preset_rows = {}
        self._marked_pslot = None
        for entry in getattr(self, "_host_slot_entries", []):
            channel = entry["physical_channel"]
            block = entry["slot"] - self.PRESET_BASE[channel] + 1
            subtitle = "Input %d · Preset button %d" % (channel, block)
            row = Adw.ActionRow(title=entry["record"]["preset_name"],
                                subtitle=subtitle)
            load = lambda e=entry: self._load_device_entry(e)
            row.add_suffix(self._preset_load_button(load))
            self._unit_preset_rows[entry["slot"]] = (row, subtitle)
            rows.append(row)
        for entry in getattr(self, "_host_device_preset_entries", []):
            subtitle = "Input %d · Device preset %d" % (
                entry["physical_channel"], entry["channel_slot"] + 1)
            row = Adw.ActionRow(
                title=entry["record"]["preset_name"], subtitle=subtitle)
            load = lambda e=entry: self._load_device_library_entry(e)
            row.add_suffix(self._preset_load_button(load))
            rows.append(row)
        try:
            saved = self.PR.load_user_presets()
        except (OSError, ValueError) as error:
            saved = {}
            rows.append(Adw.ActionRow(
                title="Your saved presets could not be read",
                subtitle=str(error)))
        for name, record in saved.items():
            row = Adw.ActionRow(title=name,
                                subtitle=self._preset_summary(record))
            load = lambda n=name, r=record: self._load_factory(
                None, n, True, record=r)
            row.add_suffix(self._preset_load_button(load))
            row.add_suffix(self._preset_menu(
                name, record, "user:%s" % name, deletable=True))
            rows.append(row)
        if not rows:
            rows.append(Adw.ActionRow(
                title="No saved presets yet",
                subtitle="Name the current sound above and press Save"))
        for row in rows:
            expander.add_row(row)
        self._mark_playing_unit_blocks(self.ctl.snap.get("preset_slot"))
        self._filter_presets()

    def _populate_factory_presets(self):
        """Fill Factory Presets from Universal Control's recovered library."""
        expander = getattr(self, "factory_presets_row", None)
        if expander is None:
            return
        by_origin = self._preset_rows_by_origin
        for row in by_origin["factory"]:
            expander.remove(row)
        rows = by_origin["factory"] = []
        if self._factory_unavailable:
            rows.append(Adw.ActionRow(title="Factory presets are not present",
                                      subtitle=self._factory_unavailable))
        records = self.PR.load() if self.factory_names else {}
        for name in self.factory_names:
            record = records[name]
            supported, reason = self.PR.direct_apply_support(record)
            summary = self._preset_summary(record)
            row = Adw.ActionRow(
                title=name,
                subtitle=summary if supported
                else "%s — %s" % (summary, reason))
            if supported:
                load = lambda n=name: self._load_factory(None, n, True)
                row.add_suffix(self._preset_load_button(load))
                row.add_suffix(self._preset_menu(
                    name, record, "factory:%s" % name))
            rows.append(row)
        for row in rows:
            expander.add_row(row)
        self._filter_presets()

    def _preset_summary(self, record):
        try:
            return self.PR.describe(record)
        except Exception:
            return ""

    @staticmethod
    def _preset_load_button(callback):
        """The single-click Host preset recall action."""
        button = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", lambda _button: callback())
        return button

    def _preset_menu(self, name, record, source, deletable=False):
        """One preset's actions: UC Device Presets store, or local delete."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        popover = Gtk.Popover(child=box)

        def item(label, action):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")

            def clicked(_button):
                popover.popdown()
                action()

            button.connect("clicked", clicked)
            box.append(button)
            return button

        button = item(
            "Save to device",
            lambda: self._put_in_device_library(name, record, source))
        if PUT_ON_UNIT_UNAVAILABLE:
            button.set_sensitive(False)
            button.set_tooltip_text(PUT_ON_UNIT_UNAVAILABLE)
        if deletable:
            item("Delete", lambda: self._confirm_user_delete(name))
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", popover=popover,
                              valign=Gtk.Align.CENTER)
        menu.add_css_class("flat")
        menu.set_tooltip_text(PUT_ON_UNIT_UNAVAILABLE or "Preset actions")
        return menu

    def _filter_presets(self):
        """Filter both drop-downs by name and description."""
        search = getattr(self, "factory_search", None)
        query = search.get_text() if search is not None else ""
        for rows in getattr(self, "_preset_rows_by_origin", {}).values():
            for row in rows:
                row.set_visible(factory_preset_matches(
                    query, row.get_title(), row.get_subtitle() or ""))
        if query.strip():
            for expander in (getattr(self, "user_presets_row", None),
                             getattr(self, "factory_presets_row", None)):
                if expander is not None:
                    expander.set_expanded(True)

    def _mark_playing_unit_blocks(self, pslot):
        """Mark each unit preset whose block its channel is playing."""
        if not isinstance(pslot, (list, tuple)) or len(pslot) != 2:
            return
        if list(pslot) == getattr(self, "_marked_pslot", None):
            return
        self._marked_pslot = list(pslot)
        for slot, (row, subtitle) in getattr(
                self, "_unit_preset_rows", {}).items():
            row.set_subtitle(subtitle + (" · Playing" if slot in pslot else ""))

    def _load_device_entry(self, entry):
        """Load a preset that lives on the unit.

        On its own channel the Host selects the physical block, then directly
        sends the retained Fat Channel and VoiceFX record. Selection alone did
        not reapply the unreadable body in the retained live test. Anywhere
        else the Host applies the same record directly, as a factory preset.
        """
        channel = entry["physical_channel"]
        name = entry["record"].get("preset_name", "that preset")
        target = self._factory_target_channel()
        chans = (1, 2) if self.link_both else (target,)
        if channel in chans:
            self._recall_host_preset(None, entry)
            others = tuple(ch for ch in chans if ch != channel)
            if others:
                self._load_factory(None, name, False, record=entry["record"],
                                   channels=others)
        else:
            self._load_factory(None, name, True, record=entry["record"],
                               channels=chans)

    def _load_device_library_entry(self, entry):
        """UC-equivalent RestorePreset: replay a retained library body."""
        name = entry["record"].get("preset_name", "that preset")
        target = self._factory_target_channel()
        channels = (1, 2) if self.link_both else (target,)
        self._load_factory(
            None, name, True, record=entry["record"], channels=channels)

    def _slot_base_name(self):
        """The factory record that supplies fields the Host does not expose."""
        names = getattr(self, "factory_names", [])
        if not names:
            raise io24.HostActionError(
                "the factory preset data is not present, so there is no "
                "complete base to save onto")
        return "Broadcast" if "Broadcast" in names else names[0]

    def _save_user_preset_clicked(self, _button):
        """Save the Load into channel's current sound as one of your presets."""
        try:
            name = self._current_slot_name()
            target = self._factory_target_channel()
            record = self._current_slot_record(
                self._slot_base_name(), target, name, strict_voicefx=False)
            exists = name in self.PR.load_user_presets()
        except Exception as error:
            self.say("Preset not saved: %s" % error)
            return
        if exists:
            self._confirm_user_overwrite(name, record)
            return
        self._store_user_preset(name, record)

    def _store_user_preset(self, name, record):
        try:
            self.PR.save_user_preset(name, record)
        except Exception as error:
            self.say("Preset not saved: %s" % error)
            return
        self._populate_user_presets()
        self.say("Saved %s to your presets" % name)

    def _confirm(self, heading, body, verb, action):
        dialog = Adw.MessageDialog.new(self, heading, body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", verb)
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response == "go":
                action()

        dialog.connect("response", responded)
        dialog.present()

    def _confirm_user_overwrite(self, name, record):
        self._confirm(
            "Replace %s?" % name,
            "One of your presets already has this name. Saving replaces it "
            "with the current sound.",
            "Replace", lambda: self._store_user_preset(name, record))

    def _confirm_user_delete(self, name):
        self._confirm(
            "Delete %s?" % name,
            "It is removed from your presets on this computer. A copy you put "
            "on the device stays there.",
            "Delete", lambda: self._delete_user_preset(name))

    def _delete_user_preset(self, name):
        try:
            self.PR.delete_user_preset(name)
        except Exception as error:
            self.say("%s was not deleted: %s" % (name, error))
            return
        self._populate_user_presets()
        self.say("Deleted %s" % name)

    def _prepare_device_library_store(self, name, record, source):
        """Resolve one of UC's six per-input ``PrsM`` destinations."""
        if self.ctl.dev is None:
            raise io24.HostActionError("io24 is not connected")
        channel = self._factory_target_channel()
        row = getattr(self, "device_preset_slot", None)
        channel_slot = int(row.get_selected()) if row is not None else 0
        if not 0 <= channel_slot < 6:
            raise io24.HostActionError(
                "Device Presets destination must be Preset 1 through 6")
        user_index = (16 if channel == 1 else 22) + channel_slot
        body = complete_device_slot_record(record)
        body["preset_name"] = name
        return {
            "name": name, "record": body, "source": source,
            "target": channel, "channel_slot": channel_slot,
            "user_index": user_index,
        }

    def _put_in_device_library(self, name, record, source):
        """Confirm and queue UC's actual Device Presets Store transaction."""
        try:
            plan = self._prepare_device_library_store(
                name, record, source)
        except Exception as error:
            self.say("%s was not sent to Device Presets: %s" % (name, error))
            return
        replaced = next((
            entry["record"]["preset_name"]
            for entry in getattr(self, "_host_device_preset_entries", [])
            if entry["user_index"] == plan["user_index"]), None)
        destination = "Input %d · Device preset %d" % (
            plan["target"], plan["channel_slot"] + 1)
        warning = ("This may replace %s." % replaced if replaced else
                   "This may replace the preset currently stored there.")
        self._confirm(
            "Save %s to %s?" % (name, destination), warning,
            "Save", lambda: self._queue_device_library_store(plan))

    def _queue_device_library_store(self, plan):
        def work(dev, prepared=plan):
            try:
                dev.save_known_device_library_preset(
                    prepared["user_index"], prepared["record"],
                    self.device_preset_library_registry, prepared["source"])
                message = "Sent %s to Input %d · Device preset %d" % (
                    prepared["name"], prepared["target"],
                    prepared["channel_slot"] + 1)
            except Exception as error:
                report = getattr(error, "device_preset_transport", None)
                message = (
                    "Preset was not sent: %s" % error
                    if report is None else
                    "Preset was sent, but its local copy could not be saved: %s"
                    % error)
                GLib.idle_add(self.say, message)
                return
            GLib.idle_add(self._device_library_saved, message)

        self.ctl.submit(work)

    def _device_library_saved(self, message):
        self._refresh_host_slot_entries()
        self.say(message)
        return False

    def _prepare_record_slot_store(self, name, record, relative_slot, source):
        """Plan writing one preset into a block of the Load into channel."""
        plan = self._prepare_slot_store_target(relative_slot=relative_slot)
        body = complete_device_slot_record(record)
        body["preset_name"] = name
        plan.update({"name": name, "record": body, "source": source,
                     "activate": False})
        return plan

    def _put_on_unit(self, name, record, relative_slot, source):
        """Confirm, then send one firmware-native Fat Channel candidate."""
        try:
            plan = self._prepare_record_slot_store(
                name, record, relative_slot, source)
        except Exception as error:
            self.say("%s was not saved to the device: %s" % (name, error))
            return
        replaced = next((entry["record"]["preset_name"]
                         for entry in getattr(self, "_host_slot_entries", [])
                         if entry["slot"] == plan["slot"]), None)
        where = "Input %d · Preset button %d" % (
            plan["target"], plan["relative_slot"] + 1)
        body = ("This may replace %s." % replaced if replaced else
                "This may replace the preset currently stored there.")
        self._confirm("Save %s to %s?" % (name, where), body,
                      "Replace",
                      lambda: self._queue_factory_slot_store(plan))

    def _preset_button_group(self):
        """How many blocks the unit's Preset button steps through."""
        g = Adw.PreferencesGroup(title="Preset button")
        # Guards programmatic updates so they are never echoed back as writes.
        self._preset_sync = False
        # 'Pari' wire 17 (internal id 2). Write-only: nothing in the state
        # blob reports it, so this row commands and cannot follow the unit.
        self.preset_mode_row = Adw.ComboRow(
            title="Available presets",
            model=Gtk.StringList.new(["One", "Two", "None"]))
        self.preset_mode_row.set_selected(1)
        self.preset_mode_row.connect("notify::selected",
                                     self._preset_mode_changed)
        g.add(self.preset_mode_row)
        return g

    def _factory_target_channel(self):
        row = getattr(self, "factory_target", None)
        return 2 if row is not None and row.get_selected() == 1 else 1

    def _prepare_factory_slot_store(self, name):
        """Validate one complete, currently inactive factory-slot write."""
        target_plan = self._prepare_slot_store_target()
        target = target_plan["target"]
        relative_slot = target_plan["relative_slot"]
        slot = target_plan["slot"]
        try:
            record = complete_device_slot_record(self.PR.load()[name])
        except KeyError as error:
            raise io24.HostActionError(
                "factory preset %r is unavailable" % name) from error
        return {
            "name": name,
            "target": target,
            "relative_slot": relative_slot,
            "slot": slot,
            "record": record,
            "source": "factory:%s" % name,
            "activate": False,
            "sample_rate_hz": target_plan["sample_rate_hz"],
        }

    def _current_slot_name(self):
        """The user's own name for the body about to be written."""
        row = getattr(self, "slot_name_row", None)
        text = row.get_text().strip() if row is not None else ""
        if not text:
            raise io24.HostActionError(
                "name this preset before saving; the Host preset list finds "
                "a stored body by name and the device cannot return one")
        if len(text.encode("utf-8")) > 0x7F:
            raise io24.HostActionError(
                "preset name is too long for the record's short text form "
                "(127 bytes)")
        return text

    def _prepare_slot_store_target(self, relative_slot=None):
        """Return one connected, live, inactive device-slot destination."""
        if self.ctl.dev is None:
            raise io24.HostActionError("io24 is not connected")
        if not self.ctl.snap.get("alive"):
            raise io24.HostActionError(
                "live device slot state is not available yet")
        target = self._factory_target_channel()
        if relative_slot is None:
            relative_slot = int(self.factory_device_slot.get_selected())
        if relative_slot not in (0, 1):
            raise io24.HostActionError("device slot must be Slot 1 or Slot 2")
        slot = self.PRESET_BASE[target] + relative_slot
        selected = self.ctl.snap.get("preset_slot")
        if not isinstance(selected, (list, tuple)) or len(selected) != 2:
            raise io24.HostActionError("live device slot selection is unavailable")
        if selected[target - 1] == slot:
            raise io24.HostActionError(
                "Channel %d device Slot %d is currently selected; choose the "
                "other slot before overwriting a body" %
                (target, relative_slot + 1))
        return {
            "target": target,
            "relative_slot": relative_slot,
            "slot": slot,
            "sample_rate_hz": getattr(
                self, "_selected_rate", DEFAULT_SAMPLE_RATE),
        }

    def _record_voicefx(self, target, strict=True):
        """The one shared FX state carried by either channel's saved sound."""
        _ = (target, strict)          # retained for call-site compatibility
        fx_model = self.FX_ORDER[max(
            0, min(len(self.FX_ORDER) - 1, self.fx_model.get_selected()))]
        voicefx = self._fx_live_params()
        # Test doubles and old session adapters may still return parameters
        # without ``on``. Production _fx_live_params includes the selected
        # model's own XML On value.
        voicefx.setdefault("on", self.fx_arm.get_active())
        return fx_model, voicefx

    def _record_uses_host_delay(self, record):
        """Whether a preset/scene Voice FX belongs on the safe Host path."""
        fx = (record or {}).get("voicefx") or {}
        if not fx:
            return False
        try:
            model, _kwargs = io24_fx.voicefx_preset_call(fx)
        except Exception:
            return False
        return model == "delay" and io24_fx.delay_needs_host_fallback(
            Win._voicefx_effective_rate(self))

    def _current_slot_record(self, base_name, target, preset_name,
                             strict_voicefx=True):
        """Build a complete slot body from the current visible controls."""
        fx_model, voicefx = self._record_voicefx(target, strict_voicefx)
        W = self.w[target]
        gate = {
            "on": self.dyn_by_ch[target]["gate"],
            "threshold_db": W["gth"].get_value(),
            "range_db": W["grange"].get_value(),
            "attack_s": W["gatk"].get_value(),
            "release_s": W["grel"].get_value(),
            "keyfilter_hz": (0.0 if W["gkey"].get_value() <= 41
                              else W["gkey"].get_value()),
            "keylisten": W["gklisten"].get_active(),
            "expander": W["gexp"].get_active(),
        }
        comp_model, compressor = self._compressor_kwargs(target)
        # Multiband runs on the computer; a block keeps the unit's
        # compressor off, which is what the unit is doing under it.
        compressor["on"] = (self.dyn_by_ch[target]["comp"]
                            and not self._multiband_selected(target))
        limiter = {
            "on": self.dyn_by_ch[target]["lim"],
            "threshold_db": W["lth"].get_value(),
        }
        try:
            base = self.PR.load()[base_name]
        except KeyError as error:
            raise io24.HostActionError(
                "complete-body base %r is unavailable" % base_name) from error
        # A selected Passive/Vintage model keeps its own semantic controls;
        # never replace it with the hidden Standard bands while saving.
        alt = self._alt_eq(target)
        return self.PR.current_slot_record(
            base, preset_name,
            bands=[dict(band) for band in self.bands_by_ch[target]],
            eq_on=self.eq_enabled(target),
            hpf_hz=self.hpf_by_ch[target],
            eq_first=self.order_by_ch[target],
            gate=gate, compressor_model=comp_model,
            compressor=compressor, limiter=limiter,
            voicefx_model=fx_model, voicefx=voicefx,
            alternate_eq=None if alt is None else alt["eq"])

    def _queue_factory_slot_store(self, plan):
        """Queue the confirmed body write, its registry commit, and — only when
        the user asked to activate — the recall that actually loads it.

        The write and the recall stay separate device operations. A failed
        recall never retracts a body that did reach the device, and it is
        reported as exactly that rather than as a failed save.
        """
        def work(dev, prepared=plan):
            try:
                dev.save_known_device_slot(
                    prepared["slot"], prepared["record"],
                    self.device_slot_registry, prepared["source"],
                    sample_rate_hz=prepared.get(
                        "sample_rate_hz", DEFAULT_SAMPLE_RATE))
                msg = "Sent %s to Input %d · Preset button %d" % (
                    prepared["name"], prepared["target"],
                    prepared["relative_slot"] + 1)
            except Exception as error:
                report = getattr(error, "device_slot_transport", None)
                if report is None:
                    msg = "Preset was not sent: %s" % error
                else:
                    msg = ("Preset was sent, but its local copy could not be "
                           "saved: %s" % error)
                GLib.idle_add(self.say, msg)
                return

            if not prepared.get("activate"):
                GLib.idle_add(self._slot_saved, prepared,
                              msg + " · Use Load to hear it")
                return
            try:
                recall = dev.recall_device_slot(
                    prepared["target"], prepared["slot"])
                recall = self._host_apply_slot_record(
                    dev, prepared["target"], prepared["record"], recall)
            except Exception as error:
                GLib.idle_add(self._slot_saved, prepared,
                              msg + " · Load failed: %s" % error)
                return
            GLib.idle_add(self._slot_saved, prepared, msg, recall)

        self.ctl.submit(work)

    def _slot_saved(self, prepared, message, recall=None):
        """List the new body, then follow the recall when one was made."""
        self._refresh_host_slot_entries()
        self.say(message)
        if recall is not None:
            self._slot_recalled(prepared["target"], prepared["slot"], recall,
                                prepared["name"])
        return False

    def _factory_slot_store_response(self, _dialog, response, plan):
        if response == "store":
            self._queue_factory_slot_store(plan)

    def _store_factory_slot_clicked(self, _button, name):
        try:
            plan = self._prepare_factory_slot_store(name)
        except Exception as error:
            self.say("Could not save to device: %s" % error)
            return
        dialog = Adw.MessageDialog.new(
            self,
            "Replace Input %d · Preset button %d?" %
            (plan["target"], plan["relative_slot"] + 1),
            "%s may replace the preset currently stored there."
            % plan["name"])
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("store", "Replace")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance(
            "store", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._factory_slot_store_response, plan)
        dialog.present()

    def _adopt_preset_record(self, pr, chans, fx_channel=None):
        """Show one complete preset record on the visible controls.

        Shared by the factory strip load and by a Host-assisted known-slot
        load. After direct Host replay, leaving the previous values on screen
        would describe a sound the Host is no longer sending.
        ``fx_channel`` additionally adopts the record's stored Voice FX, which
        only the recall path wants: an ordinary strip load must leave the
        singleton engine alone.
        """
        if getattr(self, "PR", None) is None:
            return False
        alternate_view_fn = getattr(self.PR, "alternate_eq_view", None)
        alternate_view = alternate_view_fn(pr) \
            if callable(alternate_view_fn) else None
        shown = pr if alternate_view is None else {
            key: value for key, value in pr.items() if key != "eq"
        }
        if not hasattr(self, "eq_on_by_ch"):
            self.eq_on_by_ch = {}
        for ch in chans:
            self.bands_by_ch[ch] = self.PR.to_bands(shown)
            enabled = getattr(self.PR, "standard_eq_enabled", None)
            if alternate_view is not None:
                # Keep the hidden Standard state; the selected alternate owns
                # the visible power switch and exact semantic controls.
                self.eq_on_by_ch.setdefault(ch, False)
            elif callable(enabled):
                try:
                    self.eq_on_by_ch[ch] = enabled(shown)
                except io24_presets.UnsupportedPresetModel:
                    self.eq_on_by_ch[ch] = False
            else:
                self.eq_on_by_ch[ch] = any(
                    band.get("shape") != "off"
                    for band in self.bands_by_ch[ch])
            self.dyn_by_ch[ch] = {
                "gate": bool(pr.get("gate", {}).get("on")),
                "comp": bool(pr.get("comp", {}).get("on")),
                "lim": bool(pr.get("limit", {}).get("limiteron"))}
            self.hpf_by_ch[ch] = float(
                pr.get("filter", {}).get("hpf", 24.0))
            self.order_by_ch[ch] = bool(
                pr.get("opt", {}).get("swapcompeq", False))
        self.invalidate_curve()
        self._adopt_mute = True
        try:
            for ch in chans:
                self._adopt_band(ch)
                if "eq_on" in self.w[ch]:
                    self.w[ch]["eq_on"].set_active(
                        self.eq_on_by_ch.get(ch, False))
                self._adopt_hpf_controls(ch, self.hpf_by_ch[ch])
                self._adopt_dynamics(ch, pr)
                for key in ("gate", "comp", "lim"):
                    self.w[ch][key + "_on"].set_active(
                        self.dyn_by_ch[ch][key])
                self.w[ch]["curve"].queue_draw()
                show_alternate = getattr(self, "_show_alternate_eq", None)
                if callable(show_alternate):
                    show_alternate(ch, alternate_view)
            self._sync_order_row(self._current_channel())
        finally:
            self._adopt_mute = False
        for rack in self.racks.values():
            rack.queue_draw()
        if fx_channel is not None:
            self._adopt_record_voicefx(pr, fx_channel)
        self._insert_reconcile()     # a loaded compressor replaces Multiband
        return True

    def _adopt_record_voicefx(self, pr, channel):
        """Show a record's assigned Voice FX state. Sends nothing to the device.

        A body the Host cannot model — an archive Voice FX class it has no
        builder for — leaves the controls untouched rather than inventing a
        state the record never specified.
        """
        import io24_fx
        fx = pr.get("voicefx") or {}
        try:
            model, kwargs = io24_fx.voicefx_preset_call(fx)
        except Exception:
            return False
        if model not in self.FX_ORDER:
            return False
        prior = self._fx_mute
        self._fx_mute = True         # adoption must not echo back as a write
        try:
            self.fx_target.set_selected(channel - 1)
            self.fx_model.set_selected(self.FX_ORDER.index(model))
            if getattr(self, "fx_param_stack", None) is not None:
                self.fx_param_stack.set_visible_child_name(model)
            for name, control in self.fx_params.get(model, {}).items():
                if name in kwargs:
                    control.set_value(kwargs[name])
            self.fx_arm.set_active(bool(kwargs.get("on")))
            if getattr(self, "fx_visual", None) is not None:
                self.fx_visual.set_model(model)
        finally:
            self._fx_mute = prior
        self._remember_voicefx_target(channel)
        return True

    def _load_factory(self, _b, name, with_fx=False, record=None,
                      channels=None):
        pr = record if record is not None else self.PR.load()[name]
        supported, reason = self.PR.direct_apply_support(pr)
        if not supported:
            self.say("%s was not loaded: %s" % (name, reason))
            return
        target = self._factory_target_channel()
        if channels is not None:
            chans = tuple(channels)
        else:
            chans = (1, 2) if self.link_both else (target,)
        def work(dev):
            fx_command_sent = False
            fx_hosted = False
            fx_error = None
            try:
                # The Fat Channel is the preset's reliable core and must not be
                # rolled back or reported as failed when the separate global
                # FX activation handshake is unavailable.
                for ch in chans:
                    self.PR.apply_preset(
                        dev, pr, ch, with_fx=False,
                        fs=getattr(self, "_fs", DEFAULT_SAMPLE_RATE))
            except Exception as error:
                GLib.idle_add(
                    self.say, "%s load failed: %s" % (name, error))
                return

            if with_fx:
                try:
                    fx_hosted = Win._record_uses_host_delay(self, pr)
                    if not fx_hosted:
                        fx_report = self.PR.apply_voicefx(
                            dev, pr, channel=target,
                            fs=getattr(self, "_fs", DEFAULT_SAMPLE_RATE))
                        fx_command_sent = fx_report is not None
                except Exception as error:
                    fx_error = str(error)

            def finished():
                if fx_command_sent or fx_hosted:
                    # Adopt the requested controls, but do not turn a USB ACK
                    # into an audibility claim.
                    self._adopt_preset_record(pr, chans, fx_channel=target)
                    if fx_hosted:
                        self._push_fx()
                else:
                    self._adopt_preset_record(pr, chans)
                channel_text = " and ".join(str(ch) for ch in chans)
                message = "Loaded %s on Channel%s %s" % (
                    name, "s" if len(chans) > 1 else "", channel_text)
                if fx_error:
                    message += "; FX not loaded: %s" % fx_error
                elif fx_hosted:
                    message += " · Voice FX hosted at 96 kHz"
                elif fx_command_sent:
                    message += " · Voice FX updated"
                self.say(message)
                return False

            GLib.idle_add(finished)

        self.ctl.submit(work)

    def _srow(self, title, lo, hi, step, val, fmt, cb, subtitle=None,
              curve=None, mid=None):
        row = Adw.ActionRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        widget = fader(0.0, 1.0, 0.001, vertical=False) \
            if curve == "skew" else fader(lo, hi, step, vertical=False)
        sc = (_SkewValue(widget, lo, hi, mid)
              if curve == "skew" else widget)
        sc.set_value(val); widget.set_size_request(260, -1)
        lbl = Gtk.Label(label=fmt(val), xalign=1.0)
        lbl.add_css_class("numeric"); lbl.add_css_class("dim-label")
        lbl.set_size_request(80, -1)
        thr = Throttle(cb, 0.002)

        def ch(_widget):
            value = sc.get_value()
            lbl.set_text(fmt(value))
            for c in getattr(self, "w", {}):
                self.w[c]["curve"].queue_draw()
                self.w[c]["comp_curve"].queue_draw()
            # Adoption is a display update, not a delayed user write.  Guard
            # before entering Throttle so no callback can escape after the
            # adoption flag is lowered.
            if not getattr(self, "_adopt_mute", False):
                thr(value)
        widget.connect("value-changed", ch)

        # Double-click resets to this control's own default. Every fader is
        # built here with its nominal value already in hand, so the behaviour
        # comes for free and is consistent across the whole app rather than
        # being remembered per-control. `val` is captured, so a slider always
        # returns to what it was designed to sit at, not to wherever it started
        # this session.
        reset_value = (sc.position_from_value(val)
                       if isinstance(sc, _SkewValue) else val)
        reset_format = ((lambda position: fmt(sc.value_from_position(position)))
                        if isinstance(sc, _SkewValue) else fmt)
        reset_on_double_click(widget, reset_value, reset_format)
        row.set_tooltip_text("Double-click to reset to %s" % fmt(val))

        row.add_suffix(widget); row.add_suffix(lbl)
        return row, sc

    # ------------------------------------------------------------ auto gain
    AUTOGAIN_TICK_MS = 50

    def _autogain_toggled(self, button, ch):
        """UC's per-input Automatic Preamp Gain switch; linked inputs share it.

        Silent by design: switching it on is the whole interaction, and the
        corrections that follow are shown only by the gain fader moving.
        """
        if getattr(self, "_autogain_sync", False):
            return
        on = button.get_active()
        chans = (1, 2) if getattr(self, "link_both", False) else (ch,)
        if not hasattr(self, "_autogain_on"):
            self._autogain_on = {1: False, 2: False}
        self._autogain_sync = True
        try:
            for c in chans:
                self._autogain_on[c] = on
                toggle = getattr(self, "autogain_toggles", {}).get(c)
                if toggle is not None and toggle.get_active() != on:
                    toggle.set_active(on)
                fader = getattr(self, "gain_faders", {}).get(c)
                if fader is not None:
                    # the controller owns the gain while it is on
                    fader.w.set_sensitive(not on)
        finally:
            self._autogain_sync = False
        if any(self._autogain_on.values()) and \
                getattr(self, "_autogain_timer", None) is None:
            self._autogain_timer = GLib.timeout_add(
                self.AUTOGAIN_TICK_MS, self._autogain_tick)

    def _adopt_autogain(self, state):
        """Switch Auto back on for the inputs that had it.

        Goes through the buttons, so linking, the locked fader and the
        controller start exactly as a click would. Only ever switches on: a
        session without it leaves Auto as it is, which at launch is off.
        Returns a completed-load notice when the saved value is unusable.
        """
        if state is None:
            return None
        if not isinstance(state, dict) or not isinstance(state.get("on"), list):
            return "auto gain was not restored; the saved value was unreadable"
        try:
            channels = [int(c) for c in state["on"]]
        except (TypeError, ValueError):
            return "auto gain was not restored; the saved value was unreadable"
        if not channels or any(c not in (1, 2) for c in channels):
            return "auto gain was not restored; unknown input"
        toggles = getattr(self, "autogain_toggles", {})
        for c in channels:
            toggle = toggles.get(c)
            if toggle is not None:
                toggle.set_active(True)
        return None

    def _autogain_groups(self):
        on = tuple(c for c in (1, 2)
                   if getattr(self, "_autogain_on", {}).get(c))
        if getattr(self, "link_both", False) and on == (1, 2):
            return [on]
        return [(c,) for c in on]

    def _autogain_tick(self):
        groups = self._autogain_groups()
        if not groups:
            self._autogain_timer = None
            self._autogain_ctl = {}
            return False
        s = self.ctl.snap
        if not s["alive"]:
            # nothing to hear; start fresh when the unit comes back
            self._autogain_ctl = {}
            return True
        previous = getattr(self, "_autogain_ctl", {})
        self._autogain_ctl = {g: previous.get(g) or AutoGain(g) for g in groups}
        now = time.monotonic()
        for group, controller in self._autogain_ctl.items():
            gain = controller.step(now, {c: s["in"][c - 1] for c in group},
                                   {c: s["gain"][c - 1] for c in group})
            if gain is not None:
                for c in group:
                    self._set("gain", c, gain)
        return True

    # ------------------------------------------------------------- actions
    def _set(self, param, channel, value):
        ch = channel or 1
        self.ctl.submit(lambda dev: self.ctl.dev.apply(param, ch, value, {}))

    def _phones_source_changed(self, row, _pspec):
        if getattr(self, "_phones_source_mute", False):
            return
        sources = ("main", "mixa", "mixb")
        selected = max(0, min(2, int(row.get_selected())))
        source = sources[selected]
        self._set("phonesrc", None, source)

    def select_band(self, i, ch=1):
        self.w[ch]["bands"][i].set_active(True)

    def _band_toggled(self, btn, idx, ch):
        if not btn.get_active():
            return
        for i, b in enumerate(self.w[ch]["bands"]):
            if i != idx:
                b.set_active(False)
        self.cur_band_by_ch[ch] = idx
        self._adopt_band(ch)
        self.w[ch]["curve"].queue_draw()

    def _adopt_band(self, ch):
        index = self.cur_band_by_ch[ch]
        b = self.bands_by_ch[ch][index]
        mode = io24_presets.standard_band_mode(b, index)
        band_on = io24_presets.standard_band_enabled(b)
        W = self.w[ch]
        prior = self._adopt_mute
        self._adopt_mute = True
        try:
            if "eq_on" in W:
                W["eq_on"].set_active(self.eq_enabled(ch))
            W["band_on"].set_title("%s band" % BAND_NAMES[index])
            W["band_on"].set_active(band_on)
            outer = index in (0, 3)
            W["shelf"].set_visible(outer)
            if outer:
                shelf = "lowshelf" if index == 0 else "highshelf"
                W["shelf"].set_title(
                    "Low shelf" if index == 0 else "High shelf")
                W["shelf"].set_active(mode == shelf)
            W["freq"].set_value(b["freq"])
            W["gain"].set_value(b["gain"])
            W["q"].set_value(b["q"])
        finally:
            self._adopt_mute = prior

    def _band_power_changed(self, row, _p, ch):
        if self._adopt_mute:
            return
        index = self.cur_band_by_ch[ch]
        band = self.bands_by_ch[ch][index]
        mode = io24_presets.standard_band_mode(band, index)
        band["mode"] = mode
        band["on"] = bool(row.get_active())
        band["shape"] = mode if band["on"] else "off"
        self._push_band(ch)

    def _shelf_changed(self, row, _p, ch):
        if self._adopt_mute:
            return
        index = self.cur_band_by_ch[ch]
        if index not in (0, 3):
            return
        band = self.bands_by_ch[ch][index]
        shelf = "lowshelf" if index == 0 else "highshelf"
        mode = shelf if row.get_active() else "peaking"
        band["mode"] = mode
        if io24_presets.standard_band_enabled(band):
            band["shape"] = mode
        self._push_band(ch)

    def _band_set(self, key, v, ch):
        self.bands_by_ch[ch][self.cur_band_by_ch[ch]][key] = v
        if self._adopt_mute:
            return
        self._push_band(ch)

    def _link_changed(self, row, _p):
        on = row.get_active()
        self.link_both = on
        # Drive the hardware too. Without this the app claimed the channels were
        # linked while the device still had two independent mono inputs.
        if not getattr(self, "_link_mute", False):
            self.ctl.submit(lambda dev: dev.set_channel_link(on))
        self._rebuild_chains()
        # when linked, channel 2 mirrors channel 1, so its column is redundant
        self.columns[2].set_sensitive(not self.link_both)
        self.col_titles[2].set_label(
            "Channel 2  (mirroring channel 1)" if self.link_both else "Channel 2")

    def _sync_link_row(self):
        """Follow the device's own stereo-link state (JaSt slot 42 bit 12).

        The link can be changed on the hardware, so the switch has to track it
        rather than only command it. `_link_mute` stops the resulting UI update
        from being echoed straight back to the device.
        """
        row = getattr(self, "link_row", None)
        if row is None:
            return
        dev_on = bool(self.ctl.snap.get("link"))
        if dev_on != row.get_active():
            self._link_mute = True
            try:
                row.set_active(dev_on)
            finally:
                self._link_mute = False

    def _current_channel(self):
        stack = getattr(self, "chan_stack", None)
        if stack is None:
            return 1
        try:
            return int(stack.get_visible_child_name())
        except (TypeError, ValueError):
            return 1

    def _sync_order_row(self, ch):
        row = getattr(self, "order_row", None)
        if row is None:
            return
        self._order_mute = True
        try:
            row.set_selected(1 if self.order_by_ch.get(ch, False) else 0)
        finally:
            self._order_mute = False

    def _order_changed(self, row, _p):
        if self._order_mute or self._adopt_mute:
            return
        ch = self._current_channel()
        eq_first = row.get_selected() == 1
        for c in ((1, 2) if self.link_both else (ch,)):
            self.order_by_ch[c] = eq_first
        self.set_order(ch, eq_first)

    def _push_band(self, ch):
        self.invalidate_curve()
        i = self.cur_band_by_ch[ch]
        for c in self._eq_write_targets(ch):
            if c != ch:
                self.bands_by_ch[c][i] = dict(self.bands_by_ch[ch][i])
                self._adopt_band(c)
            band = self._effective_eq_band(c, i)
            self.ctl.submit(
                lambda dev, x=c, j=i, bb=band, fs=self._fs:
                dev.set_eq_band(x, j, bb["shape"], bb["freq"], bb["gain"],
                                bb["q"], fs=fs))
        for c in self.w:
            self.w[c]["curve"].queue_draw()

    def _eq_flat(self, ch):
        self.invalidate_curve()
        for c in self._eq_write_targets(ch):
            for index, band in enumerate(self.bands_by_ch[c]):
                band["mode"] = io24_presets.standard_band_mode(band, index)
                band["on"] = False
                band["shape"] = "off"
            self.ctl.submit(lambda dev, x=c: dev.eq_off(x))
            prior = self._adopt_mute
            self._adopt_mute = True
            try:
                self.w[c]["band_on"].set_active(False)
            finally:
                self._adopt_mute = prior
            self.w[c]["curve"].queue_draw()

    # Exact public field contract used by the exhaustive parameter inventory.
    # Do not merge these sets merely because all three builders emit cpxt.
    COMPRESSOR_COMMON_FIELDS = ("on", "model", "keyfilter_hz", "keylisten")
    COMPRESSOR_MODEL_FIELDS = {
        0: ("threshold_db", "ratio", "attack_s", "release_s", "gain_db",
            "softknee", "automode"),
        1: ("peak", "gain", "limit_mode"),
        2: ("input_db", "output_db", "attack_s", "release_s", "ratio_index"),
    }

    def _comp_model_changed(self, ch):
        W = self.w[ch]
        model = max(0, min(MULTIBAND_MODEL, W["model"].get_selected()))
        W["comp_param_stack"].set_visible_child_name(COMP_MODELS[model])
        # the key filter and listen rows belong to the unit's compressors
        for name in ("ckey_row", "cklisten"):
            if name in W:
                W[name].set_visible(model != MULTIBAND_MODEL)
        # The single transfer curve has no truthful meaning for four parallel
        # bands; the rack miniature above represents all four instead.
        W["comp_curve"].set_visible(model != MULTIBAND_MODEL)
        W["comp_curve"].queue_draw()
        if not self._adopt_mute:
            self._dyn_push("comp", ch)

    def _compressor_kwargs(self, ch):
        """Return the exact selected compressor builder call."""
        W = self.w[ch]
        # Under Multiband the unit's compressor is off; its Standard settings
        # are kept, unused, for when a device model is chosen again.
        model = max(0, min(2, W["model"].get_selected()))
        if W["model"].get_selected() == MULTIBAND_MODEL:
            model = 0
        common = {
            "on": True,
            "keyfilter_hz": (0.0 if W["ckey"].get_value() <= 41
                             else W["ckey"].get_value()),
            "keylisten": W["cklisten"].get_active(),
        }
        if model == 0:
            specific = {
                "threshold_db": W["cth"].get_value(),
                "ratio": W["rat"].get_value(),
                "attack_s": W["catk"].get_value(),
                "release_s": W["crel"].get_value(),
                "gain_db": W["mk"].get_value(),
                "softknee": W["knee"].get_active(),
                "automode": W["auto"].get_active(),
            }
        elif model == 1:
            specific = {
                "peak": W["tpeak"].get_value(),
                "gain": W["tgain"].get_value(),
                "limit_mode": W["climit"].get_active(),
            }
        else:
            specific = {
                "input_db": W["finput"].get_value(),
                "output_db": W["foutput"].get_value(),
                "attack_s": W["fatk"].get_value(),
                "release_s": W["frel"].get_value(),
                "ratio_index": W["fratio"].get_selected(),
            }
        specific.update(common)
        return model, specific

    def compressor_transfer(self, ch):
        """Transfer parameters decoded from the same cpxt blob we transmit."""
        if self._multiband_selected(ch):
            return 0.0, 1.0, 0.0, "Multiband"   # the unit's compressor is off
        model, kw = self._compressor_kwargs(ch)
        builder = (io24_dsp.cpxt_comp, io24_dsp.cpxt_tube,
                   io24_dsp.cpxt_fet)[model]
        blob = builder(index=0, fs=48000.0, **kw)
        _attack, _release, slope, _knee, threshold, makeup = \
            struct.unpack_from("<6f", blob, 0x20)
        ratio = 1.0 / max(1e-6, 1.0 - slope)
        gain_db = 20.0 * math.log10(max(makeup, 1e-12))
        return (max(-60.0, min(0.0, threshold)), max(1.0, ratio),
                gain_db, ("Standard", "Tube", "FET")[model])

    def _adopt_dynamics(self, ch, preset):
        """Populate every Fat Channel widget without emitting a USB write."""
        W = self.w[ch]
        prior = self._adopt_mute
        self._adopt_mute = True
        try:
            gate = preset.get("gate") or {}
            if gate:
                W["gth"].set_value(float(gate.get("threshold", -40.0)))
                W["grange"].set_value(float(gate.get("range", -60.0)))
                W["gatk"].set_value(float(gate.get("attack", 0.005)))
                W["grel"].set_value(float(gate.get("release", 0.3)))
                W["gkey"].set_value(max(40.0, float(gate.get("keyfilter", 0.0))))
                W["gklisten"].set_active(bool(gate.get("keylisten", 0)))
                W["gexp"].set_active(bool(gate.get("expander", 1)))

            comp = preset.get("comp") or {}
            model_name = io24_presets.compressor_model(comp)
            model = {None: 0, "standard": 0, "tube": 1, "fet": 2}[model_name]
            W["model"].set_selected(model)
            W["comp_param_stack"].set_visible_child_name(
                ("standard", "tube", "fet")[model])
            W["ckey"].set_value(max(40.0, float(comp.get("keyfilter", 0.0))))
            W["cklisten"].set_active(bool(comp.get("keylisten", 0)))
            if model == 0:
                W["cth"].set_value(float(comp.get("threshold", 0.0)))
                W["rat"].set_value(float(comp.get("ratio", 2.0)))
                W["catk"].set_value(float(comp.get("attack", 0.02)))
                W["crel"].set_value(float(comp.get("release", 0.15)))
                W["mk"].set_value(float(comp.get("gain", 0.0)))
                W["knee"].set_active(bool(comp.get("softknee", 0)))
                W["auto"].set_active(bool(comp.get("automode", 0)))
            elif model == 1:
                W["tpeak"].set_value(float(comp.get("peak", 0.0)))
                W["tgain"].set_value(float(comp.get("gain", 40.0)))
                W["climit"].set_active(bool(comp.get("mode", 0)))
            else:
                W["finput"].set_value(float(comp.get("input", -43.0)))
                W["foutput"].set_value(float(comp.get("output", 0.0)))
                W["fatk"].set_value(float(comp.get("attack", 0.0001)))
                W["frel"].set_value(float(comp.get("release", 0.25)))
                W["fratio"].set_selected(max(0, min(4, int(comp.get("ratio", 0)))))

            limit = preset.get("limit") or {}
            if limit:
                W["lth"].set_value(float(limit.get("threshold", -28.0)))
        finally:
            self._adopt_mute = prior

    def _adopt_compressor_kwargs(self, ch, call):
        """Adopt one shadowed set_compressor/compressor_off call silently."""
        W = self.w[ch]
        model = max(0, min(2, int(call.get("model", 0))))
        W["model"].set_selected(model)
        W["comp_param_stack"].set_visible_child_name(
            ("standard", "tube", "fet")[model])
        W["ckey"].set_value(max(40.0, float(call.get("keyfilter_hz", 0.0))))
        W["cklisten"].set_active(bool(call.get("keylisten", False)))
        if model == 0:
            W["cth"].set_value(float(call.get("threshold_db", 0.0)))
            W["rat"].set_value(float(call.get("ratio", 2.0)))
            W["catk"].set_value(float(call.get("attack_s", 0.02)))
            W["crel"].set_value(float(call.get("release_s", 0.15)))
            W["mk"].set_value(float(call.get("gain_db", 0.0)))
            W["knee"].set_active(bool(call.get("softknee", False)))
            W["auto"].set_active(bool(call.get("automode", False)))
        elif model == 1:
            W["tpeak"].set_value(float(call.get("peak", 0.0)))
            W["tgain"].set_value(float(call.get("gain", 40.0)))
            W["climit"].set_active(bool(call.get("limit_mode", False)))
        else:
            W["finput"].set_value(float(call.get("input_db", -43.0)))
            W["foutput"].set_value(float(call.get("output_db", 0.0)))
            W["fatk"].set_value(float(call.get("attack_s", 0.0001)))
            W["frel"].set_value(float(call.get("release_s", 0.25)))
            W["fratio"].set_selected(max(
                0, min(4, int(call.get("ratio_index", 0)))))

    def _mirror_dynamics_widgets(self, key, source, target):
        """Keep the hidden linked strip identical without echoing writes."""
        src, dst = self.w[source], self.w[target]
        prior = self._adopt_mute
        self._adopt_mute = True
        try:
            if key == "gate":
                for name in ("gth", "grange", "gatk", "grel", "gkey"):
                    dst[name].set_value(src[name].get_value())
                for name in ("gklisten", "gexp"):
                    dst[name].set_active(src[name].get_active())
            elif key == "comp":
                model = src["model"].get_selected()
                dst["model"].set_selected(model)
                dst["comp_param_stack"].set_visible_child_name(
                    COMP_MODELS[model])
                slider_names = {
                    0: ("cth", "rat", "catk", "crel", "mk"),
                    1: ("tpeak", "tgain"),
                    2: ("finput", "foutput", "fatk", "frel"),
                    MULTIBAND_MODEL: (),
                }[model]
                for name in slider_names + ("ckey",):
                    dst[name].set_value(src[name].get_value())
                dst["cklisten"].set_active(src["cklisten"].get_active())
                if model == MULTIBAND_MODEL:
                    self._mbc_copy(source, target)
                if model == 0:
                    for name in ("knee", "auto"):
                        dst[name].set_active(src[name].get_active())
                elif model == 1:
                    dst["climit"].set_active(src["climit"].get_active())
                else:
                    dst["fratio"].set_selected(src["fratio"].get_selected())
            else:
                dst["lth"].set_value(src["lth"].get_value())
        finally:
            self._adopt_mute = prior

    def _dyn_toggle(self, row, _p, key, ch):
        self.dyn_by_ch[ch][key] = row.get_active()
        if self._adopt_mute:
            return
        self._dyn_push(key, ch)

    def _dyn_push(self, key, ch):
        if self._adopt_mute:
            return
        on = self.dyn_by_ch[ch][key]
        W = self.w[ch]
        for c in ((1, 2) if self.link_both else (ch,)):
            if c != ch:
                self.dyn_by_ch[c][key] = on
                prior = self._adopt_mute
                self._adopt_mute = True
                try:
                    self.w[c][key + "_on"].set_active(on)
                finally:
                    self._adopt_mute = prior
                self._mirror_dynamics_widgets(key, ch, c)
            if key == "gate":
                kw = dict(threshold_db=W["gth"].get_value(),
                          range_db=W["grange"].get_value(),
                          attack_s=W["gatk"].get_value(),
                          release_s=W["grel"].get_value(),
                          keyfilter_hz=(0.0 if W["gkey"].get_value() <= 41
                                        else W["gkey"].get_value()),
                          keylisten=W["gklisten"].get_active(),
                          expander=W["gexp"].get_active())
                self.ctl.submit(
                    lambda dev, x=c, k=kw, fs=self._fs:
                    dev.set_gate(x, on=True, fs=fs, **k)
                    if on else dev.gate_off(x))
            elif key == "comp":
                model, kw = self._compressor_kwargs(ch)
                # Multiband replaces the unit's compressor, which goes off
                device_on = on and not self._multiband_selected(c)
                self.ctl.submit(
                    lambda dev, x=c, m=model, k=kw, fs=self._fs, o=device_on:
                    dev.set_compressor(x, model=m, fs=fs, **k)
                    if o else dev.compressor_off(x))
            else:
                th = W["lth"].get_value()
                rel = W["lrel"].get_value()
                self.ctl.submit(lambda dev, x=c, fs=self._fs:
                                dev.set_limiter(x, on, th, release_s=rel, fs=fs))
        W["comp_curve"].queue_draw()
        for r in self.racks.values():
            r.queue_draw()
        if key == "comp":
            self._insert_reconcile()

    def invalidate_curve(self):
        self._curves = {}

    def curve_points(self, w, h, ch=1):
        """The response polyline, cached.

        This used to be recomputed per pixel per frame — about 800 evaluations,
        each looping four biquads with trig, 30 times a second. It only changes
        when a band changes, so cache it against the band state and the width.
        """
        alternate = self._alt_eq(ch)
        if alternate is None:
            semantic = tuple(
                (b["shape"], round(b["freq"], 2), round(b["gain"], 3),
                 round(b["q"], 3)) for b in self.bands_by_ch[ch])
        else:
            semantic = tuple(sorted(alternate["eq"].items()))
        key = (int(w), int(h), ch, self.eq_enabled(ch), semantic,
               round(float(getattr(self, "_fs", 48000.0)), 3))
        cache = getattr(self, "_curves", None)
        if cache is None:
            cache = self._curves = {}
        if cache.get(ch, (None, None))[0] == key:
            return cache[ch][1]
        step = max(1, int(w // 320))
        pts = []
        for px in range(0, int(w) + 1, step):
            f = 20.0 * (24000 / 20.0) ** (px / max(w, 1))
            y = h / 2 - self.response_for(
                ch, f, fs=getattr(self, "_fs", 48000.0)) / 18.0 * (h / 2)
            pts.append((px, max(1, min(h - 1, y))))
        cache[ch] = (key, pts)
        return pts

    def _biquad(self, b, fs=48000.0):
        if b["shape"] == "off":
            return None
        A = 10.0 ** (b["gain"] / 40.0)
        w = 2 * math.pi * b["freq"] / fs
        cw, sw = math.cos(w), math.sin(w)
        al = sw / (2 * b["q"])
        s = b["shape"]
        if s == "peaking":
            wire = io24.biquad_peaking(
                b["freq"], b["gain"], fs, b["q"])
            return (wire[0], wire[2], wire[4], -wire[1], -wire[3])
        if s == "highshelf":
            # Reorder the exact wire fields [b0,-a1,b1,-a2,b2] into the
            # conventional transfer-function tuple used by this graph.
            wire = io24.biquad_highshelf(
                b["freq"], b["gain"], fs, b["q"])
            return (wire[0], wire[2], wire[4], -wire[1], -wire[3])
        if s == "lowshelf":
            wire = io24.biquad_lowshelf(
                b["freq"], b["gain"], fs, b["q"])
            return (wire[0], wire[2], wire[4], -wire[1], -wire[3])
        a0 = 1 + al
        if s == "hp":
            return ((1 + cw) / 2 / a0, -(1 + cw) / a0, (1 + cw) / 2 / a0,
                    -2 * cw / a0, (1 - al) / a0)
        return ((1 - cw) / 2 / a0, (1 - cw) / a0, (1 - cw) / 2 / a0,
                -2 * cw / a0, (1 - al) / a0)

    def _host_features_state(self):
        """The Host-only half of a snapshot.

        The Standard EQ entry preserves the controls hidden behind its global
        and per-band bypass switches.  The driver's shadow contains only the
        effective coefficients and therefore cannot reconstruct those values
        after four identity filters were sent.
        """
        host_features = {}
        standard_eq_state = getattr(self, "_standard_eq_state", None)
        if callable(standard_eq_state):
            standard_eq = standard_eq_state()
            if standard_eq is not None:
                host_features["standard_eq"] = standard_eq
        alternate_eq_state = getattr(self, "_alternate_eq_state", None)
        alternate_eq = alternate_eq_state() \
            if callable(alternate_eq_state) else None
        if alternate_eq is not None:
            host_features["alternate_eq"] = alternate_eq
        host_delay_state = getattr(self, "_host_delay_feature_state", None)
        host_delay = host_delay_state() \
            if callable(host_delay_state) else None
        if host_delay is not None:
            host_features["voicefx_delay"] = host_delay
        insert = self._insert_state()
        if insert is not None:
            host_features["multiband_insert"] = insert
        reverb_character = self._reverb_character_state()
        if reverb_character is not None:
            host_features["reverb_character"] = reverb_character
        spring_state = getattr(self, "_spring_state", None)
        spring = spring_state() if callable(spring_state) else None
        if spring is not None:
            host_features["spring_reverb"] = spring
        autogain = sorted(c for c, on in
                          getattr(self, "_autogain_on", {}).items() if on)
        if autogain:
            host_features["autogain"] = {"version": 1, "on": autogain}
        return host_features

    def _host_delay_feature_state(self):
        """Return the semantic Delay that replaces unsafe model 5 at 96 kHz.

        The device shadow cannot carry edits made while Delay is hosted in
        PipeWire: writing those edits to block 201 would be the reset hazard
        this path exists to avoid.  Keep the selected input and all four exact
        UC controls beside the other Host-only session state instead.
        """
        model_row = getattr(self, "fx_model", None)
        target_row = getattr(self, "fx_target", None)
        if model_row is None or target_row is None or not \
                io24_fx.delay_needs_host_fallback(
                    Win._voicefx_effective_rate(self)):
            return None
        index = max(0, min(len(self.FX_ORDER) - 1,
                           model_row.get_selected()))
        if self.FX_ORDER[index] != "delay":
            return None
        target = 2 if target_row.get_selected() == 1 else 1
        return io24_voicefx_delay.validate_host_feature({
            "version": io24_voicefx_delay.HOST_FEATURE_VERSION,
            "target": target,
            "state": self._fx_live_params(),
        })

    def _adopt_host_delay_feature(self, feature):
        """Restore a Host Delay without treating its owner as device-read."""
        if feature is None:
            return None
        try:
            feature = io24_voicefx_delay.validate_host_feature(feature)
        except (TypeError, ValueError) as error:
            return "Host Delay was not restored: %s" % error
        required = (getattr(self, "fx_target", None),
                    getattr(self, "fx_model", None),
                    getattr(self, "fx_arm", None))
        if any(control is None for control in required) or \
                "delay" not in getattr(self, "fx_params", {}):
            return "Host Delay was not restored: controls unavailable"
        prior = getattr(self, "_fx_mute", False)
        self._fx_mute = True
        try:
            self.fx_target.set_selected(feature["target"] - 1)
            self.fx_model.set_selected(self.FX_ORDER.index("delay"))
            stack = getattr(self, "fx_param_stack", None)
            if stack is not None:
                stack.set_visible_child_name("delay")
            for name, control in self.fx_params["delay"].items():
                control.set_value(feature["state"][name])
            self.fx_arm.set_active(feature["state"]["on"])
            visual = getattr(self, "fx_visual", None)
            if visual is not None:
                visual.set_model("delay")
        finally:
            self._fx_mute = prior
        return None

    def _standard_eq_state(self):
        """Return the semantic Standard-EQ state that coefficients cannot hold."""
        channels = {}
        bands_by_ch = getattr(self, "bands_by_ch", {})
        for ch in (1, 2):
            bands = bands_by_ch.get(ch)
            if not isinstance(bands, (list, tuple)) or len(bands) != 4:
                continue
            if self._alt_eq(ch) is not None:
                # A recalled Passive/Vintage section remains in its slot body;
                # it must never be mislabeled as four Standard bands.
                continue
            channels[str(ch)] = {
                "on": self.eq_enabled(ch),
                "bands": [dict(band) for band in bands],
            }
        if not channels:
            return None
        return io24_presets.validate_standard_eq_host_state({
            "version": 1,
            "channels": channels,
        })

    def _alternate_eq_state(self):
        """Return complete semantic Passive/Vintage state for both channels."""
        channels = {}
        for ch in (1, 2):
            view = self._alt_eq(ch)
            if view is not None:
                channels[str(ch)] = dict(view["eq"])
        if not channels:
            return None
        return io24_presets.validate_alternate_eq_host_state({
            "version": 1,
            "channels": channels,
        })

    def _adopt_alternate_eq_state(self, state):
        """Restore selected alternate models without echoing a device write."""
        if state is None:
            return None
        try:
            state = io24_presets.validate_alternate_eq_host_state(state)
        except (TypeError, ValueError) as error:
            return "Alternate EQ was not restored: %s" % error
        restored = []
        for channel, eq in state["channels"].items():
            ch = int(channel)
            self._show_alternate_eq(
                ch, io24_presets.alternate_eq_view({"eq": eq}))
            restored.append(ch)
        if restored:
            return "Alternate EQ controls restored on Input %s" % \
                " and ".join(str(ch) for ch in restored)
        return None

    def _adopt_standard_eq_state(self, state):
        """Restore Standard controls without replaying a second device write."""
        if state is None:
            return None
        try:
            state = io24_presets.validate_standard_eq_host_state(state)
        except (TypeError, ValueError) as error:
            return "Standard EQ was not restored: %s" % error
        if not hasattr(self, "eq_on_by_ch"):
            self.eq_on_by_ch = {}
        if not hasattr(self, "bands_by_ch"):
            self.bands_by_ch = {}
        prior = getattr(self, "_adopt_mute", False)
        self._adopt_mute = True
        restored = []
        try:
            for channel, body in state["channels"].items():
                ch = int(channel)
                self.eq_on_by_ch[ch] = body["on"]
                self.bands_by_ch[ch] = [dict(band)
                                        for band in body["bands"]]
                show_alternate = getattr(self, "_show_alternate_eq", None)
                if callable(show_alternate):
                    show_alternate(ch, None)
                controls = getattr(self, "w", {}).get(ch, {})
                switch = controls.get("eq_on")
                if switch is not None:
                    switch.set_active(body["on"])
                adopt_band = getattr(self, "_adopt_band", None)
                if callable(adopt_band) and controls:
                    adopt_band(ch)
                curve = controls.get("curve")
                if curve is not None:
                    curve.queue_draw()
                restored.append(ch)
        finally:
            self._adopt_mute = prior
        invalidate = getattr(self, "invalidate_curve", None)
        if callable(invalidate):
            invalidate()
        for rack in getattr(self, "racks", {}).values():
            rack.queue_draw()
        if restored:
            return "Standard EQ controls restored on Input %s" % \
                " and ".join(str(ch) for ch in restored)
        return None

    def _save_last_session(self, path=None):
        """Remember the Host-only state for the next launch.

        The device half is already on disk: the driver keeps its mirror of
        every write in shadow.json. Returns True when the file was written.
        """
        try:
            state = {"version": 1,
                     "host_features": self._host_features_state()}
            clock_state = getattr(self, "_audio_clock_state", None)
            if callable(clock_state):
                state["audio_clock"] = clock_state()
        except Exception:
            return False
        text = json.dumps(state, sort_keys=True, indent=1)
        if text == getattr(self, "_last_session_text", None):
            return False
        target = str(path) if path else LAST_SESSION_PATH
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target)),
                        exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "w") as stream:
                stream.write(text)
            os.replace(tmp, target)
        except OSError:
            return False
        self._last_session_text = text
        return True

    def _autosave_session(self):
        self._save_last_session()
        return True

    def _maybe_resume(self, snap):
        """Resume once per connection; Host-only state only on the first."""
        if not snap.get("alive"):
            return
        if getattr(self, "_audio_clock_restore_inflight", False):
            return
        if getattr(self, "_audio_clock_restore_deferred", False):
            self._restore_audio_clock()
            return
        generation = snap.get("attach_generation")
        previous = getattr(self, "_resumed_generation", None)
        if generation == previous:
            return
        self._resumed_generation = generation
        live_target = voicefx_target_from_processing(
            snap.get("processing_channel"))
        if live_target is not None:
            self._remember_voicefx_target(live_target)
        self._resume_session(first=previous is None)
        if previous is not None:
            # a new connection: the insert's streams went with the old one
            self._insert_reconcile(restart=True)
            spring_reconcile = getattr(self, "_spring_reconcile", None)
            if callable(spring_reconcile):
                spring_reconcile(restart=True)

    def _resume_session(self, first=True, path=None):
        """Pick up where the last session left off.

        The unit keeps its gains, selected block, enable and stereo link
        through a power cycle and loses every write-only DSP setting. So on
        each connection the Host re-sends what it last sent, except what the
        unit reports itself, and shows it on the controls. Its own Host-only
        features come back once per launch. FX model state is replayed, while
        the live processingChannel assignment is read from the unit rather
        than replaced by an old cached route.
        """
        session = load_last_session(path) if first else None
        if first:
            host_features = (session or {}).get("host_features")
        else:
            # Reapply sends the effective EQ shadow.  If a complete EQ was
            # bypassed that shadow is four identity filters, so keep the live
            # semantic controls beside it and re-adopt them after reconnect.
            standard_eq_state = getattr(self, "_standard_eq_state", None)
            standard_eq = standard_eq_state() \
                if callable(standard_eq_state) else None
            alternate_eq_state = getattr(self, "_alternate_eq_state", None)
            alternate_eq = alternate_eq_state() \
                if callable(alternate_eq_state) else None
            host_features = {}
            if standard_eq is not None:
                host_features["standard_eq"] = standard_eq
            if alternate_eq is not None:
                host_features["alternate_eq"] = alternate_eq
            host_delay_state = getattr(self, "_host_delay_feature_state", None)
            host_delay = host_delay_state() \
                if callable(host_delay_state) else None
            if host_delay is not None:
                host_features["voicefx_delay"] = host_delay
            host_features = host_features or None

        host_delay_rate = Win._voicefx_effective_rate(self)

        def work(dev):
            # The worker hands each job the Io24 driver itself
            # (Ctl._loop: fn(self.dev.dev)). Its own `.dev` is the raw USB
            # handle, so it must not be unwrapped again.
            backend = dev
            mirror = json.loads(json.dumps(
                getattr(backend, "_shadow", None) or {}))
            mirror = {
                key: call for key, call in mirror.items()
                if not isinstance(call, dict) or
                call.get("fn") != "set_processing_channel"
            }
            try:
                mirrored_fx = shadow_ui_state(mirror).get("voicefx") or {}
                host_delay = (
                    str(mirrored_fx.get("model", "")).lower() == "delay" and
                    io24_fx.delay_needs_host_fallback(host_delay_rate))
                replay_skip = RESUME_SKIP
                if host_delay:
                    backend.quiesce_voicefx_for_host_delay(
                        host_delay_rate,
                        quantum=getattr(
                            self, "_selected_quantum", DEFAULT_QUANTUM))
                    self._host_delay_quiesced_device = backend
                    replay_skip += ("set_fx",)
                report = backend.reapply_shadow(
                    skip=replay_skip,
                    sample_rate_hz=getattr(
                        self, "_fs", DEFAULT_SAMPLE_RATE))
            except Exception as error:
                GLib.idle_add(self.say,
                              "Could not restore the last session: %s" % error)
                return
            message = ("Picked up where you left off"
                       if report.get("applied") or host_features else None)
            GLib.idle_add(self._after_load, mirror, host_features, [], message)

        self.ctl.submit(work)

    def on_save(self, *_a):
        self._pick(True)

    def on_load(self, *_a):
        self._pick(False)

    def _pick(self, saving):
        dlg = Gtk.FileDialog(
            title="Save snapshot" if saving else "Load snapshot",
            initial_name="io24-host-snapshot.json")

        def done(d, res):
            try:
                f = d.save_finish(res) if saving else d.open_finish(res)
            except GLib.Error:
                return
            p = f.get_path()
            host_features = None
            if saving:
                try:
                    host_features = snapshot_host_features(
                        self._host_features_state())
                except Exception as error:
                    self.say("Save failed before writing: %s" % error)
                    return

            # The work happens on the USB thread, so the result is not known
            # here. Announcing "Saved" at this point was a lie the user could
            # not detect: a preset that failed to write still said it worked.
            # Report from the worker, once there is something to report.
            def work(dev, path=p, sv=saving, features=host_features):
                try:
                    if sv:
                        snap = dev.save_preset(
                            path, host_features=features)
                        msg = "Saved %s" % os.path.basename(path)
                        quarantined = snap.get(
                            "quarantined_device_preset_calls", 0)
                    else:
                        n_live, n_calls = dev.load_preset(
                            path, sample_rate_hz=getattr(
                                self, "_fs", DEFAULT_SAMPLE_RATE))
                        msg = "Loaded %s" % os.path.basename(path)
                        load_report = getattr(
                            dev, "_last_preset_load_report", {})
                        quarantined = load_report.get(
                            "quarantined_device_preset_calls", 0)
                        # Capture the exact post-load mirror while still on the
                        # serialized worker.  The GTK thread then adopts it as
                        # display state without touching USB.
                        mirror = json.loads(json.dumps(dev._shadow))
                        host_state = load_report.get("host_features", {})
                        host_migrations = load_report.get(
                            "host_feature_migrations", [])
                    if quarantined:
                        msg += (" · %d device setting%s skipped" %
                                (quarantined,
                                 "" if quarantined == 1 else "s"))
                except Exception as e:
                    msg = "%s failed: %s" % ("Save" if sv else "Load", e)
                if not sv and "mirror" in locals():
                    GLib.idle_add(
                        self._after_load, mirror, host_state,
                        host_migrations, msg)
                else:
                    GLib.idle_add(self.say, msg)
            self.ctl.submit(work)
        (dlg.save if saving else dlg.open)(self, None, done)

    def _after_load(self, mirror=None, host_features=None,
                    host_feature_migrations=None, completion_message=None):
        """Pull the widgets back into line with what the preset just applied.

        Loading replays a pile of setters straight into the driver, so every
        control that mirrors driver state -- the mixer sliders, the bus masters
        -- is now stale. Nothing reads back from the device here; this is the
        shadow, which is all a write-only DSP can offer.
        """
        try:
            self._sync_mix_widgets()
        except Exception:
            pass
        if mirror is None:
            try:
                mirror = json.loads(json.dumps(self.ctl.dev.dev._shadow))
            except Exception:
                mirror = {}
        state = shadow_ui_state(mirror)

        prior_adopt = self._adopt_mute
        prior_fx = self._fx_mute
        prior_delay = getattr(self, "_delay_mute", False)
        prior_preset = getattr(self, "_preset_sync", False)
        prior_phones = getattr(self, "_phones_source_mute", False)
        self._adopt_mute = True
        self._fx_mute = True
        self._delay_mute = True
        self._preset_sync = True
        self._phones_source_mute = True
        try:
            # The input-strip high-pass toggle is an Appl control, distinct
            # from the Fat Channel's continuously variable low cut.
            for ch, on in state["hpf_toggle"].items():
                live = getattr(self, "hpf_live", {}).get(ch)
                if live is not None:
                    live.mute = True
                    try:
                        live.w.set_active(on)
                        live.touched = 0.0
                    finally:
                        live.mute = False

            for ch in (1, 2):
                if ch in state["hpf_freq"]:
                    hz = state["hpf_freq"][ch]
                    self._adopt_hpf_controls(ch, hz)
                for band, values in state["eq"][ch].items():
                    self.bands_by_ch[ch][band] = dict(values)
                if state["eq"][ch]:
                    # the load has just written Standard bands to this channel
                    if not hasattr(self, "eq_on_by_ch"):
                        self.eq_on_by_ch = {}
                    self.eq_on_by_ch[ch] = any(
                        band.get("shape") != "off"
                        for band in state["eq"][ch].values())
                    self._show_alternate_eq(ch, None)
                    self._adopt_band(ch)

                gate = state["gate"].get(ch)
                if gate is not None:
                    on = bool(gate.get("on"))
                    self.dyn_by_ch[ch]["gate"] = on
                    self.w[ch]["gate_on"].set_active(on)
                    if on:
                        self.w[ch]["gth"].set_value(float(
                            gate.get("threshold_db", -40.0)))
                        self.w[ch]["grange"].set_value(float(
                            gate.get("range_db", -60.0)))
                        self.w[ch]["gatk"].set_value(float(
                            gate.get("attack_s", 0.005)))
                        self.w[ch]["grel"].set_value(float(
                            gate.get("release_s", 0.3)))
                        self.w[ch]["gkey"].set_value(max(
                            40.0, float(gate.get("keyfilter_hz", 0.0))))
                        self.w[ch]["gklisten"].set_active(bool(
                            gate.get("keylisten", False)))
                        self.w[ch]["gexp"].set_active(bool(
                            gate.get("expander", True)))

                comp = state["compressor"].get(ch)
                # Under Multiband the unit's compressor is off by design, and
                # that off is not the Compressor switch, which drives Multiband.
                if comp is not None and (comp.get("on") or
                                         not self._multiband_selected(ch)):
                    on = bool(comp.get("on"))
                    self.dyn_by_ch[ch]["comp"] = on
                    self.w[ch]["comp_on"].set_active(on)
                    if on:
                        self._adopt_compressor_kwargs(ch, comp)

                limiter = state["limiter"].get(ch)
                if limiter is not None:
                    on = bool(limiter.get("on"))
                    self.dyn_by_ch[ch]["lim"] = on
                    self.w[ch]["lim_on"].set_active(on)
                    self.w[ch]["lth"].set_value(float(
                        limiter.get("threshold_db", -28.0)))
                    self.w[ch]["lrel"].set_value(float(
                        limiter.get("release_s", 0.4)))

                if ch in state["order"]:
                    self.order_by_ch[ch] = state["order"][ch]

            self._sync_order_row(self._current_channel())

            reverb = state["reverb"]
            if reverb is not None:
                self.rev_type.set_selected(0)
                self.rev_on.set_active(bool(reverb.get("on", True)))
                self.s_rsize.set_value(float(reverb.get("size", 0.5)))
                self.s_rmix.set_value(float(reverb.get("mix", 0.3)))
                self.s_rhp.set_value(float(reverb.get("hp_freq", 200.0)))
                self.s_rpre.set_value(float(reverb.get("predelay", 0.02)))
            for ch, value in state["processing_mix"].items():
                self._adopt_processing_mix(ch, value)
            for bus, value in state["reverb_return"].items():
                control = getattr(self, "rev_return_controls", {}).get(bus)
                if control is not None and value is not None:
                    control.set_value(max(-60.0, min(10.0, float(value))))

            voicefx = state["voicefx"]
            if voicefx is not None:
                model = str(voicefx.get("model", "transformer")).lower()
                if model in self.FX_ORDER:
                    index = self.FX_ORDER.index(model)
                    self.fx_model.set_selected(index)
                    self.fx_param_stack.set_visible_child_name(model)
                    for name, control in self.fx_params[model].items():
                        if name in voicefx:
                            control.set_value(voicefx[name])
                    self.fx_arm.set_active(bool(voicefx.get("on", True)))
                    self.fx_visual.set_model(model)
            target = state["voicefx_target"]
            if target in (1, 2):
                self._remember_voicefx_target(target)
            mode = state["preset_mode"]
            if mode in (0, 1, 2):
                self.preset_mode_row.set_selected(mode)
            if state["mute_mode"] is not None and \
                    getattr(self, "mute_sync_row", None) is not None:
                self.mute_sync_row.set_active(state["mute_mode"])
            for ch, slot in state["preset_slot"].items():
                row = getattr(self, "preset_rows", {}).get(ch)
                if row is not None:
                    local = slot - self.PRESET_BASE[ch]
                    row[1].set_selected(local if 0 <= local <= 1 else -1)

            bus = state["output_delay_bus"]
            if bus is not None:
                names = [name for name, _title in self._delay_buses]
                canonical = io24.Io24.BUS_ALIASES.get(bus.lower(), bus.lower())
                if canonical in names:
                    self.delay_bus_row.set_selected(names.index(canonical))
                    self.delay_scale.set_sensitive(canonical != "off")
            if state["output_delay"] is not None:
                self.delay_scale.set_value(state["output_delay"] * 1000.0)

            if state["phones_source"] in (0, 1, 2):
                self.phones_source_row.set_selected(state["phones_source"])

            if state["link"] is not None and \
                    self.link_row.get_active() != state["link"]:
                prior_link = getattr(self, "_link_mute", False)
                self._link_mute = True
                try:
                    self.link_row.set_active(state["link"])
                finally:
                    self._link_mute = prior_link

            for component, name in state["component_names"].items():
                row = getattr(self, "component_name_rows", {}).get(component)
                if row is not None:
                    row.set_text(name)
        finally:
            self._phones_source_mute = prior_phones
            self._preset_sync = prior_preset
            self._delay_mute = prior_delay
            self._fx_mute = prior_fx
            self._adopt_mute = prior_adopt

        self.invalidate_curve()
        for ch in (1, 2):
            self.w[ch]["curve"].queue_draw()
            self.w[ch]["comp_curve"].queue_draw()
        for rack in self.racks.values():
            rack.queue_draw()
        host_messages = []
        host_delay_message = None
        if host_features:
            standard_eq_message = self._adopt_standard_eq_state(
                host_features.get("standard_eq"))
            alternate_eq_message = self._adopt_alternate_eq_state(
                host_features.get("alternate_eq"))
            multiband_message = self._adopt_legacy_multiband(
                host_features.get("multiband"))
            insert_message = self._adopt_insert_state(
                host_features.get("multiband_insert"))
            reverb_message = self._adopt_reverb_character(
                host_features.get("reverb_character"))
            spring_message = self._adopt_spring_state(
                host_features.get("spring_reverb"))
            autogain_message = self._adopt_autogain(
                host_features.get("autogain"))
            host_delay_message = self._adopt_host_delay_feature(
                host_features.get("voicefx_delay"))
            messages = (standard_eq_message, alternate_eq_message,
                        multiband_message, insert_message, reverb_message,
                        spring_message, autogain_message, host_delay_message)
            host_messages = [message for message in messages
                             if message and any(word in message.casefold()
                                                for word in ("not restored",
                                                             "unavailable",
                                                             "unreadable"))]
        restored_voicefx = state.get("voicefx") or {}
        restored_host_delay = bool(
            host_features and host_features.get("voicefx_delay") is not None
            and not (host_delay_message or "").casefold().startswith(
                "host delay was not restored"))
        if restored_host_delay or (str(
                restored_voicefx.get("model", "")).lower() == "delay" and \
                io24_fx.delay_needs_host_fallback(
                    Win._voicefx_effective_rate(self))):
            # reapply_shadow deliberately skipped/refused model 5. The mirror
            # has now repopulated the exact visible controls, so materialize
            # that intent on the Host path and quiesce hardware block 201.
            self._push_fx()
        else:
            self._insert_reconcile()
        if completion_message is not None:
            completion_message = append_host_migration_notices(
                completion_message, host_feature_migrations)
            if host_messages:
                completion_message += "; " + "; ".join(host_messages)
            self.say(completion_message)
        return False

    # -------------------------------------------------------------- 30 fps
    def _tick(self):
        """30 fps, but only for what is actually on screen.

        Redrawing every meter, the chain and both curves each frame — including
        the page the user cannot see — was what made the display feel sluggish.
        The USB read behind it costs 0.34 ms; the drawing was the expensive part.
        """
        if self.ctl.snap["alive"]:
            self._sync_link_row()
        if PROFILE:
            now = time.monotonic()
            self._frames.append(now)
            if now - self._frames[0] > 3.0:
                n = len(self._frames)
                gaps = [(self._frames[i + 1] - self._frames[i]) * 1000
                        for i in range(n - 1)]
                gaps.sort()
                print("  UI %5.1f fps   gap med %5.1f ms  p95 %5.1f ms  max %6.1f ms"
                      % (n / (now - self._frames[0]), gaps[len(gaps) // 2],
                         gaps[int(len(gaps) * .95)], gaps[-1]), flush=True)
                self._frames = [now]
        s = self.ctl.snap
        page = self.stack.get_visible_child_name() if self.stack else "mix"

        if page == "mix":
            self.meters["in1"].set_db(s["in"][0])
            self.meters["in2"].set_db(s["in"][1])
            for bus in ("main", "mixa", "mixb"):
                for side in (0, 1):
                    self.meters["%s%d" % (bus, side)].set_db(s[bus][side])
            for ch, bars in getattr(self, "gr_bars", {}).items():
                for k, bar in bars.items():
                    bar.set_db(s["gr"].get(ch, {}).get(k, 0.0))
        else:
            self._half = not getattr(self, "_half", False)
            if self._half:                    # 15 fps is plenty for these
                for r in self.racks.values():
                    r.queue_draw()
                for c in self.w:
                    self.w[c]["comp_curve"].queue_draw()
                    self.w[c]["curve"].queue_draw()

        # Following the hardware is cheap and must happen on both pages — but
        # ONLY while there is hardware to follow. Without this gate the app
        # pulled snap DEFAULTS into every widget while offline: blend snapped
        # back to centre after each drag, 48V/Mute unlit themselves (Low cut
        # alone stayed lit, having no read-back), and Stereo link sprang back —
        # three "broken controls" with this single cause.
        if not s["alive"]:
            return True
        self._maybe_resume(s)
        for ch in (1, 2):
            self.gain_faders[ch].pull(s["gain"][ch - 1])
            self.phantom_live[ch].pull(s["phantom"][ch - 1])
        self.mon["mainvol"].pull(s["mainvol"])
        self.mon["hp"].pull(s["hp"])
        if getattr(self, "blend_knob", None) is not None:
            # device reports -1..+1; the knob is 0..1
            self.blend_knob.adopt((s["blend"] + 1.0) / 2.0)
        # hardware buttons the device does report back
        for ch in (1, 2):
            self.mute_live[ch].pull(s["mute"][ch - 1])
        self.mon["hpmute"].pull(s["hpmute"])
        self.mon["link"].pull(s["link"])
        poff = s.get("preset_off", [False, False])
        self._follow_device_bypass(poff)
        pslot = s.get("preset_slot", [0, 0])
        self._observe_device_slot_recalls(pslot)
        self._mark_playing_unit_blocks(pslot)
        rows = getattr(self, "preset_rows", {})
        if rows:
            self._preset_sync = True          # suppress the write-back handlers
            try:
                for ch, (status_row, sl) in rows.items():
                    names = getattr(self, "_slot_names_by_channel", {}).get(ch, [])
                    prefix = " · ".join(names)
                    if poff[ch - 1]:
                        status_row.set_subtitle(
                            (prefix + " · " if prefix else "") + "Bypassed")
                    else:
                        status_row.set_subtitle(
                            (prefix + " · " if prefix else "") + "Active")
                    local = pslot[ch - 1] - self.PRESET_BASE[ch]
                    if 0 <= local <= 1:
                        if sl.get_selected() != local:
                            sl.set_selected(local)
                    else:
                        sl.set_selected(-1)   # out of this channel's pair
            finally:
                self._preset_sync = False
        for ch, indicator in getattr(self, "preset_indicators", {}).items():
            local = pslot[ch - 1] - self.PRESET_BASE[ch]
            indicator.set_state(
                local if 0 <= local <= 1 else None,
                not poff[ch - 1])
        for r in self.racks.values():
            if r.get_mapped():
                r.queue_draw()
        muted = s.get("mainmute")
        self.mainmute_row.set_text(
            "Main output: Muted" if muted else "Main output: On")
        if muted:
            self.mainmute_row.add_css_class("error")
        else:
            self.mainmute_row.remove_css_class("error")
        self.status.set_text("" if s["alive"] else (s["error"] or "no device"))
        return True


# Styling for the chrome around the canvases. libadwaita gives a good neutral
# desktop app; this makes it read as a piece of audio equipment instead —
# darker ground, a cyan accent that matches the traces, and tabular figures for
# every number so readouts stop jittering as digits change width.
#
# Deliberately restrained: it recolours and tightens what libadwaita already
# lays out rather than re-skinning widgets wholesale, because a hand-rolled
# theme breaks on the next Adwaita release and this has to keep working.
CSS = """
:root, window.background {
  --io24-accent: #3dd6de;
  --io24-ground: #080b12;
}
window.background { background-color: #080b12; }

headerbar {
  background: linear-gradient(180deg, #131a26 0%, #0c111a 100%);
  box-shadow: inset 0 -1px 0 alpha(#3dd6de, 0.22);
}
headerbar windowtitle { letter-spacing: 0.4px; }

/* readouts: tabular figures so numbers stop dancing as digits change width */
.numeric, .io24-readout {
  font-family: "JetBrains Mono", "Cascadia Mono", "DejaVu Sans Mono", monospace;
  font-feature-settings: "tnum" 1;
  letter-spacing: 0.2px;
}
.io24-readout { color: #3dd6de; }

/* panels: a faint lit edge rather than a hard border */
.card, preferencesgroup > box > box {
  background-color: alpha(#16202e, 0.55);
  border: 1px solid alpha(#3dd6de, 0.10);
  border-radius: 10px;
}

/* Group headers. They were plain, tall, and the descriptions under them ran to
   several lines of body text that pushed the actual controls off screen. Now:
   a compact uppercase title with a lit rule beside it, and descriptions
   demoted to small dim text with the leading pulled in. Same information,
   roughly half the vertical space. */
preferencesgroup > box > label:first-child {
  color: #EDE8DC;
  font-weight: 600;
  font-size: 0.98em;
  letter-spacing: 0.4px;
  margin-top: 12px;
  margin-bottom: 5px;
  padding-bottom: 4px;
  border-bottom: 1px solid alpha(#59D6C9, 0.35);
}
preferencesgroup > box > label:nth-child(2) {
  font-size: 0.78em;
  opacity: 0.5;
  margin-top: 0;
  margin-bottom: 3px;
}
preferencesgroup { margin-top: 1px; margin-bottom: 1px; }
preferencesgroup > box { margin-top: 0; }
preferencespage > scrolledwindow > viewport > clamp > box { margin-top: 4px; }

/* Rows were 38px of mostly padding. The controls are what people came for, so
   the chrome around them gets pulled in hard: shorter rows, tighter headers,
   and subtitles small enough to be reference rather than prose. */
row { min-height: 30px; }
row > box.header { margin-top: 0; margin-bottom: 0; }
row > box.header > label.title { font-size: 0.90em; }
row > box.header > label.subtitle {
  font-size: 0.76em; opacity: 0.48; margin-top: -1px;
}
row.property > box.header > label.title { font-size: 0.90em; }
switchrow, actionrow, comborow { padding-top: 1px; padding-bottom: 1px; }

switch:checked { background-color: alpha(#3dd6de, 0.85); }

/* Toggles are instrument switches, not dialog buttons: a dark chip at rest,
   lit phosphor when engaged. The stock pill was brighter than anything else
   on the page and read as a leftover from the light theme. */
button.toggle {
  background-image: none;
  background-color: #141b26;
  border: 1px solid alpha(#3dd6de, 0.16);
  color: #c8d4e0;
}
button.toggle:hover { border-color: alpha(#3dd6de, 0.45); }
button.toggle:checked {
  background-color: alpha(#3dd6de, 0.18);
  border-color: alpha(#3dd6de, 0.75);
  color: #eafcff;
  box-shadow: 0 0 10px alpha(#3dd6de, 0.35);
}

/* Faders as console hardware: a narrow recessed track, a lit travelled
   section, and a rectangular cap with a centre line — the shape your eye
   uses to read position on a real desk. Round handles read as scrollbars. */
scale trough {
  min-height: 5px; min-width: 5px;
  background-color: #05070c;
  border: 1px solid alpha(#3dd6de, 0.13);
  border-radius: 3px;
}
scale highlight {
  background-image: linear-gradient(90deg, alpha(#3dd6de,0.55), #3dd6de);
  box-shadow: 0 0 7px alpha(#3dd6de, 0.55);
  border-radius: 3px;
}
scale.vertical highlight {
  background-image: linear-gradient(0deg, alpha(#3dd6de,0.55), #3dd6de);
}
scale slider {
  min-width: 15px; min-height: 26px;
  border-radius: 3px;
  background-image: linear-gradient(180deg, #e8eff7 0%, #aebccd 48%, #7d8b9c 52%, #cfd9e6 100%);
  border: 1px solid alpha(#0a0f16, 0.9);
  box-shadow: 0 1px 3px alpha(#000,0.6), 0 0 6px alpha(#3dd6de,0.30);
}
scale.vertical slider { min-width: 26px; min-height: 15px; }
scale slider:hover  { box-shadow: 0 1px 3px alpha(#000,0.6), 0 0 10px alpha(#3dd6de,0.6); }
scale:disabled trough, scale:disabled highlight { opacity: 0.35; }

/* Routing matrix: each bus keeps its own colour, so a row of three faders
   reads as three destinations rather than one control repeated. */
scale.bus-mixa highlight {
  background-image: linear-gradient(90deg, alpha(#a78bfa,0.55), #a78bfa);
  box-shadow: 0 0 7px alpha(#a78bfa, 0.5);
}
scale.bus-mixb highlight {
  background-image: linear-gradient(90deg, alpha(#2ad99e,0.55), #2ad99e);
  box-shadow: 0 0 7px alpha(#2ad99e, 0.5);
}
button.suggested-action { background-color: #2aa9b4; color: #04252a; }
.numeric { font-feature-settings: "tnum"; letter-spacing: 0.02em; }
.io24-danger { color: #fa4a5c; }
.io24-slot { font-weight: 700; font-size: 0.95em; }
.io24-slot:checked {
  background-image: linear-gradient(180deg, alpha(#3dd6de,0.30), alpha(#3dd6de,0.16));
  box-shadow: inset 0 0 0 1px alpha(#3dd6de,0.75), 0 0 8px alpha(#3dd6de,0.45);
  color: #eafcff;
}
.io24-slot-hw { border-bottom: 2px solid alpha(#3dd6de, 0.45); }
stackswitcher button:checked { box-shadow: inset 0 -2px 0 #3dd6de; }

/* The page tabs inherit the chip look only when active: six bordered boxes in
   the header was noise, but the current page deserves to be lit. */
viewswitcher button.toggle {
  background-color: transparent;
  border-color: transparent;
  box-shadow: none;
}
viewswitcher button.toggle:hover { border-color: alpha(#3dd6de, 0.30); }
viewswitcher button.toggle:checked {
  background-color: alpha(#3dd6de, 0.12);
  border-color: alpha(#3dd6de, 0.55);
  box-shadow: 0 0 10px alpha(#3dd6de, 0.25);
}
"""


def install_style():
    """Force dark and load the sheet. Safe to call more than once."""
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    prov = Gtk.CssProvider()
    try:
        prov.load_from_data(CSS, -1)
    except TypeError:                     # older introspection wants bytes
        prov.load_from_data(CSS.encode())
    disp = Gdk.Display.get_default()
    if disp is not None:
        Gtk.StyleContext.add_provider_for_display(
            disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def release_host_solo(ctl):
    """Release every mixer solo before the Host goes away.

    Solo is Host monitoring state that no snapshot keeps. Leaving a bus
    silenced on the device after the Host has closed would be a mute nobody
    can see or undo, so it is released on the way out. Best effort: a device
    that is already gone has nothing to release.
    """
    dev = getattr(ctl, "dev", None)
    if dev is None:
        return 0
    try:
        with dev.lock:
            return dev.dev.clear_solo()
    except Exception:
        return 0


def release_insert_routing(ctl, routing):
    """Give every input its own mixer feed back before the Host goes away.

    Returns what could not be undone (None when everything was); the session
    keeps it, so the next launch finishes the job.
    """
    if not routing:
        return None
    dev = getattr(ctl, "dev", None)
    if dev is None:
        return routing
    try:
        with dev.lock:
            for key in sorted(routing.get("moved") or {}):
                routing = io24_mbc.unroute_insert(dev.dev, int(key), routing)
    except Exception:
        return routing
    return routing if routing["moved"] or routing["return_prior"] else None


def release_spring_routing(ctl, routing):
    """Restore any dedicated Spring playback lane before Host exit."""
    if not routing:
        return None
    dev = getattr(ctl, "dev", None)
    if dev is None:
        return routing
    try:
        with dev.lock:
            return io24_spring.restore_routes(dev.dev, routing)
    except Exception:
        return routing


def snapshot_host_features(features):
    """Host features as a snapshot file keeps them. What the Host changed in
    the unit's mixer and in PipeWire belongs to this session, not the file."""
    features = dict(features or {})
    insert = features.get("multiband_insert")
    if insert is not None:
        features["multiband_insert"] = dict(
            insert, routing=None, quantum_before=None)
    spring = features.get("spring_reverb")
    if spring is not None:
        features["spring_reverb"] = dict(spring, routing=None)
    return features


class App(Adw.Application):
    def __init__(self, ctl):
        super().__init__(application_id="org.io24.Mixer")
        self.ctl = ctl
        self.win = None

    def do_shutdown(self):
        w = self.win
        # Multiband: the unit's own feeds back, PipeWire's buffer and default
        # input back, and no orphan process left behind
        if w is not None and getattr(w, "insert", None) is not None:
            w._insert_shutdown()
        if w is not None and getattr(w, "spring", None) is not None:
            w._spring_shutdown()
        if w is not None and getattr(w, "bus_sources", None) is not None:
            w.bus_sources.stop()
        # Remember this session for the next launch; a window that never
        # finished building has nothing to remember.
        save = getattr(w, "_save_last_session", None)
        if callable(save):
            save()
        release_host_solo(getattr(self, "ctl", None))
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        if not self.win:
            install_style()               # needs a display, so not at import time
            self.win = Win(self, self.ctl)
        self.win.present()


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    try:
        dev = wait_for_device(0.5, timeout=0.0)
    except SystemExit:
        # No device is no longer a refusal: the window opens offline (host-side
        # features work without the interface) and attaches when it appears.
        # --wait is accepted for compatibility but means nothing anymore.
        dev = None
        print("io24 not found — opening offline; it will attach when plugged in")
    ctl = Ctl(dev)
    app = App(ctl)
    try:
        return app.run([])
    finally:
        ctl.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
