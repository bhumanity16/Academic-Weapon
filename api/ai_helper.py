import os
from datetime import date

from dotenv import load_dotenv
from groq import Groq

# Load local environment variables from .env file
load_dotenv()

MODEL = "llama-3.1-8b-instant"
MAX_FIELD_CHARS = 2000
MODES = ("planner", "milestone", "booster")

# Shared output rules so every mode renders nicely with the frontend's markdown styles
FORMAT_RULES = (
    " Format the answer in Markdown. Use a '### ' heading for each day or phase, "
    "followed by a bullet list of concrete tasks with times or durations. "
    "Be realistic for a high school student, keep it concise, and never output raw HTML."
)


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
    return parsed.isoformat()


def build_prompt(mode, user_input, today):
    if mode not in MODES:
        raise InputError("Invalid mode selected.")
    if not isinstance(user_input, dict):
        raise InputError("Input data must be an object.")

    date_info = f" Today's date is {today.isoformat()}."

    if mode == "planner":
        exam_date = _future_date(user_input, "exam_date", "Exam start date", today)
        subjects = _text(user_input, "subjects", "Subjects")
        system_prompt = (
            "You are ScholarSync's Exam Planner. Create a structured day-by-day study calendar "
            "running from today until the exam start date. Give more time to subjects with lower "
            "confidence (1 = weakest, 5 = strongest) and schedule review sessions before the exam."
        )
        user_content = f"Exam Start Date: {exam_date}\nSubjects & Confidence:\n{subjects}"

    elif mode == "milestone":
        title = _text(user_input, "title", "Project title")
        requirements = _text(user_input, "requirements", "Requirements")
        deadline = _future_date(user_input, "deadline", "Deadline", today)
        system_prompt = (
            "You are ScholarSync's Project Assistant. Break the project down into clear, incremental "
            "micro-deadlines between today and the final deadline, then suggest 2-3 creative "
            "starting resources."
        )
        user_content = f"Project Title: {title}\nRequirements: {requirements}\nDeadline: {deadline}"

    else:  # booster
        tasks = _text(user_input, "tasks", "Tasks")
        hours_raw = _text(user_input, "hours", "Available hours")
        try:
            hours = float(hours_raw)
        except ValueError:
            raise InputError("Available hours must be a number.")
        if not 0.5 <= hours <= 16:
            raise InputError("Available hours must be between 0.5 and 16.")
        system_prompt = (
            "You are ScholarSync's Daily Booster. Take the tasks and time available, then create a "
            "highly actionable micro-schedule for today using techniques like Pomodoro, with breaks."
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
        max_tokens=2048,
    )
    return response.choices[0].message.content
