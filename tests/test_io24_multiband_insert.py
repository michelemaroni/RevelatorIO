#!/usr/bin/env python3
"""Hardware-free contracts for Multiband as a compressor type.

Asked for on 2026-09-11: the multiband as "just another type of compression
that can be selected, replacing the normal compressor block", placed right
after the Fat Channel, with the processed input coming back on USB playback
1-2. The Host takes each input from USB capture (after the unit's limiter),
runs the 4-band chain, publishes "io24 Input 1+2 Multiband", and plays the
processed inputs back in place of their own mixer feeds.

Nothing here opens USB or starts PipeWire.
"""

import ast
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import io24
import io24_mbc
import io24gtk

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = io24_mbc.default_snapshot()


def _function(name):
    source = (ROOT / "io24gtk.py").read_text()
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


def _bind(host, *names):
    for name in names:
        setattr(host, name, getattr(io24gtk.Win, name).__get__(host))
    return host


def _modules(conf):
    """The args object of every module in a rendered PipeWire config."""
    decoder = json.JSONDecoder()
    found, at = [], 0
    while True:
        at = conf.find("args = ", at)
        if at < 0:
            return found
        args, end = decoder.raw_decode(conf, at + len("args = "))
        found.append(args)
        at = end


def _links(graph):
    return {(link["output"], link["input"]) for link in graph["links"]}


class _MixerIo24(io24.Io24):
    """The driver's real send model; block-100 writes are recorded, not sent."""

    def __init__(self):
        self._shadow = {}
        self._shadow_dirty = False
        self._shadow_flushed = 0.0
        self._shadow_persist = False
        self._send_state = None
        self.wire = []
        self.calls = []

    def _write_mix(self, source, bus, gain_db):
        self.wire.append((source, bus, gain_db))

    def last(self, source, bus):
        return [w[2] for w in self.wire if w[:2] == (source, bus)][-1]

    def set_compressor(self, channel, **kwargs):
        self.calls.append(("set_compressor", channel))

    def compressor_off(self, channel):
        self.calls.append(("compressor_off", channel))


class _Control:
    """A stand-in for one GTK row or scale."""

    def __init__(self, value=0.0, active=False, selected=0):
        self.value, self.active, self.selected = value, active, selected
        self.sensitive = True
        self.visible = True
        self.visible_child_name = None
        self.subtitle = None
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

    def set_subtitle(self, subtitle):
        self.subtitle = subtitle

    def queue_draw(self):
        self.draws += 1


def _controls():
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


def _strip(model=0, comp=False):
    return {"model": _Control(selected=model), "comp_on": _Control(active=comp),
            "comp_curve": _Control()}


class _Insert:
    """InsertChain without PipeWire."""

    def __init__(self, ok=True):
        self.ok = ok
        self.starts, self.stops = [], 0
        self.channels, self._running = (), False
        self.multiband_channels, self.delay_channels = (), ()
        self.playback_guard = None
        self.playback_restores = 0
        self.last_error = "the io24 Input Multiband nodes did not appear"

    @property
    def running(self):
        return self._running

    def start(self, states, capture, sink, sample_rate=48000.0, delays=None,
              **_kwargs):
        self.starts.append((sorted(states), capture, sink))
        if self.ok:
            self._running = True
            self.playback_guard = {
                "node": sink, "restore_muted": True}
            self.multiband_channels = tuple(sorted(states))
            self.delay_channels = tuple(sorted(delays or {}))
            self.channels = tuple(sorted(
                set(self.multiband_channels) | set(self.delay_channels)))
        return self.ok

    def stop(self, restore_playback=True):
        self.stops += 1
        self._running, self.channels = False, ()
        self.multiband_channels, self.delay_channels = (), ()
        if restore_playback:
            self.restore_playback()

    def restore_playback(self):
        if self.playback_guard is None:
            return True
        self.playback_guard = None
        self.playback_restores += 1
        return True


class InsertGraphTests(unittest.TestCase):
    def test_one_channel_is_processed_and_the_other_passes_straight_through(self):
        graph = io24_mbc.build_insert_graph({1: DEFAULT})
        names = [node["name"] for node in graph["nodes"]]
        links = _links(graph)
        self.assertIn("in1_split", names)
        self.assertFalse(any(name.startswith("in2_") for name in names))
        self.assertIn(("src0:Out", "in1_split:In"), links)
        self.assertIn(("in1_sum:Out", "out1:In"), links)
        self.assertIn(("src1:Out", "out2:In"), links)
        self.assertEqual(graph["outputs"], ["out1:Out", "out2:Out"])
        self.assertEqual(len(graph["inputs"]), 6)

    def test_both_channels_get_their_own_complete_graph(self):
        graph = io24_mbc.build_insert_graph({1: DEFAULT, 2: DEFAULT})
        names = [node["name"] for node in graph["nodes"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn(("src1:Out", "in2_split:In"), _links(graph))
        for output, inp in _links(graph):
            self.assertIn(output.split(":")[0], names)
            self.assertIn(inp.split(":")[0], names)

    def test_each_channel_starts_from_its_own_settings(self):
        state = io24_mbc.default_snapshot()
        state["bands"]["low"]["standard"]["threshold_db"] = -31.0
        graph = io24_mbc.build_insert_graph({2: state}, with_comp=True)
        nodes = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(
            nodes["in2_c0"]["control"]["Threshold level (dB)"], -31.0)

    def test_the_return_carries_only_the_processed_inputs_centred(self):
        graph = io24_mbc.build_insert_return_graph([2])
        mix = next(node for node in graph["nodes"] if node["name"] == "mix")
        self.assertEqual(mix["control"], {"Gain 1": 0.0, "Gain 2": 1.0})
        self.assertLessEqual({("mix:Out", "ret_l:In"), ("mix:Out", "ret_r:In")},
                             _links(graph))

    def test_channels_other_than_one_and_two_are_refused(self):
        for states in ({}, {3: DEFAULT}, {True: DEFAULT}, None):
            with self.subTest(states=states):
                with self.assertRaises(ValueError):
                    io24_mbc.build_insert_graph(states)
        with self.assertRaises(ValueError):
            io24_mbc.build_insert_return_graph([])

    def test_the_config_follows_the_io24_and_never_falls_back(self):
        conf = io24_mbc.build_insert_conf(
            {1: DEFAULT}, "alsa_input.io24", "alsa_output.io24",
            with_comp=True)
        process, ret = _modules(conf)
        self.assertEqual(process["capture.props"]["target.object"],
                         "alsa_input.io24")
        self.assertEqual(process["capture.props"]["audio.channels"], 6)
        self.assertEqual(process["playback.props"]["media.class"],
                         "Audio/Source")
        self.assertEqual(process["playback.props"]["node.name"],
                         io24_mbc.INSERT_SOURCE_NAME)
        self.assertEqual(process["playback.props"]["node.description"],
                         "io24 Input 1+2 Multiband")
        self.assertEqual(ret["capture.props"]["target.object"],
                         io24_mbc.INSERT_SOURCE_NAME)
        self.assertEqual(ret["playback.props"]["target.object"],
                         "alsa_output.io24")
        self.assertEqual(ret["playback.props"]["audio.position"], ["FL", "FR"])
        self.assertFalse(ret["playback.props"]["channelmix.upmix"])
        for props in (process["capture.props"], ret["capture.props"],
                      ret["playback.props"]):
            self.assertTrue(props["node.dont-fallback"])

    def test_the_config_needs_both_io24_nodes(self):
        for capture, sink in ((None, "alsa_output.io24"),
                              ("alsa_input.io24", None)):
            with self.assertRaises(ValueError):
                io24_mbc.build_insert_conf({1: DEFAULT}, capture, sink,
                                           with_comp=True)

    def test_the_return_is_the_mixers_usb_playback_1_2(self):
        self.assertEqual(io24_mbc.INSERT_RETURN_SOURCE, "return/ch1")
        self.assertIn(("return/ch1", "USB playback 1-2"), io24gtk.Win.SOURCES)
        self.assertIn(io24_mbc.INSERT_RETURN_SOURCE, io24.Io24.MIXER_SOURCES)


class PlaybackGuardTests(unittest.TestCase):
    def test_a_muted_return_is_opened_then_restored_without_changing_volume(self):
        commands = []
        get_volume = iter(("Volume: 0.40 [MUTED]\n", "Volume: 0.40\n"))

        def run(command, **_kwargs):
            commands.append(command)
            if command[1] == "get-volume":
                return SimpleNamespace(returncode=0, stdout=next(get_volume),
                                       stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(io24_mbc, "node_id", return_value="42"), \
                mock.patch.object(io24_mbc.shutil, "which",
                                  return_value="/usr/bin/wpctl"), \
                mock.patch.object(io24_mbc.subprocess, "run", side_effect=run):
            guard = io24_mbc.acquire_playback("alsa_output.io24")
            self.assertEqual(guard, {
                "node": "alsa_output.io24", "restore_muted": True})
            self.assertTrue(io24_mbc.release_playback(guard))

        self.assertEqual(commands, [
            ["wpctl", "get-volume", "42"],
            ["wpctl", "set-mute", "42", "0"],
            ["wpctl", "get-volume", "42"],
            ["wpctl", "set-mute", "42", "1"],
        ])
        self.assertFalse(any("set-volume" in command for command in commands))

    def test_zero_volume_is_rejected_instead_of_silently_rerouting_the_input(self):
        result = SimpleNamespace(returncode=0, stdout="Volume: 0.00\n",
                                 stderr="")
        with mock.patch.object(io24_mbc, "node_id", return_value="42"), \
                mock.patch.object(io24_mbc.shutil, "which",
                                  return_value="/usr/bin/wpctl"), \
                mock.patch.object(io24_mbc.subprocess, "run",
                                  return_value=result):
            with self.assertRaisesRegex(RuntimeError, "volume is zero"):
                io24_mbc.acquire_playback("alsa_output.io24")

    def test_failed_unmute_confirmation_rolls_the_original_mute_back(self):
        commands = []
        reads = iter((
            SimpleNamespace(returncode=0,
                            stdout="Volume: 0.40 [MUTED]\n", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="gone"),
        ))

        def run(command, **_kwargs):
            commands.append(command)
            if command[1] == "get-volume":
                return next(reads)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(io24_mbc, "node_id", return_value="42"), \
                mock.patch.object(io24_mbc.shutil, "which",
                                  return_value="/usr/bin/wpctl"), \
                mock.patch.object(io24_mbc.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "could not be read"):
                io24_mbc.acquire_playback("alsa_output.io24")

        self.assertEqual(commands[-1],
                         ["wpctl", "set-mute", "42", "1"])

class InsertStateTests(unittest.TestCase):
    def test_the_default_state_is_valid_and_survives_json(self):
        state = io24_mbc.default_insert_state()
        self.assertEqual(
            io24_mbc.validate_insert_state(json.loads(json.dumps(state))),
            state)

    def test_bad_states_are_refused(self):
        good = io24_mbc.default_insert_state()
        cases = [
            dict(good, version=3),
            dict(good, channels={"1": good["channels"]["1"]}),
            dict(good, quantum_before=True),
            dict(good, quantum_before=-1),
            dict(good, routing={"moved": {"3": []}, "return_prior": {}}),
            dict(good, routing={"moved": {"1": ["main", "main"]},
                                "return_prior": {}}),
            dict(good, routing={"moved": {}, "return_prior": {
                "main": {"assigned": 1, "off": False}}}),
            {key: value for key, value in good.items() if key != "routing"},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    io24_mbc.validate_insert_state(case)

    def test_host_files_accept_multiband_and_the_hosts_own_features(self):
        host_delay = {
            "version": 1,
            "target": 2,
            "state": {"on": True, "time_s": 0.173,
                      "feedback": 0.25, "mix": 0.8},
        }
        features = {
            "multiband_insert": io24_mbc.default_insert_state(),
            "reverb_movement": {"version": 1, "enabled": False,
                                "depth": 0.1},
            "autogain": {"version": 1, "on": [1]},
            "voicefx_delay": host_delay,
        }
        normalized, migrations = io24._normalise_host_features(features)
        self.assertEqual(normalized, features)
        self.assertEqual(migrations, [])

    def test_host_delay_survives_a_full_setup_save_load_round_trip(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False

            def read_params(self):
                return {}

        feature = {
            "version": 1,
            "target": 1,
            "state": {"on": True, "time_s": 0.19,
                      "feedback": 0.4, "mix": 0.7},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host-delay.json"
            SnapshotIo24().save_preset(
                path, host_features={"voicefx_delay": feature})
            restored = SnapshotIo24()
            restored.load_preset(path)

        self.assertEqual(
            restored._last_preset_load_report["host_features"],
            {"voicefx_delay": feature})

    def test_a_snapshot_carrying_reverb_movement_saves_and_loads(self):
        class SnapshotIo24(io24.Io24):
            def __init__(self):
                self._shadow = {}
                self._shadow_dirty = False
                self._shadow_flushed = 0.0
                self._shadow_persist = False

            def read_params(self):
                return {}

        features = {"reverb_movement": {"version": 1, "enabled": True,
                                        "depth": 0.2},
                    "autogain": {"version": 1, "on": [2]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.json"
            saved = SnapshotIo24().save_preset(path, host_features=features)
            self.assertEqual(saved["host_features"], features)
            restored = SnapshotIo24()
            restored.load_preset(path)
            self.assertEqual(
                restored._last_preset_load_report["host_features"], features)

    def test_legacy_character_state_keeps_only_its_movement(self):
        normalized, migrations = io24._normalise_host_features({
            "reverb_character": {
                "version": 1, "type": 5, "movement": True,
                "movement_depth": 0.2,
            },
        })
        self.assertEqual(normalized, {
            "reverb_movement": {"version": 1, "enabled": True,
                                "depth": 0.2},
        })
        self.assertEqual(migrations, [
            "legacy reverb Character was removed; Movement was retained",
        ])

    def test_a_host_feature_that_is_not_an_object_is_refused(self):
        with self.assertRaisesRegex(ValueError, "reverb_movement"):
            io24._normalise_host_features({"reverb_movement": 2})

    def test_a_snapshot_file_does_not_keep_this_sessions_changes(self):
        state = io24_mbc.default_insert_state()
        state["routing"] = {"moved": {"1": ["main"]}, "return_prior": {}}
        state["quantum_before"] = 0
        autogain = {"version": 1, "on": [1]}
        kept = io24gtk.snapshot_host_features(
            {"multiband_insert": state, "autogain": autogain})
        self.assertIsNone(kept["multiband_insert"]["routing"])
        self.assertIsNone(kept["multiband_insert"]["quantum_before"])
        self.assertEqual(kept["autogain"], autogain)
        self.assertEqual(state["routing"]["moved"], {"1": ["main"]})


class RoutingTests(unittest.TestCase):
    def test_an_input_moves_onto_the_return_in_every_bus_it_was_in(self):
        dev = _MixerIo24()
        routing = io24_mbc.route_insert(dev, 1)
        self.assertEqual(routing, {"moved": {"1": ["main", "mixa", "mixb"]},
                                   "return_prior": {}})
        for bus in io24_mbc.INSERT_BUSES:
            self.assertEqual(dev.last("return/ch1", bus), 0.0)
            self.assertIsNone(dev.last("line/ch1", bus))
            self.assertFalse(dev.send_assigned("line/ch1", bus))

    def test_undoing_it_gives_the_input_its_own_level_back(self):
        dev = _MixerIo24()
        dev.set_send_db("line/ch1", "main", -6.0)
        routing = io24_mbc.unroute_insert(
            dev, 1, io24_mbc.route_insert(dev, 1))
        self.assertEqual(routing, {"moved": {}, "return_prior": {}})
        self.assertEqual(dev.last("line/ch1", "main"), -6.0)
        self.assertTrue(dev.send_assigned("line/ch1", "main"))

    def test_a_bus_the_input_was_not_in_is_left_alone(self):
        dev = _MixerIo24()
        dev.set_send_assigned("line/ch2", "mixb", False)
        dev.wire.clear()
        routing = io24_mbc.route_insert(dev, 2)
        self.assertEqual(routing["moved"], {"2": ["main", "mixa"]})
        self.assertFalse([w for w in dev.wire if w[1] == "mixb"])

    def test_an_unassigned_or_silent_return_is_opened_then_put_back(self):
        dev = _MixerIo24()
        dev.set_send_assigned("return/ch1", "mixa", False)
        dev.set_send_db("return/ch1", "mixb", None)
        routing = io24_mbc.route_insert(dev, 1)
        self.assertEqual(routing["return_prior"], {
            "mixa": {"assigned": False, "off": False},
            "mixb": {"assigned": True, "off": True}})
        self.assertEqual(dev.last("return/ch1", "mixa"), 0.0)
        self.assertEqual(dev.last("return/ch1", "mixb"), 0.0)
        io24_mbc.unroute_insert(dev, 1, routing)
        self.assertIsNone(dev.last("return/ch1", "mixa"))
        self.assertFalse(dev.send_assigned("return/ch1", "mixa"))
        self.assertIsNone(dev.last("return/ch1", "mixb"))
        self.assertIsNone(dev.send_db("return/ch1", "mixb"))

    def test_a_return_two_inputs_share_goes_back_only_after_both(self):
        dev = _MixerIo24()
        dev.set_send_assigned("return/ch1", "mixa", False)
        routing = io24_mbc.route_insert(dev, 1)
        routing = io24_mbc.route_insert(dev, 2, routing)
        routing = io24_mbc.unroute_insert(dev, 1, routing)
        self.assertTrue(dev.send_assigned("return/ch1", "mixa"))
        self.assertIn("mixa", routing["return_prior"])
        routing = io24_mbc.unroute_insert(dev, 2, routing)
        self.assertFalse(dev.send_assigned("return/ch1", "mixa"))
        self.assertEqual(routing, {"moved": {}, "return_prior": {}})

    def test_routing_an_input_twice_changes_nothing(self):
        dev = _MixerIo24()
        routing = io24_mbc.route_insert(dev, 1)
        dev.wire.clear()
        self.assertEqual(io24_mbc.route_insert(dev, 1, routing), routing)
        self.assertEqual(dev.wire, [])

    def test_the_record_passed_in_is_never_changed(self):
        dev = _MixerIo24()
        routing = io24_mbc.route_insert(dev, 1)
        frozen = json.loads(json.dumps(routing))
        io24_mbc.unroute_insert(dev, 1, routing)
        self.assertEqual(routing, frozen)

    def test_the_host_gives_every_feed_back_on_the_way_out(self):
        dev = _MixerIo24()
        routing = io24_mbc.route_insert(dev, 2)
        ctl = SimpleNamespace(dev=SimpleNamespace(lock=threading.Lock(),
                                                  dev=dev))
        self.assertIsNone(io24gtk.release_insert_routing(ctl, routing))
        self.assertTrue(dev.send_assigned("line/ch2", "main"))
        # without the unit the record is kept for the next launch to finish
        self.assertEqual(io24gtk.release_insert_routing(
            SimpleNamespace(dev=None), routing), routing)


class _Proc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class InsertChainTests(unittest.TestCase):
    def _chain(self, nodes):
        chain = io24_mbc.InsertChain()
        proc = _Proc()

        def launch(configuration, configured=True):
            chain.proc = proc
            return True
        chain._launch = launch
        chain.START_TIMEOUT_S = 0.05
        patcher = mock.patch.object(io24_mbc, "_present_nodes",
                                    return_value=set(nodes))
        patcher.start()
        self.addCleanup(patcher.stop)
        return chain, proc

    def test_it_is_up_only_once_all_three_nodes_are_present(self):
        chain, _proc = self._chain(io24_mbc.InsertChain.READY_NODES)
        guard = {"node": "alsa_output.io24", "restore_muted": True}
        with mock.patch.object(io24_mbc, "acquire_playback",
                               return_value=guard):
            self.assertTrue(chain.start({2: DEFAULT}, "alsa_input.io24",
                                        "alsa_output.io24", with_comp=True))
        self.assertEqual(chain.channels, (2,))
        self.assertEqual(chain.playback_guard, guard)

    def test_a_missing_node_stops_it_and_says_why(self):
        chain, proc = self._chain({io24_mbc.INSERT_PROCESS_NAME})
        self.assertFalse(chain.start({1: DEFAULT}, "alsa_input.io24",
                                     "alsa_output.io24", with_comp=True))
        self.assertTrue(proc.terminated)
        self.assertEqual(chain.channels, ())
        self.assertIn("did not appear", chain.last_error)

    def test_a_muted_playback_return_that_cannot_open_fails_before_routing(self):
        chain, proc = self._chain(io24_mbc.InsertChain.READY_NODES)
        with mock.patch.object(
                io24_mbc, "acquire_playback",
                side_effect=RuntimeError("the io24 playback sink stayed muted")):
            self.assertFalse(chain.start(
                {1: DEFAULT}, "alsa_input.io24", "alsa_output.io24",
                with_comp=True))
        self.assertTrue(proc.terminated)
        self.assertEqual(chain.channels, ())
        self.assertIn("stayed muted", chain.last_error)

    def test_stopping_can_hold_the_playback_guard_until_the_feed_is_back(self):
        chain, _proc = self._chain(io24_mbc.InsertChain.READY_NODES)
        guard = {"node": "alsa_output.io24", "restore_muted": True}
        with mock.patch.object(io24_mbc, "acquire_playback",
                               return_value=guard), \
                mock.patch.object(io24_mbc, "release_playback",
                                  return_value=True) as release:
            self.assertTrue(chain.start(
                {1: DEFAULT}, "alsa_input.io24", "alsa_output.io24",
                with_comp=True))
            chain.stop(restore_playback=False)
            release.assert_not_called()
            self.assertEqual(chain.playback_guard, guard)
            self.assertTrue(chain.restore_playback())
        release.assert_called_once_with(guard)
        self.assertIsNone(chain.playback_guard)

    def test_live_moves_reach_only_a_running_channel_under_its_prefix(self):
        chain = io24_mbc.InsertChain()
        chain.channels = (1,)
        sent = []
        chain.set_controls = lambda controls: sent.append(controls) or True
        self.assertTrue(chain.set_channel_controls(1, {"c0:Ratio (1:n)": 4.0}))
        self.assertFalse(chain.set_channel_controls(2, {"c0:Ratio (1:n)": 4.0}))
        self.assertEqual(sent, [{"in1_c0:Ratio (1:n)": 4.0}])


class ReconcileTests(unittest.TestCase):
    """The Host keeps the running insert, the unit's mixer and PipeWire in
    line with the Model selector and the Compressor switch."""

    def setUp(self):
        self.pw = []
        self.defaults = []
        self.forced = "0"
        for patcher in (
                mock.patch.object(io24gtk.GLib, "idle_add",
                                  side_effect=lambda fn, *a: fn(*a)),
                mock.patch.object(io24gtk, "pw_settings", side_effect=lambda: {
                    "clock.force-quantum": self.forced}),
                mock.patch.object(io24gtk, "pw_set", side_effect=lambda k, v:
                                  self.pw.append((k, v)) or (True, "")),
                mock.patch.object(io24_mbc, "set_default_input",
                                  side_effect=lambda name:
                                  self.defaults.append(name) or True),
                mock.patch.object(io24_mbc, "find_io24_capture_source",
                                  side_effect=lambda: self.capture),
                mock.patch.object(io24_mbc, "find_io24_sink",
                                  return_value="alsa_output.io24")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.capture = "alsa_input.io24"

    def _host(self, models=(0, 0), comp=(False, False), ok=True,
              missing=None):
        jobs, said = [], []
        host = SimpleNamespace(
            w={1: _strip(models[0], comp[0]), 2: _strip(models[1], comp[1])},
            dyn_by_ch={c: {"gate": False, "comp": comp[c - 1], "lim": False}
                       for c in (1, 2)},
            link_both=False, insert=_Insert(ok), mbc_status={},
            _adopt_mute=False, _insert_routing=None,
            _insert_quantum_before=None, _insert_default_input=False,
            _insert_stale=False, _insert_pending=0,
            ctl=SimpleNamespace(submit=jobs.append), say=said.append,
            _sync_mix_widgets=lambda: False,
            _mbc_snapshot_state=lambda ch: io24_mbc.default_snapshot(True),
            _insert_missing=lambda *_args: missing)
        _bind(host, "_multiband_selected", "_multiband_insert_wanted",
              "_host_delay_states", "_insert_wanted",
              "_insert_reconcile", "_insert_give_up", "_insert_sync_mixer",
              "_insert_lower_quantum", "_insert_restore_system",
              "_insert_show", "_insert_watchdog")
        return host, jobs, said

    def _started(self):
        """Input 1 on Multiband with its Compressor on, reconciled and run."""
        host, jobs, _said = self._host(models=(3, 0), comp=(True, False))
        host._insert_reconcile()
        dev = _MixerIo24()
        for job in jobs:
            job(dev)
        return host, jobs, dev

    def test_multiband_with_the_compressor_on_starts_and_takes_the_feed(self):
        host, _jobs, dev = self._started()
        self.assertEqual(host.insert.starts,
                         [([1], "alsa_input.io24", "alsa_output.io24")])
        self.assertEqual(self.pw, [("clock.force-quantum", 128)])
        self.assertEqual(self.defaults, [io24_mbc.INSERT_SOURCE_NAME])
        self.assertEqual(host._insert_routing["moved"],
                         {"1": ["main", "mixa", "mixb"]})
        self.assertFalse(dev.send_assigned("line/ch1", "main"))
        self.assertEqual(host._insert_pending, 0)

    def test_switching_it_off_gives_back_the_feed_buffer_and_input(self):
        host, jobs, dev = self._started()
        self.pw.clear()
        host.dyn_by_ch[1]["comp"] = False
        host._insert_reconcile()
        # Keep the return open until the asynchronous device job has restored
        # the input's direct mixer feed; otherwise switching off creates a mute
        # gap and an audible click.
        self.assertEqual(host.insert.playback_restores, 0)
        for job in jobs[1:]:
            job(dev)
        self.assertFalse(host.insert.running)
        self.assertIsNone(host._insert_routing)
        self.assertTrue(dev.send_assigned("line/ch1", "main"))
        self.assertEqual(self.pw, [("clock.force-quantum", 0)])
        self.assertEqual(self.defaults[-1], "alsa_input.io24")
        self.assertEqual(host.insert.playback_restores, 1)

    def test_an_already_small_buffer_is_left_alone(self):
        self.forced = "64"
        host, _jobs, _said = self._host(models=(3, 0), comp=(True, False))
        host._insert_reconcile()
        self.assertEqual(self.pw, [])
        self.assertIsNone(host._insert_quantum_before)

    def test_a_failed_start_switches_the_compressor_off_and_says_why(self):
        host, jobs, said = self._host(models=(0, 3), comp=(False, True),
                                      ok=False)
        host._insert_reconcile()
        self.assertFalse(host.dyn_by_ch[2]["comp"])
        self.assertFalse(host.w[2]["comp_on"].active)
        self.assertIn("did not start", said[-1])
        self.assertEqual(jobs, [])          # nothing was moved, nothing to undo
        self.assertEqual(self.pw, [])

    def test_a_computer_that_cannot_build_the_processor_is_told_so(self):
        host, _jobs, said = self._host(
            models=(3, 0), comp=(True, False),
            missing="cannot build Host compressor: no C compiler found")
        host._insert_reconcile()
        self.assertEqual(host.insert.starts, [])
        self.assertIn("no C compiler", said[-1])
        self.assertFalse(host.dyn_by_ch[1]["comp"])

    def test_without_the_io24s_audio_it_waits_with_each_feed_in_place(self):
        self.capture = None
        host, jobs, _said = self._host(models=(3, 0), comp=(True, False))
        dev = _MixerIo24()
        host._insert_routing = io24_mbc.route_insert(dev, 1)
        host._insert_reconcile()
        for job in jobs:
            job(dev)
        self.assertEqual(host.insert.starts, [])
        self.assertIsNone(host._insert_routing)
        self.assertTrue(dev.send_assigned("line/ch1", "main"))
        self.assertTrue(host.dyn_by_ch[1]["comp"])      # still wanted
        self.assertEqual(self.pw, [])

    def test_the_watchdog_starts_what_is_wanted_once_it_can(self):
        self.capture = None
        host, _jobs, _said = self._host(models=(3, 0), comp=(True, False))
        host._insert_watchdog()
        self.assertEqual(host.insert.starts, [])
        self.capture = "alsa_input.io24"
        self.assertTrue(host._insert_watchdog())
        self.assertEqual(host.insert.channels, (1,))

    def test_jobs_queued_back_to_back_end_in_the_last_state(self):
        host, jobs, _said = self._host(models=(3, 0), comp=(True, False))
        host._insert_reconcile()
        host.dyn_by_ch[1]["comp"] = False
        host._insert_reconcile()
        dev = _MixerIo24()
        for job in jobs:                    # run only now, in order
            job(dev)
        self.assertIsNone(host._insert_routing)
        self.assertTrue(dev.send_assigned("line/ch1", "main"))


class HostTests(unittest.TestCase):
    def test_the_model_list_offers_multiband(self):
        self.assertEqual(io24gtk.COMP_MODELS[io24gtk.MULTIBAND_MODEL],
                         "multiband")
        self.assertIn('["Standard", "Tube", "FET", "Multiband"]',
                      _function("_channel_column"))

    def _push_host(self, model):
        jobs, reconciled = [], []
        host = SimpleNamespace(
            w={1: _strip(model, True)},
            dyn_by_ch={1: {"gate": False, "comp": True, "lim": False}},
            link_both=False, _adopt_mute=False, racks={}, _fs=48000.0,
            ctl=SimpleNamespace(submit=jobs.append),
            _compressor_kwargs=lambda ch: (0, {"threshold_db": -20.0}),
            _insert_reconcile=lambda: reconciled.append(True))
        _bind(host, "_dyn_push", "_multiband_selected")
        return host, jobs, reconciled

    def test_the_units_compressor_goes_off_under_multiband(self):
        host, jobs, reconciled = self._push_host(io24gtk.MULTIBAND_MODEL)
        host._dyn_push("comp", 1)
        dev = _MixerIo24()
        jobs[0](dev)
        self.assertEqual(dev.calls, [("compressor_off", 1)])
        self.assertEqual(reconciled, [True])

    def test_a_device_model_still_switches_the_units_compressor_on(self):
        host, jobs, _reconciled = self._push_host(0)
        host._dyn_push("comp", 1)
        dev = _MixerIo24()
        jobs[0](dev)
        self.assertEqual(dev.calls, [("set_compressor", 1)])

    def _adopt_host(self, routing=None):
        host = SimpleNamespace(
            w={1: _strip(), 2: _strip()},
            dyn_by_ch={c: {"gate": False, "comp": False, "lim": False}
                       for c in (1, 2)},
            mbc_ctl={1: _controls(), 2: _controls()}, insert=_Insert(),
            _mbc_mute=False, _adopt_mute=False, _insert_routing=routing,
            _insert_quantum_before=None, _insert_stale=False)
        return _bind(host, "_adopt_insert_state", "_adopt_mbc_controls",
                     "_mbc_band_state")

    def _saved(self):
        state = io24_mbc.default_insert_state()
        state["channels"]["2"]["enabled"] = True
        state["routing"] = {"moved": {"2": ["main"]}, "return_prior": {}}
        state["quantum_before"] = 256
        return state

    def test_a_session_puts_multiband_back_where_it_was_on(self):
        host = self._adopt_host()
        message = host._adopt_insert_state(self._saved())
        self.assertEqual(message, "Multiband restored on Input 2")
        self.assertEqual(host.w[2]["model"].selected, io24gtk.MULTIBAND_MODEL)
        self.assertTrue(host.w[2]["comp_on"].active)
        self.assertTrue(host.dyn_by_ch[2]["comp"])
        self.assertEqual(host.w[1]["model"].selected, 0)
        self.assertEqual(host._insert_routing["moved"], {"2": ["main"]})
        self.assertEqual(host._insert_quantum_before, 256)
        self.assertTrue(host._insert_stale)
        self.assertFalse(host._adopt_mute)

    def test_a_host_with_its_own_routing_keeps_it(self):
        own = {"moved": {"1": ["mixa"]}, "return_prior": {}}
        host = self._adopt_host(routing=own)
        host._adopt_insert_state(self._saved())
        self.assertIs(host._insert_routing, own)
        self.assertIsNone(host._insert_quantum_before)

    def test_the_session_and_snapshots_record_multiband(self):
        state = io24_mbc.default_insert_state()
        host = _bind(SimpleNamespace(
            _insert_state=lambda: state, _reverb_movement_state=lambda: None,
            _autogain_on={}), "_host_features_state")
        self.assertEqual(host._host_features_state(),
                         {"multiband_insert": state})

    def test_the_insert_state_carries_settings_routing_and_buffer(self):
        routing = {"moved": {"1": ["main"]}, "return_prior": {}}
        host = _bind(SimpleNamespace(
            mbc_ctl={1: object(), 2: object()},
            _mbc_snapshot_state=lambda ch: io24_mbc.default_snapshot(ch == 1),
            _insert_routing=routing, _insert_quantum_before=0),
            "_insert_state")
        state = host._insert_state()
        self.assertTrue(state["channels"]["1"]["enabled"])
        self.assertFalse(state["channels"]["2"]["enabled"])
        self.assertEqual(state["routing"], routing)
        self.assertEqual(state["quantum_before"], 0)

    def test_the_off_the_unit_reports_under_multiband_is_not_adopted(self):
        source = _function("_after_load")
        self.assertIn('comp.get("on") or\n', source)
        self.assertIn("not self._multiband_selected(ch)", source)
        self.assertLess(source.index("_adopt_insert_state("),
                        source.index("self._insert_reconcile()"))

    def test_a_block_saved_under_multiband_keeps_the_units_compressor_off(self):
        source = (ROOT / "io24gtk.py").read_text()
        self.assertIn('compressor["on"] = (self.dyn_by_ch[target]["comp"]\n'
                      '                            and not '
                      'self._multiband_selected(target))', source)

    def test_a_loaded_preset_replaces_multiband(self):
        self.assertIn("self._insert_reconcile()",
                      _function("_adopt_preset_record"))

    def test_shutdown_gives_everything_back_before_the_session_is_saved(self):
        source = _function("do_shutdown")
        self.assertLess(source.index("_insert_shutdown()"),
                        source.index("_save_last_session"))

    def test_a_snapshot_file_is_saved_without_this_sessions_routing(self):
        self.assertIn("snapshot_host_features(", _function("_pick"))



class CrossoverTests(unittest.TestCase):
    """The four bands must add back up to the input before any compression."""

    @staticmethod
    def _response(node, f, fs=48000.0):
        import cmath
        import math
        w = 2 * math.pi * node["control"]["Freq"] / fs
        c, s = math.cos(w), math.sin(w)
        alpha = s / (2 * node["control"]["Q"])
        if node["label"] == "bq_lowpass":
            b = ((1 - c) / 2, 1 - c, (1 - c) / 2)
        elif node["label"] == "bq_highpass":
            b = ((1 + c) / 2, -(1 + c), (1 + c) / 2)
        else:
            b = (1 - alpha, -2 * c, 1 + alpha)
        a = (1 + alpha, -2 * c, 1 - alpha)
        z = cmath.exp(-2j * math.pi * f / fs)
        return ((b[0] + b[1] * z + b[2] * z * z) /
                (a[0] + a[1] * z + a[2] * z * z))

    def test_the_bands_sum_flat(self):
        import math
        for xovers in ([150.0, 800.0, 4500.0], [60.0, 300.0, 2000.0],
                       [400.0, 2500.0, 12000.0]):
            state = io24_mbc.default_snapshot()
            state["xovers"] = xovers
            graph = io24_mbc.build_graph(None, False, state=state)
            nodes = {node["name"]: node for node in graph["nodes"]}
            legs = [[nodes["b%d_%d" % (band, index)]
                     for index in range(len(leg))]
                    for band, leg in enumerate(io24_mbc.CROSSOVER_LEGS)]
            worst = 0.0
            for step in range(240):
                f = 20.0 * (1000.0 ** (step / 239.0))
                total = 0
                for leg in legs:
                    h = 1
                    for node in leg:
                        h *= self._response(node, f)
                    total += h
                worst = max(worst, abs(20 * math.log10(abs(total))))
            with self.subTest(xovers=xovers):
                self.assertLess(worst, 0.01)

    def test_each_split_moves_every_filter_tuned_to_it(self):
        state = io24_mbc.default_snapshot()
        graph = io24_mbc.build_graph(None, False, state=state)
        nodes = {node["name"]: node for node in graph["nodes"]}
        tuned = set()
        for split, names in enumerate(io24_mbc.crossover_nodes()):
            for name in names:
                self.assertEqual(nodes[name]["control"]["Freq"],
                                 state["xovers"][split])
                self.assertAlmostEqual(nodes[name]["control"]["Q"],
                                       0.7071067811865476)
            tuned.update(names)
        biquads = {node["name"] for node in graph["nodes"]
                   if node["label"].startswith("bq_")}
        self.assertEqual(tuned, biquads)
        self.assertIn("b0_3", io24_mbc.crossover_nodes()[2])

if __name__ == "__main__":
    unittest.main()
