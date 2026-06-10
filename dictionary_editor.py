"""Dictionary editor GUI for whisper-ptt.

Standalone viewer/editor for dictionary.json — vocab and corrections, scoped
Global or per-app (app_profiles entries). ptt.py hot-reloads the file on its
next transcription (mtime check), so additions apply to the very next
dictation: no restart, no tray click.

Run:  pythonw dictionary_editor.py   (or via tray → Dictionary → Edit)
"""
import json
import os
import tkinter as tk
from tkinter import ttk, messagebox

DICT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictionary.json")
GLOBAL = "Global"


def load_dict():
    with open(DICT_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("vocab", [])
    d.setdefault("corrections", {})
    d.setdefault("app_profiles", {})
    return d


def save_dict(d):
    tmp = DICT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DICT_FILE)


class Editor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("whisper-ptt dictionary")
        self.geometry("680x460")
        self.data = load_dict()
        self.dirty = False

        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Scope:").pack(side="left")
        self.scope = tk.StringVar(value=GLOBAL)
        self.scope_box = ttk.Combobox(top, textvariable=self.scope, state="readonly",
                                      values=self._scopes(), width=40)
        self.scope_box.pack(side="left", padx=6)
        self.scope_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(top, text="Save", command=self.save).pack(side="right")
        self.status = ttk.Label(top, text="", foreground="#555")
        self.status.pack(side="right", padx=10)

        body = ttk.Frame(self, padding=6)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        # vocab column
        ttk.Label(body, text="Vocab (terms Whisper should know)").grid(row=0, column=0, sticky="w")
        self.vocab_list = tk.Listbox(body, activestyle="none")
        self.vocab_list.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        vf = ttk.Frame(body)
        vf.grid(row=2, column=0, sticky="ew", padx=(0, 8), pady=4)
        vf.columnconfigure(0, weight=1)
        self.vocab_entry = ttk.Entry(vf)
        self.vocab_entry.grid(row=0, column=0, sticky="ew")
        self.vocab_entry.bind("<Return>", lambda e: self.add_vocab())
        ttk.Button(vf, text="Add", width=6, command=self.add_vocab).grid(row=0, column=1, padx=2)
        ttk.Button(vf, text="Delete selected", command=self.del_vocab).grid(row=1, column=0,
                                                                            columnspan=2, sticky="ew", pady=2)

        # corrections column
        ttk.Label(body, text="Corrections (misheard → correct)").grid(row=0, column=1, sticky="w")
        self.corr_list = tk.Listbox(body, activestyle="none")
        self.corr_list.grid(row=1, column=1, sticky="nsew")
        cf = ttk.Frame(body)
        cf.grid(row=2, column=1, sticky="ew", pady=4)
        cf.columnconfigure(0, weight=1)
        cf.columnconfigure(2, weight=1)
        self.wrong_entry = ttk.Entry(cf)
        self.wrong_entry.grid(row=0, column=0, sticky="ew")
        ttk.Label(cf, text="→").grid(row=0, column=1, padx=4)
        self.right_entry = ttk.Entry(cf)
        self.right_entry.grid(row=0, column=2, sticky="ew")
        self.right_entry.bind("<Return>", lambda e: self.add_corr())
        ttk.Button(cf, text="Add", width=6, command=self.add_corr).grid(row=0, column=3, padx=2)
        ttk.Button(cf, text="Delete selected", command=self.del_corr).grid(row=1, column=0,
                                                                           columnspan=4, sticky="ew", pady=2)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()

    # ── scope plumbing ────────────────────────────────────────────
    def _scopes(self):
        return [GLOBAL] + list(self.data.get("app_profiles", {}).keys())

    def _bucket(self):
        """(vocab_list, corrections_dict) for the current scope, creating the
        per-app structure on demand (string profiles upgrade to objects)."""
        if self.scope.get() == GLOBAL:
            return self.data["vocab"], self.data["corrections"]
        key = self.scope.get()
        prof = self.data["app_profiles"].get(key, "")
        if isinstance(prof, str):
            prof = {"style": prof, "vocab": [], "corrections": {}}
            self.data["app_profiles"][key] = prof
        prof.setdefault("vocab", [])
        prof.setdefault("corrections", {})
        return prof["vocab"], prof["corrections"]

    # ── actions ───────────────────────────────────────────────────
    def refresh(self):
        vocab, corr = self._bucket()
        self.vocab_list.delete(0, "end")
        for w in vocab:
            self.vocab_list.insert("end", w)
        self.corr_list.delete(0, "end")
        for k in sorted(corr):
            self.corr_list.insert("end", f"{k}  →  {corr[k]}")

    def add_vocab(self):
        w = self.vocab_entry.get().strip()
        if not w:
            return
        vocab, _ = self._bucket()
        if w not in vocab:
            vocab.append(w)
            self.mark_dirty()
        self.vocab_entry.delete(0, "end")
        self.refresh()

    def del_vocab(self):
        vocab, _ = self._bucket()
        for i in reversed(self.vocab_list.curselection()):
            del vocab[i]
            self.mark_dirty()
        self.refresh()

    def add_corr(self):
        wrong = self.wrong_entry.get().strip().lower()
        right = self.right_entry.get().strip()
        if not wrong or not right:
            return
        _, corr = self._bucket()
        corr[wrong] = right
        self.mark_dirty()
        self.wrong_entry.delete(0, "end")
        self.right_entry.delete(0, "end")
        self.refresh()

    def del_corr(self):
        _, corr = self._bucket()
        keys = sorted(corr)
        for i in reversed(self.corr_list.curselection()):
            del corr[keys[i]]
            self.mark_dirty()
        self.refresh()

    def mark_dirty(self):
        self.dirty = True
        self.status.config(text="unsaved changes")

    def save(self):
        try:
            save_dict(self.data)
            self.dirty = False
            self.status.config(text="saved — applies on next dictation")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_close(self):
        if self.dirty and messagebox.askyesno("Unsaved changes", "Save before closing?"):
            self.save()
        self.destroy()


if __name__ == "__main__":
    Editor().mainloop()
