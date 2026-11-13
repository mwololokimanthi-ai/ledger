#!/usr/bin/env python3
"""ledger_core — one record per pi session, live or ended.

Why this exists: on this machine 16 tmux sessions and ~10 `pi` processes share a
handful of directories, and half the tmux sessions are named with integers. Neither
cwd nor tmux name identifies a session. The transcript does: its first line carries
`{"type":"session","id":...,"cwd":...,"timestamp":...}`, and `pi --session <id>`
reopens it anywhere. So the transcript is the unit of record; tmux panes and pids are
attachments that come and go.

Binding a live `pi` pid to a transcript is a heuristic and is labelled as such. pi
holds no fd on its session file and writes no registry row (checked 2026-11-13:
`pgrep -x pi` fds contain no .jsonl; `.pi-hermes-locks.sqlite` `locks` table is
empty). Worse, a process can switch files: pid 88824 started 13:01 with session
01a1d45b, then the app compacted and opened 01a1d512 in the same process at 16:22.
So "session start == process start" is evidence, not proof. Ladder:

  exact        same cwd, |session start - process start| <= 180s
  succession   an exact pair whose file went stale is rebound to a newer file the
               same process created later (compaction does exactly this)
  launched     ledger started this pane itself (`ledger open` / `restore`) and wrote
               the mapping to launched.json; pi rewrites its argv to bare "pi", so the
               command line cannot be used as evidence (measured 2026-11-13)
  created-after  unbound process; the only transcripts that can belong to it were
               created during its lifetime (a process cannot create a file before
               it started) — newest wins
  (none)       left unbound and reported as such

Session discovery is NOT reimplemented here: transcript roots, tail parsing, intent
extraction and the state classifier are imported from agentboard.py, which relay
also uses. One scanner, three readers.

States
  live      working | awaiting | queued | quiet-working | quiet-awaiting | idle |
            error | unknown                      (agentboard.classify)
  started   a pi process with no transcript yet (launched, never prompted)
  recent-unowned  transcript written in the last FRESH_S seconds, no owner bound
  ended     no live process; restorable with `pi --session <id>`
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_agentboard() -> str:
    """Installed layout puts agentboard.py next to this file; the repo keeps it in
    the sibling experiment directory. One scanner, two locations."""
    for cand in (os.environ.get("LEDGER_AGENTBOARD", ""),
                 os.path.join(HERE, "agentboard.py"),
                 os.path.normpath(os.path.join(HERE, "..",
                                               "desktop-window-orchestration", "agentboard.py"))):
        if cand and os.path.exists(cand):
            return cand
    return os.path.join(HERE, "agentboard.py")


AGENTBOARD = _find_agentboard()
STATE_PATH = os.path.join(HERE, "state.json")
PIN_PATH = os.path.join(HERE, "pins.json")
LAUNCH_PATH = os.path.join(HERE, "launched.json")

DEFAULT_WINDOW_H = 72      # table/history default look-back
EXACT_S = 180              # session start vs process start
FRESH_S = 300              # "something is writing this right now"
STALE_S = 180              # exact pair untouched this long may be superseded


def _load_agentboard():
    spec = importlib.util.spec_from_file_location("agentboard", AGENTBOARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load_agentboard()
SESSION_ROOTS = ab.SESSION_ROOTS


# ---------------------------------------------------------------- processes

def _btime() -> float:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def proc_start(pid: str) -> float:
    """Process start as epoch seconds, from /proc/<pid>/stat field 22."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        return _btime() + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


def pi_procs() -> dict[str, dict]:
    out = {}
    for pid in ab.sh(["pgrep", "-x", "pi"]).split():
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        out[pid] = {"cwd": cwd, "start": proc_start(pid)}
    return out


# ---------------------------------------------------------------- transcripts

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
# pi-subagents writes its own transcript per run inside the parent session dir:
#   sessions/<parent>/<run-uuid>/run-0/session.jsonl
# Those are runs, not sessions a human resumes.
_SUBAGENT = re.compile(r"/run-\d+/session\.jsonl$")


def read_head(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            ev = json.loads(fh.readline())
        if isinstance(ev, dict) and ev.get("type") == "session":
            return ev
    except Exception:
        pass
    return {}


def parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def scan_transcripts(window_h: float | None = DEFAULT_WINDOW_H,
                     prev: dict | None = None) -> list[dict]:
    """One dict per session id. window_h=None means every file on disk.

    The same id can appear in several files (a resumed session copied into another
    --session-dir). The newest file wins; the copy count is kept, not hidden.
    """
    cutoff = time.time() - window_h * 3600 if window_h else 0.0
    seen_paths, by_id = set(), {}
    for root in SESSION_ROOTS:
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            if "subagent-artifacts" in path or path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size < 2 or (cutoff and st.st_mtime < cutoff):
                continue
            old = (prev or {}).get(path)
            if old and old.get("mtime_epoch") == int(st.st_mtime) and old.get("size") == st.st_size:
                sid, cwd, started = old["id"], old["cwd"], old["started_epoch"]
            else:
                head = read_head(path)
                sid = head.get("id")
                if not sid:
                    m = _UUID.search(os.path.basename(path))
                    sid = m.group(1) if m else ""
                cwd = head.get("cwd") or ""
                started = int(parse_ts(head.get("timestamp")) or st.st_mtime)
            if not sid:
                continue
            rec = {
                "id": sid,
                "path": path,
                "kind": "subagent" if _SUBAGENT.search(path) else "session",
                "store": "pi" if "/.pi/" in path else "feynman",
                "cwd": cwd,
                "started_at": started,
                "mtime": st.st_mtime,
                "size": st.st_size,
                "copies": 1,
            }
            old = by_id.get(sid)
            if old is None:
                by_id[sid] = rec
            else:
                old["copies"] += 1
                if rec["mtime"] > old["mtime"]:
                    rec["copies"] = old["copies"]
                    by_id[sid] = rec
    return sorted(by_id.values(), key=lambda r: -r["mtime"])


# ---------------------------------------------------------------- binding

from functools import lru_cache


@lru_cache(maxsize=None)
def _real(path: str) -> str:
    """realpath is ~5ms per call on a /mnt/c 9p path; binding calls it ~20k times."""
    return os.path.realpath(path)


def _same_dir(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _real(a) == _real(b)


def bind(procs: dict[str, dict], trans: list[dict]) -> dict[str, dict]:
    """pid -> {sid, kind}. See module docstring for the ladder."""
    pid2 = {}
    sid2pid = {}
    by_id = {t["id"]: t for t in trans}

    def take(pid: str, t: dict, kind: str) -> None:
        pid2[pid] = {"sid": t["id"], "kind": kind}
        sid2pid[t["id"]] = pid

    # 1. exact: created at process start
    for pid, p in sorted(procs.items(), key=lambda kv: kv[1]["start"]):
        if pid in pid2:
            continue
        best = None
        for t in trans:
            if t["id"] in sid2pid or not _same_dir(t["cwd"], p["cwd"]):
                continue
            d = abs(t["started_at"] - p["start"])
            if d <= EXACT_S and (best is None or d < best[0]):
                best = (d, t)
        if best:
            take(pid, best[1], "exact")

    # 2. succession: the bound file went stale, a newer file was created later by
    #    the same process (compaction). Only files born after the process started count.
    now = time.time()
    for pid in list(pid2):
        cur = next(t for t in trans if t["id"] == pid2[pid]["sid"])
        if now - cur["mtime"] <= STALE_S:
            continue
        newer = [t for t in trans
                 if t["id"] not in sid2pid and _same_dir(t["cwd"], procs[pid]["cwd"])
                 and t["started_at"] >= procs[pid]["start"] - EXACT_S
                 and t["mtime"] > cur["mtime"] + 60]
        if newer:
            newest = max(newer, key=lambda t: (t["mtime"], t["started_at"]))
            del sid2pid[pid2[pid]["sid"]]
            take(pid, newest, "succession")

    # 3. launched: ledger created this pane and recorded which session it opened
    launches = load_launches()
    if launches:
        for pid, p in procs.items():
            if pid in pid2:
                continue
            pane = ab.tmux_of(pid, ab.pane_index())
            sess = pane.split(":")[0] if pane != "-" else ""
            rec = launches.get(sess)
            hit = by_id.get(rec.get("id", "")) if rec else None
            if hit:
                take(pid, hit, "launched")

    # 4. created-after: a file born during the process's lifetime can be its own
    for pid, p in procs.items():
        if pid in pid2:
            continue
        born = [t for t in trans
                if t["id"] not in sid2pid and _same_dir(t["cwd"], p["cwd"])
                and t["started_at"] >= p["start"] - EXACT_S]
        if born:
            best = min(born, key=lambda t: (abs(t["started_at"] - p["start"]), -t["mtime"]))
            take(pid, best, "created-after")

    return pid2


# ---------------------------------------------------------------- tmux

def tmux_sessions() -> dict[str, dict]:
    fmt = "#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_created}"
    out = {}
    for line in ab.sh(["tmux", "list-sessions", "-F", fmt]).splitlines():
        p = line.split("\t")
        if len(p) == 4:
            out[p[0]] = {
                "windows": int(p[1] or 0),
                "attached": p[2] not in ("", "0"),
                "created": int(float(p[3] or 0)),
            }
    return out


def tmux_pane_meta() -> dict[str, dict]:
    """target -> {cmd, size, dead, pid}. One call; used for records and restore."""
    fmt = ("#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}\t"
           "#{pane_current_command}\t#{pane_width}x#{pane_height}\t#{pane_dead}\t#{pane_active}")
    out = {}
    for line in ab.sh(["tmux", "list-panes", "-a", "-F", fmt]).splitlines():
        p = line.split("\t")
        if len(p) != 6:
            continue
        out[p[0]] = {"pid": p[1], "cmd": p[2], "size": p[3],
                     "dead": p[4] == "1", "active": p[5] == "1"}
    return out


def pane_size(target: str) -> str:
    out = ab.sh(["tmux", "display-message", "-p", "-t", target,
                 "#{pane_width}x#{pane_height}"]).strip()
    return out if "x" in out else ""


# ---------------------------------------------------------------- records

def _split_target(target: str) -> tuple[int, int]:
    """"sess:11.0" -> (11, 0). Missing coordinates become 0."""
    try:
        coords = target.rsplit(":", 1)[1]
        w, _, pn = coords.partition(".")
        return int(w), int(pn or 0)
    except Exception:
        return 0, 0


def _record(t: dict, pid: str, kind: str, panes: dict, meta: dict, pins: set[str],
            intent: str | None = None, pane_meta: dict | None = None) -> dict:
    now = time.time()
    tmux = ab.tmux_of(pid, panes) if pid else "-"
    age = max(0.0, now - t["mtime"])
    if pid:
        state = ab.classify(ab.last_message(t["path"]), age)
    elif age <= FRESH_S:
        state = "recent-unowned"
    else:
        state = "ended"
    tname = tmux.split(":")[0] if tmux != "-" else ""
    rec = {
        "id": t["id"],
        "kind": t.get("kind", "session"),
        "short": t["id"].split("-")[0][-4:],
        "store": t["store"],
        "path": t["path"],
        "copies": t.get("copies", 1),
        "cwd": t["cwd"],
        "home_rel": t["cwd"].replace(os.path.expanduser("~"), "~") if t["cwd"] else "?",
        "project": _project(t["cwd"]),
        "intent": intent if intent is not None else ab.first_user_text(t["path"], limit=110),
        "state": state,
        "live": bool(pid),
        "binding": kind,
        "pid": pid,
        "tmux": tmux,
        "tmux_session": tname,
        "tmux_win": _split_target(tmux)[0] if tmux != "-" else 0,
        "tmux_pane": _split_target(tmux)[1] if tmux != "-" else 0,
        "pane_size": (pane_meta or {}).get(tmux, {}).get("size", "")
        if tmux != "-" else "",
        "pane_cmd": (pane_meta or {}).get(tmux, {}).get("cmd", "") if tmux != "-" else "",
        "idle_s": int(age),
        "last_activity": iso(t["mtime"]),
        "started": iso(t["started_at"]),
        "started_epoch": int(t["started_at"]),
        "mtime_epoch": int(t["mtime"]),
        "size": t["size"],
        "pinned": t["id"] in pins,
        "restore": f"pi --session {shq(t['path'])}",
        "restore_id": f"pi --session {t['id']}",
    }
    if tname in meta:
        rec["tmux_windows"] = meta[tname]["windows"]
        rec["tmux_attached"] = meta[tname]["attached"]
    return rec


def _project(cwd: str) -> str:
    if not cwd:
        return "?"
    home = os.path.expanduser("~")
    if _real(cwd) == _real(home):
        return "home"
    return os.path.basename(cwd.rstrip("/")) or "?"


def _proc_env(pid: str, key: str) -> str:
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            for item in fh.read().split(b"\0"):
                if item.startswith(key.encode() + b"="):
                    return item[len(key) + 1:].decode("utf-8", "replace")
    except OSError:
        pass
    return ""


def _bare_record(pid: str, p: dict, panes: dict, meta: dict,
                 pane_meta: dict | None = None) -> dict:
    """A live pi process that has not written a transcript yet."""
    tmux = ab.tmux_of(pid, panes)
    tname = tmux.split(":")[0] if tmux != "-" else ""
    terminal = _proc_env(pid, "PI_TERMINAL_SESSION")
    intent = (f"pi-web session '{terminal}' — no transcript in this cwd"
              if terminal else "(no transcript yet — launched, never prompted)")
    rec = {
        "id": "",
        "kind": "process",
        "short": "--",
        "store": "pi",
        "path": "",
        "copies": 0,
        "cwd": p["cwd"],
        "home_rel": p["cwd"].replace(os.path.expanduser("~"), "~") if p["cwd"] else "?",
        "project": _project(p["cwd"]),
        "intent": intent,
        "state": "started",
        "live": True,
        "binding": "none",
        "pid": pid,
        "tmux": tmux,
        "tmux_session": tname,
        "tmux_win": _split_target(tmux)[0] if tmux != "-" else 0,
        "tmux_pane": _split_target(tmux)[1] if tmux != "-" else 0,
        "pane_size": (pane_meta or {}).get(tmux, {}).get("size", "")
        if tmux != "-" else "",
        "pane_cmd": (pane_meta or {}).get(tmux, {}).get("cmd", "") if tmux != "-" else "",
        "idle_s": int(max(0.0, time.time() - p["start"])),
        "last_activity": iso(p["start"]),
        "started": iso(p["start"]),
        "started_epoch": int(p["start"]),
        "mtime_epoch": int(p["start"]),
        "size": 0,
        "pinned": False,
        "restore": "",
    }
    if tname in meta:
        rec["tmux_windows"] = meta[tname]["windows"]
        rec["tmux_attached"] = meta[tname]["attached"]
    return rec


def _intents(trans: list[dict]) -> dict[str, str]:
    """First human message per transcript, from cache when the file is unchanged.

    Reading 900+ transcript heads off a Windows mount takes ~90s serially; unchanged
    files are answered from the last state.json and the rest are read in parallel.
    """
    prev = {}
    for r in load_state().get("sessions", []):
        if r.get("path") and r.get("intent"):
            prev[r["path"]] = r
    out, todo = {}, []
    for t in trans:
        c = prev.get(t["path"])
        if c and c.get("mtime_epoch") == int(t["mtime"]) and c.get("size") == t["size"]:
            out[t["id"]] = c["intent"]
        else:
            todo.append(t)
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=12) as pool:
            for t, text in zip(todo, pool.map(lambda t: ab.first_user_text(t["path"], limit=110), todo)):
                out[t["id"]] = text
    return out


def build_state(window_h: float | None = DEFAULT_WINDOW_H) -> dict:
    now = time.time()
    prev_state = load_state()
    prev = {r["path"]: r for r in prev_state.get("sessions", []) if r.get("path")}
    procs = pi_procs()
    panes = ab.pane_index()
    trans = scan_transcripts(window_h=window_h, prev=prev)
    pid2 = bind(procs, trans)
    meta = tmux_sessions()
    pane_meta = tmux_pane_meta()
    pins = load_pins()

    intents = _intents(trans)
    records, bound_pids = [], set()
    for t in trans:
        pid, kind = "", "none"
        for p, b in pid2.items():
            if b["sid"] == t["id"]:
                pid, kind = p, b["kind"]
                bound_pids.add(p)
                break
        records.append(_record(t, pid, kind, panes, meta, pins,
                               intents.get(t["id"]), pane_meta))
    for pid, p in procs.items():
        if pid not in bound_pids:
            records.append(_bare_record(pid, p, panes, meta, pane_meta))

    counts: dict[str, int] = {}
    for r in records:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    kinds: dict[str, int] = {}
    for r in records:
        if r["live"]:
            kinds[r["binding"]] = kinds.get(r["binding"], 0) + 1
    inferred = sum(kinds.get(k, 0) for k in ("created-after", "succession"))

    return {
        "generated": iso(now),
        "generated_epoch": int(now),
        "window_h": window_h,
        "counts": counts,
        "live": sum(1 for r in records if r["live"]),
        "ended": sum(1 for r in records if not r["live"]),
        "pi_processes": len(procs),
        "tmux_sessions": len(meta),
        "bindings": kinds,
        "panes": [
            {"target": target, "session": target.split(":")[0],
             "win": _split_target(target)[0], "pane": _split_target(target)[1],
             "cmd": info["cmd"], "size": info["size"], "dead": info["dead"]}
            for target, info in sorted(pane_meta.items())
        ],
        "inferred_bindings": inferred,
        "unbound_processes": kinds.get("none", 0),
        "sessions": records,
    }


# ---------------------------------------------------------------- misc

def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'" if any(c in s for c in " '") else s


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_pins() -> set[str]:
    try:
        with open(PIN_PATH, encoding="utf-8") as fh:
            return set(json.load(fh).get("pinned", []))
    except Exception:
        return set()


def load_launches() -> dict[str, dict]:
    """tmux session -> the session ledger started in it. ledger's own record of its
    own writes; pi's argv is rewritten to "pi", so this is the only evidence."""
    try:
        with open(LAUNCH_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("launches", {})
    except Exception:
        return {}


def save_launches(launches: dict[str, dict]) -> None:
    """Prune the obvious: a launch whose session is gone and which is older than ten
    minutes is history, not an owner."""
    alive = set(tmux_sessions())
    keep = {k: v for k, v in launches.items()
            if k in alive or time.time() - float(v.get("at", 0)) < 600}
    with open(LAUNCH_PATH, "w", encoding="utf-8") as fh:
        json.dump({"launches": keep}, fh, indent=1)


def record_launch(tmux_name: str, rec: dict) -> None:
    launches = load_launches()
    launches[tmux_name] = {
        "id": rec["id"],
        "path": rec["path"],
        "at": int(time.time()),
        "restore": rec["restore"],
    }
    save_launches(launches)


def save_pins(pins: set[str]) -> None:
    with open(PIN_PATH, "w", encoding="utf-8") as fh:
        json.dump({"pinned": sorted(pins)}, fh, indent=1)


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_state(state: dict) -> str:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_PATH)
    return STATE_PATH


def resolve(state: dict, sel: str) -> dict | None:
    """Select by row number, id prefix, tmux target, project or intent substring."""
    rows = state.get("sessions") or []
    s = (sel or "").strip()
    if s.isdigit():
        i = int(s)
        if 1 <= i <= len(rows):
            return rows[i - 1]
    low = s.lower()
    for r in rows:
        if r["id"] and (r["id"] == s or r["id"].startswith(low)):
            return r
    for r in rows:
        if r["tmux"] != "-" and r["tmux"].lower() == low:
            return r
        if r["tmux_session"] and r["tmux_session"].lower() == low:
            return r
    hits = [r for r in rows if low in r["intent"].lower() or low == r["project"].lower()]
    if hits:
        return max(hits, key=lambda r: r["mtime_epoch"])
    return None


def human_age(seconds: int) -> str:
    if seconds < 0:
        return "-"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


STATE_ORDER = {"error": 0, "awaiting": 1, "quiet-awaiting": 2, "working": 3,
               "quiet-working": 4, "queued": 5, "recent-unowned": 6,
               "started": 7, "ended": 8, "idle": 9, "unknown": 10}


def sort_key(r: dict):
    return (STATE_ORDER.get(r["state"], 99), r["idle_s"])


def in_window(r: dict, hours: float) -> bool:
    return r["live"] or (time.time() - r["mtime_epoch"]) <= hours * 3600


if __name__ == "__main__":
    st = build_state()
    print(json.dumps({k: v for k, v in st.items() if k != "sessions"}, indent=1))
    for r in sorted(st["sessions"], key=sort_key)[:14]:
        print(f"{r['state']:15s} {human_age(r['idle_s']):>5s} {r['binding']:9s} "
              f"{r['tmux']:20s} {r['intent'][:50]}")
