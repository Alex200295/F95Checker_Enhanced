"""
Extras: "What to play tonight?", discretion key, clean up.

What to play tonight? (button above the grid, Library tools)
    Draws one of your installed games you have not finished. Games are weighted by how much you like them
    (rating, play time, your favourite tags), updated since your last play, and how long ago you played them.
    Filters: what kind of session (continue / something new / updated) and a mood (one of your favourite tags).
Discretion key (Library tools, default Ctrl+Alt+H, anywhere in Windows)
    First press: emergency stop of the toy, running games and F95Checker minimised, sound muted.
    Second press: the games come back and the sound too (the toy stays stopped until you press Resume).
Clean up (Library tools)
    Archives in your download folders already installed (imported with Import Update, or an older or same
    version as the one installed), Ren'Py caches (rebuilt by the game) and leftovers of interrupted imports.
    Nothing is deleted before you tick and confirm; archives go to the recycle bin.
"""
import contextlib
import dataclasses
import os
import pathlib
import random
import shutil
import threading
import time

from common.structs import (
    Game,
    LaunchState,
    MsgBox,
    Os,
)
from external import (
    async_thread,
)
from modules import (
    globals,
    icons,
    installdate,
    msgbox,
    utils,
)

LOG_PREFIX = "[Extras]"


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("discreet_key", "ctrl+alt+h")
    p.setdefault("discreet_mute", True)
    p.setdefault("discreet_hide_f95", True)
    p.setdefault("safe_key", "ctrl+alt+b")
    return p


# ---------------------------------------------------------------------------
# What to play tonight?
# ---------------------------------------------------------------------------

KINDS = ["Anything", "Continue a game I started", "Something I never played", "Updated since I last played"]


def _is_finished(game: Game) -> bool:
    return bool(game.finished) and game.finished == (game.installed or game.version)


def candidates(kind: int = 0, mood: str = "") -> list[tuple[Game, float, list[str]]]:
    """(game, weight, reasons) for every installed, unfinished game matching the filters."""
    from modules import discover, showcase
    profile = discover.build_profile()
    now = time.time()
    out = []
    for game in globals.games.values():
        if not game.installed or game.archived or _is_finished(game) or installdate._main_exe(game) is None:
            continue
        tags = [getattr(t, "text", str(t)) for t in game.tags]
        if mood and mood not in tags:
            continue
        state = showcase.to_play_state(game)
        last = game.last_launched.value or 0
        if kind == 1 and not (game.playtime or last):
            continue
        if kind == 2 and (game.playtime or last):
            continue
        if kind == 3 and state != "UPDATE":
            continue
        reasons = []
        weight = max(0.3, 1 + discover.affinity(game) / 4)
        if game.rating >= 4:
            reasons.append(f"you rated it {game.rating}/5")
        if game.playtime >= 3600:
            reasons.append(f"{game.playtime / 3600:.0f} h played")
        liked = [t for t in tags if profile.tags.get(t, 0) > 0.4]
        if tags:
            weight *= 1 + max(-0.5, sum(profile.tags.get(t, 0) for t in tags) / len(tags))
        if liked:
            reasons.append("tags you like: " + ", ".join(liked[:3]))
        if state == "UPDATE":
            weight *= 2.0
            reasons.append("updated since you last played")
        elif state == "NEW":
            weight *= 1.3
            reasons.append("installed, never played")
        if last:
            days = (now - last) / 86400
            if days < 1 and kind != 1:
                weight *= 0.5
            weight *= 1 + min(days, 120) / 240
            if days >= 30:
                reasons.append(f"not played for {int(days)} days")
        out.append((game, weight, reasons))
    return out


def favourite_tags(count: int = 12) -> list[str]:
    from modules import discover
    tags = discover.build_profile().tags
    return [t for t, w in sorted(tags.items(), key=lambda tw: -tw[1]) if w > 0][:count]


def pick(kind: int = 0, mood: str = "", avoid: set[int] = frozenset()) -> tuple[Game, list[str]] | None:
    pool = candidates(kind, mood)
    fresh = [c for c in pool if c[0].id not in avoid] or pool
    if not fresh:
        return None
    game, _, reasons = random.choices(fresh, weights=[w for _, w, _ in fresh])[0]
    return game, reasons


def open_tonight_popup():
    import imgui
    state = {"kind": 0, "mood": 0, "pick": None, "seen": set()}
    moods = ["Any mood"] + favourite_tags()

    def draw():
        state["pick"] = pick(state["kind"], moods[state["mood"]] if state["mood"] else "", state["seen"])
        if state["pick"]:
            state["seen"].add(state["pick"][0].id)

    draw()

    def content():
        scaled = globals.gui.scaled if globals.gui else (lambda x: x)
        imgui.set_next_item_width(scaled(260))
        changed1, state["kind"] = imgui.combo("###tn_kind", state["kind"], KINDS)
        imgui.same_line()
        imgui.set_next_item_width(scaled(200))
        changed2, state["mood"] = imgui.combo("###tn_mood", state["mood"], moods)
        if changed1 or changed2:
            state["seen"].clear()
            draw()
        imgui.separator()
        if state["pick"] is None:
            imgui.text("No installed, unfinished game matches. Try another filter.")
            return
        game, reasons = state["pick"]
        width = scaled(520)
        height = width / max(globals.settings.cell_image_ratio, 0.1)
        if not game.image.missing and not game.image.error:
            game.image.render(width, height, *game.image.crop_to_ratio(width / height, fit=globals.settings.fit_images),
                              rounding=scaled(globals.settings.style_corner_radius))
        imgui.push_font(imgui.fonts.big)
        imgui.text(game.name)
        imgui.pop_font()
        imgui.text_disabled(f"{game.version}  -  {game.developer}")
        for reason in reasons:
            imgui.bullet_text(reason)
        imgui.spacing()
        if imgui.button(f"{icons.play} Play###tn_play"):
            from modules import callbacks
            callbacks.launch_game(game)
            return True                    # closes the popup
        imgui.same_line()
        if imgui.button(f"{icons.information_outline} Details###tn_info"):
            utils.push_popup(globals.gui.draw_game_info_popup, game, None)
        imgui.same_line()
        if imgui.button(f"{icons.dice_multiple} Another one###tn_again"):
            draw()

    utils.push_popup(utils.popup, "What to play tonight?", content, buttons=True, closable=True, outside=True, popup_uuid="tonight")


# ---------------------------------------------------------------------------
# Discretion key
# ---------------------------------------------------------------------------

_MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
_NAMED = {"pause": 0x13, "end": 0x23, "home": 0x24, "insert": 0x2D, "delete": 0x2E, "space": 0x20, "escape": 0x1B, "esc": 0x1B}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """'ctrl+alt+h' -> (modifiers, virtual key). None if it can't be used."""
    mods, key = 0, None
    for part in [p.strip().lower() for p in (text or "").split("+") if p.strip()]:
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part in _NAMED:
            key = _NAMED[part]
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        else:
            return None
    return (mods, key) if key is not None else None


@dataclasses.dataclass(slots=True)
class Discretion:
    hidden: bool = False
    windows: list = dataclasses.field(default_factory=list)
    muted: bool = False


discretion = Discretion()
_hotkey_thread_id = None


def _windows_of(pids: set[int]) -> list[int]:
    import ctypes
    import ctypes.wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def callback(hwnd, _):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):   # GW_OWNER: top windows only
            found.append(hwnd)
        return True
    user32.EnumWindows(callback, 0)
    return found


def _press_mute():
    import ctypes
    VK_VOLUME_MUTE, KEYUP = 0xAD, 0x0002
    ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, KEYUP, 0)


def toggle_discretion(origin: str = "hotkey"):
    """Hide: toy stopped (latched), games + F95Checker minimised, sound muted. Show: games and sound back."""
    import ctypes
    from modules import toy
    user32 = ctypes.windll.user32
    if not discretion.hidden:
        with contextlib.suppress(Exception):
            toy.emergency_stop("discretion")
        pids = set()
        for game in globals.games.values():
            if game.launch_state in (LaunchState.Starting, LaunchState.Playing) and game.launch_process is not None:
                with contextlib.suppress(Exception):
                    pids |= toy._process_tree(game.launch_process.pid)
        game_windows = _windows_of(pids) if pids else []
        for hwnd in game_windows:
            user32.ShowWindow(hwnd, 6)                       # SW_MINIMIZE
        if prefs()["discreet_hide_f95"]:
            for hwnd in _windows_of({os.getpid()}):
                user32.ShowWindow(hwnd, 6)
        discretion.muted = bool(prefs()["discreet_mute"])
        if discretion.muted:
            _press_mute()
        discretion.windows = game_windows
        discretion.hidden = True
        _log(f"Discretion on ({origin}): {len(game_windows)} game window(s) minimised")
    else:
        for hwnd in discretion.windows:
            if user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, 9)                   # SW_RESTORE
        if discretion.windows and user32.IsWindow(discretion.windows[0]):
            user32.SetForegroundWindow(discretion.windows[0])
        if discretion.muted:
            _press_mute()
        discretion.hidden, discretion.windows, discretion.muted = False, [], False
        _log("Discretion off: games and sound back (toy stays stopped until Resume)")


def _hotkey_loop():
    global _hotkey_thread_id
    try:
        import ctypes
        import ctypes.wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        _hotkey_thread_id = kernel32.GetCurrentThreadId()
        actions = {}
        for hotkey_id, key, label, action in ((0x95C, "discreet_key", "Discretion key", toggle_discretion),
                                              (0x95D, "safe_key", "Safe screen key", toggle_safe_screen)):
            if not prefs()[key]:
                continue
            parsed = parse_hotkey(prefs()[key])
            if parsed is None:
                _log(f"{label} not understood: {prefs()[key]!r}")
            elif not user32.RegisterHotKey(None, hotkey_id, parsed[0] | 0x4000, parsed[1]):    # MOD_NOREPEAT
                _log(f"{label} {prefs()[key]} could not be registered (used by another program?)")
            else:
                _log(f"{label}: {prefs()[key]}")
                actions[hotkey_id] = action
        if not actions:
            return
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in actions:     # WM_HOTKEY
                with contextlib.suppress(Exception):
                    actions[msg.wParam]()
        for hotkey_id in actions:
            user32.UnregisterHotKey(None, hotkey_id)
    except Exception as exc:
        _log(f"Discretion key thread error: {exc}")


def restart_hotkey():
    """Library tools: new key typed -> registered again."""
    _stop_hotkey()
    if globals.os is Os.Windows and (prefs()["discreet_key"] or prefs()["safe_key"]):
        threading.Thread(target=_hotkey_loop, name="discretion-key", daemon=True).start()


def _stop_hotkey():
    global _hotkey_thread_id
    if _hotkey_thread_id and globals.os is Os.Windows:
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(_hotkey_thread_id, 0x0012, 0, 0)   # WM_QUIT
    _hotkey_thread_id = None


@contextlib.contextmanager
def setup():
    restart_hotkey()
    try:
        yield
    finally:
        _stop_hotkey()


# ---------------------------------------------------------------------------
# Clean up
# ---------------------------------------------------------------------------

ARCHIVE_EXTS = (".zip", ".7z", ".rar")


@dataclasses.dataclass(slots=True)
class CleanItem:
    kind: str            # "archive" / "cache" / "leftover"
    path: pathlib.Path
    size: int
    game: str
    why: str
    selected: bool = True
    done: str = ""


def _size(path: pathlib.Path) -> int:
    return path.stat().st_size if path.is_file() else installdate.folder_size(path)


def find_cleanable() -> list[CleanItem]:
    """Blocking (run it in a thread)."""
    from modules import pctools
    items: list[CleanItem] = []
    imported = {}
    for game_id, info in installdate.local_info().items():
        for archive in info.get("imported_archives", []) if isinstance(info, dict) else []:
            imported[os.path.normcase(archive)] = globals.games.get(game_id)
    # 1. archives already installed
    for folder in pctools.watch_dirs():
        with contextlib.suppress(OSError):
            for entry in os.scandir(folder):
                if not entry.is_file() or not entry.name.lower().endswith(ARCHIVE_EXTS):
                    continue
                path = pathlib.Path(entry.path)
                game = imported.get(os.path.normcase(str(path)))
                why = ""
                if game is not None:
                    why = f"installed with Import Update ({game.installed or game.version})"
                else:
                    matches = pctools.match_archive(entry.name)
                    if matches and matches[0][0] >= 0.85:
                        game = matches[0][1]
                        archive_v = installdate.version_tuple(entry.name)
                        installed_v = installdate.version_tuple(game.installed or "")
                        if game.installed and archive_v and installed_v and archive_v <= installed_v:
                            why = f"version {'.'.join(map(str, archive_v))}, you have {game.installed} installed"
                if why:
                    items.append(CleanItem("archive", path, entry.stat().st_size, game.name if game else "", why))
    # 2. Ren'Py caches, 3. interrupted imports
    roots = set()
    for game in list(globals.games.values()):
        exe = installdate._main_exe(game)
        if exe is None:
            continue
        roots.add(exe.parent.parent)
        cache = exe.parent / "game" / "cache"
        if (exe.parent / "renpy").is_dir() and cache.is_dir() and game.launch_state is LaunchState.Idle:
            size = _size(cache)
            if size > 1024 ** 2:
                items.append(CleanItem("cache", cache, size, game.name, "Ren'Py cache, rebuilt at the next launch (first start a bit slower)"))
    for root in set(installdate.library_roots(globals.games.values())) | roots:
        with contextlib.suppress(OSError):
            for entry in os.scandir(root):
                if entry.is_dir() and entry.name.startswith(".f95checker-import-"):
                    path = pathlib.Path(entry.path)
                    items.append(CleanItem("leftover", path, _size(path), "", "files of an interrupted Import Update"))
    order = {"archive": 0, "leftover": 1, "cache": 2}
    return sorted(items, key=lambda i: (order[i.kind], -i.size))


def clean(items: list[CleanItem]) -> tuple[int, list[str]]:
    """Blocking. Archives to the recycle bin (Windows), the rest deleted. Returns (bytes freed, errors)."""
    freed, errors = 0, []
    for item in items:
        if not item.selected or item.done:
            continue
        try:
            if item.kind == "archive":
                if globals.os is Os.Windows:
                    installdate._recycle(item.path)
                else:
                    item.path.unlink()
            else:
                shutil.rmtree(item.path)
            item.done = "deleted"
            freed += item.size
            _log(f"Cleaned {item.path} ({installdate.size_display(item.size)})")
        except Exception as exc:
            item.done = f"error: {exc}"
            errors.append(f"{item.path}: {exc}")
    return freed, errors


def open_cleanup_popup():
    import imgui
    state = {"items": None, "busy": True, "result": ""}

    async def scan():
        import asyncio
        state["items"] = await asyncio.get_running_loop().run_in_executor(None, find_cleanable)
        state["busy"] = False

    async def run():
        import asyncio
        state["busy"] = True
        freed, errors = await asyncio.get_running_loop().run_in_executor(None, clean, state["items"])
        state["result"] = f"{installdate.size_display(freed)} freed." + (f" {len(errors)} error(s), see the list." if errors else "")
        state["busy"] = False

    async_thread.run(scan())
    labels = {"archive": "Archive", "cache": "Ren'Py cache", "leftover": "Leftover"}

    def content():
        scaled = globals.gui.scaled if globals.gui else (lambda x: x)
        imgui.text_disabled("Archives go to the recycle bin; caches and leftovers are deleted (the games rebuild their cache).")
        if state["items"] is None:
            imgui.text("Looking for files to clean...")
            return
        if not state["items"]:
            imgui.text("Nothing to clean.")
            return
        chosen = [i for i in state["items"] if i.selected and not i.done]
        if state["result"]:
            imgui.text(state["result"])
        if not state["busy"] and chosen:
            if imgui.button(f"{icons.broom} Clean {len(chosen)} item(s), {installdate.size_display(sum(i.size for i in chosen))}###cl_go"):
                async_thread.run(run())
        elif state["busy"]:
            imgui.text("Working...")
        flags = imgui.TABLE_SCROLL_Y | imgui.TABLE_ROW_BACKGROUND | imgui.TABLE_BORDERS_INNER_HORIZONTAL
        if imgui.begin_table("###cl_table", 5, flags, outer_size_height=scaled(420)):
            for name in ("", "What", "Game", "Size", "Why"):
                imgui.table_setup_column(name)
            imgui.table_headers_row()
            for n, item in enumerate(state["items"]):
                imgui.table_next_row()
                imgui.table_next_column()
                if not item.done and not state["busy"]:
                    _, item.selected = imgui.checkbox(f"###cl_sel_{n}", item.selected)
                imgui.table_next_column()
                imgui.text(f"{labels[item.kind]}: {item.path.name}")
                if imgui.is_item_hovered():
                    imgui.set_tooltip(str(item.path))
                imgui.table_next_column()
                imgui.text(item.game)
                imgui.table_next_column()
                imgui.text(installdate.size_display(item.size))
                imgui.table_next_column()
                imgui.text(item.done or item.why)
            imgui.end_table()

    utils.push_popup(utils.popup, "Clean up", content, buttons=True, closable=True, outside=False, popup_uuid="extras_clean")


# ---------------------------------------------------------------------------
# What went wrong? (Ren'Py games: right click, and automatically after a game shows an error)
# ---------------------------------------------------------------------------
#   Ren'Py writes traceback.txt (error while playing) or errors.txt (script errors at start) next to the exe.
#   They are read when the game closes after a session started from F95Checker, and from the right click
#   menu at any time: the error in plain words, the file at fault, and a one-click fix when there is a known one.

import re as _re

CRASH_FILES = ("traceback.txt", "errors.txt")
_MISSING_RE = _re.compile(r"Image '([^']+)' not found|Couldn't find file '([^']+)'|"
                          r"Could not find (?:the )?(?:file|movie|image)[: ]+'?([^'\n]+?)'?(?:\.|$)", _re.I | _re.M)
_TWICE_RE = _re.compile(r"(\S+) is defined twice", _re.I)
_FILE_RE = _re.compile(r'File "([^"]+)", line (\d+)')
_GL_RE = _re.compile(r"OpenGL|glGetString|\bGL_\w+|angle|Could not set video mode|shader|GLES|renderer", _re.I)
_SAVE_RE = _re.compile(r"loadsave\.py|in load\b|UnpicklingError|while loading", _re.I)


@dataclasses.dataclass(slots=True)
class Diagnosis:
    path: pathlib.Path              # traceback.txt / errors.txt
    when: float
    error: str                      # the error line
    culprit: str = ""               # last game file of the trace ("game/x.rpy")
    line: int = 0
    kind: str = "other"             # missing / engine / twice / mod / save / graphics / memory / other
    explain: str = ""
    actions: list = dataclasses.field(default_factory=list)   # (label, callable returning a result text)


def _crash_file(game: Game) -> pathlib.Path | None:
    exe = installdate._main_exe(game)
    if exe is None:
        return None
    found = [exe.parent / name for name in CRASH_FILES if (exe.parent / name).is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _error_line(top: str) -> str:
    lines = [l.rstrip() for l in top.splitlines() if l.strip()]
    for line in reversed(lines):
        if not line.startswith((" ", "\t")) and not line.startswith(("While ", "I'm sorry", "File ", "Ren'Py", "--")):
            return line.strip()
    return lines[-1].strip() if lines else ""


def diagnose(game: Game) -> Diagnosis | None:
    path = _crash_file(game)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    top = text.split("-- Full Traceback", 1)[0]
    error = _error_line(top) if path.name == "traceback.txt" else \
        next((l.strip() for l in top.splitlines() if l.strip().startswith("File ")), _error_line(top))
    files = [(f, int(n)) for f, n in _FILE_RE.findall(top) if not f.replace("\\", "/").startswith(("renpy/", "lib/"))]
    d = Diagnosis(path, path.stat().st_mtime, error)
    if files:
        d.culprit, d.line = files[-1][0].replace("\\", "/"), files[-1][1]
    exe = installdate._main_exe(game)
    rel = d.culprit[5:] if d.culprit.startswith("game/") else ""

    # 1. Ren'Py engine files of two versions side by side (update extracted over an older one)
    conflicts = []
    if exe is not None and ("_ren.py" in text or _re.search(r"renpy/\S+\.rpy", text)):
        conflicts = [1] if _engine_conflicts(exe) else []
    if conflicts:
        d.kind = "engine"
        d.explain = ("The game folder holds Ren'Py engine files of two versions (an update extracted over the older one).\n"
                     "Fix: the old engine files are moved to _f95checker_old_engine (not deleted).")
        d.actions.append(("Fix the engine files", lambda: _fix_engine(game)))
        return d
    # 2. a mod: the file at fault was installed by F95Checker, or looks like a mod
    mod = _installed_mod_with(game, d.culprit) if d.culprit else None
    if mod is not None:
        d.kind = "mod"
        d.explain = (f"The error is in {d.culprit}, a file of the mod \"{mod.get('name')}\" you installed with F95Checker:\n"
                     "it does not fit this version of the game. Uninstall it (the game files it replaced come back).")
        d.actions.append((f"Uninstall {mod.get('name')}", lambda: _uninstall_mod(game, mod.get("id"))))
        return d
    if rel and installdate.MOD_RE.search(rel.rsplit("/", 1)[-1]):
        d.kind = "mod"
        d.explain = (f"The error is in {d.culprit}, which looks like a mod (walkthrough / cheat / gallery...):\n"
                     "probably made for another version. Disable it: moved to _disabled_mods (move it back to undo).")
        d.actions.append((f"Disable {rel}", lambda: _disable(game, rel)))
        return d
    # 3. defined twice: an old script left next to the new one
    twice = _TWICE_RE.search(text)
    if twice:
        d.kind = "twice"
        others = sorted({f.replace("\\", "/") for f, _ in _FILE_RE.findall(text) if f.replace("\\", "/").startswith("game/")})
        older = _oldest(exe, others) if exe is not None and len(others) >= 2 else ""
        d.explain = (f"\"{twice.group(1)}\" is defined twice" + (f" (in {', '.join(others)})" if others else "") + ":\n"
                     "usually a script of the previous version (or of a mod) left in the folder.")
        if older:
            d.explain += f"\nThe older file is {older}: set it aside (moved to _disabled_mods, move it back to undo)."
            d.actions.append((f"Set aside {older[5:]}", lambda: _disable(game, older[5:])))
        return d
    # 4. missing files
    missing = [next(g for g in m.groups() if g) for m in _MISSING_RE.finditer(top)]
    if missing:
        d.kind = "missing"
        d.explain = (f"A file of the game is missing: {missing[0]}.\n"
                     "The game folder is incomplete (archive badly extracted, or an update applied over a partial copy).\n"
                     "Download the game again (or its latest version) and install it with Import Update.")
        d.actions.append(("Open the game thread", lambda: _open(game.url)))
        return d
    # 5. saves of another version
    if _SAVE_RE.search(top):
        d.kind = "save"
        d.explain = ("The error happened while loading a save: saves made with another version (or with a mod) often\n"
                     "can't be loaded. Start a new game, or load an older save made with this version.")
        return d
    # 6. graphics
    if _GL_RE.search(text):
        d.kind = "graphics"
        d.explain = "A graphics / renderer error. In the game, Shift+G opens the renderer choice: try another one."
        try:
            from modules import pctools
            if pctools.dlss5_state(game) == "installed":
                d.explain += "\nDLSS 5 is installed for this game: remove it first."
                d.actions.append(("Remove DLSS 5 for this game", lambda: pctools.dlss5_remove(game) or "DLSS 5 removed."))
        except Exception:
            pass
        return d
    if "MemoryError" in text or "out of memory" in text.lower():
        d.kind = "memory"
        d.explain = "Not enough memory. Close other programs (browser, other games) and start the game again."
        return d
    d.explain = (f"Error in {d.culprit}, line {d.line}." if d.culprit else "Error inside the Ren'Py engine.") + \
        "\nNo known fix: the full text is in the file below (the game's thread often has the answer)."
    return d


def _engine_conflicts(exe: pathlib.Path) -> bool:
    engine = exe.parent / "renpy"
    if not engine.is_dir():
        return False
    for current, dirs, files in os.walk(engine):
        names = {f.lower() for f in files}
        if any(f.endswith("_ren.py") and f[:-7] + ".rpy" in names for f in names):
            return True
    return False


def _fix_engine(game: Game) -> str:
    exe = installdate._main_exe(game)
    fixed = installdate.fix_engine_conflicts(exe) if exe is not None else []
    return f"{len(fixed)} old engine file(s) set aside: start the game again." if fixed else "Nothing to fix any more."


def _installed_mod_with(game: Game, culprit: str) -> dict | None:
    from modules import importupdate
    for mod in reversed(importupdate.installed_mods(game)):
        files = {f.lower() for f in mod.get("files", [])}
        if culprit.lower() in files or (culprit.lower().endswith(".rpyc") and culprit.lower()[:-1] in files):
            return mod
    return None


def _uninstall_mod(game: Game, mod_id: int) -> str:
    from modules import importupdate
    err = importupdate.uninstall_mod(game, mod_id)
    return f"Not uninstalled: {err}" if err else "Mod uninstalled: start the game again."


def _disable(game: Game, rel: str) -> str:
    err = installdate.disable_mod(game, rel)
    return f"Not moved: {err}" if err else f"{rel} moved to {installdate.DISABLED_MODS_DIR}: start the game again."


def _oldest(exe: pathlib.Path, rels: list[str]) -> str:
    dated = []
    for rel in rels:
        path = exe.parent / rel
        with contextlib.suppress(OSError):
            dated.append((path.stat().st_mtime, rel))
    if len(dated) < 2:
        return ""
    dated.sort()
    return dated[0][1] if dated[1][0] - dated[0][0] > 86400 else ""      # clearly older (a day or more)


def _open(url: str) -> str:
    from modules import callbacks
    callbacks.open_webpage(url)
    return ""


def open_diagnosis_popup(game: Game, d: Diagnosis | None = None):
    import imgui
    d = d or diagnose(game)
    if d is None:
        utils.push_popup(msgbox.msgbox, "What went wrong?", f"No error file of {game.name} found (traceback.txt / errors.txt).", MsgBox.info)
        return
    state = {"result": ""}

    def content():
        imgui.text(game.name)
        imgui.text_disabled(f"{d.path.name}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(d.when))}")
        imgui.separator()
        imgui.push_text_wrap_pos(imgui.get_cursor_pos_x() + (globals.gui.scaled(700) if globals.gui else 700))
        imgui.text_colored(d.error or "(no error line)", 0.95, 0.45, 0.35)
        if d.culprit:
            imgui.text_disabled(f"In {d.culprit}, line {d.line}")
        imgui.spacing()
        imgui.text(d.explain)
        imgui.pop_text_wrap_pos()
        imgui.spacing()
        for i, (label, action) in enumerate(d.actions):
            if imgui.button(f"{label}###diag_act_{i}"):
                try:
                    state["result"] = action() or ""
                except Exception as exc:
                    state["result"] = f"Failed: {exc}"
            imgui.same_line()
        if imgui.button(f"Open {d.path.name}###diag_open"):
            from modules import callbacks
            async_thread.run(callbacks.default_open(str(d.path)))
        if state["result"]:
            imgui.text_colored(state["result"], 0.4, 0.85, 0.4)

    utils.push_popup(utils.popup, "What went wrong?", content, buttons=True, closable=True, outside=False,
                     popup_uuid=f"diagnosis_{game.id}")


def after_game_exit(game: Game, started: float):
    """Called when a game started from F95Checker closes: an error file written during the session -> popup."""
    try:
        path = _crash_file(game)
        if path is None or path.stat().st_mtime < started - 2:
            return
        d = diagnose(game)
        if d is not None:
            _log(f"'{game.name}': {d.path.name} written during the session ({d.kind}): {d.error}")
            open_diagnosis_popup(game, d)
    except Exception as exc:
        _log(f"Crash check of {game.name}: {exc!r}")


# ---------------------------------------------------------------------------
# Safe screen (Library tools button, default Ctrl+Alt+B anywhere in Windows)
# ---------------------------------------------------------------------------
#   F95Checker stays open but shows nothing explicit: banners replaced by a plain tile, game names by
#   "Title 01234", developer, tags, labels, notes and descriptions hidden. Press again to get everything back.
#   Only what the window SHOWS changes (the interface thread): refresh, database and saves see the real data.

_safe_on = False
_placeholder = None
_SAFE_ATTRS = frozenset({"name", "image", "developer", "tags", "unknown_tags", "description", "changelog", "notes", "labels",
                         "preview_images", "previews_urls"})      # thread screenshots: info popup and hover slideshow


def safe_screen_on() -> bool:
    return _safe_on


def _safe_placeholder():
    global _placeholder
    if _placeholder is None:
        from external import imagehelper
        folder = globals.data_path / "safe_screen"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "tile.png"
        if not path.is_file():
            from PIL import Image
            Image.new("RGB", (960, 450), (40, 42, 48)).save(path)
        _placeholder = imagehelper.ImageHelper(folder, glob="tile.*")
    return _placeholder


def _safe_getattribute(self, attr):
    if attr in _SAFE_ATTRS and threading.current_thread() is threading.main_thread():
        if attr == "name":
            return f"Title {object.__getattribute__(self, 'id') % 100000:05d}"
        if attr == "image":
            return _safe_placeholder()
        if attr in ("tags", "unknown_tags", "previews_urls"):
            return ()
        if attr in ("labels", "preview_images"):
            return []
        return ""
    return object.__getattribute__(self, attr)


def toggle_safe_screen(origin: str = "hotkey"):
    global _safe_on
    from common.structs import Game
    if not _safe_on:
        with contextlib.suppress(Exception):
            _safe_placeholder()
        Game.__getattribute__ = _safe_getattribute
        _safe_on = True
    else:
        with contextlib.suppress(AttributeError):
            del Game.__getattribute__
        _safe_on = False
    globals.safe_screen = _safe_on
    if globals.gui is not None:
        globals.gui.recalculate_ids = True
    _log(f"Safe screen {'on' if _safe_on else 'off'} ({origin})")


# ---------------------------------------------------------------------------
# Status tab of the game info (everything F95Checker added or found for this game, with its action)
# ---------------------------------------------------------------------------

_status_cache: dict[int, tuple[float, dict]] = {}


def _status(game: Game) -> dict:
    now = time.monotonic()
    cached = _status_cache.get(game.id)
    if cached and now - cached[0] < 2:
        return cached[1]
    from modules import cheatsheet, depth, importupdate, pctools, remaster, toy
    exe = installdate._main_exe(game)
    s = {"exe": exe, "renpy": installdate.engine_now(game) == "Ren'Py", "engine": installdate.local_engine(game)}
    s["size"] = installdate.local_size(game)
    s["compressed"] = pctools.is_compressed(game)
    s["dlss"] = pctools.dlss5_state(game)
    s["reshade"] = pctools.reshade_enabled(game)
    cfg = pctools.prefs()["display"].get(str(game.id), {})
    s["ls"] = "off" if not cfg.get("lossless") else cfg.get("ls_mode", "scale")
    console = installdate._console_path(game)
    s["cheats"] = installdate.cheats_enabled(game)
    s["unlock"] = installdate.unlock_enabled(game)
    s["cheatmenu"] = cheatsheet.menu_enabled(game)
    s["remaster"] = len(remaster.written_paths(game, exe)) if exe else 0
    s["depth"] = depth.count_done(exe.parent) if exe else 0
    s["prefs"] = console is not None and console.with_name(installdate.PREFS_FILE).is_file()
    s["mods"] = importupdate.installed_mods(game)
    s["old_mods"] = installdate.outdated_mods_of(game)
    s["crash"] = _crash_file(game)
    s["toy"] = toy.toy_display(game) if toy.is_enabled(game.id) else "off"
    _status_cache[game.id] = (now, s)
    return s


def draw_status_tab(gui, game: Game):
    import imgui
    from modules import callbacks, cheatsheet, depth, importupdate, pctools, remaster, toy
    s = _status(game)
    state = draw_status_tab.__dict__.setdefault("msg", {})

    def act(label: str, func, uid: str):
        if imgui.small_button(f"{label}###st_{uid}_{game.id}"):
            try:
                result = func()
                state[game.id] = result if isinstance(result, str) else ""
            except Exception as exc:
                state[game.id] = f"Failed: {exc}"
            _status_cache.pop(game.id, None)
        imgui.same_line()

    def row(label: str, value: str, color=None):
        imgui.table_next_row()
        imgui.table_next_column()
        imgui.text_disabled(label)
        imgui.table_next_column()
        if color:
            imgui.text_colored(value, *color)
        else:
            imgui.text(value)
        imgui.table_next_column()

    good, warn, bad = (0.4, 0.85, 0.4), (0.95, 0.7, 0.2), (0.95, 0.4, 0.35)
    if not imgui.begin_table(f"###status_{game.id}", 3, imgui.TABLE_ROW_BACKGROUND):
        return
    imgui.table_setup_column("", imgui.TABLE_COLUMN_WIDTH_FIXED, gui.scaled(150))
    imgui.table_setup_column("", imgui.TABLE_COLUMN_WIDTH_STRETCH)
    imgui.table_setup_column("", imgui.TABLE_COLUMN_WIDTH_FIXED, gui.scaled(300))
    # version
    up_to_date = game.installed and game.installed == game.version
    row("Version", f"{game.installed or 'not installed'}" + ("" if up_to_date or not game.installed else f"  ->  {game.version} available"),
        good if up_to_date else warn if game.installed else None)
    if game.installed and not up_to_date:
        act("Import Update", lambda: importupdate.import_update(game), "imp")
        if game.downloads and not game.custom:
            act("Download Update", lambda: installdate.start_download(game), "dl")
    # folder
    exe = s["exe"]
    row("Folder", str(exe.parent) if exe else "executable not found", None if exe else bad)
    if exe:
        act("Open", lambda: callbacks.open_game_folder(game), "open")
    row("Size", (installdate.size_display(s["size"]) if s["size"] else "?") + ("  (compressed)" if s["compressed"] else ""))
    if exe:
        act("Uncompress" if s["compressed"] else "Compress",
            lambda: async_thread.run(pctools.compress_game(game, uncompress=s["compressed"])) and "Started (see Compress).", "comp")
    row("Engine", s["engine"] or "?")
    # graphics
    if s["dlss"] != "n/a":
        row("DLSS 5", {"installed": "installed", "other": "ReShade / DLSS put there another way", "": "not installed"}[s["dlss"]],
            good if s["dlss"] == "installed" else None)
        if s["dlss"] == "installed":
            act("Remove", lambda: pctools.dlss5_remove(game) or "DLSS 5 removed.", "dlssrm")
        elif s["dlss"] == "":
            act("Install", lambda: pctools.dlss5_install(game) or "DLSS 5 installed.", "dlssin")
    if s["reshade"] is not None:
        row("ReShade", "on" if s["reshade"] else "off (dxgi.dll.off)", good if s["reshade"] else None)
        act("Turn off" if s["reshade"] else "Turn on", lambda: pctools.set_reshade(game, not s["reshade"]) or "Done.", "rs")
    row("Lossless Scaling", {"off": "off", "scale": "scaling", "framegen": "frame generation"}.get(s["ls"], s["ls"]))
    # Ren'Py
    if s["renpy"]:
        row("Cheats console", "on (Shift+O in game)" if s["cheats"] else "off", good if s["cheats"] else None)
        act("Disable" if s["cheats"] else "Enable", lambda: installdate.toggle_cheats(game), "cheat")
        act("Cheat sheet", lambda: cheatsheet.open_popup(game), "sheet")
        row("Gallery & replays", "unlocked" if s["unlock"] else "normal", good if s["unlock"] else None)
        act("Lock" if s["unlock"] else "Unlock", lambda: installdate.toggle_unlock(game), "unlock")
        row("In-game cheat menu", "on (Shift+C in game)" if s["cheatmenu"] else "off", good if s["cheatmenu"] else None)
        act("Remove" if s["cheatmenu"] else "Add", lambda: cheatsheet.toggle_menu(game), "cheatmenu")
        row("HD & 60 fps", f"{s['remaster']} file(s) added" if s["remaster"] else "not used", good if s["remaster"] else None)
        act("Open...", lambda: remaster.open_popup(game), "remaster")
        row("3D & living pictures", f"{s['depth']} depth map(s), Shift+P in the game" if s["depth"] else "not used", good if s["depth"] else None)
        act("Open...", lambda: depth.open_popup(game), "depth")
        row("Shared preferences", "applied" if s["prefs"] else "not in this game", good if s["prefs"] else None)
        act("Apply now", lambda: "Applied." if installdate.ensure_renpy_prefs(game) else "Already up to date.", "prefs")
        mods = s["mods"]
        row("Mods installed", ", ".join(m.get("name", "?") for m in mods) if mods else "none")
        act("Install a mod...", lambda: importupdate.install_mod(game), "modin")
        if mods:
            act("Uninstall...", lambda: importupdate.open_installed_mods_popup(game), "modun")
        if s["old_mods"]:
            row("Outdated mods", f"{len(s['old_mods'])}: " + ", ".join(s["old_mods"][:3]) + ("..." if len(s["old_mods"]) > 3 else ""), warn)
            act("Check", lambda: installdate.open_outdated_mods_popup([game]), "oldmods")
        crash = s["crash"]
        row("Last error", f"{crash.name}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(crash.stat().st_mtime))}" if crash else "none",
            warn if crash else good)
        if crash:
            act("What went wrong?", lambda: open_diagnosis_popup(game), "crash")
    # toy
    row("Toy", s["toy"])
    act("Panel", lambda: toy.open_panel(game), "toy")
    imgui.end_table()
    if state.get(game.id):
        imgui.text_colored(state[game.id], *good)
