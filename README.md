# Chess Olympiad database (2010–2026)

Builds a PostgreSQL database of every Chess Olympiad game from 2010 to 2026,
Open and Women's sections, with per-move clock times and engine evaluations
where they exist.

| Years | Games, teams, boards, results | Clock times + engine evals |
|---|---|---|
| 2010, 2012, 2014, 2016 | yes (The Week in Chess) | no |
| 2018, 2022, 2024, 2026 | yes (The Week in Chess) | yes, for broadcast games (Lichess) |
| 2020, 2021 | skipped: online rapid Olympiads | |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
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
python olympiad_pipeline.py download     # ~40 files into data/raw (Lichess part takes 30-60 min: rate limited)
python olympiad_pipeline.py load         # parse, match and load; prints a coverage table at the end
python olympiad_pipeline.py summary      # print the coverage table again
```

Both steps are safe to re-run: downloaded files are cached (`--force` re-downloads),
and loading an Olympiad again replaces its rows. `--years 2024 2026` limits either
step to some years.

If The Week in Chess blocks the script's download, open
`https://theweekinchess.com/assets/files/pgn/olym24.pgn` (etc.) in a browser and save
the files into `data/raw/twic/` with the names listed in `TWIC_FILES`.

## Tests

```bash
pytest -q tests                                          # parsing, clocks, matching
TEST_DATABASE_URL=postgresql://chess:chess@localhost:5432/chess_test pytest -q tests   # + database load
```

The database test drops and recreates the `public` schema of the database you give it,
so use a separate test database (`createdb chess_test`), never the real one.

## Tables

| Table | One row per | Key columns |
|---|---|---|
| `events` | Olympiad section | year, section (`open`/`women`), has_clocks |
| `teams` | team | name |
| `players` | player | fide_id, name |
| `matches` | team match | event, round, match_no, team_a (White on board 1), team_b, score_a, score_b |
| `games` | game | match, board, players, teams, ratings, result, has_clocks, clock_coverage, end_elapsed_s, raw PGNs |
| `moves` | half-move | ply, san, clock_s, think_s, elapsed_s, eval_cp, eval_mate |
| `unmatched_lichess` | Lichess game that could not be matched to a TWIC game | for manual checking |

Views: `game_finish` (finish order of the games in each match), `complete_matches`
(matches where all four games have clock data).

## How the timing is reconstructed

The PGNs record the **remaining clock time after each move** (`%clk`), not timestamps.

* Thinking time of a move = the player's previous clock + 30 s increment − current clock.
* The 30-minute bonus after move 40 is credited when a clock jumps up by more than
  10 minutes after move 30 (digital clocks sometimes add it a few moves late).
* One clock is always running, so the time since the round started is the sum of both
  players' thinking times. All four games of a match start together, so `elapsed_s`
  puts the four boards on one timeline.
* Not captured: arbiter stoppages, clock corrections, and time spent after the last move
  (e.g. agreeing a draw). Games finishing within ~3 minutes of each other cannot be ordered reliably.
* `clock_coverage` is the share of moves with a clock reading; `has_clocks` is true at ≥ 90 %.
  Missing readings are bridged: the next known reading's think time covers several moves
  (`think_plies` > 1).

## How Lichess games are matched to TWIC games

Within the same Olympiad, section and round: by FIDE IDs when Lichess has them
(2024, 2026), otherwise by player names (word overlap, so "So Wesley" = "So, Wesley")
confirmed by the first 10 moves. `games.match_quality` records which rule matched.
The official result is TWIC's; Lichess's is kept in `lichess_result`.

## Example queries

```sql
-- Coverage per Olympiad
SELECT e.year, e.section, count(*) games, count(*) FILTER (WHERE g.has_clocks) with_clocks
FROM games g JOIN matches m USING (match_id) JOIN events e USING (event_id)
GROUP BY 1, 2 ORDER BY 1, 2;

-- Draw rate by board
SELECT g.board, avg((g.result = '1/2-1/2')::int) draw_rate, count(*)
FROM games g GROUP BY g.board ORDER BY g.board;

-- The last game to finish in each complete match, with the score of the other three
WITH f AS (
  SELECT gf.*, m.team_a_id, g.white_team_id
  FROM game_finish gf JOIN complete_matches m USING (match_id) JOIN games g USING (game_id))
SELECT match_id, board, result,
       sum(CASE WHEN white_team_id = team_a_id THEN white_score ELSE 1 - white_score END)
         FILTER (WHERE finish_rank < 4) OVER (PARTITION BY match_id) AS team_a_score_before_last
FROM f;
```

## Sources

* The Week in Chess, Olympiad game files: https://theweekinchess.com
* Lichess broadcasts (open API, PGN export per tour): https://lichess.org/broadcast
