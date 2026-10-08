#!/usr/bin/env python3
"""Hardware-free contracts for the remaining UC parity closure.

These tests deliberately separate UC's twelve-entry Device Presets library
(``MemP/PrsM``) from the four selection-only front-panel blocks
(``MemP/Stat``).  They also cover the two host-side UC concepts that do not
need a new device command: persistent Mirror Main and storable component
names.
"""

import json
from pathlib import Path
import struct
import tempfile
import unittest

import io24
from io24_preset_record import DevicePresetLibraryRegistry
import io24_scene


IDENTITY = {
    "serial": "TEST-IO24-001",
    "vendor_id": "194f",
    "product_id": "0422",
    "bcd_device": "0128",
}


def complete_record(name="Warm Voice"):
    return {
        "preset_name": name,
        "opt": {"swapcompeq": 1},
        "filter": {"hpf": 80.0},
        "gate": {"on": 0},
        "limit": {"limiteron": 0, "threshold": -1.0},
        "comp": {"on": 0},
        "eq": {"eqallon": 0},
        "voicefx": {"on": 1},
    }


class DevicePresetLibraryTests(unittest.TestCase):
    def test_library_store_uses_prsm_16_through_27_not_stat(self):
        frames = io24._build_device_library_memp_frames(22, complete_record())

        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(struct.unpack_from("<I", frame, 20)[0], io24.PRSM)
        self.assertEqual(struct.unpack_from("<I", frame, 24)[0], 22)
        self.assertEqual(frame[41:42], b"{")

        for invalid in (15, 28, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                io24._build_device_library_memp_frames(
                    invalid, complete_record())

    def test_known_library_store_records_a_device_scoped_sent_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DevicePresetLibraryRegistry(
                Path(directory) / "device-presets.json")
            device = io24.Io24.__new__(io24.Io24)
            device.dev = type("USB", (), dict(
                serial_number=IDENTITY["serial"], idVendor=0x194F,
                idProduct=0x0422, bcdDevice=0x0128))()
            sent = []
            device._exec = lambda frame: sent.append(frame) or b""

            report = device.save_known_device_library_preset(
                22, complete_record(), registry, "user:Warm Voice",
                written_at="2026-09-20T00:00:00+00:00")

            self.assertTrue(sent)
            self.assertEqual(report["transport"]["user_index"], 22)
            self.assertEqual(report["registry"]["status"],
                             "WRITE_SENT_UNVERIFIED")
            entries = registry.entries(device_identity=IDENTITY)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["physical_channel"], 2)
            self.assertEqual(entries[0]["channel_slot"], 0)
            self.assertEqual(entries[0]["record"]["voicefx"]["on"], 1)


class _MixerDevice(io24.Io24):
    def __init__(self):
        self._shadow = {}
        self._shadow_dirty = False
        self._shadow_persist = False
        self._send_state = None
        self._solo = {}
        self.writes = []

    def _write_mix(self, source, bus, gain_db):
        self.writes.append((source, bus, gain_db))
        return self.writes[-1]


class PersistentMirrorTests(unittest.TestCase):
    def test_latch_follows_main_without_destroying_the_aux_mix(self):
        device = _MixerDevice()
        device.set_send_db("line/ch1", "mixa", -18.0)
        device.set_send_db("line/ch1", "main", -6.0)

        report = device.set_mirror_main("mixa", True)
        self.assertTrue(device.mirror_main_enabled("mixa"))
        self.assertEqual(report["sources_written"], 1)
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", -6.0))

        device.set_send_db("line/ch1", "main", -3.0)
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", -3.0))

        device.set_bus_master("mixa", -2.0)
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", -5.0))

        device.set_send_assigned("line/ch1", "main", False)
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", None))
        device.set_send_assigned("line/ch1", "main", True)
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", -5.0))

        # An aux edit made while mirrored is retained for later but does not
        # replace the mirrored signal on the wire.
        device.set_send_db("line/ch1", "mixa", -12.0)
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", -5.0))

        device.set_mirror_main("mixa", False)
        self.assertFalse(device.mirror_main_enabled("mixa"))
        self.assertEqual(device.writes[-1], ("line/ch1", "mixa", -14.0))

    def test_latch_is_shadowed_and_round_trips_through_a_scene(self):
        device = _MixerDevice()
        device.set_mirror_main("mixb", True)

        scene, omissions = io24_scene.export_snapshot({
            "live": {}, "calls": device._shadow,
        })

        self.assertEqual(scene["global"]["aux2_mirror_main"], 1)
        calls, skips = io24_scene.plan(scene)
        mirror = [call for call in calls if call[0] == "set_mirror_main"]
        self.assertEqual(mirror, [
            ("set_mirror_main", ("mixb", True), {},
             "mixb follows Main = True"),
        ])
        self.assertFalse(any("one-shot" in item for item in skips + omissions))


class ComponentNameAndSceneLibraryTests(unittest.TestCase):
    def test_uc_username_is_storable_host_component_state(self):
        device = _MixerDevice()
        device.set_component_name("line/ch1", "Narration")
        device.set_component_name("aux/ch1", "Stream")

        scene, _omissions = io24_scene.export_snapshot({
            "live": {}, "calls": device._shadow,
        })

        self.assertEqual(scene["line"]["ch1"]["username"], "Narration")
        self.assertEqual(scene["aux"]["ch1"]["username"], "Stream")
        calls, skips = io24_scene.plan(scene)
        names = [call for call in calls if call[0] == "set_component_name"]
        self.assertEqual(
            [(call[1][0], call[1][1]) for call in names],
            [("line/ch1", "Narration"), ("aux/ch1", "Stream")],
        )
        self.assertFalse(any("text label is not writable" in item
                             for item in skips))

    def test_export_includes_every_host_known_slot_and_device_library_body(self):
        presets = {
            "slots": {"0": complete_record("Front panel")},
            "userpresets": {
                "16.Warm Voice.channel": complete_record("Warm Voice"),
                "22.Keys.channel": complete_record("Keys"),
            },
        }

        scene, omissions = io24_scene.export_snapshot(
            {"live": {}, "calls": {}}, presets=presets)

        self.assertEqual(scene["presets"], presets)
        self.assertFalse(any("were not exported" in item for item in omissions))
        planned, skips = io24_scene.plan(scene)
        self.assertEqual(planned, [])
        self.assertTrue(any("2 user presets" in item for item in skips))

    def test_exported_preset_state_is_deep_copied(self):
        presets = {
            "slots": {},
            "userpresets": {
                "16.Warm.channel": complete_record("Warm"),
            },
        }
        scene, _ = io24_scene.export_snapshot(
            {"live": {}, "calls": {}}, presets=presets)
        scene["presets"]["userpresets"]["16.Warm.channel"][
            "preset_name"] = "Changed"
        self.assertEqual(
            presets["userpresets"]["16.Warm.channel"]["preset_name"],
            "Warm")


class GtkSourceContracts(unittest.TestCase):
    def test_ui_uses_device_presets_not_the_disproved_stat_writer(self):
        source = Path("io24gtk.py").read_text()
        menu_start = source.index("    def _preset_menu(")
        menu_end = source.index("    def _filter_presets(", menu_start)
        menu = source[menu_start:menu_end]
        self.assertIn("Save to device", menu)
        self.assertNotIn("Store Fat Channel on device", menu)
        self.assertIn("save_known_device_library_preset", source)

    def test_physical_main_mute_is_a_concise_display_only_state(self):
        source = Path("io24gtk.py").read_text()
        self.assertIn("Interface Mute button: Off", source)
        self.assertIn("Interface Mute button: On", source)
        self.assertIn("Read-only state of the interface's Mute button", source)
        self.assertNotIn("Physical Main Mute (read-only)", source)
        self.assertNotIn('self._set("mainmute"', source)

    def test_monitoring_separates_host_main_mute_from_headphones_mute(self):
        source = Path("io24gtk.py").read_text()
        master_start = source.index("    def _master_strip(")
        master_end = source.index("    def _mixer_page(", master_start)
        master = source[master_start:master_end]
        self.assertIn('label="Main mute"', master)
        self.assertIn('self._bus_mute_toggled, "main"', master)
        self.assertIn('(\"Phones mute\", \"hpmute\")', master)
        self.assertNotIn('(\"Output mute\", \"hpmute\")', master)


if __name__ == "__main__":
    unittest.main()
