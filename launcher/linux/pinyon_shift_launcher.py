#!/usr/bin/env python3
"""Pinyon Shift launcher for Linux (GTK 4 and libadwaita).

The Linux counterpart of launcher/PinyonShift.Launcher: choose a disc image,
verify and build it locally, change the launcher's graphics settings and
play. The work itself is done by tools/pinyon.py (setup, build, launch), run
as a child process so a failure there never takes the window down.

  pinyon_shift_launcher.py [--state-root DIR]    open the launcher
  pinyon_shift_launcher.py --install-desktop-entry
                                                 add it to the desktop's menu

--state-root keeps settings, saves, logs and crash reports somewhere other
than .local/preview (a second profile, or a test).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import pinyon  # noqa: E402  tools/pinyon.py

APPLICATION_ID = "io.github.pinyonshift.Launcher"
BRANDING = ROOT / "launcher" / "PinyonShift.Launcher" / "Branding"
STATE_ROOT = ROOT / ".local" / "preview"
CONFIG = STATE_ROOT / "config" / "pinyon_shift.toml"
SETUP_STATE = ROOT / ".local" / "setup-state.json"
BUILD_DIRECTORY = pinyon.default_build_directory("Release")
EXECUTABLE = BUILD_DIRECTORY / pinyon.EXECUTABLE

# tools/set-graphics-experiment.ps1 (Get-DefaultConfigText): written when the
# game has not created its configuration yet, since the game refuses a file
# without the schema line.
DEFAULT_CONFIG = """\
# Pinyon Shift host configuration.
# Schema 27 renders on Vulkan with the split GPU commands thread; schema 26
# stops re-uploading CPU-written memory every frame; schema 25 keeps one
# occlusion-query path; schema 24 retired the renderer choice.
pinyon_shift_config_schema = 27
input_backend = "sdl"
hid_mappings_file = "gamecontrollerdb.txt"
mnk_mode = true
keybind_a = "LMB,Space"
keybind_start = "Return"
d3d12_allow_variable_refresh_rate_and_tearing = false
gpu_backend = "vulkan"
gpu_record_thread = true
vsync = true
host_present_fps_limit = 0
host_present_sleep_spin = true
pinyon_shift_stabilize_vehicle_presentation = false
pinyon_shift_skip_opening_movies = false
pinyon_shift_fh1_render_fps_limit = 0
pinyon_shift_fh1_source_presentation = true
xma_relaxed_padding_admission = false
anisotropic_override = 3
swap_post_effect = "none"
disable_motion_blur = false
disable_depth_of_field = false
draw_resolution_scale_x = 1
draw_resolution_scale_y = 1
clear_memory_page_state = false
"""
SUPPORTED_SCHEMA = 27
RESOLUTIONS = [(1, "1× (1280 × 720)"), (2, "2× (2560 × 1440)"), (3, "3× (3840 × 2160)"),
               (4, "4× (5120 × 2880)")]
OUTPUT_SCALING = [("bilinear", "Bilinear"), ("cas", "CAS (sharpened)"), ("fsr", "FSR 1")]

# Lines tools/pinyon.py setup and build print before each step, and how far
# along the whole setup each one starts. While a step writes a Ninja log, its
# [done/total] lines fill the bar up to the next step.
SETUP_STEPS = [
    ("Reading the disc image", 0.02, None),
    ("Verified", 0.15, None),
    ("Downloading", 0.30, None),
    ("Extracting the disc image", 0.42, None),
    ("Local game files are verified", 0.58, None),
    ("Configuring the ReXGlue code generator", 0.60, None),
    ("Building the ReXGlue code generator", 0.62, "rexglue-build.log"),
    ("Translating the game code", 0.72, None),
    ("Checking the translation", 0.80, None),
    ("Configuring the game build", 0.82, None),
    ("Compiling the game", 0.84, "preview-build.log"),
]
NINJA_PROGRESS = re.compile(rb"^\[(\d+)/(\d+)\]", re.MULTILINE)

CSS = """
:root { --accent-bg-color: #2a6e44; --accent-fg-color: #eef3ec; --accent-color: #5cd08a; }
@define-color accent_bg_color #2a6e44;
@define-color accent_color #5cd08a;
window.pinyon {
  background-image: linear-gradient(160deg, #1b4229 0%, #101813 45%, #0a0f0c 100%);
  color: #eef3ec;
}
.pinyon .hero-title { font-size: 40px; font-weight: 800; }
.pinyon .hero-subtitle { color: #93a597; font-size: 15px; }
.pinyon .muted { color: #93a597; }
.pinyon .faint { color: #61736a; font-size: 12px; }
.pinyon button.play {
  background: #5cd08a; color: #0a0f0c; font-size: 18px; font-weight: 700;
  min-width: 200px; min-height: 52px; border-radius: 8px;
}
.pinyon button.play:hover { background: #7ee0a4; }
.pinyon button.play:disabled { background: #2a6e44; color: #93a597; }
.pinyon button.secondary {
  background: #16211a; color: #eef3ec; min-height: 52px; min-width: 110px;
  border: 1px solid #34493b; border-radius: 8px;
}
.pinyon button.secondary:hover { background: #1d2b22; }
.pinyon button.link { color: #eef3ec; padding: 2px 8px; }
.pinyon progressbar trough { min-height: 8px; background: #34493b; border-radius: 4px; }
.pinyon progressbar progress { min-height: 8px; background: #5cd08a; border-radius: 4px; }
.pinyon textview, .pinyon textview text { background: #0a0f0c; color: #93a597; }
.pinyon .error-text { color: #e16e5f; }
"""


# Host configuration edits, with the rules of tools/host-config.ps1 and
# src/config/host_config.cpp: a setting is the first line whose name is
# followed by optional blanks and '='; setting it replaces that line or
# appends one; files are written through a temporary file.
def toml_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?m)^[ \t]*" + re.escape(name) + r"[ \t]*=(?P<value>[^#\r\n]*)")


def toml_get(text: str, name: str, default: str) -> str:
    match = toml_pattern(name).search(text)
    if match:
        value = match.group("value").strip().strip('"')
        if value:
            return value
    return default


def toml_set(text: str, name: str, value: str) -> str:
    line = f"{name} = {value}"
    pattern = re.compile(r"(?m)^[ \t]*" + re.escape(name) + r"[ \t]*=[^\r\n]*")
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    newline = "\r\n" if "\r\n" in text else "\n"
    trimmed = text.rstrip("\r\n")
    return f"{trimmed}{newline}{line}{newline}" if trimmed else f"{line}{newline}"


def read_config() -> str:
    return CONFIG.read_text(encoding="utf-8") if CONFIG.is_file() else DEFAULT_CONFIG


def write_config(text: str) -> None:
    schema = int(toml_get(text, "pinyon_shift_config_schema", "0") or 0)
    if schema != SUPPORTED_SCHEMA:
        raise ValueError(f"the configuration uses schema {schema}; start the game once so it "
                         f"migrates to {SUPPORTED_SCHEMA}")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG.is_file():
        backups = CONFIG.parent / "backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(CONFIG, backups / f"pinyon_shift-{stamp}.toml")
    newline = "\r\n" if "\r\n" in text else "\n"
    temporary = CONFIG.with_name(CONFIG.name + ".tmp")
    temporary.write_text(text.rstrip("\r\n") + newline, encoding="utf-8")
    os.replace(temporary, CONFIG)


def shader_preparation_status() -> tuple[bool, str]:
    """Whether tools/pinyon.py prepare-shaders has run for this state, and a
    line about it for the ready page."""
    receipt = STATE_ROOT / "cache" / "shaders" / pinyon.SHADER_PREPARATION_RECEIPT
    try:
        record = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    if record.get("result") == "normal-exit" and record.get("pack"):
        finished = str(record.get("finished_utc", ""))[:10]
        return True, f"Shaders prepared{(' on ' + finished) if finished else ''}."
    if record.get("result") == "normal-exit":
        return False, ("The opening's pipelines are prepared but the disc's shaders are not "
                       "packed yet: prepare shaders again.")
    return False, ("Shaders are not prepared: the first drives through new scenery pause "
                   "briefly while they compile.")


def is_ready() -> bool:
    return EXECUTABLE.is_file() and (pinyon.GAME_ROOT / "default.xex").is_file()


def release_version() -> str:
    try:
        return json.loads((ROOT / "config" / "release.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return ""


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)


class LauncherWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Pinyon Shift")
        self.set_default_size(1100, 700)
        self.add_css_class("pinyon")
        self.iso_path: Path | None = None
        self.process: subprocess.Popen | None = None
        self.step_log: str | None = None
        self.step_range = (0.0, 0.0)
        self.progress_timer = 0

        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.set_title_widget(Gtk.Label())
        logo_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(BRANDING / "logo-light.png"), -1, 26, True)
        logo = Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(logo_pixbuf))
        logo.set_margin_start(16)
        header.pack_start(logo)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.build_setup_page(), "setup")
        self.stack.add_named(self.build_progress_page(), "progress")
        self.stack.add_named(self.build_ready_page(), "ready")

        mark = Gtk.Picture.new_for_filename(str(BRANDING / "mark-light.png"))
        mark.set_opacity(0.07)
        mark.set_can_shrink(True)
        mark.set_halign(Gtk.Align.END)
        mark.set_valign(Gtk.Align.CENTER)
        mark.set_size_request(420, 420)
        mark.set_margin_end(60)
        overlay = Gtk.Overlay(child=mark)
        overlay.add_overlay(self.stack)
        overlay.set_vexpand(True)

        self.toasts = Adw.ToastOverlay(child=overlay)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(header)
        content.append(self.toasts)
        content.append(self.build_footer())
        self.set_content(content)
        self.show_state()

    # Pages.
    def hero(self, title: str, subtitle: str) -> tuple[Gtk.Box, Gtk.Label, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.START)
        box.set_margin_start(72)
        box.set_margin_end(72)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("hero-title")
        subtitle_label = Gtk.Label(label=subtitle, xalign=0, wrap=True, max_width_chars=70)
        subtitle_label.add_css_class("hero-subtitle")
        box.append(title_label)
        box.append(subtitle_label)
        return box, title_label, subtitle_label

    def build_setup_page(self) -> Gtk.Widget:
        box, _, _ = self.hero(
            "Bring your own disc",
            "Choose the ISO you dumped from your Forza Horizon (Xbox 360) disc. It is verified "
            "by its exact SHA-256, extracted and compiled on this computer; nothing is uploaded.")
        self.iso_label = Gtk.Label(label="No disc image chosen", xalign=0)
        self.iso_label.add_css_class("muted")
        self.iso_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.iso_label.set_max_width_chars(70)
        self.ownership = Gtk.CheckButton(label="I dumped this image from a disc I own")
        self.ownership.connect("toggled", lambda *_: self.update_setup_buttons())
        choose = Gtk.Button(label="Choose ISO")
        choose.add_css_class("secondary")
        choose.connect("clicked", self.on_choose_iso)
        self.build_button = Gtk.Button(label="Verify and build")
        self.build_button.add_css_class("play")
        self.build_button.connect("clicked", self.on_verify_and_build)
        buttons = Gtk.Box(spacing=12, margin_top=12)
        buttons.append(self.build_button)
        buttons.append(choose)
        note = Gtk.Label(label="The first build takes 20–60 minutes and about 25 GB of disk "
                               "space. Needs clang, cmake, ninja and git.", xalign=0, wrap=True)
        note.add_css_class("faint")
        for widget in (self.iso_label, self.ownership, buttons, note):
            box.append(widget)
        self.update_setup_buttons()
        return box

    def build_progress_page(self) -> Gtk.Widget:
        box, self.progress_title, self.progress_detail = self.hero("Building", "")
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.FILL)
        self.progress_bar = Gtk.ProgressBar(margin_top=8)
        self.progress_bar.set_size_request(520, -1)
        self.log_buffer = Gtk.TextBuffer()
        log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True,
                                cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_scroller = Gtk.ScrolledWindow(child=log_view, min_content_height=180,
                                               vexpand=False)
        expander = Gtk.Expander(label="Details", child=self.log_scroller)
        self.progress_buttons = Gtk.Box(spacing=12, margin_top=8)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.add_css_class("secondary")
        self.cancel_button.connect("clicked", self.on_cancel)
        self.retry_button = Gtk.Button(label="Back")
        self.retry_button.add_css_class("secondary")
        self.retry_button.connect("clicked", lambda *_: self.show_state())
        self.progress_buttons.append(self.cancel_button)
        self.progress_buttons.append(self.retry_button)
        for widget in (self.progress_bar, expander, self.progress_buttons):
            box.append(widget)
        return box

    def build_ready_page(self) -> Gtk.Widget:
        box, self.ready_title, self.ready_subtitle = self.hero("Ready to drive", "")
        self.play_button = Gtk.Button()
        play_content = Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Play")
        self.play_button.set_child(play_content)
        self.play_button.add_css_class("play")
        self.play_button.connect("clicked", self.on_play)
        settings = Gtk.Button(label="Settings")
        settings.add_css_class("secondary")
        settings.connect("clicked", self.on_settings)
        self.settings_button = settings
        buttons = Gtk.Box(spacing=12, margin_top=12)
        buttons.append(self.play_button)
        buttons.append(settings)
        rebuild = Gtk.Button(label="Rebuild after source changes")
        rebuild.add_css_class("flat")
        rebuild.add_css_class("faint")
        rebuild.set_halign(Gtk.Align.START)
        rebuild.connect("clicked", self.on_rebuild)
        self.rebuild_button = rebuild
        # Shader preparation: a two-minute hidden run that fills the cache.
        shaders = Gtk.Box(spacing=12, margin_top=16)
        self.shaders_label = Gtk.Label(label="", xalign=0, wrap=True, max_width_chars=60)
        self.shaders_label.add_css_class("muted")
        self.shaders_label.set_valign(Gtk.Align.CENTER)
        prepare = Gtk.Button(label="Prepare shaders")
        prepare.add_css_class("secondary")
        prepare.set_valign(Gtk.Align.CENTER)
        prepare.connect("clicked", self.on_prepare_shaders)
        self.prepare_button = prepare
        shaders.append(prepare)
        shaders.append(self.shaders_label)
        box.append(buttons)
        box.append(shaders)
        box.append(rebuild)
        return box

    def build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(spacing=4, margin_start=56, margin_end=56, margin_top=8,
                         margin_bottom=16)
        version = release_version()
        text = (f"Pinyon Shift {version} · " if version else "Pinyon Shift · ") + \
            "Not affiliated with Microsoft or Playground Games"
        label = Gtk.Label(label=text, xalign=0, hexpand=True)
        label.add_css_class("faint")
        footer.append(label)
        for name, path in (("Saves", STATE_ROOT / "user"), ("Logs", STATE_ROOT / "logs"),
                           ("Crash reports", STATE_ROOT / "crashes")):
            button = Gtk.Button(label=name)
            button.add_css_class("flat")
            button.add_css_class("link")
            button.connect("clicked", lambda _b, p=path: open_folder(p))
            footer.append(button)
        return footer

    # State.
    def show_state(self) -> None:
        if self.process is not None:
            return
        if is_ready():
            self.refresh_ready_subtitle()
            self.stack.set_visible_child_name("ready")
        else:
            self.stack.set_visible_child_name("setup")

    def refresh_ready_subtitle(self) -> None:
        text = read_config()
        scale = int(toml_get(text, "draw_resolution_scale_x", "1") or 1)
        self.ready_subtitle.set_label(
            f"Vulkan · {scale}× ({1280 * scale} × {720 * scale}) · F6 opens settings in game")
        prepared, status = shader_preparation_status()
        self.shaders_label.set_label(status)
        self.prepare_button.set_label("Prepare shaders again" if prepared else "Prepare shaders")

    def update_setup_buttons(self) -> None:
        self.build_button.set_sensitive(self.iso_path is not None and self.ownership.get_active())

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    # Setup and build.
    def on_choose_iso(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choose your Forza Horizon disc image")
        iso_filter = Gtk.FileFilter(name="Disc images (*.iso)")
        iso_filter.add_pattern("*.iso")
        iso_filter.add_pattern("*.ISO")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(iso_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self.on_iso_chosen)

    def on_iso_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            chosen = dialog.open_finish(result)
        except GLib.Error:
            return
        if chosen and chosen.get_path():
            self.iso_path = Path(chosen.get_path())
            self.iso_label.set_label(str(self.iso_path))
            self.update_setup_buttons()

    def on_verify_and_build(self, _button: Gtk.Button) -> None:
        self.run_tool(["setup", "--iso", str(self.iso_path)], "Setting up")

    def on_rebuild(self, _button: Gtk.Button) -> None:
        self.run_tool(["build"], "Rebuilding")

    def run_tool(self, arguments: list[str], title: str) -> None:
        if pinyon.game_running():
            self.toast("Close Pinyon Shift before building.")
            return
        self.log_buffer.set_text("")
        self.progress_title.set_label(title)
        self.progress_detail.set_label("Starting…")
        self.progress_detail.remove_css_class("error-text")
        self.progress_bar.set_fraction(0.0)
        self.cancel_button.set_visible(True)
        self.retry_button.set_visible(False)
        self.step_log, self.step_range = None, (0.0, 0.02)
        self.stack.set_visible_child_name("progress")
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "tools" / "pinyon.py")] + arguments,
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True)
        threading.Thread(target=self.read_tool_output, args=(self.process,), daemon=True).start()
        self.progress_timer = GLib.timeout_add(1000, self.poll_step_log)

    def read_tool_output(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            GLib.idle_add(self.on_tool_line, line.rstrip("\n"))
        code = process.wait()
        GLib.idle_add(self.on_tool_exit, code)

    def on_tool_line(self, line: str) -> bool:
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, line + "\n")
        adjustment = self.log_scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        for index, (prefix, start, log) in enumerate(SETUP_STEPS):
            if line.startswith(prefix):
                following = SETUP_STEPS[index + 1][1] if index + 1 < len(SETUP_STEPS) else 0.99
                self.step_range = (start, following)
                self.step_log = log
                self.progress_bar.set_fraction(start)
                self.progress_detail.set_label(line.rstrip("."))
                break
        if line.startswith("error:"):
            self.progress_detail.set_label(line[len("error:"):].strip())
        return False

    def poll_step_log(self) -> bool:
        if self.process is None:
            self.progress_timer = 0
            return False
        if self.step_log:
            path = pinyon.LOGS / self.step_log
            try:
                with open(path, "rb") as handle:
                    handle.seek(max(0, path.stat().st_size - 16384))
                    matches = NINJA_PROGRESS.findall(handle.read())
            except OSError:
                matches = []
            if matches:
                done, total = (int(v) for v in matches[-1])
                start, end = self.step_range
                self.progress_bar.set_fraction(start + (end - start) * done / max(total, 1))
        return True

    def on_tool_exit(self, code: int) -> bool:
        self.process = None
        if self.progress_timer:
            GLib.source_remove(self.progress_timer)
            self.progress_timer = 0
        self.cancel_button.set_visible(False)
        self.retry_button.set_visible(True)
        if code == 0:
            self.progress_bar.set_fraction(1.0)
            self.toast("Ready to play.")
            self.show_state()
        else:
            self.progress_title.set_label("Setup stopped")
            self.progress_detail.add_css_class("error-text")
            if self.progress_detail.get_label() in ("", "Starting…"):
                self.progress_detail.set_label(f"The build tool exited with code {code}. "
                                               "The details below name the failed step.")
        return False

    def on_cancel(self, _button: Gtk.Button) -> None:
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    # Play.
    def on_play(self, _button: Gtk.Button) -> None:
        if pinyon.game_running():
            self.toast("Pinyon Shift is already running.")
            return
        self.set_playing(True)
        # The game writes its console output to the same stream as the
        # launcher tool's result, for the whole session: keep it in a file.
        pinyon.LOGS.mkdir(parents=True, exist_ok=True)
        output_path = pinyon.LOGS / "launcher-game.out"
        output = open(output_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "pinyon.py"), "launch", "--json",
             "--state-root", str(STATE_ROOT)],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        output.close()
        threading.Thread(target=self.wait_for_game, args=(process, output_path),
                         daemon=True).start()

    def wait_for_game(self, process: subprocess.Popen, output_path: Path) -> None:
        code = process.wait()
        result: dict = {}
        last_error = ""
        with open(output_path, "rb") as handle:
            handle.seek(max(0, output_path.stat().st_size - 65536))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if not result and line.startswith("{"):
                try:
                    result = json.loads(line)
                except ValueError:
                    pass
            if not last_error and line.startswith("error: "):
                last_error = line
        GLib.idle_add(self.on_game_exit, code, result, last_error)

    def on_game_exit(self, code: int, result: dict, errors: str) -> bool:
        self.set_playing(False)
        if result.get("result") == "crash":
            crashes = sorted((STATE_ROOT / "crashes").glob("*.txt"),
                             key=lambda p: p.stat().st_mtime)
            detail = f" A report is in {crashes[-1].name}." if crashes else ""
            self.toast(f"Pinyon Shift stopped unexpectedly (exit code "
                       f"{result.get('exit_code')}).{detail}")
        elif code and errors:
            self.toast(errors.splitlines()[-1].removeprefix("error: "))
        return False

    def set_playing(self, playing: bool) -> None:
        self.play_button.set_sensitive(not playing)
        self.settings_button.set_sensitive(not playing)
        self.rebuild_button.set_sensitive(not playing)
        self.prepare_button.set_sensitive(not playing)
        content = self.play_button.get_child()
        content.set_label("Running" if playing else "Play")
        self.ready_title.set_label("On the road" if playing else "Ready to drive")

    # Shader preparation: tools/pinyon.py prepare-shaders, hidden, on a
    # throwaway state with this state's cache and settings.
    def on_prepare_shaders(self, _button: Gtk.Button) -> None:
        if pinyon.game_running():
            self.toast("Pinyon Shift is already running.")
            return
        self.set_playing(True)
        self.ready_title.set_label("Preparing shaders")
        self.shaders_label.set_label(
            "Translating the disc's shaders and driving the opening in the background; "
            "about three minutes.")
        pinyon.LOGS.mkdir(parents=True, exist_ok=True)
        output_path = pinyon.LOGS / "launcher-prepare-shaders.out"
        output = open(output_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "pinyon.py"), "prepare-shaders", "--json",
             "--state-root", str(STATE_ROOT)],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        output.close()
        threading.Thread(target=self.wait_for_preparation, args=(process, output_path),
                         daemon=True).start()

    def wait_for_preparation(self, process: subprocess.Popen, output_path: Path) -> None:
        code = process.wait()
        result: dict = {}
        last_error = ""
        with open(output_path, "rb") as handle:
            handle.seek(max(0, output_path.stat().st_size - 65536))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if not result and line.startswith("{"):
                try:
                    result = json.loads(line)
                except ValueError:
                    pass
            if not last_error and line.startswith("error: "):
                last_error = line
        GLib.idle_add(self.on_preparation_exit, code, result, last_error)

    def on_preparation_exit(self, code: int, result: dict, errors: str) -> bool:
        self.set_playing(False)
        self.refresh_ready_subtitle()
        if result.get("prepared"):
            self.toast("Shaders prepared: the disc's shaders are packed and the opening's "
                       "pipelines are in the cache.")
        elif errors:
            self.toast(errors.splitlines()[-1].removeprefix("error: "))
        else:
            self.toast(f"Shader preparation stopped early ({result.get('result', code)}); "
                       "what it compiled is kept.")
        return False

    # Settings.
    def on_settings(self, _button: Gtk.Button) -> None:
        SettingsDialog(self).present(self)


class SettingsDialog(Adw.PreferencesDialog):
    """The launcher's choices; everything else is in the game's F6 menu."""

    def __init__(self, window: LauncherWindow) -> None:
        super().__init__(title="Settings")
        self.window = window
        text = read_config()
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Graphics", description="Pinyon Shift renders on Vulkan. Changes apply the next "
                                          "time the game starts; the rest is in game (F6).")
        self.resolution = Adw.ComboRow(
            title="Internal resolution",
            subtitle="Higher scales are sharper and cost far more GPU time",
            model=Gtk.StringList.new([label for _, label in RESOLUTIONS]))
        scale = int(toml_get(text, "draw_resolution_scale_x", "1") or 1)
        self.resolution.set_selected(next((i for i, (s, _) in enumerate(RESOLUTIONS)
                                           if s == scale), 0))
        self.scaling = Adw.ComboRow(title="Output scaling",
                                    model=Gtk.StringList.new([l for _, l in OUTPUT_SCALING]))
        effect = toml_get(text, "present_effect", "bilinear")
        self.scaling.set_selected(next((i for i, (v, _) in enumerate(OUTPUT_SCALING)
                                        if v == effect), 0))
        self.summary = Adw.ActionRow(title="Result")
        self.summary.add_css_class("property")
        group.add(self.resolution)
        group.add(self.scaling)
        group.add(self.summary)
        extras = Adw.PreferencesGroup(title="Game")
        self.treasure_map = Adw.SwitchRow(
            title="Treasure Map",
            subtitle="Shows every discount sign and barn find on the map, as the add-on did. "
                     "A revealed map stays revealed in that save.")
        self.treasure_map.set_active(
            toml_get(text, "pinyon_shift_dlc_treasure_map", "true") == "true")
        extras.add(self.treasure_map)
        page.add(group)
        page.add(extras)
        self.add(page)
        self.initial = self.choices()
        self.update_summary()
        self.resolution.connect("notify::selected", lambda *_: self.update_summary())
        self.scaling.connect("notify::selected", lambda *_: self.update_summary())
        self.connect("closed", self.on_closed)

    def monitor_size(self) -> tuple[int, int] | None:
        display = Gdk.Display.get_default()
        surface = self.window.get_surface()
        monitor = display.get_monitor_at_surface(surface) if surface else None
        if monitor is None:
            monitors = display.get_monitors()
            monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return None
        geometry = monitor.get_geometry()
        factor = monitor.get_scale_factor()
        return geometry.width * factor, geometry.height * factor

    def update_summary(self) -> None:
        scale = RESOLUTIONS[self.resolution.get_selected()][0]
        width, height = 1280 * scale, 720 * scale
        line = f"Renders {width} × {height}"
        size = self.monitor_size()
        effect = OUTPUT_SCALING[self.scaling.get_selected()][0]
        if size and size != (width, height):
            verb = {"fsr": "FSR 1 upscales" if size[0] > width else "scaled",
                    "cas": "CAS sharpens and scales", "bilinear": "scaled"}[effect]
            line += f", {verb} to {size[0]} × {size[1]}"
        self.summary.set_subtitle(line)

    def choices(self) -> dict[str, str]:
        scale = str(RESOLUTIONS[self.resolution.get_selected()][0])
        return {"draw_resolution_scale_x": scale, "draw_resolution_scale_y": scale,
                "present_effect": '"' + OUTPUT_SCALING[self.scaling.get_selected()][0] + '"',
                "pinyon_shift_dlc_treasure_map":
                    "true" if self.treasure_map.get_active() else "false"}

    def on_closed(self, _dialog: Adw.Dialog) -> None:
        # Only the settings changed here are written: the rest belongs to the
        # in-game settings screen and must not be overwritten.
        changed = {name: value for name, value in self.choices().items()
                   if value != self.initial[name]}
        if changed:
            text = read_config()
            for name, value in changed.items():
                text = toml_set(text, name, value)
            try:
                write_config(text)
            except (OSError, ValueError) as error:
                self.window.toast(f"Settings were not saved: {error}")
                return
        self.window.refresh_ready_subtitle()


def install_desktop_entry() -> Path:
    applications = Path(GLib.get_user_data_dir()) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "pinyon-shift.desktop"
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Pinyon Shift\n"
        "Comment=Forza Horizon, recompiled to run natively\n"
        f"Exec={sys.executable} {Path(__file__).resolve()}\n"
        f"Icon={BRANDING / 'mark-light.png'}\n"
        "Terminal=false\n"
        "Categories=Game;\n"
        f"StartupWMClass={APPLICATION_ID}\n", encoding="utf-8")
    return entry


def main() -> int:
    global STATE_ROOT, CONFIG
    arguments = sys.argv[1:]
    if "--install-desktop-entry" in arguments:
        print(f"installed {install_desktop_entry()}")
        return 0
    if "--state-root" in arguments:
        index = arguments.index("--state-root")
        if index + 1 >= len(arguments):
            print("error: --state-root needs a directory", file=sys.stderr)
            return 2
        STATE_ROOT = Path(arguments[index + 1]).expanduser().resolve()
        CONFIG = STATE_ROOT / "config" / "pinyon_shift.toml"
    application = Adw.Application(application_id=APPLICATION_ID,
                                  flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def on_activate(app: Adw.Application) -> None:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        window = app.get_active_window() or LauncherWindow(app)
        window.present()

    application.connect("activate", on_activate)
    return application.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
