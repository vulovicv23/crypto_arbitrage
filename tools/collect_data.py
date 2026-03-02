#!/usr/bin/env python3
"""
Download historical BTCUSDT 1-second klines from Binance and store in PostgreSQL.

Usage:
    python tools/collect_data.py --start 2025-03-01 --end 2026-02-28
    python tools/collect_data.py --resume           # continue from last stored row

Binance API:
    Endpoint: GET https://api.binance.com/api/v3/klines
    Params: symbol, interval, startTime, endTime, limit (max 1000)
    Rate limit: 1200 weight/min — we use 10 concurrent workers with a global
    semaphore to stay at ~10 req/s (600 weight/min, well under limit).
    Each 1s kline = [openTime, open, high, low, close, volume, closeTime,
                     quoteVolume, trades, takerBuyBaseVol, takerBuyQuoteVol, _]

PostgreSQL:
    Connects to the local instance at localhost:6501 (docker-compose).
    See schema.sql for table definitions.

Dependencies (pip):
    asyncpg, aiohttp   (already in project requirements)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as ``python tools/collect_data.py`` from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp  # noqa: E402

# Optional import — asyncpg is needed for DB but may not be installed yet.
try:
    import asyncpg  # noqa: E402
except ImportError:
    print("ERROR: asyncpg is required. Install with: pip install asyncpg")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1s"
LIMIT = 1000  # max per request

PG_DSN = "postgresql://postgres:postgres@localhost:6501/crypto_arbitrage"

# Concurrency: number of parallel HTTP fetch workers.
CONCURRENCY = 10

# Rate limiting: global semaphore ensures we don't exceed ~10 req/s.
# With 10 workers, each request takes ~0.5-0.7s, so actual throughput
# is limited by HTTP latency — semaphore is a safety net.
RATE_DELAY = 0.1  # minimum seconds between semaphore releases

logger = logging.getLogger("collect_data")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date_to_ms(date_str: str) -> int:
    """Parse YYYY-MM-DD to milliseconds since epoch (UTC)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: int) -> str:
    """Milliseconds to YYYY-MM-DD HH:MM string for display."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


async def get_resume_point(pool: asyncpg.Pool) -> int | None:
    """Return the max open_time_ms in btc_klines, or None if empty."""
    row = await pool.fetchrow(
        "SELECT MAX(open_time_ms) AS max_ts FROM btc_klines WHERE interval = '1s'"
    )
    return row["max_ts"] if row and row["max_ts"] is not None else None


async def fetch_klines(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    start_ms: int,
    end_ms: int,
) -> list[list]:
    """Fetch up to 1000 klines from Binance, respecting rate limit."""
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    }
    async with semaphore:
        for attempt in range(3):
            try:
                async with session.get(BINANCE_KLINE_URL, params=params) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "60"))
                        logger.warning("Rate limited — sleeping %ds", retry_after)
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 418:
                        # IP ban — wait longer
                        logger.error("IP banned (418) — sleeping 120s")
                        await asyncio.sleep(120)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    await asyncio.sleep(RATE_DELAY)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    logger.warning("Fetch error (attempt %d): %s", attempt + 1, e)
                    await asyncio.sleep(2**attempt)
                else:
                    logger.error("Fetch failed after 3 attempts: %s", e)
                    return []
    return []


def parse_klines(klines: list[list]) -> list[tuple]:
    """Parse Binance kline response into DB rows."""
    rows = []
    for k in klines:
        rows.append(
            (
                int(k[0]),  # open_time_ms
                INTERVAL,  # interval
                float(k[1]),  # open
                float(k[2]),  # high
                float(k[3]),  # low
                float(k[4]),  # close
                float(k[5]),  # volume
                int(k[8]),  # trades_count
                int(k[6]),  # close_time_ms
            )
        )
    return rows


async def insert_klines_batch(pool: asyncpg.Pool, rows: list[tuple]) -> int:
    """Bulk insert klines using COPY for speed, skipping duplicates."""
    if not rows:
        return 0
    # Use executemany with ON CONFLICT for idempotent inserts
    await pool.executemany(
        """
        INSERT INTO btc_klines
            (open_time_ms, interval, open, high, low, close, volume,
             trades_count, close_time_ms)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT DO NOTHING
        """,
        rows,
    )
    return len(rows)


async def worker(
    worker_id: int,
    task_queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    pool: asyncpg.Pool,
    stats: dict,
) -> None:
    """Worker coroutine: fetch from Binance + insert into PostgreSQL."""
    while True:
        item = await task_queue.get()
        if item is None:
            task_queue.task_done()
            break

        start_ms, end_ms = item
        try:
            klines = await fetch_klines(session, semaphore, start_ms, end_ms)
            if klines:
                rows = parse_klines(klines)
                await insert_klines_batch(pool, rows)
                stats["inserted"] += len(rows)
            stats["completed"] += 1
        except Exception as e:
            logger.error(
                "Worker %d error at %s: %s", worker_id, _ms_to_date(start_ms), e
            )
            stats["errors"] += 1
            stats["completed"] += 1
        finally:
            task_queue.task_done()


async def collect(
    start_date: str,
    end_date: str,
    resume: bool = False,
    concurrency: int = CONCURRENCY,
) -> None:
    """Main collection loop with concurrent workers."""
    num_workers = concurrency

    pool = await asyncpg.create_pool(PG_DSN, min_size=5, max_size=15)
    logger.info("Connected to PostgreSQL at %s", PG_DSN)

    start_ms = _date_to_ms(start_date)
    end_ms = _date_to_ms(end_date)

    if resume:
        last_ts = await get_resume_point(pool)
        if last_ts is not None:
            start_ms = last_ts + 1000  # next second
            logger.info("Resuming from %s", _ms_to_date(start_ms))
        else:
            logger.info("No existing data — starting from %s", start_date)

    total_seconds = (end_ms - start_ms) / 1000
    total_requests = int(total_seconds / LIMIT) + 1
    estimated_min = total_requests / (num_workers * 1.2) / 60  # ~1.2 req/s/worker

    logger.info(
        "Downloading %s → %s (%s seconds, ~%d requests, %d workers, ~%.0f min estimated)",
        _ms_to_date(start_ms),
        _ms_to_date(end_ms),
        f"{total_seconds:,.0f}",
        total_requests,
        num_workers,
        estimated_min,
    )

    # Build task queue: each task is a (start_ms, end_ms) chunk
    task_queue: asyncio.Queue = asyncio.Queue(maxsize=num_workers * 3)
    semaphore = asyncio.Semaphore(num_workers)
    stats = {"inserted": 0, "completed": 0, "errors": 0}

    t0 = time.monotonic()

    async def producer():
        """Enqueue all fetch tasks."""
        cursor = start_ms
        while cursor < end_ms:
            chunk_end = min(cursor + LIMIT * 1000, end_ms)
            await task_queue.put((cursor, chunk_end))
            cursor = chunk_end
        # Send poison pills to stop workers
        for _ in range(num_workers):
            await task_queue.put(None)

    async def progress_reporter():
        """Log progress periodically."""
        while stats["completed"] < total_requests:
            await asyncio.sleep(10)
            elapsed = time.monotonic() - t0
            rate = stats["completed"] / max(elapsed, 0.01)
            remaining = total_requests - stats["completed"]
            eta_min = remaining / max(rate, 0.01) / 60
            pct = stats["completed"] / max(total_requests, 1) * 100
            logger.info(
                "Progress: %5.1f%% | %s rows | %d/%d req (%.1f/s) | errors=%d | ETA %.0f min",
                pct,
                f"{stats['inserted']:,}",
                stats["completed"],
                total_requests,
                rate,
                stats["errors"],
                eta_min,
            )

    # Launch everything
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(limit=num_workers + 2),
    ) as session:
        # Start workers
        worker_tasks = [
            asyncio.create_task(worker(i, task_queue, session, semaphore, pool, stats))
            for i in range(num_workers)
        ]
        # Start producer and progress reporter
        producer_task = asyncio.create_task(producer())
        progress_task = asyncio.create_task(progress_reporter())

        # Wait for all work to complete
        await producer_task
        await task_queue.join()

        # Stop progress reporter
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        # Wait for workers to finish
        await asyncio.gather(*worker_tasks)

    elapsed = time.monotonic() - t0
    logger.info(
        "Done. Inserted %s rows in %d requests (%.1f min, %d errors)",
        f"{stats['inserted']:,}",
        stats["completed"],
        elapsed / 60,
        stats["errors"],
    )

    # Verify count
    count = await pool.fetchval("SELECT COUNT(*) FROM btc_klines WHERE interval = '1s'")
    logger.info("Total rows in btc_klines: %s", f"{count:,}")
    await pool.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download BTCUSDT 1s klines from Binance into PostgreSQL.",
    )
    parser.add_argument(
        "--start",
        default="2025-03-01",
        help="Start date (YYYY-MM-DD, UTC). Default: 2025-03-01",
    )
    parser.add_argument(
        "--end",
        default="2026-02-28",
        help="End date (YYYY-MM-DD, UTC). Default: 2026-02-28",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the last stored timestamp.",
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Trading pair symbol. Default: BTCUSDT",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=CONCURRENCY,
        help=f"Number of concurrent download workers. Default: {CONCURRENCY}",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    global SYMBOL
    SYMBOL = args.symbol

    asyncio.run(
        collect(args.start, args.end, resume=args.resume, concurrency=args.workers)
    )


if __name__ == "__main__":
    main()
