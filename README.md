# ScholarSync

An AI study planner for high school students. ScholarSync turns exam dates, big projects and today's to-do list into realistic schedules, and can add them to Google Calendar.

**Live site:** https://scholarsync-kappa.vercel.app/

I once had just one exam the next week, and I underestimated how much studying it would take. I'd already made plans with my friends, and I ended up having to back out of them. I built ScholarSync so students can see how much work they actually have ahead of them, and plan around it.

## What it does

| Planner | You enter | You get |
| --- | --- | --- |
| **Exam plan** | Exam date, plus what's on the test and a 1–5 confidence slider for each subject | A study calendar from today until the exam. Weaker subjects get more time, with review sessions before the exam. |
| **Project plan** | One or more projects, each with a name, a deadline and optional requirements | One timeline that spreads the work across dated phases, plus real places to start researching (JSTOR, Google Scholar, the school library). |
| **Today's plan** | Today's tasks, free hours and a stop time | A Pomodoro-style schedule for the rest of today that never runs past your stop time. |

Every plan can go on a calendar, with no sign-in needed:

- **Your calendar** is built into the site: a month view where you can tick tasks off or remove them. It's saved in your browser, not on a server.
- **Download (.ics)** exports the plan as a standard calendar file that opens in Apple Calendar, Google Calendar or Outlook.
- **Google Calendar sync** is optional: if you sign in, a plan can be sent straight to your Google Calendar.

## How it works

- **Backend:** Python and Flask, running as a serverless function on Vercel (`api/index.py`).
- **AI:** the Groq API (`api/ai_helper.py`). The model can be swapped with the `GROQ_MODEL` environment variable.
- **Frontend:** a single page (`templates/index.html`) with custom CSS. Plans come back as Markdown, are rendered with marked, and are sanitized with DOMPurify.
- **Calendar:** a second AI call turns a finished plan into dated events (JSON), and the server checks every date and time before returning them. The page stores the events in `localStorage` and builds `.ics` files itself.
- **Google:** OAuth 2.0 sign-in (with PKCE and state checks) and the Calendar API.

## Design decisions

- **The code does the date and time math, not the AI.** Language models often get weekdays wrong, so the server works out the date, weekdays, days left and how much time fits before the stop time, and passes those facts to the model.
- **Realistic for a school day.** On weekdays, study starts after 4 PM and stays around 2–3 hours. Weekends allow more. Plans for exams more than three weeks away are grouped by week, then switch to day-by-day for the final week.
- **No made-up sources.** The project planner may only point to well-known databases, books or search terms, never specific articles or URLs that could be invented.
- **No database.** Nothing about the user is stored on the server. Only the Google access token, which lasts about an hour, is kept in a signed cookie. The calendar, streak and XP live in the browser's local storage.
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
