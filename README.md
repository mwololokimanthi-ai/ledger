# ledger

One record per agent session — live or ended — in one table.

`ledger` answers a question that sounds trivial until you run several agents at once: *which session is this process actually working on?* The working directory is ambiguous, the tmux name is arbitrary, and the process holds no file descriptor on its transcript. The transcript itself is the only reliable identifier — its first line carries the session id, the cwd and the start time — so the transcript is the unit of record, and running processes are attachments to it.

```console
$ ledger table
  #  STATE      AGE   PROJECT        INTENT                      SESSION
  1  working    2m    ledger         add binding tests           01a1d453
  2  awaiting   14m   cv             check page count           01a1d3f9
  3  ended      6h    research       invoice reconciliation     01a1c88e
```

## Why the binding is a ladder, not a lookup

An agent process can start in one directory, be compacted, and reopen a **different** transcript in the same process. A pid that started at 13:01 with session `A` can be writing session `B` by 16:22. There is no registry row to consult and no descriptor to follow, so "process start equals session start" is evidence, not proof.

`ledger` therefore binds in ranked order and records which rung was used:

| Rung | Rule | Confidence |
|---|---|---|
| `exact` | Same cwd, session start within 180 s of process start | High |
| `succession` | An exact pair went stale; a newer file from the same process supersedes it (compaction) | High |
| `launched` | `ledger open` started this pane and recorded the session it opened | High |
| `created-after` | Unbound process; only transcripts born during its lifetime can belong to it — newest wins | Low, labelled |
| *(none)* | No defensible match; reported as unbound rather than guessed | — |

The ladder is the part covered by tests. Seven unit tests exercise the exact window, the rejection path, succession after compaction, created-after, the no-match case, and selection by row and id prefix.

```console
$ python3 -m unittest discover -s tests -v
Ran 7 tests in 0.002s
OK
```

## Commands

| Command | What it does |
|---|---|
| `ledger table [--all]` | Live sessions plus anything touched in the last 72 h |
| `ledger show <sel>` | Ids, paths, tmux target, restore command |
| `ledger open <sel>` | Attach to its tmux session, or restore it into a new one |
| `ledger history` | Ended sessions, newest first, grouped by day |
| `ledger snapshot` / `restore` | Freeze state and recreate tmux sessions from it |
| `ledger serve` | Localhost dashboard on `127.0.0.1` |
| `ledger pin <sel>` | Keep a session in restore after it ends |
| `ledger doctor` | Self-check: stores, tmux, agent binary, service, bindings |
| `ledger tui` | Full-screen console |

`<sel>` accepts a row number, a session id or prefix, a tmux target (`name`, `name:win`, `name:win.pane`), a project name, or a substring of the intent.

## Design constraints

- **Read-only against the agent's stores.** `ledger` never writes a transcript. Its only writes are `state.json`, `pins.json` and tmux sessions you explicitly open.
- **One scanner, three readers.** Transcript discovery, tail parsing and state classification are imported from `agentboard.py`; the table, the dashboard and the TUI all read the same state.
- **Honest degradation.** An unmatched process is shown as unbound. A weak match is labelled `created-after`, never promoted to `exact`.
- **No daemon required.** State is rebuilt from disk; the snapshot exists for restore, not for correctness.

## Layout

```
ledger_core.py   discovery, binding ladder, state build   (655 lines)
ledger           CLI                                       (612 lines)
tui.py           full-screen console                       (1,044 lines)
agentboard.py    transcript scanning and classification    (239 lines)
static/          dashboard
tests/           seven unit tests for the ladder
```

## Requirements

Python 3.10+, tmux, and the `pi` agent harness on `PATH`. `agentboard.py` must sit next to `ledger_core.py` (or be pointed at with `LEDGER_AGENTBOARD`).

## Limitations

- Binding is heuristic by construction; the rungs are ordered by evidence, not certainty.
- `created-after` can mis-attribute when several processes share one directory. It is labelled for that reason.
- The TUI and dashboard assume a POSIX terminal.

## License

MIT.
