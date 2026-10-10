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
    "нападник", "захисник", "уєфа", "фіфа", "футзал",
)
SKIP_WORDS = ("прогноз", "ставк", "букмекер", "бонус", "трансляц", "дивитись онлайн", "хто призначений на матч", "підготовка до матчу")
# News whose TITLE contains one of these whole words are never published.
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
        # media:content / media:thumbnail (names clash with "content" above)
        for ch in el:
            if local(ch.tag) in ("content", "thumbnail") and ch.attrib.get("url"):
                t = ch.attrib.get("type", "image")
                if t.startswith("image") or ch.attrib.get("medium") == "image" \
                        or local(ch.tag) == "thumbnail":
                    d["image"] = d["image"] or ch.attrib["url"]
        if not d["image"]:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', d["content"] + d["summary"])
            if m:
                d["image"] = html.unescape(m.group(1))
        if d["link"] and d["title"]:
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
ANNOUNCE_WORDS = (
    "проаналізуємо", "розповімо", "покажемо", "дізнаєтесь", "читайте",
    "дивитись", "дивитися", "у матеріалі", "у статті", "далі буде",
    "пропонуємо", "представляємо", "огляд", "анонс",
)


def is_announcement(item):
    """Отсеивает анонсы статей, а не сами новости."""
    blob = (item["title"] + " " + to_text(item["summary"])).lower()
    return any(w in blob for w in ANNOUNCE_WORDS)

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
    box = (
        soup.find("article")
        or soup.find("div", class_=re.compile("article|content|text|body", re.I))
        or soup.find("div", itemprop="articleBody")
        or soup
    )
    paras = [p.get_text(" ", strip=True) for p in box.find_all("p")]
    text = "\n".join(p for p in paras if len(p) > 80)
    return text[:4000], image


_state = {"cands": None, "good": "", "failed": set()}
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
TRANSIENT = (429, 500, 502, 503, 504)


class Failed(Exception):
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
    """One attempt per model. No internal retries — the outer loop handles them."""
    r = requests.post(
        f"{GEMINI_API}/models/{model}:generateContent",
        params={"key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=45,
    )
    if r.status_code >= 400:
        print(f"  model {model}: HTTP {r.status_code}")
        r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def ask_gemini(prompt):
    """Two passes over all models, one attempt per model per pass."""
    order, tried_models = [], set()
    for m in [_state["good"], GEMINI_MODEL] + list_candidates():
        if m and m not in tried_models:
            tried_models.add(m)
            order.append(m)

    for pass_num in (1, 2):
        print(f"  Gemini pass {pass_num}/2 over {len(order)} model(s)")
        for model in order:
            if model in _state["failed"]:
                continue
            try:
                out = call_gemini(model, prompt)
                if _state["good"] != model:
                    print("  Gemini model that works:", model)
                    _state["good"] = model
                return out
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code in (404, 400):
                    _state["failed"].add(model)
                    print(f"  model {model}: HTTP {code} (unusable, skipping forever)")
                elif code == 429:
                    print(f"  model {model}: HTTP 429 (quota)")
                else:
                    print(f"  model {model}: HTTP {code}")
            except requests.RequestException as e:
                print(f"  model {model}: {e}")
        if pass_num == 1:
            print("  Gemini: pass 1 failed, waiting 5 s before pass 2")
            time.sleep(5)

    raise RuntimeError("no Gemini model answered on either pass")


def ask_openai_compat(name, prompt):
    """One attempt per backup AI. No internal retries."""
    base_default, model_default = LLM_DEFAULTS[name]
    base = os.environ.get(f"{name}_BASE_URL", base_default).rstrip("/")
    model = os.environ.get(f"{name}_MODEL", model_default)
    key = os.environ[f"{name}_API_KEY"]
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    if r.status_code >= 400:
        print(f"  {name} ({model}): HTTP {r.status_code}")
        r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def rewrite(title, text):
    """Returns (headline, body) or (None, None) if all AI engines fail."""
    engines = []
    if GEMINI_KEY:
        engines.append(("Gemini", ask_gemini))
    for n in ("LLM2", "LLM3"):
        if os.environ.get(f"{n}_API_KEY"):
            engines.append((n, lambda p, n=n: ask_openai_compat(n, p)))

    if not engines:
        return None, None  # нет AI-движков — не публикуем

    prompt = (
        "Ти редактор українського футбольного Telegram-каналу «football 90+». "
        "Твоє завдання — перефразувати новину українською мовою, ЗБЕРІГАЮЧИ ВСІ ФАКТИ.\n\n"
        "ЩО МОЖНА РОБИТИ:\n"
        "- перефразовувати речення своїми словами (синоніми, інший порядок слів);\n"
        "- об'єднувати речення, прибирати ДРУГОРЯДНІ деталі (описи, повтори, "
        "загальні фрази), але НЕ факти, НЕ імена, НЕ числа, НЕ назви;\n"
        "- змінювати порядок речень для кращого звучання;\n"
        "- додавати ОДИН доречний емодзі на початку заголовка.\n\n"
        "ОБОВ'ЯЗКОВО ЗБЕРІГАЙ (це НЕ другорядні деталі):\n"
        "- УСІ імена людей: тренерів, гравців, президентів клубів "
        "(«Сергій Нагорняк», «Валерій Лучкевич», «Кирило Ковалець», «Джон Себеріо»); "
        "НЕ узагальнюй до «тренер», «гравець», «захисник»;\n"
        "- УСІ назви клубів, стадіонів, міст, країн;\n"
        "- УСІ числа, дати, терміни, рахунки;\n"
        "- конкретні факти: травми, терміни повернення.\n\n"
        "ЩО КАТЕГОРИЧНО ЗАБОРОНЕНО:\n"
        "- ДОДАВАТИ будь-які факти, числа, дати, назви, причини, оцінки, яких немає в оригіналі. "
        "Якщо в оригіналі не сказано «травма» — не пиши «травма». Якщо не сказано «нарешті» — не пиши «нарешті»;\n"
        "- ЗАМІНЮВАТИ назви клубів, стадіонів, людей. Якщо в оригіналі «Аль-Ахлі» — пиши «Аль-Ахлі», "
        "а не «Шахтар». Якщо «Marino Pusic» — пиши «Marino Pusic», не вигадуй інших тренерів;\n"
        "- ВИГАДУВАТИ прогнози, припущення, причини («схоже», «ймовірно», «можливо», "
        "«подивимось», «здається», «мабуть»);\n"
        "- ВИКОРИСТОВУВАТИ російські літери (ы, э, ё, ъ);\n"
        "- ПЕРЕКЛАДАТИ іноземні назви клубів та імена кирилицею: "
        "Al Fateh club stadium, Marino Pusic, Al-Ahli — залишай ЛАТИНКОЮ;\n"
        "- додавати посилання, згадки джерела, Markdown.\n\n"
        "УНИКАЙ ВОДЫ:\n"
        "- НЕ пиши фразы вида «проаналізуємо», «розповімо», «покажемо», "
        "«оцінимо», «розглянемо», «пропонуємо» — це анонси, а не новини;\n"
        "- якщо в оригіналі немає конкретних фактів (цифр, імен, результатів), "
        "а тільки загальні слова — напиши коротко по суті;\n"
        "- НЕ додавай речень «оцінка базується на…», «дані взяті з…» — "
        "це службова інформація, а не новина.\n\n"
        "МОВА:\n"
        "- українська; іноземні назви — латиниця; українські назви — українською "
        "(Шахтар, Динамо Київ, Артем Бондаренко);\n"
        "- жива, але стримана мова без емоційних оцінок.\n\n"
        "ОБСЯГ: 450-720 символів.\n\n"
        "ФОРМАТ ВІДПОВІДІ:\n"
        "Перший рядок — короткий заголовок з одним емодзі на початку. "
        "Далі порожній рядок і текст новини.\n\n"
        f"ОРИГІНАЛЬНИЙ ЗАГОЛОВОК: {title}\n\n"
        f"ОРИГІНАЛЬНИЙ ТЕКСТ:\n{text}"
    )

    for pass_num in (1, 2):
        print(f"  AI pass {pass_num}/2 over {len(engines)} engine(s)")
        for name, fn in engines:
            try:
                out = fn(prompt)
                head, _, body = out.partition("\n")
                head = head.strip().strip("*#").strip()
                body = body.strip()
                if head and body:
                    return head, body
                print(f"  {name}: answer in unexpected format")
            except Exception as e:
                print(f"  {name} failed:", e)
        if pass_num == 1:
            print("  AI: pass 1 failed, waiting 5 s before pass 2")
            time.sleep(5)

    if REQUIRE_REWRITE:
        raise RewriteFailed("all AI engines failed on both passes")
    return None, None

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
        if baseline or not is_football(it) or is_announcement(it):
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
             if len(full) > 200:
                 text = full
             image = image or og

            has_numbers = bool(re.search(r"\d", text))
            has_names = bool(re.search(r"[A-ZА-ЯІЇЄ][a-zа-яіїє]{2,}", text))
            has_quotes = bool(re.search(r"[«»\"']", text))
            if not (has_numbers or has_names or has_quotes):
                print(f"  Drop (no concrete facts): {it['title'][:60]}")
                seen.append(k); seen_set.add(k)
                continue

        while True:
                try:
                    head, body = rewrite(it["title"], text or it["title"])
                    if not head or not body:
                        raise RewriteFailed("empty rewrite")
                    break
                except RewriteFailed:
                    ...
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
