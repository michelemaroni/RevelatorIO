import ast
import copy
import contextlib
import io
import json
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import io24
import io24gtk
import io24_presets
from io24_preset_record import (
    DEVICE_SLOT_REGISTRY_PROVENANCE,
    DeviceSlotRegistry,
    compare_device_slot_records,
    decode_preset_record,
    encode_preset_record,
)


ROOT = Path(__file__).resolve().parents[1]
FACTORY_PRESETS = ROOT / "re" / "uc_factory_presets.json"
USB_CAPTURE_HELPER = Path(__file__).with_name("usbtap_py.py")
LEGACY_FX_PROBE = Path(__file__).with_name("fx_gate_probe.py")


class _SnapshotOnlyIo24(io24.Io24):
    """Exercise the real preset writer without opening the USB device."""

    def __init__(self, snapshot):
        self.saved_snapshot = snapshot

    def snapshot(self):
        return self.saved_snapshot


class _HardwareFreeIo24(io24.Io24):
    """Run the real setters and preset replay against the protocol fake."""

    def __init__(self, live=None):
        self.dev = type("UsbIdentity", (), {
            "serial_number": "TEST-IO24-001",
            "idVendor": io24.VID,
            "idProduct": io24.PID,
            "bcdDevice": 0x0128,
        })()
        self._shadow = {}
        self._shadow_dirty = False
        self._shadow_flushed = 0.0
        self._shadow_persist = False
        self._send_state = None
        self.protocol_writes = []
        self.live = dict(live or {
            "input1Gain": 12.0,
            "input2Gain": 18.0,
            "hpVolume": 0.4,
            "mainVolume": 0.7,
            "monitorMix": -0.2,
            "input1PhantomPower": False,
            "input2PhantomPower": True,
        })

    def read_params(self):
        return dict(self.live)

    def set_param(self, param_id, value, index=0, as_int=False):
        self.protocol_writes.append(("param", param_id, value, index, as_int))
        return b""

    def _dsp(self, *args, **kwargs):
        self.protocol_writes.append(("dsp", args, kwargs))
        return b""

    def _exec(self, payload, wait=1.5):
        self.protocol_writes.append(("exec", bytes(payload), wait))
        return b""

    def set_gain(self, channel, db):
        self.live["input%dGain" % channel] = db

    def set_hp_volume(self, value):
        self.live["hpVolume"] = value

    def set_main_volume(self, value):
        self.live["mainVolume"] = value

    def set_monitor_mix(self, value):
        self.live["monitorMix"] = value

    def set_phantom(self, channel, on):
        self.live["input%dPhantomPower" % channel] = bool(on)


class PresetPersistenceTests(unittest.TestCase):
    @unittest.skipUnless(
        FACTORY_PRESETS.is_file(),
        "the retained private factory-preset evidence is not present",
    )
    def test_current_controls_overlay_a_complete_slot_body_including_voicefx(self):
        base = io24_presets.load()["Broadcast"]
        bands = [
            {"shape": "lowshelf", "freq": 70.0, "gain": 2.5, "q": 0.7},
            {"shape": "peaking", "freq": 260.0, "gain": -2.0, "q": 1.1},
            {"shape": "peaking", "freq": 2400.0, "gain": 1.5, "q": 0.9},
            {"shape": "highshelf", "freq": 7200.0, "gain": 2.0, "q": 0.7},
        ]
        record = io24_presets.current_slot_record(
            base, "Current Channel 1", bands=bands, hpf_hz=24.0,
            eq_first=True,
            gate={"on": False, "threshold_db": -40.0, "range_db": -60.0,
                  "attack_s": .005, "release_s": .3,
                  "keyfilter_hz": 0.0, "keylisten": False,
                  "expander": True},
            compressor_model=2,
            compressor={"on": True, "input_db": -30.0,
                        "output_db": -3.0, "attack_s": .0001,
                        "release_s": .25, "ratio_index": 3,
                        "keyfilter_hz": 0.0, "keylisten": False},
            limiter={"on": True, "threshold_db": -.6},
            voicefx_model="delay",
            voicefx={"on": True, "time_s": .173,
                     "feedback": .25, "mix": .4})

        self.assertEqual(record["preset_name"], "Current Channel 1")
        self.assertEqual(record["filter"], {"hpf": 24.0})
        self.assertEqual(record["opt"], {"swapcompeq": 1})
        self.assertEqual(record["comp"]["__classid"],
                         "{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}")
        self.assertEqual(record["comp"]["input"], -30.0)
        self.assertEqual(record["limit"],
                         {"limiteron": 1, "threshold": -.6})
        self.assertEqual(record["voicefx"]["time"], .173)
        self.assertEqual(record["voicefx"]["on"], 1)
        self.assertNotIn("_offset", record)

    @unittest.skipUnless(
        FACTORY_PRESETS.is_file(),
        "the retained private factory-preset evidence is not present",
    )
    def test_user_preset_with_fx_off_saves_and_replays_the_strip_independently(self):
        record = json.loads(json.dumps(io24_presets.load()["Broadcast"]))
        record["preset_name"] = "Dry vocal"
        record["voicefx"]["on"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user-presets.json"
            io24_presets.save_user_preset("Dry vocal", record, path)
            loaded = io24_presets.load_user_presets(path)["Dry vocal"]

        device = mock.Mock()
        applied = io24_presets.apply_preset(
            device, loaded, channel=1, with_fx=False)

        self.assertEqual(loaded["voicefx"]["on"], 0)
        self.assertTrue(applied)
        device.set_voicefx_channel.assert_not_called()
        device.set_fx.assert_not_called()
        self.assertTrue(device.set_comp_eq_order.called)

    @staticmethod
    def _device_identity():
        return {
            "serial": "TEST-IO24-001",
            "vendor_id": "194f",
            "product_id": "0422",
            "bcd_device": "0128",
        }

    @staticmethod
    def _complete_slot_record():
        return {
            "preset_name": "Readback target",
            "opt": {"swapcompeq": 0},
            "filter": {"hpf": 40.0},
            "gate": {"on": 0},
            "limit": {"on": 1},
            "eq": {"on": 1},
            "comp": {"on": 1},
            "voicefx": {
                "__classid": "{66A10093-D461-4CAC-A80C-91F6A1BB37E5}",
                "on": 1,
                "lows": 0.06,
                "width": 0.405,
                "mix": 0.295,
            },
        }

    def test_slot_comparison_requires_a_decoded_record_not_only_selected_slot(self):
        """A selected slot and transport receipt must not be promoted to a match."""
        result = compare_device_slot_records(
            0, self._complete_slot_record(), observed_slot=0)

        self.assertEqual(result["status"], "READBACK_UNAVAILABLE")
        self.assertEqual(result["expected_slot"], 0)
        self.assertEqual(result["observed_slot"], 0)
        self.assertIsNone(result["observed_canonical_sha256"])
        self.assertEqual(result["differences"], [])

    def test_slot_comparison_accepts_only_semantically_equal_complete_records(self):
        expected = self._complete_slot_record()
        observed = json.loads(json.dumps(expected, sort_keys=True))
        observed["_offset"] = 99

        result = compare_device_slot_records(0, expected, 0, observed)

        self.assertEqual(result["status"], "RECORD_EQUAL")
        self.assertEqual(result["differences"], [])
        self.assertEqual(result["expected_canonical_sha256"],
                         result["observed_canonical_sha256"])
        self.assertEqual(result["physical_commit"], "UNPROVED")
        self.assertEqual(result["audibility"], "UNPROVED")

    def test_slot_comparison_reports_nested_content_or_slot_mismatch(self):
        expected = self._complete_slot_record()
        observed = self._complete_slot_record()
        observed["voicefx"]["mix"] = 0.3

        result = compare_device_slot_records(0, expected, 1, observed)

        self.assertEqual(result["status"], "RECORD_MISMATCH")
        self.assertEqual(result["differences"], ["/slot", "/voicefx/mix"])

    def test_slot_comparison_rejects_an_incomplete_readback(self):
        result = compare_device_slot_records(
            0, self._complete_slot_record(), 0,
            {"preset_name": "truncated device reply"})

        self.assertEqual(result["status"], "INVALID_READBACK")
        self.assertIn("incomplete", result["readback_error"])
        self.assertIsNone(result["observed_canonical_sha256"])

    def test_scene_json_slot_encodes_as_the_recovered_uc_record(self):
        slot = {
            "preset_name": "Test",
            "opt": {"swapcompeq": 0},
            "filter": {"hpf": 40.0, "keyfilter": 480},
            "voicefx": {"on": 1, "mix": 0.5},
        }
        expected = (
            b"{i\x0bpreset_nameSi\x04Test"
            b"i\x03opt{i\x0aswapcompeqi\x00}"
            b"i\x06filter{i\x03hpfd\x42\x20\x00\x00"
            b"i\x09keyfilterI\x01\xe0}"
            b"i\x07voicefx{i\x02oni\x01i\x03mixd\x3f\x00\x00\x00}"
            b"}"
        )

        encoded = encode_preset_record(slot)

        self.assertEqual(encoded, expected)
        self.assertEqual(decode_preset_record(encoded), slot)

    def test_tagged_slot_builder_requires_a_complete_record(self):
        slot = self._complete_slot_record()

        frames = io24._build_uc_slot_json_frames(2, slot)

        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(struct.unpack_from("<IIII", frame, 0),
                         (io24.SETP, io24.APPL, 0, io24.MEMP))
        self.assertEqual(struct.unpack_from("<II", frame, 16),
                         (io24.STATE_BLOB_SIZE, io24.STAT))
        self.assertEqual(struct.unpack_from("<I", frame, 24)[0], 2)
        size = struct.unpack_from("<H", frame, 35)[0]
        self.assertEqual(frame[41:41 + size], encode_preset_record(slot))

        with self.assertRaisesRegex(ValueError, "incomplete"):
            io24._build_uc_slot_json_frames(
                2, {"preset_name": "effect only", "voicefx": {"on": 1}})

    def test_tagged_uc_slot_api_is_refused_before_transport(self):
        device = _HardwareFreeIo24()
        slot = self._complete_slot_record()

        with self.assertRaisesRegex(
                io24.HostActionError, "tagged UC Stat archives"):
            device.save_uc_device_slot(3, slot)
        with self.assertRaisesRegex(
                io24.HostActionError, "tagged UC Stat archives"):
            device.save_uc_device_slot(3, {"preset_name": "partial"})
        self.assertEqual(device.protocol_writes, [])

    def test_slot_registry_records_a_complete_host_write_without_claiming_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceSlotRegistry(Path(directory) / "device-slots.json")
            record = self._complete_slot_record()

            prepared = registry.prepare_write(
                0, record, source="factory:Reverb",
                written_at="2026-09-08T07:30:00+00:00",
                device_identity=self._device_identity())
            entry = registry.commit_sent(prepared, {
                "slot": 0, "fragments_sent": 1, "replies_received": 0,
            })
            loaded = DeviceSlotRegistry(registry.path).get(
                0, device_identity=self._device_identity())

        self.assertEqual(entry, loaded)
        self.assertEqual(entry["physical_channel"], 1)
        self.assertEqual(entry["provenance"],
                         DEVICE_SLOT_REGISTRY_PROVENANCE)
        self.assertEqual(entry["status"], "WRITE_SENT_UNVERIFIED")
        self.assertEqual(entry["record"], record)
        self.assertEqual(len(entry["canonical_sha256"]), 64)
        self.assertEqual(entry["transport"], {
            "fragments_sent": 1, "replies_received": 0,
        })

    def test_slot_registry_read_is_bound_to_current_device_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceSlotRegistry(Path(directory) / "device-slots.json")
            prepared = registry.prepare_write(
                0, self._complete_slot_record(), "factory:Reverb",
                device_identity=self._device_identity())
            registry.commit_sent(prepared, {
                "slot": 0, "fragments_sent": 1, "replies_received": 0,
            })
            other = dict(self._device_identity())
            other["serial"] = "OTHER-IO24"

            self.assertIsNone(registry.get(0, device_identity=other))
            with self.assertRaisesRegex(TypeError, "device_identity"):
                registry.get(0)

    def test_slot_registry_rejects_incomplete_or_mismatched_writes_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceSlotRegistry(Path(directory) / "device-slots.json")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                registry.prepare_write(
                    0, {"preset_name": "partial"}, "test",
                    device_identity=self._device_identity())
            self.assertFalse(Path(registry.path).exists())

            prepared = registry.prepare_write(
                2, self._complete_slot_record(), "factory:Reverb",
                device_identity=self._device_identity())
            with self.assertRaisesRegex(ValueError, "transport slot"):
                registry.commit_sent(prepared, {
                    "slot": 3, "fragments_sent": 1, "replies_received": 0,
                })
            self.assertFalse(Path(registry.path).exists())

    @unittest.skipUnless(
        FACTORY_PRESETS.is_file(),
        "the retained private factory-preset evidence is not present",
    )
    def test_save_known_scene_writes_a_native_body_and_commits_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceSlotRegistry(Path(directory) / "device-slots.json")
            device = _HardwareFreeIo24()
            record = copy.deepcopy(io24_presets.load()["Broadcast"])

            result = device.save_known_device_slot(
                2, record, registry, source="factory:Broadcast",
                written_at="2026-09-08T07:31:00+00:00")

            self.assertTrue(Path(registry.path).exists())
            self.assertEqual(result["registry"]["body_format"],
                             "firmware-native-v2")
            self.assertEqual(result["registry"]["native_sha256"],
                             result["native_sha256"])
        self.assertEqual(device.protocol_writes[0][0], "exec")

    @unittest.skipUnless(
        FACTORY_PRESETS.is_file(),
        "the retained private factory-preset evidence is not present",
    )
    def test_save_known_slot_requires_identity_before_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceSlotRegistry(Path(directory) / "device-slots.json")
            device = _HardwareFreeIo24()
            device.dev.serial_number = ""
            record = copy.deepcopy(io24_presets.load()["Broadcast"])

            with self.assertRaisesRegex(
                    ValueError, "serial is unavailable"):
                device.save_known_device_slot(
                    0, record, registry, source="factory:Broadcast")

        self.assertEqual(device.protocol_writes, [])

    @unittest.skipUnless(
        FACTORY_PRESETS.is_file(),
        "the retained private factory-preset evidence is not present",
    )
    def test_factory_reverb_json_is_a_complete_tagged_library_template(self):
        records = json.loads(FACTORY_PRESETS.read_text())
        slot = dict(records[30])
        extraction_offset = slot.pop("_offset")

        self.assertEqual(extraction_offset, 157055599)
        self.assertEqual(slot["preset_name"], "Reverb")
        self.assertEqual(slot["voicefx"], {
            "__classid": "{66A10093-D461-4CAC-A80C-91F6A1BB37E5}",
            "on": 1,
            "lows": 0.06,
            "width": 0.405,
            "mix": 0.295,
        })
        self.assertTrue({"reverb", "vrvb", "FXA"}.isdisjoint(slot))

        frames = io24._build_uc_slot_json_frames(0, slot)

        self.assertEqual(len(frames), 1)
        frame = frames[0]
        fragment_size = struct.unpack_from("<H", frame, 35)[0]
        record_size = struct.unpack_from("<I", frame, 37)[0]
        encoded = frame[41:41 + fragment_size]
        self.assertEqual(fragment_size, record_size)
        self.assertEqual(encoded, encode_preset_record(slot))
        decoded = decode_preset_record(encoded)
        self.assertEqual(encode_preset_record(decoded), encoded)
        self.assertEqual(decoded["preset_name"], "Reverb")
        self.assertEqual(decoded["voicefx"]["__classid"],
                         slot["voicefx"]["__classid"])
        self.assertEqual(decoded["voicefx"]["on"], 1)
        for field in ("lows", "width", "mix"):
            self.assertAlmostEqual(decoded["voicefx"][field],
                                   slot["voicefx"][field], places=6)

    def test_stat_envelope_rejects_nonexistent_global_slots(self):
        for index in (-1, 4, True):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    io24._build_uc_slot_memp_frames(index, b"{")

    def test_captured_cache_refresh_envelope_encodes_one_fragment_exactly(self):
        """The recovered MemP envelope must remain byte-for-byte stable."""
        self.assertTrue(hasattr(io24, "_build_captured_memp_frames"),
                        "captured MemP wire fixture is not implemented")

        record = b"\x10\x20\x30"
        frames = io24._build_captured_memp_frames(16, record)

        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(len(frame), 0x7f8)
        self.assertEqual(struct.unpack_from("<IIII", frame, 0),
                         (0x53657450, 0x4170706c, 0, 0x4d656d50))
        self.assertEqual(struct.unpack_from("<II", frame, 16),
                         (0x7ec, 0x5072734d))
        self.assertEqual(struct.unpack_from("<I", frame, 24)[0], 16)
        self.assertEqual(struct.unpack_from("<H", frame, 28)[0], 0)
        self.assertEqual(struct.unpack_from("<I", frame, 30)[0], 0)
        self.assertEqual(frame[34], 0)
        self.assertEqual(struct.unpack_from("<H", frame, 35)[0], 3)
        self.assertEqual(struct.unpack_from("<I", frame, 37)[0], 3)
        self.assertEqual(frame[41:44], record)
        self.assertEqual(frame[44:], b"\x00" * (0x7f8 - 44))

    def test_captured_cache_refresh_envelope_fragments_without_data_loss(self):
        record = bytes(i & 0xff for i in range(0x7ce + 5))

        frames = io24._build_captured_memp_frames(27, record)

        self.assertEqual(len(frames), 2)
        self.assertEqual([struct.unpack_from("<I", f, 30)[0] for f in frames],
                         [0, 0x7ce])
        self.assertEqual([f[34] for f in frames], [1, 0])
        self.assertEqual([struct.unpack_from("<H", f, 35)[0] for f in frames],
                         [0x7ce, 5])
        self.assertEqual([struct.unpack_from("<I", f, 37)[0] for f in frames],
                         [len(record), len(record)])
        rebuilt = b"".join(
            frame[41:41 + struct.unpack_from("<H", frame, 35)[0]]
            for frame in frames)
        self.assertEqual(rebuilt, record)

    def test_captured_cache_refresh_envelope_rejects_invalid_inputs(self):
        for index in (15, 28, True):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    io24._build_captured_memp_frames(index, b"x")
        with self.assertRaises(TypeError):
            io24._build_captured_memp_frames(16, "not bytes")
        with self.assertRaises(ValueError):
            io24._build_captured_memp_frames(16, b"")
        with self.assertRaises(ValueError):
            io24._build_captured_memp_frames(16, b"x" * 0x10000)

    def test_host_json_preset_never_uses_the_captured_memp_envelope(self):
        """Host persistence must not masquerade as a device-resident write."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host-only.json"
            device = _HardwareFreeIo24()
            with mock.patch.object(io24, "_build_captured_memp_frames",
                                   side_effect=AssertionError("must stay offline")):
                device.save_preset(path)
                device.load_preset(path)

            self.assertFalse(any(
                kind == "exec" and struct.pack("<I", io24.MEMP) in payload
                for kind, payload, _wait in device.protocol_writes
                if kind == "exec"))

    def test_fx_on_refuses_unobserved_activation_without_writing_reverb_routes(self):
        """An unresolved Voice FX gate must not masquerade as send/return setup."""
        device = _HardwareFreeIo24()

        with self.assertRaisesRegex(RuntimeError,
                                    "standalone direct Voice FX activation"):
            device.fx_on(channel=1, send=1.0, return_db=0.0, bus="main")

        self.assertEqual(device.protocol_writes, [])

    def test_fx_off_refuses_unobserved_deactivation_without_writing_reverb_routes(self):
        """Voice FX off must not change the unrelated reverb send control."""
        device = _HardwareFreeIo24()

        with self.assertRaisesRegex(RuntimeError,
                                    "standalone direct Voice FX deactivation"):
            device.fx_off(channel=1, bus="main")

        self.assertEqual(device.protocol_writes, [])

    def test_voice_fx_intent_is_parameter_only_and_never_claims_audible(self):
        device = _HardwareFreeIo24()

        result = device.configure_voice_fx_intent(
            True, "delay", time_s=0.24, feedback=0.42, mix=0.6,
            fs=48000.0)

        self.assertEqual(result["model"], "delay")
        self.assertGreater(result["parameter_writes"], 0)
        self.assertFalse(result["audible"])
        self.assertEqual(result["activation"], "unresolved")
        writes_after_arm = list(device.protocol_writes)

        disabled = device.configure_voice_fx_intent(False, "delay")

        self.assertEqual(disabled["parameter_writes"], 0)
        self.assertFalse(disabled["audible"])
        self.assertEqual(device.protocol_writes, writes_after_arm)

    def test_gtk_fx_assigns_one_input_then_sends_global_model(self):
        tree = ast.parse((Path(__file__).parents[1] / "io24gtk.py").read_text())
        push_fx = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef)
                       and node.name == "_push_fx")
        push_attributes = {node.attr for node in ast.walk(push_fx)
                           if isinstance(node, ast.Attribute)}
        methods = {node.name for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)}

        self.assertNotIn("configure_voice_fx_intent", push_attributes)
        self.assertIn("submit", push_attributes)
        self.assertIn("set_voicefx_channel", push_attributes)
        self.assertIn("set_fx", push_attributes)
        for forbidden in ("fx_on", "fx_off", "set_fx_mix", "set_reverb"):
            self.assertNotIn(forbidden, push_attributes)
        self.assertNotIn("_save_private_reverb_both", methods)

    def test_gtk_fx_parameter_edits_do_not_repeat_same_assignment(self):
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
                self.assignments = []
                self.models = []

            def set_voicefx_channel(self, channel):
                self.assignments.append(channel)

            def set_fx(self, model, on=True, **params):
                self.models.append((model, on, params))
                return 1

        class Ctl:
            def __init__(self, device):
                self.dev = device

            def submit(self, fn):
                fn(self.dev)

        device = Device()
        window = io24gtk.Win.__new__(io24gtk.Win)
        window._fx_mute = False
        window._fx_last_sent_device = None
        window._fx_last_sent_target = None
        window.ctl = Ctl(device)
        window.fx_arm = Value(True)
        window.fx_model = Value(0)
        window.fx_target = Value(0)
        window.fx_params = {
            "transformer": {
                name: Value(default)
                for name, _title, _lo, _hi, _step, default, _fmt
                in io24gtk.Win.FX_PARAMS["transformer"]
            },
        }
        messages = []
        window.say = messages.append

        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            window._push_fx()
            window.fx_params["transformer"]["mix"].value = 0.7
            window._push_fx()

        self.assertEqual(device.assignments, [1])
        self.assertEqual(len(device.models), 2)
        self.assertEqual(device.models[-1][2]["mix"], 0.7)

    def test_gtk_fx_assignment_follows_target_and_new_device(self):
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
                self.assignments = []

            def set_voicefx_channel(self, channel):
                self.assignments.append(channel)

            def set_fx(self, model, on=True, **params):
                return 1

        class Ctl:
            def __init__(self, device):
                self.dev = device

            def submit(self, fn):
                fn(self.dev)

        first = Device()
        second = Device()
        window = io24gtk.Win.__new__(io24gtk.Win)
        window._fx_mute = False
        window._fx_last_sent_device = None
        window._fx_last_sent_target = None
        window.ctl = Ctl(first)
        window.fx_arm = Value(True)
        window.fx_model = Value(0)
        window.fx_target = Value(0)
        window.fx_params = {
            "transformer": {
                name: Value(default)
                for name, _title, _lo, _hi, _step, default, _fmt
                in io24gtk.Win.FX_PARAMS["transformer"]
            },
        }
        window.say = lambda _message: None

        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            window._push_fx()
            window.fx_target.value = 1
            window._push_fx()
            window.ctl.dev = second
            window._push_fx()

        self.assertEqual(first.assignments, [1, 2])
        self.assertEqual(second.assignments, [2])

    def test_factory_device_store_refuses_the_currently_selected_slot(self):
        class Selected:
            def __init__(self, value):
                self.value = value

            def get_selected(self):
                return self.value

        window = io24gtk.Win.__new__(io24gtk.Win)
        window.ctl = type("Ctl", (), {
            "dev": object(),
            "snap": {"alive": True, "preset_slot": [0, 2]},
        })()
        window.PR = type("Presets", (), {
            "load": staticmethod(lambda: {
                "Reverb": self._complete_slot_record(),
            }),
        })
        window.factory_target = Selected(0)
        window.factory_device_slot = Selected(0)

        with self.assertRaisesRegex(io24.HostActionError,
                                    "currently selected"):
            window._prepare_factory_slot_store("Reverb")

    def test_confirmed_factory_device_store_writes_only_body_and_registry(self):
        class Selected:
            def __init__(self, value):
                self.value = value

            def get_selected(self):
                return self.value

        class Device:
            def __init__(self):
                self.calls = []

            def save_known_device_slot(self, slot, record, registry, source,
                                       sample_rate_hz):
                self.calls.append((slot, record, registry, source,
                                   sample_rate_hz))
                return {
                    "transport": {"slot": slot, "fragments_sent": 1,
                                  "replies_received": 0},
                    "registry": {"status": "WRITE_SENT_UNVERIFIED"},
                }

        class Ctl:
            def __init__(self, device):
                self.dev = device
                self.snap = {"alive": True, "preset_slot": [0, 2]}

            def submit(self, fn):
                fn(self.dev)

        record = self._complete_slot_record()
        device = Device()
        registry = object()
        window = io24gtk.Win.__new__(io24gtk.Win)
        window.ctl = Ctl(device)
        window.device_slot_registry = registry
        window.PR = type("Presets", (), {
            "load": staticmethod(lambda: {"Reverb": record}),
        })
        window.factory_target = Selected(0)
        window.factory_device_slot = Selected(1)
        messages = []
        window.say = messages.append

        plan = window._prepare_factory_slot_store("Reverb")
        with mock.patch.object(io24gtk.GLib, "idle_add",
                               side_effect=lambda fn, *args: fn(*args)):
            window._queue_factory_slot_store(plan)

        self.assertEqual(len(device.calls), 1)
        slot, sent_record, used_registry, source, sample_rate_hz = device.calls[0]
        self.assertEqual(slot, 1)
        self.assertEqual(sent_record, record)
        self.assertIs(used_registry, registry)
        self.assertEqual(source, "factory:Reverb")
        self.assertEqual(sample_rate_hz, io24gtk.DEFAULT_SAMPLE_RATE)
        self.assertNotIn("selected", messages[-1].lower())
        self.assertNotIn("enabled", messages[-1].lower())

    def test_registry_lists_host_known_user_presets_in_slot_order(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceSlotRegistry(Path(directory) / "device-slots.json")
            identity = {
                "serial": "TEST-IO24-001", "vendor_id": "194f",
                "product_id": "0422", "bcd_device": "0128",
            }
            for slot, name in ((1, "B A S E"), (0, "MAIN")):
                record = self._complete_slot_record()
                record["preset_name"] = name
                prepared = registry.prepare_write(
                    slot, record, "restored-user:%s" % name,
                    device_identity=identity)
                registry.commit_sent(prepared, {
                    "slot": slot, "fragments_sent": 1,
                    "replies_received": 0,
                })

            entries = registry.entries(device_identity=identity)

        self.assertEqual(
            [(entry["slot"], entry["record"]["preset_name"])
             for entry in entries],
            [(0, "MAIN"), (1, "B A S E")])

    def test_presets_page_lists_host_registry_presets_among_user_presets(self):
        source = (Path(__file__).parents[1] / "io24gtk.py").read_text()

        self.assertNotIn('title="Host presets"', source)
        self.assertIn("self.device_slot_registry.entries", source)
        self.assertIn('getattr(self, "_host_slot_entries", [])', source)
        self.assertIn("self._load_device_entry(e)", source)

    @unittest.skipUnless(
        USB_CAPTURE_HELPER.is_file(),
        "the retained private USB-capture helper is not present",
    )
    def test_capture_baseline_records_parameters_not_an_unknown_gate(self):
        tree = ast.parse(USB_CAPTURE_HELPER.read_text())
        main = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        attributes = {node.attr for node in ast.walk(main)
                      if isinstance(node, ast.Attribute)}

        self.assertIn("configure_voice_fx_intent", attributes)
        self.assertNotIn("fx_on", attributes)
        self.assertNotIn("fx_off", attributes)
        self.assertNotIn("set_fx_mix", attributes)

    @unittest.skipUnless(
        LEGACY_FX_PROBE.is_file(),
        "the retained private legacy probe is not present",
    )
    def test_legacy_direct_fx_probe_routes_to_cp34_without_device_actions(self):
        path = LEGACY_FX_PROBE
        tree = ast.parse(path.read_text())
        main = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        attributes = {node.attr for node in ast.walk(main)
                      if isinstance(node, ast.Attribute)}

        self.assertIn("CAPTURE-MANIFEST.md", ast.get_source_segment(
            path.read_text(), main))
        self.assertNotIn("Io24", attributes)
        self.assertNotIn("set_fx", attributes)
        self.assertNotIn("set_preset_slot", attributes)

    def test_preset_frontend_distinguishes_host_files_from_device_slots(self):
        root = Path(__file__).parents[1]
        gtk_source = (root / "io24gtk.py").read_text()

        self.assertIn('menu.append("Save full Host setup…"', gtk_source)
        # 2026-09-11: presets on the unit are listed among User Presets,
        # tagged by where they live, rather than in a section of their own.
        self.assertIn('"Input %d · Preset button %d"', gtk_source)
        self.assertIn('"Input %d · Device preset %d"', gtk_source)
        self.assertNotIn("WRITE_SENT_UNVERIFIED", gtk_source)
        self.assertNotIn("selected device slot", gtk_source)

    def test_effects_survive_hardware_free_save_load_save_round_trip(self):
        """The real preset replay must preserve saved effect and route intent."""
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            source = _HardwareFreeIo24()
            source.set_reverb(on=True, size=0.72, mix=0.38,
                              hp_freq=180.0, predelay=0.027, fs=48000.0)
            source.set_fx("delay", time_s=0.24, feedback=0.42, mix=0.6,
                          fs=48000.0)
            source.set_fx_mix(1, 0.75)
            source.set_send_db("fxreturn/ch1", "aux1", -3.5)
            source.set_send_assigned("fxreturn/ch1", "mixa", True)
            source.set_bus_master("aux1", 1.0)
            source.save_preset(first)

            conflicting_live = {
                "input1Gain": 1.0,
                "input2Gain": 2.0,
                "hpVolume": 0.1,
                "mainVolume": 0.2,
                "monitorMix": 0.9,
                "input1PhantomPower": True,
                "input2PhantomPower": False,
            }
            restored = _HardwareFreeIo24(conflicting_live)
            live_count, call_count = restored.load_preset(
                first, sample_rate_hz=48000.0)
            restored.save_preset(second)

            self.assertEqual(live_count, len(io24.Io24.LIVE_KEYS))
            self.assertEqual(call_count, 6)
            self.assertEqual(restored.live, source.live)
            self.assertGreater(len(restored.protocol_writes), 0)
            self.assertEqual(io24.inspect_preset(second),
                             io24.inspect_preset(first))

    def test_failed_overwrite_preserves_last_successful_preset(self):
        """A serialization failure must not truncate the user's good preset."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.json"
            device = _SnapshotOnlyIo24({
                "version": 1,
                "live": {"monitorMix": 0.25},
                "calls": {
                    "set_reverb": {
                        "fn": "set_reverb",
                        "kwargs": {"on": True, "size": 0.7, "mix": 0.4},
                    }
                },
            })

            device.save_preset(path)
            good_bytes = path.read_bytes()
            self.assertEqual(json.loads(good_bytes)["calls"]["set_reverb"]
                             ["kwargs"]["size"], 0.7)

            device.saved_snapshot = {
                "version": 1,
                "live": {},
                "calls": {"set_fx": {"fn": "set_fx", "kwargs": object()}},
            }

            with self.assertRaises(TypeError):
                device.save_preset(path)

            self.assertEqual(path.read_bytes(), good_bytes)
            self.assertEqual(list(Path(directory).glob(".voice.json.*.tmp")), [])

    def test_saved_effect_state_is_inspectable_without_a_device(self):
        """A preset file must reveal every saved effect and its routing."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "effects.json"
            path.write_text(json.dumps({
                "version": 1,
                "live": {},
                "calls": {
                    "set_reverb": {
                        "fn": "set_reverb",
                        "kwargs": {
                            "on": True,
                            "size": 0.72,
                            "mix": 0.38,
                            "hp_freq": 180.0,
                            "predelay": 0.027,
                            "fs": 48000.0,
                        },
                    },
                    "set_fx": {
                        "fn": "set_fx",
                        "kwargs": {
                            "model": "delay",
                            "time_s": 0.24,
                            "feedback": 0.42,
                            "mix": 0.6,
                        },
                    },
                    "set_fx_mix#channel=1": {
                        "fn": "set_fx_mix",
                        "kwargs": {"channel": 1, "value": 0.75},
                    },
                    "set_fx_mix#channel=2": {
                        "fn": "set_fx_mix",
                        "kwargs": {"channel": 2, "value": 0.25},
                    },
                    "set_send_db#source=fxreturn/ch1,bus=mixa": {
                        "fn": "set_send_db",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "aux1",
                            "gain_db": -3.5,
                        },
                    },
                    "set_send_assigned#source=fxreturn/ch1,bus=mixa": {
                        "fn": "set_send_assigned",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "mixa",
                            "on": False,
                        },
                    },
                    "set_bus_master#bus=mixa": {
                        "fn": "set_bus_master",
                        "kwargs": {"bus": "aux1", "gain_db": -20.0},
                    },
                    "set_send_db#source=fxreturn/ch1,bus=main": {
                        "fn": "set_send_db",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "main",
                            "gain_db": None,
                        },
                    },
                    "set_send_db#source=fxreturn/ch1,bus=mixb": {
                        "fn": "set_send_db",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "mixb",
                            "gain_db": -6.0,
                        },
                    },
                    "set_bus_master#bus=mixb": {
                        "fn": "set_bus_master",
                        "kwargs": {"bus": "mixb", "gain_db": 1.0},
                    },
                },
            }))

            self.assertTrue(hasattr(io24, "inspect_preset"),
                            "inspect_preset is not implemented")
            self.assertEqual(io24.inspect_preset(path), {
                "reverb": {
                    "on": True,
                    "size": 0.72,
                    "mix": 0.38,
                    "hp_freq": 180.0,
                    "predelay": 0.027,
                    "fs": 48000.0,
                },
                "voice_fx": {
                    "model": "delay",
                    "time_s": 0.24,
                    "feedback": 0.42,
                    "mix": 0.6,
                },
                "processing_mix": {1: 0.75, 2: 0.25},
                "fx_returns": {
                    "main": {
                        "fader_db": None,
                        "fader_saved": True,
                        "assigned": True,
                        "master_db": 0.0,
                        "effective_db": None,
                        "effective_state": "off",
                    },
                    "mixa": {
                        "fader_db": -3.5,
                        "fader_saved": True,
                        "assigned": False,
                        "master_db": -20.0,
                        "effective_db": None,
                        "effective_state": "off",
                    },
                    "mixb": {
                        "fader_db": -6.0,
                        "fader_saved": True,
                        "assigned": True,
                        "master_db": 1.0,
                        "effective_db": -5.0,
                        "effective_state": "level",
                    },
                },
            })

    def test_successful_save_syncs_file_and_containing_directory(self):
        """A successful return must include the directory entry in the sync."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable.json"
            device = _SnapshotOnlyIo24({"version": 1, "live": {}, "calls": {}})
            real_fsync = io24.os.fsync
            synced = []

            def tracking_fsync(fd):
                mode = io24.os.fstat(fd).st_mode
                synced.append("directory" if stat.S_ISDIR(mode) else "file")
                return real_fsync(fd)

            with mock.patch.object(io24.os, "fsync", side_effect=tracking_fsync):
                device.save_preset(path)

            self.assertEqual(synced, ["file", "directory"])

    def test_presetinfo_cli_does_not_open_the_device(self):
        """Inspecting saved effects must remain a file-only operation."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "effects.json"
            path.write_text(json.dumps({
                "version": 1,
                "live": {},
                "calls": {
                    "set_reverb": {
                        "fn": "set_reverb",
                        "kwargs": {"on": False, "size": 0.1, "mix": 0.0},
                    },
                    "set_fx": {
                        "fn": "set_fx",
                        "kwargs": {"model": "detuner", "detune": 4, "mix": 0.5},
                    },
                    "set_send_db#source=fxreturn/ch1,bus=mixa": {
                        "fn": "set_send_db",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "mixa",
                            "gain_db": -3.0,
                        },
                    },
                    "set_send_assigned#source=fxreturn/ch1,bus=mixa": {
                        "fn": "set_send_assigned",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "mixa",
                            "on": False,
                        },
                    },
                    "set_send_assigned#source=fxreturn/ch1,bus=main": {
                        "fn": "set_send_assigned",
                        "kwargs": {
                            "source": "fxreturn/ch1",
                            "bus": "main",
                            "on": True,
                        },
                    },
                },
            }))
            output = io.StringIO()

            with mock.patch.object(sys, "argv", ["io24.py", "presetinfo", str(path)]), \
                    mock.patch.object(io24.Io24, "__init__",
                                      side_effect=AssertionError("USB opened")), \
                    contextlib.redirect_stdout(output):
                try:
                    io24.main()
                except AssertionError as error:
                    self.fail(str(error))

            rendered = output.getvalue()
            self.assertIn("Reverb: off", rendered)
            self.assertIn("Voice FX: detuner", rendered)
            self.assertIn("stored intent; direct playback not established", rendered)
            self.assertIn("mixa=fader -3.0 dB, unassigned, effective off",
                          rendered)
            self.assertIn("main=fader not saved, assigned, effective unknown",
                          rendered)


if __name__ == "__main__":
    unittest.main()
