"""Tests for the Olympiad pipeline. Run with:  pytest -q tests
The database test needs a PostgreSQL DSN in TEST_DATABASE_URL (it is skipped otherwise)."""
import os
from pathlib import Path

import pytest

import olympiad_pipeline as op

FIX = Path(__file__).parent / "fixtures"


def test_parse_twic_both_layouts():
    g24 = op.parse_file(FIX / "twic" / "olym24.pgn")
    assert len(g24) == 4
    assert op.twic_round_match_board(g24[2].headers) == (1, 1, 3)
    assert [p.san for p in g24[0].plies[:4]] == ["d4", "Nf6", "c4", "e6"]
    assert len(g24[0].plies) == 81                     # 41.Rxc8+ is White's 41st move
    g10 = op.parse_file(FIX / "twic" / "olymm10.pgn")
    assert op.twic_round_match_board(g10[1].headers) == (1, 1, 2)   # Round "1", Board "1.2"
    assert g10[0].plies[-1].san == "Kf2"


def test_parse_lichess_clocks_and_evals():
    lg = op.parse_file(FIX / "lichess" / "2024_open_wYigbpXq.pgn")
    assert len(lg) == 4
    p = lg[0].plies[0]
    assert (p.san, p.clock_s, p.eval_cp) == ("d4", 5456, 17)
    # Annotated move: clock sits in a later comment block, SAN loses its "?!"
    b2 = lg[1].plies
    qb6 = b2[29]
    assert qb6.san == "Qb6" and qb6.clock_s == 27 * 60 + 17 and qb6.eval_cp == 86
    # Mate evaluations
    last = lg[2].plies[-1]
    assert last.san == "R1xg3" and last.eval_mate == -4 and last.eval_cp is None


def test_time_reconstruction_and_bonus():
    lg = op.parse_file(FIX / "lichess" / "2024_open_wYigbpXq.pgn")
    plies = lg[2].plies                                  # Mendez Machado - Aronian
    t = op.reconstruct_times(plies, lg[2].headers["TimeControl"])
    # First move: 5430 + 30 - 5459 = 1 s
    assert t[0][0] == 1
    # 9.Ne4: 1:19:09 -> 1:02:19 with +30 s increment = 1040 s
    assert t[16][0] == (79 * 60 + 9) + 30 - (62 * 60 + 19)
    # Black's +30 min bonus shows up at 42...h3+ (clock 0:20:03 -> 0:48:05)
    i = 83
    assert plies[i].san == "h3+" and t[i][3] is True
    assert t[i][0] == 20 * 60 + 3 + 30 + 1800 - (48 * 60 + 5)
    # White's bonus at 42.Ree2 (0:00:58 -> 0:16:29)
    assert plies[82].san == "Ree2" and t[82][3] is True
    # No negative think times, and elapsed time is monotonic
    assert all(x[0] >= 0 for x in t if x[0] is not None)
    el = [x[2] for x in t if x[2] is not None]
    assert el == sorted(el)
    # Game length is plausible (between 2 and 6 hours)
    assert 2 * 3600 < el[-1] < 6 * 3600


def test_missing_clock_spans():
    plies = [op.Ply("e4", 5440), op.Ply("e5", 5440), op.Ply("Nf3", None), op.Ply("Nc6", 5400), op.Ply("Bb5", 5300)]
    t = op.reconstruct_times(plies)
    assert t[2] == (None, None, None, False)
    # White's Bb5 spans two plies: 5440 + 2*30 - 5300 = 200
    assert t[4][0] == 200 and t[4][1] == 2


def test_name_matching_formats():
    # 2018/2022 Lichess names have no comma and sometimes different order
    assert op.name_score("So Wesley", "So, Wesley") == 1.0
    assert op.name_score("Vidit Santosh Gujrathi", "Vidit, Santosh Gujrathi") == 1.0
    assert op.name_score("Makoto Rodwell", "Rodwell, Makoto") == 1.0
    assert op.name_score("Müller, Jörg", "Mueller Jorg") < 1.0  # accents are stripped, transliteration is not
    assert op.name_score("Carlsen, Magnus", "Caruana, Fabiano") == 0.0


def test_match_without_fide_ids():
    twic = op.parse_file(FIX / "twic" / "olym24.pgn")
    cands = [{"white": g.headers["White"], "black": g.headers["Black"], "white_fide": None, "black_fide": None,
              "sans": [p.san for p in g.plies]} for g in twic]
    lg = op.parse_file(FIX / "lichess" / "2024_open_wYigbpXq.pgn")[3]
    lg.headers = {k: v for k, v in lg.headers.items() if "Fide" not in k}
    lg.headers["White"], lg.headers["Black"] = "Robson Ray", "Baules Rodriguez Jorge Luis"
    rec, q = op.match_lichess_game(lg, cands)
    assert rec["white"] == "Robson, Ray" and q == "names+moves"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="no TEST_DATABASE_URL")
def test_load_into_postgres():
    import psycopg
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    op.load(dsn, FIX)
    op.load(dsn, FIX)          # loading twice must not duplicate anything
    with psycopg.connect(dsn) as c:
        q = lambda s: c.execute(s).fetchall()
        assert q("SELECT year, section, has_clocks FROM events ORDER BY year") == [(2010, "open", False), (2024, "open", True)]
        assert q("SELECT count(*) FROM matches") == [(2,)]
        assert q("SELECT count(*) FROM games") == [(8,)]
        # Match scores: Panama 0.5 - 3.5 USA; Russia 1 3.5 - 0.5 Ireland
        rows = q("""SELECT ta.name, m.score_a, tb.name, m.score_b FROM matches m
                    JOIN teams ta ON ta.team_id=m.team_a_id JOIN teams tb ON tb.team_id=m.team_b_id ORDER BY m.event_id""")
        assert [(a, float(sa), b, float(sb)) for a, sa, b, sb in rows] == [
            ("RUS1", 3.5, "IRL", 0.5), ("Panama", 0.5, "United States of America", 3.5)]
        assert q("SELECT count(*) FROM games WHERE has_clocks") == [(4,)]
        assert q("SELECT count(*) FROM complete_matches") == [(1,)]
        assert q("SELECT count(*) FROM unmatched_lichess") == [(0,)]
        # Every move of the 2024 games is stored with clock and elapsed time
        assert q("""SELECT count(*), count(clock_s), count(elapsed_s) FROM moves mv
                    JOIN games g USING(game_id) WHERE g.moves_source='lichess'""") == [(81 + 65 + 90 + 63,) * 3]
        # Finish order in the 2024 match: board 2 (33 moves) finished before board 3 (45 moves)
        fo = dict(q("""SELECT g.board, f.finish_rank FROM game_finish f JOIN games g USING(game_id)"""))
        assert fo[2] < fo[3]
        # Players are de-duplicated by FIDE id
        assert q("SELECT count(*) FROM players") == [(16,)]
