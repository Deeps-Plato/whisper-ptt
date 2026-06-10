"""Tests for the managed-dictionary text pipeline.

ptt.py imports CUDA/audio/input libraries at module level, so importing it on a
machine without the full stack would fail. Instead we AST-extract the pure text
functions and exec them with only stdlib injected — they are deliberately
written to depend on nothing heavier than re/difflib/json/logging.

Run:  python tests/test_dictionary.py
"""
import ast
import difflib
import json
import logging
import os
import re
import sys
import urllib.parse

PTT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ptt.py")

FUNCS = ("build_initial_prompt", "apply_corrections",
         "_norm_word", "_clean_token", "_extract_pairs", "_app_profile_for",
         "_profile_field", "_profile_style", "strip_punctuation", "process_commands",
         "_match_voice_command")

def load_funcs():
    with open(PTT, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    wanted = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef,)) and n.name in FUNCS]
    missing = set(FUNCS) - {n.name for n in wanted}
    assert not missing, f"functions not found in ptt.py: {missing}"
    ns = {"re": re, "difflib": difflib, "json": json, "logging": logging,
          "urllib": urllib,
          "PROMPT_TOKEN_BUDGET": 200, "_dictionary": {"corrections": {}},
          "WAKE_PHRASE": "send it"}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), PTT, "exec"), ns)
    return ns

ns = load_funcs()
build_initial_prompt = ns["build_initial_prompt"]
apply_corrections    = ns["apply_corrections"]
_extract_pairs       = ns["_extract_pairs"]

passed = failed = 0

def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name}\n  got:  {got!r}\n  want: {want!r}")

# ── apply_corrections ────────────────────────────────────────────────
check("corrections: basic replace",
      apply_corrections("we ship in express daily", {"in express": "InXpress"}),
      "we ship InXpress daily")
check("corrections: case-insensitive match",
      apply_corrections("In Express quoted it", {"in express": "InXpress"}),
      "InXpress quoted it")
check("corrections: longest key wins",
      apply_corrections("login to us web ship now",
                        {"web ship": "Webship", "us web ship": "USWebShip"}),
      "login to USWebShip now")
check("corrections: whole-word only (no substring hits)",
      apply_corrections("the discount expressway", {"in express": "InXpress"}),
      "the discount expressway")
check("corrections: empty text passthrough",
      apply_corrections("", {"a": "b"}), "")

# ── build_initial_prompt ─────────────────────────────────────────────
check("prompt: prefix only",
      build_initial_prompt({"prompt_prefix": "Freight dictation.", "vocab": []}),
      "Freight dictation.")
check("prompt: prefix + vocab",
      build_initial_prompt({"prompt_prefix": "Freight.", "vocab": ["NMFC", "BOL"]}),
      "Freight. Vocabulary: NMFC, BOL.")
check("prompt: vocab deduped",
      build_initial_prompt({"prompt_prefix": "", "vocab": ["NMFC", "NMFC", "BOL"]}),
      "Vocabulary: NMFC, BOL.")

big = {"prompt_prefix": "P.", "vocab": [f"LongTerm{i:03d}" for i in range(200)]}
trimmed = build_initial_prompt(big)
check("prompt: over-budget is trimmed", len(trimmed) // 3 <= 200, True)
check("prompt: trimming keeps the head of the list",
      "LongTerm000" in trimmed and "LongTerm199" not in trimmed, True)

# ── _extract_pairs (teach diff) ──────────────────────────────────────
check("teach: simple misheard word",
      _extract_pairs("ship it with jansen today", "ship it with Janszen today"),
      [("jansen", "Janszen")])
check("teach: multi-word to one word",
      _extract_pairs("tea force freight quoted", "TForce Freight quoted"),
      [("tea force", "TForce")])
check("teach: insertion is not a correction",
      _extract_pairs("ship the pallet", "ship the heavy pallet"),
      [])
check("teach: deletion is not a correction",
      _extract_pairs("ship the heavy pallet", "ship the pallet"),
      [])
check("teach: jargon casing is learned",
      _extract_pairs("file the nmfc code", "file the NMFC code"),
      [("nmfc", "NMFC")])
check("teach: plain capitalization is NOT learned",
      _extract_pairs("the discount products arrived", "The Discount Products arrived"),
      [])
check("teach: punctuation stripped from learned pair",
      _extract_pairs("call in express, then book", "call InXpress, then book"),
      [("in express", "InXpress")])
check("teach: identical text learns nothing",
      _extract_pairs("all good here", "all good here"),
      [])
check("teach: mixed real-world edit",
      _extract_pairs("Quote air bill for jan's in discount products.",
                     "Quote airbill for Janszen Discount Products."),
      _extract_pairs("Quote air bill for jan's in discount products.",
                     "Quote airbill for Janszen Discount Products."))  # smoke: no crash
mixed = _extract_pairs("Quote air bill for jan's in discount products.",
                       "Quote airbill for Janszen Discount Products.")
check("teach: mixed edit learns the airbill merge",
      any(r == "airbill" for _, r in mixed), True)

# ── _app_profile_for (per-app awareness) ─────────────────────────────
_app_profile_for = ns["_app_profile_for"]
PROFILES = {"outlook": "email style", "discord|slack": "chat style",
            "windows terminal|powershell": "skip"}
check("profiles: title substring match",
      _app_profile_for("RE: Courier to Lockheed - Outlook", PROFILES),
      "email style")
check("profiles: case-insensitive",
      _app_profile_for("OUTLOOK", PROFILES), "email style")
check("profiles: alternative key matches",
      _app_profile_for("#freight-chat - Slack", PROFILES), "chat style")
check("profiles: skip value returned verbatim",
      _app_profile_for("Administrator: Windows Terminal", PROFILES), "skip")
check("profiles: no match returns None",
      _app_profile_for("Some Random App", PROFILES), None)
check("profiles: empty title returns None",
      _app_profile_for("", PROFILES), None)
check("profiles: empty profiles returns None",
      _app_profile_for("Outlook", {}), None)

# ── object-form profiles (per-app dictionaries) ──────────────────────
_profile_field = ns["_profile_field"]
_profile_style = ns["_profile_style"]
OBJ = {"style": "chat style", "vocab": ["Gothic"], "corrections": {"usc": "UFC"}}
check("profiles: style from string profile", _profile_style("email style"), "email style")
check("profiles: style from object profile", _profile_style(OBJ), "chat style")
check("profiles: style of styleless object is empty", _profile_style({"vocab": ["x"]}), "")
check("profiles: vocab from object", _profile_field(OBJ, "vocab"), ["Gothic"])
check("profiles: vocab from string profile is None", _profile_field("email style", "vocab"), None)
check("profiles: corrections from object", _profile_field(OBJ, "corrections"), {"usc": "UFC"})
check("profiles: object profile returned whole by matcher",
      _app_profile_for("#general - Discord", {"discord": OBJ}), OBJ)

# ── process_commands radio modes (manual "over" opt-in) ──────────────
process_commands = ns["process_commands"]
check("over-mode: trailing over presses Enter",
      process_commands("send the quote over", radio="over"),
      ("Send the quote. ", True))
check("over-mode: mid-sentence over is just a word",
      process_commands("over the weekend we ship", radio="over")[1], False)
check("over-mode: correction NOT honored (stays literal)",
      "correction" in process_commands("ship it correction now", radio="over")[0].lower(),
      True)
check("over-mode: disregard NOT honored (stays literal)",
      process_commands("never mind disregard", radio="over")[0] is not None, True)
check("radio off: trailing over stays literal",
      process_commands("send the quote over", radio=False)[1], False)
check("full radio: correction still works",
      process_commands("hello world correction there", radio=True),
      ("Hello there. ", False))

# ── voice command matching ───────────────────────────────────────────
_match_voice_command = ns["_match_voice_command"]
CMDS = {"screenshot": "keys:win+shift+s", "task manager": "keys:ctrl+shift+esc"}
check("voice: exact match",
      _match_voice_command("command screenshot", CMDS, "command"),
      ("keys:win+shift+s", True))
check("voice: punctuation tolerated",
      _match_voice_command("Command, screenshot.", CMDS, "command"),
      ("keys:win+shift+s", True))
check("voice: partial phrase matches",
      _match_voice_command("command task", CMDS, "command"),
      ("keys:ctrl+shift+esc", True))
check("voice: unknown command consumed but no action",
      _match_voice_command("command make coffee", CMDS, "command"),
      (None, True))
check("voice: normal dictation not consumed",
      _match_voice_command("the command center is busy", CMDS, "command"),
      (None, False))
check("voice: bare prefix consumed, no action",
      _match_voice_command("command", CMDS, "command"), (None, True))

CMDS2 = {"screenshot": "keys:win+shift+s",
         "browse to reddit": "run:start chrome https://old.reddit.com/top/"}
check("voice: full-phrase key defines its own trigger",
      _match_voice_command("Browse to Reddit.", CMDS2, "command"),
      ("run:start chrome https://old.reddit.com/top/", True))
check("voice: full-phrase partial match",
      _match_voice_command("browse to red", CMDS2, "command"),
      ("run:start chrome https://old.reddit.com/top/", True))
check("voice: unknown browse target consumed, no action",
      _match_voice_command("browse to facebook", CMDS2, "command"),
      (None, True))
check("voice: classic key still works alongside",
      _match_voice_command("command screenshot", CMDS2, "command"),
      ("keys:win+shift+s", True))
check("voice: non-trigger first word untouched",
      _match_voice_command("browsing the rates now", CMDS2, "command"),
      (None, False))

CMDS3 = {"search google *": "run:start chrome https://www.google.com/search?q={query}",
         "search reddit *": "run:start chrome https://old.reddit.com/search?q={query}"}
check("voice: wildcard captures and URL-encodes the query",
      _match_voice_command("Search Google cheapest liftgate carriers", CMDS3, "command"),
      ("run:start chrome https://www.google.com/search?q=cheapest%20liftgate%20carriers", True))
check("voice: wildcard family routing",
      _match_voice_command("search reddit gothic remake", CMDS3, "command"),
      ("run:start chrome https://old.reddit.com/search?q=gothic%20remake", True))
check("voice: near-miss (engine, no query) consumed without action",
      _match_voice_command("search google", CMDS3, "command"),
      (None, True))
check("voice: ordinary 'search ...' speech passes through untouched",
      _match_voice_command("search the vault for the janszen quote", CMDS3, "command"),
      (None, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
