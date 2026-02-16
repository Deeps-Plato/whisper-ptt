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
pythonw ptt.py      # headless (deployed as scheduled task "WhisperPTT")
```

## Config (top of ptt.py)
- `DEVICE_NAME` — mic name (default: "Volt 2")
- `MODEL_SIZE` — whisper model (default: "base")
- `INITIAL_PROMPT` — custom vocab
- `DUCK_LEVEL` — audio ducking during transcription

## Hotkeys
- F9 / middle mouse: hold to record
- F10: toggle hot mic (continuous VAD)
- F8: toggle VAD on/off

## Constraints
- Windows-only (pycaw, pyautogui, scheduled task)
- Requires CUDA toolkit + PyTorch CUDA build
- Log: `%TEMP%\whisper-ptt.log`
