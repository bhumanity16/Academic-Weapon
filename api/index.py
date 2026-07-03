import os
import sys
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import httpx
from datetime import datetime, timedelta
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from werkzeug.middleware.proxy_fix import ProxyFix

# FIX VERCEL PROXY TRAP: Disable oauthlib HTTPS validation guardrails globally
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Ensuring relative system paths match perfectly during Vercel's serverless compilation
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ai_helper import generate_productivity_data

# Explicitly mapping where static templates are kept
base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, '..', 'templates')

app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "scholarsync-secure-session-key-fallback")

# Instruct Flask to trust proxy headers sent by Vercel routers
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Dynamic inline scopes configuration mapping
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.events"
]

def get_google_auth_flow():
    # DYNAMIC REDIRECT RESOLUTION: Detect the exact current deployment URL automatically
    try:
        dynamic_fallback = url_for('callback', _external=True)
    except Exception:
        dynamic_fallback = "http://localhost:5000/callback"

    # Read from environment variables if set, otherwise use the dynamically generated current domain context
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", dynamic_fallback)

    # Force HTTPS schema when running under serverless deployment scopes
    if os.getenv("VERCEL") and redirect_uri.startswith("http://"):
        redirect_uri = redirect_uri.replace("http://", "https://")

    client_config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "project_id": os.getenv("GOOGLE_PROJECT_ID", "scholarsync-ai"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "redirect_uris": [redirect_uri]
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

def create_calendar_event(credentials_dict, mode, user_input, ai_response):
    try:
        creds = Credentials(
            token=credentials_dict['token'],
            refresh_token=credentials_dict.get('refresh_token'),
            token_uri=credentials_dict['token_uri'],
            client_id=credentials_dict['client_id'],
            client_secret=credentials_dict['client_secret'],
            scopes=credentials_dict['scopes']
        )
        service = build('calendar', 'v3', credentials=creds)

        summary = "ScholarSync Strategy Blueprint"
        event_date = None

        if mode == "planner":
            summary = "ScholarSync: Exam Preparation Plan"
            event_date = user_input.get('exam_date')
        elif mode == "milestone":
            summary = f"ScholarSync Project Deadline: {user_input.get('title', 'Project')}"
            event_date = user_input.get('deadline')
        elif mode == "booster":
            summary = "ScholarSync: Daily Booster Schedule"
            event_date = datetime.now().strftime("%Y-%m-%d")

        if not event_date:
            event_date = datetime.now().strftime("%Y-%m-%d")

        try:
            dt = datetime.strptime(event_date, "%Y-%m-%d")
            end_dt = dt + timedelta(days=1)
            end_date_str = end_dt.strftime("%Y-%m-%d")
        except Exception:
            end_date_str = event_date

        event = {
            'summary': summary,
            'description': ai_response,
            'start': {
                'date': event_date
            },
            'end': {
                'date': end_date_str
            }
        }

        service.events().insert(calendarId='primary', body=event).execute()
        return True
    except Exception as e:
        print(f"Google Calendar Synchronization Exception: {str(e)}")
        return False

@app.route('/')
def home():
    return render_template('index.html', user=session.get('user'))

@app.route('/login')
def login():
    try:
        flow = get_google_auth_flow()
        authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
        session['oauth_state'] = state
        return redirect(authorization_url)
    except Exception as e:
        return jsonify({
            "error": "Failed to initiate Google OAuth configuration flow",
            "details": str(e),
            "hint": "Check that GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are explicitly configured in Vercel settings."
        }), 500

@app.route('/callback')
def callback():
    try:
        flow = get_google_auth_flow()

        authorization_response = request.url
        if "http://" in authorization_response and os.getenv("VERCEL"):
            authorization_response = authorization_response.replace("http://", "https://")

        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials

        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }

        headers = {"Authorization": f"Bearer {credentials.token}"}
        try:
            with httpx.Client() as client:
                resp = client.get("https://www.googleapis.com/oauth2/v2/userinfo", headers=headers)
                if resp.status_code == 200:
                    session['user'] = resp.json()
        except Exception as e:
            print(f"Error encountered during user profile sync execution: {str(e)}")

        return redirect(url_for('home'))
        
    except Exception as e:
        return jsonify({
            "error": "OAuth Callback Token Exchange Denied",
            "details": str(e),
            "hint": "Verify that your Google Cloud Console contains the exact callback URL structure."
        }), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/api/generate', methods=['POST'])
def generate():
    data = request.json
    mode = data.get('mode')
    user_input = data.get('input_data')

    if not mode or not user_input:
        return jsonify({"error": "Missing selection parameters or input details"}), 400

    current_date_str = datetime.now().strftime("%Y-%m-%d")
    ai_response = generate_productivity_data(mode, user_input, current_date=current_date_str)

    is_logged_in = 'credentials' in session
    return jsonify({
        "result": ai_response,
        "is_logged_in": is_logged_in
    })

@app.route('/api/sync-calendar', methods=['POST'])
def sync_calendar():
    if 'credentials' not in session:
        return jsonify({"error": "User unauthorized or Google Account session unlinked"}), 401

    data = request.json
    mode = data.get('mode')
    user_input = data.get('input_data')
    ai_response = data.get('ai_response')

    sync_success = create_calendar_event(session['credentials'], mode, user_input, ai_response)
    if sync_success:
        return jsonify({"success": True, "message": "This target blueprint schedule has been successfully pushed to your main feed!"})
    else:
        return jsonify({"success": False, "error": "Internal synchronization error or schema verification failure."})

if __name__ == '__main__':
    app.run(debug=True)
