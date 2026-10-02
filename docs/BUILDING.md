# Building

The supported build environment is 64-bit Windows 10 or 11 with PowerShell 5.1
or newer, a GPU with Vulkan 1.3 (the default graphics API) or Direct3D 12, an
internet connection, and about 25 GB of free disk space.

Run:

```powershell
.\tools\setup-preview.ps1 -IsoPath C:\path\to\your-disc.iso
```

The script performs six reproducible stages:

1. verifies the exact ISO size and SHA-256 against `config/supported-dumps.json`;
2. installs Visual Studio Build Tools when missing and downloads pinned portable
   tools whose hashes are recorded in `config/release-toolchain.json`;
3. initializes the pinned ShiftGlue submodule, or clones the same revision for a packaged launcher;
4. extracts the disc and generates translated source under `.local/`;
5. configures and compiles `out/build/win-amd64-release/pinyon_shift.exe`, with
   both the Vulkan and the Direct3D 12 backends; and
6. when Direct3D 12 is the chosen graphics API, prepares its shader packs from
   your local game files and validates them in a hidden, muted startup run
   before enabling play. Vulkan, the default, translates shaders as the game
   runs and needs no preparation.

For Direct3D 12 the launcher also checks graphics on every launch, including
existing installs. Valid artifacts are reused; missing, damaged or outdated
artifacts are prepared automatically for the selected resolution and graphics
driver. Preparation uses
separate temporary game states and never copies or resets your save. If it is
interrupted, reopening the launcher retries it. No shader commands are needed.

The original ISO is opened read-only and is never changed. Setup can be safely
run again after a failure; completed downloads and extraction are reused after
verification. Everything produced from the disc is ignored by Git.

To verify only the image:

```powershell
.\tools\setup-preview.ps1 -IsoPath C:\path\to\your-disc.iso -VerifyOnly
```

To rebuild after changing host or ShiftGlue code:

```powershell
.\tools\build-preview.ps1
```

To build the distributable launcher package:

```powershell
.\tools\package-launcher.ps1
```

That package contains a self-contained launcher executable and a source archive.
It deliberately excludes the compiled preview, generated translations, and all
game content. An empty `portable.txt` beside the extracted launcher (or
`--portable` on its command line) makes it a portable install that keeps
everything in a `data` folder beside it; see "Portable install" in the README.

## Linux

Linux x86-64 builds the same preview with the Vulkan backend (Direct3D 12 is
Windows-only). Install the build tools and a Vulkan driver with your
distribution's packages; on Arch Linux:

```sh
sudo pacman -S --needed clang lld cmake ninja git python vulkan-icd-loader
```

Then, from the repository:

```sh
python3 tools/pinyon.py setup --iso /path/to/your-disc.iso
python3 tools/pinyon.py launch
```

`setup` verifies the image against `config/supported-dumps.json`, prepares the
pinned ShiftGlue submodule, extracts the disc into `.local/game/base` (building
the release toolchain's extract-xiso tag under `.local/toolchain` when no
`extract-xiso` is installed), translates the game code and compiles
`out/build/linux-amd64-release/pinyon_shift`. It picks the FMA3 CPU baseline
when the processor has it and one compile job per 1.5 GB of available memory;
`--cpu-baseline` and `--jobs` override both. `python3 tools/pinyon.py build`
rebuilds after host or ShiftGlue changes, and `python3 tools/pinyon.py verify
--iso FILE` checks an image only. Logs from each step are in `.local/logs`.

On Vulkan the game translates shaders as it runs and keeps the pipelines the
driver compiles in `.local/preview/cache/shaders` for later runs, so a drive
through new scenery pauses briefly only the first time its shaders are met.
To take the opening's share of that out of play:

```sh
python3 tools/pinyon.py prepare-shaders
```

It drives the opening hidden for about two minutes, on a throwaway state that
shares the preview's cache and settings (the shaders depend on the internal
resolution), and never touches the save. Run it again after changing the
internal resolution or the graphics driver.

The graphical launcher does the same from a window: choose the ISO, verify and
build, prepare shaders, change the internal resolution, output scaling and
Treasure Map, and play. It needs GTK 4, libadwaita and PyGObject (`python-gobject`, `gtk4` and
`libadwaita` on Arch Linux):

```sh
python3 launcher/linux/pinyon_shift_launcher.py
python3 launcher/linux/pinyon_shift_launcher.py --install-desktop-entry
```

The second command adds Pinyon Shift to the desktop's application menu. The
game keeps its state (settings, saves, logs and crash reports) in
`.local/preview`, as on Windows.
