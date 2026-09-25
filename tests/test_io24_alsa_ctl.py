import copy
import json
import os
import pathlib
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "build" / "libasound_module_ctl_io24.so"
CONFIG = ROOT / "alsa" / "io24.asoundrc"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            if not raw.strip():
                continue
            request = json.loads(raw)
            self.server.requests.append(request)
            response = self.server.respond(request)
            if response is None:
                return
            if isinstance(response, bytes):
                data = response
            else:
                data = (json.dumps(response) + "\n").encode()
            self.wfile.write(data)
            self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, path):
        self.state = {
            "mainVolume": 0.82,
            "hpVolume": 0.73,
            "monitorMix": -0.25,
            "input1Gain": 40,
            "input2Gain": 12,
            "input1PhantomPower": True,
            "flags": 0,
        }
        self.requests = []
        self.mode = "normal"
        self.quantize_main_volume = False
        super().__init__(str(path), _Handler)

    def respond(self, request):
        if self.mode == "malformed":
            return b"{bad\n"
        if self.mode == "drop":
            return None
        if self.mode == "reject":
            return {"ok": False, "error": "rejected by test"}
        if request.get("cmd") == "set":
            param = request.get("param")
            value = request.get("value")
            channel = request.get("channel")
            if param == "mainvol":
                if self.quantize_main_volume:
                    value = 0.8
                self.state["mainVolume"] = value
            elif param == "hpvol":
                self.state["hpVolume"] = value
            elif param == "blend":
                self.state["monitorMix"] = value
            elif param == "gain" and channel in (1, 2):
                self.state["input{}Gain".format(channel)] = value
            elif param == "phantom" and channel == 1:
                self.state["input1PhantomPower"] = bool(value)
            elif param == "fxmix" and channel in (1, 2):
                mask = 1 << (5 if channel == 1 else 6)
                if value:
                    self.state["flags"] &= ~mask
                else:
                    self.state["flags"] |= mask
            else:
                return {"ok": False, "error": "unsupported test request"}
        elif request.get("cmd") != "status":
            return {"ok": False, "error": "unsupported test request"}
        state = copy.deepcopy(self.state)
        if self.mode == "missing" and request.get("cmd") == "status":
            state.pop("mainVolume")
        return {"ok": True, "state": state}


class _FakeDaemon:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="io24-alsa-test-")
        self.path = pathlib.Path(self.temp.name) / "io24d.sock"
        self.server = _Server(self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def run(self, *args, socket_path=None, use_runtime_dir=False, runtime_dir=None):
        environment = os.environ.copy()
        environment["ALSA_CONFIG_PATH"] = str(CONFIG)
        environment["ALSA_PLUGIN_DIR"] = str(ROOT / "build")
        environment["LC_ALL"] = "C"
        if use_runtime_dir:
            environment["XDG_RUNTIME_DIR"] = str(runtime_dir or self.path.parent)
            if socket_path is None:
                environment.pop("IO24D_SOCKET", None)
            else:
                environment["IO24D_SOCKET"] = str(socket_path)
        else:
            environment["IO24D_SOCKET"] = str(socket_path or self.path)
        return subprocess.run(
            ["amixer", "-D", "io24", *args],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )

    def set_requests(self):
        return [request for request in self.server.requests if request.get("cmd") == "set"]


@unittest.skipUnless(PLUGIN.is_file() and shutil.which("amixer"), "run make first")
class AlsaCtlTests(unittest.TestCase):
    def setUp(self):
        self.daemon = _FakeDaemon()

    def tearDown(self):
        self.daemon.close()

    def assert_amixer_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_lists_and_reads_controls(self):
        result = self.daemon.run("contents")
        self.assert_amixer_ok(result)
        for name in (
            "Main Volume",
            "Headphone Volume",
            "Monitor Blend",
            "Mic/Inst Capture Gain (dB)",
            "Headset Capture Gain (dB)",
            "Mic/Inst Capture Phantom",
            "Mic/Inst Capture Processing",
            "Headset Capture Processing",
            "Main Output Mute",
        ):
            self.assertIn(name, result.stdout)
        self.assertIn("name='Main Volume'", result.stdout)
        self.assertIn("values=82", result.stdout)
        self.assertIn("values=73", result.stdout)
        self.assertIn("values=38", result.stdout)
        self.assertIn("values=40", result.stdout)
        self.assertIn("values=12", result.stdout)
        self.assertIn("name='Main Output Mute'", result.stdout)
        self.assertIn("values=0", result.stdout)

    def test_writes_use_daemon_commands_and_readback(self):
        cases = (
            ("Main Volume", "81", "mainvol", None, 0.81),
            ("Headphone Volume", "72", "hpvol", None, 0.72),
            ("Monitor Blend", "60", "blend", None, 0.2),
            ("Mic/Inst Capture Gain (dB)", "41", "gain", 1, 41.0),
            ("Headset Capture Gain (dB)", "13", "gain", 2, 13.0),
            ("Mic/Inst Capture Phantom", "0", "phantom", 1, 0.0),
            ("Mic/Inst Capture Processing", "0", "fxmix", 1, 0.0),
            ("Headset Capture Processing", "0", "fxmix", 2, 0.0),
        )
        for name, value, param, channel, expected in cases:
            with self.subTest(name=name):
                result = self.daemon.run("cset", "name=" + name, value)
                self.assert_amixer_ok(result)
        requests = self.daemon.set_requests()
        self.assertEqual(len(requests), len(cases))
        for request, case in zip(requests, cases):
            _, _, param, channel, expected = case
            self.assertEqual(request["param"], param)
            self.assertEqual(request.get("channel"), channel)
            self.assertAlmostEqual(request["value"], expected)

    def test_uses_post_write_state(self):
        self.daemon.server.quantize_main_volume = True
        result = self.daemon.run("cset", "name=Main Volume", "81")
        self.assert_amixer_ok(result)
        self.assertIn("values=80", result.stdout)
        self.assertAlmostEqual(self.daemon.server.state["mainVolume"], 0.8)

    def test_read_only_control_is_rejected(self):
        result = self.daemon.run("cset", "name=Main Output Mute", "1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.daemon.set_requests(), [])

    def test_invalid_responses_fail_closed(self):
        cases = (
            ("malformed", ("sget", "Main")),
            ("missing", ("sget", "Main")),
            ("drop", ("sget", "Main")),
            ("reject", ("cset", "name=Main Volume", "81")),
        )
        for mode, command in cases:
            with self.subTest(mode=mode):
                self.daemon.server.mode = mode
                self.daemon.server.requests.clear()
                result = self.daemon.run(*command)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_runtime_directory_socket_path(self):
        result = self.daemon.run("contents", use_runtime_dir=True)
        self.assert_amixer_ok(result)
        self.assertTrue(self.daemon.server.requests)

    def test_explicit_socket_takes_precedence(self):
        other_runtime = self.daemon.path.parent / "other-runtime"
        other_runtime.mkdir()
        result = self.daemon.run(
            "sget",
            "Main",
            socket_path=self.daemon.path,
            use_runtime_dir=True,
            runtime_dir=other_runtime,
        )
        self.assert_amixer_ok(result)

    def test_missing_daemon_is_reported(self):
        missing = self.daemon.path.parent / "missing.sock"
        result = self.daemon.run(
            "sget",
            "Main",
            socket_path=missing
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("cannot connect", result.stdout)


if __name__ == "__main__":
    unittest.main()
