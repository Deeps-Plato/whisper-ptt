"""Push-to-talk + voice-activated dictation with faster-whisper and silero-vad.

F9 hold: manual PTT (existing behavior + radio commands)
F10: toggle voice activation on/off
Wake word "send it": hands-free dictation with radio commands

Radio commands (work in both modes):
  break    → newline
  over     → submit (Enter key) — only at end of utterance
  correction → delete previous word
  disregard  → cancel entire utterance
"""
import os
import sys
import logging
import time
import threading
import queue
import re
from enum import Enum
import winsound

# Log to file since pythonw has no console
LOG_FILE = os.path.join(os.environ['TEMP'], 'whisper-ptt.log')
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(message)s'
)

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import numpy as np
import torch
import sounddevice as sd
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad
from pynput import keyboard
import pyperclip
import pyautogui
from comtypes import CoInitialize, CoUninitialize
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

# ── Config ──────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
DEVICE_NAME = "Volt 2"
MODEL_SIZE = "base"
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL."
DUCK_LEVEL = 0.1

WAKE_PHRASE = "send it"
VAD_THRESHOLD = 0.5
WAKE_SILENCE_SECS = 0.8     # silence after wake phrase attempt
DICTATE_SILENCE_SECS = 2.0  # silence to end dictation
MAX_DICTATION_SECS = 30.0   # safety timeout
VAD_CHUNK = 512              # silero requires exactly 512 samples @ 16kHz

# ── State machine ───────────────────────────────────────────────────
class State(Enum):
    IDLE = 0
    BUFFERING = 1    # VAD detected speech, collecting wake phrase
    CHECKING = 2     # silence after buffering, checking for wake phrase
    DICTATING = 3    # wake phrase matched, collecting dictation
    PROCESSING = 4   # transcribing dictation
    MANUAL = 5       # F9 held down

state = State.IDLE
state_lock = threading.Lock()
vad_enabled = True
audio_q = queue.Queue(maxsize=200)

# ── Globals ─────────────────────────────────────────────────────────
whisper_model = None
vad_model = None
saved_volumes = {}
manual_chunks = []  # chunks collected during F9 hold

# ── Device ──────────────────────────────────────────────────────────
def find_device():
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if DEVICE_NAME.lower() in d['name'].lower() and d['max_input_channels'] > 0:
            return i
    return None

# ── Models ──────────────────────────────────────────────────────────
def load_whisper():
    global whisper_model
    if whisper_model is None:
        logging.info("Loading whisper model...")
        whisper_model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
        logging.info("Whisper ready")
    return whisper_model

def transcribe(audio):
    m = load_whisper()
    segments, _ = m.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                initial_prompt=INITIAL_PROMPT)
    return " ".join(seg.text.strip() for seg in segments)

# ── Audio ducking ───────────────────────────────────────────────────
def duck_audio():
    global saved_volumes
    saved_volumes = {}
    try:
        CoInitialize()
        sessions = AudioUtilities.GetAllSessions()
        for s in sessions:
            if s.Process:
                vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                saved_volumes[s.Process.pid] = vol.GetMasterVolume()
                vol.SetMasterVolume(saved_volumes[s.Process.pid] * DUCK_LEVEL, None)
        logging.info(f"Ducked {len(saved_volumes)} sessions")
    except Exception as e:
        logging.exception("Duck failed")
    finally:
        CoUninitialize()

def restore_audio():
    global saved_volumes
    try:
        CoInitialize()
        sessions = AudioUtilities.GetAllSessions()
        for s in sessions:
            if s.Process and s.Process.pid in saved_volumes:
                vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                vol.SetMasterVolume(saved_volumes[s.Process.pid], None)
        logging.info(f"Restored {len(saved_volumes)} sessions")
        saved_volumes = {}
    except Exception as e:
        logging.exception("Restore failed")
    finally:
        CoUninitialize()

# ── Radio commands ──────────────────────────────────────────────────
def strip_punctuation(word):
    """Strip trailing punctuation that whisper often adds."""
    return re.sub(r'[.,!?;:]+$', '', word)

def process_commands(text):
    """Process radio commands. Returns (cleaned_text, press_enter).

    Returns (None, False) for disregard.
    """
    if not text or not text.strip():
        return (None, False)

    words = text.strip().split()

    # Strip leading wake phrase if it leaked into transcription
    wake_words = WAKE_PHRASE.split()
    if len(words) >= len(wake_words):
        prefix = [strip_punctuation(w).lower() for w in words[:len(wake_words)]]
        if prefix == wake_words:
            words = words[len(wake_words):]

    if not words:
        return (None, False)

    # Check for disregard anywhere
    for w in words:
        if strip_punctuation(w).lower() == "disregard":
            logging.info("Disregard command")
            return (None, False)

    press_enter = False
    result = []

    for i, w in enumerate(words):
        cleaned = strip_punctuation(w).lower()

        if cleaned == "break":
            result.append("\n")
        elif cleaned == "over" and i == len(words) - 1:
            press_enter = True
        elif cleaned == "correction":
            if result:
                popped = result.pop()
                logging.info(f"Correction: removed '{popped}'")
        else:
            result.append(w)

    final = ""
    for i, part in enumerate(result):
        if part == "\n":
            final = final.rstrip() + "\n"
        elif i > 0 and result[i-1] != "\n" and final and not final.endswith("\n"):
            final += " " + part
        else:
            final += part

    final = final.strip()
    if not final:
        return (None, False)

    # Ensure each line ends with a period
    lines = final.split("\n")
    for i, line in enumerate(lines):
        line = line.rstrip()
        if line and line[-1] not in '.!?':
            lines[i] = line + '.'
    final = "\n".join(lines)

    return (final, press_enter)

# ── Paste helper ────────────────────────────────────────────────────
def paste_text(text, press_enter=False):
    """Clipboard save/restore paste with optional Enter."""
    if not text:
        return
    try:
        old_clip = pyperclip.paste()
    except Exception:
        old_clip = ""
    pyperclip.copy(text)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.05)
    if press_enter:
        pyautogui.press('enter')
        time.sleep(0.05)
    pyperclip.copy(old_clip)
    logging.info(f"Pasted: {text!r} (enter={press_enter})")

# ── Audio callback ──────────────────────────────────────────────────
def audio_callback(indata, frames, time_info, status):
    try:
        audio_q.put_nowait(indata.copy().flatten())
    except queue.Full:
        pass  # drop chunk silently

# ── VAD monitor thread ──────────────────────────────────────────────
def vad_monitor():
    global state, vad_enabled

    vad = vad_model
    vad.reset_states()

    buf = np.array([], dtype=np.float32)       # residual from 512-chunk slicing
    speech_buf = np.array([], dtype=np.float32) # full audio for wake/dictation
    silence_time = 0.0
    speech_start = 0.0
    dictate_heard_speech = False
    last_chunk_time = time.time()

    while True:
        try:
            chunk = audio_q.get(timeout=0.5)
        except queue.Empty:
            continue

        now = time.time()
        chunk_duration = len(chunk) / SAMPLE_RATE
        last_chunk_time = now

        with state_lock:
            cur = state

        # Skip VAD processing during manual mode or processing
        if cur in (State.MANUAL, State.PROCESSING):
            # During manual mode, collect chunks for F9
            if cur == State.MANUAL:
                manual_chunks.append(chunk)
            buf = np.array([], dtype=np.float32)
            continue

        if not vad_enabled and cur == State.IDLE:
            continue

        # Accumulate and process in 512-sample chunks
        buf = np.concatenate([buf, chunk])
        is_speech = False

        while len(buf) >= VAD_CHUNK:
            vad_chunk = buf[:VAD_CHUNK]
            buf = buf[VAD_CHUNK:]
            tensor = torch.from_numpy(vad_chunk)
            prob = vad(tensor, SAMPLE_RATE).item()
            if prob > VAD_THRESHOLD:
                is_speech = True

        # State transitions
        if cur == State.IDLE:
            if is_speech:
                with state_lock:
                    state = State.BUFFERING
                speech_buf = chunk.copy()
                silence_time = 0.0
                speech_start = now
                logging.info("VAD: speech detected, buffering")

        elif cur == State.BUFFERING:
            speech_buf = np.concatenate([speech_buf, chunk])
            if is_speech:
                silence_time = 0.0
            else:
                silence_time += chunk_duration

            if silence_time >= WAKE_SILENCE_SECS:
                with state_lock:
                    state = State.CHECKING
                logging.info("VAD: silence after buffer, checking wake phrase")

                # Transcribe and check for wake phrase
                text = transcribe(speech_buf)
                normalized = re.sub(r'[.,!?;:\s]+', ' ', text.lower()).strip()
                logging.info(f"Wake check: '{normalized}'")

                if WAKE_PHRASE in normalized:
                    # Check if there's text after the wake phrase
                    wake_idx = normalized.find(WAKE_PHRASE)
                    remainder = normalized[wake_idx + len(WAKE_PHRASE):].strip()

                    if remainder:
                        # User said wake phrase + dictation in one go
                        # Process the full text as dictation immediately
                        logging.info(f"Wake + dictation in one shot: '{text}'")
                        duck_audio()
                        cleaned, press_enter = process_commands(text)
                        if cleaned:
                            paste_text(cleaned, press_enter)
                        restore_audio()
                        with state_lock:
                            state = State.IDLE
                        speech_buf = np.array([], dtype=np.float32)
                        vad.reset_states()
                        logging.info("One-shot dictation done, back to idle")
                    else:
                        # Just the wake phrase, wait for dictation
                        with state_lock:
                            state = State.DICTATING
                        duck_audio()
                        speech_buf = np.array([], dtype=np.float32)
                        silence_time = 0.0
                        dictate_heard_speech = False
                        speech_start = now
                        vad.reset_states()
                        logging.info("Wake phrase matched! Dictating...")
                else:
                    with state_lock:
                        state = State.IDLE
                    speech_buf = np.array([], dtype=np.float32)
                    vad.reset_states()
                    logging.info("No wake phrase match, back to idle")

        elif cur == State.DICTATING:
            speech_buf = np.concatenate([speech_buf, chunk])
            if is_speech:
                silence_time = 0.0
                dictate_heard_speech = True
            elif dictate_heard_speech:
                # Only count silence after we've heard actual speech
                silence_time += chunk_duration

            elapsed = now - speech_start
            if (dictate_heard_speech and silence_time >= DICTATE_SILENCE_SECS) or elapsed >= MAX_DICTATION_SECS:
                with state_lock:
                    state = State.PROCESSING

                if elapsed >= MAX_DICTATION_SECS:
                    logging.info("Max dictation time reached")
                else:
                    logging.info("Dictation silence detected")

                # Transcribe dictation
                if len(speech_buf) > 0:
                    text = transcribe(speech_buf)
                    logging.info(f"Dictation raw: {text}")
                    cleaned, press_enter = process_commands(text)
                    if cleaned:
                        paste_text(cleaned, press_enter)

                restore_audio()
                speech_buf = np.array([], dtype=np.float32)
                vad.reset_states()
                silence_time = 0.0
                with state_lock:
                    state = State.IDLE
                logging.info("Back to idle")

# ── Keyboard handlers ──────────────────────────────────────────────
def on_press(key):
    global state, manual_chunks
    try:
        if key == keyboard.Key.f9:
            with state_lock:
                if state == State.MANUAL:
                    return  # already recording
                # Interrupt any VAD state
                prev = state
                state = State.MANUAL
            manual_chunks = []
            if prev == State.DICTATING:
                restore_audio()
                logging.info("F9 interrupted dictation")
            duck_audio()
            logging.info("F9: recording")

        elif key == keyboard.Key.f10:
            global vad_enabled
            vad_enabled = not vad_enabled
            logging.info(f"VAD {'enabled' if vad_enabled else 'disabled'}")
            # High beep = on, low beep = off
            threading.Thread(target=winsound.Beep,
                             args=(800 if vad_enabled else 400, 150),
                             daemon=True).start()

    except Exception as e:
        logging.exception("Error in on_press")

def on_release(key):
    global state, manual_chunks
    try:
        if key == keyboard.Key.f9:
            with state_lock:
                if state != State.MANUAL:
                    return
                state = State.PROCESSING

            restore_audio()
            logging.info("F9: released, transcribing...")

            # Drain any remaining chunks from queue
            while True:
                try:
                    manual_chunks.append(audio_q.get_nowait())
                except queue.Empty:
                    break

            if manual_chunks:
                audio = np.concatenate(manual_chunks)
                text = transcribe(audio)
                if text.strip():
                    logging.info(f"F9 raw: {text}")
                    cleaned, press_enter = process_commands(text)
                    if cleaned:
                        paste_text(cleaned, press_enter)
                else:
                    logging.info("No speech detected")
            else:
                logging.info("No audio captured")

            manual_chunks = []
            with state_lock:
                state = State.IDLE

    except Exception as e:
        logging.exception("Error in on_release")

# ── Main ────────────────────────────────────────────────────────────
def run_listener():
    global vad_model

    device = find_device()
    if device is None:
        logging.error(f"Could not find {DEVICE_NAME}")
        return False

    logging.info(f"Using device: {sd.query_devices(device)['name']}")
    load_whisper()

    # Load VAD
    try:
        vad_model = load_silero_vad()
        logging.info("Silero VAD loaded")
    except Exception as e:
        logging.exception("Failed to load VAD, F9-only mode")
        vad_model = None

    # Start VAD monitor thread
    if vad_model is not None:
        t = threading.Thread(target=vad_monitor, daemon=True)
        t.start()
        logging.info("VAD monitor thread started")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, device=device,
                        callback=audio_callback, blocksize=1600):
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
    return True

def main():
    logging.info("PTT with VAD starting")
    while True:
        try:
            run_listener()
        except Exception as e:
            logging.exception("Listener crashed, restarting in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main()
