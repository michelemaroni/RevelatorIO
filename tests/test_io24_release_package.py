import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ReleasePackageTests(unittest.TestCase):
    def test_wheel_contains_and_imports_host_runtime_and_supported_cli_modules(self):
        """Removing or breaking a shipped Host dependency must break the wheel."""
        required_modules = {
            "io24.py",
            "io24_dsp.py",
            "io24_fx.py",
            "io24_mbc.py",
            "io24_voicefx_delay.py",
            "io24_uc_comp.py",
            "io24_meters.py",
            "io24_mixer.py",
            "io24_native_stat.py",
            "io24_native_strip.py",
            "io24_preset_record.py",
            "io24_presets.py",
            "io24_alt_eq.py",
            "io24_uc472_passive_eq.py",
            "io24_uc472_vintage_eq.py",
            "io24_scene.py",
            "io24d.py",
            "io24gtk.py",
            "ucnet_shim.py",
            "calibrate.py",
        }
        required_entry_points = {
            "io24 = io24:main",
            "io24d = io24d:main",
            "io24-mixer = io24gtk:main",
            "io24-ucnet-shim = ucnet_shim:main",
            "io24-calibrate = calibrate:main",
            "io24-scene = io24_scene:main",
        }

        with tempfile.TemporaryDirectory(prefix="io24-wheel-test-") as temp:
            stage = pathlib.Path(temp)
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(ROOT / name, stage / name)
            for source in ROOT.glob("*.py"):
                shutil.copy2(source, stage / source.name)
            shutil.copy2(ROOT / "io24_uc_comp.c", stage / "io24_uc_comp.c")
            shutil.copy2(
                ROOT / "io24_voicefx_delay.c",
                stage / "io24_voicefx_delay.c")
            wheel_dir = stage / "wheel"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_dir),
                    str(stage),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            wheels = list(wheel_dir.glob("*.whl"))
            self.assertEqual(len(wheels), 1, result.stdout)

            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
                missing = required_modules - names
                entry_name = next(
                    name for name in names if name.endswith(".dist-info/entry_points.txt")
                )
                entry_points = set(
                    line.strip()
                    for line in archive.read(entry_name).decode("utf-8").splitlines()
                    if " = " in line
                )
                compressor_source = [
                    name for name in names
                    if name.endswith("/share/io24/io24_uc_comp.c")
                ]
                spring_source = [
                    name for name in names
                    if name.endswith("/share/io24/io24_spring.c")
                ]
                delay_source = [
                    name for name in names
                    if name.endswith("/share/io24/io24_voicefx_delay.c")
                ]
                licenses = [
                    name for name in names
                    if name.endswith(".dist-info/licenses/LICENSE")
                    or name.endswith(".dist-info/LICENSE")
                ]
                vendor_evidence = [
                    name for name in names
                    if name.endswith("uc_factory_presets.json")
                    or name.endswith("param_consumers.txt")
                    or name.endswith("dsp_fx_params.xml")
                    or name.endswith("dspusb_component_model.xml")
                ]

            self.assertEqual(missing, set())
            self.assertEqual(required_entry_points - entry_points, set())
            self.assertNotIn("io24web.py", names)
            self.assertNotIn("io24_spring.py", names)
            self.assertFalse(any(
                entry.startswith("io24-web =") for entry in entry_points))
            self.assertEqual(len(compressor_source), 1)
            self.assertEqual(spring_source, [])
            self.assertEqual(len(delay_source), 1)
            self.assertEqual(len(licenses), 1)
            self.assertEqual(vendor_evidence, [])

            isolated = stage / "isolated"
            isolated.mkdir()
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(wheels[0])
            # The GTK launcher depends on the distro-provided PyGObject
            # bindings, which are intentionally not a pip dependency.  Its
            # presence in the wheel is checked above; exercise every module
            # that can be imported in a hardware-free Python environment here.
            importable_modules = required_modules - {"io24gtk.py"}
            module_names = sorted(name[:-3] for name in importable_modules)
            import_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import importlib, os, sys\n"
                        "wheel = sys.argv[1]\n"
                        "for name in sys.argv[2:]:\n"
                        "    module = importlib.import_module(name)\n"
                        "    path = getattr(module, '__file__', '')\n"
                        "    if not path.startswith(wheel + os.sep):\n"
                        "        raise RuntimeError(f'{name} imported outside wheel: {path}')\n"
                        "print('IMPORTED_FROM_WHEEL=' + ','.join(sys.argv[2:]))\n"
                    ),
                    str(wheels[0]),
                    *module_names,
                ],
                cwd=isolated,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(import_result.returncode, 0, import_result.stdout)
            self.assertIn(
                "IMPORTED_FROM_WHEEL=" + ",".join(module_names),
                import_result.stdout,
            )

            scene_result = subprocess.run(
                [sys.executable, "-m", "io24_scene"],
                cwd=isolated,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(scene_result.returncode, 0, scene_result.stdout)
            self.assertIn(
                "Save or apply a Universal Control `.scene`",
                scene_result.stdout)


if __name__ == "__main__":
    unittest.main()
