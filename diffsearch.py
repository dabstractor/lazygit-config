#!/usr/bin/env python3
"""diffsearch — regex search across all uncommitted git changes.

Designed as a lazygit custom-command overlay (launched with subprocess/output
"terminal"): lazygit suspends, this TUI takes over the terminal, and lazygit
redraws when it exits.

Layout
    ─ search: <regex> ──────────── scope:src/lib · 3/7 files · 12 matches ─
    │ file tree (every changed    │ concatenated diffs of the files that   │
    │   file, staged/unstaged/     │ match the query, with every match     │
    │   untracked)                 │ highlighted                            │
    ────────────────────────────────────────────────────────────────────────

Interaction (all keys configurable in diffsearch.toml):
  * the search box is focused on start — type a regex (smart-case by default)
  * Esc      focus the file tree
  * j / k    move in the file tree
  * Enter    scope the search to the selected directory (or single file)
             and return focus to the search box
  * h / l    collapse / expand tree directories
  * Backspace  pop the scope up one directory
  * J / K    scroll the results pane (PgUp/PgDn work in the search box too)
  * q, Ctrl-C  quit

Files whose diff has no match stay in the tree (dimmed) but drop out of the
results pane, so only matching diffs remain.

Non-interactive mode (also used for testing):
    diffsearch.py --print 'TODO'     # dump filtered diffs, grep-like exit code

Everything shown is `git diff HEAD` per file (staged + unstaged combined) plus
untracked files rendered as pure additions.
"""
from __future__ import annotations

import argparse
import curses
import locale
import os
import re
import subprocess
import sys
import tomllib

VERSION = "1.0.0"
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

MAX_FILE_BYTES = 2_000_000   # cap for reading untracked files
MAX_DIFF_LINES = 5_000       # cap per file diff
CTX = ["-U3"]                # git diff context lines (set from config)

# ---------------------------------------------------------------- config ----

DEFAULTS: dict = {
    "keys": {
        "up": "k",            # tree: move up
        "down": "j",          # tree: move down
        "scroll_up": "K",     # scroll results up
        "scroll_down": "J",   # scroll results down
        "scope": "ENTER",     # tree: scope search to selection, focus search box
        "jump": "ENTER",      # search: jump to first match (focuses tree)
        "to_tree": "ESC",     # search: focus the file tree
        "focus_search": "/",  # tree: focus the search box
        "scope_up": "BACKSPACE",  # tree: pop scope up one directory
        "expand": "l",
        "collapse": "h",
        "quit": "q",
        "clear_query": "C-u",
    },
    "search": {
        "regex": True,        # false = plain fixed-string search
        "case": "smart",      # smart | sensitive | insensitive
    },
    "ui": {
        "tree_width": 0,      # 0 = auto (30% of width, clamped 24..48)
        "context_lines": 3,   # git diff context lines
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif k in base:
            base[k] = v
    return base


def load_config(path: str | None) -> dict:
    cfg = {sec: dict(vals) for sec, vals in DEFAULTS.items()}
    path = path or os.path.join(SCRIPT_DIR, "diffsearch.toml")
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                _merge(cfg, tomllib.load(fh))
        except Exception as ex:  # noqa: BLE001
            print(f"diffsearch: warning: bad config {path}: {ex}", file=sys.stderr)
    for sec, vals in cfg.items():
        cfg[sec] = {k: (str(v) if k in DEFAULTS[sec] and isinstance(DEFAULTS[sec][k], str) else v)
                    for k, v in vals.items()}
    return cfg


# ------------------------------------------------------------------- git ----

def run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


class Entry:
    __slots__ = ("path", "x", "y", "untracked", "diff_lines", "match_lines")

    def __init__(self, path: str, x: str, y: str, untracked: bool, diff_lines: list[str]):
        self.path, self.x, self.y, self.untracked = path, x, y, untracked
        self.diff_lines = diff_lines
        self.match_lines: list[int] | None = None


def untracked_diff(root: str, rel: str) -> list[str]:
    hdr = [f"diff --git a/{rel} b/{rel}",
           "new file mode 100644",
           "--- /dev/null",
           f"+++ b/{rel}"]
    full = os.path.join(root, rel)
    try:
        with open(full, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as ex:
        return hdr + [f"(unreadable: {ex})"]
    if b"\0" in raw[:8192]:
        return hdr + ["Binary file (untracked)"]
    lines = raw.decode("utf-8", "replace").splitlines()
    note = None
    if len(raw) > MAX_FILE_BYTES or len(lines) > MAX_DIFF_LINES:
        note = "[…file truncated…]"
        lines = lines[:MAX_DIFF_LINES]
    out = hdr + ([f"@@ -0,0 +1,{len(lines)} @@"] if lines else []) + ["+" + l for l in lines]
    if note:
        out.append(note)
    return out


def collect() -> tuple[str, list[Entry]]:
    rc, root, err = run_git(["rev-parse", "--show-toplevel"], os.getcwd())
    root = root.strip()
    if rc != 0 or not root:
        print(f"diffsearch: {err.strip() or 'not inside a git repository'}", file=sys.stderr)
        sys.exit(2)
    rc, out, err = run_git(
        ["-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=all"], root)
    if rc != 0:
        print(f"diffsearch: {err.strip()}", file=sys.stderr)
        sys.exit(2)
    have_head = run_git(["rev-parse", "--verify", "--quiet", "HEAD"], root)[0] == 0
    entries: list[Entry] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        x, y, rest = line[0], line[1], line[3:]
        path = rest
        if x in "RC" and " -> " in rest:
            path = rest.split(" -> ", 1)[1]
        untracked = x == "?" and y == "?"
        if untracked:
            dl = untracked_diff(root, path)
        else:
            base = ["diff", "HEAD"] if have_head else ["diff"]
            dl = run_git(base + CTX + ["--", path], root)[1].splitlines()
            if len(dl) > MAX_DIFF_LINES:
                dl = dl[:MAX_DIFF_LINES] + ["[…diff truncated…]"]
        entries.append(Entry(path, x, y, untracked, dl))
    return root, entries


def set_context_lines(cfg: dict) -> None:
    global CTX
    CTX = ["-U%d" % int(cfg["ui"].get("context_lines", 3))]


# ------------------------------------------------------------------ tree ----

class Node:
    __slots__ = ("name", "path", "is_dir", "children", "entry", "expanded", "parent")

    def __init__(self, name: str, path: str, is_dir: bool, parent=None,
                 entry: "Entry | None" = None):
        self.name, self.path, self.is_dir = name, path, is_dir
        self.parent, self.entry = parent, entry
        self.expanded = True
        self.children: list = []


def build_tree(entries: list) -> Node:
    root = Node("", "", True)

    def dir_node(parent: Node, name: str) -> Node:
        for c in parent.children:
            if c.is_dir and c.name == name:
                return c
        n = Node(name, f"{parent.path}/{name}" if parent.path else name, True, parent)
        parent.children.append(n)
        return n

    for e in entries:
        parts = e.path.split("/")
        node = root
        for p in parts[:-1]:
            node = dir_node(node, p)
        leaf = Node(parts[-1], e.path, False, node, e)
        node.children.append(leaf)

    def sort_rec(n: Node) -> None:
        n.children.sort(key=lambda c: (0 if c.is_dir else 1, c.name.lower()))
        for c in n.children:
            if c.is_dir:
                sort_rec(c)

    sort_rec(root)
    return root


def auto_expand(root: Node, total: int) -> None:
    def set_all(n: Node, val: bool) -> None:
        n.expanded = val
        for c in n.children:
            if c.is_dir:
                set_all(c, val)
    if total <= 25:
        set_all(root, True)
    else:
        for c in root.children:
            if c.is_dir:
                set_all(c, False)
        root.expanded = True


UP = Node("..", "..", True)  # sentinel row for "scope up"


def find_node(root: Node, path: str) -> Node | None:
    node = root
    for p in path.strip("/").split("/"):
        if not p:
            continue
        node = next((c for c in node.children if c.name == p), None)
        if node is None:
            return None
    return node


# --------------------------------------------------------------- matching ---

def compile_query(q: str, cfg: dict):
    """Return (regex_or_None, error_or_None). None regex == empty query == match all."""
    if not q:
        return None, None
    case = cfg["search"].get("case", "smart")
    flags = 0
    if case == "insensitive" or (case == "smart" and not any(c.isupper() for c in q)):
        flags |= re.IGNORECASE
    pat = q if cfg["search"].get("regex", True) else re.escape(q)
    try:
        return re.compile(pat, flags), None
    except re.error as ex:
        return None, str(ex)


def match_spans(rx, text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in rx.finditer(text)] if rx else []


def diff_attr(line: str) -> int:
    if line.startswith(("diff --git", "index ", "old mode", "new mode", "new file mode",
                        "deleted file mode", "similarity index", "rename from", "rename to")):
        return ATTR["meta"]
    if line.startswith("@@"):
        return ATTR["hunk"]
    if line.startswith(("+++", "---")):
        return ATTR["meta"]
    if line.startswith("+"):
        return ATTR["add"]
    if line.startswith("-"):
        return ATTR["del"]
    if line.startswith("\\"):
        return ATTR["dim"]
    return ATTR["plain"]


# ------------------------------------------------------------- key naming ---

SPECIAL_KEYS = {
    10: "ENTER", 13: "ENTER", 343: "ENTER",
    27: "ESC", 9: "TAB",
    8: "BACKSPACE", 127: "BACKSPACE", 263: "BACKSPACE",
    259: "UP", 258: "DOWN", 260: "LEFT", 261: "RIGHT",
    339: "PGUP", 338: "PGDN", 262: "HOME", 360: "END",
    410: "RESIZE",
}


def key_label(v) -> str:
    if isinstance(v, int):
        return SPECIAL_KEYS.get(v, f"KEY{v}")
    if isinstance(v, str) and len(v) == 1:
        k = ord(v)
        if k in SPECIAL_KEYS:
            return SPECIAL_KEYS[k]
        if k < 32:
            return "C-" + chr(k + 96)
        return v
    return str(v)


# ------------------------------------------------------------------- TUI ----

ATTR: dict[str, int] = {}


def init_attrs() -> None:
    ATTR.clear()
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for name, num, col, extra in (
            ("add", 1, curses.COLOR_GREEN, 0),
            ("del", 2, curses.COLOR_RED, 0),
            ("hunk", 3, curses.COLOR_CYAN, curses.A_BOLD),
            ("hdr", 4, curses.COLOR_YELLOW, curses.A_BOLD),
            ("dir", 5, curses.COLOR_CYAN, curses.A_BOLD),
            ("untracked", 6, curses.COLOR_MAGENTA, curses.A_BOLD),
            ("meta", 7, curses.COLOR_BLUE, 0),
        ):
            curses.init_pair(num, col, -1)
            ATTR[name] = curses.color_pair(num) | extra
    else:
        for name, extra in (("add", 0), ("del", 0), ("hunk", curses.A_BOLD),
                            ("hdr", curses.A_BOLD), ("dir", curses.A_BOLD),
                            ("untracked", curses.A_BOLD), ("meta", 0)):
            ATTR[name] = extra
    ATTR["plain"] = 0
    ATTR["dim"] = curses.A_DIM
    ATTR["match"] = curses.A_REVERSE | curses.A_BOLD
    ATTR["cursor"] = curses.A_REVERSE


class App:
    MODE_SEARCH, MODE_TREE = "search", "tree"

    def __init__(self, std, cfg, entries, args):
        self.std, self.cfg, self.args = std, cfg, args
        self.entries = entries
        self.root = build_tree(entries)
        auto_expand(self.root, len(entries))
        self.scope = self.root
        self.mode = self.MODE_SEARCH
        self.query, self.qpos = "", 0
        self.rx, self.rx_err = None, None
        self.scroll = 0
        self.tree_top = 0
        self.cursor = 0
        self.rows: list[tuple[Node, int]] = []
        self.res: list[tuple[str, str, Entry | None]] = []
        self.file_start: dict[str, int] = {}
        self.agg_fm: dict[int, Node | None] = {}
        self.agg_m: dict[int, int] = {}
        self.agg_t: dict[int, int] = {}
        self.ns = self.nt = self.mlines = 0
        self.pane_h = 10
        self.H = self.W = 0
        self.rebuild_rows()
        self.rebuild_results()
        if args.select:
            self.reveal(args.select)

    # ---- geometry helpers

    def tree_w(self) -> int:
        w = self.cfg["ui"].get("tree_width", 0)
        if not w or w < 8:
            w = max(24, min(48, self.W * 3 // 10))
        return min(w, max(10, self.W // 2))

    def max_scroll(self) -> int:
        return max(0, len(self.res) - self.pane_h)

    # ---- model updates

    def rebuild_rows(self, keep: Node | None = None):
        rows: list[tuple[Node, int]] = []
        if self.scope is not self.root:
            rows.append((UP, 0))

        def walk(n: Node, d: int) -> None:
            for ch in n.children:
                rows.append((ch, d))
                if ch.is_dir and ch.expanded:
                    walk(ch, d + 1)

        walk(self.scope, 0)
        self.rows = rows
        self.cursor = min(self.cursor, max(0, len(rows) - 1))
        if keep is not None:
            for i, (n, _) in enumerate(rows):
                if n is keep:
                    self.cursor = i
                    break
        self.ensure_cursor_visible()

    def ensure_cursor_visible(self) -> None:
        if self.cursor < self.tree_top:
            self.tree_top = self.cursor
        if self.cursor >= self.tree_top + self.pane_h:
            self.tree_top = self.cursor - self.pane_h + 1

    def scope_entries(self) -> list[Entry]:
        out: list[Entry] = []

        def walk(n: Node) -> None:
            for ch in n.children:
                if ch.is_dir:
                    walk(ch)
                else:
                    out.append(ch.entry)

        walk(self.scope)
        return out

    def is_match(self, e: Entry) -> bool:
        return self.rx is None or bool(e.match_lines)

    def rebuild_results(self) -> None:
        res: list[tuple[str, str, Entry | None]] = []
        self.file_start = {}
        self.agg_fm, self.agg_m, self.agg_t = {}, {}, {}
        ns = mlines = 0

        def agg(n: Node) -> tuple[Node | None, int, int]:
            fm, m, t = None, 0, 0
            for ch in n.children:
                if ch.is_dir:
                    cfm, cm, ct = agg(ch)
                else:
                    e = ch.entry
                    ism = self.is_match(e)
                    cfm = ch if ism else None
                    cm = 1 if ism else 0
                    ct = 1
                if fm is None:
                    fm = cfm
                m += cm
                t += ct
            self.agg_fm[id(n)] = fm
            self.agg_m[id(n)] = m
            self.agg_t[id(n)] = t
            return fm, m, t

        agg(self.scope)

        for e in self.scope_entries():
            if not self.is_match(e):
                continue
            ns += 1
            mlines += len(e.match_lines or [])
            self.file_start[e.path] = len(res)
            n = len(e.match_lines or [])
            chip = f"{e.x}{e.y}"
            hdr = f"── {e.path}  ·  {chip}"
            if self.rx is not None:
                hdr += f"  ·  {n} match lines"
            res.append(("hdr", hdr, e))
            for l in e.diff_lines:
                res.append(("diff", l, e))
            res.append(("blank", "", None))
        self.res = res
        self.ns, self.nt, self.mlines = ns, len(self.scope_entries()), mlines
        self.scroll = min(self.scroll, self.max_scroll())

    def on_query_edit(self) -> None:
        rx, err = compile_query(self.query, self.cfg)
        if err:
            self.rx_err = err  # keep previous results on screen
            return
        self.rx, self.rx_err = rx, None
        for e in self.entries:
            if rx is None:
                e.match_lines = None
            else:
                e.match_lines = [i for i, l in enumerate(e.diff_lines) if rx.search(l)]
        self.rebuild_results()

    def apply_scope(self, node: Node) -> None:
        self.scope = node
        self.rebuild_rows()
        self.rebuild_results()
        self.cursor = 0
        self.tree_top = 0
        self.scroll = 0

    def scope_up(self) -> None:
        if self.scope.parent is not None:
            self.apply_scope(self.scope.parent)

    def reveal(self, path: str) -> None:
        path = path.strip().strip("/")
        if not path:
            return
        node = find_node(self.root, path)
        if node is None:
            return
        if node.is_dir:
            # selecting a directory scopes the search to that branch
            if node is not self.root:
                self.apply_scope(node)
                if self.rows and self.rows[0][0] is UP and len(self.rows) > 1:
                    self.cursor = 1
                    self.ensure_cursor_visible()
            return
        # file: expand ancestors and put the cursor on it
        cur = node.parent
        while cur is not None:
            cur.expanded = True
            cur = cur.parent
        self.rebuild_rows()
        sp = self.scope.path
        if sp and path != sp and not path.startswith(sp + "/"):
            # the target lives outside the current scope — follow it
            self.scope = node.parent or self.root
            self.rebuild_rows()
            self.rebuild_results()
        for i, (n, _) in enumerate(self.rows):
            if n is node or (n.entry is not None and n.entry.path == path):
                self.cursor = i
                break
        self.ensure_cursor_visible()
        self.sync_scroll()

    def sync_scroll(self) -> None:
        if not self.rows:
            return
        n = self.rows[self.cursor][0]
        fm = None if n is UP else self.agg_fm.get(id(n))
        if fm is not None and fm.entry is not None and fm.entry.path in self.file_start:
            self.scroll = min(self.file_start[fm.entry.path], self.max_scroll())

    def jump_first_match(self) -> None:
        self.mode = self.MODE_TREE
        for e in self.scope_entries():
            if self.is_match(e):
                self.reveal(e.path)
                return

    def move(self, d: int) -> None:
        self.cursor = max(0, min(len(self.rows) - 1, self.cursor + d))
        self.ensure_cursor_visible()
        self.sync_scroll()

    def scroll_by(self, d: int) -> None:
        self.scroll = max(0, min(self.max_scroll(), self.scroll + d))

    # ---- drawing

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if y < 0 or x < 0 or y >= self.H or x >= self.W or not text:
            return
        try:
            self.std.addstr(y, x, text[: max(0, self.W - x - 1)], attr)
        except curses.error:
            pass

    def draw(self) -> None:
        self.std.erase()
        self.H, self.W = self.std.getmaxyx()
        if self.H < 12 or self.W < 44:
            self.put(self.H // 2, max(0, (self.W - 24) // 2), "terminal too small", curses.A_BOLD)
            self.std.refresh()
            self.pane_h = 1
            return
        tw = self.tree_w()
        self.draw_search(0, 0, self.W, tw)
        try:
            self.std.hline(1, 1, curses.ACS_HLINE, self.W - 2)
            self.std.hline(self.H - 2, 1, curses.ACS_HLINE, self.W - 2)
            for r in range(2, self.H - 2):
                self.std.vline(r, tw, curses.ACS_VLINE, 1)
        except curses.error:
            pass
        self.pane_h = self.H - 4
        self.draw_tree(2, 1, tw - 2, self.pane_h)
        self.draw_results(2, tw + 2, self.W - tw - 4, self.pane_h)
        self.draw_status(self.H - 1, 0, self.W)
        self.std.refresh()

    def draw_search(self, y: int, x: int, w: int, tw: int) -> None:
        focused = self.mode == self.MODE_SEARCH
        self.put(y, 1, " search", ATTR["hdr"] if focused else ATTR["dim"])
        if self.rx_err:
            right = f"⚠ bad regex: {self.rx_err} "
            rattr = ATTR["del"] | curses.A_BOLD
        else:
            srel = self.scope.path or "."
            right = f"scope:{srel}"
            if self.rx is None:
                right += f"  ·  {self.nt} files"
            else:
                right += f"  ·  {self.ns}/{self.nt} files  ·  {self.mlines} match lines"
            rattr = ATTR["dim"]
        rx_ = max(12, w - 2 - len(right))
        self.put(y, rx_, right, rattr)
        qx = 9
        avail = max(1, rx_ - qx - 2)
        q, pos = self.query, self.qpos
        if len(q) > avail:  # keep the cursor visible in a sliding window
            lo = max(0, pos - avail + 1) if pos >= avail else 0
            hi = lo + avail
            q, pos = q[lo:hi], pos - lo
        self.put(y, qx, q, curses.A_BOLD if focused else 0)
        cch = q[pos] if pos < len(q) else " "
        self.put(y, qx + pos, cch, ATTR["match"] if focused else ATTR["dim"])

    def draw_tree(self, y0: int, x0: int, w: int, h: int) -> None:
        for i in range(h):
            ri = self.tree_top + i
            if ri >= len(self.rows):
                break
            n, d = self.rows[ri]
            if n is UP:
                text = "‥ (scope up)"
                self.put(y0 + i, x0, text[:w], ATTR["dim"])
                if ri == self.cursor:
                    self.put(y0 + i, x0, text[:w].ljust(w), ATTR["cursor"])
                continue
            if n.is_dir:
                mk = "▾ " if n.expanded else "▸ "
                text = f"{'  ' * d}{mk}{n.name}/"
                attr = ATTR["dir"]
                if self.rx is not None:
                    m, t = self.agg_m.get(id(n), 0), self.agg_t.get(id(n), 0)
                    if ri == self.cursor:
                        self.put(y0 + i, x0, (text + f"  {m}/{t}")[:w].ljust(w), ATTR["cursor"])
                    else:
                        self.put(y0 + i, x0, text[:w], attr)
                        self.put(y0 + i, x0 + len(text) + 1, f"{m}/{t}",
                                 ATTR["hdr"] if m else ATTR["dim"])
                else:
                    self.put(y0 + i, x0, text[:w].ljust(w) if ri == self.cursor else text[:w],
                             ATTR["cursor"] if ri == self.cursor else attr)
                continue
            e = n.entry
            chip = f"{e.x}{e.y}"
            text = f"{chip} {'  ' * d}{n.name}"
            dim = self.rx is not None and not e.match_lines
            attr = ATTR["dim"] if dim else (
                ATTR["untracked"] if e.untracked else
                (ATTR["add"] if e.x not in " ?" else (ATTR["del"] if e.y not in " ?" else 0)))
            if ri == self.cursor:
                self.put(y0 + i, x0, text[:w].ljust(w), ATTR["cursor"])
                continue
            self.put(y0 + i, x0, text[:w], attr)
            if self.rx is not None and e.match_lines:
                self.put(y0 + i, x0 + min(len(text) + 1, max(0, w - 4)),
                         str(len(e.match_lines)), ATTR["hdr"])

    def draw_results(self, y0: int, x0: int, w: int, h: int) -> None:
        for i in range(h):
            gi = self.scroll + i
            if gi >= len(self.res):
                break
            kind, text, _e = self.res[gi]
            if kind == "blank" or not text:
                continue
            if kind == "hdr":
                self.put(y0 + i, x0, text[:w], ATTR["hdr"])
                continue
            base = diff_attr(text)
            spans = match_spans(self.rx, text) if self.rx else []
            if not spans:
                self.put(y0 + i, x0, text[:w], base)
                continue
            pos, cx = 0, x0
            for s, e in spans:
                s, e = max(0, s), min(len(text), e)
                if s < pos:
                    continue
                self.put(y0 + i, cx, text[pos:s][: max(0, x0 + w - cx)], base)
                cx += s - pos
                self.put(y0 + i, cx, text[s:e][: max(0, x0 + w - cx)], ATTR["match"])
                cx += e - s
                pos = e
            self.put(y0 + i, cx, text[pos:][: max(0, x0 + w - cx)], base)

    def kn(self, name: str) -> str:
        return str(self.cfg["keys"].get(name, ""))

    def draw_status(self, y: int, x: int, w: int) -> None:
        if self.mode == self.MODE_SEARCH:
            left = (f"{self.kn('jump')} jump  {self.kn('to_tree')} tree  "
                    f"PgUp/PgDn scroll  {self.kn('clear_query')} clear  ^C quit")
        else:
            left = (f"{self.kn('down')}/{self.kn('up')} move  {self.kn('scope')} scope  "
                    f"{self.kn('expand')}/{self.kn('collapse')} fold  {self.kn('scope_up')} up  "
                    f"{self.kn('scroll_down')}/{self.kn('scroll_up')} scroll  "
                    f"{self.kn('focus_search')} search  {self.kn('quit')} quit")
        lo = self.scroll + 1
        hi = min(self.scroll + self.pane_h, len(self.res))
        right = f"{self.mode.upper()}  {lo}-{hi}/{len(self.res)}"
        self.put(y, 1, left[: max(1, w - len(right) - 3)], ATTR["dim"])
        self.put(y, max(1, w - 1 - len(right)), right, ATTR["dim"])

    # ---- input

    def run(self) -> int:
        errs = 0
        while True:
            self.draw()
            try:
                v = self.std.get_wch()
                errs = 0
            except curses.error:
                errs += 1          # stdin closed / EOF: leave cleanly
                if errs > 3:
                    return 0
                continue
            lbl = key_label(v)
            if lbl == "RESIZE":
                continue
            if lbl == "C-c":
                return 0
            if lbl in ("PGUP", "PGDN"):
                self.scroll_by((-1 if lbl == "PGUP" else 1) * max(1, self.pane_h // 2))
                continue
            rc = self.dispatch_search(lbl, v) if self.mode == self.MODE_SEARCH \
                else self.dispatch_tree(lbl)
            if rc is not None:
                return rc

    def dispatch_search(self, lbl: str, v) -> int | None:
        k = self.cfg["keys"]
        if lbl == k["to_tree"]:
            self.mode = self.MODE_TREE
        elif lbl == k["jump"]:
            self.jump_first_match()
        elif lbl == k["clear_query"]:
            self.query, self.qpos = "", 0
            self.on_query_edit()
        elif lbl == "BACKSPACE":
            if self.qpos > 0:
                self.query = self.query[: self.qpos - 1] + self.query[self.qpos:]
                self.qpos -= 1
                self.on_query_edit()
        elif lbl == "LEFT":
            self.qpos = max(0, self.qpos - 1)
        elif lbl == "RIGHT":
            self.qpos = min(len(self.query), self.qpos + 1)
        elif lbl == "HOME":
            self.qpos = 0
        elif lbl == "END":
            self.qpos = len(self.query)
        elif isinstance(v, str) and len(v) == 1 and ord(v) >= 32:
            self.query = self.query[: self.qpos] + v + self.query[self.qpos:]
            self.qpos += 1
            self.on_query_edit()
        return None

    def dispatch_tree(self, lbl: str) -> int | None:
        k = self.cfg["keys"]
        if lbl in (k["up"], "UP"):
            self.move(-1)
        elif lbl in (k["down"], "DOWN"):
            self.move(1)
        elif lbl == k["scroll_up"]:
            self.scroll_by(-max(1, self.pane_h // 2))
        elif lbl == k["scroll_down"]:
            self.scroll_by(max(1, self.pane_h // 2))
        elif lbl == k["scope"]:
            n = self.rows[self.cursor][0]
            if n is UP:
                self.scope_up()
            else:
                self.apply_scope(n)
                self.mode = self.MODE_SEARCH
                self.qpos = len(self.query)
        elif lbl == k["expand"]:
            n = self.rows[self.cursor][0]
            if n is not UP and n.is_dir and not n.expanded:
                n.expanded = True
                self.rebuild_rows(keep=n)
        elif lbl == k["collapse"]:
            n = self.rows[self.cursor][0]
            if n is not UP and n.is_dir and n.expanded:
                n.expanded = False
                self.rebuild_rows(keep=n)
            else:
                self.scope_up()
        elif lbl == k["scope_up"]:
            self.scope_up()
        elif lbl == k["focus_search"]:
            self.mode = self.MODE_SEARCH
            self.qpos = len(self.query)
        elif lbl == k["quit"]:
            return 0
        elif lbl == "ESC":
            if self.scope is not self.root:
                self.scope_up()
            else:
                return 0
        return None


def run_empty(std) -> int:
    std.erase()
    msg1, msg2 = "No uncommitted changes", "(any key to close)"
    H, W = std.getmaxyx()
    try:
        std.addstr(H // 2 - 1, max(0, (W - len(msg1)) // 2), msg1, curses.A_BOLD)
        std.addstr(H // 2, max(0, (W - len(msg2)) // 2), msg2, curses.A_DIM)
        std.refresh()
        std.get_wch()
    except curses.error:
        pass
    return 0


# ----------------------------------------------------------- print mode -----

ANSI = {
    "reset": "\x1b[0m", "green": "\x1b[32m", "red": "\x1b[31m", "cyan": "\x1b[36m",
    "yellow": "\x1b[1;33m", "blue": "\x1b[34m", "dim": "\x1b[2m",
    "match": "\x1b[7;1m", "bold": "\x1b[1m",
}


def ansi_for(line: str) -> str:
    if line.startswith(("diff --git", "index ", "old mode", "new mode", "new file mode",
                        "deleted file mode", "similarity index", "rename from", "rename to",
                        "+++", "---")):
        return ANSI["blue"]
    if line.startswith("@@"):
        return ANSI["cyan"]
    if line.startswith("+"):
        return ANSI["green"]
    if line.startswith("-"):
        return ANSI["red"]
    if line.startswith("\\"):
        return ANSI["dim"]
    return ""


def cmd_print(args, cfg) -> int:
    _root, entries = collect_for(cfg)
    if not entries:
        print("(no uncommitted changes)", file=sys.stderr)
        return 1
    rx, err = compile_query(args.print, cfg)
    if err:
        print(f"diffsearch: bad regex: {err}", file=sys.stderr)
        return 2
    if args.scope:
        p = args.scope.strip().strip("/")
        entries = [e for e in entries if e.path == p or e.path.startswith(p + "/")]
        if not entries:
            print(f"diffsearch: no changes under {p}", file=sys.stderr)
            return 1
    color = (args.color == "always") or (args.color == "auto" and sys.stdout.isatty()
                                         and not args.no_color)
    shown = 0
    for e in entries:
        ml = None if rx is None else [i for i, l in enumerate(e.diff_lines) if rx.search(l)]
        if rx is not None and not ml:
            continue
        shown += 1
        hdr = f"── {e.path}  ·  {e.x}{e.y}"
        if rx is not None:
            hdr += f"  ·  {len(ml)} match lines"
        print((ANSI["yellow"] + hdr + ANSI["reset"]) if color else hdr)
        for i, l in enumerate(e.diff_lines):
            if not color:
                print(l)
                continue
            base = ansi_for(l)
            spans = match_spans(rx, l)
            if not spans:
                print(base + l + ANSI["reset"] if base else l)
                continue
            parts, pos = [], 0
            for s, ed in spans:
                parts.append(base + l[pos:s] + ANSI["reset"])
                parts.append(ANSI["match"] + l[s:ed] + ANSI["reset"])
                pos = ed
            parts.append(base + l[pos:] + ANSI["reset"] if l[pos:] else "")
            print("".join(parts))
        print()
    return 0 if (shown or rx is None) else 1


def collect_for(cfg: dict) -> tuple[str, list[Entry]]:
    set_context_lines(cfg)
    return collect()


# ------------------------------------------------------------------ main ----

def main() -> None:
    locale.setlocale(locale.LC_ALL, "")
    p = argparse.ArgumentParser(
        prog="diffsearch",
        description="Search uncommitted git changes (staged, unstaged, untracked) for a regex.")
    p.add_argument("--print", metavar="QUERY",
                   help="non-interactive: print matching diffs and exit (empty = all)")
    p.add_argument("--scope", metavar="PATH", help="start scoped to this directory/file")
    p.add_argument("--select", metavar="PATH", help="preselect this file in the tree")
    p.add_argument("--config", metavar="FILE", help=f"config file (default: {SCRIPT_DIR}/diffsearch.toml)")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = p.parse_args()
    cfg = load_config(args.config)

    if args.print is not None:
        sys.exit(cmd_print(args, cfg))

    os.environ.setdefault("ESCDELAY", "25")
    set_context_lines(cfg)
    _root, entries = collect()
    if not entries:
        try:
            curses.wrapper(run_empty)
        except curses.error:
            print("No uncommitted changes.")
        return

    scope_node = None
    if args.scope:
        probe = build_tree(entries)
        n = find_node(probe, args.scope)
        if n is None:
            print(f"diffsearch: warning: --scope {args.scope} not found", file=sys.stderr)
        elif n.is_dir:
            scope_node = n.path
        else:
            scope_node = (n.parent.path or "")
            args.select = args.select or n.path

    def runner(std):
        curses.curs_set(0)
        init_attrs()
        app = App(std, cfg, entries, args)
        if scope_node is not None:
            n = find_node(app.root, scope_node)
            if n is not None and n.is_dir and n is not app.root:
                app.apply_scope(n)
        if args.select:
            app.reveal(args.select)
        return app.run()

    try:
        rc = curses.wrapper(runner)
    except curses.error:
        print("diffsearch: needs an interactive terminal", file=sys.stderr)
        rc = 2
    sys.exit(rc)


if __name__ == "__main__":
    main()
