"""Daily football quizzes for a Telegram channel (quiz polls: 4 options, exactly 1 correct).

How it works (runs together with post_news.py every few minutes):
  * Before QUIZ_TIME (Kyiv time) it prepares ONE quiz per run until QUIZZES_PER_DAY are ready.
  * At QUIZ_TIME it publishes all ready quizzes one after another.
  * Every accepted question is appended to quiz_history.jsonl and never used again;
    new questions are compared with the whole history (exact and similar wording).
"""
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import post_news as pn

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@football_90_pluss")
QUIZ_TIME = os.environ.get("QUIZ_TIME", "19:30")          # Kyiv time, "HH:MM"
QUIZZES_PER_DAY = int(os.environ.get("QUIZZES_PER_DAY", "6"))
PAUSE_BETWEEN_SEC = float(os.environ.get("QUIZ_PAUSE_SEC", "3"))
HISTORY_KEEP_DAYS = int(os.environ.get("HISTORY_KEEP_DAYS", "0"))  # 0 = keep every question forever
STATE_FILE = Path("quiz_state.json")        # today's counter + prepared (ready) quizzes
HISTORY_FILE = Path("quiz_history.jsonl")   # every question ever accepted, one JSON per line

TOPICS = [
    "історія чемпіонатів світу", "чемпіонати Європи", "Ліга чемпіонів УЄФА",
    "Ліга Європи та Кубок УЄФА", "Динамо Київ та Шахтар в єврокубках",
    "збірна України в історії", "легенди світового футболу", "правила футболу",
    "відомі стадіони світу", "легендарні тренери", "футбольні рекорди та цікаві факти",
    "Золотий м'яч", "відомі трансфери минулих років", "англійська Прем'єр-ліга: історія",
    "Ла Ліга та Серія А: історія", "Бундесліга та Ліга 1: історія",
    "футбольні терміни та походження гри", "українські футболісти за кордоном",
    "чемпіонат України: історія", "кубки національних ліг", "воротарі в історії футболу",
    "бомбардири та рекорди голів", "знамениті дербі та суперництва", "емблеми, форма й прізвиська клубів",
]
ANGLES = (
    ["1930-ті", "1950-ті", "1960-ті", "1970-ті", "1980-ті", "1990-ті", "2000-ні", "2010-ті"]
    + ["Бразилія", "Аргентина", "Німеччина", "Італія", "Іспанія", "Франція", "Англія", "Нідерланди",
       "Португалія", "Уругвай", "Хорватія", "Бельгія", "Україна", "Польща", "Туреччина", "Шотландія",
       "Мексика", "США", "Японія", "Південна Корея", "Нігерія", "Камерун", "Сенегал", "Марокко",
       "Греція", "Данія", "Швеція", "Чехія", "Румунія", "Сербія", "Ірландія", "Колумбія"]
    + ["Барселона", "Реал Мадрид", "Манчестер Юнайтед", "Ліверпуль", "Арсенал", "Челсі", "Баварія",
       "Ювентус", "Мілан", "Інтер", "Аякс", "Бенфіка", "Порту", "ПСЖ", "Боруссія Дортмунд",
       "Динамо Київ", "Шахтар", "Дніпро", "Зоря", "Металіст", "Карпати", "Атлетіко", "Ліон", "Селтік"]
    + ["воротарі", "захисники", "півзахисники", "нападники", "тренери", "арбітри", "капітани", "стадіони"]
)
QUESTION_TYPES = ["Хто?", "Коли?", "Де?", "Скільки?", "Який клуб?", "Яка країна?", "Який рік?", "Що означає?"]
LEVELS = ["легке", "середнє", "складне"]


# ---------------------------------------------------------------- AI

def ask_ai(prompt, prefer_not=None):
    """Runs the AI chain (Gemini -> backups). Returns (text, engine_name).
    prefer_not: try the other engines first (used for independent fact checking)."""
    engines = []
    if pn.GEMINI_KEY:
        engines.append(("Gemini", pn.ask_gemini))
    for n in ("LLM2", "LLM3"):
        if os.environ.get(f"{n}_API_KEY"):
            engines.append((n, lambda p, n=n: pn.ask_openai_compat(n, p)))
    if prefer_not:
        engines.sort(key=lambda e: e[0] == prefer_not)  # stable: the others go first
    for name, fn in engines:
        try:
            return fn(prompt), name
        except Exception as e:
            print(f"  {name} failed:", e)
    raise pn.RewriteFailed("all AI engines failed")


def ai_call(prompt, run_start, prefer_not=None):
    """ask_ai with waiting and retrying inside the retry window. None if unavailable."""
    while True:
        try:
            return ask_ai(prompt, prefer_not)
        except pn.RewriteFailed:
            left = pn.RETRY_WINDOW_MIN * 60 - (time.time() - run_start)
            if left <= pn.RETRY_PAUSE_SEC:
                return None
            print(f"  AI unavailable, trying again in {pn.RETRY_PAUSE_SEC:.0f} s")
            time.sleep(pn.RETRY_PAUSE_SEC)


# ---------------------------------------------------------------- text helpers

def clean(text):
    text = re.sub(r"https?://\S+", "", str(text))
    text = text.replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def norm(text):
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def tokens(text):
    return {w for w in norm(text).split() if len(w) > 2}


# ---------------------------------------------------------------- history (never repeat)

def load_history():
    items = []
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except ValueError:
                    pass
    return items


def prune_history(items, today):
    """Optional cleanup (off by default): drops questions older than HISTORY_KEEP_DAYS."""
    if HISTORY_KEEP_DAYS <= 0:
        return items
    cutoff = (date.fromisoformat(today) - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    kept = [i for i in items if not i.get("d") or i["d"] >= cutoff]  # undated = keep
    if len(kept) != len(items):
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in kept),
                       encoding="utf-8")
        tmp.replace(HISTORY_FILE)
        print(f"History: removed {len(items) - len(kept)} question(s) older than "
              f"{HISTORY_KEEP_DAYS} days.")
    return kept


def append_history(entry):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:  # append-only keeps git history small
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class History:
    def __init__(self, items):
        self.items = items
        self.exact = {norm(i["q"]) for i in items}
        self.rows = [(tokens(i["q"]), norm(i.get("a", ""))) for i in items]

    def is_repeat(self, question, answer):
        if norm(question) in self.exact:
            return True
        t, a = tokens(question), norm(answer)
        for tt, aa in self.rows:
            sim = len(t & tt) / max(1, len(t | tt))
            if sim >= 0.75 or (sim >= 0.5 and a and a == aa):  # reworded copy / same answer
                return True
        return False

    def add(self, question, answer, day):
        entry = {"d": day, "q": question, "a": answer}
        self.items.append(entry)
        self.exact.add(norm(question))
        self.rows.append((tokens(question), norm(answer)))
        append_history(entry)

    def examples_for_prompt(self):
        qs = [i["q"] for i in self.items]
        recent, older = qs[-25:], qs[:-25]
        sample = random.sample(older, min(25, len(older)))
        return sample + recent


# ---------------------------------------------------------------- generating

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


def build_prompt(history):
    examples = "\n".join(f"- {q}" for q in history.examples_for_prompt()) or "(поки немає)"
    return (
        "Ти редактор українського футбольного Telegram-каналу «football 90+». "
        "Придумай ОДНЕ питання для вікторини.\n"
        f"Тема: {random.choice(TOPICS)}. Акцент: {random.choice(ANGLES)}. "
        f"Тип питання: {random.choice(QUESTION_TYPES)} Складність: {random.choice(LEVELS)}.\n"
        "Вимоги (дуже важливо, питання публікується для тисяч людей):\n"
        "- тільки ДОВЕДЕНІ, задокументовані факти, які легко перевірити в офіційних джерелах "
        "(ФІФА, УЄФА, офіційні сайти клубів і ліг, енциклопедії): переможці турнірів, роки й місця "
        "проведення, фіналісти, офіційні назви, правила, склади й результати відомих матчів;\n"
        "- ніяких суб'єктивних оцінок («найкращий», «наймогутніший», «найвідоміший»), "
        "прогнозів, думок, чуток, легенд і спірних тверджень;\n"
        "- точні числа (кількість голів, рік, рахунок) лише якщо ти на 100% їх знаєш; "
        "суми трансферів, відвідуваність і неоднозначну статистику не використовуй;\n"
        "- не питай про події останніх двох років і про поточні сезони;\n"
        "- питання має РІВНО одну правильну відповідь, без двозначності;\n"
        "- якщо ти хоч трохи не впевнений у факті, обери інше питання;\n"
        "- три хибні відповіді правдоподібні, але однозначно хибні;\n"
        "- мова: українська; питання до 250 символів, кожна відповідь до 90 символів;\n"
        "- пояснення (1-2 речення, до 180 символів) коротко підтверджує правильну відповідь.\n"
        f"Не повторюй і не перефразовуй питання, що вже були:\n{examples}\n\n"
        "Відповідай ЛИШЕ JSON без жодного іншого тексту, у такому форматі:\n"
        '{"question": "...", "correct_answer": "...", '
        '"wrong_answers": ["...", "...", "..."], "explanation": "..."}'
    )


def check_facts(quiz, author, run_start):
    """Asks ANOTHER AI to fact-check the quiz. True = confirmed, False = rejected,
    None = no checker available right now."""
    letters = "ABCD"
    opts = "\n".join(f"{letters[i]}) {o}" for i, o in enumerate(quiz["options"]))
    prompt = (
        "Ти суворий фактчекер футбольної історії та статистики. Перевір вікторину.\n"
        f"Питання: {quiz['question']}\n{opts}\n"
        f"Вказана правильна відповідь: {letters[quiz['correct_id']]}) "
        f"{quiz['options'][quiz['correct_id']]}\n"
        f"Пояснення: {quiz['explanation']}\n\n"
        "Перевір: 1) чи вказана відповідь фактично правильна за задокументованими даними "
        "(ФІФА, УЄФА, офіційні джерела); 2) чи кожна з трьох інших відповідей однозначно хибна; "
        "3) чи питання однозначне й без суб'єктивних оцінок. "
        "Якщо є хоч найменший сумнів, вердикт «unsure».\n"
        'Відповідай ЛИШЕ JSON: {"verdict": "ok" | "wrong" | "unsure", "reason": "коротко"}'
    )
    res = ai_call(prompt, run_start, prefer_not=author)
    if res is None:
        return None
    raw, checker = res
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0))
        verdict = str(data.get("verdict", "")).strip().lower()
        print(f"  fact check by {checker}: {verdict} {data.get('reason', '')[:120]}")
        return verdict == "ok"
    except Exception:
        print(f"  fact check by {checker}: unreadable answer, treated as not confirmed")
        return False


def make_quiz(history, run_start):
    """Returns a new, non-repeating, fact-checked quiz, or None if not possible in this run."""
    bad = 0
    while True:
        res = ai_call(build_prompt(history), run_start)
        if res is None:
            return None
        raw, author = res
        try:
            quiz = parse_quiz(raw)
            if history.is_repeat(quiz["question"], quiz["options"][quiz["correct_id"]]):
                raise ValueError("repeat of an earlier question")
        except Exception as e:
            bad += 1
            print(f"  unusable quiz ({e}), attempt {bad}")
            if bad >= 6:
                return None
            continue
        verdict = check_facts(quiz, author, run_start)
        if verdict is None:
            return None
        if verdict:
            return quiz
        bad += 1
        print(f"  quiz rejected by the fact check, attempt {bad}")
        if bad >= 6:
            return None


# ---------------------------------------------------------------- sending

def send_quiz(quiz):
    """Publishes one quiz poll. Returns True on success, False if Telegram rejects this quiz."""
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendPoll"
    body = {
        "chat_id": CHANNEL,
        "question": quiz["question"],
        "options": [{"text": o} for o in quiz["options"]],
        "type": "quiz",
        "correct_option_id": quiz["correct_id"],
        "explanation": quiz["explanation"],
        "is_anonymous": True,  # channels only allow anonymous polls
    }
    for attempt in range(2):
        r = requests.post(url, json=body, timeout=60)
        if r.status_code == 429 and attempt == 0:  # flood control: wait as Telegram asks
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 10))
            except Exception:
                wait = 10
            print(f"  Telegram asks to wait {wait} s")
            time.sleep(min(wait, 30))
            continue
        if r.ok:
            return True
        print("  Telegram refused the quiz:", r.text[:300])
        if r.status_code == 400:
            return False  # this particular quiz is invalid, drop it
        r.raise_for_status()
    raise RuntimeError("could not send the quiz")


# ---------------------------------------------------------------- state

def load_state():
    state = {"day": "", "posted": 0, "ready": []}
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.update({k: data[k] for k in ("day", "posted", "ready") if k in data})
        for q in data.get("history", []):  # migrate the very first file format
            append_history({"d": "", "q": q, "a": ""})
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main():
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    run_start = time.time()
    manual_test = os.environ.get("QUIZ_FORCE", "").lower() == "true"

    state = load_state()
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today = now.date().isoformat()
    if state["day"] != today:  # new day: counter restarts, unused ready quizzes are kept
        state["day"], state["posted"] = today, 0
    history = History(prune_history(load_history(), today))

    def prepare_one():
        quiz = make_quiz(history, run_start)
        if quiz is None:
            return False
        history.add(quiz["question"], quiz["options"][quiz["correct_id"]], today)
        state["ready"].append(quiz)
        save_state(state)
        print(f"Quiz prepared ({len(state['ready'])} ready): {quiz['question']}")
        return True

    # ---- manual test: publish exactly one quiz now, does not count for the day
    if manual_test:
        if not state["ready"] and not prepare_one():
            print("Quiz test: AI unavailable.")
            return
        quiz = state["ready"].pop(0)
        if send_quiz(quiz):
            print("Quiz test published:", quiz["question"])
        save_state(state)
        return

    if pn.in_quiet_hours():
        print("Quiz: quiet hours.")
        save_state(state)
        return
    if state["posted"] >= QUIZZES_PER_DAY:
        print(f"Quiz: all {QUIZZES_PER_DAY} quizzes already published today.")
        save_state(state)
        return

    # ---- before QUIZ_TIME: prepare one quiz per run
    if now.hour * 60 + now.minute < pn.to_minutes(QUIZ_TIME):
        need = QUIZZES_PER_DAY - state["posted"] - len(state["ready"])
        if need <= 0:
            print(f"Quiz: {len(state['ready'])}/{QUIZZES_PER_DAY} ready, waiting for {QUIZ_TIME}.")
        elif not prepare_one():
            print("Quiz: could not prepare one now, will try on the next run.")
        save_state(state)
        return

    # ---- QUIZ_TIME reached: publish everything that is ready, one after another
    while state["posted"] < QUIZZES_PER_DAY:
        if not state["ready"] and not prepare_one():
            print("Quiz: AI unavailable, the rest will follow on the next run.")
            break
        quiz = state["ready"].pop(0)
        sent = send_quiz(quiz)
        if sent:
            state["posted"] += 1
            print(f"Quiz {state['posted']}/{QUIZZES_PER_DAY} published: {quiz['question']}")
        save_state(state)
        if sent and state["posted"] < QUIZZES_PER_DAY:
            time.sleep(PAUSE_BETWEEN_SEC)
    save_state(state)

def has_foreign_cyrillic(text):
    """
    Проверяет, содержит ли текст кириллические символы, 
    которые не являются украинскими (русские буквы) или 
    которые являются транслитерацией иностранных названий.
    Возвращает True, если есть подозрительные символы.
    """
    # Русские буквы, которых нет в украинском алфавите
    russian_chars = set("ыэёъ")
    # Также можно добавить проверку на другие признаки, но начнем с этого
    for char in text.lower():
        if char in russian_chars:
            return True
    return False

if __name__ == "__main__":
    main()
