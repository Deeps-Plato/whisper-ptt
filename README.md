# Whisper PTT (Push-to-Talk Voice-to-Text)

Push-to-talk transcription using faster-whisper with CUDA acceleration.

## Setup

Requires Windows, Python 3.10+, NVIDIA GPU with CUDA.

```bash
# 1. Clone
git clone https://github.com/mithril-logic/whisper-ptt.git
cd whisper-ptt

# 2. Install PyTorch with CUDA (pick your CUDA version: https://pytorch.org/get-started/locally/)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# 3. Install dependencies
pip install faster-whisper silero-vad pynput pyperclip pyautogui sounddevice numpy pycaw comtypes pystray pillow

# 4. Find your microphone name
python -c "import sounddevice; print(sounddevice.query_devices())"

# 5. Edit ptt.py — set DEVICE_NAME to your mic (line 47)
# Also customize INITIAL_PROMPT with your own vocab (line 49)

# 6. Run
python ptt.py
```

First run downloads the Whisper model (~150MB for base). Logs go to `%TEMP%\whisper-ptt.log`.

## Usage

Hold **F9** or **middle mouse button** to record, release to transcribe. Text is auto-pasted at cursor.

### Audio Cue Beeps

Beeps are built in and enabled by default.

- **F9 / middle mouse press**: ascending chirp (G5→C6)
- **F9 / middle mouse release**: descending chirp (C6→E5)
- **F10 hot mic on**: ascending 3-tone (C5→D5→E5)
- **F10 hot mic off**: descending 3-tone (E5→D5→C5)
- **F8 VAD toggle**: high beep = on, low beep = off

If a machine has no audible beeps with `winsound`, switch to `sounddevice`:

```python
BEEP_BACKEND = "sounddevice"
```

### Radio Commands

Commands work in voice-activated modes (hot mic and wake phrase). Manual PTT (F9/middle mouse) transcribes literally.

| Command | Effect | Example |
|---------|--------|---------|
| `break` | Newline | "Line one break line two" → "Line one\nLine two" |
| `over` | Submit (press Enter) | "Send message over" → types text + presses Enter |
| `correction` | Delete previous word | "Hello world correction there" → "Hello there" |
| `disregard` | Cancel utterance | "Never mind disregard" → nothing pasted |

### Spoken Punctuation

Say punctuation names to insert symbols:

**Single-word:**
- `slash` → `/`
- `hyphen` → `-`
- `comma` → `,`
- `period` → `.`
- `colon` → `:`
- `semicolon` → `;`
- `caret` → `^`
- `ampersand` → `&`
- `asterisk` → `*`
- `plus` → `+`
- `equals` → `=`
- `pipe` → `|`
- `backslash` → `\`
- `tilde` → `~`
- `backtick` → `` ` ``
- `underscore` → `_`

**Two-word phrases:**
- `question mark` → `?`
- `exclamation point` / `exclamation mark` → `!`
- `home slash` → `~/`
- `open bracket` / `close bracket` → `[` / `]`
- `open paren` / `close paren` → `(` / `)`
- `open brace` / `close brace` → `{` / `}`
- `hash tag` → `#`
- `at symbol` / `at sign` → `@`
- `dollar sign` → `$`
- `percent sign` → `%`

**Example:** "cd home slash Documents slash openclaw hyphen surgery" → `cd ~/Documents/openclaw-surgery`

## Files

| File | Purpose |
|------|---------|
| `ptt.py` | Main script - faster-whisper with pynput hotkey listener |
| `ptt-settings.json` | Persisted settings (duck level, beep backend, mic, VAD, hotkeys) — auto-created |
| `dictionary.json` | Managed vocab + corrections (auto-created, machine-local) |
| `dictionary.example.json` | Schema example for the managed dictionary |
| `tests/test_dictionary.py` | Tests for the dictionary/teach text pipeline (`python tests/test_dictionary.py`) |
| `start-ptt.bat` | Legacy launcher (scheduled task now calls pythonw directly) |
| `install-desktop-icon.bat` | Double-click to create a "Whisper PTT" desktop shortcut |
| `install-desktop-icon.ps1` | PowerShell installer that builds the desktop shortcut |
| `make_icon.py` | Generates `whisper-ptt.ico` (mic on the green brand circle) |
| `record.bat` | FFmpeg recording (used by AHK fallback) |
| `transcribe.bat` | whisper-cli transcription (used by AHK fallback) |
| `whisper-ptt.ahk` | AutoHotkey fallback script |

## Configuration

Edit `ptt.py` to change settings:

```python
DEVICE_NAME = "Volt 2"        # Audio input device — change to your mic
MODEL_SIZE = "base"           # tiny, base, small, medium, large-v3
INITIAL_PROMPT = "..."        # Custom vocab for better transcription accuracy
DUCK_LEVEL = 0.05            # Audio ducking: 0.0 = mute, 1.0 = no change
BEEP_BACKEND = "sounddevice"  # "sounddevice" (volume-adjustable, low latency) or "winsound" (loud, fixed)
BEEP_VOLUME = 0.10           # 0.0–1.0 amplitude for sounddevice beeps (winsound ignores this)
```

Beeps use the `sounddevice` backend by default: it plays through a persistent low-latency
audio stream with a short fade envelope (soft, no click) and a volume you can set. The
`winsound` backend is the Windows system beep — loud, fixed volume, and a touch laggy.

### Hotkeys

Default bindings:

| Key | Action |
|-----|--------|
| Right Ctrl / front thumb button (x2) | Hold to record, release to transcribe (PTT) |
| F10 | Toggle hot mic (continuous voice-activated dictation) |
| F8 | Toggle VAD on/off |
| F7 | Teach: learn corrections from the selected fixed text |
| F15 | Note capture: hold/talk/release → silent append to your note file |
| F16 | Re-paste the last transcript at the current focus |
| F17 | Structured capture: like F15, but the ramble is restructured into bullets/checkboxes first |

All keys are rebindable from the tray. A small **speech-wave bubble** appears
near the cursor while the mic is hot (blue bars = recording level, orange
pulse = transcribing); toggle it via tray → **Recording indicator**. The tray
**Dictionary** menu also has **Undo last teach** to revert the most recent
learned correction. Every dictation is logged to `dictation-history.jsonl`
(machine-local).

### Auto-start / self-heal

Put a shortcut to `watchdog.ps1` in `shell:startup`
(`powershell -WindowStyle Hidden -File watchdog.ps1`): it launches `ptt.py`
at logon and relaunches it within 5 minutes if it ever dies.

Rebind any hotkey — including the **PTT mouse button** — interactively via the system tray: right-click the tray icon → **Hotkeys** → click the binding you want to change → press the new key (or click the new mouse button; Esc to cancel). Side buttons (X1/X2) and the middle button work as the PTT mouse button; left/right click are reserved and ignored while binding. Bindings persist to `ptt-settings.json` and reload on next start.

## System Tray

A tray icon appears in the Windows notification area showing PTT state at a glance:

| Icon colour | State |
|-------------|-------|
| Grey circle | Idle |
| Green circle | Recording (manual PTT or VAD buffering) |
| Yellow circle | Transcribing |
| Blue dot overlay | Hot mic active |

Right-click the tray icon to access settings without editing `ptt.py`:

- **VAD enabled** / **Hot mic** — toggle with a checkmark
- **Ollama cleanup** — toggle the optional local-LLM polish pass
- **Recording indicator** / **Trailing 'over' presses Enter** — toggles
- **Dictionary** — teach from selection, undo last teach, GUI editor, reload/open, live counts
- **Duck level** — 0%, 5%, 10%, 25%, 50% (radio buttons)
- **Beep backend** — winsound or sounddevice
- **Beep volume** — Off, 5%, 10%, 15%, 25% (sounddevice only; plays a preview on change)
- **Microphone** — switch input device; audio stream restarts automatically
- **Hotkeys** — rebind the PTT key, hot mic key, VAD key, and PTT mouse button interactively
- **Restart PTT** — restart the audio stream (e.g. after device change)
- **Quit** — exit the process

All settings persist to `ptt-settings.json` immediately on change and reload on next launch.

## Managed Dictionary

Vocabulary and corrections live in `dictionary.json` next to `ptt.py` (created
automatically on first run from the in-code defaults, machine-local, gitignored).
See `dictionary.example.json` for the schema:

```json
{
  "prompt_prefix": "Work dictation about software and logistics.",
  "vocab": ["Ollama", "InXpress", "NMFC"],
  "corrections": { "oh llama": "Ollama", "in express": "InXpress" }
}
```

- **`vocab`** — terms fed to Whisper as prompt context so it recognizes them.
  Ordered most-important-first: if the list exceeds Whisper's ~224-token prompt
  window, the tail is trimmed, never the head.
- **`corrections`** — deterministic post-transcription replacements
  (case-insensitive, whole-word, multi-word keys supported, longest key wins).
  Use for words Whisper keeps mishearing the same way.
- **`prompt_prefix`** — free-text context placed before the vocab list.

The file **hot-reloads**: any change on disk is picked up before the next
transcription — no restart, no tray click. A tkinter **GUI editor** ships too:
tray → Dictionary → **Edit dictionary (GUI)** (`dictionary_editor.py`) with a
scope picker for Global or per-app entries, vocab list and misheard→correct
pairs with add/delete.

**Per-app dictionaries:** an `app_profiles` entry may be an object instead of
a style string — `{"style": "...", "vocab": [...], "corrections": {...}}`.
Per-app vocab extends the Whisper prompt and per-app corrections merge over
the global map (app wins) only when dictating into that app, so gaming jargon
never pollutes a work email and vice versa. The tray also shows live
vocab/correction counts, and **Undo last teach** reverts the most recent
learned correction.

### Teach Mode (learn from your corrections)

When a dictation comes out wrong, fix it **in place** wherever it was pasted,
select the corrected text, and press the **teach key** (default **F7**, rebindable).
The script diffs your selection against what it last injected, extracts the
changed word pairs, and saves them to the dictionary automatically:

1. Dictate → it pastes `meet jansen at tea force freight`
2. Fix the text → `meet Janszen at TForce Freight`, select the sentence, press **F7**
3. Learned: `jansen → Janszen`, `tea force → TForce` — corrections apply to every
   future dictation, and new proper nouns are added to `vocab` so Whisper gets
   them right at transcription time too.

A rising arpeggio confirms a successful learn; a single low beep means nothing
learnable was found. Only small word-level *replacements* are learned —
insertions/deletions are treated as content edits, and plain sentence
capitalization is ignored so common words are never over-learned.

### Voice Commands

Dictations can *do things* instead of pasting. Two pieces, both opt-in:

- **Trailing "over" presses Enter** (`manual_over` setting / tray toggle):
  end a manual-PTT dictation with the word "over" and Enter fires after the
  paste — hands-free message sending. Only the final word counts; the other
  radio commands stay VAD-only so ordinary speech can't delete or cancel.
- **`voice_commands`** in `dictionary.json` maps spoken phrases to actions:

  ```json
  "voice_commands": {
    "screenshot": "keys:win+shift+s",
    "browse to reddit": "run:start chrome https://old.reddit.com/top/",
    "search google *": "run:start chrome https://www.google.com/search?q={query}"
  }
  ```

  - `keys:` sends a hotkey combo, `run:` launches a program/shell line,
    `open:` opens a URI or file.
  - Single-word keys fire as "*command* screenshot" (the `COMMAND_PREFIX`).
  - Multi-word keys define their **own trigger word** ("browse to reddit"
    fires on that phrase directly) — new command families are pure config.
  - Keys ending in ` *` **capture the spoken remainder**: `{query}` is
    URL-encoded (%20), `{raw}` is the text as spoken. "search google cheapest
    liftgate carriers" opens the executed search.
  - Safety: deterministic lookup only (no LLM decides what runs), unmatched
    attempts beep low and paste nothing, wildcard families only consume
    speech when the first two words match a key, and capture-key sessions
    never trigger commands.

### Ollama Cleanup (optional LLM polish)

If you run [Ollama](https://ollama.com) locally, toggle tray → **Ollama cleanup**
to pipe each transcript through a local LLM that fixes transcription errors,
casing, and punctuation (strictly no rephrasing — output failing a length sanity
check is discarded). Configure via `OLLAMA_MODEL` / `OLLAMA_URL` in `ptt.py`
(default `qwen2.5:14b` on `localhost:11434`; persisted to `ptt-settings.json`).
Adds ~0.5–2 s per dictation depending on model and GPU; any error or timeout
falls back to the raw transcript, so dictation never hangs. Off by default.

## Running

```bash
python ptt.py          # with console (for debugging)
pythonw ptt.py         # headless (no console window)
```

### Desktop Shortcut

Double-click `install-desktop-icon.bat` to drop a **Whisper PTT** icon on your Desktop.
It launches `pythonw ptt.py` headless (no console window) with the repo as the working
directory, using a generated microphone icon. The installer regenerates the icon each run,
so it works straight after a clone.

```powershell
# Or run the installer directly:
powershell -ExecutionPolicy Bypass -File install-desktop-icon.ps1
```

## Service (Auto-Start)

PTT runs as a Windows Task Scheduler task (`WhisperPTT`) that starts `pythonw.exe ptt.py` directly at logon. No bat wrapper — avoids a visible cmd window on boot. Runs in user session (not session 0) so it has keyboard/clipboard access. Auto-restarts up to 3x on crash with 1min interval.

```bash
# Check status
schtasks /Query /TN WhisperPTT

# Manual start/stop
schtasks /Run /TN WhisperPTT
schtasks /End /TN WhisperPTT

# Kill the process directly
taskkill /F /IM pythonw.exe

# Restart (kill + start) — for applying code changes
taskkill /F /IM pythonw.exe && schtasks /Run /TN WhisperPTT

# Remove the scheduled task entirely
schtasks /Delete /TN WhisperPTT /F
```

### Agent Restart Procedure (from WSL)

**When user reports PTT is down/offline/not working, immediately run the restart command below — no confirmation needed.**

Kill + restart. The kill may report "access denied" or "not found" but still succeeds — ignore the exit code:

```bash
PTT_WIN=$(wslpath -w "$CLAUDE_PROJECTS/whisper-ptt/ptt.py")
cmd.exe /c "taskkill /F /IM pythonw.exe" 2>&1; cmd.exe /c "pythonw.exe $PTT_WIN"
```

Verify it's running:
```bash
cmd.exe /c "tasklist | findstr pythonw"
```

**Notes:**
- Do NOT chain kill + start with `&&` — kill returns nonzero even on success from WSL
- Uses `$CLAUDE_PROJECTS` env var, not hardcoded path

**Why not a real Windows service?** Services run in session 0 with no desktop access — can't hook keyboard or paste to clipboard.

## Transcribing Audio Files (meetings, memos)

Record however you like (Audacity, phone, OBS), then:

```bash
python transcribe_file.py recording.wav                # timestamped transcript -> recording.md
python transcribe_file.py call.mp3 --structured        # meeting notes: summary, bullets, action items
```

Standalone script using the same model, dictionary corrections, and vocab
prompt as live dictation; `--structured` runs the transcript through the
local Ollama model. Handles wav/mp3/m4a/flac and most formats. Reminder:
recording other people requires their consent in all-party-consent states.

## Audio Device

`device_name` in `ptt-settings.json` may be a single name or a
**priority-ordered list** of name substrings — the first device present wins:

```json
"device_name": ["DJI MIC MINI", "Desk Mic"]
```

So a lapel mic is preferred whenever its USB receiver is plugged in, with
automatic fallback otherwise. After plugging/unplugging, tray → **Restart
PTT** re-runs the pick. The tray Microphone menu still sets a single device.
