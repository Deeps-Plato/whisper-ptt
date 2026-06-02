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
DUCK_LEVEL = 0.1              # Audio ducking: 0.0 = mute, 1.0 = no change
BEEP_BACKEND = "winsound"     # "winsound" (default) or "sounddevice"
```

### Hotkeys

Default bindings:

| Key | Action |
|-----|--------|
| Right Ctrl / front thumb button (x2) | Hold to record, release to transcribe (PTT) |
| F10 | Toggle hot mic (continuous voice-activated dictation) |
| F8 | Toggle VAD on/off |

Rebind any keyboard hotkey interactively via the system tray: right-click the tray icon → **Hotkeys** → click the binding you want to change → press the new key (Esc to cancel). Bindings persist to `ptt-settings.json` and reload on next start. The front thumb button (x2) PTT is not rebindable via the tray menu.

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
- **Duck level** — 0%, 10%, 25%, 50% (radio buttons)
- **Beep backend** — winsound or sounddevice
- **Microphone** — switch input device; audio stream restarts automatically
- **Hotkeys** — rebind PTT, hot mic, and VAD keys interactively
- **Restart PTT** — restart the audio stream (e.g. after device change)
- **Quit** — exit the process

All settings persist to `ptt-settings.json` immediately on change and reload on next launch.

## Adding Words to Prompt

To improve transcription of proper nouns, technical terms, or commonly misheard words, add them to `INITIAL_PROMPT` in `ptt.py`:

```python
INITIAL_PROMPT = "Conversation with Rei. Ollama, model, WSL, NewWord, AnotherTerm."
```

Restart the script after changes.

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

## Audio Device

Uses Volt 2 audio interface. Change `DEVICE_NAME` in ptt.py if using different hardware.
