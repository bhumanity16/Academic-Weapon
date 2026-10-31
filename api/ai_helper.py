import json
import os
import re
from datetime import date, timedelta

from dotenv import load_dotenv
from groq import Groq

# Load local environment variables from .env file
load_dotenv()

# Read from Vercel env so the model can be swapped without a code change
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_FIELD_CHARS = 2000
# Room for 12 subjects, each with what's on its test
MAX_SUBJECTS_CHARS = 4000
MODES = ("planner", "milestone", "booster")
MAX_PROJECTS = 5
# A Daily Booster stop time earlier than this (in minutes) is treated as after midnight
LATEST_PAST_MIDNIGHT = 5 * 60
# From 8:30 PM a long-range plan starts tomorrow, unless the deadline is too close to wait
TOO_LATE_TO_START = 20 * 60 + 30

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


def _text(user_input, key, label, limit=MAX_FIELD_CHARS):
    value = str(user_input.get(key) or "").strip()
    if not value:
        raise InputError(f"{label} is required.")
    if len(value) > limit:
        raise InputError(f"{label} is too long (max {limit} characters).")
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


def _projects(user_input, today):
    """The validated projects as (title, deadline, requirements). Requirements are optional."""
    # A single project at the top level is what pages loaded before multi-project support send
    raw = user_input.get("projects", [user_input])
    if not isinstance(raw, list) or not raw:
        raise InputError("Add at least one project.")
    if len(raw) > MAX_PROJECTS:
        raise InputError(f"Plan at most {MAX_PROJECTS} projects at a time.")
    projects = []
    for i, project in enumerate(raw, 1):
        if not isinstance(project, dict):
            raise InputError("Project details are invalid.")
        name = "Project" if len(raw) == 1 else f"Project {i}"
        title = _text(project, "title", f"{name} name")
        deadline = _future_date(project, "deadline", f"{name} deadline", today)
        requirements = str(project.get("requirements") or "").strip()
        if len(requirements) > MAX_FIELD_CHARS:
            raise InputError(f"{name} requirements are too long (max {MAX_FIELD_CHARS} characters).")
        projects.append((title, deadline, requirements))
    return projects


def _clock(value):
    """'HH:MM' as minutes after midnight, or None if it isn't a valid 24-hour time."""
    value = str(value or "").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        return None
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _current_time(user_input):
    """The student's local time as HH:MM (sent by the browser), or None if missing/invalid."""
    value = str(user_input.get("current_time") or "").strip()
    return value if _clock(value) is not None else None


def _twelve_hour(now):
    """'18:40' as '6:40 PM', so the plan copies a format students read."""
    hours, minutes = divmod(_clock(now), 60)
    return f"{hours % 12 or 12}:{minutes:02d} {'AM' if hours < 12 else 'PM'}"


def _today_rule(now, today, days_to_deadline):
    """Stops long-range plans scheduling today's tasks at a time that has already passed."""
    if not now:
        return ""
    if _clock(now) >= TOO_LATE_TO_START and days_to_deadline >= 2:
        tomorrow = _pretty(today + timedelta(days=1))
        return f" It is already {_twelve_hour(now)}, too late to start today. Schedule nothing today; begin on {tomorrow}."
    return (
        f" The current time is {_twelve_hour(now)}. Any task scheduled today must start after that time, "
        "never earlier, and today's list should only hold what still fits this evening. "
        "Write times as AM/PM, and don't mention the current time in headings."
    )


def _minutes_until(now, end):
    """Minutes from now until the stop time. A stop time before 5 AM counts as after midnight."""
    gap = _clock(end) - _clock(now)
    if gap <= 0 and _clock(end) < LATEST_PAST_MIDNIGHT:
        gap += 24 * 60
    return gap


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
        exam_date = _future_date(user_input, "exam_date", "Exam date", today)
        subjects = _text(user_input, "subjects", "Subjects", MAX_SUBJECTS_CHARS)
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
            "Build each subject's tasks around what the student says is on the test, and cover every "
            "topic they list. Where it says 'not given', use general study tasks for that subject and "
            "never invent specific chapters, units or topics. "
            "The student is in school: on Monday-Friday schedule at most 2-3 hours of study, "
            "starting after 4 PM. Saturdays and Sundays can have up to 5 hours. "
            "Keep each day to 4-6 bullets. Only use study methods that fit each subject "
            "(for example, no DBQs outside history)."
            + _today_rule(_current_time(user_input), today, days_left)
        )
        user_content = (
            f"Exam Date: {_pretty(exam_date)}\n"
            f"Subjects & Confidence:\n{subjects}"
        )

    elif mode == "milestone":
        projects = _projects(user_input, today)
        together = (
            " The student has several projects at once: make one combined timeline in date order, "
            "start each project with its name in every task, and spread the work so no day is overloaded."
            if len(projects) > 1 else ""
        )
        system_prompt = (
            "You are ScholarSync's Project Assistant. Break the project down into clear, incremental "
            "micro-deadlines between today and the final deadline, grouped into phases."
            + together +
            " If a project lists no requirements, use sensible general steps for that kind of project "
            "and never invent rules such as word counts or citation styles. "
            "Then suggest 2-3 starting resources. For resources, ONLY name well-known books, "
            "databases (JSTOR, Google Scholar, the school library), or useful search terms. "
            "Never invent specific articles, podcast episodes, websites, or URLs."
            + _today_rule(_current_time(user_input), today, min((deadline - today).days for _, deadline, _ in projects))
        )
        user_content = "\n\n".join(
            f"Project Title: {title}\nRequirements: {requirements or 'None given'}\n"
            f"Deadline: {_pretty(deadline)}"
            for title, deadline, requirements in projects
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
        end = _text(user_input, "end_time", "Stop time")
        if _clock(end) is None:
            raise InputError("Stop time must be a valid time (HH:MM).")
        now = _current_time(user_input)

        time_rule = ""
        if now:
            window = _minutes_until(now, end)
            # Leave a little room for rounding up to the next quarter hour
            if window < 30:
                raise InputError(f"Your stop time ({end}) has passed or is less than 30 minutes away. Pick a later one.")
            fits = min(hours, (window // 15) / 4)
            time_rule = (
                f" The current time is {now}. Start the schedule at the next quarter hour after "
                f"{now}, never earlier."
            )
            if fits < hours:
                time_rule += (
                    f" Only about {fits:g} of the student's {hours:g} hours fit before their stop time. "
                    "Start the answer with one short sentence telling them so, addressed to them as 'you'. "
                    "Put the most important tasks first. If any task gets no time at all, list it under "
                    "a final '### Didn't fit today' heading; otherwise leave that heading out."
                )
                hours = fits
        time_rule += (
            f" The student must stop by {end}. Nothing may be scheduled at or after {end}, "
            "not even a break."
        )

        system_prompt = (
            "You are ScholarSync's Daily Booster. Take the tasks and time available, then create a "
            "highly actionable micro-schedule for today using techniques like Pomodoro, with breaks. "
            "Activities that don't suit Pomodoro (music practice, exercise) can be one longer block."
            + time_rule
        )
        user_content = f"Tasks: {tasks}\nAvailable Time: {hours:g} hours\nStop By: {end}"

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

# ---------- turning a finished plan into calendar events ----------

MAX_PLAN_CHARS = 30000
MAX_EVENTS = 250
MAX_TITLE_CHARS = 120
# Events further out than this are almost certainly a misread date
MAX_DAYS_AHEAD = 400

EXTRACT_PROMPT = (
    "You convert a student's study plan into calendar events. Return ONLY a JSON object of the form "
    '{"events": [{"date": "YYYY-MM-DD", "start": "HH:MM" or null, "end": "HH:MM" or null, "title": "..."}]}. '
    "Rules: one event per scheduled task or study block; skip breaks, tips, resource lists and summaries. "
    "Use 24-hour times. If a task has no time, set start and end to null. "
    "If a heading covers a whole week or phase without naming a day, put its tasks on the first day it covers. "
    "Titles are short (under 80 characters), e.g. 'Math: 10 practice problems'. "
    "Never invent tasks that aren't in the plan."
)


def _hhmm(value):
    """'9:05' or '09:05' as '09:05', or None if it isn't a valid 24-hour time."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    padded = f"{int(match[1]):02d}:{match[2]}" if match else None
    return padded if _clock(padded) is not None else None


def _clean_event(raw, today):
    """One validated event dict, or None if the model's entry is unusable."""
    if not isinstance(raw, dict):
        return None
    try:
        day = date.fromisoformat(str(raw.get("date")))
    except ValueError:
        return None
    if not today - timedelta(days=1) <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
        return None
    title = " ".join(str(raw.get("title") or "").split())[:MAX_TITLE_CHARS]
    if not title:
        return None
    start = _hhmm(raw.get("start"))
    end = _hhmm(raw.get("end")) if start else None
    # Drop an end time that isn't after the start (e.g. a block that runs past midnight)
    if end and _clock(end) <= _clock(start):
        end = None
    return {"date": day.isoformat(), "start": start, "end": end, "title": title}


def extract_events(plan, today):
    """Turn a plan's markdown into a sorted list of dated events. Raises InputError for bad input."""
    if not isinstance(plan, str) or not plan.strip():
        raise InputError("There's no plan to add.")
    if len(plan) > MAX_PLAN_CHARS:
        raise InputError("This plan is too long to add to the calendar.")

    client = get_groq_client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT + f" Today is {_pretty(today)} ({today.isoformat()})."},
            {"role": "user", "content": plan},
        ],
        temperature=0,
        max_completion_tokens=8192,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )

    try:
        raw_events = json.loads(response.choices[0].message.content or "{}").get("events")
    except (json.JSONDecodeError, AttributeError):
        raw_events = None
    if not isinstance(raw_events, list):
        raise RuntimeError("The model didn't return a list of events.")

    events = [e for e in (_clean_event(r, today) for r in raw_events[:MAX_EVENTS]) if e]
    if not events:
        raise InputError("Couldn't find any dated tasks in this plan.")
    return sorted(events, key=lambda e: (e["date"], e["start"] or ""))
