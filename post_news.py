"""Reads RSS feeds from feeds.txt and posts new football news to a Telegram channel."""
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
USE_SOURCE_IMAGE = os.environ.get("USE_SOURCE_IMAGE", "false").lower() == "true"
FETCH_ARTICLE = os.environ.get("FETCH_ARTICLE", "true").lower() == "true"
FOOTBALL_ONLY = os.environ.get("FOOTBALL_ONLY", "true").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
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
SKIP_WORDS = ("прогноз", "ставк", "букмекер", "бонус", "трансляц", "дивитись онлайн")


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
    print("  Gemini will try these models in order:", ", ".join(cands[:8]) or "none")
    _state["cands"] = cands
    return cands


def call_gemini(model, prompt):
    r = requests.post(
        f"{GEMINI_API}/models/{model}:generateContent",
        params={"key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    if r.status_code >= 400:
        print(f"  model {model}: HTTP {r.status_code} {r.text[:300]}")
        r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def ask_gemini(prompt):
    """Tries the working model first, then others. Returns text or raises."""
    if _state["good"]:
        return call_gemini(_state["good"], prompt)
    order = [GEMINI_MODEL] + [m for m in list_candidates() if m != GEMINI_MODEL]
    tried = 0
    for model in order:
        if model in _state["failed"]:
            continue
        if tried >= 8:
            break
        tried += 1
        try:
            out = call_gemini(model, prompt)
            _state["good"] = model
            print("  Gemini model that works:", model)
            return out
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (404, 400):
                _state["failed"].add(model)
                continue  # try the next model
            raise
    raise RuntimeError("no Gemini model answered")


def rewrite(title, text):
    """Returns (headline, body). Uses Gemini if a key is set."""
    if GEMINI_KEY:
        prompt = (
            "Ти редактор українського футбольного Telegram-каналу «football 90+». "
            "Перепиши новину своїми словами українською: 350-600 символів, "
            "жива мова, без посилань, без згадок джерела чи сайту, лише факти з тексту. "
            "Не вигадуй деталей, яких немає в тексті. "
            "Формат відповіді: перший рядок - короткий заголовок з одним доречним емодзі "
            "на початку, далі порожній рядок і текст.\n\n"
            f"Заголовок: {title}\n\nТекст:\n{text}"
        )
        try:
            out = ask_gemini(prompt)
            head, _, body = out.partition("\n")
            head, body = head.strip().strip("*#").strip(), body.strip()
            if head and body:
                return head, body
        except Exception as e:
            print("  Gemini failed:", e)
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


def save_state(seen, feeds):
    STATE_FILE.write_text(json.dumps({"seen": list(seen)[-1500:], "feeds": sorted(feeds)}))


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    feeds = load_feeds()
    if not feeds:
        sys.exit("feeds.txt has no feed URLs")

    state = load_state()
    first_run = state is None
    state = state or {"seen": [], "feeds": []}
    seen = list(state["seen"])
    seen_set = set(seen)
    known_feeds = set(state["feeds"])

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
    for k, _ in candidates[:-MAX_PER_RUN] if len(candidates) > MAX_PER_RUN else []:
        seen.append(k); seen_set.add(k)  # too old to post, just remember

    for k, it in candidates[-MAX_PER_RUN:]:
        print("Posting:", it["title"])
        try:
            text = to_text(it["content"]) or to_text(it["summary"])
            image = it["image"]
            if FETCH_ARTICLE:
                full, og = fetch_article(it["link"])
                if len(full) > len(text):
                    text = full
                image = image or og
            head, body = rewrite(it["title"], text or it["title"])
            send(build_caption(head, body), image if USE_SOURCE_IMAGE else "")
            seen.append(k); seen_set.add(k)
        except Exception as e:
            print("  failed, will retry next run:", e)

    save_state(seen, known_feeds)
    print("Done.")


if __name__ == "__main__":
    main()
