"""Tests for the Olympiad pipeline. Run with:  pytest -q tests
The database test needs a PostgreSQL DSN in TEST_DATABASE_URL (it is skipped otherwise)."""
import copy
import os
from pathlib import Path

import pytest

import olympiad_pipeline as op

FIX = Path(__file__).parent / "fixtures"


def twic24():
    """The four games of Panama v United States (2024 Open, round 1, table 1), as fresh copies."""
    return copy.deepcopy(op.parse_file(FIX / "twic" / "olym24.pgn"))


def lichess24():
    return copy.deepcopy(op.parse_file(FIX / "lichess" / "2024_open_wYigbpXq.pgn"))


def records(games):
    return [{"round": 1, "game": g, "white": g.headers["White"], "black": g.headers["Black"],
             "white_fide": op._fide(g.headers.get("WhiteFideId")), "black_fide": op._fide(g.headers.get("BlackFideId"))}
            for g in games]


def clocks(white, black):
    """Plies with the given clock readings, alternating White and Black."""
    plies = []
    for w, b in zip(white, black):
        plies += [op.Ply("a3", w), op.Ply("a6", b)]
    return plies


# --- parsing -----------------------------------------------------------------------------
def test_parse_twic_both_layouts():
    g24 = op.parse_file(FIX / "twic" / "olym24.pgn")
    assert len(g24) == 4
    assert op.twic_round_match_board(g24[2].headers) == (1, 1, 3)
    assert [p.san for p in g24[0].plies[:4]] == ["d4", "Nf6", "c4", "e6"]
    assert len(g24[0].plies) == 81                     # 41.Rxc8+ is White's 41st move
    g10 = op.parse_file(FIX / "twic" / "olymm10.pgn")
    assert op.twic_round_match_board(g10[1].headers) == (1, 1, 2)   # Round "1", Board "1.2"
    assert g10[0].plies[-1].san == "Kf2"
    # Board values with text after the number occur in the 2012 files
    assert op.twic_round_match_board({"Round": "3.7", "Board": "3 KS"}) == (3, 7, 3)


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


# --- clock reconstruction ----------------------------------------------------------------
def test_time_reconstruction_and_bonus():
    lg = op.parse_file(FIX / "lichess" / "2024_open_wYigbpXq.pgn")
    plies = lg[2].plies                                  # Mendez Machado - Aronian
    t, stats = op.reconstruct_times(plies, lg[2].headers["TimeControl"])
    assert stats.bad_readings == 0 and stats.jumps == 0
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
    # Time left counts the bonus from move 40 on, also before the clock shows it
    assert t[78].time_left_s == plies[78].clock_s + 1800 and t[84].time_left_s == plies[84].clock_s
    el = [x[2] for x in t if x[2] is not None]
    assert el == sorted(el)
    # Game length is plausible (between 2 and 6 hours)
    assert 2 * 3600 < el[-1] < 6 * 3600


def test_missing_clock_spans():
    plies = [op.Ply("e4", 5440), op.Ply("e5", 5440), op.Ply("Nf3", None), op.Ply("Nc6", 5400), op.Ply("Bb5", 5300)]
    t, _ = op.reconstruct_times(plies)
    assert t[2] == op.NO_TIME
    # White's Bb5 spans two plies: 5440 + 2*30 - 5300 = 200
    assert t[4][0] == 200 and t[4][1] == 2


def test_placeholder_readings_are_ignored():
    # 0:00:00 on move 1 is a placeholder; taken literally it would be a 91-minute think
    plies = clocks([0, 5400, 5350], [5440, 5400, 5380])
    t, stats = op.reconstruct_times(plies)
    assert stats.bad_readings == 1 and t[0] == op.NO_TIME
    assert t[2].think_s == 5430 + 60 - 5400 and t[2].think_plies == 2
    assert t[-1].elapsed_s == (5430 + 90 - 5350) + (5430 + 90 - 5380)
    # A reading far above what the time control allows is ignored too
    t, stats = op.reconstruct_times(clocks([5440, 25000, 5350], [5440, 5400, 5380]))
    assert stats.bad_readings == 1 and t[2] == op.NO_TIME


def test_stuck_clock_is_not_a_timeline():
    # White's clock is no longer transmitted after three moves: the broadcast repeats the last reading
    white = [5440, 5400, 5360] + [5360] * 5 + [5000]
    black = [5440, 5400, 5360, 5320, 5280, 5240, 5200, 5160, 5120]
    t, stats = op.reconstruct_times(clocks(white, black))
    assert stats.bad_readings == 5 and all(t[i] == op.NO_TIME for i in range(6, 16, 2))
    # The next real reading gives the time used over the whole stretch
    assert t[16].think_s == 5360 + 6 * 30 - 5000 and t[16].think_plies == 6
    # A clock that never moved gives no timeline at all
    t, stats = op.reconstruct_times(clocks([5430] * 20, [5430] * 20))
    assert stats.readings == 2 and stats.bad_readings == 38


def test_implausible_think_time_is_counted():
    white = [5440, 5400, 5360, 2000, 1960]               # 56 minutes on move 4
    black = [5440, 5400, 5360, 5320, 5280]
    _, stats = op.reconstruct_times(clocks(white, black))
    assert stats.long_thinks == 1 and stats.bad_readings == 0 and stats.jumps == 0


def test_one_wrong_reading_does_not_shift_the_game():
    good = clocks([5440, 5300, 5200, 5150, 5100], [5440, 5400, 5380, 5300, 5250])
    bad = copy.deepcopy(good)
    bad[4].clock_s = 4000                                # White's third reading is 20 minutes too low
    t_good, _ = op.reconstruct_times(good)
    t_bad, stats = op.reconstruct_times(bad)
    assert stats.bad_readings == 1 and stats.jumps == 0
    assert t_bad[4] == op.NO_TIME
    # From White's next reading on, the timeline is the same as without the wrong reading
    assert [x.elapsed_s for x in t_bad[6:]] == [x.elapsed_s for x in t_good[6:]]
    assert t_bad[6].think_s == t_good[4].think_s + t_good[6].think_s and t_bad[6].think_plies == 2


def test_bonus_goes_on_both_clocks_when_one_runs_out():
    # Clocks without a move counter (2022 onwards). White uses 160 s a move and has 230 s after move 40,
    # then thinks 25 minutes on move 41: the clock runs out, and both clocks get 30 minutes.
    white = [5430 - 130 * n for n in range(1, 41)] + [230 + 30 + 1800 - 1500]
    black = [5430 - 70 * n for n in range(1, 41)] + [2630 + 30 + 1800 - 50]
    t, stats = op.reconstruct_times(clocks(white, black))
    assert stats.bad_readings == 0 and stats.jumps == 0
    assert t[80].bonus_added and t[80].think_s == 1500   # the clock went up by only 5 minutes
    assert t[81].bonus_added and t[81].think_s == 50
    assert t[-1].elapsed_s == 40 * 160 + 1500 + 40 * 100 + 50
    # Time left: before move 40 the reading itself, from move 40 the bonus counts even if not on the clock yet
    assert t[76].time_left_s == white[38] and t[78].time_left_s == 230 + 1800 and t[80].time_left_s == white[40]
    assert t[79].time_left_s == 2630 + 1800


def test_no_bonus_when_nobody_runs_out():
    # Both players keep time in hand: the clocks never add the second period, and nothing is wrong with that
    white = [5430 - 70 * n for n in range(1, 51)]
    t, stats = op.reconstruct_times(clocks(white, white))
    assert not any(x.bonus_added for x in t) and stats.jumps == 0
    assert t[98].time_left_s == white[49] + 1800 and t[76].time_left_s == white[38]


def test_time_added_by_an_arbiter_is_a_jump_not_a_bonus():
    # White gets two minutes before the 4th reading. The 3rd move took 10 s, so the 3rd reading
    # cannot be the one that is wrong: the time really was added.
    white = [5440, 5400, 5420, 5420 + 30 + 120 - 10, 5520, 5480]
    black = [5440, 5400, 5360, 5320, 5280, 5240]
    t, stats = op.reconstruct_times(clocks(white, black))
    assert stats.jumps == 1 and stats.bad_readings == 0
    assert not any(x.bonus_added for x in t) and t[6].think_s is None
    el = [x.elapsed_s for x in t]
    assert el == sorted(el) and t[8].think_s == 70
    # When the readings before and after fit together without the 3rd one, the 3rd reading is taken
    # to be wrong (too low). That is far more common than added time.
    white = [5440, 5400, 5200, 5360, 5320, 5280]
    t, stats = op.reconstruct_times(clocks(white, black))
    assert stats.jumps == 0 and stats.bad_readings == 1 and t[4] == op.NO_TIME


# --- matches -----------------------------------------------------------------------------
def test_match_from_clean_file():
    matches, notes = op.build_matches(twic24())
    assert notes == [] and len(matches) == 1
    m = matches[0]
    assert (m["round"], m["match_no"], m["team_a"], m["team_b"]) == (1, 1, "Panama", "United States of America")
    assert (m["score_a"], m["score_b"]) == (0.5, 3.5)
    assert [(r["board"], r["board_source"]) for r in m["games"]] == [(b, "header") for b in (1, 2, 3, 4)]


def test_wrong_table_number_does_not_split_a_match():
    games = twic24()
    games[1].headers["Round"] = "1.70"                   # as in the 2024 files: board 2 filed under another table,
    del games[1].headers["Board"]                        # without a board number
    matches, notes = op.build_matches(games)
    assert len(matches) == 1 and len(notes) == 1 and "board 2 inferred" in notes[0]
    m = matches[0]
    assert m["match_no"] == 1 and (m["score_a"], m["score_b"]) == (0.5, 3.5)
    assert [(r["board"], r["board_source"]) for r in m["games"]] == [(1, "header"), (2, "inferred"), (3, "header"), (4, "header")]
    assert m["games"][1]["game"] is games[1]


def test_two_games_with_the_same_board_number_are_both_kept():
    games = twic24()
    games[1].headers["Board"] = "1"                      # as in 2012 Open, round 11, Cuba v Slovenia
    matches, notes = op.build_matches(games)
    boards = {r["board"]: r["game"] for r in matches[0]["games"]}
    assert len(matches[0]["games"]) == 4 and boards[1] is games[0] and boards[2] is games[1]


def test_game_listed_twice_is_stored_once():
    games = twic24()
    twin = copy.deepcopy(games[2])
    twin.headers["Round"], twin.headers["Result"] = "1.9", "1/2-1/2"
    matches, notes = op.build_matches([twin] + games)
    assert len(matches) == 1 and len(matches[0]["games"]) == 4
    assert all(r["game"] is not twin for r in matches[0]["games"])
    assert len(notes) == 1 and "twice" in notes[0]


def test_wrong_team_name_does_not_create_a_second_match():
    games = twic24()
    games[3].headers["BlackTeam"] = "Atlantis"           # board 4: Panama's opponent is not Atlantis in this round
    matches, notes = op.build_matches(games)
    assert len(matches) == 1 and len(matches[0]["games"]) == 4
    rec = matches[0]["games"][3]
    assert rec["board"] == 4 and {rec["white_team"], rec["black_team"]} == {"Panama", "United States of America"}
    assert len(notes) == 1 and "Atlantis" in notes[0]


# --- matching Lichess games to TWIC games -------------------------------------------------
def test_name_matching_formats():
    # 2018/2022 Lichess names have no comma and sometimes different order
    assert op.name_score("So Wesley", "So, Wesley") == 1.0
    assert op.name_score("Vidit Santosh Gujrathi", "Vidit, Santosh Gujrathi") == 1.0
    assert op.name_score("Makoto Rodwell", "Rodwell, Makoto") == 1.0
    assert op.name_score("Müller, Jörg", "Mueller Jorg") < 1.0  # accents are stripped, transliteration is not
    assert op.name_score("Carlsen, Magnus", "Caruana, Fabiano") == 0.0


def test_match_by_moves_whatever_the_names():
    recs = records(twic24())
    lg = lichess24()[3]
    lg.headers = {k: v for k, v in lg.headers.items() if "Fide" not in k}
    lg.headers["White"], lg.headers["Black"] = "Robson Ray", "Baules Rodriguez Jorge Luis"
    assert op.attach_lichess(recs, [(1, lg)]) == []
    rec = next(r for r in recs if r["lichess"] is lg)
    assert rec["white"] == "Robson, Ray" and rec["quality"] == "moves"
    # A different transliteration of both names changes nothing
    recs = records(twic24())
    lg.headers["White"], lg.headers["Black"] = "Robsan R.", "Baulez J."
    assert op.attach_lichess(recs, [(1, lg)]) == []
    assert next(r for r in recs if r["lichess"] is lg)["quality"] == "moves_only"


def test_boards_mixed_up_in_the_broadcast():
    recs = records(twic24())
    lich = lichess24()
    for key in ("White", "Black", "WhiteFideId", "BlackFideId"):        # the broadcast shows boards 2 and 3 exchanged
        lich[1].headers[key], lich[2].headers[key] = lich[2].headers[key], lich[1].headers[key]
    assert op.attach_lichess(recs, [(1, g) for g in lich]) == []
    for rec, lg in zip(recs, lich):                                      # every game gets its own moves and clocks
        assert rec["lichess"] is lg
        assert op.common_plies(rec["game"].plies, lg.plies) == len(rec["game"].plies)
    assert [r["quality"] for r in recs] == ["moves", "moves_only", "moves_only", "moves"]


def test_same_game_twice_in_the_broadcast():
    recs = records(twic24())
    lich = lichess24()
    copy_without_clocks = copy.deepcopy(lich[0])
    for p in copy_without_clocks.plies:
        p.clock_s = None
    left = op.attach_lichess(recs, [(1, copy_without_clocks)] + [(1, g) for g in lich])
    assert recs[0]["lichess"] is lich[0]                                 # the copy with clock data is kept
    assert [(reason, g) for reason, _, g in left] == [("duplicate of an attached game", copy_without_clocks)]


def test_short_games_are_matched_by_players_if_the_moves_agree():
    recs = records(twic24())
    lg = lichess24()[0]
    lg.plies = lg.plies[:12]                                             # the broadcast stopped after six moves
    assert op.attach_lichess(recs, [(1, lg)]) == [] and recs[0]["lichess"] is lg and recs[0]["quality"] == "fide_id"
    recs = records(twic24())
    lg.plies = [op.Ply("h4", 5400)]                                      # one move, and not the one that was played
    left = op.attach_lichess(recs, [(1, lg)])
    assert recs[0]["lichess"] is None and left[0][0] == "moves differ from the game of these players"


def test_official_score_is_the_move_list():
    tg, lg = twic24()[0], lichess24()[0]
    lg.plies += [op.Ply("Ke5", 300), op.Ply("Kd4")]                      # kings put in the centre after the game
    plies, source, extra = op.merge_moves(tg, lg)
    assert source == "twic" and extra == 2
    assert [p.san for p in plies] == [p.san for p in tg.plies]
    assert plies[0].clock_s == 5456 and plies[0].eval_cp == 17 and all(p.clock_s for p in plies)
    lg.plies = lg.plies[:40]                                             # the broadcast stopped early
    plies, source, extra = op.merge_moves(tg, lg)
    assert len(plies) == len(tg.plies) and extra == 0
    assert plies[39].clock_s and plies[40].clock_s is None and plies[40].san == tg.plies[40].san
    tg.plies = []                                                        # TWIC has no moves for the game
    assert op.merge_moves(tg, lg) == (lg.plies, "lichess", 0)
    assert op.merge_moves(tg, None) == ([], "twic", 0)


def test_assemble_event_without_database():
    event = op.assemble_event(twic24(), lichess24())
    assert event["unmatched"] == [] and event["notes"] == []
    games = event["matches"][0]["games"]
    assert [len(r["plies"]) for r in games] == [81, 65, 90, 63]
    assert all(r["has_clocks"] and r["clock_ok"] and r["quality"] == "moves" for r in games)
    assert all(r["extra_plies"] == 0 and r["moves_source"] == "twic" for r in games)
    # Without broadcast files nothing is timed
    games = op.assemble_event(twic24(), [])["matches"][0]["games"]
    assert not any(r["has_clocks"] or r["clock_ok"] for r in games) and games[0]["times"][0] == op.NO_TIME


def test_manifest(tmp_path):
    (tmp_path / "twic").mkdir()
    (tmp_path / "twic" / "a.pgn").write_text('[Event "x"]\n\n1. e4 *\n')
    path = op.write_manifest(tmp_path)
    import hashlib
    import json
    entry = json.loads(path.read_text())["files"]["twic/a.pgn"]
    assert entry["sha256"] == hashlib.sha256((tmp_path / "twic" / "a.pgn").read_bytes()).hexdigest() and entry["bytes"] == 21
    assert op._is_pgn(b'\xef\xbb\xbf\n[Event "x"]') and not op._is_pgn(b"<!DOCTYPE html>")


# --- database ----------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="no TEST_DATABASE_URL")
def test_load_into_postgres(tmp_path):
    import psycopg
    dsn = os.environ["TEST_DATABASE_URL"]
    dbname = psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", "")
    assert dbname.endswith("_test"), f"refusing to wipe database {dbname!r}: its name must end in _test"
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
        assert q("SELECT count(*), count(*) FILTER (WHERE clock_ok) FROM games WHERE has_clocks") == [(4, 4)]
        assert q("SELECT count(*), bool_and(all_clocks_ok) FROM complete_matches") == [(1, True)]
        assert q("SELECT count(*) FROM unmatched_lichess") == [(0,)]
        assert q("SELECT DISTINCT moves_source, match_quality, board_source FROM games WHERE lichess_url IS NOT NULL") == [("twic", "moves", "header")]
        # Every move of the 2024 games is stored with clock, elapsed time and time left
        assert q("""SELECT count(*), count(clock_s), count(elapsed_s), count(time_left_s) FROM moves mv
                    JOIN games g USING(game_id) WHERE g.lichess_url IS NOT NULL""") == [(81 + 65 + 90 + 63,) * 4]
        # Finish order in the 2024 match: board 2 (33 moves) finished before board 3 (45 moves)
        fo = dict(q("""SELECT g.board, f.finish_rank FROM game_finish f JOIN games g USING(game_id)"""))
        assert fo[2] < fo[3]
        assert q("SELECT count(*) FROM game_finish WHERE gap_to_next_s IS NULL") == [(1,)]   # only the last game has no next one
        # Players are de-duplicated by FIDE id
        assert q("SELECT count(*) FROM players") == [(16,)]
    # The SQLite export has the same rows and the same views
    import sqlite3
    op.export(dsn, tmp_path)
    lite = sqlite3.connect(tmp_path / "chess_olympiad.sqlite")
    assert lite.execute("SELECT count(*) FROM games").fetchone() == (8,)
    assert lite.execute("SELECT count(*), count(time_left_s) FROM moves").fetchone()[1] == 81 + 65 + 90 + 63
    assert lite.execute("SELECT count(*), min(all_clocks_ok) FROM complete_matches").fetchone() == (1, 1)
    assert lite.execute("SELECT count(*) FROM game_finish").fetchone() == (4,)
    # --rebuild starts from empty tables
    op.load(dsn, FIX, rebuild=True)
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*), min(game_id) FROM games").fetchone() == (8, 1)
