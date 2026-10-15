"""
Рубрика «Великі футболісти» — щопонеділка 13:00 Київ.

Логіка відбору:
  1. RSSSF Ballon d\'Or — для кожного року 1956+ відкриває сторінку року
     (europa-poy56.html …) і бере ВСІХ номінантів зі списку голосування.
  2. Роки в випадковому порядку (seed зберігається в state).
  3. Пропускає вже опублікованих (published по qid / нормалізованому імені).
  4. Коли всі номінанти RSSSF вичерпані — Hall of Fame топ-ліг (Wikidata).
  5. Біографія з en.wikipedia, переказ ~95%, 2000–3000 слів.
  6. PLAYERS_FORCE публікує, але НЕ пише в published.
"""
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests

import post_news as pn
import quiz

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
PUBLISH_TIME = "13:00"
STATE_FILE = Path("great_players_state.json")
PLAYERS_IMAGE = os.environ.get("PLAYERS_IMAGE", "great_players.png")
TG_LIMIT = 3900

RSSSF_INDEX = "https://www.rsssf.org/miscellaneous/europa-poy.html"
RSSSF_YEAR = "https://www.rsssf.org/miscellaneous/europa-poy{yy}.html"

# Hall of Fame / related (Wikidata) — запасний пул
HOF_QUERIES = [
    # English Football Hall of Fame inductees (association football)
    """
    SELECT DISTINCT ?person ?personLabel ?sl WHERE {
      ?person wdt:P166 ?award .
      ?award rdfs:label ?alabel .
      FILTER(CONTAINS(LCASE(?alabel), "hall of fame"))
      ?person wdt:P106 wd:Q937857 .
      ?person wdt:P21 wd:Q6581097 .
      ?person wikibase:sitelinks ?sl .
      FILTER(?sl >= 25)
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    ORDER BY DESC(?sl)
    LIMIT 200
    """,
]

WIKI_HEADERS = {
    "User-Agent": "football90-great-players/1.0 (Telegram @football_90_pluss)",
    "Accept": "application/json",
}
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; football90-bot/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}


def run_sparql(query: str, timeout: int = 90):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(
                "https://query.wikidata.org/sparql",
                params={"query": query, "format": "json"},
                headers={**WIKI_HEADERS, "Accept": "application/sparql-results+json"},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()["results"]["bindings"]
        except Exception as e:
            last = e
            time.sleep(4 * (attempt + 1))
    raise last


def normalize_name(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[\"'`]", "", n)
    n = re.sub(r"\s+", " ", n)
    # прибрати nicknames в лапках
    n = re.sub(r"\s*[\"'].*?[\"']\s*", " ", n).strip()
    return n


def fix_rsssf_name(raw: str) -> str:
    """Stanley MATTHEWS / Alfredo DI STÉFANO → Title Case."""
    raw = re.sub(r"\s+", " ", raw.strip())
    # прибрати nick "Julinho"
    raw = re.sub(r'\s*["\'].*?["\']\s*', " ", raw).strip()
    parts = []
    for p in raw.split():
        if p.isupper() and len(p) > 1:
            parts.append(p.title())
        else:
            parts.append(p)
    return " ".join(parts)


def parse_year_page(html_text: str, year: int) -> list[str]:
    """Витягує імена гравців зі сторінки року RSSSF."""
    names = []
    # Формат таблиці:
    # 1. Stanley Matthews            England            Blackpool              47 ...
    # або з TAB/пробілами
    for m in re.finditer(
        r"^\s*\d+\.\s+([A-Za-zÀ-ÿ\.\-'\s\"]+?)\s{2,}([A-Za-z].*?)\s{2,}",
        html_text,
        re.M,
    ):
        name = fix_rsssf_name(m.group(1))
        if len(name) >= 4 and not name.lower().startswith("pos"):
            names.append(name)

    # запасний патерн без жорстких пробілів
    if len(names) < 5:
        for m in re.finditer(
            r"^\s*\d+\.\s+([A-Z][A-Za-zÀ-ÿ\.\-'\s]+?)\s+(England|Spain|France|Italy|Germany|West Germany|Netherlands|Portugal|Brazil|Argentina|Hungary|Scotland|Soviet Union|Wales|Belgium|Sweden|Czechoslovakia|Yugoslavia|Denmark|Bulgaria|Austria|Poland|Turkey|Switzerland|Greece|Northern Ireland|Croatia|Ukraine|Liberia|Czech)\b",
            html_text,
            re.M,
        ):
            name = fix_rsssf_name(m.group(1))
            if len(name) >= 4:
                names.append(name)

    # унікальні зі збереженням порядку
    seen, out = set(), []
    for n in names:
        k = normalize_name(n)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def fetch_year_nominees(year: int) -> list[str]:
    yy = str(year)[-2:]
    url = RSSSF_YEAR.format(yy=yy)
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        if r.status_code == 404:
            print(f"  year {year}: page not found ({url})")
            return []
        r.raise_for_status()
        names = parse_year_page(r.text, year)
        print(f"  year {year}: {len(names)} nominees from RSSSF")
        return names
    except Exception as e:
        print(f"  year {year} fetch failed: {e}")
        return []


def resolve_to_wikidata(name: str) -> dict | None:
    """Ім'я → qid + enwiki title."""
    # 1) wbsearchentities
    try:
        r = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities",
                "search": name,
                "language": "en",
                "type": "item",
                "limit": 8,
                "format": "json",
            },
            headers=WIKI_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        for hit in r.json().get("search", []):
            desc = (hit.get("description") or "").lower()
            if any(x in desc for x in (
                "footballer", "football player", "soccer player",
                "association football", "goalkeeper",
            )):
                qid = hit["id"]
                title = get_en_wiki_title(qid)
                if not title:
                    title = hit.get("label", name).replace(" ", "_")
                return {
                    "qid": qid,
                    "name": hit.get("label", name),
                    "wiki": title,
                    "source": "rsssf",
                    "norm": normalize_name(hit.get("label", name)),
                }
    except Exception as e:
        print(f"  resolve {name!r} failed: {e}")
    return None


def get_en_wiki_title(qid: str) -> str | None:
    query = f"""
    SELECT ?title WHERE {{
      ?sitelink schema:about wd:{qid} ;
                schema:isPartOf <https://en.wikipedia.org/> ;
                schema:name ?title .
    }} LIMIT 1
    """
    try:
        rows = run_sparql(query, timeout=20)
        if rows:
            return rows[0]["title"]["value"].replace(" ", "_")
    except Exception:
        pass
    return None


def build_rsssf_pool(state: dict) -> list[dict]:
    """
    Будує пул номінантів по роках.
    Роки — у випадковому порядку (seed у state).
    """
    years = list(range(1956, 2026))
    # роки без окремої сторінки / без нагороди
    skip_years = {2010, 2011, 2012, 2013, 2014, 2015, 2020}
    years = [y for y in years if y not in skip_years]

    seed = state.get("year_seed")
    if seed is None:
        seed = random.randint(1, 10_000_000)
        state["year_seed"] = seed
    rng = random.Random(seed)
    rng.shuffle(years)
    state["year_order"] = years

    published_qids = set(state.get("published", []))
    published_names = set(state.get("published_names", []))

    pool = []
    seen_qid = set()
    seen_norm = set()

    for year in years:
        names = fetch_year_nominees(year)
        time.sleep(0.4)
        for name in names:
            norm = normalize_name(name)
            if norm in published_names or norm in seen_norm:
                continue
            hit = resolve_to_wikidata(name)
            time.sleep(0.2)
            if not hit:
                continue
            if hit["qid"] in published_qids or hit["qid"] in seen_qid:
                continue
            if hit["norm"] in published_names or hit["norm"] in seen_norm:
                continue
            hit["year"] = year
            pool.append(hit)
            seen_qid.add(hit["qid"])
            seen_norm.add(hit["norm"])
            seen_norm.add(norm)

    print(f"  RSSSF pool size (unresolved skipped): {len(pool)}")
    return pool


def build_hof_pool(state: dict) -> list[dict]:
    """Запасний пул — Hall of Fame з Wikidata."""
    published_qids = set(state.get("published", []))
    out, seen = [], set()
    for q in HOF_QUERIES:
        try:
            rows = run_sparql(q, timeout=120)
        except Exception as e:
            print(f"  HOF query failed: {e}")
            continue
        for b in rows:
            qid = b["person"]["value"].rsplit("/", 1)[-1]
            if qid in published_qids or qid in seen:
                continue
            name = b.get("personLabel", {}).get("value", "")
            if not name or name.startswith("Q"):
                continue
            title = get_en_wiki_title(qid) or name.replace(" ", "_")
            out.append({
                "qid": qid,
                "name": name,
                "wiki": title,
                "source": "hall_of_fame",
                "norm": normalize_name(name),
                "year": None,
            })
            seen.add(qid)
    print(f"  Hall of Fame pool: {len(out)}")
    return out


def load_state():
    state = {
        "published": [],
        "published_names": [],
        "last_date": "",
        "done": False,
        "messages": [],
        "current_qid": "",
        "current_title": "",
        "pool_cache": [],
        "pool_updated": "",
        "year_seed": None,
        "year_order": [],
        "phase": "rsssf",  # rsssf | hof
    }
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state["published"] = list(dict.fromkeys(state.get("published", [])))
    state["published_names"] = list(dict.fromkeys(state.get("published_names", [])))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_pool(state) -> list:
    today = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()
    need = (
        not state.get("pool_cache")
        or state.get("pool_updated", "") < today[:7]  # раз на ~місяць можна, але refresh якщо порожній
    )
    # якщо фаза rsssf і пул порожній після фільтра — перейти на hof
    if need or not state.get("pool_cache"):
        print("  Building RSSSF nominee pool (may take a few minutes)...")
        pool = build_rsssf_pool(state)
        if not pool:
            print("  RSSSF exhausted / empty → Hall of Fame")
            state["phase"] = "hof"
            pool = build_hof_pool(state)
        state["pool_cache"] = pool
        state["pool_updated"] = today
        save_state(state)
        return pool
    return state["pool_cache"]


def choose_next_player(state) -> dict | None:
    published = set(state.get("published", []))
    pub_names = set(state.get("published_names", []))
    pool = ensure_pool(state)

    for p in pool:
        if p["qid"] in published:
            continue
        if p.get("norm") in pub_names:
            continue
        return p

    # пул вичерпано — спробувати HOF
    if state.get("phase") != "hof":
        print("  RSSSF pool fully published → switching to Hall of Fame")
        state["phase"] = "hof"
        pool = build_hof_pool(state)
        state["pool_cache"] = pool
        state["pool_updated"] = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()
        save_state(state)
        for p in pool:
            if p["qid"] not in published and p.get("norm") not in pub_names:
                return p

    print("Great Players: усі кандидати опубліковані.")
    return None


def fetch_wikipedia(title: str) -> str:
    r = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "titles": title,
            "prop": "extracts", "explaintext": True, "exsectionformat": "plain",
        },
        headers=WIKI_HEADERS, timeout=60,
    )
    r.raise_for_status()
    page = next(iter(r.json()["query"]["pages"].values()))
    extract = page.get("extract", "")
    if not extract or "missing" in page:
        raise ValueError(f"Wikipedia missing: {title}")
    return extract


def format_with_ai(source_text: str, ua_title: str, run_start: float):
    prompt = f"""
Ти — головний редактор українського футбольного Telegram-каналу «football 90+».
Напиши ДОВГИЙ розгорнутий матеріал українською про футболіста {ua_title}.

=== ТОЧНІСТЬ ===
1. Покрий ~95% фактів джерела своїми словами (не копіюй дослівно).
2. Усі клуби, тренери, дати, трофеї з тексту — з роками.
3. На ключові дати — 2–3 речення.
4. Нічого не вигадуй.

=== ОБСЯГ ===
2000–3000 слів. Імена і клуби — ЛАТИНКОЮ. Без емодзі/Markdown/посилань.
Відповідай ЛИШЕ текстом статті.

Джерело:
{source_text[:18000]}
"""
    res = quiz.ai_call(prompt, run_start)
    if res is None:
        return None
    raw, _ = res
    text = quiz.clean(raw)
    wc = len(text.split())
    if wc < 1400:
        print(f"  AI text too short: {wc}")
        return None
    print(f"  AI text ready: {wc} words")
    return text


def build_messages(text: str, ua_title: str) -> list:
    head = f"⚽ <b>Великі футболісти</b>\n📅 {ua_title}\n\n"
    cont = f"⚽ <b>Великі футболісти (продовження)</b>\n📅 {ua_title}\n\n"
    max_body = TG_LIMIT - max(len(head), len(cont)) - 20

    def split_long(chunk: str) -> list:
        if len(chunk) <= max_body:
            return [chunk]
        parts, buf = [], ""
        for s in re.split(r"(?<=[.!?…])\s+", chunk):
            if len(s) > max_body:
                if buf:
                    parts.append(buf.strip())
                    buf = ""
                for i in range(0, len(s), max_body):
                    parts.append(s[i:i + max_body])
            elif len(buf) + len(s) + 1 <= max_body:
                buf = (buf + " " + s).strip()
            else:
                if buf:
                    parts.append(buf.strip())
                buf = s
        if buf:
            parts.append(buf.strip())
        return parts

    pieces = []
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        pieces.extend(split_long(html.escape(para)))

    messages, current = [], head
    for piece in pieces:
        add = piece + "\n\n"
        if len(current) + len(add) > TG_LIMIT and current not in (head, cont):
            messages.append(current.rstrip())
            current = cont
        if len(current) + len(add) > TG_LIMIT:
            if current not in (head, cont):
                messages.append(current.rstrip())
            messages.append((cont + piece).rstrip())
            current = cont
        else:
            current += add
    if current not in (head, cont) and current.strip():
        messages.append(current.rstrip())
    elif not messages and current.strip():
        messages.append(current.rstrip())
    return messages


def prepare(run_start: float):
    state = load_state()
    player = choose_next_player(state)
    if not player:
        return None, "", ""

    print(f"Great Players: preparing {player['name']} "
          f"(year={player.get('year')}, src={player.get('source')}, wiki={player.get('wiki')})")
    try:
        source = fetch_wikipedia(player["wiki"])
        if len(source) < 500:
            print("  wiki too short → mark published & skip")
            state["published"].append(player["qid"])
            state.setdefault("published_names", []).append(player.get("norm", ""))
            save_state(state)
            return None, "", ""
    except Exception as e:
        print(f"  wiki failed: {e}")
        return None, "", ""

    text = format_with_ai(source, player["name"], run_start)
    if not text:
        return None, "", ""
    return build_messages(text, player["name"]), player["qid"], player["name"]


def send_message(text: str, image: str | None = None):
    """Перше повідомлення — з картинкою (caption), далі тільки текст."""
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    if image and os.path.exists(image):
        # caption limit Telegram = 1024; якщо довше — фото без підпису + текст окремо
        if len(text) <= 1024:
            with open(image, "rb") as f:
                r = requests.post(
                    f"{api}/sendPhoto",
                    data={
                        "chat_id": CHANNEL,
                        "caption": text,
                        "parse_mode": "HTML",
                    },
                    files={"photo": f},
                    timeout=60,
                )
            if r.ok:
                return
            print("  sendPhoto failed, fallback to text:", r.text[:200])
        else:
            # довгий текст: спочатку фото з коротким підписом, потім повний текст
            short = text[:1000].rsplit("\n", 1)[0] + "…"
            with open(image, "rb") as f:
                r = requests.post(
                    f"{api}/sendPhoto",
                    data={
                        "chat_id": CHANNEL,
                        "caption": short,
                        "parse_mode": "HTML",
                    },
                    files={"photo": f},
                    timeout=60,
                )
            if not r.ok:
                print("  sendPhoto failed:", r.text[:200])
            # повний текст окремим повідомленням
            r = requests.post(
                f"{api}/sendMessage",
                data={
                    "chat_id": CHANNEL,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=60,
            )
            if not r.ok:
                print("  Telegram error:", r.text[:300])
                r.raise_for_status()
            return

    r = requests.post(
        f"{api}/sendMessage",
        data={
            "chat_id": CHANNEL,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=60,
    )
    if not r.ok:
        print("  Telegram error:", r.text[:300])
        r.raise_for_status()


def publish(messages: list, ua_title: str = ""):
    """Спочатку фото з коротким підписом, потім усі текстові частини."""
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    img = PLAYERS_IMAGE
    if img and os.path.exists(img):
        caption = f"⚽ <b>Великі футболісти</b>\n📅 {html.escape(ua_title or '')}"
        with open(img, "rb") as f:
            r = requests.post(
                f"{api}/sendPhoto",
                data={
                    "chat_id": CHANNEL,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"photo": f},
                timeout=60,
            )
        if not r.ok:
            print("  sendPhoto failed:", r.text[:200])
        time.sleep(1)

    for i, m in enumerate(messages):
        send_message(m, image=None)
        if i < len(messages) - 1:
            time.sleep(2)


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    run_start = time.time()
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today = now.date().isoformat()

    if os.environ.get("PLAYERS_FORCE", "").lower() == "true":
        messages, qid, title = prepare(run_start)
        if messages:
            publish(messages, title)
            print(f"Great Players TEST published ({len(messages)} msgs). "
                  f"State NOT updated (qid={qid}).")
        else:
            print("Great Players test: failed.")
        return

    # тільки понеділок
    if now.weekday() != 0:
        return

    state = load_state()
    if state.get("last_date") == today and state.get("done"):
        return

    mins = now.hour * 60 + now.minute
    pub = pn.to_minutes(PUBLISH_TIME)
    PREPARE_START = 0 * 60 + 1   # 00:01
    PREPARE_END = 6 * 60 + 30    # 06:30

    if mins < pub:
        if state.get("messages"):
            print("Great Players: already prepared, waiting for", PUBLISH_TIME)
            return
        if mins < PREPARE_START:
            print("Great Players: before 00:01, skip")
            return
        print(f"Great Players: preparing (mins={mins}, window 00:01–06:30 preferred, until 13:00 ok)...")
        messages, qid, title = prepare(run_start)
        state["messages"] = messages or []
        state["current_qid"] = qid
        state["current_title"] = title
        save_state(state)
        if messages:
            print("Great Players: prepared, waiting for", PUBLISH_TIME)
        else:
            print("Great Players: not ready yet, will retry until", PUBLISH_TIME)
        return

    messages = state.get("messages")
    qid = state.get("current_qid", "")
    title = state.get("current_title", "")
    if not messages:
        messages, qid, title = prepare(run_start)
    if not messages:
        print("Great Players: nothing to publish")
        return

    publish(messages, title)
    if qid and qid not in state["published"]:
        state["published"].append(qid)
    norm = normalize_name(title) if title else ""
    if norm and norm not in state.get("published_names", []):
        state.setdefault("published_names", []).append(norm)
    state["last_date"] = today
    state["done"] = True
    state["messages"] = []
    state["current_qid"] = ""
    state["current_title"] = ""
    # прибрати з pool_cache
    state["pool_cache"] = [p for p in state.get("pool_cache", []) if p.get("qid") != qid]
    save_state(state)
    print(f"Great Players published ({len(messages)} msgs). "
          f"Total published: {len(state['published'])}")


if __name__ == "__main__":
    main()
