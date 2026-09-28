import os
import subprocess
import webbrowser
import datetime
from google.genai import types

ALLOWED_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
}


def open_app(name: str) -> str:
    """Open an application on the computer. Supported: notepad, calculator, paint."""
    exe = ALLOWED_APPS.get(name.lower())
    if not exe:
        return f"I can't open '{name}'. Supported: {', '.join(ALLOWED_APPS)}"
    subprocess.Popen(exe)
    return f"Opened {name}"

def search_web(query: str) -> str:
    """Search Google for the given query in the default browser."""
    webbrowser.open(f"https://www.google.com/search?q={query}")
    return f"Searching for {query}"

def open_website(url: str) -> str:
    """Open a website in the default browser. url must start with https://"""
    if not url.startswith("https://"):
        return "Only https URLs are allowed"
    webbrowser.open(url)
    return f"Opened {url}"

def get_time() -> str:
    """Get the current date and time."""
    return datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")

# ---------- AGENT ----------
config = types.GenerateContentConfig(
    system_instruction=(
        "You are Alexa-like voice assistant. Keep answers short (1-2 sentences) "
        "because they are spoken aloud. Use the tools when the user asks for an action."
    ),
    tools=[open_app, search_web, open_website, get_time],
)

