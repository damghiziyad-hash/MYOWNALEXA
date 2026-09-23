import speech_recognition as sr
import pyttsx3


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
            command = recognizer.recognize_google(audio)
            return command.lower()
        except sr.UnknownValueError:
            print(f"The audio has not been recognized")
            return
        except sr.RequestError:
            print("check your internet connection")
            return


def process_command(command):
    print("this will be replaced soon")
    
def main():
    speak("startedl.")
    running = True

    while running:
        user_text = listen()

        if user_text:
            running = process_command(user_text)


if __name__ == "__main__":
    main()
    