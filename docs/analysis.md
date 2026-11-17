# Project review: Olympiad database and descriptive statistics

Reviewed on 2026-11-13 at commit `ebdaef0` (plus the uncommitted re-run of the notebook).
Data checked: the 101 raw files in `data/raw`, the PostgreSQL database in `data/postgres`, and
`data/export/chess_olympiad.sqlite`. The two databases have the same row counts
(56,436 games, 14,129 matches, 5,031,300 moves).

> **Status, 2026-11-14.** This review describes the first version of the pipeline and
> database. The issues below have since been fixed and the database rebuilt:
> see `docs/changes.md` for what changed, a before-and-after table, and what is still open.
> The old export this review was run against is in `data/export/before_fixes_2026-11-03/`.

## Summary

The project is in good shape for what it has been used for so far. The parser is correct, every
game in the source files is loaded except one, 99.9% of the Lichess games are attached to the
right game, all seven tests pass, and the figures in the notebook that I recomputed match the
database. None of the problems below changes a conclusion in the descriptive notebook.

The problems are in the parts the thesis will rely on next: the reconstructed timeline, the
move-level data, and the match records. In order of importance:

1. **The 30-minute bonus works differently from what the code assumes.** From 2022 the clocks add
   it to both players when the first player's time runs out, not at move 40. This explains the
   "missing bonus" that the notebook lists as an open question. It is not a data error, but it
   changes how time left after move 40 has to be computed.
2. **About 4% of clocked games have a wrong timeline.** Bad clock readings, the way negative think
   times are handled, and missed bonus additions shift the end time by more than a minute in
   about 1,260 games and by more than ten minutes in about 530. In about 2% of complete matches
   this changes which game finished last.
3. **Lichess moves replace the official score.** 1,864 games (6.4% of those with Lichess data)
   end with moves that were never played, mostly kings placed in the centre to signal the result.
   51 other games are cut short.
4. **24 games carry the moves and clock times of a different game**, and 39 Lichess games that
   could be attached are not.
5. **123 matches are put together wrongly**, 80 of them in 2024, because the table numbers in
   the source file are wrong. One game is dropped without a message.

Each of these has a concrete fix, described below. A list of smaller issues, comments on the
notebook, and a suggested order of work follow.

## What I checked

- Read all of `olympiad_pipeline.py`, `tests/test_pipeline.py`, the README, and every cell and
  stored output of the notebook, and looked at the main charts.
- Ran the tests, including the database test against a temporary `chess_test` database.
- Ran `summary` and the three example queries from the README against the real database.
- Parsed all 16 TWIC files and 85 Lichess files again and compared them with the database.
- Compared the TWIC and Lichess move lists of all 29,333 attached games.
- Replayed all 56,036 games that have moves with `python-chess` to test whether the stored moves
  are legal and whether a checkmate on the board agrees with the stored result.
- Recomputed the clock reconstruction for all 28,768 clocked games with extra diagnostics, and
  wrote a prototype of a more robust version to measure how much the timeline would change.

Not checked: whether the list of Lichess broadcast ids is complete (I did not query Lichess),
results and standings against a third source such as chess-results.com, and the download step
itself. I did not re-run the notebook. The prototype in Appendix C has not been validated
against independent timestamps, so its numbers show the scale of the problem, not exact truth.

## What is correct

| Check | Result |
|---|---|
| Tests | 7 of 7 pass (6 without a database, 1 with) |
| TWIC games loaded | 56,436 of 56,437 |
| Lichess games | 29,553 in the files: 29,333 attached, 173 without moves skipped, 47 unmatched |
| Lichess files | Every file belongs to the year and section in its name |
| Parser | All games replay legally except 708, and those failures are in the source data (issue 3) |
| Attachments | 29,309 of 29,333 have move lists that agree with the TWIC game |
| Starting clock (1:30:30, then +30 s per move) | Holds in all four years: the median reading after White's first move is 1:30:55 |
| Engine evaluations are from White's side | Final evaluation agrees with the result in 99.6% of decisive games |
| "Team A has White on boards 1 and 3" | Holds in every four-game match but one |
| Notebook figures | Games, matches, players, clock coverage per event, odd matches (103), games without a board (47) and the draw-length counts all reproduce |

## Issues that affect results

### 1. How the 30-minute bonus really works

`reconstruct_times` credits the bonus when a clock jumps up by more than 10 minutes after
move 30. The README says clocks sometimes add it "a few moves late", and the notebook says that
about 21% of long games from 2022 show no bonus "and the reason is not yet known".

The data show two different clock settings. The first five rows give the move at which a
player's clock shows the bonus, as a share of all detected additions:

| | 2018 | 2022 | 2024 | 2026 |
|---|---|---|---|---|
| Moves 30–39 | 2% | 1% | 1% | 1% |
| Move 40 | 93% | 0.5% | 0.1% | 0.5% |
| Move 41 | 3% | 37% | 38% | 37% |
| Moves 42–45 | 1% | 49% | 50% | 50% |
| Move 46 or later | 0.1% | 12% | 11% | 11% |
| Players in games of 45+ moves who ever get it | 99.7% | 76% | 81% | 76% |
| Games of 45+ moves where neither player gets it | 0.1% | 23% | 18% | 23% |
| Both players get it on consecutive half-moves | 97% | 92% | 93% | 94% |

In 2018 the clocks counted moves and added 30 minutes when a player completed move 40. From
2022 the clocks ran without a move counter. In that mode the second period starts when one
player's first-period time reaches zero, and the 30 minutes are added to both clocks at that
moment. The DGT 3000 manual describes exactly this behaviour. It fits every row of the table:
the bonus appears at whatever move a player first runs out, it appears on both clocks together,
and it never appears when both players keep time in hand.

What follows from this:

- **The 21% is not an error.** In those games neither player used up the first 90 minutes plus
  increments. They do not need to be excluded.
- **Time left after move 40 is understated from 2022.** Until the addition happens, the clock
  shows 30 minutes less than the player really has. A comparable measure is
  `clock_s + 1800` for readings from move 40 onwards while the player's bonus has not yet been
  added, and `clock_s` otherwise. With that column the notebook can show time left after move 40,
  which it currently leaves out. Before move 40 the reading is correct in every year.
- **The 10-minute threshold misses real additions.** If a player thinks for more than 20 minutes
  on the move during which the bonus arrives, the clock goes up by less than 10 minutes. The
  code then stores a think time of 0 and every later `elapsed_s` in that game is 20 to 30 minutes
  too early. I count about 320 such cases (2022: 77, 2024: 134, 2026: 109, 2018: 1).
  Because both clocks receive the bonus together, detection can be made much safer: credit it
  when the opponent received it within the last three half-moves, or when the clock went up
  although the player must have used more time than was left.
- **A flag fall before move 40 also triggers the addition.** In 2022–2026, 183 games show the
  first addition before move 40 and 163 of them end within two half-moves. These are either
  losses on time or clocks reset after the game. They are worth separating, because losses on
  time are otherwise invisible in the data.
- The README section on timing and the last two limitation paragraphs in the notebook should be
  rewritten to say this.

### 2. Timelines are wrong in about 4% of clocked games

`elapsed_s` and `end_elapsed_s` put the four boards of a match on one timeline. Three things
distort them.

**Impossible readings are treated as real.** 1,207 half-moves in 218 games have a clock reading
of exactly 0:00:00 (999 of them in 2026), which is a placeholder, not a time. A zero on the
first move gives a "think" of 91 minutes and shifts the whole game by about 90 minutes; this
happens in 68 games. Five readings in 2018 are above 2.5 hours. In total 136 half-moves in 101
games have a stored think time above one hour.

**Negative think times are set to zero one at a time.** When one reading is too low and the
next is right, `spent = max(0, spent)` keeps the error in `elapsed_s` for the rest of the game.
2,298 clocked games (8.0%) contain at least one such correction. The time removed adds up to
more than a minute in 1,187 games (4.1%), more than five minutes in 490 (1.7%) and more than ten
minutes in 190 (0.7%). 2024 is the noisiest year, with 637 games above one minute.

**Bonus additions are missed**, as described in issue 1, in about 320 games.

62 clocked games have a stored duration above the maximum the time control allows. The notebook
removes 29 of them from the duration chart, but they stay in the database and in the
`game_finish` view, where they look like the last game of their match.

To measure the effect I wrote a prototype (Appendix C) that treats impossible readings as
missing, uses the joint bonus rule, and computes elapsed time directly from the latest readings
instead of adding up differences. Compared with the stored values:

| | Games | Share |
|---|---|---|
| End time moves by more than 1 minute | 1,261 | 4.4% of 28,768 |
| End time moves by more than 10 minutes | 534 | 1.9% |
| of which later (a long think had been stored as 0) | 377 | |
| of which earlier (a bad reading had inflated it) | 157 | |
| Duration above the theoretical maximum | 62 before, 0 after | |
| Complete matches where the last game to finish changes | 138 | 2.1% of 6,702 |
| Complete matches where the finishing order changes | 216 | 3.2% |

For scale: in 869 of the complete matches the other three boards stand at 1½–1½ when the last
game is still being played. If the thesis studies those games, a 2% error in identifying the
last game is not negligible.

Fix, in `reconstruct_times`:

1. Treat a reading of 0, or one above what the time control allows at that move, as missing.
2. Use the joint bonus rule from issue 1.
3. Compute `elapsed_s` as time granted so far minus time still on the two clocks. A single bad
   reading then affects one row and corrects itself at the next reading.
4. Store a per-game quality flag (no impossible readings, no unexplained upward jump above 30 s,
   reading present on the last move) so analyses can filter on it. `has_clocks` currently counts
   placeholder zeros as readings, and 1,598 clocked games have no reading on their last half-move,
   so their end time comes from an earlier move.

### 3. Lichess moves replace the official score

For an attached game the pipeline stores the Lichess move list and discards TWIC's
(`plies = lg.plies if lg else tg.plies`). The two lists differ more often than expected.

- **Extra moves at the end: 1,864 games (6.4%).** The official score is a prefix of the Lichess
  one (2018: 797, 2022: 710, 2024: 348, 2026: 9). In 1,458 games it is one extra half-move. About
  two thirds consist only of king moves: after the game the players put the kings on central
  squares to signal the result, and the electronic board records that as moves. 707 Lichess
  games from 2018 and 2022 contain an illegal move, 698 of them within the last four half-moves
  (for example `Kd4` with the king far away, or `Kf5+ Kxf5`).
- **Cut short: 51 games.** The broadcast stopped early and 1,425 official half-moves are lost.
  Five games keep only one or two half-moves. Four of them are decisive, so the notebook's
  forfeit rule excludes them although they were played in full.

Aggregates barely move (mean length 89.79 against 89.75 half-moves; draws at exactly move 30
are 486 against 502), so the notebook is fine. For move-level work it matters: these rows have
think times, evaluations and move counts for positions that never occurred.

Fix: keep TWIC's moves as the game. Attach Lichess clocks and evaluations position by position
over the part where both lists agree, and drop or flag anything after the official last move.
Where Lichess is shorter, keep the remaining official moves with empty clock fields. Where TWIC
has no moves at all (120 games) keep using the Lichess moves.

### 4. Wrong and missing attachments

Comparing the two move lists of every attached game:

| Outcome | Games |
|---|---|
| Move lists agree | 29,178 |
| TWIC has no or almost no moves, Lichess supplies them | 120 |
| Agree for 20+ moves and differ only near the end | 11 |
| **Lichess moves belong to a different game of the same round** | 17 |
| **Lichess has one or two half-moves and replaces a full score** | 5 |
| **Moves differ from the start, origin unknown** | 2 |

The 17 are cases where the broadcast had the boards mixed up. Example: 2022 Open, round 6,
table 33, boards 2 and 3 are exchanged, so each game is stored with the other's moves and clock
times. The player names agree perfectly, so the name rule accepts the pair even though the moves
differ from move 1. 18 of the 24 wrong games are flagged `has_clocks`. Appendix A lists all 24.

In the other direction, 39 of the 47 unmatched Lichess games have a TWIC game in the same round
with identical moves. Most involve two Kyrgyz players whose names are spelled differently in the
two sources ("Tologon tegin, Semetei" against "Tologontegin Semetey").

The cause is in `match_lichess_game`: names decide, and the move comparison only adds to the
score. It also covers 10 half-moves, not the "first 10 moves" the README states, and five moves
of opening theory are shared by many games.

Fix: match on moves first. I tested the rule "same round and same first 30 half-moves". It
agrees with the current attachment 29,012 times, disagrees in exactly the 17 wrong cases, and
finds the 39 missing ones. It is ambiguous in 7 cases and finds no game in 4. The 301 Lichess
games shorter than 30 half-moves still need FIDE ids or names. A pair whose moves disagree early
should go to `unmatched_lichess` with a reason, not into `games`.

### 5. Matches built from wrong table numbers

Matches are formed from the `Round "r.m"` header in TWIC. That label is sometimes wrong.
14,006 of 14,129 matches pass a structure check (four games, two teams, boards 1 to 4, colours
alternating). 123 do not, and 80 of those are in 2024.

The pattern in 2024 is systematic. In all 11 rounds of both sections, one game of table 18 is
filed under table 70 with no board number. Table 18 then has three games and a 3-point total,
and table 70 has five games from three teams. In rounds 3 to 11 of the Open a second game is
filed alone under a table "370" that does not exist.

Consequences:

- `score_a`, `score_b` and `n_games` are wrong for these matches, so the match result is wrong.
- 47 games have no board number.
- 9 of the 6,702 rows in `complete_matches` have four games but three teams or a repeated board.
  The view only tests `n_games = 4`.
- One game is lost. In 2012 Open, round 11, Cuba v Slovenia, two games are labelled board 1.
  `ON CONFLICT (match_id, board) DO NOTHING` silently discards Beliavsky v Bruzon Batista.
- Four board headers in 2012 such as `"3 KS"` fail `int()` although the board is clear.

Fix: group games by round and pair of teams, which is unique because a team plays one match
per round. Take the board from the header when it is valid, otherwise from the colour pattern
and the free slot. Read the leading digits of the board header. Never drop a game silently:
raise or log. Add the two-team and four-board conditions to `complete_matches`.

## Smaller issues

| Area | Issue | Suggested fix |
|---|---|---|
| Results | TWIC and Lichess give different results in 34 games (ignoring `*`). The final evaluation supports TWIC in 17, Lichess in 4, and is unclear in 13. In 2024 Women, round 11, Rawlins v Domennova, the final position is checkmate by Black but the stored result is 1-0. Game 440 (2010 Open, TWIC only) has the same contradiction. | Check these against chess-results.com and record the outcome. Query in Appendix B. |
| Coverage | The 2018 clock data is not a random 75%. In rounds 1–4 only tables 1–32 are covered (100%), and no lower table is. Rounds 5–11 are nearly complete. | Say so in the notebook. Analyses of early rounds in 2018 see top tables only. |
| Finish order | In 8.3% of complete matches the last two games end within 3 minutes of each other, the margin the README calls unreliable (3.0% within 1 minute). `rank()` also produces ties. | Store the gap to the next finisher and treat close finishes as uncertain in robustness checks. |
| README | "~40 files" is 101. "First 10 moves" is 10 half-moves. The third example query returns all four games of each match, not the last one. The bonus description is outdated (issue 1). | Corrected query in Appendix B. |
| Download | Files are written in place, so an interrupted download leaves a partial file that is later treated as cached. An error page returned with status 200 would be saved as a `.pgn`. Nothing records when the files were fetched, and Lichess files can change later, for example when evaluations are added. | Write to a temporary file and rename. Check that the content starts with `[Event`. Write a manifest with date and SHA-256 per file and archive the raw files. |
| Load | An empty or missing TWIC file returns without a message. | Print a warning. |
| Source | Line 173 contains an invisible byte-order-mark character inside `lstrip("...")`. An editor or formatter can remove it without anyone noticing. | Write it as `"﻿"`. |
| Tests | The database test drops the `public` schema of whatever `TEST_DATABASE_URL` points to. | Refuse to run unless the database name ends in `_test`. |
| Tests | No test covers a zero reading, a late bonus, mixed-up boards, a wrong table number or a duplicate board. | Add one small fixture for each fix above. |
| Players | 267 player records have no FIDE id. 341 FIDE ids appear with more than one spelling and only the last loaded spelling is kept. 8 identical names have two FIDE ids. Rows are never removed on reload. | Document it. An alias table would help if players are tracked across years. |
| Teams | One row per name across all years and both sections, so "RUS1" (2010) and "Russia" are different teams while the Open and Women's "India" share a row. 398 names in total. | Add a federation code and a table with one row per team per event. |
| Ids | `game_id` and `match_id` are serial and change on every reload. | Use year, section, round, table and board as the stable key in any file kept outside the database. |
| Dependencies | `requirements.txt` has lower bounds only. The environment runs Python 3.14 and pandas 3.1. | Add a lock file from `pip freeze`. |
| Exports | `data/export` holds a SQLite file and a SQL dump, but no code in the repository produces them. | Add an `export` command or document the steps. |
| Repository | Three published papers are committed. If the GitHub repository is public this is a copyright question. One of the PDFs is tracked as text. | Check the visibility. Add `*.pdf binary` to `.gitattributes`, or keep citations only. |
| Repository | The 2.8 MB HTML export of the notebook is committed and can go out of date. | Regenerate it with each notebook commit, or leave it out. |

## Notebook

The notebook is careful and its statements are supported by its tables. Sections 1 to 5 are
robust to everything above. Specific points:

- **Section 7, last two bullets.** Replace "the reason is not yet known" with the explanation
  from issue 1, and add time left after move 40 using the adjusted measure.
- **Section 6.** The duration and thinking-time charts should be regenerated after the clock
  fix. The current filter removes 29 impossible durations but not the games that are shifted by
  less than that.
- **Confidence bands.** `rate_with_ci` uses the normal approximation and treats games as
  independent. Games in the same match and by the same team are not, so the bands are too
  narrow. Use Wilson intervals and cluster by match, or label the bands as descriptive.
- **Women's and Open draw rates at the same rating.** The text already says nothing is
  controlled. It is worth adding why: a 2300–2400 game is a top-board game in the Women's
  section and a middle-board game in the Open.
- **The move-30 draw rule.** The rule against agreed draws before Black's 30th move was
  introduced at Dresden 2008 and is in the 2022 and 2024 regulations. I found no text for 2010
  or 2012. The data agree with the wording: official scores of drawn games end on half-move 59
  in 70 cases and on half-move 60 in 432. The 2010 figure (24.5% of Open draws before move 30)
  suggests the rule was not applied that year, so the 2010 regulations still need checking.
- **Filters.** The first-move query uses `n_plies >= 9` while the rest uses the `played`
  definition. Harmless, but one definition is easier to defend.
- **Forfeit rule.** Four decisive games are excluded as forfeits only because their Lichess data
  is cut short (issue 3).

## Suggested order of work

1. Fix match grouping and stop dropping games (issue 5). Everything else joins on matches.
2. Match on moves first and keep the official score as the move list (issues 3 and 4).
3. Rewrite the clock reconstruction and add the quality flag and the adjusted time-left column
   (issues 1 and 2).
4. Add a test for each fix, reload, and re-run the notebook. Keep a before-and-after table of
   the headline counts so the changes are documented.
5. Freeze the data: manifest with hashes, pinned requirements, an export command, a git tag.
6. Only then build the table the thesis needs, with the state of the other three boards at each
   move. It depends on all of the above being right.

## Appendix A: games with wrong Lichess data attached

Ids are from the current database and will change on reload, so round, table and board are
given too. "Half-moves" shows the official count and the stored Lichess count.

| game_id | Event | Round | Table.board | Players as stored | Half-moves | What is attached |
|---|---|---|---|---|---|---|
| 27296 | 2018 Open | 6 | 59.1 | Traore, Bakary – Gemy, Jose Daniel | 102 / 56 | Moves of Aguilar – Ndiaye (27297) |
| 27603 | 2018 Open | 7 | 43.4 | Merritt, Mario – Pheephacharakan, Pinatpong | 104 / 2 | Only 2 half-moves |
| 27746 | 2018 Open | 7 | 79.3 | Diaz, Brian – Kabamwanishi, Mashala | 69 / 1 | Only 1 half-move |
| 28098 | 2018 Open | 8 | 75.3 | Ahmad, Al Khatib – Mohamed Lemine, Mohamed | 23 / 1 | Only 1 half-move |
| 29005 | 2018 Open | 11 | 26.2 | Jurcik, Marian – Pelletier, Yannick | 72 / 41 | Moves of Gazik – Jenni (29007) |
| 29178 | 2018 Open | 11 | 69.3 | Brookfield, Toby – Iclicki, Willy | 1 / 81 | Moves of Gavarrete – Weldegebriel Beraki (29180) |
| 29189 | 2018 Open | 11 | 72.2 | Ndiaye, Amadou – Islam Sh.Nour, Kassim | 0 / 78 | Moves of Toure – Abdikani Nor (29191) |
| 29214 | 2018 Open | 11 | 78.3 | Ghanem, Al Shamari – Alothman, Abdul Wahab | 0 / 71 | Moves of Prudente – Hasford (29216) |
| 29215 | 2018 Open | 11 | 78.4 | Alothman, Salem – Khalifa Abdrabo Alqahtani | 1 / 75 | Moves of Mensah – Tirador (29217) |
| 29217 | 2018 Open | 11 | 79.2 | Mensah, Joseph Jamena – Tirador, Elias | 75 / 68 | Moves of Adu Jnr – Ferger (29219) |
| 29241 | 2018 Open | 11 | 85.2 | Khalema, Sechaba – Taleka, Jed Selu | 59 / 111 | Moves of Theko – Depaune (29243) |
| 31541 | 2018 Women | 8 | 53.2 | Mayar ElIdrissi, Firdaous – Wakuruwarewa, Colletta | 67 / 1 | Only 1 half-move |
| 31798 | 2018 Women | 9 | 43.3 | Makhlouf, Ahlam – Fisher, Michelle M | 98 / 1 | Only 1 half-move |
| 32634 | 2022 Open | 1 | 31.3 | Edwards, Emar – Westerberg, Jonathan | 66 / 59 | Moves of Berg – Parsons (32635) |
| 32635 | 2022 Open | 1 | 31.4 | Berg, Emanuel – Parsons, Justin | 59 / 66 | Moves of Edwards – Westerberg (32634) |
| 33360 | 2022 Open | 3 | 29.1 | Chekh Adm Khedr, Aram – Schitco, Ivan | 65 / 199 | Unknown game, differs from move 1 |
| 33361 | 2022 Open | 3 | 29.2 | Macovei, Andrei – Altorky, Amer | 0 / 65 | Moves of Chekh Adm Khedr – Schitco (33360) |
| 34485 | 2022 Open | 6 | 32.2 | Juarez Flores, Carlos A. – Sousa, Andre Ventura | 82 / 81 | Unknown game, differs from move 1 |
| 34486 | 2022 Open | 6 | 32.3 | Veiga, Jose Francisco – Lorenzana, Wilson Estuardo | 66 / 82 | Moves of Juarez Flores – Sousa (34485) |
| 34487 | 2022 Open | 6 | 32.4 | Giron Chavez, David Emanuel – Santos, Jose Guilherme | 95 / 66 | Moves of Veiga – Lorenzana (34486) |
| 34489 | 2022 Open | 6 | 33.2 | Tapia Ostolaza, Alexei – Sklyarov, Dmitry V. | 104 / 67 | Moves of Sipila – Delgado Rosas (34490) |
| 34490 | 2022 Open | 6 | 33.3 | Sipila, Vilka – Delgado Rosas, Alex Antonio | 67 / 104 | Moves of Tapia Ostolaza – Sklyarov (34489) |
| 35216 | 2022 Open | 8 | 29.1 | Batsuren, Dambasuren – Tran, Thanh Tu | 0 / 94 | Moves of Kojima – Gundavaa (35217) |
| 38454 | 2022 Women | 6 | 76.2 | Bangura, Fatmata Azzahara – Barra, Ana | 0 / 36 | Moves of Marie – Bah (38455) |

## Appendix B: queries

```sql
-- Impossible clock readings and the games they affect
SELECT game_id, ply, san, clock_s, think_s
FROM moves
WHERE clock_s = 0 OR think_s > 3600
ORDER BY game_id, ply;

-- Clocked games longer than the time control allows
SELECT game_id, n_plies, end_elapsed_s
FROM games
WHERE has_clocks AND end_elapsed_s > 2 * (7200 + 30 * ((n_plies + 1) / 2));

-- Results where TWIC and Lichess disagree
SELECT g.game_id, e.year, e.section, m.round, g.board, g.result, g.lichess_result, g.lichess_url
FROM games g JOIN matches m USING (match_id) JOIN events e USING (event_id)
WHERE g.lichess_result IS NOT NULL AND g.lichess_result NOT IN (g.result, '*')
ORDER BY 2, 3, 4;

-- Matches that are not four games between two teams on boards 1 to 4
SELECT m.match_id, e.year, e.section, m.round, m.match_no, m.n_games
FROM matches m JOIN events e USING (event_id)
WHERE m.n_games <> 4
   OR (SELECT count(DISTINCT board) FROM games g
       WHERE g.match_id = m.match_id AND board BETWEEN 1 AND 4) <> 4
   OR (SELECT count(DISTINCT t) FROM games g
       CROSS JOIN LATERAL (VALUES (g.white_team_id), (g.black_team_id)) v(t)
       WHERE g.match_id = m.match_id) <> 2;

-- README example 3, corrected: one row per complete match, for the last game to finish.
-- Returns 6,700 rows; the two matches with a tie for last place are left out.
WITH f AS (
  SELECT gf.match_id, gf.board, gf.result, gf.finish_rank,
         CASE WHEN g.white_team_id = m.team_a_id
              THEN gf.white_score ELSE 1 - gf.white_score END AS a_points
  FROM game_finish gf
  JOIN complete_matches m USING (match_id)
  JOIN games g USING (game_id))
SELECT match_id, board, result,
       (SELECT sum(o.a_points) FROM f o
        WHERE o.match_id = f.match_id AND o.finish_rank < 4) AS team_a_score_before_last
FROM f
WHERE finish_rank = 4;
```

Guard for the database test:

```python
from psycopg.conninfo import conninfo_to_dict

dbname = conninfo_to_dict(dsn).get("dbname", "")
assert dbname.endswith("_test"), f"refusing to wipe database {dbname!r}"
```

## Appendix C: prototype clock reconstruction

This is the function that produced the comparison in issue 2. It returns the same tuple per
half-move as `reconstruct_times`. Treat it as a starting point and test it before adopting it.

```python
def reconstruct_v2(plies, tc=None):
    base, inc, bonus = parse_time_control(tc)
    last = [base + inc, base + inc]        # latest reading per side
    pending = [0, 0]                       # half-moves since that reading
    got_bonus = [False, False]
    bonus_ply = [None, None]
    granted = [base + inc, base + inc]     # total time given to each side so far
    out = []
    elapsed_max = 0
    for i, p in enumerate(plies):
        side = i % 2
        move_no = i // 2 + 1
        pending[side] += 1
        c = p.clock_s
        ceiling = base + inc * (move_no + 1) + (bonus if move_no >= 30 else 0) + 60
        if c is None or c == 0 or c > ceiling:          # impossible reading: treat as missing
            out.append((None, None, None, False))
            continue
        raw = last[side] + inc * pending[side] - c
        flag = False
        if not got_bonus[side] and move_no >= 30 and raw < -30:
            opp = bonus_ply[1 - side]
            opp_just_got_it = opp is not None and i - opp <= 3
            flag_fell = raw + bonus >= last[side] - 5   # used more than was left on the clock
            if raw < -600 or opp_just_got_it or flag_fell:
                flag = True
                got_bonus[side] = True
                bonus_ply[side] = i
                granted[side] += bonus
                raw += bonus
        granted[side] += inc * pending[side]
        span = pending[side]
        pending[side] = 0
        last[side] = c
        elapsed = (granted[0] - last[0]) + (granted[1] - last[1])
        elapsed_max = max(elapsed_max, elapsed)          # keep the timeline monotonic
        out.append((max(0, raw), span, elapsed_max, flag))
    return out
```

## Sources

- DGT 3000 manual, period change without a move counter:
  https://www.manualslib.de/manual/405465/Dgt-3000.html?page=15
- FIDE Chess Olympiad 2022 regulations (draw by agreement before Black's 30th move):
  https://handbook.fide.com/files/handbook/Olympiad2022MainCompetition.pdf
- FIDE Chess Olympiad 2024 regulations: https://www.fide.com/FIDE/handbook/Olympiad2024.pdf
- Report on the rule's introduction at Dresden 2008: https://chessdailynews.com/?p=38289
