#!/usr/bin/env python3
"""Hardware-free contracts for picking up where the last session left off.

The user asked (2026-09-11) that the Host always resume where it was, as
Universal Control did. The io24 keeps its gains, selected block, enable and
stereo link through a power cycle and loses every write-only DSP setting. So
on each connection the Host re-sends what it last sent, except what the unit
reports itself, and restores its own Host-only features once per launch.

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
import io24gtk

ROOT = Path(__file__).resolve().parents[1]


def _function(name):
    source = (ROOT / "io24gtk.py").read_text()
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


def _bind(host, *names):
    for name in names:
        setattr(host, name, getattr(io24gtk.Win, name).__get__(host))
    return host


class _ReplayIo24(io24.Io24):
    """Records replayed setters without opening USB."""

    def __init__(self, shadow):
        self._shadow = dict(shadow)
        self._shadow_dirty = False
        self._shadow_flushed = 0.0
        self._shadow_persist = False
        self._send_state = None
        self.sent = []

    def set_mute(self, **kwargs):
        self.sent.append("set_mute")

    def set_highpass(self, **kwargs):
        self.sent.append("set_highpass")

    def set_channel_link(self, **kwargs):
        self.sent.append("set_channel_link")


def _replay_shadow():
    return {
        "set_mute#channel=1": {"fn": "set_mute",
                               "kwargs": {"channel": 1, "on": True}},
        "set_highpass#channel=1": {"fn": "set_highpass",
                                   "kwargs": {"channel": 1, "on": True}},
        "set_channel_link": {"fn": "set_channel_link",
                             "kwargs": {"on": False}},
    }


class ReplaySkipTests(unittest.TestCase):
    def test_settings_the_unit_reports_are_not_replayed(self):
        dev = _ReplayIo24(_replay_shadow())
        report = dev.reapply_shadow(skip=io24gtk.RESUME_SKIP)
        self.assertEqual(dev.sent, ["set_highpass"])
        self.assertEqual(report["skipped_by_request"], 2)

    def test_a_plain_reapply_still_sends_everything(self):
        dev = _ReplayIo24(_replay_shadow())
        dev.reapply_shadow()
        self.assertEqual(sorted(dev.sent),
                         ["set_channel_link", "set_highpass", "set_mute"])

    def test_resume_also_skips_legacy_fx_owner_routing(self):
        self.assertEqual(set(io24gtk.RESUME_SKIP),
                         {"set_mute", "set_hp_mute", "set_channel_link",
                          "set_processing_channel"})


class LastSessionFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "last-session.json"

    def _host(self, features):
        return _bind(SimpleNamespace(_host_features_state=lambda: features),
                     "_save_last_session")

    def test_the_session_round_trips(self):
        features = {"reverb_movement": {"version": 1, "enabled": True,
                                        "depth": 0.08}}
        self.assertTrue(self._host(features)._save_last_session(self.path))
        self.assertEqual(io24gtk.load_last_session(self.path),
                         {"version": 1, "host_features": features})

    def test_an_unchanged_session_is_not_rewritten(self):
        host = self._host({"multiband": {"on": False}})
        self.assertTrue(host._save_last_session(self.path))
        self.assertFalse(host._save_last_session(self.path))

    def test_a_missing_or_damaged_session_is_no_session(self):
        self.assertIsNone(io24gtk.load_last_session(self.path))
        self.path.write_text("{bad")
        self.assertIsNone(io24gtk.load_last_session(self.path))

    def test_host_features_gather_multiband_and_reverb_movement(self):
        host = _bind(SimpleNamespace(
            _insert_state=lambda: {"version": 1},
            _reverb_movement_state=lambda: None), "_host_features_state")
        self.assertEqual(host._host_features_state(),
                         {"multiband_insert": {"version": 1}})

    def test_the_snapshot_dialog_uses_the_same_gathering(self):
        self.assertIn("self._host_features_state()", _function("_pick"))

    def test_96khz_delay_state_is_gathered_outside_the_device_shadow(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get_selected(self):
                return self.value

        delay = {"on": True, "time_s": 0.173,
                 "feedback": 0.25, "mix": 0.8}
        host = _bind(SimpleNamespace(
            FX_ORDER=io24gtk.Win.FX_ORDER,
            _fs=96000.0,
            _selected_rate=96000,
            fx_model=Value(io24gtk.Win.FX_ORDER.index("delay")),
            fx_target=Value(1),
            _fx_live_params=lambda: dict(delay)),
            "_voicefx_effective_rate", "_host_delay_feature_state")

        self.assertEqual(host._host_delay_feature_state(), {
            "version": 1, "target": 2, "state": delay,
        })

    def test_saved_host_delay_adopts_controls_without_claiming_device_owner(self):
        class Value:
            def __init__(self, value=None):
                self.value = value

            def set_selected(self, value):
                self.value = value

            def set_value(self, value):
                self.value = value

            def set_active(self, value):
                self.value = value

        controls = {name: Value() for name in
                    ("time_s", "feedback", "mix")}
        host = _bind(SimpleNamespace(
            FX_ORDER=io24gtk.Win.FX_ORDER,
            _fx_mute=False,
            fx_target=Value(),
            fx_model=Value(),
            fx_arm=Value(),
            fx_params={"delay": controls}),
            "_adopt_host_delay_feature")
        feature = {
            "version": 1, "target": 2,
            "state": {"on": False, "time_s": 0.173,
                      "feedback": 0.25, "mix": 0.8},
        }

        self.assertIsNone(host._adopt_host_delay_feature(feature))
        self.assertEqual(host.fx_target.value, 1)
        self.assertEqual(host.fx_model.value,
                         io24gtk.Win.FX_ORDER.index("delay"))
        self.assertFalse(host.fx_arm.value)
        self.assertEqual({name: control.value
                          for name, control in controls.items()},
                         {"time_s": 0.173, "feedback": 0.25, "mix": 0.8})
        self.assertFalse(hasattr(host, "_fx_last_sent_target"))


class AudioClockPreferenceTests(unittest.TestCase):
    def test_first_launch_defaults_to_safe_48khz_and_512_frames(self):
        self.assertEqual(io24gtk.audio_clock_preference(None), {
            "version": 1,
            "sample_rate": 48000,
            "quantum": 512,
        })

    def test_a_saved_clock_becomes_the_next_launch_preference(self):
        session = {
            "version": 1,
            "audio_clock": {
                "version": 1,
                "sample_rate": 88200,
                "quantum": 256,
            },
        }
        self.assertEqual(io24gtk.audio_clock_preference(session),
                         session["audio_clock"])

    def test_the_session_file_keeps_the_last_user_selection(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "last-session.json"
        host = _bind(SimpleNamespace(
            _selected_rate=96000,
            _selected_quantum=512,
            _host_features_state=lambda: {}),
            "_audio_clock_state", "_save_last_session")

        self.assertTrue(host._save_last_session(path))
        self.assertEqual(io24gtk.load_last_session(path)["audio_clock"], {
            "version": 1,
            "sample_rate": 96000,
            "quantum": 512,
        })

    def test_a_temporary_multiband_quantum_does_not_replace_the_preference(self):
        host = _bind(SimpleNamespace(
            _selected_rate=96000,
            _selected_quantum=512,
            _insert_quantum_before=512),
            "_audio_clock_state")
        self.assertEqual(host._audio_clock_state()["quantum"], 512)

    def test_startup_applies_a_saved_native_delay_safe_rate_and_quantum(self):
        calls = []
        host = _bind(SimpleNamespace(
            _selected_rate=44100,
            _selected_quantum=512,
            _insert_quantum_before=None,
            _set_device_fs=lambda rate: calls.append(("device-fs", rate)),
            say=lambda message: calls.append(("message", message))),
            "_audio_clock_state", "_apply_saved_quantum",
            "_apply_saved_audio_clock",
            "_restore_audio_clock")
        with mock.patch.object(io24gtk, "pw_settings", return_value={
                "clock.allowed-rates": "[ 44100, 48000, 88200, 96000 ]",
                "clock.force-rate": "0",
                "clock.force-quantum": "0",
        }), mock.patch.object(
                io24gtk, "pw_set",
                side_effect=lambda key, value:
                (calls.append((key, value)) or (True, ""))):
            self.assertFalse(host._restore_audio_clock())

        self.assertEqual(calls, [
            ("clock.force-rate", 44100),
            ("device-fs", 44100),
            ("clock.force-quantum", 512),
        ])

    def test_offline_96khz_startup_is_held_at_48khz_until_attach(self):
        calls = []
        host = _bind(SimpleNamespace(
            _selected_rate=96000,
            _selected_quantum=512,
            _insert_quantum_before=None,
            _audio_clock_restore_deferred=False,
            _audio_clock_restore_inflight=False,
            ctl=SimpleNamespace(dev=None),
            _set_device_fs=lambda rate: calls.append(("device-fs", rate)),
            say=lambda message: calls.append(("message", message))),
            "_audio_clock_state", "_safe_delay_transition_rate",
            "_apply_saved_quantum", "_apply_saved_audio_clock",
            "_restore_audio_clock")
        settings = {
            "clock.allowed-rates": "[ 44100, 48000, 88200, 96000 ]",
            "clock.force-rate": "96000",
            "clock.force-quantum": "512",
        }
        with mock.patch.object(io24gtk, "pw_settings", return_value=settings), \
                mock.patch.object(
                    io24gtk, "pw_set",
                    side_effect=lambda key, value:
                    (calls.append((key, value)) or (True, ""))):
            self.assertFalse(host._restore_audio_clock())

        self.assertEqual(calls, [("clock.force-rate", 48000)])
        self.assertTrue(host._audio_clock_restore_deferred)

    def test_offline_882khz_startup_is_also_held_until_attach(self):
        calls = []
        host = _bind(SimpleNamespace(
            _selected_rate=88200,
            _selected_quantum=512,
            _insert_quantum_before=None,
            _audio_clock_restore_deferred=False,
            _audio_clock_restore_inflight=False,
            ctl=SimpleNamespace(dev=None),
            _set_device_fs=lambda rate: calls.append(("device-fs", rate)),
            say=lambda message: calls.append(("message", message))),
            "_audio_clock_state", "_safe_delay_transition_rate",
            "_apply_saved_quantum", "_apply_saved_audio_clock",
            "_restore_audio_clock")
        settings = {
            "clock.allowed-rates": "[ 44100, 48000, 88200, 96000 ]",
            "clock.force-rate": "88200",
            "clock.force-quantum": "512",
        }
        with mock.patch.object(io24gtk, "pw_settings", return_value=settings), \
                mock.patch.object(
                    io24gtk, "pw_set",
                    side_effect=lambda key, value:
                    (calls.append((key, value)) or (True, ""))):
            self.assertFalse(host._restore_audio_clock())

        self.assertEqual(calls, [("clock.force-rate", 48000)])
        self.assertTrue(host._audio_clock_restore_deferred)

    def test_connected_96khz_startup_restore_quiesces_at_old_clock_first(self):
        calls = []

        class Device:
            def quiesce_voicefx_for_host_delay(self, rate, quantum=512):
                calls.append(("quiesce", rate, quantum))

        device = Device()
        host = _bind(SimpleNamespace(
            _selected_rate=96000,
            _selected_quantum=512,
            _insert_quantum_before=None,
            _fs=96000.0,
            _fs_seen=False,
            _host_delay_quiesced_device=None,
            ctl=SimpleNamespace(
                dev=device, submit=lambda function: function(device)),
            _set_device_fs=lambda rate: calls.append(("device-fs", rate)),
            say=lambda message: calls.append(("message", message))),
            "_audio_clock_state", "_transition_clock",
            "_apply_saved_quantum", "_apply_saved_audio_clock",
            "_restore_audio_clock")
        settings = {
            "clock.allowed-rates": "[ 48000, 96000 ]",
            "clock.force-rate": "48000",
            "clock.rate": "48000",
            "clock.force-quantum": "0",
            "clock.quantum": "1024",
        }
        with mock.patch.object(io24gtk, "pw_settings", return_value=settings), \
                mock.patch.object(
                    io24gtk, "pw_set",
                    side_effect=lambda key, value:
                    (calls.append((key, value)) or (True, ""))), \
                mock.patch.object(
                    io24gtk.GLib, "idle_add",
                    side_effect=lambda function, *args: function(*args)), \
                mock.patch.object(io24gtk, "alsa_live", return_value={}):
            self.assertFalse(host._restore_audio_clock())

        self.assertEqual(calls, [
            ("quiesce", 48000.0, 1024),
            ("clock.force-rate", 96000),
            ("device-fs", 96000),
            ("clock.force-quantum", 512),
        ])
        self.assertIs(host._host_delay_quiesced_device, device)

    def test_failed_startup_quiesce_never_requests_96khz(self):
        calls = []

        class Device:
            def quiesce_voicefx_for_host_delay(self, _rate, quantum=512):
                _ = quantum
                raise RuntimeError("control stopped")

        device = Device()
        host = _bind(SimpleNamespace(
            _selected_rate=96000,
            _selected_quantum=512,
            _insert_quantum_before=None,
            _fs=48000.0,
            _fs_seen=True,
            ctl=SimpleNamespace(
                dev=device, submit=lambda function: function(device)),
            _set_device_fs=lambda rate: calls.append(("device-fs", rate)),
            say=lambda message: calls.append(("message", message))),
            "_audio_clock_state", "_transition_clock",
            "_apply_saved_quantum", "_apply_saved_audio_clock",
            "_restore_audio_clock")
        settings = {
            "clock.allowed-rates": "[ 48000, 96000 ]",
            "clock.force-rate": "48000",
            "clock.force-quantum": "512",
        }
        with mock.patch.object(io24gtk, "pw_settings", return_value=settings), \
                mock.patch.object(
                    io24gtk, "pw_set",
                    side_effect=lambda key, value:
                    (calls.append((key, value)) or (True, ""))), \
                mock.patch.object(
                    io24gtk.GLib, "idle_add",
                    side_effect=lambda function, *args: function(*args)), \
                mock.patch.object(io24gtk, "alsa_live", return_value={}):
            self.assertFalse(host._restore_audio_clock())

        self.assertFalse(any(call[0] == "clock.force-rate" for call in calls))
        self.assertIn("safety bypass failed", calls[-1][1])


class ResumeTriggerTests(unittest.TestCase):
    def _host(self):
        calls, self.restarts = [], []
        host = SimpleNamespace(
            _fs_seen=True,
            _resume_session=lambda first: calls.append(first),
            _insert_reconcile=lambda restart=False: self.restarts.append(
                restart))
        return _bind(host, "_maybe_resume"), calls

    def test_each_connection_resumes_once_and_only_the_first_restores_host_features(self):
        host, calls = self._host()
        host._maybe_resume({"alive": True, "attach_generation": 1})
        host._maybe_resume({"alive": True, "attach_generation": 1})
        host._maybe_resume({"alive": True, "attach_generation": 2})
        self.assertEqual(calls, [True, False])

    def test_a_reconnect_restarts_multiband_whose_streams_went_with_the_unit(self):
        # the first connection restores Multiband with the session instead
        host, _calls = self._host()
        host._maybe_resume({"alive": True, "attach_generation": 1})
        self.assertEqual(self.restarts, [])
        host._maybe_resume({"alive": True, "attach_generation": 2})
        self.assertEqual(self.restarts, [True])

    def test_nothing_resumes_while_the_unit_is_not_answering(self):
        host, calls = self._host()
        host._maybe_resume({"alive": False, "attach_generation": 1})
        self.assertEqual(calls, [])

    def test_deferred_96khz_restore_finishes_before_session_replay(self):
        calls = []
        host = _bind(SimpleNamespace(
            _audio_clock_restore_deferred=True,
            _audio_clock_restore_inflight=False,
            _restore_audio_clock=lambda: calls.append("clock"),
            _resume_session=lambda first: calls.append(("resume", first))),
            "_maybe_resume")

        host._maybe_resume({"alive": True, "attach_generation": 1})

        self.assertEqual(calls, ["clock"])
        self.assertFalse(hasattr(host, "_resumed_generation"))

    def test_first_resume_observes_the_live_clock_before_replaying_coefficients(self):
        calls = []
        host = SimpleNamespace(
            _fs_seen=False,
            _resume_session=lambda first: calls.append(("resume", first)),
            _insert_reconcile=lambda restart=False: None,
        )

        def refresh():
            calls.append("clock")
            host._fs = 48000.0
            host._fs_seen = True

        host._refresh_device_page = refresh
        host = _bind(host, "_maybe_resume")

        host._maybe_resume({"alive": True, "attach_generation": 1})

        self.assertEqual(calls, ["clock", ("resume", True)])
        self.assertEqual(host._fs, 48000.0)

    def test_the_tick_resumes_only_after_its_alive_gate(self):
        tick = _function("_tick")
        self.assertLess(tick.index('if not s["alive"]:'),
                        tick.index("self._maybe_resume(s)"))

    def test_the_session_is_saved_on_the_way_out(self):
        self.assertIn("_save_last_session", _function("do_shutdown"))


class _Backend:
    """Stands in for the Io24 driver the Host's worker passes to each job."""

    def __init__(self, shadow):
        self._shadow = shadow
        self.calls = []
        # Like the real driver, it carries its raw USB handle as `.dev`; a
        # resume that unwrapped `.dev` again would reach this and fail.
        self.dev = object()

    def reapply_shadow(self, include_device_preset_state=False, skip=(),
                       sample_rate_hz=None):
        self.calls.append(("reapply", tuple(skip), sample_rate_hz))
        return {"applied": 3, "failed": []}

    def recall_device_slot(self, channel, slot):
        self.calls.append(("recall", channel, slot))
        return {}

    def set_fx_mix(self, channel, value):
        self.calls.append(("set_fx_mix", channel, value))


class ResumeSessionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            io24gtk.GLib, "idle_add", side_effect=lambda fn, *a: fn(*a))
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "last-session.json"
        self.path.write_text(json.dumps({
            "version": 1,
            "host_features": {"reverb_movement": {
                "version": 1, "enabled": False, "depth": 0.08}}}))

    def _host(self, shadow, preset_slot=(0, 2)):
        backend = _Backend(shadow)
        adopted = []
        host = SimpleNamespace(
            ctl=SimpleNamespace(snap={"preset_slot": list(preset_slot)},
                                submit=lambda work: work(backend)),
            _after_load=lambda mirror, features, migrations, message:
            adopted.append((mirror, features, migrations, message)),
            say=lambda _message: None)
        return _bind(host, "_resume_session"), backend, adopted

    def test_first_resume_restores_host_features_and_skips_readable_settings(self):
        host, backend, adopted = self._host({})
        host._resume_session(first=True, path=self.path)
        self.assertEqual(backend.calls, [
            ("reapply", io24gtk.RESUME_SKIP, io24gtk.DEFAULT_SAMPLE_RATE),
        ])
        self.assertEqual(adopted[0][1], {"reverb_movement": {
            "version": 1, "enabled": False, "depth": 0.08}})

    def test_first_resume_migrates_legacy_character_to_movement(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "host_features": {"reverb_character": {
                "version": 1, "type": 4, "movement": True,
                "movement_depth": 0.12,
            }},
        }))
        host, _backend, adopted = self._host({})

        host._resume_session(first=True, path=self.path)

        self.assertEqual(adopted[0][1], {"reverb_movement": {
            "version": 1, "enabled": True, "depth": 0.12}})
        self.assertEqual(adopted[0][2], [
            "legacy reverb Character was removed; Movement was retained",
        ])

    def test_first_resume_restores_the_safe_host_delay_feature(self):
        feature = {
            "version": 1, "target": 2,
            "state": {"on": True, "time_s": 0.173,
                      "feedback": 0.25, "mix": 0.8},
        }
        self.path.write_text(json.dumps({
            "version": 1,
            "host_features": {"voicefx_delay": feature},
        }))
        host, _backend, adopted = self._host({})

        host._resume_session(first=True, path=self.path)

        self.assertEqual(adopted[0][1], {"voicefx_delay": feature})
        self.assertEqual(adopted[0][2], [])

    def test_a_reconnect_restores_the_unit_but_not_host_features(self):
        host, _backend, adopted = self._host({})
        host._resume_session(first=False, path=self.path)
        self.assertIsNone(adopted[0][1])

    def test_a_reconnect_keeps_the_current_96khz_host_delay(self):
        feature = {
            "version": 1, "target": 2,
            "state": {"on": True, "time_s": 0.173,
                      "feedback": 0.25, "mix": 0.8},
        }
        host, _backend, adopted = self._host({})
        host._host_delay_feature_state = lambda: feature

        host._resume_session(first=False, path=self.path)

        self.assertEqual(adopted[0][1], {"voicefx_delay": feature})

    def test_voice_fx_off_loads_no_block(self):
        shadow = {"set_fx": {"fn": "set_fx",
                             "kwargs": {"model": "detuner", "on": False}}}
        host, backend, _adopted = self._host(shadow)
        host._resume_session(first=False, path=self.path)
        self.assertEqual(backend.calls, [
            ("reapply", io24gtk.RESUME_SKIP, io24gtk.DEFAULT_SAMPLE_RATE),
        ])


class AutoGainSessionTests(unittest.TestCase):
    """The user (2026-09-11) asked that each input's Auto gain be remembered."""

    def _gathering_host(self, autogain_on):
        return _bind(SimpleNamespace(
            _insert_state=lambda: None,
            _reverb_movement_state=lambda: None,
            _autogain_on=autogain_on), "_host_features_state")

    def _adopting_host(self):
        switched = {1: [], 2: []}
        toggles = {c: SimpleNamespace(set_active=switched[c].append)
                   for c in (1, 2)}
        host = _bind(SimpleNamespace(autogain_toggles=toggles),
                     "_adopt_autogain")
        return host, switched

    def test_auto_gain_is_remembered_while_it_is_on(self):
        host = self._gathering_host({1: True, 2: False})
        self.assertEqual(host._host_features_state(),
                         {"autogain": {"version": 1, "on": [1]}})

    def test_a_session_without_auto_gain_is_unchanged(self):
        host = self._gathering_host({1: False, 2: False})
        self.assertEqual(host._host_features_state(), {})

    def test_auto_gain_round_trips_through_the_session_file(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "last-session.json"
        host = _bind(self._gathering_host({1: True, 2: True}),
                     "_save_last_session")
        self.assertTrue(host._save_last_session(path))
        self.assertEqual(
            io24gtk.load_last_session(path)["host_features"]["autogain"],
            {"version": 1, "on": [1, 2]})

    def test_saved_auto_gain_switches_those_inputs_back_on(self):
        host, switched = self._adopting_host()
        self.assertIsNone(host._adopt_autogain({"version": 1, "on": [2]}))
        self.assertEqual(switched, {1: [], 2: [True]})

    def test_no_saved_auto_gain_leaves_the_buttons_alone(self):
        host, switched = self._adopting_host()
        self.assertIsNone(host._adopt_autogain(None))
        self.assertEqual(switched, {1: [], 2: []})

    def test_an_unusable_saved_value_is_reported_and_not_applied(self):
        host, switched = self._adopting_host()
        for state in ({"on": [3]}, {"on": "1"}, {}, "on"):
            with self.subTest(state=state):
                self.assertIn("auto gain was not restored",
                              host._adopt_autogain(state))
        self.assertEqual(switched, {1: [], 2: []})

    def test_loading_restores_auto_gain_with_the_other_host_features(self):
        self.assertIn('self._adopt_autogain(\n                host_features.get("autogain"))',
                      _function("_after_load"))


if __name__ == "__main__":
    unittest.main()
