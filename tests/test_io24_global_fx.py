#!/usr/bin/env python3
"""Hardware-free contracts for the shared, two-lane FX control surface."""

import ast
import inspect
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import io24
import io24_presets
import io24_fx
import io24gtk


def with_effects_return(device):
    """Lend a stub device the driver's real effects-return transaction.

    The transaction lives in ``Io24.establish_effects_return``; borrowing it
    keeps these tests asserting the exact wire writes and their order rather
    than the fact that one method was called.
    """
    device.BUS_ALIASES = io24.Io24.BUS_ALIASES
    device.MIXER_BUSES = io24.Io24.MIXER_BUSES
    device.FX_RETURN_SOURCE = io24.Io24.FX_RETURN_SOURCE
    device._bus = lambda bus: io24.Io24._bus(device, bus)
    device.establish_effects_return = (
        lambda **kwargs: io24.Io24.establish_effects_return(device, **kwargs))
    return device


ROOT = Path(__file__).parents[1]


class _Value:
    def __init__(self, value):
        self.value = value
        self.subtitle = ""

    def get_active(self):
        return bool(self.value)

    def get_selected(self):
        return int(self.value)

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value

    def set_selected(self, value):
        self.value = value

    def set_subtitle(self, value):
        self.subtitle = value


class GlobalFxTests(unittest.TestCase):
    def test_live_processing_permutation_identifies_voicefx_input(self):
        self.assertEqual(io24gtk.voicefx_target_from_processing([0, 1]), 1)
        self.assertEqual(io24gtk.voicefx_target_from_processing([1, 0]), 2)
        self.assertIsNone(io24gtk.voicefx_target_from_processing([0, 0]))
        self.assertIsNone(io24gtk.voicefx_target_from_processing(None))

    def test_fx_page_uses_xml_components_and_rack_without_a_master(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        page = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_fx_page")
        rendered = ast.get_source_segment(source, page)

        self.assertIn('io24_fx.VOICEFX_XML_SCHEMA.items()', rendered)
        self.assertIn('title=parameter["name"]', rendered)
        self.assertIn('self.fx_power[key]', rendered)
        self.assertIn('VoiceFxRack(self)', rendered)
        self.assertIn('title="Model"', rendered)
        self.assertIn('title="Voice FX"', rendered)
        self.assertNotIn('title="FX active"', rendered)
        self.assertNotIn('title="Target input"', rendered)
        self.assertNotIn('title="Device activation"', rendered)
        self.assertNotIn('title="FX activation unresolved"', rendered)
        self.assertNotIn('label="Presets…"', rendered)

    @unittest.skipUnless(
        (ROOT / "re" / "uc_component_model" / "dsp_fx_params.xml").is_file(),
        "requires privately retained UC component-model XML",
    )
    def test_runtime_schema_is_an_exact_transcription_of_retained_uc_xml(self):
        source = (ROOT / "re" / "uc_component_model" /
                  "dsp_fx_params.xml").read_text()
        marker = source.index('<uc:StringList id="Carriers">')
        start = source.rfind("<uc:ComponentModel", 0, marker)
        end = source.index("</uc:ComponentModel>", marker) + len(
            "</uc:ComponentModel>")
        root = ET.fromstring(source[start:end])
        namespace = {"uc": "http://www.presonus.com/xml/uc"}
        string_lists = {
            item.attrib["id"]: tuple(
                child.attrib["text"] for child in item.findall("uc:String", namespace))
            for item in root.findall(".//uc:StringList", namespace)
        }

        def normalized(parameter):
            result = {
                "id": parameter.attrib["id"],
                "name": parameter.attrib["name"],
                "type": parameter.attrib["type"],
            }
            for source, target in (("min", "min"), ("max", "max"),
                                   ("def", "default"), ("mid", "mid")):
                if source in parameter.attrib:
                    result[target] = float(parameter.attrib[source])
            for key in ("curve", "units"):
                if key in parameter.attrib:
                    result[key] = parameter.attrib[key]
            if "flags" in parameter.attrib:
                result["flags"] = tuple(parameter.attrib["flags"].split())
            if result["type"] == "list":
                result["choices"] = string_lists[result["units"]]
            return result

        for model, component in io24_fx.VOICEFX_XML_SCHEMA.items():
            paramlist = root.find(
                ".//uc:ParamList[@id='%s']" % component["paramlist"], namespace)
            self.assertIsNotNone(paramlist, model)
            xml_parameters = [normalized(item)
                              for item in paramlist.findall("uc:Param", namespace)]
            runtime_parameters = []
            for parameter in component["parameters"]:
                copied = {key: value for key, value in parameter.items()
                          if key != "builder"}
                runtime_parameters.append(copied)
            self.assertEqual(runtime_parameters, xml_parameters, model)

    def test_xml_builder_routes_match_every_recovered_packet_builder(self):
        builders = {
            "transformer": io24_fx.fx_transformer_blobs,
            "detuner": io24_fx.fx_detuner,
            "vocoder": io24_fx.fx_vocoder,
            "ringmod": io24_fx.fx_ringmod,
            "filters": io24_fx.fx_filters,
            "delay": io24_fx.fx_delay,
        }
        for model, builder in builders.items():
            expected = {parameter["builder"]
                        for parameter in io24_fx.VOICEFX_XML_SCHEMA[model]
                        ["parameters"] if parameter.get("builder") is not None}
            actual = set(inspect.signature(builder).parameters) - {"fs", "index"}
            self.assertEqual(expected, actual, model)

    def test_host_rows_keep_exact_xml_order_names_defaults_and_lists(self):
        for model, component in io24_fx.VOICEFX_XML_SCHEMA.items():
            editable = [parameter for parameter in component["parameters"]
                        if parameter.get("builder") not in (None, "on")]
            rows = io24gtk.Win.FX_PARAMS[model]
            self.assertEqual([row[0] for row in rows],
                             [parameter["builder"] for parameter in editable])
            self.assertEqual([row[1] for row in rows],
                             [parameter["name"] for parameter in editable])
            self.assertEqual([row[5] for row in rows],
                             [parameter.get("default", 0) for parameter in editable])
        self.assertEqual(io24_fx.VOICEFX_XML_SCHEMA["detuner"]
                         ["parameters"][1]["choices"],
                         ("-8", "-7", "-6", "-5", "-4", "-3", "-2", "-1", "0"))
        self.assertEqual(io24_fx.VOICEFX_XML_SCHEMA["vocoder"]
                         ["parameters"][2]["choices"],
                         ("Noise", "Sawtooth", "Rect"))

    def test_every_editable_parameter_changes_the_shared_visual_profile(self):
        for model, component in io24_fx.VOICEFX_XML_SCHEMA.items():
            defaults = {
                parameter["builder"]: parameter.get("default", 0)
                for parameter in component["parameters"]
                if parameter.get("builder") not in (None, "on")
            }
            baseline = io24gtk.voicefx_visual_profile(
                model, defaults, phase=0.37, points=67)
            for parameter in component["parameters"]:
                builder = parameter.get("builder")
                if builder in (None, "on"):
                    continue
                changed = dict(defaults)
                if parameter["type"] == "toggle":
                    changed[builder] = not bool(defaults[builder])
                elif parameter["type"] == "list":
                    changed[builder] = (
                        int(defaults[builder]) + 1) % len(parameter["choices"])
                else:
                    alternative = parameter["min"]
                    if alternative == defaults[builder]:
                        alternative = parameter["max"]
                    changed[builder] = alternative
                with self.subTest(model=model, parameter=builder):
                    self.assertNotEqual(
                        baseline,
                        io24gtk.voicefx_visual_profile(
                            model, changed, phase=0.37, points=67))

    def test_large_editor_and_rack_share_the_same_visual_profile(self):
        self.assertIn("voicefx_visual_profile", inspect.getsource(
            io24gtk.FXVisual.paint))
        self.assertIn("voicefx_visual_profile", inspect.getsource(
            io24gtk.VoiceFxRack._mini))

    def test_readonly_vocoder_telemetry_is_not_a_dead_host_control(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        page = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_fx_page")
        rendered = ast.get_source_segment(source, page)

        readonly = [parameter["name"]
                    for component in io24_fx.VOICEFX_XML_SCHEMA.values()
                    for parameter in component["parameters"]
                    if parameter.get("builder") is None]
        self.assertEqual(readonly, ["Voiced"])
        self.assertIn("elif builder is None:", rendered)
        self.assertNotIn('Gtk.Label(label="—")', rendered)
        self.assertNotIn("row.set_sensitive(False)", rendered)

    def test_effects_stack_and_wheel_avoid_false_page_offsets(self):
        source = (ROOT / "io24gtk.py").read_text()
        tree = ast.parse(source)
        page = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_fx_page")
        rendered = ast.get_source_segment(source, page)

        self.assertIn("keep_page_wheel_scrolling(page)", rendered)
        self.assertIn("self.fx_param_stack.set_vhomogeneous(False)", rendered)
        self.assertEqual(io24gtk.wheel_scroll_value(
            100, 0, 1000, 400, 1), 124)
        self.assertEqual(io24gtk.wheel_scroll_value(
            100, 0, 1000, 400, 1, 56), 156)
        self.assertEqual(io24gtk.wheel_scroll_value(
            590, 0, 1000, 400, 1, 56), 600)
        self.assertEqual(io24gtk.wheel_scroll_value(
            10, 0, 1000, 400, -1, 56), 0)
        self.assertEqual(io24gtk.effects_wheel_step(0), 24)
        self.assertEqual(io24gtk.effects_wheel_step(200), 18)
        self.assertAlmostEqual(io24gtk.effects_wheel_step(676), 27.04)
        self.assertEqual(io24gtk.effects_wheel_step(1000), 32)

    def test_skew_adapter_hits_xml_midpoint_and_round_trips(self):
        scale = _Value(0.5)
        control = io24gtk._SkewValue(scale, 50.0, 500.0, 100.0)
        self.assertAlmostEqual(control.get_value(), 100.0)
        for value in (50.0, 80.0, 100.0, 250.0, 500.0):
            control.set_value(value)
            self.assertAlmostEqual(control.get_value(), value, places=8)

    def test_rack_selects_model_and_toggles_only_that_models_on_field(self):
        selector = _Value(0)
        powers = {model: _Value(False) for model in io24gtk.Win.FX_ORDER}
        host = SimpleNamespace(FX_ORDER=io24gtk.Win.FX_ORDER,
                               fx_model=selector, fx_power=powers)
        rack = SimpleNamespace(win=host, _slot=lambda _x: 3,
                               queue_draw=lambda: None)

        io24gtk.VoiceFxRack._clicked(rack, None, 1, 350, 20)
        self.assertEqual(selector.get_selected(), 3)
        self.assertFalse(powers["ringmod"].get_active())
        io24gtk.VoiceFxRack._clicked(rack, None, 2, 350, 20)
        self.assertTrue(powers["ringmod"].get_active())
        self.assertFalse(powers["transformer"].get_active())

    def test_selected_power_adapter_enforces_one_active_model(self):
        selector = _Value(0)
        controls = {
            "transformer": _Value(True),
            "delay": _Value(False),
        }
        power = io24gtk._SelectedFxPower(
            selector, controls, ("transformer", "delay"))

        self.assertTrue(power.get_active())
        selector.value = 1
        self.assertFalse(power.get_active())
        power.set_active(True)
        self.assertTrue(controls["delay"].get_active())
        self.assertFalse(controls["transformer"].get_active())
        selector.value = 0
        power.set_active(True)
        self.assertTrue(controls["transformer"].get_active())
        self.assertFalse(controls["delay"].get_active())

    def test_turning_on_a_rack_switch_turns_every_other_model_off(self):
        powers = {model: _Value(False) for model in io24gtk.Win.FX_ORDER}
        powers["transformer"].set_value(True)
        powers["delay"].set_value(True)
        pushes = []
        host = SimpleNamespace(
            FX_ORDER=io24gtk.Win.FX_ORDER,
            fx_model=_Value(io24gtk.Win.FX_ORDER.index("delay")),
            fx_power=powers,
            _fx_mute=False,
            _adopt_mute=False,
            fx_rack=None,
            fx_visual=None,
            _push_fx=lambda: pushes.append("push"),
        )
        host.fx_arm = io24gtk._SelectedFxPower(
            host.fx_model, powers, host.FX_ORDER)

        io24gtk.Win._fx_power_changed(host, "delay")

        self.assertTrue(powers["delay"].get_active())
        self.assertTrue(all(
            not control.get_active()
            for model, control in powers.items() if model != "delay"))
        self.assertEqual(pushes, ["push"])

    def test_selecting_an_off_model_clears_the_old_models_on_light(self):
        powers = {model: _Value(False) for model in io24gtk.Win.FX_ORDER}
        powers["transformer"].set_value(True)
        pushes = []
        host = SimpleNamespace(
            FX_ORDER=io24gtk.Win.FX_ORDER,
            fx_model=_Value(io24gtk.Win.FX_ORDER.index("detuner")),
            fx_power=powers,
            _fx_mute=False,
            fx_visual=None,
            fx_param_stack=None,
            fx_rack=None,
            _push_fx=lambda: pushes.append("push"),
        )
        host.fx_arm = io24gtk._SelectedFxPower(
            host.fx_model, powers, host.FX_ORDER)

        io24gtk.Win._fx_model_changed(host)

        self.assertFalse(powers["transformer"].get_active())
        self.assertFalse(powers["detuner"].get_active())
        self.assertEqual(pushes, ["push"])

    def test_fx_edit_assigns_the_selected_input_before_model_state(self):
        calls = []

        class Device:
            def set_voicefx_channel(self, channel):
                calls.append(("target", channel))

            def set_fx(self, model, **kwargs):
                calls.append((model, kwargs))
                return 2

        device = Device()
        host = SimpleNamespace(
            _fx_mute=False,
            _fs=48000.0,
            _fx_last_sent_device=None,
            _fx_last_sent_target=None,
            ctl=SimpleNamespace(dev=device, submit=lambda fn: fn(device)),
            fx_arm=_Value(True),
            fx_model=_Value(5),
            fx_target=_Value(1),
            fx_params={"delay": {
                "time_s": _Value(0.173),
                "feedback": _Value(0.82),
                "mix": _Value(1.0),
            }},
            FX_ORDER=io24gtk.Win.FX_ORDER,
            say=lambda _message: None,
        )

        with mock.patch.object(io24gtk.GLib, "idle_add", return_value=1):
            io24gtk.Win._push_fx(host)

        self.assertEqual(calls, [("target", 2), ("delay", {
            "on": True,
            "time_s": 0.173,
            "feedback": 0.82,
            "mix": 1.0,
            "fs": 48000.0,
        })])

    def test_96khz_delay_uses_host_insert_without_selecting_device_model_5(self):
        calls = []
        messages = []
        controls = []

        class Device:
            def set_voicefx_channel(self, channel):
                calls.append(("target", channel))

            def set_fx(self, model, **kwargs):
                calls.append((model, kwargs))

            def quiesce_voicefx_for_host_delay(self, fs, quantum=512):
                calls.append(("quiesce", fs, quantum))

        insert = SimpleNamespace(
            running=True, delay_channels=(1,),
            set_delay_controls=lambda channel, state:
            controls.append((channel, state)) or True)

        device = Device()
        host = SimpleNamespace(
            _fx_mute=False,
            _fs=96000.0,
            _selected_rate=96000,
            _fx_last_sent_device=None,
            _fx_last_sent_target=None,
            _host_delay_quiesced_device=None,
            ctl=SimpleNamespace(dev=device, submit=lambda fn: fn(device)),
            insert=insert,
            _insert_reconcile=lambda: None,
            fx_arm=_Value(True),
            fx_model=_Value(5),
            fx_target=_Value(0),
            fx_params={"delay": {
                "time_s": _Value(0.173),
                "feedback": _Value(0.25),
                "mix": _Value(0.5),
            }},
            FX_ORDER=io24gtk.Win.FX_ORDER,
            say=messages.append,
        )

        io24gtk.Win._push_fx(host)

        self.assertEqual(calls, [("quiesce", 96000.0, 512)])
        self.assertEqual(controls, [(1, {
            "on": True, "time_s": 0.173,
            "feedback": 0.25, "mix": 0.5,
        })])
        self.assertEqual(messages, [])
        self.assertEqual(host.fx_model.subtitle,
                         "Host processing at 96 kHz")

    def test_either_channel_preset_records_the_same_global_fx_state(self):
        host = SimpleNamespace(
            FX_ORDER=["transformer", "detuner"],
            fx_model=_Value(1),
            _fx_live_params=lambda: {"detune": 1.0},
            fx_arm=_Value(True),
        )

        first = io24gtk.Win._record_voicefx(host, 1)
        second = io24gtk.Win._record_voicefx(host, 2)

        self.assertEqual(first, second)
        self.assertTrue(first[1]["on"])

    def test_direct_preset_apply_assigns_the_requested_input(self):
        calls = []

        class Device:
            def set_voicefx_channel(self, channel):
                calls.append(("set_voicefx_channel", channel))

            def set_fx(self, model, **kwargs):
                calls.append(("set_fx", model, kwargs))
                return 2

            def set_fx_mix(self, channel, value):
                calls.append(("set_fx_mix", channel, value))

            def set_send_db(self, source, bus, gain_db):
                calls.append(("set_send_db", source, bus, gain_db))

            def set_send_assigned(self, source, bus, on=True):
                calls.append(("set_send_assigned", source, bus, on))

        preset = {
            "voicefx": {
                "__classid": io24_fx.VOICEFX_CLASS_IDS["delay"],
                "on": 1,
                "time": 0.173,
                "feedback": 0.82,
                "mix": 1.0,
            },
        }

        io24_presets.apply_preset(with_effects_return(Device()), preset,
                                  channel=2, with_fx=True, fs=48000.0)

        self.assertEqual(calls, [
            ("set_voicefx_channel", 2),
            ("set_fx", "delay", {"on": True, "time_s": 0.173,
                                 "feedback": 0.82, "mix": 1.0,
                                 "fs": 48000.0}),
        ])

    def test_voicefx_preset_apply_is_independent_of_reverb_return(self):
        class Device:
            def set_voicefx_channel(self, _channel):
                pass

            def set_fx(self, _model, **_kwargs):
                return 2

        report = io24_presets.apply_voicefx(
            Device(),
            {"voicefx": {"__classid": io24_fx.VOICEFX_CLASS_IDS["delay"],
                         "on": 1, "time": 0.1, "feedback": 0.2, "mix": 0.3}},
            channel=2, establish_return=False, fs=48000.0)

        self.assertEqual(
            report,
            "voicefx=delay (2 writes; UC 4.7.2 state transaction)",
        )

    def test_fx_activation_failure_does_not_cancel_a_preset_strip_load(self):
        calls = []

        class Presets:
            @staticmethod
            def direct_apply_support(_record):
                return True, "exact"

            @staticmethod
            def apply_preset(_device, _record, channel, with_fx=False,
                             fs=None):
                self.assertEqual(fs, 48000.0)
                calls.append(("strip", channel, with_fx))

            @staticmethod
            def apply_voicefx(_device, _record, **kwargs):
                calls.append(("fx", kwargs.get("channel"),
                              kwargs.get("establish_return")))
                raise RuntimeError("FX transport failed")

        messages = []
        host = SimpleNamespace(
            PR=Presets,
            _fs=48000.0,
            ctl=SimpleNamespace(submit=lambda fn: fn(object())),
            link_both=False,
            factory_target=_Value(0),
            _factory_target_channel=lambda: 1,
            _adopt_preset_record=lambda _record, _channels: None,
            say=messages.append,
        )

        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            io24gtk.Win._load_factory(
                host, None, "Saved", True,
                record={"voicefx": {"on": 1}})

        self.assertEqual(calls, [("strip", 1, False), ("fx", 1, None)])
        self.assertEqual(messages, [
            "Loaded Saved on Channel 1; FX not loaded: FX transport failed",
        ])

    def test_fx_transport_success_is_not_reported_as_activation_success(self):
        class Presets:
            @staticmethod
            def direct_apply_support(_record):
                return True, "exact"

            @staticmethod
            def apply_preset(_device, _record, _channel, with_fx=False,
                             fs=None):
                self.assertFalse(with_fx)
                self.assertEqual(fs, 48000.0)

            @staticmethod
            def apply_voicefx(_device, _record, **_kwargs):
                return ("voicefx=transformer (6 writes; UC 4.7.2 state "
                        "transaction)")

        messages = []
        adopted = []
        host = SimpleNamespace(
            PR=Presets,
            _fs=48000.0,
            ctl=SimpleNamespace(submit=lambda fn: fn(object())),
            link_both=False,
            _factory_target_channel=lambda: 1,
            _adopt_preset_record=lambda _record, _channels, **kwargs:
                adopted.append(kwargs),
            say=messages.append,
        )

        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            io24gtk.Win._load_factory(
                host, None, "Saved", True,
                record={"voicefx": {"on": 1}})

        self.assertEqual(adopted, [{"fx_channel": 1}])
        self.assertEqual(messages, [
            "Loaded Saved on Channel 1 · Voice FX updated",
        ])

    def test_automatic_resume_neither_recalls_a_slot_nor_replays_old_owner(self):
        source = inspect.getsource(io24gtk.Win._resume_session)
        self.assertNotIn("recall_device_slot", source)
        self.assertNotIn('state["voicefx_target"]', source)
        self.assertIn("set_processing_channel", io24gtk.RESUME_SKIP)

    class _Bypass:
        """Minimal stand-in for ProcessingMix: a composed channel scalar."""

        def __init__(self, amount, bypassed):
            self.amount, self._bypassed = amount, bypassed

        def bypassed(self):
            return self._bypassed

        def get_value(self):
            return 0.0 if self._bypassed else self.amount

    @staticmethod
    def _recording_device():
        calls = []

        class Device:
            def set_voicefx_channel(self, channel):
                calls.append(("set_voicefx_channel", channel))

            def set_fx_mix(self, channel, value):
                calls.append(("set_fx_mix", channel, value))

            def set_send_db(self, source, bus, gain_db):
                calls.append(("set_send_db", source, bus, gain_db))

            def set_send_assigned(self, source, bus, on=True):
                calls.append(("set_send_assigned", source, bus, on))

            def set_fx(self, model, **kwargs):
                calls.append(("set_fx", model, kwargs["on"]))
                return 2

        return with_effects_return(Device()), calls

    def _fx_host(self, device, amount=0.75, bypassed=False, return_db=-3.0):
        messages = []
        host = SimpleNamespace(
            _fx_mute=False,
            _fs=48000.0,
            _fx_last_sent_device=None,
            _fx_last_sent_target=None,
            ctl=SimpleNamespace(dev=device, submit=lambda fn: fn(device)),
            fx_arm=_Value(True),
            fx_model=_Value(5),
            fx_target=_Value(0),
            fx_params={"delay": {
                "time_s": _Value(0.25),
                "feedback": _Value(0.9),
                "mix": _Value(1.0),
            }},
            FX_ORDER=io24gtk.Win.FX_ORDER,
            processing_mix_controls={1: self._Bypass(amount, bypassed)},
            rev_return_controls={"main": _Value(return_db)},
            say=messages.append,
        )
        return host, messages

    def test_arming_fx_does_not_mutate_the_reverb_return(self):
        """UC 4.7.2 changes only block 201 for a VoiceFX edit."""
        device, calls = self._recording_device()
        host, _messages = self._fx_host(device)

        with mock.patch.object(io24gtk.GLib, "idle_add", return_value=1):
            io24gtk.Win._push_fx(host)

        self.assertEqual(calls, [
            ("set_voicefx_channel", 1),
            ("set_fx", "delay", True),
        ])

    def test_arming_fx_never_lifts_a_channel_bypass_and_says_so(self):
        device, calls = self._recording_device()
        host, messages = self._fx_host(device, bypassed=True)

        def run(fn, *args):
            fn(*args)
            return 1

        with mock.patch.object(io24gtk.GLib, "idle_add", side_effect=run):
            io24gtk.Win._push_fx(host)

        self.assertNotIn("set_fx_mix", [call[0] for call in calls])
        self.assertEqual(calls[-1], ("set_fx", "delay", True))
        self.assertEqual(len(messages), 1)
        self.assertIn("bypassed", messages[0])

    def test_voicefx_does_not_depend_on_the_reverb_return_floor(self):
        device, calls = self._recording_device()
        host, messages = self._fx_host(device, return_db=-60.0)

        def run(fn, *args):
            fn(*args)
            return 1

        with mock.patch.object(io24gtk.GLib, "idle_add", side_effect=run):
            io24gtk.Win._push_fx(host)

        self.assertEqual(calls, [
            ("set_voicefx_channel", 1),
            ("set_fx", "delay", True),
        ])
        self.assertEqual(messages, [])

    def test_a_usable_return_says_nothing(self):
        device, _calls = self._recording_device()
        host, messages = self._fx_host(device, return_db=-3.0)

        def run(fn, *args):
            fn(*args)
            return 1

        with mock.patch.object(io24gtk.GLib, "idle_add", side_effect=run):
            io24gtk.Win._push_fx(host)

        self.assertEqual(messages, [])

    def test_turning_fx_off_does_not_touch_reverb_routing(self):
        device, calls = self._recording_device()
        host, _messages = self._fx_host(device)
        host.fx_arm = _Value(False)

        io24gtk.Win._push_fx(host)

        self.assertEqual(calls, [
            ("set_voicefx_channel", 1),
            ("set_fx", "delay", False),
        ])

    def test_only_reverb_establishes_the_shared_return(self):
        self.assertNotIn("establish_effects_path",
                         inspect.getsource(io24gtk.Win._push_fx))
        self.assertIn("establish_effects_path",
                      inspect.getsource(io24gtk.Win._push_reverb))

    def test_a_records_fx_load_does_not_touch_reverb_routing(self):
        source = inspect.getsource(io24gtk.Win._load_factory)
        self.assertNotIn("effects_path_plan", source)
        self.assertNotIn("establish_return=", source)

    def test_voicefx_apply_does_not_call_the_reverb_route_helper(self):
        self.assertIn("establish_effects_return",
                      inspect.getsource(io24gtk.establish_effects_path))
        self.assertNotIn("establish_effects_return",
                         inspect.getsource(io24_presets._apply_voicefx_call))
        self.assertTrue(hasattr(io24.Io24, "establish_effects_return"))

    def test_an_unbuilt_host_establishes_nothing(self):
        device, calls = self._recording_device()
        host, _messages = self._fx_host(device)
        del host.processing_mix_controls

        io24gtk.Win._push_fx(host)

        self.assertEqual(calls, [
            ("set_voicefx_channel", 1),
            ("set_fx", "delay", True),
        ])

    def test_the_driver_transaction_leaves_a_bypassed_channel_alone(self):
        """``channel_mix=None`` must not write wire 4: it is a single scalar, so
        writing it would take the channel out of bypass and change what the main
        output carries."""
        calls = []

        class Device:
            def set_fx_mix(self, channel, value):
                calls.append(("set_fx_mix", channel, value))

            def set_send_db(self, source, bus, gain_db):
                calls.append(("set_send_db", source, bus, gain_db))

            def set_send_assigned(self, source, bus, on=True):
                calls.append(("set_send_assigned", source, bus, on))

        device = with_effects_return(Device())
        result = device.establish_effects_return(bus="mixa", return_db=-6.0)

        self.assertEqual(calls, [
            ("set_send_db", "fxreturn/ch1", "mixa", -6.0),
            ("set_send_assigned", "fxreturn/ch1", "mixa", True),
        ])
        self.assertEqual(result["written"],
                         ["return_level", "return_assigned"])
        self.assertEqual(result["bus"], "mixa")

    def test_the_driver_transaction_writes_a_supplied_channel_mix_first(self):
        calls = []

        class Device:
            def set_fx_mix(self, channel, value):
                calls.append(("set_fx_mix", channel, value))

            def set_send_db(self, source, bus, gain_db):
                calls.append(("set_send_db", source, bus, gain_db))

            def set_send_assigned(self, source, bus, on=True):
                calls.append(("set_send_assigned", source, bus, on))

        device = with_effects_return(Device())
        result = device.establish_effects_return(return_db=0.0,
                                                channel_mix=0.8)

        self.assertEqual(calls[0], ("set_fx_mix", 1, 0.8))
        self.assertEqual(result["written"][0], "channel_mix")
        with self.assertRaises(ValueError):
            device.establish_effects_return(channel=3, channel_mix=1.0)

    def test_assignment_shadow_is_projected_as_voicefx_target(self):
        state = io24gtk.shadow_ui_state({
            "old-owner": {
                "fn": "set_processing_channel",
                "kwargs": {"channel": 1, "source_input": 2},
            },
            "fx": {
                "fn": "set_fx",
                "kwargs": {"model": "delay", "on": True},
            },
        })

        self.assertEqual(state["voicefx_target"], 2)
        self.assertEqual(state["voicefx"], {"model": "delay", "on": True})


if __name__ == "__main__":
    unittest.main()
