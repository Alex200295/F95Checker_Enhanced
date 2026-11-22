"""
PIN lock (Library tools > Settings > PIN lock).

    F95Checker asks for your PIN when it starts, after X minutes without using the computer, and (option)
    when it is minimised (the discretion key minimises it). While locked nothing of your library is drawn.
    Options: a neutral window title, and no live preview of the window (taskbar / Alt+Tab thumbnails) and
    no capture of it (screenshots, screen sharing, recordings show it black).
    The PIN is stored hashed (PBKDF2-SHA256, random salt) in <F95Checker data>/local_scan_prefs.json.
    Forgotten PIN: close F95Checker, open that file and delete "lock_hash".
"""
import contextlib
import hashlib
import hmac
import os
import time

from common.structs import (
    Os,
)
from modules import (
    globals,
    icons,
    installdate,
)

_locked: bool | None = None
_entry = ""
_message = ""
_fails = 0
_wait_until = 0.0
_redraw = False
_window_applied: tuple | None = None
_last_activity = time.monotonic()


def _log(msg: str):
    print(f"[Lock] {msg}")


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("lock_hash", "")
    p.setdefault("lock_salt", "")
    p.setdefault("lock_idle_min", 10)          # 0 = never
    p.setdefault("lock_on_minimise", True)
    p.setdefault("lock_title", "")             # "" = F95Checker
    p.setdefault("lock_no_preview", False)     # off by default: your own screenshots of F95Checker would be black
    return p


def enabled() -> bool:
    return bool(prefs()["lock_hash"])


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def set_pin(pin: str) -> str:
    """'' or why the PIN is refused."""
    pin = pin.strip()
    if len(pin) < 4:
        return "At least 4 characters."
    p = prefs()
    p["lock_salt"] = os.urandom(16).hex()
    p["lock_hash"] = _hash(pin, p["lock_salt"])
    installdate.save_prefs()
    _log("PIN set")
    return ""


def clear_pin():
    global _locked
    p = prefs()
    p["lock_hash"], p["lock_salt"] = "", ""
    installdate.save_prefs()
    _locked = False
    _log("PIN removed")


def check(pin: str) -> bool:
    p = prefs()
    if not p["lock_hash"]:
        return True
    return hmac.compare_digest(_hash(pin, p["lock_salt"]), p["lock_hash"])


def is_locked() -> bool:
    global _locked
    if _locked is None:
        _locked = enabled()          # locked when F95Checker starts
    return _locked and enabled()


def lock_now():
    global _locked, _entry, _message, _redraw
    if enabled():
        _locked, _entry, _message, _redraw = True, "", "", True


def try_unlock(pin: str) -> bool:
    global _locked, _fails, _wait_until, _message, _entry, _redraw, _last_activity
    if time.monotonic() < _wait_until:
        return False
    _entry = ""
    if check(pin):
        _locked, _fails, _message, _redraw = False, 0, "", True
        _last_activity = time.monotonic()
        return True
    _fails += 1
    if _fails >= 5:
        _wait_until = time.monotonic() + 30
        _fails = 0
        _message = "Wrong PIN 5 times: wait 30 seconds."
    else:
        _message = "Wrong PIN."
    return False


def _system_idle_seconds() -> float:
    """Seconds since the last keyboard / mouse input anywhere in Windows (else: in F95Checker)."""
    if globals.os is Os.Windows:
        with contextlib.suppress(Exception):
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000
    return time.monotonic() - _last_activity


def note_activity():
    global _last_activity
    _last_activity = time.monotonic()


def tick(gui) -> bool:
    """Every loop of the GUI (even while not drawing). Locks on idle / minimise. True = redraw needed."""
    global _redraw
    apply_window(gui)
    if enabled() and not is_locked():
        p = prefs()
        idle_min = float(p["lock_idle_min"] or 0)
        if idle_min and _system_idle_seconds() >= idle_min * 60:
            lock_now()
            _log("Locked after inactivity")
        elif p["lock_on_minimise"] and (getattr(gui, "minimized", False) or getattr(gui, "hidden", False)):
            lock_now()
            _log("Locked (minimised)")
    if _redraw:
        _redraw = False
        return True
    return False


def apply_window(gui):
    """Window title and capture / preview protection, applied when the settings change."""
    global _window_applied
    p = prefs()
    wanted = (p["lock_title"].strip(), bool(p["lock_no_preview"]))
    if wanted == _window_applied or gui is None or getattr(gui, "window", None) is None:
        return
    _window_applied = wanted
    with contextlib.suppress(Exception):
        import glfw
        glfw.set_window_title(gui.window, wanted[0] or "F95Checker")
    if globals.os is not Os.Windows:
        return
    with contextlib.suppress(Exception):
        import ctypes
        import glfw
        hwnd = glfw.get_win32_window(gui.window)
        on = ctypes.c_int(1 if wanted[1] else 0)
        # DWMWA_FORCE_ICONIC_REPRESENTATION (7) and DWMWA_DISALLOW_PEEK (11): no live thumbnail / peek
        for attribute in (7, 11):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on))
        # WDA_EXCLUDEFROMCAPTURE (0x11): black in screenshots, recordings and screen sharing; WDA_NONE (0)
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11 if wanted[1] else 0)


def draw_lock_screen(gui, size):
    """Drawn instead of the whole interface while locked."""
    import imgui
    global _entry
    width = gui.scaled(320)
    imgui.set_cursor_pos(((size.x - width) / 2, size.y / 2 - gui.scaled(90)))
    imgui.begin_group()
    imgui.push_font(imgui.fonts.big)
    imgui.text(f"{icons.lock} Locked")
    imgui.pop_font()
    imgui.spacing()
    imgui.set_next_item_width(width)
    if imgui.is_window_appearing() or not imgui.is_any_item_active():
        imgui.set_keyboard_focus_here()
    entered, _entry = imgui.input_text("###lock_pin", _entry, 64, imgui.INPUT_TEXT_PASSWORD | imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    if (imgui.button("Unlock###lock_go", width=width) or entered) and _entry:
        try_unlock(_entry)
    waiting = _wait_until - time.monotonic()
    if waiting > 0:
        imgui.text_colored(f"Wait {int(waiting) + 1} s", 1.0, 0.5, 0.3)
    elif _message:
        imgui.text_colored(_message, 1.0, 0.5, 0.3)
    imgui.end_group()


def draw_settings(draw_settings_label, right_width: float):
    """Rows of Library tools > Settings."""
    import imgui
    p = prefs()
    state = draw_settings.__dict__.setdefault("state", {"new": "", "msg": ""})
    draw_settings_label("PIN lock:", "Asks for a PIN when F95Checker starts, after inactivity and (option) when minimised.\n"
                                     "Forgotten PIN: close F95Checker, delete \"lock_hash\" in local_scan_prefs.json.")
    imgui.set_next_item_width(right_width * 0.5)
    _, state["new"] = imgui.input_text("###lock_new", state["new"], 64, imgui.INPUT_TEXT_PASSWORD)
    imgui.same_line()
    if imgui.button("Change PIN###lock_set" if enabled() else "Set PIN###lock_set"):
        state["msg"] = set_pin(state["new"]) or "PIN saved."
        state["new"] = ""
    if enabled():
        imgui.same_line()
        if imgui.button("Remove###lock_clear"):
            clear_pin()
            state["msg"] = "PIN removed."
    if state["msg"]:
        imgui.text_disabled(state["msg"])
    if not enabled():
        return
    draw_settings_label("Lock after:", "Minutes without using the computer (keyboard / mouse anywhere). 0 = never.")
    imgui.set_next_item_width(right_width * 0.5)
    changed, value = imgui.slider_int("minutes###lock_idle", int(p["lock_idle_min"]), 0, 120)
    if changed:
        p["lock_idle_min"] = value
        installdate.save_prefs()
    draw_settings_label("Lock when minimised:", "Also when the discretion key minimises F95Checker.")
    changed, value = imgui.checkbox("###lock_min", bool(p["lock_on_minimise"]))
    if changed:
        p["lock_on_minimise"] = value
        installdate.save_prefs()
    draw_settings_label("Window title:", "Name shown in the taskbar and Alt+Tab (empty = F95Checker).")
    imgui.set_next_item_width(right_width * 0.5)
    changed, value = imgui.input_text("###lock_title", p["lock_title"], 60)
    if changed:
        p["lock_title"] = value
        installdate.save_prefs()
    draw_settings_label("No preview, no capture:", "Taskbar / Alt+Tab show the icon instead of a live thumbnail; screenshots,\n"
                                                   "recordings and screen sharing show the window black.")
    changed, value = imgui.checkbox("###lock_prev", bool(p["lock_no_preview"]))
    if changed:
        p["lock_no_preview"] = value
        installdate.save_prefs()
