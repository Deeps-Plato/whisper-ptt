"""Push-to-talk + voice-activated dictation with faster-whisper and silero-vad.

Right Ctrl hold OR middle mouse button hold: manual PTT (radio commands)
F10: toggle hot mic (no wake phrase, just talk → paste on silence)
F8: toggle VAD on/off
Wake word "send it": hands-free dictation, keeps recording until "over"

Radio commands (voice-activation only — VAD/hot mic, NOT manual PTT):
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
from pynput import keyboard, mouse
import pyperclip
import pyautogui
from comtypes import CoInitialize, CoUninitialize
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

# ── Config ──────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
DEVICE_NAME = "Yeti Classic"
MODEL_SIZE = "base"
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL, Dart. openclaw, claude, claude-code, claude-flow, .openclaw, Ableton, Roblox, audiobooks, channel-icons, claudedocs, cleo, cleo_test, dippi, drones, everdo, fuzzy_launcher_android, icon-generator-android, logan, moltbot, obsidian_mcp, opencode, rei-flow, rei-local-bot, rei-output, research-staging, sweethome3d, voice_agent_gst, web_ez, whisper-ptt, @rei. Punctuation: dot ., slash /, hyphen -, brackets [], parentheses (), braces {}, hash #, at @, dollar $, percent %, caret ^, ampersand &, asterisk *, plus +, equals =, pipe |, backslash \\, tilde ~, backtick `, home slash ~/."
DUCK_LEVEL = 0.1
BEEP_BACKEND = "winsound"  # "sounddevice" or "winsound"
TERMINAL_TITLE_HINTS = (
    "codex",
    "windows terminal",
    "powershell",
    "pwsh",
    "ubuntu",
    "wsl",
    "bash",
    "zsh",
    "cmd",
    "command prompt",
)

WAKE_PHRASE = "send it"
VAD_THRESHOLD = 0.5
SILENCE_CHECK_SECS = 2.0    # silence before checking for "over"/"disregard"
MAX_DICTATION_SECS = 300.0  # 5 min safety timeout
VAD_CHUNK = 512              # silero requires exactly 512 samples @ 16kHz
VAD_COOLDOWN_SECS = 5.0      # pause VAD after failed wake-phrase check
LEXICAL_OVERRIDES = {
    "cis": "sys",
    "ray": "Rei",
}

# ── State machine ───────────────────────────────────────────────────
class State(Enum):
    IDLE = 0
    BUFFERING = 1    # VAD detected speech, recording
    CHECKING = 2     # silence gap, transcribing to check for over/disregard
    PROCESSING = 4   # transcribing F9 recording
    MANUAL = 5       # F9 held down

state = State.IDLE
state_lock = threading.Lock()

# Serialize beeps to avoid overlap/stutter and log failures
beep_lock = threading.Lock()

def _sd_beep(tones, sample_rate=44100):
    for freq, dur_ms in tones:
        duration = max(dur_ms, 1) / 1000.0
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
        wave = (0.15 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        sd.play(wave, samplerate=sample_rate, blocking=True)

def _play_beeps(tones):
    try:
        with beep_lock:
            if BEEP_BACKEND == "winsound":
                for freq, dur in tones:
                    winsound.Beep(freq, dur)
            else:
                _sd_beep(tones)
    except Exception:
        logging.exception("Beep failed")

def beep_async(tones):
    threading.Thread(target=_play_beeps, args=(tones,), daemon=True).start()
vad_enabled = False  # F8 to enable; default off to avoid GPU churn
hot_mic = False  # hot mic: no wake phrase needed, just talk
audio_q = queue.Queue(maxsize=200)

# ── Globals ─────────────────────────────────────────────────────────
whisper_model = None
vad_model = None
saved_volumes = {}   # {(pid, session_idx): original_volume}
duck_lock = threading.Lock()
_is_ducked = False
manual_chunks = []  # chunks collected during F9 hold
key_sender = keyboard.Controller()

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

def apply_lexical_overrides(text):
    """Force preferred token replacements on Whisper output."""
    if not text:
        return text
    for source, target in LEXICAL_OVERRIDES.items():
        text = re.sub(rf'\b{re.escape(source)}\b', target, text, flags=re.IGNORECASE)
    return text

def transcribe(audio):
    m = load_whisper()
    segments, _ = m.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                initial_prompt=INITIAL_PROMPT)
    text = " ".join(seg.text.strip() for seg in segments)
    return apply_lexical_overrides(text)

# ── Audio ducking ───────────────────────────────────────────────────
def duck_audio():
    global saved_volumes, _is_ducked
    with duck_lock:
        if _is_ducked:
            logging.info("duck_audio: already ducked, skipping")
            return
        new_saved = {}
        try:
            CoInitialize()
            sessions = AudioUtilities.GetAllSessions()
            pid_counters = {}
            for s in sessions:
                if s.Process:
                    pid = s.Process.pid
                    idx = pid_counters.get(pid, 0)
                    pid_counters[pid] = idx + 1
                    vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                    orig = vol.GetMasterVolume()
                    new_saved[(pid, idx)] = orig
                    vol.SetMasterVolume(orig * DUCK_LEVEL, None)
            saved_volumes = new_saved
            _is_ducked = True
            logging.info(f"Ducked {len(saved_volumes)} sessions")
        except Exception:
            logging.exception("Duck failed")
            saved_volumes = {}
        finally:
            CoUninitialize()

def restore_audio():
    global saved_volumes, _is_ducked
    with duck_lock:
        if not _is_ducked:
            logging.info("restore_audio: not ducked, skipping")
            return
        try:
            CoInitialize()
            sessions = AudioUtilities.GetAllSessions()
            pid_counters = {}
            for s in sessions:
                if s.Process:
                    pid = s.Process.pid
                    idx = pid_counters.get(pid, 0)
                    pid_counters[pid] = idx + 1
                    key = (pid, idx)
                    if key in saved_volumes:
                        vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                        vol.SetMasterVolume(saved_volumes[key], None)
            logging.info(f"Restored {len(saved_volumes)} sessions")
        except Exception:
            logging.exception("Restore failed")
        finally:
            saved_volumes = {}
            _is_ducked = False
            CoUninitialize()

# ── Radio commands ──────────────────────────────────────────────────
def strip_punctuation(word):
    """Strip trailing punctuation that whisper often adds."""
    return re.sub(r'[.,!?;:]+$', '', word)

def process_commands(text, radio=True):
    """Process radio commands. Returns (cleaned_text, press_enter).

    radio=True enables break/over/correction/disregard (voice-activation only).
    radio=False skips them (manual PTT — just transcribe literally).
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

    # Check for disregard anywhere (voice-activation only)
    if radio:
        for w in words:
            if strip_punctuation(w).lower() == "disregard":
                logging.info("Disregard command")
                return (None, False)

    # Merge spoken punctuation/symbols into tokens before main loop
    # e.g. "question mark" → "?", "home slash" → "~/"
    REPLACEMENTS_2WORD = {
        "question mark": "?",
        "exclamation point": "!",
        "exclamation mark": "!",
        "home slash": "~/",
        "open bracket": "[",
        "close bracket": "]",
        "open paren": "(",
        "close paren": ")",
        "open brace": "{",
        "close brace": "}",
        "hash tag": "#",
        "dollar sign": "$",
        "percent sign": "%",
        "at symbol": "@",
        "at sign": "@",
    }
    REPLACEMENTS_1WORD = {
        "homeslash": "~/",
        "comma": ",",
        "dot": ".",
        "slash": "/",
        "hyphen": "-",
        "period": ". ",
        "colon": ":",
        "semicolon": ";",
        "caret": "^",
        "ampersand": "&",
        "asterisk": "*",
        "plus": "+",
        "equals": "=",
        "pipe": "|",
        "backslash": "\\",
        "tilde": "~",
        "backtick": "`",
        "underscore": "_",
    }

    merged = []
    i = 0
    while i < len(words):
        pair = strip_punctuation(words[i]).lower()
        if i + 1 < len(words):
            pair2 = pair + " " + strip_punctuation(words[i+1]).lower()
            if pair2 in REPLACEMENTS_2WORD:
                merged.append(REPLACEMENTS_2WORD[pair2])
                i += 2
                continue
        if pair in REPLACEMENTS_1WORD:
            merged.append(REPLACEMENTS_1WORD[pair])
        else:
            merged.append(words[i])
        i += 1
    words = merged

    press_enter = False
    result = []

    for i, w in enumerate(words):
        cleaned = strip_punctuation(w).lower()

        if w in ("?", "!", ","):
            # Spoken punctuation: attach to previous word
            if result:
                result[-1] = result[-1].rstrip('.,;:?!') + w
            continue
        elif radio and cleaned == "break":
            result.append("\n")
        elif radio and cleaned == "over" and i == len(words) - 1:
            press_enter = True
            # Preserve ?/! from "over?" onto previous word
            trailing = w[len(cleaned):]  # e.g. "?" from "over?"
            keep = ''.join(c for c in trailing if c in '?!')
            if keep and result:
                result[-1] = result[-1].rstrip('.,;:?!') + keep
        elif radio and cleaned == "correction":
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
        return (None, press_enter)

    # Clean up: capitalize first letter, strip orphaned punctuation, ensure period
    lines = final.split("\n")
    for i, line in enumerate(lines):
        line = line.strip().lstrip(',;:').strip()
        line = line.rstrip().rstrip(',;:')
        if line:
            line = line[0].upper() + line[1:]
            if line[-1] not in '.!?':
                line += '.'
        lines[i] = line
    final = "\n".join(lines)

    # Ensure trailing space after final period
    if final.endswith('.'):
        final += ' '

    return (final, press_enter)

# ── Paste helper ────────────────────────────────────────────────────
def _active_window_title():
    try:
        return (pyautogui.getActiveWindowTitle() or "").lower()
    except Exception:
        return ""

def _is_terminal_like_window():
    title = _active_window_title()
    if not title:
        # Unknown active window: prefer direct typing, it works in more terminals.
        return True, title
    return any(hint in title for hint in TERMINAL_TITLE_HINTS), title

def paste_text(text, press_enter=False):
    """Type into terminal-like windows, otherwise clipboard-paste with restore."""
    if not text and not press_enter:
        return

    old_clip = None
    used_direct_type = False
    is_terminal, title = _is_terminal_like_window()

    if text:
        if is_terminal:
            try:
                key_sender.type(text)
                used_direct_type = True
                time.sleep(0.03)
                logging.info(f"Typed text: {text!r} (title={title!r})")
            except Exception:
                logging.exception("Direct typing failed, falling back to clipboard paste")

        if not used_direct_type:
            try:
                old_clip = pyperclip.paste()
            except Exception:
                old_clip = ""
            pyperclip.copy(text)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.05)
            logging.info(f"Pasted text: {text!r} (title={title!r})")

    if press_enter:
        pyautogui.press('enter')
        time.sleep(0.05)

    if old_clip is not None:
        pyperclip.copy(old_clip)

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
    last_chunk_time = time.time()
    cooldown_until = 0.0  # timestamp: ignore VAD until this time

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

        # Cooldown after failed wake-phrase check to avoid GPU churn
        if cur == State.IDLE and now < cooldown_until:
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

            elapsed = now - speech_start

            # On silence gap or max time, transcribe and check
            if silence_time >= SILENCE_CHECK_SECS or elapsed >= MAX_DICTATION_SECS:
                with state_lock:
                    state = State.CHECKING

                text = transcribe(speech_buf)
                normalized = re.sub(r'[.,!?;:\s]+', ' ', text.lower()).strip()
                logging.info(f"Check: '{normalized}'")

                has_wake = WAKE_PHRASE in normalized

                # In normal mode, must contain wake phrase
                if not hot_mic and not has_wake:
                    if elapsed >= MAX_DICTATION_SECS:
                        restore_audio()
                        with state_lock:
                            state = State.IDLE
                        speech_buf = np.array([], dtype=np.float32)
                        vad.reset_states()
                        cooldown_until = time.time() + VAD_COOLDOWN_SECS
                        logging.info("Max time, no wake phrase, cooldown %ss", VAD_COOLDOWN_SECS)
                    else:
                        with state_lock:
                            state = State.BUFFERING
                        silence_time = 0.0
                        logging.info("No wake phrase yet, keep buffering")
                    continue

                # Hot mic or wake phrase found — check for termination
                words = normalized.split()
                last_word = words[-1] if words else ""

                if hot_mic:
                    # Hot mic: process on every silence gap
                    logging.info(f"Hot mic: '{text}'")
                    duck_audio()
                    cleaned, press_enter = process_commands(text)
                    if cleaned or press_enter:
                        paste_text(cleaned, press_enter)
                    restore_audio()
                    with state_lock:
                        state = State.IDLE
                    speech_buf = np.array([], dtype=np.float32)
                    vad.reset_states()
                    logging.info("Hot mic done, back to idle")
                elif last_word == "over" or last_word == "disregard" or elapsed >= MAX_DICTATION_SECS:
                    # Done! Process the full text
                    logging.info(f"Complete utterance: '{text}'")
                    duck_audio()
                    cleaned, press_enter = process_commands(text)
                    if cleaned or press_enter:
                        paste_text(cleaned, press_enter)
                    restore_audio()
                    with state_lock:
                        state = State.IDLE
                    speech_buf = np.array([], dtype=np.float32)
                    vad.reset_states()
                    logging.info("Done, back to idle")
                else:
                    # Wake phrase found but no "over" yet — keep recording
                    with state_lock:
                        state = State.BUFFERING
                    silence_time = 0.0
                    logging.info("Wake phrase found, waiting for 'over'...")
                    duck_audio()

# ── Keyboard handlers ──────────────────────────────────────────────
def on_press(key):
    global state, manual_chunks
    try:
        if key == keyboard.Key.ctrl_r:
            with state_lock:
                if state == State.MANUAL:
                    return  # already recording
                # Interrupt any VAD state
                prev = state
                state = State.MANUAL
            manual_chunks = []
            if prev in (State.BUFFERING, State.CHECKING):
                restore_audio()
                logging.info("RCtrl interrupted VAD recording")
            duck_audio()
            # Cute press chirp (ascending blip)
            beep_async([(784, 40), (1047, 50)])  # G5, C6
            logging.info("RCtrl: recording")

        elif key == keyboard.Key.f10:
            global hot_mic
            hot_mic = not hot_mic
            logging.info(f"Hot mic {'ON' if hot_mic else 'OFF'}")
            # Double beep = on, single low = off
            if hot_mic:
                # Sticky keys on: ascending
                beep_async([(523, 80), (587, 80), (659, 80)])
            else:
                # Sticky keys off: descending
                beep_async([(659, 80), (587, 80), (523, 80)])

        elif key == keyboard.Key.f8:
            global vad_enabled
            vad_enabled = not vad_enabled
            logging.info(f"VAD {'enabled' if vad_enabled else 'disabled'}")
            beep_async([(800 if vad_enabled else 400, 150)])

    except Exception as e:
        logging.exception("Error in on_press")

def on_release(key):
    global state, manual_chunks
    try:
        if key == keyboard.Key.ctrl_r:
            with state_lock:
                if state != State.MANUAL:
                    return
                state = State.PROCESSING

            restore_audio()
            # Cute release chirp (descending boop)
            beep_async([(1047, 40), (659, 60)])  # C6, E5
            logging.info("RCtrl: released, transcribing...")

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
                    cleaned, press_enter = process_commands(text, radio=False)
                    if cleaned or press_enter:
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

def on_click(x, y, button, pressed):
    """Mouse button handler - middle button acts as PTT."""
    global state, manual_chunks
    try:
        if button == mouse.Button.middle:
            if pressed:
                # Middle button pressed - start recording
                with state_lock:
                    if state == State.MANUAL:
                        return  # already recording
                    # Interrupt any VAD state
                    prev = state
                    state = State.MANUAL
                manual_chunks = []
                if prev in (State.BUFFERING, State.CHECKING):
                    restore_audio()
                    logging.info("Middle mouse interrupted VAD recording")
                duck_audio()
                # Cute press chirp (ascending blip)
                beep_async([(784, 40), (1047, 50)])  # G5, C6
                logging.info("Middle mouse: recording")
            else:
                # Middle button released - transcribe
                with state_lock:
                    if state != State.MANUAL:
                        return
                    state = State.PROCESSING

                restore_audio()
                # Cute release chirp (descending boop)
                beep_async([(1047, 40), (659, 60)])  # C6, E5
                logging.info("Middle mouse: released, transcribing...")

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
                        logging.info(f"Middle mouse raw: {text}")
                        cleaned, press_enter = process_commands(text, radio=False)
                        if cleaned or press_enter:
                            paste_text(cleaned, press_enter)
                    else:
                        logging.info("No speech detected")
                else:
                    logging.info("No audio captured")

                manual_chunks = []
                with state_lock:
                    state = State.IDLE

    except Exception as e:
        logging.exception("Error in on_click")

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
        with keyboard.Listener(on_press=on_press, on_release=on_release) as kb_listener, \
             mouse.Listener(on_click=on_click) as mouse_listener:
            kb_listener.join()
            mouse_listener.join()
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
