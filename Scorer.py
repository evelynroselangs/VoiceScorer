"""
Indoor Cricket Voice Scorer
============================
Lightweight voice-to-keystroke scoring system for indoor cricket.
Optimised for noisy sports centre environments using keyword spotting
rather than full speech-to-text for lower CPU usage.

Dependencies:
    pip install SpeechRecognition pyautogui pyaudio

Usage:
    python indoor_cricket_scorer.py

Controls:
    Press Ctrl+C to stop listening.

── Extras + batter runs ──────────────────────────────────────────────────────
After any extra (no ball / leg side / wide), the scoring app shows a brief
pop-up where the user can enter additional runs scored by the batters.

The scorer handles this in two ways:

  1. Same utterance  — "wide plus two"
     Both the extra and the plus-score are in one phrase; the scorer sends
     the extra key(s), waits POPUP_DELAY seconds for the pop-up to appear,
     then sends the plus-score digit(s).

  2. Follow-up utterance — "wide" … (pause) … "plus two"
     After sending an extra the scorer opens a PLUS_SCORE_WINDOW second
     window.  If "plus {number}" arrives within that window it is treated
     as the batter runs for that delivery.  Any other command closes the
     window first.
"""

import re
import time
import queue
import threading
import pyautogui
import speech_recognition as sr

# ── Configuration ──────────────────────────────────────────────────────────────

PHRASE_TIME_LIMIT   = 3      # max seconds per captured phrase
PAUSE_THRESHOLD     = 0.5    # silence before phrase is considered ended
ENERGY_THRESHOLD    = None   # None = auto-calibrate; hard-code e.g. 3500 if needed
KEYSTROKE_DELAY     = 0.05   # seconds between each simulated keypress

# Dead-ball window: if "dead ball" arrives within this many seconds of a
# "no ball", the NB is suppressed entirely.
DEAD_BALL_WAIT      = 1    # seconds

# How long to wait after sending an extra before sending the plus-score.
# The scoring app pop-up needs a moment to appear.
POPUP_DELAY         = 0.4    # seconds

# How long (seconds) after sending an extra to keep listening for a
# follow-up "plus {score}" phrase.  If none arrives the window simply closes.
PLUS_SCORE_WINDOW   = 1.0    # seconds

# ── Keyword → keystroke mapping ────────────────────────────────────────────────
# Order matters: more specific phrases must come before broader ones.

COMMANDS = [
    # Scores
    ("fourteen",      "14"),
    ("twelve",        "12"),
    ("ten",           "10"),
    ("eight",         "8"),
    ("seven",         "7"),
    ("six",           "6"),
    ("five",          "5"),
    ("four",          "4"),
    ("three",         "3"),
    ("two",           "2"),
    ("one",           "1"),
    ("zero",          "0"),
    ("ball is good",  "0"),
    ("good",          "0"),
    ("10","10"),
    ("12","12"),
    ("14","14"),
    ("1","1"),
    ("2","2"),
    ("3","3"),
    ("4","4"),
    ("5","5"),
    ("6", "6"),
    ("7","7"),
    ("8","8"),

    # Extras
    ("no ball",       "nb"),
    ("leg side",      "ls"),
    ("wide",          "w"),
    # mishearings
    ("lakeside","ls"),
    ("legacy","ls"),
    ("noble","nb"),
    ("nova","nb"),

    # Dismissals
    ("run out",       "r"),
    ("bowled",        "b"),
    ("stumped",       "s"),
    ("caught",        "c"),
    ("hit wicket",    "hw"),

    # Dead ball suppresses a pending no ball
    ("dead ball",     None),
]

EXTRAS = {"nb", "ls", "w"}   # keystroke values that are extras

# Number words → digit string (for "plus X" parsing)
NUMBER_WORDS = {
    "fourteen": "14",
    "twelve":   "12",
    "ten":      "10",
    "eight":    "8",
    "seven":    "7",
    "six":      "6",
    "five":     "5",
    "four":     "4",
    "three":    "3",
    "two":      "2",
    "one":      "1",
}

# ── State ──────────────────────────────────────────────────────────────────────

pending_nb:          float | None = None   # timestamp of unconfirmed "no ball"
plus_score_deadline: float | None = None   # epoch time until which plus-scores accepted
# Stores the inline plus-score waiting on NB dead-ball confirmation
pending_nb_plus:     str   | None = None
listening = True
audio_queue: queue.Queue = queue.Queue()


# ── Helpers ────────────────────────────────────────────────────────────────────

def type_keys(text: str) -> None:
    """Send each character as a keystroke with a small delay."""
    for ch in text:
        pyautogui.press(ch)
        time.sleep(KEYSTROKE_DELAY)
    print(f"  → Typed: {text!r}")


def extract_plus_score(text: str) -> str | None:
    lower = text.lower()
    if "plus" not in lower:
        return None

    for word, digit in NUMBER_WORDS.items():
        if f"plus {word}" in lower:
            return digit

    # Digit form
    m = re.search(r"plus\s+(\d+)", lower)
    if m:
        return m.group(1)

    return None


def send_plus_score(score_str: str) -> None:
    print(f"  → Waiting {POPUP_DELAY}s for pop-up…")
    time.sleep(POPUP_DELAY)
    print(f"  → Typing plus-score: {score_str!r}")
    type_keys(score_str)


def open_plus_window() -> None:
    global plus_score_deadline
    plus_score_deadline = time.time() + PLUS_SCORE_WINDOW
    print(f"  → Plus-score window open for {PLUS_SCORE_WINDOW}s…")


def close_plus_window() -> None:
    global plus_score_deadline
    plus_score_deadline = None


def plus_window_active() -> bool:
    return plus_score_deadline is not None and time.time() < plus_score_deadline


def resolve_pending_nb() -> None:
    global pending_nb, pending_nb_plus
    if pending_nb is not None and (time.time() - pending_nb) >= DEAD_BALL_WAIT:
        print("  → Confirmed no-ball (no dead ball followed)")
        type_keys("nb")
        saved_plus = pending_nb_plus
        pending_nb = None
        pending_nb_plus = None
        if saved_plus:
            # Inline plus-score was waiting — send it now
            threading.Thread(
                target=send_plus_score, args=(saved_plus,), daemon=True
            ).start()
        else:
            open_plus_window()


def match_command(text: str):
    lower = text.lower()
    for phrase, keys in COMMANDS:
        if phrase in lower:
            return phrase, keys
    return None, None

# core logic
def process_utterance(text: str) -> None:
    global pending_nb, pending_nb_plus

    print(f"Heard: {text!r}")
    text_lower = text.lower()

 # dead ball
    if "dead ball" in text_lower:
        if pending_nb is not None:
            print("  → Dead ball — suppressing no-ball")
            pending_nb = None
            pending_nb_plus = None
        else:
            print("  → Dead ball (nothing to suppress)")
        close_plus_window()
        return

    if plus_window_active():
        plus = extract_plus_score(text)
        if plus is not None:
            close_plus_window()
            threading.Thread(
                target=send_plus_score, args=(plus,), daemon=True
            ).start()
            return
        else:
            close_plus_window()

    resolve_pending_nb()

    phrase, keys = match_command(text)

    if phrase is None:
        print("  → No match found")
        return

# No ball
    if phrase == "no ball":
        inline_plus = extract_plus_score(text)
        pending_nb = time.time()
        pending_nb_plus = inline_plus
        print(f"  → Pending no-ball (waiting {DEAD_BALL_WAIT}s for dead ball…)")
        if inline_plus:
            print(f"  → Inline plus-score '{inline_plus}' held until NB confirmed")

        def _nb_timer():
            time.sleep(DEAD_BALL_WAIT + 0.1)
            resolve_pending_nb()

        threading.Thread(target=_nb_timer, daemon=True).start()
        return

# regular extras
    if keys in EXTRAS:
        inline_plus = extract_plus_score(text)
        type_keys(keys)
        if inline_plus:
            threading.Thread(
                target=send_plus_score, args=(inline_plus,), daemon=True
            ).start()
        else:
            open_plus_window()
        return

# everything else
    type_keys(keys)


# audio processing

def audio_worker() -> None:
    recogniser = sr.Recognizer()
    recogniser.pause_threshold = PAUSE_THRESHOLD

    while True:
        audio = audio_queue.get()
        if audio is None:
            break
        try:
            text = recogniser.recognize_google(audio)
            process_utterance(text)
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            print(f"  [Recognition error: {e}]")
        finally:
            audio_queue.task_done()


# listening

def listen_loop() -> None:
    recogniser = sr.Recognizer()
    recogniser.pause_threshold = PAUSE_THRESHOLD

    with sr.Microphone() as source:
        print("Calibrating for ambient noise (1 second)…")
        recogniser.adjust_for_ambient_noise(source, duration=1)

        if ENERGY_THRESHOLD is not None:
            recogniser.energy_threshold = ENERGY_THRESHOLD
        else:
            recogniser.energy_threshold = max(recogniser.energy_threshold, 1800)

        print(f"Energy threshold: {recogniser.energy_threshold:.0f}")
        print("Listening… (Ctrl+C to quit)\n")

        while listening:
            try:
                audio = recogniser.listen(
                    source,
                    timeout=5,
                    phrase_time_limit=PHRASE_TIME_LIMIT,
                )
                audio_queue.put(audio)
            except sr.WaitTimeoutError:
                resolve_pending_nb()
            except OSError:
                print("[Microphone error — retrying]")
                time.sleep(1)


# startup script

def main() -> None:
    print("=" * 55)
    print(" Indoor Cricket Voice Scorer")
    print("=" * 55)
    print()
    print("Extra + batter run examples:")
    print('  "wide plus two"          →  w  (+0.4s)  2')
    print('  "leg side plus three"    →  ls (+0.4s)  3')
    print('  "wide"  … "plus one"     →  w  (+0.4s)  1   (follow-up)')
    print('  "no ball plus two"       →  nb (+0.4s)  2   (after dead-ball window)')
    print('  "no ball" … "dead ball"  →  (suppressed)')
    print()

    worker = threading.Thread(target=audio_worker, daemon=True)
    worker.start()

    try:
        listen_loop()
    except KeyboardInterrupt:
        print("\nStopping…")

    audio_queue.put(None)
    worker.join(timeout=3)
    print("Done.")


if __name__ == "__main__":
    main()