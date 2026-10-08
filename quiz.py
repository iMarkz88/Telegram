"""Daily football quiz for a Telegram channel (quiz poll: 4 options, exactly 1 correct).

Runs together with post_news.py every few minutes, but publishes only once per day,
after QUIZ_TIME (Kyiv time) and never during quiet hours.
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import post_news as pn

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
QUIZ_TIME = os.environ.get("QUIZ_TIME", "18:00")  # Kyiv time, "HH:MM"
QUIZ_COUNT = int(os.environ.get("QUIZ_COUNT", "1"))  # quizzes published in a row each day
PAUSE_BETWEEN_SEC = 3
STATE_FILE = Path("quiz_state.json")

TOPICS = [
    "історія чемпіонатів світу", "чемпіонати Європи", "Ліга чемпіонів УЄФА",
    "Динамо Київ та Шахтар в єврокубках", "збірна України в історії",
    "легенди світового футболу", "правила футболу", "відомі стадіони світу",
    "легендарні тренери", "футбольні рекорди та цікаві факти", "Золотий м'яч",
    "відомі трансфери минулих років", "англійська Прем'єр-ліга: історія",
    "Ла Ліга та Серія А: історія", "футбольні терміни та походження гри",
    "українські футболісти за кордоном", "клубні чемпіонати: легендарні сезони",
]


def ask_ai(prompt):
    """Runs the same AI chain as the news bot (Gemini -> backups). Raises RewriteFailed."""
    engines = []
    if pn.GEMINI_KEY:
        engines.append(("Gemini", pn.ask_gemini))
    for n in ("LLM2", "LLM3"):
        if os.environ.get(f"{n}_API_KEY"):
            engines.append((n, lambda p, n=n: pn.ask_openai_compat(n, p)))
    for name, fn in engines:
        try:
            return fn(prompt)
        except Exception as e:
            print(f"  {name} failed:", e)
    raise pn.RewriteFailed("all AI engines failed")


def clean(text):
    text = re.sub(r"https?://\S+", "", str(text))
    text = text.replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def norm(text):
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def parse_quiz(raw):
    """Parses the AI answer, validates it, shuffles the options."""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON in the answer")
    data = json.loads(m.group(0))
    question = clean(data["question"])
    correct = clean(data["correct_answer"])
    wrong = [clean(w) for w in data["wrong_answers"]][:3]
    explanation = clean(data.get("explanation", ""))[:190]

    options = [correct] + wrong
    if not (10 <= len(question) <= 280):
        raise ValueError("bad question length")
    if len(wrong) != 3 or any(not o or len(o) > 95 for o in options):
        raise ValueError("need 1 correct + 3 wrong options up to 95 chars")
    if len({norm(o) for o in options}) != 4:
        raise ValueError("options are not distinct")

    random.shuffle(options)  # the AI likes to put the right answer in one place
    return {"question": question, "options": options,
            "correct_id": options.index(correct), "explanation": explanation}


def build_prompt(topic, history):
    recent = "\n".join(f"- {q}" for q in history[-40:]) or "(поки немає)"
    return (
        "Ти редактор українського футбольного Telegram-каналу «football 90+». "
        f"Придумай ОДНЕ питання для вікторини на тему: {topic}.\n"
        "Вимоги:\n"
        "- питання має однозначну, загальновідому, перевірювану відповідь; "
        "якщо ти не впевнений у факті на 100%, обери інше питання;\n"
        "- не питай про події останніх двох років, про статистику, що змінюється, "
        "і про поточні рекорди;\n"
        "- одна правильна відповідь і три хибні, але правдоподібні;\n"
        "- мова: українська; питання до 250 символів, кожна відповідь до 90 символів;\n"
        "- пояснення (1-2 речення, до 180 символів) коротко підтверджує правильну відповідь.\n"
        f"Не повторюй питання, що вже були:\n{recent}\n\n"
        "Відповідай ЛИШЕ JSON без жодного іншого тексту, у такому форматі:\n"
        '{"question": "...", "correct_answer": "...", '
        '"wrong_answers": ["...", "...", "..."], "explanation": "..."}'
    )


def make_quiz(history, run_start, used_topics=None):
    """Returns a quiz dict, or None if the AI is unavailable for the whole retry window."""
    used_topics = used_topics if used_topics is not None else []
    seen = {norm(q) for q in history}
    bad_format = 0
    while True:
        free = [t for t in TOPICS if t not in used_topics] or TOPICS
        topic = random.choice(free)
        prompt = build_prompt(topic, history)
        try:
            raw = ask_ai(prompt)
        except pn.RewriteFailed:
            left = pn.RETRY_WINDOW_MIN * 60 - (time.time() - run_start)
            if left <= pn.RETRY_PAUSE_SEC:
                return None
            print(f"  AI unavailable, trying again in {pn.RETRY_PAUSE_SEC:.0f} s")
            time.sleep(pn.RETRY_PAUSE_SEC)
            continue
        try:
            quiz = parse_quiz(raw)
            if norm(quiz["question"]) in seen:
                raise ValueError("this question was already used")
            used_topics.append(topic)
            return quiz
        except Exception as e:
            bad_format += 1
            print(f"  unusable quiz ({e}), attempt {bad_format}")
            if bad_format >= 4:
                return None


def send_quiz(quiz):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendPoll",
        json={
            "chat_id": CHANNEL,
            "question": quiz["question"],
            "options": [{"text": o} for o in quiz["options"]],
            "type": "quiz",
            "correct_option_id": quiz["correct_id"],
            "explanation": quiz["explanation"],
            "is_anonymous": True,  # channels only allow anonymous polls
        },
        timeout=60,
    )
    if not r.ok:
        print("  Telegram refused the quiz:", r.text[:300])
        r.raise_for_status()


def load_state():
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
    else:
        data = {}
    data.setdefault("last_date", "")
    data.setdefault("progress", {"date": "", "count": 0})
    data.setdefault("history", [])
    return data


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    run_start = time.time()
    manual_test = os.environ.get("QUIZ_FORCE", "").lower() == "true"

    state = load_state()
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today = now.date().isoformat()

    done_today = state["progress"]["count"] if state["progress"]["date"] == today else 0
    if not manual_test:
        if state["last_date"] == today or done_today >= QUIZ_COUNT:
            print("Quiz: today's quizzes are already published.")
            return
        if now.hour * 60 + now.minute < pn.to_minutes(QUIZ_TIME):
            print(f"Quiz: not yet, scheduled for {QUIZ_TIME} Kyiv time.")
            return
        if pn.in_quiet_hours():
            print("Quiz: quiet hours, will not publish.")
            return
        to_post = QUIZ_COUNT - done_today
    else:
        to_post = QUIZ_COUNT

    used_topics, published = [], 0
    for i in range(to_post):
        quiz = make_quiz(state["history"], run_start, used_topics)
        if quiz is None:
            print("Quiz: AI unavailable, the rest will be tried on the next run.")
            break
        send_quiz(quiz)
        published += 1
        print(f"Quiz {i + 1}/{to_post} published:", quiz["question"])
        state["history"] = (state["history"] + [quiz["question"]])[-150:]
        if not manual_test:  # progress is saved after every quiz
            state["progress"] = {"date": today, "count": done_today + published}
            if state["progress"]["count"] >= QUIZ_COUNT:
                state["last_date"] = today
        save_state(state)
        if i < to_post - 1:
            time.sleep(PAUSE_BETWEEN_SEC)


if __name__ == "__main__":
    main()
