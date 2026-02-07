# Changelog

## 2026-02-07

- Audio ducking via pycaw: other apps dim to 20% while recording, restore on release
  - Configurable via `DUCK_LEVEL` (0.0=mute, 1.0=off)
- Clipboard preservation: saves/restores clipboard around paste so PTT doesn't clobber it
- Registered `WhisperPTT` as Windows Task Scheduler task
  - Starts at logon under `admin` user session (interactive, not session 0)
  - Auto-restarts up to 3x on crash (1min interval)
  - No execution time limit, runs on battery
- Updated README with service management commands
