import speech_recognition as sr
import pyttsx3
from google import genai



def speak(text):
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()

def listen():
    """listens to microphone and returns text"""
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening...")
        recognizer.adjust_for_ambient_noise(source)
        audio = recognizer.listen(source)

        try:
            command = recognizer.recognize_google(audio) # type : ignore
            return command.lower()
        except sr.UnknownValueError:
            print(f"The audio has not been recognized")
            return
        except sr.RequestError:
            print("check your internet connection")
            return
