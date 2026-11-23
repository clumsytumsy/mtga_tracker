#!/usr/bin/env python3
"""
MTGA match tracker (v2: deck + format).

Watches Arena's Player.log, records every finished match to a CSV (with the
deck you played and the format), and rebuilds a PNG report plus a per-deck
summary CSV after each match.

Setup:
  1. In Arena: Options -> Account -> enable "Detailed Logs (Plugin Support)".
  2. pip install matplotlib pandas
  3. py mtga_tracker.py        (leave running while you play)

How the deck is detected:
  Arena logs your deck lists and, at the start of each game, the exact cards
  submitted. The script matches those cards to your saved decks to get the name.
  If detection fails, it falls back to the text in current_deck.txt (put your
  deck's name in that file; edit it whenever you switch decks), else "Unknown".

Flags:
  --player NAME   your Arena screen name (auto-detected if omitted)
  --log PATH      override the Player.log location
  --no-follow     process the existing log once and exit
  --report-only   rebuild report + summary from the CSV and exit

NOTE: Arena's log format is not a public API and can change.
"""
import argparse
import csv
import json
import platform
import re
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

FIELDS = ["match_id", "timestamp", "event", "format", "deck", "opponent",
          "result", "games_won", "games_lost"]
UNKNOWN = "Unknown"
# Output files live next to this script, no matter which folder you launch it from
SCRIPT_DIR = Path(__file__).resolve().parent

# Checked in order against the event id (case-insensitive)
FORMAT_TOKENS = [("brawl", "Brawl"), ("timeless", "Timeless"),
                 ("alchemy", "Alchemy"), ("explorer", "Explorer"),
                 ("historic", "Historic"), ("pauper", "Pauper"),
                 ("standard", "Standard"), ("draft", "Draft"),
                 ("sealed", "Sealed"), ("cube", "Cube")]


def default_log_path():
    home = Path.home()
    if platform.system() == "Darwin":
        return home / "Library/Logs/Wizards Of The Coast/MTGA/Player.log"
    return home / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"


def infer_format(event_id, deck_format):
    low = (event_id or "").lower()
    for token, name in FORMAT_TOKENS:
        if token in low:
            return name
    if deck_format:
        return str(deck_format).replace("_", " ").title()
    if low in ("ladder", "play", "traditional_ladder", "traditional_play"):
        return "Standard"  # Arena's default constructed queues
    return UNKNOWN


# --------------------------------------------------------------------------
# CSV storage (whole file is rewritten on change; it's small)
# --------------------------------------------------------------------------
REQUIRED = ["match_id", "timestamp", "opponent", "result"]
HEADER_LINE = ",".join(FIELDS)


def header_problem(csv_path, columns):
    missing = [c for c in REQUIRED if c not in columns]
    if not missing:
        return None
    return (f"{csv_path} is missing its header row (or has the wrong columns: "
            f"{list(columns)[:4]}...). Open it in Notepad and make sure the very "
            f"first line is exactly:\n{HEADER_LINE}\n"
            "(if your first line is a match, add that header line above it). "
            "Nothing was changed.")


def load_rows(csv_path):
    rows = OrderedDict()
    if not csv_path.exists():
        return rows
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        problem = header_problem(csv_path, reader.fieldnames or [])
        if problem and (reader.fieldnames or csv_path.stat().st_size > 0):
            raise SystemExit(problem)
        for r in reader:
            row = {k: (r.get(k) or "") for k in FIELDS}
            row["deck"] = row["deck"] or UNKNOWN      # migrates old v1 files
            row["format"] = row["format"] or UNKNOWN
            rows[row["match_id"]] = row
    return rows


def save_rows(csv_path, rows):
    tmp = csv_path.with_suffix(".tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows.values())
        tmp.replace(csv_path)
    except PermissionError:
        print(f"Couldn't write {csv_path} (open in Excel?). Close it; "
              "it will be saved after the next match.")


def load_seen(path):
    try:
        return {l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip()}
    except OSError:
        return set()


def save_seen(path, ids):
    try:
        path.write_text("\n".join(sorted(ids)), encoding="utf-8")
    except OSError:
        pass


def load_decks(path):
    decks = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in data.items():
            decks[k] = (v["name"],
                        Counter({int(c): q for c, q in v["cards"].items()}),
                        v.get("format", ""))
    except (OSError, ValueError, KeyError):
        pass
    return decks


def save_decks(path, decks):
    data = {k: {"name": n, "format": f, "cards": {str(c): q for c, q in cnt.items()}}
            for k, (n, cnt, f) in decks.items()}
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Log parsing
# --------------------------------------------------------------------------
START = re.compile(r"\[\{|\{|^\s*\[\s*$")
TS = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)?")


def parse_ts(line):
    """Pull a log header timestamp like '10/24/2026 9:30:03 PM' (M/D/Y)."""
    m = TS.search(line)
    if not m:
        return None
    a, b, y, hh, mm, ss, ap = m.groups()
    mon, day, hh = int(a), int(b), int(hh)
    if mon > 12:  # D/M/Y locale
        mon, day = day, mon
    if ap == "PM" and hh < 12:
        hh += 12
    if ap == "AM" and hh == 12:
        hh = 0
    try:
        return f"{int(y):04d}-{mon:02d}-{day:02d} {hh:02d}:{mm}:{ss}"
    except ValueError:
        return None


class LogParser:
    """Turns raw log lines into JSON objects, handling multi-line payloads."""

    def __init__(self):
        self.buf = []
        self.fails = 0
        self.samples = []

    @staticmethod
    def wanted(text):
        return ("matchGameRoomStateChangedEvent" in text
                or "authenticateResponse" in text
                or "deckMessage" in text
                or "CourseDeckSummary" in text
                or '"mainDeck"' in text)

    def feed(self, line):
        out = []
        if not line.strip():
            return out

        if self.buf:
            self.buf.append(line)
            # A top-level pretty-printed blob closes at column 0
            if line.rstrip("\r\n") in ("}", "]"):
                text = "".join(self.buf)
                self.buf = []
                if self.wanted(text):
                    obj = self._try(text)
                    if obj is not None:
                        out.append(obj)
            elif len(self.buf) > 20000:  # runaway protection
                self.buf = []
            return out

        m = START.search(line)
        if m:
            chunk = line[m.start():]
            if self.wanted(line):
                obj = self._try(chunk)
                if obj is not None:
                    out.append(obj)
                    return out
            if chunk.rstrip().endswith(("{", "[")):
                self.buf = [chunk]  # start of a multi-line blob
        return out

    def _try(self, text):
        t = text.lstrip()
        try:
            obj, _ = json.JSONDecoder().raw_decode(t)  # tolerates trailing text
            return obj
        except ValueError:
            self.fails += 1
            if len(self.samples) < 3:
                self.samples.append(t[:150] + " ... " + t[-100:])
            return None


def walk(o):
    """Yield every dict nested anywhere in a JSON value."""
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def deck_counter(entries):
    c = Counter()
    for e in entries or []:
        if isinstance(e, dict):
            cid = e.get("cardId", e.get("grpId", e.get("id")))
            qty = e.get("quantity", 1)
            if isinstance(cid, int) and isinstance(qty, int):
                c[cid] += qty
    return c


class Tracker:
    def __init__(self, csv_path, deck_file, player=None, on_change=None,
                 rebuild=False):
        self.csv_path = csv_path
        self.deck_file = deck_file
        self.player = player
        self.on_change = on_change
        self.rows = load_rows(csv_path)
        # Every match ever recorded. A match in this set but missing from the CSV
        # was deleted on purpose, so re-reading the log must not bring it back.
        self.seen_path = csv_path.with_name("mtga_seen.txt")
        self.seen = load_seen(self.seen_path) | set(self.rows)
        self.rebuild = rebuild
        save_seen(self.seen_path, self.seen)
        self.parser = LogParser()
        self.decks_path = csv_path.with_name("mtga_decks.json")
        self.decks = load_decks(self.decks_path)  # remembered across sessions
        self.decks_dirty = False
        self.match_events = {}   # match id -> event id seen earlier in the match
        self.stats = Counter()
        self.match_deck = None   # Counter of cards submitted for current match
        self.course_deck = None  # (name, format) of the deck queued for a match
        self.last_ts = None      # most recent timestamp seen in a log header

    def refresh_rows(self):
        """Re-read the CSV so edits made while the script runs (deleted or
        corrected rows) are respected instead of being overwritten."""
        try:
            self.rows = load_rows(self.csv_path)
            return True
        except SystemExit as e:
            print(f"{e}\nSkipping this save; fix the CSV and restart the script.")
            return False

    def register_deck(self, key, name, counter, fmt):
        new = (name, counter, fmt)
        if self.decks.get(key) != new:
            self.decks[key] = new
            self.decks_dirty = True

    def process_line(self, line):
        if line.startswith("[") and "/" in line[:60]:
            ts = parse_ts(line[:80])
            if ts:
                self.last_ts = ts
        for obj in self.parser.feed(line):
            self.handle_entry(obj)

    # -- deck helpers -------------------------------------------------------
    def lookup_deck(self):
        if not self.match_deck:
            return None
        total = sum(self.match_deck.values())
        best, best_score = None, 0.0
        for name, sig, fmt in self.decks.values():
            denom = max(total, sum(sig.values())) or 1
            score = sum((self.match_deck & sig).values()) / denom
            if score > best_score:
                best, best_score = (name, fmt), score
        return best if best_score >= 0.85 else None

    def fallback_deck(self):
        try:
            text = self.deck_file.read_text(encoding="utf-8").strip()
            return text or UNKNOWN
        except OSError:
            return UNKNOWN

    # -- main handler -------------------------------------------------------
    def handle_entry(self, obj):
        auth = obj.get("authenticateResponse") if isinstance(obj, dict) else None
        if auth and not self.player:
            self.player = auth.get("screenName") or self.player

        for d in walk(obj):
            if "CourseDeckSummary" in d and "CourseDeck" in d:
                summ, cdeck = d["CourseDeckSummary"], d["CourseDeck"]
                if isinstance(summ, dict) and isinstance(cdeck, dict):
                    attrs = {a.get("name"): a.get("value")
                             for a in summ.get("Attributes", [])
                             if isinstance(a, dict)}
                    name = summ.get("Name") or UNKNOWN
                    fmt_attr = str(attrs.get("Format", "")).strip('"')
                    self.register_deck(summ.get("DeckId") or name, name,
                                       deck_counter(cdeck.get("MainDeck")), fmt_attr)
                    self.stats["course_decks"] += 1
                    if d.get("CurrentModule") == "CreateMatch":
                        self.course_deck = (name, fmt_attr)
            if "mainDeck" in d and "name" in d:
                self.register_deck(d.get("id") or d["name"], d["name"],
                                   deck_counter(d["mainDeck"]), d.get("format", ""))
                self.stats["deck_lists"] += 1
            dm = d.get("deckMessage")
            if isinstance(dm, dict) and dm.get("deckCards"):
                self.match_deck = Counter(
                    c for c in dm["deckCards"] if isinstance(c, int))
                self.stats["deck_messages"] += 1

        if self.decks_dirty:
            save_decks(self.decks_path, self.decks)
            self.decks_dirty = False

        ev = obj.get("matchGameRoomStateChangedEvent") if isinstance(obj, dict) else None
        if not ev:
            return
        info = ev.get("gameRoomInfo", {})
        cfg = info.get("gameRoomConfig", {})
        eid = cfg.get("eventId") or next(
            (p.get("eventId") for p in cfg.get("reservedPlayers", [])
             if p.get("eventId")), "")
        if eid and cfg.get("matchId"):
            self.match_events[cfg["matchId"]] = eid
        result = info.get("finalMatchResult")
        if not result:
            return
        self.stats["finished_matches"] += 1

        config = info.get("gameRoomConfig", {})
        match_id = result.get("matchId") or config.get("matchId")
        if not match_id:
            return

        found = self.lookup_deck() or self.course_deck
        det_deck, det_fmt = found if found else (UNKNOWN, "")
        players = config.get("reservedPlayers", [])
        event_id = (config.get("eventId")
                    or next((p.get("eventId") for p in players
                             if p.get("eventId")), ""))
        event_id = event_id or self.match_events.get(match_id, "")
        fmt = infer_format(event_id, det_fmt)
        ts = self.last_ts or time.strftime("%Y-%m-%d %H:%M:%S")

        if not self.refresh_rows():
            return
        existing = self.rows.get(match_id)
        if existing:
            changed = False
            if self.last_ts and existing["timestamp"] != ts:
                existing["timestamp"], changed = ts, True
            if not existing["event"] and event_id:
                existing["event"], changed = event_id, True
            if existing["deck"] == UNKNOWN and det_deck != UNKNOWN:
                existing["deck"], changed = det_deck, True
            if existing["format"] == UNKNOWN and fmt != UNKNOWN:
                existing["format"], changed = fmt, True
            if changed:
                save_rows(self.csv_path, self.rows)
                if self.on_change:
                    self.on_change()
            self.match_deck = self.course_deck = None
            return

        if match_id in self.seen and not self.rebuild:
            self.match_deck = self.course_deck = None
            return  # you deleted this row on purpose; keep it deleted

        me = next((p for p in players if p.get("playerName") == self.player), None)
        if me is None:
            print("Couldn't identify you in this match; pass --player NAME.")
            return
        opp = next((p for p in players if p is not me), {})
        my_team = me.get("teamId")

        results = result.get("resultList", [])
        match_res = next((r for r in results if r.get("scope") == "MatchScope_Match"),
                         results[-1] if results else {})
        won = match_res.get("winningTeamId") == my_team
        games = [r for r in results if r.get("scope") == "MatchScope_Game"]
        gw = sum(1 for g in games if g.get("winningTeamId") == my_team)

        deck = det_deck if det_deck != UNKNOWN else self.fallback_deck()
        row = {
            "match_id": match_id,
            "timestamp": ts,
            "event": event_id,
            "format": fmt,
            "deck": deck,
            "opponent": opp.get("playerName", ""),
            "result": "win" if won else "loss",
            "games_won": gw,
            "games_lost": len(games) - gw,
        }
        self.rows[match_id] = row
        save_rows(self.csv_path, self.rows)
        self.seen.add(match_id)
        save_seen(self.seen_path, self.seen)
        self.match_deck = self.course_deck = None
        print(f"[{row['timestamp']}] {row['result'].upper()} | {deck} | {fmt} | "
              f"vs {row['opponent']}")
        if self.on_change:
            self.on_change()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def print_diagnostics(tracker):
    st = tracker.stats
    print("\n--- Diagnostics ---")
    print(f"Finished matches seen in log:     {st['finished_matches']}")
    print(f"Deck blocks parsed (Course):      {st['course_decks']}")
    print(f"Deck lists parsed (other format): {st['deck_lists']}")
    print(f"Game-start card lists parsed:     {st['deck_messages']}")
    print(f"Decks remembered: {[d[0] for d in tracker.decks.values()]}")
    print(f"Relevant lines that failed to parse: {tracker.parser.fails}")
    for smp in tracker.parser.samples:
        print("  sample:", smp.replace("\n", " ")[:260])
    print("(remove names before sharing any of this)")


def build_report(csv_path, png_path, verbose=False):
    try:
        import pandas as pd
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"Couldn't load the report libraries: {e}")
        print(f"This script is running on: {sys.executable}")
        print(f'To install into exactly that Python, run:  "{sys.executable}" '
              "-m pip install pandas matplotlib")
        return
    if not csv_path.exists():
        print("No matches recorded yet.")
        return

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False,
                     encoding="utf-8-sig")
    problem = header_problem(csv_path, df.columns)
    if problem:
        print(problem)
        return
    if df.empty:
        print("No matches in the CSV yet.")
        return
    for col in ("deck", "format", "event"):
        if col not in df.columns:
            df[col] = UNKNOWN if col != "event" else ""
    # Coerce so one blank or odd timestamp can't break the whole report
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("timestamp")
    df["win"] = (df["result"] == "win").astype(int)

    # Per deck x format summary table
    summary = (df.groupby(["deck", "format"])
                 .agg(matches=("win", "size"), wins=("win", "sum"))
                 .reset_index())
    summary["losses"] = summary["matches"] - summary["wins"]
    summary["win_pct"] = (summary["wins"] / summary["matches"] * 100).round(1)
    summary = summary.sort_values(["matches", "win_pct"], ascending=False)
    summary.to_csv(png_path.with_name("mtga_summary.csv"), index=False)
    if verbose:
        print(summary.to_string(index=False))

    fig, ax = plt.subplots(2, 2, figsize=(14, 9))

    # 1. Win rate over time
    x = range(1, len(df) + 1)
    ax[0, 0].plot(x, df["win"].expanding().mean() * 100, label="Overall")
    ax[0, 0].plot(x, df["win"].rolling(20, min_periods=5).mean() * 100,
                  label="Last 20", alpha=0.8)
    ax[0, 0].axhline(50, color="gray", ls="--", lw=0.8)
    ax[0, 0].set(title="Win rate over time", xlabel="Match #", ylabel="Win %")
    ax[0, 0].legend()

    # 2. Win % by deck
    by_deck = df.groupby("deck")["win"].agg(["mean", "count"])
    by_deck = by_deck.sort_values("count", ascending=False).head(10)
    by_deck = by_deck.sort_values("mean")
    labels = [f"{d} (n={int(n)})" for d, n in zip(by_deck.index, by_deck["count"])]
    ax[0, 1].barh(labels, by_deck["mean"] * 100)
    ax[0, 1].axvline(50, color="gray", ls="--", lw=0.8)
    ax[0, 1].set(title="Win % by deck (top 10 by matches)", xlabel="Win %",
                 xlim=(0, 100))

    # 3. Win % by format
    by_fmt = df.groupby("format")["win"].agg(["mean", "count"]).sort_values("mean")
    labels = [f"{f} (n={int(n)})" for f, n in zip(by_fmt.index, by_fmt["count"])]
    ax[1, 0].barh(labels, by_fmt["mean"] * 100)
    ax[1, 0].axvline(50, color="gray", ls="--", lw=0.8)
    ax[1, 0].set(title="Win % by format", xlabel="Win %", xlim=(0, 100))

    # 4. Matches per day
    daily = df.groupby(df["timestamp"].dt.date)["win"].agg(["sum", "count"])
    days = daily.index.astype(str)
    ax[1, 1].bar(days, daily["count"] - daily["sum"], label="Losses")
    ax[1, 1].bar(days, daily["sum"], bottom=daily["count"] - daily["sum"],
                 label="Wins")
    ax[1, 1].set(title="Matches per day")
    ax[1, 1].tick_params(axis="x", rotation=45)
    ax[1, 1].legend()

    total, wins = len(df), int(df["win"].sum())
    fig.suptitle(f"MTGA: {wins}-{total - wins}  ({wins / total:.0%})")
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    print(f"Report updated: {png_path} (+ mtga_summary.csv)")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=default_log_path())
    ap.add_argument("--csv", type=Path, default=SCRIPT_DIR / "mtga_matches.csv")
    ap.add_argument("--report", type=Path, default=SCRIPT_DIR / "mtga_report.png")
    ap.add_argument("--deck-file", type=Path, default=SCRIPT_DIR / "current_deck.txt")
    ap.add_argument("--player")
    ap.add_argument("--no-follow", action="store_true")
    ap.add_argument("--diagnose", action="store_true",
                    help="process the log once and print what was/wasn't found")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--drop-deck", action="append", metavar="NAME",
                    help="delete all rows with this deck name (repeatable, e.g. "
                         "--drop-deck Unknown) and keep them deleted")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-add matches from the log even if you deleted them")
    args = ap.parse_args()

    if args.drop_deck:
        rows = load_rows(args.csv)
        seen_path = args.csv.with_name("mtga_seen.txt")
        seen = load_seen(seen_path) | set(rows)
        drop = {d.lower() for d in args.drop_deck}
        keep = OrderedDict((k, v) for k, v in rows.items()
                           if v["deck"].lower() not in drop)
        save_rows(args.csv, keep)
        save_seen(seen_path, seen)
        print(f"Removed {len(rows) - len(keep)} rows; {len(keep)} remain.")
        build_report(args.csv, args.report, verbose=True)
        return
    if args.report_only:
        build_report(args.csv, args.report, verbose=True)
        return
    if not args.log.exists():
        raise SystemExit(f"Log not found: {args.log} (use --log)")

    def safe_report():
        try:
            build_report(args.csv, args.report)
        except Exception as e:  # a chart problem must never stop match tracking
            print(f"Report failed ({type(e).__name__}: {e}); "
                  "matches are still being recorded.")

    tracker = Tracker(args.csv, args.deck_file, args.player, on_change=safe_report,
                      rebuild=args.rebuild)

    size_mb = args.log.stat().st_size / 1e6
    print(f"Saving data to: {args.csv.parent}", flush=True)
    print(f"Reading existing log ({size_mb:.1f} MB): {args.log}", flush=True)
    pos = 0
    with open(args.log, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            tracker.process_line(line)
        pos = f.tell()
    if args.no_follow or args.diagnose:
        build_report(args.csv, args.report, verbose=True)
        if args.diagnose:
            print_diagnostics(tracker)
        return

    safe_report()  # refresh the chart on every start, even if nothing new was found
    print("Watching for new matches ... (Ctrl+C to stop)", flush=True)
    while True:
        try:
            if args.log.stat().st_size < pos:  # log rotated on Arena restart
                pos = 0
                tracker.parser = LogParser()
            with open(args.log, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(pos)
                for line in f:
                    tracker.process_line(line)
                pos = f.tell()
        except FileNotFoundError:
            pass
        time.sleep(1)


if __name__ == "__main__":
    main()
