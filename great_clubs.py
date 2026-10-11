"""
Рубрика «Великі клуби»
Публікація: щоп’ятниці о 13:00 за Києвом.
Джерело: Wikipedia → переказ українською (1500+ слів).
Картинка додається окремо.

Автоматичний вибір клубів через Wikidata:
- переможці Ліги чемпіонів з 1990
- переможці Ліги Європи / Кубка УЄФА з 1990
- чемпіони топ-ліг Європи + Бразилія з 1990
- переможці національних кубків (останні перемоги в 90-х / 2000-х)
- пріоритет тим, хто був на піку в 90-х–2000-х (навіть якщо зараз середняк)
- бот запам’ятовує всі опубліковані і ніколи не повторює
"""
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import post_news as pn
import quiz

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
PUBLISH_TIME = "13:00"
STATE_FILE = Path("great_clubs_state.json")
TG_LIMIT = 3900

WIKI_HEADERS = {
    "User-Agent": "football90-great-clubs/1.0 (Telegram @football_90_pluss)",
    "Accept": "application/json",
}

# Топ-ліги (чемпіонати)
TOP_LEAGUES = {
    "Q9448": "Premier League",           # England
    "Q15804": "Serie A",                 # Italy
    "Q324867": "La Liga",                # Spain
    "Q82595": "Bundesliga",              # Germany
    "Q13394": "Ligue 1",                 # France
    "Q167541": "Eredivisie",             # Netherlands
    "Q219967": "Primeira Liga",          # Portugal
    "Q202306": "Belgian Pro League",     # Belgium
    "Q202999": "Scottish Premiership",   # Scotland
    "Q167277": "Süper Lig",              # Turkey
    "Q182994": "Ukrainian Premier League",
    "Q831202": "Campeonato Brasileiro Série A",  # Brazil
}

# Національні кубки (основні)
DOMESTIC_CUPS = {
    "Q11306": "FA Cup",                  # England
    "Q11308": "EFL Cup",                 # England League Cup
    "Q11313": "Coppa Italia",            # Italy
    "Q11309": "Copa del Rey",            # Spain
    "Q11315": "DFB-Pokal",               # Germany
    "Q11317": "Coupe de France",         # France
    "Q11319": "KNVB Cup",                # Netherlands
    "Q11321": "Taça de Portugal",        # Portugal
    "Q11323": "Belgian Cup",             # Belgium
    "Q11325": "Scottish Cup",            # Scotland
    "Q11327": "Turkish Cup",             # Turkey
    "Q11329": "Ukrainian Cup",           # Ukraine
    "Q11331": "Copa do Brasil",          # Brazil
}

# Європейські турніри
UCL = "Q187664"          # Champions League
UEL = "Q18756"           # Europa League / UEFA Cup


def run_sparql(query: str, timeout: int = 90):
    last_error = None
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
            last_error = e
            wait = 5 * (attempt + 1)
            print(f"  SPARQL error (спроба {attempt + 1}/3): {e}, чекаємо {wait}с...")
            time.sleep(wait)
    raise last_error


def fetch_competition_winners(comp_qid: str, since: int = 1990, label: str = "") -> list[dict]:
    """Універсальний запит переможців будь-якого турніру з певного року."""
    query = f"""
    SELECT DISTINCT ?club ?clubLabel ?year WHERE {{
      ?award wdt:P1346 ?club .
      ?award wdt:P3450 wd:{comp_qid} .
      ?award wdt:P585 ?date .
      BIND(YEAR(?date) AS ?year)
      FILTER(?year >= {since})
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,uk". }}
    }}
    ORDER BY ?year
    """
    try:
        rows = run_sparql(query, timeout=60)
    except Exception as e:
        print(f"  {label or comp_qid} query failed: {e}")
        return []
    out = []
    for b in rows:
        qid = b["club"]["value"].rsplit("/", 1)[-1]
        name = b.get("clubLabel", {}).get("value", "")
        year = int(b["year"]["value"])
        if name and not name.startswith("Q"):
            out.append({"qid": qid, "name": name, "year": year, "source": label or comp_qid})
    return out


def fetch_league_champions() -> list[dict]:
    """Чемпіони топ-ліг + Бразилія з 1990."""
    league_values = " ".join(f"wd:{qid}" for qid in TOP_LEAGUES)
    query = f"""
    SELECT DISTINCT ?club ?clubLabel ?year WHERE {{
      VALUES ?league {{ {league_values} }}
      ?season wdt:P3450 ?league .
      ?season wdt:P1346 ?club .
      ?season wdt:P585 ?date .
      BIND(YEAR(?date) AS ?year)
      FILTER(?year >= 1990 && ?year <= 2026)
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,uk". }}
    }}
    ORDER BY ?year
    LIMIT 500
    """
    try:
        rows = run_sparql(query, timeout=120)
    except Exception as e:
        print(f"  League champions query failed: {e}")
        return []
    out = []
    for b in rows:
        qid = b["club"]["value"].rsplit("/", 1)[-1]
        name = b.get("clubLabel", {}).get("value", "")
        year = int(b["year"]["value"])
        if name and not name.startswith("Q"):
            out.append({"qid": qid, "name": name, "year": year, "source": "league"})
    return out


def fetch_cup_winners() -> list[dict]:
    """Переможці національних кубків (беремо з 1985, щоб зловити тих,
    чиї останні перемоги припали на 90-ті/2000-ті)."""
    all_winners = []
    for qid, name in DOMESTIC_CUPS.items():
        winners = fetch_competition_winners(qid, since=1985, label=name)
        all_winners.extend(winners)
        time.sleep(0.3)  # не молотити Wikidata
    return all_winners



def fetch_ucl_uel() -> list[dict]:
    """Переможці ЛЧ та Ліги Європи з 1990 — найчистіше джерело."""
    out = []
    for qid, label in [(UCL, "UCL"), (UEL, "UEL")]:
        rows = fetch_competition_winners(qid, since=1990, label=label)
        out.extend(rows)
        print(f"    {label}: {len(rows)}")
    return out


def fetch_major_league_champions() -> list[dict]:
    """Чемпіони тільки великих ліг. Фільтр по sitelinks, щоб відсіяти дрібні клуби."""
    # Тільки Big-5 + Португалія, Нідерланди, Україна, Бразилія
    major = {
        "Q9448": "Premier League",
        "Q15804": "Serie A",
        "Q324867": "La Liga",
        "Q82595": "Bundesliga",
        "Q13394": "Ligue 1",
        "Q167541": "Eredivisie",
        "Q219967": "Primeira Liga",
        "Q182994": "Ukrainian Premier League",
        "Q831202": "Campeonato Brasileiro Série A",
    }
    league_values = " ".join(f"wd:{qid}" for qid in major)
    query = f"""
    SELECT DISTINCT ?club ?clubLabel ?year ?sl WHERE {{
      VALUES ?league {{ {league_values} }}
      ?season wdt:P3450 ?league .
      ?season wdt:P1346 ?club .
      ?season wdt:P585 ?date .
      ?club wikibase:sitelinks ?sl .
      FILTER(?sl >= 25)
      BIND(YEAR(?date) AS ?year)
      FILTER(?year >= 1990 && ?year <= 2026)
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    ORDER BY DESC(?sl) ?year
    LIMIT 300
    """
    try:
        rows = run_sparql(query, timeout=120)
    except Exception as e:
        print(f"  League champions query failed: {e}")
        return []
    out = []
    for b in rows:
        qid = b["club"]["value"].rsplit("/", 1)[-1]
        name = b.get("clubLabel", {}).get("value", "")
        year = int(b["year"]["value"])
        if name and not name.startswith("Q"):
            out.append({"qid": qid, "name": name, "year": year, "source": "league"})
    return out


def build_club_pool() -> list[dict]:
    """
    Пул:
    1. Переможці ЛЧ + Ліги Європи з 1990 (основа)
    2. Чемпіони Big-5 + Португалія/Нідерланди/Україна/Бразилія (з фільтром sitelinks)
    Без національних кубків — вони тягнуть дрібні клуби (Shelbourne тощо).
    Перемішування peak-era ↔ modern grands.
    """
    print("  Fetching UCL + UEL winners...")
    euro = fetch_ucl_uel()

    print("  Fetching major league champions (sitelinks >= 25)...")
    champs = fetch_major_league_champions()
    print(f"    League titles: {len(champs)}")

    by_club: dict[str, dict] = {}
    for item in euro + champs:
        qid = item["qid"]
        if qid not in by_club:
            by_club[qid] = {
                "qid": qid,
                "name": item["name"],
                "years": [],
                "ucl": 0,
                "uel": 0,
                "titles": 0,
                "cups": 0,
            }
        c = by_club[qid]
        year = item["year"]
        c["years"].append(year)
        src = item["source"]
        if src == "UCL":
            c["ucl"] += 1
        elif src == "UEL":
            c["uel"] += 1
        else:
            c["titles"] += 1

    clubs = list(by_club.values())

    # Відсіюємо зовсім слабкі (0 євро + 1 титул і назва з дрібних ліг)
    # Залишаємо всіх з UCL/UEL або з >= 2 титулів чемпіонату
    clubs = [c for c in clubs if c["ucl"] + c["uel"] >= 1 or c["titles"] >= 2]

    def is_peak(c):
        return any(1990 <= y <= 2008 for y in c["years"])

    def is_modern_grand(c):
        return any(y >= 2013 for y in c["years"]) and (c["ucl"] + c["titles"] >= 2)

    peak_clubs = [c for c in clubs if is_peak(c)]
    modern_clubs = [c for c in clubs if is_modern_grand(c) and c not in peak_clubs]
    other = [c for c in clubs if c not in peak_clubs and c not in modern_clubs]

    def weight(c):
        return -(c["ucl"] * 5 + c["uel"] * 3 + c["titles"] * 2)

    peak_clubs.sort(key=weight)
    modern_clubs.sort(key=weight)
    other.sort(key=weight)

    mixed = []
    i = j = 0
    while i < len(peak_clubs) or j < len(modern_clubs):
        if i < len(peak_clubs):
            mixed.append(peak_clubs[i])
            i += 1
        if j < len(modern_clubs):
            mixed.append(modern_clubs[j])
            j += 1
    mixed.extend(other)

    print(f"  Total unique clubs in pool: {len(mixed)}")
    print(f"  Peak-era: {len(peak_clubs)}, Modern grands: {len(modern_clubs)}, Other: {len(other)}")
    print("  First 12 (mixed):")
    for idx, c in enumerate(mixed[:12]):
        tag = "peak" if is_peak(c) else ("grand" if is_modern_grand(c) else "other")
        print(f"    {idx+1}. {c['name']} [{tag}] UCL:{c['ucl']} UEL:{c['uel']} titles:{c['titles']}")
    return mixed


def get_en_wiki_title(qid: str) -> str | None:
    query = f"""
    SELECT ?title WHERE {{
      ?sitelink schema:about wd:{qid} ;
                schema:isPartOf <https://en.wikipedia.org/> ;
                schema:name ?title .
    }} LIMIT 1
    """
    try:
        rows = run_sparql(query, timeout=30)
        if rows:
            return rows[0]["title"]["value"].replace(" ", "_")
    except Exception:
        pass
    return None


def load_state():
    state = {
        "published": [],
        "last_date": "",
        "done": False,
        "messages": [],
        "current_qid": "",
        "current_title": "",
        "pool_cache": [],
        "pool_updated": "",
    }
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state["published"] = list(dict.fromkeys(state.get("published", [])))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def choose_next_club(state) -> dict | None:
    published = set(state.get("published", []))
    today = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()

    need_refresh = (
        not state.get("pool_cache")
        or state.get("pool_updated", "") < today[:8]
    )
    if need_refresh:
        try:
            pool = build_club_pool()
            state["pool_cache"] = pool
            state["pool_updated"] = today
            save_state(state)
        except Exception as e:
            print(f"  Failed to build pool: {e}")
            pool = state.get("pool_cache", [])
    else:
        pool = state.get("pool_cache", [])

    for club in pool:
        if club["qid"] not in published:
            title = get_en_wiki_title(club["qid"])
            if not title:
                title = club["name"].replace(" ", "_")
            club["wiki"] = title
            club["ua_title"] = club["name"]
            return club

    print("Great Clubs: усі клуби з пулу вже опубліковані.")
    return None


def fetch_wikipedia(title: str) -> str:
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "extracts",
        "explaintext": True,
        "exsectionformat": "plain",
    }
    r = requests.get(url, params=params, headers=WIKI_HEADERS, timeout=60)
    r.raise_for_status()
    pages = r.json()["query"]["pages"]
    page = next(iter(pages.values()))
    extract = page.get("extract", "")
    if not extract or "missing" in page:
        raise ValueError(f"Wikipedia page not found: {title}")
    return extract


def format_with_ai(source_text: str, ua_title: str, run_start: float) -> str | None:
    prompt = f"""
Ти — головний редактор українського футбольного Telegram-каналу «football 90+».
Напиши ДОВГИЙ розгорнутий матеріал українською мовою про футбольний клуб {ua_title}.

=== ТОЧНІСТЬ ПЕРЕКАЗУ (критично) ===
1. Переказ має покривати ПРИБЛИЗНО 95% фактів з джерела. Не скорочуй історію до «вижимки».
2. Йди по статті послідовно: майже кожне змістовне речення джерела має бути відображене своїми словами (не копіюй дослівно, але не пропускай факт).
3. Якщо в джерелі згадані гравці або тренери — ОБОВ’ЯЗКОВО вкажи їх:
   - ім’я ЛАТИНКОЮ (як у джерелі)
   - роки (прихід / відхід / період, якщо є)
   - досягнення з ними (титули, голи, роль) — ТІЛЬКИ якщо це є в тексті
4. Не вигадуй біографій гравців і тренерів. Якщо в статті лише ім’я без деталей — згадай ім’я в контексті, не додумуй.

=== ДАТИ І ТРОФЕЇ ===
5. Усі важливі дати з джерела мають потрапити в текст:
   - заснування, стадіони
   - кожен чемпіонський титул (рік)
   - кожен національний кубок (рік)
   - кожен єврокубок / фінал (Ліга чемпіонів, Кубок УЄФА / Ліга Європи, Кубок володарів кубків, Суперкубок, Клубний ЧС тощо)
6. На кожну таку дату — мінімум 2–3 речення: що сталося, з ким, рахунок або значення (якщо є в джерелі).

=== ОБСЯГ І СТИЛЬ ===
7. Орієнтир 2000–3000 слів. Краще довше і повніше, ніж коротко з пропусками. Бот розіб’є на кілька повідомлень Telegram.
8. Структура (зберігай хронологію джерела):
   - Вступ
   - Заснування і ранні роки
   - Подальші епохи з датами, гравцями, тренерами, трофеями
   - Стадіон і культура (якщо є)
   - Суперництва
   - Сучасність
   - Висновок
9. Іноземні назви клубів, стадіонів, імена — ЛАТИНКОЮ.
10. Без емодзі, Markdown, посилань, оцінок яких немає в джерелі.
11. Відповідай ЛИШЕ текстом статті. Без «Ось матеріал» і без JSON.

Джерело (англійська Вікіпедія):
{source_text[:18000]}
"""
    res = quiz.ai_call(prompt, run_start)
    if res is None:
        return None
    raw, _ = res
    text_out = quiz.clean(raw)
    word_count = len(text_out.split())
    if word_count < 900:
        print(f"  AI text too short: {word_count} words")
        return None
    print(f"  AI text ready: {word_count} words")
    return text_out


def build_messages(text: str, ua_title: str) -> list[str]:
    """Розбиває довгий текст на повідомлення ≤ TG_LIMIT символів.
    Якщо один абзац довший за ліміт — ріже по реченнях / жорстко по символах.
    """
    head = f"🏟️ <b>Великі клуби</b>\n📅 {ua_title}\n\n"
    cont = f"🏟️ <b>Великі клуби (продовження)</b>\n📅 {ua_title}\n\n"
    max_body = TG_LIMIT - max(len(head), len(cont)) - 20

    def split_long(chunk: str) -> list[str]:
        """Ріже надто довгий фрагмент по реченнях, потім по символах."""
        if len(chunk) <= max_body:
            return [chunk]
        parts = []
        # спочатку по реченнях
        sentences = re.split(r"(?<=[.!?…])\s+", chunk)
        buf = ""
        for s in sentences:
            if len(s) > max_body:
                if buf:
                    parts.append(buf.strip())
                    buf = ""
                # жорстко по символах
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

    raw_paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    pieces = []
    for para in raw_paragraphs:
        pieces.extend(split_long(html.escape(para)))

    messages = []
    current = head
    for piece in pieces:
        add = piece + "\n\n"
        if len(current) + len(add) > TG_LIMIT and current not in (head, cont):
            messages.append(current.rstrip())
            current = cont
        # якщо навіть з cont не влазить (дуже довгий piece) — окреме повідомлення
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


def prepare(run_start: float) -> tuple:
    state = load_state()
    club = choose_next_club(state)
    if not club:
        return None, "", ""

    print(f"Great Clubs: preparing {club['name']} ({club['wiki']})")
    try:
        source = fetch_wikipedia(club["wiki"])
        if len(source) < 800:
            print("  Wikipedia text too short, marking as published to skip")
            state["published"].append(club["qid"])
            save_state(state)
            return None, "", ""
    except Exception as e:
        print(f"  Wikipedia fetch failed: {e}")
        return None, "", ""

    ua_title = club["name"]
    text = format_with_ai(source, ua_title, run_start)
    if not text:
        return None, "", ""

    messages = build_messages(text, ua_title)
    return messages, club["qid"], ua_title


def send_message(text: str):
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
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


def publish(messages: list[str]):
    for i, m in enumerate(messages):
        send_message(m)
        if i < len(messages) - 1:
            time.sleep(2)


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    run_start = time.time()
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today = now.date().isoformat()

    if os.environ.get("CLUBS_FORCE", "").lower() == "true":
        # Тест: публікуємо, але НЕ записуємо в published / last_date
        messages, qid, title = prepare(run_start)
        if messages:
            publish(messages)
            print(f"Great Clubs TEST published ({len(messages)} msgs). "
                  f"State NOT updated (qid={qid} will be available for real run).")
        else:
            print("Great Clubs test: failed to prepare.")
        return

    if now.weekday() != 4:
        return

    state = load_state()
    if state.get("last_date") == today and state.get("done"):
        return
    if pn.in_quiet_hours():
        return

    before = now.hour * 60 + now.minute < pn.to_minutes(PUBLISH_TIME)

    if before:
        if not state.get("messages"):
            messages, qid, title = prepare(run_start)
            state["messages"] = messages or []
            state["current_qid"] = qid
            state["current_title"] = title
            save_state(state)
        return

    messages = state.get("messages")
    qid = state.get("current_qid", "")
    if not messages:
        messages, qid, title = prepare(run_start)

    if not messages:
        print("Great Clubs: nothing to publish")
        return

    publish(messages)

    if qid and qid not in state["published"]:
        state["published"].append(qid)
    state["last_date"] = today
    state["done"] = True
    state["messages"] = []
    state["current_qid"] = ""
    state["current_title"] = ""
    save_state(state)
    print(f"Great Clubs published ({len(messages)} msgs). Total published clubs: {len(state['published'])}")


if __name__ == "__main__":
    main()
