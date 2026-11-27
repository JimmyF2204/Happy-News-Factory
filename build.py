#!/usr/bin/env python3
"""Smiling Times: builds a static positive-news website and RSS feed."""
import calendar
import difflib
import hashlib
import html
import json
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
OUT = ROOT / "docs"


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


CONFIG = load(ROOT / "config.json", {})
SOURCES = load(ROOT / "sources.json", [])
OVERRIDES = load(ROOT / "overrides.json", {"approve": [], "block": []})

SITE_URL = CONFIG.get("site_url", "").rstrip("/")
SHOW_IMAGES = CONFIG.get("show_images", True)
PUBLISH_NEUTRAL = CONFIG.get("publish_neutral", True)
UA = f"SmilingTimesBot/1.0 (+{SITE_URL})"
DATA_VERSION = 2
KEEP_DAYS, SHOW_MAX, PER_FEED, RECHECK_DAYS, RECHECK_BATCH = 14, 150, 25, 7, 15
NOW = time.time()
socket.setdefaulttimeout(20)


def rx(words):
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


def found(pattern, text):
    return sorted({m.lower() for m in pattern.findall(text)})


# Words about death and violence: a headline with these is only shown for peace news.
SEVERE = rx([
    r"kill\w*", r"murder\w*", "dead", r"death\w*", "dies", "died", r"shoot\w*", "shot",
    r"stab\w*", r"terror\w*", r"rap(?:e|ed|ist)", r"abus\w*", r"suicid\w*", r"genocid\w*",
    r"hostage\w*", r"massacre\w*", r"kidnap\w*",
    r"mord\w*", r"mörda\w*", r"självmord\w*", r"folkmord\w*", r"dråp\w*", r"död\w*",
    "avled", "avlidit", r"skjut\w*", "sköt", r"knivhugg\w*", r"knivskur\w*", r"våldt\w*",
    r"övergrepp\w*", "gisslan", r"massak\w*",
])
# Other bad-news words: a headline with these needs a hopeful angle to be shown.
NEGATIVE = rx([
    r"attack\w*", "wars?", r"bomb\w*", r"missile\w*", r"invasion\w*", r"invad\w*",
    r"assault\w*", r"crash\w*", r"disaster\w*", r"earthquake\w*", r"flood\w*", r"wildfire\w*",
    r"hurricane\w*", r"drought\w*", "famine", "crisis", "crises", r"recession\w*",
    r"layoff\w*", "inflation", r"bankrupt\w*", r"scandal\w*", r"outrage\w*", r"arrest\w*",
    r"victim\w*", r"traged\w*", "tragic", r"lawsuit\w*", r"fraud\w*", r"collaps\w*",
    r"extinct\w*", r"threat\w*", r"fear\w*", r"warn\w*", r"injur\w*", r"wound\w*",
    r"jail\w*", r"prison\w*", "guilty", r"convict\w*", r"riot\w*", r"sanction\w*",
    r"outbreak\w*", r"epidemic\w*", r"pandemic\w*", r"corrupt\w*", "stolen", r"theft\w*",
    r"robber\w*", r"evacuat\w*", "toxic", r"pollut\w*", r"racis\w*", r"violen\w*",
    r"krig\w*", r"våld\w*", r"misshand\w*", r"krasch\w*", r"olyck\w*", r"katastrof\w*",
    r"jordbäv\w*", r"översvämning\w*", r"skogsbr\w*", "branden", "bränder", r"orkan\w*",
    r"svält\w*", "kris", "krisen", "kriser", "kriserna", r"lågkonjunktur\w*", "varsel",
    "varslar", "varslas", "varslade", r"uppsägning\w*", r"konkurs\w*", r"skandal\w*",
    "grips", "greps", "gripits", r"anhåll\w*", r"häkta\w*", r"åtal\w*", "döms", r"dömd\w*",
    r"misstänk\w*", "offren", "offret", r"brottsoffer\w*", r"tragedi\w*", r"tragisk\w*",
    r"bedrägeri\w*", r"kollaps\w*", r"utrot\w*", r"utdö\w*", r"rån\w*", "brott", "brottet",
    r"brottslig\w*", r"sprängning\w*", r"sprängd\w*", "hotar", r"hotad\w*", "hotet",
    "varnar", r"varning\w*", r"skadad\w*", r"stöld\w*", r"fängelse\w*", r"missil\w*",
    r"larmet", "oro", "oron",
])
PEACE = rx([
    "ceasefire", "cease-fire", "truce", r"peace (?:deal|agreement|accord|treaty|talks)",
    r"war (?:ends|is over)", r"ends? (?:the )?war", r"reconcil\w*",
    "vapenvila", r"vapenstillestånd\w*", r"fredsavtal\w*", r"fredssamtal\w*",
    r"kriget (?:är )?slut", r"försoning\w*",
])
POSITIVE = rx([
    r"sav(?:e|es|ed|ing)", r"rescu\w*", r"reunit\w*", r"recover\w*", r"restor\w*",
    r"breakthrough\w*", r"cure[sd]?", r"heal\w*", r"donat\w*", r"volunteer\w*", r"kind\w*",
    r"generous\w*", r"hero(?:es|ic|ine)?", r"celebrat\w*", "wins?", "won", r"success\w*",
    r"thriv\w*", r"rebound\w*", r"first[- ]ever", r"protect\w*", r"hope\w*", r"joy\w*",
    "happy", r"happi\w*", r"smil\w*", r"peace\w*", r"improv\w*", r"achiev\w*",
    r"milestone\w*", "comeback", r"adopt\w*", r"help(?:s|ed|ing)?", r"inspir\w*",
    r"renewable\w*", "clean energy", r"rewild\w*", "conservation", r"vaccine\w*",
    r"graduat\w*", r"uplift\w*", r"award\w*", r"prize\w*", r"honou?r\w*", r"discover\w*",
    r"invent\w*", r"innovat\w*", r"reopen\w*", r"reviv\w*", "freed", r"surviv\w*", "born",
    r"wedding\w*", r"love\w*", r"friend\w*", r"laugh\w*", r"delight\w*", "amazing",
    r"wonder\w*", "beautiful", r"festival\w*", r"champion\w*", "gold", r"medal\w*",
    r"triumph\w*", "victory", "progress", r"boost\w*", r"solution\w*", r"pioneer\w*",
    "good news", r"thank\w*", "grateful", r"dream\w*", r"welcom\w*",
    r"rädda\w*", r"räddning\w*", r"återförena\w*", r"återhämt\w*", r"återställ\w*",
    r"återfunn\w*", r"överlev\w*", r"genombrott\w*", "botar", "botad", "botade", "botas",
    "botemedel", r"friskförklar\w*", r"hjälp\w*", r"donera\w*", r"skänk\w*", r"volontär\w*",
    r"hjält\w*", r"fira\w*", "vinner", "vann", r"seger\w*", r"segra\w*", r"lyck\w*",
    r"framgång\w*", r"succé\w*", r"rekord\w*", "hopp", "hoppet", "hoppas", r"hoppfull\w*",
    r"gläd\w*", "glad", "glada", r"kärlek\w*", "fred", "freden", r"förbättra\w*",
    r"skydda\w*", r"milstolpe\w*", r"adoptera\w*", "gåva", "gåvor", r"förnybar\w*",
    "solenergi", r"vaccin\w*", "examen", r"hyll\w*", "prisas", r"prisad\w*", r"belön\w*",
    r"historisk\w*", "äntligen", "goda nyheter", "omtanke", r"generös\w*", r"vänlig\w*",
    r"upptäck\w*", r"guld\w*", r"medalj\w*", r"dröm\w*", r"vacker\w*", r"fantastisk\w*",
])
OPINION = rx(["opinion", "op-ed", "editorial", "sponsored", "advertorial",
              "debatt", "krönika", "insändare", "annons", "ledare"])
TOPICS = [
    ("Peace & diplomacy", rx([
        r"peace\w*", "ceasefire", "truce", r"reconcil\w*", r"treat(?:y|ies)",
        "fred", "freden", "vapenvila", r"fredsavtal\w*", r"försoning\w*"])),
    ("Health & medicine", rx([
        r"health\w*", r"medic\w*", r"cure[sd]?", r"vaccine\w*", r"hospital\w*",
        r"treatment\w*", r"therap\w*", "cancer", r"patient\w*", r"doctor\w*", r"nurse\w*",
        r"hälsa\w*", r"sjukvård\w*", r"sjukhus\w*", r"vaccin\w*", r"behandling\w*",
        r"läkemed\w*", "läkare"])),
    ("Space & discovery", rx([
        "space", "nasa", r"astronaut\w*", r"telescope\w*", r"planet\w*", "moon", r"galax\w*",
        r"discover\w*", r"archaeolog\w*", r"fossil\w*",
        r"rymd\w*", "månen", r"arkeolog\w*", r"upptäck\w*"])),
    ("Science & technology", rx([
        r"scien\w*", r"research\w*", r"breakthrough\w*", "study", r"technolog\w*",
        r"robot\w*", r"invent\w*", r"innovat\w*", r"engineer\w*",
        r"forsk\w*", r"genombrott\w*", r"teknik\w*", r"studie\w*", r"uppfinn\w*"])),
    ("Animals & wildlife", rx([
        r"animal\w*", "wildlife", "species", r"bird\w*", "dogs?", "cats?", r"pupp\w*",
        r"kitten\w*", r"whale\w*", "zoo", r"elephant\w*",
        r"djur\w*", r"fåg\w*", r"hund\w*", r"katt\w*", r"valp\w*", r"val(?:en|ar)"])),
    ("Clean energy & climate solutions", rx([
        r"renewable\w*", "solar", r"wind (?:power|farm\w*|energy)", "clean energy",
        r"electric (?:car\w*|bus\w*|vehicle\w*)", r"recycl\w*", "emissions", "climate",
        r"förnybar\w*", "solenergi", r"solcell\w*", r"vindkraft\w*", r"klimat\w*",
        r"återvinn\w*", r"elbil\w*"])),
    ("Nature & environment", rx([
        "nature", r"forest\w*", r"ocean\w*", r"river\w*", r"reef\w*", "conservation",
        r"rewild\w*", r"national park\w*", r"garden\w*", r"tree\w*",
        r"natur\w*", r"skog\w*", "hav", "havet", r"träd\w*", r"nationalpark\w*"])),
    ("Community & kindness", rx([
        r"communit\w*", r"neighbo\w*", r"volunteer\w*", r"donat\w*", r"kind\w*", r"rescu\w*",
        r"reunit\w*", r"charit\w*", r"fundrais\w*",
        r"frivillig\w*", r"volontär\w*", r"grann\w*", r"insamling\w*", r"donera\w*",
        r"rädda\w*", r"förening\w*"])),
    ("Education & young people", rx([
        r"school\w*", r"student\w*", r"educat\w*", r"graduat\w*", r"teacher\w*",
        r"universit\w*", "literacy", "children",
        r"skola\w*", r"elev\w*", r"utbildning\w*", "lärare", "barn"])),
    ("Arts & culture", rx([
        "art", r"artist\w*", r"music\w*", r"museum\w*", r"film\w*", r"book\w*", r"theat\w*",
        r"festival\w*", r"cultur\w*", r"concert\w*",
        r"konst\w*", r"musik\w*", "museet", "bok", "boken", "böcker", r"teater\w*",
        r"kultur\w*", r"konsert\w*"])),
    ("Sport & achievement", rx([
        r"sport\w*", r"champion\w*", r"olympi\w*", r"medal\w*", r"athlete\w*",
        r"marathon\w*", r"football\w*", "world cup",
        r"guld\w*", r"mästar\w*", r"medalj\w*", r"fotboll\w*", r"rekord\w*"])),
    ("Food & travel", rx([
        r"food\w*", r"recipe\w*", r"restaurant\w*", r"travel\w*", r"touris\w*",
        r"holiday\w*",
        "mat", "maten", r"recept\w*", r"restaurang\w*", "resa", "resor", r"semester\w*",
        r"turis\w*"])),
    ("Progress & economy", rx([
        "jobs?", r"start-?ups?", r"invest\w*", "growth", r"econom\w*",
        r"jobb\w*", "tillväxt", r"investering\w*", r"ekonomi\w*"])),
]
# Markers that make a paywall certain. Anything less certain is published.
HARD_WALL = re.compile(
    r'"isAccessibleForFree"\s*:\s*"?false|subscribe to (?:continue|read|unlock)|subscribers? only'
    r"|(?:sign|log) in to (?:continue|read)|create (?:a |an )?(?:free )?account to (?:continue|read)"
    r"|you(?:'ve| have) reached your (?:free )?(?:article )?limit"
    r"|logga in för att läsa|endast för prenumeranter|för att läsa hela artikeln"
    r"|artikeln är låst|bli prenumerant för att|läs vidare med",
    re.I,
)
PREMIUM_URL = re.compile(r"/(?:premium|plus)/", re.I)
IMG_TAG = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", re.I)
IMAGE_SKIP = re.compile(r"feedburner|pixel|/stats|gravatar|emoji|1x1|spacer", re.I)
OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I,
)


def clean(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
    return re.sub(r"\s*The post .{0,300} appeared first on .{0,100}$", "", text).strip()


def excerpt(text, limit):
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def topic_for(text):
    for name, pattern in TOPICS:
        if pattern.search(text):
            return name
    return "Feel-good"


def classify(title, summary, dedicated):
    """Return (status, score, reason). Status: published, review or excluded."""
    severe = found(SEVERE, title)
    negative = found(NEGATIVE, title)
    positive = found(POSITIVE, title)
    peace = bool(PEACE.search(title))
    if severe:
        words = ", ".join(severe)
        if peace:
            return "published", 0.7, "Reports a step towards peace"
        if positive:
            return "review", 0.5, f"Mixed headline ({words}), needs a human look"
        return "excluded", 0.0, f"Negative headline ({words})"
    if negative:
        if peace:
            return "published", 0.7, "Reports a step towards peace"
        if positive or dedicated:
            return "published", 0.6, "A hopeful angle on a difficult subject"
        return "excluded", 0.0, "Negative headline (" + ", ".join(negative) + ")"
    if OPINION.search(title):
        return "review", 0.5, "Possible opinion or sponsored piece"
    pos_summary = found(POSITIVE, summary)
    neg_summary = found(SEVERE, summary) + found(NEGATIVE, summary)
    if not dedicated and not positive and len(neg_summary) >= 2 and len(neg_summary) > len(pos_summary):
        return "excluded", 0.1, "Summary is mainly negative (" + ", ".join(neg_summary[:4]) + ")"
    signals = sorted(set(positive) | set(pos_summary))
    if dedicated:
        return "published", 0.9, "From a dedicated good-news publisher"
    if signals:
        return "published", 0.8, "Positive signals: " + ", ".join(signals[:5])
    if PUBLISH_NEUTRAL:
        return "published", 0.5, "No negative content found"
    return "review", 0.4, "No clear positive signals"


def safe_image(url):
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    parts = urlparse(url)
    if parts.scheme != "https" or not parts.netloc or IMAGE_SKIP.search(url):
        return ""
    return url


def first_image(candidates):
    for url in candidates:
        url = safe_image(url)
        if url:
            return url
    return ""


def feed_media_image(entry):
    candidates = []
    for media in entry.get("media_content") or []:
        url = media.get("url", "")
        if (media.get("medium") == "image" or str(media.get("type", "")).startswith("image")
                or IMAGE_EXT.search(url)):
            candidates.append(url)
    for media in entry.get("media_thumbnail") or []:
        candidates.append(media.get("url", ""))
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image"):
            candidates.append(enc.get("href") or enc.get("url", ""))
    return first_image(candidates)


def feed_inline_image(entry):
    blocks = [entry.get("summary", "")] + [c.get("value", "") for c in entry.get("content") or []]
    candidates = []
    for block in blocks:
        candidates += IMG_TAG.findall(block or "")
    return first_image(candidates)


_robots = {}


def robots_ok(url):
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        parser = RobotFileParser()
        try:
            resp = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=10)
            parser.parse(resp.text.splitlines() if resp.ok else [])
        except requests.RequestException:
            parser.parse([])
        _robots[base] = parser
    return _robots[base].can_fetch(UA, url)


def check_access(url):
    """Return (open | restricted, note, image). Never bypasses access controls."""
    if PREMIUM_URL.search(urlparse(url).path):
        return "restricted", "link points to a premium section", ""
    if not robots_ok(url):
        return "open", "not checked (robots.txt)", ""
    time.sleep(0.5)
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    except requests.RequestException as err:
        return "open", f"not checked ({type(err).__name__})", ""
    if resp.status_code in (401, 402):
        return "restricted", f"HTTP {resp.status_code}", ""
    if resp.status_code != 200:
        return "open", f"not checked (HTTP {resp.status_code})", ""
    text = resp.text[:600000]
    image = ""
    match = OG_IMAGE.search(text)
    if match:
        image = safe_image(urljoin(resp.url, html.unescape(match.group(1) or match.group(2))))
    if HARD_WALL.search(text):
        return "restricted", "paywall or sign-in marker found", ""
    return "open", "no paywall found", image


def is_duplicate(title, titles):
    for other in titles:
        matcher = difflib.SequenceMatcher(None, title, other)
        if (matcher.real_quick_ratio() > 0.85 and matcher.quick_ratio() > 0.85
                and matcher.ratio() > 0.85):
            return True
    return False


def collect(stories, state):
    titles = [s["title"].lower() for s in stories.values()]
    limit = int(CONFIG.get("excerpt_chars", 200))
    for src in SOURCES:
        saved = state.get(src["feed"], {})
        try:
            feed = feedparser.parse(src["feed"], agent=UA, etag=saved.get("etag"),
                                    modified=saved.get("modified"))
        except Exception as err:  # one broken feed must not stop the others
            print(f"! {src['name']}: {err}")
            continue
        if feed.get("status") == 304:
            continue
        if not feed.entries:
            print(f"! {src['name']}: no entries found, check the feed address")
            continue
        state[src["feed"]] = {"etag": feed.get("etag"), "modified": feed.get("modified")}
        for entry in feed.entries[:PER_FEED]:
            link = entry.get("link", "")
            title = clean(entry.get("title", ""))
            if not title or urlparse(link).scheme not in ("http", "https"):
                continue
            sid = hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
            if sid in stories:
                continue
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            published = min(calendar.timegm(parsed), NOW) if parsed else NOW
            if published < NOW - KEEP_DAYS * 86400:
                continue
            summary = clean(entry.get("summary", ""))
            lower = title.lower()
            if is_duplicate(lower, titles):
                status, score, reason = "excluded", 0.0, "Duplicate of a story already listed"
            else:
                status, score, reason = classify(title, summary, src.get("positive_outlet", False))
            image = ""
            if status == "published":
                access, note, page_image = check_access(link)
                if access == "restricted":
                    status, reason = "excluded", f"Not free to read: {note}"
                else:
                    image = feed_media_image(entry) or page_image or feed_inline_image(entry)
            titles.append(lower)
            stories[sid] = {
                "id": sid, "title": title, "link": link, "source": src["name"],
                "feed": src["feed"], "region": src.get("region", "Global"),
                "topic": topic_for(title + " " + summary), "excerpt": excerpt(summary, limit),
                "image": image, "published": published, "status": status, "score": score,
                "reason": reason, "access_checked": NOW,
            }
            print(f"{status:9} {score:.2f}  {title[:70]}")


def apply_overrides(stories):
    approve, block = set(OVERRIDES.get("approve", [])), set(OVERRIDES.get("block", []))
    for story in stories.values():
        if story["link"] in block:
            story["status"], story["reason"] = "excluded", "Removed by editor"
        elif story["link"] in approve and story["status"] != "published":
            story["status"], story["reason"] = "published", "Approved by editor"


def recheck_access(stories):
    due = [s for s in stories.values()
           if s["status"] == "published" and s["access_checked"] < NOW - RECHECK_DAYS * 86400]
    for story in sorted(due, key=lambda s: s["access_checked"])[:RECHECK_BATCH]:
        access, note, _ = check_access(story["link"])
        story["access_checked"] = NOW
        if access == "restricted":
            story["status"], story["reason"] = "excluded", f"Became restricted: {note}"


CSS = """
:root{--paper:#f7f3ea;--ink:#1c1b19;--soft:#5f5a52;--hair:#cfc7b8;--accent:#8a2b1e}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--paper);color:var(--ink);
font:17px/1.55 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif}
a{color:inherit}
.wrap{max-width:1180px;margin:0 auto;padding:0 1.2rem}
.masthead{text-align:center;padding:1.6rem 0 0}
.masthead h1{margin:0;font:900 clamp(2.6rem,8vw,5.2rem)/1 "Bodoni MT",Didot,"Playfair Display","Times New Roman",serif}
.masthead h1 a{text-decoration:none}
.tagline{margin:.4rem 0 0;font-style:italic;color:var(--soft)}
.dateline{display:flex;justify-content:space-between;flex-wrap:wrap;gap:.3rem 1rem;
border-top:3px double var(--ink);border-bottom:1px solid var(--ink);padding:.45rem 0;margin-top:.9rem;
font:12px/1.4 system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase}
.dateline a{text-decoration:none;margin:0 .4rem}.dateline a:hover{text-decoration:underline}
.filters{display:flex;flex-wrap:wrap;gap:.5rem;justify-content:center;padding:.8rem 0;
border-bottom:1px solid var(--hair)}
select{font:13px system-ui,sans-serif;padding:.35rem .5rem;border:1px solid var(--hair);
background:transparent;color:var(--ink);border-radius:0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:0 2.2rem;padding-top:1.5rem}
.card{padding:0 0 1.3rem;margin:0 0 1.3rem;border-bottom:1px solid var(--hair);min-width:0}
.card.lead{grid-column:1/-1;display:grid;grid-template-columns:3fr 2fr;gap:2.2rem;
align-items:start;border-bottom:3px double var(--ink)}
.lead .text:only-child{grid-column:1/-1}
.photo{margin:0 0 .7rem}
.photo img{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;background:#e6dfd0}
.credit{display:block;font:11px/1.6 system-ui,sans-serif;color:var(--soft);text-align:right}
.kicker{font:700 11px/1.4 system-ui,sans-serif;letter-spacing:.12em;text-transform:uppercase;
color:var(--accent);margin:0 0 .3rem}
.card h2{font:700 1.3rem/1.2 Georgia,"Times New Roman",serif;margin:0 0 .45rem}
.card h2 a{text-decoration:none}.card h2 a:hover{text-decoration:underline}
.lead h2{font-size:clamp(1.7rem,3.6vw,2.6rem);line-height:1.1}
.excerpt{margin:0 0 .5rem;color:#33312d}.lead .excerpt{font-size:1.1rem}
.byline,.why{font:12px/1.5 system-ui,sans-serif;color:var(--soft);margin:.2rem 0}
.why{font-style:italic}
.prose{max-width:680px;margin:2rem auto}.prose a{color:var(--accent)}
footer{border-top:3px double var(--ink);margin-top:1rem;padding:1rem 0 2rem;text-align:center;
font:13px/1.5 system-ui,sans-serif;color:var(--soft)}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.grid,.card.lead{grid-template-columns:1fr}.card.lead{gap:0}}
"""

SCRIPT = """<script>
const selects = ['region','topic','source'].map(id => document.getElementById(id));
const days = document.getElementById('days');
function applyFilters() {
  const cutoff = days.value ? Date.now() / 1000 - days.value * 86400 : 0;
  let shown = 0;
  document.querySelectorAll('.card').forEach(card => {
    const ok = selects.every(s => !s.value || card.dataset[s.id] === s.value)
      && Number(card.dataset.ts) >= cutoff;
    card.hidden = !ok;
    if (ok) shown++;
  });
  document.getElementById('empty').hidden = shown > 0;
}
[...selects, days].forEach(s => s.addEventListener('change', applyFilters));
</script>"""


def page(title, body, script=""):
    now = datetime.now(timezone.utc)
    today = now.strftime("%A, %d %B %Y")
    updated = now.strftime("%H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="alternate" type="application/rss+xml" title="Smiling Times" href="feed.xml">
<style>{CSS}</style></head><body><div class="wrap">
<header class="masthead"><h1><a href="index.html">Smiling Times</a></h1>
<p class="tagline">Good news from around the world</p>
<div class="dateline"><span>{today}</span>
<nav><a href="index.html">Front page</a><a href="about.html">About</a><a href="feed.xml">RSS</a></nav>
<span>Updated {updated}</span></div></header>
<main>{body}</main>
<footer>Smiling Times links to the original publishers and does not republish their articles.
Photos belong to the publishers named beside them. <a href="about.html">How stories are chosen</a></footer>
</div>{script}</body></html>"""


def card(story, lead=False):
    e = html.escape
    date = datetime.fromtimestamp(story["published"], timezone.utc).strftime("%d %b %Y")
    link = e(story["link"])
    photo = ""
    if SHOW_IMAGES and story.get("image"):
        loading = "eager" if lead else "lazy"
        photo = (
            f'<div class="photo"><a href="{link}" rel="noopener noreferrer">'
            f'<img src="{e(story["image"])}" alt="" loading="{loading}" referrerpolicy="no-referrer" '
            f'onerror="this.closest(\'.photo\').remove()"></a>'
            f'<span class="credit">Photo: {e(story["source"])}</span></div>'
        )
    text = f'<p class="excerpt">{e(story["excerpt"])}</p>' if story["excerpt"] else ""
    classes = "card lead" if lead else "card"
    return f"""<article class="{classes}" id="s{story["id"]}" data-region="{e(story["region"])}"
 data-topic="{e(story["topic"])}" data-source="{e(story["source"])}" data-ts="{int(story["published"])}">
{photo}<div class="text">
<p class="kicker">{e(story["topic"])}</p>
<h2><a href="{link}" rel="noopener noreferrer">{e(story["title"])}</a></h2>
{text}
<p class="byline">Read the full story at <a href="{link}" rel="noopener noreferrer">{e(story["source"])}</a>
 · {e(story["region"])} · {date}</p>
<p class="why">Why this story is here: {e(story["reason"])}</p>
</div></article>"""


def select(sid, label, values):
    options = "".join(f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in values)
    return f'<select id="{sid}" aria-label="{label}"><option value="">{label}</option>{options}</select>'


def render_index(published):
    filters = (
        select("region", "All regions", sorted({s["region"] for s in published}))
        + select("topic", "All topics", sorted({s["topic"] for s in published}))
        + select("source", "All sources", sorted({s["source"] for s in published}))
        + '<select id="days" aria-label="Any date"><option value="">Any date</option>'
          '<option value="1">Last 24 hours</option><option value="7">Last 7 days</option></select>'
    )
    cards = "\n".join(card(s, i == 0) for i, s in enumerate(published))
    hidden = "hidden" if published else ""
    body = (f'<div class="filters">{filters}</div><div class="grid">{cards}</div>'
            f'<p id="empty" {hidden}>No stories to show yet.</p>')
    return page("Smiling Times: good news from around the world", body, SCRIPT)


def render_about():
    e = html.escape
    email = e(CONFIG.get("contact_email", ""))
    sources = "".join(f"<li>{e(s['name'])} ({e(s.get('region', 'Global'))})</li>" for s in SOURCES)
    body = f"""<div class="prose"><h2>About Smiling Times</h2>
<p>Smiling Times collects good and hopeful news from public RSS feeds and links to the original articles.</p>
<h3>What is included</h3>
<p>Peace agreements, kindness, health and science progress, discoveries, nature and wildlife,
clean energy, culture, sport, and other stories that are not bad news.</p>
<h3>What is left out</h3>
<p>Stories whose headline is about violence, death, disasters, war, crime or economic hardship,
unless they report peace or a clearly hopeful development. Articles that are certain to need
payment or an account are also left out.</p>
<h3>How it works</h3>
<p>A program checks the feeds every hour and screens each headline and summary with English and
Swedish word lists. Articles with a clear paywall are dropped; when access is uncertain the story
is still shown. A few mixed cases wait for a human editor. The region shown is the publisher's
region. Summaries are short excerpts from the publisher's own feed and are not AI-generated.</p>
<h3>Photos</h3>
<p>Photos are the publishers' own preview images, loaded from their websites and credited to them.
Publishers who want a photo or story removed can write to the address below.</p>
<h3>Limitations</h3>
<p>Automatic filtering makes mistakes, and this site is neither complete nor perfectly objective.
Facts are not independently verified; please rely on the original publisher.</p>
<h3>Sources</h3><ul>{sources}</ul>
<h3>Funding</h3><p>{e(CONFIG.get("funding_note", ""))}</p>
<h3>Corrections and contact</h3>
<p>Spotted a problem or want something removed? Write to <a href="mailto:{email}">{email}</a>.</p></div>"""
    return page("About Smiling Times", body)


def render_rss(published):
    e = html.escape
    items = []
    for story in published[:50]:
        page_url = f"{SITE_URL}/#s{story['id']}"
        desc = f"{story['excerpt']} Source: {story['source']}. On Smiling Times: {page_url}".strip()
        date = format_datetime(datetime.fromtimestamp(story["published"], timezone.utc))
        media = ""
        if SHOW_IMAGES and story.get("image"):
            media = f'<media:content url="{e(story["image"])}" medium="image"/>'
        items.append(
            f"<item><title>{e(story['title'])}</title><link>{e(story['link'])}</link>"
            f"<guid isPermaLink=\"false\">smilingtimes-{story['id']}</guid><pubDate>{date}</pubDate>"
            f"<source url=\"{e(story['feed'])}\">{e(story['source'])}</source>"
            f"<description>{e(desc)}</description>{media}</item>"
        )
    built = format_datetime(datetime.now(timezone.utc))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:media="http://search.yahoo.com/mrss/"><channel>'
        f"<title>Smiling Times</title><link>{e(SITE_URL)}/</link>"
        "<description>Good news from around the world</description><language>en</language>"
        f"<lastBuildDate>{built}</lastBuildDate>"
        f'<atom:link href="{e(SITE_URL)}/feed.xml" rel="self" type="application/rss+xml"/>'
        + "".join(items) + "</channel></rss>\n"
    )


def render_review(stories):
    pending = sorted((s for s in stories.values() if s["status"] == "review"),
                     key=lambda s: -s["published"])
    lines = [
        "# Review queue", "",
        "Open each story and check it is positive and free to read in full.",
        "To publish one, add its link to `approve` in `overrides.json`.",
        "To reject one or remove a published story, add its link to `block`.", "",
    ]
    for story in pending:
        lines += [f"- **{story['title']}** ({story['source']}, score {story['score']})",
                  f"  - {story['reason']}", f"  - {story['link']}"]
    if not pending:
        lines.append("Nothing is waiting for review.")
    return "\n".join(lines) + "\n"


def main():
    DATA.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    stories = load(DATA / "stories.json", {})
    state = load(DATA / "feed_state.json", {})
    if state.get("_version") != DATA_VERSION:  # rules changed: start again from scratch
        stories, state = {}, {"_version": DATA_VERSION}
    collect(stories, state)
    apply_overrides(stories)
    recheck_access(stories)
    stories = {k: v for k, v in stories.items() if v["published"] >= NOW - KEEP_DAYS * 86400}
    published = sorted((s for s in stories.values() if s["status"] == "published"),
                       key=lambda s: -s["published"])[:SHOW_MAX]
    (DATA / "stories.json").write_text(json.dumps(stories, indent=1, ensure_ascii=False), encoding="utf-8")
    (DATA / "feed_state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    (DATA / "review.md").write_text(render_review(stories), encoding="utf-8")
    (OUT / "index.html").write_text(render_index(published), encoding="utf-8")
    (OUT / "about.html").write_text(render_about(), encoding="utf-8")
    (OUT / "feed.xml").write_text(render_rss(published), encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Done: {len(published)} published, "
          f"{sum(s['status'] == 'review' for s in stories.values())} awaiting review.")


if __name__ == "__main__":
    main()