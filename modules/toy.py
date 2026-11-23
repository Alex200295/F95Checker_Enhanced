"""
Connected toy (Intiface Central / Buttplug protocol v3): Lovense Max 2 and any toy Intiface supports.

Sync modes, chosen per game ("Auto" picks the best one):
 - Native  : the game drives the toy itself (it ships Buttplug / Intiface support). F95Checker only
             starts Intiface Central and lets the game connect (one client at a time).
 - B Ren'Py: a tiny script added to the game reports which animation is playing and where it is in
             its loop; F95Checker plays the matching funscript (generated from the animation by
             toygen.py, or a downloaded one which always wins).
 - C Rumble: Unity / Unreal games with gamepad rumble: F95Checker starts Intiface Game Haptics
             Router (GHR), which forwards the rumble to the toy. GHR is an Intiface client too, so
             F95Checker disconnects while it runs.
 - A Audio : any game: the sound of the PC (loopback) drives the toy (volume, beats, intensity).

Safety, always on:
 - max intensity caps (vibration, air) applied after everything else;
 - EMERGENCY STOP: Pause key or Ctrl+Alt+End anywhere in Windows, or the red button in F95Checker.
   It latches: nothing moves again until you press Resume;
 - stop when the game closes, when the game stops reporting (crash / freeze), when the game window
   is not in front (optional), after a maximum session time;
 - soft start: output ramps up over a few seconds after each start / pause;
 - sync is opt-in per game; quitting F95Checker stops the toy.

Lovense Max 2 in Intiface: "Vibrate" (20 steps) + "Constrict" (air pump, few steps, slow). Any
actuator named Vibrate / Oscillate is driven by the vibration track, Constrict / Inflate by the air one.
Strokers (The Handy, Kiiroo Keon...: Intiface "LinearCmd") follow the funscript positions themselves,
one move per script action, sent just before it starts; without a script (or a flat one) they do a
plain up-and-down whose speed follows the vibration level. Several toys at once are fine.
Data: <F95Checker data>/toy.json, toy_patterns/*.json, toy_scripts/<game id>/*.funscript.
"""
import asyncio
import bisect
import concurrent.futures
import contextlib
import dataclasses
import json
import math
import os
import pathlib
import random
import re
import subprocess
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
    toygen,
    utils,
)

LOG_PREFIX = "[Toy]"


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


# ---------------------------------------------------------------------------
# Settings (toy.json, kept out of the database: no schema change)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "server": "ws://127.0.0.1:12345",
    "enable_by_default": True,      # games never set by hand get toy sync (Ren'Py: sync script added after each scan)
    "auto_connect": True,           # connect to Intiface when F95Checker starts
    "auto_intiface": True,          # start Intiface Central when a toy-enabled game is launched
    "intiface_path": "",
    "ghr_path": "",
    "ffmpeg_path": "",
    "max_vibe": 0.70,               # caps, 0..1 of the toy's maximum
    "max_air": 0.60,
    "soft_start_s": 4.0,
    "stroke_min": 0.0,              # strokers: lowest / highest position used, 0..1 of the device's length
    "stroke_max": 1.0,
    "max_stroke_speed": 3.0,        # strokers: lengths per second at most (3 = a full stroke in 0.33 s)
    "stroke_lead_ms": 0,            # strokers: send each move this much earlier (Bluetooth delay)
    "pause_unfocused": True,
    "idle_stop_s": 15,              # mode B: stop when the game sends nothing for this long
    "session_max_min": 90,
    "hotkeys": True,
    "audio_sensitivity": 1.0,
    "audio_threshold": 0.08,
    "audio_air": True,
    "games": {},
}
DEFAULT_GAME = {
    "enabled": None,                # True / False set by hand, None = follow "enable_by_default"
    "mode": "auto",                 # auto / native / B / C / A / off
    "intensity": 1.0,               # 0.2 .. 2.0
    "offset_ms": 0,                 # -1000 .. +1000, + = toy later
    "air": 1.0,                     # air pressure multiplier 0 .. 1.5
    "floor": 0.0,                   # minimum vibration while an animation plays 0 .. 0.4
    "invert": False,                # stroke direction (changes where the pulses fall)
    "overrides": {},                # {video key: pattern name or "off"}
}
MODES = ["auto", "native", "B", "C", "A", "off"]
MODE_LABELS = {
    "auto": "Auto",
    "native": "Native (game drives the toy)",
    "B": "B - Ren'Py animation sync",
    "C": "C - Rumble (Game Haptics Router)",
    "A": "A - Audio reactive",
    "off": "Off",
}

_settings: dict | None = None


def settings() -> dict:
    global _settings
    if _settings is None:
        data = {}
        try:
            data = json.loads((globals.data_path / "toy.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        _settings = {**DEFAULT_SETTINGS, **(data if isinstance(data, dict) else {})}
        if not isinstance(_settings.get("games"), dict):
            _settings["games"] = {}
        if isinstance(data, dict) and data and "enable_by_default" not in data:
            # toy.json of the opt-in version: "enabled": false was only the old default, never a choice -> follow the new default
            for cfg in _settings["games"].values():
                if isinstance(cfg, dict) and cfg.get("enabled") is False:
                    cfg["enabled"] = None
            _log("Toy sync now on by default (toy.json converted)")
    return _settings


def is_enabled(game_id: int) -> bool:
    """Toy sync on for this game: its own checkbox if you set it, else "enable_by_default"."""
    cfg = settings()["games"].get(str(game_id))
    value = cfg.get("enabled") if isinstance(cfg, dict) else None
    return bool(settings()["enable_by_default"]) if value is None else bool(value)


def save_settings():
    try:
        tmp = globals.data_path / "toy.json.tmp"
        tmp.write_text(json.dumps(settings(), indent=1), encoding="utf-8")
        os.replace(tmp, globals.data_path / "toy.json")
    except OSError as exc:
        _log(f"Cannot save toy.json: {exc}")


def game_cfg(game_id: int) -> dict:
    """Per-game settings (defaults filled in). Changes must be followed by save_settings()."""
    games = settings()["games"]
    cfg = games.get(str(game_id))
    if not isinstance(cfg, dict):
        cfg = games[str(game_id)] = {}
    for key, value in DEFAULT_GAME.items():
        if key not in cfg:
            cfg[key] = json.loads(json.dumps(value))
    return cfg


def scripts_dir(game_id: int) -> pathlib.Path:
    return globals.data_path / "toy_scripts" / str(game_id)


def patterns_dir() -> pathlib.Path:
    return globals.data_path / "toy_patterns"


# ---------------------------------------------------------------------------
# Buttplug v3 client (Intiface Central websocket)
# ---------------------------------------------------------------------------

VIBE_ACTUATORS = ("Vibrate", "Oscillate")
AIR_ACTUATORS = ("Constrict", "Inflate")


@dataclasses.dataclass(slots=True)
class Feature:
    index: int
    steps: int
    actuator: str


@dataclasses.dataclass(slots=True)
class Device:
    index: int
    name: str
    vibe: list[Feature]
    air: list[Feature]
    linear: list[Feature] = dataclasses.field(default_factory=list)   # strokers (LinearCmd: position + duration)
    sent: dict = dataclasses.field(default_factory=dict)   # feature index -> last step sent
    air_sent_at: float = 0.0


class ButtplugClient:
    AIR_MIN_INTERVAL = 1.5          # s between two air pressure changes: the pump is slow, don't hammer it

    def __init__(self):
        self.session = None
        self.ws = None
        self.devices: dict[int, Device] = {}
        self.server_name = ""
        self.error = ""
        self.connected = False
        self._id = 0
        self._waiters: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._id = self._id % 4_000_000_000 + 1
        return self._id

    async def _send(self, kind: str, wait=False, **fields):
        if not self.ws or self.ws.closed:
            raise ConnectionError("not connected")
        msg_id = self._next_id()
        future = None
        if wait:
            future = asyncio.get_running_loop().create_future()
            self._waiters[msg_id] = future
        await self.ws.send_str(json.dumps([{kind: {"Id": msg_id, **fields}}]))
        if future:
            try:
                return await asyncio.wait_for(future, 5)
            finally:
                self._waiters.pop(msg_id, None)

    async def connect(self, url: str):
        import aiohttp
        await self.disconnect()
        self.error = ""
        try:
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(url, heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=3), proxy=None)
            self._tasks = [asyncio.create_task(self._reader())]
            info = await self._send("RequestServerInfo", wait=True, ClientName="F95Checker", MessageVersion=3)
            info = info.get("ServerInfo", {}) if isinstance(info, dict) else {}
            self.server_name = info.get("ServerName", "Intiface")
            ping = int(info.get("MaxPingTime") or 0)
            if ping > 0:
                self._tasks.append(asyncio.create_task(self._pinger(ping / 1000 / 2)))
            self.connected = True
            devices = await self._send("RequestDeviceList", wait=True)
            for dev in (devices or {}).get("DeviceList", {}).get("Devices", []):
                self._add_device(dev)
            await self.scan()
            _log(f"Connected to {self.server_name} ({url}), {len(self.devices)} device(s)")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            _log(f"Connection to {url} failed: {self.error}")
            await self.disconnect(keep_error=True)
            raise

    async def scan(self, seconds: float = 10):
        try:
            await self._send("StartScanning")
        except Exception:
            return

        async def stop_later():
            await asyncio.sleep(seconds)
            with contextlib.suppress(Exception):
                await self._send("StopScanning")
        self._tasks.append(asyncio.create_task(stop_later()))

    async def disconnect(self, keep_error=False):
        self.connected = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.close()
        self.ws = self.session = None
        self.devices.clear()
        if not keep_error:
            self.error = ""

    def _add_device(self, dev: dict):
        vibe, air = [], []
        for i, attr in enumerate(dev.get("DeviceMessages", {}).get("ScalarCmd", []) or []):
            actuator = attr.get("ActuatorType", "")
            feature = Feature(i, max(1, int(attr.get("StepCount") or 1)), actuator)
            if actuator in VIBE_ACTUATORS:
                vibe.append(feature)
            elif actuator in AIR_ACTUATORS:
                air.append(feature)
        linear = [Feature(i, max(1, int(attr.get("StepCount") or 1)), attr.get("ActuatorType", "Position"))
                  for i, attr in enumerate(dev.get("DeviceMessages", {}).get("LinearCmd", []) or [])]
        index = int(dev.get("DeviceIndex", 0))
        name = dev.get("DeviceDisplayName") or dev.get("DeviceName") or f"Device {index}"
        self.devices[index] = Device(index, name, vibe, air, linear)
        _log(f"Device: {name} (vibration x{len(vibe)} {[f.steps for f in vibe]}, air x{len(air)} {[f.steps for f in air]}, "
             f"stroke x{len(linear)})")

    async def _reader(self):
        import aiohttp
        try:
            async for raw in self.ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    messages = json.loads(raw.data)
                except ValueError:
                    continue
                for message in messages:
                    for kind, body in message.items():
                        msg_id = body.get("Id", 0)
                        if msg_id in self._waiters and not self._waiters[msg_id].done():
                            self._waiters[msg_id].set_result({kind: body})
                        if kind == "DeviceAdded":
                            self._add_device(body)
                        elif kind == "DeviceRemoved":
                            self.devices.pop(int(body.get("DeviceIndex", -1)), None)
                        elif kind == "Error":
                            self.error = body.get("ErrorMessage", "error")
                            _log(f"Intiface error: {self.error}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
        finally:
            if self.connected:
                _log("Disconnected from Intiface")
            self.connected = False

    async def _pinger(self, every: float):
        while True:
            await asyncio.sleep(max(every, 0.2))
            with contextlib.suppress(Exception):
                await self._send("Ping")

    async def send_levels(self, vibe: float, air: float | None, force=False):
        """vibe / air in 0..1. Sends only what changed (quantised to each motor's steps)."""
        if not self.connected:
            return
        async with self._lock:
            now = time.monotonic()
            for dev in list(self.devices.values()):
                scalars = []
                for feature in dev.vibe:
                    step = round(max(0.0, min(1.0, vibe)) * feature.steps)
                    if force or dev.sent.get(feature.index) != step:
                        dev.sent[feature.index] = step
                        scalars.append({"Index": feature.index, "Scalar": step / feature.steps, "ActuatorType": feature.actuator})
                if air is not None and dev.air and (force or now - dev.air_sent_at >= self.AIR_MIN_INTERVAL):
                    changed = False
                    for feature in dev.air:
                        step = round(max(0.0, min(1.0, air)) * feature.steps)
                        if force or dev.sent.get(feature.index) != step:
                            dev.sent[feature.index] = step
                            scalars.append({"Index": feature.index, "Scalar": step / feature.steps, "ActuatorType": feature.actuator})
                            changed = True
                    if changed:
                        dev.air_sent_at = now
                if scalars:
                    with contextlib.suppress(Exception):
                        await self._send("ScalarCmd", DeviceIndex=dev.index, Scalars=scalars)

    def has_linear(self) -> bool:
        return any(dev.linear for dev in self.devices.values())

    async def send_linear(self, position: float, duration_ms: int):
        """Strokers: go to position (0 = bottom, 1 = top) in duration_ms."""
        if not self.connected:
            return
        position = max(0.0, min(1.0, position))
        duration_ms = max(1, int(duration_ms))
        async with self._lock:
            for dev in list(self.devices.values()):
                if dev.linear:
                    vectors = [{"Index": f.index, "Duration": duration_ms, "Position": round(position, 4)} for f in dev.linear]
                    with contextlib.suppress(Exception):
                        await self._send("LinearCmd", DeviceIndex=dev.index, Vectors=vectors)

    async def stop_all(self):
        for dev in self.devices.values():
            dev.sent.clear()
            dev.air_sent_at = 0.0
        with contextlib.suppress(Exception):
            await self._send("StopAllDevices")


# ---------------------------------------------------------------------------
# Patterns (templates): keyframes [ms, vibration 0..1, air 0..1], linear in between
# ---------------------------------------------------------------------------

def _wave(period_ms, lo, hi, air, cycles=1, shape="sine"):
    keys = []
    steps = 8
    for c in range(cycles):
        for i in range(steps):
            phase = i / steps
            v = (0.5 - 0.5 * math.cos(2 * math.pi * phase)) if shape == "sine" else (phase * 2 if phase < 0.5 else 2 - phase * 2)
            keys.append([int(c * period_ms + phase * period_ms), round(lo + (hi - lo) * v, 3), air])
    keys.append([cycles * period_ms, lo, air])
    return keys


def _builtin_patterns() -> dict[str, dict]:
    rnd = random.Random(95)
    tease = []
    t = 0
    for _ in range(14):
        dur = rnd.randint(900, 2600)
        tease += [[t, 0.0, 0.0], [t + dur // 3, round(rnd.uniform(0.15, 0.45), 2), 0.0], [t + dur, 0.0, 0.0]]
        t += dur + rnd.randint(200, 1600)
    build = []
    for i in range(61):                       # 60 s: waves getting faster and stronger, air rising
        t = i * 1000
        level = i / 60
        build.append([t, round(0.15 + 0.6 * level * (0.75 + 0.25 * math.sin(i * (1 + level * 3))), 3), round(0.7 * level, 2)])
    edging = []
    t = 0
    for rep in range(3):
        for i in range(25):
            edging.append([t + i * 1000, round(0.2 + 0.65 * i / 24, 3), round(0.6 * i / 24, 2)])
        t += 25_000
        edging += [[t, 0.05, 0.0], [t + 8000, 0.05, 0.0]]
        t += 9000
    return {
        "Tease": {"loop": True, "desc": "Random light touches with pauses.", "keys": tease},
        "Slow waves": {"loop": True, "desc": "4 s sine waves, 10-40 %. Foreplay, kissing.", "keys": _wave(4000, 0.1, 0.4, 0.0)},
        "Heartbeat": {"loop": True, "desc": "Double pulse, 70 bpm.", "keys": [
            [0, 0.0, 0.1], [60, 0.55, 0.1], [180, 0.05, 0.1], [300, 0.4, 0.1], [420, 0.0, 0.1], [857, 0.0, 0.1]]},
        "Blowjob": {"loop": True, "desc": "1.6 s strokes, soft at the base, stronger at the tip, light air pulses.", "keys": [
            [0, 0.15, 0.2], [500, 0.55, 0.45], [800, 0.6, 0.45], [1300, 0.2, 0.2], [1600, 0.15, 0.2]]},
        "Riding": {"loop": True, "desc": "1.1 s strokes, deep and heavy, air holding.", "keys": [
            [0, 0.3, 0.5], [350, 0.85, 0.6], [550, 0.9, 0.6], [1100, 0.3, 0.5]]},
        "Doggy fast": {"loop": True, "desc": "0.5 s hard strokes.", "keys": [
            [0, 0.35, 0.6], [150, 0.95, 0.7], [250, 0.9, 0.7], [500, 0.35, 0.6]]},
        "Build-up 60 s": {"loop": False, "desc": "Waves getting faster and stronger for a minute, air rising.", "keys": build},
        "Edging": {"loop": True, "desc": "25 s climb to 85 %, 8 s near stop, 3 times.", "keys": edging},
        "Orgasm": {"loop": False, "desc": "Climb to 100 % (capped by your max), hold 8 s, then decay.", "keys": [
            [0, 0.6, 0.6], [3000, 0.9, 0.9], [4000, 1.0, 1.0], [12000, 1.0, 1.0], [14000, 0.6, 0.6], [18000, 0.25, 0.2], [24000, 0.0, 0.0]]},
        "Afterglow": {"loop": False, "desc": "Gentle 20 s fade out.", "keys": [[0, 0.25, 0.2], [20000, 0.0, 0.0]]},
        "Generic loop": {"loop": True, "desc": "Used while a script is being generated.", "keys": _wave(1400, 0.2, 0.5, 0.3)},
    }


BUILTIN_PATTERNS = _builtin_patterns()
_user_patterns: dict[str, dict] | None = None


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\- ]+", "_", name).strip()[:60] or "pattern"


def patterns() -> dict[str, dict]:
    """Built-in patterns + yours (toy_patterns/*.json, yours win on the same name)."""
    global _user_patterns
    if _user_patterns is None:
        _user_patterns = {}
        folder = patterns_dir()
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                keys = [[int(k[0]), float(k[1]), float(k[2])] for k in data["keys"]]
                if keys:
                    _user_patterns[data.get("name") or path.stem] = {"loop": bool(data.get("loop", True)), "desc": data.get("desc", ""), "keys": keys, "user": True}
            except Exception as exc:
                _log(f"Pattern {path.name} ignored: {exc}")
    return {**BUILTIN_PATTERNS, **_user_patterns}


def save_pattern(name: str, pattern: dict):
    global _user_patterns
    folder = patterns_dir()
    folder.mkdir(parents=True, exist_ok=True)
    keys = sorted(([int(k[0]), round(float(k[1]), 3), round(float(k[2]), 3)] for k in pattern["keys"]), key=lambda k: k[0])
    (folder / f"{_safe_name(name)}.json").write_text(json.dumps(
        {"name": name, "loop": bool(pattern.get("loop", True)), "desc": pattern.get("desc", ""), "keys": keys}, indent=1), encoding="utf-8")
    _user_patterns = None


def delete_pattern(name: str):
    global _user_patterns
    with contextlib.suppress(OSError):
        (patterns_dir() / f"{_safe_name(name)}.json").unlink()
    _user_patterns = None


def pattern_duration(pattern: dict) -> int:
    return max(1, pattern["keys"][-1][0]) if pattern.get("keys") else 1


def pattern_value(pattern: dict, t_ms: float) -> tuple[float, float, bool]:
    """(vibration, air, finished)."""
    keys = pattern["keys"]
    if not keys:
        return 0.0, 0.0, True
    duration = pattern_duration(pattern)
    finished = False
    if pattern.get("loop", True):
        t_ms %= duration
    elif t_ms >= duration:
        return keys[-1][1], keys[-1][2], True
    times = [k[0] for k in keys]
    i = bisect.bisect_right(times, t_ms)
    if i <= 0:
        return keys[0][1], keys[0][2], finished
    if i >= len(keys):
        return keys[-1][1], keys[-1][2], finished
    a, b = keys[i - 1], keys[i]
    f = (t_ms - a[0]) / max(1, b[0] - a[0])
    return a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, finished


# ---------------------------------------------------------------------------
# Script (funscript) playback helpers
# ---------------------------------------------------------------------------

STROKE_FULL_SPEED = 2.5             # range / s for 100 % vibration (a full stroke in 0.4 s)


def script_value(script: toygen.Script, t_ms: float, invert: bool) -> tuple[float, float]:
    """(position 0..1, speed in range/s) at t_ms, looping over the script duration."""
    times, pos = script.times, script.pos
    if len(times) == 1:
        p = pos[0]
        return (1 - p if invert else p), 0.0
    duration = max(script.duration_ms, times[-1], 1)    # a last action AT the end = the loop point
    t = t_ms % duration
    i = bisect.bisect_right(times, t)
    if i == 0:
        ta, pa, tb, pb = times[-1] - duration, pos[-1], times[0], pos[0]
    elif i >= len(times):
        ta, pa, tb, pb = times[-1], pos[-1], times[0] + duration, pos[0]
    else:
        ta, pa, tb, pb = times[i - 1], pos[i - 1], times[i], pos[i]
    span = max(1.0, tb - ta)
    p = pa + (pb - pa) * (t - ta) / span
    speed = abs(pb - pa) / (span / 1000)
    return (1 - p if invert else p), speed


def script_air(script: toygen.Script, t_ms: float) -> float | None:
    if not script.air:
        return None
    t = t_ms % max(1, script.duration_ms)
    level = script.air[0][1]
    for at, value in script.air:
        if at > t:
            break
        level = value
    return level


def script_is_flat(script: toygen.Script) -> bool:
    """No real movement (a still frame analysed as an animation): strokers do the plain up-and-down instead."""
    return max(script.pos) - min(script.pos) < 0.05


def script_segment(script: toygen.Script, t_ms: float, invert: bool) -> tuple[int, float, float]:
    """Next move of a stroker following the script at t_ms (looping):
    (segment number, target position 0..1, ms left to reach it). The segment number changes at each action."""
    times, pos = script.times, script.pos
    if len(times) == 1:
        p = pos[0]
        return 0, (1 - p if invert else p), 0.0
    duration = max(script.duration_ms, times[-1], 1)
    loop_n, t = divmod(t_ms, duration)
    i = bisect.bisect_right(times, t)
    if i >= len(times):                       # last action passed: next is the first one of the next loop
        target_t, target, seg = times[0] + duration, pos[0], (int(loop_n) + 1) * len(times)
    else:
        target_t, target, seg = times[i], pos[i], int(loop_n) * len(times) + i
    return seg, (1 - target if invert else target), target_t - t


@dataclasses.dataclass(slots=True)
class StrokeState:
    seg: object = None              # last script segment sent, "level" while doing the plain up-and-down
    up: bool = False
    next_at: float = 0.0
    last_pos: float = 0.5


# ---------------------------------------------------------------------------
# Audio (mode A): system sound loopback
# ---------------------------------------------------------------------------

class AudioReactor:
    RATE = 48000
    BLOCK = 2400                    # 50 ms

    def __init__(self):
        self.vibe = 0.0
        self.air = 0.0
        self.error = ""
        self.running = False
        self._thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="toy-audio", daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self.vibe = self.air = 0.0

    def _run(self):
        try:
            import numpy as np
            import soundcard as sc
        except Exception as exc:
            self.error = f"Audio mode needs the 'soundcard' and 'numpy' Python packages ({exc})"
            self.running = False
            return
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except Exception as exc:
            self.error = f"No loopback device: {exc}"
            self.running = False
            return
        env = floor = 0.0
        peak = 1e-3
        slow = 0.0
        dt = self.BLOCK / self.RATE
        freqs = np.fft.rfftfreq(self.BLOCK, 1 / self.RATE)
        band = (freqs >= 120) & (freqs <= 4000)           # voices, impacts, moans; not sub-bass music rumble
        window = np.hanning(self.BLOCK)
        try:
            with mic.recorder(samplerate=self.RATE, channels=2, blocksize=self.BLOCK) as rec:
                while self.running:
                    data = rec.record(numframes=self.BLOCK)
                    mono = data.mean(axis=1) if data.ndim == 2 else data
                    if mono.size < self.BLOCK:
                        mono = np.pad(mono, (0, self.BLOCK - mono.size))
                    spec = np.abs(np.fft.rfft(mono[:self.BLOCK] * window))
                    level = float(np.sqrt((spec[band] ** 2).mean()) / self.BLOCK * 40)
                    # envelope: fast attack 30 ms, release 300 ms
                    coef = 1 - math.exp(-dt / (0.03 if level > env else 0.3))
                    env += (level - env) * coef
                    # adaptive range: floor follows quiet parts slowly, peak decays over ~8 s
                    floor = env if env < floor or floor == 0 else floor + (env - floor) * (dt / 20)
                    peak = max(env, peak * math.exp(-dt / 8), floor + 1e-3)
                    cfg = settings()
                    norm = (env - floor) / max(peak - floor, 1e-6)
                    norm = 0.0 if norm < cfg["audio_threshold"] else norm
                    vibe = min(1.0, (norm ** 0.8) * cfg["audio_sensitivity"])
                    slow += (vibe - slow) * (1 - math.exp(-dt / 5))
                    self.vibe = vibe
                    self.air = slow if cfg["audio_air"] else 0.0
        except Exception as exc:
            self.error = f"Audio capture stopped: {exc}"
        finally:
            self.running = False
            self.vibe = self.air = 0.0


# ---------------------------------------------------------------------------
# Windows helpers: foreground window, global hotkeys
# ---------------------------------------------------------------------------

def _foreground_pid() -> int | None:
    if globals.os is not Os.Windows:
        return None
    try:
        import ctypes
        import ctypes.wintypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def _process_tree(pid: int) -> set[int]:
    import psutil
    pids = {pid}
    with contextlib.suppress(psutil.Error):
        pids |= {child.pid for child in psutil.Process(pid).children(recursive=True)}
    return pids


_hotkey_thread_id = None


def _hotkey_loop():
    global _hotkey_thread_id
    try:
        import ctypes
        import ctypes.wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        _hotkey_thread_id = kernel32.GetCurrentThreadId()
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
        VK_PAUSE, VK_END = 0x13, 0x23
        registered = [
            user32.RegisterHotKey(None, 0x95A, MOD_NOREPEAT, VK_PAUSE),
            user32.RegisterHotKey(None, 0x95B, MOD_NOREPEAT | MOD_ALT | MOD_CONTROL, VK_END),
        ]
        if not any(registered):
            _log("Emergency stop hotkeys could not be registered (used by another program?)")
            return
        _log(f"Emergency stop hotkeys: {'Pause ' if registered[0] else ''}{'Ctrl+Alt+End' if registered[1] else ''}")
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:   # WM_HOTKEY
                emergency_stop("hotkey")
        user32.UnregisterHotKey(None, 0x95A)
        user32.UnregisterHotKey(None, 0x95B)
    except Exception as exc:
        _log(f"Hotkey thread error: {exc}")


def _start_hotkeys():
    if globals.os is Os.Windows and settings()["hotkeys"]:
        threading.Thread(target=_hotkey_loop, name="toy-hotkeys", daemon=True).start()


def _stop_hotkeys():
    if _hotkey_thread_id and globals.os is Os.Windows:
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(_hotkey_thread_id, 0x0012, 0, 0)   # WM_QUIT


# ---------------------------------------------------------------------------
# External apps: Intiface Central, Game Haptics Router
# ---------------------------------------------------------------------------

def _program_dirs() -> list[pathlib.Path]:
    dirs = []
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "APPDATA"):
        value = os.environ.get(var)
        if value:
            dirs += [pathlib.Path(value), pathlib.Path(value) / "Programs"]
    return dirs


def _find_program(custom: str, folder_globs: tuple[str, ...], exe_globs: tuple[str, ...]) -> pathlib.Path | None:
    if custom and pathlib.Path(custom).is_file():
        return pathlib.Path(custom)
    for base in _program_dirs():
        for folder_glob in folder_globs:
            for folder in base.glob(folder_glob) if base.is_dir() else []:
                for exe_glob in exe_globs:
                    for exe in folder.glob(exe_glob):
                        if exe.is_file():
                            return exe
    return None


def find_intiface() -> pathlib.Path | None:
    return _find_program(settings()["intiface_path"], ("*ntiface*entral*", "*ntiface*"), ("intiface_central.exe", "*ntiface*entral*.exe"))


def find_ghr() -> pathlib.Path | None:
    return _find_program(settings()["ghr_path"], ("*aptics*outer*", "*ntiface*"), ("*aptics*outer*.exe", "*GameHaptics*.exe"))


def _running(name_part: str) -> list:
    import psutil
    out = []
    for proc in psutil.process_iter(("name",)):
        with contextlib.suppress(psutil.Error):
            if name_part in (proc.info["name"] or "").lower():
                out.append(proc)
    return out


def _start_program(exe: pathlib.Path):
    flags = 0x00000008 | 0x00000200 if globals.os is Os.Windows else 0     # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_intiface() -> str:
    """Starts Intiface Central if it is not running. Returns a status message."""
    if _running("intiface"):
        return "Intiface Central is running."
    exe = find_intiface()
    if exe is None:
        return "Intiface Central not found: install it (intiface.com/central) or set its path in the toy panel."
    try:
        _start_program(exe)
        return f"Intiface Central started ({exe})."
    except OSError as exc:
        return f"Could not start Intiface Central: {exc}"


# ---------------------------------------------------------------------------
# Native toy support detection (idea 2) and per-game mode
# ---------------------------------------------------------------------------

CODE_EXTS = (".dll", ".so", ".dylib", ".py", ".pyc", ".rpy", ".rpyc", ".js", ".json", ".cfg", ".ini", ".cs", ".lua", ".uplugin")
TEXT_EXTS = (".rpy", ".py", ".js", ".json", ".cfg", ".ini", ".uplugin")
NAME_RE = re.compile(r"buttplug|intiface|lovense", re.IGNORECASE)
TEXT_RES = {
    "buttplug": re.compile(rb"buttplug\.io|buttplug_?client|import buttplug|intiface|ws://(?:localhost|127\.0\.0\.1):12345", re.IGNORECASE),
    "lovense": re.compile(rb"lovense\.club|api\.lovense|lovense_?(?:sdk|api|toy|connect|remote|integration)", re.IGNORECASE),
}
_native_cache: dict[int, tuple[str, str, str]] = {}      # game id -> (folder, kind, reason)
_native_pending: set[int] = set()
_native_loaded = False
# One game at a time, in the background: checking the 200+ games at once (Toy column shown or sorted) made
# F95Checker crawl for minutes and held up the Refresh (all threads fighting for Python).
_native_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="toy-native")


def _native_file() -> pathlib.Path:
    return globals.data_path / "toy_native.json"


def _native_load():
    """Results of the previous sessions (toy_native.json): a game is checked again only when its folder changes."""
    global _native_loaded
    if _native_loaded:
        return
    _native_loaded = True
    try:
        data = json.loads(_native_file().read_text(encoding="utf-8"))
        for game_id, value in data.items():
            if isinstance(value, list) and len(value) == 3 and int(game_id) not in _native_cache:
                _native_cache[int(game_id)] = (str(value[0]), str(value[1]), str(value[2]))
    except (OSError, ValueError):
        pass


def _native_save():
    try:
        tmp = _native_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): list(v) for k, v in _native_cache.items()}), encoding="utf-8")
        os.replace(tmp, _native_file())
    except OSError as exc:
        _log(f"Cannot save {_native_file().name}: {exc}")


def _kind_of_name(name: str) -> str:
    low = name.lower()
    return "lovense" if "lovense" in low else "buttplug"


def detect_native(game: Game) -> tuple[str, str]:
    """(kind, reason): kind '' / 'buttplug' (Intiface) / 'lovense' (official Lovense SDK, Lovense Remote app).
    Looks at code / library / plugin names and small scripts only: an image called "buttplug.webp" is not support.
    Slow on big folders: GUI code uses native_cached()."""
    exe = installdate._main_exe(game)
    if exe is None:
        return "", ""
    folder = exe.parent
    _native_load()
    cached = _native_cache.get(game.id)
    if cached and cached[0] == str(folder):
        return cached[1], cached[2]
    kind = reason = ""
    try:
        for cur, dirs, files in os.walk(folder):
            rel = pathlib.Path(cur).relative_to(folder)
            if len(rel.parts) >= 5:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d.lower() not in ("saves", "cache", "__pycache__", "toy_scripts", "images", "audio", "movies", "music", "sounds")]
            for d in dirs:
                if NAME_RE.search(d):
                    kind, reason = _kind_of_name(d), f"folder {rel / d}"
                    break
            if kind:
                break
            for name in files:
                low = name.lower()
                if low.startswith(("zz_f95toy", "zz_f95checker")):
                    continue
                ext = os.path.splitext(low)[1]
                if ext in CODE_EXTS and NAME_RE.search(low):
                    kind, reason = _kind_of_name(low), f"file {rel / name}"
                    break
                if ext in TEXT_EXTS:
                    path = pathlib.Path(cur) / name
                    with contextlib.suppress(OSError):
                        if path.stat().st_size < 3_000_000:
                            data = path.read_bytes()
                            for k, regex in TEXT_RES.items():
                                if regex.search(data):
                                    kind, reason = k, f"{k} code in {rel / name}"
                                    break
                    if kind:
                        break
                if ext == ".rpa":
                    for key in toygen.rpa_index(pathlib.Path(cur) / name):
                        base = key.rsplit("/", 1)[-1]
                        if os.path.splitext(base)[1] in CODE_EXTS and NAME_RE.search(base):
                            kind, reason = _kind_of_name(base), f"{key} in {name}"
                            break
                    if kind:
                        break
            if kind:
                break
    except OSError:
        pass
    _native_cache[game.id] = (str(folder), kind, reason)
    _native_save()
    return kind, reason


def native_cached(game: Game) -> tuple[str, str] | None:
    """Cached detection result, or None while it is computed in the background."""
    exe = installdate._main_exe(game)
    _native_load()
    cached = _native_cache.get(game.id)
    if cached and exe is not None and cached[0] == str(exe.parent):
        return cached[1], cached[2]
    if exe is None:
        return "", ""
    if game.id not in _native_pending:
        _native_pending.add(game.id)

        async def compute():
            try:
                await asyncio.get_running_loop().run_in_executor(_native_pool, detect_native, game)
            finally:
                _native_pending.discard(game.id)
        with contextlib.suppress(Exception):
            async_thread.run(compute())
    return None


_engine_cache: dict[int, tuple[float, str]] = {}


def engine_of(game: Game) -> str:
    engine = installdate.local_engine(game)
    if not engine:
        # engine not recognised by the local scan: looked for again at most once a minute (Toy column, every frame)
        cached = _engine_cache.get(game.id)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        exe = installdate._main_exe(game)
        if exe is not None:
            engine = installdate.detect_engine(exe)[0]
        _engine_cache[game.id] = (time.monotonic(), engine)
    return engine


_ghr_cache: tuple[float, pathlib.Path | None] = (float("-inf"), None)


def ghr_cached() -> pathlib.Path | None:
    global _ghr_cache
    if time.monotonic() - _ghr_cache[0] > 60:
        _ghr_cache = (time.monotonic(), find_ghr())
    return _ghr_cache[1]


def resolve_mode(game: Game, gui=False) -> str:
    """native / B / C / A / off for this game (Auto resolved). gui=True never blocks (native check in background)."""
    mode = game_cfg(game.id)["mode"]
    if mode != "auto":
        return mode
    native = native_cached(game) if gui else detect_native(game)
    if native and native[0]:
        return "native"
    engine = engine_of(game)
    if engine == "Ren'Py":
        return "B"
    if engine.startswith(("Unity", "Unreal")) and ghr_cached() is not None:
        return "C"
    return "A"


_count_cache: dict[int, tuple[float, int]] = {}


def script_count(game_id: int) -> int:
    """Toy column, every frame: counted again at most once a minute (some games have 1000+ scripts;
    the generator and Import clear the cache when they add some)."""
    cached = _count_cache.get(game_id)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    count = 0
    with contextlib.suppress(OSError):
        with os.scandir(scripts_dir(game_id)) as it:
            count = sum(1 for entry in it if entry.name.endswith(".funscript"))
    _count_cache[game_id] = (time.monotonic(), count)
    return count


def toy_display(game: Game) -> str:
    """Toy column: what sync this game gets (never blocks the GUI)."""
    native = _native_cache.get(game.id)
    native_label = {"buttplug": "Native (Intiface)", "lovense": "Native (Lovense app)"}.get(native[1], "") if native else ""
    if not is_enabled(game.id):
        return native_label
    mode = resolve_mode(game, gui=True)
    label = {"native": native_label or "Native", "B": "Ren'Py sync", "C": "Rumble", "A": "Audio", "off": "Off"}.get(mode, mode)
    if mode == "B":
        label += f" ({script_count(game.id)} scripts)"
    return label


async def scan_native_all():
    """Manage > Toy: checks every installed game for built-in toy support (background)."""
    loop = asyncio.get_running_loop()
    found = []
    for game in list(globals.games.values()):
        if not game.executables:
            continue
        kind, reason = await loop.run_in_executor(None, detect_native, game)
        if kind:
            found.append(f"{game.name}: {'Intiface / Buttplug' if kind == 'buttplug' else 'Lovense app (official SDK)'} ({reason})")
    utils.push_popup(msgbox.msgbox, "Toy - games with built-in toy support",
                     ("\n".join(found) if found else "No game with built-in toy support found.") +
                     "\n\nThey show in the Toy column. Tick \"Sync with my toy\" on them (right click > Toy) so F95Checker "
                     "starts the right app when you launch them.", MsgBox.info)


# ---------------------------------------------------------------------------
# Ren'Py hook (mode B)
# ---------------------------------------------------------------------------

HOOK_FILE = "zz_f95toy.rpy"
HOOK_TEMPLATE = '''# Added by F95Checker (toy sync, right click > Toy). Delete this file to remove it.
# Sends the name and position of the animation on screen to F95Checker, on this PC only
# (http://127.0.0.1:{port}). Never blocks or changes the game; saves are not affected
# (everything is defined at init time and never reassigned, so Ren'Py does not save it).
init 999 python:
    import json as _json, threading as _threading, time as _time, os as _os
    try:
        from urllib.request import Request as _Request, urlopen as _urlopen
    except ImportError:
        from urllib2 import Request as _Request, urlopen as _urlopen

    def _f95toy_state():
        movies = []
        channels = set(["movie"])
        try:
            for key in renpy.display.video.displayable_channels.keys():
                channels.add(key[0])
        except Exception:
            pass
        for channel in channels:
            try:
                playing = renpy.music.get_playing(channel=channel)
            except Exception:
                playing = None
            if not playing:
                continue
            try:
                pos = renpy.music.get_pos(channel=channel)
            except Exception:
                pos = None
            try:
                duration = renpy.music.get_duration(channel=channel)
            except Exception:
                duration = None
            movies.append([channel, playing, pos, duration])
        menu = False
        try:
            context = renpy.game.context()
            menu = bool(getattr(context, "_menu", False) or getattr(context, "_main_menu", False))
        except Exception:
            pass
        return movies, menu

    def _f95toy_send(data):
        try:
            _urlopen(_Request("http://127.0.0.1:{port}/toy/event", data, {{"Content-Type": "application/json"}}), timeout=1).read()
            _f95toy_tick.fails = 0
        except Exception:
            _f95toy_tick.fails += 1
        _f95toy_tick.busy = False

    def _f95toy_tick():
        try:
            now = _time.time()
            if _f95toy_tick.busy or (_f95toy_tick.fails >= 5 and now - _f95toy_tick.sent < 20):
                return
            movies, menu = _f95toy_state()
            key = (tuple(m[1] for m in movies), menu)
            if key == _f95toy_tick.last and now - _f95toy_tick.sent < 0.5:
                return
            _f95toy_tick.last = key
            _f95toy_tick.sent = now
            _f95toy_tick.busy = True
            data = _json.dumps({{"game": {game_id}, "pid": _os.getpid(), "movies": movies, "menu": menu}}).encode("utf-8")
            thread = _threading.Thread(target=_f95toy_send, args=(data,))
            thread.daemon = True
            thread.start()
        except Exception:
            _f95toy_tick.busy = False

    _f95toy_tick.busy = False
    _f95toy_tick.fails = 0
    _f95toy_tick.sent = 0.0
    _f95toy_tick.last = None
    config.periodic_callbacks.append(_f95toy_tick)
'''


def _hook_path(game: Game) -> pathlib.Path | None:
    exe = installdate._main_exe(game)
    if exe is None or not (exe.parent / "game").is_dir() or not (exe.parent / "renpy").is_dir():
        return None
    return exe.parent / "game" / HOOK_FILE


def hook_installed(game: Game) -> bool:
    path = _hook_path(game)
    return path is not None and path.is_file()


def install_hook(game: Game) -> str:
    path = _hook_path(game)
    if path is None:
        return "not a Ren'Py game (or executable not found)"
    content = HOOK_TEMPLATE.format(port=globals.rpc_port, game_id=int(game.id))
    try:
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            path.with_suffix(".rpyc").unlink(missing_ok=True)
        return ""
    except OSError as exc:
        return str(exc)


def remove_hook(game: Game) -> str:
    path = _hook_path(game)
    if path is None:
        return ""
    try:
        path.unlink(missing_ok=True)
        path.with_suffix(".rpyc").unlink(missing_ok=True)
        return ""
    except OSError as exc:
        return str(exc)


def game_dir_of(game: Game) -> pathlib.Path | None:
    exe = installdate._main_exe(game)
    if exe is None or not (exe.parent / "game").is_dir():
        return None
    return exe.parent / "game"


def find_script_file(game: Game, key: str) -> pathlib.Path | None:
    """Your scripts first (downloaded / hand made), then the generated one."""
    name = toygen.script_name(key)
    stem = key.rsplit(".", 1)[0]
    candidates = []
    game_dir = game_dir_of(game)
    if game_dir is not None:
        candidates += [
            game_dir / (stem + ".funscript"),                 # next to a loose video
            game_dir / "toy_scripts" / name,
            game_dir / "toy_scripts" / (pathlib.PurePosixPath(stem).name + ".funscript"),
        ]
    candidates += [
        scripts_dir(game.id) / "user" / name,
        scripts_dir(game.id) / "user" / (pathlib.PurePosixPath(stem).name + ".funscript"),
        scripts_dir(game.id) / name,
    ]
    for path in candidates:
        with contextlib.suppress(OSError):
            if path.is_file():
                return path
    return None


# ---------------------------------------------------------------------------
# Script generation jobs
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class GenJob:
    game_id: int                # 0 = every Ren'Py game (generate_all_scripts)
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str = ""
    running: bool = True
    cancel: bool = False
    message: str = ""
    games_total: int = 0        # generate_all_scripts only
    games_done: int = 0
    made: int = 0               # scripts written, all games


gen_job: GenJob | None = None
_ondemand: set[tuple[int, str]] = set()


async def generate_scripts(game: Game, regenerate=False):
    """Generates the scripts of every animation of a Ren'Py game (background, cancellable)."""
    global gen_job
    if gen_job and gen_job.running:
        return
    job = gen_job = GenJob(game.id, message="Listing animations...")
    loop = asyncio.get_running_loop()
    try:
        game_dir = game_dir_of(game)
        if game_dir is None:
            job.message = "Executable not found or not a Ren'Py game."
            return
        decoder = toygen.decoder_available(settings()["ffmpeg_path"])
        if not decoder:
            job.message = "No video decoder: install PyAV (pip install av) or ffmpeg, or set the ffmpeg path."
            return
        videos = await loop.run_in_executor(None, toygen.list_videos, game_dir)
        out = scripts_dir(game.id)
        todo = [v for v in videos if regenerate or not (out / toygen.script_name(v.key)).is_file()]
        job.total = len(todo)
        job.message = f"{len(videos)} animations, {len(todo)} to analyse ({decoder})"
        for video in todo:
            if job.cancel:
                job.message = f"Cancelled after {job.done} scripts."
                break
            job.current = video.key
            try:
                await loop.run_in_executor(None, toygen.generate, video, out, settings()["ffmpeg_path"])
            except Exception as exc:
                job.failed += 1
                _log(f"Script for {video.key} failed: {exc}")
            job.done += 1
        else:
            job.message = f"Done: {job.done - job.failed} scripts made, {job.failed} failed."
        engine.scripts.clear()
    finally:
        job.running = False
        job.current = ""


def gen_workers() -> int:
    """Animations analysed at once by "Generate for all games": half the CPU threads (the GUI stays smooth), 1..6."""
    return max(1, min(6, (os.cpu_count() or 2) // 2))


async def generate_all_scripts():
    """Scripts of every Ren'Py game with toy sync on (Ren'Py animation sync mode): only the missing ones,
    several animations at once. Cancel / restart at will: what is done stays done."""
    global gen_job
    if gen_job and gen_job.running:
        return
    job = gen_job = GenJob(0, message="Looking for Ren'Py games...")
    loop = asyncio.get_running_loop()
    ffmpeg = settings()["ffmpeg_path"]
    pool = concurrent.futures.ThreadPoolExecutor(gen_workers(), thread_name_prefix="toygen")
    try:
        decoder = toygen.decoder_available(ffmpeg)
        if not decoder:
            job.message = "No video decoder: install PyAV (pip install av) or ffmpeg, or set the ffmpeg path."
            return
        games = sorted((g for g in globals.games.values() if is_enabled(g.id) and engine_of(g) == "Ren'Py"),
                       key=lambda g: g.name.lower())
        job.games_total = len(games)
        _log(f"Scripts for all games: {len(games)} Ren'Py game(s), {gen_workers()} at once ({decoder})")
        for game in games:
            if job.cancel:
                break
            job.current, job.done, job.total = game.name, 0, 0
            try:
                game_dir = game_dir_of(game)
                mode = await loop.run_in_executor(None, resolve_mode, game) if game_dir is not None else ""
                if mode != "B":
                    continue
                videos = await loop.run_in_executor(None, toygen.list_videos, game_dir)
                out = scripts_dir(game.id)
                todo = [v for v in videos if not (out / toygen.script_name(v.key)).is_file()]
                job.total = len(todo)
                futures = [loop.run_in_executor(pool, toygen.generate, video, out, ffmpeg) for video in todo]
                for future in asyncio.as_completed(futures):
                    try:
                        await future
                        job.made += 1
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        job.failed += 1
                        _log(f"Script for {game.name} failed: {exc}")
                    job.done += 1
                    if job.cancel:
                        for pending in futures:
                            pending.cancel()
                        break
                engine.scripts.clear()
                _count_cache.pop(game.id, None)
            except Exception as exc:
                _log(f"Scripts for {game.name}: {exc!r}")
            finally:
                job.games_done += 1
        verb = "Cancelled" if job.cancel else "Done"
        job.message = f"{verb}: {job.made} scripts made, {job.failed} failed, {job.games_done}/{job.games_total} games."
        _log(job.message)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        job.running = False
        job.current = ""


def ensure_hooks(games=None) -> int:
    """After each local scan (blocking, run it in a thread): the sync script in every toy-enabled Ren'Py game
    in mode B, so it also works when the game is started from Explorer or a new version folder. Returns how many were added."""
    added = 0
    for game in list(games or globals.games.values()):
        try:
            if not is_enabled(game.id) or engine_of(game) != "Ren'Py" or hook_installed(game):
                continue
            mode = game_cfg(game.id)["mode"] if str(game.id) in settings()["games"] else "auto"
            if _hook_path(game) is None:
                continue
            if mode == "auto":
                cached = _native_cache.get(game.id)
                exe = installdate._main_exe(game)
                if cached and exe is not None and cached[0] == str(exe.parent):
                    kind = cached[1]
                else:
                    kind = detect_native(game)[0]
                mode = "native" if kind else "B"
            if mode == "B" and not install_hook(game):
                added += 1
        except Exception as exc:
            _log(f"ensure_hooks {game.name}: {exc!r}")
    if added:
        _log(f"Sync script added to {added} Ren'Py game(s)")
    return added


async def _generate_one(game: Game, key: str):
    loop = asyncio.get_running_loop()
    try:
        game_dir = game_dir_of(game)
        if game_dir is None:
            return
        videos = await loop.run_in_executor(None, toygen.list_videos, game_dir)
        video = next((v for v in videos if v.key == key), None)
        if video is None:
            _log(f"Animation {key} not found in the game files")
            return
        await loop.run_in_executor(None, toygen.generate, video, scripts_dir(game.id), settings()["ffmpeg_path"])
        engine.scripts.pop((game.id, key), None)
        _log(f"Script generated for {key}")
    except Exception as exc:
        _log(f"On-demand script for {key} failed: {exc}")
    finally:
        _ondemand.discard((game.id, key))


# ---------------------------------------------------------------------------
# Engine: sources, safety, output
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class HookState:
    received: float = 0.0
    movies: list = dataclasses.field(default_factory=list)
    menu: bool = False
    pid: int = 0
    first_seen: dict = dataclasses.field(default_factory=dict)   # file -> monotonic time it appeared


class Engine:
    TICK = 0.05

    def __init__(self):
        self.client = ButtplugClient()
        self.audio = AudioReactor()
        self.estopped = False
        self.source = ""                 # "" / "pattern" / "B" / "A" / "C" / "native"
        self.game: Game | None = None
        self.game_seen_running = False
        self.pattern_name = ""
        self.pattern_started = 0.0
        self.hook: dict[int, HookState] = {}
        self.scripts: dict[tuple[int, str], toygen.Script | None] = {}
        self.vibe = 0.0                  # last output, 0..1 after caps
        self.air = 0.0
        self.status = "Idle"
        self.current_video = ""
        self.paused_reason = ""
        self.active_since = 0.0
        self.ramp_start = 0.0
        self.zero_since = time.monotonic()
        self.speed_avg = 0.0
        self.speed_short = 0.0
        self.stroke = None               # what strokers follow this tick: ("script", script, t_ms, invert) / ("level", 0..1) / None
        self.stroke_state = StrokeState()
        self.live_test: tuple[float, float] | None = None   # panel sliders
        self.ghr_process = None
        self.resume_client_after = False
        self.suppress: set[int] = set()      # games stopped by hand: their hook events don't restart the sync
        self._task = None
        self._connecting = False

    # --- connection -------------------------------------------------------
    async def connect(self):
        if self._connecting:
            return
        self._connecting = True
        try:
            await self.client.connect(settings()["server"])
        except Exception:
            pass
        finally:
            self._connecting = False

    async def connect_retry(self, seconds: float = 25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.client.connected:
            await self.connect()
            if not self.client.connected:
                await asyncio.sleep(2)

    # --- control ------------------------------------------------------------
    def ensure_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def play_pattern(self, name: str):
        self.estopped_check()
        self.source = "pattern"
        self.game = None
        self.pattern_name = name
        self.pattern_started = time.monotonic()
        self._activate()

    async def stop(self, reason="Stopped", manual=False):
        if manual and self.game is not None:
            self.suppress.add(self.game.id)
        if self.source == "A":
            self.audio.stop()
        if self.source == "C":
            self._kill_ghr()
        was_external = self.source in ("C", "native")
        self.source = ""
        self.game = None
        self.pattern_name = ""
        self.current_video = ""
        self.live_test = None
        self.status = reason
        self.vibe = self.air = 0.0
        self.stroke, self.stroke_state = None, StrokeState()
        await self.client.stop_all()
        if was_external and self.resume_client_after:
            self.resume_client_after = False
            asyncio.create_task(self.connect_retry(15))

    def estopped_check(self):
        if self.estopped:
            raise RuntimeError("Emergency stop is active: press Resume first")

    def _activate(self):
        self.active_since = time.monotonic()
        self.ramp_start = time.monotonic()
        self.ensure_loop()

    async def start_game(self, game: Game, mode: str):
        if self.estopped:
            self.status = "Emergency stop active: game sync not started"
            return
        if self.source:
            await self.stop("Switched")
        self.game = game
        self.game_seen_running = False
        self.source = mode
        self.status = f"{game.name}: {MODE_LABELS.get(mode, mode)}"
        if mode == "A":
            self.audio.start()
        self._activate()

    # --- the loop -------------------------------------------------------------
    async def _loop(self):
        last_send = 0.0
        smooth = 0.0
        while True:
            await asyncio.sleep(self.TICK)
            now = time.monotonic()
            vibe, air = 0.0, None
            try:
                self.stroke = None
                if self.estopped:
                    vibe, air = 0.0, 0.0
                elif self.live_test is not None:
                    vibe, air = self.live_test
                    self.stroke = ("level", vibe)
                elif self.source:
                    vibe, air = await self._source_value(now)
                vibe = max(0.0, min(1.0, vibe))
                # smoothing: fast attack, slower release (no buzzing at the end of strokes)
                coef = 1 - math.exp(-self.TICK / (0.04 if vibe > smooth else 0.12))
                smooth = vibe if self.estopped else smooth + (vibe - smooth) * coef
                out = smooth
                # soft start after a stop / pause
                if out <= 0.01:
                    if self.zero_since == 0.0:
                        self.zero_since = now
                elif self.zero_since and now - self.zero_since > 2.0:
                    self.ramp_start = now
                    self.zero_since = 0.0
                else:
                    self.zero_since = 0.0
                ramp = min(1.0, 0.3 + 0.7 * (now - self.ramp_start) / max(0.1, settings()["soft_start_s"]))
                cfg = settings()
                out = min(out * ramp, 1.0) * cfg["max_vibe"]
                air_out = None if air is None else max(0.0, min(1.0, air)) * cfg["max_air"] * ramp
                # session limit
                if self.source and self.active_since and now - self.active_since > cfg["session_max_min"] * 60:
                    await self.stop(f"Stopped: session limit ({cfg['session_max_min']} min) reached")
                    continue
                if out < 0.02:
                    out = 0.0
                if now - last_send >= 0.1 or (out == 0.0 and self.vibe != 0.0):
                    await self.client.send_levels(out, air_out)
                    last_send = now
                await self._drive_strokers(now, None if self.estopped else self.stroke, ramp)
                self.vibe, self.air = out, (air_out if air_out is not None else self.air)
                if not self.source and self.live_test is None and out == 0.0 and smooth < 0.005:
                    await self.client.send_levels(0.0, 0.0, force=True)
                    self.vibe = self.air = 0.0
                    self._task = None
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(f"Engine error: {exc!r}")
                await asyncio.sleep(0.5)

    def _game_pids(self) -> set[int]:
        pids = set()
        game = self.game
        if game is not None and game.launch_process is not None:
            with contextlib.suppress(Exception):
                pids |= _process_tree(game.launch_process.pid)
        if game is not None and (state := self.hook.get(game.id)) and state.pid:
            pids.add(state.pid)
        return pids

    async def _source_value(self, now: float) -> tuple[float, float | None]:
        game = self.game
        cfg = settings()
        # --- pattern test -----------------------------------------------------
        if self.source == "pattern":
            pattern = patterns().get(self.pattern_name)
            if pattern is None:
                await self.stop("Pattern not found")
                return 0.0, 0.0
            vibe, air, finished = pattern_value(pattern, (now - self.pattern_started) * 1000)
            if finished:
                await self.stop(f"Pattern {self.pattern_name} finished")
                return 0.0, 0.0
            self.stroke = ("level", vibe)
            return vibe, air
        if game is None:
            return 0.0, 0.0
        # --- game closed ------------------------------------------------------
        if game.launch_state in (LaunchState.Starting, LaunchState.Playing):
            self.game_seen_running = True
        elif self.game_seen_running:
            await self.stop(f"{game.name} closed")
            return 0.0, 0.0
        if self.source in ("C", "native"):
            # the toy is driven by GHR / the game: nothing to output, but watch the game to clean up
            if not self.game_seen_running and now - self.active_since > 120:
                await self.stop(f"{game.name} not seen running")
            return 0.0, None
        # --- focus --------------------------------------------------------------
        if cfg["pause_unfocused"] and globals.os is Os.Windows:
            fg = _foreground_pid()
            pids = self._game_pids()
            if fg and pids and fg not in pids and fg != os.getpid():
                self.paused_reason = "game window not in front"
                return 0.0, 0.0
        self.paused_reason = ""
        gcfg = game_cfg(game.id)
        # --- audio --------------------------------------------------------------
        if self.source == "A":
            if not self.audio.running and self.audio.error:
                self.status = self.audio.error
            self.stroke = ("level", self.audio.vibe * gcfg["intensity"])
            return self.audio.vibe * gcfg["intensity"], self.audio.air * gcfg["air"]
        # --- Ren'Py sync ----------------------------------------------------------
        state = self.hook.get(game.id)
        silent = now - (state.received if state is not None else self.active_since)
        if state is None or silent > cfg["idle_stop_s"]:
            self.current_video = ""
            self.paused_reason = "waiting for the game" if state is None else "game not reporting"
            # launched outside F95Checker (no process to watch): the hook going silent = game closed / frozen
            if not self.game_seen_running and silent > max(cfg["idle_stop_s"], 120 if state is None else 0):
                await self.stop(f"{game.name} stopped reporting")
            return 0.0, 0.0
        if state.menu or not state.movies:
            self.current_video = ""
            self.paused_reason = "menu" if state.menu else "no animation on screen"
            return 0.0, 0.0
        channel, filename, pos, duration = max(state.movies, key=lambda m: state.first_seen.get(m[1], 0.0))
        key = toygen.video_key(filename)
        self.current_video = key
        override = gcfg["overrides"].get(key)
        if override == "off":
            return 0.0, 0.0
        elapsed_ms = (now - state.received) * 1000
        if pos is not None:
            t = float(pos) * 1000 + elapsed_ms
        else:
            t = (now - state.first_seen.get(filename, now)) * 1000
        t -= gcfg["offset_ms"]
        if override and override in patterns():
            vibe, air, _ = pattern_value(patterns()[override], t)
            self.stroke = ("level", vibe * gcfg["intensity"])
            return vibe * gcfg["intensity"], air * gcfg["air"]
        script = self._script(game, key)
        if script is None:
            if (game.id, key) not in _ondemand and toygen.decoder_available(cfg["ffmpeg_path"]):
                _ondemand.add((game.id, key))
                asyncio.create_task(_generate_one(game, key))
            vibe, air, _ = pattern_value(BUILTIN_PATTERNS["Generic loop"], t)
            self.stroke = ("level", vibe * gcfg["intensity"] * 0.8)
            return vibe * gcfg["intensity"] * 0.8, air * gcfg["air"]
        if script_is_flat(script):
            generic, _, _ = pattern_value(BUILTIN_PATTERNS["Generic loop"], t)
            self.stroke = ("level", generic * gcfg["intensity"] * 0.8)
        else:
            self.stroke = ("script", script, t + float(cfg["stroke_lead_ms"]), gcfg["invert"])
        position, speed = script_value(script, t, gcfg["invert"])
        speed_level = min(1.0, speed / STROKE_FULL_SPEED)
        # stroke intensity = speed averaged over ~1 stroke (instant speed is 0 at every turn)
        self.speed_short += (speed_level - self.speed_short) * (1 - math.exp(-self.TICK / 0.6))
        self.speed_avg += (speed_level - self.speed_avg) * (1 - math.exp(-self.TICK / 3.0))
        level = min(1.0, self.speed_short * 1.25) ** 0.7
        # one pulse per stroke, strongest when fully in (funscript convention: 0 = bottom / deep)
        vibe = level * (0.25 + 0.75 * (1.0 - position))
        vibe = gcfg["floor"] + (1 - gcfg["floor"]) * vibe
        air = script_air(script, t)
        if air is None:
            air = self.speed_avg
        return vibe * gcfg["intensity"], air * gcfg["air"]

    async def _drive_strokers(self, now: float, stroke, ramp: float):
        """Strokers: one move per script action (target + time to reach it), or a plain up-and-down."""
        c = self.client
        if not c.connected or not c.has_linear():
            return
        st = self.stroke_state
        if stroke is None:
            st.seg = None                    # nothing sent: the device ends its move and stays there
            return
        cfg = settings()
        if stroke[0] == "script":
            _, script, t, invert = stroke
            seg, target, duration = script_segment(script, t, invert)
            if seg == st.seg:
                return
            st.seg = seg
        else:
            level = max(0.0, min(1.0, float(stroke[1])))
            if level < 0.05:
                st.seg = None
                return
            if st.seg == "level" and now < st.next_at:
                return
            half = (1.6 - 1.1 * level) / 2              # 1.6 s strokes when soft .. 0.5 s at full level
            depth = 0.4 + 0.6 * level
            st.up = not st.up
            target = 0.5 + (depth if st.up else -depth) / 2
            duration = half * 1000
            st.seg, st.next_at = "level", now + half
        lo, hi = sorted((float(cfg["stroke_min"]), float(cfg["stroke_max"])))
        hi = max(hi, min(1.0, lo + 0.05))
        target = 0.5 + (target - 0.5) * max(0.0, min(1.0, ramp))   # soft start: short strokes around the middle first
        position = lo + (hi - lo) * target
        # safety: never faster than max_stroke_speed (the move takes longer instead)
        duration = max(duration, abs(position - st.last_pos) / max(0.2, float(cfg["max_stroke_speed"])) * 1000, 20)
        st.last_pos = position
        await c.send_linear(position, int(duration))

    def _script(self, game: Game, key: str) -> toygen.Script | None:
        cache_key = (game.id, key)
        if cache_key not in self.scripts or self.scripts[cache_key] is None:
            path = find_script_file(game, key)
            self.scripts[cache_key] = toygen.load_script(path) if path else None
        return self.scripts[cache_key]

    def _kill_ghr(self):
        """Closing Game Haptics Router disconnects it from Intiface, which then stops the toy."""
        proc, self.ghr_process = self.ghr_process, None
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
        if self.source == "C":
            for running in _running("haptics"):      # also when it was already open before the game
                with contextlib.suppress(Exception):
                    running.terminate()


engine = Engine()


# ---------------------------------------------------------------------------
# Public entry points (thread safe)
# ---------------------------------------------------------------------------

def emergency_stop(origin="button"):
    """Latching stop: works from any thread. Nothing moves again until resume()."""
    engine.estopped = True
    engine.live_test = None
    _log(f"EMERGENCY STOP ({origin})")

    async def _do():
        engine.audio.stop()
        engine._kill_ghr()
        await engine.client.stop_all()
        await asyncio.sleep(0.2)
        await engine.client.stop_all()
        native = engine.source == "native"
        await engine.stop("EMERGENCY STOP - press Resume to use the toy again" +
                          (" (this game drives the toy itself: use its own stop or close it)" if native else ""))
    with contextlib.suppress(Exception):
        async_thread.run(_do())


def resume():
    engine.estopped = False
    engine.status = "Ready"


def on_hook_event(data: dict):
    """From the RPC thread: a Ren'Py game with the hook reports what it shows."""
    try:
        game_id = int(data.get("game"))
    except (TypeError, ValueError):
        return
    game = globals.games.get(game_id)
    if game is None:
        return
    now = time.monotonic()
    state = engine.hook.setdefault(game_id, HookState())
    movies = []
    for movie in data.get("movies") or []:
        if isinstance(movie, (list, tuple)) and len(movie) >= 2 and isinstance(movie[1], str):
            pos = movie[2] if len(movie) > 2 and isinstance(movie[2], (int, float)) else None
            duration = movie[3] if len(movie) > 3 and isinstance(movie[3], (int, float)) else None
            movies.append((str(movie[0]), movie[1], pos, duration))
    names = {m[1] for m in movies}
    state.first_seen = {name: state.first_seen.get(name, now) for name in names}
    state.movies = movies
    state.menu = bool(data.get("menu"))
    state.pid = int(data.get("pid") or 0)
    state.received = now
    if is_enabled(game_id) and not engine.estopped and game_id not in engine.suppress \
            and (engine.game is None or engine.game.id != game_id) and resolve_mode(game, gui=True) == "B":
        # game started outside F95Checker (or F95Checker restarted while playing)
        async_thread.run(engine.start_game(game, "B"))


def set_enabled(game: Game, enabled: bool):
    """Opt-in checkbox. Ren'Py games get the sync script right away (it must be there when the game starts)."""
    cfg = game_cfg(game.id)
    cfg["enabled"] = enabled
    save_settings()
    if enabled:
        engine.suppress.discard(game.id)
        if game_cfg(game.id)["mode"] in ("auto", "B") and engine_of(game) == "Ren'Py":
            async def _install():
                mode = await asyncio.get_running_loop().run_in_executor(None, resolve_mode, game)
                if mode == "B":
                    error = install_hook(game)
                    if error:
                        utils.push_popup(msgbox.msgbox, "Toy", f"Could not add the sync script to {game.name}:\n{error}", MsgBox.warn)
            async_thread.run(_install())
    else:
        remove_hook(game)
        if engine.game is not None and engine.game.id == game.id:
            async_thread.run(engine.stop("Sync disabled for this game"))


async def before_launch(game: Game):
    """callbacks._launch_game_exe, BEFORE starting the game: the Ren'Py sync script must exist when Ren'Py loads."""
    if not is_enabled(game.id):
        return
    try:
        mode = await asyncio.get_running_loop().run_in_executor(None, resolve_mode, game)
        if mode == "B":
            error = install_hook(game)
            if error:
                _log(f"Sync script not added to {game.name}: {error}")
    except Exception as exc:
        _log(f"before_launch: {exc!r}")


async def _on_game_launched(game: Game):
    if not is_enabled(game.id):
        return
    engine.suppress.discard(game.id)
    mode = await asyncio.get_running_loop().run_in_executor(None, resolve_mode, game)
    if mode == "off":
        return
    s = settings()
    if mode in ("native", "C"):
        kind = detect_native(game)[0] if mode == "native" else ""
        if kind == "lovense":
            utils.push_popup(msgbox.msgbox, "Toy - Lovense game",
                             f"{game.name} drives Lovense toys itself through the official Lovense app.\n\n"
                             " 1. Open Lovense Remote on the PC (or phone), connect the Max 2, enable Game Mode;\n"
                             " 2. In the game, enter the code / IP shown by Lovense Remote if asked.\n\n"
                             "F95Checker disconnects from Intiface. If Lovense Remote cannot see the toy,\n"
                             "quit Intiface Central (it may still hold the Bluetooth connection).", MsgBox.info)
        elif s["auto_intiface"]:
            _log(ensure_intiface())
        if engine.client.connected:
            await engine.client.disconnect()        # Intiface accepts one client: leave it to the game / GHR
            engine.resume_client_after = True
        await engine.start_game(game, mode)
        if mode == "C":
            exe = find_ghr()
            if exe is None:
                utils.push_popup(msgbox.msgbox, "Toy", "Game Haptics Router not found: install it from intiface.com/ghr "
                                 "or set its path in the toy panel. Falling back to nothing for this session.", MsgBox.warn)
            else:
                if not _running("haptics"):
                    with contextlib.suppress(OSError):
                        engine.ghr_process = _start_program(exe)
                utils.push_popup(msgbox.msgbox, "Toy - Rumble mode",
                                 f"{game.name} is starting with Intiface Game Haptics Router.\n\n"
                                 "In Game Haptics Router:\n"
                                 " 1. Intiface tab: connect, then tick your toy;\n"
                                 " 2. Processes tab: select the game's process, then Attach To Process\n"
                                 "    (if it offers several hook types: XInput first, the Unity one otherwise);\n"
                                 " 3. Use a gamepad in the game: rumble now goes to the toy.\n\n"
                                 "Emergency stop (Pause key / Ctrl+Alt+End) closes Game Haptics Router: the toy stops.",
                                 MsgBox.info)
        return
    if s["auto_intiface"] and not engine.client.connected:
        _log(ensure_intiface())
    if not engine.client.connected:
        await engine.connect_retry(25)
    if mode == "B":
        if not hook_installed(game):
            error = install_hook(game)
            utils.push_popup(msgbox.msgbox, "Toy", (f"Could not add the sync script to {game.name}:\n{error}" if error else
                             f"The sync script was just added to {game.name}: restart the game once so Ren'Py loads it."),
                             MsgBox.warn)
        if not globals.settings.rpc_enabled:
            utils.push_popup(msgbox.msgbox, "Toy", "Ren'Py sync needs the F95Checker local server: enable "
                             "sidebar > Extension > RPC enabled (used by the browser extension too).", MsgBox.warn)
    await engine.start_game(game, mode)


def on_game_launched(game: Game):
    """Called by callbacks._launch_game_exe after a successful launch."""
    with contextlib.suppress(Exception):
        async_thread.run(_on_game_launched(game))


@contextlib.contextmanager
def setup():
    """main.py: connect at start, hotkeys, and ALWAYS stop the toy when F95Checker quits."""
    try:
        _start_hotkeys()
        if settings()["auto_connect"]:
            async_thread.run(engine.connect())
    except Exception as exc:
        _log(f"Setup error: {exc}")
    try:
        yield
    finally:
        _stop_hotkeys()
        engine.audio.stop()
        engine._kill_ghr()
        with contextlib.suppress(Exception):
            future = async_thread.run(engine.client.stop_all())
            future.result(timeout=2)
        with contextlib.suppress(Exception):
            async_thread.run(engine.client.disconnect()).result(timeout=2)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _status_line() -> str:
    c = engine.client
    if engine.estopped:
        return "EMERGENCY STOP active"
    if c.connected:
        devices = ", ".join(d.name for d in c.devices.values()) or "no toy found yet (turn it on)"
        link = f"Connected: {devices}"
    elif engine.source in ("C", "native"):
        link = "Intiface left to the game / Game Haptics Router"
    else:
        link = f"Not connected{(' - ' + c.error) if c.error else ''}"
    return link


def draw_sidebar_stop(width: float):
    """Big red STOP at the top of the sidebar while the toy may move."""
    import imgui
    if not (engine.source or engine.vibe > 0 or engine.estopped or engine.live_test):
        return
    if engine.estopped:
        imgui.push_style_color(imgui.COLOR_BUTTON, 0.85, 0.55, 0.10)
        if imgui.button(f"{icons.play} Toy stopped - Resume###toy_resume", width=-1, height=imgui.get_frame_height() * 1.5):
            resume()
        imgui.pop_style_color()
        return
    imgui.push_style_color(imgui.COLOR_BUTTON, 0.80, 0.10, 0.10)
    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.95, 0.15, 0.15)
    imgui.push_style_color(imgui.COLOR_TEXT, 1, 1, 1)
    if imgui.button(f"{icons.stop} STOP TOY  (Pause key)###toy_stop", width=-1, height=imgui.get_frame_height() * 1.6):
        emergency_stop("button")
    imgui.pop_style_color(3)
    imgui.text_disabled(f"{int(engine.vibe * 100)}% vib  {int(engine.air * 100)}% air  {engine.paused_reason or engine.current_video[-28:]}")


def draw_context_menu(game: Game):
    import imgui
    if game is None:
        return
    if imgui.begin_menu(f"{icons.vibrate} Toy"):
        cfg = game_cfg(game.id)
        changed, value = imgui.checkbox("Sync with my toy###toy_enabled", is_enabled(game.id))
        if changed:
            set_enabled(game, value)
        mode = resolve_mode(game, gui=True)
        imgui.text_disabled(f"Mode: {MODE_LABELS.get(cfg['mode'])}{' -> ' + MODE_LABELS.get(mode) if cfg['mode'] == 'auto' else ''}")
        native = native_cached(game)
        if native is None:
            imgui.text_disabled("Checking the game files for built-in toy support...")
        elif native[0]:
            imgui.text_disabled(f"Built-in toy support ({'Intiface' if native[0] == 'buttplug' else 'Lovense app'}): {native[1]}")
        if imgui.selectable(f"{icons.tune} Toy settings for this game...", False)[0]:
            open_panel(game)
        if mode == "B" and engine_of(game) == "Ren'Py" and not (gen_job and gen_job.running):
            if imgui.selectable(f"{icons.movie_open_cog_outline} Generate scripts from the animations", False)[0]:
                async_thread.run(generate_scripts(game))
                open_panel(game)
        if hook_installed(game) and imgui.selectable(f"{icons.delete_outline} Remove sync script from the game", False)[0]:
            error = remove_hook(game)
            utils.push_popup(msgbox.msgbox, "Toy", error or "Sync script removed.", MsgBox.warn if error else MsgBox.info)
        imgui.end_menu()


_panel_state = {"section": 0, "pattern": "Slow waves", "edit": None, "edit_name": "", "test_vibe": 0.0, "test_air": 0.0, "game": None}
SECTIONS = ["Connection & safety", "This game", "Patterns", "Help"]


def open_panel(game: Game = None):
    if game is not None:
        _panel_state["game"] = game.id
        _panel_state["section"] = 1
    utils.push_popup(utils.popup, "Toy", _draw_panel, buttons=True, closable=True, outside=False, popup_uuid="toy_panel")


def _slider(label: str, value: float, lo: float, hi: float, fmt="%.2f", width=None) -> tuple[bool, float]:
    import imgui
    if width:
        imgui.set_next_item_width(width)
    return imgui.slider_float(label, value, lo, hi, fmt)


def _draw_panel():
    import imgui
    s = settings()
    st = _panel_state
    scaled = globals.gui.scaled if globals.gui else (lambda x: x)
    imgui.text(_status_line())
    imgui.same_line()
    if engine.client.connected:
        if imgui.small_button("Disconnect"):
            async_thread.run(engine.client.disconnect())
        imgui.same_line()
        if imgui.small_button("Scan for toys"):
            async_thread.run(engine.client.scan())
    elif imgui.small_button("Connect"):
        async_thread.run(engine.connect())
    stroke_text = (f", stroke at {int(engine.stroke_state.last_pos * 100)}%"
                   if engine.client.connected and engine.client.has_linear() else "")
    imgui.text(f"Output: {int(engine.vibe * 100)}% vibration, {int(engine.air * 100)}% air{stroke_text}  -  {engine.status}"
               f"{('  (paused: ' + engine.paused_reason + ')') if engine.paused_reason else ''}")
    if engine.estopped:
        if imgui.button(f"{icons.play} Resume (emergency stop is latched)"):
            resume()
    else:
        imgui.push_style_color(imgui.COLOR_BUTTON, 0.80, 0.10, 0.10)
        if imgui.button(f"{icons.stop} EMERGENCY STOP"):
            emergency_stop("panel")
        imgui.pop_style_color()
        if engine.source:
            imgui.same_line()
            if imgui.button("Stop"):
                async_thread.run(engine.stop("Stopped by hand", manual=True))
    _draw_all_scripts_line()
    imgui.separator()
    for i, name in enumerate(SECTIONS):
        if imgui.radio_button(name, st["section"] == i):
            st["section"] = i
        imgui.same_line()
    imgui.new_line()
    imgui.separator()
    width = scaled(260)
    dirty = False

    if st["section"] == 0:
        changed, value = imgui.input_text("Intiface server###toy_server", s["server"], 200)
        if changed:
            s["server"] = value.strip()
            dirty = True
        for key, label in (("auto_connect", "Connect when F95Checker starts"),
                           ("auto_intiface", "Start Intiface Central when a toy game launches"),
                           ("pause_unfocused", "Pause when the game window is not in front"),
                           ("hotkeys", "Emergency stop hotkeys: Pause / Ctrl+Alt+End (restart F95Checker to apply)"),
                           ("enable_by_default", "Toy sync on for every game you did not set by hand (Ren'Py: sync script added at each scan)")):
            changed, value = imgui.checkbox(f"{label}###toy_{key}", s[key])
            if changed:
                s[key] = value
                dirty = True
        changed, value = _slider("Max vibration###toy_maxv", s["max_vibe"], 0.05, 1.0, "%.2f", width)
        if changed:
            s["max_vibe"], dirty = value, True
        changed, value = _slider("Max air pressure###toy_maxa", s["max_air"], 0.0, 1.0, "%.2f", width)
        if changed:
            s["max_air"], dirty = value, True
        changed, value = _slider("Soft start (s)###toy_soft", s["soft_start_s"], 0.0, 15.0, "%.1f", width)
        if changed:
            s["soft_start_s"], dirty = value, True
        imgui.text("Strokers (The Handy, Keon...):")
        changed, value = _slider("Lowest position###toy_smin", s["stroke_min"], 0.0, 0.9, "%.2f", width)
        if changed:
            s["stroke_min"], dirty = value, True
        changed, value = _slider("Highest position###toy_smax", s["stroke_max"], 0.1, 1.0, "%.2f", width)
        if changed:
            s["stroke_max"], dirty = value, True
        changed, value = _slider("Max speed (lengths/s)###toy_sspeed", s["max_stroke_speed"], 0.5, 5.0, "%.1f", width)
        if changed:
            s["max_stroke_speed"], dirty = value, True
        imgui.set_next_item_width(width)
        changed, value = imgui.slider_int("Send moves earlier (ms)###toy_slead", int(s["stroke_lead_ms"]), 0, 500)
        if changed:
            s["stroke_lead_ms"], dirty = value, True
        imgui.set_next_item_width(width)
        changed, value = imgui.slider_int("Stop if game silent (s)###toy_idle", int(s["idle_stop_s"]), 5, 120)
        if changed:
            s["idle_stop_s"], dirty = value, True
        imgui.set_next_item_width(width)
        changed, value = imgui.slider_int("Session limit (min)###toy_session", int(s["session_max_min"]), 5, 240)
        if changed:
            s["session_max_min"], dirty = value, True
        imgui.text("Audio mode (A):")
        changed, value = _slider("Sensitivity###toy_asens", s["audio_sensitivity"], 0.2, 3.0, "%.2f", width)
        if changed:
            s["audio_sensitivity"], dirty = value, True
        changed, value = _slider("Threshold###toy_athr", s["audio_threshold"], 0.0, 0.6, "%.2f", width)
        if changed:
            s["audio_threshold"], dirty = value, True
        changed, value = imgui.checkbox("Air pressure follows the overall intensity###toy_aair", s["audio_air"])
        if changed:
            s["audio_air"], dirty = value, True
        imgui.text("Programs (empty = found automatically):")
        for key, label, finder in (("intiface_path", "Intiface Central exe", find_intiface),
                                   ("ghr_path", "Game Haptics Router exe", find_ghr),
                                   ("ffmpeg_path", "ffmpeg exe (if PyAV is missing)", lambda: toygen.find_ffmpeg(s["ffmpeg_path"]))):
            imgui.set_next_item_width(scaled(380))
            changed, value = imgui.input_text(f"{label}###toy_{key}", s[key], 400)
            if changed:
                s[key], dirty = value.strip().strip('"'), True
            found = finder()
            imgui.text_disabled(f"    -> {found or 'not found'}")
        imgui.text_disabled(f"Video decoder for scripts: {toygen.decoder_available(s['ffmpeg_path']) or 'none'}")
        imgui.text("Live test (moves the toy, caps apply):")
        changed_v, st["test_vibe"] = _slider("Vibration###toy_tv", st["test_vibe"], 0.0, 1.0, "%.2f", width)
        changed_a, st["test_air"] = _slider("Air###toy_ta", st["test_air"], 0.0, 1.0, "%.2f", width)
        if changed_v or changed_a:
            if not engine.estopped:
                engine.live_test = (st["test_vibe"], st["test_air"])
                async_thread.run(_ensure_loop())
        if engine.live_test is not None:
            imgui.same_line()
            if imgui.button("End test"):
                engine.live_test = None
                st["test_vibe"] = st["test_air"] = 0.0

    elif st["section"] == 1:
        game = globals.games.get(st["game"]) if st["game"] else None
        if game is None:
            imgui.text("Right click a game > Toy > Toy settings for this game.")
        else:
            _draw_game_section(game, width)
            dirty = False

    elif st["section"] == 2:
        _draw_patterns_section(width)

    elif st["section"] == 3:
        imgui.text(
            "1. Install Intiface Central (intiface.com/central). In its settings, enable\n"
            "   \"Start server when Intiface Central launches\". Turn the Lovense Max 2 on (Bluetooth).\n"
            "2. Right click a game > Toy > tick \"Sync with my toy\". Launch the game from F95Checker.\n"
            "   - Ren'Py games: a small script is added to the game, F95Checker plays a script per animation.\n"
            "     Generate them all in advance: right click > Toy > Generate scripts (a few minutes).\n"
            "     A downloaded funscript (same name as the animation) in game\\toy_scripts always wins.\n"
            "   - Unity / Unreal: Game Haptics Router turns gamepad rumble into vibrations.\n"
            "   - Others: audio mode, the sound of the PC drives the toy.\n"
            "   - Games with their own toy support: F95Checker only starts Intiface for them.\n"
            "3. EMERGENCY STOP anytime: Pause key, Ctrl+Alt+End, or the red button (sidebar / here).\n"
            "   It stays stopped until you press Resume.\n"
            "Caps (max vibration / air) apply to everything, including patterns and tests."
        )
    if dirty:
        save_settings()


async def _ensure_loop():
    engine.ensure_loop()


def _draw_game_section(game: Game, width: float):
    import imgui
    cfg = game_cfg(game.id)
    dirty = False
    imgui.text(f"{game.name}  -  engine: {engine_of(game) or '?'}")
    native = native_cached(game)
    if native and native[0]:
        app = "Intiface" if native[0] == "buttplug" else "the Lovense Remote app (official Lovense SDK)"
        imgui.text_colored(f"Built-in toy support ({native[1]}): Auto = the game drives the toy through {app}.", 0.3, 0.85, 0.35)
    changed, value = imgui.checkbox("Sync with my toy###toy_g_en", is_enabled(game.id))
    if changed:
        set_enabled(game, value)
    imgui.set_next_item_width(width)
    changed, index = imgui.combo("Mode###toy_g_mode", MODES.index(cfg["mode"]) if cfg["mode"] in MODES else 0, [MODE_LABELS[m] for m in MODES])
    if changed:
        cfg["mode"], dirty = MODES[index], True
    if cfg["mode"] == "auto":
        imgui.same_line()
        imgui.text_disabled(f"-> {MODE_LABELS[resolve_mode(game, gui=True)]}")
    for key, label, lo, hi, fmt in (("intensity", "Intensity", 0.2, 2.0, "x%.2f"),
                                    ("air", "Air pressure", 0.0, 1.5, "x%.2f"),
                                    ("floor", "Minimum vibration during animations", 0.0, 0.4, "%.2f")):
        changed, value = _slider(f"{label}###toy_g_{key}", float(cfg[key]), lo, hi, fmt, width)
        if changed:
            cfg[key], dirty = value, True
    imgui.set_next_item_width(width)
    changed, value = imgui.slider_int("Offset ms (+ = toy later)###toy_g_off", int(cfg["offset_ms"]), -1000, 1000)
    if changed:
        cfg["offset_ms"], dirty = value, True
    changed, value = imgui.checkbox("Invert stroke direction###toy_g_inv", cfg["invert"])
    if changed:
        cfg["invert"], dirty = value, True
    if engine.game is not None and engine.game.id == game.id:
        imgui.text(f"Playing: {engine.current_video or '-'}  ({engine.paused_reason or 'active'})")
        if engine.current_video:
            names = ["(script)", "off"] + list(patterns())
            current = cfg["overrides"].get(engine.current_video, "")
            index = names.index(current) if current in names else 0
            imgui.set_next_item_width(width)
            changed, index = imgui.combo("For this animation###toy_g_ovr", index, names)
            if changed:
                if index == 0:
                    cfg["overrides"].pop(engine.current_video, None)
                else:
                    cfg["overrides"][engine.current_video] = names[index]
                dirty = True
            imgui.same_line()
            if imgui.small_button("Regenerate###toy_g_regen"):
                key = engine.current_video
                with contextlib.suppress(OSError):
                    (scripts_dir(game.id) / toygen.script_name(key)).unlink()
                engine.scripts.pop((game.id, key), None)
    elif is_enabled(game.id):
        if imgui.button("Start now (game already running)###toy_g_start"):
            engine.suppress.discard(game.id)
            async_thread.run(_on_game_launched(game))
    if engine_of(game) == "Ren'Py":
        imgui.separator()
        folder = scripts_dir(game.id)
        count = sum(1 for _ in folder.glob("*.funscript")) if folder.is_dir() else 0
        imgui.text(f"Scripts: {count} generated in {folder}")
        job = gen_job if gen_job and gen_job.game_id == game.id else None
        if job and job.running:
            imgui.text(f"Analysing {job.done}/{job.total}  {job.current[-50:]}")
            if imgui.button("Cancel###toy_gen_cancel"):
                job.cancel = True
        else:
            if job and job.message:
                imgui.text_disabled(job.message)
            if gen_job and gen_job.running:
                imgui.text_disabled("Scripts are being generated for all games (top of this window).")
            elif imgui.button("Generate missing scripts###toy_gen"):
                async_thread.run(generate_scripts(game))
            imgui.same_line()
            if imgui.button("Regenerate all###toy_regen"):
                async_thread.run(generate_scripts(game, regenerate=True))
        imgui.text_disabled("Your own funscripts: game\\toy_scripts\\<animation name>.funscript (they win over generated ones).")
        imgui.text_disabled(f"Sync script in the game: {'installed' if hook_installed(game) else 'added at next launch'}")
    if dirty:
        save_settings()


def _draw_all_scripts_line():
    """Progress of "Generate scripts for all Ren'Py games" (top of the panel, every section)."""
    import imgui
    job = gen_job
    if job is None or job.game_id != 0:
        if imgui.small_button(f"{icons.movie_open_cog_outline} Generate scripts for all Ren'Py games###toy_gen_all"):
            async_thread.run(generate_all_scripts())
        return
    if job.running:
        imgui.text(f"All games: game {min(job.games_done + 1, job.games_total)}/{job.games_total} "
                   f"({job.current[:40]}) {job.done}/{job.total}  -  {job.made} scripts made, {job.failed} failed")
        imgui.same_line()
        if imgui.small_button("Cancel###toy_gen_all_cancel"):
            job.cancel = True
    else:
        imgui.text_disabled(job.message)
        imgui.same_line()
        if imgui.small_button("Again###toy_gen_all_again"):
            async_thread.run(generate_all_scripts())


def _draw_patterns_section(width: float):
    import imgui
    st = _panel_state
    all_patterns = patterns()
    names = list(all_patterns)
    if st["pattern"] not in all_patterns:
        st["pattern"] = names[0]
    imgui.set_next_item_width(width)
    changed, index = imgui.combo("Pattern###toy_pat", names.index(st["pattern"]), names)
    if changed:
        st["pattern"] = names[index]
        st["edit"] = None
    pattern = all_patterns[st["pattern"]]
    imgui.text_disabled(f"{pattern.get('desc', '')}  ({pattern_duration(pattern) / 1000:.1f} s, {'loop' if pattern.get('loop', True) else 'once'})")
    playing = engine.source == "pattern" and engine.pattern_name == st["pattern"]
    if imgui.button("Stop###toy_pat_stop" if playing else f"{icons.play} Test on the toy###toy_pat_play"):
        if playing:
            async_thread.run(engine.stop("Pattern stopped"))
        elif not engine.estopped:
            async_thread.run(engine.play_pattern(st["pattern"]))
    imgui.same_line()
    if imgui.button("Edit a copy###toy_pat_edit"):
        st["edit"] = {"loop": pattern.get("loop", True), "desc": pattern.get("desc", ""), "keys": [list(k) for k in pattern["keys"]]}
        st["edit_name"] = st["pattern"] if pattern.get("user") else f"{st['pattern']} (mine)"
    if pattern.get("user"):
        imgui.same_line()
        if imgui.button("Delete###toy_pat_del"):
            delete_pattern(st["pattern"])
            st["pattern"] = names[0]
    # preview curve
    duration = pattern_duration(pattern)
    samples = [pattern_value(pattern, duration * i / 199)[0] for i in range(200)]
    imgui.plot_lines("###toy_pat_curve", _float_array(samples), scale_min=0.0, scale_max=1.0, graph_size=(width * 1.6, 60))
    edit = st["edit"]
    if edit is None:
        return
    imgui.separator()
    imgui.set_next_item_width(width)
    _, st["edit_name"] = imgui.input_text("Name###toy_edit_name", st["edit_name"], 60)
    _, edit["loop"] = imgui.checkbox("Loop###toy_edit_loop", edit["loop"])
    imgui.text("Keyframes: time (ms) / vibration / air")
    remove = None
    for i, key in enumerate(edit["keys"]):
        imgui.set_next_item_width(width * 0.45)
        _, key[0] = imgui.input_int(f"###toy_k_t{i}", int(key[0]), 50, 500)
        key[0] = max(0, key[0])
        imgui.same_line()
        imgui.set_next_item_width(width * 0.5)
        _, key[1] = imgui.slider_float(f"###toy_k_v{i}", float(key[1]), 0.0, 1.0, "vib %.2f")
        imgui.same_line()
        imgui.set_next_item_width(width * 0.5)
        _, key[2] = imgui.slider_float(f"###toy_k_a{i}", float(key[2]), 0.0, 1.0, "air %.2f")
        imgui.same_line()
        if imgui.small_button(f"x###toy_k_x{i}"):
            remove = i
        if len(edit["keys"]) > 40 and i == 39:
            imgui.text_disabled(f"... {len(edit['keys']) - 40} more keyframes (edit the json file)")
            break
    if remove is not None and len(edit["keys"]) > 1:
        edit["keys"].pop(remove)
    if imgui.button("+ keyframe###toy_k_add"):
        last = edit["keys"][-1] if edit["keys"] else [0, 0.0, 0.0]
        edit["keys"].append([last[0] + 500, last[1], last[2]])
    imgui.same_line()
    if imgui.button("Save###toy_edit_save"):
        name = st["edit_name"].strip() or "My pattern"
        edit["keys"].sort(key=lambda k: k[0])
        save_pattern(name, edit)
        st["pattern"] = name
        st["edit"] = None
    imgui.same_line()
    if imgui.button("Cancel###toy_edit_cancel"):
        st["edit"] = None


def _float_array(values: list[float]):
    import array
    return array.array("f", values)
