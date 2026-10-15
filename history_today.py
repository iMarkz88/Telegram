"""Daily post "Цього дня в історії футболу", built from REAL data.

Facts come from Wikidata (footballers' and managers' births/deaths) and from Wikipedia's
"On this day" feed (football events). The AI only translates and shortens the supplied
facts; code checks that it did not add a single number. The post is prepared before
DIGEST_TIME and published right after it (Kyiv time); long posts are split in two messages.

Only men's football. No forced priority for Ukrainians.
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
DIGEST_MIN_ITEMS = int(os.environ.get("DIGEST_MIN_ITEMS", "3"))  # fewer facts -> do not publish
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

# Тільки чоловічий футбол
FOOT_YES = re.compile(
    r"football\w*|soccer|FIFA|UEFA|Premier League|Champions League|"
    r"Europa League|Serie A|La Liga|Bundesliga|Ligue 1|FA Cup|Ballon d.Or|"
    r"\bclub\b|\bmatch\b|\bstadium\b|\bplayer\b|\bgoal\b|\bteam\b|"
    r"\bleague\b|\bcup\b|\bfinal\b|\btournament\b|\bcoach\b|\breferee\b|"
    r"World Cup|European Championship|Olympic|Football Association|"
    r"footballer|manager|coach",
    re.I,
)

FOOT_NO = re.compile(
    r"American football|NFL|Super Bowl|Gaelic|Australian rules|rugby|cricket|"
    r"Canadian football|college football|gridiron|\bAFL\b|"
    r"basketball|NBA|WNBA|baseball|hockey|tennis|golf|volleyball|"
    r"women.?s football|women.?s soccer|female footballer|woman footballer|"
    r"ladies.? football|girls.? football|women.?s team|women.?s national|"
    r"women.?s world cup|women.?s champions|women.?s euro|"
    r"\bwomen\b.*\bfootball|\bwomen\b.*\bsoccer|"
    r"handball|water polo|futsal(?!.*men)|"
    r"OSCE|Helsinki Final Act|treaty|parliament|election|president of|"
    r"signed the|peace accord|diplomatic",
    re.I,
)


# ------------------------------------------------------------------ sources

def run_sparql(query):
    """Выполняет SPARQL-запрос с повторными попытками при таймаутах."""
    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(
                "https://query.wikidata.org/sparql",
                params={"query": query, "format": "json"},
                headers={**WIKI_HEADERS, "Accept": "application/sparql-results+json"},
                timeout=120,
            )
            r.raise_for_status()
            return r.json()["results"]["bindings"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            wait = 5 * (attempt + 1)
            print(f"  SPARQL timeout (попытка {attempt + 1}/3), ждём {wait} с...")
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in (502, 503, 504):
                last_error = e
                wait = 5 * (attempt + 1)
                print(f"  SPARQL {e.response.status_code} (попытка {attempt + 1}/3), ждём {wait} с...")
                time.sleep(wait)
            else:
                raise
    raise last_error


def people_query(kind, month, day, ukrainian=False):
    """Только мужчины. Украинцы — опционально, без приоритета."""
    prop = "P569" if kind == "birth" else "P570"
    country = (
        "{ ?p wdt:P27 wd:Q212 } UNION { ?p wdt:P19 ?bp . ?bp wdt:P17 wd:Q212 }"
        if ukrainian else ""
    )
    langs = "uk,en" if ukrainian else "en,mul"
    min_links = 8 if ukrainian else 20
    return f"""
SELECT ?p ?pLabel ?pDescription ?date ?sl WHERE {{
  VALUES ?occ {{ wd:Q937857 wd:Q628099 }}
  ?p wdt:P106 ?occ .
  ?p wdt:P21 wd:Q6581097 .          # только мужчины
  ?p wikibase:sitelinks ?sl .
  FILTER(?sl >= {min_links})
  {country}
  ?p p:{prop}/psv:{prop} ?node .
  ?node wikibase:timeValue ?date ; wikibase:timePrecision ?prec .
  FILTER(?prec = 11 && MONTH(?date) = {month} && DAY(?date) = {day} && YEAR(?date) >= 1860)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{langs}". }}
}}
ORDER BY DESC(?sl)
LIMIT 10"""


def parse_people(bindings, kind, ukrainian):
    out = []
    for b in bindings:
        try:
            name = b.get("pLabel", {}).get("value", "")
            if not name or re.fullmatch(r"Q\d+", name):
                continue
            desc = b.get("pDescription", {}).get("value", "")
            # дополнительная защита от женского футбола
            if re.search(r"women|female|lady|girl", desc, re.I):
                continue
            out.append({
                "kind": kind,
                "year": int(b["date"]["value"][:4]),
                "qid": b["p"]["value"].rsplit("/", 1)[-1],
                "name": name,
                "desc": desc,
                "score": int(b["sl"]["value"]),
                "ua": ukrainian,
                "text": "",
            })
        except (KeyError, ValueError):
            continue
    return out


def wikidata_people(month, day):
    items = []
    for kind in ("birth", "death"):
        # сначала мир, потом украинцы (без приоритета в отборе)
        for ukrainian in (False, True):
            try:
                items += parse_people(
                    run_sparql(people_query(kind, month, day, ukrainian)),
                    kind, ukrainian,
                )
            except Exception as e:
                print(f"  Wikidata ({kind}, {'UA' if ukrainian else 'world'}) failed: {e}")
    return items


def wikipedia_feed(api_kind, month, day):
    """api_kind: events | births | deaths. Только мужской футбол."""
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/{api_kind}/{month:02d}/{day:02d}"
    try:
        r = requests.get(url, headers=WIKI_HEADERS, timeout=30)
        r.raise_for_status()
        entries = r.json().get(api_kind, [])
        print(f"  [DEBUG] Wikipedia feed ({api_kind}): {len(entries)} raw entries")
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
        # дополнительный отсев женского футбола
        if re.search(r"\bwomen\b|\bfemale\b|\bladies\b|\bgirls\b", text, re.I):
            continue

        qid = ""
        if kind != "event":
            qid = next(
                (p.get("wikibase_item", "") for p in e.get("pages", []) if p.get("wikibase_item")),
                "",
            )
        name, desc, ua = "", "", False
        if kind != "event":
            name, _, desc = (p.strip() for p in text.partition(","))
            ua = bool(re.search(r"Ukrain", text, re.I))
        out.append({
            "kind": kind,
            "year": year,
            "qid": qid,
            "name": name,
            "desc": desc,
            "score": 0,
            "ua": ua,
            "text": text if kind == "event" else "",
        })
    print(f"  [DEBUG] Wikipedia feed ({api_kind}): {len(out)} football entries after filter")
    return out


def collect(month, day):
    people = wikidata_people(month, day)
    best = {}
    for it in people:
        # одна запись на человека; украинская версия имени имеет приоритет только если есть
        old = best.get(it["qid"])
        if old is None or (it["ua"] and not old["ua"]):
            best[it["qid"]] = it
    items = list(best.values())
    known = {i["qid"] for i in items if i["qid"]}
    for api_kind in ("events", "births", "deaths"):
        for it in wikipedia_feed(api_kind, month, day):
            if it["qid"] and it["qid"] in known:
                continue
            items.append(it)

    # збагачуємо біографії клубами/нагородами (лише people з qid)
    enriched = []
    for it in items:
        if it["kind"] in ("birth", "death") and it.get("qid"):
            try:
                it = enrich_person(it)
                time.sleep(0.15)
            except Exception as e:
                print(f"  enrich {it.get('name')}: {e}")
        enriched.append(it)
    return enriched



def enrich_person(item):
    """Додає клуби, позицію, збірну, нагороди; для зірок — ще summary з Вікіпедії."""
    qid = item.get("qid") or ""
    if not qid or item["kind"] == "event":
        return item
    query = f"""
    SELECT ?clubLabel ?awardLabel ?posLabel ?nlLabel WHERE {{
      OPTIONAL {{
        wd:{qid} wdt:P54 ?club .
        ?club rdfs:label ?clubLabel . FILTER(LANG(?clubLabel) = "en")
      }}
      OPTIONAL {{
        wd:{qid} wdt:P166 ?award .
        ?award rdfs:label ?awardLabel . FILTER(LANG(?awardLabel) = "en")
      }}
      OPTIONAL {{
        wd:{qid} wdt:P413 ?pos .
        ?pos rdfs:label ?posLabel . FILTER(LANG(?posLabel) = "en")
      }}
      OPTIONAL {{
        wd:{qid} wdt:P1447 ?nl .
        ?nl rdfs:label ?nlLabel . FILTER(LANG(?nlLabel) = "en")
      }}
    }}
    LIMIT 40
    """
    try:
        rows = run_sparql(query)
    except Exception:
        rows = []
    clubs, awards, positions, nations = [], [], [], []
    seen = {"c": set(), "a": set(), "p": set(), "n": set()}
    for b in rows:
        if "clubLabel" in b:
            c = b["clubLabel"]["value"]
            if c not in seen["c"]:
                seen["c"].add(c)
                clubs.append(c)
        if "awardLabel" in b:
            a = b["awardLabel"]["value"]
            if a not in seen["a"]:
                seen["a"].add(a)
                awards.append(a)
        if "posLabel" in b:
            pos = b["posLabel"]["value"]
            if pos not in seen["p"]:
                seen["p"].add(pos)
                positions.append(pos)
        if "nlLabel" in b:
            n = b["nlLabel"]["value"]
            if n not in seen["n"]:
                seen["n"].add(n)
                nations.append(n)

    wiki_bit = ""
    # для відомих (багато sitelinks) — короткий extract з enwiki
    if item.get("score", 0) >= 20 or len(clubs) >= 1:
        try:
            tr = run_sparql(f"""
            SELECT ?title WHERE {{
              ?s schema:about wd:{qid} ;
                 schema:isPartOf <https://en.wikipedia.org/> ;
                 schema:name ?title .
            }} LIMIT 1
            """)
            if tr:
                wtitle = tr[0]["title"]["value"].replace(" ", "_")
                wr = requests.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{wtitle}",
                    headers=WIKI_HEADERS,
                    timeout=20,
                )
                if wr.ok:
                    extract = (wr.json().get("extract") or "")[:450]
                    if extract:
                        wiki_bit = extract
        except Exception as e:
            print(f"  wiki summary {qid}: {e}")

    extra = []
    if positions:
        extra.append("position: " + ", ".join(positions[:2]))
    if nations:
        extra.append("national team: " + ", ".join(nations[:2]))
    if clubs:
        extra.append("clubs: " + ", ".join(clubs[:10]))
    if awards:
        extra.append("honours: " + ", ".join(awards[:8]))
    if wiki_bit:
        extra.append("bio: " + wiki_bit)

    if extra:
        base = item.get("desc") or ""
        item["desc"] = (base + "; " if base else "") + "; ".join(extra)
        item["score"] = item.get("score", 0) + len(clubs) + len(awards) * 2 + (5 if wiki_bit else 0)
    return item


def is_thin_person(item):
    """Занадто сухий запис: лише «footballer» без клубів/досягнень."""
    if item["kind"] == "event":
        return False
    d = (item.get("desc") or "").lower()
    if "clubs:" in d or "awards:" in d:
        return False
    # generic one-liners
    if re.fullmatch(r".*\b(footballer|soccer player|manager|coach)\b\.?", d.strip()):
        return True
    if len(d) < 25:
        return True
    return False


# ------------------------------------------------------------------ selection


def verify_on_this_day(item, month, day):
    """Додаткова перевірка: факт справді прив'язаний до цього дня.
    Wikidata people вже відфільтровані MONTH/DAY у SPARQL.
    Для Wikipedia events — рік і текст мають бути, і не виглядати як інша дата.
    """
    if item["kind"] in ("birth", "death"):
        # qid + sparql уже гарантують місяць/день; без qid (wiki-only) — обережніше
        if not item.get("qid") and not item.get("text") and not item.get("desc"):
            return False
        return not is_thin_person(item) or item.get("score", 0) >= 25
    # event
    text = item.get("text") or ""
    if len(text) < 40:
        return False
    # відсікаємо явні згадки інших місяців у тексті (груба евристика)
    other = []
    months_en = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    cur = months_en[month - 1]
    for i, mname in enumerate(months_en, 1):
        if i == month:
            continue
        if re.search(rf"\b{mname}\b", text):
            # якщо є інший місяць і немає поточного — підозріло
            if not re.search(rf"\b{cur}\b", text) and not re.search(rf"\b{day}\b", text):
                other.append(mname)
    if other:
        print(f"  event dropped (other month in text): {text[:60]}...")
        return False
    return True


def select(items, total):
    """Тільки цікаві підтверджені факти. Краще 3–4 сильних, ніж 12 сухих.
    Сухі «народився — національність футболіст» НЕ додаємо для набиття обсягу.
    """
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

    events = [i for i in items if i["kind"] == "event" and len((i.get("text") or "")) >= 40]
    rich_births = [i for i in items if i["kind"] == "birth" and not is_thin_person(i)]
    rich_deaths = [i for i in items if i["kind"] == "death" and not is_thin_person(i)]

    # 1. Події з нормальним текстом
    take(events, min(8, total))
    # 2. Змістовні народження / смерті
    take(by_score(rich_births), min(5, total - len(chosen)))
    take(by_score(rich_deaths), min(3, total - len(chosen)))
    # 3. Ще з «товстих», якщо є місце
    rest_rich = [i for i in items if i not in chosen and not is_thin_person(i)]
    take(by_score(rest_rich), total - len(chosen))
    # Навмисно НЕ беремо thin — краще менше фактів

    return sorted(chosen, key=lambda i: i["year"])




# ------------------------------------------------------------------ AI formatting (checked by code)

def numbers(s):
    return set(re.findall(r"\d+", s))


def line_ok(item, text):
    src = numbers(f"{item['year']} {item['text']} {item['desc']} {item['name']}")
    if not (40 <= len(text) <= 550) or not numbers(text) <= src:
        return False
    low = text.lower()
    if item["kind"] == "birth" and "народив" not in low:
        return False
    if item["kind"] == "death" and not ("помер" in low or "загин" in low):
        return False
    if quiz.has_foreign_cyrillic(text):
        return False
    # відсікаємо сухі рядки типу «Народився X — англійський футболіст.»
    if item["kind"] in ("birth", "death"):
        stripped = re.sub(r"народився|помер|загинув", "", low)
        if re.search(r"^[^—–-]*[—–-]\s*(англійський|німецький|французький|іспанський|"
                     r"італійський|бразильський|аргентинський|український|нідерландський|"
                     r"португальський|японський|грецький|чеський|бельгійський|валлійський|"
                     r"латвійський|нігерійський)\s+футболіст\.?$", stripped.strip()):
            # дозволити лише якщо в джерелі реально немає нічого більше
            if not is_thin_person(item):
                return False
        name = item["name"]
        if name:
            latin = re.search(r"[A-Za-z]", name) is not None
            if not item["ua"] and latin and name not in text:
                return False
            if item["ua"] and not latin and name not in text:
                return False
    return True


def format_with_ai(items, run_start):
    payload = [
        {
            "id": i,
            "type": it["kind"],
            "year": it["year"],
            "name": it["name"],
            "description": it["desc"],
            "text": it["text"],
            "ukrainian": it["ua"],
        }
        for i, it in enumerate(items)
    ]

    prompt = (
        "Ти редактор українського футбольного Telegram-каналу «football 90+». "
        "Нижче — перевірені факти з Вікіпедії та Вікіданих про чоловічий футбол. "
        "Для кожного напиши ОДИН змістовний абзац українською (1–3 речення), БЕЗ року на початку.\n\n"
        "=== СТИЛЬ (обов’язково дотримуйся) ===\n"
        "Пиши живо і цікаво, як у хороших історичних постах. Не сухі довідки.\n\n"
        "• type=event — розкажи подію детально: що сталося, де, чому це важливо або цікаво. "
        "Можна 2–3 речення.\n"
        "  Приклади хорошого стилю:\n"
        "  «У Глазго відбулася гра між Queen’s Park і лондонським Wanderers. "
        "Програма цього матчу вважається першою відомою друкованою програмою матчу, "
        "де були зазначені команди та склади.»\n"
        "  «На засіданні Brentford Rowing Club було ухвалено рішення провести зустріч "
        "щодо створення футбольного клубу. Це стало безпосереднім початком процесу, "
        "який привів до заснування Brentford Football Club.»\n"
        "  «У Белграді відбулася перша футбольна гра на ще не завершеній арені, "
        "яка згодом стала відомою як стадіон Югославської народної армії, "
        "а нині має назву Partizan Stadium.»\n"
        "  «Відбірковий матч між Естонією та Шотландією фактично завершився через "
        "кілька секунд після стартового свистка. Естонська команда не вийшла на поле "
        "через суперечку щодо часу початку матчу та освітлення стадіону. "
        "Пізніше FIFA ухвалила рішення про проведення гри повторно.»\n\n"
        "• type=birth — починай з «Народився», далі ім’я + хто це + 1–2 речення "
        "про досягнення / клуби / значення (ТІЛЬКИ з даних).\n"
        "  Приклади:\n"
        "  «Народився Вільям «Джек» Крейстон — майбутній футболіст і тренер Arsenal. "
        "Він виступав за збірну Англії, а після завершення ігрової кар’єри став "
        "менеджером Arsenal.»\n"
        "  «Народився Петр Жеков (Болгарія) — один із найвидатніших бомбардирів "
        "болгарського футболу. Він забив 253 голи в чемпіонаті Болгарії та здобув "
        "європейську «Золоту бутсу» 1969 року.»\n"
        "  «Народився Тоні Адамс (Англія) — легендарний капітан Arsenal, який провів "
        "за клуб усю свою професійну ігрову кар’єру. Він виграв чотири чемпіонські "
        "титули Англії та три Кубки Англії.»\n\n"
        "• type=death — починай з «Помер», далі ім’я + хто це + коротко чим відомий "
        "(ТІЛЬКИ з даних).\n\n"
        "=== ЖОРСТКІ ПРАВИЛА ===\n"
        "1. Використовуй ТІЛЬКИ дані з наведеного запису. Нічого не додавай і не вигадуй. "
        "Не змінюй і не додавай жодних чисел, рахунків, дат, назв клубів чи титулів.\n"
        "2. Іноземні імена, прізвища, назви клубів і стадіонів залишай ЛАТИНКОЮ "
        "(Real Madrid, Zvonimir Boban, Craven Cottage, Wembley, Arsenal, Partizan Stadium). "
        "НЕ перекладай і НЕ транслітеруй.\n"
        "3. Українські імена та назви пиши українською (Динамо Київ, Андрій Шевченко, "
        "НСК «Олімпійський»).\n"
        "4. Тільки чоловічий рід: «народився», «помер» (ніколи «народилася» / «померла»).\n"
        "5. Заборонено російські літери (ы, э, ё, ъ).\n"
        "6. Без емодзі, Markdown, посилань, оціночних епітетів типу «великий», «легендарний» "
        "— якщо цього немає в даних.\n"
        "7. Відповідай ЛИШЕ валідним JSON:\n"
        '{"lines": [{"id": 0, "text": "..."}, ...]}\n\n'
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
    head = f"⚽ <b>Цього дня в історії футболу</b>\n📅 {day} {MONTHS[month - 1]}\n\n"
    cont = "⚽ <b>Цього дня в історії футболу (продовження)</b>\n\n"
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
    items = [it for it in items if verify_on_this_day(it, month, day)]
    print(f"Digest: {len(items)} after on-this-day verify")
    if len(items) < DIGEST_MIN_ITEMS:
        return []
    chosen = select(items, DIGEST_ITEMS)
    print(f"Digest: selected {len(chosen)} facts "
          f"(events={sum(1 for i in chosen if i['kind']=='event')}, "
          f"births={sum(1 for i in chosen if i['kind']=='birth')}, "
          f"deaths={sum(1 for i in chosen if i['kind']=='death')})")
    lines = format_with_ai(chosen, run_start)
    if not lines or len(lines) < DIGEST_MIN_ITEMS:
        print(f"Digest: only {len(lines or [])} verified line(s), minimum is {DIGEST_MIN_ITEMS}.")
        return []
    return build_messages(lines, day, month)


# ------------------------------------------------------------------ publishing & state

DIGEST_IMAGE = os.environ.get("DIGEST_IMAGE", "Цього дня в історії футбола.jpg")

def send_message(text, image=None):
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    if image and os.path.exists(image):
        with open(image, "rb") as f:
            r = requests.post(
                f"{api}/sendPhoto",
                data={"chat_id": CHANNEL, "caption": text, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=60,
            )
        if r.ok:
            return
        print("  sendPhoto failed, sending text only:", r.text[:200])
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
        print("  Telegram refused the message:", r.text[:300])
        r.raise_for_status()


def publish(messages):
    for n, m in enumerate(messages):
        send_message(m, image=DIGEST_IMAGE if n == 0 else None)
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

    if os.environ.get("DIGEST_FORCE", "").lower() == "true":
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
    if state["done"]:
        save_state(state)
        return

    mins = now.hour * 60 + now.minute
    dig = pn.to_minutes(DIGEST_TIME)
    PREPARE_START = 0 * 60 + 1   # 00:01
    PREPARE_END = 6 * 60 + 30    # 06:30
    before_time = mins < dig

    # ---- підготовка: 00:01–06:30 пріоритет; якщо не встигли — до DIGEST_TIME ----
    # quiet hours НЕ блокують підготовку
    if not state["messages"]:
        if mins < PREPARE_START:
            save_state(state)
            return
        # не молотити AI частіше ніж раз на 10 хв
        if time.time() - state.get("last_try", 0) < 600:
            save_state(state)
            return
        state["last_try"] = time.time()
        if not before_time:
            state["attempts"] = state.get("attempts", 0) + 1
        print(f"Digest: preparing (mins={mins})...")
        state["messages"] = prepare(now, run_start)
        print("Digest: prepared." if state["messages"] else "Digest: not ready yet.")
        if not state["messages"] and not before_time and state.get("attempts", 0) >= MAX_ATTEMPTS:
            state["done"] = True
            print("Digest: too many failed attempts, skipping today.")
        save_state(state)
        return

    if before_time:
        print("Digest: ready, waiting for", DIGEST_TIME)
        save_state(state)
        return

    # ---- публікація після DIGEST_TIME (09:00) ----
    publish(state["messages"])
    state["done"] = True
    save_state(state)
    print(f"Digest published ({len(state['messages'])} message(s)).")


if __name__ == "__main__":
    main()
