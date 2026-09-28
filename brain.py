import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def ask(text):
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=text,
    )
    return response.text