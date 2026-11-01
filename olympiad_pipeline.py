#!/usr/bin/env python3
"""
Chess Olympiad database pipeline (2010-2026, Open and Women's sections).

Sources
  * The Week in Chess (TWIC): complete game files for every Olympiad, with
    teams, boards, ratings, FIDE IDs and results. No clock times.
  * Lichess broadcasts (2018, 2022, 2024, 2026): per-move clock readings
    (%clk) and engine evaluations (%eval) for most games.

The pipeline downloads both, stores TWIC as the master list of games and
matches Lichess games onto them. For matched games the moves come from
Lichess (with clocks); otherwise from TWIC (moves only).

Usage
  python olympiad_pipeline.py download                # fetch raw PGN files into ./data/raw
  python olympiad_pipeline.py load --dsn postgresql://user:pass@localhost:5432/chess_olympiad
  python olympiad_pipeline.py all  --dsn ...           # download + load
  python olympiad_pipeline.py summary --dsn ...        # coverage table per Olympiad

The DSN can also be given via the DATABASE_URL environment variable.
2020 and 2021 are skipped: those were online rapid Olympiads.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("OLYMPIAD_DATA_DIR", "data/raw"))
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
        out.write_bytes(_get(TWIC_BASE + fname))
        time.sleep(1)
    for (year, section), ids in LICHESS_TOURS.items():
        if years and year not in years:
            continue
        for tid in ids:
            out = data_dir / "lichess" / f"{year}_{section}_{tid}.pgn"
            if out.exists() and not force:
                continue
            print(f"Lichess {year} {section}: tour {tid}")
            out.write_bytes(_get(f"{LICHESS_BASE}{tid}.pgn"))
            time.sleep(3)  # be polite; the export endpoint is rate limited
    print("download complete")


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


def reconstruct_times(plies: list[Ply], tc: str | None = None):
    """Per ply: think time (s), number of plies it spans, elapsed time since game start (s),
    and whether the 40-move bonus was credited on that ply.

    think = previous clock of the same player + increment - current clock.
    The 30-minute bonus is credited when a player's clock jumps up by more than 10 minutes
    after move 30 (digital clocks sometimes add it a few moves late or early).
    Elapsed time is the sum of both players' think times: one clock is always running.
    """
    base, inc, bonus = parse_time_control(tc)
    last = [base + inc, base + inc]           # digital clocks usually show base+inc before move 1
    pending = [0, 0]                           # plies since the player's last known reading
    bonus_used = [False, False]
    out = []
    elapsed = 0
    for i, p in enumerate(plies):
        side = i % 2
        move_no = i // 2 + 1
        pending[side] += 1
        if p.clock_s is None:
            out.append((None, None, None, False))
            continue
        spent = last[side] + inc * pending[side] - p.clock_s
        got_bonus = False
        if spent < -600 and move_no >= 30 and not bonus_used[side]:
            spent += bonus
            bonus_used[side] = got_bonus = True
        span = pending[side]
        pending[side] = 0
        last[side] = p.clock_s
        spent = max(0, spent)
        elapsed += spent
        out.append((spent, span, elapsed, got_bonus))
    return out


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


def match_lichess_game(lg: PgnGame, candidates: list[dict]) -> tuple[dict | None, str]:
    """Return (twic_game, quality). candidates: TWIC games of the same event and round."""
    h = lg.headers
    wf, bf = _fide(h.get("WhiteFideId")), _fide(h.get("BlackFideId"))
    if wf and bf:
        for c in candidates:
            if c["white_fide"] == wf and c["black_fide"] == bf:
                return c, "fide_id"
    best, best_score = None, 0.0
    for c in candidates:
        sw, sb = name_score(h.get("White"), c["white"]), name_score(h.get("Black"), c["black"])
        if sw < 0.5 or sb < 0.5:
            continue
        score = sw + sb
        prefix = [p.san for p in lg.plies[:10]]
        if prefix and prefix == c["sans"][: len(prefix)]:
            score += 1.0
        if score > best_score:
            best, best_score = c, score
    if best is None:
        return None, "unmatched"
    return best, "names+moves" if best_score > 2.0 else "names"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
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
    match_no   int NOT NULL,
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
    moves_source    text,                -- 'lichess' or 'twic'
    lichess_url     text,
    lichess_result  text,
    match_quality   text,                -- how the Lichess game was matched
    has_clocks      boolean NOT NULL DEFAULT false,
    clock_coverage  real,                -- share of plies with a clock reading
    end_elapsed_s   int,                 -- reconstructed duration (s since round start)
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
    clock_s      int,                    -- clock reading after the move (s)
    think_s      int,                    -- reconstructed thinking time (s)
    think_plies  int,                    -- >1 if earlier readings were missing
    elapsed_s    int,                    -- time since round start when the move was made (s)
    eval_cp      int,                    -- engine eval, centipawns, White's view (Lichess)
    eval_mate    int,                    -- mate in N (White's view), if given
    bonus_added  boolean,
    PRIMARY KEY (game_id, ply)
);
CREATE TABLE IF NOT EXISTS unmatched_lichess (
    id       serial PRIMARY KEY,
    year     int, section text, round int,
    white    text, black text, result text, url text, pgn text
);

-- Finish order of the games within each match (for match-state analyses)
CREATE OR REPLACE VIEW game_finish AS
SELECT g.game_id, g.match_id, g.board, g.result, g.white_score, g.end_elapsed_s,
       rank() OVER (PARTITION BY g.match_id ORDER BY g.end_elapsed_s) AS finish_rank
FROM games g
WHERE g.has_clocks AND g.end_elapsed_s IS NOT NULL;

-- Matches where all four games have clock data
CREATE OR REPLACE VIEW complete_matches AS
SELECT m.*
FROM matches m
WHERE m.n_games = 4
  AND (SELECT count(*) FROM games g WHERE g.match_id = m.match_id AND g.has_clocks) = 4;
"""

RESULT_SCORE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def twic_round_match_board(h: dict) -> tuple[int | None, int | None, int | None]:
    """TWIC uses two layouts: Round 'r.m' + Board 'b' (2012+) or Round 'r' + Board 'm.b' (2010)."""
    rnd, brd = h.get("Round", ""), h.get("Board", "")
    if "." in brd:
        m, b = brd.split(".", 1)
        return _int(rnd.split(".")[0]), _int(m), _int(b)
    if "." in rnd:
        r, m = rnd.split(".", 1)
        return _int(r), _int(m), _int(brd)
    return _int(rnd), None, _int(brd)


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


def load(dsn: str, data_dir: Path = DATA_DIR, years: list[int] | None = None) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute(SCHEMA)
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
        return
    h0 = twic_games[0].headers
    start = h0.get("EventDate") or h0.get("Date")
    start = start.replace(".", "-") if start and "?" not in start else None
    lichess_files = sorted((data_dir / "lichess").glob(f"{year}_{section}_*.pgn")) if (data_dir / "lichess").exists() else []

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

    # --- Build matches from TWIC -------------------------------------------------
    by_match: dict[tuple[int, int], list[PgnGame]] = {}
    for g in twic_games:
        r, m, b = twic_round_match_board(g.headers)
        if r is None:
            continue
        g.headers["_round"], g.headers["_match"], g.headers["_board"] = r, m, b
        by_match.setdefault((r, m if m is not None else -1), []).append(g)

    twic_records = []   # dicts used for matching
    for (r, m), games in sorted(by_match.items()):
        games.sort(key=lambda g: g.headers["_board"] or 99)
        first = games[0].headers
        team_a = first.get("WhiteTeam") if (first["_board"] or 1) % 2 == 1 else first.get("BlackTeam")
        team_b = first.get("BlackTeam") if team_a == first.get("WhiteTeam") else first.get("WhiteTeam")
        score_a = score_b = 0.0
        for g in games:
            s = RESULT_SCORE.get(g.headers.get("Result"))
            if s is None:
                continue
            if g.headers.get("WhiteTeam") == team_a:
                score_a, score_b = score_a + s, score_b + 1 - s
            else:
                score_a, score_b = score_a + 1 - s, score_b + s
        match_id = conn.execute(
            "INSERT INTO matches(event_id, round, match_no, team_a_id, team_b_id, score_a, score_b, n_games) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING match_id",
            (event_id, r, m, loader.team(team_a), loader.team(team_b), score_a, score_b, len(games))).fetchone()[0]
        for g in games:
            h = g.headers
            wf, bf = _fide(h.get("WhiteFideId")), _fide(h.get("BlackFideId"))
            twic_records.append({
                "round": r, "match_id": match_id, "board": h["_board"], "game": g,
                "white": h.get("White"), "black": h.get("Black"),
                "white_fide": wf, "black_fide": bf,
                "sans": [p.san for p in g.plies], "lichess": None, "quality": None,
            })

    # --- Match Lichess games onto TWIC games -------------------------------------
    by_round: dict[int, list[dict]] = {}
    for rec in twic_records:
        by_round.setdefault(rec["round"], []).append(rec)
    unmatched = []
    for lf in lichess_files:
        for lg in parse_file(lf):
            rnd = _int((lg.headers.get("Round") or "").split(".")[0])
            if rnd is None:
                m = re.search(r"Rd\.?\s*(\d+)", lg.headers.get("BroadcastName", ""))
                rnd = int(m.group(1)) if m else None
            if not lg.plies:
                continue
            cands = [c for c in by_round.get(rnd, []) if c["lichess"] is None]
            rec, quality = match_lichess_game(lg, cands)
            if rec is None:
                unmatched.append((rnd, lg))
                continue
            rec["lichess"], rec["quality"] = lg, quality

    # --- Insert games and moves --------------------------------------------------
    move_rows = []
    for rec in twic_records:
        tg: PgnGame = rec["game"]
        lg: PgnGame | None = rec["lichess"]
        h = tg.headers
        plies = lg.plies if lg else tg.plies
        times = reconstruct_times(plies, (lg.headers.get("TimeControl") if lg else None)) if lg else [(None,) * 4] * len(plies)
        n_clk = sum(1 for p in plies if p.clock_s is not None)
        coverage = n_clk / len(plies) if plies else 0.0
        has_clocks = bool(lg) and coverage >= 0.9
        end_elapsed = next((t[2] for t in reversed(times) if t[2] is not None), None) if lg else None
        res = h.get("Result")
        game_id = conn.execute(
            """INSERT INTO games(match_id, board, white_id, black_id, white_team_id, black_team_id,
                   white_elo, black_elo, white_title, black_title, result, white_score, eco, n_plies,
                   moves_source, lichess_url, lichess_result, match_quality, has_clocks, clock_coverage,
                   end_elapsed_s, twic_pgn, lichess_pgn)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (match_id, board) DO NOTHING RETURNING game_id""",
            (rec["match_id"], rec["board"],
             loader.player(h.get("White"), rec["white_fide"]), loader.player(h.get("Black"), rec["black_fide"]),
             loader.team(h.get("WhiteTeam")), loader.team(h.get("BlackTeam")),
             _int(h.get("WhiteElo")), _int(h.get("BlackElo")), h.get("WhiteTitle"), h.get("BlackTitle"),
             res, RESULT_SCORE.get(res), h.get("ECO"), len(plies),
             "lichess" if lg else "twic",
             lg.headers.get("GameURL") if lg else None, lg.headers.get("Result") if lg else None,
             rec["quality"], has_clocks, round(coverage, 4) if lg else None, end_elapsed,
             tg.raw, lg.raw if lg else None)).fetchone()
        if game_id is None:
            continue
        for i, (p, t) in enumerate(zip(plies, times)):
            move_rows.append((game_id[0], i + 1, i // 2 + 1, "w" if i % 2 == 0 else "b", p.san,
                              p.clock_s, t[0], t[1], t[2], p.eval_cp, p.eval_mate, t[3]))

    with conn.cursor() as cur:
        with cur.copy("COPY moves (game_id, ply, move_no, color, san, clock_s, think_s, think_plies, "
                      "elapsed_s, eval_cp, eval_mate, bonus_added) FROM STDIN") as cp:
            for row in move_rows:
                cp.write_row(row)

    for rnd, lg in unmatched:
        h = lg.headers
        conn.execute("INSERT INTO unmatched_lichess(year, section, round, white, black, result, url, pgn) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (year, section, rnd, h.get("White"), h.get("Black"), h.get("Result"), h.get("GameURL"), lg.raw))
    n_l = sum(1 for r in twic_records if r["lichess"])
    print(f"  {len(twic_records)} games in {len(by_match)} matches; "
          f"{n_l} with Lichess data; {len(unmatched)} Lichess games unmatched")


SUMMARY_SQL = """
SELECT e.year, e.section,
       count(DISTINCT m.match_id)                                  AS matches,
       count(g.game_id)                                            AS games,
       count(g.lichess_url)                                        AS with_lichess,
       count(*) FILTER (WHERE g.has_clocks)                        AS with_clocks,
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
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["download", "load", "all", "summary"])
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="PostgreSQL connection string")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--years", type=int, nargs="*", help="only these years (default: all)")
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir)
    if a.command in ("download", "all"):
        download(data_dir, a.force, a.years)
    if a.command in ("load", "all", "summary") and not a.dsn:
        ap.error("--dsn or DATABASE_URL is required")
    if a.command in ("load", "all"):
        load(a.dsn, data_dir, a.years)
    if a.command in ("load", "all", "summary"):
        summary(a.dsn)


if __name__ == "__main__":
    main()
