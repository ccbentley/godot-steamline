# GodotSteamline (steamline.py)

A single Python script that builds a Godot game for Windows, Linux, macOS, and Web,
then uploads it to **Steam** (via `steamcmd`) and **itch.io** (via `butler`).

- Python **3.11+**, standard library only.
- Driven by one `steamline.toml` config file.
- Generates Steam `app_build` VDF scripts for you, so there are no hand-written depot scripts.
- Game `name`/`version` are auto-read from `project.godot` if left out of the config.

## Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| [Godot](https://godotengine.org/download) (editor binary) | builds | Export **templates** must be installed (Editor -> Manage Export Templates). Godot 3 and 4 both work. |
| [steamcmd](https://partner.steamgames.com/doc/sdk/uploading) | Steam upload | Log in once manually if Steam Guard / 2FA is involved. |
| [butler](https://itch.io/docs/butler/) | itch.io upload | Run `butler login` once before first upload. |

## Setup

```sh
# 1. Generate a config to edit (steamline.toml is gitignored, it's personal)
python steamline.py init
# then edit steamline.toml: game name, presets, Steam app/depot IDs, itch user/game

# 2. Verify everything is found and preset names match Godot
python steamline.py check -c steamline.toml

# 3. Build everything
python steamline.py build -c steamline.toml

# 4. Upload
python steamline.py steam -c steamline.toml
python steamline.py itch  -c steamline.toml
# or everything at once:
python steamline.py all   -c steamline.toml
```

Output lands in `dist/<platform>/`, and each export's size is printed after building.

## Commands

| Command | What it does |
|---|---|
| `init` | Writes an example `steamline.toml` (`--force` to overwrite). |
| `check` | Verifies config, Godot, steamcmd, butler; warns if configured export presets don't exist in `export_presets.cfg`. |
| `build` | Runs Godot headless exports (`--export-release` on Godot 4, `--export` on Godot 3). |
| `steam` | Generates `steam/scripts/app_build_<id>.vdf` with per-platform depots, then runs steamcmd. |
| `itch` | Runs `butler push` per platform with the configured channel. |
| `all` | build -> steam -> itch (`--skip-build` reuses `dist/`). |

Useful flags (available on `build`, `steam`, `itch`, `all`):

- `-p windows,linux`: restrict to specific platforms (repeatable or comma-separated)
- `--dry-run`: print every command instead of running it
- `--no-clean`: don't wipe the platform folder before exporting
- `--verbose` / `-v`: stream the full Godot export output live. By default exports run
  quietly and the log is only printed if the export fails.
- `--set-version 1.2.3` (on `build`/`all`): stamps `config/version="1.2.3"` into
  `project.godot` before exporting, so the in-game version matches what you ship

## Extras

- **Name/version auto-fill**: omit `[game] name` and/or `version` and they are read
  from `project.godot` (`config/name` / `config/version`). Explicit config values win.
- **Default output names**: if `out_file` is omitted, the export filename is derived
  from the game name: `Iskora` -> `iskora.exe`, `iskora.x86_64`, `iskora.zip`, `index.html`.
- **`--set-version`**: bump and stamp the version in one step:
  `python steamline.py all --set-version 1.2.3`
- **Substitution tokens**: `{version}`, `{name}`, and `{date}` work in
  `[steam] build_desc` and `[itch] userversion`.
- **Post-build size report**: each platform's export size is printed after building.
- **macOS depot handling**: see below.

## macOS and Steam

Steam depots contain *files*: SteamPipe does not extract archives for you, so the
macOS depot must contain the unpacked `Iskora.app/` bundle, not the zip.

1. Godot (on Windows/Linux) exports macOS as a `.zip` containing the `.app`.
   Keep this: it's also what itch.io wants (butler/itch handles the zip fine).
2. When building the Steam depot, Steamline auto-extracts that zip into
   `dist/macos/` so the depot maps `Iskora.app/...`, and excludes the `*.zip`
   from the depot to avoid shipping duplicate content.
3. Permissions caveat (from the Godot + Steamworks docs): `.app` bundles
   created on Windows lose the executable flag, and SteamPipe only preserves Unix
   file permissions when the depot is built on macOS/Linux. If you upload the mac
   depot from Windows, the inner binaries may end up without `+x` and the game
   won't launch ("damaged / can't be opened").

   Recommended setup: build wherever you like, but run `steamline steam` for the
   mac depot from a **Mac** (or Linux/WSL machine). Steamline warns when it detects
   this situation on Windows.

## Config reference

```toml
[game]
name = "My Awesome Game"   # optional, auto-read from project.godot if omitted
version = "1.0.0"          # optional, auto-read from project.godot if omitted
project = "."              # folder containing project.godot
godot = "godot"            # Godot binary: name on PATH or explicit path

[build]
output = "dist"            # per-platform subfolders are created under this
clean = true               # wipe each platform folder before export

[build.platforms.windows]  # keys: windows | linux | macos | web
preset = "Windows Desktop" # must match a preset name in Godot's Export dialog
out_file = "MyGame.exe"    # optional, defaults to <game_name>.exe/.x86_64/.zip/index.html
args = []                  # extra CLI args for the export command
steam = true               # opt out per platform by setting false
itch = true

[steam]
app_id = 1234567
user = "yoursteamlogin"
steamcmd = "steamcmd"      # optional: path or name on PATH
password_env = "STEAM_PASSWORD"  # password read from this env var, or prompted
set_live = ""              # branch to set live after upload, e.g. "Public"; leave empty to not change the live branch
build_desc = "Build {version}"   # tokens: {version}, {name}, {date}
scripts_dir = "steam/scripts"    # where the generated VDF goes
exclude = ["*.pdb"]        # global depot exclusions

[steam.depots.windows]     # one depot per platform shipping to Steam
depot_id = 1234568
exclude = []               # extra per-depot exclusions

[itch]
user = "youritchname"
game = "my-awesome-game"
butler = "butler"          # optional: path or name on PATH
userversion = "{version}"  # tokens: {version}, {name}, {date}
channels = {}              # default: windows->windows, linux->linux, macos->mac, web->html
```

### Steam notes

- Create your **depots** in the Steamworks console (one per platform) and put their IDs in
  `[steam.depots.*]`. The depot's content root is `dist/<platform>/`, mapped recursively.
- The generated build script lives at `[steam] scripts_dir`, and steamcmd writes its
  intermediate output to `steam/scripts/output/`.
- Set `STEAM_PASSWORD` (or your `password_env` name) to avoid the prompt:
  `setx STEAM_PASSWORD "..."`. If Steam Guard challenges you, run steamcmd once manually
  to complete the login, or enter the code in the console: the script streams steamcmd's
  console output directly.
- `set_live` controls which branch goes live. Leave it empty while testing so uploads
  never touch the live branch, then set it to `"Public"` (or a beta branch name) when
  you actually want to ship.

### itch.io notes

- Channel defaults: Windows -> `windows`, Linux -> `linux`, macOS -> `mac`, Web -> `html`.
- A macOS export ending in `.zip` is pushed as a single archive; otherwise the platform
  folder is pushed (which is what you want for `html` web builds).
