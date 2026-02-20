# Whisper PTT — Custom Functionality Reference

Single-file voice-to-text tool (`ptt.py`). Records from audio interface, transcribes with faster-whisper (CUDA), pastes at cursor. Everything below is custom behavior built on top of the Whisper engine.

## Input Modes

### Manual PTT (F9 / Middle Mouse)
Hold to record, release to transcribe. Audio ducking activates on press, restores on release. Beep feedback: ascending chirp on press (G5→C6), descending boop on release (C6→E5).

### Hot Mic (F10 toggle)
Continuous VAD-driven dictation. No wake phrase needed — just talk. On silence gap (2s), transcribes and pastes automatically, then returns to listening. Ascending 3-tone on enable (C5→D5→E5), descending on disable.

### Wake Phrase Mode (always-on VAD)
Say **"send it"** to begin dictation. Keeps recording through silence gaps until you say **"over"** or **"disregard"**. Audio ducks after wake phrase detected. 5min safety timeout.

### VAD Toggle (F8)
Enables/disables voice activity detection entirely. High beep = on, low beep = off. When off, only manual PTT works. **Default: off** (prevents GPU churn from ambient noise triggering repeated Whisper transcriptions).

## State Machine

```
IDLE → BUFFERING (VAD detects speech)
BUFFERING → CHECKING (silence gap ≥ 2s)
CHECKING → IDLE (processed or no wake phrase)
CHECKING → BUFFERING (wake phrase found, no "over" yet)

IDLE/BUFFERING → MANUAL (F9/middle mouse pressed)
MANUAL → PROCESSING → IDLE (F9/middle mouse released)
```

F9/middle mouse interrupts any VAD state. Thread-safe via `state_lock`.

## Radio Commands

Processed in `process_commands()`. Applied after transcription, before paste.

| Command | Behavior | Position constraint |
|---------|----------|-------------------|
| `over` | Press Enter after pasting | Must be last word |
| `break` | Insert newline | Anywhere |
| `correction` | Delete previous word from output | Anywhere |
| `disregard` | Cancel entire utterance, paste nothing | Anywhere in utterance |

`over` preserves trailing `?` or `!` — e.g. "is that right over?" pastes "Is that right?" + Enter.

## Spoken Punctuation

Two-pass replacement in `process_commands()`. Two-word phrases checked first, then single-word.

### Two-word replacements (`REPLACEMENTS_2WORD`)

| Phrase | Output |
|--------|--------|
| `question mark` | `?` |
| `exclamation point` | `!` |
| `exclamation mark` | `!` |
| `home slash` | `~/` |
| `open bracket` | `[` |
| `close bracket` | `]` |
| `open paren` | `(` |
| `close paren` | `)` |
| `open brace` | `{` |
| `close brace` | `}` |
| `hash tag` | `#` |
| `dollar sign` | `$` |
| `percent sign` | `%` |
| `at symbol` | `@` |
| `at sign` | `@` |

### Single-word replacements (`REPLACEMENTS_1WORD`)

| Word | Output |
|------|--------|
| `homeslash` | `~/` |
| `comma` | `,` |
| `dot` | `.` |
| `slash` | `/` |
| `hyphen` | `-` |
| `period` | `. ` (with trailing space) |
| `colon` | `:` |
| `semicolon` | `;` |
| `caret` | `^` |
| `ampersand` | `&` |
| `asterisk` | `*` |
| `plus` | `+` |
| `equals` | `=` |
| `pipe` | `\|` |
| `backslash` | `\` |
| `tilde` | `~` |
| `backtick` | `` ` `` |
| `underscore` | `_` |

Spoken `?`, `!`, `,` (after replacement) attach to previous word with no space.

## Text Post-Processing

Applied in `process_commands()` after command/punctuation handling:

1. **Wake phrase stripping** — removes leading "send it" if Whisper included it
2. **Newline joining** — `break` tokens become `\n`, surrounding spaces trimmed
3. **Line capitalization** — first letter of each line uppercased
4. **Orphan punctuation cleanup** — strips leading/trailing `,;:` from lines
5. **Auto-period** — appends `.` if line doesn't end with `.!?`
6. **Trailing space** — adds space after final period (for continued typing)

## Audio Ducking

Via pycaw (Windows audio sessions API). On record start, all other app volumes multiplied by `DUCK_LEVEL` (default 0.1 = 10%). Restored on transcription complete. Per-process volume saved/restored by PID. Requires COM init/uninit per call.

## Clipboard Preservation

`paste_text()` saves clipboard before copying transcription, restores after paste. Uses pyperclip for clipboard, pyautogui `ctrl+v` for paste. 50ms sleep between paste and enter/restore.

## Initial Prompt (Whisper Conditioning)

`INITIAL_PROMPT` (line 49) — fed to Whisper as fake prior context to bias recognition toward:
- Conversational framing: "Conversation with Rei."
- Tech terms: Ollama, WSL, Dart
- 34 project/repo names: claude, cleo, openclaw, Ableton, whisper-ptt, etc.
- 20 punctuation symbol examples with their glyphs

Not a replacement mapping — just vocabulary hints. The actual symbol replacement is in `process_commands()`.

## Audio Pipeline

- **Device**: Volt 2 audio interface, found by name match via sounddevice
- **Format**: 16kHz mono float32
- **VAD**: Silero VAD, 512-sample chunks, threshold 0.5
- **Transcription**: faster-whisper `base` model, CUDA float16, beam_size=5, vad_filter=true
- **Stream**: sounddevice InputStream with 1600-sample blocks, callback pushes to queue (max 200)
- **Cooldown**: 5s pause (`VAD_COOLDOWN_SECS`) after failed wake-phrase cycle hits max time, prevents GPU churn
- **Threading**: VAD monitor runs in daemon thread, processes queue continuously

## Service Configuration

Windows Task Scheduler task `WhisperPTT`. Runs `pythonw.exe ptt.py` directly at logon (no bat wrapper — avoids visible cmd window). Auto-restarts up to 3x on crash, 1min interval. Not a Windows service (needs desktop access for keyboard hooks + clipboard).

## Legacy Fallback (AHK)

`whisper-ptt.ahk` + `record.bat` + `transcribe.bat` — original implementation using AutoHotkey v2, ffmpeg for recording, whisper-cpp for transcription. F9 PTT only, no VAD/wake phrase/radio commands. Superseded by `ptt.py`.
