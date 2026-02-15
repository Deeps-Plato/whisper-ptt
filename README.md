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
pip install faster-whisper silero-vad pynput pyperclip pyautogui sounddevice numpy pycaw comtypes

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

### Radio Commands

Commands work in both manual PTT (F9) and voice-activated modes:

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
| `record.bat` | FFmpeg recording (used by AHK fallback) |
| `transcribe.bat` | whisper-cli transcription (used by AHK fallback) |
| `whisper-ptt.ahk` | AutoHotkey fallback script |

## Configuration

Edit `ptt.py` to change settings:

```python
DEVICE_NAME = "Volt 2"        # Audio input device — change to your mic
MODEL_SIZE = "base"           # tiny, base, small, medium, large-v3
INITIAL_PROMPT = "..."        # Custom vocab for better transcription accuracy
DUCK_LEVEL = 0.1              # Audio ducking: 0.0 = mute, 1.0 = no change
```

### Hotkeys

Hotkeys are in the `on_press`/`on_release`/`on_click` functions near the bottom of `ptt.py`. Defaults:

| Key | Action |
|-----|--------|
| F9 / middle mouse | Hold to record, release to transcribe (PTT) |
| F10 | Toggle hot mic (continuous voice-activated dictation) |
| F8 | Toggle VAD on/off |

To change, replace `keyboard.Key.f9` etc. with your preferred key from [pynput's Key enum](https://pynput.readthedocs.io/en/latest/keyboard.html#pynput.keyboard.Key).

## Adding Words to Prompt

To improve transcription of proper nouns, technical terms, or commonly misheard words, add them to `INITIAL_PROMPT` in `ptt.py` line 28:

```python
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL, NewWord, AnotherTerm."
```

Restart the script after changes.

## Running

```bash
python ptt.py          # with console (for debugging)
pythonw ptt.py         # headless (no console window)
```

## Service (Auto-Start)

PTT runs as a Windows Task Scheduler task (`WhisperPTT`) that starts at logon. Runs in user session (not session 0) so it has keyboard/clipboard access. Auto-restarts up to 3x on crash with 1min interval.

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

After editing `ptt.py`, restart to apply changes. The kill may report "access denied" or "not found" but still succeed — ignore the exit code and proceed to start:

```bash
cmd.exe /c "taskkill /F /IM pythonw.exe" 2>&1; cmd.exe /c "schtasks /Run /TN WhisperPTT"
```

Verify it's running:
```bash
cmd.exe /c "tasklist | findstr pythonw"
```

**Do NOT chain with `&&`** — the kill command returns nonzero even on success when called from WSL.

**Why not a real Windows service?** Services run in session 0 with no desktop access — can't hook keyboard or paste to clipboard.

## Audio Device

Uses Volt 2 audio interface. Change `DEVICE_NAME` in ptt.py if using different hardware.
