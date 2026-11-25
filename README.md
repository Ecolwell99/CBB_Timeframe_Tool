# CBB Timeframe Tool

Streamlit tool that results the **Timeframe Both Teams To Score** market for men's
college basketball, live from ESPN play-by-play.

- One-minute windows for each 20-minute half: `20:00-19:00`, `18:59-18:00`, ... `0:59-0:00`.
- Both halves side by side; overtimes (5 minutes, `5:00-4:00` ... `0:59-0:00`) appear below when reached.
- **Yes**: both teams scored (field goal or free throw) inside the window.
  **No**: window complete and both teams did not score. **-**: not reached / in progress.
- A window is labelled lower-bound inclusive: a score at exactly 19:00 belongs to `20:00-19:00`.
  The first row of each period starts at the period start (20:00, or 5:00 in OT). Set
  `OPENING_WINDOW_FROM_PERIOD_START = False` in `app.py` to label it `19:59-19:00`, NBA style.
- A window only goes **No** once the clock has gone *below* its lower bound, so free throws
  at a stoppage on exactly 19:00 still count for `20:00-19:00`.
- The highlighted row is the window the clock is in. **Invert** flips row order.

Logic is ported from the NBA Live Market Tracker's timeframe table; only the period
lengths differ.

## Run

```
pip install -r requirements.txt
streamlit run app.py
```

Sidebar: **Load Live Games** (or paste an ESPN game ID / URL) → pick a game → **Track Game**.
Polls every 4 seconds while live (15 seconds before tip), with a cooldown after an HTTP 429.

## Data source

`site.api.espn.com/.../basketball/mens-college-basketball`. The scoreboard is called with
`groups=50&limit=500` (all of Division I); without `groups` ESPN returns only featured games.
Keep the `python-requests/...` User-Agent prefix: ESPN's CDN 403s a browser UA.
