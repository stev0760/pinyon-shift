#!/usr/bin/env python3
"""Pinyon Shift command-line launcher for Windows and Linux (NP-12.7).

`launch` does what tools/launch-preview.ps1 does without PowerShell: it
checks the build and the game files, prepares the state directory, rebuilds
the enabled mods' database patches, archive members and merges, sets the
environment the game reads and starts it, then reports how it exited:

  pinyon.py launch [--state-root DIR] [--game-root DIR] [--build-directory DIR]
                   [--configuration Release|RelWithDebInfo] [--hidden] [--json]
                   [--render-test-script FILE --render-test-output DIR]
                   [-- game arguments...]

On Windows the D3D12 shader pack is prepared first through
tools/prepare-fh1-shaders.ps1 (skip it with --skip-shader-preparation); on
Linux the game runs on Vulkan, translates shaders itself and keeps the
pipelines it compiles in the state's cache for the next run. `prepare-shaders`
fills that cache before play, since every pipeline the game meets for the
first time costs it a frame or more: it runs the shader preparation route
hidden, on a throwaway state that shares the player's cache and settings,
for about two minutes:

  pinyon.py prepare-shaders [--state-root DIR] [--json]

A crash on Windows is bundled by tools/create-crash-report.ps1; elsewhere the
exit code is reported.

On Linux, `verify`, `setup` and `build` do what tools/verify-game.ps1,
setup-preview.ps1 and build-preview.ps1 do on Windows, with the system's
toolchain (clang, cmake, ninja, git) instead of provisioned copies:

  pinyon.py verify --iso FILE [--extracted-root DIR] [--json]
  pinyon.py setup --iso FILE [build options]
  pinyon.py build [--configuration Release|RelWithDebInfo] [--jobs N]
                  [--cpu-baseline auto|sse4.1|fma] [--clean-generated]

`setup` verifies the image, prepares the ShiftGlue submodule, extracts the
disc into .local/game/base (building the pinned extract-xiso from source when
none is installed) and builds; `build` compiles the code generator, translates
the game when its generated trees are missing and compiles the game.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
EXECUTABLE = "pinyon_shift.exe" if WINDOWS else "pinyon_shift"
STATE_DIRECTORIES = ("cache", "config", "crashes", "logs", "reports", "update", "user")


class LaunchError(RuntimeError):
    pass


SUPPORTED_DUMPS = ROOT / "config" / "supported-dumps.json"
# The launcher's shader preparation route, through the opening movie, the
# menus and the opening drive; what prepare-shaders runs and where it records
# having done so, under the state's cache/shaders.
SHADER_PREPARATION_ROUTE = ROOT / "config" / "render-tests" / "fh1-shader-preparation.fh1test"
SHADER_PREPARATION_RECEIPT = "fh1-vulkan-preparation.json"
SHADER_PREPARATION_WORK = ROOT / ".local" / "shader-preparation"
SDK_ROOT = ROOT / "thirdparty" / "shiftglue-sdk"
GAME_ROOT = ROOT / ".local" / "game" / "base"
GENERATED_ROOT = ROOT / ".local" / "generated"
GENERATED_TREES = ("default", "speech", "xmedia")
LOGS = ROOT / ".local" / "logs"
# The release toolchain's extract-xiso (config/release-toolchain.json), built
# from the same tag where no extract-xiso is installed.
EXTRACT_XISO_REPOSITORY = "https://github.com/XboxDev/extract-xiso.git"
EXTRACT_XISO_TAG = "build-202505152050"
EXTRACT_XISO_ROOT = ROOT / ".local" / "toolchain" / f"extract-xiso-{EXTRACT_XISO_TAG}"
# As build-preview.ps1 sets it, so identical inputs build identical outputs.
SOURCE_DATE_EPOCH = "1784764800"


def default_build_directory(configuration: str) -> Path:
    system = "win" if WINDOWS else platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return ROOT / "out" / "build" / f"{system}-{arch}-{configuration.lower()}"


def game_running() -> bool:
    if WINDOWS:
        listing = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXECUTABLE}", "/NH"],
                                 capture_output=True, text=True)
        return EXECUTABLE.lower() in listing.stdout.lower()
    if shutil.which("pgrep"):
        return subprocess.run(["pgrep", "-x", EXECUTABLE], capture_output=True).returncode == 0
    return False


def powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def prepare_state(state: Path) -> None:
    for directory in ("",) + STATE_DIRECTORIES:
        (state / directory).mkdir(parents=True, exist_ok=True)
    pending = state / "reports" / "pending-report.json"
    if pending.is_file():
        pending.unlink()


def build_mods(state: Path, game: Path, build: Path) -> None:
    """Mods' database patches (NP-10.2) and archive members and merges
    (NP-10.1, NP-10.2), rebuilt from the player's files before each start."""
    if not (state / "mods").is_dir():
        return
    patches = [sys.executable, str(ROOT / "tools" / "build-mod-patches.py"), str(state),
               "--game-root", str(game)]
    if subprocess.run(patches, stdout=subprocess.DEVNULL).returncode:
        raise LaunchError("could not build the mods' database patches")
    archives = [sys.executable, str(ROOT / "tools" / "build-mod-archives.py"), str(state),
                "--game-root", str(game)]
    extractor = build / ("pinyon_shift_fh1_archive_extract" + (".exe" if WINDOWS else ""))
    if extractor.is_file():
        archives += ["--archive-extractor", str(extractor)]
    if subprocess.run(archives, stdout=subprocess.DEVNULL).returncode:
        raise LaunchError("could not build the mods' archive members")


def prepare_shaders(state: Path, game: Path, build: Path) -> None:
    shell = powershell()
    if shell is None:
        raise LaunchError("PowerShell is needed to prepare the D3D12 shader pack "
                          "(or pass --skip-shader-preparation)")
    command = [shell, "-NoProfile", "-File", str(ROOT / "tools" / "prepare-fh1-shaders.ps1"),
               "-StateRoot", str(state), "-GameRoot", str(game), "-BuildDirectory", str(build)]
    if subprocess.run(command, stdout=subprocess.DEVNULL).returncode:
        raise LaunchError("could not prepare the shader pack")


def crash_report(state: Path, executable: Path, started: datetime, pid: int,
                 exit_code: int) -> dict:
    shell = powershell()
    if not WINDOWS or shell is None:
        return {}
    command = [shell, "-NoProfile", "-File", str(ROOT / "tools" / "create-crash-report.ps1"),
               "-StateRoot", str(state), "-Executable", str(executable),
               "-StartedUtc", started.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
               "-ProcessId", str(pid), "-ExitCode", str(exit_code), "-Json"]
    completed = subprocess.run(command, capture_output=True, text=True)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}


def launch(args: argparse.Namespace) -> dict:
    build = (args.build_directory or default_build_directory(args.configuration)).resolve()
    executable = build / EXECUTABLE
    game = (args.game_root or ROOT / ".local" / "game" / "base").resolve()
    state = (args.state_root or ROOT / ".local" / "preview").resolve()
    if not executable.is_file():
        raise LaunchError(f"the game is not built at {build}")
    if not (game / "default.xex").is_file():
        raise LaunchError(f"game files are missing at {game}")
    if game_running():
        raise LaunchError("Pinyon Shift is already running")
    prepare_state(state)
    if WINDOWS and not args.skip_shader_preparation and not args.render_test_script:
        prepare_shaders(state, game, build)
    build_mods(state, game, build)

    environment = dict(os.environ)
    environment.update({"PINYON_SHIFT_STATE_ROOT": str(state),
                        "PINYON_SHIFT_GAME_ROOT": str(game),
                        "REX_D3D12_ALLOW_VARIABLE_REFRESH_RATE_AND_TEARING": "false"})
    if getattr(args, "cache_root", None):
        environment["PINYON_SHIFT_CACHE_ROOT"] = str(args.cache_root.resolve())
    arguments = list(args.game_arguments)
    if args.hidden:
        environment["REX_WINDOW_HIDDEN"] = "1"
        arguments.append("--audio_mute=true")
    if args.render_test_script:
        environment["PINYON_SHIFT_FH1_RENDER_TEST_SCRIPT"] = str(args.render_test_script.resolve())
        if args.render_test_output:
            environment["PINYON_SHIFT_FH1_RENDER_TEST_OUTPUT"] = str(
                args.render_test_output.resolve())
        arguments.append("--pinyon_shift_skip_opening_movies=true")
    started = datetime.now(timezone.utc)
    process = subprocess.Popen([str(executable)] + arguments, cwd=str(build), env=environment)
    try:
        exit_code = process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise LaunchError(f"timed out after {args.timeout} seconds")
    if WINDOWS and exit_code >= 0x80000000:
        exit_code -= 1 << 32  # NTSTATUS, as launch-preview.ps1 reports it
    result = {"result": "normal-exit" if exit_code == 0 else "crash",
              "process_id": process.pid, "exit_code": exit_code}
    if exit_code:
        report = crash_report(state, executable, started, process.pid, exit_code)
        for key in ("crash_id", "bundle", "issue_url"):
            if key in report:
                result[key] = report[key]
    return result


def prepare_vulkan_shaders(args: argparse.Namespace) -> dict:
    """Runs the shader preparation route hidden on a throwaway state that
    shares the player's cache, so the Vulkan pipelines along it are compiled
    and saved before play. The player's settings are copied over, because the
    shaders depend on them (the draw resolution scale above all); the save is
    never touched."""
    if WINDOWS:
        raise LaunchError("on Windows the launcher prepares the D3D12 shader pack itself")
    state = (args.state_root or ROOT / ".local" / "preview").resolve()
    if not SHADER_PREPARATION_ROUTE.is_file():
        raise LaunchError(f"the shader preparation route is missing at {SHADER_PREPARATION_ROUTE}")
    work = SHADER_PREPARATION_WORK
    if work.exists():
        shutil.rmtree(work)
    throwaway = work / "state"
    (throwaway / "config").mkdir(parents=True)
    config = state / "config" / "pinyon_shift.toml"
    if config.is_file():
        shutil.copyfile(config, throwaway / "config" / "pinyon_shift.toml")
    started = datetime.now(timezone.utc)
    print("Preparing shaders (about two minutes)...", flush=True)
    result = launch(argparse.Namespace(
        configuration=args.configuration, build_directory=args.build_directory,
        game_root=args.game_root, state_root=throwaway, cache_root=state / "cache", hidden=True,
        skip_shader_preparation=True, render_test_script=SHADER_PREPARATION_ROUTE,
        render_test_output=work / "out", timeout=args.timeout or 900.0, game_arguments=[]))
    receipt = {"schema": 1, "route": SHADER_PREPARATION_ROUTE.name,
               "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "result": result["result"], "exit_code": result.get("exit_code")}
    shaders = state / "cache" / "shaders"
    shaders.mkdir(parents=True, exist_ok=True)
    receipt_path = shaders / SHADER_PREPARATION_RECEIPT
    receipt_path.write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    return {**result, "prepared": result["result"] == "normal-exit", "receipt": str(receipt_path)}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(8 << 20):
            digest.update(chunk)
    return digest.hexdigest().upper()


def supported_dumps() -> list[dict]:
    return json.loads(SUPPORTED_DUMPS.read_text(encoding="utf-8"))["dumps"]


def check_executables(dump: dict, game: Path) -> list[dict]:
    results = []
    for executable in dump["executables"]:
        path = game / executable["guest_path"]
        exists = path.is_file()
        size = path.stat().st_size if exists else None
        digest = sha256_file(path) if exists else None
        results.append({"guest_path": executable["guest_path"], "role": executable["role"],
                        "exists": exists, "size_bytes": size, "sha256": digest,
                        "matches": exists and size == int(executable["size_bytes"]) and
                        digest == executable["sha256"].upper()})
    return results


def verify(args: argparse.Namespace) -> dict:
    """tools/verify-game.ps1: the exact image size and SHA-256, and optionally
    the extracted executables."""
    iso = args.iso.resolve()
    if not iso.is_file():
        raise LaunchError(f"no disc image at {iso}")
    size = iso.stat().st_size
    candidates = [d for d in supported_dumps() if int(d["iso"]["size_bytes"]) == size]
    digest = sha256_file(iso) if candidates else None
    dump = next((d for d in candidates if d["iso"]["sha256"].upper() == digest), None)
    if dump is None:
        return {"recognized": False, "size_bytes": size, "sha256": digest,
                "reason": "No exact size and SHA-256 match in supported-dumps.json."}
    result = {"recognized": True, "dump_id": dump["id"], "status": dump["status"],
              "title_id": dump["title_id"], "serial": dump["serial"],
              "iso_size_bytes": size, "iso_sha256": digest}
    if args.extracted_root:
        executables = check_executables(dump, args.extracted_root.resolve())
        result["executables"] = executables
        result["extracted_executables_match"] = all(e["matches"] for e in executables)
    return result


def require_tools(*names: str) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise LaunchError("missing build tools: " + ", ".join(missing) +
                          " (install them with your distribution's package manager)")


def run_logged(command: list[str], log: Path, step: str, cwd: Path,
               environment: dict | None = None) -> None:
    """Runs a build step with its output in .local/logs; a failure names the
    step and ends with the log's last lines."""
    LOGS.mkdir(parents=True, exist_ok=True)
    print(f"{step}...", flush=True)
    with open(log, "w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=cwd, env=environment, stdout=handle,
                                   stderr=subprocess.STDOUT)
    if completed.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        raise LaunchError(f"{step} failed (exit code {completed.returncode}); see {log}\n" +
                          "\n".join(tail))


def cpu_baseline(requested: str) -> str:
    flags: set[str] = set()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                flags = set(line.split(":", 1)[1].split())
                break
    except OSError:
        pass
    if flags and "sse4_1" not in flags:
        raise LaunchError("this CPU lacks SSE4.1, the minimum Pinyon Shift supports")
    if requested != "auto":
        if requested == "fma" and flags and "fma" not in flags:
            raise LaunchError("this CPU lacks FMA3; build with --cpu-baseline sse4.1")
        return requested
    # NP-3.5: FMA3 builds the same results faster where the CPU has it.
    return "fma" if "fma" in flags else "sse4.1"


def build_jobs(requested: int) -> int:
    """Parallel compile jobs: the logical processors less one, at most 16 and
    about 1.5 GB of available memory each (the translated sources are large)."""
    if requested:
        return requested
    jobs = max(2, min(16, (os.cpu_count() or 2) - 1))
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available_gb = int(line.split()[1]) / (1 << 20)
                jobs = max(1, min(jobs, int(available_gb / 1.5)))
                break
    except OSError:
        pass
    return jobs


def git_state(repository: Path) -> tuple[str, bool]:
    if not (repository / ".git").exists() or shutil.which("git") is None:
        return "unknown", False
    commit = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip().lower()
    dirty = bool(subprocess.run(["git", "-C", str(repository), "status", "--porcelain",
                                 "--ignore-submodules=dirty"],
                                capture_output=True, text=True).stdout.strip())
    return commit or "unknown", dirty


def build(args: argparse.Namespace) -> dict:
    """tools/build-preview.ps1 on Linux: the code generator, the translated
    game code and the game itself."""
    if WINDOWS:
        raise LaunchError("on Windows, build with tools/build-preview.ps1")
    require_tools("cmake", "ninja", "clang", "clang++", "git")
    if not (SDK_ROOT / "CMakeLists.txt").is_file():
        raise LaunchError(f"the ShiftGlue source is missing at {SDK_ROOT}; run setup first")
    xex = GAME_ROOT / "default.xex"
    if not xex.is_file():
        raise LaunchError(f"game files are missing at {GAME_ROOT}; run setup first")
    # Every supported dump carries the same main-XEX code (the analysis
    # addresses in config/rexglue hold for each); a different default.xex
    # would translate wrongly.
    xex_size, xex_sha256 = xex.stat().st_size, sha256_file(xex)
    if not any(e["guest_path"] == "default.xex" and int(e["size_bytes"]) == xex_size and
               e["sha256"].upper() == xex_sha256
               for d in supported_dumps() for e in d["executables"]):
        raise LaunchError("default.xex does not match a supported dump")

    environment = dict(os.environ, SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH)
    jobs = build_jobs(args.jobs)
    baseline = cpu_baseline(args.cpu_baseline)
    flags = "-msse4.1 -mfma -ffp-contract=off" if baseline == "fma" else "-msse4.1"
    machine = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    print(f"Compiling with {jobs} parallel jobs; CPU baseline {baseline}.", flush=True)

    run_logged(["cmake", "--preset", f"linux-{machine}", "-DREXGLUE_ENABLE_TRACY=OFF"],
               LOGS / "rexglue-configure.log", "Configuring the ReXGlue code generator",
               SDK_ROOT, environment)
    run_logged(["cmake", "--build", "--preset", f"linux-{machine}-release", "--target",
                "rexglue", "--parallel", str(jobs)],
               LOGS / "rexglue-build.log", "Building the ReXGlue code generator",
               SDK_ROOT, environment)
    generator = SDK_ROOT / "out" / f"linux-{machine}" / "Release" / "rexglue"
    if not generator.is_file():
        raise LaunchError(f"the code generator was not produced: {generator}")

    if args.clean_generated and GENERATED_ROOT.exists():
        shutil.rmtree(GENERATED_ROOT)
    stamps = [GENERATED_ROOT / tree / "codegen.build.stamp" for tree in GENERATED_TREES]
    if args.clean_generated or not (GENERATED_ROOT / "default" / "codegen.build.stamp").is_file() or \
            not all((GENERATED_ROOT / tree / "sources.cmake").is_file()
                    for tree in GENERATED_TREES):
        GENERATED_ROOT.mkdir(parents=True, exist_ok=True)
        codegen_log = LOGS / "codegen.log"
        if codegen_log.exists():
            codegen_log.unlink()
        try:
            run_logged([str(generator), "--log-level", "info", "--log-file", str(codegen_log),
                        "codegen", str(ROOT / "config" / "rexglue" / "pinyon_shift_manifest.toml")],
                       LOGS / "codegen-output.log", "Translating the game code", ROOT, environment)
            run_logged([sys.executable, str(ROOT / "tools" / "verify-codegen-log.py"),
                        str(codegen_log)], LOGS / "codegen-verify.log",
                       "Checking the translation's warnings", ROOT, environment)
        except LaunchError:
            # Incomplete generated trees must be translated again next time.
            for stamp in stamps:
                stamp.unlink(missing_ok=True)
            raise

    preset = f"linux-{machine}-{args.configuration.lower()}"
    run_logged(["cmake", "--preset", preset, f"-DREXSDK_DIR={SDK_ROOT}",
                "-DPINYON_SHIFT_HOST_TESTS_ONLY=OFF", f"-DPINYON_SHIFT_CPU_BASELINE={baseline}",
                f"-DCMAKE_C_FLAGS={flags}", f"-DCMAKE_CXX_FLAGS={flags}",
                f"-DPYTHON_EXECUTABLE={sys.executable}"],
               LOGS / "preview-configure.log", "Configuring the game build", ROOT, environment)
    run_logged(["cmake", "--build", "--preset", preset, "--parallel", str(jobs)],
               LOGS / "preview-build.log", "Compiling the game (the longest step)", ROOT,
               environment)
    build_directory = ROOT / "out" / "build" / preset
    executable = build_directory / EXECUTABLE
    if not executable.is_file():
        raise LaunchError(f"compilation completed without producing {executable}")

    commit, dirty = git_state(ROOT)
    sdk_commit, sdk_dirty = git_state(SDK_ROOT)
    patch_set = ROOT / "config" / "rexglue" / "analysis" / "fh1-post-processing.toml"
    manifest = {
        "schema_version": 3,
        "configuration": args.configuration,
        "cpu_baseline": baseline,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "executable": str(executable.relative_to(ROOT)),
        "executable_sha256": sha256_file(executable),
        "generated_locally": True,
        "pinyon_shift_commit": commit,
        "pinyon_shift_dirty": str(dirty).lower(),
        "pinyon_shift_source_payload_sha256": "",
        "rexglue_commit": sdk_commit,
        "rexglue_dirty": str(sdk_dirty).lower(),
        "guest_executable_sha256": xex_sha256,
        "guest_codegen_patch_profile": "fh1-retail-base-post-processing-v1",
        "guest_codegen_patch_set_sha256": sha256_file(patch_set),
    }
    text = json.dumps(manifest, indent=2) + "\n"
    name = "build.json" if args.configuration == "Release" else "build-profile.json"
    (ROOT / ".local" / name).write_text(text, encoding="utf-8")
    (build_directory / "pinyon_shift_build.json").write_text(text, encoding="utf-8")
    return {"result": "built", "executable": str(executable), "cpu_baseline": baseline}


def extract_xiso() -> Path:
    """An installed extract-xiso, or the release toolchain's tag built once
    under .local/toolchain."""
    installed = shutil.which("extract-xiso")
    if installed:
        return Path(installed)
    built = EXTRACT_XISO_ROOT / "build" / "extract-xiso"
    if built.is_file():
        return built
    require_tools("git", "cmake", "ninja")
    source = EXTRACT_XISO_ROOT / "source"
    if source.exists():
        shutil.rmtree(source)
    run_logged(["git", "clone", "--depth", "1", "--branch", EXTRACT_XISO_TAG,
                EXTRACT_XISO_REPOSITORY, str(source)],
               LOGS / "extract-xiso-clone.log", "Downloading extract-xiso", ROOT)
    run_logged(["cmake", "-S", str(source), "-B", str(EXTRACT_XISO_ROOT / "build"), "-G", "Ninja",
                "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"],
               LOGS / "extract-xiso-configure.log", "Configuring extract-xiso", ROOT)
    run_logged(["cmake", "--build", str(EXTRACT_XISO_ROOT / "build")],
               LOGS / "extract-xiso-build.log", "Building extract-xiso", ROOT)
    if not built.is_file():
        raise LaunchError(f"extract-xiso was not produced at {built}")
    return built


def prepare_sdk() -> None:
    """tools/prepare-rexglue.ps1 for a Git checkout: the pinned ShiftGlue
    submodule and its dependencies. Never resets local SDK changes."""
    if not (ROOT / ".git").exists():
        if not (SDK_ROOT / "CMakeLists.txt").is_file():
            raise LaunchError("this is not a Git checkout and the ShiftGlue source is missing")
        return
    require_tools("git")
    if not (SDK_ROOT / ".git").exists():
        run_logged(["git", "-C", str(ROOT), "submodule", "update", "--init", "--recursive",
                    "--jobs", "8", "--", "thirdparty/shiftglue-sdk"],
                   LOGS / "rexglue-prepare.log", "Downloading the pinned ShiftGlue source", ROOT)
    else:
        run_logged(["git", "-C", str(SDK_ROOT), "submodule", "update", "--init", "--recursive",
                    "--jobs", "8"],
                   LOGS / "rexglue-prepare.log", "Downloading ShiftGlue dependencies", ROOT)


def setup(args: argparse.Namespace) -> dict:
    """tools/setup-preview.ps1 on Linux."""
    if WINDOWS:
        raise LaunchError("on Windows, set up with tools/setup-preview.ps1")
    if game_running():
        raise LaunchError("close Pinyon Shift before building")
    print("Reading the disc image. Nothing is uploaded.", flush=True)
    verification = verify(argparse.Namespace(iso=args.iso, extracted_root=None))
    if not verification["recognized"]:
        raise LaunchError("this disc image is not a supported revision")
    print(f"Verified {verification['serial']} ({verification['dump_id']}) by exact SHA-256.",
          flush=True)
    dump = next(d for d in supported_dumps() if d["id"] == verification["dump_id"])
    prepare_sdk()
    if not (GAME_ROOT / "default.xex").is_file() or \
            not all(e["matches"] for e in check_executables(dump, GAME_ROOT)):
        tool = extract_xiso()
        if GAME_ROOT.exists():
            shutil.rmtree(GAME_ROOT)
        GAME_ROOT.mkdir(parents=True)
        run_logged([str(tool), "-q", "-s", "-x", "-d", str(GAME_ROOT), str(args.iso.resolve())],
                   LOGS / "extract.log",
                   "Extracting the disc image locally (the image is not modified)", ROOT)
        if not all(e["matches"] for e in check_executables(dump, GAME_ROOT)):
            raise LaunchError("the extracted game executables failed verification")
    print("Local game files are verified.", flush=True)
    result = build(args)
    state = {"schema_version": 1, "completed_utc": datetime.now(timezone.utc).isoformat(),
             "dump_id": verification["dump_id"], "iso_sha256": verification["iso_sha256"],
             "result": "ready"}
    (ROOT / ".local" / "setup-state.json").write_text(json.dumps(state, indent=2) + "\n",
                                                      encoding="utf-8")
    return result


def add_build_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--configuration", choices=("Release", "RelWithDebInfo"),
                         default="Release")
    command.add_argument("--jobs", type=int, default=0,
                         help="parallel compile jobs (default: from processors and memory)")
    command.add_argument("--cpu-baseline", choices=("auto", "sse4.1", "fma"), default="auto")
    command.add_argument("--clean-generated", action="store_true",
                         help="translate the game code again from scratch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch", help="start the game")
    start.add_argument("--configuration", choices=("Release", "RelWithDebInfo"),
                       default="Release")
    start.add_argument("--build-directory", type=Path)
    start.add_argument("--game-root", type=Path)
    start.add_argument("--state-root", type=Path)
    start.add_argument("--cache-root", type=Path,
                       help="use this cache directory instead of the state's own")
    start.add_argument("--hidden", action="store_true", help="no window, audio muted")
    start.add_argument("--skip-shader-preparation", action="store_true")
    start.add_argument("--render-test-script", type=Path)
    start.add_argument("--render-test-output", type=Path)
    start.add_argument("--timeout", type=float, help="seconds before the game is stopped")
    start.add_argument("--json", action="store_true", help="print the result as JSON")
    start.add_argument("game_arguments", nargs="*", help="after --, passed to the game")
    check = commands.add_parser("verify", help="check a disc image against the supported dumps")
    check.add_argument("--iso", type=Path, required=True)
    check.add_argument("--extracted-root", type=Path)
    check.add_argument("--json", action="store_true", help="print the result as JSON")
    prepare = commands.add_parser("setup", help="verify, extract and build (Linux)")
    prepare.add_argument("--iso", type=Path, required=True)
    prepare.add_argument("--json", action="store_true", help="print the result as JSON")
    add_build_options(prepare)
    compile_ = commands.add_parser("build", help="translate and compile the game (Linux)")
    compile_.add_argument("--json", action="store_true", help="print the result as JSON")
    add_build_options(compile_)
    shaders = commands.add_parser(
        "prepare-shaders", help="compile the opening's Vulkan pipelines into the cache (Linux)")
    shaders.add_argument("--configuration", choices=("Release", "RelWithDebInfo"),
                         default="Release")
    shaders.add_argument("--build-directory", type=Path)
    shaders.add_argument("--game-root", type=Path)
    shaders.add_argument("--state-root", type=Path)
    shaders.add_argument("--timeout", type=float, help="seconds before the run is stopped")
    shaders.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    handlers = {"launch": launch, "verify": verify, "setup": setup, "build": build,
                "prepare-shaders": prepare_vulkan_shaders}
    try:
        result = handlers[args.command](args)
    except LaunchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items()))
    if args.command == "verify":
        return 0 if result["recognized"] and result.get("extracted_executables_match", True) else 1
    return 0 if result["result"] in ("normal-exit", "built") else 1


if __name__ == "__main__":
    sys.exit(main())
