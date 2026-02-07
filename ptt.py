"""Push-to-talk with faster-whisper. Hold F9 to record, release to transcribe."""
import os
import sys
import logging
import time

# Log to file since pythonw has no console
LOG_FILE = os.path.join(os.environ['TEMP'], 'whisper-ptt.log')
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(message)s'
)

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from pynput import keyboard
import pyperclip
import pyautogui
from comtypes import CoInitialize, CoUninitialize
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

# Config
SAMPLE_RATE = 16000
DEVICE_NAME = "Volt 2"
MODEL_SIZE = "base"
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL."
DUCK_LEVEL = 0.2  # 0.0 = mute, 1.0 = no change

# State
recording = False
audio_chunks = []
model = None
saved_volumes = {}  # pid -> original volume

def find_device():
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if DEVICE_NAME.lower() in d['name'].lower() and d['max_input_channels'] > 0:
            return i
    return None

def load_model():
    global model
    if model is None:
        logging.info("Loading whisper model...")
        model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
        logging.info("Model ready!")
    return model

def transcribe(audio):
    m = load_model()
    segments, _ = m.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                initial_prompt=INITIAL_PROMPT)
    return " ".join(seg.text.strip() for seg in segments)

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
        logging.info(f"Ducked {len(saved_volumes)} audio sessions")
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
        logging.info(f"Restored {len(saved_volumes)} audio sessions")
        saved_volumes = {}
    except Exception as e:
        logging.exception("Restore failed")
    finally:
        CoUninitialize()

def on_press(key):
    global recording, audio_chunks
    try:
        if key == keyboard.Key.f9 and not recording:
            recording = True
            audio_chunks = []
            duck_audio()
            logging.info("Recording started")
    except Exception as e:
        logging.exception("Error in on_press")

def on_release(key):
    global recording, audio_chunks
    try:
        if key == keyboard.Key.f9 and recording:
            recording = False
            restore_audio()
            logging.info("Recording stopped, transcribing...")

            if audio_chunks:
                audio = np.concatenate(audio_chunks)
                text = transcribe(audio)
                if text.strip():
                    logging.info(f"Transcribed: {text}")
                    old_clip = pyperclip.paste()
                    pyperclip.copy(text)
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.05)
                    pyperclip.copy(old_clip)
                else:
                    logging.info("No speech detected")
            else:
                logging.info("No audio captured")
    except Exception as e:
        logging.exception("Error in on_release")

def audio_callback(indata, frames, time_info, status):
    if recording:
        audio_chunks.append(indata.copy().flatten())

def run_listener():
    device = find_device()
    if device is None:
        logging.error(f"Could not find {DEVICE_NAME}")
        return False

    logging.info(f"Using device: {sd.query_devices(device)['name']}")
    load_model()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, device=device,
                        callback=audio_callback, blocksize=1600):
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
    return True

def main():
    logging.info("PTT starting")
    while True:
        try:
            run_listener()
        except Exception as e:
            logging.exception("Listener crashed, restarting in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main()
