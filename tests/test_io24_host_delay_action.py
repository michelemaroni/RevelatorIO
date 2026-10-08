import copy
import unittest

import io24


class _HostActionDevice:
    def __init__(self, *, channel2_slot=3, channel2_enabled=True):
        flags = 1 << 5  # Channel 1 disabled; preserve this guard as observed.
        if not channel2_enabled:
            flags |= 1 << 6
        self.params = {
            "input1Gain": 37.0,
            "input1SlotIndex": 0,
            "input1ProcessingChannel": 0,
            "input2Gain": 0.0,
            "input2SlotIndex": channel2_slot,
            "input2ProcessingChannel": 1,
            "flags": flags,
        }
        self.calls = []

    def read_params(self):
        return copy.deepcopy(self.params)

    def set_preset_enabled(self, channel, on):
        self.calls.append(("set_preset_enabled", channel, on))
        bit = 5 if channel == 1 else 6
        if on:
            self.params["flags"] &= ~(1 << bit)
        else:
            self.params["flags"] |= 1 << bit

    def set_preset_slot(self, channel, slot):
        self.calls.append(("set_preset_slot", channel, slot))
        self.params["input%dSlotIndex" % channel] = slot

    def set_fx(self, model, **kwargs):
        self.calls.append(("set_fx", model, kwargs))
        return 2


class HostDelayActionTests(unittest.TestCase):
    def test_arms_exact_delay_after_reasserting_channel2_slot3(self):
        """Fails if the host action omits, misorders, or mis-scopes a write."""
        device = _HostActionDevice()
        arm = getattr(io24, "arm_channel2_delay", lambda _device: {"status": "MISSING"})

        result = arm(device, sample_rate_hz=48000.0)

        self.assertEqual("HOST_DELAY_ARMED_CH2_REASSERTED", result["status"])
        self.assertEqual("UNPROVED", result["audibility"])
        self.assertTrue(result["channel1_unchanged"])
        self.assertEqual(2, result["delay_parameter_writes"])
        self.assertEqual([
            ("set_preset_enabled", 2, True),
            ("set_preset_slot", 2, 3),
            ("set_fx", "delay", {
                "on": True,
                "time_s": 0.173,
                "feedback": 0.25,
                "mix": 0.5,
                "fs": 48000.0,
            }),
        ], device.calls)

    def test_refuses_before_any_write_when_channel2_is_not_slot3(self):
        """Fails if an unexpected active Channel-2 slot can be overwritten."""
        device = _HostActionDevice(channel2_slot=2)
        arm = getattr(io24, "arm_channel2_delay", lambda _device: None)

        with self.assertRaisesRegex(RuntimeError, "slot 3"):
            arm(device, sample_rate_hz=48000.0)

        self.assertEqual([], device.calls)

    def test_refuses_an_unsafe_rate_before_reasserting_channel2(self):
        device = _HostActionDevice()

        with self.assertRaisesRegex(RuntimeError, "above 48 kHz"):
            io24.arm_channel2_delay(device, sample_rate_hz=96000.0)

        self.assertEqual([], device.calls)


if __name__ == "__main__":
    unittest.main()
