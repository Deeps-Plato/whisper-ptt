# Changelog

## 2026-06-03

- PTT mouse button is now rebindable from the tray menu (**Hotkeys → PTT mouse button**),
  alongside the existing key rebinds. Click the entry, then press the new mouse button
  (middle / X1 / X2 — left and right click are reserved and ignored while binding; Esc cancels).
  Persisted as `ptt_mouse_button` in `ptt-settings.json`; the mouse listener picks it up live,
  no restart needed. Default remains the front thumb button (x2).

## 2026-06-02

- **Desktop shortcut installer**: `install-desktop-icon.bat` (double-click) / `install-desktop-icon.ps1`
  create a "Whisper PTT" shortcut on the Desktop that launches `pythonw ptt.py` headless with the
  repo as the working directory
- Added `make_icon.py` to generate `whisper-ptt.ico` — a white microphone on the green brand
  circle, matching the tray icon's active-state colour
- Manual PTT mouse binding changed from the middle mouse button to the front thumb button (x2)

## 2026-02-22

- Fixed volume stuck at ducked level after speaking (three bugs in audio ducking):
  - **Thread race condition**: `duck_audio`/`restore_audio` are called from the VAD, keyboard,
    and mouse listener threads concurrently. Added `duck_lock` (`threading.Lock`) to serialise
    all duck/restore operations and prevent `saved_volumes` from being corrupted mid-call
  - **Double-duck corruption**: `duck_audio` always reset `saved_volumes = {}` first, so a
    second call while already ducked would save the already-lowered volume as the "original".
    Added `_is_ducked` flag — `duck_audio` is now a no-op when already ducked; `restore_audio`
    clears the flag in a `finally` block so cleanup always runs even on COM errors
  - **Multiple sessions per PID**: `saved_volumes` used process PID as the key, so apps with
    multiple audio sessions (e.g. Chrome, Discord) had all but the last session restored to the
    wrong volume. Changed key to `(pid, session_index)` to track each session independently
- Removed local `ducked` tracking variable from `vad_monitor` — now handled globally by `_is_ducked`
- RCtrl keybinding (was F9), Yeti Classic mic, terminal-aware paste with direct key typing

## 2026-02-07

- Audio ducking via pycaw: other apps dim to 20% while recording, restore on release
  - Configurable via `DUCK_LEVEL` (0.0=mute, 1.0=off)
- Clipboard preservation: saves/restores clipboard around paste so PTT doesn't clobber it
- Registered `WhisperPTT` as Windows Task Scheduler task
  - Starts at logon under `admin` user session (interactive, not session 0)
  - Auto-restarts up to 3x on crash (1min interval)
  - No execution time limit, runs on battery
- Updated README with service management commands
