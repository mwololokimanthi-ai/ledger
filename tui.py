#!/usr/bin/env python3
"""ledger tui — full-screen session console.

The third surface, after the CLI and the browser: one screen, many sessions,
keys at the bottom (k9s grammar), status icons and peek-before-open (Linear, sesh),
expand-in-place (GitHub Actions), dense rows (Portainer), labelled footer (lazygit).

Layout, top to bottom:
  header    counts, clock, refresh interval
  tabs      all · live · needs you · ended · runs
  columns   state · project · intent · target · last
  list      every session in the tab, sorted by "needs a human" then age
  preview   the selected session's identity, owner and restore command
  footer    keys, plus the filter prompt or a status message

Keys
  j/k, ↑/↓       move            g/G          first / last
  ctrl-d/u, pgdn/pgup            half page
  enter          expand the row  d            full detail
  o              open (attach, or restore an ended session)
  r              restore summary for this row
  p              pin / unpin     y            copy restore command
  s              snapshot        R            refresh now
  /              filter          esc          clear filter / close overlay
  1..5           tabs            ?            help
  q, ctrl-c      quit

Nothing here writes a transcript. The only writes are pins.json, launched.json and
state.json, exactly as the CLI does.
"""
from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as L  # noqa: E402

ESC = "\x1b"
RESET = ESC + "[0m"


# ---------------------------------------------------------------- text cells

def wcwidth(ch: str) -> int:
    if ch == "\t":
        return 1
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def dwidth(s: str) -> int:
    return sum(wcwidth(c) for c in s)


def trunc(s: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if dwidth(s) <= width:
        return s
    out, used = [], 0
    limit = width - dwidth(ellipsis)
    for ch in s:
        w = wcwidth(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def wrap_words(s: str, width: int, max_lines: int = 2) -> list[str]:
    out: list[str] = []
    while s and len(out) < max_lines:
        if dwidth(s) <= width:
            out.append(s)
            return out
        cut, used = 0, 0
        for i, ch in enumerate(s):
            w = wcwidth(ch)
            if used + w > width:
                break
            used += w
            cut = i + 1
        head = s[:cut]
        brk = head.rfind(" ")
        if brk > width // 3:
            head, rest = s[:brk], s[brk + 1:]
        else:
            rest = s[cut:]
        out.append(head)
        s = rest
    if s:
        out[-1] = trunc(out[-1] + " " + s, width)
    return out


def pad(s: str, width: int) -> str:
    s = trunc(s, width)
    return s + " " * max(0, width - dwidth(s))


class Screen:
    """A grid of cells with a style per cell; only cells that changed are written."""

    def __init__(self, w: int, h: int, color: bool = True):
        self.w, self.h, self.color = w, h, color
        self.ch = [[" "] * w for _ in range(h)]
        self.st = [[""] * w for _ in range(h)]
        self.cont = [[False] * w for _ in range(h)]
        self.dirty = True

    def clear(self) -> None:
        for y in range(self.h):
            for x in range(self.w):
                self.ch[y][x] = " "
                self.st[y][x] = ""
                self.cont[y][x] = False

    def put(self, x: int, y: int, s: str, style: str = "") -> None:
        if y < 0 or y >= self.h:
            return
        for ch in s:
            w = wcwidth(ch)
            if w == 0:
                continue
            if x + w > self.w:
                break
            self.ch[y][x] = ch
            self.st[y][x] = style
            if w == 2:
                self.cont[y][x] = False
                if x + 1 < self.w:
                    self.cont[y][x + 1] = True
                    self.st[y][x + 1] = style
            x += w

    def fill(self, x: int, y: int, w: int, style: str = "") -> None:
        self.put(x, y, " " * w, style)

    def render(self) -> str:
        out = []
        for y in range(self.h):
            line, cur = [], None
            for x in range(self.w):
                if self.cont[y][x]:
                    continue
                st = self.st[y][x] if self.color else ""
                if st != cur:
                    line.append(RESET if cur else "")
                    if st:
                        line.append(st)
                    cur = st
                line.append(self.ch[y][x])
            line.append(RESET)
            out.append("".join(line))
        return "\r\n".join(out)


def plain(rendered: str) -> str:
    import re
    return re.sub(ESC + r"\[[0-9;]*m", "", rendered)


# ---------------------------------------------------------------- palette

def palette() -> dict[str, str]:
    return {
        "reset": RESET,
        "dim": ESC + "[38;5;246m",
        "dim_sel": ESC + "[38;5;248m",
        "faint": ESC + "[38;5;245m",
        "faint_sel": ESC + "[38;5;246m",
        "text": ESC + "[38;5;252m",
        "bright": ESC + "[38;5;255m",
        "accent": ESC + "[38;5;75m",
        "rule": ESC + "[38;5;240m",
        "sel": ESC + "[48;5;236m",
        "tab_on": ESC + "[48;5;238m",
        "hdr": ESC + "[38;5;245m",
        "working": ESC + "[38;5;78m",
        "awaiting": ESC + "[38;5;178m",
        "error": ESC + "[38;5;210m",
        "idle": ESC + "[38;5;246m",
        "ended": ESC + "[38;5;246m",
        "started": ESC + "[38;5;141m",
        "unowned": ESC + "[38;5;75m",
        "ok": ESC + "[38;5;78m",
        "warn": ESC + "[38;5;178m",
        "bold": ESC + "[1m",
    }


ICON = {
    "working": "◐", "quiet-working": "◑", "awaiting": "●", "quiet-awaiting": "◍",
    "queued": "◌", "error": "✕", "idle": "·", "ended": "○", "started": "◌",
    "recent-unowned": "◎", "unknown": "?",
}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

STATE_ORDER = L.STATE_ORDER


# ---------------------------------------------------------------- keys

def parse_keys(buf: str) -> list[str]:
    keys, i = [], 0
    while i < len(buf):
        c = buf[i]
        if c == ESC:
            if buf.startswith(ESC + "[A", i):
                keys.append("up"); i += 3
            elif buf.startswith(ESC + "[B", i):
                keys.append("down"); i += 3
            elif buf.startswith(ESC + "[C", i):
                keys.append("right"); i += 3
            elif buf.startswith(ESC + "[D", i):
                keys.append("left"); i += 3
            elif buf.startswith(ESC + "[H", i) or buf.startswith(ESC + "[1~", i):
                keys.append("home"); i += 3
            elif buf.startswith(ESC + "[F", i) or buf.startswith(ESC + "[4~", i):
                keys.append("end"); i += 3
            elif buf.startswith(ESC + "[5~", i):
                keys.append("pgup"); i += 4
            elif buf.startswith(ESC + "[6~", i):
                keys.append("pgdn"); i += 4
            elif buf.startswith(ESC + "[<", i):
                end = buf.find("M", i)
                if end < 0:
                    end = buf.find("m", i)
                if end < 0:
                    keys.append("esc"); i += 1
                else:
                    body = buf[i + 3:end]
                    try:
                        b, x, y = (int(v) for v in body.split(";"))
                        keys.append(f"mouse:{b}:{x}:{y}")
                    except ValueError:
                        keys.append("esc")
                    i = end + 1
            else:
                keys.append("esc"); i += 1
        elif c in ("\r", "\n"):
            keys.append("enter"); i += 1
        elif c == "\t":
            keys.append("tab"); i += 1
        elif c in ("\x7f", "\b"):
            keys.append("backspace"); i += 1
        elif c == "\x03":
            keys.append("ctrl-c"); i += 1
        elif c == "\x04":
            keys.append("ctrl-d"); i += 1
        elif c == "\x15":
            keys.append("ctrl-u"); i += 1
        else:
            keys.append(c); i += 1
    return keys


# ---------------------------------------------------------------- terminal

class Terminal:
    def __init__(self, color: bool):
        self.fd = sys.stdin.fileno()
        self.out = sys.stdout
        self.color = color
        self.saved = None

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.out.write(ESC + "[?1049h")          # alternate screen
        self.out.write(ESC + "[?25l")            # hide cursor
        self.out.write(ESC + "[?1000h" + ESC + "[?1006h")  # mouse: click + SGR
        self.out.write(ESC + "[2J")
        self.out.flush()
        return self

    def __exit__(self, *exc):
        try:
            self.out.write(ESC + "[?1000l" + ESC + "[?1006l")
            self.out.write(ESC + "[?25h")
            self.out.write(ESC + "[?1049l")
            self.out.flush()
        finally:
            if self.saved is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((100, 30))
        return max(40, s.columns), max(12, s.lines)

    def draw(self, screen: Screen) -> None:
        self.out.write(ESC + "[H" + screen.render() + ESC + "[J")
        self.out.flush()

    def read(self, timeout: float) -> str:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return ""
        try:
            return os.read(self.fd, 4096).decode("utf-8", "replace")
        except OSError:
            return ""


# ---------------------------------------------------------------- app

TABS = [
    ("all", "all", lambda r: r["kind"] != "subagent"),
    ("live", "live", lambda r: r["live"] and r["kind"] != "subagent"),
    ("needs", "needs you", lambda r: r["kind"] != "subagent" and r["state"] in
     ("awaiting", "quiet-awaiting", "error")),
    ("ended", "ended", lambda r: not r["live"] and r["kind"] != "subagent"),
    ("runs", "runs", lambda r: r["kind"] == "subagent"),
]

HELP: list[tuple[str, str] | None] = [
    ("j / k  ↑ / ↓", "move the selection"),
    ("g / G", "first / last row"),
    ("ctrl-d / ctrl-u", "half page down / up"),
    None,
    ("enter", "expand the row in place"),
    ("d", "full detail overlay"),
    ("tab", "toggle the preview pane"),
    None,
    ("o", "open: switch to its pane, or restore an ended session"),
    ("p", "pin / unpin (pinned sessions survive restore)"),
    ("y", "copy the restore command"),
    ("s", "write a snapshot (state.json)"),
    ("R", "refresh now"),
    None,
    ("/", "filter by intent, project, id or target"),
    ("t", "cycle sort: state · age · project"),
    ("1 2 3 4 5", "all · live · needs you · ended · runs"),
    ("? / esc", "help / close overlay"),
    ("q", "quit"),
]


class App:
    def __init__(self, interval: float = 5.0, window_h: float | None = L.DEFAULT_WINDOW_H):
        self.interval = interval
        self.window_h = window_h
        self.state = L.build_state(window_h=window_h)
        self.at = time.time()
        self.tab = "all"
        self.filter = ""
        self.filtering = False
        self.selected: str | None = None      # session key
        self.offset = 0
        self.expanded: set[str] = set()
        self.overlay: str | None = None
        self.preview = True
        self.sort = "state"
        self.view: tuple[int, int, int] | None = None
        self.status = ""
        self.status_until = 0.0
        self.running = True
        self.tick = 0
        self.pal = palette()

    # -- data -------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.state = L.build_state(window_h=self.window_h)
            self.at = time.time()
        except Exception as exc:                       # never die on a bad scan
            self.say(f"refresh failed: {exc}", warn=True)

    def say(self, msg: str, warn: bool = False) -> None:
        self.status = ("! " if warn else "") + msg
        self.status_until = time.time() + 4

    def key_of(self, r: dict) -> str:
        return r["id"] or "pid:" + (r["pid"] or "?")

    def rows(self) -> list[dict]:
        test = next(t for k, _, t in TABS if k == self.tab)
        f = self.filter.strip().lower()
        rows = [r for r in self.state["sessions"] if test(r)]
        if f:
            rows = [r for r in rows
                    if f in (r["intent"] + " " + r["project"] + " " + r["id"] + " "
                             + r["tmux"]).lower()]
        if self.sort == "age":
            return sorted(rows, key=lambda r: -r["mtime_epoch"])
        if self.sort == "project":
            return sorted(rows, key=lambda r: (r["project"].lower(), L.sort_key(r)))
        return sorted(rows, key=L.sort_key)

    def current(self) -> dict | None:
        rows = self.rows()
        if not rows:
            return None
        for r in rows:
            if self.key_of(r) == self.selected:
                return r
        self.selected = self.key_of(rows[0])
        return rows[0]

    # -- chrome -----------------------------------------------------
    def header(self, sc: Screen, p: dict) -> None:
        s = self.state
        live = sum(1 for r in s["sessions"] if r["live"] and r["kind"] != "subagent")
        wait = sum(1 for r in s["sessions"] if r["kind"] != "subagent"
                   and r["state"] in ("awaiting", "quiet-awaiting", "error"))
        ended = sum(1 for r in s["sessions"] if not r["live"] and r["kind"] != "subagent")
        inferred = s.get("inferred_bindings", 0)
        unbound = s.get("unbound_processes", 0)

        sc.put(1, 0, "ledger", p["bold"] + p["bright"])
        if sc.w >= 62:
            sc.put(8, 0, "pi sessions", p["faint"])

        clock = time.strftime("%H:%M:%S")
        clock_x = max(1, sc.w - dwidth(clock) - 1)
        sc.put(clock_x, 0, clock, p["faint"])
        span = ""
        if self.view:
            a, b, n = self.view
            span = f"{a}\u2013{b} of {n}"
            sx = clock_x - dwidth(span) - 3
            if sx > 24:
                sc.put(sx, 0, span, p["faint"])
            else:
                span = ""

        bits = [(f"{live}", "live", p["working"]), (f"{wait}", "waiting", p["awaiting"]),
                (f"{ended}", "ended", p["dim"])]
        if inferred:
            bits.append((f"{inferred}", "inferred", p["dim"]))
        if unbound:
            bits.append((f"{unbound}", "unbound", p["dim"]))

        budget = clock_x - (dwidth(span) + 3 if span else 0) - 2
        x = 22 if sc.w >= 62 else 9
        sep = " · "
        for i, (n, label, style) in enumerate(bits):
            chunk = ("" if i == 0 else sep) + n + " " + label
            if x + dwidth(chunk) > budget:
                break
            if i:
                sc.put(x, 0, sep, p["faint"])
                x += dwidth(sep)
            sc.put(x, 0, n, p["bright"])
            x += dwidth(n) + 1
            sc.put(x, 0, label, style)
            x += dwidth(label)

        if sc.w >= 120 and self.sort != "state":
            tag = f"sort {self.sort}"
            sc.put(max(1, clock_x - dwidth(tag) - 2), 0, tag, p["faint"])

    def tabs(self, sc: Screen, p: dict) -> None:
        x = 1
        counts = {k: sum(1 for r in self.state["sessions"] if t(r)) for k, _, t in TABS}
        for key, label, _ in TABS:
            active = key == self.tab
            seg = f" {label}"
            num = f" {counts[key]} "
            style = (p["tab_on"] + p["bright"] + p["bold"]) if active else p["faint"]
            sc.put(x, 1, seg, style)
            sc.put(x + dwidth(seg), 1, num, (p["tab_on"] + p["accent"]) if active else p["faint"])
            x += dwidth(seg) + dwidth(num)
        if self.filter and not self.filtering:
            n = len(self.rows())
            prompt = f"/ {self.filter}"
            tail = f"  {n} match" + ("" if n == 1 else "es")
            prompt_x = max(1, sc.w - dwidth(prompt + tail) - 2)
            sc.put(prompt_x, 1, prompt, p["accent"] if self.filtering else p["dim"])
            sc.put(prompt_x + dwidth(prompt), 1, tail, p["faint"])
            if self.filtering:
                sc.put(prompt_x + dwidth(prompt), 1, "▏", p["accent"])

    def columns(self, sc: Screen, p: dict, cols: list[tuple[str, int, str]]) -> None:
        x = 1
        for name, width, _ in cols:
            label = name.upper() if name != "idx" else "#"
            if name in ("last", "idx"):
                sc.put(x + width - dwidth(label) - 1, 2, label, p["faint"])
            else:
                sc.put(x, 2, label, p["faint"])
            x += width
        for xx in range(1, sc.w - 1):
            if sc.ch[2][xx] == " ":
                continue
            sc.st[2][xx] = p["faint"]

    def row_line(self, sc: Screen, p: dict, y: int, r: dict, selected: bool, cols,
                 num: int = 0) -> None:
        base = p["sel"] if selected else ""
        if selected:
            sc.fill(0, y, sc.w, p["sel"])
        st = r["state"]
        color = p["working"] if st.startswith("working") or st.startswith("quiet-working") \
            else p["awaiting"] if st in ("awaiting", "quiet-awaiting", "queued") \
            else p["error"] if st == "error" \
            else p["started"] if st == "started" \
            else p["unowned"] if st == "recent-unowned" \
            else p["ended"] if st == "ended" else p["idle"]
        sc.put(0, y, "▌" if selected else " ", p["accent"] if selected else "")
        x = 1
        for name, width, _ in cols:
            if name == "idx":
                s_num = f"{num:>3d}"
                sc.put(x + width - dwidth(s_num) - 1, y, s_num,
                       (p["accent"] if selected else p["faint"]) + base)
            elif name == "state":
                icon = SPIN[self.tick % len(SPIN)] if st in ("working", "quiet-working") \
                    else ICON.get(st, "?")
                sc.put(x, y, icon, color + base)
                sc.put(x + 2, y, trunc(st, width - 3), color + base)
            elif name == "project":
                sc.put(x, y, trunc(r["project"], width - 1),
                       (p["dim_sel"] if selected else p["dim"]) + base)
            elif name == "intent":
                fg = p["bright"] if selected else (
                    p["text"] if r["live"] else
                    ESC + "[38;5;246m" if st == "ended" else ESC + "[38;5;250m")
                sc.put(x, y, trunc(r["intent"], width - 2), fg + base)
            elif name == "target":
                tgt = r["tmux"] if r["tmux"] != "-" else "—"
                fg = (p["dim_sel"] if selected else p["dim"]) if r["tmux"] != "-" \
                    else (p["faint_sel"] if selected else p["faint"])
                sc.put(x, y, trunc(tgt, width - 2), fg + base)
            elif name == "last":
                age = L.human_age(r["idle_s"])
                sc.put(x + width - dwidth(age) - 1, y, age,
                       (p["faint_sel"] if selected else p["faint"]) + base)
            x += width

    def draw_preview(self, sc: Screen, p: dict, r: dict | None, y0: int, height: int) -> None:
        for x in range(sc.w):
            sc.put(x, y0, "─", p["rule"])
        if not r:
            return
        st = r["state"]
        color = p["working"] if "working" in st else p["awaiting"] if "awaiting" in st \
            else p["error"] if st == "error" else p["dim"]
        y = y0 + 1
        icon = ICON.get(st, "?")
        sc.put(2, y, icon, color + p["bold"])
        sc.put(4, y, st, color + p["bold"])
        x = 4 + dwidth(st) + 2
        sc.put(x, y, r["project"], p["bright"])
        x += dwidth(r["project"]) + 2
        sc.put(x, y, f"· {L.human_age(r['idle_s'])} ago", p["faint"])
        rid = r["id"][:8] if r["id"] else f"pid {r['pid']}"
        sc.put(max(2, sc.w - dwidth(rid) - 2), y, rid, p["faint"])

        if height < 3:
            return
        intent = " ".join((r["intent"] or "").split())
        sc.put(4, y + 1, trunc(intent, sc.w - 7), p["text"])

        if height < 5:
            return
        candidates = [
            ("cwd", r["home_rel"]),
            ("tmux", (f"{r['tmux']} · {r['pane_size']}" if r["tmux"] != "-" else "(not in tmux)")),
            ("owner", (f"pid {r['pid']} · {r['binding']}" if r["live"]
                       else f"{r['binding']} · not running")),
            ("restore", r["restore"] or "—"),
        ]
        avail = max(0, height - 3)
        keep = set(["tmux", "owner", "restore", "cwd"][:avail])
        rows = [c for c in candidates if c[0] in keep]
        yy = y + 2
        for label, value in rows:
            if yy >= y0 + height:
                break
            sc.put(2, yy, label, p["faint"])
            sc.put(10, yy, trunc(value, sc.w - 12),
                   p["accent"] if label == "restore" else p["dim"])
            yy += 1

    def footer(self, sc: Screen, p: dict) -> None:
        y = sc.h - 1
        if self.filtering:
            left = "/ "
            sc.put(1, y, left, p["accent"])
            sc.put(1 + dwidth(left), y, self.filter, p["bright"])
            sc.put(1 + dwidth(left) + dwidth(self.filter), y, "▏", p["accent"])
            hint = "enter apply · esc clear"
            sc.put(max(0, sc.w - dwidth(hint) - 1), y, hint, p["faint"])
            return
        if self.status and time.time() < self.status_until:
            msg = self.status
            sc.put(max(0, sc.w - dwidth(msg) - 2), y, msg,
                   p["warn"] if msg.startswith("!") else p["ok"])
            return
        cur = self.current()
        action = ""
        if cur and sc.w >= 100:
            action = ("o switch to " + cur["tmux"]) if cur["tmux"] != "-" \
                else ("o restore" if cur["id"] else "")
        keys = ("j/k move · enter expand · d detail · o open · p pin · y copy · / filter · ? help"
                if sc.w >= 110 else
                "j/k · enter · o open · p pin · / filter · ? help")
        room = sc.w - (dwidth(action) + 4 if action else 2)
        sc.put(1, y, trunc(keys, max(8, room)), p["faint"])
        if action:
            sc.put(max(1, sc.w - dwidth(action) - 1), y, action, p["dim"])

    def draw_empty(self, sc: Screen, p: dict, top: int, height: int) -> None:
        f = self.filter.strip()
        if f:
            title, hint = f'no session matches "{f}"', "esc clears the filter"
        elif self.tab == "runs":
            title, hint = "no subagent runs in this window", "1-5 switch tabs"
        elif self.tab == "needs":
            title, hint = "nothing needs you", "live sessions appear here when they stop mid-turn"
        elif self.tab == "ended":
            title, hint = "no ended sessions in this window", "ledger tui --all indexes everything"
        else:
            title, hint = "no sessions in this window", "ledger tui --all indexes everything"
        y = top + 2
        sc.put(3, y, title, p["dim"])
        sc.put(3, y + 1, hint, p["faint"])

    # -- overlays ---------------------------------------------------
    def box(self, sc: Screen, p: dict, title: str, height: int) -> tuple[int, int, int, int]:
        w = min(sc.w - 4, 100)
        h = min(sc.h - 2, height)
        x0, y0 = (sc.w - w) // 2, (sc.h - h) // 2
        sc.put(x0, y0, "┌", p["faint"])
        sc.put(x0 + w - 1, y0, "┐", p["faint"])
        sc.put(x0, y0 + h - 1, "└", p["faint"])
        sc.put(x0 + w - 1, y0 + h - 1, "┘", p["faint"])
        for x in range(x0 + 1, x0 + w - 1):
            sc.put(x, y0, "─", p["faint"])
            sc.put(x, y0 + h - 1, "─", p["faint"])
        for y in range(y0 + 1, y0 + h - 1):
            sc.put(x0, y, "│", p["faint"])
            sc.put(x0 + w - 1, y, "│", p["faint"])
            for x in range(x0 + 1, x0 + w - 1):
                sc.put(x, y, " ", p["sel"])
        label = f" {title} "
        sc.put(x0 + 2, y0, label, p["bright"] + p["bold"] + p["sel"])
        return x0, y0, w, h

    def draw_help(self, sc: Screen, p: dict) -> None:
        x0, y0, w, h = self.box(sc, p, "keys", len(HELP) + 3)
        y = y0 + 1
        for item in HELP:
            if y >= y0 + h - 1:
                break
            if item is None:
                y += 1
                continue
            keys, desc = item
            sc.put(x0 + 2, y, pad(keys, 20), p["bright"] + p["sel"])
            sc.put(x0 + 24, y, trunc(desc, w - 27), p["text"] + p["sel"])
            y += 1

    def draw_detail(self, sc: Screen, p: dict, r: dict) -> None:
        inner = min(sc.w - 4, 100) - 15
        intent = " ".join((r["intent"] or "").split())
        intent_lines = wrap_words(intent, inner, 2)
        fields = [
            ("state", [f"{r['state']} · {L.human_age(r['idle_s'])} ago · {r['last_activity']}"]),
            ("project", [f"{r['project']}  ({r['home_rel']})"]),
            ("intent", intent_lines),
            ("started", [r["started"]]),
            ("tmux", [(f"{r['tmux']}  {r['pane_size']}  [{r.get('pane_cmd', '')}]"
                       if r["tmux"] != "-" else "(not in tmux)")]),
            ("owner", [f"pid {r['pid']} · binding {r['binding']}" if r["live"]
                       else f"binding {r['binding']} · not running"]),
            ("store", [f"{r['store']} · {r['size'] // 1024} KiB"
                       + (f" · {r['copies']} copies" if r.get("copies", 1) > 1 else "")]),
            ("file", [r["path"] or "—"]),
            ("restore", [r["restore"] or "—"]),
            ("pinned", ["yes" if r["pinned"] else "no"]),
        ]
        need = sum(len(lines) for _, lines in fields) + 3
        x0, y0, w, h = self.box(sc, p, f"session {r['id'][:8] or '(no transcript)'}", need)
        y = y0 + 1
        for label, lines in fields:
            if y >= y0 + h - 1:
                break
            sc.put(x0 + 2, y, label, p["faint"] + p["sel"])
            first = True
            for line in lines:
                if y >= y0 + h - 1:
                    break
                sc.put(x0 + 12, y, trunc(line, w - 15),
                       (p["accent"] if label == "restore" else p["text"]) + p["sel"])
                y += 1
                first = False
            if not first and not lines:
                y += 1
        hint = " esc close "
        sc.put(x0 + w - dwidth(hint) - 1, y0 + h - 1, hint, p["faint"])

    # -- actions ----------------------------------------------------
    def open_row(self, r: dict) -> None:
        if r["tmux"] != "-":
            sess = r["tmux_session"]
            if os.environ.get("TMUX"):
                rc = subprocess.call(["tmux", "switch-client", "-t", sess])
                if rc == 0:
                    self.say(f"switched to {sess}")
                else:
                    self.say(f"no client to switch; tmux attach -t {sess}", warn=True)
            else:
                self.exec_attach(sess)
            return
        if not r["id"]:
            self.say("no transcript and no pane — nothing to open", warn=True)
            return
        if r.get("path") and not os.path.exists(r["path"]):
            self.say("transcript is gone; refusing to open an empty session", warn=True)
            return
        name = self.slug(r["project"])
        if name in L.tmux_sessions():
            name = f"{name}-{r['short']}"
        cmd = ["tmux", "new-session", "-d", "-s", name, "-c", r["cwd"], r["restore"]]
        if subprocess.call(cmd) != 0:
            self.say("tmux refused the new session", warn=True)
            return
        L.record_launch(name, r)
        self.say(f"restored into {name}")
        self.refresh()
        if os.environ.get("TMUX"):
            if subprocess.call(["tmux", "switch-client", "-t", name]) != 0:
                self.say(f"created {name}; tmux attach -t {name}")
        else:
            self.exec_attach(name)

    def exec_attach(self, name: str) -> None:
        self.cleanup()
        os.execvp("tmux", ["tmux", "attach", "-t", name])

    def slug(self, name: str) -> str:
        import re
        s = re.sub(r"[^a-z0-9]+", "-", (name or "session").lower()).strip("-")
        return s[:24] or "session"

    def copy_row(self, r: dict) -> None:
        text = r.get("restore") or r.get("restore_id") or ""
        if not text:
            self.say("nothing to copy", warn=True)
            return
        for cmd in (["xclip", "-selection", "clipboard"], ["wl-copy"]):
            if shutil.which(cmd[0]):
                try:
                    p_ = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    p_.communicate(text.encode())
                    if p_.returncode == 0:
                        self.say("restore command copied")
                        return
                except Exception:
                    pass
        self.say("no clipboard tool (xclip/wl-copy)", warn=True)

    def toggle_pin(self, r: dict) -> None:
        if not r["id"]:
            self.say("a process with no transcript cannot be pinned", warn=True)
            return
        pins = L.load_pins()
        if r["id"] in pins:
            pins.discard(r["id"])
            self.say(f"unpinned {r['id'][:8]}")
        else:
            pins.add(r["id"])
            self.say(f"pinned {r['id'][:8]}")
        L.save_pins(pins)
        self.refresh()

    def snapshot(self) -> None:
        try:
            st = L.build_state(window_h=None)
            path = L.write_state(st)
            self.say(f"snapshot written ({len(st['sessions'])} sessions)")
        except Exception as exc:
            self.say(f"snapshot failed: {exc}", warn=True)

    # -- loop -------------------------------------------------------
    def cleanup(self) -> None:
        if self.term:
            self.term.__exit__(None, None, None)
            self.term = None

    def columns_for(self, w: int) -> list[tuple[str, int, str]]:
        """Priority when the window is narrow: state, intent, target, last; project
        is the first to go (the target and cwd already say where it is)."""
        cols: list[tuple[str, int, str]] = [("idx", 5, ""), ("state", 17, "")]
        show_project = w >= 118
        show_target = w >= 98
        show_last = w >= 78
        if show_project:
            cols.append(("project", 15, ""))
        fixed = sum(c[1] for c in cols) + (23 if show_target else 0) + (7 if show_last else 0)
        cols.append(("intent", max(16, w - fixed - 2), ""))
        if show_target:
            cols.append(("target", 25 if w >= 140 else 23, ""))
        if show_last:
            cols.append(("last", 7, ""))
        return cols

    def run(self) -> int:
        self.term = None
        with Terminal(color=sys.stdout.isatty()) as term:
            self.term = term
            next_refresh = time.time() + self.interval
            while self.running:
                w, h = term.size()
                sc = Screen(w, h, color=term.color)
                p = self.pal
                rows = self.rows()
                if self.selected is None and rows:
                    self.selected = self.key_of(rows[0])
                cur = self.current()
                idx = next((i for i, r in enumerate(rows) if self.key_of(r) == self.selected), 0)

                preview_h0 = 6 if (rows and self.preview and h >= 22 and h < 30) else \
                    (8 if (rows and self.preview and h >= 22) else 0)
                list_h0 = h - 3 - preview_h0 - 1
                self.view = ((self.offset + 1,
                              min(len(rows), self.offset + list_h0),
                              len(rows)) if len(rows) > list_h0 else None)
                self.header(sc, p)
                self.tabs(sc, p)
                if self.overlay:
                    if self.overlay == "help":
                        self.draw_help(sc, p)
                    elif self.overlay == "detail" and cur:
                        self.draw_detail(sc, p, cur)
                    self.footer(sc, p)
                    term.draw(sc)
                    raw = term.read(0.2)
                    keys = parse_keys(raw) if raw else []
                    for k in keys:
                        self.handle(k, rows, 1)
                        if not self.running:
                            break
                    if time.time() >= next_refresh:
                        self.refresh()
                        next_refresh = time.time() + self.interval
                    continue

                preview_h = 0
                if rows and self.preview and h >= 22:
                    preview_h = 6 if h < 30 else 8
                list_top = 3
                list_h = h - list_top - preview_h - 1
                if idx < self.offset:
                    self.offset = idx
                if idx >= self.offset + list_h:
                    self.offset = idx - list_h + 1

                cols = self.columns_for(w)
                self.columns(sc, p, cols)
                i = 0
                while i < list_h:
                    pos = self.offset + i
                    if pos >= len(rows):
                        break
                    r = rows[pos]
                    self.row_line(sc, p, list_top + i, r, pos == idx, cols, pos + 1)
                    i += 1
                    if self.key_of(r) in self.expanded and i + 1 < list_h:
                        self.draw_expanded(sc, p, list_top + i - 1, r)
                        i += 2
                if not rows:
                    self.draw_empty(sc, p, list_top, list_h)
                if preview_h:
                    self.draw_preview(sc, p, cur, h - preview_h - 1, preview_h)
                self.footer(sc, p)
                term.draw(sc)

                busy = any(r["state"] in ("working", "quiet-working") for r in rows)
                raw = term.read(0.2 if busy else 0.7)
                self.tick += 1
                keys = parse_keys(raw) if raw else []
                for k in keys:
                    self.handle(k, rows, list_h)
                    if not self.running:
                        break
                if time.time() >= next_refresh:
                    self.refresh()
                    next_refresh = time.time() + self.interval

        return 0

    def draw_expanded(self, sc: Screen, p: dict, y: int, r: dict) -> None:
        """Two lines under the selected row. Caller has already drawn the row."""
        lines = [
            f"id {r['id'] or '—'}   binding {r['binding']}   "
            f"tmux {r['tmux']}   {r['home_rel']}",
            f"restore  {r['restore'] or '—'}",
        ]
        for n, text in enumerate(lines):
            if y + 1 + n < sc.h - 1:
                sc.put(0, y + 1 + n, "▎", p["accent"])
                sc.put(2, y + 1 + n, trunc(text, sc.w - 4), p["faint"])

    def handle(self, k: str, rows: list[dict], list_h: int) -> None:
        cur = self.current()
        if k.startswith("mouse:"):
            _, b, mx, my = k.split(":")
            b, mx, my = int(b), int(mx), int(my)
            if b == 64:
                self.move(-3, rows)
            elif b == 65:
                self.move(3, rows)
            elif b == 0 and 3 <= my - 1 < 3 + list_h:
                pos = self.offset + (my - 1 - 3)
                if pos < len(rows):
                    self.selected = self.key_of(rows[pos])
            return
        if self.overlay:
            if k in ("esc", "q", "?", "enter", "d"):
                self.overlay = None
            return
        if self.filtering:
            if k == "enter":
                self.filtering = False
            elif k == "esc":
                self.filtering = False
                self.filter = ""
            elif k == "backspace":
                self.filter = self.filter[:-1]
            elif len(k) == 1 and k.isprintable():
                self.filter += k
            return
        if k in ("q", "ctrl-c"):
            self.running = False
        elif k in ("j", "down"):
            self.move(1, rows)
        elif k in ("k", "up"):
            self.move(-1, rows)
        elif k == "ctrl-d" or k == "pgdn":
            self.move(max(1, list_h // 2), rows)
        elif k == "ctrl-u" or k == "pgup":
            self.move(-max(1, list_h // 2), rows)
        elif k == "g":
            self.move(-10 ** 6, rows)
        elif k == "G":
            self.move(10 ** 6, rows)
        elif k == "enter" and cur:
            key = self.key_of(cur)
            self.expanded = set() if key in self.expanded else {key}
        elif k == "d" and cur:
            self.overlay = "detail"
        elif k == "?":
            self.overlay = "help"
        elif k == "/":
            self.filtering = True
        elif k == "esc":
            self.filter = ""
        elif k == "tab":
            self.preview = not self.preview
        elif k == "t":
            order = ["state", "age", "project"]
            self.sort = order[(order.index(self.sort) + 1) % len(order)]
            self.say(f"sorted by {self.sort}")
        elif k == "R":
            self.refresh()
            self.say("refreshed")
        elif k == "s":
            self.snapshot()
        elif k == "p" and cur:
            self.toggle_pin(cur)
        elif k == "y" and cur:
            self.copy_row(cur)
        elif k in ("o", "r") and cur:
            self.open_row(cur)
        elif k.isdigit() and 1 <= int(k) <= len(TABS):
            self.tab = TABS[int(k) - 1][0]
            self.offset = 0
            self.selected = None

    def move(self, delta: int, rows: list[dict]) -> None:
        if not rows:
            return
        cur = self.current()
        i = rows.index(cur) if cur in rows else 0
        i = max(0, min(len(rows) - 1, i + delta))
        self.selected = self.key_of(rows[i])
        self.expanded = {k for k in self.expanded if k == self.selected}


def dump(interval: float, window_h, color: bool = False) -> int:
    """Render one frame to stdout. Used by the selftest and for screenshots."""
    app = App(interval=interval, window_h=window_h)
    app.preview = True
    w, h = shutil.get_terminal_size((120, 34))
    if os.environ.get("LEDGER_TUI_SIZE"):
        try:
            w, h = (int(v) for v in os.environ["LEDGER_TUI_SIZE"].split("x"))
        except ValueError:
            pass
    sc = Screen(w, h, color=color)
    p = app.pal
    rows = app.rows()
    app.selected = app.key_of(rows[0]) if rows else None
    cur = app.current()
    preview_h = 8 if h >= 30 else 6
    list_h = h - 3 - preview_h - 1
    app.view = (1, min(len(rows), list_h), len(rows)) if len(rows) > list_h else None
    app.header(sc, p)
    app.tabs(sc, p)
    cols = app.columns_for(w)
    app.columns(sc, p, cols)
    for i, r in enumerate(rows[:list_h]):
        app.row_line(sc, p, 3 + i, r, i == 0, cols, i + 1)
    app.draw_preview(sc, p, cur, h - preview_h - 1, preview_h)
    app.footer(sc, p)
    sys.stdout.write((plain(sc.render()) if not color else sc.render()) + "\n")
    return 0


def main(argv: list[str]) -> int:
    interval = 5.0
    window_h: float | None = L.DEFAULT_WINDOW_H
    if "--interval" in argv:
        try:
            interval = max(0.5, float(argv[argv.index("--interval") + 1]))
        except (IndexError, ValueError):
            sys.exit("--interval needs a number")
    if "--all" in argv:
        window_h = None
    if "--dump" in argv:
        return dump(interval, window_h, color="--color" in argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("ledger tui needs a terminal; use `ledger table` for pipes")
    return App(interval=interval, window_h=window_h).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
