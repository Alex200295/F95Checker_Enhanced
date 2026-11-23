"""
3D & living pictures (right click on a Ren'Py game > 3D & living pictures...).

Each picture of the game gets a depth map (how far each pixel is), computed once on the graphics card by
Depth Anything V2 Small (ONNX Runtime, DirectML), and stored in game\\_f95depth. A small script in the game
(zz_f95checker_depth.rpy) uses them, Shift+P in the game switching between:
    off -> living pictures (near things move slightly over the background, slow camera breathing)
        -> 3D side by side (Virtual Desktop on the Quest 3: 3D mode "Half SBS") -> off
Ren'Py 8.1 or newer (shaders on every layer). Animations get the depth of a frame from their middle.
Depth maps are centred on the main subject (it stays still / on the screen plane) and their near edges are grown
a little before smoothing (no halo around characters).
Nothing written in the game's own files or saves: Remove deletes game\\_f95depth and the script.
"""
import asyncio
import contextlib
import dataclasses
import os
import pathlib
import shutil
import time

from common.structs import (
    Game,
    LaunchState,
)
from external import (
    async_thread,
)
from modules import (
    globals,
    installdate,
    remaster,
    toygen,
    utils,
)

LOG_PREFIX = "[Depth]"
DEPTH_DIR = "_f95depth"
SCRIPT_FILE = "zz_f95checker_depth.rpy"
# Base (finer details, cleaner edges) preferred when both are there; Small is enough and 4 times faster
MODEL_NAMES = ("depth_anything_v2_base.onnx", "depth_anything_v2_vitb.onnx",
               "depth_anything_v2_small.onnx", "depth_anything_v2_vits.onnx", "depth_anything_v2_small_fp16.onnx")
DEPTH_VERSION = "2"             # depth maps made before (version file missing / older) are made again
VERSION_FILE = "version.txt"
LIVE_MOTIONS = ("mouse", "sway")
MIN_VERSION = (8, 1)
MIN_SIDE = 200                  # smaller pictures (buttons, icons) are left out
MAX_DEPTH_WIDTH = 640           # depth maps are smooth: stored small (about 15 KB each)
NET_SIDE = 518                  # Depth Anything input: shorter side, multiple of 14

SCRIPT = r'''# Added by F95Checker (right click > 3D & living pictures...). Delete this file (and game\_f95depth) to remove it.
# Shift+P in the game: off -> living pictures -> 3D side by side (Virtual Desktop: 3D mode "Half SBS") -> off.
# Uses the depth maps in game\_f95depth (one per picture and per animation, made by F95Checker). Nothing is written in your saves.
init 999 python:
    import collections as _f95d_collections
    import math as _f95d_math
    import time as _f95d_time
    import renpy.display.im as _f95d_im
    import renpy.display.render as _f95d_R

    _F95D_LIVE_AMP = F95D_LIVE_AMP_VALUE      # living pictures: how far the background moves behind the subject (share of the width)
    _F95D_3D_AMP = F95D_3D_AMP_VALUE          # 3D: separation between the two eyes (share of the width)
    _F95D_MOTION = "F95D_MOTION_VALUE"        # living pictures: "mouse" = the view follows the mouse slowly, "sway" = slow drift
    _F95D_FOCUS = 0.5                         # depth maps are centred on the main subject: it stays still / on the screen plane
    _F95D_MODES = ("off", "live", "3d")
    _F95D_NAMES = {"off": "3D & living pictures: off", "live": "Living pictures", "3d": "3D side by side (Half SBS)"}

    if renpy.version_tuple >= (8, 1, 0) and not getattr(_f95d_im, "_f95_depth_on", False):
        _f95d_im._f95_depth_on = True
        import builtins as _f95d_builtins

        class _F95DepthState(object):
            def __init__(self):
                self.pass_depth = False
                self.known = {}
                self.view = (0.0, 0.0)
                self.view_time = 0.0

            def mode(self):
                m = persistent._f95_depth_mode
                return m if m in _F95D_MODES else "off"

            def depth_for(self, filename):
                if not isinstance(filename, str):
                    filename = filename[0] if filename else ""
                if filename in self.known:
                    return self.known[filename]
                found = None
                name = filename.replace("\\", "/")
                for candidate in ("_f95depth/" + name + ".webp", "_f95depth/images/" + name + ".webp"):
                    if renpy.loadable(candidate):
                        found = _f95d_im.Image(candidate)
                        break
                self.known[filename] = found
                return found

            def view_dir(self):
                """Where the 'camera' looks, -1..1 on each axis, smoothed (no sudden move)."""
                now = _f95d_time.time()
                if now - self.view_time < 0.004:
                    return self.view
                if _F95D_MOTION == "mouse":
                    mx, my = renpy.get_mouse_pos()
                    tx = max(-1.0, min(1.0, (float(mx) / config.screen_width - 0.5) * 2.0))
                    ty = max(-1.0, min(1.0, (float(my) / config.screen_height - 0.5) * 2.0)) * 0.6
                else:
                    tx = _f95d_math.sin(now * 2.0 * _f95d_math.pi / 24.0)
                    ty = 0.3 * _f95d_math.sin(now * 2.0 * _f95d_math.pi / 37.0)
                dt = min(0.1, max(0.0, now - self.view_time))
                k = 1.0 - _f95d_math.exp(-dt * 2.2)
                vx, vy = self.view
                self.view = (vx + (tx - vx) * k, vy + (ty - vy) * k)
                self.view_time = now
                return self.view

        _f95d_state = _F95DepthState()

        _f95d_vars = """
            uniform sampler2D tex0;
            uniform sampler2D tex1;
            uniform float u_f95_amp;
            uniform float u_f95_focus;
            uniform vec2 u_f95_dir;
            attribute vec2 a_tex_coord;
            varying vec2 v_tex_coord;
        """
        renpy.register_shader("f95.plain", variables=_f95d_vars, vertex_200="v_tex_coord = a_tex_coord;",
                              fragment_200="gl_FragColor = texture2D(tex0, v_tex_coord);")
        # no depth map: the picture stays on the screen plane (neither pushed back nor popping out)
        renpy.register_shader("f95.flat", variables=_f95d_vars, vertex_200="v_tex_coord = a_tex_coord;", fragment_200="""
            float a = texture2D(tex0, v_tex_coord).a;
            gl_FragColor = vec4(u_f95_focus * a, u_f95_focus * a, u_f95_focus * a, a);
        """)
        # living pictures: the subject stays still, what is behind / in front of it slides a little (parallax)
        renpy.register_shader("f95.live", variables=_f95d_vars, vertex_200="v_tex_coord = a_tex_coord;", fragment_200="""
            vec2 base = 0.5 + (v_tex_coord - 0.5) * (1.0 - 2.0 * u_f95_amp);
            vec2 uv = base;
            for (int i = 0; i < 4; i++) {
                vec4 dep = texture2D(tex1, clamp(uv, 0.0, 1.0));
                float d = dep.a > 0.0 ? dep.r / dep.a : u_f95_focus;
                uv = base - (d - u_f95_focus) * u_f95_dir;
            }
            gl_FragColor = texture2D(tex0, clamp(uv, 0.0, 1.0));
        """)
        renpy.register_shader("f95.sbs", variables=_f95d_vars, vertex_200="v_tex_coord = a_tex_coord;", fragment_200="""
            float eye = v_tex_coord.x < 0.5 ? 1.0 : -1.0;
            vec2 uv = vec2(v_tex_coord.x < 0.5 ? v_tex_coord.x * 2.0 : v_tex_coord.x * 2.0 - 1.0, v_tex_coord.y);
            float s = 0.0;
            for (int i = 0; i < 4; i++) {
                s = (texture2D(tex1, clamp(uv - vec2(eye * s, 0.0), 0.0, 1.0)).r - u_f95_focus) * u_f95_amp * 0.5;
            }
            gl_FragColor = texture2D(tex0, clamp(uv - vec2(eye * s, 0.0), 0.0, 1.0));
        """)

        def _f95d_textured(renders, w, h, shader, uniforms):
            from renpy.gl2.gl2mesh2 import Mesh2
            rv = renpy.Render(w, h)
            for r in renders:
                rv.blit(r, (0, 0))
            rv.mesh = Mesh2.texture_rectangle(0.0, 0.0, w, h, 0.0, 0.0, 1.0, 1.0)
            rv.add_shader(shader)
            for k, v in uniforms.items():
                rv.add_uniform(k, v)
            return rv

        def _f95d_wrap(d, cr, depth, st, at):
            """A picture / animation drawn with its depth: depth pass of 3D, or living pictures."""
            state = _f95d_state
            cw, ch = cr.get_size()
            if state.pass_depth:
                if depth is None:
                    return _f95d_textured([cr], cw, ch, "f95.flat", {"u_f95_focus": _F95D_FOCUS})
                dr = _f95d_im.render_for_texture(depth, cw, ch, st, at)
                return _f95d_textured([dr], cw, ch, "f95.plain", {})
            if state.mode() == "live" and depth is not None:
                dr = _f95d_im.render_for_texture(depth, cw, ch, st, at)
                fade = min(1.0, st / 1.5)
                fade = fade * fade * (3.0 - 2.0 * fade)          # a new picture starts still, the motion eases in
                vx, vy = state.view_dir()
                amp = _F95D_LIVE_AMP * fade
                renpy.redraw(d, 0)
                return _f95d_textured([cr, dr], cw, ch, "f95.live",
                                      {"u_f95_amp": amp, "u_f95_focus": _F95D_FOCUS, "u_f95_dir": (vx * amp, vy * amp)})
            return cr

        _f95d_orig_image_render = _f95d_im.Image.render

        def _f95d_image_render(self, w, h, st, at):
            cr = _f95d_orig_image_render(self, w, h, st, at)
            state = _f95d_state
            if state.mode() == "off" and not state.pass_depth:
                return cr
            filename = getattr(self, "filename", "") or ""
            if filename.startswith("_f95depth/"):
                return cr
            try:
                return _f95d_wrap(self, cr, state.depth_for(filename), st, at)
            except Exception:
                return cr

        _f95d_im.Image.render = _f95d_image_render

        try:
            import renpy.display.video as _f95d_video
            _f95d_orig_movie_render = _f95d_video.Movie.render

            def _f95d_movie_render(self, w, h, st, at):
                cr = _f95d_orig_movie_render(self, w, h, st, at)
                state = _f95d_state
                if state.mode() == "off" and not state.pass_depth:
                    return cr
                try:
                    play = getattr(self, "_play", None)
                    return _f95d_wrap(self, cr, state.depth_for(play) if play else None, st, at)
                except Exception:
                    return cr

            _f95d_video.Movie.render = _f95d_movie_render
        except Exception:
            pass

        class _F95Stereo(renpy.Displayable):
            """The master layer twice: once as it is, once with every picture replaced by its depth map."""

            def __init__(self, child, **kwargs):
                super(_F95Stereo, self).__init__(**kwargs)
                self.child = child

            def render(self, w, h, st, at):
                cr = renpy.render(self.child, w, h, st, at)
                old = _f95d_R.render_cache
                try:
                    _f95d_R.render_cache = _f95d_collections.defaultdict(_f95d_builtins.dict)
                    _f95d_state.pass_depth = True
                    try:
                        dr = renpy.render(self.child, w, h, st, at)
                    finally:
                        _f95d_state.pass_depth = False
                        _f95d_R.render_cache = old
                    return _f95d_textured([cr, dr], w, h, "f95.sbs", {"u_f95_amp": _F95D_3D_AMP, "u_f95_focus": _F95D_FOCUS})
                except Exception:
                    return cr       # this Ren'Py does it differently: the game stays playable in 2D

            def event(self, ev, x, y, st):
                return self.child.event(ev, x, y, st)

            def visit(self):
                return [self.child]

        def _f95d_master(child):
            if _f95d_state.mode() != "3d":
                return child
            return _F95Stereo(child)

        def _f95d_flat(child):
            # interface layers: the same picture for both eyes, each squeezed into its half
            if _f95d_state.mode() != "3d":
                return child
            return Fixed(Transform(child, xzoom=0.5), Transform(child, xzoom=0.5, xpos=0.5), fit_first=False)

        for _f95d_layer in list(config.layers) + list(config.top_layers):
            config.layer_transforms.setdefault(_f95d_layer, []).append(_f95d_master if _f95d_layer == "master" else _f95d_flat)

        def _f95d_set(mode):
            persistent._f95_depth_mode = mode
            try:
                _f95d_R.render_cache.clear()        # pictures drawn again with the new mode
            except Exception:
                pass
            renpy.restart_interaction()

        def _f95d_cycle():
            mode = _F95D_MODES[(_F95D_MODES.index(_f95d_state.mode()) + 1) % len(_F95D_MODES)]
            _f95d_set(mode)
            renpy.notify(_F95D_NAMES[mode])

        config.keymap["f95_depth"] = ["shift_K_p"]
        config.underlay.append(renpy.Keymap(f95_depth=_f95d_cycle))
'''


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("depth_model_path", "")
    p.setdefault("depth_live_amp", 0.6)       # % of the picture width
    p.setdefault("depth_3d_amp", 3.0)
    p.setdefault("depth_live_motion", "mouse")
    if p.get("depth_prefs_v", 1) < 2:         # living pictures were too strong (nausea): gentler default once
        p["depth_live_amp"] = min(float(p.get("depth_live_amp", 0.6)), 0.6)
        p["depth_prefs_v"] = 2
    return p


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

_model_cache: tuple[float, str, pathlib.Path | None] = (float("-inf"), "", None)


def find_model() -> pathlib.Path | None:
    """Library tools path (file or folder), else <app>/tools/depth (shipped with the release), <data>/depth, Downloads (2 levels deep)."""
    global _model_cache
    custom = prefs()["depth_model_path"].strip().strip('"')
    if _model_cache[1] == custom and time.monotonic() - _model_cache[0] < 30:
        return _model_cache[2]
    found = None
    if custom:
        path = pathlib.Path(custom)
        if path.is_file():
            found = path
        elif path.is_dir():
            found = next((p for n in MODEL_NAMES + ("model.onnx", "model_fp16.onnx") for p in [path / n] if p.is_file()), None)
    else:
        for folder in (globals.self_path / "tools" / "depth", globals.data_path / "depth", pathlib.Path.home() / "Downloads", globals.data_path):
            for pattern in [n for n in MODEL_NAMES] + [f"*/{n}" for n in MODEL_NAMES] + [f"*/*/{n}" for n in MODEL_NAMES]:
                with contextlib.suppress(OSError):
                    found = next((p for p in folder.glob(pattern) if p.is_file()), None)
                if found:
                    break
            if found:
                break
    _model_cache = (time.monotonic(), custom, found)
    return found


def forget_model():
    global _model_cache
    _model_cache = (float("-inf"), "", None)


def runtime_error() -> str:
    """'' if ONNX Runtime can be used, else why."""
    try:
        import onnxruntime  # noqa: F401
        return ""
    except Exception as exc:
        return f"ONNX Runtime missing in this build ({type(exc).__name__})"


class Estimator:
    """Depth Anything V2 (ONNX): picture -> depth map, near = 1, far = 0."""

    def __init__(self, model: pathlib.Path):
        import onnxruntime
        available = onnxruntime.get_available_providers()
        providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider") if p in available]
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(str(model), sess_options=options, providers=providers)
        self.input = self.session.get_inputs()[0].name
        self.provider = self.session.get_providers()[0]
        _log(f"model {model.name} on {self.provider}")

    def __call__(self, image):
        """PIL RGB picture -> float32 numpy array (same aspect, network size), 0 far .. 1 near."""
        import numpy
        from PIL import Image
        w, h = image.size
        scale = NET_SIDE / min(w, h)
        nw, nh = max(14, round(w * scale / 14) * 14), max(14, round(h * scale / 14) * 14)
        x = numpy.asarray(image.resize((nw, nh), Image.BICUBIC), dtype=numpy.float32) / 255.0
        x = (x - numpy.array([0.485, 0.456, 0.406], dtype=numpy.float32)) / numpy.array([0.229, 0.224, 0.225], dtype=numpy.float32)
        x = x.transpose(2, 0, 1)[None].astype(numpy.float32)
        out = self.session.run(None, {self.input: x})[0]
        depth = numpy.asarray(out, dtype=numpy.float32).reshape(out.shape[-2], out.shape[-1])
        lo, hi = numpy.percentile(depth, 1), numpy.percentile(depth, 99)
        if hi - lo < 1e-6:
            return numpy.zeros_like(depth)
        return numpy.clip((depth - lo) / (hi - lo), 0.0, 1.0)


def depth_name(key: str) -> str:
    """'images/bg room.png' -> '_f95depth/images/bg room.png.webp' (relative to game/, as the script looks for it)."""
    return f"{DEPTH_DIR}/{key}.webp"


def centre_on_subject(depth):
    """Depth remapped so the main subject (centre of the picture, weighted) is at 0.5: it stays still in living
    pictures and on the screen plane in 3D, the background goes behind it, closer things come forward."""
    import numpy
    h, w = depth.shape
    yy, xx = numpy.mgrid[0:h, 0:w]
    weight = numpy.exp(-(((xx / max(1, w - 1) - 0.5) / 0.28) ** 2 + ((yy / max(1, h - 1) - 0.45) / 0.33) ** 2))
    order = numpy.argsort(depth, axis=None)
    cumulative = numpy.cumsum(weight.ravel()[order])
    subject = float(depth.ravel()[order][numpy.searchsorted(cumulative, cumulative[-1] / 2)])
    subject = min(0.85, max(0.15, subject))
    return numpy.where(depth < subject, depth / subject * 0.5, 0.5 + (depth - subject) / (1.0 - subject) * 0.5)


def depth_picture(rgba, estimate):
    """RGBA PIL picture -> RGBA depth map (grey = depth, alpha of the picture: sprites stay sprites)."""
    import numpy
    from PIL import Image, ImageFilter
    alpha = rgba.getchannel("A")
    rgb = Image.new("RGB", rgba.size, (0, 0, 0))
    rgb.paste(rgba, mask=alpha)
    depth = centre_on_subject(estimate(rgb))
    width = min(rgba.width, MAX_DEPTH_WIDTH)
    height = max(1, round(rgba.height * width / rgba.width))
    grey = Image.fromarray((numpy.clip(depth, 0, 1) * 255).astype(numpy.uint8), "L").resize((width, height), Image.BICUBIC)
    # near things grown by a few pixels before smoothing: the stretch at their outline falls on the background
    # (blurry anyway) instead of eating into the character's edge: no halo around them when the view moves
    grow = max(3, (width // 160) | 1)
    grey = grey.filter(ImageFilter.MaxFilter(grow)).filter(ImageFilter.GaussianBlur(max(1.5, width / 320)))
    return Image.merge("RGBA", (grey, grey, grey, alpha.resize((width, height), Image.BILINEAR)))


def video_frame(media, t: dict):
    """A frame from the middle of an animation (PIL RGBA), or None. PyAV (in F95Checker) or ffmpeg."""
    import io
    import subprocess
    import tempfile
    from PIL import Image
    work = pathlib.Path(tempfile.mkdtemp(prefix="f95dframe_"))
    try:
        src = work / ("v" + pathlib.PurePosixPath(media.key).suffix)
        if media.entry:
            src.write_bytes(media.read())
        else:
            src = media.path
        with contextlib.suppress(ImportError):
            import av
            with av.open(str(src)) as container:
                stream = container.streams.video[0]
                frames = []
                for i, frame in enumerate(container.decode(stream)):
                    frames.append(frame)
                    if i >= 400:
                        break
                if frames:
                    return frames[len(frames) // 2].to_image().convert("RGBA")
            return None
        ffmpeg = (t or {}).get("ffmpeg")
        if not ffmpeg:
            return None
        info = remaster.video_info(src, (t or {}).get("ffprobe")) if (t or {}).get("ffprobe") else {}
        middle = max(0.0, float(info.get("duration") or 0) / 2)
        out = work / "frame.png"
        subprocess.run([str(ffmpeg), "-v", "error", "-y", "-ss", f"{middle:.3f}", "-i", str(src), "-frames:v", "1", str(out)],
                       capture_output=True, timeout=120, **remaster._no_window())
        if not out.is_file():
            return None
        with Image.open(out) as im:
            return im.convert("RGBA")
    except Exception:
        return None
    finally:
        import shutil as _shutil
        _shutil.rmtree(work, ignore_errors=True)


def make_depth(media, dst: pathlib.Path, estimate, tools: dict | None = None) -> str:
    """'' / 'skip' / an error. Pictures, and animations (a frame from their middle: the camera rarely moves)."""
    import io
    from PIL import Image
    try:
        if pathlib.PurePosixPath(media.key).suffix.lower() in tuple(toygen.VIDEO_EXTS):
            rgba = video_frame(media, tools)
            if rgba is None:
                return "skip"
        else:
            with Image.open(io.BytesIO(media.read())) as im:
                if getattr(im, "is_animated", False):
                    return "skip"
                im.load()
                rgba = im.convert("RGBA")
        if min(rgba.size) < MIN_SIDE:
            return "skip"
        out = depth_picture(rgba, estimate)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        out.save(tmp, "WEBP", quality=85, method=4)
        os.replace(tmp, dst)
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# A game
# ---------------------------------------------------------------------------

def game_root(game: Game) -> pathlib.Path | None:
    exe = installdate._main_exe(game)
    if exe is None or not (exe.parent / "game").is_dir() or not (exe.parent / "renpy").is_dir():
        return None
    return exe.parent


def eligible(root: pathlib.Path) -> str:
    """'' if the game can have 3D & living pictures, else why."""
    version = remaster.renpy_version(root)
    if not version:
        return "Ren'Py version not found"
    if version[:2] < MIN_VERSION:
        return f"needs Ren'Py 8.1 or newer (this game: {'.'.join(map(str, version))})"
    return ""


def wanted_images(game_dir: pathlib.Path) -> list:
    """Pictures and animations of the game (the interface left out)."""
    media = remaster.list_images(game_dir) + remaster.list_videos(game_dir)
    return [m for m in media if not m.key.lower().startswith(("gui/", DEPTH_DIR.lower() + "/"))]


def maps_outdated(game_dir: pathlib.Path) -> bool:
    """Depth maps made by an older F95Checker (before subject centring / clean edges): made again."""
    folder = game_dir / DEPTH_DIR
    if not folder.is_dir():
        return False
    with contextlib.suppress(OSError):
        return (folder / VERSION_FILE).read_text(encoding="utf-8").strip() != DEPTH_VERSION
    return True


def script_content() -> str:
    p = prefs()
    live = max(0.1, min(3.0, float(p["depth_live_amp"]))) / 100
    sbs = max(0.5, min(8.0, float(p["depth_3d_amp"]))) / 100
    motion = p["depth_live_motion"] if p["depth_live_motion"] in LIVE_MOTIONS else "mouse"
    return (SCRIPT.replace("F95D_LIVE_AMP_VALUE", f"{live:.4f}").replace("F95D_3D_AMP_VALUE", f"{sbs:.4f}")
            .replace("F95D_MOTION_VALUE", motion))


def write_script(root: pathlib.Path) -> bool:
    path = root / "game" / SCRIPT_FILE
    content = script_content()
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    path.with_suffix(".rpyc").unlink(missing_ok=True)
    return True


def installed(game: Game) -> bool:
    root = game_root(game)
    return root is not None and (root / "game" / SCRIPT_FILE).is_file()


def count_done(root: pathlib.Path) -> int:
    folder = root / "game" / DEPTH_DIR
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.rglob("*.webp"))


def remove(game: Game) -> str:
    root = game_root(game)
    if root is None:
        return "game folder not found"
    if game.launch_state is not LaunchState.Idle:
        return f"{game.name} is running: close it first"
    if job.running and job.game_id == game.id:
        return "being processed: cancel first"
    for name in (SCRIPT_FILE, SCRIPT_FILE + "c"):
        with contextlib.suppress(FileNotFoundError):
            (root / "game" / name).unlink()
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(root / "game" / DEPTH_DIR)
    _log(f"'{game.name}': removed")
    return ""


def refresh_scripts():
    """Library tools strength changed: the script of every game that has it is written again."""
    count = 0
    for game in list(globals.games.values()):
        root = game_root(game)
        if root is not None and (root / "game" / SCRIPT_FILE).is_file():
            with contextlib.suppress(OSError):
                count += write_script(root)
    return count


# ---------------------------------------------------------------------------
# Processing (one game, or every game)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Job:
    running: bool = False
    cancel: bool = False
    game_id: int = 0
    game_name: str = ""
    done: int = 0
    total: int = 0
    made: int = 0
    skipped: int = 0
    failed: int = 0
    games_done: int = 0
    games_total: int = 0
    started: float = 0.0
    message: str = ""
    provider: str = ""


job = Job()


def _process(games: list, estimator_factory=None):
    """Blocking. Depth maps of every picture without one, then the script. Resumable (what exists is kept)."""
    model = find_model()
    estimate = (estimator_factory or Estimator)(model)
    job.provider = getattr(estimate, "provider", "")
    job.games_total = len(games)
    for game in games:
        if job.cancel:
            return
        root = game_root(game)
        if root is None or eligible(root) or game.launch_state is not LaunchState.Idle:
            job.games_done += 1
            continue
        game_dir = root / "game"
        job.game_id, job.game_name = game.id, game.name
        redo = maps_outdated(game_dir)
        todo = [m for m in wanted_images(game_dir) if redo or not (game_dir / depth_name(m.key)).is_file()]
        job.done, job.total = 0, len(todo)
        tools = remaster.tools() if any(pathlib.PurePosixPath(m.key).suffix.lower() in tuple(toygen.VIDEO_EXTS) for m in todo) else {}
        (game_dir / DEPTH_DIR).mkdir(parents=True, exist_ok=True)
        if redo:
            with contextlib.suppress(OSError):
                (game_dir / DEPTH_DIR / VERSION_FILE).unlink()
        for media in todo:
            if job.cancel or remaster._drive_full(root):
                break
            error = make_depth(media, game_dir / depth_name(media.key), estimate, tools)
            if error == "skip":
                job.skipped += 1
            elif error:
                job.failed += 1
                _log(f"'{game.name}': {media.key}: {error}")
            else:
                job.made += 1
            job.done += 1
        if job.done == job.total and not job.cancel:
            with contextlib.suppress(OSError):
                (game_dir / DEPTH_DIR / VERSION_FILE).write_text(DEPTH_VERSION, encoding="utf-8")
        if count_done(root):
            with contextlib.suppress(OSError):
                write_script(root)
        job.games_done += 1


async def start(games: list):
    if job.running:
        return
    job.__init__()
    job.running, job.started = True, time.time()
    try:
        await asyncio.get_running_loop().run_in_executor(None, _process, games)
        verb = "Cancelled" if job.cancel else "Done"
        job.message = f"{verb}: {job.made} depth map(s) made, {job.skipped} small pictures left out, {job.failed} failed."
    except Exception as exc:
        job.message = f"Stopped: {type(exc).__name__}: {exc}"
        _log(repr(exc))
    finally:
        job.running = False
        _log(job.message)


def needs_redraw() -> bool:
    return job.running


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _draw_requirements(imgui) -> bool:
    error = runtime_error()
    model = find_model()
    if error:
        imgui.text_colored(f"{error}: update F95Checker.", 0.95, 0.6, 0.2)
    if not model:
        imgui.text_colored("Depth model not found: Library tools > Settings > Depth model (see the guide).", 0.95, 0.6, 0.2)
    return not error and model is not None


def _draw_progress(imgui):
    elapsed = time.time() - job.started
    eta = ""
    if job.done and job.total > job.done:
        left = elapsed / max(1, job.done) * (job.total - job.done)
        eta = f", about {int(left // 60) + 1} min left for this game"
    if job.games_total > 1:
        imgui.text(f"Game {min(job.games_done + 1, job.games_total)} / {job.games_total}: {job.game_name}")
    imgui.text(f"Depth maps: {job.done} / {job.total}{eta}  ({job.made} made, {job.failed} failed){'  - ' + job.provider if job.provider else ''}")
    if imgui.button("Cancel###dp_cancel"):
        job.cancel = True


def open_popup(game: Game):
    import imgui
    state = {"msg": ""}

    def content():
        imgui.text_disabled("Each picture and animation of the game gets a depth map, made once by your graphics card.\n"
                            "In the game, Shift+P: off -> living pictures (the view follows the mouse slowly, the subject stays still)\n"
                            "    -> 3D side by side (VR headset, e.g. Quest 3 + Virtual Desktop: 3D mode \"Half SBS\") -> off.\n"
                            "The game's own files and your saves are not touched.")
        imgui.separator()
        root = game_root(game)
        if root is None:
            imgui.text("Not a Ren'Py game (or its executable was not found).")
            return
        why = eligible(root)
        if why:
            imgui.text(f"Not possible: {why}.")
            return
        done = count_done(root)
        imgui.text(f"Depth maps: {done}" + ("  - script installed (Shift+P in the game)" if installed(game) else ""))
        if done and maps_outdated(root / "game"):
            imgui.text_colored("Made by an older F95Checker: Start makes them again (cleaner edges, subject centred, animations).",
                               0.95, 0.7, 0.2)
        if job.running:
            if job.game_id == game.id or job.games_total == 1:
                _draw_progress(imgui)
            else:
                imgui.text(f"Busy with {job.game_name or 'every game'}.")
            return
        if job.message and job.game_id == game.id:
            imgui.text_colored(job.message, 0.4, 0.85, 0.4)
        if _draw_requirements(imgui) and imgui.button("Start###dp_start"):
            if game.launch_state is not LaunchState.Idle:
                state["msg"] = "Close the game first."
            else:
                async_thread.run(start([game]))
        if done or installed(game):
            imgui.same_line()
            if imgui.button("Remove###dp_remove"):
                err = remove(game)
                state["msg"] = f"Not removed: {err}" if err else "Removed: the game is back as it was."
        if state["msg"]:
            imgui.text_colored(state["msg"], 0.95, 0.7, 0.2)

    utils.push_popup(utils.popup, f"3D & living pictures - {game.name}", content, buttons=True, closable=True, outside=False,
                     popup_uuid=f"depth_{game.id}")


def open_all_popup():
    import imgui

    def content():
        imgui.text_disabled("Every Ren'Py 8.1+ game of the library, one after the other, in the background.\n"
                            "Close F95Checker or Cancel any time: Start all goes on where it stopped.\n"
                            f"Stops writing on a drive with less than {remaster.MIN_FREE_BYTES // 1024 ** 3} GB free.")
        imgui.separator()
        if job.running:
            _draw_progress(imgui)
            return
        if job.message:
            imgui.text_colored(job.message, 0.4, 0.85, 0.4)
        if _draw_requirements(imgui) and imgui.button("Start all###dpa_start"):
            games = sorted(globals.games.values(), key=lambda g: g.name.lower())
            async_thread.run(start(games))

    utils.push_popup(utils.popup, "3D & living pictures - every game", content, buttons=True, closable=True, outside=False,
                     popup_uuid="depth_all")
