"""
Рубрика «Великі футболісти»
Публікація: щопонеділка о 13:00 за Києвом.
Джерело: Wikipedia → переказ українською (1500+ слів).
Картинка додається окремо.
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
PUBLISH_TIME = "13:00"          # Київ
STATE_FILE = Path("great_players_state.json")
TG_LIMIT = 3900

# Список легенд (можна розширювати). Скрипт бере наступного по черзі.
PLAYERS = [
    {"name": "Rui Costa", "wiki": "Rui_Costa", "ua_title": "Руй Кошта"},
    {"name": "Zinedine Zidane", "wiki": "Zinedine_Zidane", "ua_title": "Зінедін Зідан"},
    {"name": "Andrea Pirlo", "wiki": "Andrea_Pirlo", "ua_title": "Андреа Пірло"},
    {"name": "Xavi", "wiki": "Xavi", "ua_title": "Хаві"},
    {"name": "Andrés Iniesta", "wiki": "Andrés_Iniesta", "ua_title": "Андрес Іньєста"},
    {"name": "Francesco Totti", "wiki": "Francesco_Totti", "ua_title": "Франческо Тотті"},
    {"name": "Paolo Maldini", "wiki": "Paolo_Maldini", "ua_title": "Паоло Мальдіні"},
    {"name": "Franco Baresi", "wiki": "Franco_Baresi", "ua_title": "Франко Барезі"},
    {"name": "Michel Platini", "wiki": "Michel_Platini", "ua_title": "Мішель Платіні"},
    {"name": "Johan Cruyff", "wiki": "Johan_Cruyff", "ua_title": "Йоган Кройф"},
    {"name": "Marco van Basten", "wiki": "Marco_van_Basten", "ua_title": "Марко ван Бастен"},
    {"name": "Ruud Gullit", "wiki": "Ruud_Gullit", "ua_title": "Рууд Гулліт"},
    {"name": "George Best", "wiki": "George_Best", "ua_title": "Джордж Бест"},
    {"name": "Bobby Charlton", "wiki": "Bobby_Charlton", "ua_title": "Боббі Чарлтон"},
    {"name": "Eusébio", "wiki": "Eusébio", "ua_title": "Ейсебіо"},
    {"name": "Ferenc Puskás", "wiki": "Ferenc_Puskás", "ua_title": "Ференц Пушкаш"},
    {"name": "Diego Maradona", "wiki": "Diego_Maradona", "ua_title": "Дієго Марадона"},
    {"name": "Pelé", "wiki": "Pelé", "ua_title": "Пеле"},
    {"name": "Garrincha", "wiki": "Garrincha", "ua_title": "Гаррінча"},
    {"name": "Ronaldo", "wiki": "Ronaldo_(Brazilian_footballer)", "ua_title": "Роналдо"},
    {"name": "Ronaldinho", "wiki": "Ronaldinho", "ua_title": "Роналдіньйо"},
    {"name": "Kaká", "wiki": "Kaká", "ua_title": "Кака"},
    {"name": "Rivaldo", "wiki": "Rivaldo", "ua_title": "Рівалдо"},
    {"name": "Thierry Henry", "wiki": "Thierry_Henry", "ua_title": "Тьєррі Анрі"},
    {"name": "Zlatan Ibrahimović", "wiki": "Zlatan_Ibrahimović", "ua_title": "Златан Ібрагімович"},
    {"name": "Andriy Shevchenko", "wiki": "Andriy_Shevchenko", "ua_title": "Андрій Шевченко"},
    {"name": "Oleh Blokhin", "wiki": "Oleh_Blokhin", "ua_title": "Олег Блохін"},
]

WIKI_HEADERS = {
    "User-Agent": "football90-great-players/1.0 (Telegram @football_90_pluss)",
    "Accept": "application/json",
}


def fetch_wikipedia(title: str) -> str:
    """Отримує повний текст статті англійською."""
    url = f"https://en.wikipedia.org/w/api.php"
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
    return page.get("extract", "")


def format_with_ai(source_text: str, ua_title: str, run_start: float) -> str | None:
    prompt = f"""
Ти — головний редактор українського футбольного Telegram-каналу «football 90+».
Напиши ДОВГИЙ розгорнутий матеріал українською мовою про футболіста {ua_title}.

ОБОВ’ЯЗКОВІ ВИМОГИ:
1. Об’єм — НЕ МЕНШЕ 1500 слів (краще 1600–1800).
2. Перекажи зміст статті своїми словами. Нічого не вигадуй. Не додавай фактів, яких немає в джерелі.
3. Структура:
   - Вступ (хто це, чому легенда)
   - Дитинство і початок кар’єри
   - Клубна кар’єра (по клубах, з деталями)
   - Збірна
   - Стиль гри
   - Досягнення і трофеї
   - Після кар’єри (якщо є)
   - Висновок / значення для футболу
4. Іноземні імена, назви клубів, стадіонів залишай ЛАТИНКОЮ (Rui Costa, Benfica, Fiorentina, AC Milan, San Siro тощо).
5. Українські імена та назви — українською.
6. Без емодзі, Markdown, посилань, оціночних епітетів, яких немає в джерелі.
7. Пиши живо, цікаво, але строго по фактах.
8. Відповідай ЛИШЕ текстом статті. Без вступу типу «Ось текст» і без JSON.

Джерело (англійська Вікіпедія):
{source_text[:12000]}
"""
    res = quiz.ai_call(prompt, run_start)
    if res is None:
        return None
    raw, _ = res
    text = quiz.clean(raw)
    # проста перевірка довжини
    if len(text.split()) < 1400:
        print(f"  AI text too short: {len(text.split())} words")
        return None
    return text


def build_messages(text: str, ua_title: str) -> list[str]:
    head = f"⚽ <b>Великі футболісти</b>\n📅 {ua_title}\n\n"
    cont = f"⚽ <b>Великі футболісти (продовження)</b>\n📅 {ua_title}\n\n"
    messages, current = [], head
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    for p in paragraphs:
        p = html.escape(p)
        if len(current) + len(p) + 2 > TG_LIMIT and current not in (head, cont):
            messages.append(current.rstrip())
            current = cont
        current += p + "\n\n"
    messages.append(current.rstrip())
    return messages


def load_state():
    state = {"index": 0, "last_date": "", "done": False}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def prepare(run_start: float) -> list[str] | None:
    state = load_state()
    idx = state["index"] % len(PLAYERS)
    player = PLAYERS[idx]
    print(f"Great Players: preparing {player['ua_title']} ({player['wiki']})")
    try:
        source = fetch_wikipedia(player["wiki"])
        if len(source) < 500:
            print("  Wikipedia text too short")
            return None
    except Exception as e:
        print(f"  Wikipedia fetch failed: {e}")
        return None
    text = format_with_ai(source, player["ua_title"], run_start)
    if not text:
        return None
    return build_messages(text, player["ua_title"])


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

    if os.environ.get("PLAYERS_FORCE", "").lower() == "true":
        messages = prepare(run_start)
        if messages:
            publish(messages)
            print(f"Great Players test published ({len(messages)} msgs).")
        else:
            print("Great Players test: failed to prepare.")
        return

    # тільки понеділок
    if now.weekday() != 0:
        return

    state = load_state()
    if state.get("last_date") == today and state.get("done"):
        return
    if pn.in_quiet_hours():
        return

    before = now.hour * 60 + now.minute < pn.to_minutes(PUBLISH_TIME)
    if before:
        # готуємо заздалегідь
        if not state.get("messages"):
            state["messages"] = prepare(run_start) or []
            save_state(state)
        return

    # час публікації
    messages = state.get("messages") or prepare(run_start)
    if not messages:
        print("Great Players: nothing to publish")
        return
    publish(messages)
    state["last_date"] = today
    state["done"] = True
    state["index"] = (state.get("index", 0) + 1) % len(PLAYERS)
    state["messages"] = []
    save_state(state)
    print(f"Great Players published ({len(messages)} msgs).")


if __name__ == "__main__":
    main()
