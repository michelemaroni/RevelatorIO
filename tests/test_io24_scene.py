"""Hardware-free contracts for UC scene import, export, and recovery."""
import json
import tempfile
import unittest
from pathlib import Path

import io24_fx
import io24_scene


def _delay(mix=0.5):
    return io24_fx.voicefx_preset_state(
        "delay", on=True, time_s=0.125, feedback=0.25, mix=mix)


def _standard_eq():
    eq = {
        "__classid": io24_scene.EQ_PARAMETRIC,
        "eqallon": 1, "eqbandop1": 1, "eqbandop4": 1,
    }
    for band, frequency in enumerate((130.0, 320.0, 1400.0, 5000.0), 1):
        eq["eqbandon%d" % band] = 1
        eq["eqfreq%d" % band] = frequency
        eq["eqgain%d" % band] = 0.0
        eq["eqq%d" % band] = 0.6
    return eq


def _scene():
    return {
        "global": {
            "phonesSrc": 2, "aux1_mirror_main": 1,
            "aux2_mirror_main": 0, "outputDelay": 0.02,
            "outputDelayBus": 1, "presetButtonMode": 1,
            "auxMuteMode": 1,
        },
        "line": {
            "ch1": {
                "link": 0, "preampgain": 11.0, "48v": 0, "mute": 0,
                "processingChannel": 0, "volume": -3.0, "lr": 1,
                "aux1": -12.0, "assign_aux1": 1, "aux2": -18.0,
                "assign_aux2": 1, "dspAmount": 0.75, "solo": 0,
                "pan": 0.5, "stereopan": 0.0, "FXA": 0.0,
                "dawpostdsp": 1, "opt": {"swapcompeq": 1},
                "filter": {"hpf": 80.0},
                "gate": {"on": 0, "threshold": -40.0, "range": -60.0,
                         "attack": 0.01, "release": 0.3,
                         "keyfilter": 0.0, "expander": 1, "keylisten": 0},
                "comp": {
                    "__classid": "{870D04F7-212E-4F9C-ADBB-39A97216433F}",
                    "on": 1, "threshold": -24.0, "ratio": 3.0,
                    "attack": 0.02, "release": 0.15, "gain": 0.0,
                    "softknee": 0, "automode": 0, "keyfilter": 40.0,
                    "keylisten": 0,
                },
                "eq": _standard_eq(),
                "limit": {"limiteron": 1, "threshold": -1.0},
                "voicefx": _delay(),
            },
        },
        "return": {
            "ch1": {"mute": 1, "solo": 0, "volume": 0.0, "lr": 1,
                    "aux1": -20.0, "assign_aux1": 1,
                    "aux2": -30.0, "assign_aux2": 0},
        },
        "fxreturn": {
            "ch1": {"mute": 0, "solo": 0, "volume": -10.0, "lr": 1,
                    "aux1": -20.0, "assign_aux1": 1,
                    "aux2": -20.0, "assign_aux2": 1},
        },
        "aux": {
            "ch1": {"mute": 0, "volume": -2.0, "mono": 0},
            "ch2": {"mute": 1, "volume": -4.0, "mono": 0},
        },
        "main": {"ch1": {"mute": 0, "volume": -1.0, "mono": 0}},
        "fx": {"ch1": {"reverb": {
            "on": 1, "size": 0.5, "mix": 1.0,
            "hp_freq": 200.0, "predelay": 0.02,
        }}},
        "presets": {"slots": {}, "userpresets": {}},
    }


class ScenePlanTests(unittest.TestCase):
    def test_live_cli_requires_an_explicit_current_rate(self):
        with self.assertRaisesRegex(SystemExit, "--sample-rate is required"):
            io24_scene._cli_sample_rate(None, live_apply=True)

        self.assertEqual(io24_scene._cli_sample_rate(None), 48000.0)
        self.assertEqual(
            io24_scene._cli_sample_rate(88200.0, live_apply=True), 88200.0)

    def test_complete_uc_sections_map_to_linux_host_setters(self):
        calls, skips = io24_scene.plan(_scene(), sample_rate_hz=48000.0)
        names = [call[0] for call in calls]

        for expected in (
                "set_mute_mode", "set_preset_mode", "set_comp_eq_order",
                "set_fx_mix", "set_fx", "set_reverb", "set_source_mute",
                "set_bus_mute", "set_bus_master", "set_mirror_main"):
            self.assertIn(expected, names)
        self.assertEqual(names.count("set_fx"), 1)
        self.assertEqual(names.count("set_eq_band"), 4)
        fx = next(call for call in calls if call[0] == "set_fx")
        self.assertEqual(fx[1], ("delay",))
        self.assertTrue(fx[2]["on"])
        self.assertEqual(fx[2]["fs"], 48000.0)
        reverb = next(call for call in calls if call[0] == "set_reverb")
        self.assertEqual(reverb[2]["fs"], 48000.0)
        self.assertFalse(any(".pan = 0.5" in skip for skip in skips))
        self.assertFalse(any(".FXA = -96" in skip for skip in skips))
        self.assertFalse(any(".dawpostdsp = 1" in skip for skip in skips))
        self.assertFalse(any(".mono = 0" in skip for skip in skips))
        self.assertTrue(any("stereopan" in skip for skip in skips))
        self.assertFalse(any("voicefx" in skip.lower() for skip in skips))

    def test_fixed_uc_mixer_states_are_satisfied_without_false_omissions(self):
        scene = _scene()
        for component in scene["line"].values():
            component.update(pan=0.5, FXA=-96.0, dawpostdsp=1)
        for section in ("aux", "main"):
            for component in scene[section].values():
                component["mono"] = 0

        _calls, skips = io24_scene.plan(scene, sample_rate_hz=48000.0)

        self.assertFalse(any(".pan =" in skip for skip in skips))
        self.assertFalse(any(".FXA =" in skip for skip in skips))
        self.assertFalse(any(".dawpostdsp =" in skip for skip in skips))
        self.assertFalse(any(".mono =" in skip for skip in skips))

    def test_nonfixed_uc_mixer_requests_remain_explicit_omissions(self):
        scene = _scene()
        scene["line"]["ch1"].update(
            pan=0.25, FXA=-12.0, dawpostdsp=0)
        scene["main"]["ch1"]["mono"] = 1

        _calls, skips = io24_scene.plan(scene, sample_rate_hz=48000.0)

        self.assertTrue(any("line.ch1.pan = 0.25" in item for item in skips))
        self.assertTrue(any("line.ch1.FXA = -12.0" in item for item in skips))
        self.assertTrue(any("line.ch1.dawpostdsp = 0" in item
                            for item in skips))
        self.assertTrue(any("main.ch1.mono = 1" in item for item in skips))

    def test_delay_scene_at_96khz_is_rejected_during_planning(self):
        with self.assertRaisesRegex(RuntimeError, "96 kHz"):
            io24_scene.plan(_scene(), sample_rate_hz=96000.0)

    def test_host_plan_retains_96khz_delay_without_a_device_model_5_call(self):
        calls, skips = io24_scene.plan(
            _scene(), sample_rate_hz=96000.0, allow_host_delay=True)

        self.assertNotIn("set_fx", [call[0] for call in calls])
        self.assertFalse(any("voicefx" in skip.lower() for skip in skips))
        self.assertIn("set_reverb", [call[0] for call in calls])

    def test_conflicting_channel_voicefx_fails_before_a_plan_is_returned(self):
        scene = _scene()
        scene["line"]["ch2"] = {"voicefx": _delay(mix=0.75)}

        with self.assertRaisesRegex(ValueError, "one shared VoiceFX"):
            io24_scene.plan(scene)

    def test_invalid_scene_toggle_is_not_coerced_from_text(self):
        scene = _scene()
        scene["global"]["auxMuteMode"] = "0"

        with self.assertRaisesRegex(ValueError, "boolean or 0/1"):
            io24_scene.plan(scene)

    def test_apply_checks_every_setter_before_the_first_call(self):
        invoked = []

        class Device:
            def set_gain(self, *args, **kwargs):
                invoked.append((args, kwargs))

        calls = [
            ("set_gain", (1, 10.0), {}, "gain"),
            ("missing_setter", (), {}, "missing"),
        ]
        with self.assertRaisesRegex(AttributeError, "missing_setter"):
            io24_scene.apply(Device(), calls)
        self.assertEqual(invoked, [])

    def test_apply_stops_after_the_first_runtime_failure(self):
        invoked = []

        class Device:
            def first(self):
                invoked.append("first")
                raise RuntimeError("transport stopped")

            def second(self):
                invoked.append("second")

        done, failed = io24_scene.apply(Device(), [
            ("first", (), {}, "first call"),
            ("second", (), {}, "second call"),
        ])
        self.assertEqual((done, failed), (0, 1))
        self.assertEqual(invoked, ["first"])


class SceneExportTests(unittest.TestCase):
    def test_host_snapshot_exports_vendor_fields_and_round_trips_to_a_plan(self):
        standard_eq = {
            "version": 1,
            "channels": {
                "1": {"on": True, "bands": [
                    {"shape": "lowshelf", "mode": "lowshelf", "on": True,
                     "freq": 130.0, "gain": 2.0, "q": 0.6},
                    {"shape": "peaking", "mode": "peaking", "on": True,
                     "freq": 320.0, "gain": -1.0, "q": 0.7},
                    {"shape": "peaking", "mode": "peaking", "on": False,
                     "freq": 1400.0, "gain": 0.0, "q": 0.8},
                    {"shape": "highshelf", "mode": "highshelf", "on": True,
                     "freq": 5000.0, "gain": 3.0, "q": 0.6},
                ]},
            },
        }
        snapshot = {
            "version": 1,
            "live": {
                "input1Gain": 11.0,
                "input2Gain": 22.0,
                "input1PhantomPower": False,
                "input2PhantomPower": True,
                "mainVolume": 0.7,
            },
            "calls": {
                "mute-mode": {"fn": "set_mute_mode",
                              "kwargs": {"mode": True}},
                "phones": {"fn": "set_phones_source",
                           "kwargs": {"source": 2}},
                "link": {"fn": "set_channel_link",
                         "kwargs": {"on": False}},
                "input-mute": {"fn": "set_mute",
                               "kwargs": {"channel": 1, "on": True}},
                "mix": {"fn": "set_fx_mix",
                        "kwargs": {"channel": 1, "value": 0.75}},
                "hpf": {"fn": "set_highpass_freq",
                        "kwargs": {"channel": 1, "freq_hz": 80.0,
                                   "fs": 96000.0}},
                "order": {"fn": "set_comp_eq_order",
                          "kwargs": {"channel": 1, "eq_first": True}},
                "gate": {"fn": "set_gate", "kwargs": {
                    "channel": 1, "on": True, "threshold_db": -36.0,
                    "range_db": -48.0, "attack_s": 0.01,
                    "release_s": 0.3, "keyfilter_hz": 0.0,
                    "expander": True, "keylisten": False,
                    "instance": None, "fs": 96000.0,
                }},
                "comp": {"fn": "set_compressor", "kwargs": {
                    "channel": 1, "model": 0, "on": True,
                    "threshold_db": -24.0, "ratio": 3.0,
                    "attack_s": 0.02, "release_s": 0.15,
                    "gain_db": 1.0, "softknee": False,
                    "automode": False, "keyfilter_hz": 40.0,
                    "keylisten": False, "instance": None,
                    "fs": 96000.0,
                }},
                "limiter": {"fn": "set_limiter", "kwargs": {
                    "channel": 1, "on": True, "threshold_db": -1.0,
                    "fs": 96000.0,
                }},
                "send-main": {"fn": "set_send_db", "kwargs": {
                    "source": "line/ch1", "bus": "main", "gain_db": -3.0,
                }},
                "send-a": {"fn": "set_send_db", "kwargs": {
                    "source": "line/ch1", "bus": "mixa", "gain_db": -12.0,
                }},
                "assign-a": {"fn": "set_send_assigned", "kwargs": {
                    "source": "line/ch1", "bus": "mixa", "on": True,
                }},
                "return-mute": {"fn": "set_source_mute", "kwargs": {
                    "source": "return/ch1", "on": True,
                }},
                "master-a": {"fn": "set_bus_master", "kwargs": {
                    "bus": "mixa", "gain_db": -2.0,
                }},
                "mute-b": {"fn": "set_bus_mute", "kwargs": {
                    "bus": "mixb", "on": True,
                }},
                "voicefx": {"fn": "set_fx", "kwargs": {
                    "model": "delay", "on": True, "time_s": 0.125,
                    "feedback": 0.25, "mix": 0.5,
                }},
                "reverb": {"fn": "set_reverb", "kwargs": {
                    "on": True, "size": 0.5, "mix": 0.3,
                    "hp_freq": 200.0, "predelay": 0.02,
                    "fs": 96000.0,
                }},
            },
        }

        scene, omissions = io24_scene.export_snapshot(
            snapshot, host_features={"standard_eq": standard_eq},
            solo={"main": [], "mixa": [], "mixb": []})

        self.assertEqual(scene["global"]["auxMuteMode"], 1)
        self.assertEqual(scene["global"]["phonesSrc"], 2)
        self.assertEqual(scene["line"]["ch1"]["preampgain"], 11.0)
        self.assertEqual(scene["line"]["ch2"]["48v"], 1)
        self.assertEqual(scene["line"]["ch1"]["eq"]["eqallon"], 1)
        self.assertEqual(scene["line"]["ch1"]["eq"]["eqbandon3"], 0)
        self.assertEqual(scene["line"]["ch1"]["voicefx"]["on"], 1)
        self.assertEqual(scene["return"]["ch1"]["mute"], 1)
        self.assertEqual(scene["aux"]["ch1"]["volume"], -2.0)
        self.assertEqual(scene["aux"]["ch2"]["mute"], 1)
        self.assertEqual(scene["fx"]["ch1"]["reverb"]["mix"], 0.3)
        self.assertNotIn("presets", scene)
        self.assertTrue(any("mainVolume" in item for item in omissions))

        calls, skips = io24_scene.plan(scene, sample_rate_hz=48000.0)
        names = [call[0] for call in calls]
        self.assertIn("set_fx", names)
        self.assertIn("set_reverb", names)
        self.assertIn("set_source_mute", names)
        self.assertIn("set_bus_mute", names)
        self.assertFalse(any("not in the UC 4.7.2" in skip for skip in skips))

    def test_scene_save_validates_before_atomically_replacing_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.scene"
            path.write_text("original")
            invalid = _scene()
            invalid["global"]["auxMuteMode"] = "on"

            with self.assertRaisesRegex(ValueError, "boolean or 0/1"):
                io24_scene.save(path, invalid)
            self.assertEqual(path.read_text(), "original")

            scene = _scene()
            io24_scene.save(path, scene, sample_rate_hz=96000.0)
            self.assertEqual(json.loads(path.read_text()), scene)

    def test_host_delay_overrides_stale_shadow_and_keeps_its_input_owner(self):
        snapshot = {
            "live": {},
            "calls": {
                "voicefx": {"fn": "set_fx", "kwargs": {
                    "model": "delay", "on": True, "time_s": 0.125,
                    "feedback": 0.5, "mix": 0.1,
                }},
            },
        }
        feature = {
            "version": 1, "target": 2,
            "state": {"on": False, "time_s": 0.173,
                      "feedback": 0.25, "mix": 0.8},
        }

        scene, omissions = io24_scene.export_snapshot(
            snapshot, host_features={"voicefx_delay": feature})

        self.assertNotIn("voicefx", scene["line"].get("ch1", {}))
        model, state = io24_fx.voicefx_preset_call(
            scene["line"]["ch2"]["voicefx"])
        self.assertEqual(model, "delay")
        self.assertEqual(state, feature["state"])
        self.assertFalse(any("voicefx_delay" in item for item in omissions))
        calls, _skips = io24_scene.plan(
            scene, sample_rate_hz=96000.0, allow_host_delay=True)
        self.assertNotIn("set_fx", [call[0] for call in calls])

    def test_readable_mute_link_processing_and_bypass_override_stale_shadow(self):
        snapshot = {
            "live": {
                "flags": (1 << 3) | (1 << 6) | (1 << 12),
                "input1ProcessingChannel": 2,
                "input2ProcessingChannel": 1,
            },
            "calls": {
                "mute": {"fn": "set_mute",
                         "kwargs": {"channel": 1, "on": False}},
                "link": {"fn": "set_channel_link",
                         "kwargs": {"on": False}},
                "mix": {"fn": "set_fx_mix",
                        "kwargs": {"channel": 2, "value": 0.8}},
            },
        }

        scene, omissions = io24_scene.export_snapshot(snapshot)

        self.assertEqual(scene["line"]["ch1"]["mute"], 1)
        self.assertEqual(scene["line"]["ch1"]["link"], 1)
        self.assertEqual(scene["line"]["ch1"]["processingChannel"], 1)
        self.assertEqual(scene["line"]["ch2"]["processingChannel"], 0)
        self.assertEqual(scene["line"]["ch2"]["dspAmount"], 0.0)
        self.assertTrue(any("line.ch1.dspAmount" in item
                            for item in omissions))


class TransactionalApplyTests(unittest.TestCase):
    class Device:
        def __init__(self, with_checkpoint=True):
            self._shadow = {}
            if with_checkpoint:
                self._shadow = {
                    "set_fx_mix#channel=1": {
                        "fn": "set_fx_mix",
                        "kwargs": {"channel": 1, "value": 0.25},
                    },
                    "set_reverb": {
                        "fn": "set_reverb",
                        "kwargs": {"on": True, "size": 0.4, "mix": 0.3,
                                   "hp_freq": 200.0, "predelay": 0.02,
                                   "fs": 48000.0},
                    },
                }
            self._solo = {}
            self.events = []

        def read_params(self):
            return {}

        def set_fx_mix(self, channel, value):
            self.events.append(("mix", channel, value))

        def set_reverb(self, on=True, size=0.5, mix=0.3, hp_freq=200.0,
                       predelay=0.02, fs=48000.0):
            self.events.append(("reverb", mix))
            if mix == 0.9:
                raise RuntimeError("transport stopped")

    def test_failure_replays_the_exact_host_checkpoint(self):
        device = self.Device()
        report = io24_scene.apply_transactional(device, [
            ("set_fx_mix", (1, 0.9), {}, "new processing mix"),
            ("set_reverb", (), {
                "on": True, "size": 0.8, "mix": 0.9,
                "hp_freq": 100.0, "predelay": 0.04, "fs": 48000.0,
            }, "new reverb"),
        ])

        self.assertEqual(report["applied"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertTrue(report["rollback"]["attempted"])
        self.assertTrue(report["rollback"]["complete"])
        self.assertEqual(report["rollback"]["unresolved"], [])
        self.assertEqual(device.events[-2:], [
            ("mix", 1, 0.25), ("reverb", 0.3),
        ])

    def test_failure_reports_a_control_whose_prior_state_was_unknown(self):
        device = self.Device(with_checkpoint=False)
        report = io24_scene.apply_transactional(device, [
            ("set_fx_mix", (1, 0.9), {}, "new processing mix"),
            ("set_reverb", (), {
                "on": True, "size": 0.8, "mix": 0.9,
                "hp_freq": 100.0, "predelay": 0.04, "fs": 48000.0,
            }, "new reverb"),
        ])

        self.assertFalse(report["rollback"]["complete"])
        self.assertIn("new processing mix", report["rollback"]["unresolved"])

    def test_readable_bypass_wins_over_a_stale_positive_shadow_on_rollback(self):
        device = self.Device()
        device.read_params = lambda: {"flags": 1 << 5}

        report = io24_scene.apply_transactional(device, [
            ("set_fx_mix", (1, 0.9), {}, "new processing mix"),
            ("set_reverb", (), {
                "on": True, "size": 0.8, "mix": 0.9,
                "hp_freq": 100.0, "predelay": 0.04, "fs": 48000.0,
            }, "new reverb"),
        ])

        self.assertTrue(report["rollback"]["complete"])
        self.assertEqual(device.events[-1], ("mix", 1, 0.0))

    def test_readable_enabled_state_rejects_a_stale_zero_as_exact_rollback(self):
        device = self.Device()
        device._shadow["set_fx_mix#channel=1"]["kwargs"]["value"] = 0.0
        device.read_params = lambda: {"flags": 0}

        report = io24_scene.apply_transactional(device, [
            ("set_fx_mix", (1, 0.9), {}, "new processing mix"),
            ("set_reverb", (), {
                "on": True, "size": 0.8, "mix": 0.9,
                "hp_freq": 100.0, "predelay": 0.04, "fs": 48000.0,
            }, "new reverb"),
        ])

        self.assertFalse(report["rollback"]["complete"])
        self.assertIn("new processing mix", report["rollback"]["unresolved"])
        self.assertNotIn(("mix", 1, 0.0), device.events[2:])


if __name__ == "__main__":
    unittest.main()
