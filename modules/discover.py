"""
Discover: recommendations from your tastes, new games of your developers, mods & extras of your games.

Recommendations (Manage > Discover > Recommend):
 1. Your taste profile, from your library only: each game weighs by your rating (stars), your
    playtime, Finished and Installed. Tags of the games you like get positive weights, tags of
    the games you rated 1-2 stars negative ones (normalised so very common tags don't dominate).
 2. Candidates from F95zone "Latest Updates" (most liked + most recent pages) and the full list of
    the developers of your favourite games.
 3. Latest Updates gives tag NUMBERS, not names: their meaning is learned from your own games found in
    the same lists (tag number <-> tag names they have in F95Checker).
 4. Score = tags match + your developer + F95 rating + popularity. Games already in F95Checker are skipped.

Mods & extras (Manage > Discover > Mods, or right click > Mods & Extras): the "Extras" / mod / patch /
walkthrough links found in the download section of each game's thread (already fetched by F95Checker),
with what is already installed in your game folder.
"""
import asyncio
import collections
import dataclasses
import json
import math
import re
import time

from common.structs import (
    Game,
    MsgBox,
    SearchResult,
)
from external import (
    async_thread,
    error,
)
from modules import (
    api,
    globals,
    icons,
    installdate,
    msgbox,
    utils,
)

LOG_PREFIX = "[Discover]"


def _log(msg: str):
    print(f"{LOG_PREFIX} {msg}")


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------

def affinity(game: Game) -> float:
    """How much you like a game, from what F95Checker knows (-2 .. ~+6)."""
    score = 0.0
    if game.rating:
        score += (game.rating - 3) * 1.2                         # 1 star = -2.4 ... 5 stars = +2.4
    hours = game.playtime / 3600
    score += math.log1p(hours) * 0.9                             # 1 h = 0.6, 10 h = 2.2, 50 h = 3.5
    if game.finished:
        score += 0.8
    if game.installed:
        score += 0.3
    return score


@dataclasses.dataclass(slots=True)
class Profile:
    tags: dict[str, float]                  # tag name -> weight
    developers: dict[str, float]            # developer (lowercase) -> weight
    owned: set[int]


def build_profile() -> Profile:
    tag_sum = collections.defaultdict(float)
    tag_count = collections.Counter()
    devs = collections.defaultdict(float)
    owned = set()
    for game in globals.games.values():
        owned.add(game.id)
        a = affinity(game)
        for tag in game.tags:
            name = getattr(tag, "text", str(tag))
            tag_sum[name] += a
            tag_count[name] += 1
        if game.developer:
            devs[game.developer.strip().lower()] += a
    tags = {name: total / math.sqrt(tag_count[name]) for name, total in tag_sum.items()}
    top = max((abs(v) for v in tags.values()), default=1.0) or 1.0
    tags = {name: v / top for name, v in tags.items()}            # -1 .. +1
    return Profile(tags, dict(devs), owned)


# ---------------------------------------------------------------------------
# Candidates (F95zone latest updates)
# ---------------------------------------------------------------------------

async def _latest(sort: str, page: int, search: str = "", query: str = "") -> list[dict]:
    url = api.f95_latest_endpoint.format(
        cmd="list", cat="games", page=page, search=search or "search", query=query, sort=sort, rows=90, ts=int(time.time()),
    )
    res = await api.fetch("GET", url)
    api.raise_f95zone_error(res)
    data = json.loads(res)
    api.raise_f95zone_error(data)
    rows = data.get("msg", {}).get("data", []) if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict) and row.get("thread_id")]


@dataclasses.dataclass(slots=True)
class Recommendation:
    id: int
    title: str
    creator: str
    rating: float
    likes: int
    score: float
    why: list[str]
    url: str


def learn_tag_names(rows: list[dict]) -> dict[int, str]:
    """Tag number -> tag name, from your games present in the rows (tag present in all/most of the games having that number)."""
    seen = collections.Counter()
    pairs = collections.defaultdict(collections.Counter)
    name_freq = collections.Counter()
    games_seen = set()
    for row in rows:
        game = globals.games.get(int(row["thread_id"]))
        if game is None:
            continue
        names = {getattr(tag, "text", str(tag)) for tag in game.tags}
        if game.id not in games_seen:
            games_seen.add(game.id)
            name_freq.update(names)
        for number in set(row.get("tags") or []):
            seen[number] += 1
            for name in names:
                pairs[number][name] += 1
    total = max(1, len(games_seen))
    mapping = {}
    for number, count in seen.items():
        if count < 2:
            continue
        # name present in >= 75 % of the games with this number; ties: the most specific one
        # (lift = how much more often than in your library in general), then alphabetical for stability
        candidates = [(hits / count, (hits / count) / (name_freq[name] / total), name)
                      for name, hits in pairs[number].items() if hits / count >= 0.75]
        if candidates:
            best = max(candidates, key=lambda c: (round(c[0], 3), round(c[1], 3), c[2]))
            mapping[number] = best[2]
    return mapping


def score_rows(rows: list[dict], profile: Profile, tag_names: dict[int, str], dev_rows: set[int]) -> list[Recommendation]:
    # learned weights per tag number, for numbers whose name could not be learned
    number_weight = collections.defaultdict(float)
    number_count = collections.Counter()
    for row in rows:
        game = globals.games.get(int(row["thread_id"]))
        if game is None:
            continue
        a = affinity(game)
        for number in row.get("tags") or []:
            number_weight[number] += a
            number_count[number] += 1
    top = max((abs(v) / math.sqrt(number_count[n]) for n, v in number_weight.items()), default=1.0) or 1.0
    learned = {n: v / math.sqrt(number_count[n]) / top for n, v in number_weight.items()}

    out: dict[int, Recommendation] = {}
    for row in rows:
        tid = int(row["thread_id"])
        if tid in profile.owned or tid in out:
            continue
        why = []
        tag_score = 0.0
        liked = []
        for number in row.get("tags") or []:
            name = tag_names.get(number)
            if name is not None and name in profile.tags:
                w = profile.tags[name]
                tag_score += w
                if w > 0.25:
                    liked.append(name)
            elif number in learned:
                tag_score += learned[number] * 0.7
        tags_total = len(row.get("tags") or []) or 1
        tag_score = tag_score / math.sqrt(tags_total) * 3
        if liked:
            why.append("your tags: " + ", ".join(sorted(liked, key=lambda n: -profile.tags[n])[:5]))
        creator = (row.get("creator") or "").strip()
        dev_score = 0.0
        dev_weight = profile.developers.get(creator.lower(), 0.0)
        if dev_weight > 0:
            dev_score = min(3.0, 1.0 + dev_weight / 3)
            why.insert(0, f"by {creator}, a developer you play")
        rating = float(row.get("rating") or 0)
        likes = int(row.get("likes") or 0)
        quality = (rating - 3.5) * 0.8 + math.log10(likes + 1) * 0.35
        if rating >= 4.3:
            why.append(f"rated {rating:.1f}/5 on F95")
        score = tag_score + dev_score + quality
        out[tid] = Recommendation(tid, row.get("title") or f"Thread {tid}", creator, rating, likes, round(score, 2),
                                  why or ["popular"], api.f95_threads_page + str(tid))
    return sorted(out.values(), key=lambda r: -r.score)


@dataclasses.dataclass(slots=True)
class RecoState:
    running: bool = False
    message: str = ""
    items: list[Recommendation] = dataclasses.field(default_factory=list)
    new_from_devs: list[Recommendation] = dataclasses.field(default_factory=list)
    tag_names: int = 0


reco = RecoState()


async def recommend():
    if reco.running:
        return
    reco.running = True
    reco.message = "Reading your library..."
    try:
        if not await api.assert_login():
            reco.message = "Log in to F95zone first (F95Checker asks when you Refresh)."
            return
        profile = build_profile()
        rows: list[dict] = []
        for sort, pages in (("likes", 3), ("date", 2)):
            for page in range(1, pages + 1):
                reco.message = f"F95zone latest updates: {sort} page {page}..."
                rows += await _latest(sort, page)
        top_devs = [dev for dev, w in sorted(profile.developers.items(), key=lambda kv: -kv[1]) if w > 1.0][:10]
        dev_ids = set()
        for dev in top_devs:
            reco.message = f"Games of {dev}..."
            query = api.latest_updates_search_sanitize_query(dev)
            if not query:
                continue
            with contextlib_suppress():
                found = await _latest("date", 1, "creator", query)
                rows += found
                dev_ids |= {int(r["thread_id"]) for r in found}
        tag_names = learn_tag_names(rows)
        reco.tag_names = len(tag_names)
        ranked = score_rows(rows, profile, tag_names, dev_ids)
        reco.new_from_devs = [r for r in ranked if r.id in dev_ids][:20]
        reco.items = [r for r in ranked if r.id not in dev_ids][:30]
        reco.message = (f"{len(rows)} games looked at, {len(tag_names)} tag numbers understood from your library. "
                        f"Ranked by your tastes.")
    except Exception:
        reco.message = f"Error: {error.text()}"
        _log(error.traceback())
    finally:
        reco.running = False


class contextlib_suppress:
    """Like contextlib.suppress(Exception) but logs (one developer search failing must not stop the rest)."""
    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is not None and issubclass(kind, Exception):
            _log(f"skipped: {value}")
            return True
        return False


def open_recommend_popup():
    import imgui
    if not reco.items and not reco.running:
        async_thread.run(recommend())

    def row(r: Recommendation, key: str):
        if imgui.small_button(f"{icons.open_in_new}###open_{key}_{r.id}"):
            from modules import callbacks
            callbacks.open_webpage(r.url)
        imgui.same_line()
        if imgui.small_button(f"{icons.plus}###add_{key}_{r.id}"):
            from modules import callbacks
            async_thread.run(callbacks.add_games(SearchResult(r.title, r.creator, r.url, r.id)))
        imgui.same_line()
        imgui.text(f"{r.title}")
        imgui.same_line()
        imgui.text_disabled(f"- {r.creator}  {r.rating:.1f}/5  {r.likes} likes  ({'; '.join(r.why)})")

    def content():
        imgui.text_disabled(reco.message)
        if reco.running:
            imgui.text("Working...")
            return
        if imgui.button(f"{icons.reload} Recompute"):
            async_thread.run(recommend())
        imgui.text_disabled(f"{icons.open_in_new} opens the thread, {icons.plus} adds it to F95Checker. "
                            "Rate your games (stars) to sharpen the results.")
        if reco.new_from_devs:
            imgui.text("New / other games from developers you play:")
            for r in reco.new_from_devs:
                row(r, "dev")
            imgui.spacing()
        if reco.items:
            imgui.text("Recommended for you:")
            for r in reco.items:
                row(r, "rec")

    utils.push_popup(utils.popup, "Recommendations", content, buttons=True, closable=True, outside=False, popup_uuid="discover_reco")


# ---------------------------------------------------------------------------
# Mods & extras
# ---------------------------------------------------------------------------

MOD_WORDS = re.compile(
    r"\bmods?\b|patch|walk ?through|\bwt\b|cheat|gallery|unlock|incest|restor|uncensor|translation|translated|"
    r"\burm\b|universal ren|multi.?mod|save ?(file|game)|100 ?%|\bextras?\b|bonus|dlc|fan ?art|guide",
    re.IGNORECASE,
)
NOT_MOD = re.compile(r"\b(win|windows|linux|mac|android|pc|compressed|official)\b(?!.*mod)", re.IGNORECASE)
HOST_WORDS = re.compile(r"^(mega|pixeldrain|gofile|workupload|buzzheavier|datanodes|mixdrop|mediafire|anonfiles|"
                        r"google ?drive|gdrive|catbox|uploadhaven|nopy|fileditch|krakenfiles|torrent|itch|patreon|steam|"
                        r"subscribestar|discord|1fichier|uptobox|zippyshare|racaty|send|filen|mega\.nz)$", re.IGNORECASE)


@dataclasses.dataclass(slots=True)
class ModLink:
    game: Game
    label: str
    host: str
    url: str
    installed: str          # "" or the local file that looks like it


def mods_of(game: Game) -> list[ModLink]:
    out = []
    local = _local_mod_files(game)
    for section, mirrors in game.downloads or ():
        section_is_mod = bool(MOD_WORDS.search(section or "")) and not (NOT_MOD.search(section or "") and not MOD_WORDS.search(section or ""))
        for text, url in mirrors or ():
            text = (text or "").strip()
            is_host = bool(HOST_WORDS.match(text))
            if section_is_mod or (not is_host and MOD_WORDS.search(text)):
                label = text if not is_host else section
                if section_is_mod and not is_host:
                    label = f"{section.strip(': ')}: {text}"
                key = re.sub(r"[^a-z]", "", label.lower())
                installed = next((name for name in local if _looks_same(key, name)), "")
                out.append(ModLink(game, label.strip(), text if is_host else "", url, installed))
    return out


def _local_mod_files(game: Game) -> list[str]:
    exe = installdate._main_exe(game)
    if exe is None:
        return []
    names = installdate._listdir(exe.parent / "game") if (exe.parent / "game").is_dir() else installdate._listdir(exe.parent)
    return [n for n in names if installdate.MOD_RE.search(n) or re.search(r"mod|patch|walkthrough|gallery", n, re.IGNORECASE)]


def _looks_same(key: str, filename: str) -> bool:
    name = re.sub(r"[^a-z]", "", filename.lower())
    for word in ("walkthrough", "gallery", "cheat", "incest", "urm", "universal", "multimod", "patch", "mod"):
        if word in key and word in name:
            return True
    return False


def open_mods_popup(games: list[Game] = None):
    import imgui
    if games is None:
        games = sorted((g for g in globals.games.values() if g.installed), key=lambda g: g.name.lower())
    found = [(game, mods_of(game)) for game in games]
    found = [(game, mods) for game, mods in found if mods]
    show_installed = [True]

    def content():
        if not found:
            imgui.text("No mod / patch / walkthrough / extras link found in the download section of these threads.\n"
                       "(F95Checker reads it during a full recheck; a lot of mods are posted in separate threads,\n"
                       "search \"<game> mod\" on F95zone for those.)")
            return
        imgui.text_disabled("Links from the download section of each thread (Extras, mods, patches, walkthroughs).\n"
                            "Mods go into the game's \"game\" folder (Ren'Py). Back up your saves before installing a mod.")
        _, show_installed[0] = imgui.checkbox("Show the ones that look installed", show_installed[0])
        for game, mods in found:
            visible = [m for m in mods if show_installed[0] or not m.installed]
            if not visible:
                continue
            imgui.text(game.name)
            for i, mod in enumerate(visible):
                imgui.text("   ")
                imgui.same_line()
                if imgui.small_button(f"{icons.open_in_new}###mod_open_{game.id}_{i}"):
                    from modules import callbacks
                    url = mod.url if not mod.url.startswith("//") else game.url
                    callbacks.open_webpage(url)
                imgui.same_line()
                if imgui.small_button(f"{icons.download}###mod_dl_{game.id}_{i}"):
                    async_thread.run(send_mod_to_jdownloader(mod))
                imgui.same_line()
                imgui.text(mod.label + (f" ({mod.host})" if mod.host else ""))
                if mod.installed:
                    imgui.same_line()
                    imgui.text_colored(f"looks installed: {mod.installed}", 0.3, 0.85, 0.35)

    utils.push_popup(utils.popup, "Mods & extras", content, buttons=True, closable=True, outside=False, popup_uuid="discover_mods")


async def send_mod_to_jdownloader(mod: ModLink):
    """Resolves F95zone masked links like the update download does, then hands the link to JDownloader."""
    from modules import callbacks, webview
    link = mod.url
    game = mod.game
    try:
        if link.startswith("//") or link.startswith(f"{api.f95_host}/masked/"):
            if not await api.assert_login():
                return
            if link.startswith("//"):
                args = ("xpath_redirect", game.url, "//*[contains(@class,'message-threadStarterPost')][1]" + link)
            else:
                args = ("css_redirect", link, "a.host_link")
            with await webview.start(*args, title=f"Retrieve link: {mod.label}", size=(520, 480), pipe=True, minimal=True) as pipe:
                link = await pipe.get_async()
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, installdate.send_to_jdownloader, link, game.url):
            utils.push_popup(msgbox.msgbox, "Mod", f"{game.name} - {mod.label}: sent to JDownloader.\n"
                             "Once extracted, copy its files into the game folder (usually <game>\\game).", MsgBox.info)
        else:
            callbacks.clipboard_copy(link)
            utils.push_popup(msgbox.msgbox, "Mod", "JDownloader is not running: the link was copied to the clipboard.", MsgBox.warn)
    except Exception:
        utils.push_popup(msgbox.msgbox, "Mod", f"Could not retrieve the link:\n{error.text()}", MsgBox.error, more=error.traceback())
