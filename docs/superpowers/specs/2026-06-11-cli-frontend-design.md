# CLI Frontend Design

**Date:** 2026-06-11
**Goal:** A console frontend (`cli.py`) for the MTGA Draft Tool — watch Player.log
during a draft and print 17Lands-rated pick suggestions. No GUI, no fancy TUI;
plain console output with a few single-key commands.

## Principle

Purely additive. One new file at the repo root next to `main.py`. Zero changes to
existing modules, so the GUI keeps working and upstream pulls stay conflict-free.

## Reused components

| Concern | Reused module |
|---|---|
| Bootstrap (log discovery, MTGA data dir, dataset cloud sync, set list) | same call sequence as `main.py:load_data` (UI-free) |
| Log tailing, truncation handling, draft detection | `src/ui/orchestrator.py:DraftOrchestrator` (no tkinter despite the path) |
| Pack/pick parsing, state | `src/log_scanner.py:ArenaScanner` |
| Pick recommendations | `src/advisor/engine.py:DraftAdvisor` |
| Color signals | `src/signals.py:SignalCalculator` |
| Grades / win-rate formatting, color filter | `src/card_logic.py:format_win_rate`, `filter_options` |

The render path mirrors `app_controller.py:refresh_ui_data` exactly (snapshot under
`scanner.lock` → signal math → advisor → rows), replacing tkinter widgets with
printed tables.

## CLI behavior

- `python cli.py` — bootstrap, then watch the live log. On each new pack: print a
  table sorted like the GUI (advisor score, else GIH WR): score, grade, GIH%, OH%,
  ALSA, IWD, wheel%, colors, name (⭐ elite / [+] high archetype fit markers).
- Keys (cbreak, no Enter): `p` picks so far, `c` color signals, `r` re-print pack,
  `q` quit. Disabled when stdin is not a TTY.
- Flags: `-f/--file` (log override; also how you replay an old log), `-d/--data`
  (MTGA data dir), `--once` (render current state and exit — used for testing),
  `--no-sync` (skip dataset cloud sync), `--filter` (deck color filter, default
  Auto), `--format` (Grade/Percentage/Rating).
- `os.chdir` to the script dir before importing `src` (BASE_DIR is cwd-derived).

## Error handling

- No log found → print expected path + "enable Detailed Logs (Plugin Support)".
- No dataset for the detected set → show pack by card name, warn once.
- Non-TTY stdin → watch-only mode, no key handling.

## Dependencies

Headless subset only: `numpy`, `pydantic`, `requests` (advisor + config + HTTP).
No ttkbootstrap/Pillow/pynput/numba/scipy needed.

## Verification

- `--once` against the real `~/Library/Logs/Wizards of the Coast/MTGA/Player.log`
  (contains a past draft) must print the rated final pack state.
- Existing pytest suite for scanner/advisor stays green (untouched code).
