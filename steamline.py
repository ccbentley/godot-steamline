#!/usr/bin/env python3
"""
Steamline - build Godot games for multiple platforms, then ship them
to Steam (via steamcmd) and itch.io (via butler).

Usage:
    python steamline.py init                 # create an example steamline.toml
    python steamline.py check                # verify godot / steamcmd / butler
    python steamline.py build [-p windows,linux]
    python steamline.py steam [-p ...]       # generate VDFs + upload via steamcmd
    python steamline.py itch  [-p ...]       # upload via butler
    python steamline.py all   [-p ...]       # build + steam + itch

Requires Python 3.11+ (for tomllib). Everything else is stdlib only.
External tools used (when configured): Godot editor binary, steamcmd, butler.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

PROGRAM = "steamline"

SUPPORTED = ("windows", "linux", "macos", "web")
# Default itch.io channel per platform
ITCH_CHANNELS = {"windows": "windows", "linux": "linux", "macos": "mac", "web": "html"}


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


_COLOR = use_color()


def c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return c("1", t)


def red(t: str) -> str:
    return c("31", t)


def green(t: str) -> str:
    return c("32", t)


def yellow(t: str) -> str:
    return c("33", t)


def cyan(t: str) -> str:
    return c("36", t)


def info(msg: str) -> None:
    print(f"{cyan('==>')} {msg}")


def warn(msg: str) -> None:
    print(f"{yellow('WARN:')} {msg}")


def die(msg: str, code: int = 1) -> "None":
    print(red(f"ERROR: {msg}"), file=sys.stderr)
    sys.exit(code)


def cmd_str(args: list[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in args)


class Step:
    """Runs a subprocess. By default Godot exports are captured and only shown on
    failure; `--verbose` (or explicit stream=True) streams output live instead."""

    def __init__(self, dry_run: bool = False, verbose: bool = False):
        self.dry_run = dry_run
        self.verbose = verbose

    def run(self, args: list[str], capture: bool = False, stream: bool | None = None) -> subprocess.CompletedProcess:
        if self.dry_run:
            print(f"  {yellow('[dry-run]')} {cmd_str(args)}")
            return subprocess.CompletedProcess(args, 0, "", "")
        if stream is None:
            stream = self.verbose and not capture
        if stream:
            proc = subprocess.run([str(a) for a in args])
        else:
            proc = subprocess.run(
                [str(a) for a in args], capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
        if proc.returncode != 0:
            if not stream:
                if proc.stdout:
                    sys.stderr.write(proc.stdout)
                if proc.stderr:
                    sys.stderr.write(proc.stderr)
            die(f"command failed (exit {proc.returncode}): {cmd_str(args)}", proc.returncode)
        return proc


def resolve(cmd: str) -> str | None:
    """Expand an 'on PATH or explicit path' tool reference to a usable path."""
    p = Path(cmd)
    if p.is_file():
        return str(p)
    found = shutil.which(cmd)
    if found:
        return found
    # Windows: try common suffixes
    if os.name == "nt" and not p.suffix:
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(cmd + ext)
            if found:
                return found
    return None


def substitute(text: str, cfg: "Config") -> str:
    return (
        text.replace("{version}", cfg.game.version)
        .replace("{name}", cfg.game.name)
        .replace("{date}", date.today().isoformat())
    )


def default_out_file(name: str, platform: str) -> str:
    """Output filename derived from the game name, e.g. 'Iskora' -> 'iskora.exe'."""
    base = re.sub(r"[^\w-]+", "_", name).strip("_").lower() or "game"
    return {
        "windows": f"{base}.exe",
        "linux": f"{base}.x86_64",
        "macos": f"{base}.zip",
        "web": "index.html",
    }[platform]


def read_godot_project_info(project_dir: Path) -> dict[str, str]:
    """Pull name/version defaults out of project.godot's [application] section."""
    pf = project_dir / "project.godot"
    if not pf.is_file():
        return {}
    meta: dict[str, str] = {}
    for line in pf.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r'\s*(config/name|config/version|application/version)\s*=\s*"([^"]*)"', line)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta


def stamp_version(cfg: "Config", version: str, dry_run: bool) -> None:
    """Write config/version=<version> into project.godot so in-game version matches the build."""
    pf = cfg.project_dir / "project.godot"
    if not pf.is_file():
        die(f"project.godot not found in {cfg.project_dir}")
    text = pf.read_text(encoding="utf-8")
    entry = f'config/version="{version}"'
    if re.search(r"^\s*config/version\s*=", text, re.M):
        new = re.sub(r"^(\s*)config/version\s*=.*$", lambda m: f"{m.group(1)}{entry}", text, flags=re.M)
    elif re.search(r"^\[application\]\s*$", text, re.M):
        new = re.sub(r"^\[application\]\s*$", f"[application]\n{entry}", text, count=1, flags=re.M)
    else:
        new = f"[application]\n{entry}\n\n" + text
    if dry_run:
        print(f"  {yellow('[dry-run]')} would set {entry} in {pf}")
        return
    pf.write_text(new, encoding="utf-8")
    info(f"stamped config/version = {version} into {pf}")


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human_size(n: int) -> str:
    mb = n / 1048576
    return f"{mb:.1f} MB" if mb >= 1 else f"{max(n, 0) / 1024:.0f} KB"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Game:
    name: str = ""
    version: str = "0.0.0"
    project: str = "."
    godot: str = "godot"


@dataclass
class PlatformCfg:
    preset: str = ""
    out_file: str = ""
    args: list[str] = field(default_factory=list)
    steam: bool | None = None   # None = auto (yes if a steam depot exists)
    itch: bool | None = None    # None = auto (yes if itch section exists)


@dataclass
class Build:
    output: str = "dist"
    clean: bool = True
    platforms: dict[str, PlatformCfg] = field(default_factory=dict)


@dataclass
class SteamDepot:
    depot_id: int = 0
    exclude: list[str] = field(default_factory=list)


@dataclass
class Steam:
    app_id: int = 0
    user: str = ""
    steamcmd: str = "steamcmd"
    password_env: str = "STEAM_PASSWORD"
    set_live: str = ""   # empty = do not change the live branch
    build_desc: str = "Build {version}"
    scripts_dir: str = "steam/scripts"
    exclude: list[str] = field(default_factory=list)
    depots: dict[str, SteamDepot] = field(default_factory=dict)


@dataclass
class Itch:
    user: str = ""
    game: str = ""
    butler: str = "butler"
    channels: dict[str, str] = field(default_factory=dict)
    userversion: str = "{version}"


@dataclass
class Config:
    path: Path
    game: Game
    build: Build
    steam: Steam | None
    itch: Itch | None

    @property
    def project_dir(self) -> Path:
        return (self.path.parent / self.game.project).resolve()

    def out_path_for(self, platform: str) -> tuple[Path, Path]:
        """(platform output dir, exported file path); out_file defaults to a name derived from the game."""
        out_dir = (self.path.parent / self.build.output / platform).resolve()
        out_file = self.build.platforms[platform].out_file or default_out_file(self.game.name, platform)
        return out_dir, out_dir / out_file


def _tbl(table: dict, key: str) -> dict:
    v = table.get(key)
    return v if isinstance(v, dict) else {}


def load_config(path: Path) -> Config:
    if not path.is_file():
        die(f"config not found: {path}\nRun `python {Path(__file__).name} init` to create one.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        die(f"could not parse {path}:\n{e}")

    g = _tbl(data, "game")
    project_dir = (path.parent / str(g.get("project", "."))).resolve()

    # Auto-fill name/version from project.godot when left out of the config.
    raw_name = g.get("name")
    raw_version = g.get("version")
    meta: dict[str, str] = {}
    if raw_name is None or raw_version is None:
        meta = read_godot_project_info(project_dir)
    name = str(raw_name) if raw_name is not None else meta.get("config/name", "")
    version = str(raw_version) if raw_version is not None else (
        meta.get("config/version") or meta.get("application/version") or ""
    )
    auto: list[str] = []
    if raw_name is None and name:
        auto.append(f"name='{name}'")
    if raw_version is None and version:
        auto.append(f"version='{version}'")
    if auto:
        info(f"pulled {', '.join(auto)} from {project_dir / 'project.godot'}")

    game = Game(
        name=name,
        version=version or "0.0.0",
        project=str(g.get("project", ".")),
        godot=str(g.get("godot", "godot")),
    )
    if not game.name:
        die("[game] name is missing (set it in the config, or as config/name in project.godot)")
    if raw_version is None and not version:
        warn("no version in config or project.godot; defaulting to 0.0.0")

    b = _tbl(data, "build")
    platforms: dict[str, PlatformCfg] = {}
    for name, pc in _tbl(b, "platforms").items():
        platforms[str(name)] = PlatformCfg(
            preset=str(pc.get("preset", "")),
            out_file=str(pc.get("out_file", "")),
            args=[str(a) for a in pc.get("args", [])],
            steam=pc.get("steam"),
            itch=pc.get("itch"),
        )
    build = Build(output=str(b.get("output", "dist")), clean=bool(b.get("clean", True)), platforms=platforms)
    if not platforms:
        die(f"no [build.platforms.*] entries in {path}")

    steam_cfg: Steam | None = None
    if "steam" in data:
        s = _tbl(data, "steam")
        depots = {
            str(k): SteamDepot(int(d.get("depot_id", 0)), [str(x) for x in d.get("exclude", [])])
            for k, d in _tbl(s, "depots").items()
        }
        steam_cfg = Steam(
            app_id=int(s.get("app_id", 0)),
            user=str(s.get("user", "")),
            steamcmd=str(s.get("steamcmd", "steamcmd")),
            password_env=str(s.get("password_env", "STEAM_PASSWORD")),
            set_live=str(s.get("set_live", "")),
            build_desc=str(s.get("build_desc", "Build {version}")),
            scripts_dir=str(s.get("scripts_dir", "steam/scripts")),
            exclude=[str(x) for x in s.get("exclude", [])],
            depots=depots,
        )

    itch_cfg: Itch | None = None
    if "itch" in data:
        i = _tbl(data, "itch")
        itch_cfg = Itch(
            user=str(i.get("user", "")),
            game=str(i.get("game", "")),
            butler=str(i.get("butler", "butler")),
            channels={str(k): str(v) for k, v in _tbl(i, "channels").items()},
            userversion=str(i.get("userversion", "{version}")),
        )

    return Config(path=path.resolve(), game=game, build=build, steam=steam_cfg, itch=itch_cfg)


# --------------------------------------------------------------------------- #
# Platform selection
# --------------------------------------------------------------------------- #

def select_platforms(cfg: Config, only: list[str] | None) -> list[str]:
    have = list(cfg.build.platforms)
    for name in have:
        if name not in SUPPORTED:
            warn(f"unknown platform '{name}' in config (supported: {', '.join(SUPPORTED)})")
    if not only:
        return have
    only = [p.strip() for p in ",".join(only).split(",") if p.strip()]
    unknown = [p for p in only if p not in cfg.build.platforms]
    if unknown:
        die(f"platform(s) not in config: {', '.join(unknown)}. Configured: {', '.join(have)}")
    return only


def want_steam(cfg: Config, platform: str) -> bool:
    if cfg.steam is None or platform not in cfg.steam.depots:
        return False
    pc = cfg.build.platforms[platform]
    return True if pc.steam is None else bool(pc.steam)


def want_itch(cfg: Config, platform: str) -> bool:
    if cfg.itch is None:
        return False
    pc = cfg.build.platforms[platform]
    if pc.itch is None:
        return platform in ITCH_CHANNELS  # all known platforms have a default channel
    return bool(pc.itch)


# --------------------------------------------------------------------------- #
# Godot export
# --------------------------------------------------------------------------- #

def godot_binary(cfg: Config, dry_run: bool = False) -> str:
    found = resolve(cfg.game.godot)
    if not found:
        if dry_run:
            warn(f"Godot not found: '{cfg.game.godot}' (dry run, continuing)")
            return cfg.game.godot
        die(f"Godot not found: '{cfg.game.godot}'. Install it or set [game] godot in the config.")
    return found


def godot_major(binary: str) -> int:
    proc = subprocess.run([binary, "--version"], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    m = re.match(r"(\d+)\.", out)
    if proc.returncode != 0 or not m:
        die(f"could not determine Godot version from `{binary} --version` (got: {out!r})")
    return int(m.group(1))


def export_platform(cfg: Config, platform: str, runner: Step, step: Step) -> Path:
    pc = cfg.build.platforms[platform]
    if not pc.preset:
        die(f"[build.platforms.{platform}] preset is missing")
    out_dir, out_path = cfg.out_path_for(platform)

    if cfg.build.clean and out_dir.exists() and not runner.dry_run:
        shutil.rmtree(out_dir)
    if not runner.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)  # Godot won't create the export path itself

    binary = godot_binary(cfg, runner.dry_run)
    major = 4 if runner.dry_run else godot_major(binary)
    if major >= 4:
        headless, export_flag = ["--headless"], "--export-release"
    else:
        headless = ["--no-window"] if os.name == "nt" else []
        export_flag = "--export"

    args = [binary, *headless, "--path", cfg.project_dir, export_flag, pc.preset, out_path, *pc.args]
    info(f"exporting {bold(platform)} -> {out_path}")
    step.run(args)

    if not runner.dry_run and not out_path.exists():
        die(f"expected export output was not created: {out_path}\n"
            f"Check the preset name ('{pc.preset}') matches one in Godot's Export dialog.")
    return out_path


def do_build(cfg: Config, platforms: list[str], runner: Step, step: Step) -> None:
    info(f"building {bold(cfg.game.name)} v{cfg.game.version} ({', '.join(platforms)})")
    for p in platforms:
        out_path = export_platform(cfg, p, runner, step)
        target = out_path if out_path.is_file() else out_path.parent
        info(f"  {bold(p)}: {green(human_size(dir_size(out_path)))} -> {target}")
    info(green(f"build complete: {(cfg.path.parent / cfg.build.output).resolve()}"))


# --------------------------------------------------------------------------- #
# Steam (steamcmd + generated VDF scripts)
# --------------------------------------------------------------------------- #

def vdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_vdf_quote(value: str) -> str:
    return f'"{vdf_escape(value)}"'


def generate_steam_scripts(cfg: Config, platforms: list[str], step: Step) -> Path:
    steam = cfg.steam
    scripts_dir = (cfg.path.parent / steam.scripts_dir).resolve()
    scripts_dir.mkdir(parents=True, exist_ok=True)
    vdf_path = scripts_dir / f"app_build_{steam.app_id}.vdf"

    desc = substitute(steam.build_desc, cfg)

    lines = [
        '"appbuild"',
        "{",
        f'  "appid"   {write_vdf_quote(str(steam.app_id))}',
        f'  "desc"    {write_vdf_quote(desc)}',
        f'  "buildoutput" {write_vdf_quote(str(scripts_dir / "output"))}',
    ]
    if steam.set_live:
        lines.append(f'  "setlive" {write_vdf_quote(steam.set_live)}')
    lines += [
        '  "depots"',
        "  {",
    ]
    for platform in platforms:
        if not want_steam(cfg, platform):
            continue
        depot = steam.depots[platform]
        if not depot.depot_id:
            die(f"[steam.depots.{platform}] depot_id is missing")
        out_dir, out_path = cfg.out_path_for(platform)
        excludes = [*steam.exclude, *depot.exclude]
        if platform == "macos" and out_path.suffix.lower() == ".zip":
            # Steam depots need the unpacked .app bundle (SteamPipe serves files, not archives),
            # so unzip the Godot export and exclude the zip to avoid shipping duplicate content.
            excludes.append("*.zip")
            if step.dry_run:
                print(f"  {yellow('[dry-run]')} would unzip {out_path} into {out_dir}")
            elif not out_path.is_file():
                die(f"macOS zip not found: {out_path} - run `build` first")
            elif not (out_dir / (out_path.stem + ".app")).exists():
                with zipfile.ZipFile(out_path) as zf:
                    zf.extractall(out_dir)
                info(f"unpacked {out_path.name} for the macOS depot")
        content_root = out_dir
        lines += [
            f'    "{depot.depot_id}"',
            "    {",
            '      "FileMapping"',
            "      {",
            f'        "Local Path" {write_vdf_quote(str(content_root / "*"))}',
            '        "Depot Path" "."',
            '        "recursive"  "1"',
            "      }",
        ]
        for pattern in excludes:
            lines.append(f'      "FileExclusion" {write_vdf_quote(pattern)}')
        lines.append("    }")
    lines += ["  }", "}"]
    vdf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return vdf_path


def steam_password(cfg: Config) -> str:
    env_name = cfg.steam.password_env
    pw = os.environ.get(env_name, "")
    if pw:
        return pw
    warn(f"${env_name} not set; you will be prompted (input hidden).")
    pw = getpass.getpass(f"Steam password for {cfg.steam.user}: ")
    if not pw:
        die("empty password")
    return pw


def do_steam(cfg: Config, platforms: list[str], runner: Step, step: Step) -> None:
    steam = cfg.steam
    if steam is None:
        die("no [steam] section in the config")
    if not steam.app_id:
        die("[steam] app_id is missing")
    if not steam.user:
        die("[steam] user is missing")

    shipping = [p for p in platforms if want_steam(cfg, p)]
    if not shipping:
        warn("no platforms are configured for Steam; nothing to do")
        return
    if "macos" in shipping and os.name == "nt":
        warn(
            "uploading the macOS depot from Windows: SteamPipe preserves Unix file permissions\n"
            "      only when the depot is built on macOS/Linux, so the .app may fail to launch\n"
            "      ('damaged / can't be opened'). Best practice: run `steamline steam` for the\n"
            "      mac depot from a Mac or Linux/WSL machine."
        )

    vdf = generate_steam_scripts(cfg, shipping, step)
    info(f"generated Steam build script: {vdf}")

    binary = resolve(steam.steamcmd)
    if not binary:
        if step.dry_run:
            warn(f"steamcmd not found: '{steam.steamcmd}' (dry run, continuing)")
            binary = steam.steamcmd
        else:
            die(f"steamcmd not found: '{steam.steamcmd}'. Install it (developer.valvesoftware.com/wiki/SteamCMD).")
    pw = "<password>" if step.dry_run else steam_password(cfg)
    args = [binary, "+login", steam.user, pw, "+run_app_build", str(vdf), "+quit"]
    info(f"uploading to Steam (app {steam.app_id}; depots for: {', '.join(shipping)})")
    step.run(args, stream=True)  # stream: steamcmd may prompt for Steam Guard codes
    if steam.set_live:
        info(green(f"Steam upload finished - '{steam.set_live}' was set live if steamcmd reported success."))
    else:
        info(green("Steam upload finished - set_live is empty, so no branch was changed."))


# --------------------------------------------------------------------------- #
# itch.io (butler)
# --------------------------------------------------------------------------- #

def itch_push_target(platform: str, out_path: Path) -> Path:
    """Directory to push for a platform; single-file archives (zip) are pushed directly."""
    if platform == "macos" and out_path.suffix.lower() == ".zip":
        return out_path
    if out_path.suffix.lower() in (".zip", ".app"):
        return out_path
    return out_path.parent


def do_itch(cfg: Config, platforms: list[str], runner: Step, step: Step) -> None:
    itch = cfg.itch
    if itch is None:
        die("no [itch] section in the config")
    if not itch.user or not itch.game:
        die("[itch] user and game are required")

    binary = resolve(itch.butler)
    if not binary:
        if step.dry_run:
            warn(f"butler not found: '{itch.butler}' (dry run, continuing)")
            binary = itch.butler
        else:
            die(f"butler not found: '{itch.butler}'. Install it (itch.io/docs/butler) and run `butler login` first.")

    shipping = [p for p in platforms if want_itch(cfg, p)]
    if not shipping:
        warn("no platforms are configured for itch.io; nothing to do")
        return

    userversion = substitute(itch.userversion, cfg)
    for p in shipping:
        out_dir, out_path = cfg.out_path_for(p)
        if not out_path.exists():
            if step.dry_run:
                warn(f"build for {p} not found at {out_path} (dry run, continuing)")
            else:
                die(f"build for {p} not found at {out_path} - run `build` first")
        channel = itch.channels.get(p) or ITCH_CHANNELS.get(p) or p
        target = itch_push_target(p, out_path)
        args = [binary, "push", target, f"{itch.user}/{itch.game}:{channel}", "--userversion", userversion]
        info(f"pushing {bold(p)} to itch.io channel '{channel}' ({target})")
        step.run(args, stream=True)  # stream: butler shows upload progress
    info(green("itch.io upload finished."))


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def read_export_presets(project_dir: Path) -> list[str]:
    """Preset names as declared in the project's export_presets.cfg."""
    f = project_dir / "export_presets.cfg"
    if not f.is_file():
        return []
    names = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("name="):
            names.append(line[len("name="):].strip().strip('"'))
    return names


def do_check(cfg: Config) -> None:
    ok = True

    info(f"config: {cfg.path}")
    info(f"project: {cfg.project_dir}")
    if not (cfg.project_dir / "project.godot").is_file():
        warn(f"project.godot not found in {cfg.project_dir}")
        ok = False

    binary = godot_binary(cfg)
    major = godot_major(binary)
    info(f"godot: {binary} (Godot {major}.x)")
    presets = read_export_presets(cfg.project_dir)
    if presets:
        info(f"godot export presets: {', '.join(presets)}")
        for name, pc in cfg.build.platforms.items():
            if pc.preset not in presets:
                warn(f"[build.platforms.{name}] preset '{pc.preset}' was not found in {cfg.project_dir / 'export_presets.cfg'}")
                ok = False
    else:
        warn("no export_presets.cfg found - create presets in Godot's Export dialog first")

    for label, tool, configured in [
        ("steamcmd", "steamcmd", cfg.steam.steamcmd if cfg.steam else None),
        ("butler", "butler", cfg.itch.butler if cfg.itch else None),
    ]:
        if configured is None:
            info(f"{label}: not configured (skipped)")
            continue
        found = resolve(configured)
        if not found:
            warn(f"{label} not found: '{configured}'")
            ok = False
        else:
            info(f"{label}: {found}")

    if cfg.steam and not os.environ.get(cfg.steam.password_env):
        warn(f"${cfg.steam.password_env} is not set; you'll be prompted for the Steam password on upload")

    if ok:
        info(green("check complete - everything looks good."))
    else:
        warn("check finished with warnings (see above).")


# --------------------------------------------------------------------------- #
# init - write an example config
# --------------------------------------------------------------------------- #

EXAMPLE_TOML = """\
# Steamline configuration - https://github.com/ccbentley/godot-steamline
# Docs: see README.md next to steamline.py

[game]
# name and version are optional: if omitted they are read from project.godot
# (config/name and config/version in the [application] section).
# name = "My Awesome Game"
# version = "1.0.0"
project = "."              # folder containing project.godot
godot = "godot"            # Godot editor binary (path, or name on PATH)

[build]
output = "dist"            # builds land in dist/<platform>/
clean = true               # wipe each platform folder before exporting

# One entry per export preset you made in Godot's Export dialog.
# `out_file` is optional: it defaults to the game name, e.g. my_awesome_game.exe.
# Tip: run `python steamline.py build --set-version 1.2.3` to stamp the version
# into project.godot (config/version) so the in-game version matches the build.
[build.platforms.windows]
preset = "Windows Desktop"
# out_file = "MyAwesomeGame.exe"
# steam = true              # set false to keep this platform off Steam
# itch = true               # set false to keep this platform off itch.io
# args = []                 # extra args passed to Godot's --export-release

[build.platforms.linux]
preset = "Linux"
# out_file = "MyAwesomeGame.x86_64"

[build.platforms.macos]
preset = "macOS"
# out_file = "MyAwesomeGame.zip"  # default <name>.zip; Steam depots get the unzipped .app

[build.platforms.web]
preset = "Web"
# out_file = "index.html"
itch = true
steam = false              # web builds don't ship to Steam depots

[steam]
app_id = 480               # your Steam App ID
user = "yoursteamlogin"
password_env = "STEAM_PASSWORD"   # steamcmd password is read from this env var (or prompted)
# set_live = "Public"       # branch to set live after upload (e.g. "Public" or a beta name);
#                           # leave unset or empty to not change the live branch
build_desc = "Build {version}"
scripts_dir = "steam/scripts"     # generated app_build VDF lives here
exclude = ["*.pdb"]               # global file exclusions for every depot

# Depot IDs (created in the Steamworks console, one per platform).
[steam.depots.windows]
depot_id = 481

[steam.depots.linux]
depot_id = 482

[steam.depots.macos]
depot_id = 483

[itch]
user = "youritchname"
game = "my-awesome-game"
userversion = "{version}"
# Override itch channel names per platform if you like (defaults below):
# channels = { windows = "windows", linux = "linux", macos = "mac", web = "html" }
"""


def do_init(path: Path, force: bool) -> None:
    if path.exists() and not force:
        die(f"{path} already exists (use --force to overwrite)")
    path.write_text(EXAMPLE_TOML, encoding="utf-8")
    info(green(f"wrote example config: {path}"))
    print("Next steps:")
    print(f"  1. Edit {path} (name, presets, Steam app/depot IDs, itch user/game).")
    print(f"  2. Run: python {Path(__file__).name} check")
    print(f"  3. Run: python {Path(__file__).name} all")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROGRAM, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", "-c", default="steamline.toml", help="path to config file (default: steamline.toml)")
    sub = ap.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write an example steamline.toml")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("check", help="verify tools and config")

    def common(p):
        p.add_argument("--platform", "-p", action="append", help="platform(s) to process, repeatable or comma-separated")
        p.add_argument("--dry-run", action="store_true", help="print commands instead of running them")
        p.add_argument("--no-clean", action="store_true", help="don't wipe platform output folders before exporting")
        p.add_argument("--verbose", "-v", action="store_true", help="stream full tool output instead of showing it only on failure")

    p_build = sub.add_parser("build", help="export with Godot")
    common(p_build)
    p_build.add_argument("--set-version", metavar="X.Y.Z",
                         help="stamp config/version=X.Y.Z into project.godot before building")
    p_steam = sub.add_parser("steam", help="upload to Steam via steamcmd")
    common(p_steam)
    p_itch = sub.add_parser("itch", help="upload to itch.io via butler")
    common(p_itch)
    p_all = sub.add_parser("all", help="build + steam + itch")
    common(p_all)
    p_all.add_argument("--skip-build", action="store_true", help="reuse existing dist/ output")
    p_all.add_argument("--set-version", metavar="X.Y.Z",
                       help="stamp config/version=X.Y.Z into project.godot before building")
    return ap


def main(argv: list[str] | None = None) -> None:
    if sys.version_info < (3, 11):
        die("Python 3.11+ is required (for tomllib).")

    args = build_parser().parse_args(argv)
    config_path = Path(args.config)

    if args.command == "init":
        do_init(config_path, args.force)
        return

    cfg = load_config(config_path)
    set_version = getattr(args, "set_version", None)
    if set_version:
        cfg.game.version = set_version
        stamp_version(cfg, set_version, getattr(args, "dry_run", False))
    runner = Step(dry_run=getattr(args, "dry_run", False))   # for quick probes
    step = Step(dry_run=getattr(args, "dry_run", False), verbose=getattr(args, "verbose", False))

    if args.command == "check":
        do_check(cfg)
        return

    platforms = select_platforms(cfg, args.platform)
    if getattr(args, "no_clean", False):
        cfg.build.clean = False

    if args.command == "build":
        do_build(cfg, platforms, runner, step)
        return

    if args.command == "steam":
        do_steam(cfg, platforms, runner, step)
        return

    if args.command == "itch":
        do_itch(cfg, platforms, runner, step)
        return

    if args.command == "all":
        if not args.skip_build:
            do_build(cfg, platforms, runner, step)
        do_steam(cfg, platforms, runner, step)
        do_itch(cfg, platforms, runner, step)
        return

    die(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
