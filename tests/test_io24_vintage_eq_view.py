#!/usr/bin/env python3
"""Hardware-free contracts for editable Passive/Vintage EQ bodies.

The Host keeps each model in its own UC semantic form, exposes the exact
switches and amounts, draws the vendor-designed response, and carries that
state through save/reconnect without coercing it into Standard bands.

Nothing here opens USB.
"""

import unittest
from types import SimpleNamespace

import io24
import io24_alt_eq
import io24_presets
import io24gtk


VINTAGE = {
    "__classid": "{E1C5E024-C5CD-473C-B08A-6EC177812E01}",
    "eqallon": 1, "lowgain": 1.76, "lowfreq": 0, "lowmidgain": -3.84,
    "lowmidfreq": 0, "himidgain": 1.6, "himidfreq": 1, "higain": 0.96,
}

PASSIVE = {
    "__classid": "{C0730CBB-5135-4558-9222-C40BDBA036ED}",
    "eqallon": 1, "bboost": 3.899999, "batten": 0.0, "bfreq": 3,
    "mboost": 1.8, "bbwidth": 4.55, "mfreq": 6, "hatten": 0.0,
    "hsfreq": 2,
}

FLAT = [{"shape": "off", "freq": f, "gain": 0.0, "q": 0.7}
        for f in (120.0, 600.0, 2500.0, 8000.0)]

STANDARD = {
    "__classid": "{A0A8A068-14F0-4B04-BB6F-AF8329D0E8EE}",
    "eqallon": 1,
}

COMPLETE_BASE = {
    "preset_name": "public-test-base",
    "opt": {},
    "filter": {},
    "gate": {},
    "limit": {},
    "eq": STANDARD,
    "comp": {},
}

EXACT_ALT_EQ_AVAILABLE = io24_alt_eq.designer_status()[0]


def _slot_kwargs(**extra):
    kwargs = dict(
        bands=[dict(band) for band in FLAT], hpf_hz=24.0, eq_first=False,
        gate={"on": True, "threshold_db": -48.7, "range_db": -60.0,
              "attack_s": .005, "release_s": .3, "keyfilter_hz": 325.0,
              "keylisten": False, "expander": True},
        compressor_model=2,
        compressor={"on": True, "input_db": -30.0, "output_db": -3.0,
                    "attack_s": .0001, "release_s": .25, "ratio_index": 3,
                    "keyfilter_hz": 0.0, "keylisten": False},
        limiter={"on": True, "threshold_db": -.8},
        voicefx_model="delay",
        voicefx={"on": False, "time_s": .0351, "feedback": .25, "mix": .4})
    kwargs.update(extra)
    return kwargs


class AlternateEqViewTests(unittest.TestCase):
    def test_a_vintage_body_is_listed_as_stored(self):
        view = io24_presets.alternate_eq_view({"eq": dict(VINTAGE)})
        self.assertEqual(view["model"], "vintage")
        self.assertTrue(view["on"])
        self.assertEqual(view["rows"], [
            ("Low shelf", "position 1 of 4 (35 Hz)", "+1.8 dB"),
            ("Low mid", "position 1 of 3 (360 Hz)", "-3.8 dB"),
            ("High mid", "position 2 of 3 (4800 Hz)", "+1.6 dB"),
            ("High shelf", "fixed frequency", "+1.0 dB"),
        ])
        self.assertEqual(view["eq"], VINTAGE)
        self.assertIsNot(view["eq"], VINTAGE)

    def test_a_passive_body_uses_the_recovered_switch_labels(self):
        view = io24_presets.alternate_eq_view({"eq": dict(PASSIVE)})
        self.assertEqual(view["model"], "passive")
        self.assertEqual(view["rows"], [
            ("Low boost", "position 4 of 4 (100 Hz)", "3.9 / 10"),
            ("Low attenuation", "position 4 of 4 (100 Hz)", "0.0 / 10"),
            ("High boost", "position 7 of 7 (16000 Hz)",
             "1.8 / 10; bandwidth 4.5 / 10"),
            ("High attenuation", "position 3 of 3 (20000 Hz)", "0.0 / 10"),
        ])

    def test_standard_and_absent_eq_have_no_alternate_view(self):
        self.assertEqual(io24_presets.eq_model(STANDARD), "standard")
        self.assertIsNone(io24_presets.alternate_eq_view({"eq": STANDARD}))
        self.assertIsNone(io24_presets.alternate_eq_view({}))
        self.assertIsNone(io24_presets.alternate_eq_view({"eq": {}}))

    def test_an_out_of_range_switch_is_rejected_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "lowfreq must be in"):
            io24_presets.alternate_eq_view(
                {"eq": dict(VINTAGE, lowfreq=7)})

    def test_saving_carries_the_stored_vintage_eq_verbatim(self):
        kept = io24_presets.current_slot_record(
            COMPLETE_BASE, "MAIN", **_slot_kwargs(alternate_eq=dict(VINTAGE)))
        self.assertEqual(kept["eq"], VINTAGE)
        rebuilt = io24_presets.current_slot_record(
            COMPLETE_BASE, "MAIN", **_slot_kwargs())
        self.assertEqual(io24_presets.eq_model(rebuilt["eq"]), "standard")

    def test_a_standard_section_is_refused_as_alternate_eq(self):
        with self.assertRaises(ValueError):
            io24_presets.current_slot_record(
                COMPLETE_BASE, "MAIN",
                **_slot_kwargs(alternate_eq=dict(STANDARD)))


class _Widget:
    def __init__(self):
        self.sensitive, self.visible, self.text, self.draws = True, None, "", 0
        self.active = False
        self.value = 0.0
        self.selected = 0

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_text(self, text):
        self.text = text

    def set_active(self, value):
        self.active = bool(value)

    def get_active(self):
        return self.active

    def set_value(self, value):
        self.value = float(value)

    def get_value(self):
        return self.value

    def set_selected(self, value):
        self.selected = int(value)

    def get_selected(self):
        return self.selected

    def queue_draw(self):
        self.draws += 1


class _Value:
    def __init__(self, value):
        self.value = value

    def get_value(self):
        return self.value

    def get_active(self):
        return bool(self.value)

    def get_selected(self):
        return int(self.value)


def _eq_widgets():
    W = {key: _Widget() for key in (
        "curve", "curve_row", "alternate_eq_rack", "alternate_eq_rack_row",
        "eq_on", "eq_model", "band_on", "shelf", "freq",
        "gain", "q", "eq_flat", "p_bboost", "p_batten", "p_bfreq",
        "p_mboost", "p_bbwidth", "p_mfreq", "p_hatten", "p_hsfreq",
        "v_lowgain", "v_lowfreq", "v_lowmidgain", "v_lowmidfreq",
        "v_himidgain", "v_himidfreq", "v_higain")}
    W["bands"] = [_Widget() for _ in range(4)]
    W["_standard_eq_rows"] = [_Widget() for _ in range(7)]
    W["_passive_eq_rows"] = [_Widget() for _ in range(8)]
    W["_vintage_eq_rows"] = [_Widget() for _ in range(7)]
    return W


class AlternateEqRackContractTests(unittest.TestCase):
    def test_passive_rack_exposes_every_exact_uc_control(self):
        specs = io24gtk.ALTERNATE_EQ_RACK_SPECS["passive"]
        self.assertEqual([spec["field"] for spec in specs], [
            "bboost", "batten", "bfreq", "mboost", "bbwidth", "mfreq",
            "hatten", "hsfreq",
        ])
        self.assertEqual(specs[2]["choices"],
                         ("20 Hz", "30 Hz", "60 Hz", "100 Hz"))
        self.assertEqual(specs[5]["choices"],
                         ("3 kHz", "4 kHz", "5 kHz", "8 kHz", "10 kHz",
                          "12 kHz", "16 kHz"))
        self.assertEqual(specs[7]["choices"],
                         ("5 kHz", "10 kHz", "20 kHz"))

    def test_vintage_rack_exposes_every_exact_uc_control(self):
        specs = io24gtk.ALTERNATE_EQ_RACK_SPECS["vintage"]
        self.assertEqual([spec["field"] for spec in specs], [
            "lowgain", "lowfreq", "lowmidgain", "lowmidfreq",
            "himidgain", "himidfreq", "higain",
        ])
        self.assertEqual(specs[1]["choices"],
                         ("35 Hz", "60 Hz", "110 Hz", "220 Hz"))
        self.assertEqual(specs[3]["choices"],
                         ("360 Hz", "700 Hz", "1.6 kHz"))
        self.assertEqual(specs[5]["choices"],
                         ("3.2 kHz", "4.8 kHz", "7.2 kHz"))

    def test_every_rack_parameter_has_a_distinct_visual_position(self):
        for model, specs in io24gtk.ALTERNATE_EQ_RACK_SPECS.items():
            with self.subTest(model=model):
                points = {(spec["x"], spec["y"]) for spec in specs}
                self.assertEqual(len(points), len(specs))
                for spec in specs:
                    lo = spec.get("lo", 0)
                    hi = spec.get("hi", len(spec.get("choices", ())) - 1)
                    self.assertEqual(
                        io24gtk.alternate_eq_control_fraction(spec, lo), 0.0)
                    self.assertEqual(
                        io24gtk.alternate_eq_control_fraction(spec, hi), 1.0)

    def test_rack_fraction_snaps_selector_fields_and_scales_amounts(self):
        passive = {s["field"]: s for s in
                   io24gtk.ALTERNATE_EQ_RACK_SPECS["passive"]}
        self.assertEqual(io24gtk.alternate_eq_control_value(
            passive["bfreq"], 0.49), 1)
        self.assertEqual(io24gtk.alternate_eq_control_value(
            passive["bfreq"], 0.51), 2)
        self.assertAlmostEqual(io24gtk.alternate_eq_control_value(
            passive["bboost"], 0.55), 5.5)

    def test_faceplate_scale_marks_use_exact_decoded_ranges(self):
        passive = {s["field"]: s for s in
                   io24gtk.ALTERNATE_EQ_RACK_SPECS["passive"]}
        vintage = {s["field"]: s for s in
                   io24gtk.ALTERNATE_EQ_RACK_SPECS["vintage"]}
        self.assertEqual(io24gtk.alternate_eq_scale_marks(
            passive["bboost"]), (
                (0.0, "0"), (0.2, "2"), (0.4, "4"),
                (0.6, "6"), (0.8, "8"), (1.0, "10")))
        self.assertEqual(io24gtk.alternate_eq_scale_marks(
            vintage["lowgain"]), (
                (0.0, "-16"), (0.25, "-8"), (0.5, "0"),
                (0.75, "+8"), (1.0, "+16")))
        self.assertEqual(io24gtk.alternate_eq_scale_marks(
            passive["bfreq"]), (
                (0.0, "20"), (1 / 3, "30"), (2 / 3, "60"),
                (1.0, "100")))


def _window():
    window = io24gtk.Win.__new__(io24gtk.Win)
    window._adopt_mute = False
    window.messages = []
    window.say = window.messages.append
    window.link_both = False
    window._fs = 48000.0
    window.eq_on_by_ch = {1: False, 2: False}
    window.bands_by_ch = {
        1: io24_presets.default_standard_eq_bands(),
        2: io24_presets.default_standard_eq_bands(),
    }
    window.alt_eq_by_ch = {1: None, 2: None}
    window._curves = {}
    window.racks = {}
    window.w = {}
    window.submitted = []
    window.ctl = SimpleNamespace(submit=window.submitted.append)
    return window


class RecalledVintageBodyHostTests(unittest.TestCase):
    @unittest.skipUnless(
        EXACT_ALT_EQ_AVAILABLE,
        "requires a local UC 4.7.2 dspusbdevice.dll",
    )
    def test_a_recall_shows_the_rest_and_lists_the_eq(self):
        record = {"preset_name": "MAIN", "eq": dict(VINTAGE),
                  "voicefx": {"on": 0}}
        window = _window()
        window.PR = io24_presets
        window._host_slot_entry = lambda slot: {"record": record}
        adopted = []
        window._adopt_preset_record = \
            lambda rec, chans, fx_channel=None: (
                adopted.append((rec, chans, fx_channel)),
                window._show_alternate_eq(
                    chans[0], io24_presets.alternate_eq_view(rec)),
                True)[-1]
        self.assertTrue(window._adopt_recalled_slot(1, 0))
        self.assertEqual(len(adopted), 1)
        shown, chans, fx_channel = adopted[0]
        self.assertEqual(shown["eq"], VINTAGE)
        self.assertEqual((chans, fx_channel), ((1,), 1))
        self.assertEqual(window._alt_eq(1)["eq"], VINTAGE)
        self.assertIsNone(window._alt_eq(2))
        self.assertEqual(window.messages[-1], "Input 1 · Vintage EQ")
        self.assertEqual(record["eq"], VINTAGE)      # the entry is untouched

    @unittest.skipUnless(
        EXACT_ALT_EQ_AVAILABLE,
        "requires a local UC 4.7.2 dspusbdevice.dll",
    )
    def test_vintage_controls_are_editable_and_send_the_exact_model(self):
        window = _window()
        window.w = {1: _eq_widgets()}
        view = io24_presets.alternate_eq_view({"eq": dict(VINTAGE)})
        window._show_alternate_eq(1, view)
        W = window.w[1]
        self.assertEqual(W["eq_model"].selected, 2)
        self.assertFalse(any(row.visible for row in W["_standard_eq_rows"]))
        self.assertFalse(any(row.visible for row in W["_vintage_eq_rows"]))
        self.assertFalse(any(row.visible for row in W["_passive_eq_rows"]))
        self.assertFalse(W["curve_row"].visible)
        self.assertTrue(W["alternate_eq_rack_row"].visible)
        self.assertTrue(W["eq_on"].active)
        self.assertEqual(W["v_lowfreq"].selected, 0)
        self.assertAlmostEqual(W["v_lowgain"].value, 1.76)
        self.assertIsNotNone(window._alt_eq(1)["sections"])
        self.assertNotEqual(window.response_for(1, 35.0), 0.0)

        window._alternate_eq_set(1, "higain", 4.0)
        self.assertEqual(len(window.submitted), 1)

        class Device:
            calls = []

            def set_alternate_eq(self, channel, eq, fs):
                self.calls.append((channel, eq, fs))

        device = Device()
        window.submitted.pop()(device)
        self.assertEqual(device.calls[0][0], 1)
        self.assertEqual(device.calls[0][1]["higain"], 4.0)
        self.assertEqual(device.calls[0][2], 48000.0)

    def test_clearing_a_channel_that_was_never_marked_touches_nothing(self):
        window = _window()
        window.w = {1: _eq_widgets()}
        window._show_alternate_eq(1, None)
        self.assertEqual(window.w[1]["curve"].draws, 1)
        self.assertTrue(all(row.visible
                            for row in window.w[1]["_standard_eq_rows"]))
        self.assertTrue(window.w[1]["curve_row"].visible)
        self.assertFalse(window.w[1]["alternate_eq_rack_row"].visible)

    def test_linked_edits_skip_a_held_channel_and_say_so_once(self):
        window = _window()
        window.link_both = True
        window._show_alternate_eq(
            2, io24_presets.alternate_eq_view({"eq": dict(VINTAGE)}))
        self.assertEqual(window._eq_write_targets(1), (1,))
        self.assertEqual(window._eq_write_targets(1), (1,))
        self.assertEqual(len(window.messages), 1)
        self.assertIn("skipped Channel 2 because its EQ model differs",
                      window.messages[0])

    def test_saving_passes_the_stored_eq_through(self):
        window = _window()
        window.fx_target = _Value(0)
        window.w = {1: {"gth": _Value(-48.7), "grange": _Value(-60.0),
                        "gatk": _Value(.005), "grel": _Value(.3),
                        "gkey": _Value(325.0), "gklisten": _Value(False),
                        "gexp": _Value(True), "lth": _Value(-.8)}}
        window.dyn_by_ch = {1: {"gate": True, "comp": True, "lim": True}}
        window._compressor_kwargs = lambda target: (2, {})
        window.fx_model = _Value(0)
        window._fx_live_params = lambda: {}
        window.fx_arm = _Value(False)
        window.bands_by_ch = {1: [dict(band) for band in FLAT]}
        window.hpf_by_ch = {1: 24.0}
        window.order_by_ch = {1: False}
        seen = []
        window.PR = SimpleNamespace(
            load=lambda: {"base": {}},
            current_slot_record=lambda base, name, **kw: seen.append(kw) or kw)

        window._current_slot_record("base", 1, "MAIN")
        self.assertIsNone(seen[-1]["alternate_eq"])
        window.alt_eq_by_ch = {
            1: io24_presets.alternate_eq_view({"eq": dict(VINTAGE)}), 2: None}
        window._current_slot_record("base", 1, "MAIN")
        self.assertEqual(seen[-1]["alternate_eq"], VINTAGE)

    def test_host_snapshot_round_trips_two_independent_alternate_models(self):
        window = _window()
        window.w = {1: _eq_widgets(), 2: _eq_widgets()}
        window._show_alternate_eq(
            1, io24_presets.alternate_eq_view({"eq": dict(PASSIVE)}))
        window._show_alternate_eq(
            2, io24_presets.alternate_eq_view({"eq": dict(VINTAGE)}))

        state = window._alternate_eq_state()
        normalized, migrations = io24._normalise_host_features(
            {"alternate_eq": state})
        self.assertEqual(migrations, [])
        self.assertEqual(normalized["alternate_eq"], state)

        restored = _window()
        restored.w = {1: _eq_widgets(), 2: _eq_widgets()}
        message = restored._adopt_alternate_eq_state(state)
        self.assertEqual(restored._eq_model_name(1), "passive")
        self.assertEqual(restored._eq_model_name(2), "vintage")
        self.assertEqual(restored._alt_eq(1)["eq"], PASSIVE)
        self.assertEqual(restored._alt_eq(2)["eq"], VINTAGE)
        self.assertEqual(restored.submitted, [])
        self.assertIn("Input 1 and 2", message)

    @unittest.skipUnless(
        EXACT_ALT_EQ_AVAILABLE,
        "requires a local UC 4.7.2 dspusbdevice.dll",
    )
    def test_power_changes_only_the_selected_model_on_one_input(self):
        window = _window()
        window.w = {1: _eq_widgets(), 2: _eq_widgets()}
        passive_off = dict(PASSIVE, eqallon=0)
        window._show_alternate_eq(
            1, io24_presets.alternate_eq_view({"eq": passive_off}))

        window._set_eq_enabled(1, True)
        self.assertTrue(window.eq_enabled(1))
        self.assertFalse(window.eq_enabled(2))
        self.assertEqual(window._alt_eq(1)["eq"]["eqallon"], 1)
        self.assertEqual(len(window.submitted), 1)


if __name__ == "__main__":
    unittest.main()
