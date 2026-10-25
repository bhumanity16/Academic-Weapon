import os
import re
from datetime import date

from dotenv import load_dotenv
from groq import Groq

# Load local environment variables from .env file
load_dotenv()

# Read from Vercel env so the model can be swapped without a code change
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_FIELD_CHARS = 2000
MODES = ("planner", "milestone", "booster")

# Shared output rules so every mode renders nicely with the frontend's markdown styles
FORMAT_RULES = (
    " Format the answer in Markdown. Use a '### ' heading for each day or phase, "
    "followed by a bullet list of concrete tasks with times or durations. "
    "Be realistic for a high school student, keep it concise, and never output raw HTML."
)

TRIMMED_NOTE = "\n\n*(This plan was too long and got trimmed. Try a closer date or fewer items.)*"


class InputError(ValueError):
    """The user's input is missing or malformed. The route turns this into an HTTP 400."""


def get_groq_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing from your environment variables.")
    return Groq(api_key=api_key)


def _text(user_input, key, label):
    value = str(user_input.get(key) or "").strip()
    if not value:
        raise InputError(f"{label} is required.")
    if len(value) > MAX_FIELD_CHARS:
        raise InputError(f"{label} is too long (max {MAX_FIELD_CHARS} characters).")
    return value


def _future_date(user_input, key, label, today):
    value = _text(user_input, key, label)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise InputError(f"{label} must be a valid date (YYYY-MM-DD).")
    if parsed < today:
        raise InputError(f"{label} can't be in the past.")
    return parsed


def _pretty(d):
    """e.g. 'Friday, November 20, 2026' — computed in Python because LLMs get weekdays wrong."""
    return d.strftime("%A, %B %d, %Y").replace(" 0", " ")


def _current_time(user_input):
    """The student's local time as HH:MM (sent by the browser), or None if missing/invalid."""
    value = str(user_input.get("current_time") or "").strip()
    return value if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else None


def build_prompt(mode, user_input, today):
    if mode not in MODES:
        raise InputError("Invalid mode selected.")
    if not isinstance(user_input, dict):
        raise InputError("Input data must be an object.")

    date_info = (
        f" Today is {_pretty(today)}. Label every day with its correct weekday; "
        "count forward from today and do not guess."
    )

    if mode == "planner":
        exam_date = _future_date(user_input, "exam_date", "Exam start date", today)
        subjects = _text(user_input, "subjects", "Subjects")
        days_left = (exam_date - today).days
        grouping = (
            "Give one '### ' heading per day."
            if days_left <= 21
            else "The exam is more than three weeks away, so give one '### ' heading per WEEK, "
                 "then switch to one heading per day for the final 7 days."
        )
        system_prompt = (
            "You are ScholarSync's Exam Planner. Create a structured study calendar running from "
            f"today until the day before the exam ({days_left} days). {grouping} "
            "Give more time to subjects with lower confidence (1 = weakest, 5 = strongest) and "
            "schedule review sessions before the exam. "
            "The student is in school: on Monday-Friday schedule at most 2-3 hours of study, "
            "starting after 4 PM. Saturdays and Sundays can have up to 5 hours. "
            "Keep each day to 4-6 bullets. Only use study methods that fit each subject "
            "(for example, no DBQs outside history)."
        )
        user_content = (
            f"Exam Start Date: {_pretty(exam_date)}\n"
            f"Subjects & Confidence:\n{subjects}"
        )

    elif mode == "milestone":
        title = _text(user_input, "title", "Project title")
        requirements = _text(user_input, "requirements", "Requirements")
        deadline = _future_date(user_input, "deadline", "Deadline", today)
        system_prompt = (
            "You are ScholarSync's Project Assistant. Break the project down into clear, incremental "
            "micro-deadlines between today and the final deadline, grouped into phases. "
            "Then suggest 2-3 starting resources. For resources, ONLY name well-known books, "
            "databases (JSTOR, Google Scholar, the school library), or useful search terms. "
            "Never invent specific articles, podcast episodes, websites, or URLs."
        )
        user_content = (
            f"Project Title: {title}\nRequirements: {requirements}\n"
            f"Deadline: {_pretty(deadline)}"
        )

    else:  # booster
        tasks = _text(user_input, "tasks", "Tasks")
        hours_raw = _text(user_input, "hours", "Available hours")
        try:
            hours = float(hours_raw)
        except ValueError:
            raise InputError("Available hours must be a number.")
        if not 0.5 <= hours <= 16:
            raise InputError("Available hours must be between 0.5 and 16.")
        now = _current_time(user_input)
        start_rule = (
            f" The current time is {now}. Start the schedule at the next quarter hour after "
            f"{now}, never earlier."
            if now else ""
        )
        system_prompt = (
            "You are ScholarSync's Daily Booster. Take the tasks and time available, then create a "
            "highly actionable micro-schedule for today using techniques like Pomodoro, with breaks. "
            "Activities that don't suit Pomodoro (music practice, exercise) can be one longer block."
            + start_rule
        )
        user_content = f"Tasks: {tasks}\nAvailable Time: {hours:g} hours"

    return system_prompt + date_info + FORMAT_RULES, user_content


def generate_productivity_data(mode, user_input, today):
    """Return the AI's markdown plan. Raises InputError for bad input, other exceptions for service failures."""
    system_prompt, user_content = build_prompt(mode, user_input, today)

    client = get_groq_client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.6,
        max_completion_tokens=8192,
        reasoning_effort="low",
    )

    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    if not text:
        raise RuntimeError("The model returned an empty response.")
    if choice.finish_reason == "length":
        text += TRIMMED_NOTE
    return text