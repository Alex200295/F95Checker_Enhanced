"""
Import Update: install a downloaded update archive (.zip / .7z / .rar) into a game's folder.

 1. You pick the archive (file picker, or drop it on the picker).
 2. F95Checker reads its list of files and finds which archive folder is the game folder:
    the folder that contains the game's exe, else the one containing "game/" (Ren'Py),
    "<exe>_Data/" (Unity), "www/" (RPG Maker), else the single top folder is skipped.
 3. Preview: files replaced / added, size, version read from the archive name, free space,
    and a warning when the archive does not look like this game.
 4. Extraction into a temporary folder ON THE SAME DRIVE, then the files are moved over the
    game's files. If extraction fails (damaged archive, disk full), the game is untouched.
    Locked files (game running) are detected before anything is replaced.
 5. Save folders inside the game are copied to <F95Checker data>/saves_backup first.
 6. Optional: the game folder is renamed to the archive's folder name (LessonInLoyalty-0.7.0-pc ->
    LessonInLoyalty-0.8.2-pc), the executable path follows.
 7. Installed is ticked (when the archive version is the latest or unreadable), the install date
    is today, and the game is rescanned (exe, size, engine).

Files present in the game but not in the archive are kept (like extracting over it by hand).
"""
import asyncio
import contextlib
import dataclasses
import os
import pathlib
import posixpath
import shutil
import stat
import time

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
    installdate,
    msgbox,
    utils,
)

LOG_PREFIX = "[ImportUpdate]"
ARCHIVE_EXTS = (".zip", ".7z", ".rar")


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


# ---------------------------------------------------------------------------
# Archive listing / extraction (zip, 7z, rar)
# ---------------------------------------------------------------------------

def _sevenzip_exe() -> str | None:
    for candidate in (shutil.which("7z"), r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    return None


def _rarfile():
    """rarfile needs an external tool for compressed RARs: UnRAR (WinRAR's folder) or 7-Zip, found even off PATH."""
    import rarfile
    for unrar in (r"C:\Program Files\WinRAR\UnRAR.exe", r"C:\Program Files (x86)\WinRAR\UnRAR.exe"):
        if pathlib.Path(unrar).is_file():
            rarfile.UNRAR_TOOL = unrar
            break
    if seven := _sevenzip_exe():
        rarfile.SEVENZIP_TOOL = seven
    try:
        rarfile.tool_setup(force=True)
    except rarfile.RarCannotExec:
        raise RuntimeError("RAR archives need 7-Zip (7-zip.org) or WinRAR installed.")
    return rarfile


def _clean(name: str) -> str | None:
    """Archive member name -> safe relative posix path, None for directories / unsafe paths (../, C:\\...)."""
    name = name.replace("\\", "/")
    if not name or name.endswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        return None
    return "/".join(parts)


def list_archive(path: pathlib.Path) -> dict[str, int]:
    """{relative path: uncompressed size} of the files in the archive."""
    suffix = path.suffix.lower()
    files: dict[str, int] = {}
    if suffix == ".zip":
        import zipfile
        try:
            import zipfile_deflate64  # noqa: F401  (registers Deflate64, used by Windows' own zips > 2 GB)
        except Exception:
            pass
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not info.is_dir() and (name := _clean(info.filename)):
                    files[name] = info.file_size
    elif suffix == ".7z":
        import py7zr
        with py7zr.SevenZipFile(path, "r") as z:
            for info in z.list():
                if not info.is_directory and (name := _clean(info.filename)):
                    files[name] = int(info.uncompressed or 0)
    elif suffix == ".rar":
        rarfile = _rarfile()
        with rarfile.RarFile(path) as z:
            for info in z.infolist():
                if not info.is_dir() and (name := _clean(info.filename)):
                    files[name] = int(info.file_size or 0)
    else:
        raise ValueError(f"Unsupported archive type {suffix} (zip, 7z, rar)")
    return files


def extract_archive(path: pathlib.Path, dest: pathlib.Path, progress: "Job"):
    """Extracts everything into dest (an empty staging folder). Member names are sanitised."""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        import zipfile
        try:
            import zipfile_deflate64  # noqa: F401
        except Exception:
            pass
        with zipfile.ZipFile(path) as z:
            members = [info for info in z.infolist() if not info.is_dir() and _clean(info.filename)]
            progress.total = len(members)
            for info in members:
                target = dest / _clean(info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                progress.done += 1
    elif suffix == ".7z":
        import py7zr
        with py7zr.SevenZipFile(path, "r") as z:
            progress.total = 1
            z.extractall(path=dest)        # py7zr refuses absolute / parent paths itself
            progress.done = 1
    elif suffix == ".rar":
        rarfile = _rarfile()
        with rarfile.RarFile(path) as z:
            members = [info for info in z.infolist() if not info.is_dir() and _clean(info.filename)]
            progress.total = len(members)
            for info in members:
                target = dest / _clean(info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                progress.done += 1


# ---------------------------------------------------------------------------
# Plan: which archive folder is the game folder
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Plan:
    game: Game
    archive: pathlib.Path
    exe: pathlib.Path
    dest: pathlib.Path                  # game folder (folder of the exe)
    prefix: str                         # archive folder mapped onto dest ("" = archive root), ends with "/"
    files: dict[str, int]               # archive name -> size, only those under prefix
    replaced: int = 0
    added: int = 0
    size: int = 0
    how: str = ""
    new_name: str = ""                  # archive folder name (version) for the rename
    new_version: str = ""
    looks_wrong: str = ""
    free: int = 0
    rename: bool = False
    rename_dirs: list = dataclasses.field(default_factory=list)


def _common_root(names: list[str]) -> str:
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        return next(iter(tops)) + "/"
    return ""


def find_prefix(names: list[str], exe: pathlib.Path) -> tuple[str, str]:
    """(prefix, explanation)."""
    lower = {n.lower(): n for n in names}
    # 1. the game's exe
    hits = [n for low, n in lower.items() if posixpath.basename(low) == exe.name.lower()]
    if hits:
        best = min(hits, key=lambda n: n.count("/"))
        return posixpath.dirname(best) + "/" if "/" in best else "", f"contains {exe.name}"
    # 2. engine folders that exist in the game folder
    markers = [m for m in ("game/", f"{exe.stem.lower()}_data/", "www/", "renpy/", "lib/", "data/")
               if (exe.parent / m.rstrip("/")).is_dir()]
    for marker in markers:
        found = [low[:low.index(marker)] for low in lower if low.startswith(marker) or f"/{marker}" in low]
        if found:
            prefix = min(found, key=len)
            original = next(n for low, n in lower.items() if low.startswith(prefix))[:len(prefix)]
            return original, f"contains the {marker.rstrip('/')} folder"
    # 3. single top folder
    root = _common_root(names)
    if root and not (exe.parent / root.rstrip("/")).is_dir():
        return root, f"single folder {root.rstrip('/')}"
    return "", "archive root"


def make_plan(game: Game, archive: pathlib.Path) -> Plan:
    exe = installdate._main_exe(game)
    if exe is None:
        raise ValueError(f"The executable of {game.name} is not set or not found: Add Exe first (or run a local scan).")
    names_sizes = list_archive(archive)
    if not names_sizes:
        raise ValueError("The archive is empty (or only has folders).")
    prefix, how = find_prefix(list(names_sizes), exe)
    files = {n: s for n, s in names_sizes.items() if n.startswith(prefix)}
    plan = Plan(game, archive, exe, exe.parent, prefix, files, how=how)
    for name, size in files.items():
        plan.size += size
        if (exe.parent / name[len(prefix):]).exists():
            plan.replaced += 1
        else:
            plan.added += 1
    # sanity: an update shares a good part of its files with the installed copy
    if files and plan.replaced < max(1, len(files) // 10) and "contains" not in how:
        plan.looks_wrong = (f"Only {plan.replaced} of the {len(files)} files of the archive already exist in the game folder: "
                            "is it really an update of this game?")
    # version / rename
    folder_name = prefix.rstrip("/").rsplit("/", 1)[-1] if prefix else archive.stem
    plan.new_name = folder_name
    plan.new_version = installdate.version_label(folder_name) if installdate.version_tuple(folder_name) else ""
    old_name = exe.parent.name
    if (prefix and folder_name != old_name and installdate.version_tuple(folder_name) and installdate.version_tuple(old_name)
            and installdate.folder_key(folder_name) == installdate.folder_key(old_name)):
        # rename every folder between the library root and the exe named like the old version folder (nested extracts)
        dirs = [d for d in [exe.parent, *exe.parent.parents] if d.name == old_name]
        if all(not (d.parent / folder_name).exists() for d in dirs):
            plan.rename_dirs = dirs                 # deepest first
            plan.rename = True
    plan.free = installdate.free_space(exe.parent)
    return plan


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Job:
    phase: str = "Reading the archive..."
    done: int = 0
    total: int = 0
    finished: bool = False
    result: str = ""
    failed: bool = False


def _locked(path: pathlib.Path) -> bool:
    """True when another program (the game) holds the file. Read-only files are made writable."""
    if not os.access(path, os.W_OK):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            return True
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _backup_saves(plan: Plan, label: str = "before import") -> pathlib.Path | None:
    save_dirs = []
    for current, dirs, _ in os.walk(plan.dest):
        depth = len(pathlib.Path(current).relative_to(plan.dest).parts)
        for d in list(dirs):
            if d.lower() in installdate.PLAY_DIRS:
                save_dirs.append(pathlib.Path(current) / d)
                dirs.remove(d)
        if depth >= 3:
            dirs[:] = []
    save_dirs = [d for d in save_dirs if any(d.iterdir())]
    if not save_dirs:
        return None
    target = globals.data_path / "saves_backup" / f"{utils.clean_str(plan.game.name)[:40]} - {label} - {time.strftime('%Y%m%d-%H%M%S')}"
    n = 2
    while target.exists():                  # two in the same second
        target = target.with_name(f"{target.name.split(' (')[0]} ({n})")
        n += 1
    for d in save_dirs:
        shutil.copytree(d, target / d.relative_to(plan.dest))
    return target


ENGINE_DIRS = ("renpy", "lib")


def _set_aside_old_engine(dest: pathlib.Path, installed: list[pathlib.Path]) -> int:
    """The archive brought a whole Ren'Py engine: engine files it does not have are the previous engine's.
    Left there they can stop the game from starting (e.g. 00layeredimage.rpy next to 00layeredimage_ren.py),
    so they are moved to <game>/_f95checker_old_engine/ (not deleted). Returns how many."""
    new = {os.path.normcase(str(p)) for p in installed}
    engine_files = [p for p in installed if p.relative_to(dest).parts[:1] == ("renpy",)]
    if len(engine_files) < 20 or not (dest / "renpy" / "__init__.py").is_file():
        return 0                            # a patch, not a whole engine
    moved = 0
    for sub in ENGINE_DIRS:
        for current, _, names in os.walk(dest / sub):
            for name in names:
                path = pathlib.Path(current) / name
                if os.path.normcase(str(path)) in new:
                    continue
                target = dest / installdate.OLD_ENGINE_DIR / path.relative_to(dest)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
                    moved += 1
                except OSError as exc:
                    _log(f"Old engine file not moved {path}: {exc}")
    return moved


def _install(plan: Plan, job: Job) -> str:
    """Blocking part (worker thread). Returns the summary text."""
    staging = plan.dest.parent / f".f95checker-import-{plan.game.id}-{int(time.time())}"
    staging.mkdir()
    try:
        job.phase = "Extracting (the game is not touched yet)..."
        extract_archive(plan.archive, staging, job)
        source = staging / plan.prefix if plan.prefix else staging
        moves = []
        for current, _, names in os.walk(source):
            for name in names:
                src = pathlib.Path(current) / name
                moves.append((src, plan.dest / src.relative_to(source)))
        job.phase = "Checking the game files are not in use..."
        locked = [dst for _, dst in moves if dst.is_file() and _locked(dst)]
        if locked:
            raise PermissionError(f"{len(locked)} file(s) are in use (is the game running?), e.g. {locked[0]}. Nothing was changed.")
        job.phase = "Backing up saves..."
        backup = _backup_saves(plan)
        job.phase = "Installing the files..."
        job.done, job.total = 0, len(moves)
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                raise IsADirectoryError(f"{dst} is a folder in the game but a file in the archive")
            os.replace(src, dst)            # same drive: instant, overwrites
            job.done += 1
        old_engine = _set_aside_old_engine(plan.dest, [dst for _, dst in moves])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    lines = [f"{len(moves)} files installed into {plan.dest} ({plan.replaced} replaced, {plan.added} new)."]
    if old_engine:
        lines.append(f"{old_engine} file(s) of the previous Ren'Py engine set aside in {installdate.OLD_ENGINE_DIR} "
                     "(an old engine file left next to the new one stops the game from starting).")
    if backup:
        lines.append(f"Saves backed up to {backup}.")
    return "\n".join(lines)


def _rename(plan: Plan) -> tuple[pathlib.Path, str]:
    """Renames the version folder(s). Returns (new exe path, message)."""
    exe = plan.exe
    if not plan.rename or not plan.rename_dirs:
        return exe, ""
    done = []
    try:
        for d in plan.rename_dirs:              # deepest first: the path of the outer folder is still valid
            d.rename(d.with_name(plan.new_name))
            done.append(d.name)
    except OSError as exc:
        msg = f"Folder rename stopped ({exc}): {len(done)}/{len(plan.rename_dirs)} renamed."
    else:
        msg = f"Folder renamed to {plan.new_name}."
    parts = list(exe.parts)
    old = plan.exe.parent.name
    # rebuild the exe path: every renamed component took the new name
    renamed = len(done)
    for i in range(len(parts) - 2, -1, -1):
        if renamed and parts[i] == old:
            parts[i] = plan.new_name
            renamed -= 1
    return pathlib.Path(*parts), msg


async def run_import(plan: Plan, job: Job):
    game = plan.game
    loop = asyncio.get_running_loop()
    try:
        summary = await loop.run_in_executor(None, _install, plan, job)
        job.phase = "Finishing..."
        new_exe, rename_msg = await loop.run_in_executor(None, _rename, plan)
        if rename_msg:
            summary += "\n" + rename_msg
        if new_exe != plan.exe and new_exe.is_file():
            installdate._set_executable(game, new_exe, installdate._exe_dir())
        else:
            new_exe = plan.exe
        latest = installdate.version_tuple(game.version)
        imported = installdate.version_tuple(plan.new_version) if plan.new_version else ()
        info = installdate.local_info().setdefault(game.id, {})
        if not imported or not latest or imported >= latest:
            game.installed = game.version
            game.updated = False
            info["imported_version"] = game.version
            summary += f"\nInstalled ticked ({game.version})."
        else:
            info["imported_version"] = plan.new_version
            summary += f"\nThe archive is version {plan.new_version}, older than {game.version}: Installed not ticked."
        # the scan reads versions from folder names: remember what this folder really holds now
        info["imported_folder"] = str(new_exe.parent)
        info["import_dir"] = str(plan.archive.parent)
        # Library tools > Clean up: archives already installed can be deleted
        imported = [a for a in info.get("imported_archives", []) if a != str(plan.archive)] + [str(plan.archive)]
        info["imported_archives"] = imported[-20:]
        installdate.save_local_info()
        await installdate._record_install_date(game, int(time.time()), plan.new_name or plan.dest.name, replace_newer=False)
        _log(f"'{game.name}': {summary!r}")
        job.result = summary + "\n\nYou can now delete the archive. Back up note: files of the old version that the update "\
                               "no longer has were kept (as when extracting over it by hand)."
        try:
            await installdate.scan_library(game, manual=False)
        except Exception:
            _log(error.traceback())
        try:
            from modules import pctools
            if pctools.is_compressed(game):
                job.phase = "Re-compressing the new files..."
                summary_extra = await pctools.compress_game(game, quiet=True)
                job.result += f"\n\n{summary_extra}"
        except Exception:
            _log(error.traceback())
    except Exception:
        job.failed = True
        job.result = f"Import failed:\n{error.text()}"
        _log(error.traceback())
    finally:
        job.finished = True


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _start_dir(game: Game) -> pathlib.Path:
    remembered = installdate.local_info().get(game.id, {}).get("import_dir") or next(
        (v.get("import_dir") for v in installdate.local_info().values() if v.get("import_dir")), "")
    for candidate in (remembered, pathlib.Path.home() / "Downloads", installdate.jdownloader_target()):
        if candidate and pathlib.Path(candidate).is_dir():
            return pathlib.Path(candidate)
    return pathlib.Path.home()


def _can_import(game: Game) -> bool:
    if game.launch_state is not LaunchState.Idle:
        utils.push_popup(msgbox.msgbox, "Import update", f"{game.name} is running: close it first.", MsgBox.warn)
        return False
    if installdate._main_exe(game) is None:
        utils.push_popup(msgbox.msgbox, "Import update", f"The executable of {game.name} is not set or not found.\n"
                         "Add Exe (or Local scan) first, so F95Checker knows where the game is.", MsgBox.warn)
        return False
    return True


def import_file(game: Game, path: pathlib.Path):
    """Import a known archive (downloads watcher): straight to the preview."""
    if _can_import(game):
        async_thread.run(_prepare(game, pathlib.Path(path)))


def import_update(game: Game):
    """Entry point (button / right click): pick the archive."""
    from external import filepicker
    if not _can_import(game):
        return

    def picked(selected):
        if not selected:
            return
        path = pathlib.Path(selected)
        if path.suffix.lower() not in ARCHIVE_EXTS:
            utils.push_popup(msgbox.msgbox, "Import update", f"{path.name}: choose a .zip, .7z or .rar archive.", MsgBox.warn)
            return
        async_thread.run(_prepare(game, path))

    picker = filepicker.FilePicker(
        title=f"Select or drop the update archive (.zip / .7z / .rar) for {game.name}",
        start_dir=_start_dir(game),
        callback=picked,
    )
    utils.push_popup(picker.tick)


async def _prepare(game: Game, path: pathlib.Path):
    try:
        plan = await asyncio.get_running_loop().run_in_executor(None, make_plan, game, path)
    except Exception:
        utils.push_popup(msgbox.msgbox, "Import update", f"Cannot read {path.name}:\n{error.text()}", MsgBox.error, more=error.traceback())
        return
    _open_preview(plan)


def _open_preview(plan: Plan):
    import imgui
    confirm_wrong = [False]

    def content():
        imgui.text(f"{plan.game.name}: {plan.archive.name}")
        imgui.text(f"Game folder: {plan.dest}")
        imgui.text_disabled(f"Archive folder used: {plan.prefix or '(root)'}   ({plan.how})")
        imgui.text(f"{len(plan.files)} files, {installdate.size_display(plan.size)}: {plan.replaced} replaced, {plan.added} new.")
        imgui.text(f"Version in the archive name: {plan.new_version or 'unknown'}   -   latest on F95zone: {plan.game.version}")
        if plan.free and plan.free < plan.size * 1.05:
            imgui.text_colored(f"Not enough space: {installdate.size_display(plan.free)} free on {plan.dest.anchor}.", 0.95, 0.3, 0.2)
        if plan.rename_dirs:
            _, plan.rename = imgui.checkbox(f"Rename the folder {plan.exe.parent.name}  ->  {plan.new_name}"
                                            + (f" ({len(plan.rename_dirs)} nested folders)" if len(plan.rename_dirs) > 1 else ""), plan.rename)
        if plan.looks_wrong:
            imgui.text_colored(plan.looks_wrong, 0.95, 0.69, 0.10)
            _, confirm_wrong[0] = imgui.checkbox("I'm sure, install it anyway", confirm_wrong[0])
        imgui.text_disabled("Saves inside the game folder are backed up first. Extraction goes to a temporary folder:\n"
                            "if it fails, the game is not touched. Files the archive doesn't have are kept.")

    def start():
        if plan.looks_wrong and not confirm_wrong[0]:
            utils.push_popup(msgbox.msgbox, "Import update", "Tick \"I'm sure\" first (or choose another archive).", MsgBox.warn)
            return
        if plan.free and plan.free < plan.size * 1.05:
            utils.push_popup(msgbox.msgbox, "Import update", "Not enough free space on the game's drive.", MsgBox.error)
            return
        job = Job()
        async_thread.run(run_import(plan, job))
        _open_progress(plan, job)

    utils.push_popup(utils.popup, "Import update", content,
                     buttons={f"{icons.archive_arrow_down_outline} Install": start, f"{icons.cancel} Cancel": None},
                     closable=True, outside=False)


def _open_progress(plan: Plan, job: Job):
    import imgui

    def content():
        if job.finished:
            utils.push_popup(msgbox.msgbox, "Import update", f"{plan.game.name}\n\n{job.result}",
                             MsgBox.error if job.failed else MsgBox.info)
            return True
        imgui.text(f"{plan.game.name}: {job.phase}")
        if job.total:
            imgui.text(f"{job.done} / {job.total}")
        imgui.text_disabled("Don't start the game until it's finished.")

    utils.push_popup(utils.popup, "Importing update", content, buttons=None, closable=False, outside=False)


# ---------------------------------------------------------------------------
# Install a mod (right click > Mods & Extras, or the Install button of the mods list)
# ---------------------------------------------------------------------------
#  - From the downloaded archive or from the folder you extracted (Downloads...): the files are copied,
#    the extracted folder is left as it is.
#  - Where they go: like the archive says when it holds a "game" folder (or the game's exe); else, for a
#    Ren'Py game, into <game>\game (a wrapper folder named like the release, "WT-Mod-1.2", is skipped).
#  - Saves are backed up first; game files the mod replaces are moved to <game>\_f95checker_mod_backup\<id>
#    and put back by Uninstall, which also deletes the mod's files (and the .rpyc Ren'Py compiled from them).
#  - Nothing else changes: version, Installed, install date (mod files are ignored by the local scan), folder name.

MOD_BACKUP_DIR = "_f95checker_mod_backup"
SCRIPT_EXTS = (".rpy", ".rpyc", ".rpa", ".py")


@dataclasses.dataclass(slots=True)
class ModPlan:
    game: Game
    source: pathlib.Path                # archive, or extracted folder
    exe: pathlib.Path
    root: pathlib.Path                  # game folder (folder of the exe): paths in the manifest are relative to it
    target: pathlib.Path                # where the source folder <prefix> goes
    prefix: str                         # source folder used ("" = everything), ends with "/"
    files: dict[str, int]               # source names (all of them) -> size
    how: str = ""
    wrapper: str = ""                   # single top folder that may be skipped (Ren'Py, no game/ folder)
    skip_wrapper: bool = False
    name: str = ""
    free: int = 0

    def used(self) -> dict[str, int]:
        prefix = self.prefix + (self.wrapper if self.skip_wrapper else "")
        return {n[len(prefix):]: s for n, s in self.files.items() if n.startswith(prefix) and n[len(prefix):]}

    def counts(self) -> tuple[int, int, int]:
        used = self.used()
        replaced = sum(1 for n in used if (self.target / n).exists())
        return replaced, len(used) - replaced, sum(used.values())


def list_folder(path: pathlib.Path) -> dict[str, int]:
    files = {}
    for current, _, names in os.walk(path):
        for name in names:
            full = pathlib.Path(current) / name
            with contextlib.suppress(OSError):
                files[full.relative_to(path).as_posix()] = full.stat().st_size
    return files


def make_mod_plan(game: Game, source: pathlib.Path) -> ModPlan:
    exe = installdate._main_exe(game)
    if exe is None:
        raise ValueError(f"The executable of {game.name} is not set or not found: Add Exe first (or run a local scan).")
    files = list_folder(source) if source.is_dir() else list_archive(source)
    if not files:
        raise ValueError("Nothing to install: no file in it.")
    root = exe.parent
    renpy = (root / "renpy").is_dir() and (root / "game").is_dir()
    name = source.name if source.is_dir() else source.stem
    plan = ModPlan(game, source, exe, root, root, "", files, name=name, free=installdate.free_space(root))
    prefix, how = find_prefix(list(files), exe)
    if how.startswith("contains"):                       # the mod has the game's layout (game/, the exe...)
        plan.prefix, plan.how = prefix, how
    elif renpy:
        plan.target, plan.how = root / "game", "Ren'Py mod without a game folder: goes into game"
        top = _common_root(list(files))
        if top and not (root / "game" / top.rstrip("/")).is_dir():
            plan.wrapper = top
            name_top = top.rstrip("/")
            # a release folder ("WT-Mod-v1.2", "BeingADIK-0.11.1-mod") is a wrapper; a plain name may be meant inside game
            plan.skip_wrapper = bool(installdate.version_tuple(name_top)) or \
                installdate.folder_key(name_top) == installdate.folder_key(root.name)
    else:
        plan.prefix, plan.how = prefix, how
    return plan


def _install_mod(plan: ModPlan, job: Job) -> str:
    """Blocking part (worker thread). Returns the summary text."""
    staging = None
    try:
        if plan.source.is_dir():
            base = plan.source
        else:
            staging = plan.root.parent / f".f95checker-mod-{plan.game.id}-{int(time.time())}"
            staging.mkdir()
            job.phase = "Extracting (the game is not touched yet)..."
            extract_archive(plan.source, staging, job)
            base = staging
        prefix = plan.prefix + (plan.wrapper if plan.skip_wrapper else "")
        source = base / prefix if prefix else base
        pairs = []
        for current, _, names in os.walk(source):
            for name in names:
                src = pathlib.Path(current) / name
                pairs.append((src, plan.target / src.relative_to(source)))
        if not pairs:
            raise ValueError("Nothing to install.")
        job.phase = "Checking the game files are not in use..."
        locked = [dst for _, dst in pairs if dst.is_file() and _locked(dst)]
        if locked:
            raise PermissionError(f"{len(locked)} file(s) are in use (is the game running?), e.g. {locked[0]}. Nothing was changed.")
        job.phase = "Backing up saves..."
        import types
        saves = _backup_saves(types.SimpleNamespace(dest=plan.root, game=plan.game), "before mod")
        mod_id = int(time.time() * 1000)
        backup = plan.root / MOD_BACKUP_DIR / str(mod_id)
        wanted = {os.path.normcase(str(dst)) for _, dst in pairs}
        to_set_aside = [dst for _, dst in pairs if dst.is_file()]
        # Ren'Py compiles the mod's x.rpy over the game's own x.rpyc: keep the original for Uninstall
        for _, dst in pairs:
            if dst.suffix.lower() == ".rpy":
                compiled = dst.with_suffix(".rpyc")
                if compiled.is_file() and os.path.normcase(str(compiled)) not in wanted:
                    to_set_aside.append(compiled)
        job.phase = "Installing the files..."
        job.done, job.total = 0, len(pairs)
        replaced = []
        for dst in to_set_aside:
            rel = dst.relative_to(plan.root)
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(dst, backup / rel)
            replaced.append(rel.as_posix())
        for src, dst in pairs:
            if dst.is_dir():
                raise IsADirectoryError(f"{dst} is a folder in the game but a file in the mod")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if staging is not None:
                os.replace(src, dst)        # extracted on the same drive: instant
            else:
                shutil.copy2(src, dst)      # your extracted folder stays as it is
            job.done += 1
        info = installdate.local_info().setdefault(plan.game.id, {})
        info.setdefault("mods_installed", []).append({
            "id": mod_id, "name": plan.name, "source": str(plan.source), "date": int(time.time()),
            "files": [dst.relative_to(plan.root).as_posix() for _, dst in pairs], "replaced": replaced,
        })
        installdate.save_local_info()
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    lines = [f"{len(pairs)} file(s) installed into {plan.target}"
             + (f", {len(replaced)} game file(s) replaced (kept for Uninstall)." if replaced else ".")]
    if saves:
        lines.append(f"Saves backed up to {saves}.")
    lines.append("Launch the game: if it shows an error at start, the mod does not fit this version: right click > "
                 "Mods & Extras > Installed mods > Uninstall.")
    return "\n".join(lines)


def uninstall_mod(game: Game, mod_id: int) -> str:
    """Deletes the mod's files (and their compiled .rpyc), puts back the game files it replaced. '' or an error."""
    exe = installdate._main_exe(game)
    info = installdate.local_info().get(game.id, {})
    mods = info.get("mods_installed") or []
    mod = next((m for m in mods if m.get("id") == mod_id), None)
    if exe is None or mod is None:
        return "mod or game folder not found"
    root = exe.parent
    if game.launch_state is not LaunchState.Idle:
        return f"{game.name} is running: close it first"
    later = [m for m in mods if m.get("id", 0) > mod_id and set(m.get("files", [])) & set(mod.get("files", []))]
    if later:
        return f"\"{later[0].get('name')}\" was installed after it over some of the same files: uninstall it first"
    errors = []
    dirs = set()
    restored = {r.lower() for r in mod.get("replaced", [])}
    for rel in mod.get("files", []):
        targets = [rel] + ([rel + "c"] if rel.lower().endswith(".rpy") and (rel + "c").lower() not in restored else [])
        for item in targets:
            path = root / item
            try:
                if path.is_file():
                    path.unlink()
                dirs.add(path.parent)
            except OSError as exc:
                errors.append(f"{item}: {exc}")
    backup = root / MOD_BACKUP_DIR / str(mod_id)
    for rel in mod.get("replaced", []):
        try:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup / rel, root / rel)
        except OSError as exc:
            errors.append(f"restore {rel}: {exc}")
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):      # folders the mod created, if now empty
        with contextlib.suppress(OSError):
            while d != root and d != root / "game" and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
    if not errors:
        shutil.rmtree(backup, ignore_errors=True)
        with contextlib.suppress(OSError):
            (root / MOD_BACKUP_DIR).rmdir()
        info["mods_installed"] = [m for m in mods if m.get("id") != mod_id]
        installdate.save_local_info()
        _log(f"'{game.name}': mod {mod.get('name')} uninstalled")
        return ""
    return "\n".join(errors[:10])


def installed_mods(game: Game) -> list[dict]:
    return list(installdate.local_info().get(game.id, {}).get("mods_installed") or [])


def install_mod(game: Game):
    """Entry point: archive or extracted folder."""
    import imgui
    if game.launch_state is not LaunchState.Idle:
        utils.push_popup(msgbox.msgbox, "Install mod", f"{game.name} is running: close it first.", MsgBox.warn)
        return
    if installdate._main_exe(game) is None:
        utils.push_popup(msgbox.msgbox, "Install mod", f"The executable of {game.name} is not set or not found.\n"
                         "Add Exe (or Local scan) first, so F95Checker knows where the game is.", MsgBox.warn)
        return

    def pick(folder: bool):
        from external import filepicker

        def picked(selected):
            if not selected:
                return
            path = pathlib.Path(selected)
            if not folder and path.suffix.lower() not in ARCHIVE_EXTS:
                utils.push_popup(msgbox.msgbox, "Install mod", f"{path.name}: choose a .zip, .7z or .rar archive.", MsgBox.warn)
                return
            async_thread.run(_prepare_mod(game, path))

        picker = filepicker.FilePicker(
            title=(f"Select the extracted mod folder for {game.name}" if folder else
                   f"Select or drop the mod archive (.zip / .7z / .rar) for {game.name}"),
            start_dir=pathlib.Path.home() / "Downloads" if (pathlib.Path.home() / "Downloads").is_dir() else _start_dir(game),
            dir_picker=folder,
            callback=picked,
        )
        utils.push_popup(picker.tick)

    def content():
        imgui.text(f"Install a mod into {game.name}: from the downloaded archive, or from the folder you extracted.")

    utils.push_popup(utils.popup, "Install mod", content,
                     buttons={f"{icons.archive_outline} Archive (.zip / .7z / .rar)": lambda: pick(False),
                              f"{icons.folder_outline} Extracted folder": lambda: pick(True),
                              f"{icons.cancel} Cancel": None},
                     closable=True, outside=False)


async def _prepare_mod(game: Game, path: pathlib.Path):
    try:
        plan = await asyncio.get_running_loop().run_in_executor(None, make_mod_plan, game, path)
    except Exception:
        utils.push_popup(msgbox.msgbox, "Install mod", f"Cannot read {path.name}:\n{error.text()}", MsgBox.error, more=error.traceback())
        return
    _open_mod_preview(plan)


def _open_mod_preview(plan: ModPlan):
    import imgui

    def content():
        replaced, added, size = plan.counts()
        imgui.text(f"{plan.game.name}: {plan.name}")
        imgui.text(f"Files go into: {plan.target}")
        imgui.text_disabled(f"({plan.how}{', mod folder used: ' + plan.prefix if plan.prefix else ''})")
        if plan.wrapper:
            _, plan.skip_wrapper = imgui.checkbox(
                f"Put the content of \"{plan.wrapper.rstrip('/')}\" directly in game (unticked: the folder itself goes into game)",
                plan.skip_wrapper)
        imgui.text(f"{added + replaced} files, {installdate.size_display(size)}: {added} new, {replaced} replacing game files "
                   "(kept aside, put back by Uninstall).")
        if plan.free and plan.free < size * 1.05:
            imgui.text_colored(f"Not enough space: {installdate.size_display(plan.free)} free on {plan.root.anchor}.", 0.95, 0.3, 0.2)
        names = sorted(plan.used())
        imgui.text_disabled("\n".join(names[:12]) + (f"\n... and {len(names) - 12} more" if len(names) > 12 else ""))
        imgui.text_disabled("Check the mod is made for your version of the game. Saves are backed up first.")

    def start():
        _, _, size = plan.counts()
        if plan.free and plan.free < size * 1.05:
            utils.push_popup(msgbox.msgbox, "Install mod", "Not enough free space on the game's drive.", MsgBox.error)
            return
        job = Job()

        async def run():
            try:
                job.result = await asyncio.get_running_loop().run_in_executor(None, _install_mod, plan, job)
                _log(f"'{plan.game.name}': mod {plan.name}: {job.result!r}")
            except Exception:
                job.failed = True
                job.result = f"Install failed (the game was not changed):\n{error.text()}"
                _log(error.traceback())
            finally:
                job.finished = True

        async_thread.run(run())

        def progress():
            if job.finished:
                utils.push_popup(msgbox.msgbox, "Install mod", f"{plan.game.name} - {plan.name}\n\n{job.result}",
                                 MsgBox.error if job.failed else MsgBox.info)
                return True
            imgui.text(f"{plan.game.name}: {job.phase}")
            if job.total:
                imgui.text(f"{job.done} / {job.total}")

        utils.push_popup(utils.popup, "Installing mod", progress, buttons=None, closable=False, outside=False)

    utils.push_popup(utils.popup, "Install mod", content,
                     buttons={f"{icons.archive_arrow_down_outline} Install": start, f"{icons.cancel} Cancel": None},
                     closable=True, outside=False)


def open_installed_mods_popup(game: Game):
    import imgui
    state = {"msg": ""}

    def content():
        mods = installed_mods(game)
        if not mods:
            imgui.text("No mod installed with F95Checker in this game.")
            return
        imgui.text_disabled("Uninstall deletes the mod's files and puts back the game files it replaced.")
        for mod in sorted(mods, key=lambda m: m.get("id", 0), reverse=True):
            imgui.separator()
            imgui.text(f"{mod.get('name')}   ({time.strftime('%Y-%m-%d', time.localtime(mod.get('date', 0)))}, "
                       f"{len(mod.get('files', []))} files, {len(mod.get('replaced', []))} replaced)")
            imgui.same_line()
            if imgui.small_button(f"Uninstall###mod_un_{mod.get('id')}"):
                err = uninstall_mod(game, mod.get("id"))
                state["msg"] = f"Uninstalled: {mod.get('name')}" if not err else f"Not uninstalled: {err}"
        if state["msg"]:
            imgui.text_disabled(state["msg"])

    utils.push_popup(utils.popup, f"Installed mods - {game.name}", content, buttons=True, closable=True, outside=False)
