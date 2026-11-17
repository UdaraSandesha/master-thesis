# Changes made after the review

Done on 2026-11-13 and 2026-11-14, following `docs/analysis.md`. Nothing is committed yet.

## Summary

All five main issues from the review are fixed, and most of the smaller ones. The database
was rebuilt from the raw files with the new pipeline, exported again, and the notebook was
re-run and its text brought in line with the new numbers. 25 tests pass (7 before).

Three things are left for you, because they need a decision or an outside source:

1. **Results where TWIC and Lichess disagree** (34 games) have not been checked against
   chess-results.com. The query is in Appendix B of the review.
2. **Federation codes for teams and aliases for players** are not added. They need a curated
   mapping of the 398 team names.
3. **Nothing is committed or tagged**, and the three papers in `literature/` are untouched.
   Whether they may stay depends on whether the GitHub repository is public.

The table of other boards' state at each move (step 6 in the review) is not built. It is new
analysis work that depends on the research design, not a fix.

## Before and after

"Before" is the old export, now in `data/export/before_fixes_2026-11-03/`. "After" is the
rebuilt database. Both were audited with the same checks.

| | Before | After |
|---|---|---|
| Games stored | 56,436 | 56,436 |
| Matches | 14,129 | 14,119 |
| Matches with four games on boards 1–4 between two teams | 14,006 | 14,080 |
| Matches whose games involve three teams | 29 | 0 |
| Matches with a number of games other than four | 103 | 39 |
| Games without a board number | 47 | 3 |
| Games with Lichess data attached | 29,333 | 29,345 |
| of which carry another game's moves, or only 1–2 moves | 24 | 0 |
| Lichess games not attached | 47 | 35 |
| Games stored with moves that are not in the official score | 1,873 | 0 |
| Games stored with fewer moves than the official score | 51 | 0 |
| Games containing an illegal move | 708 | 2 |
| Games with clock data (`has_clocks`) | 28,768 | 28,660 |
| of which pass all clock checks (`clock_ok`) | – | 27,562 |
| 0:00:00 readings used as real times | 1,207 | 0 |
| Clocked games longer than the time control allows | 62 | 0 |
| Bonus additions recognised | 24,904 | 25,083 |
| Complete matches | 6,702 | 6,664 |
| of which have three teams or a repeated board | 9 | 0 |
| of which have four games that pass the clock checks | – | 5,733 |

The number of games is unchanged because one game that used to be dropped is now loaded
and one game that is in the source file twice is now stored once.

Of the 6,577 matches that are complete both before and after, the last game to finish is a
different one in 103 (1.6%), and the finishing order differs in 190 (2.9%).

The two remaining illegal moves are castling errors in the official TWIC scores (2016
Women, round 4, Zaura v Dlamini; 2018 Open, round 9, Cabral v Diallo).

## What changed, issue by issue

### 1. The 30-minute bonus

`reconstruct_times` now knows both clock settings. The bonus is recognised when a clock
goes up by more than 10 minutes from move 30 on (as before), and from move 40 also when it
goes up by less, if the opponent has just received the bonus, or if the player used more
time than was left and the opponent's next reading goes up as well.

New column `moves.time_left_s`: the clock reading plus the 30 minutes from move 40 on while
the clock does not show them yet. The notebook now shows time left after move 40 and has a
table of when the bonus appears, by year.

### 2. Timelines

- Impossible readings (0:00:00, values above what the time control allows) are ignored.
- `elapsed_s` is taken from the latest reading of each clock, not from a running sum, so
  one wrong reading no longer shifts the rest of the game.
- A reading is dropped when the readings before and after it fit together without it but
  not with it. 5,765 readings in 1,237 games are dropped in total.
- A clock that goes up and stays up for no known reason is counted in `games.clock_jumps`
  (295 cases). Steps of up to 2.5 minutes are treated as time added by an arbiter.
- New column `games.clock_ok`, and `complete_matches.all_clocks_ok`.

Two more problems turned up while testing this and are fixed too:

- **Stuck clocks.** 184 games have a clock that repeats the same reading four or more
  times in a row. In one game all 74 readings are 1:30:30, which the old pipeline stored
  as a 37-minute game with 30 seconds per move. Repeats are now ignored, and 98 games no
  longer count as having clock data.
- **Impossible think times.** 146 games contain a single move of more than 45 minutes
  according to the readings, usually where both clocks drop at once in the opening. These
  cannot be repaired, so the games are stored but `clock_ok` is false.

### 3. Official score

The TWIC score is now the move list of every game that has one. Lichess clock readings and
evaluations are attached to those moves as far as the two scores agree. 3,206 broadcast
half-moves in 1,878 games are left out (`games.lichess_extra_plies` says how many per game).
Where TWIC has no moves (44 attached games), the broadcast moves are stored.

### 4. Matching

Lichess games are matched by their moves first (same round, same first 30 half-moves), and
by FIDE ids or names only when a game is too short or TWIC has no moves, and then only if
the moves that exist do not contradict each other. `match_quality` is now `moves` (29,015),
`moves_only` (38: the broadcast shows other names), `fide_id` (170) or `names` (122).

`unmatched_lichess` has a new `reason` column: 22 are second copies of a game that is
already attached, 5 have moves that differ from the game of those players, and for 8 no game
was found.

### 5. Matches

Matches are formed from the round and the two teams, not from the table number in the file.
Boards that are missing, repeated or on the wrong colour are worked out from the colours
(40 games, `games.board_source = 'inferred'`). `load` prints every repair.

Two faults in the source files that the review had not found are handled as well:

- Four games name a team that plays another match in that round (2014 Women round 1, and
  2024 Open rounds 1, 4 and 5). They are put in the match their table number points to.
- One game is in the 2012 Women's file twice with different results (round 10, Debowska v
  Stojanovic, 1-0 and ½-½). The copy filed under the match's own table number (½-½) is
  kept. Which result is right needs checking.

The 39 matches that still have fewer than four games are missing a game in the source file.

### Smaller issues

| Issue | Status |
|---|---|
| Results where the sources disagree | Not done: needs an outside source |
| 2018 coverage is not random | Stated in the README and the notebook, with a table |
| Close finishes | `game_finish.gap_to_next_s` added. Among complete matches that pass the clock checks, the last game ends within 3 minutes of the third in 481 of 5,732 |
| README | Rewritten: file count, matching, timing, the third example query, new columns |
| Download | Writes to a temporary file first, rejects anything that is not a PGN file, writes `data/raw/manifest.json` |
| Load | Warns about empty files and about any game it could not load |
| Invisible character in the source | Replaced with `"﻿"` |
| Test database safety | The database test refuses any database whose name does not end in `_test` |
| Missing tests | 18 new tests, one or more for each fix |
| Players and teams | Described in the README; structure unchanged |
| Ids change on reload | Described in the README; `load --rebuild` added |
| Dependencies | `requirements.lock` added |
| Exports | `export` command added; new SQLite file and SQL dump written |
| PDFs | `.gitattributes` marks them binary; the files themselves are untouched |
| Notebook | Wilson intervals, one definition of a played game, 2018 coverage, time left after move 40, bonus table, all text and numbers updated, HTML regenerated |

## Where the result differs from what the review said

- **`clock_ok` is defined differently.** The review proposed that any discarded reading
  should fail the check. A discarded reading is gone from the timeline, and `has_clocks`
  already requires usable readings for 90% of the moves, so that would have excluded games
  for no reason. `clock_ok` now means: no unexplained jump, no move above 45 minutes, and a
  reading on the last move.
- **The reconstruction goes further than the prototype** in Appendix C of the review: it
  drops single wrong readings and stuck clocks, and it does not credit a bonus that no
  second reading confirms. 48 unexplained jumps from move 40 on remain, 8 of them on the
  last move of a game. Some may be missed bonuses. All are flagged through `clock_ok`.
- **The review overstated the unmatched games.** It said 39 Lichess games could be attached
  but were not. 22 of the unmatched games are second copies of games that already had
  data. 28 games gained Lichess data.
- **The effect on finishing order is a little smaller** than the prototype suggested: the
  last game changes in 1.6% of complete matches, not 2.1%.

## Files

| File | Change |
|---|---|
| `olympiad_pipeline.py` | Match grouping, matching, move list, clock reconstruction, schema, `export`, manifest |
| `tests/test_pipeline.py` | 25 tests |
| `notebooks/01_descriptive_statistics.ipynb`, `.html` | Updated and re-run |
| `README.md` | Rewritten |
| `requirements.lock`, `.gitattributes`, `.gitignore` | New or changed |
| `data/raw/manifest.json` | New; the only file under `data/` in git |
| `data/export/` | New `chess_olympiad.sqlite` and `chess_olympiad.sql.gz`; the old exports are in `before_fixes_2026-11-03/` (860 MB) and can be deleted when no longer needed |
