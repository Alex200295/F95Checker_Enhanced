"""
Visuals: sharper banners, "Continue" banner above the grid, colour of each game.

Sharper banners (Library tools > Sharper banners)
    Banners narrower than 1600 px are enlarged once with Real-ESRGAN (realesrgan-ncnn-vulkan.exe: offline,
    on the graphics card) into <F95Checker data>/images/upscaled/<id>.webp. The original is never touched:
    untick the option and the originals are shown again. A banner changed on F95zone is enlarged again
    (signature = file name, size and date of the original). Animated banners (.gif) are left as they are.
Continue banner
    Above the grid: the installed game of the current tab you played last, wide, with Play and Details.
Game colours
    Main colour of each banner (Pillow): frame and light tint of its card in the grid. A blurred, darker copy of
    the banner fills the empty sides of the image in the game's info popup.

Everything is computed in the background (one banner at a time) and kept in <F95Checker data>/visuals.json.
"""
import asyncio
import colorsys
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

from common.structs import (
    Game,
    Os,
)
from external import (
    async_thread,
    imagehelper,
)
from modules import (
    globals,
    icons,
    installdate,
    utils,
)

UPSCALE_BELOW = 1600          # px: wider banners are sharp enough
UPSCALE_MAX_WIDTH = 2560      # px: the enlarged copy is never wider (x4 of a 1000 px banner is resized down)
MODELS = {
    "realesrgan-x4plus": "Photos / 3D renders (most games)",
    "realesrgan-x4plus-anime": "Drawings / anime",
    "realesr-animevideov3": "Fast, drawings",
}
EXE_NAME = "realesrgan-ncnn-vulkan.exe"


def _log(msg: str):
    print(f"[Visuals] {msg}")


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("upscale", True)
    p.setdefault("upscale_path", "")
    p.setdefault("upscale_model", "realesrgan-x4plus")
    p.setdefault("hero", True)
    p.setdefault("game_colors", True)
    return p


# ---------------------------------------------------------------------------
# Index: <data>/visuals.json  {game id: {"sig", "color", "blur", "up", "err"}}
# ---------------------------------------------------------------------------

_index: dict | None = None
_redraw = False


def index() -> dict:
    global _index
    if _index is None:
        try:
            data = json.loads((globals.data_path / "visuals.json").read_text(encoding="utf-8"))
            _index = data if isinstance(data, dict) else {}
        except Exception:
            _index = {}
    return _index


def save_index():
    try:
        tmp = globals.data_path / "visuals.json.tmp"
        tmp.write_text(json.dumps(index()), encoding="utf-8")
        os.replace(tmp, globals.data_path / "visuals.json")
    except OSError as exc:
        _log(f"Cannot save visuals.json: {exc}")


def needs_redraw() -> bool:
    """GUI draw condition: one frame after a banner / colour changed."""
    global _redraw
    if _redraw:
        _redraw = False
        return True
    return False


def upscaled_dir() -> pathlib.Path:
    return globals.images_path / "upscaled"


def blurred_dir() -> pathlib.Path:
    return globals.images_path / "blurred"


def source_image(game_id: int) -> pathlib.Path | None:
    """The banner F95Checker downloaded (not its compressed .ktx.zst copies)."""
    files = [p for p in globals.images_path.glob(f"{game_id}.*") if p.suffix.lower() != ".zst" and p.is_file()]
    if not files:
        return None
    files.sort(key=lambda p: (p.suffix.lower() == ".gif", p.name))
    return files[0]


def signature(path: pathlib.Path) -> str:
    st = path.stat()
    return f"{path.name}|{st.st_size}|{int(st.st_mtime)}"


# ---------------------------------------------------------------------------
# Real-ESRGAN
# ---------------------------------------------------------------------------

_exe_cache: tuple[float, str, pathlib.Path | None] = (float("-inf"), "", None)


def find_upscaler() -> pathlib.Path | None:
    """Library tools path (file or its folder), else <app>/tools/realesrgan (shipped with the release), <data>/realesrgan, Downloads (2 levels deep)."""
    global _exe_cache
    custom = prefs()["upscale_path"].strip().strip('"')
    if _exe_cache[1] == custom and time.monotonic() - _exe_cache[0] < 30:
        return _exe_cache[2]
    found = None
    names = (EXE_NAME, EXE_NAME[:-4])
    if custom:
        path = pathlib.Path(custom)
        for candidate in ([path / n for n in names] if path.is_dir() else [path]):
            if candidate.is_file():
                found = candidate
                break
    else:
        for folder in (globals.self_path / "tools" / "realesrgan", globals.data_path / "realesrgan", pathlib.Path.home() / "Downloads", globals.data_path):
            for pattern in [n for n in names] + [f"*/{n}" for n in names] + [f"*/*/{n}" for n in names]:
                with contextlib.suppress(OSError):
                    found = next((p for p in folder.glob(pattern) if p.is_file()), None)
                if found:
                    break
            if found:
                break
    _exe_cache = (time.monotonic(), custom, found)
    return found


def _no_window() -> dict:
    if globals.os is Os.Windows:
        return dict(creationflags=subprocess.CREATE_NO_WINDOW)
    return {}


def image_info(path: pathlib.Path) -> tuple[int, int, bool]:
    from PIL import Image
    with Image.open(path) as im:
        return im.width, im.height, bool(getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1)


def upscale(src: pathlib.Path, dst: pathlib.Path, exe: pathlib.Path, model: str) -> str:
    """Enlarges src x4 with Real-ESRGAN (then down to UPSCALE_MAX_WIDTH), writes dst (.webp). '' or an error."""
    from PIL import Image
    work = pathlib.Path(tempfile.mkdtemp(prefix="f95up_"))
    try:
        inp, out = work / "in.png", work / "out.png"
        with Image.open(src) as im:
            width = im.width
            im.convert("RGB").save(inp)     # every model reads png (F95 banners are often webp)
        proc = subprocess.run(
            [str(exe), "-i", str(inp), "-o", str(out), "-n", model, "-s", "4"],
            cwd=str(exe.parent), capture_output=True, timeout=600, **_no_window(),
        )
        if proc.returncode != 0 or not out.is_file():
            text = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip().splitlines()
            return (text[-1] if text else "") or f"Real-ESRGAN exit code {proc.returncode}"
        with Image.open(out) as big:
            img = big.convert("RGB")
        target = min(width * 4, UPSCALE_MAX_WIDTH)
        if img.width > target:
            img = img.resize((target, round(img.height * target / img.width)), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.stem + ".tmp")
        img.save(tmp, "WEBP", quality=92, method=4)
        os.replace(tmp, dst)
        return ""
    except subprocess.TimeoutExpired:
        return "Real-ESRGAN took more than 10 minutes"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Colour and blurred copy
# ---------------------------------------------------------------------------

def _first_frame(im):
    with contextlib.suppress(Exception):
        im.seek(0)
    return im.convert("RGB")


def main_color(src: pathlib.Path) -> list[float]:
    """Most present colour of the banner that is neither grey nor almost black, made readable on a dark UI."""
    from PIL import Image
    with Image.open(src) as im:
        small = _first_frame(im)
    small.thumbnail((64, 64))
    quantized = small.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()
    best, best_score = (128, 128, 128), -1.0
    for count, i in quantized.getcolors() or []:
        r, g, b = palette[i * 3:i * 3 + 3]
        high, low = max(r, g, b), min(r, g, b)
        saturation = (high - low) / high if high else 0.0
        value = high / 255
        score = count * (0.25 + saturation) * (0.3 + min(value, 0.85))
        if value < 0.12 or (saturation < 0.08 and value > 0.92):
            score *= 0.2
        if score > best_score:
            best, best_score = (r, g, b), score
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in best))
    return [round(c, 4) for c in colorsys.hsv_to_rgb(h, min(s, 0.85), max(v, 0.55))]


def make_blurred(src: pathlib.Path, dst: pathlib.Path):
    from PIL import Image, ImageEnhance, ImageFilter
    with Image.open(src) as im:
        img = _first_frame(im)
    img.thumbnail((480, 480))
    img = ImageEnhance.Brightness(img.filter(ImageFilter.GaussianBlur(14))).enhance(0.55)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".tmp")
    img.save(tmp, "JPEG", quality=85)
    os.replace(tmp, dst)


def _drop_files(game_id: int):
    for folder in (upscaled_dir(), blurred_dir()):
        for path in folder.glob(f"{game_id}.*"):
            with contextlib.suppress(OSError):
                path.unlink()


def process(game_id: int, allow_upscale: bool) -> bool:
    """Colour, blurred copy and (allow_upscale) enlarged banner of one game, if missing or outdated. True if changed."""
    src = source_image(game_id)
    key = str(game_id)
    entry = index().get(key)
    if src is None:
        if isinstance(entry, dict):        # banner reset / removed: back to nothing
            _drop_files(game_id)
            index().pop(key, None)
            return True
        return False
    sig = signature(src)
    changed = False
    if not isinstance(entry, dict) or entry.get("sig") != sig:
        if isinstance(entry, dict):
            _drop_files(game_id)
        entry = index()[key] = {"sig": sig}
        changed = True
    if "color" not in entry:
        try:
            entry["color"] = main_color(src)
        except Exception as exc:
            entry["color"] = None
            _log(f"Colour of {src.name}: {exc}")
        changed = True
    if "blur" not in entry:
        try:
            make_blurred(src, blurred_dir() / f"{game_id}.jpg")
            entry["blur"] = True
        except Exception as exc:
            entry["blur"] = False
            _log(f"Blurred copy of {src.name}: {exc}")
        changed = True
    if allow_upscale and "up" not in entry:
        exe = find_upscaler()
        if exe is not None:
            try:
                width, _, animated = image_info(src)
            except Exception as exc:
                width, animated = 0, False
                entry["up"], entry["err"] = "error", str(exc)
            if "up" in entry:
                pass
            elif animated:
                entry["up"] = "animated"
            elif width >= UPSCALE_BELOW:
                entry["up"] = "big"
            else:
                error_text = upscale(src, upscaled_dir() / f"{game_id}.webp", exe, prefs()["upscale_model"])
                entry["up"] = "error" if error_text else "ok"
                if error_text:
                    entry["err"] = error_text
                    _log(f"Real-ESRGAN failed for {src.name}: {error_text}")
            changed = True
    return changed


def retry_upscale_errors():
    for entry in index().values():
        if isinstance(entry, dict) and entry.get("up") == "error":
            entry.pop("up", None)
            entry.pop("err", None)


def redo_upscale():
    """New model chosen: every banner is enlarged again."""
    for key, entry in index().items():
        if isinstance(entry, dict) and entry.pop("up", None) is not None:
            entry.pop("err", None)
            with contextlib.suppress(OSError):
                (upscaled_dir() / f"{key}.webp").unlink()


def upscale_stats() -> dict:
    stats = {"ok": 0, "big": 0, "animated": 0, "error": 0, "todo": 0}
    for game_id in globals.games:
        entry = index().get(str(game_id))
        state = entry.get("up") if isinstance(entry, dict) else None
        stats[state if state in stats else "todo"] += 1
    return stats


# ---------------------------------------------------------------------------
# Which image each game shows
# ---------------------------------------------------------------------------

_helpers: dict[int, dict] = {}     # game id -> {"orig": ImageHelper, "sig": str, "up": ImageHelper, "blur": ImageHelper}


def apply(game: Game):
    """game.image = enlarged copy (option on and ready) or the original banner."""
    global _redraw
    helpers = _helpers.setdefault(game.id, {})
    if "orig" not in helpers:
        helpers["orig"] = game.image
    entry = index().get(str(game.id))
    entry = entry if isinstance(entry, dict) else {}
    if helpers.get("sig") != entry.get("sig"):
        # new banner downloaded: forget the old textures
        if helpers.get("sig") is not None:
            for name in ("orig", "up", "blur"):
                if helpers.get(name) is not None:
                    helpers[name].reload()
        helpers["sig"] = entry.get("sig")
    target = helpers["orig"]
    if prefs()["upscale"] and entry.get("up") == "ok" and (upscaled_dir() / f"{game.id}.webp").is_file():
        if helpers.get("up") is None:
            helpers["up"] = imagehelper.ImageHelper(upscaled_dir(), glob=f"{game.id}.*")
        target = helpers["up"]
    if game.image is not target:
        game.image = target
        _redraw = True


def apply_all():
    for game in list(globals.games.values()):
        with contextlib.suppress(Exception):
            apply(game)


def blurred_image(game: Game):
    """Blurred, darker banner (info popup background) or None."""
    if not prefs()["game_colors"] or getattr(globals, "safe_screen", False):
        return None
    entry = index().get(str(game.id))
    if not (isinstance(entry, dict) and entry.get("blur")):
        return None
    helpers = _helpers.setdefault(game.id, {})
    if helpers.get("blur") is None:
        if not (blurred_dir() / f"{game.id}.jpg").is_file():
            return None
        helpers["blur"] = imagehelper.ImageHelper(blurred_dir(), glob=f"{game.id}.*")
    return helpers["blur"]


def color(game: Game) -> tuple[float, float, float] | None:
    if not prefs()["game_colors"]:
        return None
    entry = index().get(str(game.id))
    value = entry.get("color") if isinstance(entry, dict) else None
    return tuple(value) if value else None


def card_colors(game: Game) -> tuple[int, int] | None:
    """(background, frame) colours of the game's card in the grid, or None (default look)."""
    import imgui
    rgb = color(game)
    if rgb is None:
        return None
    base = imgui.style.colors[imgui.COLOR_TABLE_ROW_BACKGROUND_ALT]
    mix = 0.14
    bg = [base[i] * (1 - mix) + rgb[i] * mix for i in range(3)]
    return (imgui.get_color_u32_rgba(*bg, max(base[3], 0.9)),
            imgui.get_color_u32_rgba(*rgb, 0.75))


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------

async def _loop():
    loop = asyncio.get_running_loop()
    await asyncio.sleep(10)
    apply_all()
    while True:
        try:
            allow = bool(prefs()["upscale"])
            changed_any = False
            for game_id in list(globals.games):
                changed = await loop.run_in_executor(None, process, game_id, allow)
                if changed:
                    changed_any = True
                    save_index()
                    game = globals.games.get(game_id)
                    if game is not None:
                        apply(game)
            if changed_any:
                apply_all()
        except Exception as exc:
            _log(f"Background work: {exc!r}")
        await asyncio.sleep(60)


@contextlib.contextmanager
def setup():
    future = async_thread.run(_loop())
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            future.cancel()


# ---------------------------------------------------------------------------
# "Continue" banner above the grid
# ---------------------------------------------------------------------------

def _ago(timestamp: float) -> str:
    days = int((time.time() - timestamp) // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def draw_hero(gui, ids: list[int]):
    """Wide banner of the installed game of this tab you played last, with Play / Details."""
    import imgui
    if not prefs()["hero"]:
        return
    games = [globals.games[i] for i in ids if i in globals.games]
    games = [g for g in games if g.installed and g.last_launched.value]
    if not games:
        return
    game = max(games, key=lambda g: g.last_launched.value)
    width = imgui.get_content_region_available_width()
    height = gui.scaled(150)
    rounding = gui.scaled(globals.settings.style_corner_radius)
    x, y = imgui.get_cursor_screen_pos()
    draw_list = imgui.get_window_draw_list()
    rgb = color(game) or tuple(globals.settings.style_accent[:3])
    image = game.image
    if image.missing or image.error:
        draw_list.add_rect_filled(x, y, x + width, y + height, imgui.get_color_u32_rgba(*rgb, 0.35), rounding=rounding)
        imgui.dummy(width, height)
    else:
        crop = image.crop_to_ratio(width / height, fit=False)
        image.render(width, height, *crop, rounding=rounding)
    image_hovered = imgui.is_item_hovered()
    # Dark gradient on the left for the text
    strip = width * 0.62
    dark, clear = imgui.get_color_u32_rgba(0, 0, 0, 0.82), imgui.get_color_u32_rgba(0, 0, 0, 0.0)
    left = getattr(imgui, "DRAW_ROUND_CORNERS_LEFT", imgui.DRAW_ROUND_CORNERS_TOP_LEFT | imgui.DRAW_ROUND_CORNERS_BOTTOM_LEFT)
    draw_list.add_rect_filled(x, y, x + rounding, y + height, dark, rounding=rounding, flags=left)
    if hasattr(draw_list, "add_rect_filled_multicolor"):
        draw_list.add_rect_filled_multicolor(x + rounding, y, x + strip, y + height, dark, clear, clear, dark)
    else:   # same gradient in 16 bands
        band = (strip - rounding) / 16
        for i in range(16):
            alpha = 0.82 * (1 - i / 16)
            draw_list.add_rect_filled(x + rounding + i * band, y, x + rounding + (i + 1) * band + 0.5, y + height,
                                      imgui.get_color_u32_rgba(0, 0, 0, alpha))
    pad = gui.scaled(14)
    imgui.set_cursor_screen_pos((x + pad, y + pad))
    imgui.begin_group()
    imgui.text_colored("CONTINUE", *rgb)
    imgui.push_font(imgui.fonts.big)
    imgui.text(game.name)
    imgui.pop_font()
    details = [f"Last played {_ago(game.last_launched.value)}", game.version]
    if getattr(game, "playtime", 0):
        details.append(f"{game.playtime / 3600:.1f} h played")
    imgui.text_disabled("  -  ".join(d for d in details if d))
    imgui.spacing()
    imgui.push_style_color(imgui.COLOR_BUTTON, *rgb, 0.85)
    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *rgb, 1.0)
    imgui.push_style_color(imgui.COLOR_TEXT, 0.05, 0.05, 0.05, 1.0)
    if imgui.button(f"{icons.play} Play###hero_play"):
        from modules import callbacks
        callbacks.launch_game(game)
    imgui.pop_style_color(3)
    imgui.same_line()
    if imgui.button(f"{icons.information_outline} Details###hero_info"):
        utils.push_popup(gui.draw_game_info_popup, game, None)
    imgui.end_group()
    if image_hovered and imgui.is_mouse_clicked(imgui.MOUSE_BUTTON_LEFT) and not imgui.is_any_item_hovered():
        utils.push_popup(gui.draw_game_info_popup, game, None)
    imgui.set_cursor_screen_pos((x, y + height))
    imgui.dummy(width, gui.scaled(6))
