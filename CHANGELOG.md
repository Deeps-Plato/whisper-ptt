# Changelog

## 2026-02-07

- Clipboard preservation: saves/restores clipboard around paste so PTT doesn't clobber it
- Registered `WhisperPTT` as Windows Task Scheduler task
  - Starts at logon under `admin` user session (interactive, not session 0)
  - Auto-restarts up to 3x on crash (1min interval)
  - No execution time limit, runs on battery
- Updated README with service management commands
