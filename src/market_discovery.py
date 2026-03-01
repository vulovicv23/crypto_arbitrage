"""
Dynamic market discovery for short-duration BTC Up/Down markets.

Polymarket creates new 5-minute and 15-minute BTC Up/Down markets on a
rolling basis.  This module uses **predictive scheduling** to discover
new markets as fast as possible — instead of blind polling, it calculates
when the next market window opens and polls the Gamma API right at that
moment.

Scheduling strategy:
  1. Compute the next 5m and 15m boundary times (e.g., next :05, :10, :15).
  2. Sleep until ~2 seconds before that boundary (markets appear shortly before).
  3. Poll aggressively every 2–3 seconds for a short burst window.
  4. Fall back to a slower background poll (every 15s) between bursts.
  5. This means new markets are typically discovered within 2–5 seconds of
     creation, vs 20 seconds with blind polling.

Slug format:  "{asset}-updown-{timeframe}-{unix_timestamp}"
  Examples:
    btc-updown-5m-1771942800
    btc-updown-15m-1771943700

Discovery loop:
  1. GET https://gamma-api.polymarket.com/markets with end_date window.
  2. Filter: question contains "up or down", asset=BTC, slug matches
     timeframe pattern, has enough seconds remaining.
  3. Extract YES/NO (Up/Down) token IDs.
  4. Yield new markets to the bot so it can subscribe to their books.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & patterns
# ---------------------------------------------------------------------------

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
GAMMA_PAGE_SIZE = 500  # Max per request

UP_OR_DOWN = "up or down"

# Slug patterns for supported timeframes
TIMEFRAME_PATTERNS: dict[re.Pattern, str] = {
    re.compile(r"-updown-5m-"): "5m",
    re.compile(r"-updown-15m-"): "15m",
    re.compile(r"-updown-1h-"): "1h",
    re.compile(r"-updown-4h-"): "4h",
}

# Asset extraction from slug: "btc-updown-5m-1234567890" → "BTC"
_ASSET_SLUG_PATTERN = re.compile(r"^([a-z]+)-updown-\d+[mh]-\d+$")

# Timeframe → resolution in seconds (for predictive scheduling)
_TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


class Timeframe(str, Enum):
    """Supported market timeframes."""

    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"

    @property
    def resolution_seconds(self) -> int:
        return _TF_SECONDS[self.value]

    @property
    def sizing_multiplier(self) -> float:
        """Shorter timeframes get smaller bets due to higher noise."""
        return {"5m": 0.50, "15m": 1.00, "1h": 1.25, "4h": 1.50}[self.value]


# ---------------------------------------------------------------------------
# DiscoveredMarket dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredMarket:
    """An active short-duration BTC market found on Polymarket."""

    condition_id: str
    question: str
    slug: str
    yes_token_id: str  # "Up" / "Yes" outcome
    no_token_id: str  # "Down" / "No" outcome
    end_date: datetime
    timeframe: Timeframe
    asset: str  # e.g. "BTC"

    def seconds_remaining(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max((self.end_date - now).total_seconds(), 0.0)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.seconds_remaining(now) <= 0

    @property
    def token_ids(self) -> list[str]:
        """Both token IDs for book subscription."""
        return [self.yes_token_id, self.no_token_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_timeframe(slug: str) -> Timeframe | None:
    """Extract timeframe from a Polymarket slug."""
    for pattern, tf_str in TIMEFRAME_PATTERNS.items():
        if pattern.search(slug):
            return Timeframe(tf_str)
    return None


def _parse_asset(slug: str) -> str | None:
    """Extract asset ticker from a Polymarket slug."""
    match = _ASSET_SLUG_PATTERN.match(slug)
    if match:
        return match.group(1).upper()
    return None


def _is_btc_question(question: str) -> bool:
    q = question.lower()
    return "bitcoin" in q or "btc" in q


def _parse_end_date(raw: str | None) -> datetime | None:
    """Parse ISO datetime from Gamma API."""
    if not raw:
        return None
    try:
        # Handle both "2026-02-27T14:05:00Z" and "2026-02-27T14:05:00.000Z"
        raw = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _parse_tokens(data: dict) -> list[tuple[str, str]]:
    """Parse clobTokenIds + outcomes from Gamma API response.

    Returns list of (token_id, outcome) tuples.
    """
    raw_ids = data.get("clobTokenIds")
    raw_outcomes = data.get("outcomes")
    if raw_ids is None:
        return []

    token_ids: list[str] = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes: list[str] = (
        json.loads(raw_outcomes)
        if isinstance(raw_outcomes, str)
        else raw_outcomes or []
    )
    return [
        (tid, outcomes[i] if i < len(outcomes) else "")
        for i, tid in enumerate(token_ids)
    ]


def _next_boundary(now: datetime, interval_s: int) -> datetime:
    """Compute the next clean time boundary for a given interval.

    For interval_s=300 (5m), returns next :00, :05, :10, ...
    For interval_s=900 (15m), returns next :00, :15, :30, :45.
    """
    epoch_s = now.timestamp()
    next_s = math.ceil(epoch_s / interval_s) * interval_s
    return datetime.fromtimestamp(next_s, tz=timezone.utc)


def _seconds_until_next_boundary(now: datetime, timeframes: set[str]) -> float:
    """Find the soonest upcoming market creation boundary across all timeframes.

    Returns seconds until that boundary.
    """
    soonest = float("inf")
    for tf in timeframes:
        interval_s = _TF_SECONDS.get(tf)
        if interval_s is None:
            continue
        boundary = _next_boundary(now, interval_s)
        delta = (boundary - now).total_seconds()
        if delta < soonest:
            soonest = delta
    return soonest


# ---------------------------------------------------------------------------
# MarketDiscovery
# ---------------------------------------------------------------------------


class MarketDiscovery:
    """Discovers active short-duration BTC Up/Down markets using predictive
    scheduling — polls aggressively around expected market creation times
    and slowly in between.

    Parameters
    ----------
    assets : list[str]
        Asset tickers to track (default: ["BTC"]).
    timeframes : list[str]
        Timeframes to look for (default: ["5m", "15m"]).
    min_seconds_to_resolution : int
        Skip markets with fewer seconds remaining (default: 60).
    discovery_interval : float
        Background poll interval (seconds) between burst windows (default: 15).
    burst_poll_interval : float
        Fast poll interval during burst window (default: 2.0 seconds).
    burst_window : float
        How long to poll aggressively around boundaries (default: 15 seconds).
    lead_time : float
        Start burst polling this many seconds BEFORE the boundary (default: 5).
    callback : callable or None
        ``async def callback(new: list[DiscoveredMarket], expired: list[str])``
        Called whenever the active market set changes.
    """

    def __init__(
        self,
        assets: list[str] | None = None,
        timeframes: list[str] | None = None,
        min_seconds_to_resolution: int = 60,
        discovery_interval: float = 15.0,
        burst_poll_interval: float = 2.0,
        burst_window: float = 15.0,
        lead_time: float = 5.0,
        callback=None,
    ):
        self._assets = set(a.upper() for a in (assets or ["BTC"]))
        self._valid_timeframes = set(timeframes or ["5m", "15m"])
        self._min_seconds = min_seconds_to_resolution
        self._interval = discovery_interval
        self._burst_interval = burst_poll_interval
        self._burst_window = burst_window
        self._lead_time = lead_time
        self._callback = callback

        # Current active markets by condition_id
        self._active: dict[str, DiscoveredMarket] = {}

        # Session (created in start)
        self._session: aiohttp.ClientSession | None = None

        # Stats
        self._total_discovered = 0
        self._total_expired = 0
        self._cycles = 0
        self._burst_cycles = 0

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run the predictive discovery loop forever.

        The loop alternates between two modes:
        1. **Sleep mode**: Wait until the next market boundary approaches.
        2. **Burst mode**: Poll every 2s for ~15s around the boundary.

        In between bursts, a background poll runs every 15s as a safety net.
        """
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        )
        logger.info(
            "Market discovery started (predictive scheduling): "
            "assets=%s timeframes=%s burst_interval=%.1fs lead_time=%.0fs",
            self._assets,
            self._valid_timeframes,
            self._burst_interval,
            self._lead_time,
        )

        try:
            # Initial full discovery
            await self._discover_cycle()

            while True:
                now = datetime.now(timezone.utc)
                secs_to_boundary = _seconds_until_next_boundary(
                    now, self._valid_timeframes
                )

                # Time until we should start the burst
                sleep_until_burst = max(secs_to_boundary - self._lead_time, 0)

                if sleep_until_burst > self._interval:
                    # Far from a boundary — do a background poll, then sleep
                    await asyncio.sleep(self._interval)
                    await self._discover_cycle()
                else:
                    # Close to a boundary — sleep, then burst-poll
                    if sleep_until_burst > 0:
                        logger.debug(
                            "Next market boundary in %.1fs, sleeping %.1fs before burst",
                            secs_to_boundary,
                            sleep_until_burst,
                        )
                        await asyncio.sleep(sleep_until_burst)

                    # ── BURST MODE: poll aggressively ──
                    burst_start = time.monotonic()
                    found_new_in_burst = False
                    while (time.monotonic() - burst_start) < self._burst_window:
                        new_count = await self._discover_cycle()
                        self._burst_cycles += 1
                        if new_count > 0:
                            found_new_in_burst = True
                            # Found new markets — no need to keep hammering
                            break
                        await asyncio.sleep(self._burst_interval)

                    if found_new_in_burst:
                        logger.info(
                            "Burst discovery found new markets in %.1fs",
                            time.monotonic() - burst_start,
                        )
                    else:
                        logger.debug(
                            "Burst window completed without new markets (%.1fs)",
                            time.monotonic() - burst_start,
                        )

        except asyncio.CancelledError:
            logger.info("Market discovery cancelled")
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── discovery cycle ────────────────────────────────────────────────

    async def _discover_cycle(self) -> int:
        """One full discovery cycle: fetch, filter, diff, notify.

        Returns the number of newly discovered markets.
        """
        self._cycles += 1
        now = datetime.now(timezone.utc)

        try:
            raw_markets = await self._fetch_markets(now)
        except Exception:
            logger.exception("Market discovery fetch failed")
            return 0

        # Filter to matching markets
        candidates = self._filter_markets(raw_markets, now)

        # Diff against current active set
        new_markets: list[DiscoveredMarket] = []
        current_ids = set()

        for m in candidates:
            current_ids.add(m.condition_id)
            if m.condition_id not in self._active:
                new_markets.append(m)
                self._active[m.condition_id] = m
                self._total_discovered += 1

        # Find expired markets
        expired_ids: list[str] = []
        for cid in list(self._active.keys()):
            if cid not in current_ids or self._active[cid].is_expired(now):
                expired_ids.append(cid)
                del self._active[cid]
                self._total_expired += 1

        if new_markets or expired_ids:
            logger.info(
                "Discovery cycle %d: %d new, %d expired, %d active total",
                self._cycles,
                len(new_markets),
                len(expired_ids),
                len(self._active),
            )
            for m in new_markets:
                logger.info(
                    "  NEW: %s [%s] %s ends=%s (%.0fs left)",
                    m.asset,
                    m.timeframe.value,
                    m.question[:60],
                    m.end_date.strftime("%H:%M:%S"),
                    m.seconds_remaining(now),
                )

            # Notify callback
            if self._callback:
                try:
                    await self._callback(new_markets, expired_ids)
                except Exception:
                    logger.exception("Discovery callback error")
        else:
            logger.debug(
                "Discovery cycle %d: no changes (%d active)",
                self._cycles,
                len(self._active),
            )

        return len(new_markets)

    # ── Gamma API fetch ────────────────────────────────────────────────

    async def _fetch_markets(self, now: datetime) -> list[dict]:
        """Fetch active markets from Gamma API with pagination."""
        assert self._session is not None

        params = {
            "active": "true",
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order": "volume24hr",
            "ascending": "false",
        }

        all_markets: list[dict] = []
        offset = 0
        max_pages = 20  # Safety limit

        for _ in range(max_pages):
            params["limit"] = str(GAMMA_PAGE_SIZE)
            params["offset"] = str(offset)

            async with self._session.get(
                f"{GAMMA_API_BASE}/markets", params=params
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if not data:
                break

            all_markets.extend(data)
            if len(data) < GAMMA_PAGE_SIZE:
                break  # Last page
            offset += GAMMA_PAGE_SIZE

        return all_markets

    # ── filtering ──────────────────────────────────────────────────────

    def _filter_markets(
        self, raw_markets: list[dict], now: datetime
    ) -> list[DiscoveredMarket]:
        """Apply all filters and return matching DiscoveredMarket objects."""
        results: list[DiscoveredMarket] = []

        for m in raw_markets:
            question = m.get("question", "")
            slug = m.get("slug", "")

            # 1. Must be "up or down" market
            if UP_OR_DOWN not in question.lower():
                continue

            # 2. Must match a configured asset (slug first, then question)
            asset = _parse_asset(slug)
            if asset is None or asset not in self._assets:
                if _is_btc_question(question) and "BTC" in self._assets:
                    asset = "BTC"
                else:
                    continue

            # 3. Must match a supported timeframe
            timeframe = _parse_timeframe(slug)
            if timeframe is None or timeframe.value not in self._valid_timeframes:
                continue

            # 4. Must have enough time left
            end_date = _parse_end_date(m.get("endDate") or m.get("end_date"))
            if end_date is None:
                continue
            seconds_left = (end_date - now).total_seconds()
            if seconds_left < self._min_seconds:
                continue

            # 5. Extract YES/NO token IDs
            tokens = _parse_tokens(m)
            yes_token = None
            no_token = None
            for tid, outcome in tokens:
                ol = outcome.lower()
                if ol in ("yes", "up"):
                    yes_token = tid
                elif ol in ("no", "down"):
                    no_token = tid

            if not yes_token or not no_token:
                continue

            condition_id = m.get("conditionId") or m.get("condition_id", "")

            results.append(
                DiscoveredMarket(
                    condition_id=condition_id,
                    question=question,
                    slug=slug,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    end_date=end_date,
                    timeframe=timeframe,
                    asset=asset,
                )
            )

        return results

    # ── accessors ──────────────────────────────────────────────────────

    @property
    def active_markets(self) -> dict[str, DiscoveredMarket]:
        """Currently active markets by condition_id."""
        return dict(self._active)

    @property
    def active_token_ids(self) -> list[str]:
        """All YES + NO token IDs for currently active markets."""
        ids = []
        for m in self._active.values():
            ids.extend(m.token_ids)
        return ids

    @property
    def active_yes_token_ids(self) -> list[str]:
        return [m.yes_token_id for m in self._active.values()]

    @property
    def active_no_token_ids(self) -> list[str]:
        return [m.no_token_id for m in self._active.values()]

    def stats(self) -> dict:
        now = datetime.now(timezone.utc)
        secs_to_boundary = _seconds_until_next_boundary(now, self._valid_timeframes)
        return {
            "cycles": self._cycles,
            "burst_cycles": self._burst_cycles,
            "active": len(self._active),
            "total_discovered": self._total_discovered,
            "total_expired": self._total_expired,
            "next_boundary_s": round(secs_to_boundary, 1),
        }
