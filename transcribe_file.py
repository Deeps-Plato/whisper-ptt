"""File transcription mode — drop an audio file, get a markdown transcript.

Record a meeting/call however you like (Audacity, phone, OBS), then:

    python transcribe_file.py recording.wav
    python transcribe_file.py recording.mp3 --structured
    python transcribe_file.py call.m4a --out notes.md

Uses the same model (model_size from ptt-settings.json), the same
dictionary.json corrections, and optionally the same local-LLM pass as live
dictation. --structured turns the transcript into meeting notes (summary,
bullets, action items) via Ollama. Output defaults to <input>.md.

Standalone on purpose: no coupling to the live ptt.py process. Handles
wav/mp3/m4a/flac/ogg and most other formats (PyAV decoding).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_json(name, default):
    try:
        with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def apply_corrections(text, corrections):
    for source in sorted(corrections, key=len, reverse=True):
        text = re.sub(rf'\b{re.escape(source)}\b', corrections[source], text,
                      flags=re.IGNORECASE)
    return text


def hms(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


MEETING_INSTRUCTION = (
    "Turn this raw transcript into clean meeting notes in markdown: a 2-3 "
    "sentence summary, then concise bullets of what was discussed, then a "
    "'## Action items' section with '- [ ] ' checkboxes. Keep ALL names, "
    "numbers, dates, and commitments. Do not invent anything."
)


def restructure(text, model, url):
    body = json.dumps({"model": model, "prompt": MEETING_INSTRUCTION +
                       f"\n\nTranscript:\n{text}", "stream": False,
                       "keep_alive": "30m", "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(f"{url}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    timeout = 30 + len(text) / 100
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())["response"].strip()


def main():
    ap = argparse.ArgumentParser(description="Transcribe an audio file with the whisper-ptt stack")
    ap.add_argument("audio", help="path to the audio file (wav/mp3/m4a/flac/...)")
    ap.add_argument("--out", help="output path (default: <input>.md)")
    ap.add_argument("--structured", action="store_true",
                    help="restructure into meeting notes via Ollama")
    ap.add_argument("--model", help="whisper model override (default: ptt-settings model_size)")
    ap.add_argument("--no-timestamps", action="store_true", help="plain text, no [hh:mm:ss] markers")
    args = ap.parse_args()

    if not os.path.exists(args.audio):
        sys.exit(f"not found: {args.audio}")

    settings = load_json("ptt-settings.json", {})
    dictionary = load_json("dictionary.json", {})
    model_size = args.model or settings.get("model_size", "base")
    corrections = dictionary.get("corrections", {})
    vocab = dictionary.get("vocab", [])
    prefix = (dictionary.get("prompt_prefix") or "").strip()
    prompt = (prefix + " " if prefix else "") + (
        "Vocabulary: " + ", ".join(vocab[:60]) + "." if vocab else "")

    print(f"loading {model_size} ...")
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(model_size, device="cuda", compute_type="float16")
    except Exception:
        print("CUDA unavailable, using CPU int8 (slower)")
        m = WhisperModel(model_size, device="cpu", compute_type="int8")

    t0 = time.time()
    segments, info = m.transcribe(args.audio, language="en", beam_size=5,
                                  vad_filter=True, initial_prompt=prompt or None)
    lines, flat = [], []
    for seg in segments:
        text = apply_corrections(seg.text.strip(), corrections)
        if not text:
            continue
        flat.append(text)
        lines.append(text if args.no_timestamps else f"`[{hms(seg.start)}]` {text}")
        print(f"  [{hms(seg.start)}] {text}")
    print(f"transcribed {hms(info.duration)} of audio in {time.time() - t0:.0f}s")

    body = "\n".join(lines)
    title = os.path.splitext(os.path.basename(args.audio))[0]
    out_text = f"# Transcript: {title}\n\n{body}\n"

    if args.structured and flat:
        print("restructuring into meeting notes ...")
        try:
            notes = restructure(" ".join(flat),
                                settings.get("ollama_model", "qwen2.5:14b"),
                                "http://localhost:11434")
            out_text = (f"# Meeting notes: {title}\n\n{notes}\n\n---\n\n"
                        f"## Full transcript\n\n{body}\n")
        except Exception as e:
            print(f"restructure failed ({e}); writing plain transcript")

    out = args.out or os.path.splitext(args.audio)[0] + ".md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(out_text)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
