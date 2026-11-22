"""
Local library scan: executables, local updates and install dates ("Installed On").

For every game, from the folders of your library on disk:
 1. Executable: keeps it if valid; if the folder was moved / renamed (e.g. sorted into
    "HD GAMES\\"), relocates it by its old path; else finds the game folder by name
    (or acronym: "HoS" = House of Shinobi) and picks the best executable.
    If a NEWER version of the same folder exists (MansionDays-0.0.5 -> MansionDays-0.0.9),
    switches to it.
 2. Installed checkbox: local version (from the folder names) or file dates compared
    with the F95zone version / date -> ticks Installed when you updated locally, and
    un-ticks wrongly ticked games when the local version is older.
 3. Install date: date the files of the version you have landed on your disk
    (creation / modification times, ignoring saves, logs, caches, screenshots).

Library folders are found automatically: "Set exe dir" + the "<drive>\\<folder>" of
every absolute executable (e.g. E:\\JeuxEr, F:\\JeuxE...). Game folders are searched
at depth 1 and 2 under them (so category folders like "HD GAMES\\<game>" work).

Runs automatically after each full Refresh, and manually from Settings > Manage >
Install dates (Detect) or right click > Detect Install Date.
"""
import asyncio
import contextlib
import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request

from common.structs import (
    Game,
    MsgBox,
    Os,
    Status,
    TimelineEventType,
    Timestamp,
)
from external import (
    async_thread,
    error,
)
from modules import (
    db,
    globals,
    icons,
    msgbox,
    utils,
)

LOG_PREFIX = "[LocalScan]"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Written by games while playing (Ren'Py, RPG Maker, Tyrano, Unity...), lowercase
EXCLUDED_DIRS = {
    "saves", "save", "savedata", "cache", "logs", "log", "__pycache__", "screenshots", "crashes", "temp", "tmp",
    # added next to games by F95Checker / graphics mods, not by a game update
    "reshade-shaders", "_f95checker_dlss5", "_disabled_mods",
}
EXCLUDED_FILES = {
    "log.txt", "traceback.txt", "errors.txt", "persistent", "output_log.txt", "player.log", "player-prev.log",
    "desktop.ini", "thumbs.db",
    # F95Checker's own files in games (cheats console, toy sync, DLSS 5 kit) and ReShade / DLSS 5 mods:
    # adding them must not make the game look freshly updated
    "zz_f95checker_console.rpy", "zz_f95checker_console.rpyc", "zz_f95toy.rpy", "zz_f95toy.rpyc",
    "zz_f95checker_prefs.rpy", "zz_f95checker_prefs.rpyc",
    "dlss5_settings.rpy", "dlss5_settings.rpyc", "_f95dlss5.json",
    "dxgi.dll", "dxgi.dll.off", "reshade.ini", "reshadepreset.ini", "nvngx_dlss.dll", "nvngx_dlssnr.dll",
    "nvngx_dlss.dll.ver", "nvngx_dlssnr.dll.ver", "setup dlss5.bat", "uninstall dlss5.bat", "reshade_setup.exe",
    "dlss5-feed.cfg",
    # RHI / OptiScaler / DLSS Enabler kits (upscaler & frame-pacing injectors put next to the exe)
    "optiscaler.ini", "dlss-enabler.ini", "bfipresenter.ini", "relimiter.ini", "rhi_install.txt",
    "version.dll", "winmm.dll",
}
EXCLUDED_EXTS = {
    ".log", ".save", ".sav", ".tmp", ".bak", ".addon64", ".addon32", ".original",
}
# Play-time files: their modification time = last time you played (never used for the install date)
PLAY_DIRS = {"saves", "save", "savedata"}
PLAY_FILES = {"log.txt", "traceback.txt", "persistent", "output_log.txt", "player.log"}
PLAY_EXTS = {".save", ".sav"}
SCREENSHOT_RE = re.compile(r"^screenshot\d*\.(png|jpe?g)$", re.IGNORECASE)
PROBE_MAX_DEPTH = 4          # game files deeper than this don't change the install date in practice
LIBRARY_DEPTH = 2            # <root>\<game> and <root>\<category>\<game>
NAME_MATCH_THRESHOLD = 0.80

# Executables that are never "the game"
EXE_EXCLUDED_WORDS = (
    "unitycrashhandler", "crashhandler", "crashpad", "notification_helper", "nwjc", "unins", "uninstall", "setup",
    "redist", "vcredist", "dxweb", "dxsetup", "prereq", "python", "zsync", "payload", "chromedriver", "7z", "unrar",
    "updater", "crashreport", "dotnet", "createdump", "cefsharp",
)
EXE_EXCLUDED_DIRS = {
    "lib", "renpy", "engine", "redist", "_commonredist", "prerequisites", "directx", "tools", "mods", "saves", "cache",
}

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[ _\-]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[\s_\.\-\+\[\]\(\)\{\},;!'’&:~]+")
_VERSION_TOKEN = re.compile(r"^(v|ver|ep|ch|s|part)?\d+[a-z]?$", re.IGNORECASE)
_VERSION_IN_TEXT = re.compile(r"(?<![a-z0-9])v?\.?(\d+(?:\.\d+){1,3}[a-z]?)(?![a-z0-9])", re.IGNORECASE)
_PLATFORM_TOKENS = {
    "pc", "win", "win64", "windows", "linux", "lin", "mac", "osx", "android", "x64", "x86",
    "steam", "market", "patreon", "public", "elite",
}
_VERSION_PREFIXES = {
    "v", "ver", "version", "ep", "episode", "ch", "chap", "chapter", "season", "part", "act", "book", "update",
}


def _log(msg: str):
    # Visible in the console of F95Checker-Debug / when running from source
    print(f"{LOG_PREFIX} {msg}")


# ---------------------------------------------------------------------------
# Names and versions
# ---------------------------------------------------------------------------

def compact(text: str) -> str:
    """Lowercase, no accents, letters and digits only."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c.lower() for c in decomposed if unicodedata.category(c) != "Mn" and c.isalnum())


def acronym(text: str) -> str:
    """'House of Shinobi' -> 'hos', 'My Bimbo Dream: Kingpin' -> 'mbdk'."""
    decomposed = unicodedata.normalize("NFD", text or "")
    words = re.findall(r"[A-Za-z0-9]+", "".join(c for c in decomposed if unicodedata.category(c) != "Mn"))
    return "".join(word[0] for word in words).lower()


def folder_key(name: str) -> str:
    """'HouseOfHearts-Ep.2-pc' -> 'houseofhearts': cut at the first version / platform token (never the first token)."""
    text = _DATE_PREFIX.sub("", name)
    text = _CAMEL_SPLIT.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    tokens = [token for token in text.split(" ") if token.strip()]
    cut = len(tokens)
    for i in range(1, len(tokens)):
        if _VERSION_TOKEN.match(tokens[i]) or tokens[i].lower() in _PLATFORM_TOKENS:
            cut = i
            if i - 1 >= 1 and tokens[i - 1].lower() in _VERSION_PREFIXES:
                cut = i - 1
            break
    return compact(" ".join(tokens[:cut]))


def similarity(a: str, b: str) -> float:
    """1.0 identical; 0.85-0.95 one contains the other (>= 6 chars); else Dice coefficient on bigrams."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 6 and shorter in longer:
        return 0.85 + 0.10 * len(shorter) / len(longer)
    if len(a) < 2 or len(b) < 2:
        return 0.0
    bigrams = {}
    for i in range(len(a) - 1):
        bigrams[a[i:i + 2]] = bigrams.get(a[i:i + 2], 0) + 1
    common = 0
    for i in range(len(b) - 1):
        if bigrams.get(b[i:i + 2], 0) > 0:
            bigrams[b[i:i + 2]] -= 1
            common += 1
    return 2.0 * common / ((len(a) - 1) + (len(b) - 1))


def name_score(name: str, game_name: str) -> float:
    """How much a folder / exe name looks like the game name (acronyms included)."""
    key = folder_key(name)
    score = similarity(key, compact(game_name))
    acro = acronym(game_name)
    if len(acro) >= 3 and key == acro:
        score = max(score, 0.90)
    return score


def version_tuple(text: str) -> tuple[int, ...]:
    """'MansionDays-0.0.9-TEST' -> (0, 0, 9); 'v0.13.2a' -> (0, 13, 2); no dotted version -> ()."""
    match = _VERSION_IN_TEXT.search(_DATE_PREFIX.sub("", text or ""))
    if not match:
        return ()
    return tuple(int(part) for part in re.sub(r"[a-z]$", "", match.group(1), flags=re.IGNORECASE).split("."))


def version_label(text: str) -> str:
    match = _VERSION_IN_TEXT.search(_DATE_PREFIX.sub("", text or ""))
    return f"v{match.group(1)}" if match else ""


# ---------------------------------------------------------------------------
# File scan (install date)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Probe:
    installed: int = 0   # Most recent creation / modification time of the game files (play-time files ignored)
    source: str = ""     # File that gave it
    scanned: int = 0
    played: int = 0      # Most recent modification time of saves / logs inside the game folder


def latest_mtime(root: pathlib.Path | str, max_depth: int = 2) -> int:
    """Most recent file modification time under root (0 if none)."""
    limit = time.time() + 86400
    best = 0.0
    stack = [(str(root), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((entry.path, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                            if best < mtime <= limit:
                                best = mtime
                    except OSError:
                        pass
        except OSError:
            pass
    return int(best)


def external_play_time(exe: pathlib.Path) -> int:
    """Last played date from saves / logs kept OUTSIDE the game folder (Unity, Unreal). 0 if unknown."""
    best = 0
    try:
        # Unity: <exe>_Data/app.info = company + product -> AppData/LocalLow/<company>/<product> (saves, Player.log)
        info = exe.with_name(exe.stem + "_Data") / "app.info"
        profile = os.environ.get("USERPROFILE")
        if profile and info.is_file():
            lines = [line.strip() for line in info.read_text(encoding="utf-8", errors="ignore").splitlines()]
            if len(lines) >= 2 and lines[0] and lines[1]:
                local_low = pathlib.Path(profile, "AppData", "LocalLow", lines[0], lines[1])
                if local_low.is_dir():
                    best = max(best, latest_mtime(local_low, 2))
        # Unreal: AppData/Local/<exe name>/Saved
        local = os.environ.get("LOCALAPPDATA")
        if local:
            saved = pathlib.Path(local, exe.stem, "Saved")
            if saved.is_dir():
                best = max(best, latest_mtime(saved, 3))
    except (OSError, ValueError) as exc:
        _log(f"External saves unreadable for {exe}: {exc}")
    return best


def probe_folder(root: pathlib.Path, max_depth: int = PROBE_MAX_DEPTH) -> Probe:
    """Install date + last played date of a game folder (or single-file game), from its files."""
    limit = time.time() + 86400  # Ignore future dates (badly dated archives)
    best = 0.0
    best_file = None
    scanned = 0
    played = 0.0

    def consider(path: str, stat):
        nonlocal best, best_file
        # st_birthtime = creation time (Windows with Python 3.12+, macOS); st_ctime as fallback
        created = getattr(stat, "st_birthtime", None)
        if created is None:
            created = stat.st_ctime
        for candidate in (created, stat.st_mtime):
            if best < candidate <= limit:
                best = candidate
                best_file = path

    if root.is_file():  # Single-file game (one .exe directly in a category folder)
        try:
            consider(str(root), root.stat())
            scanned = 1
        except OSError as exc:
            _log(f"Unreadable file {root}: {exc}")
    else:
        stack = [(str(root), 0)]
        while stack:
            current, depth = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError as exc:
                _log(f"Unreadable folder skipped: {current}: {exc}")
                continue
            for entry in entries:
                try:
                    # Symlinks / junctions: may loop or point to another drive
                    if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in PLAY_DIRS:
                            played = max(played, latest_mtime(entry.path, 2))
                        elif depth < max_depth and entry.name.lower() not in EXCLUDED_DIRS:
                            stack.append((entry.path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name.lower()
                    if name in PLAY_FILES or os.path.splitext(name)[1] in PLAY_EXTS:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                        if played < mtime <= limit:
                            played = mtime
                        continue
                    if name in EXCLUDED_FILES or os.path.splitext(name)[1] in EXCLUDED_EXTS or SCREENSHOT_RE.match(name):
                        continue
                    scanned += 1
                    consider(entry.path, entry.stat(follow_symlinks=False))
                except OSError as exc:
                    _log(f"Unreadable entry skipped: {entry.path}: {exc}")
    if not best:
        _log(f"No usable file in {root} ({scanned} scanned)")
    return Probe(int(best), best_file or "", scanned, int(played))


# ---------------------------------------------------------------------------
# Library inventory
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class LibFolder:
    path: pathlib.Path
    depth: int
    key: str
    version: tuple[int, ...]


class Library:
    def __init__(self, roots: list[pathlib.Path]):
        self.roots = roots
        self.folders: list[LibFolder] = []
        self.by_name: dict[str, list[LibFolder]] = {}
        for root in roots:
            for child in self._subdirs(root):
                self._add(child, 1)
                for grandchild in self._subdirs(child):
                    self._add(grandchild, 2)
        _log(f"Library: {len(roots)} root(s) {[str(r) for r in roots]}, {len(self.folders)} folder(s)")

    @staticmethod
    def _subdirs(parent: pathlib.Path) -> list[pathlib.Path]:
        dirs = []
        try:
            with os.scandir(parent) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(("$", ".")) \
                                and entry.name != "System Volume Information":
                            dirs.append(pathlib.Path(entry.path))
                    except OSError:
                        pass
        except OSError as exc:
            _log(f"Cannot list {parent}: {exc}")
        return dirs

    def _add(self, path: pathlib.Path, depth: int):
        folder = LibFolder(path, depth, folder_key(path.name), version_tuple(path.name))
        self.folders.append(folder)
        self.by_name.setdefault(path.name.lower(), []).append(folder)

    def owner(self, exe: pathlib.Path) -> LibFolder | None:
        """Deepest library folder containing exe."""
        best = None
        for folder in self.folders:
            if folder.path in exe.parents and (best is None or folder.depth > best.depth):
                best = folder
        return best

    def is_root(self, path: pathlib.Path) -> bool:
        return any(_same_path(path, root) for root in self.roots)


def _same_path(a: pathlib.Path, b: pathlib.Path):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _exe_dir() -> pathlib.Path | None:
    base = globals.settings.default_exe_dir.get(globals.os)
    return pathlib.Path(base) if base else None


def _abs_exe(executable: str, base: pathlib.Path | None) -> pathlib.Path | None:
    if utils.is_uri(executable):
        return None
    path = pathlib.Path(executable)
    if not path.is_absolute():
        if base is None:
            return None
        path = base / path
    return path


def library_roots(games) -> list[pathlib.Path]:
    """Set exe dir + '<drive>\\<first folder>' of every absolute executable, if they exist."""
    candidates = []
    base = _exe_dir()
    if base:
        candidates.append(base)
    for game in games:
        for executable in game.executables:
            path = _abs_exe(executable, None)
            if path is not None and len(path.parts) >= 3:
                candidates.append(pathlib.Path(path.parts[0], path.parts[1]))
    roots, seen = [], set()
    for root in candidates:
        key = os.path.normcase(os.path.abspath(root))
        if key in seen:
            continue
        seen.add(key)
        try:
            if root.is_dir():
                roots.append(root)
        except OSError:
            pass
    return roots


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------

def pick_executable(folder: pathlib.Path, game_name: str) -> pathlib.Path | None:
    """Best executable of a game folder: name closest to the game / folder, shallowest, 64-bit, Unity/Godot data next to it."""
    best = None
    for current, dirs, files in os.walk(folder):
        rel_parts = pathlib.Path(current).relative_to(folder).parts
        depth = len(rel_parts)
        dirs[:] = [
            d for d in dirs
            if depth < 3 and d.lower() not in EXE_EXCLUDED_DIRS and not d.lower().endswith("_data") and not d.startswith(".")
        ]
        lower_names = {name.lower() for name in files} | {name.lower() for name in dirs}
        for file in files:
            stem, ext = os.path.splitext(file)
            if ext.lower() != ".exe":
                continue
            low = stem.lower()
            if any(word in low for word in EXE_EXCLUDED_WORDS):
                continue
            path = pathlib.Path(current, file)
            score = max(name_score(stem, game_name), name_score(folder.name, game_name) * 0.5) - 0.3 * depth
            if re.search(r"(-32|32bit|x86|_32)$", low):
                score -= 0.4  # Ren'Py ships Game.exe + Game-32.exe: prefer 64-bit
            if low in ("game", "nw", "start", "play"):
                score += 0.3  # RPG Maker / NW.js standard names
            if f"{low}_data" in lower_names or f"{low}.pck" in lower_names:
                score += 0.5  # Unity / Godot main executable
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size < 300_000:
                score -= 0.2  # Tiny stubs
            candidate = (score, size, str(path))
            if best is None or candidate > best:
                best = candidate
    if best is None:
        # HTML games
        for name in ("index.html", os.path.join("www", "index.html")):
            if (folder / name).is_file():
                return folder / name
        return None
    return pathlib.Path(best[2])


@dataclasses.dataclass(slots=True)
class Resolution:
    executable: pathlib.Path | None = None   # Main executable (absolute)
    target: pathlib.Path | None = None       # Folder (or single file) to probe for the install date
    how: str = ""                            # ok / relocated / found / newer
    old_folders: list[tuple[pathlib.Path, str]] = dataclasses.field(default_factory=list)  # (folder, "old" / "duplicate")


def _relocate(executable: str, lib: Library) -> pathlib.Path | None:
    """Old path 'NewLife-0.4-win/NewLife-0.4-win/NewLife.exe' -> same sub-path under any library folder of that name."""
    parts = pathlib.PurePath(executable.replace("\\", "/")).parts
    if parts and (parts[0].endswith(":\\") or parts[0].endswith(":/") or parts[0] in ("/", "\\")):
        parts = parts[1:]
    for start in range(len(parts) - 1):
        for folder in lib.by_name.get(parts[start].lower(), []):
            candidate = folder.path.joinpath(*parts[start + 1:])
            if candidate.is_file():
                return candidate
    # Single-file game moved elsewhere: same file name directly inside a library folder
    if parts:
        hits = [folder.path / parts[-1] for folder in lib.folders if (folder.path / parts[-1]).is_file()]
        if len(hits) == 1:
            return hits[0]
    return None


def _target_of(exe: pathlib.Path, game_name: str, lib: Library) -> pathlib.Path:
    owner = lib.owner(exe)
    if owner is None or lib.is_root(owner.path):
        return exe.parent if not lib.is_root(exe.parent) else exe
    if owner.path == exe.parent and name_score(owner.path.name, game_name) < 0.5:
        return exe  # Single .exe directly inside a category folder ("ANIME\Game-1.0.exe")
    return owner.path


def resolve(game: Game, lib: Library, base: pathlib.Path | None) -> Resolution:
    res = Resolution()
    # 1. Current executable still valid
    for executable in game.executables:
        path = _abs_exe(executable, base)
        try:
            if path is not None and path.is_file():
                res.executable, res.how = path, "ok"
                break
        except OSError:
            pass
    # 2. Folder moved / renamed: relocate by old path
    if res.executable is None:
        for executable in game.executables:
            if not utils.is_uri(executable):
                found = _relocate(executable, lib)
                if found is not None:
                    res.executable, res.how = found, "relocated"
                    break
    # 3. Find the folder by name (or acronym), newest version on ties
    if res.executable is None:
        scored = [(name_score(folder.path.name, game.name), folder) for folder in lib.folders]
        scored = [(score, folder) for score, folder in scored if score >= NAME_MATCH_THRESHOLD]
        if scored:
            top = max(score for score, _ in scored)
            ties = [folder for score, folder in scored if score >= top - 0.02]
            folder = max(ties, key=lambda f: (f.version, -f.depth))
            exe = pick_executable(folder.path, game.name)
            if exe is not None:
                res.executable, res.how = exe, "found"
    if res.executable is None:
        return res
    res.target = _target_of(res.executable, game.name, lib)
    # 4. Newer version of the SAME folder elsewhere (same name without version, higher version)
    if res.target.is_dir():
        current_key, current_version = folder_key(res.target.name), version_tuple(res.target.name)
        if current_key and current_version:
            newer = [
                folder for folder in lib.folders
                if folder.key == current_key and folder.version and folder.version > current_version
                and not _same_path(folder.path, res.target)
            ]
            if newer:
                folder = max(newer, key=lambda f: f.version)
                same_layout = folder.path / res.executable.relative_to(res.target)
                exe = same_layout if same_layout.is_file() else pick_executable(folder.path, game.name)
                if exe is not None:
                    res.executable, res.target, res.how = exe, folder.path, "newer"
    res.old_folders = find_old_versions(res, lib)
    return res


def _related(a: pathlib.Path, b: pathlib.Path) -> bool:
    return _same_path(a, b) or a in b.parents or b in a.parents


def find_old_versions(res: Resolution, lib: Library) -> list[tuple[pathlib.Path, str]]:
    """Folders of OLDER versions of the game in use: same name without version, lower version number.
    Looks in the whole library, and next to the nested version folders leading to the executable
    ("WildLife\\Build_2024.04.11" next to "WildLife\\Build_2026.09.18")."""
    if res.executable is None or res.target is None or not res.target.is_dir():
        return []
    found = []
    key, version = folder_key(res.target.name), version_tuple(res.target.name)
    if key and version:
        for folder in lib.folders:
            if folder.key == key and folder.version and folder.version <= version and not _related(folder.path, res.target):
                # Same name and same version elsewhere = second copy of the same download ("Game-1.0-pc (1)")
                found.append((folder.path, "old" if folder.version < version else "duplicate"))
    try:
        parts = res.executable.parent.relative_to(res.target).parts
    except ValueError:
        parts = ()
    chain = [res.target] + [res.target.joinpath(*parts[:i + 1]) for i in range(len(parts))]
    for parent, current in zip(chain, chain[1:]):
        key, version = folder_key(current.name), version_tuple(current.name)
        if not (key and version):
            continue
        for sibling in Library._subdirs(parent):
            sibling_version = version_tuple(sibling.name)
            if sibling != current and folder_key(sibling.name) == key and sibling_version and sibling_version < version:
                found.append((sibling, "old"))
    return found


def local_version(res: Resolution) -> tuple[tuple[int, ...], str]:
    """Version of the local copy, from the deepest folder name between the game folder and its exe."""
    if res.executable is None or res.target is None:
        return (), ""
    names = [res.target.name]
    if res.target.is_dir():
        try:
            names += list(res.executable.parent.relative_to(res.target).parts)
        except ValueError:
            pass
    else:
        names = [res.executable.stem]
    for name in reversed(names):
        version = version_tuple(name)
        if version:
            return version, version_label(name)
    return (), ""


# ---------------------------------------------------------------------------
# Game updates
# ---------------------------------------------------------------------------

def _set_executable(game: Game, new: pathlib.Path, base: pathlib.Path | None):
    """Replaces the main (first) executable, keeps the others. No side effect on Installed (unlike add_executable)."""
    exe = new
    if base and base in exe.parents:
        exe = exe.relative_to(base)
    value = exe.as_posix()
    executables = [value] + [e for e in game.executables[1:] if e != value]
    game.executables = executables  # Saved to the database by Game.__setattr__
    game.validate_executables()


async def _record_install_date(game: Game, timestamp: int, folder_name: str, replace_newer: bool) -> bool:
    """Makes `timestamp` the install date. replace_newer: drop "Installed" events more recent than the files
    (manual Detect: fixes dates of an imported library). Returns False if nothing changed."""
    current = game.installed_on
    if current and abs(current - timestamp) < 3600:
        return False
    if not replace_newer and current > timestamp:
        return False  # Auto scan only moves forward (you ticked Installed after the files landed)
    if replace_newer:
        await db.connection.execute("""
            DELETE FROM timeline_events
            WHERE game_id=? AND type=? AND timestamp>?
        """, (game.id, db.py_to_sql(TimelineEventType.GameInstalled), timestamp))
        game.timeline_events[:] = [
            event for event in game.timeline_events
            if not (event.type == TimelineEventType.GameInstalled and event.timestamp.value > timestamp)
        ]
    await db.create_timeline_event(
        game.id,
        Timestamp(timestamp),
        [f"{game.installed} (detected from files in \"{folder_name}\")"],
        TimelineEventType.GameInstalled,
    )
    # create_timeline_event() inserts at the top, but this event may be in the past
    game.timeline_events.sort(key=lambda event: event.timestamp.value, reverse=True)
    return True


@dataclasses.dataclass(slots=True)
class OldVersion:
    game: str
    path: pathlib.Path    # Older version folder, can be deleted
    kept: pathlib.Path    # Folder of the version in use
    kind: str = "old"     # "old" (lower version), "duplicate" (same version), "uninstall" (finished game)

    def __str__(self):
        return f"{self.game}: {self.path}" + (" (duplicate)" if self.kind == "duplicate" else "")


_announced_old_versions: set[str] = set()  # Auto scan: only pop up once per session for the same old folder


@dataclasses.dataclass
class ScanReport:
    exe_relocated: list[str] = dataclasses.field(default_factory=list)
    exe_found: list[str] = dataclasses.field(default_factory=list)
    exe_newer: list[str] = dataclasses.field(default_factory=list)
    marked_installed: list[str] = dataclasses.field(default_factory=list)
    marked_older: list[str] = dataclasses.field(default_factory=list)
    dates: list[str] = dataclasses.field(default_factory=list)
    not_found: list[str] = dataclasses.field(default_factory=list)
    played: list[str] = dataclasses.field(default_factory=list)
    saves_carried: list[str] = dataclasses.field(default_factory=list)
    mods: list["ModCarry"] = dataclasses.field(default_factory=list)
    mods_old: list[str] = dataclasses.field(default_factory=list)   # games with mods older than their update
    old_versions: list["OldVersion"] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    @property
    def changes(self):
        return (
            len(self.exe_relocated) + len(self.exe_found) + len(self.exe_newer) + len(self.marked_installed)
            + len(self.marked_older) + len(self.saves_carried)
        )


async def _scan_game(game: Game, lib: Library, base: pathlib.Path | None, manual: bool, report: ScanReport):
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, resolve, game, lib, base)
    if res.executable is None:
        local_info().pop(game.id, None)
        if game.installed or manual:
            report.not_found.append(game.name)
        _log(f"'{game.name}': no folder / executable found")
        return

    # 1. Executable
    if res.how in ("relocated", "found", "newer"):
        _set_executable(game, res.executable, base)
        {"relocated": report.exe_relocated, "found": report.exe_found, "newer": report.exe_newer}[res.how].append(
            f"{game.name}: {res.executable}"
        )
        _log(f"'{game.name}': executable {res.how}: {res.executable}")
    for old_folder, kind in res.old_folders:
        report.old_versions.append(OldVersion(game.name, old_folder, res.target, kind))

    # 2. Installed checkbox
    probe = await loop.run_in_executor(None, probe_folder, res.target)
    files_ts = probe.installed
    # Size on disk (Size column): only recomputed when the folder or its files changed
    info = local_info().setdefault(game.id, {})
    if info.get("path") != str(res.target) or info.get("files_ts") != files_ts or not info.get("size"):
        size = await loop.run_in_executor(None, folder_size, res.target)
        info.update(path=str(res.target), files_ts=files_ts, size=size)
    # Real engine + cheat mods found in the folder (Engine / Cheats columns)
    info["engine"], info["cheats"] = await loop.run_in_executor(None, detect_engine, res.executable)
    # Version-specific mods (walkthrough, cheats, gallery...) older than the files of this version: may break it
    old_mods = await loop.run_in_executor(None, outdated_mods, res.executable, files_ts)
    old_mods = [m for m in old_mods if m not in info.get("mods_old_ok", [])]
    if old_mods != info.get("mods_old", []):
        if [m for m in old_mods if m not in info.get("mods_old", [])]:
            report.mods_old.append(f"{game.name}: {', '.join(old_mods)}")
        info["mods_old"] = old_mods
    # Carry over from older version folders still on disk: RPG Maker saves (copied), Ren'Py mods (proposed)
    for old_folder, kind in res.old_folders:
        if kind != "old":
            continue
        if info["engine"].startswith("RPGM"):
            copied = await loop.run_in_executor(None, carry_rpgm_saves, old_folder, res.executable)
            if copied:
                report.saves_carried.append(f"{game.name}: {copied} save file(s) from {old_folder.name}")
        elif info["engine"] == "Ren'Py":
            files = await loop.run_in_executor(None, find_missing_mods, old_folder, res.executable)
            if files:
                report.mods.append(ModCarry(game.name, files, res.executable.parent / "game"))
    local_v, local_label = local_version(res)
    if info.get("imported_folder") == str(res.executable.parent) and info.get("imported_version"):
        # Update installed with "Import Update" without renaming the folder: its name still has the old version
        local_v, local_label = version_tuple(info["imported_version"]), info["imported_version"]
        if not local_v and info["imported_version"] == game.version:
            local_v = version_tuple(game.version) or (1,)
            local_label = game.version
    f95_v = version_tuple(game.version)
    state = None
    if not game.custom:
        if local_v and f95_v:
            state = "latest" if local_v >= f95_v else "older"
        elif files_ts and game.last_updated.value and files_ts >= game.last_updated.value:
            state = "latest"  # Files landed on disk after the latest version was released
    _log(f"'{game.name}': local {local_label or '?'} / F95 {game.version} / files {time.strftime('%Y-%m-%d', time.localtime(files_ts)) if files_ts else '?'} -> {state}")
    if state == "latest" and game.installed != game.version:
        game.installed = game.version
        game.updated = False
        report.marked_installed.append(f"{game.name}: {game.version}")
        if files_ts:
            await _record_install_date(game, files_ts, res.target.name, replace_newer=True)
    elif state == "older" and local_label and (not game.installed or game.installed == game.version):
        # Ticked as up to date (eg: library imported with "Installed on add") but the local copy is older
        game.installed = local_label
        game.updated = True
        report.marked_older.append(f"{game.name}: {local_label} < {game.version}")

    # 3. Install date
    if game.installed and files_ts:
        if await _record_install_date(game, files_ts, res.target.name, replace_newer=manual):
            report.dates.append(f"{game.name}: {time.strftime('%Y-%m-%d', time.localtime(files_ts))}")

    # 4. Last played (Last Launched column): saves / logs written after the files were installed,
    #    in the game folder or in AppData (Unity, Unreal). Games launched outside F95Checker are covered.
    played = probe.played if probe.played > files_ts + 60 else 0  # Saves shipped inside the archive don't count
    played = max(played, await loop.run_in_executor(None, external_play_time, res.executable))
    if played > game.last_launched.value + 3600:
        game.last_launched = played  # Saved to the database by Game.__setattr__
        await db.create_timeline_event(game.id, Timestamp(played), ["(detected from save files)"], TimelineEventType.GameLaunched)
        game.timeline_events.sort(key=lambda event: event.timestamp.value, reverse=True)
        report.played.append(f"{game.name}: {time.strftime('%Y-%m-%d', time.localtime(played))}")


async def scan_library(*games: Game, manual=True, reserved=False):
    """manual=True: Detect button / right click (summary popup, install dates recalibrated on files).
    manual=False: after a full Refresh (popup only if something changed, dates only move forward).
    reserved=True: the full Refresh already counted these games in its progress bar (no 100% -> 51% jump)."""
    games = list(games or globals.games.values())
    base = _exe_dir()
    loop = asyncio.get_running_loop()
    lib = await loop.run_in_executor(None, Library, library_roots(globals.games.values()))
    if manual:
        globals.refresh_total = max(1, len(games))
        globals.refresh_progress = 0
    elif not reserved:
        globals.refresh_total += len(games)
    report = ScanReport()
    _log(f"Start ({'manual' if manual else 'auto'}): {len(games)} game(s)")
    for game in games:
        try:
            await _scan_game(game, lib, base, manual, report)
            await loop.run_in_executor(None, ensure_renpy_prefs, game)
            if await loop.run_in_executor(None, ensure_default_cheats, game):
                info = local_info().get(game.id)
                exe = _main_exe(game)
                if info is not None and exe is not None:
                    info["engine"], info["cheats"] = detect_engine(exe)
        except Exception:
            _log(f"'{game.name}': error {error.traceback()}")
            report.errors.append(f"{game.name}: {error.text()}")
        globals.refresh_progress += 1
    await db.save()
    save_local_info()
    _log(f"End: {report}")
    try:
        from modules import toy  # toy imports this module
        await loop.run_in_executor(None, toy.ensure_hooks, games)
    except Exception:
        _log(f"Toy sync scripts: {error.traceback()}")
    if globals.gui:
        globals.gui.recalculate_ids = True
    new_old_versions = [old for old in report.old_versions if str(old.path) not in _announced_old_versions]
    new_old_versions += [mod for mod in report.mods if str(mod.dest) + "#mods" not in _announced_old_versions]
    _announced_old_versions.update(str(old.path) for old in report.old_versions)
    _announced_old_versions.update(str(mod.dest) + "#mods" for mod in report.mods)
    if not manual and not report.changes and not report.errors and not new_old_versions and not report.mods_old:
        return

    lines = []
    if report.marked_installed:
        lines.append(f"{len(report.marked_installed)} local update(s) detected: marked as Installed.")
    if report.marked_older:
        lines.append(f"{len(report.marked_older)} game(s) were ticked Installed but your copy is older: un-ticked.")
    exe_count = len(report.exe_relocated) + len(report.exe_found) + len(report.exe_newer)
    if exe_count:
        lines.append(
            f"{exe_count} executable(s) updated ({len(report.exe_relocated)} moved folders, "
            f"{len(report.exe_found)} found by name, {len(report.exe_newer)} newer version folders)."
        )
    if manual:
        lines.append(f"{len(report.dates)} install date(s) set from the files.")
        lines.append(f"{len(report.played)} last played date(s) set from your saves (Last Launched column).")
    if report.not_found and manual:
        lines.append(f"{len(report.not_found)} game(s) not found on disk.")
    if report.old_versions:
        lines.append(f"{len(report.old_versions)} older version folder(s) still on disk: you can delete them below.")
    if report.saves_carried:
        lines.append(f"{len(report.saves_carried)} RPG Maker game(s): saves copied from the old version to the new one.")
    if report.mods:
        lines.append(f"{len(report.mods)} game(s) have cheat / walkthrough mods in their old version only: you can copy them below.")
    if report.mods_old:
        lines.append(f"{len(report.mods_old)} game(s) have mods older than their update (they can make the game crash): see below.")
    if report.errors:
        lines.append(f"{len(report.errors)} error(s), see details.")
    more = ""
    for title, names in (
        ("Marked as Installed", report.marked_installed),
        ("Un-ticked (older local copy)", report.marked_older),
        ("Executable: moved folder", report.exe_relocated),
        ("Executable: found by name", report.exe_found),
        ("Executable: newer version folder", report.exe_newer),
        ("Older versions still on disk (can be deleted)", [str(old) for old in report.old_versions]),
        ("Saves copied to the new version", report.saves_carried),
        ("Mods only in the old version", [str(mod) for mod in report.mods]),
        ("Mods older than the update", report.mods_old),
        ("Last played", report.played if manual else []),
        ("Not found on disk", report.not_found if manual else []),
        ("Install dates", report.dates if manual else []),
        ("Errors", report.errors),
    ):
        if names:
            more += f"{title}:\n - " + "\n - ".join(names) + "\n\n"
    buttons = {f"{icons.check} Ok": None}
    if report.old_versions:
        old_versions = list(report.old_versions)
        buttons[f"{icons.trash_can_outline} Delete old versions"] = lambda: open_delete_popup(old_versions)
    if report.mods:
        mods = list(report.mods)
        buttons[f"{icons.content_copy} Copy mods ({len(mods)})"] = lambda: copy_mods(mods)
    if report.mods_old:
        buttons[f"{icons.alert_outline} Mods to check ({len(report.mods_old)})"] = open_outdated_mods_popup
    if outdated_games():
        buttons[f"{icons.download} Updates to do ({len(outdated_games())})"] = open_updates_popup
    utils.push_popup(
        msgbox.msgbox, "Local library scan",
        "\n".join(lines) or "Nothing changed.",
        MsgBox.warn if report.errors else MsgBox.info,
        buttons,
        more=more.strip() or None,
    )


# ---------------------------------------------------------------------------
# Old versions deletion
# ---------------------------------------------------------------------------

def folder_size(path: pathlib.Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for current, _, files in os.walk(path):
        for file in files:
            try:
                total += os.lstat(os.path.join(current, file)).st_size
            except OSError:
                pass
    return total


def size_display(size: int) -> str:
    return f"{size / 1024 ** 3:.2f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"


def _in_use_by(path: pathlib.Path, ignore: Game = None) -> str | None:
    """Name of a game whose executable is inside path (then it is NOT an old version anymore)."""
    base = _exe_dir()
    for game in globals.games.values():
        if game is ignore:
            continue
        for executable in game.executables:
            exe = _abs_exe(executable, base)
            if exe is not None and (path in exe.parents or _same_path(path, exe)):
                return game.name
    return None


def backup_saves(old: OldVersion) -> pathlib.Path | None:
    """Copies the save folders found inside an old version to <F95Checker data>/saves_backup. None if there are none."""
    save_dirs = []
    stack = [(old.path, 0)]
    while stack:
        current, depth = stack.pop()
        for child in Library._subdirs(current):
            if child.name.lower() in PLAY_DIRS:
                save_dirs.append(child)
            elif depth < 5:
                stack.append((child, depth + 1))
    save_dirs = [d for d in save_dirs if any(d.iterdir())]
    if not save_dirs:
        return None
    dest = globals.data_path / "saves_backup" / f"{utils.clean_str(old.game)[:40]} - {old.path.name[:60]} - {time.strftime('%Y%m%d-%H%M%S')}"
    for save_dir in save_dirs:
        shutil.copytree(save_dir, dest / save_dir.relative_to(old.path))
    return dest


def _recycle(path: pathlib.Path):
    """Windows recycle bin (same drive, instant). Windows itself asks before deleting for good if it is too big for the bin."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 0x0010, 0x0040, 0x0400, 0x4000
    source = ctypes.create_unicode_buffer(str(path) + "\0")  # List of paths: double null terminated
    operation = SHFILEOPSTRUCTW(
        None, FO_DELETE, ctypes.cast(source, wintypes.LPCWSTR), None,
        FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_WANTNUKEWARNING, False, None, None,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"Recycle bin operation failed or was cancelled (code {result})")


async def delete_old_versions(items: list[OldVersion], permanent: bool, owners: dict[str, Game] = None):
    """owners: {folder: game} for uninstalls. The folder of that game is deleted, its executables and
    Installed tick are cleared. Any folder holding the executable of ANOTHER game is always refused."""
    owners = owners or {}
    loop = asyncio.get_running_loop()
    done, failed, backups, freed = [], [], [], 0
    for old in items:
        try:
            if not old.path.is_dir():
                failed.append(f"{old}: folder not found")
                continue
            # Safety: re-checked at the last moment, a folder holding a game executable is never deleted
            owner = owners.get(str(old.path))
            if user := _in_use_by(old.path, ignore=owner):
                failed.append(f"{old}: still used by '{user}', not deleted")
                continue
            if any(_same_path(old.path, root) or old.path in root.parents for root in library_roots(globals.games.values())):
                failed.append(f"{old}: is a library folder, not deleted")
                continue
            backup = await loop.run_in_executor(None, backup_saves, old)
            if backup is not None:
                backups.append(f"{old.game}: {backup}")
            size = await loop.run_in_executor(None, folder_size, old.path)
            if permanent:
                await loop.run_in_executor(None, shutil.rmtree, old.path)
            else:
                await loop.run_in_executor(None, _recycle, old.path)
            _log(f"Old version {'deleted' if permanent else 'recycled'}: {old.path} ({size_display(size)})")
            done.append(f"{old} ({size_display(size)})")
            freed += size
            if owner is not None:
                owner.executables = []
                owner.validate_executables()
                owner.installed = ""
                owner.updated = False
                local_info().pop(owner.id, None)
        except Exception:
            _log(f"Deleting {old.path} failed: {error.traceback()}")
            failed.append(f"{old}: {error.text()}")
    save_local_info()
    if globals.gui:
        globals.gui.recalculate_ids = True
    message = (
        f"{len(done)} folder(s) deleted, {size_display(freed)} freed." if permanent else
        f"{len(done)} folder(s) moved to the recycle bin ({size_display(freed)}): empty it to free the space."
    )
    if backups:
        message += f"\n{len(backups)} save folder(s) copied first to:\n{globals.data_path / 'saves_backup'}"
    if failed:
        message += f"\n{len(failed)} folder(s) could not be deleted, see details."
    more = ""
    for title, names in (("Deleted", done), ("Saves backed up", backups), ("Not deleted", failed)):
        if names:
            more += f"{title}:\n - " + "\n - ".join(names) + "\n\n"
    utils.push_popup(
        msgbox.msgbox, "Delete old versions",
        message,
        MsgBox.warn if failed else MsgBox.info,
        more=more.strip() or None,
    )


def open_delete_popup(items: list[OldVersion]):
    """Checklist of old version folders, with their size, and Recycle bin / Delete permanently buttons."""
    import imgui
    selected = {str(old.path): True for old in items}
    sizes: dict[str, int] = {}

    async def compute_sizes():
        loop = asyncio.get_running_loop()
        for old in items:
            sizes[str(old.path)] = await loop.run_in_executor(None, folder_size, old.path)
    async_thread.run(compute_sizes())

    def popup_content():
        imgui.text(
            "Older versions (or second copies) of games you have a newer version of. Untick the ones you want to keep.\n"
            "Save folders found inside are copied to F95Checker's data folder before deleting.\n\n"
        )
        total = 0
        for old in items:
            key = str(old.path)
            _, selected[key] = imgui.checkbox(f"###old_version_{key}", selected[key])
            imgui.same_line()
            imgui.text(f"{old.game}  -  {old.path}" + ("   [duplicate of the same version]" if old.kind == "duplicate" else ""))
            imgui.same_line()
            imgui.text_disabled(size_display(sizes[key]) if key in sizes else "computing size...")
            imgui.text_disabled(f"        version in use: {old.kept}")
            if selected[key]:
                total += sizes.get(key, 0)
        imgui.text(f"\nSelected: {size_display(total)}")

    def start(permanent: bool):
        chosen = [old for old in items if selected[str(old.path)]]
        if chosen:
            async_thread.run(delete_old_versions(chosen, permanent))

    buttons = {}
    if globals.os is Os.Windows:
        buttons[f"{icons.delete_restore} Recycle bin"] = lambda: start(False)
    buttons[f"{icons.trash_can_outline} Delete permanently"] = lambda: start(True)
    buttons[f"{icons.cancel} Cancel"] = None
    utils.push_popup(
        utils.popup, "Delete old versions",
        popup_content,
        buttons,
        closable=True,
        outside=False
    )


async def detect_install_dates(*games: Game):
    """Kept for the right click menu / Manage button: full manual scan of the given games."""
    await scan_library(*games, manual=True)


# ---------------------------------------------------------------------------
# Local info cache (size on disk), kept in <F95Checker data>/local_scan.json: no database change
# ---------------------------------------------------------------------------

_local_info: dict[int, dict] | None = None


def local_info() -> dict[int, dict]:
    global _local_info
    if _local_info is None:
        try:
            raw = json.loads((globals.data_path / "local_scan.json").read_text(encoding="utf-8"))
            _local_info = {int(key): value for key, value in raw.items() if isinstance(value, dict)}
        except Exception:
            _local_info = {}
    return _local_info


def save_local_info():
    try:
        (globals.data_path / "local_scan.json").write_text(json.dumps(local_info()), encoding="utf-8")
    except OSError as exc:
        _log(f"Cannot save local_scan.json: {exc}")


def local_size(game: Game) -> int:
    return local_info().get(game.id, {}).get("size", 0)


def local_size_display(game: Game) -> str:
    size = local_size(game)
    return size_display(size) if size else ""


def free_space(path: pathlib.Path | str) -> int:
    try:
        return shutil.disk_usage(pathlib.Path(path).anchor or path).free
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Updates to do: priorities, changelog since your version, space check, download via JDownloader
# ---------------------------------------------------------------------------

PREFERRED_HOSTS = ("pixeldrain", "gofile", "mega", "workupload", "buzzheavier", "datanodes", "mixdrop")
JD_CNL_URL = "http://127.0.0.1:9666/flash/add"  # JDownloader "Click'n'Load" local interface


def outdated_games() -> list[Game]:
    """Installed games whose local version is not the latest: played most recently first (never played last)."""
    games = [
        game for game in globals.games.values()
        if game.installed and game.installed != game.version and not game.archived and not game.custom
        and local_info().get(game.id, {}).get("ignored_version") != game.version
    ]
    games.sort(key=lambda game: (-game.last_launched.value, game.name.lower()))
    return games


def changelog_since(game: Game, max_lines: int = 60) -> str:
    """Part of the changelog above the heading of your installed version (whole start of it if not found)."""
    lines = [line for line in game.changelog.splitlines()]
    local = version_tuple(game.installed)
    if local:
        for i, line in enumerate(lines):
            line_version = version_tuple(line)
            # A short line holding a version <= yours = heading of a version you already have
            if i > 0 and line_version and line_version <= local and len(line.strip()) <= 60:
                lines = lines[:i]
                break
    text = "\n".join(lines[:max_lines]).strip()
    if len(lines) > max_lines:
        text += "\n[...]"
    return text or "No changelog in the thread."


def jdownloader_dir() -> pathlib.Path | None:
    local = os.environ.get("LOCALAPPDATA")
    path = pathlib.Path(local, "JDownloader 2") if local else None
    return path if path and path.is_dir() else None


def jdownloader_target() -> pathlib.Path | None:
    """Folder where JDownloader extracts (custom extraction path, else default download folder)."""
    jd = jdownloader_dir()
    if jd is None:
        return None
    try:
        extraction = json.loads((jd / "cfg" / "org.jdownloader.extensions.extraction.ExtractionExtension.json").read_text(encoding="utf-8"))
        if extraction.get("customextractionpathenabled") and extraction.get("customextractionpath"):
            return pathlib.Path(extraction["customextractionpath"])
        general = json.loads((jd / "cfg" / "org.jdownloader.settings.GeneralSettings.json").read_text(encoding="utf-8"))
        if general.get("defaultdownloadfolder"):
            return pathlib.Path(general["defaultdownloadfolder"])
    except (OSError, ValueError):
        pass
    return None


def space_warning(game: Game) -> str:
    """'' if there is room for the update. Estimate: archive + extracted game = 2x the size of your current copy."""
    size = local_size(game)
    target = jdownloader_target() or (pathlib.Path(local_info()[game.id]["path"]) if game.id in local_info() else None)
    if not size or target is None:
        return ""
    free = free_space(target)
    if free and free < 2 * size:
        return f"Low space on {target.anchor}: needs about {size_display(2 * size)}, {size_display(free)} free"
    return ""


def pick_download(game: Game) -> tuple[str, str] | None:
    """(host, link) to use: Windows section first, then hosts that need no account."""
    sections = [(name, mirrors) for name, mirrors in game.downloads if mirrors]
    if not sections:
        return None
    windows = [section for section in sections if re.search(r"win|pc", section[0], re.IGNORECASE)]
    _, mirrors = (windows or sections)[0]

    def rank(mirror):
        host = mirror[0].lower()
        return next((i for i, preferred in enumerate(PREFERRED_HOSTS) if preferred in host), len(PREFERRED_HOSTS))
    host, link = sorted(mirrors, key=rank)[0]
    return host, link


def send_to_jdownloader(link: str, source: str) -> bool:
    try:
        data = urllib.parse.urlencode({"urls": link, "source": source}).encode()
        with urllib.request.urlopen(urllib.request.Request(JD_CNL_URL, data=data), timeout=5) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        _log(f"JDownloader not reachable: {exc}")
        return False


_prefs: dict | None = None


def prefs() -> dict:
    """Small preferences of this module (local_scan_prefs.json): {"jdownloader": False}."""
    global _prefs
    if _prefs is None:
        try:
            _prefs = json.loads((globals.data_path / "local_scan_prefs.json").read_text(encoding="utf-8"))
        except Exception:
            _prefs = {}
        _prefs.setdefault("jdownloader", False)
        _prefs.setdefault("cheats_default", True)
    return _prefs


def save_prefs():
    try:
        (globals.data_path / "local_scan_prefs.json").write_text(json.dumps(prefs()), encoding="utf-8")
    except OSError as exc:
        _log(f"Cannot save local_scan_prefs.json: {exc}")


async def download_update(game: Game):
    """Default: opens the host page in your browser (full speed, you choose the file), then Import Update installs it.
    With "Send to JDownloader": resolves the real host link (same automated browser as the Downloads tab) for JDownloader."""
    from modules import api, callbacks, webview
    picked = pick_download(game)
    if picked is None or not picked[1]:
        callbacks.open_webpage(game.url)  # Thread without parsed download links
        return
    host, link = picked
    if not prefs()["jdownloader"]:
        # F95zone masked links open fine in your browser (you are logged in there): one click on the host button
        callbacks.open_webpage(link if link.startswith("http") else game.url)
        _log(f"'{game.name}': {host} page opened in the browser")
        return
    try:
        if link.startswith("//") or link.startswith(f"{api.f95_host}/masked/"):
            if not await api.assert_login():
                return
            if link.startswith("//"):
                args = ("xpath_redirect", game.url, "//*[contains(@class,'message-threadStarterPost')][1]" + link)
            else:
                args = ("css_redirect", link, "a.host_link")
            with await webview.start(*args, title=f"Retrieve link for {host}", size=(520, 480), pipe=True, minimal=True) as pipe:
                link = await pipe.get_async()
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, send_to_jdownloader, link, game.url):
            _log(f"'{game.name}': {host} link sent to JDownloader")
            utils.push_popup(
                msgbox.msgbox, "Download",
                f"{game.name} {game.version}: {host} link sent to JDownloader.\n"
                "After it is extracted, the next Refresh ticks Installed by itself.",
                MsgBox.info,
            )
            return
        # JDownloader closed: clipboard (it watches it) + start it
        callbacks.clipboard_copy(link)
        started = False
        jd = jdownloader_dir()
        if jd is not None and (jd / "JDownloader2.exe").is_file():
            subprocess.Popen([str(jd / "JDownloader2.exe")], cwd=str(jd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = True
        utils.push_popup(
            msgbox.msgbox, "Download",
            f"{game.name}: JDownloader is not running, the {host} link was copied to the clipboard.\n" +
            ("JDownloader is starting: copy the link again (middle click in the Downloads tab) once it is open,\nor paste it in JDownloader with Ctrl+V."
             if started else "Paste it in your download manager."),
            MsgBox.warn,
        )
    except Exception:
        utils.push_popup(
            msgbox.msgbox, "Download error",
            f"Could not retrieve the download link of {game.name}:\n{error.text()}",
            MsgBox.error,
            more=error.traceback(),
        )


def start_download(game: Game):
    """Download button: asks first when the disk is too full."""
    warning = space_warning(game)
    if warning:
        utils.push_popup(
            msgbox.msgbox, "Low disk space",
            f"{warning}.\nFree some space (delete old versions, finished games) or download anyway?",
            MsgBox.warn,
            {f"{icons.download} Download anyway": lambda: async_thread.run(download_update(game)), f"{icons.cancel} Cancel": None},
        )
    else:
        async_thread.run(download_update(game))


def _date(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(timestamp)) if timestamp else "never"


def open_updates_popup():
    """Updates to do, most recently played first, with what changed since your version and a download button."""
    import imgui
    from modules import callbacks
    games = outdated_games()
    roots = library_roots(globals.games.values())
    target = jdownloader_target()

    def ignore(game: Game):
        # Hidden from this list until the NEXT version comes out
        local_info().setdefault(game.id, {})["ignored_version"] = game.version
        save_local_info()
        games.remove(game)

    def popup_content():
        scaled = globals.gui.scaled
        free = "   ".join(f"{root.anchor} {size_display(free_space(root))}" for root in roots)
        imgui.text(f"{len(games)} game(s) where your copy is older than the latest version. Most recently played first.")
        imgui.text_disabled(f"Free space:  {free}" + (f"      JDownloader extracts to: {target}" if target and prefs()["jdownloader"] else ""))
        changed, value = imgui.checkbox("Download: send the link to JDownloader (otherwise: open the host page in your browser)", prefs()["jdownloader"])
        if changed:
            prefs()["jdownloader"] = value
            save_prefs()
        imgui.text_disabled("Browser: download the archive, then Import to install it into the game folder.")
        imgui.spacing()
        imgui.begin_child("###updates_list", width=scaled(980), height=min(scaled(560), imgui.io.display_size.y * 0.65))
        for game in list(games):
            if imgui.small_button(f"{icons.download} Download###update_dl_{game.id}"):
                start_download(game)
            imgui.same_line()
            if imgui.small_button(f"{icons.archive_arrow_down_outline} Import###update_import_{game.id}"):
                from modules import importupdate
                importupdate.import_update(game)
            imgui.same_line()
            if imgui.small_button(f"{icons.eye_off_outline} Ignore###update_ignore_{game.id}"):
                ignore(game)
                continue
            imgui.same_line()
            if imgui.small_button(f"{icons.open_in_new}###update_thread_{game.id}"):
                callbacks.open_webpage(game.url)
            imgui.same_line()
            imgui.text(game.name)
            imgui.same_line()
            imgui.text_disabled(f"{game.installed}  ->  {game.version}")
            if game.status is Status.Completed:
                imgui.same_line()
                imgui.text_colored("FINAL VERSION", 0.00, 0.87, 0.00)
            imgui.same_line()
            imgui.text_disabled(f"  played: {_date(game.last_launched.value)}   {local_size_display(game)}")
            warning = space_warning(game)
            if warning:
                imgui.text_colored(f"        {warning}", 0.95, 0.69, 0.10)
            if imgui.tree_node(f"What's new since {game.installed}###update_changes_{game.id}"):
                imgui.push_text_wrap_pos(scaled(940))
                imgui.text(changelog_since(game))
                imgui.pop_text_wrap_pos()
                imgui.tree_pop()
        imgui.end_child()

    utils.push_popup(
        utils.popup, "Updates to do",
        popup_content,
        buttons=True,
        closable=True,
        outside=False
    )


# ---------------------------------------------------------------------------
# Finished games: tick Finished, optionally uninstall (saves kept)
# ---------------------------------------------------------------------------

FINISHED_AFTER_DAYS = 60


def finished_candidates() -> list[Game]:
    """Completed by the developer, latest version installed, played, but not for 60+ days, not ticked Finished."""
    limit = time.time() - FINISHED_AFTER_DAYS * 86400
    games = [
        game for game in globals.games.values()
        if game.status is Status.Completed and game.installed and game.installed == game.version
        and not game.finished and 0 < game.last_launched.value < limit
    ]
    games.sort(key=lambda game: game.last_launched.value)
    return games


def _mark_finished(games: list[Game]):
    for game in games:
        game.finished = game.installed or game.version
        game.add_timeline_event(TimelineEventType.GameFinished, game.version)


async def uninstall_games(games: list[Game], permanent: bool):
    items, owners = [], {}
    for game in games:
        path = local_info().get(game.id, {}).get("path")
        if path and pathlib.Path(path).is_dir():
            items.append(OldVersion(game.name, pathlib.Path(path), pathlib.Path(path), "uninstall"))
            owners[path] = game
    await delete_old_versions(items, permanent, owners)


def open_finished_popup():
    import imgui
    games = finished_candidates()
    selected = {game.id: False for game in games}

    def popup_content():
        scaled = globals.gui.scaled
        if not games:
            imgui.text(
                f"No candidate: a candidate is a game completed by its developer, with the latest version installed,\n"
                f"that you played but not in the last {FINISHED_AFTER_DAYS} days. Run a local scan first to read your saves."
            )
            return
        imgui.text(
            f"Completed games, latest version installed, not played for {FINISHED_AFTER_DAYS}+ days. Tick the ones you finished.\n"
            "Uninstall deletes the game folder; its save folders are copied to F95Checker's data folder first,\n"
            "and saves kept by the engine in AppData are not touched.\n\n"
        )
        total = 0
        imgui.begin_child("###finished_list", width=scaled(860), height=min(scaled(420), imgui.io.display_size.y * 0.5))
        for game in games:
            _, selected[game.id] = imgui.checkbox(f"###finished_{game.id}", selected[game.id])
            imgui.same_line()
            imgui.text(game.name)
            imgui.same_line()
            imgui.text_disabled(f"{game.version}   last played: {_date(game.last_launched.value)}   {local_size_display(game)}")
            if selected[game.id]:
                total += local_size(game)
        imgui.end_child()
        imgui.text(f"Selected: {size_display(total)}")

    def chosen():
        return [game for game in games if selected[game.id]]

    def finish(uninstall: bool, permanent: bool = False):
        picked = chosen()
        _mark_finished(picked)
        if uninstall and picked:
            async_thread.run(uninstall_games(picked, permanent))

    buttons = {f"{icons.flag_checkered} Mark finished": lambda: finish(False)}
    if games:
        if globals.os is Os.Windows:
            buttons[f"{icons.delete_restore} + Uninstall (recycle bin)"] = lambda: finish(True, False)
        buttons[f"{icons.trash_can_outline} + Uninstall (permanent)"] = lambda: finish(True, True)
    else:
        buttons = {}
    buttons[f"{icons.cancel} Cancel"] = None
    utils.push_popup(
        utils.popup, "Finished games",
        popup_content,
        buttons,
        closable=True,
        outside=False
    )


# ---------------------------------------------------------------------------
# Engine detection, cheats, carry-over of mods and saves between versions
# ---------------------------------------------------------------------------

CONSOLE_FILE = "zz_f95checker_console.rpy"
CONSOLE_SCRIPT = (
    "# Added by F95Checker (right click > Enable Cheats). Delete this file to disable.\n"
    "init 999 python:\n"
    "    config.developer = True\n"
    "    config.console = True\n"
)
MOD_RE = re.compile(r"cheat|urm|universal.?ren|0x52|unren|walkthrough|wt.?mod|multi.?mod|gallery.?unlock", re.IGNORECASE)
MOD_EXTS = {".rpy", ".rpyc", ".rpa"}


def _listdir(path: pathlib.Path) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def detect_engine(exe: pathlib.Path) -> tuple[str, list[str]]:
    """(real engine from the files next to the executable, cheat mods found). ('', []) if unknown."""
    folder = exe.parent
    names = {name.lower() for name in _listdir(folder)}
    cheats = []
    if "renpy" in names and "game" in names:
        for name in _listdir(folder / "game"):
            low = name.lower()
            if low == CONSOLE_FILE:
                cheats.append("console")
            elif os.path.splitext(low)[1] in MOD_EXTS - {".rpyc"} and MOD_RE.search(low) and not re.search(r"walkthrough|wt.?mod", low):
                cheats.append(name)
        return "Ren'Py", cheats
    if f"{exe.stem.lower()}_data" in names:
        if "bepinex" in names or "melonloader" in names:
            cheats.append("mod loader")
        return ("Unity (IL2CPP)" if "gameassembly.dll" in names else "Unity (Mono)"), cheats
    js = folder / "www" / "js" if "www" in names else folder / "js"
    if (js / "rmmz_core.js").is_file() or (js / "rpg_core.js").is_file():
        cheats += [name for name in _listdir(js / "plugins") if "cheat" in name.lower()]
        return ("RPGM MZ" if (js / "rmmz_core.js").is_file() else "RPGM MV"), cheats
    if "engine" in names or any(name.endswith("-win64-shipping.exe") for name in names) \
            or any((folder / name / "Binaries" / "Win64").is_dir() for name in _listdir(folder)):
        return "Unreal", cheats
    if any(name.endswith(".pck") for name in names):
        return "Godot", cheats
    if "www" in names or "package.json" in names or "nw.dll" in names \
            or (folder / "resources" / "app.asar").is_file() or (folder / "resources" / "app").is_dir():
        return "HTML", cheats
    if "rgss301.dll" in names or any(name.endswith((".rgss3a", ".rgss2a", ".rvdata2")) for name in names):
        return "RPGM VX/Ace", cheats
    return "", cheats


def local_engine(game: Game) -> str:
    return local_info().get(game.id, {}).get("engine") or ""


_engine_now_cache: dict[int, tuple[float, str]] = {}


def engine_now(game: Game) -> str:
    """Engine from the last scan, or read from the game folder now (cheap, cached 30 s) when not scanned yet."""
    engine = local_engine(game)
    if engine:
        return engine
    cached = _engine_now_cache.get(game.id)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    exe = _main_exe(game)
    engine = detect_engine(exe)[0] if exe is not None else ""
    _engine_now_cache[game.id] = (time.monotonic(), engine)
    return engine


def local_cheats_display(game: Game) -> str:
    return ", ".join(local_info().get(game.id, {}).get("cheats") or [])


def _main_exe(game: Game) -> pathlib.Path | None:
    base = _exe_dir()
    for executable in game.executables:
        exe = _abs_exe(executable, base)
        try:
            if exe is not None and exe.is_file():
                return exe
        except OSError:
            pass
    return None


def _console_path(game: Game) -> pathlib.Path | None:
    exe = _main_exe(game)
    if exe is None or not (exe.parent / "game").is_dir() or not (exe.parent / "renpy").is_dir():
        return None
    return exe.parent / "game" / CONSOLE_FILE


def cheats_enabled(game: Game) -> bool:
    path = _console_path(game)
    return path is not None and path.is_file()


PREFS_FILE = "zz_f95checker_prefs.rpy"
RENPY_PREFS_DEFAULT = {
    "on": True,
    "skip_unseen": True,        # Skip also passes text you have not read yet
    "afm_time": None,           # Auto-forward speed 1 (fast) .. 40 (slow); None = leave the game's
    "music": None,              # volumes 0..100; None = leave the game's
    "sound": None,
    "voice": None,
    "fullscreen": None,         # True / False; None = leave the game's
}
PREFS_TEMPLATE = """# Added by F95Checker (Library tools > Ren'Py preferences). Delete this file to remove it.
# Sets your usual preferences once per F95Checker settings version, and only those still at the game's
# default value: what you changed in the game stays. Saves are not affected.
# Applied on the first frame (after Ren'Py has set its own defaults), then never again.
init 999 python:
    def _f95_prefs_default(name, configured):
        # what the game starts with: its config.default_* if set, else Ren'Py's own default
        if configured is not None:
            return configured
        try:
            for pref in renpy.preferences.all_preferences:
                if pref.name == name:
                    return pref.default
        except Exception:
            pass
        return None

    def _f95_prefs_apply():
        if _f95_prefs_apply.done:
            return
        _f95_prefs_apply.done = True
        try:
            if getattr(persistent, "f95checker_prefs", None) == {token!r}:
                return
            p = renpy.game.preferences
            want = {values!r}
            if "skip_unseen" in want and p.skip_unseen == _f95_prefs_default("skip_unseen", None):
                p.skip_unseen = want["skip_unseen"]
            if "afm_time" in want and p.afm_time == _f95_prefs_default("afm_time", config.default_afm_time):
                p.afm_time = want["afm_time"]
            for mixer, default in (("music", config.default_music_volume), ("sfx", config.default_sfx_volume),
                                   ("voice", config.default_voice_volume)):
                if mixer not in want:
                    continue
                try:
                    now = p.get_volume(mixer)
                except Exception:
                    now = p.volumes.get(mixer, default)
                if abs(now - default) < 0.001:
                    try:
                        p.set_volume(mixer, want[mixer])
                    except Exception:
                        p.volumes[mixer] = want[mixer]
            if "fullscreen" in want and p.fullscreen == _f95_prefs_default("fullscreen", config.default_fullscreen):
                p.fullscreen = want["fullscreen"]
            persistent.f95checker_prefs = {token!r}
        except Exception:
            pass
    _f95_prefs_apply.done = False
    config.periodic_callbacks.append(_f95_prefs_apply)
"""


def renpy_prefs() -> dict:
    p = prefs()
    current = p.get("renpy_prefs")
    if not isinstance(current, dict):
        current = p["renpy_prefs"] = {}
    for key, value in RENPY_PREFS_DEFAULT.items():
        current.setdefault(key, value)
    return current


def renpy_prefs_script() -> str | None:
    """Content of zz_f95checker_prefs.rpy for the current settings, None if the option is off or empty.
    Python 2 and 3 compatible (Ren'Py 6 to 8)."""
    rp = renpy_prefs()
    if not rp["on"]:
        return None
    values = {}
    if rp["skip_unseen"]:
        values["skip_unseen"] = True
    if rp["afm_time"] is not None:
        values["afm_time"] = int(rp["afm_time"])
    for key, mixer in (("music", "music"), ("sound", "sfx"), ("voice", "voice")):
        if rp[key] is not None:
            values[mixer] = round(max(0, min(100, int(rp[key]))) / 100.0, 2)
    if rp["fullscreen"] is not None:
        values["fullscreen"] = bool(rp["fullscreen"])
    if not values:
        return None
    token = "v1:" + json.dumps(values, sort_keys=True)
    return PREFS_TEMPLATE.format(token=token, values=dict(sorted(values.items())))


def ensure_renpy_prefs(game: Game, content: str | None = "") -> bool:
    """Writes / updates / removes zz_f95checker_prefs.rpy in a Ren'Py game. True if something changed.
    content="" = computed from the settings (renpy_prefs_script)."""
    path = _console_path(game)
    if path is None:
        return False
    path = path.with_name(PREFS_FILE)
    if content == "":
        content = renpy_prefs_script()
    try:
        if content is None:
            if path.is_file():
                path.unlink()
                path.with_suffix(".rpyc").unlink(missing_ok=True)
                return True
            return False
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return False
        path.write_text(content, encoding="utf-8")
        path.with_suffix(".rpyc").unlink(missing_ok=True)
        return True
    except OSError as exc:
        _log(f"'{game.name}': Ren'Py preferences not written: {exc}")
        return False


def apply_renpy_prefs_all() -> int:
    """Library tools > Ren'Py preferences > Apply: every Ren'Py game now (blocking: run it in a thread)."""
    content = renpy_prefs_script()
    return sum(1 for game in list(globals.games.values()) if ensure_renpy_prefs(game, content))


def ensure_default_cheats(game: Game) -> bool:
    """Library tools > Cheats in Ren'Py games: adds the console script to a Ren'Py game (new version folders too),
    unless you disabled the cheats of this game by hand. True if it was added."""
    if not prefs().get("cheats_default", True) or local_info().get(game.id, {}).get("cheats_off"):
        return False
    path = _console_path(game)
    if path is None or path.is_file():
        return False
    try:
        path.write_text(CONSOLE_SCRIPT, encoding="utf-8")
        _log(f"'{game.name}': cheats console added")
        return True
    except OSError as exc:
        _log(f"'{game.name}': cheats console not added: {exc}")
        return False


def toggle_cheats(game: Game):
    """Ren'Py: adds / removes a 4-line script enabling the developer console. Nothing else is modified."""
    path = _console_path(game)
    if path is None:
        utils.push_popup(
            msgbox.msgbox, "Cheats",
            f"{game.name} is not a Ren'Py game (or its executable was not found): no one-click cheat for it.",
            MsgBox.warn,
        )
        return
    try:
        if path.is_file():
            path.unlink()
            path.with_suffix(".rpyc").unlink(missing_ok=True)
            local_info().setdefault(game.id, {})["cheats_off"] = True  # not added back by the next scan
            save_local_info()
            message = f"Cheats disabled for {game.name}: console script removed."
        else:
            path.write_text(CONSOLE_SCRIPT, encoding="utf-8")
            if local_info().get(game.id, {}).pop("cheats_off", None):
                save_local_info()
            message = (
                f"Cheats enabled for {game.name}. Restart the game, then:\n"
                " - Shift+O opens the console: type for example  money = 99999  then Enter\n"
                " - Shift+D opens the developer menu (Variable Viewer lists the game's variables)\n"
                "Variable names differ in every game. Save before changing them.\n"
                "After updating the game to a new folder, enable it again."
            )
    except OSError as exc:
        message = f"Could not change the cheats of {game.name}:\n{exc}"
    info = local_info().get(game.id)
    exe = _main_exe(game)
    if info is not None and exe is not None:
        info["engine"], info["cheats"] = detect_engine(exe)
        save_local_info()
    utils.push_popup(msgbox.msgbox, "Cheats", message, MsgBox.info)


UNIVERSAL_MOD_RE = re.compile(r"urm|universal|0x52|unren|console", re.IGNORECASE)   # work with any version
DISABLED_MODS_DIR = "_disabled_mods"


def outdated_mods(exe: pathlib.Path, files_ts: int) -> list[str]:
    """Ren'Py: version-specific mod files in game/ (2 levels) dated more than a day before the newest game file."""
    game_dir = exe.parent / "game"
    if not files_ts or not (exe.parent / "renpy").is_dir() or not game_dir.is_dir():
        return []
    found = []
    for current, dirs, files in os.walk(game_dir):
        depth = len(pathlib.Path(current).relative_to(game_dir).parts)
        dirs[:] = [d for d in dirs if depth < 2 and d.lower() not in EXCLUDED_DIRS]
        for name in files:
            low = name.lower()
            if os.path.splitext(low)[1] not in MOD_EXTS or not MOD_RE.search(low) or UNIVERSAL_MOD_RE.search(low):
                continue
            if low in EXCLUDED_FILES:
                continue
            with contextlib.suppress(OSError):
                if os.stat(os.path.join(current, name)).st_mtime < files_ts - 86400:
                    found.append((pathlib.Path(current) / name).relative_to(game_dir).as_posix())
    return sorted(found, key=str.lower)


def disable_mod(game: Game, rel: str) -> str:
    """Moves game/<rel> to <game folder>/_disabled_mods/<rel> (outside game/: Ren'Py loads every subfolder of game/)."""
    exe = _main_exe(game)
    if exe is None:
        return "executable not found"
    src = exe.parent / "game" / rel
    dst = exe.parent / DISABLED_MODS_DIR / rel
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        if src.suffix.lower() == ".rpy":                   # its compiled copy too
            compiled = src.with_suffix(".rpyc")
            if compiled.is_file():
                os.replace(compiled, dst.with_suffix(".rpyc"))
    except OSError as exc:
        return str(exc)
    info = local_info().setdefault(game.id, {})
    info["mods_old"] = [m for m in info.get("mods_old", []) if m not in (rel, rel[:-4] + ".rpyc" if rel.endswith(".rpy") else rel)]
    save_local_info()
    _log(f"'{game.name}': mod {rel} moved to {DISABLED_MODS_DIR}")
    return ""


def keep_mod(game: Game, rel: str):
    info = local_info().setdefault(game.id, {})
    info.setdefault("mods_old_ok", []).append(rel)
    info["mods_old"] = [m for m in info.get("mods_old", []) if m != rel]
    save_local_info()


def outdated_mods_of(game: Game) -> list[str]:
    return list(local_info().get(game.id, {}).get("mods_old", []))


def open_outdated_mods_popup(games: list[Game] = None):
    import imgui
    from modules import discover

    def content():
        rows = [(g, outdated_mods_of(g)) for g in (games or globals.games.values())]
        rows = [(g, mods) for g, mods in rows if mods]
        imgui.text_disabled("These walkthrough / cheat / gallery mods are older than the version of the game installed now.\n"
                            "A mod made for an older version can crash the game or break its scenes.\n"
                            f"Disable = moved to <game folder>\\{DISABLED_MODS_DIR} (move it back to game\\ to undo).")
        if not rows:
            imgui.text("Nothing left to check.")
            return
        for g, mods in sorted(rows, key=lambda r: r[0].name.lower()):
            imgui.separator()
            imgui.text(f"{g.name}  ({g.installed or g.version})")
            imgui.same_line()
            if imgui.small_button(f"Find an update of the mod###mo_find_{g.id}"):
                discover.open_mods_popup([g])
            for rel in mods:
                imgui.bullet_text(rel)
                imgui.same_line()
                if imgui.small_button(f"Disable###mo_off_{g.id}_{rel}"):
                    error_text = disable_mod(g, rel)
                    if error_text:
                        utils.push_popup(msgbox.msgbox, "Mods", f"{rel}: {error_text}", MsgBox.warn)
                imgui.same_line()
                if imgui.small_button(f"It works, keep it###mo_ok_{g.id}_{rel}"):
                    keep_mod(g, rel)

    utils.push_popup(utils.popup, "Mods to check", content, buttons=True, closable=True, outside=False, popup_uuid="mods_old")


@dataclasses.dataclass(slots=True)
class ModCarry:
    game: str
    files: list[pathlib.Path]   # Mod files of the old version missing from the new one
    dest: pathlib.Path          # "game" folder of the version in use

    def __str__(self):
        return f"{self.game}: " + ", ".join(file.name for file in self.files)


def find_missing_mods(old_folder: pathlib.Path, exe: pathlib.Path) -> list[pathlib.Path]:
    """Ren'Py cheat / walkthrough mod files present in an old version but not next to the current executable."""
    dest = exe.parent / "game"
    have = {name.lower() for name in _listdir(dest)}
    found = []
    stack = [(old_folder, 0)]
    while stack:
        current, depth = stack.pop()
        if (current / "renpy").is_dir() and (current / "game").is_dir():
            for name in _listdir(current / "game"):
                low = name.lower()
                if os.path.splitext(low)[1] in MOD_EXTS and MOD_RE.search(low) and low not in have and low != CONSOLE_FILE \
                        and (current / "game" / name).is_file():
                    found.append(current / "game" / name)
            continue
        if depth < 3:
            stack += [(child, depth + 1) for child in Library._subdirs(current)]
    return found


def copy_mods(mods: list[ModCarry]):
    done, failed = [], []
    for mod in mods:
        for file in mod.files:
            try:
                if not (mod.dest / file.name).exists():
                    shutil.copy2(file, mod.dest / file.name)
                done.append(f"{mod.game}: {file.name}")
            except OSError as exc:
                failed.append(f"{mod.game}: {file.name}: {exc}")
    more = ""
    for title, names in (("Copied", done), ("Failed", failed)):
        if names:
            more += f"{title}:\n - " + "\n - ".join(names) + "\n\n"
    utils.push_popup(
        msgbox.msgbox, "Copy mods",
        f"{len(done)} mod file(s) copied to the new versions." + (f" {len(failed)} failed." if failed else "") + "\n"
        "A mod made for an older version can break the new one (errors at launch, missing scenes).\n"
        "If a game misbehaves, delete the copied files listed in the details, or get the mod's update on F95zone.",
        MsgBox.warn if failed else MsgBox.info,
        more=more.strip() or None,
    )


def carry_rpgm_saves(old_folder: pathlib.Path, exe: pathlib.Path) -> int:
    """RPG Maker keeps saves inside the game folder: copies them from the old version when the new one has none.
    Never overwrites. Returns the number of files copied."""
    dest = exe.parent / "www" / "save" if (exe.parent / "www").is_dir() else exe.parent / "save"
    if any(name.lower().endswith((".rpgsave", ".rmmzsave")) for name in _listdir(dest)):
        return 0
    stack = [(old_folder, 0)]
    while stack:
        current, depth = stack.pop()
        for child in Library._subdirs(current):
            saves = [name for name in _listdir(child) if name.lower().endswith((".rpgsave", ".rmmzsave"))] if child.name.lower() == "save" else []
            if saves:
                try:
                    shutil.copytree(child, dest, dirs_exist_ok=True)
                except OSError as exc:
                    _log(f"Could not copy saves {child} -> {dest}: {exc}")
                    return 0
                return len(saves)
            if depth < 4:
                stack.append((child, depth + 1))
    return 0
