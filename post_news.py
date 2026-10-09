"""Reads RSS feeds from feeds.txt and posts new football news to a Telegram channel."""
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# Optional backup AIs (any OpenAI-compatible API): LLM2_* and LLM3_*. Used in this order
# if Gemini fails. Example defaults: LLM2 = GitHub Models, LLM3 = DeepSeek.
LLM_DEFAULTS = {
    "LLM2": ("https://models.github.ai/inference", "openai/gpt-4o-mini"),
    "LLM3": ("https://api.deepseek.com", "deepseek-chat"),
}
# true = never publish an un-rewritten short post; wait and retry on the next run instead.
REQUIRE_REWRITE = os.environ.get("REQUIRE_REWRITE", "true").lower() == "true"
# If the AI fails, keep retrying inside the same run for up to RETRY_WINDOW_MIN minutes
# (every RETRY_PAUSE_SEC seconds) and publish the moment it succeeds.
RETRY_WINDOW_MIN = float(os.environ.get("RETRY_WINDOW_MIN", "4"))
RETRY_PAUSE_SEC = float(os.environ.get("RETRY_PAUSE_SEC", "60"))
# Safety limit for one run: no new item is started after this many minutes.
RUN_BUDGET_MIN = float(os.environ.get("RUN_BUDGET_MIN", "10"))
USE_SOURCE_IMAGE = os.environ.get("USE_SOURCE_IMAGE", "false").lower() == "true"
FETCH_ARTICLE = os.environ.get("FETCH_ARTICLE", "true").lower() == "true"
FOOTBALL_ONLY = os.environ.get("FOOTBALL_ONLY", "true").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "6"))
# Rhythm: one post at most every POST_INTERVAL_MIN minutes, no matter how often the bot runs.
POST_INTERVAL_MIN = float(os.environ.get("POST_INTERVAL_MIN", "20"))
# Quiet hours in Kyiv time: no posts from QUIET_FROM (inclusive) to QUIET_TO (exclusive).
# Format "21:30" (a plain "21" also works).
QUIET_FROM = os.environ.get("QUIET_FROM", "21:30")
QUIET_TO = os.environ.get("QUIET_TO", "07:30")
QUIET_DISCARD = os.environ.get("QUIET_DISCARD", "true").lower() == "true"  # forget night news
STATE_FILE = Path("posted.json")
FEEDS_FILE = Path("feeds.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "uk-UA,uk;q=0.9",
}

# Words that mark a news item as football (lowercase, matched as substrings).
FOOTBALL_WORDS = (
    "футбол", "матч", "збірн", "упл", "прем'єр-ліг", "прем’єр-ліг", "ліга чемпіонів",
    "ліга європи", "ліга конференцій", "ліга націй", "чемпіонат", "кубок", "трансфер",
    "тренер", "динамо", "шахтар", "барселон", "реал", "ліверпул", "арсенал", "челсі",
    "манчестер", "баварі", "ювентус", "мілан", "інтер", "псж", "мессі", "роналду",
    "забарн", "довбик", "мудрик", "гол ", "голи", "воротар", "півзахисник",
    "нападник", "захисник", "уєфа", "фіфа"
)

SKIP_WORDS = (
    "прогноз", "ставк", "букмекер", "бонус", "трансляц", "дивитись онлайн",
    # Блокування анонсів трансляцій та 'де дивитися'
    "де дивитися", "де дивитись", "де переглянути", "пряма трансляція", "онлайн-трансляція",
    # Блокування жіночого футболу
    "жіноч", "жінок", "женск", "жін ",
    # Блокування статей-дайджестів та підбірок новин
    "головні новини", "підсумки дня", "добірк", "головних новин", "главные новости",
    # Блокування інших видів спорту та єдиноборств
    "теніс", "світолін", "костюк", "баскетбол", "волейбол", "хокей", "біатлон",
    "бокс", "мма", "ufc", "боротьб", "дзюдо", "карате", "джиу-джитсу",
    "олімпіад", "олімпійськ", "формула-1", "f1", "легка атлетик", "гімнастик",
    "плаванн", "гандбол", "футзал", "пляжний футбол",
)

SKIP_TITLE_RE = re.compile(r"\b(відео|видео)\b", re.IGNORECASE)

def local(tag):
    return tag.split("}")[-1]


def to_text(markup):
    return BeautifulSoup(markup or "", "html.parser").get_text(" ", strip=True)


def parse_date(value):
    if not value:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def parse_feed(xml_bytes):
    """Parses RSS 2.0 or Atom. Returns list of dicts."""
    root = ET.fromstring(xml_bytes)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "summary": "", "content": "", "image": "", "ts": 0.0}
        for ch in el:
            name = local(ch.tag)
            text = (ch.text or "").strip()
            if name == "title":
                d["title"] = to_text(text)
            elif name == "link":
                href = ch.attrib.get("href")
                if href:
                    if ch.attrib.get("rel", "alternate") == "alternate" or not d["link"]:
                        d["link"] = href
                elif text:
                    d["link"] = text
            elif name in ("description", "summary"):
                d["summary"] = text
            elif name in ("encoded", "content") and text:
                d["content"] = text
            elif name in ("pubDate", "published", "updated", "date"):
                d["ts"] = d["ts"] or parse_date(text)
            elif name in ("content", "thumbnail") and ch.attrib.get("url"):
                d["image"] = d["image"] or ch.attrib["url"]
            elif name == "enclosure" and ch.attrib.get("url"):
                if ch.attrib.get("type", "image").startswith("image"):
                    d["image"] = d["image"] or ch.attrib["url"]
        # media:content / media:thumbnail
        for ch in el:
            if local(ch.tag) in ("content", "thumbnail") and ch.attrib.get("url"):
                t = ch.attrib.get("type", "image")
                if t.startswith("image") or ch.attrib.get("medium") == "image" \
                   or local(ch.tag) == "thumbnail":
                    d["image"] = d["image"] or ch.attrib["url"]
        if not d["image"]:
                        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', d["content"] + d["summary"])
                        if m:
                            d["image"] = html.unescape(m.group(1))
            
        if d["link"] and d["title"]:
            if d["ts"] > 0 and (time.time() - d["ts"]) > 10800:
                continue
            out.append(d)
        
        return out

def fetch_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_feed(r.content)

def is_football(item):
    if SKIP_TITLE_RE.search(item["title"]):
        return False
    blob = (item["title"] + " " + to_text(item["summary"])).lower() + " "
    if any(w in blob for w in SKIP_WORDS):
        return False
    return (not FOOTBALL_ONLY) or any(w in blob for w in FOOTBALL_WORDS)


def item_key(item):
    return hashlib.sha1(item["link"].encode()).hexdigest()[:16]


def fetch_article(url):
    """Best effort: full text and og:image. Returns ("", "") if the site refuses."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("  article not available:", e)
        return "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("meta", property="og:image")
    image = tag["content"].strip() if tag and tag.get("content") else ""
    box = soup.find("article") or soup
    paras = [p.get_text(" ", strip=True) for p in box.find_all("p")]
    return "\n".join(p for p in paras if len(p) > 50)[:4000], image


_state = {"cands": None, "good": "", "failed": set()}
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
TRANSIENT = (429, 500, 502, 503, 504)


class RewriteFailed(Exception):
    pass


def list_candidates():
    """Asks Google which models this key can use; returns text 'flash' models, best first."""
    if _state["cands"] is not None:
        return _state["cands"]
    names = []
    try:
        r = requests.get(f"{GEMINI_API}/models", params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
        r.raise_for_status()
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                names.append(m["name"].split("/", 1)[-1])
    except Exception as e:
        print("  could not list Gemini models:", e)
    bad = ("tts", "image", "live", "audio", "embedding", "computer", "robotics", "vision",
           "omni", "transcribe", "lyria", "antigravity", "research", "customtools")
    cands = [n for n in names if "flash" in n and not any(b in n for b in bad)]
    cands.sort(key=lambda n: ("preview" in n or "exp" in n, "lite" in n, n))
    _state["cands"] = cands
    return cands


def call_gemini(model, prompt):
    """One model, with retries when Google is overloaded (503) or rate limits (429)."""
    for attempt, pause in enumerate((0, 4, 10)):
        if pause:
            time.sleep(pause)
        r = requests.post(
            f"{GEMINI_API}/models/{model}:generateContent",
            params={"key": GEMINI_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=45,
        )
        if r.status_code in TRANSIENT and attempt < 2:
            print(f"  model {model}: HTTP {r.status_code}, retrying...")
            continue
        if r.status_code >= 400:
            print(f"  model {model}: HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def ask_gemini(prompt):
    """Calls Gemini with automatic fallback between active official flash models."""
    if not GEMINI_KEY:
        raise RewriteFailed("GEMINI_API_KEY is not set")
    
    # Список строго актуальных и доступных моделей Google AI Studio
    models = [
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
    ]
    
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 429:
                print(f"  model {model}: HTTP 429 (quota exceeded)")
                continue
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"  Gemini model that works: {model}")
            return text
        except Exception as e:
            print(f"  model {model} failed: {e}")
            
    raise RewriteFailed("no Gemini model answered")


def ask_openai_compat(name, prompt):
    """Backup AI through an OpenAI-compatible API (GitHub Models, DeepSeek, Groq, ...)."""
    base_default, model_default = LLM_DEFAULTS[name]
    base = os.environ.get(f"{name}_BASE_URL", base_default).rstrip("/")
    model = os.environ.get(f"{name}_MODEL", model_default)
    key = os.environ[f"{name}_API_KEY"]
    for attempt, pause in enumerate((0, 5)):
        if pause:
            time.sleep(pause)
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        if r.status_code in TRANSIENT and attempt < 1:
            print(f"  {name} ({model}): HTTP {r.status_code}, retrying...")
            continue
        if r.status_code >= 400:
            print(f"  {name} ({model}): HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def rewrite(title, text):
    """Returns (headline, body). Uses Gemini if a key is set."""
    engines = []
    if GEMINI_KEY:
        engines.append(("Gemini", ask_gemini))
    for n in ("LLM2", "LLM3"):
        if os.environ.get(f"{n}_API_KEY"):
            engines.append((n, lambda p, n=n: ask_openai_compat(n, p)))
    if engines:
        prompt = (
            "Ти редактор українського Telegram-каналу СТРОГО про класичний великий футбол «football 90+». "
            "Напиши один якісний, насичений фактами пост на основі наданої новини українською мовою.\n\n"
            "ОБСЯГ ТА СТИЛЬ:\n"
            "- Для звичайних новин: 450–550 символів;\n"
            "- Для великих статей, інтерв'ю та розлогих цитат: до 700 символів (щоб повністю розкрити зміст);\n"
            "- Передавай КОНКРЕТНІ ФАКТИ, СТАТИСТИКУ ТА СУТЬ слів/події! "
            "Категорично заборонено використовувати порожню 'журналістську воду' (фрази типу 'тренер поділився думками', "
            "'фахівець відверто розповів', 'щира рефлексія'). Одразу розкривай суть: що конкретно сталося, які цифри "
            "або які саме слова сказав тренер/гравець;\n"
            "- Пиши ТІЛЬКИ про класичний футбол (гравці, тренери, матчі, трансфери);\n"
            "- Повністю ІГНОРУЙ інші види спорту (теніс, бокс, футзал, Олімпіаду тощо), навіть якщо вони є у тексті;\n"
            "- Без посилань, без згадок сайту чи джерела, нічого не вигадуй.\n\n"
            "ФОРМАТ ВІДПОВІДІ:\n"
            "Перший рядок - короткий влучний заголовок з одним доречним емодзі на початку.\n"
            "Далі порожній рядок і один-два місткі абзаци з фактами.\n\n"
            f"Заголовок: {title}\n\nТекст:\n{text}"
        )
    for name, fn in engines:
            try:
                out = fn(prompt)
                head, _, body = out.partition("\n")
                head, body = head.strip().strip("*#").strip(), body.strip()
                if head and body:
                    return head, body
                print(f" {name}: answer in unexpected format")
            except Exception as e:
                print(f" {name} failed:", e)

    if REQUIRE_REWRITE:
        raise RewriteFailed("all AI engines failed")
    return "⚽ " + title, text[:500]

def build_caption(head, body, limit=1024):
    head = re.sub(r"https?://\S+", "", head).strip()
    body = re.sub(r"https?://\S+", "", body).strip()
    while True:
        cap = f"<b>{html.escape(head)}</b>\n\n{html.escape(body)}"
        if len(cap) <= limit:
            return cap
        body = body[: int(len(body) * 0.9)]
        cut = body.rfind(". ")
        if cut > 100:
            body = body[: cut + 1]


def send(caption, image):
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    if image:
        r = requests.post(
            f"{api}/sendPhoto",
            data={"chat_id": CHANNEL, "photo": image, "caption": caption, "parse_mode": "HTML"},
            timeout=60,
        )
        if r.ok:
            return
        print("  sendPhoto failed, sending text only:", r.text[:200])
    r = requests.post(
        f"{api}/sendMessage",
        data={"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=60,
    )
    r.raise_for_status()


def now_ts():
    return time.time()


def to_minutes(value):
    """'21:30' -> 1290, '8' -> 480 (minutes since midnight)."""
    h, _, m = str(value).strip().partition(":")
    return int(h) * 60 + int(m or 0)


def kyiv_minutes():
    t = datetime.now(ZoneInfo("Europe/Kyiv"))
    return t.hour * 60 + t.minute


def in_quiet_hours():
    start, end, now = to_minutes(QUIET_FROM), to_minutes(QUIET_TO), kyiv_minutes()
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # the quiet period crosses midnight


def load_feeds():
    urls = []
    if FEEDS_FILE.exists():
        for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    urls += [u.strip() for u in os.environ.get("FEEDS", "").split() if u.strip()]
    return urls


def load_state():
    if not STATE_FILE.exists():
        return None
    data = json.loads(STATE_FILE.read_text())
    if isinstance(data, list):  # old format
        data = {"seen": data, "feeds": []}
    return data


def save_state(seen, feeds, last_post=0):
    STATE_FILE.write_text(json.dumps(
        {"seen": list(seen)[-1500:], "feeds": sorted(feeds), "last_post": last_post}))


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    run_start = now_ts()
    feeds = load_feeds()
    if not feeds:
        sys.exit("feeds.txt has no feed URLs")

    state = load_state()
    first_run = state is None
    state = state or {"seen": [], "feeds": []}
    seen = list(state["seen"])
    seen_set = set(seen)
    known_feeds = set(state["feeds"])
    last_post = float(state.get("last_post", 0))

    ok_feeds, candidates = 0, []
    for url in feeds:
        try:
            items = fetch_feed(url)
        except Exception as e:
            print("Feed failed:", url, "->", e)
            continue
        ok_feeds += 1
        print(f"Feed ok: {url} ({len(items)} items)")
        baseline = first_run or url not in known_feeds  # do not flood on first sight
        known_feeds.add(url)
        for it in items:
            k = item_key(it)
            if k in seen_set:
                continue
            if baseline or not is_football(it):
                seen.append(k); seen_set.add(k)
                continue
            candidates.append((k, it))

    if ok_feeds == 0:
        sys.exit("No feed could be read - run 'Check feeds' to see which ones work.")

    candidates.sort(key=lambda c: c[1]["ts"])

    if in_quiet_hours():
        if QUIET_DISCARD:  # night news are forgotten, the morning starts fresh
            for k, _ in candidates:
                seen.append(k); seen_set.add(k)
        print(f"Quiet hours ({QUIET_FROM}-{QUIET_TO} Kyiv): not posting.")
        save_state(seen, known_feeds, last_post)
        return

    # Oldest first, so the channel keeps chronological order. Items that did not
    # fit into this run stay in the queue; items older than MAX_AGE_HOURS are dropped.
    now = now_ts()
    queue = []
    for k, it in candidates:
        if it["ts"] and now - it["ts"] > MAX_AGE_HOURS * 3600:
            seen.append(k); seen_set.add(k)  # too old, skip for good
        else:
            queue.append((k, it))
    print(f"Queue: {len(queue)} item(s) waiting")

    wait = POST_INTERVAL_MIN * 60 - 150 - (now - last_post)  # 150 s grace for run jitter
    if queue and wait > 0:
        print(f"Not due yet: next post in about {int(wait // 60) + 1} min.")
        queue = []

    # Work through the queue: every item gets its own retry window. An item that cannot be
    # processed now stays in the queue and the next one is tried; each finished item is
    # published immediately.
    posted_now, attempts, failed_in_row = 0, 0, 0
    for k, it in queue:
        if posted_now >= MAX_PER_RUN or attempts >= MAX_PER_RUN + 4:
            break
        if now_ts() - run_start > RUN_BUDGET_MIN * 60:
            print("Run time budget used up - the rest follows on the next run.")
            break
        attempts += 1
        item_start = now_ts()
        print("Processing:", it["title"])
        try:
            text = to_text(it["content"]) or to_text(it["summary"])
            image = it["image"]
            if FETCH_ARTICLE:
                full, og = fetch_article(it["link"])
                if len(full) > len(text):
                    text = full
                image = image or og
            while True:  # retry the AI until it works or this item's retry window is over
                try:
                    head, body = rewrite(it["title"], text or it["title"])
                    break
                except RewriteFailed:
                    left = RETRY_WINDOW_MIN * 60 - (now_ts() - item_start)
                    if left <= RETRY_PAUSE_SEC:
                        raise
                    print(f"  AI unavailable, trying again in {RETRY_PAUSE_SEC:.0f} s "
                          f"({left / 60:.1f} min of retry window left)")
                    time.sleep(RETRY_PAUSE_SEC)
            send(build_caption(head, body), image if USE_SOURCE_IMAGE else "")
            seen.append(k); seen_set.add(k)
            last_post = now_ts()
            posted_now += 1
            failed_in_row = 0
            print("  Published.")
            if posted_now < MAX_PER_RUN:
                time.sleep(2)  # be gentle with Telegram between posts
        except RewriteFailed:
            failed_in_row += 1
            print("  Could not be processed now - it stays in the queue, trying the next one.")
            if failed_in_row >= 2:
                print("  The AI seems to be down - stopping, will try again on the next run.")
                break
        except Exception as e:
            print("  failed, will retry next run:", e)

    save_state(seen, known_feeds, last_post)
    print("Done.")


if __name__ == "__main__":
    main()
