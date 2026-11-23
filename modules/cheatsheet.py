"""
Cheat sheet (right click > Cheat sheet, or the button next to Enable Cheats in a game's info popup).

    Lists the variables of a Ren'Py game that are worth changing with the console (Shift+O): money, love / lust
    points, stats, items, flags... with their starting value and a value to set, and a Copy button giving the line
    to paste in the console (Ctrl+V, Enter).

    Read from the game's scripts, never run:
      - .rpy sources (default / define / $ x = ... / $ x += ...);
      - compiled .rpyc scripts, loose or inside .rpa archives (most games ship only those): they are pickled
        syntax trees, read with an unpickler that only builds inert placeholder objects (no class of the game or of
        Ren'Py is loaded, no code runs). Python 2 (Ren'Py 6/7) and Python 3 (Ren'Py 8) games.
    Variables stored inside objects (mc.money...) are not listed: the console line vars(mc) shows them.
"""
import ast
import collections
import contextlib
import dataclasses
import io
import os
import pathlib
import pickle
import re
import struct
import zlib

from common.structs import (
    Game,
)
from external import (
    async_thread,
)
from modules import (
    globals,
    icons,
    installdate,
    toygen,
    utils,
)

LOG_PREFIX = "[CheatSheet]"


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


# ---------------------------------------------------------------------------
# Reading compiled scripts (.rpyc) without running anything
# ---------------------------------------------------------------------------

class _Stub:
    """Placeholder for any class found in a pickle: keeps the state and arguments, does nothing else."""
    _qualname = "?"

    def __new__(cls, *args, **kwargs):
        # str subclasses (Ren'Py's PyExpr: the source text of an expression) come through here with their text
        obj = object.__new__(cls)
        obj.__dict__["_args"] = args
        return obj

    def __init__(self, *args, **kwargs):
        if args:
            self.__dict__["_args"] = args

    def __setstate__(self, state):
        if isinstance(state, tuple) and len(state) == 2 and (state[0] is None or isinstance(state[0], dict)) and isinstance(state[1], dict):
            merged = dict(state[0] or {})
            merged.update(state[1])
            state = merged
        if isinstance(state, dict):
            for key, value in state.items():
                if isinstance(key, bytes):
                    key = key.decode("utf-8", "replace")
                if isinstance(key, str):
                    self.__dict__[key] = value
        else:
            self.__dict__["_state"] = state

    # pickle may fill list / dict subclasses through these
    def append(self, item):
        self.__dict__.setdefault("_items", []).append(item)

    def extend(self, items):
        self.__dict__.setdefault("_items", []).extend(items)

    def __setitem__(self, key, value):
        self.__dict__.setdefault("_map", {})[key] = value


_stub_classes: dict[str, type] = {}
_SAFE_BUILTINS = {"set": set, "frozenset": frozenset, "object": object, "bytearray": bytearray,
                  "complex": complex, "list": list, "dict": dict, "tuple": tuple}


def _reconstructor(cls, base=None, state=None):
    """copy_reg._reconstructor of Python 2 pickles: only ever builds our placeholders."""
    if isinstance(cls, type) and issubclass(cls, _Stub):
        return cls.__new__(cls)
    return _stub_class("copy_reg", "_unknown").__new__(_stub_class("copy_reg", "_unknown"))


def _stub_class(module: str, name: str) -> type:
    qualname = f"{module}.{name}"
    if qualname not in _stub_classes:
        _stub_classes[qualname] = type(name, (_Stub,), {"_qualname": qualname})
    return _stub_classes[qualname]


class _InertUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module in ("copy_reg", "copyreg") and name == "_reconstructor":
            return _reconstructor
        if module in ("__builtin__", "builtins") and name in _SAFE_BUILTINS:
            return _SAFE_BUILTINS[name]
        if module == "collections" and name == "OrderedDict":
            return collections.OrderedDict
        return _stub_class(module, name)


def rpyc_tree(data: bytes):
    """Syntax tree of a .rpyc file (RPC2 slots or old plain zlib), made of placeholders. None if unreadable."""
    try:
        if data.startswith(b"RENPY RPC2"):
            pos, payload = 10, None
            while pos + 12 <= len(data):
                slot, start, length = struct.unpack("<III", data[pos:pos + 12])
                if slot == 0:
                    break
                if slot == 1:
                    payload = data[start:start + length]
                    break
                pos += 12
            if payload is None:
                return None
            raw = zlib.decompress(payload)
        else:
            raw = zlib.decompress(data)
        return _InertUnpickler(io.BytesIO(raw), encoding="utf-8", errors="replace").load()
    except Exception:
        return None


def _text(value) -> str:
    if isinstance(value, _Stub):                     # PyExpr placeholder: its text is its first argument
        args = value.__dict__.get("_args") or ()
        value = args[0] if args else None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _pycode_source(code) -> str:
    """Source text of a renpy.ast.PyCode placeholder (state tuple: (version, source, location, mode, ...))."""
    if code is None:
        return ""
    state = code.__dict__.get("_state") if isinstance(code, _Stub) else None
    if isinstance(state, tuple):
        for item in state[1:]:
            if isinstance(item, (str, bytes)) or (isinstance(item, _Stub) and _text(item)):
                return _text(item)
    if isinstance(code, _Stub):
        return _text(code.__dict__.get("source"))
    return ""


def _walk(tree):
    """Every placeholder object of the tree (iterative, each object once)."""
    seen, stack = set(), [tree]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, _Stub):
            yield obj
            stack.extend(obj.__dict__.values())
            stack.extend(getattr(obj, "_args", ()) or ())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        elif isinstance(obj, dict):
            stack.extend(obj.values())


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Var:
    name: str
    start: object = None            # starting value (int / float / bool) when known
    declared: bool = False          # default / define (saved with the game)
    changes: int = 0                # how many times the script changes it (+=, -=, =)
    category: str = "Other"
    suggested: str = ""


CATEGORIES = [
    ("Money", r"money|cash|gold|coin|dollar|credit|euro|yen|wallet|bank|budget|salary|savings|funds"),
    ("Relationships", r"^rp[a-z]|^lp[a-z]|love|lust|affect|aff$|_aff|rel$|_rel|relation|trust|corrupt|desire|arous|horny|obedien|submis|slut|"
                      r"attract|friend|romance|_lp$|_cp$|_rp$|_lv$|points?$|pts$|_pt$|_like|like_|loyal|dominan"),
    ("Stats", r"energy|strength|^str$|stamina|health|^hp$|_hp$|^xp$|_xp$|^exp$|level|lvl|skill|charisma|intel|fitness|"
              r"confiden|karma|reputation|fame|stress|mood|charm|dexter|agility|luck|willpower|perv"),
    ("Items", r"potion|pill|wine|choco|flower|ticket|gift|item|condom|beer|drink|food|ring|lockpick|mushroom|fish|key$|keys$|candy|cookie"),
    ("Time (careful)", r"^day|_day|day$|week|^time|hour|month|^year|calendar|dayweek|period"),
]
SUGGESTED = {"Money": "99999", "Relationships": "100", "Stats": "100", "Items": "10", "Flags": "True"}
IGNORED = re.compile(r"^(_|config$|gui$|build$|style$|preferences$|persistent$|renpy$|store$|[a-z]$|i\d*$|tmp|temp|"
                     r"page|index|idx$|counter$|loop|cnt$|pos$|xpos|ypos|zoom|alpha|speed$|delay$|volume|version|ver$)")


def _categorize(var: Var):
    low = var.name.lower()
    if isinstance(var.start, bool):
        var.category = "Flags"
    else:
        var.category = next((cat for cat, pattern in CATEGORIES if re.search(pattern, low)), "Other")
    if var.category in SUGGESTED:
        var.suggested = SUGGESTED[var.category]
    elif var.start is not None:
        var.suggested = repr(var.start)


def _literal(text: str):
    with contextlib.suppress(Exception):
        value = ast.literal_eval(text.strip())
        if isinstance(value, (bool, int, float)):
            return value
    return None


_DEFAULT_RE = re.compile(r"^\s*(default|define)\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", re.M)
_ASSIGN_RE = re.compile(r"^\s*\$\s*([A-Za-z_]\w*)\s*(\+=|-=|=)\s*([-+]?\d+(?:\.\d+)?|True|False)\s*(?:#.*)?$", re.M)


def _note(found: dict, name: str, value=None, declared=False, change=False):
    if IGNORED.match(name):
        return
    var = found.get(name)
    if var is None:
        var = found[name] = Var(name)
    if declared:
        var.declared = True
        if value is not None:
            var.start = value
    elif var.start is None and value is not None and not change:
        var.start = value
    if change or not declared:
        var.changes += 1


def _from_python_source(found: dict, source: str):
    """Simple assignments of a python block: x = 5, x += 1 (top-level names only)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:                      # Python 2 only syntax: fall back to the line regex
        for name, op, value in _ASSIGN_RE.findall(source):
            _note(found, name, _literal(value), change=op != "=")
        return
    # statements run in the game's store only: not the inside of functions / classes (their own local names)
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            stack.extend(getattr(node, field, None) or [])
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value.value if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (bool, int, float)) else None
            if value is not None:
                _note(found, node.targets[0].id, value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and isinstance(node.op, (ast.Add, ast.Sub)):
            _note(found, node.target.id, change=True)


def _from_rpy(found: dict, text: str):
    for kind, name, value in _DEFAULT_RE.findall(text):
        literal = _literal(value)
        if literal is not None or kind == "default":
            _note(found, name, literal, declared=kind == "default" or literal is not None)
    for name, op, value in _ASSIGN_RE.findall(text):
        _note(found, name, _literal(value), change=op != "=")


def _from_rpyc(found: dict, data: bytes) -> bool:
    tree = rpyc_tree(data)
    if tree is None:
        return False
    for node in _walk(tree):
        kind = node._qualname.rsplit(".", 1)[-1]
        if kind in ("Default", "Define"):
            store = _text(node.__dict__.get("store")) or "store"
            name = _text(node.__dict__.get("varname"))
            if store != "store" or not name or node.__dict__.get("index") is not None:
                continue
            literal = _literal(_pycode_source(node.__dict__.get("code")))
            if literal is not None or kind == "Default":
                _note(found, name, literal, declared=True)
        elif kind in ("Python", "EarlyPython"):
            source = _pycode_source(node.__dict__.get("code"))
            if source:
                _from_python_source(found, source)
    return True


@dataclasses.dataclass(slots=True)
class Sheet:
    variables: list[Var]
    scripts: int = 0                 # scripts read
    unreadable: int = 0              # compiled scripts that could not be read


def scan_game_dir(game_dir: pathlib.Path) -> Sheet:
    """Blocking. Variables of a Ren'Py game (its game/ folder)."""
    found: dict[str, Var] = {}
    sources: dict[str, bytes | str] = {}            # script path (relative, no extension) -> best source
    archives = []
    for current, dirs, files in os.walk(game_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ("cache", "saves", "tl", "__pycache__")]
        for name in files:
            low = name.lower()
            path = pathlib.Path(current) / name
            rel = path.relative_to(game_dir).as_posix()
            if low.endswith(".rpa"):
                archives.append(path)
            elif low.endswith(".rpyc"):               # compiled = exact syntax tree: preferred to the .rpy
                sources[rel[:-5]] = ("rpyc", path)
            elif low.endswith(".rpy"):
                sources.setdefault(rel[:-4], ("rpy", path))
    for archive in archives:
        for name, entry in toygen.rpa_index(archive).items():
            low = name.lower()
            if low.startswith("tl/"):
                continue
            if low.endswith(".rpy") and name[:-4] not in sources:
                sources[name[:-4]] = ("rpa-rpy", (archive, entry))
            elif low.endswith(".rpyc") and (name[:-5] not in sources or sources[name[:-5]][0] == "rpa-rpy"):
                sources[name[:-5]] = ("rpa-rpyc", (archive, entry))   # loose files win (as in Ren'Py)
    sheet = Sheet([])
    for kind, where in sources.values():
        try:
            if kind == "rpy":
                _from_rpy(found, where.read_text(encoding="utf-8", errors="replace"))
            elif kind == "rpyc":
                if not _from_rpyc(found, where.read_bytes()):
                    sheet.unreadable += 1
            else:
                archive, (offset, length, prefix) = where
                with open(archive, "rb") as f:
                    f.seek(offset)
                    data = prefix + f.read(length - len(prefix))
                if kind == "rpa-rpy":
                    _from_rpy(found, data.decode("utf-8", "replace"))
                elif not _from_rpyc(found, data):
                    sheet.unreadable += 1
            sheet.scripts += 1
        except OSError as exc:
            _log(f"{where}: {exc}")
    # a number / True-False start value, and saved with the game (default) or changed by the story several times
    variables = [v for v in found.values() if v.start is not None and (v.declared or v.changes >= 2)]
    for var in variables:
        _categorize(var)
    order = {cat: i for i, cat in enumerate(["Money", "Relationships", "Stats", "Items", "Flags", "Time (careful)", "Other"])}
    variables.sort(key=lambda v: (order.get(v.category, 9), -v.changes, v.name.lower()))
    sheet.variables = variables
    return sheet


def game_dir_of(game: Game) -> pathlib.Path | None:
    exe = installdate._main_exe(game)
    if exe is None or not (exe.parent / "game").is_dir():
        return None
    return exe.parent / "game"


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

_cache: dict[int, tuple[float, Sheet]] = {}


def open_popup(game: Game):
    import imgui
    from modules import callbacks
    state = {"sheet": None, "filter": "", "values": {}, "search": "", "busy": True, "copied": ""}

    async def load():
        import asyncio
        game_dir = game_dir_of(game)
        if game_dir is None:
            state["sheet"], state["busy"] = Sheet([]), False
            return
        stamp = max((p.stat().st_mtime for p in game_dir.glob("*.rp*")), default=0)
        cached = _cache.get(game.id)
        if cached and cached[0] == stamp:
            state["sheet"] = cached[1]
        else:
            state["sheet"] = await asyncio.get_running_loop().run_in_executor(None, scan_game_dir, game_dir)
            _cache[game.id] = (stamp, state["sheet"])
        state["busy"] = False

    async_thread.run(load())

    def copy(text: str, what: str):
        callbacks.clipboard_copy(text)
        state["copied"] = f"Copied: {what}"

    def content():
        scaled = globals.gui.scaled if globals.gui else (lambda x: x)
        if not installdate.cheats_enabled(game):
            imgui.text_colored("The console is off in this game.", 1.0, 0.6, 0.2)
            imgui.same_line()
            if imgui.small_button(f"{icons.cheese} Enable Cheats###cs_enable"):
                installdate.toggle_cheats(game)
        imgui.text_disabled("In the game: Shift+O opens the console, Ctrl+V pastes the copied line, Enter runs it, Esc closes.\n"
                            "Save first. Values are read from the scripts: the starting value, not your current one\n"
                            "(type the variable name in the console to see it).")
        if state["busy"] or state["sheet"] is None:
            imgui.text("Reading the game's scripts...")
            return
        sheet = state["sheet"]
        imgui.text(f"{len(sheet.variables)} variables in {sheet.scripts} scripts"
                   + (f" ({sheet.unreadable} compiled scripts unreadable)" if sheet.unreadable else ""))
        if state["copied"]:
            imgui.same_line()
            imgui.text_colored(state["copied"], 0.4, 0.85, 0.4)
        imgui.set_next_item_width(scaled(220))
        _, state["filter"] = imgui.input_text_with_hint("###cs_filter", "Filter (money, love, mia...)", state["filter"], 64)
        imgui.same_line()
        imgui.set_next_item_width(scaled(120))
        _, state["search"] = imgui.input_text_with_hint("###cs_search", "value on screen", state["search"], 32)
        imgui.same_line()
        if imgui.button("Copy search by value###cs_search_copy") and state["search"].strip():
            value = state["search"].strip()
            copy(f"[n for n, v in vars(renpy.store).items() if type(v) in (int, float) and v == {value}]",
                 "search by value (paste in the console)")
        if imgui.is_item_hovered():
            imgui.set_tooltip("Type the number the game shows (money, points...), copy, paste in the console:\n"
                              "it lists the variables holding that value. Change it in game and search again to narrow it down.")
        words = state["filter"].lower().split()
        shown = [v for v in sheet.variables if all(w in v.name.lower() or w in v.category.lower() for w in words)]
        if not shown:
            imgui.text("No variable found." if not sheet.variables else "No variable matches the filter.")
            if not sheet.variables:
                imgui.text_disabled("The scripts may be protected. Use the search by value above, or Shift+D > Variable Viewer.")
            return
        imgui.begin_child("###cs_list", height=scaled(430), border=False)
        for category in dict.fromkeys(v.category for v in shown):
            group = [v for v in shown if v.category == category]
            opened = imgui.collapsing_header(f"{category} ({len(group)})###cs_cat_{category}",
                                             flags=imgui.TREE_NODE_DEFAULT_OPEN if category != "Other" else 0)[0]
            if not opened:
                continue
            settable = [v for v in group if state["values"].get(v.name, v.suggested)]
            # not for flags / time / other: switching every flag at once can break the story
            if category in ("Money", "Relationships", "Stats", "Items") and settable and imgui.small_button(f"Copy all {category.lower()}###cs_all_{category}"):
                copy("; ".join(f"{v.name} = {state['values'].get(v.name, v.suggested)}" for v in settable), f"{len(settable)} {category.lower()}")
            if imgui.begin_table(f"###cs_t_{category}", 4, imgui.TABLE_ROW_BACKGROUND):
                imgui.table_setup_column("Variable", imgui.TABLE_COLUMN_WIDTH_STRETCH)
                imgui.table_setup_column("Start", imgui.TABLE_COLUMN_WIDTH_FIXED, scaled(70))
                imgui.table_setup_column("Set to", imgui.TABLE_COLUMN_WIDTH_FIXED, scaled(110))
                imgui.table_setup_column("", imgui.TABLE_COLUMN_WIDTH_FIXED, scaled(60))
                for v in group:
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.text(v.name)
                    if imgui.is_item_hovered():
                        imgui.set_tooltip(f"{'Declared (saved with the game)' if v.declared else 'Set in the script'}"
                                          f", changed {v.changes} time(s) in the scripts")
                    imgui.table_next_column()
                    imgui.text_disabled("?" if v.start is None else repr(v.start))
                    imgui.table_next_column()
                    imgui.set_next_item_width(-1)
                    changed, value = imgui.input_text(f"###cs_v_{v.name}", state["values"].get(v.name, v.suggested), 32)
                    if changed:
                        state["values"][v.name] = value.strip()
                    imgui.table_next_column()
                    value = state["values"].get(v.name, v.suggested)
                    if value and imgui.small_button(f"Copy###cs_c_{v.name}"):
                        copy(f"{v.name} = {value}", f"{v.name} = {value}")
                imgui.end_table()
        imgui.end_child()

    utils.push_popup(utils.popup, f"Cheat sheet - {game.name}", content, buttons=True, closable=True, outside=False,
                     popup_uuid=f"cheatsheet_{game.id}")
