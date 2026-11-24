"""
Showcase: hover previews on the grid, "To play" shelf + badges, library statistics.

Hover preview
    Keep the mouse on a game's cover in the grid for half a second: its thread screenshots play as a
    slideshow (1.4 s each) in the card. Uses the same preview cache as the game's info popup.

To play shelf
    Above the grid: the games whose update you installed since you last played them (or never played),
    newest install first. Click = info popup, double click = play, right click = remove from the shelf.
    Cards in the grid get an UPDATE / NEW badge for the same games.

Statistics (Library tools > Stats)
    Play time per week (from F95Checker's own play log, kept from now on, plus launches recorded in the
    timeline before), top developers / tags / engines by play time, space used per drive, completion.
"""
import asyncio
import collections
import contextlib
import datetime
import math
import os
import pathlib
import time

from common.structs import (
    Game,
    LaunchState,
    TimelineEventType,
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

HOVER_DELAY = 0.45
SLIDE_SECONDS = 1.4


def prefs() -> dict:
    p = installdate.prefs()
    p.setdefault("hover_preview", True)
    p.setdefault("shelf", True)
    p.setdefault("shelf_open", True)
    return p


# ---------------------------------------------------------------------------
# Hover preview
# ---------------------------------------------------------------------------

_hover = {"id": None, "since": 0.0, "last": 0.0}
_loaded_by_hover: set[int] = set()


def needs_redraw() -> bool:
    """GUI redraw condition: keep animating while a slideshow is shown."""
    return _hover["id"] is not None and time.monotonic() - _hover["last"] < 0.3


def _release(game_id: int):
    game = globals.games.get(game_id)
    if game is None or game_id not in _loaded_by_hover:
        return
    _loaded_by_hover.discard(game_id)
    # (if its info popup is open meanwhile, the popup simply reloads them from the disk cache)
    game.cancel_preview_loading()
    game.unload_previews()


def cell_hover(game: Game, x: float, y: float, w: float, h: float, rounding: float, hovered: bool):
    """Called by draw_game_cell right after the cover: draws the slideshow over it when hovered."""
    if not prefs()["hover_preview"]:
        return
    now = time.monotonic()
    if not hovered:
        if _hover["id"] == game.id and now - _hover["last"] > 6:
            _release(game.id)
            _hover["id"] = None
        return
    if _hover["id"] != game.id:
        if _hover["id"] is not None:
            _release(_hover["id"])
        _hover.update(id=game.id, since=now)
    _hover["last"] = now
    shown = now - _hover["since"] - HOVER_DELAY
    if shown < 0 or not game.previews_urls:
        return
    if not game.previews_loaded and not game.previews_loading:
        game.preview_load_future = async_thread.run(game.load_previews_async())
        _loaded_by_hover.add(game.id)
    images = [img for img in game.preview_images if img is not None and not img.error]
    if not images:
        return
    index = int(shown / SLIDE_SECONDS) % len(images)
    image = images[index]
    _ = images[(index + 1) % len(images)].texture_id          # preload the next one
    texture = image.texture_id
    if texture == imagehelper.dummy_texture_id():
        return
    import imgui
    crop = image.crop_to_ratio(w / h, fit=globals.settings.fit_images)
    draw_list = imgui.get_window_draw_list()
    draw_list.add_image_rounded(texture, (x, y), (x + w, y + h), *crop, rounding=rounding, flags=imgui.DRAW_ROUND_CORNERS_TOP)
    # position dots
    count = min(len(images), 12)
    dot = max(3.0, h / 60)
    start = x + w / 2 - count * dot * 1.5
    for i in range(count):
        color = imgui.get_color_u32_rgba(1, 1, 1, 0.95 if i == index % count else 0.4)
        draw_list.add_circle_filled(start + i * dot * 3 + dot, y + h - dot * 2.5, dot, color)


# ---------------------------------------------------------------------------
# To play: badges and shelf
# ---------------------------------------------------------------------------

def to_play_state(game: Game) -> str:
    """'NEW' (installed, never played), 'UPDATE' (installed after your last play), or ''."""
    if not game.installed or game.custom:
        return ""
    installed_on = game.installed_on or 0
    if not installed_on:
        return ""
    if installdate.local_info().get(game.id, {}).get("shelf_dismissed", 0) >= installed_on:
        return ""
    last = game.last_launched.value or 0
    if not last:
        return "NEW"
    return "UPDATE" if installed_on > last + 300 else ""


def draw_badge(game: Game, x: float, y: float, w: float, scale: float):
    """Top-right badge on grid cards."""
    state = to_play_state(game)
    if not state:
        return
    import imgui
    draw_list = imgui.get_window_draw_list()
    text = f"{icons.new_box if state == 'NEW' else icons.update} {state}"
    size = imgui.calc_text_size(text)
    pad = 4 * scale
    x2 = x + w - 6 * scale
    x1 = x2 - size.x - pad * 2
    y1 = y + 6 * scale
    color = (0.15, 0.55, 0.95, 0.92) if state == "NEW" else (0.95, 0.55, 0.10, 0.92)
    draw_list.add_rect_filled(x1, y1, x2, y1 + size.y + pad, imgui.get_color_u32_rgba(*color), rounding=pad)
    draw_list.add_text(x1 + pad, y1 + pad / 2, imgui.get_color_u32_rgba(1, 1, 1, 1), text)


def _shelf_header(gui, opened: bool, count: int) -> bool:
    """Section title of the shelf: chevron, bold "To play", count in an accent pill, dim explanation, accent line.
    The whole row is the open / close button. True when clicked."""
    import imgui
    accent = globals.settings.style_accent
    draw_list = imgui.get_window_draw_list()
    x, y = imgui.get_cursor_screen_pos()
    width = imgui.get_content_region_available_width()
    imgui.push_font(imgui.fonts.bold)
    title_size = imgui.calc_text_size("To play")
    imgui.pop_font()
    pad = gui.scaled(6)
    row_h = title_size.y + pad * 2
    clicked = imgui.invisible_button("###shelf_toggle", width, row_h)
    hovered = imgui.is_item_hovered()
    rounding = gui.scaled(globals.settings.style_corner_radius)
    if hovered:
        draw_list.add_rect_filled(x, y, x + width, y + row_h, imgui.get_color_u32_rgba(*accent[:3], 0.12), rounding=rounding)
    text_col = imgui.get_color_u32_rgba(*imgui.style.colors[imgui.COLOR_TEXT])
    dim_col = imgui.get_color_u32_rgba(*imgui.style.colors[imgui.COLOR_TEXT_DISABLED])
    line_h = imgui.get_text_line_height()
    cx = x + pad
    chevron = icons.chevron_down if opened else icons.chevron_right
    draw_list.add_text(cx, y + (row_h - line_h) / 2, imgui.get_color_u32_rgba(*accent), chevron)
    cx += imgui.calc_text_size(chevron).x + pad
    imgui.push_font(imgui.fonts.bold)
    draw_list.add_text(cx, y + pad, text_col, "To play")
    imgui.pop_font()
    cx += title_size.x + pad
    # Count in a pill
    label = str(count)
    label_w = imgui.calc_text_size(label).x
    pill_h = line_h + gui.scaled(4)
    pill_y = y + (row_h - pill_h) / 2
    draw_list.add_rect_filled(cx, pill_y, cx + label_w + pad * 2, pill_y + pill_h,
                              imgui.get_color_u32_rgba(*accent[:3], 0.9), rounding=pill_h / 2)
    draw_list.add_text(cx + pad, pill_y + gui.scaled(2), imgui.get_color_u32_rgba(1, 1, 1, 1), label)
    cx += label_w + pad * 3
    draw_list.add_text(cx, y + (row_h - line_h) / 2, dim_col,
                       "updated since you last played, or installed and never played")
    # Accent line under the title, fading to the right
    line_y = y + row_h - gui.scaled(1)
    steps = 12
    for i in range(steps):
        draw_list.add_line(x + width * i / steps, line_y, x + width * (i + 1) / steps, line_y,
                           imgui.get_color_u32_rgba(*accent[:3], 0.6 * (1 - i / steps)), gui.scaled(1.5))
    if hovered:
        imgui.set_tooltip("Click to " + ("hide" if opened else "show") + " the shelf")
    imgui.dummy(0, gui.scaled(2))
    return clicked


def draw_shelf(gui, ids: list[int]):
    """Horizontal row of 'to play' games above the grid (games of the current tab / filters)."""
    import imgui
    if not prefs()["shelf"]:
        return
    games = [globals.games[i] for i in ids if i in globals.games and to_play_state(globals.games[i])]
    if not games:
        return
    games.sort(key=lambda g: -(g.installed_on or 0))
    opened = prefs()["shelf_open"]
    if _shelf_header(gui, opened, len(games)):
        prefs()["shelf_open"] = not opened
        installdate.save_prefs()
    if not opened:
        return
    card_h = gui.scaled(118)
    text_h = imgui.get_frame_height_with_spacing()
    img_h = card_h - text_h
    img_w = img_h * globals.settings.cell_image_ratio
    imgui.begin_child("###shelf", height=card_h + imgui.style.scrollbar_size + gui.scaled(6), border=False,
                      flags=imgui.WINDOW_HORIZONTAL_SCROLLING_BAR)
    rounding = gui.scaled(globals.settings.style_corner_radius)
    for i, game in enumerate(games):
        if i:
            imgui.same_line()
        imgui.begin_group()
        x, y = imgui.get_cursor_screen_pos()
        if game.image.error:
            imgui.dummy(img_w, img_h)
        else:
            crop = game.image.crop_to_ratio(img_w / img_h, fit=globals.settings.fit_images)
            game.image.render(img_w, img_h, *crop, rounding=rounding)
        hovered = imgui.is_item_hovered()
        draw_badge(game, x, y, img_w, gui.scaled(1))
        if hovered:
            if imgui.is_mouse_clicked(imgui.MOUSE_BUTTON_LEFT):
                utils.push_popup(gui.draw_game_info_popup, game, None)
            imgui.set_tooltip(f"{game.name}\nInstalled {game.installed}  -  "
                              f"{time.strftime('%d/%m/%Y', time.localtime(game.installed_on))}\n"
                              "Click: details, right click: remove from the shelf")
        if imgui.begin_popup_context_item(f"###shelf_ctx_{game.id}"):
            if imgui.selectable("Remove from the shelf (until the next update)", False)[0]:
                installdate.local_info().setdefault(game.id, {})["shelf_dismissed"] = game.installed_on
                installdate.save_local_info()
            imgui.end_popup()
        accent = globals.settings.style_accent
        imgui.push_style_color(imgui.COLOR_BUTTON, *accent[:3], 0.85)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *accent[:3], 1.0)
        imgui.push_style_color(imgui.COLOR_TEXT, 1.0, 1.0, 1.0, 1.0)
        play = imgui.small_button(f"{icons.play}###shelf_play_{game.id}")
        imgui.pop_style_color(3)
        if imgui.is_item_hovered():
            imgui.set_tooltip(f"Play {game.name}")
        if play:
            from modules import callbacks
            callbacks.launch_game(game)
        imgui.same_line()
        avail = img_w - imgui.get_cursor_screen_pos().x + x
        name = game.name
        while name and imgui.calc_text_size(game.name).x > avail and imgui.calc_text_size(name + "...").x > avail:
            name = name[:-1]
        imgui.text(name if name == game.name else name.rstrip() + "...")
        imgui.end_group()
    imgui.end_child()


# ---------------------------------------------------------------------------
# Play-time log (minutes per day per game, from now on)
# ---------------------------------------------------------------------------

_play_log: dict | None = None


def play_log() -> dict:
    global _play_log
    if _play_log is None:
        _play_log = installdate._load_json("play_log.json")
        if not isinstance(_play_log, dict):
            _play_log = {}
    return _play_log


def _save_play_log():
    with contextlib.suppress(OSError):
        installdate._save_json("play_log.json", play_log())


async def play_log_loop():
    """Adds one minute per running game every minute."""
    while True:
        await asyncio.sleep(60)
        playing = [g for g in globals.games.values() if g.launch_state is LaunchState.Playing]
        if not playing:
            continue
        day = play_log().setdefault(datetime.date.today().isoformat(), {})
        for game in playing:
            day[str(game.id)] = day.get(str(game.id), 0) + 1
        _save_play_log()


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _week_start(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=day.weekday())


def compute_stats() -> dict:
    games = list(globals.games.values())
    today = datetime.date.today()
    weeks = [_week_start(today) - datetime.timedelta(weeks=i) for i in range(11, -1, -1)]
    minutes = collections.Counter()
    for day, per_game in play_log().items():
        with contextlib.suppress(ValueError):
            week = _week_start(datetime.date.fromisoformat(day))
            minutes[week] += sum(per_game.values())
    sessions = collections.Counter()
    for game in games:
        for event in getattr(game, "timeline_events", []) or []:
            if event.type == TimelineEventType.GameLaunched:
                week = _week_start(datetime.date.fromtimestamp(event.timestamp.value))
                sessions[week] += 1
    by_dev = collections.Counter()
    by_tag = collections.Counter()
    by_engine = collections.Counter()
    by_status = collections.Counter()
    for game in games:
        hours = game.playtime / 3600
        if game.developer and hours:
            by_dev[game.developer] += hours
        for tag in game.tags:
            if hours:
                by_tag[getattr(tag, "text", str(tag))] += hours
        by_engine[installdate.local_engine(game) or getattr(game.type, "name", "?")] += 1
        by_status[getattr(game.status, "name", str(game.status))] += 1
    drives: dict[str, dict] = {}
    for game in games:
        exe = installdate._main_exe(game)
        if exe is None:
            continue
        drive = exe.anchor.upper()
        entry = drives.setdefault(drive, {"games": 0, "count": 0})
        entry["games"] += installdate.local_size(game)
        entry["count"] += 1
    for drive, entry in drives.items():
        with contextlib.suppress(OSError):
            import shutil
            usage = shutil.disk_usage(drive)
            entry.update(total=usage.total, free=usage.free)
    installed = [g for g in games if g.installed]
    return {
        "count": len(games),
        "installed": len(installed),
        "finished": sum(1 for g in games if g.finished),
        "playtime_h": sum(g.playtime for g in games) / 3600,
        "size": sum(installdate.local_size(g) for g in games),
        "weeks": weeks,
        "minutes": [minutes.get(w, 0) for w in weeks],
        "sessions": [sessions.get(w, 0) for w in weeks],
        "dev": by_dev.most_common(10),
        "tag": by_tag.most_common(10),
        "engine": by_engine.most_common(8),
        "status": by_status.most_common(),
        "drives": dict(sorted(drives.items())),
        "never_played": sum(1 for g in installed if not g.last_launched.value),
    }


def _accent():
    with contextlib.suppress(Exception):
        r, g, b, *_ = globals.settings.style_accent
        return r, g, b
    return 0.84, 0.18, 0.25


def _hbars(rows: list[tuple[str, float, str]], label_w: float, bar_w: float):
    import imgui
    if not rows:
        imgui.text_disabled("  (nothing yet)")
        return
    top = max(v for _, v, _ in rows) or 1
    draw_list = imgui.get_window_draw_list()
    r, g, b = _accent()
    for label, value, text in rows:
        while label and imgui.calc_text_size(label).x > label_w:
            label = label[:-2]
        imgui.text(label)
        imgui.same_line(position=label_w + imgui.style.item_spacing.x * 2)
        x, y = imgui.get_cursor_screen_pos()
        h = imgui.get_text_line_height()
        draw_list.add_rect_filled(x, y + 2, x + bar_w, y + h - 1, imgui.get_color_u32_rgba(1, 1, 1, 0.07), rounding=3)
        draw_list.add_rect_filled(x, y + 2, x + max(2.0, bar_w * value / top), y + h - 1, imgui.get_color_u32_rgba(r, g, b, 0.85), rounding=3)
        imgui.dummy(bar_w, h)
        imgui.same_line()
        imgui.text_disabled(text)


def _columns(values: list[float], labels: list[str], height: float, width: float, fmt):
    import imgui
    draw_list = imgui.get_window_draw_list()
    x, y = imgui.get_cursor_screen_pos()
    top = max(values) or 1
    n = len(values)
    slot = width / n
    r, g, b = _accent()
    for i, value in enumerate(values):
        bh = (height - 2 * imgui.get_text_line_height()) * value / top
        x1 = x + i * slot + slot * 0.18
        x2 = x + (i + 1) * slot - slot * 0.18
        base = y + height - imgui.get_text_line_height()
        draw_list.add_rect_filled(x1, base - bh, x2, base, imgui.get_color_u32_rgba(r, g, b, 0.85), rounding=3)
        if value:
            text = fmt(value)
            tw = imgui.calc_text_size(text).x
            draw_list.add_text(x1 + (x2 - x1 - tw) / 2, base - bh - imgui.get_text_line_height(),
                               imgui.get_color_u32_rgba(1, 1, 1, 0.8), text)
        lw = imgui.calc_text_size(labels[i]).x
        draw_list.add_text(x + i * slot + (slot - lw) / 2, base + 2, imgui.get_color_u32_rgba(1, 1, 1, 0.5), labels[i])
    imgui.dummy(width, height)


def open_stats_popup():
    import imgui
    stats = {"data": compute_stats(), "at": time.monotonic()}

    def content():
        if time.monotonic() - stats["at"] > 30:
            stats["data"], stats["at"] = compute_stats(), time.monotonic()
        s = stats["data"]
        scaled = globals.gui.scaled if globals.gui else (lambda v: v)
        width = scaled(760)
        imgui.push_font(imgui.fonts.bold)
        imgui.text(f"{s['count']} games   {s['installed']} installed   {s['finished']} finished "
                   f"({s['finished'] / max(1, s['count']) * 100:.0f} %)   {s['playtime_h']:.0f} h played   "
                   f"{installdate.size_display(s['size'])} on disk   {s['never_played']} installed but never played")
        imgui.pop_font()
        imgui.spacing()
        labels = [w.strftime("%d/%m") for w in s["weeks"]]
        if any(s["minutes"]):
            imgui.text("Play time per week (hours, F95Checker's play log)")
            _columns([m / 60 for m in s["minutes"]], labels, scaled(150), width, lambda v: f"{v:.1f}")
        else:
            imgui.text("Game launches per week (play time per week starts being recorded now)")
            _columns(s["sessions"], labels, scaled(150), width, lambda v: f"{v:.0f}")
        imgui.spacing()
        half = width / 2 - scaled(10)
        label_w = scaled(150)
        bar_w = half - label_w - scaled(70)
        imgui.begin_group()
        imgui.text("Top developers (hours played)")
        _hbars([(d, h, f"{h:.0f} h") for d, h in s["dev"]], label_w, bar_w)
        imgui.end_group()
        imgui.same_line(position=half + scaled(20))
        imgui.begin_group()
        imgui.text("Top tags (hours played)")
        _hbars([(t, h, f"{h:.0f} h") for t, h in s["tag"]], label_w, bar_w)
        imgui.end_group()
        imgui.spacing()
        imgui.begin_group()
        imgui.text("Space per drive (games / free)")
        draw_list = imgui.get_window_draw_list()
        r, g, b = _accent()
        for drive, d in s["drives"].items():
            total = d.get("total") or 1
            imgui.text(f"{drive}")
            imgui.same_line(position=scaled(40))
            x, y = imgui.get_cursor_screen_pos()
            h = imgui.get_text_line_height()
            bw = half - scaled(60)
            used_other = max(0, total - d.get("free", 0) - d["games"])
            draw_list.add_rect_filled(x, y + 2, x + bw, y + h - 1, imgui.get_color_u32_rgba(1, 1, 1, 0.07), rounding=3)
            gx = x + bw * d["games"] / total
            ox = gx + bw * used_other / total
            draw_list.add_rect_filled(x, y + 2, gx, y + h - 1, imgui.get_color_u32_rgba(r, g, b, 0.85), rounding=3)
            draw_list.add_rect_filled(gx, y + 2, ox, y + h - 1, imgui.get_color_u32_rgba(0.5, 0.5, 0.5, 0.6))
            imgui.dummy(bw, h)
            imgui.text_disabled(f"     {d['count']} games, {installdate.size_display(d['games'])} - "
                                f"{installdate.size_display(d.get('free', 0))} free of {installdate.size_display(total)}")
        imgui.end_group()
        imgui.same_line(position=half + scaled(20))
        imgui.begin_group()
        imgui.text("Engines")
        _hbars([(e, n, str(n)) for e, n in s["engine"]], label_w, bar_w)
        imgui.spacing()
        imgui.text("Status on F95zone")
        _hbars([(st, n, str(n)) for st, n in s["status"]], label_w, bar_w)
        imgui.end_group()

    utils.push_popup(utils.popup, "Library statistics", content, buttons=True, closable=True, outside=False, popup_uuid="showcase_stats")
