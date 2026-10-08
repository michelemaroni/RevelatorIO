#!/usr/bin/env python3
"""Hardware-free contracts for the reorganized Presets page.

The user asked (2026-09-11) for two drop-downs, User Presets and Factory
Presets, in place of separate sections for device slots, Host-known presets,
factory sounds and A/B previews. A preset loads, Fat Channel and Voice FX
together, into the selected channel or both when linked. Presets the user saves
live on the computer, and putting one on the unit's Preset button is an action
on the preset.

Nothing here opens USB.
"""

import ast
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import io24
import io24_presets
import io24gtk

ROOT = Path(__file__).resolve().parents[1]


def _function(name):
    source = (ROOT / "io24gtk.py").read_text()
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


def _record(name="MY VOCAL"):
    return {"preset_name": name, "comp": {}, "eq": {}, "voicefx": {"on": 0}}


def _entry(slot, channel, name="MAIN"):
    return {"slot": slot, "physical_channel": channel,
            "record": _record(name), "device_identity": {"serial": "TEST"}}


class _Selected:
    def __init__(self, value):
        self.value = value

    def get_selected(self):
        return self.value


class _Row:
    def __init__(self):
        self.subtitle = None

    def set_subtitle(self, text):
        self.subtitle = text


def _bind(host, *names):
    for name in names:
        setattr(host, name, getattr(io24gtk.Win, name).__get__(host))
    return host


class UserPresetStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "user-presets.json"

    def test_a_missing_store_is_empty(self):
        self.assertEqual(io24_presets.load_user_presets(self.path), {})

    def test_presets_come_back_in_the_order_they_were_saved(self):
        io24_presets.save_user_preset("B", _record("B"), self.path)
        io24_presets.save_user_preset("A", _record("A"), self.path)
        self.assertEqual(list(io24_presets.load_user_presets(self.path)),
                         ["B", "A"])

    def test_saving_an_existing_name_replaces_it_in_place(self):
        io24_presets.save_user_preset("A", _record("A"), self.path)
        io24_presets.save_user_preset("B", _record("B"), self.path)
        changed = _record("A")
        changed["comp"] = {"on": 1}
        io24_presets.save_user_preset("A", changed, self.path)
        presets = io24_presets.load_user_presets(self.path)
        self.assertEqual(list(presets), ["A", "B"])
        self.assertEqual(presets["A"]["comp"], {"on": 1})

    def test_the_record_carries_the_name_it_was_saved_under(self):
        io24_presets.save_user_preset("New name", _record("old"), self.path)
        presets = io24_presets.load_user_presets(self.path)
        self.assertEqual(presets["New name"]["preset_name"], "New name")

    def test_rename_and_delete(self):
        io24_presets.save_user_preset("A", _record("A"), self.path)
        io24_presets.save_user_preset("B", _record("B"), self.path)
        io24_presets.rename_user_preset("A", "C", self.path)
        self.assertEqual(list(io24_presets.load_user_presets(self.path)),
                         ["C", "B"])
        with self.assertRaises(ValueError):
            io24_presets.rename_user_preset("C", "B", self.path)
        io24_presets.delete_user_preset("C", self.path)
        self.assertEqual(list(io24_presets.load_user_presets(self.path)), ["B"])
        with self.assertRaises(KeyError):
            io24_presets.delete_user_preset("C", self.path)

    def test_the_store_uses_the_factory_file_format(self):
        io24_presets.save_user_preset("A", _record("A"), self.path)
        self.assertIsInstance(json.loads(self.path.read_text()), list)
        self.assertIn("A", io24_presets.load(self.path))

    def test_a_damaged_store_is_refused_rather_than_emptied(self):
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            io24_presets.load_user_presets(self.path)
        with self.assertRaises(ValueError):
            io24_presets.save_user_preset("A", _record("A"), self.path)
        self.assertEqual(self.path.read_text(), "{not json")


class PresetsPageShapeTests(unittest.TestCase):
    def test_two_drop_downs_and_nothing_else_to_scroll_past(self):
        page = _function("_presets_page")
        self.assertIn('title="User Presets"', page)
        self.assertIn('title="Factory Presets"', page)
        self.assertEqual(page.count("Adw.ExpanderRow("), 2)
        self.assertIn('title="Preset name"', page)
        self.assertIn("self._save_user_preset_clicked", page)
        for gone in ("Temporary A/B", "Complete-body base", "Load + Voice FX",
                     "Load strip", "What these controls do",
                     "self._device_preset_group()", "self._host_preset_group()",
                     "Save and activate…", "Save only…"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, page)

    def test_the_unit_preset_mode_moved_to_the_device_page(self):
        self.assertIn("self._preset_button_group()", _function("_device_page"))
        self.assertIn('title="Available presets"',
                      _function("_preset_button_group"))

    def test_uc_scenes_have_visible_validated_save_and_load_actions(self):
        page = _function("_presets_page")
        self.assertIn('title="Whole setup"', page)
        self.assertIn('title="UC scene"', page)
        self.assertIn('title="Full Host setup"', page)
        self.assertIn('title="Automatic recovery"', page)
        self.assertIn('label="Save scene…"', page)
        self.assertIn('label="Load scene…"', page)
        loader = _function("_load_scene_clicked")
        self.assertIn("io24_scene.plan", loader)
        self.assertIn("io24_scene.apply_transactional", loader)
        saver = _function("_save_scene_clicked")
        self.assertIn("io24_scene.capture", saver)
        self.assertIn("io24_scene.save", saver)

    def test_save_to_device_asks_for_input_and_exact_storage_destination(self):
        chooser = _function("_choose_device_preset_destination")
        self.assertIn('"Preset-button block 1"', chooser)
        self.assertIn('"Preset-button block 2"', chooser)
        self.assertIn('"Device library slot 1"', chooser)
        self.assertIn('"Device library slot 6"', chooser)
        self.assertIn('title="Input"', chooser)
        self.assertIn('title="Destination"', chooser)

        calls = []
        host = SimpleNamespace(
            _put_on_unit=lambda *args, **kwargs:
            calls.append(("block", args, kwargs)),
            _put_in_device_library=lambda *args, **kwargs:
            calls.append(("library", args, kwargs)),
        )
        row = lambda value: SimpleNamespace(get_selected=lambda: value)
        io24gtk.Win._device_preset_destination_response(
            host, None, "continue", "Warm", _record("Warm"), "user:Warm",
            row(1), row(0))
        io24gtk.Win._device_preset_destination_response(
            host, None, "continue", "Warm", _record("Warm"), "user:Warm",
            row(0), row(7))

        self.assertEqual(calls[0][0], "block")
        self.assertEqual(calls[0][2], {"target": 2})
        self.assertEqual(calls[1][0], "library")
        self.assertEqual(calls[1][2], {"target": 1, "channel_slot": 5})

    def test_every_loadable_row_has_a_visible_load_button(self):
        self.assertEqual(
            _function("_populate_user_presets").count(
                "self._preset_load_button("), 3)
        self.assertEqual(
            _function("_populate_factory_presets").count(
                "self._preset_load_button("), 1)
        self.assertIn('Gtk.Button(label="Load"',
                      _function("_preset_load_button"))

    def test_load_is_single_click_only(self):
        self.assertNotIn(
            "_on_double_click", _function("_populate_user_presets"))
        self.assertNotIn(
            "_on_double_click", _function("_populate_factory_presets"))
        self.assertNotIn(
            "Double-click to load", (ROOT / "io24gtk.py").read_text())
        self.assertIn(
            'button.connect("clicked"', _function("_preset_load_button"))

    def test_preset_summaries_are_plain_component_labels(self):
        record = {
            "gate": {"on": 1},
            "comp": {"on": 1, "compmodel": "standard"},
            "eq": {"eqallon": 1, "class": "vintage"},
            "limit": {"limiteron": 1},
            "voicefx": {"on": 1},
        }
        with mock.patch.object(io24_presets, "compressor_model",
                               return_value="standard"), \
             mock.patch.object(io24_presets, "eq_model",
                               return_value="vintage"):
            summary = io24_presets.describe(record)

        self.assertEqual(
            summary, "Gate, Standard Comp, Vintage EQ, Limiter, Voice FX")
        self.assertNotIn("device-slot", summary)


class LoadingTests(unittest.TestCase):
    def _host(self, target=1, linked=False):
        calls = []
        host = SimpleNamespace(link_both=linked,
                               factory_target=_Selected(target - 1))
        host._recall_host_preset = lambda _b, entry: calls.append(
            ("recall", entry["physical_channel"], entry["slot"]))
        host._load_factory = (
            lambda _b, name, with_fx=False, record=None, channels=None:
            calls.append(("apply", name, with_fx,
                          tuple(channels) if channels else None)))
        _bind(host, "_factory_target_channel", "_load_device_entry")
        return host, calls

    def test_a_unit_preset_on_its_own_channel_is_loaded_by_the_unit(self):
        host, calls = self._host(target=1)
        host._load_device_entry(_entry(0, 1))
        self.assertEqual(calls, [("recall", 1, 0)])

    def test_a_unit_preset_loaded_into_the_other_channel_is_applied(self):
        host, calls = self._host(target=2)
        host._load_device_entry(_entry(0, 1))
        self.assertEqual(calls, [("apply", "MAIN", True, (2,))])

    def test_linked_loads_the_owner_on_the_unit_and_the_strip_on_the_other(self):
        host, calls = self._host(target=1, linked=True)
        host._load_device_entry(_entry(1, 1, "B A S E"))
        self.assertEqual(calls, [("recall", 1, 1),
                                 ("apply", "B A S E", False, (2,))])

    def test_the_factory_load_accepts_a_record_and_explicit_channels(self):
        source = _function("_load_factory")
        self.assertIn("record=None", source)
        self.assertIn("channels=None", source)
        self.assertIn("self._factory_target_channel()", source)


class SavingTests(unittest.TestCase):
    def _host(self, existing=()):
        events = []
        host = SimpleNamespace(
            factory_target=_Selected(1),
            slot_name_row=SimpleNamespace(get_text=lambda: "  Warm  "),
            PR=SimpleNamespace(
                load_user_presets=lambda: {name: {} for name in existing},
                save_user_preset=lambda name, record: events.append(
                    ("saved", name, record))),
            _slot_base_name=lambda: "Broadcast",
            _current_slot_record=(
                lambda base, target, name, strict_voicefx=True:
                {"preset_name": name, "base": base, "target": target,
                 "strict": strict_voicefx}),
            _populate_user_presets=lambda: events.append("refreshed"),
            _confirm_user_overwrite=lambda name, record: events.append(
                ("confirm", name)),
            say=events.append,
        )
        _bind(host, "_current_slot_name", "_factory_target_channel",
              "_save_user_preset_clicked", "_store_user_preset")
        return host, events

    def test_save_stores_the_selected_channel_on_the_computer(self):
        host, events = self._host()
        host._save_user_preset_clicked(None)
        self.assertEqual(events[0], ("saved", "Warm", {
            "preset_name": "Warm", "base": "Broadcast", "target": 2,
            "strict": False}))
        self.assertIn("refreshed", events)

    def test_saving_over_an_existing_name_asks_first(self):
        host, events = self._host(existing=("Warm",))
        host._save_user_preset_clicked(None)
        self.assertEqual(events, [("confirm", "Warm")])


class GlobalFxPresetTests(unittest.TestCase):
    def _host(self, armed=True):
        host = SimpleNamespace(
            FX_ORDER=["transformer", "detuner"],
            fx_model=_Selected(1),
            _fx_live_params=lambda: {"detune": 1.0},
            fx_arm=SimpleNamespace(get_active=lambda: armed))
        return _bind(host, "_record_voicefx")

    def test_channel_one_keeps_the_global_fx_state(self):
        model, voicefx = self._host()._record_voicefx(1)
        self.assertEqual(model, "detuner")
        self.assertTrue(voicefx["on"])

    def test_channel_two_keeps_the_same_global_fx_state(self):
        first = self._host()._record_voicefx(1, strict=True)
        second = self._host()._record_voicefx(2, strict=True)
        self.assertEqual(first, second)

    def test_non_strict_host_save_does_not_switch_global_fx_off(self):
        _model, voicefx = self._host()._record_voicefx(2, strict=False)
        self.assertTrue(voicefx["on"])


class PutOnUnitTests(unittest.TestCase):
    def test_channel2_confirmation_does_not_claim_fx_is_owner_bound(self):
        confirmations = []
        host = SimpleNamespace(
            _host_slot_entries=[],
            _prepare_record_slot_store=lambda name, record, relative_slot,
            source: {
                "target": 2, "slot": 2, "relative_slot": relative_slot,
            },
            _confirm=lambda *args: confirmations.append(args),
        )

        io24gtk.Win._put_on_unit(
            host, "Delay", {"voicefx": {"on": True}}, 0, "user:Delay")

        self.assertEqual(len(confirmations), 1)
        body = confirmations[0][1]
        self.assertNotIn("Channel 2 blocks do not load", body)
        self.assertNotIn("effect will not play", body)

    def test_the_chosen_block_is_written_without_a_fake_host_recall(self):
        host = SimpleNamespace(
            ctl=SimpleNamespace(dev=object(),
                                snap={"alive": True, "preset_slot": [0, 2]}),
            factory_target=_Selected(0),
            PRESET_BASE=io24gtk.Win.PRESET_BASE)
        _bind(host, "_prepare_slot_store_target", "_factory_target_channel",
              "_prepare_record_slot_store")
        with mock.patch.object(io24gtk, "complete_device_slot_record",
                               side_effect=lambda record: dict(record)):
            plan = host._prepare_record_slot_store(
                "Warm", _record("old"), relative_slot=1, source="user:Warm")
        self.assertEqual((plan["target"], plan["slot"], plan["relative_slot"]),
                         (1, 1, 1))
        self.assertFalse(plan["activate"])
        self.assertEqual(plan["record"]["preset_name"], "Warm")
        self.assertEqual(plan["source"], "user:Warm")

    def test_the_block_now_playing_is_refused(self):
        host = SimpleNamespace(
            ctl=SimpleNamespace(dev=object(),
                                snap={"alive": True, "preset_slot": [0, 2]}),
            factory_target=_Selected(0),
            PRESET_BASE=io24gtk.Win.PRESET_BASE)
        _bind(host, "_prepare_slot_store_target", "_factory_target_channel")
        with self.assertRaises(io24.HostActionError):
            host._prepare_slot_store_target(relative_slot=0)


class PlayingMarkerTests(unittest.TestCase):
    def test_the_selected_unit_block_is_marked_playing(self):
        rows = {0: (_Row(), "Host-written candidate · Channel 1, block 1"),
                1: (_Row(), "Host-written candidate · Channel 1, block 2")}
        host = _bind(SimpleNamespace(_unit_preset_rows=rows),
                     "_mark_playing_unit_blocks")
        host._mark_playing_unit_blocks([1, 2])
        self.assertEqual(rows[1][0].subtitle,
                         "Host-written candidate · Channel 1, block 2 · Playing")
        self.assertEqual(rows[0][0].subtitle,
                         "Host-written candidate · Channel 1, block 1")


if __name__ == "__main__":
    unittest.main()
