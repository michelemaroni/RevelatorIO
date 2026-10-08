#!/usr/bin/env python3
"""Hardware-free contracts for firmware-native io24 ``Stat`` records."""

import importlib.util
import hashlib
import os
from pathlib import Path
import struct
import unittest
from unittest import mock

import io24
import io24_fx
from io24_native_stat import (
    CHANNEL_COMPONENT_SIZES,
    NativeChunk,
    NativeStatFormatError,
    NativeStatRecord,
    build_native_delay_stat_record,
    build_native_model0_stat_record,
    build_native_voicefx_stat_record,
    build_native_slot_record,
    decode_chunk_group,
    decode_native_stat_record,
    encode_chunk_group,
    encode_native_delay_state,
    encode_native_model0_state,
    encode_native_stat_record,
    native_stat_summary,
    replace_native_channel_component,
    stock_native_stat_record,
    validate_native_stat_record,
)
import io24_dsp
import io24_native_strip
import io24_uc472_vintage_eq
from io24_preset_record import encode_preset_record


ROOT = Path(__file__).resolve().parents[1]
UC472_DLL = Path(os.environ.get(
    "IO24_UC472_DSPUSBDEVICE",
    Path.home() / ".cache" / "io24" / "re" / "dspusbdevice.dll",
))


def _has_exact_uc472_dll():
    try:
        if UC472_DLL.stat().st_size != io24_uc472_vintage_eq.DSP_SIZE:
            return False
        return hashlib.sha256(UC472_DLL.read_bytes()).hexdigest() == \
            io24_uc472_vintage_eq.DSP_SHA256
    except OSError:
        return False


HAS_EXACT_UC472_DLL = _has_exact_uc472_dll()


def _native_record(include_voicefx=True):
    channel = encode_chunk_group([
        (key, bytes([index + 1]) * size)
        for index, (key, size) in enumerate(CHANNEL_COMPONENT_SIZES.items())
    ])
    chunks = [NativeChunk(b"opt ", channel)]
    if include_voicefx:
        voicefx = (
            struct.pack("<II", 6, 5) +
            encode_chunk_group([(
                b"\x00\x00\x00\x05",
                encode_native_delay_state(enabled=False, time_s=0.19,
                                          feedback=0.2, mix=0.5),
            )])
        )
        chunks.append(NativeChunk(b"\x00\x00\x00\xc9", voicefx))
    return encode_native_stat_record(NativeStatRecord(2, tuple(chunks)))


def _tagged_record():
    return {
        "preset_name": "Reverb",
        "opt": {"swapcompeq": 0},
        "filter": {"hpf": 40.0},
        "gate": {"on": 0},
        "limit": {"limiteron": 0},
        "eq": {"eqallon": 0},
        "comp": {"on": 0},
        "voicefx": {"on": 1, "mix": 0.295},
    }


class _WireFake(io24.Io24):
    def __init__(self):
        self.payloads = []

    def _exec(self, payload, wait=1.5):
        self.payloads.append(bytes(payload))
        return struct.pack("<I", io24.RPLY)


class NativeStatTests(unittest.TestCase):
    @staticmethod
    def _standard_scene():
        return {
            "preset_name": "Test",
            "opt": {"swapcompeq": 0},
            "filter": {"hpf": 24.0},
            "gate": {
                "on": 0, "threshold": -40.0, "range": -60.0,
                "attack": 0.005, "release": 0.3,
                "keyfilter": 0.0, "expander": 1, "keylisten": 0,
            },
            "comp": {
                "__classid": "{870D04F7-212E-4F9C-ADBB-39A97216433F}",
                "on": 0, "threshold": 0.0, "ratio": 2.0,
                "attack": 0.02, "release": 0.15, "gain": 0.0,
                "softknee": 0, "automode": 0,
                "keyfilter": 20.0, "keylisten": 0,
            },
            "eq": {
                "__classid": "{A0A8A068-14F0-4B04-BB6F-AF8329D0E8EE}",
                "eqallon": 0,
                **{"eqbandon%d" % i: 0 for i in range(1, 5)},
                **{"eqfreq%d" % i: value for i, value in enumerate(
                    (120.0, 600.0, 2500.0, 8000.0), 1)},
                **{"eqgain%d" % i: 0.0 for i in range(1, 5)},
                **{"eqq%d" % i: 0.7 for i in range(1, 5)},
                "eqbandop1": 0, "eqbandop4": 0,
            },
            "limit": {"limiteron": 0, "threshold": -28.0},
            "voicefx": {
                "__classid": "{66A10093-D461-4CAC-A80C-91F6A1BB37E5}",
                "on": 0, "lows": 0.06, "width": 0.405, "mix": 0.295,
            },
        }

    def test_stock_native_base_is_self_contained_and_pinned(self):
        raw = stock_native_stat_record()

        self.assertEqual(len(raw), 0x404)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "84201c25f28bbc2590e172ebc477392dcc2615c117bfcf565c6b4bd7453bd4be",
        )
        self.assertEqual(validate_native_stat_record(raw), raw)

    def test_complete_standard_scene_builds_a_firmware_native_slot_body(self):
        scene = self._standard_scene()

        raw = build_native_slot_record(scene, 1, sample_rate_hz=96000.0)

        self.assertEqual(validate_native_stat_record(raw, slot_index=1), raw)
        top = {chunk.key: chunk.payload
               for chunk in decode_native_stat_record(raw).chunks}
        self.assertNotIn(b"\x00\x00\x00\xc9", top)
        leaves = {chunk.key: chunk.payload
                  for chunk in decode_chunk_group(top[b"opt "])}
        components = io24_native_strip.decode_components(
            decode_chunk_group(top[b"opt "]))
        identity = (1.0, 0.0, 0.0, 0.0, 0.0)
        self.assertEqual(
            components[b"filt"].filter_coefficients(),
            {rate: identity for rate in io24_native_strip.RATES},
        )
        self.assertEqual(
            components[b"gate"].gate_blob(0),
            io24_dsp.gate_blob(
                0, on=False, threshold_db=-40.0, range_db=-60.0,
                attack_s=0.005, release_s=0.3, keyfilter_hz=0.0,
                expander=True, keylisten=False, fs=96000.0),
        )
        self.assertEqual(
            components[b"comp"].compressor_blob(0),
            io24_dsp.cpxt_comp(
                0, on=False, threshold_db=0.0, ratio=2.0,
                attack_s=0.02, release_s=0.15, gain_db=0.0,
                softknee=False, automode=False, keyfilter_hz=20.0,
                keylisten=False, fs=96000.0),
        )
        self.assertEqual(components[b"lim "].limiter(), {
            "on": False,
            "inverse_threshold": io24_dsp.limiter_inv_threshold(-28.0),
            "release_coefficient": io24_dsp.limiter_release_coef(
                0.4, 96000.0),
        })
        self.assertEqual(set(leaves), set(CHANNEL_COMPONENT_SIZES))

    def test_complete_scene_rejects_truthy_text_in_gate_toggles(self):
        scene = self._standard_scene()
        scene["gate"]["on"] = "0"

        with self.assertRaisesRegex(ValueError, "gate on must be boolean"):
            build_native_slot_record(scene, 1, sample_rate_hz=96000.0)

    def test_active_delay_is_never_embedded_in_a_device_resident_block(self):
        scene = self._standard_scene()
        scene["voicefx"] = {
            "__classid": "{98A527BA-2D6E-4B35-BB26-251EC081A067}",
            "on": 1,
            "time": 0.19,
            "feedback": 0.2,
            "mix": 0.5,
        }

        with self.assertRaisesRegex(
                ValueError, "active Delay.*device-resident"):
            build_native_slot_record(scene, 1, sample_rate_hz=48000.0)

    @unittest.skipUnless(HAS_EXACT_UC472_DLL,
                         "exact UC EQ designer not retained")
    def test_complete_vintage_scene_builds_a_firmware_native_slot_body(self):
        scene = self._standard_scene()
        scene["eq"] = {
            "__classid": "{E1C5E024-C5CD-473C-B08A-6EC177812E01}",
            "eqallon": 1,
            "lowgain": 1.76, "lowfreq": 0,
            "lowmidgain": -3.84, "lowmidfreq": 0,
            "himidgain": 1.6, "himidfreq": 1,
            "higain": 0.96,
        }

        with mock.patch.dict(
                "os.environ",
                {"IO24_UC472_DSPUSBDEVICE": str(UC472_DLL)}):
            raw = build_native_slot_record(scene, 1, sample_rate_hz=96000.0)

        self.assertEqual(validate_native_stat_record(raw, slot_index=1), raw)
        top = {chunk.key: chunk.payload
               for chunk in decode_native_stat_record(raw).chunks}
        components = io24_native_strip.decode_components(
            decode_chunk_group(top[b"opt "]))
        self.assertNotEqual(
            components[b"eq  "].eq_coefficients()[96000.0]["wide"],
            (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        )

    def test_native_container_round_trips_without_hidden_prefix(self):
        raw = _native_record()

        self.assertEqual(validate_native_stat_record(raw), raw)
        self.assertEqual(encode_native_stat_record(
            decode_native_stat_record(raw)), raw)
        summary = native_stat_summary(raw)
        self.assertEqual(summary["version"], 2)
        self.assertEqual(
            [chunk["key"] for chunk in summary["channel_chunks"]],
            ["filt", "gate", "comp", "eq  ", "lim "],
        )
        self.assertEqual(summary["voicefx"]["component_id"], 201)
        self.assertEqual(summary["voicefx"]["selected_model"], 5)

    def test_embedded_default_shapes_do_not_define_slot_ownership(self):
        with_voicefx = _native_record(include_voicefx=True)
        without_voicefx = _native_record(include_voicefx=False)

        for slot in range(4):
            self.assertEqual(
                validate_native_stat_record(with_voicefx, slot_index=slot),
                with_voicefx,
            )
            self.assertEqual(
                validate_native_stat_record(without_voicefx, slot_index=slot),
                without_voicefx,
            )

    def test_model0_native_state_matches_the_mapped_godv_state_words(self):
        native = encode_native_model0_state(
            True, lows=0.06, width=0.405, mix=0.295)
        godv = io24_fx.fx_transformer_blobs(
            True, lows=0.06, width=0.405, mix=0.295)[-1]

        self.assertEqual(struct.unpack_from("<I", native)[0], 20)
        self.assertEqual(native[4:], godv[12:])

    def test_model0_builder_changes_only_the_voicefx_component(self):
        base = _native_record(include_voicefx=True)
        before = {chunk.key: chunk.payload
                  for chunk in decode_native_stat_record(base).chunks}

        result = build_native_model0_stat_record(
            base, 1, enabled=True, lows=0.06, width=0.405, mix=0.295)

        after = {chunk.key: chunk.payload
                 for chunk in decode_native_stat_record(result).chunks}
        self.assertEqual(after[b"opt "], before[b"opt "])
        voicefx = after[b"\x00\x00\x00\xc9"]
        self.assertEqual(struct.unpack_from("<II", voicefx), (6, 0))
        model = decode_chunk_group(voicefx[8:])
        self.assertEqual(model[0].key, b"\x00\x00\x00\x00")
        self.assertEqual(model[0].payload,
                         encode_native_model0_state(
                             True, lows=0.06, width=0.405, mix=0.295))
        self.assertEqual(validate_native_stat_record(result, slot_index=1), result)

        channel2_base = _native_record(include_voicefx=False)
        channel2_result = build_native_model0_stat_record(channel2_base, 3)
        self.assertIsNotNone(native_stat_summary(
            channel2_result, slot_index=3)["voicefx"])

    def test_channel_component_replacement_preserves_every_other_leaf(self):
        base = _native_record(include_voicefx=True)
        replacement = bytes(range(256)) + bytes(range(176))

        result = replace_native_channel_component(
            base, 2, b"eq  ", replacement)

        before_top = {chunk.key: chunk.payload
                      for chunk in decode_native_stat_record(base).chunks}
        after_top = {chunk.key: chunk.payload
                     for chunk in decode_native_stat_record(result).chunks}
        self.assertEqual(after_top[b"\x00\x00\x00\xc9"],
                         before_top[b"\x00\x00\x00\xc9"])
        before_channel = {chunk.key: chunk.payload for chunk in
                          decode_chunk_group(before_top[b"opt "])}
        after_channel = {chunk.key: chunk.payload for chunk in
                         decode_chunk_group(after_top[b"opt "])}
        self.assertEqual(after_channel[b"eq  "], replacement)
        for key in set(before_channel) - {b"eq  "}:
            with self.subTest(key=key):
                self.assertEqual(after_channel[key], before_channel[key])
        self.assertEqual(validate_native_stat_record(result, slot_index=2), result)

    def test_channel_component_replacement_refuses_a_partial_leaf(self):
        base = _native_record(include_voicefx=False)

        with self.assertRaisesRegex(NativeStatFormatError, "eq.*expected"):
            replace_native_channel_component(base, 0, b"eq  ", bytes(431))

    def test_chunk_group_round_trips_binary_and_numeric_keys(self):
        raw = encode_chunk_group([
            (b"opt ", b"abc"),
            (b"\x00\x00\x00\xc9", b"\x00\xff"),
        ])

        self.assertEqual(
            decode_chunk_group(raw),
            (NativeChunk(b"opt ", b"abc"),
             NativeChunk(b"\x00\x00\x00\xc9", b"\x00\xff")),
        )
        self.assertEqual(encode_chunk_group(decode_chunk_group(raw)), raw)

    def test_tagged_library_archive_is_not_native_stat(self):
        archive = encode_preset_record({"preset_name": "Slap Echo"})

        with self.assertRaisesRegex(NativeStatFormatError, "version"):
            validate_native_stat_record(archive)

    def test_component_size_mutation_is_rejected(self):
        channel = encode_chunk_group([
            (key, bytes(size - (key == b"gate")))
            for key, size in CHANNEL_COMPONENT_SIZES.items()
        ])
        raw = encode_native_stat_record(
            NativeStatRecord(2, (NativeChunk(b"opt ", channel),)))

        with self.assertRaisesRegex(NativeStatFormatError, "gate.*expected"):
            validate_native_stat_record(raw)

    def test_native_slot_envelope_contains_the_exact_native_record(self):
        raw = _native_record(include_voicefx=False)

        frames = io24._build_native_slot_memp_frames(3, raw)

        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(struct.unpack_from("<IIIIII", frame),
                         (io24.SETP, io24.APPL, 0, io24.MEMP,
                          io24.STATE_BLOB_SIZE, io24.STAT))
        self.assertEqual(struct.unpack_from("<I", frame, 24)[0], 3)
        size = struct.unpack_from("<H", frame, 35)[0]
        self.assertEqual(frame[41:41 + size], raw)

    def test_live_api_rejects_tagged_archives_before_transport(self):
        fake = _WireFake()
        archive = encode_preset_record({"preset_name": "Slap Echo"})

        with self.assertRaises(NativeStatFormatError):
            fake.save_device_slot(3, archive)
        with self.assertRaises(TypeError):
            fake.save_device_slot(3, {"preset_name": "Slap Echo"})

        self.assertEqual(fake.payloads, [])

    def test_live_api_sends_complete_native_record(self):
        fake = _WireFake()
        record = _native_record(include_voicefx=False)

        report = fake.save_device_slot(3, record)

        self.assertEqual(report, {
            "slot": 3,
            "fragments_sent": 1,
            "replies_received": 1,
        })
        self.assertEqual(len(fake.payloads), 1)
        frame = fake.payloads[0]
        size = struct.unpack_from("<H", frame, 35)[0]
        self.assertEqual(frame[41:41 + size], record)

    def test_tagged_uc_slot_api_is_retired_before_transport(self):
        fake = _WireFake()

        with self.assertRaisesRegex(
                io24.HostActionError, "tagged UC Stat archives"):
            fake.save_uc_device_slot(3, _tagged_record())

        self.assertEqual(fake.payloads, [])

    def test_native_model0_live_api_is_retired_before_transport(self):
        fake = _WireFake()
        base = _native_record(include_voicefx=True)

        with self.assertRaisesRegex(
                io24.HostActionError, "unreadable current slot"):
            fake.save_native_model0_voicefx_slot(3, base)

        self.assertEqual(fake.payloads, [])


@unittest.skipUnless(HAS_EXACT_UC472_DLL,
                     "retained exact UC 4.7.2 DLL unavailable")
class NativeStatFirmwareAttributionTests(unittest.TestCase):
    def test_exact_firmware_separates_four_native_slots_from_library(self):
        path = ROOT / "re" / "cp34_native_stat_layout.py"
        spec = importlib.util.spec_from_file_location(
            "cp34_native_stat_layout_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        report = module.analyze(UC472_DLL.read_bytes())

        self.assertEqual(
            report["status"],
            "FIRMWARE128_STAT_DEFAULTS_NATIVE_V2_SEPARATE_FROM_TAGGED_LIBRARY",
        )
        self.assertEqual(report["native_stat"]["lengths"],
                         [0x440, 0x440, 0x404, 0x404])
        self.assertEqual(report["native_stat"]["end_raw_offset"], "0x6a2b8")
        self.assertEqual(report["factory_library"]["first_raw_offset"],
                         "0x6a2b8")
        self.assertEqual(report["factory_library"]["first_prefix_hex"],
                         "7b690b70")
        self.assertEqual(
            report["correction"]["old_shared_tagged_record_model"],
            "RETRACTED",
        )
        records = report["native_stat"]["records"]
        self.assertEqual([record["version"] for record in records], [2] * 4)
        self.assertEqual([record["voicefx"] is not None for record in records],
                         [True, True, False, False])
        bridge = report["voicefx_native_bridge"]
        self.assertEqual(bridge["voicefx_storage_slot_indexes"], [0, 1])
        self.assertEqual(
            bridge["channel_strip_only_slot_indexes"], [2, 3])
        self.assertTrue(bridge["storage_owner_does_not_fix_runtime_input"])
        self.assertTrue(bridge["model5_matches_mapped_delay_state"])
        self.assertEqual(bridge["model0_serializer_raw_offset"], "0x53680")
        self.assertEqual(bridge["model0_state_bytes"], 20)

        candidate_report, artifacts = module.build_model0_candidates(
            UC472_DLL.read_bytes())
        candidates = candidate_report["model0_private_reverb_candidates"]
        self.assertEqual(candidates["physical_outcome"], "UNOBSERVED")
        self.assertEqual(
            candidates["restoration_basis"],
            "EXACT_FIRMWARE_DEFAULT_NOT_CURRENT_DEVICE_CONTENT",
        )
        self.assertEqual(len(candidates["records"]), 2)
        for slot_index in (0, 1):
            base = artifacts["slot%d-firmware-default.stat" % slot_index]
            candidate = artifacts[
                "slot%d-model0-private-reverb.stat" % slot_index]
            before = {
                chunk.key: chunk.payload
                for chunk in decode_native_stat_record(base).chunks
            }
            after = {
                chunk.key: chunk.payload
                for chunk in decode_native_stat_record(candidate).chunks
            }
            self.assertEqual(after[b"opt "], before[b"opt "])
            self.assertEqual(
                native_stat_summary(candidate, slot_index=slot_index)[
                    "voicefx"
                ]["selected_model"],
                0,
            )



class NativeDelayComponentTests(unittest.TestCase):
    _standard_scene = staticmethod(NativeStatTests._standard_scene)

    """The native Delay leaf, grounded on the firmware's own slot records.

    Firmware 1.28 ships a Delay in both Channel-1 default slots. That makes the
    leaf format an observation rather than an inference, and these tests hold it
    to the firmware's exact bytes.
    """

    FIRMWARE_DELAY_STATE = bytes.fromhex(
        "000000000000003fcdcccc3d5c8f423e")

    @staticmethod
    def _firmware_record():
        import importlib.util
        disassembler = ROOT / "re" / "io24_fw_disasm.py"
        if not disassembler.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_io24_fw_disasm", str(disassembler))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not Path(module.DLL).is_file():
            return None
        image = module.firmware()
        return bytes(image[0x68070:0x68070 + 1088])

    def test_the_leaf_is_the_live_blob_payload(self):
        """native leaf = 4-byte length + everything after the live message's
        12-byte header. This is the rule the whole format rests on."""
        leaf = encode_native_delay_state(
            enabled=False, time_s=0.19, feedback=0.2, mix=0.5)
        payload = io24_fx.fx_delay(False, 0.19, 0.2, 0.5)[12:]
        self.assertEqual(leaf, struct.pack("<I", len(payload)) + payload)
        self.assertEqual(leaf[4:], self.FIRMWARE_DELAY_STATE)

    def test_our_component_matches_the_firmware_default_record(self):
        record = self._firmware_record()
        if record is None:
            self.skipTest("the retained private firmware evidence is not present")
        theirs = native_stat_summary(record)["voicefx"]
        ours = native_stat_summary(
            build_native_delay_stat_record(
                stock_native_stat_record(), 0, enabled=False,
                time_s=0.19, feedback=0.2, mix=0.5))["voicefx"]
        self.assertEqual(theirs["selected_model"], 5)
        self.assertEqual(ours["selected_model"], theirs["selected_model"])
        self.assertEqual(ours["parent_version"], theirs["parent_version"])
        self.assertEqual(ours["bytes"], theirs["bytes"])
        self.assertEqual(ours["state_hex"], theirs["state_hex"])

    def test_an_armed_delay_round_trips_and_validates(self):
        record = build_native_delay_stat_record(
            stock_native_stat_record(), 2, enabled=True,
            time_s=0.25, feedback=1.0, mix=1.0)
        summary = native_stat_summary(record, slot_index=2)
        self.assertEqual(summary["voicefx"]["selected_model"], 5)
        self.assertEqual(summary["voicefx"]["state_bytes"], 16)
        on, mix, half, time_s = struct.unpack(
            "<Ifff", bytes.fromhex(summary["voicefx"]["state_hex"]))
        self.assertEqual(on, 1)
        self.assertAlmostEqual(mix, 1.0, places=6)
        self.assertAlmostEqual(half, 0.5, places=6)   # the blob halves feedback
        self.assertAlmostEqual(time_s, 0.25, places=6)

    def test_a_wrong_sized_delay_leaf_is_refused(self):
        good = build_native_delay_stat_record(
            stock_native_stat_record(), 0)
        broken = good.replace(self.FIRMWARE_DELAY_STATE[:8],
                              self.FIRMWARE_DELAY_STATE[:8] + b"\x00\x00")
        if broken != good:
            with self.assertRaises(NativeStatFormatError):
                validate_native_stat_record(broken)

    def test_out_of_range_delay_controls_are_refused(self):
        for kwargs in ({"time_s": 0.0}, {"time_s": 0.5}, {"mix": 1.5},
                       {"feedback": 2.0}):
            with self.assertRaises(
                    (NativeStatFormatError, ValueError)):
                encode_native_delay_state(**kwargs)

    def test_an_active_delay_is_refused_at_96khz_too(self):
        scene = self._standard_scene()
        scene["voicefx"] = {"__classid": io24_fx.VOICEFX_CLASS_IDS["delay"],
                            "on": 1, "time": 0.2, "feedback": 0.6, "mix": 1.0}
        with self.assertRaisesRegex(
                NativeStatFormatError, "active Delay.*device-resident"):
            build_native_slot_record(scene, 1, sample_rate_hz=96000.0)

    def test_a_model_without_a_decoded_leaf_is_still_refused(self):
        with self.assertRaises(NativeStatFormatError):
            build_native_voicefx_stat_record(
                stock_native_stat_record(), 0, 3, b"\x00" * 4)


if __name__ == "__main__":
    unittest.main()
