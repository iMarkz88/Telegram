"""Daily post "Цього дня в історії футболу", built from REAL data.

Facts come from Wikidata (footballers' and managers' births/deaths) and from Wikipedia's
"On this day" feed (football events). The AI only translates and shortens the supplied
facts; code checks that it did not add a single number. The post is prepared before
DIGEST_TIME and published right after it (Kyiv time); long posts are split in two messages.
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
DIGEST_TIME = os.environ.get("DIGEST_TIME", "09:00")             # Kyiv time
DIGEST_ITEMS = int(os.environ.get("DIGEST_ITEMS", "12"))         # about this many facts
DIGEST_MIN_ITEMS = int(os.environ.get("DIGEST_MIN_ITEMS", "5"))  # fewer facts -> do not publish
MAX_ATTEMPTS = 8            # after DIGEST_TIME: how many runs may try before the day is skipped
TG_LIMIT = 3900             # Telegram allows 4096 characters per message
STATE_FILE = Path("digest_state.json")

WIKI_HEADERS = {
    "User-Agent": "football90-digest-bot/1.0 (Telegram channel @football_90_pluss; "
                  "GitHub Actions) python-requests",
    "Accept": "application/json",
}
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня",
          "вересня", "жовтня", "листопада", "грудня"]

FOOT_YES = re.compile(r"football\w*|soccer|FIFA|UEFA|Premier League|Champions League|"
                      r"Europa League|Serie A|La Liga|Bundesliga|Ligue 1|FA Cup|Ballon d.Or", re.I)
FOOT_NO = re.compile(r"American football|NFL|Super Bowl|Gaelic|Australian rules|rugby|cricket|"
                     r"Canadian football|college football|gridiron|\bAFL\b", re.I)


# ------------------------------------------------------------------ sources

def run_sparql(query):
    r = requests.get("https://query.wikidata.org/sparql",
                     params={"query": query, "format": "json"},
                     headers={**WIKI_HEADERS, "Accept": "application/sparql-results+json"},
                     timeout=70)
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def people_query(kind, month, day, ukrainian):
    prop = "P569" if kind == "birth" else "P570"
    country = ("{ ?p wdt:P27 wd:Q212 } UNION { ?p wdt:P19 ?bp . ?bp wdt:P17 wd:Q212 }"
               if ukrainian else "")
    langs = "uk,en" if ukrainian else "en,mul"  # Для украинцев — украинские имена, для иностранцев — латиница
    min_links = 5 if ukrainian else 25
    return f"""
SELECT ?p ?pLabel ?pDescription ?date ?sl WHERE {{
  VALUES ?occ {{ wd:Q937857 wd:Q628099 }}
  ?p wdt:P106 ?occ .
  ?p wikibase:sitelinks ?sl .
  FILTER(?sl >= {min_links})
  {country}
  ?p p:{prop} ?st .
  ?st psv:{prop} ?node .
  ?node wikibase:timeValue ?date ; wikibase:timePrecision 11 .
  FILTER(MONTH(?date) = {month} && DAY(?date) = {day} && YEAR(?date) >= 1860)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{langs}". }}
}}
ORDER BY DESC(?sl)
LIMIT 12"""


def parse_people(bindings, kind, ukrainian):
    out = []
    for b in bindings:
        try:
            name = b.get("pLabel", {}).get("value", "")
            if not name or re.fullmatch(r"Q\d+", name):
                continue
            out.append({
                "kind": kind, "year": int(b["date"]["value"][:4]),
                "qid": b["p"]["value"].rsplit("/", 1)[-1], "name": name,
                "desc": b.get("pDescription", {}).get("value", ""),
                "score": int(b["sl"]["value"]), "ua": ukrainian, "text": "",
            })
        except (KeyError, ValueError):
            continue
    return out


def wikidata_people(month, day):
    items = []
    for kind in ("birth", "death"):
        for ukrainian in (False, True):
            try:
                items += parse_people(run_sparql(people_query(kind, month, day, ukrainian)),
                                      kind, ukrainian)
            except Exception as e:
                print(f"  Wikidata ({kind}, {'UA' if ukrainian else 'world'}) failed: {e}")
    return items


def wikipedia_feed(api_kind, month, day):
    """api_kind: events | births | deaths. Keeps only football-related entries."""
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/{api_kind}/{month:02d}/{day:02d}"
    try:
        r = requests.get(url, headers=WIKI_HEADERS, timeout=30)
        r.raise_for_status()
        entries = r.json().get(api_kind, [])
    except Exception as e:
        print(f"  Wikipedia feed ({api_kind}) failed: {e}")
        return []
    kind = {"events": "event", "births": "birth", "deaths": "death"}[api_kind]
    out = []
    for e in entries:
        text, year = e.get("text", ""), e.get("year")
        if not text or not isinstance(year, int) or year < 1860:
            continue
        if not FOOT_YES.search(text) or FOOT_NO.search(text):
            continue
        qid = next((p.get("wikibase_item", "") for p in e.get("pages", [])
                    if p.get("wikibase_item")), "") if kind != "event" else ""
        name, desc, ua = "", "", False
        if kind != "event":  # entries look like "Zvonimir Boban, Croatian footballer"
            name, _, desc = (p.strip() for p in text.partition(","))
            ua = bool(re.search(r"Ukrain", text))
        out.append({"kind": kind, "year": year, "qid": qid, "name": name, "desc": desc,
                    "score": 0, "ua": ua, "text": text if kind == "event" else ""})
    return out


def collect(month, day):
    people = wikidata_people(month, day)
    best = {}
    for it in people:  # one entry per person, Ukrainian version (Ukrainian spelling) wins
        old = best.get(it["qid"])
        if old is None or (it["ua"] and not old["ua"]):
            best[it["qid"]] = it
    items = list(best.values())
    known = {i["qid"] for i in items if i["qid"]}
    for api_kind in ("events", "births", "deaths"):
        for it in wikipedia_feed(api_kind, month, day):
            if it["qid"] and it["qid"] in known:
                continue  # the same person is already in the list
            items.append(it)
    return items


# ------------------------------------------------------------------ selection

def select(items, total):
    """Mix of events, births and deaths; Ukrainians are included first; sorted by year."""
    items = list(items)
    chosen = []

    def take(pool, limit):
        for it in pool:
            if len(chosen) >= total or limit <= 0:
                return
            if it not in chosen:
                chosen.append(it)
                limit -= 1

    by_score = lambda xs: sorted(xs, key=lambda i: -i["score"])
    people = [i for i in items if i["kind"] in ("birth", "death")]
    take(by_score([i for i in people if i["ua"]]), 2)
    take([i for i in items if i["kind"] == "event"], 5)
    take(by_score([i for i in people if i["kind"] == "birth"]), 5 - sum(c["kind"] == "birth" for c in chosen) + 0)
    take(by_score([i for i in people if i["kind"] == "death"]), 3 - sum(c["kind"] == "death" for c in chosen))
    take(by_score([i for i in items if i not in chosen]), total)  # fill the rest
    return sorted(chosen, key=lambda i: i["year"])


# ------------------------------------------------------------------ AI formatting (checked by code)

def numbers(s):
    return set(re.findall(r"\d+", s))


def line_ok(item, text):
    src = numbers(f"{item['year']} {item['text']} {item['desc']} {item['name']}")
    if not (12 <= len(text) <= 330) or not numbers(text) <= src:
        return False  # the AI must not introduce any number that is not in the source
    low = text.lower()
    if item["kind"] == "birth" and "народив" not in low:
        return False
    if item["kind"] == "death" and not ("помер" in low or "загин" in low):
        return False
    if quiz.has_foreign_cyrillic(text):
        return False  # a foreign club or famous person written in Cyrillic
    name = item["name"]
    if item["kind"] in ("birth", "death") and name:
        latin = re.search(r"[A-Za-z]", name) is not None
        if not item["ua"] and latin and name not in text:
            return False  # foreign people must keep their international (Latin) name
        if item["ua"] and not latin and name not in text:
            return False  # Ukrainians: the Ukrainian name exactly as in the source
    return True


def format_with_ai(items, run_start):
    payload = [{"id": i, "type": it["kind"], "year": it["year"], "name": it["name"],
                "description": it["desc"], "text": it["text"], "ukrainian": it["ua"]}
               for i, it in enumerate(items)]
    prompt = (
        "Ти редактор українського футбольного Telegram-каналу. Нижче перевірені факти з "
        "Вікіпедії та Вікіданих. Для кожного напиши ОДИН короткий рядок українською, БЕЗ року на початку.\n"
        "Правила:\n"
        "- використовуй ТІЛЬКИ дані з наведеного запису; нічого не додавай і не вигадуй; "
        "не змінюй і не додавай жодних чисел, рахунків, назв, дат;\n"
        "- для type=birth почни з «народився» («народилася»), для type=death з «помер» («померла»), "
        "далі ім'я та коротко хто це за описом (національність, футболіст/тренер); "
        "для type=event коротко перекажи подію;\n"
        "- ІМЕНА, ПРІЗВИЩА та НАЗВИ КЛУБІВ: іноземних людей та іноземні клуби залишай ЛАТИНКОЮ "
        "точно так, як у вхідних даних, і НЕ перекладай та НЕ транслітеруй їх кирилицею "
        "(Zvonimir Boban, AC Milan, Real Madrid). Українською пиши лише назви українських клубів "
        "(Динамо Київ, Шахтар, Дніпро, Зоря, Металіст, Карпати, Чорноморець, Ворскла, Кривбас, "
        "Олександрія, Колос, Рух, Верес, Полісся) та імена людей, у яких ukrainian=true "
        "(використай name точно як у вхідних даних); назви країн і змагань пиши українською;\n"
        "- без оцінок, епітетів, емодзі, Markdown і посилань.\n"
        'Відповідай ЛИШЕ JSON: {"lines": [{"id": 0, "text": "..."}, ...]} (id з вхідних даних).\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    res = quiz.ai_call(prompt, run_start)
    if res is None:
        return None
    raw, _engine = res
    try:
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        texts = {int(x["id"]): quiz.clean(x["text"]) for x in data["lines"]}
    except Exception as e:
        print("  AI answer is not usable:", e)
        return []
    lines = []
    for i, it in enumerate(items):
        if i in texts and line_ok(it, texts[i]):
            lines.append(f"{it['year']} — {texts[i]}")
        else:
            print(f"  fact dropped by the safety check: {it['year']} {it['name'] or it['text'][:50]}")
    return lines


# ------------------------------------------------------------------ message building

def build_messages(lines, day, month):
    head = "<b>Цього дня в історії футболу</b>\n\n"
    cont = "<b>Цього дня в історії футболу (продовження)</b>\n\n"
    messages, current = [], head
    for ln in lines:
        ln = html.escape(ln)
        if len(current) + len(ln) + 2 > TG_LIMIT and current not in (head, cont):
            messages.append(current.rstrip())
            current = cont
        current += ln + "\n\n"
    messages.append(current.rstrip())
    return messages


def prepare(now, run_start):
    """Returns a list of ready messages, or [] when the facts are not good enough yet."""
    month, day = now.month, now.day
    items = collect(month, day)
    print(f"Digest: {len(items)} candidate fact(s) found for {day:02d}.{month:02d}")
    if len(items) < DIGEST_MIN_ITEMS:
        return []
    chosen = select(items, DIGEST_ITEMS)
    lines = format_with_ai(chosen, run_start)
    if not lines or len(lines) < DIGEST_MIN_ITEMS:
        print(f"Digest: only {len(lines or [])} verified line(s), minimum is {DIGEST_MIN_ITEMS}.")
        return []
    return build_messages(lines, day, month)


# ------------------------------------------------------------------ publishing & state

def send_message(text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data={"chat_id": CHANNEL, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=60,
    )
    if not r.ok:
        print("  Telegram refused the message:", r.text[:300])
        r.raise_for_status()


def publish(messages):
    for n, m in enumerate(messages):
        send_message(m)
        if n < len(messages) - 1:
            time.sleep(2)


def load_state():
    state = {"day": "", "messages": [], "done": False, "attempts": 0, "last_try": 0}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    run_start = time.time()
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today = now.date().isoformat()

    if os.environ.get("DIGEST_FORCE", "").lower() == "true":  # manual test: publish now
        messages = prepare(now, run_start)
        if messages:
            publish(messages)
            print(f"Digest test published ({len(messages)} message(s)).")
        else:
            print("Digest test: not enough verified facts.")
        return

    state = load_state()
    if state["day"] != today:
        state = {"day": today, "messages": [], "done": False, "attempts": 0, "last_try": 0}
    if state["done"] or pn.in_quiet_hours():
        save_state(state)
        return

    before_time = now.hour * 60 + now.minute < pn.to_minutes(DIGEST_TIME)
    if not state["messages"]:
        if before_time and time.time() - state["last_try"] < 600:
            save_state(state)  # prepare calmly: one try per 10 minutes
            return
        state["last_try"] = time.time()
        if not before_time:
            state["attempts"] += 1
        state["messages"] = prepare(now, run_start)
        print("Digest: prepared." if state["messages"] else "Digest: not ready yet.")
        if not state["messages"] and not before_time and state["attempts"] >= MAX_ATTEMPTS:
            state["done"] = True
            print("Digest: too many failed attempts, skipping today.")
        save_state(state)
        if before_time or not state["messages"]:
            return
    elif before_time:
        print("Digest: ready, waiting for", DIGEST_TIME)
        save_state(state)
        return

    publish(state["messages"])
    state["done"] = True
    save_state(state)
    print(f"Digest published ({len(state['messages'])} message(s)).")


if __name__ == "__main__":
    main()
