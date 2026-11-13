#!/usr/bin/env python3
"""agentboard v2 — read-only status board for `pi` agents on this machine.

Why v2: v1 tested `event["type"] in ("tool_use","tool_call")`, but pi's transcript
schema nests everything under a `message` event, so that branch could never fire.
Observed tail events (2026-11-13, all live sessions):
  {"type":"message","message":{"role":"assistant","stopReason":"toolUse",...}}
  {"type":"message","message":{"role":"toolResult","toolName":...,"isError":...}}
  {"type":"message","message":{"role":"assistant","stopReason":"stop",...}}

State mapping (derived, not signalled by the agent):
  working    last message is an assistant turn that ended in toolUse, or a toolResult
             (the model is mid-turn and will continue on its own)
  awaiting   last message is an assistant turn that ended with stopReason=stop
             (the turn is over; it will not continue without a human)
  quiet      no transcript write for > QUIET_S while in working/awaiting
  idle       no transcript write for > IDLE_S
There is deliberately no "blocked" state: a pending permission prompt looks identical
to a running tool call in the transcript. That needs either a pi-side signal or the
OSC 7501 `state=blocked;kind=permission` convention.

Usage: python3 agentboard.py [--json] [--wide]

v3: adds the INTENT column — the first human message in each transcript. Reason: on
this machine 5 of 6 pi panes share one cwd, and 14 of 16 tmux sessions are named with
integers, so neither session name nor path distinguishes them. The opening prompt does.
"""
from __future__ import annotations
import glob, json, os, subprocess, sys, time

SESSIONS = os.path.expanduser("~/.feynman/sessions")
# pi 1.5.x (the agent CLI) keeps its own store, one directory per project:
#   ~/.pi/agent/sessions/--mnt-c-Users-DELL-Desktop-Upgrades-Projector--/<ts>_<uuid>.jsonl
# v2 globbed only the feynman root, so five live Projector sessions showed as
# "no-transcript". Scan both.
SESSION_ROOTS = [
    os.path.expanduser("~/.feynman/sessions"),
    os.path.expanduser("~/.pi/agent/sessions"),
]
QUIET_S = 300
IDLE_S = 1800


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def pi_procs() -> dict[str, str]:
    out = {}
    for pid in sh(["pgrep", "-x", "pi"]).split():
        try:
            out[pid] = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
    return out


def pane_index() -> dict[str, str]:
    fmt = "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}"
    out = {}
    for line in sh(["tmux", "list-panes", "-a", "-F", fmt]).splitlines():
        p = line.split("\t")
        if len(p) == 2:
            out[p[0]] = p[1]
    return out


def ppid(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            return fh.read().rsplit(")", 1)[1].split()[1]
    except Exception:
        return ""


def tmux_of(pid: str, panes: dict[str, str]) -> str:
    cur, seen = pid, set()
    while cur and cur not in seen:
        if cur in panes:
            return panes[cur]
        seen.add(cur)
        cur = ppid(cur)
    return "-"


def head_cwd(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.loads(fh.readline()).get("cwd")
    except Exception:
        return None


def last_message(path: str) -> dict:
    """Last *message* event in the transcript (tail-read).

    Skips trailing `custom` events: pi writes coms-log / status customs after a
    turn, and treating those as the latest event made live sessions read
    `unknown`.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed([l for l in tail.splitlines() if l.startswith("{")]):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev.get("message"), dict) or ev.get("type") == "message":
                return ev
    except Exception:
        pass
    return {}


def first_user_text(path: str, limit: int = 58) -> str:
    """First human message in the transcript = what this session is for.

    Autonomous sessions (the APM worker/manager panes) open with a `system`
    message and may never have a `user` turn, so fall back to the first text of
    any message rather than showing "-".
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(262144)
    except Exception:
        return "-"
    fallback = ""
    for line in head.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if not msg:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        text = " ".join(text.split())
        if not text:
            continue
        if msg.get("role") == "user":
            return text[:limit] + ("…" if len(text) > limit else "")
        if not fallback:
            fallback = text
    if fallback:
        return fallback[:limit] + ("…" if len(fallback) > limit else "")
    return "(no prompt)"


def classify(ev: dict, age: float) -> str:
    msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
    role, stop = msg.get("role"), msg.get("stopReason")
    if stop == "error":
        return "error"          # a failed turn is not idle; it needs a human
    if role == "assistant" and stop == "toolUse":
        base = "working"
    elif role == "toolResult":
        base = "working"
    elif role == "assistant" and stop == "stop":
        base = "awaiting"
    elif role == "user":
        base = "queued"
    else:
        base = "unknown"
    if age > IDLE_S:
        return "idle"
    if age > QUIET_S and base in ("working", "awaiting"):
        return f"quiet-{base}"
    return base


def main() -> int:
    procs, panes = pi_procs(), pane_index()
    files = []
    for root in SESSION_ROOTS:
        for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            if "subagent-artifacts" in f:
                continue
            files.append(f)
    files = sorted(set(files), key=os.path.getmtime, reverse=True)
    cwds = {f: head_cwd(f) for f in files}
    now = time.time()

    rows, used = [], set()
    for pid, cwd in procs.items():
        cands = [f for f in files if cwds.get(f) == cwd]
        match = next((f for f in cands if f not in used), None)
        ambiguous = len(cands) > 1 or sum(1 for c in procs.values() if c == cwd) > 1
        if match:
            used.add(match)
        age = now - os.path.getmtime(match) if match else -1
        rows.append({
            "pid": pid,
            "cwd": cwd.replace(os.path.expanduser("~"), "~"),
            "tmux": tmux_of(pid, panes),
            "state": classify(last_message(match), age) if match else "no-transcript",
            "idle_s": int(age),
            "ambiguous": ambiguous,
            "session": os.path.basename(match)[:18] if match else "-",
            "intent": first_user_text(match) if match else "-",
        })

    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
        return 0

    with_pane = sum(1 for r in rows if r["tmux"] != "-")
    amb = sum(1 for r in rows if r["ambiguous"])
    print(f"{len(rows)} pi processes · {with_pane} bound to a tmux pane · "
          f"{amb} ambiguous cwd binding · {len(files)} transcripts on disk\n")
    print(f"{'STATE':14s} {'AGE':>6s}  {'TMUX':12s} {'PROJECT':16s} INTENT")
    for r in sorted(rows, key=lambda r: -r["idle_s"]):
        age = "-" if r["idle_s"] < 0 else (f"{r['idle_s']}s" if r["idle_s"] < 600 else f"{r['idle_s']//60}m")
        flag = " ?" if r["ambiguous"] else "  "
        proj = os.path.basename(r["cwd"].rstrip("/"))[:16] or "/"
        line = f"{r['state']:14s} {age:>6s} {flag}{r['tmux']:12s} {proj:16s} {r['intent']}"
        if "--wide" in sys.argv:
            line += f"  pid={r['pid']} {r['cwd']}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
