"""
PC tools: downloads watcher, transparent compression of game folders, per-game display profile.

Downloads watcher
    Every few seconds, looks at your download folders (Downloads by default). When a .zip / .7z / .rar
    finishes downloading (size stable, no .part / .crdownload next to it) and its name looks like one
    of your games, F95Checker asks: Import it into <game>? Archives already there when F95Checker
    starts are not proposed.

Compression (Windows, NTFS drives)
    Windows' own file compression for programs (compact.exe /EXE:LZX, the same as CompactGUI): the
    game folder takes less space and runs as before (decompression on the fly, usually faster than
    reading more from the disk). Reversible at any time. A file the game rewrites is silently stored
    uncompressed again; after Import Update, the new files are compressed again automatically.
    The gain is estimated first by compressing samples of the files (images / videos / audio are
    already compressed and don't gain anything).

Display profile (per game)
    - Lossless Scaling: started with the game, the scaling hotkey (Ctrl+Alt+S by default) is sent once the
      game window has been in front for a few seconds, closed with the game if F95Checker started it.
    - ReShade: if ReShade is installed in the game, it is switched on or off before each launch
      (its DLL is renamed to .dll.off when off).
"""
import asyncio
import contextlib
import dataclasses
import os
import pathlib
import random
import re
import subprocess
import time
import zlib

from common.structs import (
    Game,
    LaunchState,
    MsgBox,
    Os,
)
from external import (
    async_thread,
    error,
)
from modules import (
    globals,
    icons,
    importupdate,
    installdate,
    msgbox,
    utils,
)

LOG_PREFIX = "[PcTools]"
_NO_WINDOW = 0x08000000


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("watch_downloads", True)
    p.setdefault("watch_dirs", [])
    p.setdefault("display", {})
    p.setdefault("lossless_path", "")
    p.setdefault("lossless_hotkey", "ctrl+alt+s")
    p.setdefault("lossless_delay", 4.0)
    return p


def watch_dirs() -> list[pathlib.Path]:
    dirs = [pathlib.Path(d) for d in prefs()["watch_dirs"] if d] or [pathlib.Path.home() / "Downloads"]
    return [d for d in dirs if d.is_dir()]


# ---------------------------------------------------------------------------
# Downloads watcher
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class _Pending:
    size: int
    mtime: float
    stable: int = 0


_seen: dict[str, tuple[int, float]] = {}
_pending: dict[str, _Pending] = {}
_baseline_done = False


def match_archive(name: str) -> list[tuple[float, Game]]:
    """Games whose name looks like the archive name, best first. Outdated installed games get a small bonus."""
    stem = re.sub(r"\.(zip|7z|rar)$", "", name, flags=re.IGNORECASE)
    out = []
    for game in globals.games.values():
        if game.custom and not game.executables:
            continue
        score = installdate.name_score(stem, game.name)
        exe = installdate._main_exe(game)
        if exe is not None:
            score = max(score, installdate.similarity(installdate.folder_key(stem), installdate.folder_key(exe.parent.name)))
        if score < 0.75:
            continue
        if game.installed and game.installed != game.version:
            score += 0.05
        out.append((score, game))
    return sorted(out, key=lambda sg: -sg[0])


def _scan_downloads() -> list[pathlib.Path]:
    """Archives that just finished downloading (blocking, worker thread)."""
    global _baseline_done
    ready = []
    for folder in watch_dirs():
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        names = {e.name.lower() for e in entries}
        for entry in entries:
            low = entry.name.lower()
            if not low.endswith(importupdate.ARCHIVE_EXTS) or not entry.is_file():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            key = entry.path
            sig = (st.st_size, st.st_mtime)
            if not _baseline_done or _seen.get(key) == sig:
                _seen[key] = sig
                continue
            if f"{low}.part" in names or f"{low}.crdownload" in names or st.st_size == 0:
                continue                         # Firefox / Chrome still writing it
            pending = _pending.get(key)
            if pending is None or (pending.size, pending.mtime) != sig:
                _pending[key] = _Pending(*sig)
                continue
            pending.stable += 1
            if pending.stable < 2:                # unchanged for 2 more polls
                continue
            try:
                with open(key, "rb"):
                    pass
            except OSError:
                continue                         # still locked by the browser / antivirus
            _pending.pop(key, None)
            _seen[key] = sig
            ready.append(pathlib.Path(key))
    _baseline_done = True
    return ready


def _propose(path: pathlib.Path):
    matches = match_archive(path.name)
    if not matches:
        _log(f"{path.name}: downloaded, no game matches its name")
        return
    game = matches[0][1]
    versions = f"{game.installed or 'not installed'} -> {game.version}" if game.installed != game.version else f"{game.version}, up to date"
    buttons = {f"{icons.archive_arrow_down_outline} Import into {game.name[:40]}": lambda: importupdate.import_file(game, path)}
    for _, other in matches[1:3]:
        buttons[f"{other.name[:40]}"] = (lambda g: lambda: importupdate.import_file(g, path))(other)
    buttons[f"{icons.cancel} Ignore"] = None
    utils.push_popup(
        msgbox.msgbox, "Update downloaded",
        f"{path.name}\nhas finished downloading in {path.parent}.\n\n"
        f"It looks like {game.name} ({versions}).\nImport it into the game folder now?",
        MsgBox.info, buttons,
    )
    with contextlib.suppress(Exception):
        from modules import notification_proc
        notification_proc.notify(title="Update downloaded", msg=f"{path.name}: import into {game.name}?")


async def _watch_loop():
    loop = asyncio.get_running_loop()
    while True:
        try:
            if prefs()["watch_downloads"]:
                for path in await loop.run_in_executor(None, _scan_downloads):
                    _propose(path)
        except Exception:
            _log(error.traceback())
        await asyncio.sleep(4)


# ---------------------------------------------------------------------------
# Compression (compact.exe /EXE:LZX)
# ---------------------------------------------------------------------------

INCOMPRESSIBLE = {
    ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".webm", ".mp4", ".ogv", ".mkv", ".avi", ".mov", ".m4v",
    ".ogg", ".opus", ".mp3", ".m4a", ".aac", ".flac", ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst",
    ".bk2", ".usm", ".wem", ".bank", ".ktx2", ".crn",
}


def filesystem(path: pathlib.Path) -> str:
    """'NTFS', 'exFAT', ... ('' if unknown / not Windows)."""
    if globals.os is not Os.Windows:
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(64)
        root = str(pathlib.Path(path).anchor)
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, buf, 64):
            return buf.value
    except Exception:
        pass
    return ""


def on_disk_size(path: pathlib.Path) -> int:
    """Real space used (compressed size) of a folder, Windows only (else logical size)."""
    if globals.os is not Os.Windows:
        return installdate.folder_size(path)
    import ctypes
    import ctypes.wintypes
    get = ctypes.windll.kernel32.GetCompressedFileSizeW
    get.restype = ctypes.wintypes.DWORD
    total = 0
    for current, _, files in os.walk(path):
        for name in files:
            full = os.path.join(current, name)
            high = ctypes.wintypes.DWORD(0)
            low = get(full, ctypes.byref(high))
            if low == 0xFFFFFFFF and ctypes.GetLastError() != 0:
                with contextlib.suppress(OSError):
                    total += os.lstat(full).st_size
                continue
            total += (high.value << 32) + low
    return total


def estimate(folder: pathlib.Path, budget: int = 160 << 20) -> tuple[int, int]:
    """(logical size, estimated size after LZX). Samples 32 KB chunks of the compressible files with zlib
    (LZX does a little better: about 8 % smaller), files < 4 KB and media files count as incompressible."""
    files = []
    for current, _, names in os.walk(folder):
        for name in names:
            full = os.path.join(current, name)
            with contextlib.suppress(OSError):
                files.append((full, os.lstat(full).st_size, os.path.splitext(name)[1].lower()))
    logical = sum(size for _, size, _ in files)
    if not files:
        return 0, 0
    estimated = 0
    ratios: dict[str, list[float]] = {}
    candidates = [f for f in files if f[1] >= 4096 and f[2] not in INCOMPRESSIBLE]
    rnd = random.Random(95)
    # sample the biggest files first, then random ones, within the read budget
    order = sorted(candidates, key=lambda f: -f[1])[:200] + rnd.sample(candidates, min(len(candidates), 300))
    read = 0
    sampled: dict[str, float] = {}
    for full, size, ext in order:
        if full in sampled or read >= budget:
            continue
        chunks = []
        try:
            with open(full, "rb") as f:
                for pos in sorted({0, size // 2, max(0, size - 32768)} if size > 98304 else {0}):
                    f.seek(pos)
                    chunks.append(f.read(32768))
        except OSError:
            continue
        raw = sum(len(c) for c in chunks) or 1
        packed = 0
        for chunk in chunks:
            size_zlib = len(zlib.compress(chunk, 6))
            # WOF keeps a chunk raw when it doesn't shrink; LZX ends up ~8 % smaller than zlib otherwise
            packed += len(chunk) if size_zlib > len(chunk) * 0.95 else size_zlib * 0.92
        ratio = min(1.0, packed / raw)
        sampled[full] = ratio
        ratios.setdefault(ext, []).append(ratio)
        read += raw
    for full, size, ext in files:
        if size < 4096 or ext in INCOMPRESSIBLE:
            estimated += size
        elif full in sampled:
            estimated += int(size * sampled[full])
        elif ext in ratios:
            estimated += int(size * sum(ratios[ext]) / len(ratios[ext]))
        else:
            estimated += int(size * 0.85)
    return logical, estimated


def game_folder(game: Game) -> pathlib.Path | None:
    exe = installdate._main_exe(game)
    return exe.parent if exe is not None else None


def is_compressed(game: Game) -> bool:
    return bool(installdate.local_info().get(game.id, {}).get("compressed"))


@dataclasses.dataclass(slots=True)
class CompressJob:
    queue: list = dataclasses.field(default_factory=list)
    current: str = ""
    done: int = 0
    log: list = dataclasses.field(default_factory=list)
    running: bool = False
    cancel: bool = False


compress_job = CompressJob()
estimates: dict[int, tuple[int, int, float]] = {}      # game id -> (logical, estimated, when)
_estimating: set[int] = set()


def _compact(folder: pathlib.Path, compress: bool) -> tuple[int, str]:
    args = ["compact", "/C" if compress else "/U", f"/S:{folder}", "/EXE:LZX" if compress else "/EXE", "/I", "/Q", "*"]
    proc = subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW if globals.os is Os.Windows else 0,
                          stdin=subprocess.DEVNULL, timeout=6 * 3600)
    out = (proc.stdout or b"").decode("cp850", "replace").strip().splitlines()
    return proc.returncode, (out[-1] if out else "")


async def compress_game(game: Game, quiet=False, uncompress=False) -> str:
    """Compresses (or uncompresses) one game folder. Returns a one-line summary."""
    folder = game_folder(game)
    if folder is None:
        return f"{game.name}: executable not found."
    if globals.os is not Os.Windows:
        return "Compression is only available on Windows."
    fs = filesystem(folder)
    if fs and fs.upper() != "NTFS":
        return f"{game.name}: drive {folder.anchor} is {fs}, compression needs NTFS."
    if game.launch_state is not LaunchState.Idle:
        return f"{game.name} is running: skipped."
    loop = asyncio.get_running_loop()
    before = await loop.run_in_executor(None, on_disk_size, folder)
    code, last = await loop.run_in_executor(None, _compact, folder, not uncompress)
    after = await loop.run_in_executor(None, on_disk_size, folder)
    info = installdate.local_info().setdefault(game.id, {})
    if uncompress:
        info.pop("compressed", None)
    else:
        info["compressed"] = int(time.time())
        info["compressed_size"] = after
    installdate.save_local_info()
    delta = before - after
    verb = "uncompressed" if uncompress else "compressed"
    summary = (f"{game.name}: {verb}, {installdate.size_display(before)} -> {installdate.size_display(after)} on disk "
               f"({'+' if delta < 0 else '-'}{installdate.size_display(abs(delta))})")
    if code != 0:
        summary += f" - compact.exe reported: {last}"
    _log(summary)
    return summary


async def _run_compress_queue():
    job = compress_job
    if job.running:
        return
    job.running = True
    job.cancel = False
    try:
        while job.queue and not job.cancel:
            game, uncompress = job.queue.pop(0)
            job.current = game.name
            job.log.append(await compress_game(game, uncompress=uncompress))
            job.done += 1
    except Exception:
        job.log.append(f"Error: {error.text()}")
        _log(error.traceback())
    finally:
        job.running = False
        job.current = ""


async def _estimate(game: Game):
    if game.id in _estimating:
        return
    _estimating.add(game.id)
    try:
        folder = game_folder(game)
        if folder is not None:
            logical, est = await asyncio.get_running_loop().run_in_executor(None, estimate, folder)
            estimates[game.id] = (logical, est, time.time())
    finally:
        _estimating.discard(game.id)


async def _estimate_all(games: list[Game]):
    for game in games:
        if game.id not in estimates:
            await _estimate(game)


def open_compression_popup():
    import imgui
    if globals.os is not Os.Windows:
        utils.push_popup(msgbox.msgbox, "Compression", "Compression is only available on Windows.", MsgBox.info)
        return
    games = sorted((g for g in globals.games.values() if game_folder(g) is not None), key=lambda g: g.name.lower())
    selected: set[int] = set()
    state = {"filter": 0, "started": False}
    filters = ["All", "Finished", "Not played for 30 days", "Compressed"]
    if not state["started"]:
        state["started"] = True
        async_thread.run(_estimate_all(games))

    def keep(game: Game) -> bool:
        f = state["filter"]
        if f == 1:
            return bool(game.finished)
        if f == 2:
            return time.time() - max(game.last_launched.value, game.installed_on or 0) > 30 * 86400
        if f == 3:
            return is_compressed(game)
        return True

    def content():
        job = compress_job
        imgui.text_disabled("Windows compresses the files of the game (like CompactGUI). The game runs as before, it's reversible.\n"
                            "Gain is high on Unity / Unreal / RPG Maker (code, data), low on Ren'Py (images and videos are already compressed).\n"
                            "Estimates come from samples of the files; the real result is shown after compression.")
        if job.running:
            imgui.text(f"Working: {job.current}  ({job.done} done, {len(job.queue)} waiting)")
            if imgui.small_button("Stop after this game"):
                job.cancel = True
        for line in job.log[-6:]:
            imgui.text_disabled(line)
        imgui.set_next_item_width(globals.gui.scaled(220) if globals.gui else 220)
        _, state["filter"] = imgui.combo("Show###comp_filter", state["filter"], filters)
        total_gain = sum(max(0, e[0] - e[1]) for gid, e in estimates.items() if gid in selected)
        imgui.same_line()
        if imgui.button(f"Compress selected ({len(selected)}, ~{installdate.size_display(total_gain)})###comp_go") and selected:
            for gid in sorted(selected, key=lambda i: -(estimates.get(i, (0, 0))[0] - estimates.get(i, (0, 0))[1])):
                if (game := globals.games.get(gid)) is not None:
                    job.queue.append((game, False))
            selected.clear()
            async_thread.run(_run_compress_queue())
        imgui.same_line()
        if imgui.button("Select gain > 20 %###comp_auto"):
            for g in games:
                e = estimates.get(g.id)
                if e and e[0] and (e[0] - e[1]) / e[0] > 0.2 and not is_compressed(g) and keep(g):
                    selected.add(g.id)
        flags = imgui.TABLE_SCROLL_Y | imgui.TABLE_ROW_BACKGROUND | imgui.TABLE_BORDERS_INNER_HORIZONTAL
        if imgui.begin_table("###comp_table", 6, flags, outer_size_height=globals.gui.scaled(420) if globals.gui else 420):
            for name in ("", "Game", "Size", "Estimated after", "Gain", "State"):
                imgui.table_setup_column(name)
            imgui.table_headers_row()
            for g in games:
                if not keep(g):
                    continue
                imgui.table_next_row()
                imgui.table_next_column()
                changed, on = imgui.checkbox(f"###comp_sel_{g.id}", g.id in selected)
                if changed:
                    (selected.add if on else selected.discard)(g.id)
                imgui.table_next_column()
                imgui.text(g.name)
                e = estimates.get(g.id)
                imgui.table_next_column()
                imgui.text(installdate.size_display(e[0]) if e else installdate.local_size_display(g) or "...")
                imgui.table_next_column()
                imgui.text(installdate.size_display(e[1]) if e else ("estimating..." if g.id in _estimating else "..."))
                imgui.table_next_column()
                if e and e[0]:
                    pct = (e[0] - e[1]) / e[0] * 100
                    imgui.text(f"{pct:.0f} %  ({installdate.size_display(e[0] - e[1])})")
                imgui.table_next_column()
                if is_compressed(g):
                    imgui.text(f"compressed, {installdate.size_display(installdate.local_info()[g.id].get('compressed_size', 0))}")
                    imgui.same_line()
                    if imgui.small_button(f"Undo###comp_undo_{g.id}"):
                        job.queue.append((g, True))
                        async_thread.run(_run_compress_queue())
            imgui.end_table()

    utils.push_popup(utils.popup, "Compression", content, buttons=True, closable=True, outside=False, popup_uuid="pctools_compress")


# ---------------------------------------------------------------------------
# Display profile: Lossless Scaling, ReShade
# ---------------------------------------------------------------------------

RESHADE_DLLS = ("dxgi.dll", "d3d9.dll", "d3d10.dll", "d3d11.dll", "d3d12.dll", "opengl32.dll", "dinput8.dll")


def display_cfg(game: Game) -> dict:
    cfg = prefs()["display"].setdefault(str(game.id), {})
    cfg.setdefault("lossless", False)
    cfg.setdefault("reshade", None)          # None = leave as it is, True / False = force at launch
    return cfg


def _steam_libraries() -> list[pathlib.Path]:
    roots = []
    if globals.os is Os.Windows:
        with contextlib.suppress(Exception):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                roots.append(pathlib.Path(winreg.QueryValueEx(key, "SteamPath")[0]))
    roots += [pathlib.Path(r"C:\Program Files (x86)\Steam"), pathlib.Path(r"C:\Program Files\Steam")]
    libs = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        with contextlib.suppress(OSError):
            for match in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                libs.append(pathlib.Path(match.group(1).replace("\\\\", "\\")))
        libs.append(root)
    return list(dict.fromkeys(libs))


_ls_cache: tuple[float, pathlib.Path | None] = (0.0, None)


def find_lossless() -> pathlib.Path | None:
    global _ls_cache
    custom = prefs()["lossless_path"]
    if custom and pathlib.Path(custom).is_file():
        return pathlib.Path(custom)
    if time.monotonic() - _ls_cache[0] < 60:
        return _ls_cache[1]
    found = None
    for lib in _steam_libraries():
        exe = lib / "steamapps" / "common" / "Lossless Scaling" / "LosslessScaling.exe"
        if exe.is_file():
            found = exe
            break
    _ls_cache = (time.monotonic(), found)
    return found


def reshade_dlls(game: Game) -> list[pathlib.Path]:
    """ReShade DLLs installed for this game (enabled or .off), next to the exe or Ren'Py's real exe in lib/."""
    exe = installdate._main_exe(game)
    if exe is None:
        return []
    dirs = [exe.parent] + [d for d in (exe.parent / "lib").glob("*windows*") if d.is_dir()] if (exe.parent / "lib").is_dir() else [exe.parent]
    found = []
    for d in dirs:
        if not (d / "ReShade.ini").is_file():
            continue
        for name in RESHADE_DLLS:
            for candidate in (d / name, d / f"{name}.off"):
                if candidate.is_file():
                    found.append(candidate)
    return found


def reshade_enabled(game: Game) -> bool | None:
    dlls = reshade_dlls(game)
    if not dlls:
        return None
    return any(not d.name.endswith(".off") for d in dlls)


def set_reshade(game: Game, on: bool) -> str:
    errors = []
    for dll in reshade_dlls(game):
        try:
            if on and dll.name.endswith(".off"):
                dll.rename(dll.with_name(dll.name[:-4]))
            elif not on and not dll.name.endswith(".off"):
                dll.rename(dll.with_name(dll.name + ".off"))
        except OSError as exc:
            errors.append(f"{dll.name}: {exc}")
    return "; ".join(errors)


def reshade_target(game: Game) -> pathlib.Path | None:
    """The exe to choose in the ReShade installer (Ren'Py runs a copy of python in lib\\(py3-)windows-x86_64)."""
    exe = installdate._main_exe(game)
    if exe is None:
        return None
    for sub in ("lib/py3-windows-x86_64", "lib/windows-x86_64", "lib/py2-windows-x86_64"):
        candidate = exe.parent / sub / exe.name
        if candidate.is_file():
            return candidate
    return exe


_VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}


# ---------------------------------------------------------------------------
# DLSS 5 Neural Rendering kit for Ren'Py (ReShade + DLSS5-Feeder, F95zone guide 312990)
# One copy of the files per drive (<library root>\_F95Checker_DLSS5), NTFS hard links in each game.
# ---------------------------------------------------------------------------

DLSS5_KIT = "_F95Checker_DLSS5"
DLSS5_MANIFEST = "_f95dlss5.json"
DLSS5_LINKED = [
    "dxgi.dll", "dlss5-feed.addon64", "renodx-dlss5.addon64", "nvngx_dlss.dll", "nvngx_dlssnr.dll",
    "reshade-shaders/Shaders/DLSS5_Feed.fx", "reshade-shaders/Shaders/ReShade.fxh", "reshade-shaders/Shaders/ReShadeUI.fxh",
    "reshade-shaders/Shaders/LumeniteFX/lumenite_Kernel.fx",
    "reshade-shaders/Shaders/LumeniteFX/include/lumenite_Compute.fxh",
    "reshade-shaders/Shaders/LumeniteFX/include/lumenite_Helpers.fxh",
    "reshade-shaders/Shaders/LumeniteFX/include/lumenite_Projections.fxh",
]
DLSS5_RESHADE_INI = """[GENERAL]
EffectSearchPaths=.\\reshade-shaders\\Shaders\\**
TextureSearchPaths=.\\reshade-shaders\\Textures\\**
PresetPath=.\\ReShadePreset.ini

[OVERLAY]
TutorialProgress=4

[RenoDX.DLSS5]
EnableHooks=2
NeuralUplift=1
"""
DLSS5_PRESET_INI = """Techniques=Lumenite_Kernel@lumenite_Kernel.fx,DLSS5_Feed@DLSS5_Feed.fx
TechniqueSorting=Lumenite_Kernel@lumenite_Kernel.fx,DLSS5_Feed@DLSS5_Feed.fx,DLSS5_Feed_Debug@DLSS5_Feed.fx

[DLSS5_Feed.fx]
DEBUG_VIEW=0
MV_SCALE=1.000000
MV_SIGN=1.000000,1.000000
PreprocessorDefinitions=DLSS5_MV_PROVIDER=3
"""
DLSS5_RPY = """# Added by F95Checker: DLSS 5 Neural Rendering (ReShade + DLSS5-Feeder, F95zone guide thread 312990).
# With ReShade on (dxgi.dll next to the game's exe): Direct3D 11 renderer (ANGLE2, which ReShade hooks),
# powersave off, 30 fps cap (RTX 3060: keeps the GPU cool, the neural pass is slow anyway).
# With ReShade switched off by F95Checker (dxgi.dll.off): Ren'Py defaults are restored.
# Delete this file (and dlss5_settings.rpyc) to stop it; the files next to the exe are listed in _f95dlss5.json.

init python:
    try:
        import os as _f95dlss5_os
        if _f95dlss5_os.path.exists(_f95dlss5_os.path.join(config.basedir, "dxgi.dll")):
            renpy.game.preferences.renderer = "angle2"
            renpy.game.preferences.gl_powersave = False
            renpy.game.preferences.gl_framerate = 30
        elif _f95dlss5_os.path.exists(_f95dlss5_os.path.join(config.basedir, "dxgi.dll.off")) and renpy.game.preferences.renderer == "angle2":
            renpy.game.preferences.renderer = "auto"
            renpy.game.preferences.gl_powersave = True
            renpy.game.preferences.gl_framerate = None
    except Exception:
        pass
"""


def dlss5_folder(game: Game) -> pathlib.Path | None:
    """Folder of a 64-bit Ren'Py game (where ReShade's dxgi.dll goes: next to the exe)."""
    exe = installdate._main_exe(game)
    if exe is None:
        return None
    folder = exe.parent
    lib = folder / "lib"
    if not ((folder / "renpy").is_dir() and (folder / "game").is_dir() and lib.is_dir()):
        return None
    if not any(d.is_dir() and "windows-x86_64" in d.name for d in lib.iterdir()):
        return None
    return folder


def dlss5_kit(folder: pathlib.Path) -> pathlib.Path | None:
    for parent in list(folder.parents)[:5]:
        kit = parent / DLSS5_KIT
        if (kit / "nvngx_dlssnr.dll").is_file() and (kit / "dxgi.dll").is_file():
            return kit
    return None


def dlss5_state(game: Game) -> str:
    """'installed' (by F95Checker), 'other' (ReShade / DLSS put there another way), '' (none), 'n/a' (not a 64-bit Ren'Py game)."""
    folder = dlss5_folder(game)
    if folder is None:
        return "n/a"
    if (folder / DLSS5_MANIFEST).is_file():
        return "installed"
    if any((folder / n).exists() for n in ("dxgi.dll", "dxgi.dll.off", "nvngx_dlssnr.dll", "game/dlss5_settings.rpy")):
        return "other"
    return ""


def dlss5_install(game: Game) -> str:
    """Hard links from the drive's kit (copies if it is on another drive). Returns '' or an error."""
    import json
    import shutil
    folder = dlss5_folder(game)
    if folder is None:
        return "not a 64-bit Ren'Py game (or executable not found)"
    kit = dlss5_kit(folder)
    if kit is None:
        return f"no {DLSS5_KIT} folder in the library folders above {folder}"
    clash = [n for n in ("dxgi.dll", "dxgi.dll.off", "d3d11.dll", "ReShade.ini", "ReShadePreset.ini", "nvngx_dlss.dll",
                         "nvngx_dlssnr.dll", "reshade-shaders", DLSS5_MANIFEST) if (folder / n).exists()]
    if clash:
        return "already present: " + ", ".join(clash)
    created, dirs = [], set()
    try:
        for rel in DLSS5_LINKED:
            dst = folder / rel
            parent = dst.parent
            while parent != folder and not parent.exists():
                dirs.add(parent.relative_to(folder).as_posix())
                parent = parent.parent
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(kit / rel, dst)
            except OSError:
                shutil.copy2(kit / rel, dst)
            created.append(rel)
        for rel, text in (("ReShade.ini", DLSS5_RESHADE_INI), ("ReShadePreset.ini", DLSS5_PRESET_INI), ("game/dlss5_settings.rpy", DLSS5_RPY)):
            with open(folder / rel, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(text)
            created.append(rel)
    except OSError as exc:
        for rel in created:
            with contextlib.suppress(OSError):
                (folder / rel).unlink()
        return str(exc)
    (folder / DLSS5_MANIFEST).write_text(json.dumps({
        "by": "F95Checker DLSS5 kit", "installed_at": int(time.time()), "kit": str(kit),
        "files": created + ["game/dlss5_settings.rpyc", "ReShade.log", "dlss5-feed.log", "dlss5-feed.cfg"],
        "dirs": sorted(dirs, key=len, reverse=True),
    }, indent=1), encoding="utf-8")
    _log(f"DLSS 5 kit installed in {folder}")
    return ""


def dlss5_remove(game: Game) -> str:
    """Removes exactly the files listed in _f95dlss5.json (the kit itself stays)."""
    import json
    folder = dlss5_folder(game)
    if folder is None or not (folder / DLSS5_MANIFEST).is_file():
        return "not installed by F95Checker"
    if game.launch_state is not LaunchState.Idle:
        return f"{game.name} is running"
    data = json.loads((folder / DLSS5_MANIFEST).read_text(encoding="utf-8"))
    errors = []
    for rel in data.get("files", []) + ["dxgi.dll.off"]:
        path = folder / rel
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
    for rel in data.get("dirs", []):
        with contextlib.suppress(OSError):
            (folder / rel).rmdir()           # only if empty
    if errors:
        return "; ".join(errors)
    (folder / DLSS5_MANIFEST).unlink()
    _log(f"DLSS 5 kit removed from {folder}")
    return ""


def _send_hotkey(combo: str):
    import ctypes
    keys = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in _VK:
            keys.append(_VK[part])
        elif len(part) == 1:
            keys.append(ord(part.upper()))
        elif re.fullmatch(r"f\d{1,2}", part):
            keys.append(0x6F + int(part[1:]))
        elif part == "home":
            keys.append(0x24)
    user32 = ctypes.windll.user32
    for vk in keys:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    for vk in reversed(keys):
        user32.keybd_event(vk, 0, 0x0002, 0)   # KEYEVENTF_KEYUP
        time.sleep(0.02)


async def before_launch(game: Game):
    cfg = prefs()["display"].get(str(game.id))
    if isinstance(cfg, dict) and cfg.get("reshade") is not None and reshade_dlls(game):
        error_text = set_reshade(game, bool(cfg["reshade"]))
        if error_text:
            _log(f"ReShade switch for {game.name}: {error_text}")


async def _lossless_session(game: Game):
    from modules import toy
    exe = find_lossless()
    if exe is None:
        utils.push_popup(msgbox.msgbox, "Lossless Scaling", "Lossless Scaling was not found (Steam). Set its path in "
                         "Library tools > Display profiles.", MsgBox.warn)
        return
    started = None
    if not toy._running("losslessscaling"):
        with contextlib.suppress(OSError):
            started = toy._start_program(exe)
            _log(f"Lossless Scaling started for {game.name}")
    delay = float(prefs()["lossless_delay"])
    front_since = 0.0
    sent = False
    deadline = time.monotonic() + 180
    seen_running = False
    while True:
        await asyncio.sleep(0.5)
        if game.launch_state in (LaunchState.Starting, LaunchState.Playing):
            seen_running = True
        elif seen_running or time.monotonic() > deadline:
            break
        if sent or game.launch_process is None:
            continue
        fg = toy._foreground_pid()
        pids = set()
        with contextlib.suppress(Exception):
            pids = toy._process_tree(game.launch_process.pid)
        if fg and fg in pids:
            front_since = front_since or time.monotonic()
            if time.monotonic() - front_since >= delay:
                _send_hotkey(prefs()["lossless_hotkey"])
                sent = True
                _log(f"Lossless Scaling hotkey sent to {game.name}")
        else:
            front_since = 0.0
    if started is not None:
        with contextlib.suppress(Exception):
            started.terminate()
            _log("Lossless Scaling closed with the game")


def on_game_launched(game: Game):
    cfg = prefs()["display"].get(str(game.id))
    if isinstance(cfg, dict) and cfg.get("lossless") and globals.os is Os.Windows:
        async_thread.run(_lossless_session(game))


def draw_context_menu(game: Game):
    import imgui
    if game is None:
        return
    if imgui.begin_menu(f"{icons.monitor_screenshot} Display profile"):
        cfg = display_cfg(game)
        changed, value = imgui.checkbox("Lossless Scaling with this game###disp_ls", cfg["lossless"])
        if changed:
            cfg["lossless"] = value
            installdate.save_prefs()
        state = reshade_enabled(game)
        if state is None:
            imgui.text_disabled("ReShade: not installed in this game")
            target = reshade_target(game)
            if target is not None and imgui.selectable("Copy the exe to choose in the ReShade installer", False)[0]:
                from modules import callbacks
                callbacks.clipboard_copy(str(target))
        else:
            if reshade_dlls(game) and dlss5_state(game) in ("installed", "other"):
                imgui.text_disabled("(ReShade here runs DLSS 5: on/off below switches DLSS 5)")
            labels = ["leave as it is", "on at launch", "off at launch"]
            index = {None: 0, True: 1, False: 2}[cfg["reshade"]]
            imgui.text(f"ReShade (now {'on' if state else 'off'}):")
            for i, label in enumerate(labels):
                if imgui.radio_button(f"{label}###disp_rs{i}", index == i):
                    cfg["reshade"] = {0: None, 1: True, 2: False}[i]
                    installdate.save_prefs()
                    if cfg["reshade"] is not None and game.launch_state is LaunchState.Idle:
                        set_reshade(game, cfg["reshade"])
        state = dlss5_state(game)
        if state == "installed":
            if imgui.selectable("Remove DLSS 5 from this game###disp_dlss5_rm", False)[0]:
                error_text = dlss5_remove(game)
                utils.push_popup(msgbox.msgbox, "DLSS 5", error_text or
                                 "DLSS 5 removed. Ren'Py keeps the DirectX renderer it was switched to:\n"
                                 "in the game, Shift+G > Renderer > Automatic to go back to the default.",
                                 MsgBox.warn if error_text else MsgBox.info)
        elif state == "":
            folder = dlss5_folder(game)
            if folder is not None and dlss5_kit(folder) is not None:
                if imgui.selectable("Install DLSS 5 (kit of this drive)###disp_dlss5_in", False)[0]:
                    error_text = dlss5_install(game)
                    utils.push_popup(msgbox.msgbox, "DLSS 5", error_text or "DLSS 5 installed: launch the game, Home key = ReShade.",
                                     MsgBox.warn if error_text else MsgBox.info)
        elif state == "other":
            imgui.text_disabled("DLSS 5 / ReShade installed by another tool")
        imgui.end_menu()
    if is_compressed(game):
        if imgui.selectable(f"{icons.package_variant} Uncompress game folder", False)[0]:
            compress_job.queue.append((game, True))
            async_thread.run(_run_compress_queue())
    elif globals.os is Os.Windows and game_folder(game) is not None:
        if imgui.selectable(f"{icons.package_variant_closed} Compress game folder", False)[0]:
            compress_job.queue.append((game, False))
            async_thread.run(_run_compress_queue())
            open_compression_popup()


@contextlib.contextmanager
def setup():
    """main.py: background loops (downloads watcher, play-time log)."""
    with contextlib.suppress(Exception):
        async_thread.run(_watch_loop())
    with contextlib.suppress(Exception):
        from modules import showcase
        async_thread.run(showcase.play_log_loop())
    yield
