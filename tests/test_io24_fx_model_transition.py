#!/usr/bin/env python3
"""Hardware-free contracts copied from the UC 4.7.2 block-201 capture."""

import struct
import unittest

import io24
import io24_fx


def _frame_tag(frame):
    """Return the component tag from a fully wrapped SetP frame."""
    return struct.unpack_from("<I", frame, 20)[0]


def _body_tag(body):
    """Return the component tag after send_fx has removed the SDK header."""
    return struct.unpack_from("<I", body, 12)[0]


class VoiceFxModelTransitionTests(unittest.TestCase):
    def _device(self):
        device = object.__new__(io24.Io24)
        device.sent = []
        device._exec = device.sent.append
        return device

    def test_transformer_materialization_matches_uc472_six_frame_order(self):
        frames = io24_fx.set_fx_transformer(
            on=False, lows=0.82, width=0.8, mix=0.36, fs=48000.0)

        self.assertEqual([_frame_tag(frame) for frame in frames], [
            io24_fx.TAG_VOFX,
            io24_fx.TAG_BQDF,
            io24_fx.TAG_BQDF,
            io24_fx.TAG_MBDF,
            io24_fx.TAG_MBDF,
            io24_fx.TAG_GODV,
        ])

        # UC's MBdf payload is 0x1f4 bytes: two identifiers, four populated
        # sample-rate entries, sixteen zero-filled spare entries, then count.
        expected_rates = (44100.0, 48000.0, 88200.0, 96000.0)
        captured_first_coefficients = (
            (1.0355829000473022, 1.9081146717071533,
             -1.90394127368927, -0.9120805859565735,
             0.880670964717865),
            (1.0326546430587769, 1.91556978225708,
             -1.912034511566162, -0.9189292192459106,
             0.8898100256919861),
            (1.0176633596420288, 1.9540246725082397,
             -1.9529578685760498, -0.9550382494926453,
             0.9384417533874512),
            (1.0162180662155151, 1.957757830619812,
             -1.9568557739257812, -0.9586150646209717,
             0.943298876285553),
        )
        for table, frame in enumerate(frames[3:5]):
            blob = frame[20:]
            self.assertEqual(len(blob), 0x1F4)
            self.assertEqual(
                struct.unpack_from("<IIII", blob),
                (io24_fx.TAG_MBDF, 0x1F4, 0, table),
            )
            self.assertEqual(struct.unpack_from("<I", blob, 0x1F0)[0], 4)
            self.assertEqual(
                tuple(struct.unpack_from("<f", blob, 0x10 + 24 * i + 20)[0]
                      for i in range(4)),
                expected_rates,
            )
            self.assertEqual(blob[0x70:0x1F0], bytes(0x180))
        for i, expected in enumerate(captured_first_coefficients):
            actual = struct.unpack_from(
                "<5f", frames[3], 20 + 0x10 + 24 * i)
            for captured, rebuilt in zip(expected, actual):
                # The Windows DLL computes internally in double precision;
                # the portable builder differs by at most a few float ULPs.
                self.assertAlmostEqual(captured, rebuilt, delta=5e-7)

    def test_send_fx_has_no_host_invented_interframe_sleep(self):
        device = self._device()
        waits = []
        frames = io24_fx.set_fx_delay(
            on=True, time_s=0.25, feedback=1.0, mix=1.0)

        logical = device.send_fx(frames, sleep_fn=waits.append)

        self.assertEqual(logical, 2)
        self.assertEqual(device.sent, [frames[0][8:], frames[1][8:]])
        self.assertEqual(waits, [])

    def test_same_delay_model_edit_sends_state_without_reselection(self):
        device = self._device()

        first = device.set_fx(
            "delay", on=False, time_s=0.0350860022,
            feedback=1.0, mix=1.0, fs=48000.0)
        first_tags = [_body_tag(body) for body in device.sent]
        device.sent.clear()
        second = device.set_fx(
            "delay", on=True, time_s=0.0350860022,
            feedback=1.0, mix=1.0, fs=48000.0)

        self.assertEqual(first, 2)
        self.assertEqual(first_tags, [io24_fx.TAG_VOFX, io24_fx.TAG_VECH])
        self.assertEqual(second, 1)
        self.assertEqual([_body_tag(body) for body in device.sent],
                         [io24_fx.TAG_VECH])

    def test_delay_at_96khz_is_refused_before_any_usb_write(self):
        device = self._device()

        with self.assertRaisesRegex(RuntimeError, "96 kHz"):
            device.set_fx(
                "delay", on=True, time_s=0.173,
                feedback=0.25, mix=0.5, fs=96000.0)

        self.assertEqual(device.sent, [])
        self.assertIsNone(getattr(device, "_voicefx_selected_model", None))

    def test_delay_at_882khz_is_also_refused_before_any_usb_write(self):
        device = self._device()

        with self.assertRaisesRegex(RuntimeError, "above 48 kHz"):
            device.set_fx(
                "delay", on=True, time_s=0.173,
                feedback=0.25, mix=0.5, fs=88200.0)

        self.assertEqual(device.sent, [])
        self.assertIsNone(getattr(device, "_voicefx_selected_model", None))

    def test_host_delay_quiesce_selects_only_bypassed_transformer(self):
        device = self._device()
        waits = []

        written = device.quiesce_voicefx_for_host_delay(
            48000.0, sleep_fn=waits.append)

        self.assertEqual(written, 7)
        self.assertEqual([_body_tag(body) for body in device.sent], [
            io24_fx.TAG_VOFX,
            io24_fx.TAG_VOFX,
            io24_fx.TAG_BQDF,
            io24_fx.TAG_BQDF,
            io24_fx.TAG_MBDF,
            io24_fx.TAG_MBDF,
            io24_fx.TAG_GODV,
        ])
        self.assertEqual(waits, [io24.FX_MODEL_TRANSITION_SETTLE_S,
                                 2.0 * 512 / 48000.0])
        self.assertEqual(device._voicefx_selected_model, "transformer")
        self.assertFalse(device._voicefx_selected_state["on"])
        self.assertEqual(device._voicefx_selected_state["fs"], 48000.0)

    def test_post_ack_barrier_scales_to_two_old_rate_quanta(self):
        self.assertAlmostEqual(
            io24.voicefx_audio_settle_seconds(48000.0, 2048),
            2.0 * 2048 / 48000.0)

    def test_invalid_quantum_is_rejected_before_any_usb_write(self):
        device = self._device()

        with self.assertRaisesRegex(ValueError, "quantum"):
            device.quiesce_voicefx_for_host_delay(48000.0, quantum=0)

        self.assertEqual(device.sent, [])

    def test_delay_requires_the_current_rate_before_any_usb_write(self):
        device = self._device()

        with self.assertRaisesRegex(RuntimeError, "sample rate"):
            device.set_fx(
                "delay", on=False, time_s=0.173,
                feedback=0.25, mix=0.5)

        self.assertEqual(device.sent, [])

    def test_reconnect_replay_blocks_an_old_delay_shadow_at_96khz(self):
        device = self._device()
        device._shadow = {
            "set_fx": {
                "fn": "set_fx",
                "kwargs": {
                    "model": "delay", "on": True, "time_s": 0.173,
                    "feedback": 0.25, "mix": 0.5,
                },
            },
        }
        device._shadow_dirty = False
        device._shadow_persist = False
        device._send_state = None

        report = device.reapply_shadow(sample_rate_hz=96000.0)

        self.assertEqual(device.sent, [])
        self.assertEqual(report["applied"], 0)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("96 kHz", report["failed"][0]["error"])

    def test_same_transformer_on_or_mix_edit_sends_only_godv(self):
        device = self._device()
        state = dict(on=False, lows=0.82, width=0.8, mix=0.36)

        self.assertEqual(device.set_fx("transformer", **state), 6)
        device.sent.clear()
        state.update(on=True, mix=0.42)
        self.assertEqual(device.set_fx("transformer", **state), 1)

        self.assertEqual([_body_tag(body) for body in device.sent],
                         [io24_fx.TAG_GODV])

    def test_transformer_lows_edit_refreshes_tables_without_reselection(self):
        device = self._device()
        state = dict(on=True, lows=0.82, width=0.8, mix=0.36)
        device.set_fx("transformer", **state)
        device.sent.clear()

        state["lows"] = 0.5
        written = device.set_fx("transformer", **state)

        self.assertEqual(written, 5)
        self.assertEqual([_body_tag(body) for body in device.sent], [
            io24_fx.TAG_BQDF,
            io24_fx.TAG_BQDF,
            io24_fx.TAG_MBDF,
            io24_fx.TAG_MBDF,
            io24_fx.TAG_GODV,
        ])

    def test_switching_models_materializes_the_new_model(self):
        device = self._device()
        device.set_fx("delay", on=False, time_s=0.125,
                      feedback=0.5, mix=0.5, fs=48000.0)
        device.sent.clear()

        written = device.set_fx(
            "ringmod", on=False, carrier_hz=30.0, dist=0.5, vol=1.0,
            carrier2=False, carrier2_hz=50.0, mix=0.5)

        self.assertEqual(written, 2)
        self.assertEqual([_body_tag(body) for body in device.sent],
                         [io24_fx.TAG_VOFX, io24_fx.TAG_BOTB])


if __name__ == "__main__":
    unittest.main()
