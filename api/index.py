import os
import sys
from datetime import date, datetime, timedelta

import httpx
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from werkzeug.middleware.proxy_fix import ProxyFix

# Ensuring relative system paths match perfectly during Vercel's serverless compilation
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ai_helper import MODES, InputError, extract_events, generate_productivity_data

load_dotenv()

ON_VERCEL = os.getenv("VERCEL") is not None

# Plain-HTTP OAuth is only allowed for local development
if not ON_VERCEL:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
# Google may return the granted scopes in a different order (or with previously granted extras)
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# Explicitly mapping where static templates are kept
base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, "..", "templates")

app = Flask(__name__, template_folder=template_dir)
# Vercel terminates HTTPS at its proxy; trust its forwarded headers so request.url is https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY must be set. Generate one with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL,
)

# In-memory limits are per serverless instance; set RATELIMIT_STORAGE_URI (e.g. a Redis URL) for global limits
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    CALENDAR_SCOPE,
]
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
# Google Calendar rejects very long descriptions; keep a safe margin
CALENDAR_DESCRIPTION_LIMIT = 8000


def get_redirect_uri():
    return os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/callback")


def get_google_auth_flow(**kwargs):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set.")

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [get_redirect_uri()],
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=get_redirect_uri(), **kwargs)


def client_today(data):
    """The student's local date (sent by the browser), falling back to the server's date."""
    try:
        return date.fromisoformat(str(data.get("client_date")))
    except (TypeError, ValueError):
        return date.today()


def load_credentials():
    """Rebuild the short-lived Google access token from the session, or None if missing/expired."""
    data = session.get("credentials")
    if not data:
        return None
    expiry = datetime.fromisoformat(data["expiry"]) if data.get("expiry") else None
    creds = Credentials(token=data["token"], expiry=expiry, scopes=data.get("scopes"))
    if creds.expired:
        # No refresh token is stored, so the user has to sign in again
        session.clear()
        return None
    return creds


def can_sync_calendar():
    creds = load_credentials()
    return creds is not None and CALENDAR_SCOPE in (creds.scopes or [])


def create_calendar_event(creds, mode, user_input, plan, today):
    """Insert one all-day event holding the plan. Returns the event's Google Calendar link."""
    if mode == "planner":
        summary = "ScholarSync: Exam Preparation Plan"
        event_date = user_input.get("exam_date")
    elif mode == "milestone":
        title = str(user_input.get("title") or "Project").strip()[:150]
        summary = f"ScholarSync Project Deadline: {title}"
        event_date = user_input.get("deadline")
    else:
        summary = "ScholarSync: Daily Booster Schedule"
        event_date = None

    try:
        start = date.fromisoformat(str(event_date))
    except (TypeError, ValueError):
        start = today

    description = plan
    if len(description) > CALENDAR_DESCRIPTION_LIMIT:
        description = description[:CALENDAR_DESCRIPTION_LIMIT] + "\n\n…(trimmed — see ScholarSync for the full plan)"

    event = {
        "summary": summary,
        "description": description,
        # All-day events use an exclusive end date, so it must be the following day
        "start": {"date": start.isoformat()},
        "end": {"date": (start + timedelta(days=1)).isoformat()},
    }

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    created = service.events().insert(calendarId="primary", body=event).execute()
    return created.get("htmlLink")


@app.errorhandler(429)
def rate_limited(_error):
    return jsonify({"error": "Too many requests. Please wait a minute and try again."}), 429


@app.route("/")
def home():
    return render_template("index.html", user=session.get("user"))


@app.route("/login")
def login():
    flow = get_google_auth_flow(autogenerate_code_verifier=True)
    authorization_url, state = flow.authorization_url(
        include_granted_scopes="true",
        prompt="select_account",
    )
    # Both are needed again in /callback: state blocks CSRF, the verifier completes PKCE
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(authorization_url)


@app.route("/callback")
def callback():
    expected_state = session.pop("oauth_state", None)
    code_verifier = session.pop("code_verifier", None)

    if request.args.get("error"):
        flash("Google sign-in was cancelled.")
        return redirect(url_for("home"))
    if not expected_state or request.args.get("state") != expected_state:
        flash("Your sign-in link expired. Please try signing in again.")
        return redirect(url_for("home"))

    flow = get_google_auth_flow(state=expected_state, code_verifier=code_verifier)
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception:
        app.logger.exception("Google OAuth token exchange failed")
        flash("Couldn't complete Google sign-in. Please try again.")
        return redirect(url_for("home"))

    credentials = flow.credentials
    # Users can untick individual scopes on Google's consent screen, so record what was actually granted
    granted = getattr(credentials, "granted_scopes", None) or credentials.scopes or []
    if isinstance(granted, str):
        granted = granted.split()
    # Only the short-lived access token goes in the (signed, not encrypted) cookie:
    # no refresh token and never the client secret.
    session["credentials"] = {
        "token": credentials.token,
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        "scopes": list(granted),
    }

    try:
        resp = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {credentials.token}"}, timeout=10)
        resp.raise_for_status()
        info = resp.json()
        session["user"] = {"name": info.get("name"), "email": info.get("email"), "picture": info.get("picture")}
    except httpx.HTTPError:
        app.logger.exception("Fetching the Google user profile failed")
        session["user"] = {"name": None, "email": None, "picture": None}

    if CALENDAR_SCOPE not in session["credentials"]["scopes"]:
        flash("Signed in, but calendar access wasn't granted, so plans can't be synced. Sign in again to allow it.")

    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/api/generate", methods=["POST"])
@limiter.limit("10 per minute;60 per day")
def generate():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    user_input = data.get("input_data")

    if mode not in MODES or not isinstance(user_input, dict):
        return jsonify({"error": "Missing or invalid mode or input details."}), 400

    try:
        ai_response = generate_productivity_data(mode, user_input, today=client_today(data))
    except InputError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        app.logger.exception("AI generation failed")
        return jsonify({"error": "The AI service is unavailable right now. Please try again in a minute."}), 502

    return jsonify({
        "result": ai_response,
        "is_logged_in": "user" in session,
        "can_sync": can_sync_calendar(),
    })


@app.route("/api/calendar-events", methods=["POST"])
@limiter.limit("10 per minute;60 per day")
def calendar_events():
    """Turn a generated plan into dated events for the in-browser calendar and .ics download."""
    data = request.get_json(silent=True) or {}
    try:
        events = extract_events(data.get("plan"), today=client_today(data))
    except InputError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        app.logger.exception("Event extraction failed")
        return jsonify({"error": "Couldn't turn this plan into calendar events. Please try again."}), 502
    return jsonify({"events": events})


@app.route("/api/sync-calendar", methods=["POST"])
@limiter.limit("10 per minute")
def sync_calendar():
    creds = load_credentials()
    if creds is None:
        return jsonify({"success": False, "reauth": True,
                        "error": "Your Google session has expired. Please sign in again."}), 401
    if CALENDAR_SCOPE not in (creds.scopes or []):
        return jsonify({"success": False, "reauth": True,
                        "error": "Calendar access wasn't granted. Sign in again and allow it."}), 403

    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    user_input = data.get("input_data")
    plan = data.get("ai_response")

    if mode not in MODES or not isinstance(user_input, dict) or not isinstance(plan, str) or not plan.strip():
        return jsonify({"success": False, "error": "Missing or invalid plan details."}), 400

    try:
        link = create_calendar_event(creds, mode, user_input, plan, client_today(data))
    except RefreshError:
        session.clear()
        return jsonify({"success": False, "reauth": True,
                        "error": "Your Google session has expired. Please sign in again."}), 401
    except HttpError as e:
        if e.resp.status == 401:
            session.clear()
            return jsonify({"success": False, "reauth": True,
                            "error": "Your Google session has expired. Please sign in again."}), 401
        app.logger.exception("Google Calendar insert failed")
        return jsonify({"success": False, "error": "Google Calendar rejected the event. Please try again."}), 502
    except Exception:
        app.logger.exception("Google Calendar sync failed")
        return jsonify({"success": False, "error": "Couldn't reach Google Calendar. Please try again."}), 502

    return jsonify({"success": True, "message": "Your plan was added to your Google Calendar.", "link": link})


if __name__ == "__main__":
    app.run(debug=True)
