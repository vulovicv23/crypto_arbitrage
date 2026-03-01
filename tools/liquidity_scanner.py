#!/usr/bin/env python3
"""
Polymarket Liquidity Scanner — discover which markets have real order books.

Scans ALL active Polymarket markets (not just BTC Up/Down) via the Gamma API,
fetches order book snapshots via the CLOB REST API, and reports which markets
have tradeable liquidity (tight spreads, real bids/asks).

Usage:
    python tools/liquidity_scanner.py                     # One-shot scan
    python tools/liquidity_scanner.py --continuous 3600    # Scan every 5 min for 1 hour
    python tools/liquidity_scanner.py --category crypto    # Filter by category
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("liquidity_scanner")

# ── Constants ─────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PAGE_SIZE = 500


# ── Data Models ───────────────────────────────────────────────────────


@dataclass
class BookSnapshot:
    """Order book snapshot for one token."""

    token_id: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid_price: float = 0.0
    spread: float = 0.0
    bid_depth: int = 0  # number of bid levels
    ask_depth: int = 0  # number of ask levels
    bid_size: float = 0.0  # total bid liquidity in USDC
    ask_size: float = 0.0  # total ask liquidity in USDC


@dataclass
class MarketScan:
    """Scan result for one market."""

    condition_id: str
    question: str
    slug: str
    category: str
    volume_24h: float
    volume_total: float
    liquidity: float
    end_date: str
    active: bool
    outcomes: list[str]
    token_ids: list[str]
    books: list[BookSnapshot] = field(default_factory=list)

    @property
    def best_spread(self) -> float:
        """Tightest spread across all tokens."""
        spreads = [b.spread for b in self.books if b.spread > 0]
        return min(spreads) if spreads else 999.0

    @property
    def total_depth(self) -> int:
        return sum(b.bid_depth + b.ask_depth for b in self.books)

    @property
    def total_book_liquidity(self) -> float:
        return sum(b.bid_size + b.ask_size for b in self.books)

    @property
    def has_real_book(self) -> bool:
        """At least one token has a spread < 90%."""
        return any(0 < b.spread < 0.90 for b in self.books)


# ── CLOB Auth ─────────────────────────────────────────────────────────


def sign_request(
    api_key: str,
    api_secret: str,
    passphrase: str,
    method: str,
    path: str,
    body: str = "",
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = timestamp + method.upper() + path + body
    signature = hmac.new(
        api_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": signature,
        "POLY-TIMESTAMP": timestamp,
        "POLY-PASSPHRASE": passphrase,
    }


# ── Scanner ───────────────────────────────────────────────────────────


class LiquidityScanner:
    """Scan Polymarket for markets with real liquidity."""

    def __init__(self):
        self._api_key = os.getenv("POLY_API_KEY", "")
        self._api_secret = os.getenv("POLY_API_SECRET", "")
        self._passphrase = os.getenv("POLY_API_PASSPHRASE", "")
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        )

    async def close(self):
        if self._session:
            await self._session.close()

    # ── Gamma API: discover all markets ──────────────────────────────

    async def fetch_all_markets(self, category: str = "") -> list[dict]:
        """Fetch ALL active markets from Gamma API (paginated)."""
        assert self._session
        now = datetime.now(timezone.utc)

        params: dict[str, str] = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
        if category:
            params["tag"] = category

        all_markets: list[dict] = []
        offset = 0

        for _ in range(40):  # Up to 20,000 markets
            params["limit"] = str(PAGE_SIZE)
            params["offset"] = str(offset)

            async with self._session.get(
                f"{GAMMA_API}/markets",
                params=params,
            ) as resp:
                if resp.status != 200:
                    logger.warning("Gamma API error: %d", resp.status)
                    break
                data = await resp.json()

            if not data:
                break
            all_markets.extend(data)
            if len(data) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            await asyncio.sleep(0.2)  # Rate limit

        logger.info("Fetched %d markets from Gamma API", len(all_markets))
        return all_markets

    # ── CLOB API: fetch order book ───────────────────────────────────

    async def fetch_book(self, token_id: str) -> BookSnapshot:
        """Fetch order book for one token via CLOB REST API."""
        assert self._session
        path = f"/book?token_id={token_id}"
        headers = sign_request(
            self._api_key,
            self._api_secret,
            self._passphrase,
            "GET",
            path,
        )

        try:
            async with self._session.get(
                f"{CLOB_API}{path}",
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    return BookSnapshot(token_id=token_id)
                data = await resp.json()
        except Exception:
            return BookSnapshot(token_id=token_id)

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # CLOB REST API returns bids ascending (worst first) and asks
        # descending (worst first). Use max/min for correct best prices.
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=0.0) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread = best_ask - best_bid if best_bid and best_ask else 0.0

        bid_size = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
        ask_size = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)

        return BookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
            spread=spread,
            bid_depth=len(bids),
            ask_depth=len(asks),
            bid_size=bid_size,
            ask_size=ask_size,
        )

    # ── Full scan ────────────────────────────────────────────────────

    async def scan(self, category: str = "", max_books: int = 500) -> list[MarketScan]:
        """Discover all markets and fetch books for top ones by volume."""
        raw_markets = await self.fetch_all_markets(category)

        # Parse into MarketScan objects
        scans: list[MarketScan] = []
        for m in raw_markets:
            raw_ids = m.get("clobTokenIds")
            raw_outcomes = m.get("outcomes")

            if not raw_ids:
                continue

            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            outcomes = (
                json.loads(raw_outcomes)
                if isinstance(raw_outcomes, str)
                else (raw_outcomes or [])
            )

            scans.append(
                MarketScan(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    category=m.get("groupItemTitle", m.get("category", "")),
                    volume_24h=float(m.get("volume24hr", 0) or 0),
                    volume_total=float(m.get("volume", 0) or 0),
                    liquidity=float(m.get("liquidityClob", 0) or 0),
                    end_date=m.get("endDate", ""),
                    active=m.get("active", False),
                    outcomes=outcomes,
                    token_ids=token_ids,
                )
            )

        # Sort by 24h volume (highest first)
        scans.sort(key=lambda s: s.volume_24h, reverse=True)

        # Fetch books for top markets (rate-limited)
        book_count = 0
        sem = asyncio.Semaphore(5)  # Max 5 concurrent book fetches

        async def fetch_with_limit(scan: MarketScan):
            nonlocal book_count
            for tid in scan.token_ids:
                if book_count >= max_books:
                    return
                async with sem:
                    book = await self.fetch_book(tid)
                    scan.books.append(book)
                    book_count += 1
                    await asyncio.sleep(0.05)  # Rate limit

        # Process in batches
        logger.info(
            "Fetching books for top %d markets (up to %d books)...",
            min(len(scans), max_books),
            max_books,
        )

        tasks = []
        for scan in scans:
            if book_count >= max_books:
                break
            tasks.append(fetch_with_limit(scan))

        await asyncio.gather(*tasks)

        return scans


# ── Reporting ─────────────────────────────────────────────────────────


def print_report(scans: list[MarketScan], scan_num: int = 1):
    """Print a detailed liquidity report."""
    total = len(scans)
    with_books = [s for s in scans if s.books]
    with_real_books = [s for s in with_books if s.has_real_book]

    print(f"\n{'='*80}")
    print(
        f"  POLYMARKET LIQUIDITY SCAN #{scan_num} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"{'='*80}\n")

    print(f"  Total markets discovered: {total}")
    print(f"  Markets with books fetched: {len(with_books)}")
    print(f"  Markets with REAL books (spread < 90%): {len(with_real_books)}")
    print()

    # ── Category breakdown ──
    categories: dict[str, list[MarketScan]] = defaultdict(list)
    for s in with_books:
        cat = s.category or s.slug.split("-")[0] if s.slug else "unknown"
        categories[cat].append(s)

    # ── Markets with real liquidity ──
    if with_real_books:
        print(f"  {'─'*76}")
        print("  MARKETS WITH REAL LIQUIDITY (spread < 90%)")
        print(f"  {'─'*76}")

        # Sort by spread (tightest first)
        with_real_books.sort(key=lambda s: s.best_spread)

        for s in with_real_books[:50]:  # Top 50
            print(f"\n  [{s.best_spread*100:5.1f}% spread] {s.question[:70]}")
            print(f"    slug: {s.slug}")
            print(
                f"    vol_24h: ${s.volume_24h:,.0f}  |  vol_total: ${s.volume_total:,.0f}  |  liquidity: ${s.liquidity:,.0f}"
            )
            for i, book in enumerate(s.books):
                outcome = s.outcomes[i] if i < len(s.outcomes) else "?"
                if book.best_bid > 0 or book.best_ask > 0:
                    print(
                        f"    {outcome:>4}: bid={book.best_bid:.4f} ask={book.best_ask:.4f} "
                        f"spread={book.spread:.4f} depth={book.bid_depth}b/{book.ask_depth}a "
                        f"liq=${book.bid_size + book.ask_size:,.0f}"
                    )
    else:
        print("  ⚠  NO MARKETS WITH REAL LIQUIDITY FOUND (all spreads ≥ 90%)")

    # ── Spread distribution ──
    print(f"\n  {'─'*76}")
    print("  SPREAD DISTRIBUTION (all markets with books)")
    print(f"  {'─'*76}")

    spread_buckets = defaultdict(int)
    for s in with_books:
        sp = s.best_spread
        if sp < 0.05:
            spread_buckets["< 5%"] += 1
        elif sp < 0.10:
            spread_buckets["5-10%"] += 1
        elif sp < 0.20:
            spread_buckets["10-20%"] += 1
        elif sp < 0.50:
            spread_buckets["20-50%"] += 1
        elif sp < 0.90:
            spread_buckets["50-90%"] += 1
        elif sp < 999:
            spread_buckets["≥ 90%"] += 1
        else:
            spread_buckets["no book"] += 1

    for bucket in ["< 5%", "5-10%", "10-20%", "20-50%", "50-90%", "≥ 90%", "no book"]:
        count = spread_buckets.get(bucket, 0)
        bar = "█" * (count // 2) if count > 0 else ""
        print(f"    {bucket:>8}: {count:4d}  {bar}")

    # ── Volume leaders ──
    print(f"\n  {'─'*76}")
    print("  TOP 20 MARKETS BY 24H VOLUME")
    print(f"  {'─'*76}")

    for s in with_books[:20]:
        spread_str = f"{s.best_spread*100:.1f}%" if s.best_spread < 999 else "N/A"
        real = "✓" if s.has_real_book else "✗"
        print(
            f"    {real} ${s.volume_24h:>12,.0f}  spread={spread_str:>6}  {s.question[:50]}"
        )

    print(f"\n{'='*80}\n")


def save_report(scans: list[MarketScan], filepath: str):
    """Save scan results to JSON for later analysis."""
    data = []
    for s in scans:
        if not s.books:
            continue
        data.append(
            {
                "condition_id": s.condition_id,
                "question": s.question,
                "slug": s.slug,
                "category": s.category,
                "volume_24h": s.volume_24h,
                "volume_total": s.volume_total,
                "liquidity": s.liquidity,
                "end_date": s.end_date,
                "outcomes": s.outcomes,
                "best_spread": s.best_spread,
                "has_real_book": s.has_real_book,
                "total_depth": s.total_depth,
                "total_book_liquidity": s.total_book_liquidity,
                "books": [
                    {
                        "token_id": b.token_id,
                        "best_bid": b.best_bid,
                        "best_ask": b.best_ask,
                        "spread": b.spread,
                        "bid_depth": b.bid_depth,
                        "ask_depth": b.ask_depth,
                        "bid_size": b.bid_size,
                        "ask_size": b.ask_size,
                    }
                    for b in s.books
                ],
            }
        )

    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved %d market scans to %s", len(data), filepath)


# ── Main ──────────────────────────────────────────────────────────────


async def main(args: argparse.Namespace):
    scanner = LiquidityScanner()
    await scanner.start()

    output_dir = PROJECT_ROOT / "matrix_runs" / "liquidity_scans"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.continuous > 0:
            # Continuous mode: scan every 5 minutes for N seconds
            end_time = time.time() + args.continuous
            scan_num = 0
            interval = 300  # 5 minutes between scans

            while time.time() < end_time:
                scan_num += 1
                logger.info(
                    "=== Scan #%d (%.0f min remaining) ===",
                    scan_num,
                    (end_time - time.time()) / 60,
                )

                scans = await scanner.scan(
                    category=args.category,
                    max_books=args.max_books,
                )
                print_report(scans, scan_num)

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                save_report(scans, str(output_dir / f"scan_{ts}.json"))

                remaining = end_time - time.time()
                if remaining > interval:
                    logger.info("Next scan in %d seconds...", interval)
                    await asyncio.sleep(interval)
                else:
                    break

            logger.info(
                "Continuous scan complete (%d scans over %ds)",
                scan_num,
                args.continuous,
            )
        else:
            # One-shot scan
            scans = await scanner.scan(
                category=args.category,
                max_books=args.max_books,
            )
            print_report(scans)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_report(scans, str(output_dir / f"scan_{ts}.json"))

    finally:
        await scanner.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Liquidity Scanner")
    parser.add_argument(
        "--continuous",
        type=int,
        default=0,
        help="Run continuously for N seconds (default: one-shot)",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="",
        help="Filter by market category/tag",
    )
    parser.add_argument(
        "--max-books",
        type=int,
        default=500,
        help="Max number of book fetches per scan (default: 500)",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
