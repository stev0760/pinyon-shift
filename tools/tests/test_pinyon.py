import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "pinyon.py"
SPEC = importlib.util.spec_from_file_location("pinyon", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PinyonLauncherTests(unittest.TestCase):
    def test_default_build_directory_follows_the_host(self):
        with mock.patch.object(MODULE, "WINDOWS", False), \
                mock.patch.object(MODULE.platform, "system", return_value="Linux"), \
                mock.patch.object(MODULE.platform, "machine", return_value="aarch64"):
            self.assertEqual(MODULE.ROOT / "out" / "build" / "linux-arm64-release",
                             MODULE.default_build_directory("Release"))
        with mock.patch.object(MODULE, "WINDOWS", True), \
                mock.patch.object(MODULE.platform, "machine", return_value="AMD64"):
            self.assertEqual(MODULE.ROOT / "out" / "build" / "win-amd64-relwithdebinfo",
                             MODULE.default_build_directory("RelWithDebInfo"))

    def test_refuses_a_missing_build_or_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(1, MODULE.main(["launch", "--build-directory", str(root / "build"),
                                                 "--game-root", str(root / "game"),
                                                 "--state-root", str(root / "state")]))
            self.assertIn("not built", stderr.getvalue())
            (root / "build").mkdir()
            (root / "build" / MODULE.EXECUTABLE).write_bytes(b"")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(1, MODULE.main(["launch", "--build-directory", str(root / "build"),
                                                 "--game-root", str(root / "game"),
                                                 "--state-root", str(root / "state")]))
            self.assertIn("game files are missing", stderr.getvalue())
            self.assertFalse((root / "state").exists())

    def test_prepares_the_state_and_drops_a_pending_report(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            (state / "reports").mkdir(parents=True)
            (state / "reports" / "pending-report.json").write_text("{}", encoding="utf-8")
            MODULE.prepare_state(state)
            for name in MODULE.STATE_DIRECTORIES:
                self.assertTrue((state / name).is_dir())
            self.assertFalse((state / "reports" / "pending-report.json").exists())

    def test_game_arguments_follow_a_double_dash(self):
        parser_args = ["launch", "--hidden", "--", "--gpu_backend=vulkan", "--fh1_frame_dump_frame=9"]
        with mock.patch.object(MODULE, "launch", return_value={"result": "normal-exit"}) as launch:
            with redirect_stderr(io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, MODULE.main(parser_args))
        args = launch.call_args[0][0]
        self.assertTrue(args.hidden)
        self.assertEqual(["--gpu_backend=vulkan", "--fh1_frame_dump_frame=9"], args.game_arguments)

    def test_prepare_shaders_runs_the_route_on_a_throwaway_state_with_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            (state / "config").mkdir(parents=True)
            (state / "config" / "pinyon_shift.toml").write_text("draw_resolution_scale_x = 2\n",
                                                                encoding="utf-8")
            work = root / "work"
            with mock.patch.object(MODULE, "WINDOWS", False), \
                    mock.patch.object(MODULE, "SHADER_PREPARATION_WORK", work), \
                    mock.patch.object(MODULE, "launch",
                                      return_value={"result": "normal-exit", "exit_code": 0}) \
                    as launch, redirect_stderr(io.StringIO()), \
                    mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, MODULE.main(["prepare-shaders", "--state-root", str(state),
                                                 "--json"]))
            args = launch.call_args[0][0]
            self.assertEqual(work / "state", args.state_root)
            self.assertEqual(state / "cache", args.cache_root)
            self.assertTrue(args.hidden)
            self.assertEqual(MODULE.SHADER_PREPARATION_ROUTE, args.render_test_script)
            self.assertEqual("draw_resolution_scale_x = 2\n",
                             (work / "state" / "config" / "pinyon_shift.toml").read_text())
            receipt = state / "cache" / "shaders" / MODULE.SHADER_PREPARATION_RECEIPT
            self.assertEqual("normal-exit", json.loads(receipt.read_text())["result"])

    def test_verify_matches_the_image_and_the_extracted_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            iso = root / "disc.iso"
            iso.write_bytes(b"disc image" * 100)
            game = root / "game"
            game.mkdir()
            (game / "default.xex").write_bytes(b"XEX2 main")
            dumps = root / "supported-dumps.json"
            dumps.write_text(MODULE.json.dumps({"dumps": [{
                "id": "test-dump", "status": "test", "title_id": "4D5309C9", "serial": "MS-2505",
                "iso": {"size_bytes": iso.stat().st_size, "sha256": MODULE.sha256_file(iso)},
                "executables": [{"guest_path": "default.xex", "role": "entrypoint",
                                 "size_bytes": 9, "sha256": MODULE.sha256_file(game / "default.xex")}],
            }]}), encoding="utf-8")
            with mock.patch.object(MODULE, "SUPPORTED_DUMPS", dumps):
                result = MODULE.verify(MODULE.argparse.Namespace(iso=iso, extracted_root=game))
                self.assertTrue(result["recognized"])
                self.assertEqual("test-dump", result["dump_id"])
                self.assertTrue(result["extracted_executables_match"])
                (game / "default.xex").write_bytes(b"XEX2 edit")
                result = MODULE.verify(MODULE.argparse.Namespace(iso=iso, extracted_root=game))
                self.assertFalse(result["extracted_executables_match"])
                iso.write_bytes(b"disc imagf" * 100)
                result = MODULE.verify(MODULE.argparse.Namespace(iso=iso, extracted_root=None))
                self.assertFalse(result["recognized"])

    def test_cpu_baseline_follows_the_processor(self):
        def cpuinfo(flags):
            return mock.patch.object(MODULE.Path, "read_text", return_value=f"flags\t: {flags}\n")
        with cpuinfo("fpu sse4_1 avx2 fma"):
            self.assertEqual("fma", MODULE.cpu_baseline("auto"))
            self.assertEqual("sse4.1", MODULE.cpu_baseline("sse4.1"))
        with cpuinfo("fpu sse4_1"):
            self.assertEqual("sse4.1", MODULE.cpu_baseline("auto"))
            with self.assertRaises(MODULE.LaunchError):
                MODULE.cpu_baseline("fma")
        with cpuinfo("fpu ssse3"), self.assertRaises(MODULE.LaunchError):
            MODULE.cpu_baseline("auto")

    def test_build_jobs_leave_memory_for_the_translated_sources(self):
        def meminfo(gigabytes):
            return mock.patch.object(MODULE.Path, "read_text",
                                     return_value=f"MemAvailable: {gigabytes << 20} kB\n")
        with mock.patch.object(MODULE.os, "cpu_count", return_value=24):
            with meminfo(64):
                self.assertEqual(16, MODULE.build_jobs(0))
            with meminfo(12):
                self.assertEqual(8, MODULE.build_jobs(0))
            with meminfo(1):
                self.assertEqual(1, MODULE.build_jobs(0))
            self.assertEqual(3, MODULE.build_jobs(3))

    def test_setup_and_build_point_windows_at_powershell(self):
        with mock.patch.object(MODULE, "WINDOWS", True):
            for command in (["setup", "--iso", "disc.iso"], ["build"]):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(1, MODULE.main(command))
                self.assertIn(".ps1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
