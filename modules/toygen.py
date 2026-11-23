"""
Toy scripts generator: animation video -> funscript (strokes) + air pressure track.

Works without F95Checker's GUI (pure functions) so it can be tested and run from a worker thread.

How a script is made from an animation (no AI, only the pixels' motion):
 1. Decode the video small and in grayscale (96x54 at 20 fps): PyAV if installed, else ffmpeg.
 2. Keep the pixels that move the most (the "action" area of the loop).
 3. The 1st principal component of those pixels over time = the back-and-forth movement.
 4. Band-pass 0.4-4 Hz (24-240 strokes / min) by FFT. FFT is circular, so a looping
    animation gives a script that loops seamlessly too.
 5. Each top / bottom of that curve = one funscript action (pos 100 / 0), the stroke
    length follows the amount of motion (small moves = short strokes).
 6. Air pressure (Lovense Max "Constrict"): the scene intensity (strokes per second x motion
    energy), smoothed over seconds, because the pump is slow.

The vibration is NOT stored: the player derives it from the strokes (speed + position), so
funscripts downloaded from the internet drive the toy exactly like generated ones.
"""
import dataclasses
import io
import json
import os
import pathlib
import pickle
import re
import shutil
import subprocess
import tempfile
import zlib

VIDEO_EXTS = (".webm", ".ogv", ".mp4", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg", ".mov")
GENERATOR = "F95Checker toygen 1"
FPS = 20
WIDTH, HEIGHT = 96, 54
MAX_SECONDS = 180            # longer videos are cutscenes, analysing the first 3 minutes is enough
MIN_HZ, MAX_HZ = 0.4, 4.0    # 24..240 strokes per minute


# ---------------------------------------------------------------------------
# Ren'Py archives (.rpa): read the index safely, extract one file
# ---------------------------------------------------------------------------

class _SafeUnpickler(pickle.Unpickler):
    """A .rpa index only contains dicts / lists / tuples / str / bytes / int: refuse any class (no code execution)."""
    def find_class(self, module, name):
        raise pickle.UnpicklingError(f"refused {module}.{name}")


def rpa_index(path: pathlib.Path) -> dict[str, tuple[int, int, bytes]]:
    """{'images/xx.webm': (offset, length, prefix)} for RPA-3.0 / 3.2 / 2.0 archives. {} if unreadable."""
    try:
        with open(path, "rb") as f:
            header = f.readline(300).decode("latin-1").split()
            if not header or header[0] not in ("RPA-3.0", "RPA-3.2", "RPA-2.0"):
                return {}
            offset = int(header[1], 16)
            key = 0
            if header[0] == "RPA-3.0":
                for part in header[2:]:
                    key ^= int(part, 16)
            elif header[0] == "RPA-3.2":
                for part in header[3:]:
                    key ^= int(part, 16)
            f.seek(offset)
            raw = _SafeUnpickler(io.BytesIO(zlib.decompress(f.read()))).load()
    except Exception:
        return {}
    index = {}
    for name, entries in raw.items():
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if not entries:
            continue
        entry = entries[0]
        off, length = entry[0] ^ key, entry[1] ^ key
        prefix = entry[2] if len(entry) > 2 else b""
        if isinstance(prefix, str):
            prefix = prefix.encode("latin-1")
        index[name.replace("\\", "/")] = (off, length, prefix or b"")
    return index


def rpa_extract(path: pathlib.Path, entry: tuple[int, int, bytes], dest: pathlib.Path):
    off, length, prefix = entry
    with open(path, "rb") as src, open(dest, "wb") as out:
        src.seek(off)
        out.write(prefix)
        remaining = length - len(prefix)
        while remaining > 0:
            chunk = src.read(min(remaining, 1 << 20))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)


@dataclasses.dataclass(slots=True)
class Video:
    key: str                       # path as Ren'Py names it, relative to game/ ("images/ep9/sex1.webm"), lowercase
    path: pathlib.Path             # loose file, or the .rpa containing it
    entry: tuple | None = None     # rpa entry, None for loose files
    size: int = 0


def list_videos(game_dir: pathlib.Path) -> list[Video]:
    """Every animation of a Ren'Py game: loose in game/ and inside its .rpa archives. Loose files win (Ren'Py does the same)."""
    videos: dict[str, Video] = {}
    archives = []
    for cur, dirs, files in os.walk(game_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ("cache", "saves", "__pycache__", "toy_scripts")]
        for name in files:
            full = pathlib.Path(cur) / name
            low = name.lower()
            if low.endswith(VIDEO_EXTS):
                key = full.relative_to(game_dir).as_posix().lower()
                try:
                    size = full.stat().st_size
                except OSError:
                    size = 0
                videos[key] = Video(key, full, None, size)
            elif low.endswith(".rpa"):
                archives.append(full)
    for archive in sorted(archives):
        for name, entry in rpa_index(archive).items():
            key = name.lower()
            if key.endswith(VIDEO_EXTS) and key not in videos:
                videos[key] = Video(key, archive, entry, entry[1])
    return sorted(videos.values(), key=lambda v: v.key)


def video_key(filename: str) -> str:
    """Normalises what the game reports ('images/Ep9/Sex 1.webm', 'game/images/..') to the key of list_videos()."""
    key = filename.replace("\\", "/").strip().lower()
    key = re.sub(r"^<[^>]*>", "", key)   # Ren'Py playback specs: "<loop 2.0>images/a.webm"
    if key.startswith("game/"):
        key = key[5:]
    return key.lstrip("/")


def script_name(key: str) -> str:
    """Cache file name for a video key: 'images/ep9/sex1.webm' -> 'images__ep9__sex1.funscript'."""
    stem = key.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9_\-\.]+", "_", stem.replace("/", "__")) + ".funscript"


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def find_ffmpeg(custom: str = "") -> str | None:
    if custom and pathlib.Path(custom).is_file():
        return custom
    # shipped with the release (<app>/tools/ffmpeg/bin): a recent ffmpeg, before an older one on PATH
    # (common.meta and not modules.globals: this module also runs in the worker processes)
    from common.meta import self_path
    for name in ("ffmpeg.exe", "ffmpeg"):
        for bundled in (self_path / "tools" / "ffmpeg" / "bin" / name, self_path / "tools" / "ffmpeg" / name):
            if bundled.is_file():
                return str(bundled)
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
        if pathlib.Path(candidate).is_file():
            return candidate
    return None


def decoder_available(ffmpeg: str = "") -> str:
    """'PyAV' / 'ffmpeg' / '' (no way to decode videos)."""
    try:
        import av  # noqa: F401
        return "PyAV"
    except Exception:
        pass
    return "ffmpeg" if find_ffmpeg(ffmpeg) else ""


def _decode_av(path: pathlib.Path):
    import av
    import numpy as np
    frames = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        next_t = 0.0
        for frame in container.decode(stream):
            t = float(frame.time) if frame.time is not None else next_t
            if t > MAX_SECONDS:
                break
            if t + 1e-6 < next_t:
                continue                     # resample to FPS: keep the 1st frame of each 1/FPS slot
            while next_t <= t:
                next_t += 1 / FPS
            frames.append(frame.reformat(width=WIDTH, height=HEIGHT, format="gray").to_ndarray())
    if not frames:
        raise ValueError("no frame decoded")
    return np.asarray(frames, dtype=np.float32)


def _decode_ffmpeg(path: pathlib.Path, ffmpeg: str):
    import numpy as np
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-t", str(MAX_SECONDS), "-i", str(path), "-an",
        "-vf", f"fps={FPS},scale={WIDTH}:{HEIGHT},format=gray", "-f", "rawvideo", "-",
    ]
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: no console popping up on Windows
    proc = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=flags)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode("utf-8", "replace").strip()[:300] or f"ffmpeg exit {proc.returncode}")
    raw = np.frombuffer(proc.stdout, dtype=np.uint8)
    count = raw.size // (WIDTH * HEIGHT)
    if count == 0:
        raise ValueError("no frame decoded")
    return raw[:count * WIDTH * HEIGHT].reshape(count, HEIGHT, WIDTH).astype(np.float32)


def decode(path: pathlib.Path, ffmpeg: str = ""):
    """Grayscale frames [T, 54, 96] float32 at FPS."""
    try:
        import av  # noqa: F401
        return _decode_av(path)
    except ImportError:
        pass
    exe = find_ffmpeg(ffmpeg)
    if not exe:
        raise RuntimeError("No video decoder: install PyAV (pip install av) or ffmpeg")
    return _decode_ffmpeg(path, exe)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Analysis:
    duration_ms: int
    actions: list[dict]          # funscript actions [{"at": ms, "pos": 0..100}]
    air: list[list]              # [[ms, 0..1], ...] constrict / air pump level
    bpm: float                   # strokes per minute (0 = no rhythm found: still / slow scene)
    energy: float                # 0..1 overall motion amount
    periodicity: float           # 0..1 how clearly rhythmic the motion is


def _smooth(x, n):
    import numpy as np
    n = min(int(n), x.size)
    if n <= 1 or x.size < 2:
        return x
    kernel = np.ones(n, dtype=np.float32) / n
    pad = np.concatenate([x[-n:], x, x[:n]])        # circular (looping animations)
    return np.convolve(pad, kernel, mode="same")[n:-n]


def _extrema(s, min_gap):
    """Alternating maxima / minima of s, at least min_gap samples apart: [(index, is_top)]."""
    import numpy as np
    d = np.diff(s)
    sign = np.sign(d)
    for i in range(1, sign.size):                    # flat parts take the previous slope
        if sign[i] == 0:
            sign[i] = sign[i - 1]
    turns = np.where(np.diff(sign) != 0)[0] + 1
    out: list[tuple[int, bool]] = []
    for i in turns:
        top = bool(sign[i - 1] > 0)
        if out and out[-1][1] == top:                # same kind twice: keep the more extreme one
            if (top and s[i] > s[out[-1][0]]) or (not top and s[i] < s[out[-1][0]]):
                out[-1] = (int(i), top)
            continue
        if out and i - out[-1][0] < min_gap:         # too close = noise: drop the previous turn
            out.pop()
            if out and out[-1][1] == top and ((top and s[i] > s[out[-1][0]]) or (not top and s[i] < s[out[-1][0]])):
                out[-1] = (int(i), top)
            elif not out or out[-1][1] != top:
                out.append((int(i), top))
            continue
        out.append((int(i), top))
    return out


MIN_ANALYSIS_SECONDS = 12


def _dominant_hz(signal, lo, hi):
    """(frequency, share of the power) of the strongest periodic component of signal within [lo, hi] Hz."""
    import numpy as np
    sig = signal - signal.mean()
    power = np.abs(np.fft.rfft(sig)) ** 2
    freqs = np.fft.rfftfreq(sig.size, d=1 / FPS)
    band = (freqs >= lo) & (freqs <= hi)
    if not band.any() or power[1:].sum() <= 0:
        return 0.0, 0.0
    i = int(np.argmax(np.where(band, power, 0)))
    return float(freqs[i]), float(power[max(i - 1, 1):i + 2].sum() / (power[1:].sum() + 1e-9))


def _interp_pos(a: dict, b: dict, at: int) -> int:
    if b["at"] == a["at"]:
        return b["pos"]
    return int(round(a["pos"] + (b["pos"] - a["pos"]) * (at - a["at"]) / (b["at"] - a["at"])))


def analyze(frames) -> Analysis:
    import numpy as np
    loop_count = frames.shape[0]
    duration_ms = int(round(loop_count * 1000 / FPS))
    if loop_count < FPS // 2:                         # < 0.5 s: a still image / title card
        return Analysis(duration_ms, [{"at": 0, "pos": 0}], [[0, 0.0]], 0.0, 0.0, 0.0)
    # Ren'Py animations are short loops (1-6 s): tile the loop to >= 12 s, which is exact for a loop
    # and gives the FFT enough resolution / the stroke detection enough cycles. Actions of the
    # first loop are kept at the end.
    repeats = max(1, int(np.ceil(MIN_ANALYSIS_SECONDS * FPS / loop_count)))
    if repeats > 1:
        frames = np.concatenate([frames] * repeats, axis=0)
    count = frames.shape[0]

    flat = frames.reshape(count, -1)
    flat = flat - flat.mean(axis=1, keepdims=True)    # ignore global fades / flashes
    diff = np.abs(np.diff(flat, axis=0)).mean(axis=1)
    contrast = float(np.percentile(np.abs(flat), 90)) + 1.0
    # Motion energy per frame, 0..1 (frame diff relative to the image contrast, saturates at 12 %)
    motion = np.clip(np.concatenate([diff[:1], diff]) / contrast / 0.12, 0, 1)
    energy = float(np.clip(np.percentile(motion, 75), 0, 1))

    # Pixels that move the most = the action area
    std = flat.std(axis=0)
    mask = std >= np.percentile(std, 85)
    x = flat[:, mask]
    x = x - x.mean(axis=0, keepdims=True)

    # Principal components of the moving area, keep the most rhythmic one
    try:
        u, sv, _ = np.linalg.svd(x, full_matrices=False)
    except np.linalg.LinAlgError:
        u, sv = np.zeros((count, 1)), np.zeros(1)
    freqs = np.fft.rfftfreq(count, d=1 / FPS)
    band = (freqs >= MIN_HZ) & (freqs <= MAX_HZ)
    best = None
    for k in range(min(3, u.shape[1])):
        comp = u[:, k] * sv[k]
        spec = np.fft.rfft(comp)
        power = np.abs(spec) ** 2
        total = power[1:].sum() + 1e-9
        if not band.any():
            break
        inband = power[band].sum() / total
        peak_i = np.argmax(np.where(band, power, 0))
        # periodicity: share of the power around the main peak (±1 bin + 1st harmonic)
        bins = {b for b in (peak_i - 1, peak_i, peak_i + 1, 2 * peak_i - 1, 2 * peak_i, 2 * peak_i + 1) if 1 <= b < power.size}
        periodicity = float(min(1.0, sum(power[b] for b in bins) / total))
        score = inband * (0.5 + periodicity) * (1.0 if k == 0 else 0.85)
        if best is None or score > best[0]:
            best = (score, comp, spec, peak_i, periodicity)

    if best is None or energy < 0.04:
        # still or nearly still scene: no strokes, air follows the (tiny) motion
        return Analysis(duration_ms, [{"at": 0, "pos": 0}, {"at": duration_ms, "pos": 0}],
                        [[0, round(energy * 0.5, 3)]], 0.0, energy, 0.0)

    _, comp, spec, peak_i, periodicity = best
    main_hz = float(freqs[peak_i])
    # Cross-check: the motion energy peaks twice per stroke (once per direction). If the principal
    # component is not clearly rhythmic but the energy is, trust the energy (half its frequency).
    energy_hz, energy_share = _dominant_hz(motion, MIN_HZ * 2, MAX_HZ * 2)
    if energy_hz and energy_share > periodicity and energy_share > 0.3 and abs(energy_hz / 2 - main_hz) > 0.15:
        target = energy_hz / 2
        peak_i = int(np.argmin(np.abs(freqs - target)))
        main_hz = float(freqs[peak_i])
        periodicity = energy_share
    # Band-pass around the rhythm: from 0.6x the main frequency up to 2.6x (keeps the stroke shape)
    lo, hi = max(MIN_HZ, main_hz * 0.6), min(MAX_HZ * 1.3, max(main_hz * 2.6, main_hz + 0.8))
    filt = spec.copy()
    filt[(freqs < lo) | (freqs > hi)] = 0
    s = np.fft.irfft(filt, n=count)
    s = s / (np.percentile(np.abs(s), 95) + 1e-9)

    period = FPS / main_hz if main_hz > 0 else FPS
    turns = _extrema(s, max(2, int(period * 0.35)))
    local_motion = _smooth(motion, max(1, int(period)))

    actions = []
    for i, top in turns:
        amp = min(1.0, abs(float(s[i])))
        # stroke length 30..100 %: size of this movement x absolute motion around it (gentle scenes = short strokes)
        length = 0.3 + 0.7 * min(1.0, amp) * (0.35 + 0.65 * min(1.0, float(local_motion[i]) / 0.35))
        half = 50 * length
        pos = 50 + half if top else 50 - half
        actions.append({"at": int(round(i * 1000 / FPS)), "pos": int(round(np.clip(pos, 0, 100)))})
    if repeats > 1 and actions:
        # keep one loop: actions of the 2nd copy (steady state), shifted back to 0
        start, end = duration_ms, 2 * duration_ms
        one = [{"at": a["at"] - start, "pos": a["pos"]} for a in actions if start <= a["at"] < end]
        gap_ms = 1000 * max(2, int(period * 0.35)) / FPS
        while len(one) > 2 and one[0]["at"] + duration_ms - one[-1]["at"] < gap_ms:
            one.pop(0)                               # turn glued to the previous loop's last one: seam glitch
        if len(one) > 1 and one[0]["at"] > 0:
            last = {"at": one[-1]["at"] - duration_ms, "pos": one[-1]["pos"]}
            one.insert(0, {"at": 0, "pos": _interp_pos(last, one[0], 0)})
        actions = one or actions[:1]
    if not actions:
        actions = [{"at": 0, "pos": 0}]
    if actions[0]["at"] > 0:
        actions.insert(0, {"at": 0, "pos": actions[-1]["pos"]})   # loop: start where the end lands

    bpm = 60.0 * main_hz if periodicity >= 0.15 else 60.0 * len(turns) / 2 / max(count / FPS, 1e-3)

    # Air pressure: scene intensity (rhythm x motion), smoothed over 3 s, quantised later by the player
    speed_factor = min(1.0, bpm / 150.0)
    intensity = np.clip(_smooth(motion, FPS * 3) / max(energy, 1e-3), 0, 1.5) * (0.35 + 0.65 * speed_factor)
    intensity = np.clip(intensity * (0.4 + 0.6 * energy), 0, 1)
    air = []
    step = FPS * 2                                       # one point every 2 s at most
    count = loop_count                                   # air of the first loop only
    for i in range(0, count, step):
        level = round(float(intensity[i:i + step].mean()), 3)
        if not air or abs(air[-1][1] - level) >= 0.08:
            air.append([int(i * 1000 / FPS), level])
    return Analysis(duration_ms, actions, air or [[0, 0.0]], round(bpm, 1), round(energy, 3), round(periodicity, 3))


def to_funscript(result: Analysis, source: str) -> dict:
    return {
        "version": "1.0",
        "inverted": False,
        "range": 100,
        "actions": result.actions,
        "metadata": {
            "creator": GENERATOR,
            "description": f"Generated from {source}",
            "duration": round(result.duration_ms / 1000, 3),   # OpenFunscripter convention: seconds
        },
        "f95toy": {
            "generator": GENERATOR,
            "duration_ms": result.duration_ms,
            "air": result.air,
            "bpm": result.bpm,
            "energy": result.energy,
            "periodicity": result.periodicity,
        },
    }


def generate(video: Video, out_dir: pathlib.Path, ffmpeg: str = "") -> pathlib.Path:
    """Analyse one video and write its .funscript in out_dir. Returns the script path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / script_name(video.key)
    if video.entry is None:
        frames = decode(video.path, ffmpeg)
    else:
        suffix = os.path.splitext(video.key)[1]
        fd, tmp = tempfile.mkstemp(prefix="f95toy_", suffix=suffix)
        os.close(fd)
        try:
            rpa_extract(video.path, video.entry, pathlib.Path(tmp))
            frames = decode(pathlib.Path(tmp), ffmpeg)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    result = analyze(frames)
    tmp_target = target.with_suffix(".tmp")
    tmp_target.write_text(json.dumps(to_funscript(result, video.key)), encoding="utf-8")
    os.replace(tmp_target, target)
    return target


# ---------------------------------------------------------------------------
# Reading scripts (generated or downloaded) for the player
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Script:
    times: list[int]             # ms, sorted
    pos: list[float]             # 0..1
    duration_ms: int
    air: list[tuple[int, float]]
    generated: bool
    path: str = ""


def load_script(path: pathlib.Path) -> Script | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        actions = sorted(
            ((int(a["at"]), float(a["pos"])) for a in data.get("actions", []) if "at" in a and "pos" in a),
            key=lambda a: a[0],
        )
    except Exception:
        return None
    if not actions:
        return None
    rng = float(data.get("range") or 100) or 100.0
    inverted = bool(data.get("inverted"))
    times = [a[0] for a in actions]
    pos = [min(1.0, max(0.0, a[1] / rng)) for a in actions]
    if inverted:
        pos = [1.0 - p for p in pos]
    extra = data.get("f95toy") if isinstance(data.get("f95toy"), dict) else {}
    duration = int(extra.get("duration_ms") or times[-1] or 1)
    air = [(int(a[0]), float(a[1])) for a in extra.get("air", []) if isinstance(a, (list, tuple)) and len(a) == 2]
    return Script(times, pos, max(duration, times[-1], 1), air, bool(extra.get("generator")), str(path))
