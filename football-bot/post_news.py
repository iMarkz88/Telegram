"""Collects football news and posts them to a Telegram channel.

Flow: read the news list -> find new articles -> rewrite the text
(Gemini, optional) -> send to the channel via the Telegram Bot API.
"""
import html
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://sport.ua/uk/football"
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
# "true" -> use the article's own photo. Their site terms forbid this
# without written permission, so it is OFF by default.
USE_SOURCE_IMAGE = os.environ.get("USE_SOURCE_IMAGE", "false").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
STATE_FILE = Path("posted.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
NEWS_RE = re.compile(r"^https://sport\.ua/uk/news/(\d+)-")
SKIP_WORDS = ("прогноз", "ставк", "букмекер", "бонус", "анонс")


def get_latest():
    """Returns {article_id: (url, title)} from the football page."""
    r = requests.get(LIST_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0]
        if href.startswith("/"):
            href = "https://sport.ua" + href
        m = NEWS_RE.match(href)
        title = a.get_text(" ", strip=True)
        if not m or len(title) < 15:
            continue
        if any(w in title.lower() for w in SKIP_WORDS):
            continue
        found.setdefault(int(m.group(1)), (href, title))
    return found


def load_state():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return None


def save_state(ids):
    STATE_FILE.write_text(json.dumps(sorted(ids)[-500:]))


def fetch_article(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"].strip() if tag and tag.get("content") else ""

    title, desc, image = meta("og:title"), meta("og:description"), meta("og:image")
    box = soup.find("article") or soup
    paras = [p.get_text(" ", strip=True) for p in box.find_all("p")]
    text = "\n".join(p for p in paras if len(p) > 50)[:4000] or desc
    if "social_logo" in image:
        image = ""
    return title, text, image


def rewrite(title, text):
    """Returns (headline, body). Uses Gemini if a key is set."""
    if GEMINI_KEY:
        prompt = (
            "Ти редактор українського футбольного Telegram-каналу «football 90+». "
            "Перепиши новину своїми словами українською: 350-600 символів, "
            "жива мова, без посилань, без згадок джерела чи сайту, лише факти з тексту. "
            "Формат відповіді: перший рядок - короткий заголовок з одним доречним емодзі "
            "на початку, далі порожній рядок і текст.\n\n"
            f"Заголовок: {title}\n\nТекст:\n{text}"
        )
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                params={"key": GEMINI_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            r.raise_for_status()
            out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            head, _, body = out.partition("\n")
            head = head.strip().strip("*#").strip()
            body = body.strip()
            if head and body:
                return head, body
        except Exception as e:  # fall back to the plain text below
            print("Gemini failed:", e)
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
    api = f"https://api.telegram.org/bot{TOKEN}"
    if image:
        r = requests.post(
            f"{api}/sendPhoto",
            data={"chat_id": CHANNEL, "photo": image, "caption": caption, "parse_mode": "HTML"},
            timeout=60,
        )
        if r.ok:
            return
        print("sendPhoto failed, sending text only:", r.text)
    r = requests.post(
        f"{api}/sendMessage",
        data={"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=60,
    )
    r.raise_for_status()


def main():
    latest = get_latest()
    if not latest:
        print("No articles found - the site layout may have changed.")
        sys.exit(1)

    posted = load_state()
    if posted is None:  # first run: remember current news, post nothing
        save_state(set(latest))
        print(f"First run: remembered {len(latest)} articles, nothing posted.")
        return

    new_ids = sorted(i for i in latest if i not in posted)
    to_post = new_ids[-MAX_PER_RUN:]
    for i in to_post:
        url, list_title = latest[i]
        try:
            title, text, image = fetch_article(url)
            head, body = rewrite(title or list_title, text)
            send(build_caption(head, body), image if USE_SOURCE_IMAGE else "")
            print("Posted:", list_title)
        except Exception as e:
            print("Failed:", list_title, e)
            new_ids.remove(i)  # try again next run
    save_state(posted | set(new_ids))


if __name__ == "__main__":
    main()
