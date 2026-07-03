import os
from groq import Groq
from dotenv import load_dotenv

# Load local environment variables from .env file
load_dotenv()

def get_groq_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("CRITICAL: GROQ_API_KEY is missing from your environment variables!")
    return Groq(api_key=api_key)

def generate_productivity_data(mode, user_input, current_date=None):
    try:
        client = get_groq_client()

        # Inject relative context boundaries so dates read logically to the student
        date_info = f" Today's date is {current_date}." if current_date else ""

        if mode == "planner":
            system_prompt = f"You are AcademicWeapons's Exam Planner. Create a structured day-by-day study calendar based on user specs. Prioritize weaker subjects.{date_info}"
            user_content = f"Exam Start Date: {user_input.get('exam_date')}\nSubjects & Confidence: {user_input.get('subjects')}"

        elif mode == "milestone":
            system_prompt = f"You are AcademicWeapons's Project Assistant. Break the project down into clear, incremental micro-deadlines and suggest 2-3 creative starting resources.{date_info}"
            user_content = f"Project Title: {user_input.get('title')}\nRequirements: {user_input.get('requirements')}\nDeadline: {user_input.get('deadline')}"

        elif mode == "booster":
            system_prompt = f"You are AcademicWeapons's Daily Booster. Take the tasks and time available, then create a highly actionable micro-schedule using productivity methodologies like Pomodoro.{date_info}"
            user_content = f"Tasks: {user_input.get('tasks')}\nAvailable Time: {user_input.get('hours')} hours"

        else:
            return "Invalid operational mode selected."

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            temperature=0.6,
        )
        return response.choices[0].message.content

    except Exception as e:
        return f"Backend Error: {str(e)}"