# Whisper PTT (Push-to-Talk Voice-to-Text)

Push-to-talk transcription using faster-whisper with CUDA acceleration.

## Usage

Hold **F9** to record, release to transcribe. Text is auto-pasted at cursor.

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
- `at` → `@`
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
SAMPLE_RATE = 16000
DEVICE_NAME = "Volt 2"        # Audio input device
MODEL_SIZE = "base"           # tiny, base, small, medium, large-v3
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL."
DUCK_LEVEL = 0.1              # Audio ducking: 0.0 = mute, 1.0 = no change
```

## Adding Words to Prompt

To improve transcription of proper nouns, technical terms, or commonly misheard words, add them to `INITIAL_PROMPT` in `ptt.py` line 28:

```python
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL, NewWord, AnotherTerm."
```

Restart the script after changes.

## Requirements

- Python 3.10+
- CUDA-capable GPU
- faster-whisper (`pip install faster-whisper`)
- pynput (`pip install pynput`)
- pyperclip (`pip install pyperclip`)
- pyautogui (`pip install pyautogui`)
- sounddevice (`pip install sounddevice`)
- numpy
- pycaw (`pip install pycaw`)

## Running

```bash
# With console (for debugging)
python ptt.py

# Headless (no console window)
pythonw ptt.py
```

Logs written to `%TEMP%\whisper-ptt.log`

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

### Agent Restart Procedure

After editing `ptt.py`, restart to apply changes:

```bash
cmd.exe /c "taskkill /F /IM pythonw.exe && schtasks /Run /TN WhisperPTT"
```

If `schtasks /Run` fails with "file not found" (`0x80070002`), the task needs the full pythonw path:
```bash
cmd.exe /c "taskkill /F /IM pythonw.exe"
cmd.exe /c "schtasks /Run /TN WhisperPTT"
# If that fails, start directly:
cmd.exe /c "start /B pythonw.exe C:\Users\admin\Documents\Claude\whisper-ptt\ptt.py"
```

Verify it's running: `cmd.exe /c "tasklist | findstr pythonw"`

**Why not a real Windows service?** Services run in session 0 with no desktop access — can't hook keyboard or paste to clipboard.

## Audio Device

Uses Volt 2 audio interface. Change `DEVICE_NAME` in ptt.py if using different hardware.
