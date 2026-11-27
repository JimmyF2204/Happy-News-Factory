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
from urllib.parse import urlparse
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
UA = f"SmilingTimesBot/1.0 (+{SITE_URL})"
PUBLISH_SCORE, REVIEW_SCORE = 0.7, 0.4
KEEP_DAYS, SHOW_MAX, RECHECK_DAYS, RECHECK_BATCH = 30, 150, 7, 15
NOW = time.time()
socket.setdefaulttimeout(20)


def rx(words):
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


NEGATIVE = rx([
    r"kill\w*", r"murder\w*", "dead", r"death\w*", "dies", "died", r"shoot\w*", "shot",
    r"stab\w*", r"attack\w*", "wars?", r"bomb\w*", r"terror\w*", r"rap(?:e|ed|ist)",
    r"abus\w*", r"assault\w*", r"crash\w*", r"disaster\w*", r"earthquake\w*", r"flood\w*",
    r"wildfire\w*", r"hurricane\w*", "famine", "crisis", "crises", r"recession\w*",
    r"layoff\w*", "inflation", r"bankrupt\w*", r"scandal\w*", r"outrage\w*", r"arrest\w*",
    r"victim\w*", r"traged\w*", "tragic", r"suicid\w*", r"genocid\w*", r"hostage\w*",
    r"massacre\w*", r"lawsuit\w*", r"fraud\w*", r"collaps\w*", r"extinct\w*",
])
PEACE = rx([
    "ceasefire", "cease-fire", "truce", r"peace (?:deal|agreement|accord|treaty|talks)",
    r"war (?:ends|is over)", r"ends? (?:the )?war", r"reconcil\w*",
])
POSITIVE = rx([
    r"sav(?:e|es|ed|ing)", r"rescu\w*", r"reunit\w*", r"recover\w*", r"restor\w*",
    r"breakthrough\w*", r"cure[sd]?", r"heal\w*", r"donat\w*", r"volunteer\w*", r"kind\w*",
    r"generous\w*", r"hero\w*", r"celebrat\w*", "wins?", "won", r"success\w*", r"thriv\w*",
    r"rebound\w*", r"first[- ]ever", r"protect\w*", r"hope\w*", r"joy\w*", "happy",
    r"happi\w*", r"smil\w*", r"peace\w*", r"improv\w*", r"achiev\w*", r"milestone\w*",
    "comeback", r"adopt\w*", r"gift\w*", r"help(?:s|ed|ing)?", r"inspir\w*",
    r"renewable\w*", "clean energy", r"rewild\w*", "conservation", r"vaccine\w*",
    r"graduat\w*", r"uplift\w*",
])
OPINION = rx(["opinion", "op-ed", "editorial", "sponsored", "advertorial"])
TOPICS = [
    ("Peace", rx([r"peace\w*", "ceasefire", "truce", r"reconcil\w*"])),
    ("Health & science", rx([r"health\w*", r"medic\w*", r"cure[sd]?", r"vaccine\w*", r"scien\w*",
                             r"research\w*", r"hospital\w*", r"treatment\w*", r"breakthrough\w*"])),
    ("Nature", rx([r"wildlife", r"species", r"forest\w*", r"ocean\w*", r"river\w*", r"conservation",
                   r"rewild\w*", r"renewable\w*", r"solar", r"animal\w*", r"bird\w*", r"reef\w*"])),
    ("Education & progress", rx([r"school\w*", r"student\w*", r"educat\w*", r"graduat\w*",
                                 r"accessib\w*", r"literacy", r"universit\w*"])),
    ("Community & kindness", rx([r"communit\w*", r"neighbo\w*", r"volunteer\w*", r"donat\w*",
                                 r"kind\w*", r"rescu\w*", r"reunit\w*", r"charit\w*"])),
]
HARD_WALL = re.compile(
    r'"isAccessibleForFree"\s*:\s*"?false|subscribe to (?:continue|read|unlock)|subscribers? only'
    r"|(?:sign|log) in to (?:continue|read)|create (?:a |an )?(?:free )?account to (?:continue|read)"
    r"|you(?:'ve| have) reached your (?:free )?(?:article )?limit|free articles? (?:left|remaining)",
    re.I,
)
SOFT_WALL = re.compile(r"paywall|metered|piano\.io|tinypass|already a subscriber", re.I)


def clean(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


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
    neg_title = sorted({m.lower() for m in NEGATIVE.findall(title)})
    if neg_title:
        words = ", ".join(neg_title)
        if PEACE.search(title) or dedicated:
            return "review", 0.5, f"Sensitive words in headline ({words})"
        return "excluded", 0.0, f"Negative words in headline ({words})"
    if OPINION.search(title):
        return "review", 0.5, "Possible opinion or sponsored piece"
    pos_title = {m.lower() for m in POSITIVE.findall(title)}
    pos_summary = {m.lower() for m in POSITIVE.findall(summary)} - pos_title
    neg_summary = {m.lower() for m in NEGATIVE.findall(summary)}
    score = (0.6 if dedicated else 0.25) + 0.15 * len(pos_title) + 0.05 * len(pos_summary)
    score = round(max(0.0, min(1.0, score - 0.15 * len(neg_summary))), 2)
    parts = []
    if pos_title or pos_summary:
        parts.append("Positive signals: " + ", ".join(sorted(pos_title | pos_summary)[:5]))
    if dedicated:
        parts.append("from a dedicated good-news publisher")
    if neg_summary:
        parts.append("negative words in summary: " + ", ".join(sorted(neg_summary)))
    reason = " · ".join(parts) or "No clear positive signals"
    if score >= PUBLISH_SCORE:
        return "published", score, reason
    if score >= REVIEW_SCORE:
        return "review", score, reason
    return "excluded", score, reason


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
    """Return (free | restricted | unknown, note). Never bypasses access controls."""
    if not robots_ok(url):
        return "unknown", "robots.txt does not allow an automatic check"
    time.sleep(1)
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    except requests.RequestException as err:
        return "unknown", f"page could not be loaded ({type(err).__name__})"
    if resp.status_code in (401, 402):
        return "restricted", f"HTTP {resp.status_code}"
    if resp.status_code != 200:
        return "unknown", f"HTTP {resp.status_code}"
    if HARD_WALL.search(resp.text):
        return "restricted", "paywall or sign-in marker found"
    if SOFT_WALL.search(resp.text):
        return "unknown", "possible paywall marker found"
    if len(clean(resp.text).split()) < 250:
        return "unknown", "could not confirm the full text is visible"
    return "free", "full text loaded without sign-in"

# --- Swedish support ---
def add_words(pattern, words):
    return re.compile(pattern.pattern + "|" + rx(words).pattern, re.I)


NEGATIVE = add_words(NEGATIVE, [
    r"mord\w*", r"mörda\w*", r"självmord\w*", r"folkmord\w*", r"dråp\w*", r"död\w*",
    "avled", "avlidit", r"skjut\w*", "sköt", r"knivhugg\w*", r"knivskur\w*", r"krig\w*",
    r"våld\w*", r"misshand\w*", r"övergrepp\w*", r"krasch\w*", r"olyck\w*", r"katastrof\w*",
    r"jordbäv\w*", r"översvämning\w*", r"skogsbr\w*", "branden", "bränder", r"orkan\w*",
    r"svält\w*", "kris", "krisen", "kriser", "kriserna", r"lågkonjunktur\w*", "varsel",
    "varslar", "varslas", "varslade", r"uppsägning\w*", r"konkurs\w*", r"skandal\w*",
    "grips", "greps", "gripits", r"anhåll\w*", r"häkta\w*", r"åtal\w*", "döms", r"dömd\w*",
    r"misstänk\w*", "offren", "offret", r"brottsoffer\w*", r"tragedi\w*", r"tragisk\w*",
    "gisslan", r"massak\w*", r"bedrägeri\w*", r"kollaps\w*", r"utrot\w*", r"utdö\w*",
    r"rån\w*", "brott", "brottet", r"brottslig\w*", r"sprängning\w*", r"sprängd\w*",
])
PEACE = add_words(PEACE, [
    "vapenvila", r"vapenstillestånd\w*", r"fredsavtal\w*", r"fredssamtal\w*",
    r"kriget (?:är )?slut", r"försoning\w*",
])
POSITIVE = add_words(POSITIVE, [
    r"rädda\w*", r"räddning\w*", r"återförena\w*", r"återhämt\w*", r"återställ\w*",
    r"återfunn\w*", r"överlev\w*", r"genombrott\w*", "botar", "botad", "botade", "botas",
    "botemedel", r"friskförklar\w*", r"hjälp\w*", r"donera\w*", r"skänk\w*", r"volontär\w*",
    r"hjält\w*", r"fira\w*", "vinner", "vann", r"seger\w*", r"segra\w*", r"lyck\w*",
    r"framgång\w*", r"succé\w*", r"rekord\w*", "hopp", "hoppet", "hoppas", r"hoppfull\w*",
    r"gläd\w*", "glad", "glada", r"kärlek\w*", "fred", "freden", r"förbättra\w*",
    r"skydda\w*", r"milstolpe\w*", r"adoptera\w*", "gåva", "gåvor", r"förnybar\w*",
    "solenergi", r"vaccin\w*", "examen", r"hyll\w*", "prisas", r"prisad\w*", r"belön\w*",
    r"historisk\w*", "äntligen", "goda nyheter", "omtanke", r"generös\w*", r"vänlig\w*",
])
OPINION = add_words(OPINION, ["debatt", "krönika", "insändare", "annons"])

SV_TOPICS = {
    "Peace": ["fred", "freden", "vapenvila", r"fredsavtal\w*", r"försoning\w*"],
    "Health & science": [r"hälsa\w*", r"sjukvård\w*", r"sjukhus\w*", r"forsk\w*", r"vaccin\w*",
                         r"behandling\w*", r"läkemed\w*", r"genombrott\w*"],
    "Nature": [r"natur\w*", r"djur\w*", r"skog\w*", "hav", "havet", r"fåg\w*", r"klimat\w*",
               r"förnybar\w*", "solenergi", r"vindkraft\w*"],
    "Education & progress": [r"skola\w*", r"elev\w*", r"utbildning\w*", r"universitet\w*"],
    "Community & kindness": [r"frivillig\w*", r"volontär\w*", r"grann\w*", r"insamling\w*",
                             r"donera\w*", r"rädda\w*", r"förening\w*"],
}
TOPICS = [(name, add_words(pattern, SV_TOPICS[name])) for name, pattern in TOPICS]

HARD_WALL = re.compile(
    HARD_WALL.pattern + r"|logga in för att läsa|endast för prenumeranter"
    r"|för att läsa hela artikeln|artikeln är låst|bli prenumerant för att|läs vidare med",
    re.I,
)
SOFT_WALL = re.compile(SOFT_WALL.pattern + r"|plusartikel|plus-artikel|låst artikel", re.I)
PREMIUM_URL = re.compile(r"/(?:premium|plus)/", re.I)
_base_check_access = check_access


def check_access(url):
    if PREMIUM_URL.search(urlparse(url).path):
        return "restricted", "link points to a premium section"
    return _base_check_access(url)
# --- end Swedish support ---

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
        for entry in feed.entries[:30]:
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
            if any(difflib.SequenceMatcher(None, lower, t).ratio() > 0.85 for t in titles):
                status, score, reason = "excluded", 0.0, "Duplicate of a story already listed"
            else:
                status, score, reason = classify(title, summary, src.get("positive_outlet", False))
            if status == "published":
                access, note = check_access(link)
                if access == "restricted":
                    status, reason = "excluded", f"Not free to read: {note}"
                elif access == "unknown":
                    status, reason = "review", f"{reason} · Free access not confirmed: {note}"
            titles.append(lower)
            stories[sid] = {
                "id": sid, "title": title, "link": link, "source": src["name"],
                "feed": src["feed"], "region": src.get("region", "Global"),
                "topic": topic_for(title + " " + summary), "excerpt": excerpt(summary, limit),
                "published": published, "status": status, "score": score,
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
        access, note = check_access(story["link"])
        story["access_checked"] = NOW
        if access == "restricted":
            story["status"], story["reason"] = "excluded", f"Became restricted: {note}"


CSS = """
:root{--bg:#fffaf2;--card:#fff;--ink:#2b2a28;--soft:#6b665e;--accent:#d9822b;--line:#eadfce}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;font:17px/1.6 Georgia,serif;background:var(--bg);color:var(--ink)}
header,main,footer{max-width:760px;margin:0 auto;padding:1rem}
header h1{margin:.5rem 0 0;font-size:2.2rem}header h1 a{color:var(--ink);text-decoration:none}
header p{margin:0;color:var(--soft)}nav{margin-top:.5rem}a{color:#9a5510}
.filters{display:flex;flex-wrap:wrap;gap:.5rem;margin:1rem 0}
select{font:inherit;padding:.4rem;border:1px solid var(--line);border-radius:6px;background:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.2rem;margin:0 0 1rem}
.card h2{font-size:1.25rem;line-height:1.35;margin:.3rem 0}.card h2 a{color:var(--ink)}
.meta,.src,.why,footer,.updated{font:14px/1.5 system-ui,sans-serif;color:var(--soft);margin:.3rem 0}
.tag{background:var(--accent);color:#fff;border-radius:99px;padding:.1rem .6rem;margin-right:.4rem}
.excerpt{margin:.4rem 0}
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
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="alternate" type="application/rss+xml" title="Smiling Times" href="feed.xml">
<style>{CSS}</style></head><body>
<header><h1><a href="index.html">Smiling Times</a></h1>
<p>Good news from around the world</p>
<nav><a href="index.html">Stories</a> · <a href="about.html">About</a> · <a href="feed.xml">RSS feed</a></nav>
</header><main>{body}</main>
<footer>Smiling Times links to the original publishers and does not republish their articles.
<a href="about.html">How stories are chosen</a></footer>{script}</body></html>"""


def card(story):
    e = html.escape
    date = datetime.fromtimestamp(story["published"], timezone.utc).strftime("%d %b %Y")
    text = f'<p class="excerpt">{e(story["excerpt"])}</p>' if story["excerpt"] else ""
    return f"""<article class="card" id="s{story["id"]}" data-region="{e(story["region"])}"
 data-topic="{e(story["topic"])}" data-source="{e(story["source"])}" data-ts="{int(story["published"])}">
<p class="meta"><span class="tag">{e(story["topic"])}</span>{e(story["region"])} · {date}</p>
<h2><a href="{e(story["link"])}" rel="noopener noreferrer">{e(story["title"])}</a></h2>
{text}
<p class="src">Read the full story at <a href="{e(story["link"])}" rel="noopener noreferrer">{e(story["source"])}</a></p>
<p class="why">Why this story is here: {e(story["reason"])}</p>
</article>"""


def select(sid, label, values):
    options = "".join(f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in values)
    return f'<select id="{sid}" aria-label="{label}"><option value="">{label}</option>{options}</select>'


def render_index(published):
    updated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    filters = (
        select("region", "All regions", sorted({s["region"] for s in published}))
        + select("topic", "All topics", sorted({s["topic"] for s in published}))
        + select("source", "All sources", sorted({s["source"] for s in published}))
        + '<select id="days" aria-label="Any date"><option value="">Any date</option>'
          '<option value="1">Last 24 hours</option><option value="7">Last 7 days</option></select>'
    )
    cards = "\n".join(card(s) for s in published)
    body = (f'<p class="updated">Last updated: {updated}</p><div class="filters">{filters}</div>'
            f'{cards}<p id="empty" {"hidden" if published else ""}>No stories to show yet.</p>')
    return page("Smiling Times: good news from around the world", body, SCRIPT)


def render_about():
    e = html.escape
    email = e(CONFIG.get("contact_email", ""))
    sources = "".join(f"<li>{e(s['name'])} ({e(s.get('region', 'Global'))})</li>" for s in SOURCES)
    body = f"""<h2>About Smiling Times</h2>
<p>Smiling Times collects uplifting news from public RSS feeds and links to the original articles.</p>
<h3>What is included</h3>
<p>Stories about a concrete positive development: peace agreements, acts of kindness, medical and
scientific progress, conservation successes, rescues, recoveries and hopeful human-interest stories.</p>
<h3>What is left out</h3>
<p>Stories mainly about violence, tragedy, disasters, ongoing wars, environmental damage, economic
hardship, political outrage, crime or gossip, and articles that need payment or an account to read.</p>
<h3>How it works</h3>
<p>A program checks the feeds every hour and scores each headline and summary with word lists.
High-scoring stories are published automatically once the article page loads in full without sign-in.
Uncertain stories wait for a human editor. The region shown is the publisher's region. Summaries are
short excerpts from the publisher's own feed and are not AI-generated.</p>
<h3>Limitations</h3>
<p>Automatic filtering makes mistakes, and this site is neither complete nor perfectly objective.
Facts are not independently verified; please rely on the original publisher.</p>
<h3>Sources</h3><ul>{sources}</ul>
<h3>Funding</h3><p>{e(CONFIG.get("funding_note", ""))}</p>
<h3>Corrections and contact</h3>
<p>Spotted a problem or want a story removed? Write to <a href="mailto:{email}">{email}</a>.</p>"""
    return page("About Smiling Times", body)


def render_rss(published):
    e = html.escape
    items = []
    for story in published[:50]:
        page_url = f"{SITE_URL}/#s{story['id']}"
        desc = f"{story['excerpt']} Source: {story['source']}. On Smiling Times: {page_url}".strip()
        date = format_datetime(datetime.fromtimestamp(story["published"], timezone.utc))
        items.append(
            f"<item><title>{e(story['title'])}</title><link>{e(story['link'])}</link>"
            f"<guid isPermaLink=\"false\">smilingtimes-{story['id']}</guid><pubDate>{date}</pubDate>"
            f"<source url=\"{e(story['feed'])}\">{e(story['source'])}</source>"
            f"<description>{e(desc)}</description></item>"
        )
    built = format_datetime(datetime.now(timezone.utc))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
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