# Poker HUD + GTO Solver

A full-featured No-Limit Texas Hold'em poker HUD and GTO solver built with React + Python FastAPI.

## Features

### HUD (Heads-Up Display)
- **Core Stats**: VPIP, PFR, 3-Bet%, AF, WTSD, W$SD, C-Bet%, Fold to 3-Bet
- **Advanced Stats**: Squeeze%, Check-Raise%, Donk Bet%, Steal%, 4-Bet%, Limp%, positional breakdowns
- **Hand History Parser**: Import PokerStars format hand histories
- **Session Tracking**: Profit/loss graphs, bb/100 win rates, bankroll management
- **Table View**: Visual poker table with player HUD overlays
- **Player Notes**: Tag and categorize opponents (fish, reg, whale, etc.)

### GTO Solver
- **CFR+ Algorithm**: Counterfactual Regret Minimization Plus for Nash equilibrium computation
- **Preflop & Postflop**: Solve any spot with configurable bet sizings
- **Range Visualization**: Interactive 13x13 hand matrix with frequency coloring
- **Equity Calculator**: Hand vs range and range vs range equity calculations
- **Strategy Display**: Multi-color range grids showing fold/call/raise frequencies
- **Board Texture Analysis**: Dry, wet, paired, monotone classification

## Tech Stack

- **Frontend**: React 18 + TypeScript + Tailwind CSS + Vite
- **Backend**: Python FastAPI + NumPy
- **Solver**: CFR+ with Monte Carlo variant (MCCFR) for large game trees
- **Communication**: REST API + WebSocket for real-time updates

## Quick Start

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — the frontend proxies API calls to the backend.

## Project Structure

```
poker-hud/
├── backend/
│   ├── main.py              # FastAPI application
│   ├── requirements.txt
│   ├── api/
│   │   ├── models.py        # Pydantic request/response schemas
│   │   ├── solver_routes.py  # GTO solver endpoints
│   │   └── hud_routes.py    # HUD stats endpoints
│   ├── solver/
│   │   ├── card.py          # Card/hand evaluation engine
│   │   ├── equity.py        # Monte Carlo equity calculator
│   │   ├── ranges.py        # Preflop range management
│   │   ├── game_tree.py     # Game tree construction
│   │   └── cfr.py           # CFR+ solver algorithm
│   └── hud/
│       ├── parser.py        # PokerStars hand history parser
│       ├── stats.py         # HUD statistics calculator
│       └── tracker.py       # Session and player tracking
└── frontend/
    ├── src/
    │   ├── App.tsx           # Main app with routing
    │   ├── api.ts            # API client
    │   ├── types.ts          # TypeScript interfaces
    │   ├── pages/            # Page components
    │   └── components/       # Reusable UI components
    │       ├── HUD/          # HUD display components
    │       └── Solver/       # Solver UI components
    └── package.json
```

## API Endpoints

### Solver
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/solver/solve` | Solve a poker spot |
| POST | `/api/solver/equity` | Calculate hand equity |
| GET | `/api/solver/ranges/{position}` | Default range for position |
| POST | `/api/solver/ranges/visualize` | Range grid visualization |
| WS | `/api/solver/solve-stream` | Stream solver progress |

### HUD
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/hud/upload` | Upload hand history files |
| GET | `/api/hud/players` | List tracked players |
| GET | `/api/hud/players/{name}/stats` | Player statistics |
| GET | `/api/hud/players/{name}/stats/positional` | Positional breakdown |
| POST | `/api/hud/session/start` | Start tracking session |
| GET | `/api/hud/table` | Table view with all HUDs |

## HUD Stats Reference

| Stat | Description | Good Range |
|------|-------------|------------|
| VPIP | Voluntarily Put $ In Pot | 18-25% |
| PFR | Pre-Flop Raise | 15-22% |
| 3-Bet | Three-bet frequency | 6-10% |
| AF | Aggression Factor | 2.0-3.5 |
| WTSD | Went To Showdown | 25-32% |
| W$SD | Won $ at Showdown | 50-55% |
| C-Bet | Continuation Bet | 60-75% |
| Steal | Steal attempt frequency | 30-40% |

## License

MIT
