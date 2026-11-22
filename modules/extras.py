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
        parsed = parse_hotkey(prefs()["discreet_key"])
        if parsed is None:
            _log(f"Discretion key not understood: {prefs()['discreet_key']!r}")
            return
        if not user32.RegisterHotKey(None, 0x95C, parsed[0] | 0x4000, parsed[1]):    # MOD_NOREPEAT
            _log(f"Discretion key {prefs()['discreet_key']} could not be registered (used by another program?)")
            return
        _log(f"Discretion key: {prefs()['discreet_key']}")
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam == 0x95C:     # WM_HOTKEY
                with contextlib.suppress(Exception):
                    toggle_discretion()
        user32.UnregisterHotKey(None, 0x95C)
    except Exception as exc:
        _log(f"Discretion key thread error: {exc}")


def restart_hotkey():
    """Library tools: new key typed -> registered again."""
    _stop_hotkey()
    if globals.os is Os.Windows and prefs()["discreet_key"]:
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
