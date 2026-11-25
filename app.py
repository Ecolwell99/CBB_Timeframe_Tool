"""
CBB Timeframe Tool
==================

Internal tool for sportsbook traders / QC analysts.

Results the Timeframe Both Teams To Score market for men's college basketball,
one-minute windows per half: "20:00-19:00", "18:59-18:00", ... "0:59-0:00",
plus five-minute overtimes ("5:00-4:00" ... "0:59-0:00") when a game goes there.

The market logic is a port of the NBA Live Market Tracker's timeframe table
(`timeframe_windows` / `window_index` / `timeframe_table` in
nba_live_tracker/app.py), with the period lengths changed for college: two
20-minute halves instead of four 12-minute quarters. Sidebar, game header,
status line, rate-limit cooldown and styling are the house pattern from the
same tool.

Data source
-----------
ESPN's public men's college basketball endpoints (SECTION 3). Same payload
shape as the NBA feed - checked on game 401906774 (Milwaukee @ Florida,
2026-11-24): period.number 1 / 2 are the halves, clock.displayValue is "20:00"
down to "0:00", `scoringPlay` / `scoreValue` / `team.id` on every scoring play,
and the per-team sum of scoreValue equals the official final (76-67). The
scoreboard needs `groups=50` (all of Division I) or it returns only a handful
of featured games.

All source-specific parsing is confined to SECTION 3 (fetch) and SECTION 4
(normalise). Everything downstream works only on the neutral `GameEvent`.
"""

from __future__ import annotations

import html
import re
from copy import deepcopy
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any, Sequence

import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:  # pragma: no cover - surfaced in the UI instead of crashing
    st_autorefresh = None


# ===========================================================================
# SECTION 1 - CONFIGURATION
# Everything a trader might want to retune lives here.
# ===========================================================================

APP_TITLE = "CBB Timeframe Tool"

# Auto-refresh cadence while a game is tracked (seconds), same as the NBA tool.
REFRESH_SECONDS = 4

# Slower cadence before tip-off: nothing can change yet, so do not hammer the feed.
PREGAME_REFRESH_SECONDS = 15

# Men's college: two 20-minute halves, 5-minute overtimes.
REGULATION_PERIODS = 2
HALF_SECONDS = 1200
OVERTIME_PERIOD_SECONDS = 300

# Timeframe table ordering. "chronological" -> 20:00-19:00 first.
# "reverse" -> 0:59-0:00 first. The Invert button flips it per session.
TIMEFRAME_ORDER = "chronological"

# Label for the opening window of each period. The window itself always covers
# 20:00 down to 19:00 (20:00 is folded in, exactly as 12:00 is in the NBA tool);
# this only changes what the first row SAYS.
#   False -> "19:59-19:00"  (matches the NBA tool's "11:59-11:00")
#   True  -> "20:00-19:00"
OPENING_WINDOW_FROM_PERIOD_START = True

# Cooldown after an HTTP 429: skip this many refresh ticks before polling again,
# as the NHL / NFL / MLB / NBA tools all do.
RATE_LIMIT_SKIP_TICKS = 2

HTTP_TIMEOUT = 12
DASH = "-"

# Team logo size in the game header (px), same as the NBA tool.
LOGO_PX = 56

# Team accent colour for the edges of the game header: ESPN's primary `color` if it
# reads against the dark card background, else `alternateColor`, else grey. Same
# 2:1 rule as the NBA tool (see `team_accent` there for the reasoning).
ACCENT_MIN_CONTRAST = 2.0
ACCENT_FALLBACK = "#8a93a3"


# ===========================================================================
# SECTION 2 - STYLING
# The house style: near-empty stylesheet, everything else drawn from the active
# Streamlit theme. Blocks below are the NBA tool's, trimmed to what this uses.
# ===========================================================================

CSS = """
<style>
/* padding-top must clear Streamlit's own top bar (2.875rem), which overlaps the
   main block - at 1rem the game header sits underneath it, unreachable. */
.block-container { padding-top: 3.5rem; padding-bottom: 1rem; }

.sect {
  font-size: 15px; text-transform: uppercase; letter-spacing: .06em;
  font-weight: 800; color: var(--text-color);
  margin: 20px 0 8px 0; padding: 0 0 0 10px;
  border-left: 3px solid var(--primary-color, #5b8dd6);
}
.note { font-size: 11px; color: var(--text-color); opacity: .55; margin: 3px 0 0 0; }

/* --- game header --- */
.gh {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 16px;
  padding: 14px 26px; margin: 0 0 10px 0; border-radius: 12px;
  background: rgba(128,128,128,0.05);
  border: 1px solid var(--secondary-background-color);
  color: var(--text-color);
  position: relative; overflow: hidden;
}
.gh::before, .gh::after { content: ""; position: absolute; top: 0; bottom: 0; width: 5px; }
.gh::before { left: 0; background: var(--away, transparent); }
.gh::after { right: 0; background: var(--home, transparent); }
.gh .tm { display: flex; align-items: center; gap: 16px; min-width: 0; }
.gh .tm.h { justify-content: flex-end; }
.gh .lg { flex: 0 0 auto; object-fit: contain; }
.gh .nm {
  font-size: 22px; font-weight: 800; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.gh .sc { font-size: 46px; font-weight: 900; line-height: 1; margin: 0 10px; flex: 0 0 auto; }
.gh .mid {
  text-align: center; padding: 0 40px;
  border-left: 1px solid rgba(128,128,128,.25); border-right: 1px solid rgba(128,128,128,.25);
}
.gh .ck { font-size: 24px; font-weight: 800; white-space: nowrap; }
.gh .pd { font-size: 14px; font-weight: 700; opacity: .7; margin-left: 6px; }
.gh .up { font-size: 12px; opacity: .55; margin-top: 3px; white-space: nowrap; }

/* --- compact status line (the normal, nothing-wrong state) --- */
.statusline {
  display: flex; align-items: center; gap: 8px; margin: 0 0 12px 0;
  font-size: 12px; font-weight: 700; color: var(--text-color); opacity: .85;
}
.statusline.inrow { margin: 0; white-space: nowrap; }
.statusline .dot {
  width: 8px; height: 8px; border-radius: 50%; background: #2ecc71; flex: 0 0 auto;
}

/* --- timeframe table (the NBA tool's, unchanged) --- */
.tfwrap { margin: 2px 0 0 0; }
.tf { border-collapse: collapse; width: auto; min-width: 300px; max-width: 380px; }
.tf th {
  font-size: 11px; text-transform: uppercase; letter-spacing: .06em; font-weight: 800;
  text-align: left; padding: 5px 16px 5px 12px; white-space: nowrap;
  color: var(--text-color); opacity: .7;
  border-bottom: 2px solid var(--secondary-background-color);
}
.tf td {
  font-size: 13px; padding: 5px 16px 5px 12px; white-space: nowrap;
  color: var(--text-color); border-left: 3px solid transparent;
}
.tf tr:nth-child(even) td { background: rgba(128,128,128,0.05); }
.tf td.v { font-weight: 800; }
.tf td.yes { color: #6fd68d; }
.tf td.no { color: #e08a80; }
.tf td.na { opacity: .35; font-weight: 700; }
/* Current window. After the striping, so it wins on equal specificity. */
.tf tr.cur td { background: rgba(255,153,0,0.10); font-weight: 800; }
.tf tr.cur td:first-child { border-left-color: #ff9900; }

/* Invert: same height as a selectbox, like the NBA tool's. */
.st-key-tf_invert button { width: 100%; min-height: 40px; }
</style>
"""


# ===========================================================================
# SECTION 3 - DATA SOURCE (ESPN)
# The only place that knows about ESPN's URL shapes, alongside SECTION 4.
# ===========================================================================

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"

# ESPN's image resizer: the published logos are 500x500; this returns them sized.
ESPN_IMG_COMBINER = "https://a.espncdn.com/combiner/i"

# DO NOT put a browser User-Agent here. ESPN sits behind Akamai, which matches the
# LEADING token of the UA against known HTTP clients and 403s anything else -
# including a spoofed Chrome string (measured on the NBA tool, 2026-09-06). Keep a
# real `python-requests/<version>` prefix first, then identify ourselves.
_HTTP_HEADERS = {
    "User-Agent": f"python-requests/{requests.__version__} cbb-timeframe-tool/1.0",
    "Accept": "application/json, text/plain, */*",
}


class DataSourceError(Exception):
    """Any failure talking to the feed. Rendered as a message, never a traceback."""


class RateLimitedError(DataSourceError):
    pass


@dataclass(frozen=True)
class TeamInfo:
    team_id: str
    abbr: str
    display_name: str
    # Defaulted on purpose: TeamInfo(**dict) rehydrates dicts held in
    # session_state, which can outlive an edit to this file.
    logo: str = ""
    color: str = ""
    alt_color: str = ""


@dataclass(frozen=True)
class GameInfo:
    game_id: str
    start_iso: str
    state: str          # "pre" | "in" | "post"
    status_detail: str  # e.g. "7:00 PM ET", "12:34 - 1st Half", "Final"
    away: TeamInfo
    home: TeamInfo
    away_score: int
    home_score: int

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    @property
    def is_final(self) -> bool:
        return self.state == "post"


def _http_get_json(url: str) -> dict:
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    except requests.exceptions.Timeout as exc:
        raise DataSourceError("Feed timed out.") from exc
    except requests.exceptions.RequestException as exc:
        raise DataSourceError(f"Network error reaching the feed: {exc.__class__.__name__}") from exc

    if resp.status_code == 429:
        raise RateLimitedError("Rate limited by the feed (HTTP 429). Backing off.")
    if resp.status_code == 404:
        raise DataSourceError("Game not found on the feed (HTTP 404).")
    if resp.status_code == 403:
        raise DataSourceError(
            "Feed refused the request (HTTP 403) - bot filtering, not a bad game ID. "
            "Check the User-Agent in _HTTP_HEADERS."
        )
    if resp.status_code >= 500:
        raise DataSourceError(f"Feed is unavailable (HTTP {resp.status_code}).")
    if resp.status_code != 200:
        raise DataSourceError(f"Feed returned HTTP {resp.status_code}.")

    try:
        return resp.json()
    except ValueError as exc:
        raise DataSourceError("Feed returned a malformed (non-JSON) response.") from exc


def _logo_src(raw: dict) -> str:
    """Display-ready logo URL, or "". The scoreboard gives `team.logo` (a string),
    the summary header gives `team.logos` (a list of {href}); both are read here."""
    href = raw.get("logo")
    if not isinstance(href, str) or not href:
        href = ""
        for entry in raw.get("logos") or []:
            if isinstance(entry, dict) and isinstance(entry.get("href"), str) and entry["href"]:
                href = entry["href"]
                break
    if not href:
        return ""
    px = LOGO_PX * 2  # 2x, so it stays sharp on a high-DPI screen
    return f"{ESPN_IMG_COMBINER}?img={href}&w={px}&h={px}"


def _hex_color(value: Any) -> str:
    """ESPN's "1d428a" -> "#1d428a"; anything that is not six hex digits -> ""."""
    text = str(value or "").strip().lstrip("#")
    return f"#{text.lower()}" if re.fullmatch(r"[0-9a-fA-F]{6}", text) else ""


def _team_from_raw(raw: dict) -> TeamInfo:
    return TeamInfo(
        team_id=str(raw.get("id", "")),
        abbr=(raw.get("abbreviation") or raw.get("shortDisplayName") or "").upper(),
        display_name=raw.get("displayName") or raw.get("name") or "",
        logo=_logo_src(raw),
        color=_hex_color(raw.get("color")),
        alt_color=_hex_color(raw.get("alternateColor")),
    )


def _int_score(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _game_dict(event_id: str, comp: dict, status: dict) -> dict | None:
    """One competition -> the plain-dict game shape kept in session_state.
    Shared by the scoreboard and the manual-ID path so both feed identical code."""
    stype = status.get("type") or {}
    away_raw = home_raw = None
    away_score = home_score = 0
    for c in comp.get("competitors") or []:
        side = c.get("homeAway")
        if side == "home":
            home_raw, home_score = c.get("team") or {}, _int_score(c.get("score"))
        elif side == "away":
            away_raw, away_score = c.get("team") or {}, _int_score(c.get("score"))
    if not away_raw or not home_raw:
        return None
    return {
        "game_id": event_id,
        "start_iso": comp.get("date") or "",
        "state": stype.get("state", "pre"),
        "status_detail": stype.get("shortDetail") or stype.get("description") or "",
        "away": asdict(_team_from_raw(away_raw)),
        "home": asdict(_team_from_raw(home_raw)),
        "away_score": away_score,
        "home_score": home_score,
    }


def _game_from_dict(g: dict) -> GameInfo:
    return GameInfo(
        game_id=g["game_id"],
        start_iso=g["start_iso"],
        state=g["state"],
        status_detail=g["status_detail"],
        away=TeamInfo(**g["away"]),
        home=TeamInfo(**g["home"]),
        away_score=g["away_score"],
        home_score=g["home_score"],
    )


@st.cache_data(ttl=20, show_spinner=False)
def fetch_scoreboard(day: str | None) -> list[dict]:
    """Games for `day` (YYYYMMDD), or the feed's 'today' when None.

    `groups=50` is all of Division I; without it ESPN returns only a short list
    of featured games. `limit` lifts the default page size - a busy Saturday is
    well over a hundred games. 2026-11-24 returned 89 with these parameters.
    """
    params = "groups=50&limit=500" + (f"&dates={day}" if day else "")
    payload = _http_get_json(f"{ESPN_BASE}/scoreboard?{params}")

    games: list[dict] = []
    for event in payload.get("events") or []:
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = comp.get("status") or event.get("status") or {}
        g = _game_dict(str(event.get("id", "")), comp, status)
        if g:
            if not g["start_iso"]:
                g["start_iso"] = event.get("date") or ""
            games.append(g)
    return games


def list_games(day: str | None) -> list[GameInfo]:
    """Scoreboard as GameInfo, sorted live -> upcoming -> final."""
    out = [_game_from_dict(g) for g in fetch_scoreboard(day)]
    rank = {"in": 0, "pre": 1, "post": 2}
    out.sort(key=lambda g: (rank.get(g.state, 3), g.start_iso))
    return out


_GAME_ID_RE = re.compile(r"(\d{6,})")


def extract_game_id(text: str) -> str:
    """Accept a bare ESPN event id or a pasted ESPN game URL.

    'https://www.espn.com/mens-college-basketball/game/_/gameId/401906774/...'
    -> '401906774'
    """
    found = _GAME_ID_RE.search(str(text or ""))
    return found.group(1) if found else ""


@st.cache_data(ttl=20, show_spinner=False)
def fetch_game_header(game_id: str) -> dict:
    """One game's identity + status from the summary endpoint's `header` block,
    for a game loaded by ID rather than picked off the scoreboard."""
    payload = _http_get_json(f"{ESPN_BASE}/summary?event={game_id}")
    comps = (payload.get("header") or {}).get("competitions") or []
    if not comps:
        raise DataSourceError(f"ID {game_id} is not a men's college basketball game on this feed.")
    g = _game_dict(game_id, comps[0], comps[0].get("status") or {})
    if not g:
        raise DataSourceError(f"ID {game_id} did not return two teams.")
    return g


def game_from_id(game_id: str) -> GameInfo:
    return _game_from_dict(fetch_game_header(game_id))


@st.cache_data(ttl=max(REFRESH_SECONDS - 1, 2), show_spinner=False)
def fetch_summary(game_id: str) -> dict:
    """Play-by-play for one game. TTL just under the refresh interval so incidental
    reruns (the Invert button, widget clicks) reuse the last payload."""
    return _http_get_json(f"{ESPN_BASE}/summary?event={game_id}")


# ===========================================================================
# SECTION 4 - NORMALISATION
# Raw feed plays -> neutral GameEvent list. Nothing downstream touches
# provider-specific keys.
# ===========================================================================

_ISO_CLOCK_RE = re.compile(r"^PT(?:(\d+)M)?(?:([\d.]+)S)?$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class GameEvent:
    """One play, provider-neutral - only what the timeframe market needs."""

    event_id: str
    period: int
    clock_display: str
    clock_seconds: float | None   # seconds remaining in the period
    team_id: str
    score_value: int              # points actually scored by this play
    type_text: str
    short_desc: str
    description: str

    @property
    def is_scoring(self) -> bool:
        return self.score_value > 0


def norm_text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def parse_clock_seconds(display: Any) -> float | None:
    """Seconds remaining in the period.

      "19:39"        -> 1179.0  (above one minute)
      "0:42"         -> 42.0    (ESPN CBB under a minute, seen on 401906774)
      "42.3"         -> 42.3    (ESPN NBA-style tenths, in case CBB ever sends it)
      "PT11M39.00S"  -> 699.0   (ISO-8601)
    """
    raw = str(display or "").strip()
    if not raw:
        return None
    iso = _ISO_CLOCK_RE.match(raw)
    if iso:
        return float(iso.group(1) or 0) * 60 + float(iso.group(2) or 0)
    try:
        if ":" in raw:
            parts = raw.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def period_label(period: int) -> str:
    """1 -> '1st Half', 2 -> '2nd Half', 3 -> 'OT', 4 -> '2OT'."""
    if period <= 0:
        return DASH
    if period == 1:
        return "1st Half"
    if period == 2:
        return "2nd Half"
    n = period - REGULATION_PERIODS
    return "OT" if n == 1 else f"{n}OT"


def period_seconds(period: int) -> int:
    return HALF_SECONDS if period <= REGULATION_PERIODS else OVERTIME_PERIOD_SECONDS


def normalise_plays(payload: dict) -> list[GameEvent]:
    """Raw `summary.plays` -> GameEvent list, in the payload's own order.

    Not sorted by `sequenceNumber`: on the NBA feed that field is not monotonic
    with game time, while the array order had zero chronology violations over
    ~2900 plays. Same provider, same payload, so the same rule.
    """
    events: list[GameEvent] = []
    for idx, raw in enumerate(payload.get("plays") or []):
        if not isinstance(raw, dict):
            continue
        period_raw = raw.get("period") or {}
        period = int(period_raw.get("number") or 0) if isinstance(period_raw, dict) else 0

        clock_raw = raw.get("clock") or {}
        clock_display = (
            clock_raw.get("displayValue") if isinstance(clock_raw, dict) else str(clock_raw or "")
        ) or ""

        type_raw = raw.get("type") or {}
        type_text = norm_text(type_raw.get("text") if isinstance(type_raw, dict) else "")

        score_value = _int_score(raw.get("scoreValue"))
        # scoringPlay is the authority on whether points went up; a scoreValue on a
        # non-scoring play (ESPN sometimes carries the attempt's value) is ignored.
        if not raw.get("scoringPlay"):
            score_value = 0

        team_raw = raw.get("team") or {}
        team_id = str(team_raw.get("id", "")) if isinstance(team_raw, dict) else ""

        events.append(
            GameEvent(
                event_id=str(raw.get("id") or f"idx-{idx}"),
                period=period,
                clock_display=clock_display,
                clock_seconds=parse_clock_seconds(clock_display),
                team_id=team_id,
                score_value=score_value,
                type_text=type_text,
                short_desc=norm_text(raw.get("shortDescription")),
                description=norm_text(raw.get("text")),
            )
        )
    return events


# ===========================================================================
# SECTION 5 - TIMEFRAME BOTH TEAMS TO SCORE
# Ported from the NBA tool. The only changes are the period lengths (via
# `period_seconds`) and the optional opening-window label.
# ===========================================================================

def _clock_label(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def timeframe_windows(period: int) -> list[tuple[str, int, int]]:
    """One-minute windows for a period as (label, high_seconds, low_seconds).

    A window covers game clock in [low, high) and is labelled lower-bound
    inclusive: "20:00-19:00" (see below), "18:59-18:00", ... "0:59-0:00". A score at exactly
    19:00 belongs to the first window. The opening 20:00 itself is folded into the
    first window (see `window_index`), and OPENING_WINDOW_FROM_PERIOD_START
    relabels that first row "20:00-19:00".
    """
    total = period_seconds(period)
    out: list[tuple[str, int, int]] = []
    for i in range(total // 60):
        high = total - i * 60
        low = high - 60
        top = high if (i == 0 and OPENING_WINDOW_FROM_PERIOD_START) else high - 1
        out.append((f"{_clock_label(top)}-{_clock_label(low)}", high, low))
    return out


def window_index(clock_seconds: float | None, period: int) -> int | None:
    """Which one-minute window a clock reading belongs to.

    20:00 (1200s) -> window 0 ("20:00-19:00")   folded into the first window
    19:59 (1199s) -> window 0
    19:00 (1140s) -> window 0                   labels are lower-bound inclusive
    18:59 (1139s) -> window 1 ("18:59-18:00")
    1:00  (60s)   -> window 18 ("1:59-1:00")
    0:42  (42s)   -> window 19 ("0:59-0:00")
    """
    total = period_seconds(period)
    if clock_seconds is None or clock_seconds > total or clock_seconds < 0:
        return None
    last = total // 60 - 1
    return max(0, last - int(clock_seconds // 60))


def period_progress(events: Sequence[GameEvent], period: int) -> tuple[float | None, bool]:
    """(lowest clock reached in this period, period is finished).

    "Finished" is an explicit end-of-period play (CBB: type "End Period", text
    "End of 1st half"), or the feed having moved on to a later period.
    """
    lowest: float | None = None
    finished = False
    max_period = 0
    for ev in events:
        max_period = max(max_period, ev.period)
        if ev.period != period:
            continue
        if ev.clock_seconds is not None:
            lowest = ev.clock_seconds if lowest is None else min(lowest, ev.clock_seconds)
        sd = ev.short_desc.lower()
        tt = ev.type_text.lower()
        desc = ev.description.lower()
        if (sd.startswith("end of") or desc.startswith("end of") or sd == "halftime"
                or tt in ("end period", "end game")):
            finished = True
    if max_period > period:
        finished = True
    return lowest, finished


def timeframe_table(events: Sequence[GameEvent], period: int, away_id: str, home_id: str,
                    game_is_final: bool) -> list[dict[str, str]]:
    """Yes / No / - per one-minute window, always chronological.

    Yes  - both teams scored at least once inside the exact window.
    No   - the window is fully complete and both teams did not score.
    -    - the window has not happened yet, or is still in progress.

    Any scoring play counts (made field goal or made free throw).
    """
    windows = timeframe_windows(period)
    scored: list[set[str]] = [set() for _ in windows]
    for ev in events:
        if ev.period != period or not ev.is_scoring or not ev.team_id:
            continue
        idx = window_index(ev.clock_seconds, period)
        if idx is None:
            continue
        scored[idx].add(ev.team_id)

    lowest, finished = period_progress(events, period)
    finished = finished or game_is_final

    rows: list[dict[str, str]] = []
    for idx, (label, _high, low) in enumerate(windows):
        both = away_id in scored[idx] and home_id in scored[idx]
        # Complete only once the clock has run PAST the lower bound: a stoppage at
        # exactly 19:00 can still produce free throws for the first window.
        complete = finished or (lowest is not None and lowest < low)
        if both:
            verdict = "Yes"
        elif complete:
            verdict = "No"
        else:
            verdict = DASH
        rows.append({"Time": label, "Yes/No": verdict})
    return rows


def current_window_label(events: Sequence[GameEvent], period: int, live_period: int,
                         game_is_final: bool) -> str:
    """The window the clock is in, for the period being played; "" otherwise.

    Only the live period gets a highlight - with both halves on screen at once,
    a highlight on a finished half's last row would be a second "you are here".
    """
    if game_is_final or period != live_period:
        return ""
    lowest, finished = period_progress(events, period)
    windows = timeframe_windows(period)
    if finished:
        return windows[-1][0]
    if lowest is None:
        return ""
    idx = window_index(lowest, period)
    return windows[idx][0] if idx is not None else ""


# ===========================================================================
# SECTION 6 - SESSION STATE
# ===========================================================================

DEFAULT_STATE: dict[str, Any] = {
    "games": [],
    "games_error": None,
    "games_loaded": False,
    "manual_ids": set(),      # game ids loaded by hand, not from the scoreboard
    "scoreboard_day": None,
    "selected_game_id": None,
    "tracking": False,
    "tf_reversed": TIMEFRAME_ORDER == "reverse",  # Invert button; UI only
    "rate_limit_skip_remaining": 0,
    "last_fetch_ok": None,
    "last_error": None,
    "error_streak": 0,
}


def init_state() -> None:
    for key, value in DEFAULT_STATE.items():
        if key not in st.session_state:
            st.session_state[key] = deepcopy(value)


def reset_game_state() -> None:
    """Called on every change of selected game, so one game's fetch status never
    bleeds into the next."""
    st.session_state.last_fetch_ok = None
    st.session_state.last_error = None
    st.session_state.error_streak = 0
    st.session_state.rate_limit_skip_remaining = 0


# ===========================================================================
# SECTION 7 - UI HELPERS
# ===========================================================================

def sect(text: str) -> None:
    st.markdown(f'<div class="sect">{html.escape(text)}</div>', unsafe_allow_html=True)


def note(text: str) -> None:
    st.markdown(f'<div class="note">{html.escape(text)}</div>', unsafe_allow_html=True)


_WARNING_STYLES = {
    "alert": "background-color:#3a1600; color:#ffd966; border:2px solid #ff9900",
    "ok":    "background-color:#132117; color:#66ff99; border:2px solid #2e6b45",
}


def warning_box(message: str, warning_type: str = "ok") -> None:
    """The house status banner. `message` is injected as HTML, so callers escape
    any feed-derived text themselves."""
    style = _WARNING_STYLES.get(warning_type, _WARNING_STYLES["ok"])
    st.markdown(
        f'<div style="margin-top:10px; margin-bottom:18px; padding:16px; border-radius:10px;'
        f' font-size:22px; font-weight:700; {style}">{message}</div>',
        unsafe_allow_html=True,
    )


def status_line(message: str, inrow: bool = False) -> None:
    """Compact form of the banner, for the state that needs no attention."""
    cls = "statusline inrow" if inrow else "statusline"
    st.markdown(
        f'<div class="{cls}"><span class="dot"></span>{message}</div>',
        unsafe_allow_html=True,
    )


def _hex_luminance(color: str) -> float:
    channels = []
    for i in (1, 3, 5):
        v = int(color[i:i + 2], 16) / 255
        channels.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _hex_luminance(a), _hex_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# The theme's secondaryBackgroundColor in .streamlit/config.toml.
_ACCENT_AGAINST = "#171b22"


def team_accent(team: TeamInfo) -> str:
    for color in (team.color, team.alt_color):
        if color and _contrast(color, _ACCENT_AGAINST) >= ACCENT_MIN_CONTRAST:
            return color
    return ACCENT_FALLBACK


def _logo_tag(team: TeamInfo) -> str:
    if not team.logo:
        return ""
    return (
        f'<img class="lg" src="{html.escape(team.logo, quote=True)}" '
        f'width="{LOGO_PX}" height="{LOGO_PX}" alt="" loading="eager">'
    )


def game_header(game: GameInfo, clock_text: str, period_text: str, updated: str) -> None:
    period = f'<span class="pd">{html.escape(period_text)}</span>' if period_text else ""
    edges = f"--away:{team_accent(game.away)}; --home:{team_accent(game.home)};"
    st.markdown(
        f'<div class="gh" style="{edges}">'
        f'<div class="tm">{_logo_tag(game.away)}'
        f'<span class="nm">{html.escape(game.away.display_name)}</span>'
        f'<span class="sc">{game.away_score}</span></div>'
        '<div class="mid">'
        f'<div class="ck">{html.escape(clock_text or DASH)}{period}</div>'
        f'<div class="up">Updated {html.escape(updated or DASH)}</div>'
        '</div>'
        f'<div class="tm h"><span class="sc">{game.home_score}</span>'
        f'<span class="nm">{html.escape(game.home.display_name)}</span>'
        f'{_logo_tag(game.home)}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


_TF_CLASS = {"Yes": "yes", "No": "no"}


def render_timeframe(rows: Sequence[dict], current_label: str = "") -> None:
    """Narrow Time / Both Scored table - the NBA tool's, unchanged."""
    body = []
    for row in rows:
        label = str(row.get("Time", ""))
        verdict = str(row.get("Yes/No", DASH))
        cls = _TF_CLASS.get(verdict, "na")
        cur = ' class="cur"' if label and label == current_label else ""
        body.append(
            f'<tr{cur}><td>{html.escape(label)}</td>'
            f'<td class="v {cls}">{html.escape(verdict)}</td></tr>'
        )
    st.markdown(
        '<div class="tfwrap"><table class="tf">'
        '<thead><tr><th>Time</th><th>Both Scored</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>',
        unsafe_allow_html=True,
    )


def toggle_tf_order() -> None:
    """Flip every timeframe table between 20:00-19:00 first and 0:59-0:00 first."""
    st.session_state.tf_reversed = not st.session_state.tf_reversed


def stretch() -> dict:
    """Full-width widget kwargs for whichever Streamlit version is installed
    (`use_container_width` was replaced by `width="stretch"` in 1.49)."""
    try:
        major, minor = (int(p) for p in st.__version__.split(".")[:2])
    except (ValueError, AttributeError):
        return {"use_container_width": True}
    return {"width": "stretch"} if (major, minor) >= (1, 49) else {"use_container_width": True}


def game_option_label(game: GameInfo) -> str:
    marker = {"in": "LIVE", "pre": "UPCOMING", "post": "FINAL"}.get(game.state, game.state.upper())
    when = ""
    if game.start_iso:
        try:
            dt = datetime.fromisoformat(game.start_iso.replace("Z", "+00:00")).astimezone()
            when = dt.strftime("%a %I:%M %p").replace(" 0", " ")
        except ValueError:
            when = ""
    score = ""
    if game.state in ("in", "post"):
        score = f"  {game.away.abbr} {game.away_score}-{game.home_score} {game.home.abbr}"
    detail = game.status_detail or when
    return f"[{marker}] {game.away.display_name} @ {game.home.display_name}  ·  {detail}{score}"


# ===========================================================================
# SECTION 8 - SETUP PANEL (sidebar)
# ===========================================================================

def render_manual_id_entry(sb) -> None:
    manual_id = sb.text_input(
        "Or enter a Game ID manually",
        placeholder="e.g. 401906774",
        disabled=st.session_state.tracking,
    )
    if sb.button("Load Manual Game ID", disabled=st.session_state.tracking, **stretch()):
        gid = extract_game_id(manual_id)
        if not gid:
            sb.error("Enter a numeric ESPN game ID, or paste the full game URL.")
        else:
            if gid != st.session_state.selected_game_id:
                st.session_state.selected_game_id = gid
                reset_game_state()
            st.session_state.manual_ids = set(st.session_state.manual_ids) | {gid}
            sb.success(f"Game ID {gid} loaded.")


def resolve_selected_game() -> tuple[GameInfo | None, str | None]:
    gid = st.session_state.selected_game_id
    if not gid:
        return None, None
    for blob in st.session_state.games:
        if blob["game_id"] == gid:
            return _game_from_dict(blob), None
    try:
        return game_from_id(gid), None
    except DataSourceError as exc:
        return None, f"Game ID {gid}: {exc}"


def render_setup_sidebar() -> tuple[GameInfo | None, str | None]:
    sb = st.sidebar
    sb.markdown("## CBB Timeframe")

    day_choice = sb.date_input(
        "Slate date", value=date.today(), format="YYYY-MM-DD", key="slate_date"
    )
    if sb.button("Load Live Games", type="primary", **stretch()):
        day = None if day_choice == date.today() else day_choice.strftime("%Y%m%d")
        st.session_state.scoreboard_day = day
        try:
            st.session_state.games = [asdict(g) for g in list_games(day)]
            st.session_state.games_error = (
                None if st.session_state.games else "No games found for that date."
            )
        except DataSourceError as exc:
            st.session_state.games = []
            st.session_state.games_error = str(exc)
        st.session_state.games_loaded = True

    if st.session_state.games_error:
        sb.warning(st.session_state.games_error, icon="⚠️")

    games = [_game_from_dict(g) for g in st.session_state.games]
    ids = [g.game_id for g in games]
    by_id = {g.game_id: g for g in games}

    # index=None plus a placeholder: a manually loaded id is not on the slate, and
    # the selector must not silently snap onto the first game of the day. The
    # selectbox is type-to-search, which matters on a 100+ game college slate.
    selected = st.session_state.selected_game_id
    chosen_id = sb.selectbox(
        "Game",
        options=ids,
        index=ids.index(selected) if selected in ids else None,
        format_func=lambda gid: game_option_label(by_id[gid]),
        placeholder="Load games first" if not ids else "Type a team to search",
        label_visibility="collapsed",
        disabled=st.session_state.tracking,
    )
    if chosen_id and chosen_id != st.session_state.selected_game_id:
        st.session_state.selected_game_id = chosen_id
        st.session_state.manual_ids = set(st.session_state.manual_ids) - {chosen_id}
        reset_game_state()

    sb.divider()
    render_manual_id_entry(sb)
    sb.divider()

    game, game_error = resolve_selected_game()
    if game_error:
        sb.warning(game_error, icon="⚠️")
        return None, game_error
    if game is None:
        if not st.session_state.games_loaded:
            sb.caption("Click **Load Live Games** to begin, or load a game by ID.")
        return None, None

    if not st.session_state.tracking:
        if sb.button("▶  Track Game", type="primary", **stretch()):
            st.session_state.tracking = True
            reset_game_state()
            st.rerun()
    else:
        sb.caption(f"{game.away.display_name} @ {game.home.display_name}")
        if sb.button("Stop Tracking", **stretch()):
            st.session_state.tracking = False
            st.rerun()
        if sb.button("Refresh now", **stretch()):
            fetch_summary.clear()
            fetch_scoreboard.clear()
            fetch_game_header.clear()
            st.rerun()
        ok = st.session_state.last_fetch_ok
        sb.markdown(
            f'<div style="font-size:12px; opacity:0.6;">Last good fetch: {html.escape(ok or "never")}'
            + (f" · errors: {st.session_state.error_streak}"
               if st.session_state.error_streak else "")
            + "</div>",
            unsafe_allow_html=True,
        )
        sb.markdown(
            f'<div style="font-size:12px; opacity:0.6;">Interval: {REFRESH_SECONDS}s</div>',
            unsafe_allow_html=True,
        )

    return game, None


# ===========================================================================
# SECTION 9 - TRACKING + MAIN
# ===========================================================================

def load_events(game: GameInfo) -> list[GameEvent] | None:
    """Fetch + normalise. None on a failed poll, so the caller can say so."""
    try:
        payload = fetch_summary(game.game_id)
    except RateLimitedError as exc:
        # Must precede DataSourceError, which it subclasses: a 429 needs the
        # cooldown rather than another attempt in four seconds.
        st.session_state.rate_limit_skip_remaining = RATE_LIMIT_SKIP_TICKS
        st.session_state.last_error = str(exc)
        st.session_state.error_streak += 1
        return None
    except DataSourceError as exc:
        st.session_state.last_error = str(exc)
        st.session_state.error_streak += 1
        return None
    st.session_state.last_fetch_ok = datetime.now().astimezone().strftime("%H:%M:%S")
    st.session_state.last_error = None
    st.session_state.error_streak = 0
    return normalise_plays(payload)


def refresh_live_status(game: GameInfo) -> GameInfo:
    """Keep status/score current. Both calls are cached at 20s."""
    if game.game_id in st.session_state.manual_ids:
        try:
            return game_from_id(game.game_id)
        except DataSourceError:
            return game
    try:
        for g in list_games(st.session_state.scoreboard_day):
            if g.game_id == game.game_id:
                return g
    except DataSourceError:
        pass
    return game


def render_period_table(events: Sequence[GameEvent], period: int, live_period: int,
                        game: GameInfo) -> None:
    sect(period_label(period))
    rows = timeframe_table(events, period, game.away.team_id, game.home.team_id, game.is_final)
    if st.session_state.tf_reversed:
        rows.reverse()
    render_timeframe(rows, current_window_label(events, period, live_period, game.is_final))


def render_timeframes(events: Sequence[GameEvent], game: GameInfo) -> None:
    """Both halves side by side, then any overtimes the feed has reached."""
    live_period = max((e.period for e in events), default=0)

    head_col, inv_col, _spacer = st.columns([3, 0.7, 4], vertical_alignment="bottom")
    with head_col:
        sect("Timeframe Both Teams To Score")
    with inv_col:
        # on_click, so the tables flip in the same run as the click.
        st.button("Invert", key="tf_invert", type="secondary", on_click=toggle_tf_order)

    first, second = st.columns(2)
    with first:
        render_period_table(events, 1, live_period, game)
    with second:
        render_period_table(events, 2, live_period, game)

    overtimes = sorted({e.period for e in events if e.period > REGULATION_PERIODS})
    for start in range(0, len(overtimes), 2):
        cols = st.columns(2)
        for col, period in zip(cols, overtimes[start:start + 2]):
            with col:
                render_period_table(events, period, live_period, game)
    # Legend, for the record rather than the screen: Yes = both teams scored inside
    # the exact window (field goals and free throws); No = window complete with both
    # teams not scoring; - = not reached or in progress. The highlighted row is the
    # window the clock is in.


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    game, game_error = render_setup_sidebar()

    if not st.session_state.tracking or game is None:
        if game_error:
            warning_box(f"⚠ {html.escape(game_error)}", "alert")
        else:
            status_line("STATUS: OK — Load a game and click Track Game")
        note(
            "Sidebar: Load Live Games (or enter a Game ID) → select a game → Track Game. "
            "No play-by-play requests are made until tracking starts."
        )
        return

    # Auto-refresh only while the game can still change. Registered before the
    # cooldown check, so a rate-limited page still wakes up to resume.
    if not game.is_final:
        interval = PREGAME_REFRESH_SECONDS if game.state == "pre" else REFRESH_SECONDS
        if st_autorefresh is not None:
            st_autorefresh(interval=interval * 1000, key="cbb_tf_tick")
        else:
            st.warning(
                "`streamlit-autorefresh` is not installed, so the page will not update on "
                "its own. Install it (see requirements.txt) or use **Refresh now**.",
                icon="⚠️",
            )

    if st.session_state.rate_limit_skip_remaining > 0:
        st.session_state.rate_limit_skip_remaining -= 1
        secs_left = st.session_state.rate_limit_skip_remaining * REFRESH_SECONDS
        warning_box(f"⚠ RATE LIMITED — resuming in ~{secs_left}s", "alert")
        return

    game = refresh_live_status(game)
    loaded = load_events(game)
    # On a failed poll keep showing the last good events rather than blanking.
    cache_key = f"events_{game.game_id}"
    if loaded is not None:
        st.session_state[cache_key] = loaded
    events: list[GameEvent] = st.session_state.get(cache_key) or []

    clock_text = game.status_detail or ("Not started" if game.state == "pre" else "")
    period_text = ""
    if game.is_live and events:
        last = events[-1]
        clock_text, period_text = last.clock_display, period_label(last.period)
    game_header(game, clock_text, period_text, st.session_state.last_fetch_ok or "")

    if st.session_state.last_error:
        text = f"DATA DELAY — {st.session_state.last_error}"
        if st.session_state.error_streak > 1:
            text += f" ({st.session_state.error_streak} consecutive)"
        warning_box(f"⚠ {html.escape(text)}", "alert")
    else:
        status_line("STATUS: OK")

    if game.state == "pre":
        note("Game has not started. Tables populate from the first play.")
    elif not events and st.session_state.last_error is None:
        note("Play-by-play is not published for this game yet.")

    render_timeframes(events, game)


if __name__ == "__main__":
    main()
