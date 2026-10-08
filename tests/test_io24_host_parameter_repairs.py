"""Hardware-free contracts for the P1 Host parameter repairs."""

import copy
import gc
import hashlib
import inspect
import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
import warnings

import io24
import io24_alt_eq
import io24_mbc
import io24_presets
import io24_scene
import io24d
import io24gtk
import ucnet_shim


class _Control:
    """A stand-in for one GTK row or scale."""

    def __init__(self):
        self.value, self.active, self.selected = 0.0, False, 0
        self.sensitive = True
        self.visible = True
        self.visible_child_name = None
        self.draws = 0

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active

    def get_selected(self):
        return self.selected

    def set_selected(self, selected):
        self.selected = selected

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive

    def set_visible_child_name(self, name):
        self.visible_child_name = name

    def set_visible(self, visible):
        self.visible = visible

    def queue_draw(self):
        self.draws += 1


def _multiband_controls():
    """Every control one channel's Multiband page holds, by its key."""
    controls = {"x%d" % index: _Control() for index in range(3)}
    for index in range(len(io24_mbc.BANDS)):
        for key in (
                "type", "stack",
                "s_thr", "s_ratio", "s_attack", "s_release", "s_gain",
                "s_knee", "s_auto", "t_peak", "t_gain", "t_limit",
                "f_input", "f_output", "f_attack", "f_release", "f_ratio",
                "keyf", "keyon", "listen"):
            controls[(index, key)] = _Control()
    return controls


class ProtocolDevice(io24.Io24):
    def __init__(self):
        self.payloads = []
        self._shadow = {}
        self._shadow_dirty = False
        self._shadow_flushed = 0.0
        self._shadow_persist = False

    def _exec(self, payload, wait=1.5):
        self.payloads.append(bytes(payload))
        return b""


class CallDevice:
    # The effects-return transaction lives in the driver; borrowing the real
    # one keeps these assertions on the exact writes it makes.
    BUS_ALIASES = io24.Io24.BUS_ALIASES
    MIXER_BUSES = io24.Io24.MIXER_BUSES
    FX_RETURN_SOURCE = io24.Io24.FX_RETURN_SOURCE
    _bus = io24.Io24._bus
    establish_effects_return = io24.Io24.establish_effects_return

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("set_") or name.endswith("_off"):
            def call(*args, **kwargs):
                self.calls.append((name, args, kwargs))
            return call
        raise AttributeError(name)


class ReverbAudiblePathTests(unittest.TestCase):
    class Value:
        def __init__(self, value):
            self.value = value

        def get_value(self):
            return self.value

        def get_active(self):
            return bool(self.value)

    def test_enabling_reverb_establishes_visible_channel1_and_main_path(self):
        device = CallDevice()
        host = SimpleNamespace(
            _adopt_mute=False,
            _fs=48000.0,
            rev_on=self.Value(True),
            s_rsize=self.Value(0.72), s_rmix=self.Value(0.38),
            s_rhp=self.Value(180.0), s_rpre=self.Value(0.027),
            processing_mix_controls={1: self.Value(0.65)},
            rev_return_controls={"main": self.Value(-3.0)},
            ctl=SimpleNamespace(submit=lambda work: work(device)),
        )

        io24gtk.Win._push_reverb(host, establish_path=True)

        self.assertEqual(device.calls, [
            ("set_fx_mix", (1, 0.65), {}),
            ("set_send_db", ("fxreturn/ch1", "main", -3.0), {}),
            ("set_send_assigned", ("fxreturn/ch1", "main", True), {}),
            ("set_reverb", (), {
                "on": True, "size": 0.72, "mix": 0.38,
                "hp_freq": 180.0, "predelay": 0.027, "fs": 48000.0,
            }),
        ])

    def test_disabling_reverb_does_not_rewrite_send_or_return_path(self):
        device = CallDevice()
        host = SimpleNamespace(
            _adopt_mute=False,
            _fs=48000.0,
            rev_on=self.Value(False),
            s_rsize=self.Value(0.5), s_rmix=self.Value(0.35),
            s_rhp=self.Value(200.0), s_rpre=self.Value(0.02),
            processing_mix_controls={1: self.Value(1.0)},
            rev_return_controls={"main": self.Value(0.0)},
            ctl=SimpleNamespace(submit=lambda work: work(device)),
        )

        io24gtk.Win._push_reverb(host)

        self.assertEqual(device.calls, [("set_reverb", (), {
            "on": False, "size": 0.5, "mix": 0.35,
            "hp_freq": 200.0, "predelay": 0.02, "fs": 48000.0,
        })])


class PhonesSourceTests(unittest.TestCase):
    def test_public_setter_emits_exact_pari_11_and_is_shadowed(self):
        dev = ProtocolDevice()

        dev.set_phones_source("Mix B")

        self.assertEqual(len(dev.payloads), 1)
        payload = dev.payloads[0]
        self.assertEqual(len(payload), 32)
        self.assertEqual(struct.unpack_from("<III", payload),
                         (io24.SETP, io24.APPL, 0))
        self.assertEqual(struct.unpack_from("<IIIII", payload, 12),
                         (io24.PARI, 0x14, 0, 11, 2))
        self.assertEqual(io24.Io24.KNOWN_PARI[11], "phonesSource")
        self.assertEqual(dev._shadow, {
            "set_phones_source": {
                "fn": "set_phones_source", "kwargs": {"source": "Mix B"},
            },
        })

    def test_all_aliases_map_and_invalid_or_ambiguous_values_emit_nothing(self):
        for source, expected in (("main", 0), ("aux1", 1), ("stream mix b", 2),
                                 (0, 0), (1.0, 1), ("2", 2)):
            with self.subTest(source=source):
                dev = ProtocolDevice()
                dev.set_phones_source(source)
                self.assertEqual(struct.unpack_from("<i", dev.payloads[0], 28)[0],
                                 expected)
        for source in (True, -1, 3, 0.5, float("nan"), "unknown"):
            with self.subTest(source=source):
                dev = ProtocolDevice()
                with self.assertRaises(ValueError):
                    dev.set_phones_source(source)
                self.assertEqual(dev.payloads, [])
                self.assertEqual(dev._shadow, {})

    def test_daemon_scene_shadow_and_ucnet_paths_reach_the_public_setter(self):
        fake = CallDevice()
        daemon = object.__new__(io24d.Device)
        daemon.lock = threading.RLock()
        daemon.dev = fake
        daemon.apply("phonesrc", 1, "mixa", {})
        self.assertEqual(fake.calls, [("set_phones_source", ("mixa",), {})])

        calls, skips = io24_scene.plan({"global": {"phonesSrc": 2}})
        self.assertEqual(calls, [
            ("set_phones_source", (2,), {}, "headphones source = 2"),
        ])
        self.assertFalse(any("phonesSrc" in skip for skip in skips))

        state = io24gtk.shadow_ui_state({
            "phones": {"fn": "set_phones_source", "kwargs": {"source": "aux1"}},
        })
        self.assertEqual(state["phones_source"], 1)

        shim = object.__new__(ucnet_shim.Shim)
        shim.state = SimpleNamespace(values={})
        shim.dry_run = False
        shim.dev = fake = CallDevice()
        shim.usb_lock = threading.Lock()
        shim.apply("global/phonesSrc", 1.0)
        self.assertEqual(fake.calls, [("set_phones_source", (2,), {})])

    def test_programmatic_gtk_adoption_is_silent(self):
        writes = []
        messages = []
        host = SimpleNamespace(
            _phones_source_mute=True,
            _set=lambda *args: writes.append(args),
            say=messages.append,
        )
        row = SimpleNamespace(get_selected=lambda: 2)
        io24gtk.Win._phones_source_changed(host, row, None)
        self.assertEqual(writes, [])
        self.assertEqual(messages, [])

        host._phones_source_mute = False
        io24gtk.Win._phones_source_changed(host, row, None)
        self.assertEqual(writes, [("phonesrc", None, "mixb")])

    def test_ucnet_does_not_publish_a_fabricated_phones_source(self):
        state = ucnet_shim.State(None)
        self.assertNotIn("global/phonesSrc", state.values)

        dev = SimpleNamespace(_shadow={"phones": {
            "fn": "set_phones_source", "kwargs": {"source": "aux1"},
        }})
        shadowed = ucnet_shim.State(dev)
        self.assertEqual(shadowed.values["global/phonesSrc"], 0.5)


@unittest.skipUnless(
    Path(io24_presets.PRESET_JSON).is_file(),
    "requires factory data recovered from the maintainer's local UC install",
)
class FactoryPresetDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.presets = io24_presets.load()

    def _by_class(self, component, class_id):
        return next(copy.deepcopy(p) for p in self.presets.values()
                    if (p.get(component) or {}).get("__classid") == class_id)

    def test_all_three_compressor_classes_dispatch_to_their_own_builder(self):
        expected = {
            "{870D04F7-212E-4F9C-ADBB-39A97216433F}": (0, "threshold_db"),
            "{7F8A4262-D377-48E3-9D48-15D82C400A71}": (1, "peak"),
            "{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}": (2, "input_db"),
        }
        for class_id, (model, diagnostic) in expected.items():
            with self.subTest(class_id=class_id):
                preset = self._by_class("comp", class_id)
                preset = {"comp": preset["comp"]}
                dev = CallDevice()
                io24_presets.apply_preset(dev, preset, channel=2)
                name, args, kwargs = dev.calls[0]
                self.assertEqual((name, args, kwargs["model"]),
                                 ("set_compressor", (2,), model))
                self.assertIn(diagnostic, kwargs)

    def test_standard_outer_band_toggle_true_means_shelf(self):
        preset = self._by_class(
            "eq", "{A0A8A068-14F0-4B04-BB6F-AF8329D0E8EE}")
        eq = preset["eq"]
        eq["eqallon"] = 1
        eq["eqbandon1"] = eq["eqbandon4"] = 1
        eq["eqbandop1"] = 1
        eq["eqbandop4"] = 0

        bands = io24_presets.to_bands({"eq": eq})

        self.assertEqual(bands[0]["shape"], "lowshelf")
        self.assertEqual(bands[3]["shape"], "peaking")

    @unittest.skipUnless(io24_alt_eq.designer_status()[0],
                         "exact UC 4.7.2 EQ designer is unavailable")
    def test_alternate_eq_dispatches_exactly_without_standard_flattening(self):
        for class_id, model in (
                ("{C0730CBB-5135-4558-9222-C40BDBA036ED}", "Passive"),
                ("{E1C5E024-C5CD-473C-B08A-6EC177812E01}", "Vintage")):
            with self.subTest(model=model):
                preset = self._by_class("eq", class_id)
                preset["opt"] = {"swapcompeq": 1}
                dev = CallDevice()
                done = io24_presets.apply_preset(
                    dev, preset, channel=2, fs=96000.0)
                self.assertEqual(dev.calls[0], (
                    "set_comp_eq_order", (2, True), {}))
                alternate_call = next(
                    call for call in dev.calls
                    if call[0] == "set_alternate_eq")
                self.assertEqual(alternate_call[1][0], 2)
                self.assertEqual(alternate_call[2]["fs"], 96000.0)
                self.assertIn("eq(%s)" % model.lower(), done)
                with self.assertRaises(io24_presets.UnsupportedPresetModel):
                    io24_presets.to_bands(preset)
                supported, reason = io24_presets.direct_apply_support(preset)
                self.assertTrue(supported, reason)
                self.assertEqual(reason, "exact direct-live mapping")

    def test_malformed_standard_eq_fails_before_an_order_write(self):
        preset = self._by_class(
            "eq", "{A0A8A068-14F0-4B04-BB6F-AF8329D0E8EE}")
        preset["opt"] = {"swapcompeq": 1}
        del preset["eq"]["eqfreq2"]
        dev = CallDevice()

        with self.assertRaisesRegex(ValueError, "Standard EQ missing field"):
            io24_presets.apply_preset(dev, preset)

        self.assertEqual(dev.calls, [])

    def test_malformed_compressors_fail_before_every_device_call(self):
        cases = (
            ("{870D04F7-212E-4F9C-ADBB-39A97216433F}",
             "threshold", math.nan),
            ("{870D04F7-212E-4F9C-ADBB-39A97216433F}",
             "softknee", 2),
            ("{7F8A4262-D377-48E3-9D48-15D82C400A71}",
             "peak", 101.0),
            ("{7F8A4262-D377-48E3-9D48-15D82C400A71}",
             "mode", 2),
            ("{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}",
             "ratio", 1.5),
            ("{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}",
             "release", math.inf),
        )
        for class_id, field, value in cases:
            with self.subTest(class_id=class_id, field=field, value=value):
                preset = self._by_class("comp", class_id)
                preset["opt"] = {"swapcompeq": 1}
                preset["filter"] = {"hpf": 80.0}
                preset["gate"] = {"on": 0}
                preset["comp"][field] = value
                dev = CallDevice()

                with self.assertRaises(ValueError):
                    io24_presets.apply_preset(dev, preset, channel=2)

                self.assertEqual(dev.calls, [])

    def test_below_minimum_compressor_keyfilters_fail_before_every_device_call(self):
        for class_id, model in (
                ("{870D04F7-212E-4F9C-ADBB-39A97216433F}", "Standard"),
                ("{7F8A4262-D377-48E3-9D48-15D82C400A71}", "Tube"),
                ("{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}", "FET")):
            with self.subTest(model=model):
                preset = self._by_class("comp", class_id)
                preset = {"comp": preset["comp"]}
                preset["opt"] = {"swapcompeq": 1}
                preset["filter"] = {"hpf": 80.0}
                preset["gate"] = {"on": 0}
                preset["comp"]["keyfilter"] = 1.0
                dev = CallDevice()

                with self.assertRaisesRegex(
                        ValueError, r"off sentinel or in \[40.0, 16000.0\]"):
                    io24_presets.apply_preset(dev, preset, channel=2)

                self.assertEqual(dev.calls, [])

    def test_selected_compressor_builder_serializes_before_device_calls(self):
        preset = self._by_class(
            "comp", "{7F8A4262-D377-48E3-9D48-15D82C400A71}")
        preset["opt"] = {"swapcompeq": 1}
        preset["filter"] = {"hpf": 80.0}
        dev = CallDevice()

        with mock.patch.object(
                io24_presets.io24_dsp, "cpxt_tube",
                side_effect=ValueError("serialization refused")):
            with self.assertRaisesRegex(ValueError, "serialization refused"):
                io24_presets.apply_preset(dev, preset, channel=2)

        self.assertEqual(dev.calls, [])

    def test_gtk_compressor_adoption_uses_the_dispatch_model_resolver(self):
        class Value:
            def __init__(self):
                self.value = None

            def set_value(self, value):
                self.value = value

            def set_active(self, value):
                self.value = value

            def set_selected(self, value):
                self.value = value

        class Stack:
            def __init__(self):
                self.name = None

            def set_visible_child_name(self, name):
                self.name = name

        widgets = {name: Value() for name in (
            "gth", "grange", "gatk", "grel", "gkey", "gklisten", "gexp",
            "model", "ckey", "cklisten", "cth", "rat", "catk", "crel",
            "mk", "knee", "auto", "tpeak", "tgain", "climit", "finput",
            "foutput", "fatk", "frel", "fratio", "lth",
        )}
        widgets["comp_param_stack"] = Stack()
        host = SimpleNamespace(w={1: widgets}, _adopt_mute=False)
        preset = {"comp": {
            "__classid": "{870D04F7-212E-4F9C-ADBB-39A97216433F}",
            "on": 1, "threshold": -24.0, "ratio": 3.0,
            "attack": 0.02, "release": 0.15, "gain": 4.0,
            "softknee": 1, "automode": 0,
            "keyfilter": 40.0, "keylisten": 0,
            # A misleading foreign-model field must not override the class ID.
            "input": -31.0,
        }}

        io24gtk.Win._adopt_dynamics(host, 1, preset)

        self.assertEqual(widgets["model"].value, 0)
        self.assertEqual(widgets["comp_param_stack"].name, "standard")
        self.assertEqual(widgets["cth"].value, -24.0)

    def test_scene_vintage_frequency_tables_are_exact_uc_lists(self):
        self.assertEqual(io24_scene.VINTAGE_LOW_HZ, (35.0, 60.0, 110.0, 220.0))
        self.assertEqual(io24_scene.VINTAGE_LOWMID_HZ, (360.0, 700.0, 1600.0))
        self.assertEqual(io24_scene.VINTAGE_HIMID_HZ, (3200.0, 4800.0, 7200.0))
        self.assertEqual(io24_scene.VINTAGE_HIGH_HZ, 12000.0)

    @unittest.skipUnless(io24_alt_eq.designer_status()[0],
                         "exact UC 4.7.2 EQ designer is unavailable")
    def test_scene_vintage_uses_exact_alternate_eq_not_standard_biquads(self):
        scene = {"line": {"ch1": {"eq": {
            "__classid": io24_scene.EQ_VINTAGE,
            "eqallon": 1,
            "lowgain": 4.0, "lowfreq": 2,
            "lowmidgain": -2.0, "lowmidfreq": 1,
            "himidgain": 3.0, "himidfreq": 2,
            "higain": 1.0,
        }}}}

        calls, skips = io24_scene.plan(scene, vintage=True)

        self.assertFalse(any(call[0] == "set_eq_band" for call in calls))
        alternate = [call for call in calls if call[0] == "set_alternate_eq"]
        self.assertEqual(len(alternate), 1)
        self.assertEqual(alternate[0][1][0], 1)
        self.assertEqual(alternate[0][1][1]["__classid"], io24_scene.EQ_VINTAGE)
        self.assertEqual(skips, [])


class ArtifactAndResourceTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    RUN = ROOT / "runs" / "20260906T165749-0700-host-live-routing-repair"

    @unittest.skipUnless(
        (RUN / "PARAMETER-VERIFICATION-MATRIX.json").is_file(),
        "requires private live-campaign evidence",
    )
    def test_matrix_hash_inventory_binds_all_dispatch_sources(self):
        matrix = json.loads((self.RUN / "PARAMETER-VERIFICATION-MATRIX.json")
                            .read_text())
        for name in ("io24d.py", "io24_scene.py", "ucnet_shim.py"):
            with self.subTest(name=name):
                expected = hashlib.sha256((self.ROOT / name).read_bytes()).hexdigest()
                self.assertEqual(matrix["source_sha256"].get(name), expected)

    @unittest.skipUnless(
        (RUN / "PARAMETER-INVENTORY.json").is_file(),
        "requires private live-campaign evidence",
    )
    def test_inventory_records_implemented_exact_host_routes(self):
        inventory = json.loads((self.RUN / "PARAMETER-INVENTORY.json").read_text())
        unresolved = set(inventory["known_unimplemented_or_unresolved"])
        self.assertNotIn("global/phonesSrc", unresolved)
        # The Host A/B (Hot Key) was removed at the user's request
        # (2026-09-11), so UC's Hot Key routes are unimplemented again.
        self.assertIn("line/ch1/presetHotKey", unresolved)
        self.assertIn("line/ch2/presetHotKey", unresolved)
        self.assertNotIn("passive_eq_exact_builder", unresolved)
        self.assertNotIn("vintage_eq_exact_builder", unresolved)
        policy = inventory["alternate_eq_per_physical_channel"]["direct_live_policy"]
        self.assertIn("exact model-specific live routes", policy)
        self.assertIn("fail before transport", policy)
        self.assertIn("no Standard-biquad approximation", policy)
        supported = inventory["supported_host_capabilities"]
        self.assertEqual(supported["phones_source"]["route"],
                         "global/phonesSrc")
        self.assertEqual(supported["phones_source"]["readback"],
                         "write_only_host_shadow")
        self.assertNotIn("host_hot_key", supported)

    def test_factory_preset_load_closes_its_input_file(self):
        records = [{"preset_name": "test", "comp": {}}]
        with tempfile.NamedTemporaryFile("w", suffix=".json") as stream:
            json.dump(records, stream)
            stream.flush()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                self.assertIn("test", io24_presets.load(stream.name))
                gc.collect()
        self.assertFalse([warning for warning in caught
                          if issubclass(warning.category, ResourceWarning)])


class HostRefinementTests(unittest.TestCase):
    @staticmethod
    def _multiband_state():
        bands = {}
        for index, name in enumerate(io24_mbc.BANDS):
            bands[name] = {
                "type": ("standard", "tube", "fet", "standard")[index],
                "standard": {
                    "threshold_db": -18.0 - index,
                    "ratio": 2.0 + index,
                    "attack_s": 0.004 + index * 0.001,
                    "release_s": 0.09 + index * 0.01,
                    "gain_db": 1.0 + index,
                    "softknee": index % 2 == 0,
                    "automode": index == 0,
                },
                "tube": {
                    "peak": 10.0 + index,
                    "gain": 40.0 + index,
                    "limit_mode": index == 1,
                },
                "fet": {
                    "input_db": -18.0 - index,
                    "output_db": -4.0 - index,
                    "attack_s": 0.0001 + index * 0.0001,
                    "release_s": 0.09 + index * 0.01,
                    "ratio_index": index,
                },
                "key_filter": index == 2,
                "key": 120.0 * (index + 1),
                "listen": index == 3,
            }
        return {
            "version": io24_mbc.SNAPSHOT_VERSION,
            "enabled": True,
            "xovers": [140.0, 900.0, 4200.0],
            "bands": bands,
        }

    def test_multiband_state_round_trips_in_host_file_only(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False
                self.applied = []

            def read_params(self):
                return {}

            def set_gain(self, channel, db):
                self.applied.append((channel, db))

        state = self._multiband_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.json"
            source = SnapshotIo24()
            saved = source.save_preset(
                path, host_features={"multiband": state})
            self.assertEqual(saved["host_features"]["multiband"], state)
            self.assertNotIn("host_features", source.snapshot())

            restored = SnapshotIo24()
            restored.load_preset(path)
            self.assertEqual(
                restored._last_preset_load_report["host_features"],
                {"multiband": state})

    def test_host_file_ignores_removed_bus_pan_and_reports_the_discard(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False
                self.applied = []

            def set_gain(self, channel, db):
                self.applied.append((channel, db))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-host.json"
            path.write_text(json.dumps({
                "version": 1,
                "live": {"input1Gain": 31.0},
                "calls": {},
                "host_features": {"pan": {"arbitrary": "old data"}},
            }))
            device = SnapshotIo24()

            device.load_preset(path)

        report = device._last_preset_load_report
        self.assertEqual(device.applied, [(1, 31.0)])
        self.assertEqual(report["host_features"], {})
        self.assertEqual(report["host_feature_migrations"], [
            "legacy Host bus pan was ignored; bus pan controls were removed",
        ])

    def test_removed_bus_pan_data_cannot_block_device_state_load(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False
                self.applied = []

            def set_gain(self, channel, db):
                self.applied.append((channel, db))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad-legacy-host.json"
            path.write_text(json.dumps({
                "version": 1,
                "live": {"input1Gain": 31.0},
                "calls": {},
                "host_features": {"pan": {
                    "version": 1,
                    "enabled": True,
                    "positions": [0.5, 1.2],
                }},
            }))
            device = SnapshotIo24()

            device.load_preset(path)

        self.assertEqual(device.applied, [(1, 31.0)])
        self.assertNotIn(
            "pan", device._last_preset_load_report["host_features"])

    def test_saving_removed_bus_pan_drops_it(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False

            def read_params(self):
                return {}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normalized-host.json"
            device = SnapshotIo24()

            saved = device.save_preset(path, host_features={"pan": {
                "version": 1,
                "enabled": False,
                "positions": [0.1, 0.9],
            }})

        self.assertNotIn("host_features", saved)

    def test_bus_graph_selects_only_its_exact_capture_pair(self):
        mixa = io24_mbc.build_bus_source_graph("mixa")
        mixb = io24_mbc.build_bus_source_graph("mixb")

        self.assertEqual(mixa["inputs"], [
            "src0:In", "src1:In", "src2:In",
            "src3:In", "src4:In", "src5:In",
        ])
        self.assertEqual(mixb["inputs"], mixa["inputs"])
        self.assertEqual(mixa["outputs"],
                         ["source_l:Out", "source_r:Out"])
        self.assertEqual(mixb["outputs"], mixa["outputs"])
        mixa_routes = {(link["output"], link["input"])
                       for link in mixa["links"]}
        mixb_routes = {(link["output"], link["input"])
                       for link in mixb["links"]}
        self.assertIn(("src2:Out", "source_l:In"), mixa_routes)
        self.assertIn(("src3:Out", "source_r:In"), mixa_routes)
        self.assertIn(("src4:Out", "source_l:In"), mixb_routes)
        self.assertIn(("src5:Out", "source_r:In"), mixb_routes)
        self.assertFalse(any(route[0] in {"src0:Out", "src1:Out"}
                             for route in mixa_routes | mixb_routes))
        for graph in (mixa, mixb):
            gains = {node["name"]: node["control"]["Mult"]
                     for node in graph["nodes"]
                     if node["name"] in {"source_l", "source_r"}}
            self.assertEqual(gains, {"source_l": 1.0, "source_r": 1.0})

    def test_bus_source_conf_is_passive_source_without_playback_target(self):
        for bus, label in (("mixa", "io24 Host Mix A"),
                           ("mixb", "io24 Host Mix B")):
            with self.subTest(bus=bus):
                conf = io24_mbc.build_bus_source_conf(
                    bus, "alsa_input.usb-PreSonus_io24")
                self.assertIn(
                    '"target.object": "alsa_input.usb-PreSonus_io24"', conf)
                self.assertIn('"media.class": "Stream/Input/Audio"', conf)
                self.assertIn('"media.class": "Audio/Source"', conf)
                self.assertIn('"node.description": "%s"' % label, conf)
                self.assertEqual(conf.count('"stream.dont-remix": true'), 2)
                self.assertEqual(conf.count('"node.passive": true'), 2)
                self.assertNotIn('"media.class": "Audio/Sink"', conf)
                self.assertNotIn("alsa_output", conf)

    def test_io24_audio_node_discovery_keeps_input_and_output_distinct(self):
        # Real pw-cli prefixes each id line with a tab. A parser that splits
        # only on "\nid " sees this as one giant block and returns the first
        # ordinary ALSA input/output once any later io24 marker is present.
        listing = """\tid 40, type PipeWire:Interface:Node/3
            node.name = \"alsa_input.pci-0000_04_00.6.analog-stereo\"
            node.description = \"Laptop Analog Stereo\"
        \tid 41, type PipeWire:Interface:Node/3
            node.name = \"alsa_output.pci-0000_04_00.6.analog-stereo\"
            node.description = \"Laptop Analog Stereo\"
        \tid 42, type PipeWire:Interface:Node/3
            node.name = \"alsa_input.usb-PreSonus_Revelator_io24-00.pro-input-0\"
            node.description = \"Revelator io24 Pro Input\"
        \tid 43, type PipeWire:Interface:Node/3
            node.name = \"alsa_output.usb-PreSonus_Revelator_io24-00.pro-output-0\"
            node.description = \"Revelator io24 Pro Output\"
        """
        completed = SimpleNamespace(stdout=listing)
        with mock.patch.object(io24_mbc.subprocess, "run",
                               return_value=completed):
            self.assertEqual(
                io24_mbc.find_io24_capture_source(),
                "alsa_input.usb-PreSonus_Revelator_io24-00.pro-input-0")
            self.assertEqual(
                io24_mbc.find_io24_sink(),
                "alsa_output.usb-PreSonus_Revelator_io24-00.pro-output-0")

    def test_old_playback_multiband_settings_seed_both_inputs_switched_off(self):
        # 2026-09-11: the playback multiband became the compressor type
        # Multiband. An older session or snapshot still carries its panel.
        state = self._multiband_state()
        host = SimpleNamespace(
            mbc_ctl={ch: _multiband_controls() for ch in (1, 2)},
            _mbc_mute=False, _insert_stale=False,
            _multiband_insert_wanted=lambda: ())
        for name in ("_adopt_mbc_controls", "_mbc_snapshot_state",
                     "_mbc_band_state"):
            setattr(host, name, getattr(io24gtk.Win, name).__get__(host))

        status = io24gtk.Win._adopt_legacy_multiband(host, state)

        self.assertIn("switched off", status)
        expected = dict(state, enabled=False)
        for ch in (1, 2):
            self.assertEqual(host._mbc_snapshot_state(ch), expected)
        self.assertTrue(host._insert_stale)
        self.assertFalse(host._mbc_mute)

    def test_invalid_multiband_file_fails_before_device_state_is_applied(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False
                self.applied = []

            def set_gain(self, channel, db):
                self.applied.append((channel, db))

        bad = self._multiband_state()
        bad["bands"]["low"]["standard"]["ratio"] = 0.5
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({
                "version": 1,
                "live": {"input1Gain": 31.0},
                "calls": {},
                "host_features": {"multiband": bad},
            }))
            device = SnapshotIo24()
            with self.assertRaisesRegex(ValueError, "multiband.*ratio"):
                device.load_preset(path)
            self.assertEqual(device.applied, [])

    def test_multiband_graph_starts_from_every_saved_audible_value(self):
        state = self._multiband_state()
        normalized = io24_mbc.validate_snapshot(state)
        graph = io24_mbc.build_graph(
            normalized["xovers"], True, state=normalized)
        nodes = {node["name"]: node for node in graph["nodes"]}

        for index, band in enumerate(io24_mbc.BANDS):
            node = nodes["c%d" % index]
            self.assertEqual(node["label"], "io24_uc_comp")
            self.assertEqual(
                node["control"],
                io24_mbc.compressor_controls(normalized["bands"][band]))
        low = nodes["c0"]["control"]
        self.assertEqual(low["Threshold level (dB)"], -18.0)
        self.assertEqual(low["Slope"], 0.5)
        # Auto mode is an exact UC behavior: it forces 10 ms / 150 ms.
        self.assertAlmostEqual(low["Attack time (s)"], 0.01)
        self.assertNotEqual(nodes["c2"]["control"]["Biquad b0"], 1.0)
        self.assertEqual(nodes["c3"]["control"]["Key listen"], 1.0)

    def test_spectrum_uses_overlapping_windows_and_time_based_release(self):
        frame, remainder = io24gtk.Spectrum._take_window(
            b"0123456789", need=8, hop=2)
        self.assertEqual(frame, b"01234567")
        self.assertEqual(remainder, b"23456789")
        self.assertEqual(io24gtk.Spectrum.BINS, 256)
        self.assertGreaterEqual(48000 / io24gtk.Spectrum.HOP, 40.0)
        self.assertAlmostEqual(
            io24gtk.Spectrum._release_alpha(
                io24gtk.Spectrum.RELEASE_TAU_S), math.exp(-1.0), places=7)

    def test_curve_drag_publishes_one_coherent_band_update(self):
        class Value:
            def __init__(self, host, key):
                self.host = host
                self.key = key

            def set_value(self, value):
                self.host.band[self.key] = value
                if not self.host._adopt_mute:
                    self.host.pushes.append(dict(self.host.band))

        host = SimpleNamespace(
            band={"shape": "peaking", "freq": 100.0,
                  "gain": 0.0, "q": 0.7},
            _adopt_mute=False,
            pushes=[],
            invalidate_curve=lambda: None,
            racks={},
        )
        curve = SimpleNamespace(
            win=host,
            channel=1,
            drag_band=0,
            _start=(10.0, 10.0, 100.0, 0.0),
            bands=[host.band],
            wid=lambda key: Value(host, key),
            get_width=lambda: 200,
            get_height=lambda: 100,
            _f_from_x=lambda x, _w: x * 10.0,
            queue_draw=lambda: None,
            _drag_push=lambda: host.pushes.append(dict(host.band)),
        )

        io24gtk.EQCurve._drag_update(curve, None, 20.0, -10.0)

        self.assertEqual(host.pushes, [{
            "shape": "peaking", "freq": 300.0,
            "gain": 3.6, "q": 0.7,
        }])

    def test_continuous_hpf_move_uses_inline_feedback_without_toast(self):
        messages = []
        submitted = []
        host = SimpleNamespace(
            link_both=False,
            _adopt_mute=False,
            _fs=48000.0,
            hpf_by_ch={1: 24.0, 2: 24.0},
            ctl=SimpleNamespace(submit=submitted.append),
            _adopt_hpf_controls=lambda _ch, _hz: None,
            racks={},
            say=messages.append,
        )

        io24gtk.Win.set_hpf(host, 1, 347.0)

        self.assertEqual(len(submitted), 1)
        self.assertEqual(messages, [])

    def test_preset_indicator_identifies_slot_and_respects_reduced_motion(self):
        visual = getattr(
            io24gtk, "preset_indicator_visual",
            lambda *_args: {"glyph": None, "opacity": None, "radius": None})
        slow_a = visual(True, 0, 0.0, True)
        slow_b = visual(True, 0, 0.9, True)
        fast_b = visual(True, 1, 0.45, True)

        self.assertEqual(slow_a["glyph"], "●")
        self.assertEqual(fast_b["glyph"], "●")
        self.assertEqual(slow_a["opacity"], 0.25)
        self.assertEqual(slow_b["opacity"], 1.0)
        self.assertEqual(fast_b["opacity"], 1.0)
        self.assertEqual(slow_a["radius"], 5.0)
        self.assertEqual(slow_b["radius"], 5.0)
        self.assertEqual(fast_b["radius"], 5.0)
        # Internal slot index 0 is user-facing Slot 1 (slow); index 1 is
        # user-facing Slot 2 (twice the breathing rate).
        reduced_slow = visual(True, 0, 0.8, False)
        reduced_fast = visual(True, 1, 0.8, False)
        self.assertEqual(reduced_slow["opacity"], 1.0)
        self.assertEqual(reduced_fast["opacity"], 1.0)
        self.assertLess(reduced_slow["radius"], reduced_fast["radius"])
        bypassed = visual(False, 1, 0.8, True)
        self.assertEqual(bypassed["glyph"], "○")
        self.assertEqual(bypassed["opacity"], 0.28)

    def test_sections_have_no_embedded_descriptions(self):
        source = inspect.getsource(io24gtk.Win)
        self.assertNotIn("description=", source)

    def test_bus_meter_columns_have_exact_compact_labels(self):
        columns = getattr(io24gtk, "bus_meter_columns", lambda: ())
        self.assertEqual(columns(), (
            ("main", "Main", "Main output"),
            ("mixa", "Mix A", "USB capture 3–4"),
            ("mixb", "Mix B", "USB capture 5–6"),
        ))

    def test_mixer_removes_input_and_bus_pan_controls(self):
        input_source = inspect.getsource(io24gtk.Win._input_strip)
        bus_source = inspect.getsource(io24gtk.Win._bus_strip)

        self.assertNotIn("Knob(", input_source)
        self.assertNotIn("Knob(", bus_source)
        self.assertNotIn("balance", bus_source.casefold())
        self.assertNotIn("Gtk.ToggleButton", bus_source)
        self.assertNotIn("bus_balance_knobs", inspect.getsource(io24gtk.Win))

    def test_bus_source_watchdog_silently_disables_absent_sources(self):
        class Sources:
            def reconcile(self, capture):
                self.call = capture
                return {"mixa": "unavailable", "mixb": "unavailable"}

        messages = []
        sources = Sources()
        host = SimpleNamespace(
            bus_sources=sources,
            _bus_source_status={"mixa": "ready", "mixb": "ready"},
            say=messages.append,
        )
        host._set_bus_source_status = lambda bus, status, notify=False: (
            io24gtk.Win._set_bus_source_status(
                host, bus, status, notify=notify))
        with mock.patch.object(io24_mbc, "find_io24_capture_source",
                               return_value=None):
            keep = io24gtk.Win._bus_source_watchdog(host)

        self.assertTrue(keep)
        self.assertEqual(messages, [])
        self.assertIsNone(sources.call)

    def test_bus_source_failure_toasts_once_and_recovers_independently(self):
        class Sources:
            def __init__(self):
                self.results = [
                    {"mixa": "failed", "mixb": "ready"},
                    {"mixa": "failed", "mixb": "ready"},
                    {"mixa": "ready", "mixb": "unavailable"},
                ]

            def reconcile(self, _capture):
                return self.results.pop(0)

        messages = []
        host = SimpleNamespace(
            bus_sources=Sources(),
            _bus_source_status={"mixa": "starting", "mixb": "starting"},
            say=messages.append,
        )
        host._set_bus_source_status = lambda bus, status, notify=False: (
            io24gtk.Win._set_bus_source_status(
                host, bus, status, notify=notify))
        with mock.patch.object(io24_mbc, "find_io24_capture_source",
                               return_value="capture-node"):
            io24gtk.Win._bus_source_watchdog(host)
            io24gtk.Win._bus_source_watchdog(host)
            io24gtk.Win._bus_source_watchdog(host)

        self.assertEqual(messages, ["io24 Host Mix A source failed to start"])

    def test_legacy_notice_is_appended_only_to_completed_load_message(self):
        notice = \
            "legacy Host bus pan was ignored; bus pan controls were removed"
        append = getattr(io24gtk, "append_host_migration_notices", None)

        self.assertIsNotNone(append)
        self.assertEqual(append("Loaded 4 host-file settings", [notice]),
                         "Loaded 4 host-file settings; " + notice)
        self.assertIsNone(append(None, [notice]))

    def test_application_shutdown_stops_both_bus_sources(self):
        stopped = []
        app = SimpleNamespace(win=SimpleNamespace(
            mbc=None,
            bus_sources=SimpleNamespace(stop=lambda: stopped.append(True))))
        with mock.patch.object(io24gtk.Adw.Application, "do_shutdown"):
            io24gtk.App.do_shutdown(app)
        self.assertEqual(stopped, [True])

    def test_user_docs_describe_passive_bus_sources_and_unavailable_main(self):
        for filename in ("README.md", "GUIDE.md"):
            with self.subTest(filename=filename):
                content = Path(filename).read_text()
                self.assertIn("io24 Host Mix A", content)
                self.assertIn("io24 Host Mix B", content)
                self.assertIn("USB capture 3–4", content)
                self.assertIn("USB capture 5–6", content)
                self.assertIn("Main is not exposed by USB capture", content)
                self.assertIn("route nowhere by default", content)
                self.assertIn("host_features.pan", content)
                self.assertIn("ignored", content)
                self.assertNotIn("Host input pan", content)
                self.assertNotIn("capture→playback", content)

    def test_spectrum_resolves_low_guitar_without_slower_updates(self):
        spectrum = io24gtk.Spectrum(autostart=False)

        resolution = getattr(spectrum, "frequency_resolution_hz",
                             lambda: float("inf"))
        interval = getattr(spectrum, "update_interval_seconds",
                           lambda: float("inf"))
        self.assertLessEqual(resolution(), 6.0)
        self.assertLessEqual(interval(), 0.022)

    def test_reconnect_ui_keeps_connection_banner_without_reapply_prompt(self):
        init_source = inspect.getsource(io24gtk.Win.__init__)
        window_source = inspect.getsource(io24gtk.Win)

        self.assertIn("self.offline_banner", init_source)
        self.assertNotIn("reconcile_banner", window_source)
        self.assertNotIn("def _reapply_cached", window_source)
        self.assertNotIn("def _forget_cached", window_source)

    def test_routine_control_changes_across_tabs_do_not_toast(self):
        messages = []
        submitted = []

        phones = SimpleNamespace(
            _phones_source_mute=False,
            _set=lambda *args: submitted.append(args),
            say=messages.append,
        )
        io24gtk.Win._phones_source_changed(
            phones, SimpleNamespace(get_selected=lambda: 1), None)

        presets = SimpleNamespace(
            _preset_sync=False,
            PRESET_BASE=io24gtk.Win.PRESET_BASE,
            ctl=SimpleNamespace(submit=submitted.append),
            say=messages.append,
        )
        io24gtk.Win._preset_mode_changed(
            presets, SimpleNamespace(get_selected=lambda: 1), None)
        io24gtk.Win._slot_changed(presets, 1, 1)

        order = SimpleNamespace(
            _order_mute=False, _adopt_mute=False, link_both=False,
            order_by_ch={1: False, 2: False},
            _current_channel=lambda: 1,
            set_order=lambda *args: submitted.append(args),
            say=messages.append,
        )
        io24gtk.Win._order_changed(
            order, SimpleNamespace(get_selected=lambda: 1), None)

        delay_scale = SimpleNamespace(
            get_value=lambda: 12.0,
            set_sensitive=lambda _sensitive: None,
        )
        device_page = SimpleNamespace(
            _dev_mute=False, _delay_mute=False,
            _quanta=[128], _delay_buses=[("main", "Main")],
            delay_scale=delay_scale,
            ctl=SimpleNamespace(submit=submitted.append),
            say=messages.append,
        )
        with mock.patch.object(io24gtk, "pw_set", return_value=(True, "ok")):
            io24gtk.Win._quantum_changed(
                device_page, SimpleNamespace(get_selected=lambda: 0), None)
        io24gtk.Win._delay_bus_changed(
            device_page, SimpleNamespace(get_selected=lambda: 0), None)

        self.assertEqual(messages, [])

    def test_voice_fx_parameter_success_and_offline_state_are_silent(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get_value(self):
                return self.value

            def get_active(self):
                return bool(self.value)

            def get_selected(self):
                return int(self.value)

        class Device:
            def __init__(self):
                self.fail = False

            def set_voicefx_channel(self, _channel):
                pass

            def set_fx(self, _model, **_params):
                if self.fail:
                    raise RuntimeError("broken transport")
                return 3

        class Controller:
            def __init__(self, device):
                self.dev = device

            def submit(self, callback):
                callback(self.dev)

        device = Device()
        messages = []
        host = SimpleNamespace(
            _fx_mute=False,
            _fs=48000.0,
            FX_ORDER=io24gtk.Win.FX_ORDER,
            _fx_last_sent_device=None,
            _fx_last_sent_target=None,
            ctl=Controller(device),
            fx_arm=Value(True), fx_model=Value(0), fx_target=Value(0),
            fx_params={"transformer": {
                name: Value(default)
                for name, _title, _lo, _hi, _step, default, _fmt
                in io24gtk.Win.FX_PARAMS["transformer"]
            }},
            say=messages.append,
        )
        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            io24gtk.Win._push_fx(host)
            self.assertEqual(messages, [])

            device.fail = True
            io24gtk.Win._push_fx(host)
            self.assertEqual(messages, [
                "Voice FX send failed: broken transport",
            ])

            host.ctl.dev = None
            io24gtk.Win._push_fx(host)
            self.assertEqual(messages, [
                "Voice FX send failed: broken transport",
            ])

    def test_after_load_tolerates_presets_page_not_built_yet(self):
        source = inspect.getsource(io24gtk.Win._after_load)
        self.assertIn('getattr(self, "preset_rows", {}).get(ch)', source)

    def test_factory_load_reports_worker_completion_not_early_queueing(self):
        class Toggle:
            def set_active(self, _active):
                pass

            def queue_draw(self):
                pass

        class Presets:
            def __init__(self):
                self.fail = False

            def load(self):
                return {"Clean": {
                    "filter": {"hpf": 24.0}, "gate": {}, "comp": {},
                    "limit": {}, "opt": {}, "voicefx": {},
                }}

            def direct_apply_support(self, _preset):
                return True, ""

            def to_bands(self, _preset):
                return []

            def apply_preset(self, _dev, _preset, _channel, with_fx=False,
                             fs=None):
                if fs != 48000.0:
                    raise AssertionError("factory load lost its runtime rate")
                if self.fail:
                    raise RuntimeError("apply broke")

        class Controller:
            def __init__(self):
                self.pending = []

            def submit(self, callback):
                self.pending.append(callback)

        messages = []
        presets = Presets()
        ctl = Controller()
        toggle = Toggle()
        host = SimpleNamespace(
            PR=presets, ctl=ctl, link_both=False, _fs=48000.0,
            _factory_target_channel=lambda: 1,
            _current_channel=lambda: 1,
            bands_by_ch={1: []}, dyn_by_ch={1: {}},
            hpf_by_ch={1: 24.0}, order_by_ch={1: False},
            invalidate_curve=lambda: None, _adopt_mute=False,
            _insert_reconcile=lambda restart=False: None,
            _adopt_band=lambda _ch: None,
            _adopt_hpf_controls=lambda _ch, _hz: None,
            _adopt_dynamics=lambda _ch, _preset: None,
            _sync_order_row=lambda _ch: None,
            w={1: {"gate_on": toggle, "comp_on": toggle,
                   "lim_on": toggle, "curve": toggle}},
            racks={}, say=messages.append,
        )
        host._adopt_preset_record = lambda preset, channels: \
            io24gtk.Win._adopt_preset_record(host, preset, channels)

        io24gtk.Win._load_factory(host, None, "Clean", False)
        self.assertEqual(messages, [])
        self.assertEqual(len(ctl.pending), 1)

        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            ctl.pending.pop()(object())
        self.assertEqual(messages, ["Loaded Clean on Channel 1"])

        presets.fail = True
        io24gtk.Win._load_factory(host, None, "Clean", False)
        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            ctl.pending.pop()(object())
        self.assertEqual(messages[-1], "Clean load failed: apply broke")

    def test_mixer_layout_fits_width_instead_of_enabling_horizontal_scroll(self):
        class Box:
            def __init__(self, **kwargs):
                self.homogeneous = kwargs.get("homogeneous", False)
                self.children = []

            def set_margin_start(self, value):
                self.margin_start = value

            def set_margin_end(self, value):
                self.margin_end = value

            def append(self, child):
                self.children.append(child)

        class Strip:
            def set_hexpand(self, value):
                self.hexpand = value

        class Scrolled:
            def set_child(self, child):
                self.child = child

            def set_policy(self, horizontal, vertical):
                self.policy = (horizontal, vertical)

        class Clamp:
            def __init__(self, **kwargs):
                self.maximum_size = kwargs.get("maximum_size")
                self.tightening_threshold = kwargs.get(
                    "tightening_threshold")

            def set_child(self, child):
                self.child = child

            def set_hexpand(self, value):
                self.hexpand = value

        strips = [Strip() for _ in range(4)]
        host = SimpleNamespace(
            _input_strip=lambda ch: strips[ch - 1],
            _bus_strip=lambda: strips[2],
            _master_strip=lambda: strips[3],
        )
        with mock.patch.object(io24gtk.Gtk, "Box", Box), \
                mock.patch.object(io24gtk.Gtk, "ScrolledWindow", Scrolled), \
                mock.patch.object(io24gtk.Adw, "Clamp", Clamp):
            page = io24gtk.Win._mixer_page(host)

        self.assertEqual(page.child.maximum_size, io24gtk.MIXER_WIDTH)
        self.assertEqual(
            page.child.tightening_threshold, io24gtk.MIXER_TIGHTEN)
        self.assertTrue(page.child.hexpand)
        self.assertTrue(page.child.child.homogeneous)
        self.assertEqual(
            page.policy[0], io24gtk.Gtk.PolicyType.NEVER)
        self.assertEqual([strip.hexpand for strip in strips],
                         [True, True, True, True])

    def test_mixer_fader_banks_grow_vertically_at_large_window_sizes(self):
        class Box:
            def __init__(self, **kwargs):
                self.homogeneous = kwargs.get("homogeneous", False)
                self.halign = kwargs.get("halign")

            def set_size_request(self, width, height):
                self.size_request = (width, height)

            def set_vexpand(self, value):
                self.vexpand = value

        with mock.patch.object(io24gtk.Gtk, "Box", Box):
            input_bank = io24gtk.mixer_fader_bank(8)
            monitor_bank = io24gtk.mixer_fader_bank(14, homogeneous=True)

        self.assertEqual(input_bank.size_request, (-1, 260))
        self.assertTrue(input_bank.vexpand)
        self.assertFalse(input_bank.homogeneous)
        self.assertTrue(monitor_bank.vexpand)
        self.assertTrue(monitor_bank.homogeneous)
        self.assertIn(
            "mixer_fader_bank(14, homogeneous=True)",
            inspect.getsource(io24gtk.Win._master_strip))

    def test_factory_sound_filter_matches_name_and_description(self):
        self.assertTrue(io24gtk.factory_preset_matches(
            "guitar", "Bright Guitar", "Open modern instrument curve"))
        self.assertTrue(io24gtk.factory_preset_matches(
            "modern", "Bright Guitar", "Open modern instrument curve"))
        self.assertFalse(io24gtk.factory_preset_matches(
            "vocal", "Bright Guitar", "Open modern instrument curve"))

    def test_eq_drag_throttle_keeps_the_final_coherent_update(self):
        sent = []
        callbacks = []
        now = [1.0]
        throttle = io24gtk.Throttle(sent.append, interval=0.012)

        def timeout_add(_wait, callback):
            callbacks.append(callback)
            return len(callbacks)

        with mock.patch.object(io24gtk.time, "monotonic",
                               side_effect=lambda: now[0]), \
                mock.patch.object(io24gtk.GLib, "timeout_add",
                                  side_effect=timeout_add):
            throttle({"freq": 100.0, "gain": 0.0})
            now[0] = 1.004
            throttle({"freq": 200.0, "gain": 1.0})
            now[0] = 1.009
            throttle({"freq": 300.0, "gain": 2.0})
            now[0] = 1.012
            callbacks[0]()

        self.assertEqual(sent, [
            {"freq": 100.0, "gain": 0.0},
            {"freq": 300.0, "gain": 2.0},
        ])

    def test_multiband_controls_capture_and_restore_one_channel_exactly(self):
        # Each input has its own Multiband page since it became a
        # compressor type; a failed start is covered in
        # test_io24_multiband_insert.py.
        state = self._multiband_state()
        controls = _multiband_controls()
        host = SimpleNamespace(mbc_ctl={1: controls}, _mbc_mute=False,
                               _multiband_insert_wanted=lambda: (1,))
        for name in ("_adopt_mbc_controls", "_mbc_snapshot_state",
                     "_mbc_band_state"):
            setattr(host, name, getattr(io24gtk.Win, name).__get__(host))

        host._adopt_mbc_controls(1, state)

        self.assertEqual(host._mbc_snapshot_state(1), state)
        for index, band in enumerate(io24_mbc.BANDS):
            self.assertEqual(
                controls[(index, "stack")].visible_child_name,
                state["bands"][band]["type"])
        self.assertFalse(host._mbc_mute)
        host._multiband_insert_wanted = lambda: ()
        self.assertFalse(host._mbc_snapshot_state(1)["enabled"])

    def test_multiband_selection_hides_single_band_only_controls(self):
        widgets = {name: _Control() for name in (
            "model", "comp_param_stack", "ckey_row", "cklisten",
            "comp_curve")}
        host = SimpleNamespace(w={1: widgets}, _adopt_mute=True)

        widgets["model"].set_selected(io24gtk.MULTIBAND_MODEL)
        io24gtk.Win._comp_model_changed(host, 1)

        self.assertEqual(widgets["comp_param_stack"].visible_child_name,
                         "multiband")
        self.assertFalse(widgets["ckey_row"].visible)
        self.assertFalse(widgets["cklisten"].visible)
        self.assertFalse(widgets["comp_curve"].visible)

        widgets["model"].set_selected(0)
        io24gtk.Win._comp_model_changed(host, 1)

        self.assertEqual(widgets["comp_param_stack"].visible_child_name,
                         "standard")
        self.assertTrue(widgets["ckey_row"].visible)
        self.assertTrue(widgets["cklisten"].visible)
        self.assertTrue(widgets["comp_curve"].visible)
        self.assertEqual(widgets["comp_curve"].draws, 2)

    def test_host_file_worker_uses_its_serialized_device_argument(self):
        picker = inspect.getsource(io24gtk.Win._pick)
        loader = inspect.getsource(io24gtk.Win._load_full_host_setup)

        self.assertIn("dev.save_preset(", picker)
        self.assertIn("self._load_full_host_setup(dev, path)", picker)
        self.assertIn("dev.load_preset(", loader)
        self.assertNotIn("self.ctl.dev.save_preset(", picker)
        self.assertNotIn("self.ctl.dev.load_preset(", picker + loader)

    def test_multiband_chain_start_failure_cleans_private_config(self):
        with tempfile.TemporaryDirectory() as parent:
            config = Path(parent) / "chain"
            config.mkdir()
            chain = io24_mbc.Chain()
            with mock.patch.object(io24_mbc.os.path, "exists",
                                   return_value=True), \
                    mock.patch.object(io24_mbc.tempfile, "mkdtemp",
                                      return_value=str(config)), \
                    mock.patch.object(io24_mbc.shutil, "copy"), \
                    mock.patch.object(io24_mbc, "uc_comp_available",
                                      return_value=None), \
                    mock.patch.object(io24_mbc.subprocess, "Popen",
                                      side_effect=OSError("spawn failed")):
                started = chain.start(
                    target="alsa_output.io24",
                    state=self._multiband_state())

            self.assertFalse(started)
            self.assertFalse(config.exists())
            self.assertIsNone(chain.proc)
            self.assertIsNone(chain.conf_path)

    def test_multiband_start_requires_public_node_not_only_live_child(self):
        class Process:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout):
                return 0

        with tempfile.TemporaryDirectory() as parent:
            config = Path(parent) / "chain"
            config.mkdir()
            chain = io24_mbc.Chain()
            with mock.patch.object(io24_mbc.os.path, "exists", return_value=True), \
                    mock.patch.object(io24_mbc.tempfile, "mkdtemp",
                                      return_value=str(config)), \
                    mock.patch.object(io24_mbc.shutil, "copy"), \
                    mock.patch.object(io24_mbc, "uc_comp_available",
                                      return_value=None), \
                    mock.patch.object(io24_mbc.subprocess, "Popen",
                                      return_value=Process()), \
                    mock.patch.object(chain, "node_id", return_value=None), \
                    mock.patch.object(io24_mbc.time, "sleep"), \
                    mock.patch.object(io24_mbc.time, "monotonic",
                                      side_effect=[0.0, 3.0]):
                started = chain.start(
                    target="alsa_output.io24",
                    state=self._multiband_state())

            self.assertFalse(started)
            self.assertFalse(config.exists())
            self.assertIn("node", chain.last_error.lower())

    def test_multiband_start_succeeds_after_public_node_appears(self):
        class Process:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as parent:
            config = Path(parent) / "chain"
            config.mkdir()
            chain = io24_mbc.Chain()
            with mock.patch.object(io24_mbc.os.path, "exists", return_value=True), \
                    mock.patch.object(io24_mbc.tempfile, "mkdtemp",
                                      return_value=str(config)), \
                    mock.patch.object(io24_mbc.shutil, "copy"), \
                    mock.patch.object(io24_mbc, "uc_comp_available",
                                      return_value=None), \
                    mock.patch.object(io24_mbc.subprocess, "Popen",
                                      return_value=Process()), \
                    mock.patch.object(chain, "node_id",
                                      side_effect=[None, "42"]), \
                    mock.patch.object(io24_mbc.time, "sleep"), \
                    mock.patch.object(io24_mbc.time, "monotonic",
                                      side_effect=[0.0, 0.1]):
                started = chain.start(
                    target="alsa_output.io24",
                    state=self._multiband_state())

            self.assertTrue(started)
            self.assertIsNone(chain.last_error)

    def test_multiband_stop_cleans_config_when_kill_loses_exit_race(self):
        class Process:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout):
                raise io24_mbc.subprocess.TimeoutExpired("pipewire", timeout)

            def kill(self):
                raise ProcessLookupError("already exited")

        with tempfile.TemporaryDirectory() as parent:
            config = Path(parent) / "old-chain"
            config.mkdir()
            chain = io24_mbc.Chain()
            chain.proc = Process()
            chain.conf_path = str(config)

            chain.stop()

            self.assertFalse(config.exists())
            self.assertIsNone(chain.proc)
            self.assertIsNone(chain.conf_path)

    def test_configured_start_replaces_a_running_stale_chain(self):
        class OldProcess:
            stopped = False

            def poll(self):
                return None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout):
                return 0

        class NewProcess:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as parent:
            old_config = Path(parent) / "old-chain"
            new_config = Path(parent) / "new-chain"
            old_config.mkdir()
            new_config.mkdir()
            old_process = OldProcess()
            chain = io24_mbc.Chain()
            chain.proc = old_process
            chain.conf_path = str(old_config)
            with mock.patch.object(io24_mbc.os.path, "exists",
                                   return_value=True), \
                    mock.patch.object(io24_mbc.tempfile, "mkdtemp",
                                      return_value=str(new_config)), \
                    mock.patch.object(io24_mbc.shutil, "copy"), \
                    mock.patch.object(io24_mbc, "uc_comp_available",
                                      return_value=None), \
                    mock.patch.object(io24_mbc.subprocess, "Popen",
                                      return_value=NewProcess()) as spawn, \
                    mock.patch.object(chain, "node_id", return_value="42"):
                started = chain.start(
                    target="alsa_output.io24",
                    state=self._multiband_state())

            self.assertTrue(started)
            self.assertTrue(old_process.stopped)
            self.assertFalse(old_config.exists())
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(chain.conf_path, str(new_config))

    def test_repeated_identical_bus_source_start_is_idempotent(self):
        class RunningProcess:
            def poll(self):
                return None

        chain = io24_mbc.BusSourceChain("mixa")
        launches = []

        def launch(configuration, configured=True):
            launches.append((configuration, configured))
            chain.proc = RunningProcess()
            return True

        with mock.patch.object(chain, "_launch", side_effect=launch):
            self.assertTrue(chain.start("capture-node"))
            self.assertTrue(chain.start("capture-node"))

        self.assertEqual(len(launches), 1)

    @staticmethod
    def _fake_bus_chain(started=True, node_id="42"):
        class FakeChain:
            def __init__(self):
                self.running = False
                self.starts = []
                self.stops = 0

            def start(self, capture_target, positions=None):
                self.starts.append(capture_target)
                self.running = bool(started)
                return bool(started)

            def node_id(self):
                return node_id if self.running else None

            def stop(self):
                self.stops += 1
                self.running = False

        return FakeChain()

    def test_bus_source_manager_isolates_partial_start_failure(self):
        mixa = self._fake_bus_chain(started=False)
        mixb = self._fake_bus_chain(started=True, node_id="84")
        manager = io24_mbc.BusSourceManager(
            chains={"mixa": mixa, "mixb": mixb})

        statuses = manager.reconcile("capture-node", now=10.0,
                                    positions=io24_mbc.CAPTURE_POSITIONS)

        self.assertEqual(statuses, {"mixa": "failed", "mixb": "ready"})
        self.assertEqual(mixa.starts, ["capture-node"])
        self.assertEqual(mixb.starts, ["capture-node"])

    def test_bus_source_manager_times_out_missing_public_node(self):
        mixa = self._fake_bus_chain(started=True, node_id=None)
        mixb = self._fake_bus_chain(started=True, node_id="84")
        manager = io24_mbc.BusSourceManager(
            chains={"mixa": mixa, "mixb": mixb})
        first = manager.reconcile("capture-node", now=10.0,
                                    positions=io24_mbc.CAPTURE_POSITIONS)
        expired = manager.reconcile("capture-node", now=15.1)

        self.assertEqual(first["mixa"], "starting")
        self.assertEqual(expired["mixa"], "failed")
        self.assertEqual(expired["mixb"], "ready")
        self.assertEqual(mixa.stops, 1)

    def test_bus_source_manager_stops_both_sources(self):
        mixa = self._fake_bus_chain()
        mixb = self._fake_bus_chain()
        manager = io24_mbc.BusSourceManager(
            chains={"mixa": mixa, "mixb": mixb})

        manager.stop()

        self.assertEqual(mixa.stops, 1)
        self.assertEqual(mixb.stops, 1)


class ChannelMuteSyncTests(unittest.TestCase):
    """Owner's manual 7.1 item 5, 'Pari' wire 8 -> firmware internal 5."""

    def test_public_setter_emits_exact_pari_8(self):
        dev = ProtocolDevice()

        result = dev.set_mute_mode(1)

        self.assertEqual(len(dev.payloads), 1)
        payload = dev.payloads[0]
        self.assertEqual(len(payload), 32)
        self.assertEqual(struct.unpack_from("<III", payload),
                         (io24.SETP, io24.APPL, 0))
        self.assertEqual(struct.unpack_from("<IIIII", payload, 12),
                         (io24.PARI, 0x14, 0, 8, 1))
        self.assertEqual(io24.Io24.KNOWN_PARI[8], "muteMode")
        self.assertEqual(result["value_mapping"],
                         "INFERRED_FROM_VENDOR_NAMING")
        self.assertEqual(result["readback"], "UNAVAILABLE")

    def test_wire_8_is_no_longer_an_unmapped_id_needing_unsafe(self):
        dev = ProtocolDevice()

        dev.set_param(8, 0, as_int=True)

        self.assertEqual(struct.unpack_from("<i", dev.payloads[0], 28)[0], 0)

    def test_booleans_are_accepted_and_other_values_write_nothing(self):
        for mode, expected in ((True, 1), (False, 0), (0, 0), (1, 1)):
            with self.subTest(mode=mode):
                dev = ProtocolDevice()
                dev.set_mute_mode(mode)
                self.assertEqual(
                    struct.unpack_from("<i", dev.payloads[0], 28)[0], expected)
        for mode in (2, -1):
            with self.subTest(mode=mode):
                dev = ProtocolDevice()
                with self.assertRaises(ValueError):
                    dev.set_mute_mode(mode)
                self.assertEqual(dev.payloads, [])
        for mode in (0.0, "1", None):
            with self.subTest(mode=mode):
                dev = ProtocolDevice()
                with self.assertRaises(TypeError):
                    dev.set_mute_mode(mode)
                self.assertEqual(dev.payloads, [])



if __name__ == "__main__":
    unittest.main()
