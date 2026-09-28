from voice import speak, listen
from brain import ask

def process_command(command):
    if "stop" in command or "exit" in command:
        speak("Shutting down.")
        return False
    else:
        answer = ask(command)
        speak(answer)
        return True
        
def main():
    speak("started.")
    running = True

    while running:
        user_text = listen()

        if user_text:
            running = process_command(user_text)


if __name__ == "__main__":
    main()
