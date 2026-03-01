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
import json
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
import pystray
from PIL import Image, ImageDraw

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

# ── Hotkey bindings (overwritten by load_settings at startup) ────────
PTT_KEY      = keyboard.Key.ctrl_r   # keyboard PTT hold
HOT_MIC_KEY  = keyboard.Key.f10     # toggle hot mic
VAD_KEY      = keyboard.Key.f8      # toggle VAD

def _key_to_str(key: keyboard.Key | keyboard.KeyCode) -> str:
    """Serialize a pynput key to a JSON-safe string."""
    if isinstance(key, keyboard.Key):
        return key.name          # e.g. "ctrl_r", "f10"
    return f"char:{key.char}"   # e.g. "char:a"

def _str_to_key(s: str) -> keyboard.Key | keyboard.KeyCode:
    """Deserialize a pynput key from a JSON string."""
    if s.startswith("char:"):
        return keyboard.KeyCode.from_char(s[5:])
    return keyboard.Key[s]      # raises KeyError on unknown name — caller handles

def _key_label(key: keyboard.Key | keyboard.KeyCode) -> str:
    """Human-readable key name for display in the tray menu."""
    if isinstance(key, keyboard.Key):
        return key.name.replace("_", " ").title()   # "ctrl_r" → "Ctrl R"
    return key.char

# ── Settings persistence ─────────────────────────────────────────────
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ptt-settings.json")

_SETTINGS_DEFAULTS = {
    "duck_level": DUCK_LEVEL, "beep_backend": BEEP_BACKEND,
    "device_name": DEVICE_NAME, "vad_enabled": False, "hot_mic": False,
    "ptt_key": "ctrl_r", "hot_mic_key": "f10", "vad_key": "f8",
}

def load_settings() -> dict:
    defaults = dict(_SETTINGS_DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        defaults.update({k: saved[k] for k in defaults if k in saved})
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("load_settings failed, using defaults")
    return defaults

def save_settings() -> None:
    data = {
        "duck_level": DUCK_LEVEL, "beep_backend": BEEP_BACKEND,
        "device_name": DEVICE_NAME, "vad_enabled": vad_enabled, "hot_mic": hot_mic,
        "ptt_key": _key_to_str(PTT_KEY),
        "hot_mic_key": _key_to_str(HOT_MIC_KEY),
        "vad_key": _key_to_str(VAD_KEY),
    }
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        logging.exception("save_settings failed")

# ── State machine ───────────────────────────────────────────────────
class State(Enum):
    IDLE = 0
    BUFFERING = 1    # VAD detected speech, recording
    CHECKING = 2     # silence gap, transcribing to check for over/disregard
    PROCESSING = 4   # transcribing F9 recording
    MANUAL = 5       # F9 held down

state = State.IDLE
state_lock = threading.Lock()

# Binding mode + restart event
_restart_event = threading.Event()
_binding_mode: str | None = None   # "ptt_key" | "hot_mic_key" | "vad_key" | None
_binding_lock  = threading.Lock()  # guards _binding_mode reads/writes

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

# Apply saved settings — overwrites Config defaults with persisted values
_s = load_settings()
DUCK_LEVEL   = _s["duck_level"]
BEEP_BACKEND = _s["beep_backend"]
DEVICE_NAME  = _s["device_name"]
vad_enabled  = _s["vad_enabled"]
hot_mic      = _s["hot_mic"]
for _attr, _setting, _default in [
    ("PTT_KEY",     "ptt_key",     keyboard.Key.ctrl_r),
    ("HOT_MIC_KEY", "hot_mic_key", keyboard.Key.f10),
    ("VAD_KEY",     "vad_key",     keyboard.Key.f8),
]:
    try:
        globals()[_attr] = _str_to_key(_s[_setting])
    except (KeyError, AttributeError):
        globals()[_attr] = _default

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
                update_tray()
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
                update_tray()

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
                        update_tray()
                        speech_buf = np.array([], dtype=np.float32)
                        vad.reset_states()
                        cooldown_until = time.time() + VAD_COOLDOWN_SECS
                        logging.info("Max time, no wake phrase, cooldown %ss", VAD_COOLDOWN_SECS)
                    else:
                        with state_lock:
                            state = State.BUFFERING
                        update_tray()
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
                    update_tray()
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
                    update_tray()
                    speech_buf = np.array([], dtype=np.float32)
                    vad.reset_states()
                    logging.info("Done, back to idle")
                else:
                    # Wake phrase found but no "over" yet — keep recording
                    with state_lock:
                        state = State.BUFFERING
                    update_tray()
                    silence_time = 0.0
                    logging.info("Wake phrase found, waiting for 'over'...")
                    duck_audio()

# ── Tray icon ────────────────────────────────────────────────────────
_STATE_COLORS = {
    State.IDLE:       (90,  90,  90),
    State.BUFFERING:  (50,  200,  50),
    State.MANUAL:     (50,  200,  50),
    State.CHECKING:   (220, 200,   0),
    State.PROCESSING: (220, 200,   0),
}

def _make_tray_image(state_val: State, hot_mic_active: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=_STATE_COLORS.get(state_val, (90, 90, 90)))
    if hot_mic_active:
        draw.ellipse([42, 42, 58, 58], fill=(30, 130, 255))  # blue dot = hot mic
    return img

_tray_icon: "pystray.Icon | None" = None  # strong ref to prevent GC

def update_tray() -> None:
    if _tray_icon is None:
        return
    try:
        with state_lock:
            cur = state
        _tray_icon.icon  = _make_tray_image(cur, hot_mic)
        _tray_icon.title = f"PTT: {cur.name}"
        _tray_icon.update_menu()
    except Exception:
        logging.exception("update_tray failed")

# ── Tray menu callbacks ──────────────────────────────────────────────
_DUCK_LEVELS = (0.0, 0.1, 0.25, 0.5)

def _on_toggle_vad(icon, item):
    global vad_enabled
    vad_enabled = not vad_enabled
    save_settings()
    update_tray()

def _on_toggle_hot_mic(icon, item):
    global hot_mic
    hot_mic = not hot_mic
    save_settings()
    update_tray()

def _on_set_duck(level):
    def cb(icon, item):
        global DUCK_LEVEL
        DUCK_LEVEL = level
        save_settings()
        update_tray()
    return cb

def _on_set_beep(backend):
    def cb(icon, item):
        global BEEP_BACKEND
        BEEP_BACKEND = backend
        save_settings()
        update_tray()
    return cb

def _on_set_device(name):
    def cb(icon, item):
        global DEVICE_NAME
        DEVICE_NAME = name
        save_settings()
        _restart_event.set()
    return cb

def _on_bind(target: str):
    """Enter binding mode for the given target key."""
    def cb(icon, item):
        global _binding_mode
        with _binding_lock:
            _binding_mode = target
        if _tray_icon:
            _tray_icon.title = f"Press any key for {target.replace('_', ' ')}... (Esc to cancel)"
        logging.info(f"Binding mode: {target}")
    return cb

def _on_restart(icon, item):
    _restart_event.set()

def _on_quit(icon, item):
    icon.stop()
    os._exit(0)   # sys.exit() is swallowed by pynput's blocked .join()

def _get_input_devices():
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0]

def build_menu():
    duck_items = [pystray.MenuItem(f"{int(l*100)}%", _on_set_duck(l),
                  checked=lambda item, l=l: DUCK_LEVEL == l, radio=True)
                  for l in _DUCK_LEVELS]
    beep_items = [pystray.MenuItem(b, _on_set_beep(b),
                  checked=lambda item, b=b: BEEP_BACKEND == b, radio=True)
                  for b in ("winsound", "sounddevice")]
    mic_items  = [pystray.MenuItem(name, _on_set_device(name),
                  checked=lambda item, n=name: DEVICE_NAME == n, radio=True)
                  for _, name in _get_input_devices()]
    hotkey_items = pystray.Menu(
        pystray.MenuItem(lambda item: f"PTT key: {_key_label(PTT_KEY)}",
                         _on_bind("ptt_key")),
        pystray.MenuItem(lambda item: f"Hot mic key: {_key_label(HOT_MIC_KEY)}",
                         _on_bind("hot_mic_key")),
        pystray.MenuItem(lambda item: f"VAD key: {_key_label(VAD_KEY)}",
                         _on_bind("vad_key")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("(click a key to rebind, Esc to cancel)", None, enabled=False),
    )
    return pystray.Menu(
        pystray.MenuItem(lambda item: f"PTT: {state.name}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("VAD enabled", _on_toggle_vad, checked=lambda item: vad_enabled),
        pystray.MenuItem("Hot mic",     _on_toggle_hot_mic, checked=lambda item: hot_mic),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Duck level",   pystray.Menu(*duck_items)),
        pystray.MenuItem("Beep backend", pystray.Menu(*beep_items)),
        pystray.MenuItem("Microphone",   pystray.Menu(*mic_items)),
        pystray.MenuItem("Hotkeys",      hotkey_items),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart PTT", _on_restart),
        pystray.MenuItem("Quit",        _on_quit),
    )

def start_tray():
    global _tray_icon
    icon = pystray.Icon("whisper-ptt", _make_tray_image(State.IDLE, False),
                        "PTT: IDLE", build_menu())
    _tray_icon = icon
    threading.Thread(target=icon.run, name="tray-thread", daemon=True).start()
    logging.info("System tray started")

# ── Binding capture ──────────────────────────────────────────────────
def _finish_bind(mode: str, key: keyboard.Key | keyboard.KeyCode) -> None:
    """Assign the captured key to the binding target and persist."""
    global PTT_KEY, HOT_MIC_KEY, VAD_KEY, _binding_mode
    if key == keyboard.Key.esc:          # Escape cancels without changing anything
        logging.info("Bind cancelled (Escape)")
    else:
        if mode == "ptt_key":
            PTT_KEY = key
        elif mode == "hot_mic_key":
            HOT_MIC_KEY = key
        elif mode == "vad_key":
            VAD_KEY = key
        logging.info(f"Bound {mode} → {_key_label(key)}")
        save_settings()
    with _binding_lock:
        _binding_mode = None
    update_tray()

# ── Keyboard handlers ──────────────────────────────────────────────
def on_press(key):
    global state, manual_chunks
    try:
        # Binding mode intercept — capture next key press as new binding
        with _binding_lock:
            mode = _binding_mode
        if mode is not None:
            _finish_bind(mode, key)
            return    # don't let the key also trigger its normal action

        if key == PTT_KEY:
            with state_lock:
                if state == State.MANUAL:
                    return  # already recording
                # Interrupt any VAD state
                prev = state
                state = State.MANUAL
            update_tray()
            manual_chunks = []
            if prev in (State.BUFFERING, State.CHECKING):
                restore_audio()
                logging.info("RCtrl interrupted VAD recording")
            duck_audio()
            # Cute press chirp (ascending blip)
            beep_async([(784, 40), (1047, 50)])  # G5, C6
            logging.info("RCtrl: recording")

        elif key == HOT_MIC_KEY:
            global hot_mic
            hot_mic = not hot_mic
            logging.info(f"Hot mic {'ON' if hot_mic else 'OFF'}")
            save_settings()
            # Double beep = on, single low = off
            if hot_mic:
                # Sticky keys on: ascending
                beep_async([(523, 80), (587, 80), (659, 80)])
            else:
                # Sticky keys off: descending
                beep_async([(659, 80), (587, 80), (523, 80)])
            update_tray()

        elif key == VAD_KEY:
            global vad_enabled
            vad_enabled = not vad_enabled
            logging.info(f"VAD {'enabled' if vad_enabled else 'disabled'}")
            save_settings()
            beep_async([(800 if vad_enabled else 400, 150)])
            update_tray()

    except Exception as e:
        logging.exception("Error in on_press")

def on_release(key):
    global state, manual_chunks
    try:
        # Ignore release events during binding mode
        with _binding_lock:
            if _binding_mode is not None:
                return

        if key == PTT_KEY:
            with state_lock:
                if state != State.MANUAL:
                    return
                state = State.PROCESSING
            update_tray()

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
            update_tray()

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
                update_tray()
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
                update_tray()

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
                update_tray()

    except Exception as e:
        logging.exception("Error in on_click")

# ── Main ────────────────────────────────────────────────────────────
def run_listener():
    global vad_model

    _restart_event.clear()

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
            while kb_listener.is_alive() and mouse_listener.is_alive():
                if _restart_event.is_set():
                    kb_listener.stop()
                    mouse_listener.stop()
                    break
                _restart_event.wait(timeout=0.25)
    return True

def main():
    logging.info("PTT starting")
    start_tray()                  # once; tray outlives run_listener() restarts

    while True:
        try:
            run_listener()
            if _restart_event.is_set():
                logging.info("Restarting run_listener() (device change or explicit restart)")
                time.sleep(0.5)   # let InputStream teardown fully close the device
                continue
            time.sleep(5)         # device-not-found or other clean exit
        except Exception:
            logging.exception("Listener crashed, restarting in 5s")
            time.sleep(5)

if __name__ == "__main__":
    main()
