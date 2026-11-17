#!/usr/bin/env python3
"""
Chess Olympiad database pipeline (2010-2026, Open and Women's sections).

Sources
  * The Week in Chess (TWIC): complete game files for every Olympiad, with
    teams, boards, ratings, FIDE IDs, results and the official moves. No clock times.
  * Lichess broadcasts (2018, 2022, 2024, 2026): per-move clock readings
    (%clk) and engine evaluations (%eval) for most games.

The pipeline downloads both and stores TWIC as the master list of games and moves.
Each Lichess game is matched to a TWIC game by its moves, and its clock readings and
evaluations are attached to the official moves.

Usage
  python olympiad_pipeline.py download                # fetch raw PGN files into ./data/raw
  python olympiad_pipeline.py load --dsn postgresql://user:pass@localhost:5432/chess_olympiad
  python olympiad_pipeline.py all  --dsn ...           # download + load
  python olympiad_pipeline.py summary --dsn ...        # coverage table per Olympiad
  python olympiad_pipeline.py export --dsn ...         # SQLite file and SQL dump in ./data/export

The DSN can also be given via the DATABASE_URL environment variable.
2020 and 2021 are skipped: those were online rapid Olympiads.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

DATA_DIR = Path(os.environ.get("OLYMPIAD_DATA_DIR", "data/raw"))
EXPORT_DIR = Path("data/export")
USER_AGENT = "olympiad-thesis-pipeline (academic research; contact via IT:U)"

# ---------------------------------------------------------------------------
# Source catalogue
# ---------------------------------------------------------------------------
TWIC_BASE = "https://theweekinchess.com/assets/files/pgn/"
TWIC_FILES = {  # (year, section) -> file name
    (2010, "open"): "olymm10.pgn", (2010, "women"): "olymw10.pgn",
    (2012, "open"): "olymm12.pgn", (2012, "women"): "olymw12.pgn",
    (2014, "open"): "olym14.pgn",  (2014, "women"): "olymw14.pgn",
    (2016, "open"): "olym16.pgn",  (2016, "women"): "olymw16.pgn",
    (2018, "open"): "olym18.pgn",  (2018, "women"): "olymw18.pgn",
    (2022, "open"): "olym22.pgn",  (2022, "women"): "olymw22.pgn",
    (2024, "open"): "olym24.pgn",  (2024, "women"): "olymw24.pgn",
    (2026, "open"): "olym26.pgn",  (2026, "women"): "olymw26.pgn",
}
EVENT_NAMES = {
    2010: "39th Chess Olympiad, Khanty-Mansiysk", 2012: "40th Chess Olympiad, Istanbul",
    2014: "41st Chess Olympiad, Tromsø", 2016: "42nd Chess Olympiad, Baku",
    2018: "43rd Chess Olympiad, Batumi", 2022: "44th Chess Olympiad, Chennai",
    2024: "45th Chess Olympiad, Budapest", 2026: "46th Chess Olympiad, Samarkand",
}

LICHESS_BASE = "https://lichess.org/api/broadcast/"
# Lichess broadcast tour ids, (year, section) -> [ids]. Each tour is exported whole.
LICHESS_TOURS = {
    (2018, "open"): [
        "2gdiULhO", "f4q1thGe", "Tp8UNVjh", "Ll76dJZT",                                   # Rd 1-4
        "j0zqppxH", "9CtXnsyS", "BOxvvoTu", "kwXfa0lf", "kMRMucnI", "Li51Ypzh",           # Rd 5
        "HXC1b88y", "iC8pHi1k", "pjBjP3Zc", "pkaxDaJ2", "JCAHZWib", "f5B9Wp5J",           # Rd 6
        "3hy44TnC", "H5jMwp0X", "CPnHhArn", "SQ6C7qyT", "uWtkOHRs", "0aoqVmaY",           # Rd 7-8
        "GdpEqg9i", "LKG1oiE8", "prJl6Mk9", "QaSEkdCL", "zV19UAHV", "d4sdFnEt",           # Rd 9-10
        "FEX44Hv7", "d5EIduys", "vJR1hdej",                                               # Rd 11
    ],
    (2018, "women"): [
        "FXGOzZrU", "HEoI2Krt", "IkvPN9iW", "iPUcYWX8",                                   # Rd 1-4
        "5FzmqBcV", "MQuEGnGP", "KuPQpvY1", "cpmlG2a2", "DbZNesxr",                       # Rd 5
        "86aHXWVT", "u3wl2Fhq", "XOVCLJaW", "KwLYN1QJ", "3mg6nj6f",                       # Rd 6
        "E62hJbsP", "Fi5frNfp", "XGrvo93U", "AsHjYVli", "kHOjuR9k", "vY9gOfpc",           # Rd 7-8
        "OqVJEmee", "y7dd7qjb", "pn8NuVcm", "qAisKIvj", "VXFV1LkE", "P62bdui7",           # Rd 9-10
        "ynId9Iib", "lo6v0cul", "RI4HJLvo",                                               # Rd 11
    ],
    (2022, "open"): ["41uXOrqR", "boYEDCA3", "T7BhMqWu", "ogDJ96Ni"],
    (2022, "women"): ["evAN0x2E", "wtfmQnTj", "JsJYFuaf"],
    (2024, "open"): ["wYigbpXq", "M5YHvpOX", "q6ezoCXP", "jCcYzv6v", "V3en3lb9"],
    (2024, "women"): ["SjZWjkng", "aZ8unZw3", "9SgOOjEo", "b1XsWkeS"],
    (2026, "open"): ["n1pPI5Q0", "MSQXIzkK", "Uvg2Lyjt", "sfWvd9Pd", "MyBFv3Ha"],
    (2026, "women"): ["HtMn014k", "UjEVXNHv", "JyQqARgN", "oQuU2arG"],
}

# Olympiad time control (all years): 90 min for 40 moves, +30 min, +30 s per move from move 1.
DEFAULT_BASE_S, DEFAULT_INC_S, DEFAULT_BONUS_S, BONUS_MOVE = 5400, 30, 1800, 40
BONUS_EARLIEST_MOVE = 30   # a clock that counts moves can add the bonus a few moves early
JUMP_TOLERANCE_S = 30      # a clock may read this much above what is expected before it counts as a jump
SMALL_ADDITION_S = 150     # arbiters add 2 minutes after an illegal move; steps up to this size count as time granted
STUCK_RUN = 4              # the same reading this many times in a row for one player: the clock was not transmitting
LONGEST_THINK_S = 2700     # a single move that takes longer than this is treated as an error in the readings
MOVE_KEY_PLIES = 30        # half-moves compared when a Lichess game is matched to a TWIC game by its moves
BOARDS = (1, 2, 3, 4)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _get(url: str, *, retries: int = 8) -> bytes:
    import requests
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(30, 1800))
        except requests.RequestException as e:
            wait = 10 * (attempt + 1)
            print(f"  network error ({e}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 200:
            return r.content
        if r.status_code == 429:  # Lichess rate limit: wait a full minute
            print("  rate limited; waiting 65s", file=sys.stderr)
            time.sleep(65)
            continue
        if r.status_code == 404:
            raise FileNotFoundError(url)
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"giving up on {url}")


def _is_pgn(data: bytes) -> bool:
    return data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"[Event ")


def _fetch(url: str, out: Path) -> bool:
    """Download url to out. The file appears only when the download is complete and is a PGN file."""
    data = _get(url)
    if not _is_pgn(data):
        print(f"  {out.name}: the server did not return a PGN file; nothing saved", file=sys.stderr)
        return False
    part = out.with_name(out.name + ".part")
    part.write_bytes(data)
    part.replace(out)
    return True


def write_manifest(data_dir: Path = DATA_DIR) -> Path:
    """Record size, SHA-256 and fetch time of every raw file in data_dir/manifest.json.

    Lichess files can change after the event (evaluations are added later), so the manifest
    is what identifies the exact data a database was built from."""
    path = data_dir / "manifest.json"
    old = json.loads(path.read_text()).get("files", {}) if path.exists() else {}
    files = {}
    for f in sorted(data_dir.glob("*/*.pgn")):
        rel = f.relative_to(data_dir).as_posix()
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        known = old.get(rel)
        fetched = (known["fetched"] if known and known["sha256"] == digest
                   else datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"))
        files[rel] = {"bytes": f.stat().st_size, "sha256": digest, "fetched": fetched}
    path.write_text(json.dumps({"files": files}, indent=1) + "\n")
    return path


def download(data_dir: Path = DATA_DIR, force: bool = False, years: list[int] | None = None) -> None:
    (data_dir / "twic").mkdir(parents=True, exist_ok=True)
    (data_dir / "lichess").mkdir(parents=True, exist_ok=True)
    for (year, section), fname in TWIC_FILES.items():
        if years and year not in years:
            continue
        out = data_dir / "twic" / fname
        if out.exists() and not force:
            continue
        print(f"TWIC {year} {section}: {fname}")
        _fetch(TWIC_BASE + fname, out)
        time.sleep(1)
    for (year, section), ids in LICHESS_TOURS.items():
        if years and year not in years:
            continue
        for tid in ids:
            out = data_dir / "lichess" / f"{year}_{section}_{tid}.pgn"
            if out.exists() and not force:
                continue
            print(f"Lichess {year} {section}: tour {tid}")
            _fetch(f"{LICHESS_BASE}{tid}.pgn", out)
            time.sleep(3)  # be polite; the export endpoint is rate limited
    print(f"download complete; file list with checksums in {write_manifest(data_dir)}")


# ---------------------------------------------------------------------------
# PGN parsing (no external chess library needed)
# ---------------------------------------------------------------------------
_HEADER_RE = re.compile(r'^\[(\w+)\s+"(.*)"\]\s*$')
_TOKEN_RE = re.compile(
    r"\{(?P<comment>[^}]*)\}"          # comment
    r"|(?P<var>\()|(?P<varend>\))"       # variation brackets
    r"|(?P<nag>\$\d+)"
    r"|(?P<num>\d+\.(?:\.\.)?)"
    r"|(?P<result>1-0|0-1|1/2-1/2|\*)"
    r"|(?P<san>[A-Za-z][A-Za-z0-9+#=\-]*[?!]*|O-O(?:-O)?[+#]?[?!]*)"
)
_CLK_RE = re.compile(r"\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]")
_EVAL_RE = re.compile(r"\[%eval\s+(#?-?\d+(?:\.\d+)?)")


@dataclass
class Ply:
    san: str
    clock_s: int | None = None
    eval_cp: int | None = None
    eval_mate: int | None = None


@dataclass
class PgnGame:
    headers: dict
    plies: list = field(default_factory=list)
    raw: str = ""


def split_pgn(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").strip()
    if not text:
        return []
    return [g.strip() for g in re.split(r"\n\s*\n(?=\[Event )", text) if g.strip()]


def parse_game(raw: str) -> PgnGame:
    headers, body_lines = {}, []
    for line in raw.split("\n"):
        m = _HEADER_RE.match(line)
        if m and not body_lines:
            headers[m.group(1)] = m.group(2)
        elif line.strip() or body_lines:
            body_lines.append(line)
    body = " ".join(body_lines)
    plies: list[Ply] = []
    depth = 0
    for m in _TOKEN_RE.finditer(body):
        if m.group("var"):
            depth += 1
        elif m.group("varend"):
            depth = max(0, depth - 1)
        elif depth:
            continue
        elif m.group("san"):
            san = re.sub(r"[?!]+$", "", m.group("san"))
            plies.append(Ply(san))
        elif m.group("comment") is not None and plies:
            c = m.group("comment")
            p = plies[-1]
            if p.clock_s is None and (cm := _CLK_RE.search(c)):
                p.clock_s = int(cm.group(1)) * 3600 + int(cm.group(2)) * 60 + int(float(cm.group(3)))
            if p.eval_cp is None and p.eval_mate is None and (em := _EVAL_RE.search(c)):
                v = em.group(1)
                if v.startswith("#"):
                    p.eval_mate = int(v[1:])
                else:
                    p.eval_cp = int(round(float(v) * 100))
    return PgnGame(headers, plies, raw)


def parse_file(path: Path) -> list[PgnGame]:
    return [parse_game(g) for g in split_pgn(path.read_text(encoding="utf-8", errors="replace"))]


# ---------------------------------------------------------------------------
# Clock reconstruction
# ---------------------------------------------------------------------------
def parse_time_control(tc: str | None) -> tuple[int, int, int]:
    """'40/5400+30:1800+30' -> (5400, 30, 1800). Falls back to the Olympiad default."""
    if tc:
        m = re.match(r"\s*40/(\d+)\+(\d+):(\d+)", tc)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return DEFAULT_BASE_S, DEFAULT_INC_S, DEFAULT_BONUS_S


class PlyTime(NamedTuple):
    think_s: int | None       # time used for the move; None if unknown
    think_plies: int | None   # moves of this player the think time covers (>1 after missing readings)
    elapsed_s: int | None     # time since the round started
    bonus_added: bool         # the 30-minute bonus first shows on this reading
    time_left_s: int | None   # clock reading, made comparable around move 40 (see reconstruct_times)


NO_TIME = PlyTime(None, None, None, False, None)


@dataclass
class ClockStats:
    readings: int = 0       # readings used
    bad_readings: int = 0   # readings discarded: impossible values, or contradicted by the readings around them
    jumps: int = 0          # times a clock went up for no known reason and stayed up (both readings are kept)
    long_thinks: int = 0    # single moves that took longer than LONGEST_THINK_S according to the readings


def reconstruct_times(plies: list[Ply], tc: str | None = None) -> tuple[list[PlyTime], ClockStats]:
    """Timeline of a game from its clock readings (time left after each move).

    think    = time used since the same player's previous reading, increments included.
    elapsed  = time since the round started = time used by both players. One clock is always
               running, and elapsed is taken from the latest reading of each clock, so one wrong
               reading cannot shift the rest of the game.
    time_left = the reading, adjusted so that it means the same thing in every year: the time to
               reach move 40 before the player has made 40 moves, and the time for the rest of the
               game afterwards (see "bonus" below).

    Bonus. Players get 30 extra minutes after move 40. Clocks that count moves (2018) add them
    when move 40 is completed. Clocks that do not (2022 onwards) add them to BOTH clocks at the
    moment the first player's time runs out, whenever that is, and never if neither player runs out.
    The bonus is recognised when a clock goes up by more than 10 minutes from move 30 on, or, from
    move 40, when it goes up by less but the opponent has just received the bonus, or the player
    used more time than was left and the opponent's next reading goes up as well.

    Bad readings. 0:00:00 and values above what the time control allows are placeholders and are
    ignored. So are repeats of the same reading four or more times in a row: that clock was not
    being transmitted, and the next real reading gives the time used over the whole stretch.
    A reading is also ignored when the readings before and after it fit together without
    it but not with it. A clock that goes up and stays up for no known reason is counted in
    `jumps` and the think time of that move is unknown. A step of up to 2.5 minutes is treated as
    time granted (arbiters add 2 minutes after an illegal move).
    """
    base, inc, bonus = parse_time_control(tc)

    def possible(i: int) -> bool:
        move_no = i // 2 + 1
        most = base + inc * (move_no + 1) + (bonus if move_no >= BONUS_EARLIEST_MOVE else 0) + 60
        return 0 < plies[i].clock_s <= most

    dropped = {i for i, p in enumerate(plies) if p.clock_s is not None and not possible(i)}
    for side in (0, 1):  # a clock that shows the same time move after move is stuck: keep only the first of the run
        run: list[int] = []
        for i in list(range(side, len(plies), 2)) + [None]:
            c = plies[i].clock_s if i is not None else None
            if run and c is not None and c == plies[run[0]].clock_s:
                run.append(i)
                continue
            if len(run) >= STUCK_RUN:
                dropped.update(run[1:])
            run = [i] if c is not None and i not in dropped else []
    for _ in range(6):   # each pass drops the readings that the previous pass showed to be wrong
        times, blamed, jumps = _walk_clocks(plies, dropped, base, inc, bonus)
        if not blamed:
            break
        dropped |= blamed
    else:                # still not settled: keep what is left and count it as unexplained
        times, blamed, jumps = _walk_clocks(plies, dropped, base, inc, bonus)
        jumps += len(blamed)
    readings = sum(1 for t in times if t.elapsed_s is not None)
    long_thinks = sum(1 for t in times if t.think_s is not None and t.think_plies == 1 and t.think_s > LONGEST_THINK_S)
    return times, ClockStats(readings, len(dropped), jumps, long_thinks)


def _walk_clocks(plies, dropped, base, inc, bonus):
    """One pass over the readings that are not in `dropped`.
    Returns the per-ply times, the readings to drop next, and the number of unexplained jumps."""
    start = base + inc                    # clocks show base + increment before move 1
    order = ([], [])                      # per player: indices of the usable readings
    for i, p in enumerate(plies):
        if p.clock_s is not None and i not in dropped:
            order[i % 2].append(i)
    seen = [0, 0]                         # readings of each player consumed so far
    used = ([0], [0])                     # time used by each player after each consumed reading
    bonus_ply: list[int | None] = [None, None]
    added = [0, 0]                        # time put on each clock for no known reason (see `jumps`)

    def used_at(i: int, side: int) -> int:
        """Time the player has used when reading i is taken (given the time credited so far)."""
        granted = start + inc * (i // 2 + 1) + (bonus if bonus_ply[side] is not None else 0) + added[side]
        return granted - plies[i].clock_s

    out, blamed, jumps, elapsed = [], set(), 0, 0
    for i, p in enumerate(plies):
        side, move_no = i % 2, i // 2 + 1
        if p.clock_s is None or i in dropped:
            out.append(NO_TIME)
            continue
        k = seen[side]
        prev = order[side][k - 1] if k else None            # this player's previous reading
        raw = used_at(i, side) - used[side][-1]
        span = (i - prev) // 2 if prev is not None else move_no
        got_bonus = False
        if bonus_ply[side] is None and move_no >= BONUS_EARLIEST_MOVE and raw < -JUMP_TOLERANCE_S:
            other = 1 - side
            if raw < -600:
                got_bonus = True
            elif move_no >= BONUS_MOVE and bonus_ply[other] is not None:
                got_bonus = i - bonus_ply[other] <= 3       # both clocks receive it together
            elif move_no >= BONUS_MOVE:
                left = (plies[prev].clock_s if prev is not None else start) + inc * (span - 1)
                ran_out = raw + bonus >= left - 5
                nxt = order[other][seen[other]] if seen[other] < len(order[other]) else None
                got_bonus = ran_out and nxt is not None and used_at(nxt, other) - used[other][-1] < -JUMP_TOLERANCE_S
        if got_bonus:
            bonus_ply[side] = i
            raw += bonus
        think: int | None = max(0, raw)
        if raw < -JUMP_TOLERANCE_S:
            # The clock went up. Either the previous reading was too low, or this one is too high,
            # or time really was added. Drop a reading only if the others fit together without it.
            think = None
            nxt = order[side][k + 1] if k + 1 < len(order[side]) else None
            if prev is not None and prev != bonus_ply[side] and used_at(i, side) - used[side][-2] >= -JUMP_TOLERANCE_S:
                blamed.add(prev)
            elif nxt is not None and used_at(nxt, side) - used[side][-1] >= -JUMP_TOLERANCE_S:
                blamed.add(i)
            else:
                jumps += 1
                if -raw <= SMALL_ADDITION_S:
                    added[side] -= raw    # an arbiter's addition: time granted, so the timeline does not step back
                # a bigger step is more likely the end of a run of wrong readings: the timeline then
                # stands still until the clocks have caught up with it
        seen[side] += 1
        used[side].append(used_at(i, side))
        elapsed = max(elapsed, used[0][-1] + used[1][-1])
        has_bonus = bonus_ply[side] is not None
        if move_no >= BONUS_MOVE and not has_bonus:
            left_now = p.clock_s + bonus                    # entitled to the bonus, not yet on the clock
        elif move_no < BONUS_MOVE and has_bonus:
            left_now = max(0, p.clock_s - bonus)            # on the clock early, not yet earned
        else:
            left_now = p.clock_s
        out.append(PlyTime(think, span, elapsed, got_bonus, left_now))
    return out, blamed, jumps


# ---------------------------------------------------------------------------
# Matches: grouping TWIC games
# ---------------------------------------------------------------------------
RESULT_SCORE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _leading_int(v):
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else None


def twic_round_match_board(h: dict) -> tuple[int | None, int | None, int | None]:
    """TWIC uses two layouts: Round 'r.m' + Board 'b' (2012+) or Round 'r' + Board 'm.b' (2010).
    Board values such as '3 KS' occur; the leading number is the board."""
    rnd, brd = h.get("Round", ""), h.get("Board", "")
    if "." in brd:
        m, b = brd.split(".", 1)
        return _int(rnd.split(".")[0]), _int(m), _leading_int(b)
    if "." in rnd:
        r, m = rnd.split(".", 1)
        return _int(r), _int(m), _leading_int(brd)
    return _int(rnd), None, _leading_int(brd)


def build_matches(games: list[PgnGame]) -> tuple[list[dict], list[str]]:
    """Group TWIC games into team matches. Returns (matches, notes about repaired labels).

    A match is all games of a round between the same two teams. TWIC's table number is not
    used for grouping because it is sometimes wrong (in 2024 one game of table 18 carries
    table 70 in every round). The table number of a match is the one most of its games carry.

    Two other faults in the files are repaired, each with a note:
      * a game whose two teams are not opponents in that round (one team name is wrong) goes to
        the match of the team that still lacks a game, and takes that match's teams;
      * a game that is in the file twice for the same match is stored once, as the copy filed
        under the match's own table number.

    Boards. Team A has White on boards 1 and 3, team B on boards 2 and 4. A board number from
    the file is kept when it is 1-4, fits that pattern and is not already taken. Otherwise the
    board is inferred from the colours if exactly one board fits, and left empty if not.
    """
    notes: list[str] = []
    groups: dict[tuple, list[dict]] = {}
    for g in games:
        h = g.headers
        r, m, b = twic_round_match_board(h)
        if r is None:
            notes.append(f"no round number: {h.get('White')} - {h.get('Black')} (game not loaded)")
            continue
        rec = {"game": g, "table": m, "file_board": b, "board": None, "board_source": None,
               "white_team": h.get("WhiteTeam"), "black_team": h.get("BlackTeam")}
        teams = frozenset((rec["white_team"], rec["black_team"]))
        key = (r, teams) if len(teams) == 2 and all(teams) else (r, ("table", m))
        groups.setdefault(key, []).append(rec)

    # A team plays one match per round, so a small group that shares a team with a larger one has a wrong team name.
    plays: dict[tuple, list[tuple]] = {}
    for key in groups:
        if isinstance(key[1], frozenset):
            for team in key[1]:
                plays.setdefault((key[0], team), []).append(key)
    for key in list(groups):
        r, teams = key
        if not isinstance(teams, frozenset):
            continue
        for rec in list(groups[key]):
            homes = [k for team in teams for k in plays[(r, team)]
                     if k != key and len(groups[key]) < len(groups.get(k, ())) < len(BOARDS)]
            homes = [k for k in homes if any(x["table"] == rec["table"] for x in groups[k])] or homes
            if len(homes) != 1:
                continue
            home = homes[0]
            wrong_side, right_side = ("black_team", "white_team") if rec["white_team"] in home[1] else ("white_team", "black_team")
            wrong = rec[wrong_side]
            rec[wrong_side] = next(t for t in home[1] if t != rec[right_side])
            groups[key].remove(rec)
            groups[home].append(rec)
            hh = rec["game"].headers
            notes.append(f"round {r}: {hh.get('White')} - {hh.get('Black')}: the file gives {wrong!r} as a team, but the game "
                         f"belongs to the match {' v '.join(sorted(home[1]))}; team corrected")
        if not groups[key]:
            del groups[key]

    matches = []
    for (r, _), recs in groups.items():
        labels = collections.Counter(rec["table"] for rec in recs if rec["table"] is not None)
        match_no = min(labels, key=lambda m: (-labels[m], m)) if labels else None

        # The same game twice in one match: keep the copy filed under this match's table number
        first_copy: dict[tuple, dict] = {}
        unique = []
        for rec in sorted(recs, key=lambda rec: rec["table"] != match_no):
            h = rec["game"].headers
            pair = (h.get("White"), h.get("Black"))
            if all(pair) and pair in first_copy:
                kept = first_copy[pair]["game"].headers
                notes.append(f"round {r}: {pair[0]} - {pair[1]} is in the file twice; kept the copy under table "
                             f"{kept.get('Round')!r} ({kept.get('Result')}), left out the copy under table {h.get('Round')!r} ({h.get('Result')})")
                continue
            first_copy[pair] = rec
            unique.append(rec)
        recs = unique

        # Team A: the team that has White on the odd boards, by majority of the usable labels
        votes = collections.Counter()
        for rec in recs:
            if rec["file_board"] in BOARDS:
                votes[rec["white_team"] if rec["file_board"] % 2 else rec["black_team"]] += 1
        team_a = votes.most_common(1)[0][0] if votes else recs[0]["white_team"]
        team_b = next((t for rec in recs for t in (rec["white_team"], rec["black_team"]) if t != team_a), None)

        def fits(rec, b):
            return (rec["white_team"] == team_a) == (b % 2 == 1)

        taken: dict[int, dict] = {}
        for rec in recs:
            b = rec["file_board"]
            if b in BOARDS and fits(rec, b) and b not in taken:
                rec["board"], rec["board_source"] = b, "header"
                taken[b] = rec
        for rec in recs:
            if rec["board"] is None:
                free = [b for b in BOARDS if b not in taken and fits(rec, b)]
                h = rec["game"].headers
                who = f"round {r}, {team_a} v {team_b}: {h.get('White')} - {h.get('Black')}"
                if len(free) == 1:
                    rec["board"], rec["board_source"] = free[0], "inferred"
                    taken[free[0]] = rec
                    notes.append(f"{who}: board {free[0]} inferred (file says table {h.get('Round')!r}, board {h.get('Board')!r})")
                else:
                    notes.append(f"{who}: board unknown")
        recs.sort(key=lambda rec: rec["board"] or 99)
        score_a = score_b = 0.0
        for rec in recs:
            s = RESULT_SCORE.get(rec["game"].headers.get("Result"))
            if s is None:
                continue
            if rec["white_team"] == team_a:
                score_a, score_b = score_a + s, score_b + 1 - s
            else:
                score_a, score_b = score_a + 1 - s, score_b + s
        matches.append({"round": r, "match_no": match_no, "team_a": team_a, "team_b": team_b,
                        "score_a": score_a, "score_b": score_b, "games": recs,
                        "label_votes": labels[match_no] if match_no is not None else 0})

    # Table numbers must be unique within a round: the match with more games under a number keeps it.
    by_round: dict[int, list[dict]] = {}
    for mt in matches:
        by_round.setdefault(mt["round"], []).append(mt)
    for r, ms in by_round.items():
        used = set()
        spare = max([mt["match_no"] or 0 for mt in ms] + [0])
        for mt in sorted(ms, key=lambda mt: (mt["match_no"] is None, -mt["label_votes"], -len(mt["games"]))):
            if mt["match_no"] is None or mt["match_no"] in used:
                spare += 1
                notes.append(f"round {r}, {mt['team_a']} v {mt['team_b']}: table {mt['match_no']} is taken, numbered {spare}")
                mt["match_no"] = spare
            used.add(mt["match_no"])
    matches.sort(key=lambda mt: (mt["round"], mt["match_no"]))
    return matches, notes


# ---------------------------------------------------------------------------
# Matching Lichess games to TWIC games
# ---------------------------------------------------------------------------
def name_tokens(name: str | None) -> frozenset:
    if not name:
        return frozenset()
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return frozenset(t for t in re.split(r"[^a-z]+", s) if len(t) > 1)


def name_score(a: str | None, b: str | None) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _fide(v: str | None) -> int | None:
    return int(v) if v and v.strip().isdigit() and int(v) > 0 else None


def _plain(san: str) -> str:
    return san.rstrip("+#")


def common_plies(a: list[Ply], b: list[Ply]) -> int:
    """Number of half-moves at the start of two games that are the same (check marks ignored)."""
    n = 0
    for x, y in zip(a, b):
        if _plain(x.san) != _plain(y.san):
            break
        n += 1
    return n


def _same_players(lg: PgnGame, rec: dict) -> tuple[bool, float, float]:
    """(FIDE ids agree, White name agreement 0-1, Black name agreement 0-1) for a Lichess game and a TWIC record."""
    h = lg.headers
    wf, bf = _fide(h.get("WhiteFideId")), _fide(h.get("BlackFideId"))
    ids = bool(wf and bf and wf == rec["white_fide"] and bf == rec["black_fide"])
    return ids, name_score(h.get("White"), rec["white"]), name_score(h.get("Black"), rec["black"])


def _readings(lg: PgnGame) -> int:
    return sum(1 for p in lg.plies if p.clock_s)


def attach_lichess(records: list[dict], lichess_games: list[tuple[int | None, PgnGame]]) -> list[tuple[str, int | None, PgnGame]]:
    """Attach each Lichess game to the TWIC record of the same game: sets rec['lichess'] and rec['quality'].

    records: one dict per TWIC game with round, game, white, black, white_fide, black_fide.
    lichess_games: (round, game) pairs. Returns the games left over as (reason, round, game).

    The moves decide. A Lichess game belongs to the TWIC game of the same round with the same
    first 30 half-moves, whatever names the broadcast shows: boards are sometimes mixed up in a
    broadcast, and names are spelled differently in the two sources. Only games too short for
    that, or whose TWIC score is missing, are matched by FIDE ids or names, and then the moves
    that exist must not contradict each other.
    """
    by_key: dict[tuple, list[dict]] = {}
    by_round: dict[int | None, list[dict]] = {}
    for rec in records:
        rec.setdefault("lichess", None)
        rec.setdefault("quality", None)
        by_round.setdefault(rec["round"], []).append(rec)
        plies = rec["game"].plies
        if len(plies) >= MOVE_KEY_PLIES:
            by_key.setdefault((rec["round"], tuple(_plain(p.san) for p in plies[:MOVE_KEY_PLIES])), []).append(rec)

    left: list[tuple[str, int | None, PgnGame]] = []
    later: list[tuple[int | None, PgnGame]] = []
    for rnd, lg in lichess_games:                       # pass 1: by moves
        hits = by_key.get((rnd, tuple(_plain(p.san) for p in lg.plies[:MOVE_KEY_PLIES])), []) if len(lg.plies) >= MOVE_KEY_PLIES else []
        if not hits:
            later.append((rnd, lg))
            continue
        # several TWIC games with the same 30 half-moves: the players, then the longest agreement, decide
        def closeness(c):
            ids, sw, sb = _same_players(lg, c)
            return ids, sw + sb, common_plies(lg.plies, c["game"].plies)
        rec = max(hits, key=closeness)
        ids, sw, sb = _same_players(lg, rec)
        quality = "moves" if ids or (sw >= 0.5 and sb >= 0.5) else "moves_only"   # moves_only: the broadcast shows other names
        if rec["lichess"] is None:
            rec["lichess"], rec["quality"] = lg, quality
        elif _readings(lg) > _readings(rec["lichess"]):     # the same game twice: keep the copy with more clock data
            left.append(("duplicate of an attached game", rnd, rec["lichess"]))
            rec["lichess"], rec["quality"] = lg, quality
        else:
            left.append(("duplicate of an attached game", rnd, lg))

    for rnd, lg in later:                               # pass 2: by players, for games without a move match
        best, best_key = None, (False, 0.0)
        h = lg.headers
        for c in by_round.get(rnd, []):
            if c["lichess"] is not None:
                continue
            ids = _same_players(lg, c)[0]
            sw, sb = name_score(h.get("White"), c["white"]), name_score(h.get("Black"), c["black"])
            if not ids and (sw < 0.5 or sb < 0.5):
                continue
            if (ids, sw + sb) > best_key:
                best, best_key = c, (ids, sw + sb)
        if best is None:
            left.append(("no game with these moves or players in the round", rnd, lg))
        elif common_plies(lg.plies, best["game"].plies) < min(len(lg.plies), len(best["game"].plies)):
            left.append(("moves differ from the game of these players", rnd, lg))
        else:
            best["lichess"], best["quality"] = lg, "fide_id" if best_key[0] else "names"
    return left


def merge_moves(tg: PgnGame, lg: PgnGame | None) -> tuple[list[Ply], str, int]:
    """Moves to store for a game: (plies, source, broadcast half-moves left out).

    The TWIC score is the game. Lichess clock readings and evaluations are attached to its moves
    as far as the two scores agree. Half-moves that only the broadcast has are left out: they are
    mostly kings put on a centre square to signal the result, which the board records as moves.
    If TWIC has no moves for the game, the broadcast moves are stored as they are.
    """
    if lg is None:
        return tg.plies, "twic", 0
    if not tg.plies:
        return lg.plies, "lichess", 0
    n = common_plies(tg.plies, lg.plies)
    plies = [Ply(t.san, l.clock_s, l.eval_cp, l.eval_mate) for t, l in zip(tg.plies[:n], lg.plies[:n])]
    plies += [Ply(t.san) for t in tg.plies[n:]]
    return plies, "twic", len(lg.plies) - n


def lichess_round(lg: PgnGame) -> int | None:
    rnd = _int((lg.headers.get("Round") or "").split(".")[0])
    if rnd is None:
        m = re.search(r"Rd\.?\s*(\d+)", lg.headers.get("BroadcastName", ""))
        rnd = int(m.group(1)) if m else None
    return rnd


def assemble_event(twic_games: list[PgnGame], lichess_games: list[PgnGame]) -> dict:
    """Everything that is stored for one Olympiad section, worked out from the parsed files.
    Returns {"matches": [...], "unmatched": [(reason, round, game)], "notes": [...]}; no database needed."""
    matches, notes = build_matches(twic_games)
    records = []
    for mt in matches:
        for rec in mt["games"]:
            h = rec["game"].headers
            rec.update(round=mt["round"], white=h.get("White"), black=h.get("Black"),
                       white_fide=_fide(h.get("WhiteFideId")), black_fide=_fide(h.get("BlackFideId")))
            records.append(rec)
    unmatched = attach_lichess(records, [(lichess_round(lg), lg) for lg in lichess_games if lg.plies])
    for rec in records:
        lg = rec["lichess"]
        plies, source, extra = merge_moves(rec["game"], lg)
        rec.update(plies=plies, moves_source=source, extra_plies=extra if lg else None,
                   times=[NO_TIME] * len(plies), has_clocks=False, clock_ok=False,
                   coverage=None, bad_readings=None, jumps=None, end_elapsed=None)
        if lg is None:
            continue
        times, stats = reconstruct_times(plies, lg.headers.get("TimeControl"))
        coverage = stats.readings / len(plies) if plies else 0.0
        end = next((t.elapsed_s for t in reversed(times) if t.elapsed_s is not None), None)
        has_clocks = coverage >= 0.9
        rec.update(times=times, coverage=coverage, has_clocks=has_clocks, end_elapsed=end,
                   bad_readings=stats.bad_readings, jumps=stats.jumps,
                   # A timeline to rely on. Discarded readings do not count against it: they are gone from the
                   # timeline, and has_clocks already requires 90% of the moves to have a usable reading.
                   clock_ok=(has_clocks and not stats.jumps and not stats.long_thinks
                             and times[-1].elapsed_s is not None))
    return {"matches": matches, "unmatched": unmatched, "notes": notes}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
TABLES = ["events", "teams", "players", "matches", "games", "moves", "unmatched_lichess"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     serial PRIMARY KEY,
    year         int  NOT NULL,
    section      text NOT NULL CHECK (section IN ('open','women')),
    name         text,
    site         text,
    start_date   date,
    has_clocks   boolean NOT NULL DEFAULT false,
    UNIQUE (year, section)
);
CREATE TABLE IF NOT EXISTS teams (
    team_id  serial PRIMARY KEY,
    name     text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    player_id serial PRIMARY KEY,
    fide_id   bigint UNIQUE,
    name      text NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS players_name_nofide ON players (name) WHERE fide_id IS NULL;
CREATE TABLE IF NOT EXISTS matches (
    match_id   serial PRIMARY KEY,
    event_id   int NOT NULL REFERENCES events,
    round      int NOT NULL,
    match_no   int NOT NULL,             -- table number
    team_a_id  int REFERENCES teams,     -- team with White on board 1
    team_b_id  int REFERENCES teams,
    score_a    numeric(3,1),
    score_b    numeric(3,1),
    n_games    int,
    UNIQUE (event_id, round, match_no)
);
CREATE TABLE IF NOT EXISTS games (
    game_id         serial PRIMARY KEY,
    match_id        int NOT NULL REFERENCES matches,
    board           int,
    board_source    text,                -- 'header' (from the TWIC file) or 'inferred' (from the colours)
    white_id        int REFERENCES players,
    black_id        int REFERENCES players,
    white_team_id   int REFERENCES teams,
    black_team_id   int REFERENCES teams,
    white_elo       int,
    black_elo       int,
    white_title     text,
    black_title     text,
    result          text,                -- official result (TWIC)
    white_score     numeric(2,1),        -- 1, 0.5, 0
    eco             text,
    n_plies         int,
    moves_source    text,                -- 'twic' (official score) or 'lichess' (TWIC has no moves for the game)
    lichess_url     text,
    lichess_result  text,
    match_quality   text,                -- how the Lichess game was matched: moves, moves_only, fide_id, names
    lichess_extra_plies int,             -- broadcast half-moves that are not in the official score (not stored)
    has_clocks      boolean NOT NULL DEFAULT false,
    clock_coverage  real,                -- share of plies with a usable clock reading
    clock_ok        boolean NOT NULL DEFAULT false,  -- has_clocks, no unexplained jump, no move above 45 min, last move timed
    clock_bad_readings int,              -- clock readings that were discarded
    clock_jumps     int,                 -- times a clock went up for no known reason
    end_elapsed_s   int,                 -- time of the last timed move (s since round start)
    twic_pgn        text,
    lichess_pgn     text,
    UNIQUE (match_id, board)
);
CREATE TABLE IF NOT EXISTS moves (
    game_id      int NOT NULL REFERENCES games ON DELETE CASCADE,
    ply          int NOT NULL,           -- 1 = White's first move
    move_no      int NOT NULL,
    color        char(1) NOT NULL,       -- 'w' or 'b'
    san          text NOT NULL,
    clock_s      int,                    -- clock reading after the move (s), as broadcast
    think_s      int,                    -- reconstructed thinking time (s)
    think_plies  int,                    -- >1 if earlier readings were missing
    elapsed_s    int,                    -- time since round start when the move was made (s)
    time_left_s  int,                    -- clock_s, plus the 30-min bonus from move 40 while the clock does not show it yet
    eval_cp      int,                    -- engine eval, centipawns, White's view (Lichess)
    eval_mate    int,                    -- mate in N (White's view), if given
    bonus_added  boolean,
    PRIMARY KEY (game_id, ply)
);
CREATE TABLE IF NOT EXISTS unmatched_lichess (
    id       serial PRIMARY KEY,
    year     int, section text, round int,
    white    text, black text, result text, url text, pgn text,
    reason   text
);

-- Columns added after the first version (these do nothing on a new database)
ALTER TABLE games ADD COLUMN IF NOT EXISTS board_source text;
ALTER TABLE games ADD COLUMN IF NOT EXISTS lichess_extra_plies int;
ALTER TABLE games ADD COLUMN IF NOT EXISTS clock_ok boolean NOT NULL DEFAULT false;
ALTER TABLE games ADD COLUMN IF NOT EXISTS clock_bad_readings int;
ALTER TABLE games ADD COLUMN IF NOT EXISTS clock_jumps int;
ALTER TABLE moves ADD COLUMN IF NOT EXISTS time_left_s int;
ALTER TABLE unmatched_lichess ADD COLUMN IF NOT EXISTS reason text;
"""

# Written so that PostgreSQL and SQLite both accept them (the export recreates them in SQLite).
VIEWS = {
    # Finish order of the games within each match, and how far ahead of the next game each one ended
    "game_finish": """
        SELECT g.game_id, g.match_id, g.board, g.result, g.white_score, g.end_elapsed_s,
               rank() OVER (PARTITION BY g.match_id ORDER BY g.end_elapsed_s) AS finish_rank,
               lead(g.end_elapsed_s) OVER (PARTITION BY g.match_id ORDER BY g.end_elapsed_s) - g.end_elapsed_s AS gap_to_next_s
        FROM games g
        WHERE g.has_clocks AND g.end_elapsed_s IS NOT NULL""",
    # Matches with four games on boards 1-4, all with clock data
    "complete_matches": """
        SELECT m.*,
               (SELECT count(*) FROM games g WHERE g.match_id = m.match_id AND g.clock_ok) = 4 AS all_clocks_ok
        FROM matches m
        WHERE m.n_games = 4
          AND (SELECT count(DISTINCT g.board) FROM games g WHERE g.match_id = m.match_id AND g.board BETWEEN 1 AND 4) = 4
          AND (SELECT count(*) FROM games g WHERE g.match_id = m.match_id AND g.has_clocks) = 4""",
}


def create_schema(conn, rebuild: bool = False) -> None:
    for name in VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {name}")
    if rebuild:
        conn.execute("DROP TABLE IF EXISTS " + ", ".join(reversed(TABLES)) + " CASCADE")
    conn.execute(SCHEMA)
    for name, sql in VIEWS.items():
        conn.execute(f"CREATE VIEW {name} AS {sql}")


class Loader:
    def __init__(self, conn):
        self.conn = conn
        self.team_ids: dict[str, int] = {}
        self.player_ids: dict = {}

    def team(self, name: str | None) -> int | None:
        if not name:
            return None
        if name not in self.team_ids:
            row = self.conn.execute(
                "INSERT INTO teams(name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING team_id",
                (name,)).fetchone()
            self.team_ids[name] = row[0]
        return self.team_ids[name]

    def player(self, name: str | None, fide: int | None) -> int | None:
        if not name:
            return None
        key = fide or name
        if key not in self.player_ids:
            if fide:
                row = self.conn.execute(
                    "INSERT INTO players(fide_id, name) VALUES (%s,%s) ON CONFLICT (fide_id) DO UPDATE SET name=EXCLUDED.name RETURNING player_id",
                    (fide, name)).fetchone()
            else:
                row = self.conn.execute("SELECT player_id FROM players WHERE fide_id IS NULL AND name=%s", (name,)).fetchone()
                if row is None:
                    row = self.conn.execute("INSERT INTO players(name) VALUES (%s) RETURNING player_id", (name,)).fetchone()
            self.player_ids[key] = row[0]
        return self.player_ids[key]


def load(dsn: str, data_dir: Path = DATA_DIR, years: list[int] | None = None, rebuild: bool = False) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        create_schema(conn, rebuild)
        loader = Loader(conn)
        for (year, section), fname in TWIC_FILES.items():
            if years and year not in years:
                continue
            path = data_dir / "twic" / fname
            if not path.exists():
                print(f"skip {year} {section}: {path} not found (run download first)")
                continue
            print(f"loading {year} {section} ...", flush=True)
            load_event(conn, loader, year, section, path, data_dir)
            conn.commit()
    print("load complete")


def load_event(conn, loader: Loader, year: int, section: str, twic_path: Path, data_dir: Path) -> None:
    twic_games = parse_file(twic_path)
    if not twic_games:
        print(f"  WARNING: no games found in {twic_path}; nothing loaded for {year} {section}", file=sys.stderr)
        return
    h0 = twic_games[0].headers
    start = h0.get("EventDate") or h0.get("Date")
    start = start.replace(".", "-") if start and "?" not in start else None
    lichess_files = sorted((data_dir / "lichess").glob(f"{year}_{section}_*.pgn")) if (data_dir / "lichess").exists() else []
    event = assemble_event(twic_games, [lg for lf in lichess_files for lg in parse_file(lf)])

    # Re-load cleanly: remove this event's previous rows.
    old = conn.execute("SELECT event_id FROM events WHERE year=%s AND section=%s", (year, section)).fetchone()
    if old:
        conn.execute("DELETE FROM moves WHERE game_id IN (SELECT g.game_id FROM games g JOIN matches m USING(match_id) WHERE m.event_id=%s)", old)
        conn.execute("DELETE FROM games WHERE match_id IN (SELECT match_id FROM matches WHERE event_id=%s)", old)
        conn.execute("DELETE FROM matches WHERE event_id=%s", old)
        conn.execute("DELETE FROM unmatched_lichess WHERE year=%s AND section=%s", (year, section))
        event_id = old[0]
        conn.execute("UPDATE events SET name=%s, site=%s, start_date=%s, has_clocks=%s WHERE event_id=%s",
                     (EVENT_NAMES.get(year), h0.get("Site"), start, bool(lichess_files), event_id))
    else:
        event_id = conn.execute(
            "INSERT INTO events(year, section, name, site, start_date, has_clocks) VALUES (%s,%s,%s,%s,%s,%s) RETURNING event_id",
            (year, section, EVENT_NAMES.get(year), h0.get("Site"), start, bool(lichess_files))).fetchone()[0]

    move_rows = []
    n_games = n_lichess = 0
    for mt in event["matches"]:
        match_id = conn.execute(
            "INSERT INTO matches(event_id, round, match_no, team_a_id, team_b_id, score_a, score_b, n_games) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING match_id",
            (event_id, mt["round"], mt["match_no"], loader.team(mt["team_a"]), loader.team(mt["team_b"]),
             mt["score_a"], mt["score_b"], len(mt["games"]))).fetchone()[0]
        for rec in mt["games"]:
            tg: PgnGame = rec["game"]
            lg: PgnGame | None = rec["lichess"]
            h = tg.headers
            res = h.get("Result")
            game_id = conn.execute(
                """INSERT INTO games(match_id, board, board_source, white_id, black_id, white_team_id, black_team_id,
                       white_elo, black_elo, white_title, black_title, result, white_score, eco, n_plies,
                       moves_source, lichess_url, lichess_result, match_quality, lichess_extra_plies,
                       has_clocks, clock_coverage, clock_ok, clock_bad_readings, clock_jumps,
                       end_elapsed_s, twic_pgn, lichess_pgn)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING game_id""",
                (match_id, rec["board"], rec["board_source"],
                 loader.player(h.get("White"), rec["white_fide"]), loader.player(h.get("Black"), rec["black_fide"]),
                 loader.team(rec["white_team"]), loader.team(rec["black_team"]),
                 _int(h.get("WhiteElo")), _int(h.get("BlackElo")), h.get("WhiteTitle"), h.get("BlackTitle"),
                 res, RESULT_SCORE.get(res), h.get("ECO"), len(rec["plies"]), rec["moves_source"],
                 lg.headers.get("GameURL") if lg else None, lg.headers.get("Result") if lg else None,
                 rec["quality"], rec["extra_plies"], rec["has_clocks"],
                 round(rec["coverage"], 4) if lg else None, rec["clock_ok"], rec["bad_readings"], rec["jumps"],
                 rec["end_elapsed"], tg.raw, lg.raw if lg else None)).fetchone()[0]
            n_games += 1
            n_lichess += lg is not None
            for i, (p, t) in enumerate(zip(rec["plies"], rec["times"])):
                move_rows.append((game_id, i + 1, i // 2 + 1, "w" if i % 2 == 0 else "b", p.san, p.clock_s,
                                  t.think_s, t.think_plies, t.elapsed_s, t.time_left_s, p.eval_cp, p.eval_mate,
                                  t.bonus_added if lg else None))

    with conn.cursor() as cur:
        with cur.copy("COPY moves (game_id, ply, move_no, color, san, clock_s, think_s, think_plies, "
                      "elapsed_s, time_left_s, eval_cp, eval_mate, bonus_added) FROM STDIN") as cp:
            for row in move_rows:
                cp.write_row(row)

    for reason, rnd, lg in event["unmatched"]:
        h = lg.headers
        conn.execute("INSERT INTO unmatched_lichess(year, section, round, white, black, result, url, pgn, reason) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                     (year, section, rnd, h.get("White"), h.get("Black"), h.get("Result"), h.get("GameURL"), lg.raw, reason))
    print(f"  {n_games} games in {len(event['matches'])} matches; "
          f"{n_lichess} with Lichess data; {len(event['unmatched'])} Lichess games unmatched")
    if n_games != len(twic_games):
        print(f"  WARNING: {len(twic_games) - n_games} of {len(twic_games)} games in the file were not loaded", file=sys.stderr)
    if event["notes"]:
        print(f"  {len(event['notes'])} labels in the TWIC file were repaired or could not be used:")
        for note in event["notes"]:
            print(f"    {note}")


SUMMARY_SQL = """
SELECT e.year, e.section,
       count(DISTINCT m.match_id)                                  AS matches,
       count(g.game_id)                                            AS games,
       count(g.lichess_url)                                        AS with_lichess,
       count(*) FILTER (WHERE g.has_clocks)                        AS with_clocks,
       count(*) FILTER (WHERE g.clock_ok)                          AS clocks_ok,
       (SELECT count(*) FROM complete_matches cm WHERE cm.event_id = e.event_id) AS complete_matches,
       (SELECT count(*) FROM unmatched_lichess u WHERE u.year = e.year AND u.section = e.section) AS unmatched
FROM events e
JOIN matches m ON m.event_id = e.event_id
JOIN games g   ON g.match_id = m.match_id
GROUP BY e.event_id, e.year, e.section
ORDER BY e.year, e.section;
"""


def summary(dsn: str) -> None:
    import psycopg
    with psycopg.connect(dsn) as conn:
        cur = conn.execute(SUMMARY_SQL)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    widths = [max(len(str(x)) for x in [c] + [r[i] for r in rows]) for i, c in enumerate(cols)]
    print("  ".join(c.rjust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(x).rjust(w) for x, w in zip(r, widths)))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
_SQLITE_TYPES = {"integer": "INTEGER", "bigint": "INTEGER", "smallint": "INTEGER", "boolean": "INTEGER",
                 "numeric": "REAL", "real": "REAL", "double precision": "REAL"}
_SQLITE_INDEXES = [
    "CREATE UNIQUE INDEX events_pk ON events (event_id)", "CREATE UNIQUE INDEX teams_pk ON teams (team_id)",
    "CREATE UNIQUE INDEX players_pk ON players (player_id)", "CREATE UNIQUE INDEX matches_pk ON matches (match_id)",
    "CREATE UNIQUE INDEX games_pk ON games (game_id)", "CREATE UNIQUE INDEX moves_pk ON moves (game_id, ply)",
    "CREATE INDEX matches_event ON matches (event_id, round, match_no)", "CREATE INDEX games_match ON games (match_id, board)",
]


def export(dsn: str, out_dir: Path = EXPORT_DIR) -> None:
    """Write the database to out_dir as one SQLite file and, if pg_dump is installed, a compressed SQL dump."""
    import decimal
    import sqlite3
    import psycopg

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "chess_olympiad.sqlite"
    part = out_dir / "chess_olympiad.sqlite.part"
    part.unlink(missing_ok=True)

    def plain(v):
        if isinstance(v, decimal.Decimal):
            return float(v)
        if isinstance(v, (bool, int, float, str)) or v is None:
            return v
        return str(v)   # dates

    lite = sqlite3.connect(part)
    try:
        with psycopg.connect(dsn) as conn:
            for table in TABLES:
                cols = conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position", (table,)).fetchall()
                names = ", ".join(c for c, _ in cols)
                typed = ", ".join(c + " " + _SQLITE_TYPES.get(t, "TEXT") for c, t in cols)
                marks = ", ".join("?" * len(cols))
                lite.execute(f"CREATE TABLE {table} ({typed})")
                insert = f"INSERT INTO {table} VALUES ({marks})"
                n = 0
                with conn.cursor(name=f"export_{table}") as cur:     # server-side cursor: rows arrive in batches
                    cur.execute(f"SELECT {names} FROM {table}")
                    while rows := cur.fetchmany(50_000):
                        lite.executemany(insert, [tuple(plain(v) for v in row) for row in rows])
                        n += len(rows)
                print(f"  {table}: {n:,} rows")
        for sql in _SQLITE_INDEXES:
            lite.execute(sql)
        for name, sql in VIEWS.items():
            lite.execute(f"CREATE VIEW {name} AS {sql}")
        lite.commit()
    finally:
        lite.close()
    part.replace(target)
    print(f"wrote {target}")

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        print("pg_dump not found on PATH: SQL dump skipped")
        return
    dump = out_dir / "chess_olympiad.sql.gz"
    subprocess.run([pg_dump, "--no-owner", "--no-privileges", "-Z", "6", "-f", str(dump), dsn], check=True)
    print(f"wrote {dump}")


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["download", "load", "all", "summary", "export"])
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="PostgreSQL connection string")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--years", type=int, nargs="*", help="only these years (default: all)")
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--rebuild", action="store_true", help="load: drop this pipeline's tables first and start from empty")
    ap.add_argument("--out", default=str(EXPORT_DIR), help="export: output directory")
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir)
    if a.command in ("download", "all"):
        download(data_dir, a.force, a.years)
    if a.command in ("load", "all", "summary", "export") and not a.dsn:
        ap.error("--dsn or DATABASE_URL is required")
    if a.command in ("load", "all"):
        load(a.dsn, data_dir, a.years, a.rebuild)
    if a.command in ("load", "all", "summary"):
        summary(a.dsn)
    if a.command == "export":
        export(a.dsn, Path(a.out))


if __name__ == "__main__":
    main()
