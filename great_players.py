"""
Рубрика «Великі футболісти»
Публікація: щопонеділка о 13:00 за Києвом.

Джерела пулу (автоматично):
  1. Переможці Ballon d\'Or (Wikidata Q166177) з 1956
  2. Номінанти (P1411) з Wikidata
  3. Топ-5 за роками з RSSSF europa-poy.html

Бот не повторює published; тест PLAYERS_FORCE не пише в лог.
Переказ ~95% фактів, 2000–3000 слів, жорстке розбиття Telegram.
"""
import html
import json
import os
import re
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
STATE_FILE = Path("great_players_state.json")
TG_LIMIT = 3900
BALLON_DOR = "Q166177"
RSSSF_URL = "https://www.rsssf.org/miscellaneous/europa-poy.html"

WIKI_HEADERS = {
    "User-Agent": "football90-great-players/1.0 (Telegram @football_90_pluss)",
    "Accept": "application/json",
}
HTTP_HEADERS = {
    "User-Agent": "football90-great-players/1.0 (Telegram @football_90_pluss)",
}


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
            time.sleep(5 * (attempt + 1))
    raise last_error


def fetch_ballon_dor_winners() -> list:
    query = """
    SELECT DISTINCT ?person ?personLabel ?year ?sl WHERE {
      ?person wdt:P166 wd:Q166177 .
      OPTIONAL {
        ?person p:P166 ?st .
        ?st ps:P166 wd:Q166177 .
        OPTIONAL { ?st pq:P585 ?date . BIND(YEAR(?date) AS ?year) }
      }
      ?person wikibase:sitelinks ?sl .
      FILTER(?sl >= 20)
      ?person wdt:P21 wd:Q6581097 .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    ORDER BY DESC(?sl)
    """
    try:
        rows = run_sparql(query, timeout=120)
    except Exception as e:
        print(f"  Ballon d'Or winners query failed: {e}")
        return []
    out, seen = [], set()
    for b in rows:
        qid = b["person"]["value"].rsplit("/", 1)[-1]
        if qid in seen:
            continue
        seen.add(qid)
        name = b.get("personLabel", {}).get("value", "")
        if not name or name.startswith("Q"):
            continue
        year = int(b["year"]["value"]) if "year" in b else None
        out.append({
            "qid": qid, "name": name, "source": "ballon_winner",
            "years": [year] if year else [], "score": int(b.get("sl", {}).get("value", 0)),
        })
    print(f"  Ballon d'Or winners: {len(out)}")
    return out


def fetch_ballon_dor_nominees_wikidata() -> list:
    query = """
    SELECT DISTINCT ?person ?personLabel ?sl WHERE {
      ?person wdt:P1411 wd:Q166177 .
      ?person wikibase:sitelinks ?sl .
      FILTER(?sl >= 15)
      ?person wdt:P21 wd:Q6581097 .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    ORDER BY DESC(?sl)
    LIMIT 400
    """
    try:
        rows = run_sparql(query, timeout=90)
    except Exception as e:
        print(f"  Ballon nominees (WD) failed: {e}")
        return []
    out, seen = [], set()
    for b in rows:
        qid = b["person"]["value"].rsplit("/", 1)[-1]
        if qid in seen:
            continue
        seen.add(qid)
        name = b.get("personLabel", {}).get("value", "")
        if not name or name.startswith("Q"):
            continue
        out.append({
            "qid": qid, "name": name, "source": "ballon_nominee",
            "years": [], "score": int(b.get("sl", {}).get("value", 0)),
        })
    print(f"  Ballon d'Or nominees (WD): {len(out)}")
    return out


def fetch_rsssf_top5_names() -> list:
    try:
        r = requests.get(RSSSF_URL, headers=HTTP_HEADERS, timeout=45)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        print(f"  RSSSF fetch failed: {e}")
        return []
    names = []
    for m in re.finditer(r"^\s*\d+\.\s+([A-Za-zÀ-ÿ\.\-\'\s]+?)\s{2,}\(", text, re.M):
        raw = m.group(1).strip()
        parts = raw.split()
        fixed = [p.title() if p.isupper() and len(p) > 1 else p for p in parts]
        name = " ".join(fixed)
        if len(name) > 3:
            names.append(name)
    seen, uniq = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(n)
    print(f"  RSSSF top-5 names parsed: {len(uniq)}")
    return uniq


def resolve_via_wbsearch(name: str):
    try:
        r = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities", "search": name, "language": "en",
                "type": "item", "limit": 5, "format": "json",
            },
            headers=WIKI_HEADERS, timeout=20,
        )
        r.raise_for_status()
        for hit in r.json().get("search", []):
            desc = (hit.get("description") or "").lower()
            if "footballer" in desc or "football player" in desc or "soccer" in desc:
                return {
                    "qid": hit["id"], "name": hit.get("label", name),
                    "source": "rsssf_search", "years": [], "score": 0,
                }
    except Exception:
        pass
    return None


def resolve_name_to_wikidata(name: str):
    safe = name.replace('"', "")
    query = f"""
    SELECT ?person ?personLabel ?sl WHERE {{
      ?person wdt:P106 wd:Q937857 .
      ?person wdt:P21 wd:Q6581097 .
      ?person rdfs:label "{safe}"@en .
      ?person wikibase:sitelinks ?sl .
      FILTER(?sl >= 10)
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    ORDER BY DESC(?sl) LIMIT 1
    """
    try:
        rows = run_sparql(query, timeout=30)
        if not rows:
            return resolve_via_wbsearch(name)
        b = rows[0]
        return {
            "qid": b["person"]["value"].rsplit("/", 1)[-1],
            "name": b.get("personLabel", {}).get("value", name),
            "source": "rsssf", "years": [],
            "score": int(b.get("sl", {}).get("value", 0)),
        }
    except Exception:
        return resolve_via_wbsearch(name)


def get_en_wiki_title(qid: str):
    query = f"""
    SELECT ?title WHERE {{
      ?sitelink schema:about wd:{qid} ;
                schema:isPartOf <https://en.wikipedia.org/> ;
                schema:name ?title .
    }} LIMIT 1
    """
    try:
        rows = run_sparql(query, timeout=25)
        if rows:
            return rows[0]["title"]["value"].replace(" ", "_")
    except Exception:
        pass
    return None


def build_player_pool() -> list:
    winners = fetch_ballon_dor_winners()
    nominees_wd = fetch_ballon_dor_nominees_wikidata()
    by_qid = {}
    for p in winners + nominees_wd:
        qid = p["qid"]
        if qid not in by_qid:
            by_qid[qid] = p
        else:
            old = by_qid[qid]
            old["years"] = list(set(old.get("years", []) + p.get("years", [])))
            old["score"] = max(old.get("score", 0), p.get("score", 0))
            if p["source"] == "ballon_winner":
                old["source"] = "ballon_winner"

    rsssf_names = fetch_rsssf_top5_names()
    resolved = 0
    for name in rsssf_names[:120]:
        if resolved >= 80:
            break
        hit = resolve_name_to_wikidata(name)
        if not hit:
            continue
        if hit["qid"] not in by_qid:
            by_qid[hit["qid"]] = hit
            resolved += 1
        time.sleep(0.15)
    print(f"  RSSSF resolved new: {resolved}")

    pool = list(by_qid.values())
    pool.sort(key=lambda p: (0 if p.get("source") == "ballon_winner" else 1, -p.get("score", 0), p.get("name", "")))
    print(f"  Total unique players in pool: {len(pool)}")
    for i, p in enumerate(pool[:15]):
        print(f"    {i+1}. {p['name']} [{p.get('source')}] sl={p.get('score')}")
    return pool


def load_state():
    state = {
        "published": [], "last_date": "", "done": False, "messages": [],
        "current_qid": "", "current_title": "", "pool_cache": [], "pool_updated": "",
    }
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state["published"] = list(dict.fromkeys(state.get("published", [])))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def choose_next_player(state):
    published = set(state.get("published", []))
    today = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()
    need_refresh = not state.get("pool_cache") or state.get("pool_updated", "") < today[:8]
    if need_refresh:
        try:
            pool = build_player_pool()
            state["pool_cache"] = pool
            state["pool_updated"] = today
            save_state(state)
        except Exception as e:
            print(f"  Failed to build player pool: {e}")
            pool = state.get("pool_cache", [])
    else:
        pool = state.get("pool_cache", [])
    for p in pool:
        if p["qid"] not in published:
            title = get_en_wiki_title(p["qid"]) or p["name"].replace(" ", "_")
            p = dict(p)
            p["wiki"] = title
            p["ua_title"] = p["name"]
            return p
    print("Great Players: усі з пулу вже опубліковані.")
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
        raise ValueError(f"Wikipedia page not found: {title}")
    return extract


def format_with_ai(source_text: str, ua_title: str, run_start: float):
    prompt = f"""
Ти — головний редактор українського футбольного Telegram-каналу «football 90+».
Напиши ДОВГИЙ розгорнутий матеріал українською мовою про футболіста {ua_title}.

=== ТОЧНІСТЬ ПЕРЕКАЗУ (критично) ===
1. Переказ має покривати ПРИБЛИЗНО 95% фактів з джерела. Не скорочуй до вижимки.
2. Йди по статті послідовно: майже кожне змістовне речення відображай своїми словами.
3. Усі згадані клуби, тренери, партнери — з роками і досягненнями, ЯКЩО вони є в тексті. Не вигадуй.

=== ДАТИ І ТРОФЕЇ ===
4. Усі важливі дати: народження, дебют, трансфери, титули, Ballon d\'Or, збірна.
5. На ключові дати — 2–3 речення (лише з даних).

=== ОБСЯГ ===
6. Орієнтир 2000–3000 слів. Бот розіб\'є на кілька повідомлень.
7. Іноземні імена і клуби — ЛАТИНКОЮ.
8. Без емодзі, Markdown, посилань.
9. Відповідай ЛИШЕ текстом статті.

Джерело (англійська Вікіпедія):
{source_text[:18000]}
"""
    res = quiz.ai_call(prompt, run_start)
    if res is None:
        return None
    raw, _ = res
    text_out = quiz.clean(raw)
    wc = len(text_out.split())
    if wc < 1400:
        print(f"  AI text too short: {wc} words")
        return None
    print(f"  AI text ready: {wc} words")
    return text_out


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
    print(f"Great Players: preparing {player['name']} ({player.get('wiki')})")
    try:
        source = fetch_wikipedia(player["wiki"])
        if len(source) < 500:
            print("  Wikipedia too short, skip & mark published")
            state["published"].append(player["qid"])
            save_state(state)
            return None, "", ""
    except Exception as e:
        print(f"  Wikipedia fetch failed: {e}")
        return None, "", ""
    text = format_with_ai(source, player["name"], run_start)
    if not text:
        return None, "", ""
    return build_messages(text, player["name"]), player["qid"], player["name"]


def send_message(text: str):
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    r = requests.post(
        f"{api}/sendMessage",
        data={"chat_id": CHANNEL, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=60,
    )
    if not r.ok:
        print("  Telegram error:", r.text[:300])
        r.raise_for_status()


def publish(messages: list):
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

    if os.environ.get("PLAYERS_FORCE", "").lower() == "true":
        messages, qid, title = prepare(run_start)
        if messages:
            publish(messages)
            print(f"Great Players TEST published ({len(messages)} msgs). State NOT updated (qid={qid}).")
        else:
            print("Great Players test: failed to prepare.")
        return

    if now.weekday() != 0:
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
        print("Great Players: nothing to publish")
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
    print(f"Great Players published ({len(messages)} msgs). Total published: {len(state['published'])}")


if __name__ == "__main__":
    main()
