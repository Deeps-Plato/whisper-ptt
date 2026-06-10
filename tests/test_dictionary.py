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

PTT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ptt.py")

FUNCS = ("build_initial_prompt", "apply_corrections",
         "_norm_word", "_clean_token", "_extract_pairs", "_app_profile_for",
         "_profile_field", "_profile_style")

def load_funcs():
    with open(PTT, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    wanted = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef,)) and n.name in FUNCS]
    missing = set(FUNCS) - {n.name for n in wanted}
    assert not missing, f"functions not found in ptt.py: {missing}"
    ns = {"re": re, "difflib": difflib, "json": json, "logging": logging,
          "PROMPT_TOKEN_BUDGET": 200, "_dictionary": {"corrections": {}}}
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

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
