"""
HD & 60 fps (right click on a Ren'Py game > HD & 60 fps...): the game's own pictures and animations improved
once, in the background, without touching the game's files.

HD pictures (Ren'Py 8.4 or newer, games made for less than the screen: 1080p, or 1440p / 2160p for the Quest 3
    through Virtual Desktop, Library tools > Settings > HD for)
    Ren'Py 8.4+ looks for "picture@2.png" next to "picture.png" when the window is larger than the game
    (automatic oversampling) and shows it at the same place, with twice the detail. Every picture of the game
    (loose or inside its .rpa archives) is enlarged x4 by Real-ESRGAN (the tool used for the banners), brought
    back to exactly x2, and written as <name>@2.<ext> in game\\. The originals are not modified.

60 fps animations
    Animations below 50 fps get their in-between frames computed: by RIFE (rife-ncnn-vulkan, best quality,
    on the graphics card) when it is found, else by ffmpeg's motion interpolation. Same length, so toy sync and
    the game's timing are unchanged. The new video is written at the same place in game\\: a loose file wins over
    the archives; a loose original is moved to <game folder>\\_f95checker_original first.
    Left as they are: videos with transparency or sound, and videos already at 50 fps or more.

Everything written is listed in <game folder>\\_f95checker_remaster.json: Undo deletes it and puts the moved
originals back. Ignored by the install date (it is not a game update). Long: it runs one file at a time,
can be cancelled and restarted (what is done stays done).
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
import tempfile
import time

from common.structs import (
    Game,
    LaunchState,
    Os,
)
from external import (
    async_thread,
)
from modules import (
    globals,
    installdate,
    toygen,
    utils,
)

LOG_PREFIX = "[Remaster]"
MANIFEST = "_f95checker_remaster.json"
ORIGINALS_DIR = "_f95checker_original"
OVERSAMPLE_FILE = "zz_f95checker_oversample.rpy"
OVERSAMPLE_SCRIPT = (
    "# Added by F95Checker (HD & 60 fps): Ren'Py uses the @2 pictures when the window is larger than the game.\n"
    "init 999 python:\n"
    "    try:\n"
    "        if config.automatic_oversampling is not None and config.automatic_oversampling < 2:\n"
    "            config.automatic_oversampling = 2\n"
    "    except Exception:\n"
    "        pass  # Ren'Py older than 8.4: no oversampling, nothing to do\n"
)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
RIFE_EXE = "rife-ncnn-vulkan.exe"
MIN_FPS = 50.0
HD_MIN_VERSION = (8, 4)
MIN_FREE_BYTES = 15 * 1024 ** 3        # stops writing on a drive with less free space than this


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


def _no_window() -> dict:
    if globals.os is Os.Windows:
        return dict(creationflags=subprocess.CREATE_NO_WINDOW)
    return {}


HD_TARGETS = (1080, 1440, 2160)


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("rife_path", "")
    p.setdefault("hd_target", 1080)
    return p


def hd_target() -> int:
    """Height of the screen the games are played on (HD pictures for the games made for less)."""
    with contextlib.suppress(Exception):
        value = int(prefs()["hd_target"])
        if value in HD_TARGETS:
            return value
    return 1080


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _find(names: tuple[str, ...], custom: str, sub: str, depth: int = 2) -> pathlib.Path | None:
    custom = (custom or "").strip().strip('"')
    if custom:
        path = pathlib.Path(custom)
        for candidate in ([path / n for n in names] if path.is_dir() else [path]):
            if candidate.is_file():
                return candidate
    for folder in (globals.data_path / sub, pathlib.Path.home() / "Downloads", globals.data_path):
        for level in range(depth + 1):
            for name in names:
                with contextlib.suppress(OSError):
                    found = next((p for p in folder.glob("/".join(["*"] * level + [name])) if p.is_file()), None)
                    if found:
                        return found
    return None


_tools_cache: tuple[float, dict] = (float("-inf"), {})


def forget_tools():
    global _tools_cache
    _tools_cache = (float("-inf"), {})


def tools() -> dict:
    """{'ffmpeg', 'ffprobe', 'rife', 'esrgan'}: paths or None (looked for again every 30 s at most)."""
    global _tools_cache
    if time.monotonic() - _tools_cache[0] < 30:
        return _tools_cache[1]
    from modules import toy, visuals
    ffmpeg = toygen.find_ffmpeg(toy.settings().get("ffmpeg_path", "")) or _find(("ffmpeg.exe", "ffmpeg"), "", "ffmpeg", 2)
    ffprobe = None
    if ffmpeg:
        ffmpeg = pathlib.Path(ffmpeg)
        for name in ("ffprobe.exe", "ffprobe"):
            if (ffmpeg.parent / name).is_file():
                ffprobe = ffmpeg.parent / name
        ffprobe = ffprobe or (shutil.which("ffprobe") and pathlib.Path(shutil.which("ffprobe")))
    found = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "rife": _find((RIFE_EXE, RIFE_EXE[:-4]), prefs()["rife_path"], "rife"),
        "esrgan": visuals.find_upscaler(),
    }
    _tools_cache = (time.monotonic(), found)
    return found


# ---------------------------------------------------------------------------
# What the game is made of
# ---------------------------------------------------------------------------

def renpy_version(root: pathlib.Path) -> tuple[int, ...]:
    with contextlib.suppress(OSError):
        text = (root / "renpy" / "vc_version.py").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"version\s*=\s*['\"](\d+)\.(\d+)(?:\.(\d+))?", text)
        if m:
            return tuple(int(x) for x in m.groups() if x is not None)
    with contextlib.suppress(OSError):
        text = (root / "renpy" / "__init__.py").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"version_tuple\s*=\s*\(?\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", text)
        if m:
            return tuple(int(x) for x in m.groups())
    return ()


_SIZE_RE = re.compile(r"gui\.init\(\s*(\d+)\s*,\s*(\d+)|config\.screen_width\s*=\s*(\d+)[\s\S]{0,200}?config\.screen_height\s*=\s*(\d+)")


def _size_in(text: str) -> tuple[int, int] | None:
    m = _SIZE_RE.search(text or "")
    if not m:
        return None
    w, h = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(w), int(h)


def virtual_size(game_dir: pathlib.Path) -> tuple[int, int] | None:
    """Size the game is made for (gui.init(w, h) / config.screen_width), from its gui / options scripts,
    loose or in its .rpa archives, at the top of game/ or in a sub-folder (scripts/gui.rpyc), translations left out."""
    from modules import cheatsheet
    names = ("gui", "options")

    def from_compiled(data: bytes):
        tree = cheatsheet.rpyc_tree(data)
        for node in cheatsheet._walk(tree) if tree is not None else ():
            size = _size_in(cheatsheet._pycode_source(node.__dict__.get("code")))
            if size:
                return size
        return None

    def wanted(rel: str) -> bool:
        rel = rel.replace("\\", "/").lower()
        stem, _, ext = rel.rpartition("/")[2].rpartition(".")
        return stem in names and ext in ("rpy", "rpyc") and not rel.startswith("tl/") and "/tl/" not in rel

    def read(rel: str, data_of):
        # .rpy first (plain text), then .rpyc
        for ext in ("rpy", "rpyc"):
            data = data_of(rel + "." + ext)
            if data is None:
                continue
            size = _size_in(data.decode("utf-8", "replace")) if ext == "rpy" else from_compiled(data)
            if size:
                return size
        return None

    loose = {}
    for current, dirs, files in os.walk(game_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ("cache", "saves", "__pycache__", "tl", "toy_scripts")]
        for name in files:
            full = pathlib.Path(current) / name
            rel = full.relative_to(game_dir).as_posix()
            if wanted(rel):
                loose[rel.lower()] = full
    stems = sorted({k.rpartition(".")[0] for k in loose}, key=lambda k: (k.count("/"), k))
    for stem in stems:
        with contextlib.suppress(OSError):
            size = read(stem, lambda rel: loose[rel.lower()].read_bytes() if rel.lower() in loose else None)
            if size:
                return size
    for archive in sorted(game_dir.glob("*.rpa")):
        index = toygen.rpa_index(archive)
        lower = {k.lower(): k for k in index if wanted(k)}
        for stem in sorted({k.rpartition(".")[0] for k in lower}, key=lambda k: (k.count("/"), k)):
            with contextlib.suppress(OSError):
                size = read(stem, lambda rel: _read_entry(archive, index[lower[rel.lower()]]) if rel.lower() in lower else None)
                if size:
                    return size
    return None


def _read_entry(archive: pathlib.Path, entry: tuple) -> bytes:
    offset, length, prefix = entry
    with open(archive, "rb") as f:
        f.seek(offset)
        return prefix + f.read(length - len(prefix))


@dataclasses.dataclass(slots=True)
class Media:
    key: str                        # path relative to game/, as Ren'Py names it (case kept)
    path: pathlib.Path              # loose file or .rpa
    entry: tuple | None = None

    def read(self) -> bytes:
        return _read_entry(self.path, self.entry) if self.entry else self.path.read_bytes()


def _list_media(game_dir: pathlib.Path, exts: tuple[str, ...]) -> list[Media]:
    """Files of these types, loose in game/ and inside its .rpa archives (loose wins, as in Ren'Py). Case kept.
    Leaves out @2 pictures, translations, saves, cache and what HD & 60 fps moved aside."""
    found: dict[str, Media] = {}
    archives = []
    for current, dirs, files in os.walk(game_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ("cache", "saves", "__pycache__", "tl", "toy_scripts")]
        for name in files:
            low = name.lower()
            if low.endswith(".rpa"):
                archives.append(pathlib.Path(current) / name)
            elif low.endswith(exts) and "@" not in name and ".f95" not in low:
                full = pathlib.Path(current) / name
                key = full.relative_to(game_dir).as_posix()
                found[key.lower()] = Media(key, full)
    for archive in sorted(archives):
        for name, entry in toygen.rpa_index(archive).items():
            low = name.lower()
            if low.endswith(exts) and "@" not in name and low not in found and not low.startswith("tl/"):
                found[low] = Media(name, archive, entry)
    return sorted(found.values(), key=lambda m: m.key.lower())


def list_images(game_dir: pathlib.Path) -> list[Media]:
    return _list_media(game_dir, IMAGE_EXTS)


def list_videos(game_dir: pathlib.Path) -> list[Media]:
    return _list_media(game_dir, tuple(toygen.VIDEO_EXTS))


def hd_name(key: str) -> str:
    base, dot, ext = key.rpartition(".")
    return f"{base}@2.{ext}"


# ---------------------------------------------------------------------------
# Manifest (what was written, for Undo)
# ---------------------------------------------------------------------------

def _manifest_path(root: pathlib.Path) -> pathlib.Path:
    return root / MANIFEST


def load_manifest(root: pathlib.Path) -> dict:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("files", [])
            data.setdefault("moved", [])
            return data
    return {"files": [], "moved": []}


def save_manifest(root: pathlib.Path, data: dict):
    tmp = _manifest_path(root).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, _manifest_path(root))


def undo(game: Game) -> str:
    """Deletes what HD & 60 fps wrote in this game, puts back the moved originals. '' or an error."""
    exe = installdate._main_exe(game)
    if exe is None:
        return "game folder not found"
    if game.launch_state is not LaunchState.Idle:
        return f"{game.name} is running: close it first"
    if job.running and job.game_id == game.id:
        return "being processed: cancel first"
    root = exe.parent
    data = load_manifest(root)
    errors = []
    dirs = set()
    for rel in data["files"]:
        path = root / rel
        with contextlib.suppress(FileNotFoundError):
            try:
                path.unlink()
                dirs.add(path.parent)
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
    for rel in data["moved"]:
        src = root / ORIGINALS_DIR / rel
        try:
            if src.is_file():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, root / rel)
        except OSError as exc:
            errors.append(f"restore {rel}: {exc}")
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            while d not in (root, root / "game") and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
    for name in (OVERSAMPLE_FILE, OVERSAMPLE_FILE + "c"):
        with contextlib.suppress(OSError):
            (root / "game" / name).unlink()
    if errors:
        return "\n".join(errors[:10])
    with contextlib.suppress(OSError):
        shutil.rmtree(root / ORIGINALS_DIR)
    with contextlib.suppress(OSError):
        _manifest_path(root).unlink()
    _log(f"'{game.name}': undone ({len(data['files'])} files)")
    return ""


def written_paths(game: Game, exe: pathlib.Path | None = None) -> list[str]:
    """Files written by HD & 60 fps in this game, relative to its folder (the local scan ignores them)."""
    exe = exe or installdate._main_exe(game)
    if exe is None or not _manifest_path(exe.parent).is_file():
        return []
    return list(load_manifest(exe.parent)["files"])


# ---------------------------------------------------------------------------
# Analysis (popup)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Analysis:
    version: tuple = ()
    size: tuple | None = None
    hd_ok: bool = False
    hd_why: str = ""
    images: int = 0
    images_done: int = 0
    videos: int = 0
    videos_sampled: int = 0
    videos_slow: int = 0            # of the sampled ones, below MIN_FPS
    done_files: int = 0


def video_info(path: pathlib.Path, ffprobe) -> dict:
    """{'fps', 'alpha', 'audio', 'duration', 'codec'} from ffprobe ({} if unreadable)."""
    if not ffprobe:
        return {}
    try:
        proc = subprocess.run([str(ffprobe), "-v", "error", "-show_entries",
                               "stream=codec_type,codec_name,r_frame_rate,avg_frame_rate,pix_fmt:stream_tags:format=duration",
                               "-of", "json", str(path)], capture_output=True, timeout=60, **_no_window())
        data = json.loads(proc.stdout or b"{}")
    except Exception:
        return {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return {}

    def rate(text):
        with contextlib.suppress(Exception):
            num, _, den = str(text).partition("/")
            return float(num) / float(den or 1)
        return 0.0

    fps = rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate"))
    tags = {str(k).lower(): str(v) for k, v in (video.get("tags") or {}).items()}
    pix_fmt = (video.get("pix_fmt") or "").lower()
    alpha = tags.get("alpha_mode", "0") == "1" or pix_fmt.startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya"))
    duration = 0.0
    with contextlib.suppress(Exception):
        duration = float((data.get("format") or {}).get("duration") or 0)
    return {"fps": fps, "alpha": alpha, "audio": any(s.get("codec_type") == "audio" for s in streams),
            "duration": duration, "codec": video.get("codec_name", "")}


def analyse(game: Game) -> Analysis:
    """Blocking."""
    a = Analysis()
    exe = installdate._main_exe(game)
    if exe is None or not (exe.parent / "game").is_dir():
        a.hd_why = "game folder not found"
        return a
    root, game_dir = exe.parent, exe.parent / "game"
    a.version = renpy_version(root)
    a.size = virtual_size(game_dir)
    if not a.version:
        a.hd_why = "Ren'Py version not found (HD needs 8.4 or newer)"
    elif a.version[:2] < HD_MIN_VERSION:
        a.hd_why = f"needs Ren'Py 8.4 or newer (this game: {'.'.join(map(str, a.version))})"
    elif a.size is None:
        a.hd_why = "game size not found in its scripts"
    elif a.size[1] >= hd_target():
        a.hd_why = (f"already made for {a.size[0]}x{a.size[1]}: nothing to gain on a {hd_target()}p screen"
                    + (" (Library tools > Settings > HD for: 1440p / 2160p for the Quest 3)" if hd_target() == 1080 else ""))
    else:
        a.hd_ok = True
    images = list_images(game_dir) if a.hd_ok else []
    a.images = len(images)
    a.images_done = sum(1 for m in images if (game_dir / hd_name(m.key)).is_file())
    videos = list_videos(game_dir)
    a.videos = len(videos)
    ffprobe = tools()["ffprobe"]
    if ffprobe and videos:
        step = max(1, len(videos) // 12)
        for v in videos[::step][:12]:
            info = _probe_media(v, ffprobe)
            if info:
                a.videos_sampled += 1
                if 0 < info["fps"] < MIN_FPS and not info["alpha"] and not info["audio"]:
                    a.videos_slow += 1
    a.done_files = len(load_manifest(root)["files"])
    return a


def _probe_media(media: Media, ffprobe) -> dict:
    if not media.entry:
        return video_info(media.path, ffprobe)
    work = pathlib.Path(tempfile.mkdtemp(prefix="f95probe_"))
    try:
        tmp = work / ("v" + pathlib.PurePosixPath(media.key).suffix)
        tmp.write_bytes(media.read())
        return video_info(tmp, ffprobe)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Job:
    running: bool = False
    cancel: bool = False
    game_id: int = 0
    game_name: str = ""
    phase: str = ""
    done: int = 0
    total: int = 0
    made: int = 0
    skipped: int = 0
    failed: int = 0
    started: float = 0.0
    message: str = ""
    drive_full: bool = False
    last_error: str = ""


job = Job()


def _run(cmd: list, timeout: int) -> str:
    """'' or the error (last line of the tool's output). Runs in the tool's folder (Real-ESRGAN / RIFE find their models there)."""
    try:
        proc = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout,
                              cwd=str(pathlib.Path(str(cmd[0])).parent), **_no_window())
    except subprocess.TimeoutExpired:
        return f"{pathlib.Path(str(cmd[0])).name} took more than {timeout // 60} minutes"
    except OSError as exc:
        return str(exc)
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip().splitlines()
        return (text[-1] if text else "") or f"exit code {proc.returncode}"
    return ""


def make_hd(media: Media, dst: pathlib.Path, esrgan: pathlib.Path, model: str) -> str:
    """Picture x4 by Real-ESRGAN, back to exactly x2, same format. '' or an error."""
    from PIL import Image
    work = pathlib.Path(tempfile.mkdtemp(prefix="f95hd_"))
    try:
        src = work / ("src" + pathlib.PurePosixPath(media.key).suffix.lower())
        src.write_bytes(media.read())
        with Image.open(src) as im:
            if getattr(im, "is_animated", False):
                return "skip"
            size = im.size
            if size[0] < 16 or size[1] < 16 or max(size) * 2 > 8192:
                return "skip"
            alpha = im.mode in ("RGBA", "LA", "P") and "A" in im.convert("RGBA").getbands()
            im.convert("RGBA" if alpha else "RGB").save(work / "in.png")
        error = _run([esrgan, "-i", work / "in.png", "-o", work / "out.png", "-n", model, "-s", "4"], 900)
        if error or not (work / "out.png").is_file():
            return error or "no output"
        with Image.open(work / "out.png") as big:
            out = big.convert("RGBA" if alpha else "RGB").resize((size[0] * 2, size[1] * 2), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.stem + ".f95tmp" + dst.suffix)
        ext = dst.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            out.convert("RGB").save(tmp, "JPEG", quality=94)
        elif ext == ".webp":
            out.save(tmp, "WEBP", quality=92, method=4)
        else:
            out.save(tmp, "PNG", compress_level=6)
        os.replace(tmp, dst)
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def make_60fps(media: Media, dst: pathlib.Path, t: dict) -> tuple[str, dict]:
    """Video with its in-between frames computed. ('' or an error or 'skip ...', info of the source)."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="f95fps_"))
    try:
        ext = pathlib.PurePosixPath(media.key).suffix.lower()
        src = work / ("src" + ext)
        src.write_bytes(media.read())
        info = video_info(src, t["ffprobe"])
        if not info:
            return "skip: unreadable", info
        if info["fps"] >= MIN_FPS or info["fps"] <= 0:
            return "skip: already smooth", info
        if info["alpha"]:
            return "skip: transparency", info
        if info["audio"]:
            return "skip: has sound", info
        target = round(info["fps"] * 2, 3)
        if ext in (".webm", ".mkv"):
            codec = ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4", "-deadline", "good"]
        elif ext in (".mp4", ".m4v", ".mov"):
            codec = ["-c:v", "libx264", "-crf", "17", "-preset", "medium", "-movflags", "+faststart"]
        else:
            return f"skip: {ext} not handled", info
        out = work / ("out" + ext)
        if t["rife"]:
            (work / "in").mkdir()
            (work / "mid").mkdir()
            # every frame as it is: -fps_mode (ffmpeg 5.1+; -vsync was removed in recent versions), -vsync for old ones
            error = _run([t["ffmpeg"], "-v", "error", "-i", src, "-fps_mode", "passthrough", work / "in" / "%08d.png"], 1800)
            if error and ("fps_mode" in error or "Option not found" in error or "Unrecognized option" in error):
                error = _run([t["ffmpeg"], "-v", "error", "-y", "-i", src, "-vsync", "0", work / "in" / "%08d.png"], 1800)
            if not error:
                error = _run([t["rife"], "-i", work / "in", "-o", work / "mid"], 3600)
            if not error:
                error = _run([t["ffmpeg"], "-v", "error", "-y", "-framerate", str(target), "-i", work / "mid" / "%08d.png",
                              *codec, "-pix_fmt", "yuv420p", out], 3600)
        else:
            # last frame repeated once so the last in-between frames exist, then cut at the original length
            # (same length = same loop timing for the game and the toy scripts)
            vf = f"tpad=stop_mode=clone:stop=2,minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            if info["duration"]:
                vf += f",trim=end_frame={round(info['duration'] * info['fps']) * 2}"
            error = _run([t["ffmpeg"], "-v", "error", "-y", "-i", src, "-vf", vf, *codec, "-pix_fmt", "yuv420p", "-an", out], 3600)
        if error or not out.is_file():
            return error or "no output", info
        new = video_info(out, t["ffprobe"])
        if not new or (info["duration"] and abs(new["duration"] - info["duration"]) > max(0.1, info["duration"] * 0.03)):
            return f"length changed ({info['duration']:.2f}s -> {new.get('duration', 0):.2f}s): not used", info
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), str(dst))
        return "", info
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _drive_full(root: pathlib.Path) -> bool:
    try:
        if shutil.disk_usage(root).free >= MIN_FREE_BYTES:
            return False
    except OSError:
        return False
    job.message = f"stopped: less than {MIN_FREE_BYTES // 1024 ** 3} GB free on {root.anchor or root}"
    job.drive_full = True
    return True


def _process(game: Game, hd: bool, fps: bool):
    """Blocking, one file at a time. Writes the manifest after each file (cancel / crash = what is done stays)."""
    exe = installdate._main_exe(game)
    root, game_dir = exe.parent, exe.parent / "game"
    t = tools()
    data = load_manifest(root)
    files = set(data["files"])

    def record(rel: str, moved: str = ""):
        if rel not in files:
            files.add(rel)
            data["files"].append(rel)
        if moved and moved not in data["moved"]:
            data["moved"].append(moved)
        save_manifest(root, data)

    if hd:
        from modules import visuals
        model = visuals.prefs().get("upscale_model") or "realesrgan-x4plus"
        job.phase = "Listing the pictures..."
        images = list_images(game_dir)
        todo = [m for m in images if not (game_dir / hd_name(m.key)).is_file()]
        job.done, job.total = 0, len(todo)
        job.phase = "HD pictures"
        if todo:
            (game_dir / OVERSAMPLE_FILE).write_text(OVERSAMPLE_SCRIPT, encoding="utf-8")
            record(f"game/{OVERSAMPLE_FILE}")
        for media in todo:
            if job.cancel or _drive_full(root):
                return
            dst = game_dir / hd_name(media.key)
            error = make_hd(media, dst, t["esrgan"], model)
            if error == "skip":
                job.skipped += 1
            elif error:
                job.failed += 1
                job.last_error = f"{media.key}: {error}"
                _log(f"'{game.name}': {media.key}: {error}")
            else:
                job.made += 1
                record(dst.relative_to(root).as_posix())
            job.done += 1
    if fps:
        job.phase = "Listing the animations..."
        videos = list_videos(game_dir)
        done_videos = set(data.get("videos_done", []))
        todo = [v for v in videos if v.key.lower() not in done_videos]
        job.done, job.total = 0, len(todo)
        job.phase = "60 fps animations" + ("" if t["rife"] else " (ffmpeg motion interpolation)")
        for media in todo:
            if job.cancel or _drive_full(root):
                return
            if media.entry is None:
                # loose original: the new video is made beside it, the original is moved aside only once it is ready
                dst = media.path
                rel = dst.relative_to(root).as_posix()
                tmp_dst = dst.with_name(dst.stem + ".f95new" + dst.suffix)
                error, info = make_60fps(media, tmp_dst, t)
                if not error:
                    aside = root / ORIGINALS_DIR / rel
                    try:
                        aside.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(dst, aside)
                    except OSError as exc:
                        error = f"original not moved (game running?): {exc}"
                    else:
                        os.replace(tmp_dst, dst)
                        record(rel, moved=rel)
                if error:
                    with contextlib.suppress(OSError):
                        tmp_dst.unlink()
            else:
                # inside an archive: the new video is written loose at the same path, Ren'Py takes it first
                dst = game_dir / media.key
                error, info = make_60fps(media, dst, t)
                if not error:
                    record(dst.relative_to(root).as_posix())
            if error.startswith("skip"):
                job.skipped += 1
            elif error:
                job.failed += 1
                job.last_error = f"{media.key}: {error}"
                _log(f"'{game.name}': {media.key}: {error}")
                job.done += 1
                continue                    # not marked as done: tried again at the next Start
            else:
                job.made += 1
            data.setdefault("videos_done", []).append(media.key.lower())
            save_manifest(root, data)
            job.done += 1


async def start(game: Game, hd: bool, fps: bool):
    if job.running or batch.running:
        return
    job.__init__()
    job.running, job.game_id, job.game_name, job.started = True, game.id, game.name, time.time()
    try:
        await asyncio.get_running_loop().run_in_executor(None, _process, game, hd, fps)
        verb = "Stopped, drive almost full" if job.drive_full else "Cancelled" if job.cancel else "Done"
        job.message = f"{verb}: {job.made} file(s) made, {job.skipped} left as they are, {job.failed} failed."
    except Exception as exc:
        job.message = f"Stopped: {exc}"
        _log(f"'{game.name}': {exc!r}")
    finally:
        job.running = False
        job.phase = ""
        _log(f"'{game.name}': {job.message}")
        with contextlib.suppress(Exception):
            from modules import toy
            toy._count_cache.pop(game.id, None)


# ---------------------------------------------------------------------------
# Every game (Library tools > HD & 60 fps)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Batch:
    running: bool = False
    cancel: bool = False
    phase: str = ""
    games: list = dataclasses.field(default_factory=list)        # [(game, files to look at)]
    index: int = 0
    files_total: int = 0
    files_done: int = 0         # games finished before the current one
    made: int = 0
    skipped: int = 0
    failed: int = 0
    games_skipped: list = dataclasses.field(default_factory=list)  # "name: reason"
    started: float = 0.0
    message: str = ""


batch = Batch()


def _batch_plan(games: list, hd: bool, fps: bool) -> list:
    """Blocking: Ren'Py games of the library with what is left to do in each (pictures only where HD can work)."""
    plan = []
    for game in games:
        exe = installdate._main_exe(game)
        if exe is None or not (exe.parent / "game").is_dir() or not (exe.parent / "renpy").is_dir():
            continue
        root, game_dir = exe.parent, exe.parent / "game"
        count = 0
        hd_here = False
        if hd:
            version, size = renpy_version(root), virtual_size(game_dir)
            # every game at once: only the games made for less than 1080p (1080p games at 1440p / 2160p would need
            # about 300 GB for the whole library: one by one, right click > HD & 60 fps...)
            hd_here = bool(version) and version[:2] >= HD_MIN_VERSION and size is not None and size[1] < min(hd_target(), 1080)
            if hd_here:
                count += sum(1 for m in list_images(game_dir) if not (game_dir / hd_name(m.key)).is_file())
        if fps:
            done = set(load_manifest(root).get("videos_done", []))
            count += sum(1 for v in list_videos(game_dir) if v.key.lower() not in done)
        if count:
            plan.append((game, count, hd_here))
    return plan


async def start_all(games: list, hd: bool, fps: bool):
    """One game after the other, one file at a time. Cancel stops after the current file; Start again resumes."""
    if job.running or batch.running:
        return
    loop = asyncio.get_running_loop()
    batch.__init__()
    batch.running, batch.started = True, time.time()
    try:
        batch.phase = "Listing what is left to do in every Ren'Py game..."
        plan = await loop.run_in_executor(None, _batch_plan, games, hd, fps)
        batch.games = [(g, n) for g, n, _ in plan]
        batch.files_total = sum(n for _, n, _ in plan)
        for batch.index, (game, count, hd_here) in enumerate(plan):
            if batch.cancel:
                break
            batch.phase = game.name
            if game.launch_state is not LaunchState.Idle:
                batch.games_skipped.append(f"{game.name}: running")
                batch.files_done += count
                continue
            job.__init__()
            job.running, job.game_id, job.game_name, job.started = True, game.id, game.name, time.time()
            try:
                await loop.run_in_executor(None, _process, game, hd and hd_here, fps)
            except Exception as exc:
                batch.games_skipped.append(f"{game.name}: {exc}")
                _log(f"'{game.name}': {exc!r}")
            finally:
                job.running = False
            batch.made += job.made
            batch.skipped += job.skipped
            batch.failed += job.failed
            batch.files_done += count
            if job.drive_full:
                batch.games_skipped.append(f"{game.name}: {job.message}")
            if job.failed and job.last_error:
                batch.games_skipped.append(f"{game.name}: {job.failed} failed (last: {job.last_error[:150]})")
            with contextlib.suppress(Exception):
                from modules import toy
                toy._count_cache.pop(game.id, None)
        verb = "Cancelled" if batch.cancel else "Done"
        batch.message = (f"{verb}: {batch.made} file(s) made, {batch.skipped} left as they are, {batch.failed} failed, "
                         f"{len(batch.games)} game(s) looked at.")
    except Exception as exc:
        batch.message = f"Stopped: {exc}"
        _log(f"All games: {exc!r}")
    finally:
        batch.running = False
        batch.phase = ""
        _log(f"All games: {batch.message}")


def cancel_all():
    batch.cancel = True
    job.cancel = True


def open_all_popup():
    import imgui
    state = {"hd": True, "fps": True}

    def content():
        t = tools()
        imgui.text_disabled("Every Ren'Py game of the library, one after the other, one file at a time, in the background.\n"
                            "The games' files stay as they are: each game's Undo (right click > HD & 60 fps...) removes what was added.\n"
                            "Very long for a big library: close F95Checker or Cancel any time, Start again resumes where it stopped.")
        imgui.separator()
        if batch.running:
            elapsed = time.time() - batch.started
            done = batch.files_done + (job.done if job.running else 0)
            eta = ""
            if done and batch.files_total > done:
                left = elapsed / done * (batch.files_total - done)
                eta = f", about {int(left // 3600)} h {int(left % 3600 // 60)} min left"
            if batch.games:
                imgui.text(f"Game {batch.index + 1} / {len(batch.games)}: {batch.phase}")
                if job.running:
                    imgui.text(f"   {job.phase}: {job.done} / {job.total}")
                imgui.text(f"All games: {done} / {batch.files_total} files{eta}")
                imgui.text(f"{batch.made + job.made} made, {batch.skipped + job.skipped} kept, {batch.failed + job.failed} failed")
            else:
                imgui.text(batch.phase)
            if imgui.button("Cancel###rma_cancel"):
                cancel_all()
            return
        _, state["hd"] = imgui.checkbox("HD pictures (Ren'Py 8.4+ games made for less than 1080p; 1080p games: one by one)###rma_hd", state["hd"])
        if state["hd"] and not t["esrgan"]:
            imgui.text_colored("   Real-ESRGAN not found: Library tools > Settings > Real-ESRGAN.", 0.95, 0.6, 0.2)
        _, state["fps"] = imgui.checkbox("Smoother animations (x2 fps), videos below 50 fps###rma_fps", state["fps"])
        if state["fps"]:
            if not t["ffmpeg"]:
                imgui.text_colored("   ffmpeg not found.", 0.95, 0.6, 0.2)
            elif not t["rife"]:
                imgui.text_colored("   RIFE not found: ffmpeg motion interpolation would take weeks for a whole library.\n"
                                   "   Put rife-ncnn-vulkan in Downloads (or Library tools > Settings > RIFE) first.", 0.95, 0.6, 0.2)
            else:
                imgui.text_disabled("   RIFE found: in-between frames computed by the graphics card.")
        imgui.text_disabled(f"Stops writing on a drive with less than {MIN_FREE_BYTES // 1024 ** 3} GB free (the other drives go on).")
        if batch.message:
            imgui.text_colored(batch.message, 0.4, 0.85, 0.4)
            for line in batch.games_skipped[:8]:
                imgui.text_disabled("   " + line)
        can_hd = state["hd"] and t["esrgan"]
        can_fps = state["fps"] and t["ffmpeg"]
        if job.running:
            imgui.text(f"Busy with {job.game_name}: wait for it (or cancel it in its window).")
        elif (can_hd or can_fps) and imgui.button("Start all###rma_start"):
            games = sorted(globals.games.values(), key=lambda g: g.name.lower())
            async_thread.run(start_all(games, bool(can_hd), bool(can_fps)))

    utils.push_popup(utils.popup, "HD & 60 fps - every game", content, buttons=True, closable=True, outside=False,
                     popup_uuid="remaster_all")


def needs_redraw() -> bool:
    return job.running or batch.running


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

def open_popup(game: Game):
    import imgui
    state = {"a": None, "hd": True, "fps": True, "msg": ""}

    async def load():
        state["a"] = await asyncio.get_running_loop().run_in_executor(None, analyse, game)
        a = state["a"]
        state["hd"] = a.hd_ok
        state["fps"] = a.videos_slow > 0

    async_thread.run(load())

    def content():
        if not job.running and job.game_id == game.id and job.started and state.get("ran") != job.started:
            state["ran"] = job.started      # a run just ended (or ended earlier): counts read again
            if state["a"] is not None:
                state["a"] = None
                async_thread.run(load())
        a = state["a"]
        t = tools()
        imgui.text_disabled("Improves the game's own pictures and animations once, in the background. The game's files stay\n"
                            "as they are: what is added can be removed with Undo. One file at a time: long for big games.")
        if a is None:
            imgui.text("Looking at the game...")
            return
        version = ".".join(map(str, a.version)) or "?"
        size = f"{a.size[0]}x{a.size[1]}" if a.size else "?"
        imgui.text(f"Ren'Py {version}, made for {size}.")
        imgui.separator()
        # HD
        if a.hd_ok:
            _, state["hd"] = imgui.checkbox(f"HD pictures: {a.images} pictures ({a.images_done} done), x2 with Real-ESRGAN###rm_hd", state["hd"])
            if not t["esrgan"]:
                imgui.text_colored("   Real-ESRGAN not found: Library tools > Settings > Real-ESRGAN (same tool as the banners).", 0.95, 0.6, 0.2)
        else:
            imgui.text_disabled(f"HD pictures: {a.hd_why}.")
            state["hd"] = False
        # 60 fps
        if a.videos:
            sampled = f"{a.videos_slow} of {a.videos_sampled} checked are below 50 fps" if a.videos_sampled else "fps not checked (ffprobe missing)"
            _, state["fps"] = imgui.checkbox(f"Smoother animations (x2 fps: 30 -> 60, 24 -> 48): {a.videos} videos, {sampled}###rm_fps", state["fps"])
            if not t["ffmpeg"]:
                imgui.text_colored("   ffmpeg not found: install it (ffmpeg.exe in PATH, Downloads or C:\\ffmpeg\\bin).", 0.95, 0.6, 0.2)
                state["fps"] = False
            else:
                imgui.text_disabled("   " + ("RIFE found: in-between frames computed by the graphics card (best)." if t["rife"] else
                                             "RIFE not found: ffmpeg motion interpolation (slower, can blur fast moves).\n"
                                             "   For better results put rife-ncnn-vulkan in Downloads (or set its path in Library tools)."))
        else:
            imgui.text_disabled("Smoother animations: no video in this game.")
            state["fps"] = False
        imgui.separator()
        running_here = job.running and job.game_id == game.id
        if running_here:
            elapsed = time.time() - job.started
            eta = ""
            if job.done and job.total:
                eta = f", about {int(elapsed / job.done * (job.total - job.done) / 60) + 1} min left"
            imgui.text(f"{job.phase}: {job.done}/{job.total}{eta}  ({job.made} made, {job.skipped} kept, {job.failed} failed)")
            if imgui.button("Cancel###rm_cancel"):
                job.cancel = True
        elif job.running or batch.running:
            imgui.text(f"Busy with {job.game_name or 'every game'}: {job.phase} {job.done}/{job.total}")
        else:
            if job.message and job.game_id == game.id:
                imgui.text_colored(job.message, 0.4, 0.85, 0.4)
                if job.failed and job.last_error:
                    imgui.text_colored(f"Last error: {job.last_error[:300]}", 0.95, 0.5, 0.4)
                    imgui.text_disabled("The failed files are tried again at the next Start.")
            can_hd = state["hd"] and t["esrgan"]
            if (can_hd or state["fps"]) and imgui.button("Start###rm_start"):
                if game.launch_state is not LaunchState.Idle:
                    state["msg"] = "Close the game first."
                else:
                    async_thread.run(start(game, bool(can_hd), bool(state["fps"])))
            if a.done_files or written_paths(game):
                imgui.same_line()
                if imgui.button("Undo (remove what was added)###rm_undo"):
                    err = undo(game)
                    state["msg"] = f"Not undone: {err}" if err else "Removed: the game is back as it was."
                    state["a"] = None
                    async_thread.run(load())
        if state["msg"]:
            imgui.text_colored(state["msg"], 0.95, 0.7, 0.2)

    utils.push_popup(utils.popup, f"HD & 60 fps - {game.name}", content, buttons=True, closable=True, outside=False,
                     popup_uuid=f"remaster_{game.id}")
