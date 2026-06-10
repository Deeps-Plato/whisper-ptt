"""Push-to-talk + voice-activated dictation with faster-whisper and silero-vad.

Right Ctrl hold OR front thumb button (x2) hold: manual PTT (radio commands)
  (both the PTT key and the PTT mouse button are configurable from the tray menu)
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
import difflib
import urllib.request
import urllib.parse
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
DUCK_LEVEL = 0.05
BEEP_BACKEND = "sounddevice"  # "sounddevice" (volume-adjustable, low latency) or "winsound" (loud, fixed)
BEEP_VOLUME = 0.10  # 0.0–1.0 amplitude for sounddevice beeps; winsound ignores this
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
MANUAL_OVER = False   # opt-in: ending a manual-PTT dictation with "over" presses Enter
COMMAND_PREFIX = "command"   # dictations starting with this word run voice_commands
                             # from dictionary.json instead of pasting text
VAD_THRESHOLD = 0.5
SILENCE_CHECK_SECS = 2.0    # silence before checking for "over"/"disregard"
MAX_DICTATION_SECS = 300.0  # 5 min safety timeout
VAD_CHUNK = 512              # silero requires exactly 512 samples @ 16kHz
VAD_COOLDOWN_SECS = 5.0      # pause VAD after failed wake-phrase check
LEXICAL_OVERRIDES = {
    "cis": "sys",
    "ray": "Rei",
}

# Ollama cleanup pass (optional LLM polish of transcriptions; tray-toggleable)
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:14b"
OLLAMA_CLEANUP = False           # default off — adds ~0.5-2s latency per dictation
OLLAMA_TIMEOUT_SECS = 6.0        # on timeout/error we paste the raw transcript instead

# ── Hotkey bindings (overwritten by load_settings at startup) ────────
PTT_KEY          = keyboard.Key.ctrl_r   # keyboard PTT hold
HOT_MIC_KEY      = keyboard.Key.f10      # toggle hot mic
VAD_KEY          = keyboard.Key.f8       # toggle VAD
TEACH_KEY        = keyboard.Key.f7       # learn corrections from selected fixed text
CAPTURE_KEY      = keyboard.Key.f15      # note-capture PTT: silent append to your note
REPASTE_KEY      = keyboard.Key.f16      # re-paste the last transcript at current focus
CAPTURE_STRUCT_KEY = keyboard.Key.f17    # like capture, but the ramble is restructured
                                         # into bullets/action items before it lands
PTT_MOUSE_BUTTON = mouse.Button.x2       # mouse PTT hold (front thumb button)

# Note-capture: hold CAPTURE_KEY, talk, release — the transcript is delivered
# to your note WITHOUT touching the screen. Delivery modes (first set wins):
#   CAPTURE_FILE — append CAPTURE_ENTRY directly to a file path template.
#     Completely silent: no window opens, no focus moves (a note app like
#     Obsidian live-reloads the file). Preferred.
#   CAPTURE_TEXT_URI — deliver via a URI handler ({text} placeholder,
#     URL-encoded). The handler app will usually raise its window.
#   neither — fall back to a normal clipboard paste at the cursor.
# CAPTURE_URI optionally opens a URI on PRESS (visual feedback) — empty skips.
# {date}/{time}/{text} are substituted in all templates.
CAPTURE_URI = ""
CAPTURE_TEXT_URI = ""
CAPTURE_FILE = ""          # e.g. C:\\vault\\Quick Notes\\{date}.md
CAPTURE_ENTRY = "\n\n## {time}\n{text}\n"
CAPTURE_WINDOW_HINT = ""   # window-title substring to focus after press-open (best-effort)

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

def _button_to_str(button: mouse.Button) -> str:
    """Serialize a pynput mouse button to a JSON-safe string."""
    return button.name              # e.g. "middle", "x1", "x2"

def _str_to_button(s: str) -> mouse.Button:
    """Deserialize a pynput mouse button from a JSON string."""
    return mouse.Button[s]          # raises KeyError on unknown name — caller handles

def _button_label(button: mouse.Button) -> str:
    """Human-readable mouse-button name for display in the tray menu."""
    return button.name.replace("_", " ").title()   # "middle" → "Middle", "x1" → "X1"

# ── Settings persistence ─────────────────────────────────────────────
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ptt-settings.json")

_SETTINGS_DEFAULTS = {
    "duck_level": DUCK_LEVEL, "beep_backend": BEEP_BACKEND, "beep_volume": BEEP_VOLUME,
    "device_name": DEVICE_NAME, "model_size": MODEL_SIZE,
    "vad_enabled": False, "hot_mic": False,
    "ptt_key": "ctrl_r", "hot_mic_key": "f10", "vad_key": "f8", "teach_key": "f7",
    "capture_key": "f15", "repaste_key": "f16", "capture_struct_key": "f17",
    "ptt_mouse_button": "x2",
    "capture_uri": CAPTURE_URI, "capture_text_uri": CAPTURE_TEXT_URI,
    "capture_file": CAPTURE_FILE, "capture_entry": CAPTURE_ENTRY,
    "capture_window_hint": CAPTURE_WINDOW_HINT,
    "ollama_cleanup": OLLAMA_CLEANUP, "ollama_model": OLLAMA_MODEL,
    "indicator": True, "manual_over": MANUAL_OVER,
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
        "duck_level": DUCK_LEVEL, "beep_backend": BEEP_BACKEND, "beep_volume": BEEP_VOLUME,
        "device_name": DEVICE_NAME, "model_size": MODEL_SIZE,
        "vad_enabled": vad_enabled, "hot_mic": hot_mic,
        "ptt_key": _key_to_str(PTT_KEY),
        "hot_mic_key": _key_to_str(HOT_MIC_KEY),
        "vad_key": _key_to_str(VAD_KEY),
        "teach_key": _key_to_str(TEACH_KEY),
        "capture_key": _key_to_str(CAPTURE_KEY),
        "repaste_key": _key_to_str(REPASTE_KEY),
        "capture_struct_key": _key_to_str(CAPTURE_STRUCT_KEY),
        "ptt_mouse_button": _button_to_str(PTT_MOUSE_BUTTON),
        "capture_uri": CAPTURE_URI, "capture_text_uri": CAPTURE_TEXT_URI,
        "capture_file": CAPTURE_FILE, "capture_entry": CAPTURE_ENTRY,
        "capture_window_hint": CAPTURE_WINDOW_HINT,
        "ollama_cleanup": OLLAMA_CLEANUP, "ollama_model": OLLAMA_MODEL,
        "indicator": INDICATOR, "manual_over": MANUAL_OVER,
    }
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        logging.exception("save_settings failed")

# ── Managed dictionary (vocab + corrections) ─────────────────────────
# dictionary.json next to ptt.py:
#   prompt_prefix — free-text context fed to Whisper before the vocab list
#   vocab         — terms Whisper should recognize (ordered most-important first;
#                   trimmed from the end if the prompt budget is exceeded)
#   corrections   — {"misheard phrase": "Correct Form"} applied to every transcript
# Created from the in-code defaults on first run. Machine-local (gitignored).
DICT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictionary.json")
PROMPT_TOKEN_BUDGET = 200   # whisper conditions on the last ~224 prompt tokens; stay under
                            # so the prefix is never silently pushed out of the window

_dictionary = {"prompt_prefix": "", "vocab": [], "corrections": {}}
EFFECTIVE_PROMPT = INITIAL_PROMPT

def _default_dictionary():
    """First-run migration: preserve the legacy in-code INITIAL_PROMPT and
    LEXICAL_OVERRIDES so behavior is unchanged until the user edits the file."""
    return {
        "prompt_prefix": INITIAL_PROMPT,
        "vocab": [],
        "corrections": dict(LEXICAL_OVERRIDES),
    }

def build_initial_prompt(d):
    vocab = list(dict.fromkeys(d.get("vocab") or []))   # dedupe, keep order
    prefix = (d.get("prompt_prefix") or "").strip()

    def render(words):
        parts = [prefix] if prefix else []
        if words:
            parts.append("Vocabulary: " + ", ".join(words) + ".")
        return " ".join(parts)

    prompt = render(vocab)
    while vocab and len(prompt) // 3 > PROMPT_TOKEN_BUDGET:   # ~3 chars/token heuristic
        vocab.pop()
        prompt = render(vocab)
    if len(prompt) // 3 > PROMPT_TOKEN_BUDGET:
        logging.warning("dictionary: prompt_prefix alone exceeds the prompt budget")
    return prompt

def load_dictionary():
    global _dictionary, EFFECTIVE_PROMPT, _dict_mtime
    try:
        with open(DICT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        _dict_mtime = os.path.getmtime(DICT_FILE)
    except FileNotFoundError:
        d = _default_dictionary()
        try:
            with open(DICT_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            logging.info(f"dictionary: created {DICT_FILE} from in-code defaults")
        except Exception:
            logging.exception("dictionary: could not write default file")
    except Exception:
        logging.exception("dictionary: load failed, using in-code defaults")
        d = _default_dictionary()
    if not isinstance(d, dict):
        d = _default_dictionary()
    d.setdefault("prompt_prefix", "")
    d.setdefault("vocab", [])
    d.setdefault("corrections", {})
    d.setdefault("app_profiles", {})
    d.setdefault("voice_commands", {})   # "phrase": "keys:..." | "run:..." | "open:..."
    d.setdefault("_teach_history", [])   # last N teach events, for undo
    _dictionary = d
    EFFECTIVE_PROMPT = build_initial_prompt(d)
    logging.info(f"dictionary: {len(d['vocab'])} vocab terms, "
                 f"{len(d['corrections'])} corrections")
    return d

def save_dictionary():
    global _dict_mtime
    tmp = DICT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_dictionary, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DICT_FILE)
        _dict_mtime = os.path.getmtime(DICT_FILE)
    except Exception:
        logging.exception("save_dictionary failed")

_dict_mtime = 0.0

def _maybe_reload_dictionary():
    """Hot-reload: if dictionary.json changed on disk (GUI editor, manual edit,
    another tool), pick it up before the next transcription."""
    global _dict_mtime
    try:
        m = os.path.getmtime(DICT_FILE)
    except OSError:
        return
    if m != _dict_mtime:
        _dict_mtime = m
        load_dictionary()
        logging.info("dictionary: hot-reloaded (file changed on disk)")

# App profiles may be a plain string (style instruction) or an object with
# optional per-app extensions: {"style": ..., "vocab": [...], "corrections": {...}}
def _profile_field(profile, key, default=None):
    return profile.get(key, default) if isinstance(profile, dict) else default

def _profile_style(profile):
    if isinstance(profile, str):
        return profile
    return _profile_field(profile, "style", "") or ""

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
_binding_mode: str | None = None   # "ptt_key" | "hot_mic_key" | "vad_key" | "ptt_mouse_button" | None
_binding_lock  = threading.Lock()  # guards _binding_mode reads/writes

# Serialize beeps to avoid overlap/stutter and log failures
beep_lock = threading.Lock()
_BEEP_SR = 44100
_BEEP_FADE_MS = 6            # attack/release ramp — removes the click that makes beeps feel harsh
_beep_stream = None          # persistent output stream so beeps fire without per-call device-open lag

def _get_beep_stream():
    global _beep_stream
    if _beep_stream is None:
        stream = sd.OutputStream(samplerate=_BEEP_SR, channels=1, dtype="float32")
        stream.start()
        _beep_stream = stream
    return _beep_stream

# Chirps as (freq_hz, duration_ms[, gain]). Equal durations + a small gain lift on the
# lower tone keep both halves of a chirp at the same perceived loudness.
PRESS_CHIRP   = [(784, 55, 1.18), (1047, 55)]   # G5 → C6 (ascending blip)
RELEASE_CHIRP = [(1047, 55), (784, 55, 1.12)]   # C6 → G5 (descending boop)

def _sd_beep(tones):
    stream = _get_beep_stream()
    fade = int(_BEEP_SR * _BEEP_FADE_MS / 1000.0)
    for tone in tones:
        freq, dur_ms = tone[0], tone[1]
        gain = tone[2] if len(tone) > 2 else 1.0
        amp = min(0.95, BEEP_VOLUME * gain)
        n = int(_BEEP_SR * max(dur_ms, 1) / 1000.0)
        t = np.arange(n, dtype=np.float32) / _BEEP_SR
        wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        if n > 2 * fade:
            wave[:fade]  *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
            wave[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
        stream.write(wave)

def _play_beeps(tones):
    global _beep_stream
    try:
        with beep_lock:
            if BEEP_BACKEND == "winsound":
                for tone in tones:
                    winsound.Beep(tone[0], tone[1])
            elif BEEP_VOLUME > 0:
                _sd_beep(tones)
    except Exception:
        logging.exception("Beep failed")
        _beep_stream = None   # drop the stream so it reopens next time (device may have changed)

def beep_async(tones, then=None):
    """Play tones on a background thread; run optional `then` callback once they finish."""
    def run():
        _play_beeps(tones)
        if then is not None:
            try:
                then()
            except Exception:
                logging.exception("beep 'then' callback failed")
    threading.Thread(target=run, daemon=True).start()

vad_enabled = False  # F8 to enable; default off to avoid GPU churn
hot_mic = False  # hot mic: no wake phrase needed, just talk
audio_q = queue.Queue(maxsize=200)

# Apply saved settings — overwrites Config defaults with persisted values
_s = load_settings()
DUCK_LEVEL   = _s["duck_level"]
BEEP_BACKEND = _s["beep_backend"]
BEEP_VOLUME  = _s["beep_volume"]
DEVICE_NAME  = _s["device_name"]
MODEL_SIZE   = _s["model_size"]
vad_enabled  = _s["vad_enabled"]
hot_mic      = _s["hot_mic"]
OLLAMA_CLEANUP = bool(_s["ollama_cleanup"])
OLLAMA_MODEL   = _s["ollama_model"]
INDICATOR      = bool(_s["indicator"])
MANUAL_OVER    = bool(_s["manual_over"])
CAPTURE_URI         = _s["capture_uri"]
CAPTURE_TEXT_URI    = _s["capture_text_uri"]
CAPTURE_FILE        = _s["capture_file"]
CAPTURE_ENTRY       = _s["capture_entry"]
CAPTURE_WINDOW_HINT = _s["capture_window_hint"]
for _attr, _setting, _default in [
    ("PTT_KEY",     "ptt_key",     keyboard.Key.ctrl_r),
    ("HOT_MIC_KEY", "hot_mic_key", keyboard.Key.f10),
    ("VAD_KEY",     "vad_key",     keyboard.Key.f8),
    ("TEACH_KEY",   "teach_key",   keyboard.Key.f7),
    ("CAPTURE_KEY", "capture_key", keyboard.Key.f15),
    ("REPASTE_KEY", "repaste_key", keyboard.Key.f16),
    ("CAPTURE_STRUCT_KEY", "capture_struct_key", keyboard.Key.f17),
]:
    try:
        globals()[_attr] = _str_to_key(_s[_setting])
    except (KeyError, AttributeError):
        globals()[_attr] = _default
try:
    PTT_MOUSE_BUTTON = _str_to_button(_s["ptt_mouse_button"])
except (KeyError, AttributeError):
    PTT_MOUSE_BUTTON = mouse.Button.x2

load_dictionary()   # builds EFFECTIVE_PROMPT; creates dictionary.json on first run

# ── Globals ─────────────────────────────────────────────────────────
whisper_model = None
vad_model = None
saved_volumes = {}   # {session_instance_id: original_volume}
duck_lock = threading.Lock()
_is_ducked = False
RESTORE_DELAY = 0.12           # seconds after the release beep before un-ducking
_VOL_TOLERANCE = 0.02          # treat a session within this of its target as restored
_RESTORE_VERIFY_PASSES = 4     # re-check/re-apply restored volumes up to this many times
manual_chunks = []  # chunks collected during F9 hold
_capture_session = False   # current manual recording was started by a capture key
_capture_struct = False    # ...by the STRUCTURED capture key specifically
key_sender = keyboard.Controller()

# ── Device ──────────────────────────────────────────────────────────
def find_device():
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if DEVICE_NAME.lower() in d['name'].lower() and d['max_input_channels'] > 0:
            return i
    return None

# ── Models ──────────────────────────────────────────────────────────
def _compute_candidates():
    """(device, compute_type) pairs to try, best-first, for the current hardware.

    float16 is only efficient on CUDA capability >= 7.0 (Turing/Ampere+). Pascal cards
    (GTX 10xx, cap 6.x) raise "target device or backend do not support efficient float16",
    so prefer int8 there. Falls back to CPU int8 if CUDA is unavailable or all GPU types fail.
    """
    if torch.cuda.is_available():
        major = torch.cuda.get_device_capability(0)[0]
        if major >= 7:
            return [("cuda", "float16"), ("cuda", "int8_float16"),
                    ("cuda", "int8"), ("cpu", "int8")]
        return [("cuda", "int8"), ("cpu", "int8")]   # Pascal & older
    return [("cpu", "int8")]

def load_whisper():
    global whisper_model
    if whisper_model is not None:
        return whisper_model
    last_err = None
    for dev, ctype in _compute_candidates():
        try:
            logging.info(f"Loading whisper model ({dev}/{ctype})...")
            whisper_model = WhisperModel(MODEL_SIZE, device=dev, compute_type=ctype)
            logging.info(f"Whisper ready ({dev}/{ctype})")
            return whisper_model
        except Exception as e:
            last_err = e
            logging.warning(f"Whisper load failed for {dev}/{ctype}: {e}")
    logging.error("All whisper compute types failed")
    raise last_err

def apply_corrections(text, corrections=None):
    """Force preferred replacements on Whisper output (case-insensitive,
    whole-word, multi-word keys supported). Longest keys first so
    "us web ship" wins over "web ship". Applied twice: a single-word fix
    (janssen→Janszen) can make a multi-word key match on the second pass
    ("janszen discount products"→"Janszen Discount Products")."""
    if not text:
        return text
    corr = corrections if corrections is not None else _dictionary.get("corrections") or {}
    ordered = sorted(corr, key=len, reverse=True)
    for _pass in range(2):
        before = text
        for source in ordered:
            text = re.sub(rf'\b{re.escape(source)}\b', corr[source], text, flags=re.IGNORECASE)
        if text == before:
            break
    return text

def transcribe(audio):
    _maybe_reload_dictionary()
    # Per-app dictionary: the focused window's profile can extend the global
    # vocab (prompt) and corrections for this transcription only.
    window = _active_window_title()
    profile = _app_profile_for(window, _dictionary.get("app_profiles"))
    prompt = EFFECTIVE_PROMPT
    app_vocab = _profile_field(profile, "vocab") or []
    if app_vocab:
        prompt = build_initial_prompt({
            "prompt_prefix": _dictionary.get("prompt_prefix", ""),
            "vocab": list(app_vocab) + list(_dictionary.get("vocab") or []),
        })
    m = load_whisper()
    segments, _ = m.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                initial_prompt=prompt)
    text = " ".join(seg.text.strip() for seg in segments)
    corr = dict(_dictionary.get("corrections") or {})
    corr.update(_profile_field(profile, "corrections") or {})   # app wins on clash
    return apply_corrections(text, corr)

# ── Ollama cleanup pass ──────────────────────────────────────────────
_OLLAMA_INSTRUCTION = (
    "Fix transcription errors, capitalization, and punctuation in the dictated text. "
    "Do NOT rephrase, summarize, expand, or add content. NEVER insert words, "
    "abbreviations, parentheses, or annotations that were not spoken — the vocabulary "
    "list is for spelling reference only, not for insertion. Capitalize only sentence "
    "starts and proper nouns; do not capitalize ordinary mid-sentence words. Preserve "
    "all symbols, slashes, numbers, dollar signs, and line breaks exactly. "
    "Speech-to-text often inserts a period where the speaker merely paused — merge "
    "those fragments back into one sentence. Return ONLY the corrected text.\n"
    "\n"
    "Example input: I told the customer we'd email the BOL and label. Together. "
    "And the driver, isaiah, Said the rate stands at $675.\n"
    "Example output: I told the customer we'd email the BOL and label together, "
    "and the driver, Isaiah, said the rate stands at $675.\n"
    "\n"
    "Example input: The Consignee is a hospital dock. so some carriers post-bill "
    "limited access\n"
    "Example output: The consignee is a hospital dock, so some carriers post-bill "
    "limited access."
)

def _app_profile_for(title, profiles):
    """Match a window title against app_profiles keys. Keys are |-separated
    case-insensitive title substrings ("outlook", "powershell|cmd|wsl").
    First matching profile (insertion order) wins; None if no match."""
    t = (title or "").lower()
    if not t:
        return None
    for key, instruction in (profiles or {}).items():
        for alt in key.split("|"):
            alt = alt.strip().lower()
            if alt and alt in t:
                return instruction
    return None

_CLEANUP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanup-log.jsonl")

def _log_cleanup_pair(raw, cleaned, model, duration, window):
    """Append a (raw → cleaned) pair for later prompt refinement / tuning.
    Local file, audit + future fine-tune corpus."""
    try:
        with open(_CLEANUP_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "window": window, "model": model,
                "duration_s": round(duration, 2),
                "raw": raw, "cleaned": cleaned,
            }, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("cleanup pair log failed")

def llm_cleanup(text):
    """Optional local-LLM polish via Ollama. Hard timeout; any failure returns
    the raw transcript so dictation never hangs on a cold/slow model (a timed-out
    request still warms the model server-side for the next one)."""
    if not OLLAMA_CLEANUP or not text or not text.strip():
        return text
    vocab_hint = ", ".join((_dictionary.get("vocab") or [])[:40])
    window = _active_window_title()
    profile = _app_profile_for(window, _dictionary.get("app_profiles"))
    style = _profile_style(profile)
    if profile is not None and (profile is False
                                or style.strip().lower() in ("skip", "off", "raw")):
        logging.info(f"ollama: app profile says skip for window {window!r}")
        return text
    prompt = _OLLAMA_INSTRUCTION
    if vocab_hint:
        prompt += f"\nKnown vocabulary (use these exact spellings): {vocab_hint}"
    if window:
        # App context (window TITLE only — content is never read): lets the
        # model match register, e.g. an email vs a quick note vs a chat.
        prompt += f"\nThe text is being dictated into this window: {window}"
    if style:
        prompt += f"\nStyle for this app: {style}"
    prompt += f"\n\nText: {text}"
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0},
    }).encode("utf-8")
    # Long dictations produce proportionally long cleanups — scale the timeout
    # with text length so long-form doesn't silently fall back to raw.
    timeout = OLLAMA_TIMEOUT_SECS + len(text) / 200.0
    t0 = time.time()
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
        logging.info(f"ollama cleanup: {len(text)} chars in {time.time()-t0:.1f}s")
        out = out.strip('"').strip()
        # Guardrail: the model must not rewrite/expand — big length drift means
        # it disobeyed (hallucinated or summarized), so keep the raw transcript.
        if not out or len(out) > len(text) * 1.6 + 20 or len(out) < len(text) * 0.4:
            logging.warning("ollama: output failed sanity check, using raw text")
            _log_cleanup_pair(text, "[REJECTED] " + (out or ""), OLLAMA_MODEL,
                              time.time() - t0, window)
            return text
        _log_cleanup_pair(text, out, OLLAMA_MODEL, time.time() - t0, window)
        return out
    except Exception as e:
        logging.warning(f"ollama cleanup unavailable ({e}); using raw text")
        return text

# ── Audio ducking ───────────────────────────────────────────────────
def _session_key(s):
    """Stable identifier for an audio session, robust to enumeration-order changes.

    Multi-session apps (Discord runs several at once) get mis-matched when keyed by
    position, leaving the actually-ducked session stuck low. The Windows session
    *instance* identifier is unique per session and stable for its lifetime.
    """
    try:
        sid = s.InstanceIdentifier
        if sid:
            return sid
    except Exception:
        pass
    try:
        sid = s._ctl.GetSessionInstanceIdentifier()
        if sid:
            return sid
    except Exception:
        pass
    return f"pid:{s.Process.pid if s.Process else 0}"   # last-resort fallback

def duck_audio():
    global saved_volumes, _is_ducked
    with duck_lock:
        if _is_ducked:
            logging.info("duck_audio: already ducked, skipping")
            return
        new_saved = {}
        try:
            CoInitialize()
            for s in AudioUtilities.GetAllSessions():
                if not s.Process:
                    continue
                vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                orig = vol.GetMasterVolume()
                # Don't poison the saved original with an already-ducked value: if a prior
                # restore failed and left this session low, skip ducking it further.
                if orig <= DUCK_LEVEL + _VOL_TOLERANCE:
                    logging.warning(f"duck: {_session_key(s)} already at {orig:.3f} "
                                    f"(<= duck level); leaving it alone")
                    continue
                new_saved[_session_key(s)] = orig
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
    """Un-duck and verify: re-apply each saved volume, then re-check a few times and
    re-set any that didn't take. Some apps silently ignore the first SetMasterVolume,
    which is what leaves them stuck low in the Windows volume mixer."""
    global saved_volumes, _is_ducked
    with duck_lock:
        if not _is_ducked:
            logging.info("restore_audio: not ducked, skipping")
            return
        targets = dict(saved_volumes)   # session_key -> original volume
        unrestored = set(targets)
        try:
            CoInitialize()
            for attempt in range(_RESTORE_VERIFY_PASSES):
                if not unrestored:
                    break
                if attempt:
                    time.sleep(0.04)
                present = set()
                for s in AudioUtilities.GetAllSessions():
                    if not s.Process:
                        continue
                    key = _session_key(s)
                    if key not in targets:
                        continue
                    present.add(key)
                    vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                    orig = targets[key]
                    if abs(vol.GetMasterVolume() - orig) <= _VOL_TOLERANCE:
                        unrestored.discard(key)   # already at target
                    else:
                        vol.SetMasterVolume(orig, None)   # didn't take — re-apply, verify next pass
                unrestored &= present   # sessions that have since closed can't (and needn't) be restored
            if unrestored:
                logging.warning(f"restore: {len(unrestored)} session(s) not verified back to "
                                f"original volume: {sorted(unrestored)}")
            logging.info(f"Restored {len(targets)} sessions "
                         f"({len(targets) - len(unrestored)} verified)")
        except Exception:
            logging.exception("Restore failed")
        finally:
            saved_volumes = {}
            _is_ducked = False
            CoUninitialize()

def _delayed_restore():
    """Un-duck a moment after the release beep so the chirp isn't masked by other apps
    jumping back to full volume. Skips the restore if recording resumed during the wait."""
    time.sleep(RESTORE_DELAY)
    with state_lock:
        if state in (State.MANUAL, State.BUFFERING, State.CHECKING):
            return   # pressed again during the delay — stay ducked; the new cycle will restore
    restore_audio()

# ── Radio commands ──────────────────────────────────────────────────
def strip_punctuation(word):
    """Strip trailing punctuation that whisper often adds."""
    return re.sub(r'[.,!?;:]+$', '', word)

def process_commands(text, radio=True):
    """Process radio commands. Returns (cleaned_text, press_enter).

    radio=True enables break/over/correction/disregard (voice-activation only).
    radio="over" enables ONLY the trailing-"over"-presses-Enter command —
    the manual-PTT opt-in (MANUAL_OVER setting). Other commands stay off so
    ordinary speech can't delete words or cancel an utterance.
    radio=False skips all commands (manual PTT — just transcribe literally).
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
    if radio is True:
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
        elif radio is True and cleaned == "break":
            result.append("\n")
        elif radio and cleaned == "over" and i == len(words) - 1:
            press_enter = True
            # Preserve ?/! from "over?" onto previous word
            trailing = w[len(cleaned):]  # e.g. "?" from "over?"
            keep = ''.join(c for c in trailing if c in '?!')
            if keep and result:
                result[-1] = result[-1].rstrip('.,;:?!') + keep
        elif radio is True and cleaned == "correction":
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

_last_pasted_text = ""   # most recent injected dictation — diff target for teach mode

def paste_text(text, press_enter=False):
    """Type into terminal-like windows, otherwise clipboard-paste with restore."""
    global _last_pasted_text
    if not text and not press_enter:
        return
    if text:
        _last_pasted_text = text

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

# ── Teach mode (Wispr-style correction learning, explicit gesture) ───
# Fix the pasted text in place, select the corrected version, hit the teach key.
# We diff the selection against what we last injected, extract the changed word
# pairs, and add them to the dictionary — corrections map + vocab. No passive
# monitoring of what you type; learning only happens on the explicit gesture.

def _norm_word(w):
    """Normalize a word for diff alignment: lowercase, strip outer punctuation."""
    return re.sub(r"^[^\w&$@#~/]+|[^\w&$@#~/%+)\]]+$", "", w).lower()

def _clean_token(w):
    """Strip sentence punctuation from a learned token, keep inner symbols."""
    return re.sub(r"^[\"'(\[]+|[\"')\],.:;!?]+$", "", w)

def _extract_pairs(original, corrected, max_words=4):
    """Word-level diff → (misheard, correct) pairs.

    Only small 'replace' runs count — insertions/deletions are content edits,
    not mishearings. Case-only changes are harvested from 'equal' runs too,
    but only non-trivial casing (NMFC, TForce, InXpress): plain Capitalized
    forms are sentence mechanics and would over-learn common words."""
    ow, cw = original.split(), corrected.split()
    sm = difflib.SequenceMatcher(a=[_norm_word(w) for w in ow],
                                 b=[_norm_word(w) for w in cw], autojunk=False)
    pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace" and (i2 - i1) <= max_words and (j2 - j1) <= max_words:
            wrong = " ".join(_norm_word(w) for w in ow[i1:i2]).strip()
            right = " ".join(_clean_token(w) for w in cw[j1:j2]).strip()
            if wrong and right and wrong != right.lower():
                pairs.append((wrong, right))
        elif tag == "equal":
            for k in range(i2 - i1):
                o, c = ow[i1 + k], cw[j1 + k]
                oc, cc = _clean_token(o), _clean_token(c)
                if oc == cc or oc.lower() != cc.lower():
                    continue   # identical, or not a pure case change
                if cc == oc.capitalize():
                    continue   # ordinary capitalization, not jargon casing
                pairs.append((oc.lower(), cc))
    return pairs

def teach_from_selection():
    """Learn corrections by diffing the user's selected (fixed) text against
    the last injected dictation."""
    global EFFECTIVE_PROMPT
    last = (_last_pasted_text or "").strip()
    if not last:
        beep_async([(400, 120)])
        logging.info("teach: nothing was dictated yet")
        return
    try:
        old_clip = pyperclip.paste()
    except Exception:
        old_clip = ""
    try:
        pyautogui.hotkey('ctrl', 'c')
        time.sleep(0.15)
        corrected = (pyperclip.paste() or "").strip()
    except Exception:
        logging.exception("teach: could not read selection")
        corrected = ""
    finally:
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass
    if not corrected or corrected == old_clip.strip():
        beep_async([(400, 120)])
        logging.info("teach: no selection captured (select the corrected text first)")
        return
    if corrected == last:
        beep_async([(400, 120)])
        logging.info("teach: selection identical to last dictation, nothing to learn")
        return

    pairs = _extract_pairs(last, corrected)
    if not pairs:
        beep_async([(400, 120)])
        logging.info("teach: no learnable word-level differences found")
        return

    vocab_added = []
    for wrong, right in pairs:
        _dictionary["corrections"][wrong] = right
        # New proper nouns also go in vocab so Whisper can get them right
        # first try next time (single tokens with a capital or digit).
        if (" " not in right and right not in _dictionary["vocab"]
                and any(ch.isupper() or ch.isdigit() for ch in right)):
            _dictionary["vocab"].append(right)
            vocab_added.append(right)
    _dictionary["_teach_history"].append({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pairs": [[w, r] for w, r in pairs],
        "vocab_added": vocab_added,
    })
    del _dictionary["_teach_history"][:-20]   # keep last 20 events
    save_dictionary()
    EFFECTIVE_PROMPT = build_initial_prompt(_dictionary)
    learned = ", ".join(f"{w}→{r}" for w, r in pairs)
    logging.info(f"teach: learned {learned}")
    beep_async([(523, 70), (659, 70), (784, 90)])   # success arpeggio
    if _tray_icon:
        _tray_icon.title = f"Learned: {learned[:96]}"

def undo_last_teach():
    """Revert the most recent teach event — its corrections and any vocab it
    added. One bad learn ("bols"→"both") silently poisons every future
    dictation; this makes the teach key fearless."""
    global EFFECTIVE_PROMPT
    hist = _dictionary.get("_teach_history") or []
    if not hist:
        beep_async([(400, 120)])
        logging.info("undo teach: history is empty")
        return
    event = hist.pop()
    for wrong, right in event.get("pairs", []):
        if _dictionary["corrections"].get(wrong) == right:
            del _dictionary["corrections"][wrong]
    for word in event.get("vocab_added", []):
        if word in _dictionary["vocab"]:
            _dictionary["vocab"].remove(word)
    save_dictionary()
    EFFECTIVE_PROMPT = build_initial_prompt(_dictionary)
    undone = ", ".join(f"{w}→{r}" for w, r in event.get("pairs", []))
    logging.info(f"undo teach: reverted {undone}")
    beep_async([(784, 70), (659, 70), (523, 90)])   # descending arpeggio
    if _tray_icon:
        _tray_icon.title = f"Undid teach: {undone[:90]}"

# ── Note-capture PTT ─────────────────────────────────────────────────
def _open_capture_target():
    """Open the capture URI (e.g. an obsidian:// quick-note link) and focus the
    target window, while recording is already running. {date}/{time} in the
    template are substituted URL-encoded. Best-effort: any failure just means
    the paste lands wherever focus is, same as plain PTT."""
    try:
        now = time.localtime()
        uri = CAPTURE_URI.format(
            date=urllib.parse.quote(time.strftime("%Y-%m-%d", now)),
            time=urllib.parse.quote(time.strftime("%H:%M", now)),
        )
        os.startfile(uri)
        logging.info(f"capture: opened {uri}")
    except Exception:
        logging.exception("capture: could not open URI")
        return
    if not CAPTURE_WINDOW_HINT:
        return
    try:
        import pygetwindow as gw
        time.sleep(0.9)   # let the app raise the note
        wins = [w for w in gw.getAllWindows()
                if CAPTURE_WINDOW_HINT.lower() in (w.title or "").lower()]
        if wins:
            wins[0].activate()
            time.sleep(0.25)
            with key_sender.pressed(keyboard.Key.ctrl):
                key_sender.press(keyboard.Key.end)
                key_sender.release(keyboard.Key.end)
            logging.info(f"capture: focused '{wins[0].title}', cursor to end")
    except Exception:
        logging.exception("capture: window focus failed (paste follows focus)")

def _append_capture_text(text):
    """Deliver a capture-session transcript. File mode appends directly to the
    note file — completely silent, no window opens, no focus stolen (a vault
    app like Obsidian live-reloads the file). URI mode goes through the app's
    own handler (which usually raises its window). Both are cursor-independent."""
    now = time.localtime()
    if CAPTURE_FILE:
        path = CAPTURE_FILE.format(date=time.strftime("%Y-%m-%d", now),
                                   time=time.strftime("%H:%M", now))
        entry = CAPTURE_ENTRY.format(date=time.strftime("%Y-%m-%d", now),
                                     time=time.strftime("%H:%M", now), text=text)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        logging.info(f"capture: appended {len(text)} chars to {path}")
        return
    uri = CAPTURE_TEXT_URI.format(
        date=urllib.parse.quote(time.strftime("%Y-%m-%d", now)),
        time=urllib.parse.quote(time.strftime("%H:%M", now)),
        text=urllib.parse.quote(text),
    )
    os.startfile(uri)
    logging.info(f"capture: appended {len(text)} chars via URI")

# ── Voice commands ───────────────────────────────────────────────────
# Dictations starting with COMMAND_PREFIX run an action from the dictionary's
# voice_commands map instead of pasting text. Deterministic lookup — no LLM
# decides what executes. Actions: "keys:win+shift+s", "run:calc.exe",
# "open:obsidian://...". Hot-editable like the rest of dictionary.json.
_KEYNAMES = {
    "win": keyboard.Key.cmd, "ctrl": keyboard.Key.ctrl, "alt": keyboard.Key.alt,
    "shift": keyboard.Key.shift, "enter": keyboard.Key.enter, "tab": keyboard.Key.tab,
    "esc": keyboard.Key.esc, "space": keyboard.Key.space, "del": keyboard.Key.delete,
    "home": keyboard.Key.home, "end": keyboard.Key.end,
    "printscreen": keyboard.Key.print_screen, "prtsc": keyboard.Key.print_screen,
    **{f"f{i}": getattr(keyboard.Key, f"f{i}") for i in range(1, 21)},
}

def _match_voice_command(text, commands, prefix):
    """(action, consumed): consumed=True means the utterance was a command
    attempt (matched or not) and must NOT be pasted.

    Two key styles:
      "screenshot"        — classic: spoken as "<prefix> screenshot"
      "browse to reddit"  — full-phrase: any multi-word key whose first word
                            isn't the prefix defines its OWN trigger word, so
                            new command families need no code change.
    Exact match first, then prefix-overlap either direction."""
    if not commands:
        return None, False
    words = re.sub(r"[^\w\s+]", " ", (text or "").lower()).split()
    if not words:
        return None, False

    def lookup(phrase, keys):
        if not phrase:
            return None
        if phrase in keys:
            return commands[keys[phrase]]
        for kl in sorted(keys, key=len, reverse=True):
            if phrase.startswith(kl) or kl.startswith(phrase):
                return commands[keys[kl]]
        return None

    if words[0] == prefix.lower():
        keys = {k.lower(): k for k in commands
                if k.split()[0].lower() != words[0]}   # classic, prefix-less keys
        return lookup(" ".join(words[1:]), keys), True

    triggers = {k.split()[0].lower() for k in commands if " " in k}
    if words[0] in triggers:
        phrase = " ".join(words)
        family = {k.lower(): k for k in commands
                  if k.split()[0].lower() == words[0]}   # full-phrase keys
        plain = {kl: k for kl, k in family.items() if not kl.endswith(" *")}
        action = lookup(phrase, plain)
        if action is not None:
            return action, True
        # Parameterized keys: "search google *" captures the remainder as the
        # query — {query} is URL-encoded, {raw} is the spoken text as-is.
        for kl in sorted(family, key=len, reverse=True):
            if not kl.endswith(" *"):
                continue
            fixed = kl[:-2]
            if phrase.startswith(fixed + " "):
                remainder = phrase[len(fixed):].strip()
                if remainder:
                    tmpl = commands[family[kl]]
                    # %20 encoding (not +): every engine accepts it, and some
                    # (ClassIT) treat a literal + as part of the search text.
                    return (tmpl.replace("{query}", urllib.parse.quote(remainder, safe=""))
                                .replace("{raw}", remainder)), True
        # Near-miss (first two words match a key) → consume, beep, paste nothing.
        # Anything less is ordinary speech — leave it alone.
        two = " ".join(words[:2])
        if len(words) >= 2 and any(kl.startswith(two) for kl in family):
            return None, True
        return None, False

    return None, False

def _send_keys(spec):
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    keys = [_KEYNAMES.get(p, p) for p in parts]
    if not keys:
        return
    *mods, last = keys
    if mods:
        with key_sender.pressed(*mods):
            key_sender.press(last)
            key_sender.release(last)
    else:
        key_sender.press(last)
        key_sender.release(last)

def _execute_voice_command(action):
    import subprocess
    if action.startswith("keys:"):
        _send_keys(action[5:])
    elif action.startswith("run:"):
        subprocess.Popen(action[4:], shell=True)   # user's own config, own machine
    elif action.startswith("open:"):
        os.startfile(action[5:])
    else:
        raise ValueError(f"unknown action type: {action!r}")

def try_voice_command(text):
    """Intercept command-prefixed dictations. Returns True if consumed."""
    action, consumed = _match_voice_command(
        text, _dictionary.get("voice_commands") or {}, COMMAND_PREFIX)
    if not consumed:
        return False
    if action is None:
        logging.info(f"voice command: no match for {text!r}")
        beep_async([(400, 120)])
        return True
    try:
        _execute_voice_command(action)
        logging.info(f"voice command: {text!r} -> {action!r}")
        beep_async([(880, 60), (1175, 80)])
    except Exception:
        logging.exception(f"voice command failed: {action!r}")
        beep_async([(400, 120)])
    return True

_RESTRUCTURE_INSTRUCTION = (
    "Restructure this spoken ramble into clean, concise markdown notes. Use short "
    "bullet points; phrase any tasks or follow-ups as '- [ ] ' checkbox items. Keep "
    "ALL facts, names, numbers, and dates — do not summarize away detail, do not add "
    "commentary or content that was not spoken. Return ONLY the markdown."
)

def llm_restructure(text):
    """Turn a dictated ramble into structured markdown via Ollama. Explicit user
    intent (the struct capture key), so it runs even when the cleanup toggle is
    off. Falls back to the verbatim transcript on any failure."""
    if not text or not text.strip():
        return text
    prompt = _RESTRUCTURE_INSTRUCTION + f"\n\nTranscript: {text}"
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0},
    }).encode("utf-8")
    timeout = 10.0 + len(text) / 150.0
    t0 = time.time()
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
        # Restructuring legitimately changes length — only reject the absurd.
        if not out or len(out) > len(text) * 3 + 100 or len(out) < len(text) * 0.25:
            logging.warning("restructure: output failed sanity check, keeping verbatim")
            return text
        logging.info(f"restructure: {len(text)} chars in {time.time()-t0:.1f}s")
        return out
    except Exception as e:
        logging.warning(f"restructure unavailable ({e}); keeping verbatim")
        return text

_HISTORY_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictation-history.jsonl")

def _log_dictation(raw, final, mode, window):
    """Unified history of every manual dictation — including raw-path ones the
    cleanup log never sees. Powers the re-paste key's paper trail and audits."""
    try:
        with open(_HISTORY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode, "window": window,
                "raw": raw, "final": final,
            }, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("dictation history log failed")

def deliver_text(cleaned, press_enter, raw=""):
    """Route a manual-session transcript: capture sessions append to the note,
    plain PTT pastes at the cursor. Logs to history either way."""
    global _capture_session, _capture_struct, _last_pasted_text
    mode = ("capture-struct" if _capture_struct else
            "capture" if _capture_session else "ptt")
    try:
        if _capture_session and (CAPTURE_FILE or CAPTURE_TEXT_URI) and cleaned:
            if _capture_struct:
                cleaned = llm_restructure(cleaned)
            _append_capture_text(cleaned)
            _last_pasted_text = cleaned   # teach + re-paste track captures too
        elif cleaned or press_enter:
            paste_text(cleaned, press_enter)
    except Exception:
        logging.exception("deliver_text failed, falling back to paste")
        if cleaned or press_enter:
            paste_text(cleaned, press_enter)
    finally:
        _capture_session = False
        _capture_struct = False
        if cleaned:
            _log_dictation(raw, cleaned, mode, _active_window_title())

def repaste_last():
    """Re-paste the most recent transcript at the current focus — insurance for
    when the target app ate the paste (dialog stole focus, wrong window)."""
    last = (_last_pasted_text or "").strip()
    if not last:
        beep_async([(400, 120)])
        logging.info("repaste: nothing to re-paste")
        return
    paste_text(last)
    logging.info(f"repaste: {len(last)} chars")

# ── Audio callback ──────────────────────────────────────────────────
_mic_rms = 0.0   # live input level, feeds the recording indicator

def audio_callback(indata, frames, time_info, status):
    global _mic_rms
    try:
        _mic_rms = float(np.sqrt(np.mean(np.square(indata))))
    except Exception:
        pass
    try:
        audio_q.put_nowait(indata.copy().flatten())
    except queue.Full:
        pass  # drop chunk silently

# ── Recording indicator (speech-wave overlay) ───────────────────────
# A small always-on-top bubble near the cursor while the mic is hot: bars
# driven by live input level (blue = recording, orange pulse = transcribing).
# The window is NEVER unmapped or activated — it parks off-screen when idle —
# so it can never steal focus from the paste target. Any failure disables it
# for the session; dictation is unaffected.
INDICATOR = True

def _indicator_loop():
    try:
        import tkinter as tk
        W, H, N = 104, 34, 7          # bubble size, bar count
        OFFSCREEN = f"{W}x{H}+-{W*3}+-{H*3}"
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.92)
        except Exception:
            pass
        root.geometry(OFFSCREEN)
        canvas = tk.Canvas(root, width=W, height=H, bg="#1b1f27", highlightthickness=0)
        canvas.pack()
        hist = [0.0] * N
        visible = False
        while True:
            with state_lock:
                cur = state
            if cur in (State.MANUAL, State.PROCESSING):
                if not visible:
                    try:
                        x, y = pyautogui.position()
                    except Exception:
                        x, y = 300, 300
                    root.geometry(f"{W}x{H}+{x + 18}+{y + 26}")
                    visible = True
                hist.pop(0)
                if cur == State.MANUAL:
                    hist.append(min(1.0, (max(_mic_rms, 0.0) * 8.0) ** 0.7))
                    color = "#4aa3ff"                       # blue: recording
                else:
                    hist.append(0.55 + 0.4 * np.sin(time.time() * 6.0))
                    color = "#ff9f40"                       # orange: transcribing
                canvas.delete("all")
                bw, gap = 8, 6
                x0 = (W - (N * bw + (N - 1) * gap)) // 2
                for i, v in enumerate(hist):
                    h = max(3, int(v * (H - 10)))
                    cx = x0 + i * (bw + gap)
                    canvas.create_rectangle(cx, H // 2 - h // 2, cx + bw,
                                            H // 2 + h // 2, fill=color, width=0)
                root.update()
                time.sleep(1 / 30)
            else:
                if visible:
                    root.geometry(OFFSCREEN)   # park, never unmap (no focus events)
                    visible = False
                root.update()
                time.sleep(0.12)
    except Exception:
        logging.exception("indicator failed — disabled for this session")

_indicator_started = False

def start_indicator():
    global _indicator_started
    if _indicator_started or not INDICATOR:
        return
    _indicator_started = True
    threading.Thread(target=_indicator_loop, daemon=True, name="indicator").start()

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
                    if cleaned:
                        cleaned = llm_cleanup(cleaned)
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
                    if cleaned:
                        cleaned = llm_cleanup(cleaned)
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
_DUCK_LEVELS = (0.0, 0.05, 0.1, 0.25, 0.5)
_BEEP_VOLUMES = (0.0, 0.05, 0.1, 0.15, 0.25)  # 0.0 = silent

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

def _on_set_beep_volume(vol):
    def cb(icon, item):
        global BEEP_VOLUME
        BEEP_VOLUME = vol
        save_settings()
        beep_async(PRESS_CHIRP)  # preview the new level
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
    """Enter binding mode for the given target key (or mouse button)."""
    def cb(icon, item):
        global _binding_mode
        with _binding_lock:
            _binding_mode = target
        what = "mouse button" if target.endswith("mouse_button") else "key"
        if _tray_icon:
            _tray_icon.title = f"Press any {what} for {target.replace('_', ' ')}... (Esc to cancel)"
        logging.info(f"Binding mode: {target}")
    return cb

def _on_toggle_ollama(icon, item):
    global OLLAMA_CLEANUP
    OLLAMA_CLEANUP = not OLLAMA_CLEANUP
    save_settings()
    update_tray()

def _on_toggle_manual_over(icon, item):
    global MANUAL_OVER
    MANUAL_OVER = not MANUAL_OVER
    save_settings()
    update_tray()

def _on_toggle_indicator(icon, item):
    global INDICATOR
    INDICATOR = not INDICATOR
    if INDICATOR:
        start_indicator()   # no-op if the thread already runs
    save_settings()
    update_tray()

def _on_teach(icon, item):
    threading.Thread(target=teach_from_selection, daemon=True).start()

def _on_undo_teach(icon, item):
    threading.Thread(target=undo_last_teach, daemon=True).start()

def _on_reload_dict(icon, item):
    load_dictionary()
    beep_async([(659, 70), (784, 70)])
    update_tray()

def _on_open_dict(icon, item):
    try:
        os.startfile(DICT_FILE)
    except Exception:
        logging.exception("open dictionary.json failed")

def _on_edit_dict_gui(icon, item):
    """Launch the dictionary editor GUI as its own process. Saves hot-apply on
    the next dictation via the mtime reload — no restart needed."""
    try:
        import subprocess
        editor = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "dictionary_editor.py")
        subprocess.Popen([sys.executable, editor], close_fds=True)
    except Exception:
        logging.exception("launch dictionary editor failed")

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
    beep_vol_items = [pystray.MenuItem(f"{int(v*100)}%" if v else "Off", _on_set_beep_volume(v),
                      checked=lambda item, v=v: BEEP_VOLUME == v, radio=True)
                      for v in _BEEP_VOLUMES]
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
        pystray.MenuItem(lambda item: f"Teach key: {_key_label(TEACH_KEY)}",
                         _on_bind("teach_key")),
        pystray.MenuItem(lambda item: f"Capture key: {_key_label(CAPTURE_KEY)}",
                         _on_bind("capture_key")),
        pystray.MenuItem(lambda item: f"Re-paste key: {_key_label(REPASTE_KEY)}",
                         _on_bind("repaste_key")),
        pystray.MenuItem(lambda item: f"Structured capture key: {_key_label(CAPTURE_STRUCT_KEY)}",
                         _on_bind("capture_struct_key")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: f"PTT mouse button: {_button_label(PTT_MOUSE_BUTTON)}",
                         _on_bind("ptt_mouse_button")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("(click an entry, then press the new key / mouse button — Esc cancels)",
                         None, enabled=False),
    )
    dict_items = pystray.Menu(
        pystray.MenuItem(lambda item: f"Teach from selection ({_key_label(TEACH_KEY)})",
                         _on_teach),
        pystray.MenuItem(
            lambda item: ("Undo last teach"
                          + ((" (" + ", ".join(f"{w}→{r}" for w, r in
                              (_dictionary.get("_teach_history") or [{}])[-1].get("pairs", [])[:2]) + ")")
                             if (_dictionary.get("_teach_history") or []) else " (none)")),
            _on_undo_teach),
        pystray.MenuItem("Edit dictionary (GUI)",  _on_edit_dict_gui),
        pystray.MenuItem("Reload dictionary.json", _on_reload_dict),
        pystray.MenuItem("Open dictionary.json",   _on_open_dict),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: (f"{len(_dictionary['vocab'])} vocab / "
                                       f"{len(_dictionary['corrections'])} corrections"),
                         None, enabled=False),
    )
    return pystray.Menu(
        pystray.MenuItem(lambda item: f"PTT: {state.name}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("VAD enabled", _on_toggle_vad, checked=lambda item: vad_enabled),
        pystray.MenuItem("Hot mic",     _on_toggle_hot_mic, checked=lambda item: hot_mic),
        pystray.MenuItem(lambda item: f"Ollama cleanup ({OLLAMA_MODEL})",
                         _on_toggle_ollama, checked=lambda item: OLLAMA_CLEANUP),
        pystray.MenuItem("Recording indicator", _on_toggle_indicator,
                         checked=lambda item: INDICATOR),
        pystray.MenuItem("Trailing 'over' presses Enter (manual PTT)",
                         _on_toggle_manual_over, checked=lambda item: MANUAL_OVER),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Dictionary",   dict_items),
        pystray.MenuItem("Duck level",   pystray.Menu(*duck_items)),
        pystray.MenuItem("Beep backend", pystray.Menu(*beep_items)),
        pystray.MenuItem("Beep volume",  pystray.Menu(*beep_vol_items)),
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
    global PTT_KEY, HOT_MIC_KEY, VAD_KEY, TEACH_KEY, CAPTURE_KEY, REPASTE_KEY, \
           CAPTURE_STRUCT_KEY, _binding_mode
    if key == keyboard.Key.esc:          # Escape cancels without changing anything
        logging.info("Bind cancelled (Escape)")
    else:
        if mode == "ptt_key":
            PTT_KEY = key
        elif mode == "hot_mic_key":
            HOT_MIC_KEY = key
        elif mode == "vad_key":
            VAD_KEY = key
        elif mode == "teach_key":
            TEACH_KEY = key
        elif mode == "capture_key":
            CAPTURE_KEY = key
        elif mode == "repaste_key":
            REPASTE_KEY = key
        elif mode == "capture_struct_key":
            CAPTURE_STRUCT_KEY = key
        logging.info(f"Bound {mode} → {_key_label(key)}")
        save_settings()
    with _binding_lock:
        _binding_mode = None
    update_tray()

def _finish_bind_mouse(button: mouse.Button) -> None:
    """Assign the captured mouse button to the PTT mouse binding and persist."""
    global PTT_MOUSE_BUTTON, _binding_mode
    PTT_MOUSE_BUTTON = button
    logging.info(f"Bound ptt_mouse_button → {_button_label(button)}")
    save_settings()
    with _binding_lock:
        _binding_mode = None
    update_tray()

def _cancel_bind() -> None:
    """Leave binding mode without changing anything."""
    global _binding_mode
    logging.info("Bind cancelled (Escape)")
    with _binding_lock:
        _binding_mode = None
    update_tray()

# ── Keyboard handlers ──────────────────────────────────────────────
_ctrl_down = False   # tracked so Ctrl+<PTT mouse button> chords (e.g. an external
                     # Ctrl+MB5 macro) don't also start a phantom recording

def on_press(key):
    global state, manual_chunks, _ctrl_down
    try:
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl):
            _ctrl_down = True

        # Binding mode intercept — capture next key press as new binding
        with _binding_lock:
            mode = _binding_mode
        if mode is not None:
            if mode == "ptt_mouse_button":
                # Waiting for a mouse button; only Esc (a key) cancels here.
                if key == keyboard.Key.esc:
                    _cancel_bind()
                return    # swallow all other keys until a mouse button arrives
            _finish_bind(mode, key)
            return    # don't let the key also trigger its normal action

        if key in (PTT_KEY, CAPTURE_KEY, CAPTURE_STRUCT_KEY):
            global _capture_session, _capture_struct
            is_capture = key in (CAPTURE_KEY, CAPTURE_STRUCT_KEY)
            if is_capture and not (CAPTURE_URI or CAPTURE_FILE or CAPTURE_TEXT_URI):
                logging.warning("capture key pressed but no capture target configured — ignoring")
                return
            with state_lock:
                if state == State.MANUAL:
                    return  # already recording
                # Interrupt any VAD state
                prev = state
                state = State.MANUAL
            update_tray()
            manual_chunks = []
            _capture_session = is_capture
            _capture_struct = (key == CAPTURE_STRUCT_KEY)
            if prev in (State.BUFFERING, State.CHECKING):
                restore_audio()
                logging.info("PTT key interrupted VAD recording")
            duck_audio()
            if is_capture and CAPTURE_URI:
                # Optional press-open visual; file delivery needs no window at all
                threading.Thread(target=_open_capture_target, daemon=True).start()
            # Cute press chirp (ascending blip)
            beep_async(PRESS_CHIRP)
            logging.info("Capture key: recording" if is_capture else "PTT key: recording")

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

        elif key == TEACH_KEY:
            # Clipboard + key-send work off-thread so the listener never blocks
            threading.Thread(target=teach_from_selection, daemon=True).start()

        elif key == REPASTE_KEY:
            threading.Thread(target=repaste_last, daemon=True).start()

    except Exception as e:
        logging.exception("Error in on_press")

def on_release(key):
    global state, manual_chunks, _ctrl_down
    try:
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl):
            _ctrl_down = False

        # Ignore release events during binding mode
        with _binding_lock:
            if _binding_mode is not None:
                return

        if key in (PTT_KEY, CAPTURE_KEY, CAPTURE_STRUCT_KEY):
            with state_lock:
                if state != State.MANUAL:
                    return
                state = State.PROCESSING
            update_tray()

            # Release chirp plays while still ducked; un-duck shortly after (see
            # _delayed_restore) so the beep isn't masked by apps returning to full volume.
            beep_async(RELEASE_CHIRP, then=_delayed_restore)
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
                    if not _capture_session and try_voice_command(text):
                        pass   # executed (or consumed) a voice command — no paste
                    else:
                        cleaned, press_enter = process_commands(
                            text, radio="over" if MANUAL_OVER else False)
                        if cleaned:
                            cleaned = llm_cleanup(cleaned)
                        deliver_text(cleaned, press_enter, raw=text)
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
    """Mouse button handler - the configured PTT mouse button acts as PTT."""
    global state, manual_chunks
    try:
        # Binding mode: capture a mouse button as the new PTT button.
        with _binding_lock:
            mode = _binding_mode
        if mode is not None:
            if (mode == "ptt_mouse_button" and pressed
                    and button not in (mouse.Button.left, mouse.Button.right)):
                _finish_bind_mouse(button)
            return    # swallow all clicks while a bind is pending

        if button == PTT_MOUSE_BUTTON:
            if pressed:
                if _ctrl_down:
                    # Ctrl+<PTT button> belongs to an external chord macro
                    # (e.g. Ctrl+MB5 quick-note) — don't start recording.
                    logging.info("PTT mouse button ignored (Ctrl held — chord)")
                    return
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
                    logging.info("Thumb button interrupted VAD recording")
                duck_audio()
                # Cute press chirp (ascending blip)
                beep_async(PRESS_CHIRP)
                logging.info("Thumb button: recording")
            else:
                # Middle button released - transcribe
                with state_lock:
                    if state != State.MANUAL:
                        return
                    state = State.PROCESSING
                update_tray()

                # Release chirp plays while still ducked; un-duck shortly after (see
                # _delayed_restore) so the beep isn't masked by apps returning to full volume.
                beep_async(RELEASE_CHIRP, then=_delayed_restore)
                logging.info("Thumb button: released, transcribing...")

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
                        logging.info(f"Thumb button raw: {text}")
                        if not _capture_session and try_voice_command(text):
                            pass   # executed (or consumed) a voice command — no paste
                        else:
                            cleaned, press_enter = process_commands(
                                text, radio="over" if MANUAL_OVER else False)
                            if cleaned:
                                cleaned = llm_cleanup(cleaned)
                            deliver_text(cleaned, press_enter, raw=text)
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
    start_indicator()             # once; parks off-screen until recording

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
