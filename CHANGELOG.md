# Changelog

## 2026-06-10

- **Undo last teach** (tray → Dictionary): teach events are recorded to `_teach_history`
  in dictionary.json (last 20) and the newest can be reverted — corrections and any vocab
  it added — so one bad learn can't silently poison future dictations
- **Re-paste key** (F16, rebindable): re-pastes the last transcript at the current focus,
  for when a dialog or focus change ate the original paste. Every manual dictation is now
  logged to `dictation-history.jsonl` (raw, final, mode, window; machine-local)
- **Structured capture key** (F17, rebindable): like the capture key, but the transcript is
  restructured by the local LLM into concise markdown bullets with `- [ ]` action items
  before it lands in the note; verbatim fallback if Ollama is unreachable
- **Recording indicator**: always-on-top speech-wave bubble near the cursor — bars follow
  live mic level while recording, orange pulse while transcribing. Parks off-screen instead
  of unmapping so it can never steal focus from the paste target. Tray-toggleable
- **watchdog.ps1**: auto-start at logon + self-heal (relaunches ptt.py within 5 min if the
  process dies); run from a `shell:startup` shortcut, hidden window
- Per-app style profiles (2026-06-09 late): `app_profiles` in dictionary.json maps window-
  title substrings to style instructions for the cleanup pass; `"skip"` bypasses the LLM
  entirely (terminals). Capture delivery moved to silent direct file-append (`capture_file`)
  after the URI-open flow stole focus mid-dictation

## 2026-06-09

- **Managed dictionary**: vocab + corrections move from in-code constants to
  `dictionary.json` (auto-created on first run from the legacy `INITIAL_PROMPT` /
  `LEXICAL_OVERRIDES`, so behavior is unchanged until edited; machine-local, gitignored;
  `dictionary.example.json` documents the schema). Whisper's prompt is built from
  `prompt_prefix` + `vocab` and trimmed to a ~200-token budget from the tail so the most
  important terms always survive. Corrections are case-insensitive whole-word replacements,
  multi-word keys supported, longest key wins. Tray → **Dictionary** submenu: teach from
  selection, reload without restart, open the file, live vocab/correction counts
- **Teach mode** (F7, rebindable via tray → Hotkeys): fix a dictation where it was pasted,
  select the corrected text, press the teach key — the script diffs the selection against
  the last injected transcript, learns the changed word pairs into `corrections`, and adds
  new proper nouns to `vocab`. Only small word-level replacements are learned; insertions/
  deletions are content edits, and plain sentence capitalization is ignored so common words
  are never over-learned. Success = rising arpeggio, nothing-learned = single low beep
- **Ollama cleanup pass** (optional, off by default, tray-toggleable): pipes each transcript
  through a local Ollama model (`qwen2.5:14b` default) to fix casing/punctuation with a
  strict no-rephrasing instruction, dictionary vocab as spelling hints, temperature 0, a
  hard 6 s timeout, and a length sanity check — any failure falls back to the raw transcript
  so dictation never hangs. Persisted as `ollama_cleanup` / `ollama_model` in settings
- Tests: `tests/test_dictionary.py` covers the corrections/prompt/teach-diff pipeline via
  AST extraction (runs without torch/pynput installed): `python tests/test_dictionary.py`

## 2026-06-03 (later)

- Audio sessions are now matched by their Windows session-instance id instead of by list
  position, so multi-session apps (e.g. Discord) are restored to their original volume
  correctly and no longer stick at the duck level; `duck_audio()` also refuses to duck an
  already-ducked session to avoid poisoning the saved original
- Balanced the two tones within each chirp: equal 55 ms durations and a small gain lift on
  the lower tone, so the first half of a chirp no longer sounds quieter than the second.
  Both press and release chirps now share single `PRESS_CHIRP` / `RELEASE_CHIRP` constants
- Un-ducking is now delayed ~0.12 s after the release beep (`RESTORE_DELAY`) so the chirp
  isn't masked by other apps jumping back to full volume; skipped if recording resumes
- `restore_audio()` now verifies: after re-applying saved volumes it re-checks each session
  up to 4 times and re-applies any that didn't take, with a warning logged for any it can't restore
- PTT mouse button is now rebindable from the tray menu (**Hotkeys → PTT mouse button**),
  alongside the existing key rebinds. Click the entry, then press the new mouse button
  (middle / X1 / X2 — left and right click are reserved and ignored while binding; Esc cancels).
  Persisted as `ptt_mouse_button` in `ptt-settings.json`; the mouse listener picks it up live,
  no restart needed. Default remains the front thumb button (x2).

## 2026-06-03

- Tuned beep/duck levels from real use: default `BEEP_VOLUME` 0.06 → 0.10, default `DUCK_LEVEL`
  0.1 → 0.05 (other apps dim further while recording)
- Brightened the release chirp (C6→G5, longer tail) so it carries over audio that has just been
  un-ducked — the "let off" beep no longer sounds softer than the press beep
- Tray submenus now offer 5% steps: **Duck level** adds 5%, **Beep volume** is Off/5/10/15/25%

## 2026-06-02 (later)

- **Softer, snappier beeps**: default beep backend switched to `sounddevice`, played through a
  persistent low-latency output stream — removes the startup lag of `winsound.Beep`
- Added `BEEP_VOLUME` (default `0.06`, ~60% quieter than before) with a 6 ms attack/release
  fade envelope so the press/release chirps no longer click
- New **Beep volume** tray submenu (Off / 3% / 6% / 12% / 25%), persisted to `ptt-settings.json`;
  changing it plays a short preview

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
