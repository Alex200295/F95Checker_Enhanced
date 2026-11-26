"""The Handy over WiFi (Handy REST API v3, handyfeeling.com), as an alternative to Intiface over Bluetooth.

Script (Ren'Py animation sync): the points of the animation's script are sent once to the Handy
(HSP, Handy Streaming Protocol), which plays them in a loop by itself, locked to the Handy server clock.
F95Checker only puts it back at the right place in the loop (synctime) every few seconds, or at once
when the game jumps. A new animation = a new script sent (a fraction of a second).
No script (gamepad rumble, audio, panel test): HAMP, the Handy strokes by itself, the level sets
speed and depth.
The caps of the toy panel apply: stroke zone (slider/stroke, opened gradually by the soft start),
max speed (moves too fast for it are shortened), emergency stop (stop sent at once, twice).

Credentials: the Handy's connection key (Handy app / onboarding) + an Application ID
(X-Api-Key, meant to be public: its use is counted on its owner's account).
Docs: https://handyverse-docs-mvkgd.ondigitalocean.app/apis/handy-rest-v3/
"""

import asyncio
import contextlib
import time

BASE_URL = "https://www.handyfeeling.com/api/handy-rest/v3"
DEFAULT_APP_ID = ""                 # F95Checker Enhanced's Application ID once there is one ("" = asked in the panel)
PORTAL_URL = "https://links.handyfeeling.com/user-portal"
MAX_POINTS_PER_ADD = 100            # API limit per /hsp/add request
SYNC_EVERY_S = 5.0                  # position in the loop corrected this often
JUMP_MS = 250                       # game ahead / behind by more than this: corrected at once
STOP_AFTER_S = 0.6                  # nothing to do for this long: stopped (a gap between two animations does not stop it)
ZONE_EVERY_S = 0.5                  # stroke zone changes (soft start) at most this often
CLOCK_REFRESH_S = 600


class HandyError(Exception):
    pass


class _Aborted(Exception):
    """A stop came in while a move was being prepared: the move is not sent."""


def _value_at_zero(times: list[int], pos: list[float], duration: int) -> float:
    """Position at t=0 of a looping script (between its last action and the first one of the next loop)."""
    if times[0] <= 0:
        return pos[0]
    ta, pa, tb, pb = times[-1] - duration, pos[-1], times[0], pos[0]
    span = max(1.0, tb - ta)
    return pa + (pb - pa) * (0 - ta) / span


def loop_points(script, invert: bool, max_speed: float, zone_len: float) -> list[dict]:
    """HSP points {t ms, x 0..100} of one loop of the script: starts at t=0, ends at the loop length with
    the same position (seamless loop). Moves faster than max_speed (zone lengths / s) are shortened."""
    times, pos = list(script.times), list(script.pos)
    duration = max(int(script.duration_ms), times[-1], 1)
    if len(times) == 1:
        raw = [(0, pos[0]), (duration, pos[0])]
    else:
        p0 = _value_at_zero(times, pos, duration)
        raw = [] if times[0] <= 0 else [(0, p0)]
        raw += list(zip(times, pos))
        if times[-1] < duration:
            raw.append((duration, p0))
    pts = []
    for t, p in raw:                                   # strictly increasing times
        t = int(t)
        if pts and t <= pts[-1][0]:
            pts[-1] = (pts[-1][0], p)
            continue
        pts.append((t, max(0.0, min(1.0, float(p)))))
    if invert:
        pts = [(t, 1.0 - p) for t, p in pts]
    # speed cap (max_speed is in zone lengths / s): each point gets as close to its target as the time since the
    # previous one allows. A second pass starts from where the first one ended, so the loop joins up without a jump.
    limit = max(0.2, float(max_speed)) / max(0.05, float(zone_len))       # in 0..1 positions per second
    targets = list(pts)
    start = targets[0][1]
    for _pass in range(2):
        cur, prev_t, out = start, targets[0][0], []
        for t, target in targets:
            step = limit * (t - prev_t) / 1000
            cur = max(cur - step, min(cur + step, target))
            out.append((t, cur))
            prev_t = t
        start = out[-1][1]
        pts = out
    return [{"t": t, "x": int(round(p * 100))} for t, p in pts]


class HandyCloud:
    """Connection to one Handy through handyfeeling.com. drive() never waits: a worker task sends the requests."""

    def __init__(self, transport=None):
        self._transport = transport          # tests: async (method, url, body, headers) -> (status, headers, data)
        self._session = None
        self.log = lambda msg: None
        self.key = ""
        self.app_id = ""
        self.connected = False
        self.error = ""
        self.fw = ""
        self.offset_ms = 0.0                 # Handy server clock - this PC's clock
        self.clock_at = 0.0
        self.mode = None                     # "hsp" / "hamp" / None (stopped)
        self.loaded = None                   # key of the script in the Handy
        self.duration = 0
        self.play_t = 0.0                    # position in the loop at play_at (monotonic): to see when the game jumps
        self.play_at = 0.0
        self.last_sync = 0.0
        self.zone = None
        self.zone_at = 0.0
        self.hamp_velocity = None
        self.hamp_at = 0.0
        self.backoff_until = 0.0
        self.want = None                     # last wish of the toy engine: (stroke, ramp, caps, monotonic)
        self.none_since = 0.0
        self.epoch = 0                       # +1 at each stop: a move being prepared is dropped
        self.requests = 0
        self._task = None
        self._lock = asyncio.Lock()

    # --- HTTP -------------------------------------------------------------------
    async def _http(self, method: str, path: str, body, headers: dict):
        if self._transport is not None:
            return await self._transport(method, BASE_URL + path, body, headers)
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        async with self._session.request(method, BASE_URL + path, json=body, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            return resp.status, dict(resp.headers), data

    async def _request(self, method: str, path: str, body=None, raw=False):
        if time.monotonic() < self.backoff_until:
            raise HandyError("too many requests: waiting a little")
        headers = {"Accept": "application/json", "X-Connection-Key": self.key, "X-Api-Key": self.app_id}
        self.requests += 1
        status, resp_headers, data = await self._http(method, path, body, headers)
        if status == 429:
            try:
                wait = float((resp_headers or {}).get("X-RateLimit-Reset", 5))   # seconds until the window resets
            except (TypeError, ValueError):
                wait = 5.0
            self.backoff_until = time.monotonic() + min(60.0, max(1.0, wait))
            raise HandyError("too many requests to handyfeeling.com: waiting a little")
        if status in (401, 403):
            raise HandyError("Application ID refused by handyfeeling.com (check it in the toy panel)")
        if isinstance(data, dict) and data.get("error"):
            err = data["error"] if isinstance(data["error"], dict) else {"name": str(data["error"])}
            if err.get("code") == 1001 or err.get("connected") is False:
                self.connected = False
                raise HandyError("The Handy went offline (WiFi): turn it on in WiFi mode, then Connect again")
            raise HandyError(f"{err.get('name', 'Error')}: {err.get('message', '')}".strip(": "))
        if status >= 400:
            raise HandyError(f"handyfeeling.com answered HTTP {status}")
        if raw:
            return data
        return data.get("result") if isinstance(data, dict) else None

    # --- clock ------------------------------------------------------------------
    async def _sync_clock(self):
        samples = []
        for _ in range(5):
            t0 = time.time() * 1000
            data = await self._request("GET", "/servertime", raw=True)
            t1 = time.time() * 1000
            with contextlib.suppress(TypeError, ValueError, AttributeError):
                samples.append((t1 - t0, float(data["server_time"]) - (t0 + t1) / 2))
        if not samples:
            raise HandyError("no clock from handyfeeling.com")
        samples.sort()
        best = samples[:3]
        self.offset_ms = sum(o for _, o in best) / len(best)
        self.clock_at = time.monotonic()

    def server_now(self) -> int:
        return int(time.time() * 1000 + self.offset_ms)

    # --- connection -------------------------------------------------------------
    async def connect(self, key: str, app_id: str = ""):
        await self.stop_now()
        self.key = (key or "").strip()
        self.app_id = (app_id or "").strip() or DEFAULT_APP_ID
        self.connected = False
        self.mode = self.loaded = self.zone = self.hamp_velocity = None
        if not self.key:
            self.error = "Enter the Handy's connection key (Handy app or handyfeeling.com)"
            return False
        if not self.app_id:
            self.error = f"An Application ID is needed (free): {PORTAL_URL}"
            return False
        try:
            result = await self._request("GET", "/connected")
            if not (isinstance(result, dict) and result.get("connected")):
                raise HandyError("The Handy is not online: turn it on in WiFi mode (pink LED), check the connection key")
            info = await self._request("GET", "/info") or {}
            if info.get("fw_status") == 2:
                raise HandyError("The Handy needs a firmware update first (Handy app or handyfeeling.com)")
            self.fw = str(info.get("fw_version", ""))
            await self._sync_clock()
        except HandyError as exc:
            self.error = str(exc)
            self.log(f"Handy WiFi: {exc}")
            return False
        except Exception as exc:
            self.error = f"Cannot reach handyfeeling.com ({exc.__class__.__name__})"
            self.log(f"Handy WiFi: {self.error}")
            return False
        self.connected = True
        self.error = ""
        self.log(f"Handy WiFi connected (firmware {self.fw}, clock offset {int(self.offset_ms)} ms)")
        return True

    async def disconnect(self):
        await self.stop_now()
        self.connected = False
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    # --- driving ----------------------------------------------------------------
    def drive(self, stroke, ramp: float, caps: dict):
        """Called by the toy engine at every tick: stroke = ("script", script, t_ms, invert) / ("level", 0..1) / None."""
        if not self.connected:
            return
        if stroke is None:
            if self.want is not None:
                self.none_since = time.monotonic()
            self.want = None
            if self.mode is None:
                return
        else:
            self.want = (stroke, float(ramp), {
                "stroke_min": float(caps.get("stroke_min", 0.0)), "stroke_max": float(caps.get("stroke_max", 1.0)),
                "max_stroke_speed": float(caps.get("max_stroke_speed", 3.0)),
            }, time.monotonic())
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

    async def _worker(self):
        while self.connected:
            try:
                async with self._lock:
                    await self._apply()
                if self.error and self.connected:
                    self.error = ""                  # worked again
            except _Aborted:
                pass
            except HandyError as exc:
                if exc.args and exc.args[0] != self.error:
                    self.log(f"Handy WiFi: {exc}")
                self.error = str(exc)
                await asyncio.sleep(1.0)
            except Exception as exc:
                self.error = f"{exc.__class__.__name__}: {exc}"
                self.log(f"Handy WiFi error: {self.error}")
                await asyncio.sleep(1.0)
            if self.want is None and self.mode is None:
                return
            await asyncio.sleep(0.1)

    def _check(self, epoch: int):
        if epoch != self.epoch or self.want is None:
            raise _Aborted()

    async def _apply(self):
        epoch = self.epoch
        want = self.want
        if want is None:
            if self.mode is not None and time.monotonic() - self.none_since >= STOP_AFTER_S:
                await self._stop()
            return
        if time.monotonic() < self.backoff_until:
            return
        if time.monotonic() - self.clock_at > CLOCK_REFRESH_S:
            await self._sync_clock()
        stroke, ramp, caps, stamp = want
        lo, hi = sorted((caps["stroke_min"], caps["stroke_max"]))
        hi = max(hi, min(1.0, lo + 0.05))
        ramp = max(0.0, min(1.0, ramp))
        if stroke[0] == "script":
            _, script, t, invert = stroke
            # soft start: the zone opens from the middle, like the short strokes of the Bluetooth path
            mid = (lo + hi) / 2
            await self._set_zone(mid - (mid - lo) * ramp, mid + (hi - mid) * ramp)
            key = (id(script), bool(invert), round(caps["max_stroke_speed"], 2), round(hi - lo, 2))
            if self.mode != "hsp" or self.loaded != key:
                await self._load(script, bool(invert), caps["max_stroke_speed"], hi - lo, key, epoch)
                self._check(epoch)
                await self._play(self._now_t(want), epoch)
            else:
                now_t = self._now_t(want) % self.duration
                predicted = (self.play_t + (time.monotonic() - self.play_at) * 1000) % self.duration
                ahead = (now_t - predicted + self.duration / 2) % self.duration - self.duration / 2
                if abs(ahead) > JUMP_MS or time.monotonic() - self.last_sync >= SYNC_EVERY_S:
                    await self._request("PUT", "/hsp/synctime", {"current_time": int(now_t), "server_time": self.server_now()})
                    self.play_t, self.play_at, self.last_sync = now_t, time.monotonic(), time.monotonic()
        else:
            level = max(0.0, min(1.0, float(stroke[1])))
            if level < 0.05:
                if self.mode is not None:
                    await self._stop()
                return
            depth = (0.4 + 0.6 * level) * ramp                 # same strokes as the Bluetooth path
            mid, half = (lo + hi) / 2, (hi - lo) / 2 * depth
            await self._set_zone(mid - half, mid + half)
            velocity = round(min(1.0, (0.1 + 0.9 * level) * min(1.0, caps["max_stroke_speed"] / 3.0)), 2)
            if self.mode != "hamp":
                self._check(epoch)
                await self._request("PUT", "/hamp/start")
                self.mode, self.loaded, self.hamp_velocity = "hamp", None, None
            if self.hamp_velocity is None or (abs(velocity - self.hamp_velocity) >= 0.05 and time.monotonic() - self.hamp_at >= 0.5):
                self._check(epoch)
                await self._request("PUT", "/hamp/velocity", {"velocity": velocity})
                self.hamp_velocity, self.hamp_at = velocity, time.monotonic()

    @staticmethod
    def _now_t(want) -> float:
        stroke, _, _, stamp = want
        return float(stroke[2]) + (time.monotonic() - stamp) * 1000

    async def _set_zone(self, lo: float, hi: float):
        zone = (round(max(0.0, lo), 2), round(min(1.0, hi), 2))
        if zone == self.zone:
            return
        if self.zone is not None and time.monotonic() - self.zone_at < ZONE_EVERY_S:
            return
        await self._request("PUT", "/slider/stroke", {"min": zone[0], "max": zone[1]})
        self.zone, self.zone_at = zone, time.monotonic()

    async def _load(self, script, invert: bool, max_speed: float, zone_len: float, key, epoch: int):
        if self.mode != "hsp":
            await self._request("PUT", "/mode2", {"mode": 4})
        self.mode, self.loaded = "hsp", None
        setup = await self._request("PUT", "/hsp/setup", {}) or {}
        points = loop_points(script, invert, max_speed, zone_len)
        max_points = setup.get("max_points") if isinstance(setup, dict) else None
        if isinstance(max_points, int) and max_points > 1 and len(points) > max_points:
            points = points[:max_points]
        for start in range(0, len(points), MAX_POINTS_PER_ADD):
            self._check(epoch)
            chunk = points[start:start + MAX_POINTS_PER_ADD]
            await self._request("PUT", "/hsp/add", {"points": chunk, "tail_point_stream_index": start + len(chunk) - 1})
        self.loaded, self.duration = key, max(1, points[-1]["t"])

    async def _play(self, t: float, epoch: int):
        self._check(epoch)
        start = int(t % self.duration)
        await self._request("PUT", "/hsp/play", {"start_time": start, "server_time": self.server_now(),
                                                 "playback_rate": 1.0, "loop": True})
        self.play_t, self.play_at, self.last_sync = start, time.monotonic(), time.monotonic()

    async def _stop(self):
        mode, self.mode, self.loaded, self.hamp_velocity = self.mode, None, None, None
        if mode == "hsp":
            await self._request("PUT", "/hsp/stop")
        elif mode == "hamp":
            await self._request("PUT", "/hamp/stop")

    async def stop_now(self):
        """Engine stop / emergency stop: drops what is being prepared and stops both modes, without waiting for the worker."""
        self.epoch += 1
        self.want = None
        self.none_since = time.monotonic() - STOP_AFTER_S
        if not self.connected:
            return
        mode, self.mode, self.loaded, self.hamp_velocity = self.mode, None, None, None
        paths = ["/hsp/stop", "/hamp/stop"] if mode is None else (["/hsp/stop"] if mode == "hsp" else ["/hamp/stop"])
        await asyncio.gather(*(self._request("PUT", path) for path in paths), return_exceptions=True)

    def status(self) -> str:
        if not self.connected:
            return f"not connected{(' - ' + self.error) if self.error else ''}"
        state = {"hsp": "playing the animation's script", "hamp": f"stroking by itself ({int((self.hamp_velocity or 0) * 100)}% speed)"}
        text = f"connected (firmware {self.fw}) - {state.get(self.mode, 'ready')}"
        return text + (f" - {self.error}" if self.error else "")
