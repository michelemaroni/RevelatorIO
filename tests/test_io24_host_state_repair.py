import ast
import json
import tempfile
import unittest
from pathlib import Path

import io24


ROOT = Path(__file__).parents[1]


class _MixerOnlyIo24(io24.Io24):
    """Exercise Host mixer bookkeeping without opening the USB interface."""

    def __init__(self, shadow=None):
        self._shadow = dict(shadow or {})
        self._shadow_dirty = False
        self._shadow_flushed = 0.0
        self._shadow_persist = False
        self._send_state = None
        self.writes = []

    def _write_mix(self, source, bus, gain_db):
        self.writes.append((source, bus, gain_db))
        return self.writes[-1]


class _OrderOnlyIo24(_MixerOnlyIo24):
    def __init__(self, shadow):
        super().__init__(shadow)
        self.calls = []

    def set_preset_mode(self, mode):
        self.calls.append(("mode", mode))

    def set_preset_slot(self, channel, slot):
        self.calls.append(("slot", channel, slot))

    def set_preset_enabled(self, channel, on):
        self.calls.append(("enabled", channel, on))

    def set_fx_mix(self, channel, value):
        self.calls.append(("processing_mix", channel, value))

    def set_processing_channel(self, channel, source_input):
        self.calls.append(("processing", channel, source_input))

    def set_reverb(self, **kwargs):
        self.calls.append(("reverb", kwargs.get("on")))

    def set_fx(self, model, **kwargs):
        self.calls.append(("voicefx", model, kwargs.get("on")))

    def read_params(self):
        return {
            "input1Gain": 30.0,
            "input2Gain": 24.0,
            "hpVolume": 0.5,
            "mainVolume": 0.6,
            "monitorMix": 0.0,
            "input1PhantomPower": False,
            "input2PhantomPower": False,
        }


class HostStateRepairTests(unittest.TestCase):
    def test_assigning_an_uninitialised_send_materialises_unity(self):
        dev = _MixerOnlyIo24()

        dev.set_send_assigned("line/ch2", "mixb", True)

        self.assertEqual(dev.send_db("line/ch2", "mixb"), 0.0)
        self.assertEqual(dev.writes, [("line/ch2", "mixb", 0.0)])
        self.assertIn("set_send_db#source=line/ch2,bus=mixb", dev._shadow)
        self.assertIn("set_send_assigned#source=line/ch2,bus=mixb", dev._shadow)

    def test_balance_materialises_both_stereo_pair_levels(self):
        dev = _MixerOnlyIo24()

        dev.set_pair_pan("line/ch1", "main", 0.25)

        self.assertEqual(dev.send_db("line/ch1", "main"), 0.0)
        self.assertEqual(dev.send_db("line/ch2", "main"), 0.0)
        self.assertIn("set_pan#source=line/ch1,bus=main", dev._shadow)
        self.assertIn("set_pan#source=line/ch2,bus=main", dev._shadow)
        self.assertEqual(len(dev.writes), 2)

    def test_explicitly_off_send_is_distinct_from_an_unknown_send(self):
        dev = _MixerOnlyIo24()

        self.assertFalse(dev.has_send_level("fxreturn/ch1", "main"))
        dev.set_send_db("fxreturn/ch1", "main", None)

        self.assertTrue(dev.has_send_level("fxreturn/ch1", "main"))
        self.assertIsNone(dev.send_db("fxreturn/ch1", "main"))

    def test_source_mute_preserves_and_restores_every_bus_level(self):
        dev = _MixerOnlyIo24()
        levels = {"main": -3.0, "mixa": -12.0, "mixb": -24.0}
        for bus, level in levels.items():
            dev.set_send_db("return/ch1", bus, level)
        dev.writes.clear()

        dev.set_source_mute("return/ch1", True)

        self.assertTrue(dev.source_muted("return/ch1"))
        self.assertEqual(dev.writes, [
            ("return/ch1", "main", None),
            ("return/ch1", "mixa", None),
            ("return/ch1", "mixb", None),
        ])
        self.assertEqual(
            {bus: dev.send_db("return/ch1", bus) for bus in levels}, levels)

        dev.writes.clear()
        dev.set_source_mute("return/ch1", False)
        self.assertFalse(dev.source_muted("return/ch1"))
        self.assertEqual(dev.writes, [
            ("return/ch1", "main", -3.0),
            ("return/ch1", "mixa", -12.0),
            ("return/ch1", "mixb", -24.0),
        ])

    def test_bus_mute_preserves_levels_and_never_materialises_line_three(self):
        dev = _MixerOnlyIo24()

        dev.set_bus_mute("mixa", True)

        self.assertTrue(dev.bus_muted("mixa"))
        self.assertNotIn(("line/ch3", "mixa"), dev._sends()["level"])
        self.assertTrue(dev.writes)
        self.assertTrue(all(bus == "mixa" and value is None
                            for _source, bus, value in dev.writes))
        self.assertIn("set_bus_mute#bus=mixa", dev._shadow)

    def test_source_and_bus_mutes_rebuild_from_shadow(self):
        dev = _MixerOnlyIo24({
            "set_source_mute#source=return/ch1": {
                "fn": "set_source_mute",
                "kwargs": {"source": "return/ch1", "on": True},
            },
            "set_bus_mute#bus=mixb": {
                "fn": "set_bus_mute",
                "kwargs": {"bus": "mixb", "on": True},
            },
        })

        self.assertTrue(dev.source_muted("return/ch1"))
        self.assertTrue(dev.bus_muted("mixb"))

    def test_mute_mode_is_saved_in_host_shadow(self):
        dev = _MixerOnlyIo24()
        writes = []
        dev.set_param = lambda *args, **kwargs: writes.append((args, kwargs))

        report = dev.set_mute_mode(True)

        self.assertEqual(report["readback"], "UNAVAILABLE")
        self.assertEqual(writes, [((8, 1), {"index": 0, "as_int": True})])
        self.assertEqual(dev._shadow["set_mute_mode"]["kwargs"], {"mode": True})

    def test_shadow_normalisation_discards_old_one_leg_pan(self):
        shadow = {
            "set_pan#source=line/ch2,bus=main": {
                "fn": "set_pan",
                "kwargs": {"source": "line/ch2", "bus": "main", "pan": 0.0},
            },
            "set_send_db#source=line/ch2,bus=main": {
                "fn": "set_send_db",
                "kwargs": {"source": "line/ch2", "bus": "main", "gain_db": 0.0},
            },
        }

        normalised = io24._normalise_shadow(shadow)

        self.assertNotIn("set_pan#source=line/ch2,bus=main", normalised)
        self.assertIn("set_send_db#source=line/ch2,bus=main", normalised)

    def test_shadow_normalisation_retains_a_coherent_pair_balance(self):
        shadow = {
            "set_channel_link": {
                "fn": "set_channel_link", "kwargs": {"on": True},
            }
        }
        for source in ("line/ch1", "line/ch2"):
            shadow["set_pan#source=%s,bus=main" % source] = {
                "fn": "set_pan",
                "kwargs": {"source": source, "bus": "main", "pan": 0.25},
            }

        normalised = io24._normalise_shadow(shadow)

        self.assertEqual(set(normalised), set(shadow))

    def test_shadow_normalisation_drops_pair_balance_when_unlinked(self):
        shadow = {
            "set_channel_link": {
                "fn": "set_channel_link", "kwargs": {"on": False},
            }
        }
        for source in ("line/ch1", "line/ch2"):
            shadow["set_pan#source=%s,bus=main" % source] = {
                "fn": "set_pan",
                "kwargs": {"source": source, "bus": "main", "pan": 0.5},
            }

        normalised = io24._normalise_shadow(shadow)

        self.assertEqual(list(normalised), ["set_channel_link"])

    def test_deprecated_preset_enable_key_is_canonicalised(self):
        shadow = {
            "set_preset_button_mode#channel=2": {
                "fn": "set_preset_button_mode",
                "kwargs": {"channel": 2, "two_slots": True},
            }
        }

        normalised = io24._normalise_shadow(shadow)

        self.assertEqual(list(normalised), ["set_fx_mix#channel=2"])
        self.assertEqual(normalised["set_fx_mix#channel=2"], {
            "fn": "set_fx_mix", "kwargs": {"channel": 2, "value": 1.0},
        })

    def test_runtime_enable_and_scalar_mix_share_one_shadow_entry(self):
        dev = _MixerOnlyIo24()
        writes = []
        dev.set_param = lambda *args, **kwargs: writes.append((args, kwargs))

        dev.set_fx_mix(1, 0.0)
        dev.set_preset_enabled(1, True)
        self.assertEqual(dev._shadow, {
            "set_fx_mix#channel=1": {
                "fn": "set_fx_mix",
                "kwargs": {"channel": 1, "value": 1.0},
            }
        })
        self.assertEqual(writes[-1], ((4, 1.0), {"index": 0}))

        dev.set_fx_mix(1, 0.0)
        self.assertEqual(dev._shadow, {
            "set_fx_mix#channel=1": {
                "fn": "set_fx_mix",
                "kwargs": {"channel": 1, "value": 0.0},
            }
        })

    def test_legacy_conflict_preserves_explicit_bypass_intent(self):
        cases = (
            (True, 0.0, 1.0,
             "LEGACY_ENABLE_TRUE_OVERRULED_ZERO_MIX_INFERRED_FULL"),
            (True, 0.35, 0.35, None),
            (False, 0.35, 0.0, "LEGACY_ENABLE_FALSE_FORCED_BYPASS"),
        )
        for enabled, old_mix, expected, provenance in cases:
            with self.subTest(enabled=enabled, old_mix=old_mix):
                normalised = io24._normalise_shadow({
                    "set_fx_mix#channel=1": {
                        "fn": "set_fx_mix",
                        "kwargs": {"channel": 1, "value": old_mix},
                    },
                    "set_preset_enabled#channel=1": {
                        "fn": "set_preset_enabled",
                        "kwargs": {"channel": 1, "on": enabled},
                    },
                })
                self.assertEqual(list(normalised), ["set_fx_mix#channel=1"])
                call = normalised["set_fx_mix#channel=1"]
                self.assertEqual(call["fn"], "set_fx_mix")
                self.assertEqual(call["kwargs"], {
                    "channel": 1, "value": expected})
                self.assertEqual(call.get("migration"), provenance)

    def test_reapply_cached_state_repairs_assigned_without_level(self):
        dev = _MixerOnlyIo24({
            "set_send_assigned#source=line/ch2,bus=mixb": {
                "fn": "set_send_assigned",
                "kwargs": {"source": "line/ch2", "bus": "mixb", "on": True},
            }
        })

        report = dev.reapply_shadow()

        self.assertEqual(report["failed"], [])
        self.assertEqual(report["applied"], 1)
        self.assertEqual(dev.send_db("line/ch2", "mixb"), 0.0)
        self.assertEqual(dev.writes, [("line/ch2", "mixb", 0.0)])

    def test_loading_an_old_host_file_does_not_restore_one_leg_pan(self):
        calls = {
            "set_send_db#source=line/ch2,bus=main": {
                "fn": "set_send_db",
                "kwargs": {"source": "line/ch2", "bus": "main", "gain_db": 0.0},
            },
            "set_pan#source=line/ch2,bus=main": {
                "fn": "set_pan",
                "kwargs": {"source": "line/ch2", "bus": "main", "pan": 0.0},
            },
        }
        dev = _MixerOnlyIo24()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-host.json"
            path.write_text(json.dumps({"version": 1, "live": {}, "calls": calls}))

            _live, applied = dev.load_preset(
                path, sample_rate_hz=48000.0)

        self.assertEqual(applied, 1)
        self.assertIsNone(dev.pan("line/ch2", "main"))
        self.assertEqual(dev.writes, [("line/ch2", "main", 0.0)])

    @staticmethod
    def _mixed_preset_domain_calls():
        return {
            "set_reverb": {
                "fn": "set_reverb", "kwargs": {"on": True},
            },
            "set_preset_enabled#channel=2": {
                "fn": "set_preset_enabled",
                "kwargs": {"channel": 2, "on": True},
            },
            "set_processing_channel#channel=1": {
                "fn": "set_processing_channel",
                "kwargs": {"channel": 1, "source_input": 2},
            },
            "set_preset_slot#channel=2": {
                "fn": "set_preset_slot",
                "kwargs": {"channel": 2, "slot": 3},
            },
            "set_preset_mode": {
                "fn": "set_preset_mode", "kwargs": {"mode": 1},
            },
        }

    def test_host_snapshot_quarantines_device_preset_state_by_default(self):
        dev = _OrderOnlyIo24(self._mixed_preset_domain_calls())

        default = dev.snapshot()
        included = dev.snapshot(include_device_preset_state=True)

        self.assertEqual(set(default["calls"]), {
            "set_reverb", "set_fx_mix#channel=2",
            "set_processing_channel#channel=1"})
        self.assertFalse(default["device_preset_state_included"])
        self.assertEqual(default["quarantined_device_preset_calls"], 2)
        self.assertEqual(set(included["calls"]), {
            "set_reverb", "set_fx_mix#channel=2",
            "set_processing_channel#channel=1",
            "set_preset_slot#channel=2", "set_preset_mode"})
        self.assertTrue(included["device_preset_state_included"])
        self.assertEqual(included["quarantined_device_preset_calls"], 0)
        self.assertEqual(dev.replayable_shadow_count(), 3)
        self.assertEqual(
            dev.replayable_shadow_count(include_device_preset_state=True), 5)

    def test_host_file_load_never_moves_slots_without_explicit_opt_in(self):
        calls = self._mixed_preset_domain_calls()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed-host-snapshot.json"
            path.write_text(json.dumps({
                "version": 1, "live": {}, "calls": calls,
                "device_preset_state_included": True,
            }))
            dev = _OrderOnlyIo24({})

            _live, applied = dev.load_preset(path)

            self.assertEqual(applied, 3)
            self.assertEqual(
                dev._last_preset_load_report[
                    "quarantined_device_preset_calls"], 2)
            self.assertEqual(dev.calls, [
                ("processing", 1, 2),
                ("reverb", True),
                ("processing_mix", 2, 1.0)])

            dev.calls.clear()
            _live, applied = dev.load_preset(
                path, include_device_preset_state=True)

        self.assertEqual(applied, 5)
        self.assertEqual(
            dev._last_preset_load_report[
                "quarantined_device_preset_calls"], 0)
        self.assertEqual(dev.calls, [
            ("mode", 1),
            ("slot", 2, 3),
            ("processing", 1, 2),
            ("reverb", True),
            ("processing_mix", 2, 1.0),
        ])

    def test_host_file_load_assigns_voicefx_before_model_state(self):
        calls = {
            "set_fx": {
                "fn": "set_fx",
                "kwargs": {"model": "delay", "on": True},
            },
            "set_processing_channel#channel=1": {
                "fn": "set_processing_channel",
                "kwargs": {"channel": 1, "source_input": 2},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel-2-voicefx.json"
            path.write_text(json.dumps({
                "version": 1, "live": {}, "calls": calls,
            }))
            dev = _OrderOnlyIo24({})

            _live, applied = dev.load_preset(
                path, sample_rate_hz=48000.0)

        self.assertEqual(applied, 2)
        self.assertEqual(dev.calls, [
            ("processing", 1, 2),
            ("voicefx", "delay", True),
        ])

    def test_reapply_does_not_touch_stale_channel1_selector_by_default(self):
        calls = {
            "set_reverb": {
                "fn": "set_reverb", "kwargs": {"on": True},
            },
            "set_preset_slot#channel=1": {
                "fn": "set_preset_slot",
                "kwargs": {"channel": 1, "slot": 3},
            },
            "set_preset_enabled#channel=1": {
                "fn": "set_preset_enabled",
                "kwargs": {"channel": 1, "on": False},
            },
        }
        dev = _OrderOnlyIo24(calls)

        report = dev.reapply_shadow()

        self.assertEqual(dev.calls, [
            ("reverb", True), ("processing_mix", 1, 0.0)])
        self.assertEqual(report["applied"], 2)
        self.assertEqual(report["quarantined_device_preset_calls"], 1)

    def test_explicit_reapply_keeps_selector_first_phase_order(self):
        calls = self._mixed_preset_domain_calls()
        dev = _OrderOnlyIo24(calls)

        report = dev.reapply_shadow(include_device_preset_state=True)

        self.assertEqual(report["failed"], [])
        self.assertEqual(dev.calls, [
            ("mode", 1),
            ("slot", 2, 3),
            ("processing", 1, 2),
            ("reverb", True),
            ("processing_mix", 2, 1.0),
        ])

    def test_gtk_labels_snapshots_and_hides_experiment_controls(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        fx_page = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_fx_page"
        )
        rendered = ast.get_source_segment(source, fx_page)

        self.assertIn('menu.append("Save full Host setup…"', source)
        self.assertIn('menu.append("Load full Host setup…"', source)
        self.assertNotIn("Arm 173 ms Delay on Channel 2", rendered)
        self.assertNotIn("Recall stock Slap Echo on Channel 2", rendered)
        self.assertNotIn("Custom-firmware lane extension", rendered)
        self.assertNotIn("Dual-slot private-reverb experiment", rendered)

    def test_effects_page_keeps_native_reverb_and_removes_host_spring(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        fx_page = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_fx_page"
        )
        rendered = ast.get_source_segment(source, fx_page)
        methods = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }

        self.assertIn('title="Shared reverb"', rendered)
        self.assertNotIn('title="Spring reverb"', rendered)
        self.assertNotIn("io24_spring", source)
        self.assertFalse(any(name.startswith("_spring_") for name in methods))
        # The return carries the native reverb and Voice FX engine.
        self.assertIn('title="Shared effects returns"', rendered)
        # DSP amount and Bypass moved to the Device page
        self.assertNotIn("processing_mix_controls", rendered)
        self.assertNotIn('"0% bypasses EQ, compression, limiting and effects; "',
                         rendered)
        self.assertNotIn('"Channel %d send"', rendered)
        self.assertIn('title="Voice FX"', rendered)
        self.assertIn('title=parameter["name"]', rendered)
        self.assertIn('VoiceFxRack(self)', rendered)
        self.assertNotIn('title="FX active"', rendered)
        self.assertNotIn('title="Target input"', rendered)
        self.assertNotIn('title="Device activation"', rendered)
        self.assertNotIn('title="Voice FX insert"', rendered)
        self.assertNotIn('standalone activation requires', rendered)
        self.assertNotIn(
            "Doubler private reverb has two structural input lanes", rendered)
        self.assertNotIn("fx_save_both", source)
        self.assertNotIn("_preset_enable_changed", methods)
        self.assertNotIn("_arm_channel2_delay", methods)
        self.assertNotIn("_load_channel2_factory_slap_echo", methods)

    def test_retired_host_spring_state_is_discarded_during_migration(self):
        spring = {
            "version": 2,
            "enabled": True,
            "input1_db": -6.0,
            "input2_db": -6.0,
            "dwell": 0.64,
            "tone": 0.55,
            "drip": 0.42,
            "width": 0.82,
            "predelay_s": 0.008,
            "output_db": -12.0,
            "routing": None,
        }

        normalized, migrations = io24._normalise_host_features(
            {"spring_reverb": spring})

        self.assertEqual(normalized, {})
        self.assertEqual(migrations, [
            "retired Host spring reverb was ignored",
        ])

    def test_effects_page_uses_concise_uc_style_controls(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        fx_page = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_fx_page"
        )
        rendered = ast.get_source_segment(source, fx_page)

        self.assertNotIn('title="FX activation unresolved"', rendered)
        self.assertNotIn("currently meter-negative", rendered)
        self.assertNotIn('title="Character"', rendered)
        self.assertIn('title="Size movement"', rendered)
        self.assertIn('"Movement depth"', rendered)
        self.assertIn('self._srow("Reverb return blend"', rendered)
        self.assertNotIn('title="Host approximation"', rendered)
        self.assertNotIn("not separate device algorithms", rendered)
        self.assertNotIn("device readback unavailable", rendered)
        self.assertNotIn("block-201 readback unavailable", rendered)

    def test_processing_mix_uses_the_slider_as_the_only_establish_action(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        group = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_processing_group"
        )
        rendered = ast.get_source_segment(source, group)

        self.assertNotIn("Set 100%", rendered)
        self.assertNotIn("processing_mix_establish", source)
        self.assertNotIn("sc.set_sensitive(False)", rendered)
        self.assertIn('"Move to set"', rendered)

    def test_presets_page_has_current_sound_save_workflows(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        preset_page = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_presets_page"
        )
        rendered = ast.get_source_segment(source, preset_page)

        # 2026-09-11: saving goes to the user's presets on this computer;
        # putting one on the unit is an action on the preset itself.
        self.assertIn("Preset name", rendered)
        self.assertIn("self._save_user_preset_clicked", rendered)
        self.assertNotIn("Store in device slot…", rendered)
        self.assertNotIn("Complete-body base", rendered)

    def test_normal_ui_copy_omits_protocol_and_manual_notation(self):
        source = (ROOT / "io24gtk.py").read_text()

        for phrase in (
                "UC 4.7.2 Standard, Passive, and Vintage",
                "Off selects the XML's parametric mode",
                "Firmware write-only selector",
                "Read-only in UC; block-201 readback unavailable",
                "This uses Universal Control's MemP/PrsM Store route",
                "WRITE_SENT_UNVERIFIED",
                "Legacy front-panel block receipt",
                "Host component names",
                "richness, depth, a hint of reverb",
                "artificially lowers the voice",
                "talking synthesizer",
                "voice x oscillator",
                "custom filter bank",
                'subtitle="index %d"'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, source)

    def test_gtk_separates_fixed_preamp_hpf_from_fat_channel_cutoff(self):
        source = (ROOT / "io24gtk.py").read_text()

        self.assertIn('("HPF", "hpf")', source)
        self.assertIn('HPF_BASIC_HZ = (24.0, 40.0, 80.0, 160.0)', source)
        self.assertIn('"Off", "40 Hz", "80 Hz", "160 Hz", "Advanced"',
                      source)
        self.assertIn('title="HPF"', source)
        self.assertIn('"Exact cutoff"', source)
        self.assertNotIn(
            '(("48V", "phantom"), ("Mute", "mute"), ("Low cut", "hpf"))',
            source)

    def test_gtk_factory_load_uses_explicit_target_not_channel_one_literal(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_load_factory"
        )
        rendered = ast.get_source_segment(source, method)

        self.assertIn("self._factory_target_channel()", rendered)
        self.assertNotIn("else (1,)", rendered)

    def test_gtk_omits_reapply_ui_and_exposes_capture_bus_semantics(self):
        source = (ROOT / "io24gtk.py").read_text()

        self.assertIn('"shadow_pending"', source)
        self.assertNotIn("reconcile_banner", source)
        self.assertNotIn("def _reapply_cached", source)
        self.assertNotIn("def _forget_cached", source)
        self.assertIn('not dev.has_send_level(\n                    "fxreturn/ch1", "main")',
                      source)
        self.assertIn("assigned = dev.send_assigned(\n"
                      "                    src, display_bus) if known else False",
                      source)
        self.assertIn('off.set_tooltip_text("Add to mix at 0 dB")', source)
        self.assertIn('("mixa", "Mix A", "USB capture 3–4")', source)
        self.assertIn('("mixb", "Mix B", "USB capture 5–6")', source)

if __name__ == "__main__":
    unittest.main()
