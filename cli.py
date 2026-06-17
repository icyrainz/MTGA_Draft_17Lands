#!/usr/bin/env python3
"""
cli.py
MTGA Draft Tool - Console Frontend.

Watches the Arena Player.log during a draft and prints 17Lands-rated pick
suggestions using the same scanner, datasets, and advisor as the GUI.

Usage:
    python cli.py                 watch the live Player.log
    python cli.py -f <log>        watch (or replay) a specific log file
    python cli.py --once          print the current draft state and exit
    python cli.py --deck          build a suggested deck from the pool and exit

Keys while watching:
    p  picks    c  color signals    d  build deck    r  re-print pack    q  quit

Requires only the headless dependencies (requirements-cli.txt); no GUI libraries.
"""

import argparse
import logging
import os
import queue
import select
import sys

try:
    import termios
    import tty
except ImportError:
    # No termios (e.g. native Windows): watch-only mode, no key commands
    termios = tty = None

# BASE_DIR (Sets/, Logs/, config) is derived from the working directory, so the
# CLI must run from the repo root regardless of where it was invoked.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure:
    _reconfigure(line_buffering=True)
logging.basicConfig(level=logging.WARNING)
# src.configuration logs INFO during import, before any post-import cleanup can
# run; filter the shared logger at the source.
logging.getLogger("debug_log").addFilter(lambda record: record.levelno >= logging.WARNING)

from src import constants
from src.advisor.engine import DraftAdvisor
from src.card_logic import filter_options, format_win_rate
from src.configuration import read_configuration, write_configuration
from src.file_extractor import search_arena_log_locations, retrieve_arena_directory
from src.limited_sets import LimitedSets
from src.log_scanner import ArenaScanner
from src.signals import SignalCalculator
from src.ui.orchestrator import DraftOrchestrator

# The project's shared loggers echo INFO chatter to the console (a stdout
# handler on "debug_log", plus propagation to root). Keep their file logging
# but take them off the terminal.
for _name in ("debug_log", "draftLog"):
    _logger = logging.getLogger(_name)
    _logger.propagate = False
    for _handler in list(_logger.handlers):
        if type(_handler) is logging.StreamHandler:
            _logger.removeHandler(_handler)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
MANA_ANSI = {
    "W": "\033[97m",
    "U": "\033[94m",
    "B": "\033[95m",
    "R": "\033[91m",
    "G": "\033[92m",
}


def tint(text, code):
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def tint_colors(colors):
    return "".join(tint(c, MANA_ANSI.get(c, "")) for c in colors)


def card_name_tint(name, colors):
    """Bold the card name, colored by its identity (gold = multicolor)."""
    if not sys.stdout.isatty():
        return name
    if len(colors) == 1:
        code = MANA_ANSI.get(colors[0], "")
    elif len(colors) >= 2:
        code = "\033[33m"  # gold for multicolor cards
    else:
        code = ""  # colorless / artifact: leave default
    return f"{BOLD}{code}{name}{RESET}"


def grade_tint(grade):
    if grade.startswith(("A", "B")):
        return tint(grade, "\033[92m")
    if grade.startswith("C"):
        return tint(grade, "\033[93m")
    if grade in ("-", ""):
        return grade
    return tint(grade, "\033[91m")


def pad(colored, plain, width):
    """Left-justify ANSI-colored text by its visible (plain) length."""
    return colored + " " * max(0, width - len(plain))


def say(message):
    print(f"{DIM if sys.stdout.isatty() else ''}... {message}{RESET if sys.stdout.isatty() else ''}")


def load_set_dataset(scanner, config, set_code):
    """Loads the local Sets/ dataset matching the event's set code."""
    sources = scanner.retrieve_data_sources()
    tag = f"[{set_code.upper()}]"
    matches = [(label, path) for label, path in sources.items() if tag in label.upper()]
    # 17Lands "Top" (top-player) datasets are frequently empty for QuickDraft,
    # which would leave every card ungraded. Prefer the "(All)" variant, which
    # carries the full ratings.
    matches.sort(key=lambda lp: "(ALL)" not in lp[0].upper())
    for label, path in matches:
        scanner.retrieve_set_data(path)
        config.card_data.latest_dataset = os.path.basename(path)
        write_configuration(config)
        return True
    return False


def bootstrap(args):
    """Same sequence as main.py:load_data, minus the splash screen."""
    # Display preferences stay out of `config`: bootstrap and the orchestrator
    # both persist it to the GUI's shared config.json, and the CLI must not
    # rewrite the GUI's settings.
    config, _ = read_configuration()

    say("Locating Arena logs")
    log_path = search_arena_log_locations(args.file, config.settings.arena_log_location)
    if not log_path:
        sys.exit(
            "No Player.log found. Pass -f <path to log>.\n"
            "For live drafts, enable 'Detailed Logs (Plugin Support)' in MTGA "
            "(Options -> Account) and restart Arena."
        )
    print(f"    log: {log_path}")
    # A one-off replay (-f) must not overwrite the persisted live-log path, or
    # the next plain run would keep reading the replayed file.
    if not args.file:
        config.settings.arena_log_location = log_path
        write_configuration(config)

    db_loc = config.settings.database_location
    if not (db_loc and os.path.exists(os.path.join(db_loc, "Downloads", "Raw"))):
        db_loc = args.data or retrieve_arena_directory(log_path)
        if db_loc:
            config.settings.database_location = db_loc
            write_configuration(config)
    if config.settings.database_location:
        print(f"    arena data: {config.settings.database_location}")

    if config.settings.auto_sync_datasets and not args.no_sync:
        say("Syncing 17Lands datasets (use --no-sync to skip)")
        from src.dataset_updater import DatasetUpdater

        try:
            DatasetUpdater(config).sync_datasets(say)
        except Exception as error:
            say(f"Dataset sync failed ({error}); using local datasets")

    say("Checking 17Lands for new sets")
    limited_sets = LimitedSets().retrieve_limited_sets()

    scanner = ArenaScanner(
        filename=log_path,
        set_list=limited_sets,
        retrieve_unknown=True,
        db_path=config.settings.database_location,
    )

    say("Searching log for a draft")
    scanner.draft_start_search()
    event_set, event_type = scanner.retrieve_current_limited_event()
    if event_set:
        print(f"    found: {event_set} {event_type}")
        if not load_set_dataset(scanner, config, event_set):
            print(
                f"    WARNING: no local dataset for {event_set} - cards will "
                "show without ratings (the GUI's Data tab can download one)"
            )
        say("Parsing picks")
        scanner.draft_data_search()
    else:
        say("No draft found in the log yet - waiting for one to start")

    return config, scanner


def snapshot(scanner, config, opts):
    """Mirror of app_controller.refresh_ui_data's state snapshot + math."""
    with scanner.lock:
        event_set, event_type = scanner.retrieve_current_limited_event()
        pack, pick = scanner.retrieve_current_pack_and_pick()
        metrics = scanner.retrieve_set_metrics()
        taken_cards = scanner.retrieve_taken_cards()
        pack_cards = scanner.retrieve_current_pack_cards()
        picked_cards = scanner.retrieve_current_picked_cards()
        # copy: the scanner returns its live list and the orchestrator thread
        # appends to it
        history = list(scanner.retrieve_draft_history())

    scores = {c: 0.0 for c in constants.CARD_COLORS}
    try:
        calculator = SignalCalculator(metrics)
        for entry in history:
            if entry["Pack"] == 2:
                continue
            h_pack = scanner.set_data.get_data_by_id(entry["Cards"])
            for c, v in calculator.calculate_pack_signals(h_pack, entry["Pick"]).items():
                scores[c] += v
    except Exception as error:
        logging.getLogger(__name__).warning(f"Signal math failed: {error}")

    recommendations = []
    try:
        advisor = DraftAdvisor(metrics, taken_cards, signals=scores)
        recommendations = advisor.evaluate_pack(pack_cards, pick, current_pack=pack)
    except Exception as error:
        logging.getLogger(__name__).warning(f"Advisor failed: {error}")

    colors = filter_options(taken_cards, opts.deck_filter, metrics, config)

    return {
        "event_set": event_set,
        "event_type": event_type,
        "pack": pack,
        "pick": pick,
        "metrics": metrics,
        "taken_cards": taken_cards,
        "pack_cards": pack_cards,
        "picked_cards": picked_cards,
        "recommendations": recommendations,
        "signals": scores,
        "filter": colors[0] if colors else constants.FILTER_OPTION_ALL_DECKS,
    }


def card_stat_row(card, active_filter, metrics, result_format):
    stats = card.get("deck_colors", {}).get(active_filter, {})
    gihwr = stats.get(constants.DATA_FIELD_GIHWR, 0.0) or 0.0
    grade = format_win_rate(
        gihwr, active_filter, constants.DATA_FIELD_GIHWR, metrics, result_format
    )

    def num(field):
        value = stats.get(field, 0.0)
        return f"{value:.1f}" if isinstance(value, float) and value != 0.0 else "-"

    return {
        "gihwr": gihwr,
        "grade": grade if gihwr else "-",
        "gih": num(constants.DATA_FIELD_GIHWR),
        "oh": num(constants.DATA_FIELD_OHWR),
        "alsa": num(constants.DATA_FIELD_ALSA),
        "iwd": num(constants.DATA_FIELD_IWD),
    }


def render_pack(snap, opts):
    pack, pick = snap["pack"], snap["pick"]
    if pack <= 0 or not snap["pack_cards"]:
        say("Waiting for a pack... (start or continue a draft in MTGA)")
        return

    active_filter = snap["filter"]
    metrics = snap["metrics"]
    rec_map = {r.card_name: r for r in snap["recommendations"]}
    picked_names = {c.get(constants.DATA_FIELD_NAME) for c in (snap["picked_cards"] or [])}

    title = (
        f"{snap['event_set']} {snap['event_type']} - Pack {pack} Pick {pick}"
        f"   filter: {active_filter}"
    )
    print()
    print(tint(f"== {title} ==", BOLD))
    header = f"{'SCORE':>5}  {'GRADE':<5} {'GIH%':>5} {'OH%':>5} {'ALSA':>4} {'IWD':>5} {'WHEEL':>5}  {'CLR':<5} CARD"
    print(tint(header, DIM))

    rows = []
    for card in snap["pack_cards"]:
        name = card.get(constants.DATA_FIELD_NAME, "Unknown")
        rec = rec_map.get(name)
        stats = card_stat_row(card, active_filter, metrics, opts.result_format)
        rows.append((rec.contextual_score if rec else stats["gihwr"], name, rec, card, stats))
    rows.sort(key=lambda r: r[0], reverse=True)

    for _, name, rec, card, stats in rows:
        marker = ""
        if rec and rec.is_elite:
            marker = "* "
        elif rec and rec.archetype_fit == "High":
            marker = "+ "
        display = f"{marker}{name}"
        if name in picked_names:
            display = tint(f"{display}  [taken]", DIM)
        score = f"{rec.contextual_score:.0f}" if rec else "-"
        wheel = f"{rec.wheel_chance:.0f}%" if rec and rec.wheel_chance > 0 else "-"
        colors = card.get("colors", [])
        print(
            f"{score:>5}  {pad(grade_tint(stats['grade']), stats['grade'], 5)} "
            f"{stats['gih']:>5} {stats['oh']:>5} {stats['alsa']:>4} {stats['iwd']:>5} {wheel:>5}  "
            f"{pad(tint_colors(colors), ''.join(colors), 5)} {display}"
        )

    top = [r for r in snap["recommendations"] if r.card_name not in picked_names][:1]
    # base_win_rate == 0 means no 17Lands data; a suggestion would be arbitrary
    if top and top[0].base_win_rate > 0:
        rec = top[0]
        sugg = next(
            (c for c in snap["pack_cards"]
             if c.get(constants.DATA_FIELD_NAME) == rec.card_name),
            None,
        )
        sugg_colors = sugg.get("colors", []) if sugg else []
        reasons = "; ".join(rec.reasoning[:3]) if rec.reasoning else ""
        tail = f" (score {rec.contextual_score:.0f})" + (f" - {reasons}" if reasons else "")
        print(
            tint(">> Suggested: ", BOLD)
            + card_name_tint(rec.card_name, sugg_colors)
            + tint(tail, BOLD)
        )


def render_picks(snap, opts):
    taken = snap["taken_cards"]
    if not taken:
        say("No picks yet")
        return
    counts = {}
    for card in taken:
        name = card.get(constants.DATA_FIELD_NAME, "Unknown")
        counts.setdefault(name, {"card": card, "count": 0})
        counts[name]["count"] += 1

    active_filter = snap["filter"]
    metrics = snap["metrics"]
    print()
    print(tint(f"== Picks so far ({len(taken)}) ==", BOLD))
    print(tint(f"{'#':>2}  {'GRADE':<5} {'GIH%':>5}  {'CLR':<5} CARD", DIM))
    rows = []
    for name, info in counts.items():
        stats = card_stat_row(info["card"], active_filter, metrics, opts.result_format)
        rows.append((stats["gihwr"], name, info, stats))
    rows.sort(key=lambda r: r[0], reverse=True)
    for _, name, info, stats in rows:
        colors = info["card"].get("colors", [])
        print(
            f"{info['count']:>2}  {pad(grade_tint(stats['grade']), stats['grade'], 5)} "
            f"{stats['gih']:>5}  {pad(tint_colors(colors), ''.join(colors), 5)} {name}"
        )


def render_signals(snap):
    scores = snap["signals"]
    print()
    print(tint("== Color signals (higher = more open) ==", BOLD))
    peak = max(scores.values()) if any(scores.values()) else 1.0
    for color, value in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        bar = "#" * int((value / peak) * 30) if peak > 0 else ""
        print(f"  {tint_colors(color)}  {value:6.1f}  {bar}")
    print(f"  active filter: {snap['filter']}")


def render_deck(snap, opts, config):
    """Build and print a suggested 40-card deck from the drafted pool."""
    taken = snap["taken_cards"]
    if not taken or len(taken) < 15:
        say("Not enough cards in the pool to build a deck yet")
        return

    from src.card_logic import suggest_deck

    say("Building decks (simulating archetypes, this can take a moment)...")
    decks = suggest_deck(
        taken,
        snap["metrics"],
        config,
        event_type=snap["event_type"] or "PremierDraft",
        progress_callback=lambda m: (
            say(m["status"]) if isinstance(m, dict) and "status" in m else None
        ),
        dataset_name=config.card_data.latest_dataset,
    )
    if not decks:
        say("Not enough on-color playables to form a 40-card deck (need ~22 spells)")
        return

    labels = list(decks.keys())
    best = decks[labels[0]]
    cards = best.get("deck_cards", [])
    spells = [c for c in cards if "Land" not in c.get("types", [])]
    lands = [c for c in cards if "Land" in c.get("types", [])]
    count = lambda lst: sum(c.get(constants.DATA_FIELD_COUNT, 1) for c in lst)

    print()
    print(tint(f"== Suggested deck: {labels[0]} ==", BOLD))
    print(
        f"  {tint_colors(best.get('colors', []))}  |  {count(cards)} cards "
        f"({count(spells)} spells, {count(lands)} lands)  |  est. record {best.get('record', '?')}"
    )
    print(tint(f"  {'#':>2} {'CMC':>3}  {'CLR':<5} CARD", DIM))
    # Match MTGA's deck-builder order: mana value ascending -> color (WUBRG,
    # then multicolor, then colorless) -> alphabetical. Lets the CLI list line
    # up row-for-row against the in-client deck when deciding cuts.
    wubrg = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4}

    def color_rank(c):
        cols = c.get("colors", [])
        if not cols:
            return 6  # colorless / artifact, after colored
        if len(cols) >= 2:
            return 5  # multicolor, after mono
        return wubrg.get(cols[0], 5)

    for card in sorted(
        spells,
        key=lambda c: (
            c.get("cmc", 0) or 0,
            color_rank(c),
            c.get(constants.DATA_FIELD_NAME, ""),
        ),
    ):
        cnt = card.get(constants.DATA_FIELD_COUNT, 1)
        cmc = card.get("cmc", 0) or 0
        colors = card.get("colors", [])
        print(
            f"  {cnt:>2} {float(cmc):>3.0f}  "
            f"{pad(tint_colors(colors), ''.join(colors), 5)} {card.get(constants.DATA_FIELD_NAME)}"
        )
    if lands:
        land_str = ", ".join(
            f"{c.get(constants.DATA_FIELD_COUNT, 1)} {c.get(constants.DATA_FIELD_NAME)}"
            for c in lands
        )
        print(f"  lands: {land_str}")
    sideboard = best.get("sideboard_cards") or []
    if sideboard:
        sb = ", ".join(c.get(constants.DATA_FIELD_NAME) for c in sideboard[:15])
        print(tint(f"  sideboard: {sb}", DIM))
    if len(labels) > 1:
        print(tint("  other builds: " + "  |  ".join(labels[1:5]), DIM))


def state_key(snap):
    """Identity of the pack on screen; a REFRESH only re-prints when it changes.

    Keyed on the pick slot and its cards only -- deliberately NOT picked_cards
    or filter. Making a pick marks the card [taken] and recomputes deck colors,
    which would otherwise re-print the very same pack a second time.
    """
    names = tuple(sorted(c.get(constants.DATA_FIELD_NAME, "") for c in snap["pack_cards"]))
    return (snap["event_set"], snap["pack"], snap["pick"], names)


def watch(config, scanner, opts, last_state):
    """Follow the log; `last_state` is the state main() already rendered."""
    orchestrator = DraftOrchestrator(scanner, config, lambda: None)
    orchestrator.start()

    interactive = sys.stdin.isatty() and termios is not None
    old_attrs = None
    try:
        if interactive and termios is not None and tty is not None:
            old_attrs = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            print("keys: [p]icks  [c]olor signals  [d]eck build  [r]eprint pack  [q]uit")

        while True:
            refresh = False
            try:
                message = orchestrator.update_queue.get(timeout=0.25)
                while True:
                    if isinstance(message, dict) and "status" in message:
                        say(message["status"])
                    elif message == "REFRESH":
                        refresh = True
                    try:
                        message = orchestrator.update_queue.get_nowait()
                    except queue.Empty:
                        break
            except queue.Empty:
                pass

            if refresh:
                snap = snapshot(scanner, config, opts)
                key = state_key(snap)
                if key != last_state:
                    last_state = key
                    render_pack(snap, opts)

            if interactive:
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if ready:
                    char = sys.stdin.read(1).lower()
                    if char == "q":
                        break
                    if char == "p":
                        render_picks(snapshot(scanner, config, opts), opts)
                    elif char == "c":
                        render_signals(snapshot(scanner, config, opts))
                    elif char == "d":
                        render_deck(snapshot(scanner, config, opts), opts, config)
                    elif char == "r":
                        snap = snapshot(scanner, config, opts)
                        last_state = state_key(snap)
                        render_pack(snap, opts)
    except KeyboardInterrupt:
        pass
    finally:
        if old_attrs is not None and termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        orchestrator.stop()
        orchestrator.join(timeout=2)
    print("\nbye")


def main():
    parser = argparse.ArgumentParser(description="MTGA draft pick suggestions in the console")
    parser.add_argument("-f", "--file", help="Path to Player.log (default: auto-detect)")
    parser.add_argument("-d", "--data", help="Path to the MTGA_Data directory")
    parser.add_argument("--once", action="store_true", help="Print current draft state and exit")
    parser.add_argument("--deck", action="store_true", help="Build a suggested deck from the pool, then exit")
    parser.add_argument("--no-sync", action="store_true", help="Skip the dataset cloud sync")
    parser.add_argument("--filter", help="Deck color filter (e.g. 'All Decks', 'Auto', 'WU')")
    parser.add_argument(
        "--format",
        choices=[
            constants.RESULT_FORMAT_GRADE,
            constants.RESULT_FORMAT_WIN_RATE,
            constants.RESULT_FORMAT_RATING,
        ],
        help="Rating display format",
    )
    args = parser.parse_args()
    opts = argparse.Namespace(
        deck_filter=args.filter or constants.DECK_FILTER_DEFAULT,
        result_format=args.format or constants.RESULT_FORMAT_GRADE,
    )

    config, scanner = bootstrap(args)

    snap = snapshot(scanner, config, opts)
    render_pack(snap, opts)
    if snap["taken_cards"]:
        render_picks(snap, opts)

    if args.deck:
        render_deck(snap, opts, config)
        return

    if not args.once:
        # seed dedupe from the exact state just rendered, so a pack that
        # arrives between this render and the watch loop still prints
        watch(config, scanner, opts, state_key(snap))


if __name__ == "__main__":
    main()
