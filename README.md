# ScholarSync

An AI study planner for high school students. ScholarSync turns exam dates, big projects and today's to-do list into realistic schedules, and can add them to Google Calendar.

**Live site:** https://scholarsync-kappa.vercel.app/

## What it does

| Planner | You enter | You get |
| --- | --- | --- |
| **Study Planner** | Exam date and a 1–5 confidence level per subject | A study calendar from today until the exam. Weaker subjects get more time, with review sessions before the exam. |
| **Milestone Matrix** | Project name, requirements and deadline | The project split into dated phases, plus real places to start researching (JSTOR, Google Scholar, the school library). |
| **Daily Booster** | Today's tasks, free hours and a stop time | A Pomodoro-style schedule for the rest of today that never runs past your stop time. |

If you're signed in with Google, any plan can be added to your calendar as one all-day event.

## How it works

- **Backend:** Python and Flask, running as a serverless function on Vercel (`api/index.py`).
- **AI:** the Groq API (`api/ai_helper.py`). The model can be swapped with the `GROQ_MODEL` environment variable.
- **Frontend:** a single page (`templates/index.html`) using Tailwind CSS. Plans come back as Markdown, are rendered with marked, and are sanitized with DOMPurify.
- **Google:** OAuth 2.0 sign-in (with PKCE and state checks) and the Calendar API.

## Design decisions

- **The code does the date and time math, not the AI.** Language models often get weekdays wrong, so the server works out the date, weekdays, days left and how much time fits before the stop time, and passes those facts to the model.
- **Realistic for a school day.** On weekdays, study starts after 4 PM and stays around 2–3 hours. Weekends allow more. Plans for exams more than three weeks away are grouped by week, then switch to day-by-day for the final week.
- **No made-up sources.** The project planner may only point to well-known databases, books or search terms, never specific articles or URLs that could be invented.
- **No database.** Nothing about the user is stored on the server. Only the Google access token, which lasts about an hour, is kept in a signed cookie. Streak and XP live in the browser's local storage.
- **Validated twice.** The browser checks inputs for quick feedback, and the server checks them again. Requests are rate-limited.

## Running it locally

1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
   ```
2. Create a `.env` file:
   ```
   GROQ_API_KEY=...
   FLASK_SECRET_KEY=...        # python -c "import secrets; print(secrets.token_hex(32))"
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   GOOGLE_REDIRECT_URI=http://localhost:5000/callback
   ```
3. Start the server and open http://localhost:5000:
   ```bash
   python api/index.py
   ```
