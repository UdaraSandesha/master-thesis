# Chess Olympiad database (2010–2026)

Builds a PostgreSQL database of every Chess Olympiad game from 2010 to 2026,
Open and Women's sections, with per-move clock times and engine evaluations
where they exist.

| Years | Games, teams, boards, results, moves | Clock times + engine evals |
|---|---|---|
| 2010, 2012, 2014, 2016 | yes (The Week in Chess) | no |
| 2018, 2022, 2024, 2026 | yes (The Week in Chess) | yes, for broadcast games (Lichess) |
| 2020, 2021 | skipped: online rapid Olympiads | |

`docs/analysis.md` is a review of the first version of the pipeline and `docs/changes.md`
lists what was changed after it.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                        # or requirements.lock for the exact versions used
brew install postgresql@16
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"

# First time only: create the database in data/postgres (enter "chess" as the password)
initdb -D data/postgres -U chess --pwprompt --auth=scram-sha-256 -E UTF-8
pg_ctl -D data/postgres -l data/postgres.log start
PGPASSWORD=chess createdb -h localhost -U chess chess_olympiad

export DATABASE_URL=postgresql://chess:chess@localhost:5432/chess_olympiad
```

The server does not start by itself after a restart. Start it with
`pg_ctl -D data/postgres -l data/postgres.log start`, and stop it with
`pg_ctl -D data/postgres stop`.

Already have PostgreSQL installed? Create a database and point `DATABASE_URL` at it.

## Run

```bash
python olympiad_pipeline.py download     # 101 files into data/raw (Lichess part takes 30-60 min: rate limited)
python olympiad_pipeline.py load         # parse, match and load; prints repaired labels and a coverage table
python olympiad_pipeline.py summary      # print the coverage table again
python olympiad_pipeline.py export       # data/export/chess_olympiad.sqlite and chess_olympiad.sql.gz
```

Both steps are safe to re-run: downloaded files are cached (`--force` re-downloads),
and loading an Olympiad again replaces its rows. `--years 2024 2026` limits either
step to some years. After a change to the pipeline use `load --rebuild`, which drops
the pipeline's tables first so that no row of the older version remains.

`game_id` and `match_id` are assigned on load and change when the data is loaded again.
Outside the database, identify a game by year, section, round, table (`match_no`) and board.

`download` also writes `data/raw/manifest.json` with the size, SHA-256 and fetch time of
every raw file. It is the only file under `data/` that is kept in git. Lichess files can
change after an event (evaluations are added later), so the manifest is what identifies
the data a database was built from.

If The Week in Chess blocks the script's download, open
`https://theweekinchess.com/assets/files/pgn/olym24.pgn` (etc.) in a browser and save
the files into `data/raw/twic/` with the names listed in `TWIC_FILES`.

## Tests

```bash
pytest -q tests                                          # parsing, clocks, matches, matching
TEST_DATABASE_URL=postgresql://chess:chess@localhost:5432/chess_test pytest -q tests   # + database load and export
```

The database test drops and recreates the `public` schema of the database you give it.
It refuses to run unless the database name ends in `_test` (`createdb chess_test`).

## Tables

| Table | One row per | Key columns |
|---|---|---|
| `events` | Olympiad section | year, section (`open`/`women`), has_clocks |
| `teams` | team name | name |
| `players` | player | fide_id, name |
| `matches` | team match | event, round, match_no (table), team_a (White on board 1), team_b, score_a, score_b, n_games |
| `games` | game | match, board, players, teams, ratings, result, n_plies, has_clocks, clock_ok, end_elapsed_s, raw PGNs |
| `moves` | half-move | ply, san, clock_s, think_s, elapsed_s, time_left_s, eval_cp, eval_mate |
| `unmatched_lichess` | Lichess game that was not attached to a game | reason |

Views: `game_finish` (finish order of the games in each match, with the gap to the next
game to finish) and `complete_matches` (four games on boards 1–4, all with clock data;
`all_clocks_ok` says whether all four pass the clock checks).

Columns that need a word of explanation:

| Column | Meaning |
|---|---|
| `games.board_source` | `header`: board number from the TWIC file. `inferred`: worked out from the colours because the file had none or a wrong one. |
| `games.moves_source` | `twic`: the official score. `lichess`: TWIC has no moves for the game, so the broadcast moves are stored. |
| `games.match_quality` | How the Lichess game was found: `moves`, `moves_only` (the broadcast shows other player names), `fide_id`, `names`. |
| `games.lichess_extra_plies` | Half-moves in the broadcast that are not in the official score. They are not stored. |
| `games.has_clocks` | At least 90% of the moves have a usable clock reading. |
| `games.clock_ok` | `has_clocks`, and no unexplained jump of a clock, no single move above 45 minutes, and a reading on the last move. |
| `games.clock_bad_readings`, `clock_jumps` | Readings that were discarded, and times a clock went up for no known reason. |
| `games.end_elapsed_s` | Time of the last timed move, in seconds since the round started. |
| `moves.think_s` | Time used for the move. Empty when it is not known. `think_plies` > 1 means it covers several moves. |
| `moves.time_left_s` | Clock reading, made comparable across years: see the bonus below. |

Teams are identified by name only. The same name in the Open and Women's sections is one
row, and a team whose name changes between years (`RUS1` in 2010, `Russia` later) is two.
Players are identified by FIDE id. 267 players have none and are identified by name.

## How matches are formed

A match is all games of a round between the same two teams. The table number in the TWIC
file is not used for grouping, because it is wrong for some games (in the 2024 files one
game of table 18 is filed under table 70 in every round). Team A is the team with White on
boards 1 and 3. A board number that is missing, repeated or on the wrong colour is worked
out from the colours when only one board fits. `load` prints every such repair.

The TWIC files leave out some games that were not played, so 39 matches have fewer than
four games.

## How Lichess games are matched to TWIC games

The moves decide. A Lichess game is attached to the TWIC game of the same round with the
same first 30 half-moves, whatever names the broadcast shows: broadcasts sometimes have
boards mixed up, and names are spelled differently in the two sources. Games too short
for that, or without moves in TWIC, are matched by FIDE ids or by names, and only if the
moves that exist do not contradict each other. `games.match_quality` records which rule
matched.

The TWIC score is the game. Clock readings and evaluations are attached to its moves as
far as the two scores agree. Moves that only the broadcast has are left out: they are
mostly kings placed on a centre square after the game to signal the result, which the
electronic board records as moves. The official result is TWIC's; Lichess's is kept in
`lichess_result`.

## How the timing is reconstructed

The PGNs record the **remaining clock time after each move** (`%clk`), not timestamps.

* Thinking time of a move = the player's previous clock + 30 s increment − current clock.
* One clock is always running, so the time since the round started is the time both
  players have used. All four games of a match start together, so `elapsed_s` puts the
  four boards on one timeline. It is taken from the latest reading of each clock, so one
  wrong reading cannot shift the rest of the game.
* **The 30-minute bonus.** In 2018 the clocks counted moves and added the bonus when a
  player completed move 40. From 2022 the clocks do not count moves: they add the bonus
  to both clocks at the moment the first player's time runs out, and never if neither
  player runs out (about one game of 45+ moves in five). `bonus_added` marks the reading
  on which it first shows.
* `time_left_s` is the clock reading plus the 30 minutes from move 40 on while the clock
  does not show them yet. Use it, not `clock_s`, to compare time left across years.
* **Bad readings** are left out: 0:00:00 and other impossible values, a clock that repeats
  the same time four or more moves in a row, and a reading that the readings before and
  after it contradict. A clock that goes up and stays up for no known reason is counted
  in `clock_jumps` (arbiters add 2 minutes after an illegal move).
* Not captured: arbiter stoppages and the time between the last move and the end of the
  game. Games finishing within ~3 minutes of each other cannot be ordered reliably;
  `game_finish.gap_to_next_s` gives the gap.

In 2018 the broadcast of rounds 1–4 covers only the top 32 tables, so the clock data of
that year is not a random sample.

## Example queries

```sql
-- Coverage per Olympiad
SELECT e.year, e.section, count(*) games, count(*) FILTER (WHERE g.has_clocks) with_clocks,
       count(*) FILTER (WHERE g.clock_ok) clocks_ok
FROM games g JOIN matches m USING (match_id) JOIN events e USING (event_id)
GROUP BY 1, 2 ORDER BY 1, 2;

-- Draw rate by board
SELECT g.board, avg((g.result = '1/2-1/2')::int) draw_rate, count(*)
FROM games g GROUP BY g.board ORDER BY g.board;

-- The last game to finish in each complete match, with the score of the other three
-- and how long before it the third game ended (one row per match)
WITH f AS (
  SELECT gf.match_id, gf.board, gf.result, gf.finish_rank, gf.gap_to_next_s,
         CASE WHEN g.white_team_id = m.team_a_id
              THEN gf.white_score ELSE 1 - gf.white_score END AS a_points
  FROM game_finish gf
  JOIN complete_matches m USING (match_id)
  JOIN games g USING (game_id)
  WHERE m.all_clocks_ok)
SELECT match_id, board, result,
       (SELECT sum(o.a_points) FROM f o
        WHERE o.match_id = f.match_id AND o.finish_rank < 4) AS team_a_score_before_last,
       (SELECT o.gap_to_next_s FROM f o
        WHERE o.match_id = f.match_id AND o.finish_rank = 3) AS seconds_after_third_game
FROM f
WHERE finish_rank = 4;
```

## Sources

* The Week in Chess, Olympiad game files: https://theweekinchess.com
* Lichess broadcasts (open API, PGN export per tour): https://lichess.org/broadcast
