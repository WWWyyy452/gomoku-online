# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

五子棋 (Gomoku) — a real-time online multiplayer Gomoku (Five-in-a-Row) game. Single-page web app with a Python backend. No build step, no framework on the frontend, no database.

- **Backend**: Flask + Flask-SocketIO (eventlet async mode), single file `app.py` (~1600 lines)
- **Frontend**: Single `templates/index.html` with embedded vanilla JS + Canvas rendering (~937 lines). No bundler, no npm.
- **Deployment**: Heroku (see `Procfile`). `PORT` env var controls listen port; defaults to 5000.

## Commands

```bash
python app.py              # start server (default port 5000)
PORT=8000 python app.py    # custom port
ruff check .               # lint (ruff 0.15.x is cached; install with `pip install ruff`)
pip install -r requirements.txt  # install deps
```

No test suite exists. To verify behavior manually: start the server, open two browser tabs, create a room in one and join with the room code from the other.

## Architecture

### Board Representation

- 15×15 grid (`GRID = 15`), stored as a 2D list: `0` = empty, `1` = black, `2` = white.
- Player identity is always 1 or 2. Opponent is computed as `3 - player` — this pattern recurs everywhere; don't break it.
- Coordinates are `(row, col)`, i.e. `(r, c)`, zero-indexed.

### Room & Game State

All state lives in the in-memory `rooms` dict keyed by room ID (auto-incrementing integer string starting at 10001). Each room holds:

```
board, players (list of socket SIDs), names, turn, over (bool),
moves (history), roles (SID → player number), ai_enabled, ai_on, ai_player
```

Because state is process-local, the server runs single-worker only (see bottom of app.py). Do not add multi-worker mode without replacing `rooms` with a shared store (e.g. Redis).

### SocketIO Events

Outbound events the frontend listens to: `room_created`, `waiting`, `room_joined`, `game_start`, `opponent_move`, `game_restarted`, `opponent_left`, `error_msg`, `undo_asked`/`undo_done`/`undo_denied`, `ai_status`, `ai_thinking`.

Inbound events the backend handles: `create_room`, `set_name`, `join_room`, `move`, `toggle_ai`, `restart`, `undo_request`/`undo_accept`/`undo_reject`, `leave`.

Roles (who plays black/white) are randomly assigned **at join time** and **re-randomized on every restart**, not fixed to creator/joiner.

### Scoring Race Condition

The frontend's local `tryPlace` advances `currentPlayer` immediately for responsiveness, but authoritative win/turn logic lives server-side in `on_move`. The server sends back `next_turn` and `result` with `opponent_move`; the client trusts the server. Keep this split — don't try to make the server echo the client's optimistic state.

### AI Subsystem

`GomokuAI` class (line ~585) is a Gomoku engine with: iterative deepening (default max_depth=6, time_limit=2.0s), alpha-beta pruning with PVS, transposition table, killer-move heuristic, history table, VCF/VCT forcing-sequence search, double-threat detection, jump-four and cross-shape detection.

The AI is a **hidden feature**: creating a room with the name `"st"` (case-insensitive) enables `ai_enabled` on that room. The AI then plays the creator's stones when the user toggles it on via the "启用AI替我下棋" button. AI moves run in an eventlet greenthread (`_execute_ai_move`) with a variable "thinking" delay tuned to board complexity.

Key AI entry points:
- `find_immediate_win` / `find_immediate_block` — tactical shortcuts checked before the full search
- `best_move(board, player)` — the main search, returns `(r, c)`
- `evaluate_move` — static scoring of a single candidate (attack × 1.5 + defend × 1.2 + threat/block/connectivity/position bonuses)

### Candidate Generation

`get_candidates` restricts search to empty cells within a 2-cell radius of existing stones. On an empty board it returns only the center `(7, 7)`. This is the main performance lever — the search space stays small because of it.

## Conventions

- All UI text is in Chinese (Simplified). Keep new strings consistent.
- The frontend has no module system — everything is global scope in one `<script>` block. JS functions and variables are top-level; don't introduce ES modules without also changing how the file is served.
- `DEFEND_MULT = 1.2` and the `attack_score * 1.5` weight tilt the AI slightly offense-leaning. Tune these together if rebalancing.
- The win condition is exactly 5 in a row (`check_win` uses `cnt >= 5`, so overlines count as wins too).
