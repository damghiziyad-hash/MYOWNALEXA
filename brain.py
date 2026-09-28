import os
from dotenv import load_dotenv
from google import genai
import re
from google.genai import types
import tools
 
load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
 
MODEL = "gemini-2.5-flash-lite"
 
SYSTEM_INSTRUCTION = (
    "You are MYOWNALEXA, a voice assistant running on the user's Ubuntu PC. "
    "Use the provided tools to perform real actions on the computer. "
    "Your answers are read aloud: keep them short, in plain text, no markdown. "
    "Reply in the language the user speaks. "
    "Only report an action as done if the tool result starts with SUCCESS. "
    "If a tool returns CONFIRMATION_REQUIRED, ask the user to confirm and stop there."
)
 
# A chat session keeps the conversation history and lets the SDK run the tool
# calls automatically (Gemini -> tool -> result -> Gemini).
_chat = client.chats.create(
    model=MODEL,
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=tools.TOOLS,
    ),
)
 
# The user's confirmation is interpreted here, in Python, never by the model.
_YES_WORDS = {"yes", "yeah", "yep", "ok", "okay", "confirm", "oui", "confirme", "d'accord"}
_NO_WORDS = {"no", "nope", "cancel", "non", "annule", "annuler"}
 
 
def _spoken(result: str) -> str:
    """'SUCCESS: Volume set.' -> 'Volume set.'"""
    return result.split(": ", 1)[-1]
 
 
def _handle_confirmation(text: str) -> str | None:
    """Resolve a pending dangerous action. Return the reply, or None to continue normally."""
    words = set(re.findall(r"[\w']+", text.lower()))
    if words & _NO_WORDS:  # checked first: "no" always wins
        return _spoken(tools.cancel_pending_action())
    if words & _YES_WORDS:
        return _spoken(tools.confirm_pending_action())
    tools.cancel_pending_action()  # any other sentence cancels the pending action
    return None
 
 
def ask(text: str) -> str:
    if tools.has_pending_action():
        reply = _handle_confirmation(text)
        if reply is not None:
            return reply
    try:
        response = _chat.send_message(text)
    except Exception as exc:  # network / quota errors must not crash the voice loop
        print(f"Gemini error: {exc}")
        return "Sorry, I could not reach Gemini."
    return response.text or "Done."
 