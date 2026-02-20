# whisper-ptt

Push-to-talk voice transcription with CUDA-accelerated Whisper.

## Stack
- Python 3.10+, Windows only, NVIDIA GPU required
- faster-whisper, silero-vad, pynput, sounddevice, pyautogui

## Key Files
| File | Purpose |
|------|---------|
| `ptt.py` | Main script — transcription engine, hotkeys, commands |
| `CUSTOM_FUNCTIONALITY.md` | Extension guide |

## Run
```bash
python ptt.py       # with console
pythonw ptt.py      # headless
```

## Service
Scheduled task `WhisperPTT` runs `pythonw.exe ptt.py` directly at logon (no bat wrapper). Auto-restarts up to 3x on crash, 1min interval. Runs in user session for keyboard/clipboard access.

## Config (top of ptt.py)
- `DEVICE_NAME` — mic name (default: "Volt 2")
- `MODEL_SIZE` — whisper model (default: "base")
- `INITIAL_PROMPT` — custom vocab
- `DUCK_LEVEL` — audio ducking during transcription

## Hotkeys
- F9 / middle mouse: hold to record
- F10: toggle hot mic (continuous VAD)
- F8: toggle VAD on/off

## Restart (from WSL)
When user reports PTT is down/offline/not working, run immediately — no confirmation needed.
```bash
PTT_WIN=$(wslpath -w "$CLAUDE_PROJECTS/whisper-ptt/ptt.py")
cmd.exe /c "taskkill /F /IM pythonw.exe" 2>&1; cmd.exe /c "pythonw.exe $PTT_WIN"
```
- Do NOT use `&&` — kill returns nonzero even on success from WSL
- "Access denied" / "not found" errors are normal — ignore them
- Verify: `cmd.exe /c "tasklist | findstr pythonw"`

## Constraints
- Windows-only (pycaw, pyautogui, scheduled task)
- Requires CUDA toolkit + PyTorch CUDA build
- Log: `%TEMP%\whisper-ptt.log`
