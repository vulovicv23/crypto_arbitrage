#!/usr/bin/env python3
"""
Parallel Test Matrix — run multiple bot configurations simultaneously.

Spawns N bot instances in dry-run mode, each with different parameter
combinations defined as "profiles."  After a configurable duration every
instance is gracefully stopped, trade logs are parsed, metrics computed,
and a ranked comparison report is generated.

Usage:
    python tools/test_matrix.py --duration 3600                    # 1 hour, all profiles
    python tools/test_matrix.py --profiles moderate,aggressive -d 300
    python tools/test_matrix.py --list-profiles
    python tools/test_matrix.py --custom-profiles my.json --duration 600
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median

logger = logging.getLogger("test_matrix")

# ── Project root (one level up from tools/) ─────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ═════════════════════════════════════════════════════════════════════
# Data Models
# ═════════════════════════════════════════════════════════════════════


@dataclass
class Profile:
    """A named set of env-var overrides that define a parameter configuration."""

    name: str
    description: str
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class ProfileResult:
    """Metrics computed from a single profile's dry-run output."""

    profile_name: str
    description: str
    exit_code: int
    error: str
    # Trade activity
    total_trades: int = 0
    total_volume: float = 0.0
    trades_per_hour: float = 0.0
    # PnL (real = from resolution records; falls back to edge×size if unresolved)
    total_pnl: float = 0.0
    avg_edge: float = 0.0
    median_edge: float = 0.0
    # Resolution-based metrics
    resolved_count: int = 0
    force_resolved_count: int = 0
    unresolved_count: int = 0
    real_win_count: int = 0
    real_loss_count: int = 0
    real_win_rate: float = 0.0
    total_fees: float = 0.0
    # Latency
    avg_latency_ms: float = 0.0
    # Risk
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    # Context
    avg_seconds_to_expiry: float = 0.0
    regime_distribution: dict[str, int] = field(default_factory=dict)
    strength_distribution: dict[str, int] = field(default_factory=dict)
    outcome_distribution: dict[str, int] = field(default_factory=dict)
    # Ranking
    composite_score: float = 0.0


# ═════════════════════════════════════════════════════════════════════
# Built-in Profiles
# ═════════════════════════════════════════════════════════════════════

BUILTIN_PROFILES: dict[str, Profile] = {
    "conservative": Profile(
        name="conservative",
        description="Wide edge threshold, small positions, tight stop-loss",
        env_overrides={
            "MIN_EDGE_THRESHOLD": "0.03",
            "MAX_EDGE_THRESHOLD": "0.25",
            "MAX_POSITION_PCT": "0.003",
            "MAX_DAILY_LOSS_PCT": "0.01",
            "MAX_OPEN_POSITIONS": "10",
            "COOLDOWN_AFTER_LOSSES": "3",
            "COOLDOWN_DURATION_S": "60",
            "SIDEWAYS_SIZE_MULTIPLIER": "0.3",
            "TREND_SIZE_MULTIPLIER": "0.8",
        },
    ),
    "moderate": Profile(
        name="moderate",
        description="Balanced defaults — matches .env.example baseline",
        env_overrides={
            "MIN_EDGE_THRESHOLD": "0.02",
            "MAX_EDGE_THRESHOLD": "0.30",
            "MAX_POSITION_PCT": "0.005",
            "MAX_DAILY_LOSS_PCT": "0.02",
            "MAX_OPEN_POSITIONS": "20",
        },
    ),
    "aggressive": Profile(
        name="aggressive",
        description="Low edge bar, larger positions, higher risk tolerance",
        env_overrides={
            "MIN_EDGE_THRESHOLD": "0.01",
            "MAX_EDGE_THRESHOLD": "0.35",
            "MAX_POSITION_PCT": "0.01",
            "MAX_DAILY_LOSS_PCT": "0.03",
            "MAX_OPEN_POSITIONS": "30",
            "COOLDOWN_AFTER_LOSSES": "7",
            "COOLDOWN_DURATION_S": "15",
            "SIDEWAYS_SIZE_MULTIPLIER": "0.6",
            "TREND_SIZE_MULTIPLIER": "1.2",
        },
    ),
    "near_expiry": Profile(
        name="near_expiry",
        description="Targets large edges near contract expiry (low min-seconds)",
        env_overrides={
            "MIN_EDGE_THRESHOLD": "0.005",
            "MAX_EDGE_THRESHOLD": "0.40",
            "MAX_POSITION_PCT": "0.008",
            "MAX_DAILY_LOSS_PCT": "0.025",
            "MAX_OPEN_POSITIONS": "25",
            "DISCOVERY_MIN_SECONDS": "30",
            "SIDEWAYS_SIZE_MULTIPLIER": "0.5",
            "TREND_SIZE_MULTIPLIER": "1.0",
        },
    ),
    "edge_sweep_low": Profile(
        name="edge_sweep_low",
        description="Edge threshold sweep: 0.005 (very permissive)",
        env_overrides={"MIN_EDGE_THRESHOLD": "0.005"},
    ),
    "edge_sweep_mid": Profile(
        name="edge_sweep_mid",
        description="Edge threshold sweep: 0.015 (moderate selectivity)",
        env_overrides={"MIN_EDGE_THRESHOLD": "0.015"},
    ),
    "edge_sweep_high": Profile(
        name="edge_sweep_high",
        description="Edge threshold sweep: 0.04 (highly selective)",
        env_overrides={"MIN_EDGE_THRESHOLD": "0.04"},
    ),
}


# ═════════════════════════════════════════════════════════════════════
# Custom Profile Loader
# ═════════════════════════════════════════════════════════════════════


def load_custom_profiles(path: str) -> dict[str, Profile]:
    """Load custom profile definitions from a JSON file.

    Expected format::

        {
          "profile_name": {
            "description": "...",
            "env_overrides": {"KEY": "value", ...}
          }
        }
    """
    with open(path) as f:
        data = json.load(f)
    profiles: dict[str, Profile] = {}
    for name, spec in data.items():
        if not isinstance(spec, dict):
            continue  # skip comment entries like "__comment_ml"
        profiles[name] = Profile(
            name=name,
            description=spec.get("description", ""),
            env_overrides=spec.get("env_overrides", {}),
        )
    return profiles


# ═════════════════════════════════════════════════════════════════════
# Results Analyzer
# ═════════════════════════════════════════════════════════════════════


class ResultsAnalyzer:
    """Parses trade logs and computes comparison metrics."""

    def __init__(
        self,
        run_dir: Path,
        profiles: list[Profile],
        duration_s: int,
        capital: float,
    ):
        self._run_dir = run_dir
        self._profiles = {p.name: p for p in profiles}
        self._duration_s = duration_s
        self._capital = capital

    # ── public ───────────────────────────────────────────────────────

    def analyze(self) -> dict[str, ProfileResult]:
        """Parse all profile trade logs and compute ranked results."""
        results: dict[str, ProfileResult] = {}
        for name, profile in self._profiles.items():
            profile_dir = self._run_dir / name
            trades = self._parse_trades(profile_dir / "trades.jsonl")
            exit_code = self._read_exit_code(profile_dir)
            error = self._read_error(profile_dir)
            result = self._compute_metrics(
                name,
                profile.description,
                trades,
                exit_code,
                error,
            )
            results[name] = result

        self._compute_composite_scores(results)
        return results

    # ── trade parsing ────────────────────────────────────────────────

    @staticmethod
    def _parse_trades(path: Path) -> list[dict]:
        """Read trades.jsonl, tolerating missing or malformed lines."""
        trades: list[dict] = []
        if not path.exists():
            return trades
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return trades

    @staticmethod
    def _read_exit_code(profile_dir: Path) -> int:
        """Read the cached exit code, or -1 if unknown."""
        ec_path = profile_dir / ".exit_code"
        if ec_path.exists():
            try:
                return int(ec_path.read_text().strip())
            except ValueError:
                pass
        return -1

    @staticmethod
    def _read_error(profile_dir: Path) -> str:
        """Check stderr.log for errors."""
        stderr_path = profile_dir / "stderr.log"
        if not stderr_path.exists():
            return ""
        text = stderr_path.read_text().strip()
        # Only report if there's actual error content
        if text and ("Error" in text or "FATAL" in text or "Traceback" in text):
            # Truncate to first 500 chars
            return text[:500]
        return ""

    # ── metrics computation ──────────────────────────────────────────

    def _compute_metrics(
        self,
        name: str,
        description: str,
        records: list[dict],
        exit_code: int,
        error: str,
    ) -> ProfileResult:
        """Compute all metrics from trade records (entries + resolutions).

        When resolution records are present (type=resolution), we use real
        PnL (pnl_net, after fees) instead of theoretical edge × size.
        For entries without a matching resolution, we fall back to edge × size.
        """
        result = ProfileResult(
            profile_name=name,
            description=description,
            exit_code=exit_code,
            error=error,
        )

        if not records:
            return result

        # Separate entries from resolutions
        entries: list[dict] = []
        resolutions: dict[str, dict] = {}  # order_id -> resolution record
        for rec in records:
            rec_type = rec.get("type", "entry")  # backward compat: no type = entry
            if rec_type == "resolution":
                oid = rec.get("order_id", "")
                if oid:
                    resolutions[oid] = rec
            else:
                entries.append(rec)

        if not entries:
            return result

        # Build per-trade PnL: prefer real resolution over edge×size
        pnl_per_trade: list[float] = []
        edges: list[float] = []
        sizes: list[float] = []
        latencies: list[float] = []
        expiry_times: list[float] = []
        total_fees = 0.0
        resolved_count = 0
        force_resolved_count = 0
        unresolved_count = 0
        real_win_count = 0
        real_loss_count = 0

        for entry in entries:
            edge = entry.get("edge", 0.0)
            size = entry.get("fill_size", entry.get("size", 0.0))
            edges.append(edge)
            sizes.append(size)
            latencies.append(entry.get("latency_ms", 0.0))
            expiry_times.append(entry.get("seconds_to_expiry", 0.0))

            order_id = entry.get("order_id", "")
            resolution = resolutions.get(order_id) if order_id else None

            if resolution is not None:
                # Use real PnL from resolution (after fees)
                pnl_net = resolution.get("pnl_net", 0.0)
                fee = resolution.get("fee", 0.0)
                pnl_per_trade.append(pnl_net)
                total_fees += fee
                resolved_count += 1
                if resolution.get("force_resolved", False):
                    force_resolved_count += 1
                if pnl_net > 0:
                    real_win_count += 1
                else:
                    real_loss_count += 1
            else:
                # Fallback: theoretical edge × size
                pnl_per_trade.append(edge * size)
                unresolved_count += 1

        total_pnl = sum(pnl_per_trade)

        # Cumulative PnL for drawdown
        cumulative: list[float] = []
        running = 0.0
        for p in pnl_per_trade:
            running += p
            cumulative.append(running)

        # Duration in hours
        duration_hours = self._duration_s / 3600.0

        # Regime / strength / outcome distributions
        regime_dist: dict[str, int] = {}
        strength_dist: dict[str, int] = {}
        outcome_dist: dict[str, int] = {}
        for t in entries:
            r = t.get("regime", "UNKNOWN")
            s = t.get("strength", "UNKNOWN")
            o = t.get("outcome", "UNKNOWN")
            regime_dist[r] = regime_dist.get(r, 0) + 1
            strength_dist[s] = strength_dist.get(s, 0) + 1
            outcome_dist[o] = outcome_dist.get(o, 0) + 1

        result.total_trades = len(entries)
        result.total_volume = sum(sizes)
        result.trades_per_hour = (
            len(entries) / duration_hours if duration_hours > 0 else 0.0
        )
        result.total_pnl = total_pnl
        result.avg_edge = sum(edges) / len(edges)
        result.median_edge = median(edges) if edges else 0.0
        result.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0
        result.max_drawdown = self._max_drawdown(cumulative)
        result.sharpe_ratio = self._sharpe(pnl_per_trade, duration_hours)
        result.profit_factor = self._profit_factor(pnl_per_trade)
        result.avg_seconds_to_expiry = (
            sum(expiry_times) / len(expiry_times) if expiry_times else 0.0
        )
        result.regime_distribution = regime_dist
        result.strength_distribution = strength_dist
        result.outcome_distribution = outcome_dist

        # Resolution-specific fields
        result.resolved_count = resolved_count
        result.force_resolved_count = force_resolved_count
        result.unresolved_count = unresolved_count
        result.real_win_count = real_win_count
        result.real_loss_count = real_loss_count
        result.real_win_rate = (
            real_win_count / resolved_count if resolved_count > 0 else 0.0
        )
        result.total_fees = total_fees

        return result

    # ── helper calculations ──────────────────────────────────────────

    @staticmethod
    def _max_drawdown(cumulative_pnl: list[float]) -> float:
        if not cumulative_pnl:
            return 0.0
        peak = cumulative_pnl[0]
        max_dd = 0.0
        for val in cumulative_pnl:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _sharpe(pnl_per_trade: list[float], duration_hours: float) -> float:
        """Annualized Sharpe ratio from per-trade PnL."""
        if len(pnl_per_trade) < 2 or duration_hours <= 0:
            return 0.0
        n = len(pnl_per_trade)
        mean_pnl = sum(pnl_per_trade) / n
        variance = sum((p - mean_pnl) ** 2 for p in pnl_per_trade) / (n - 1)
        std_pnl = variance**0.5
        if std_pnl < 1e-12:
            return 0.0
        trades_per_hour = n / duration_hours
        annualization = (trades_per_hour * 24 * 365) ** 0.5
        return (mean_pnl / std_pnl) * annualization

    @staticmethod
    def _profit_factor(pnl_per_trade: list[float]) -> float:
        gains = sum(p for p in pnl_per_trade if p > 0)
        losses = abs(sum(p for p in pnl_per_trade if p <= 0))
        if losses < 1e-12:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    # ── composite scoring ────────────────────────────────────────────

    @staticmethod
    def _compute_composite_scores(results: dict[str, ProfileResult]) -> None:
        """Rank profiles via weighted min-max normalized metrics."""
        active = {k: v for k, v in results.items() if v.total_trades > 0}
        if not active:
            return

        # Check if resolution data is available (any profile has resolutions)
        has_resolutions = any(r.resolved_count > 0 for r in active.values())

        weights = {
            "sharpe": 0.20,
            "pnl": 0.20,
            "profit_factor": 0.15,
            "drawdown": 0.15,  # inverted: lower is better
            "win_rate": 0.15,
            "trades_per_hour": 0.10,
            "avg_edge": 0.05,
        }

        # When no resolution data, redistribute win_rate weight
        if not has_resolutions:
            weights["win_rate"] = 0.0
            weights["sharpe"] = 0.25
            weights["pnl"] = 0.25
            weights["avg_edge"] = 0.10

        raw: dict[str, list[float]] = {
            "sharpe": [r.sharpe_ratio for r in active.values()],
            "pnl": [r.total_pnl for r in active.values()],
            "profit_factor": [min(r.profit_factor, 100.0) for r in active.values()],
            "drawdown": [r.max_drawdown for r in active.values()],
            "win_rate": [r.real_win_rate for r in active.values()],
            "trades_per_hour": [r.trades_per_hour for r in active.values()],
            "avg_edge": [r.avg_edge for r in active.values()],
        }

        def normalize(vals: list[float], invert: bool = False) -> list[float]:
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-12:
                return [0.5] * len(vals)
            normed = [(v - lo) / (hi - lo) for v in vals]
            return [1.0 - n for n in normed] if invert else normed

        normed = {
            "sharpe": normalize(raw["sharpe"]),
            "pnl": normalize(raw["pnl"]),
            "profit_factor": normalize(raw["profit_factor"]),
            "drawdown": normalize(raw["drawdown"], invert=True),
            "win_rate": normalize(raw["win_rate"]),
            "trades_per_hour": normalize(raw["trades_per_hour"]),
            "avg_edge": normalize(raw["avg_edge"]),
        }

        for i, name in enumerate(active):
            score = sum(weights[k] * normed[k][i] for k in weights)
            results[name].composite_score = round(score, 4)


# ═════════════════════════════════════════════════════════════════════
# Report Generator
# ═════════════════════════════════════════════════════════════════════


class ReportGenerator:
    """Produces console, JSON, and Markdown reports."""

    @staticmethod
    def print_console_table(
        results: dict[str, ProfileResult],
        run_id: str,
        duration_s: int,
        capital: float,
    ) -> None:
        """Print a ranked comparison table to stdout."""
        ranked = sorted(
            results.values(),
            key=lambda r: r.composite_score,
            reverse=True,
        )

        # Detect if any profile has resolution data
        has_resolutions = any(r.resolved_count > 0 for r in ranked)
        pnl_label = "Real PnL" if has_resolutions else "Exp. PnL"

        header = (
            f"\n{'=' * 125}\n"
            f"  PARALLEL TEST MATRIX — RESULTS  (run: {run_id})\n"
            f"  Duration: {duration_s}s | Capital: ${capital:,.2f} "
            f"| Profiles: {len(ranked)}\n"
            f"{'=' * 125}\n"
        )
        print(header)

        # Table header
        fmt = (
            "  {rank:<4} {name:<18} {trades:>7} {volume:>10} {pnl:>10} "
            "{wr:>6} {fees:>8} {edge:>9} {sharpe:>8} {dd:>10} {pf:>7} "
            "{tph:>8} {score:>7}"
        )
        print(
            fmt.format(
                rank="Rank",
                name="Profile",
                trades="Trades",
                volume="Volume",
                pnl=pnl_label,
                wr="WR%",
                fees="Fees",
                edge="Avg Edge",
                sharpe="Sharpe",
                dd="Max DD",
                pf="PF",
                tph="Trd/Hr",
                score="Score",
            )
        )
        print(f"  {'-' * 121}")

        for i, r in enumerate(ranked, 1):
            pf_str = f"{r.profit_factor:.1f}" if r.profit_factor < 1000 else "∞"
            err_flag = " ⚠" if r.error else ""
            wr_str = f"{r.real_win_rate * 100:.1f}" if r.resolved_count > 0 else "—"
            fees_str = f"${r.total_fees:.2f}" if r.total_fees > 0 else "—"
            print(
                fmt.format(
                    rank=f"#{i}",
                    name=f"{r.profile_name}{err_flag}",
                    trades=str(r.total_trades),
                    volume=f"${r.total_volume:,.2f}",
                    pnl=f"${r.total_pnl:,.2f}",
                    wr=wr_str,
                    fees=fees_str,
                    edge=f"{r.avg_edge:.4f}",
                    sharpe=f"{r.sharpe_ratio:.2f}",
                    dd=f"${r.max_drawdown:,.2f}",
                    pf=pf_str,
                    tph=f"{r.trades_per_hour:.1f}",
                    score=f"{r.composite_score:.4f}",
                )
            )

        # Resolution summary
        if has_resolutions:
            total_resolved = sum(r.resolved_count for r in ranked)
            total_force = sum(r.force_resolved_count for r in ranked)
            total_unresolved = sum(r.unresolved_count for r in ranked)
            total_entries = sum(r.total_trades for r in ranked)
            print(
                f"\n  Resolution: {total_resolved}/{total_entries} entries resolved "
                f"({total_force} force-resolved at shutdown, "
                f"{total_unresolved} unresolved → fallback edge×size)"
            )

        if ranked and ranked[0].total_trades > 0:
            print(f"\n  🏆 Best profile: {ranked[0].profile_name}")
            print(f"     {ranked[0].description}")

        print(f"\n{'=' * 125}\n")

    @staticmethod
    def save_json(
        results: dict[str, ProfileResult],
        path: Path,
        run_meta: dict,
    ) -> None:
        """Save structured results to JSON."""
        output = {
            "meta": run_meta,
            "results": {name: asdict(r) for name, r in results.items()},
            "ranking": [
                r.profile_name
                for r in sorted(
                    results.values(),
                    key=lambda x: x.composite_score,
                    reverse=True,
                )
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(output, f, indent=2, default=str)
        logger.info("JSON report saved to %s", path)

    @staticmethod
    def save_markdown(
        results: dict[str, ProfileResult],
        path: Path,
        run_id: str,
        duration_s: int,
        capital: float,
    ) -> None:
        """Save a human-readable Markdown report."""
        ranked = sorted(
            results.values(),
            key=lambda r: r.composite_score,
            reverse=True,
        )

        has_resolutions = any(r.resolved_count > 0 for r in ranked)
        pnl_label = "Real PnL" if has_resolutions else "Exp. PnL"

        lines: list[str] = [
            f"# Test Matrix Report — {run_id}\n",
            f"**Duration:** {duration_s}s | "
            f"**Capital:** ${capital:,.2f} | "
            f"**Profiles:** {len(ranked)}\n",
            "## Ranking\n",
            f"| Rank | Profile | Trades | {pnl_label} | WR% | Fees "
            "| Avg Edge | Sharpe | Max DD | PF | Score |",
            "|------|---------|--------|----------|------|------"
            "|----------|--------|--------|-----|-------|",
        ]

        for i, r in enumerate(ranked, 1):
            pf = f"{r.profit_factor:.1f}" if r.profit_factor < 1000 else "∞"
            wr = f"{r.real_win_rate * 100:.1f}%" if r.resolved_count > 0 else "—"
            fees = f"${r.total_fees:.2f}" if r.total_fees > 0 else "—"
            lines.append(
                f"| #{i} | {r.profile_name} | {r.total_trades} "
                f"| ${r.total_pnl:,.2f} | {wr} | {fees} "
                f"| {r.avg_edge:.4f} "
                f"| {r.sharpe_ratio:.2f} | ${r.max_drawdown:,.2f} "
                f"| {pf} | {r.composite_score:.4f} |"
            )

        # Resolution summary
        if has_resolutions:
            total_resolved = sum(r.resolved_count for r in ranked)
            total_force = sum(r.force_resolved_count for r in ranked)
            total_unresolved = sum(r.unresolved_count for r in ranked)
            total_entries = sum(r.total_trades for r in ranked)
            lines.append(
                f"\n> **Resolution:** {total_resolved}/{total_entries} entries "
                f"resolved ({total_force} force-resolved at shutdown, "
                f"{total_unresolved} unresolved → fallback edge×size)\n"
            )

        lines.append("\n## Per-Profile Details\n")

        for r in ranked:
            lines.append(f"### {r.profile_name}\n")
            lines.append(f"_{r.description}_\n")
            if r.error:
                lines.append(f"**Error:** `{r.error[:200]}`\n")
            lines.append(f"- **Trades:** {r.total_trades}")
            lines.append(f"- **Volume:** ${r.total_volume:,.2f}")
            lines.append(f"- **{pnl_label}:** ${r.total_pnl:,.2f}")
            if r.resolved_count > 0:
                lines.append(
                    f"- **Win Rate:** {r.real_win_rate * 100:.1f}% "
                    f"({r.real_win_count}W / {r.real_loss_count}L)"
                )
                lines.append(f"- **Total Fees:** ${r.total_fees:.2f}")
                lines.append(
                    f"- **Resolved:** {r.resolved_count} "
                    f"({r.force_resolved_count} force-resolved)"
                )
                if r.unresolved_count > 0:
                    lines.append(
                        f"- **Unresolved:** {r.unresolved_count} "
                        "(PnL from edge×size fallback)"
                    )
            lines.append(f"- **Avg Edge:** {r.avg_edge:.4f}")
            lines.append(f"- **Median Edge:** {r.median_edge:.4f}")
            lines.append(f"- **Avg Latency:** {r.avg_latency_ms:.1f}ms")
            lines.append(f"- **Sharpe Ratio:** {r.sharpe_ratio:.2f}")
            lines.append(f"- **Max Drawdown:** ${r.max_drawdown:,.2f}")
            pf = f"{r.profit_factor:.2f}" if r.profit_factor < 1000 else "∞"
            lines.append(f"- **Profit Factor:** {pf}")
            lines.append(f"- **Trades/Hour:** {r.trades_per_hour:.1f}")
            lines.append(f"- **Avg Time to Expiry:** {r.avg_seconds_to_expiry:.0f}s")
            if r.regime_distribution:
                dist = ", ".join(
                    f"{k}: {v}" for k, v in sorted(r.regime_distribution.items())
                )
                lines.append(f"- **Regime:** {dist}")
            if r.strength_distribution:
                dist = ", ".join(
                    f"{k}: {v}" for k, v in sorted(r.strength_distribution.items())
                )
                lines.append(f"- **Strength:** {dist}")
            if r.outcome_distribution:
                dist = ", ".join(
                    f"{k}: {v}" for k, v in sorted(r.outcome_distribution.items())
                )
                lines.append(f"- **Outcomes:** {dist}")
            lines.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines))
        logger.info("Markdown report saved to %s", path)


# ═════════════════════════════════════════════════════════════════════
# Matrix Orchestrator
# ═════════════════════════════════════════════════════════════════════


class MatrixOrchestrator:
    """Spawns and manages parallel bot instances."""

    # Stagger delay between process launches (seconds)
    _LAUNCH_STAGGER_S = 2.0
    # Grace period after duration before sending SIGTERM
    _GRACE_PERIOD_S = 30.0
    # Timeout after SIGTERM before SIGKILL
    _KILL_TIMEOUT_S = 10.0

    def __init__(
        self,
        profiles: list[Profile],
        duration_s: int,
        capital: float,
        output_dir: str,
        max_parallel: int,
        run_id: str | None = None,
    ):
        self._profiles = profiles
        self._duration_s = duration_s
        self._capital = capital
        self._output_dir = Path(output_dir)
        self._max_parallel = max_parallel or len(profiles)
        self._run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._run_dir: Path = self._output_dir / self._run_id

    # ── public ───────────────────────────────────────────────────────

    def run(self) -> dict[str, ProfileResult]:
        """Execute all profiles and return analyzed results."""
        # 1. Create directory structure
        self._create_run_directory()
        run_meta = self._save_run_metadata()

        # 2. Spawn processes (respecting max_parallel)
        logger.info(
            "Starting matrix run '%s': %d profiles, %ds duration, $%.2f capital",
            self._run_id,
            len(self._profiles),
            self._duration_s,
            self._capital,
        )
        processes = self._spawn_all()

        # 3. Wait for completion
        self._wait_for_completion(processes)

        # 4. Analyze results
        analyzer = ResultsAnalyzer(
            self._run_dir,
            self._profiles,
            self._duration_s,
            self._capital,
        )
        results = analyzer.analyze()

        # 5. Generate reports
        report_dir = self._run_dir / "report"
        ReportGenerator.print_console_table(
            results,
            self._run_id,
            self._duration_s,
            self._capital,
        )
        ReportGenerator.save_json(
            results,
            report_dir / "summary.json",
            run_meta,
        )
        ReportGenerator.save_markdown(
            results,
            report_dir / "summary.md",
            self._run_id,
            self._duration_s,
            self._capital,
        )

        logger.info(
            "Matrix run complete. Results in %s",
            self._run_dir,
        )
        return results

    # ── directory setup ──────────────────────────────────────────────

    def _create_run_directory(self) -> None:
        """Create the run directory and per-profile subdirectories."""
        self._run_dir.mkdir(parents=True, exist_ok=True)
        for p in self._profiles:
            (self._run_dir / p.name).mkdir(exist_ok=True)
        logger.info("Run directory: %s", self._run_dir)

    def _save_run_metadata(self) -> dict:
        """Save run configuration as JSON for reproducibility."""
        meta = {
            "run_id": self._run_id,
            "start_time": datetime.now().isoformat(),
            "duration_s": self._duration_s,
            "capital": self._capital,
            "max_parallel": self._max_parallel,
            "python": sys.executable,
            "cwd": str(PROJECT_ROOT),
            "profiles": {
                p.name: {
                    "description": p.description,
                    "env_overrides": p.env_overrides,
                }
                for p in self._profiles
            },
        }
        meta_path = self._run_dir / "run_meta.json"
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
        return meta

    # ── process management ───────────────────────────────────────────

    def _build_env(self, profile: Profile, profile_dir: Path) -> dict[str, str]:
        """Build the environment for a subprocess."""
        env = os.environ.copy()
        # Apply profile-specific overrides
        for key, value in profile.env_overrides.items():
            env[key] = value
        # Route logs to profile-specific directory
        env["LOG_DIR"] = str(profile_dir)
        return env

    def _spawn_process(
        self,
        profile: Profile,
        profile_dir: Path,
    ) -> subprocess.Popen:
        """Spawn a single bot subprocess."""
        env = self._build_env(profile, profile_dir)
        stdout_path = profile_dir / "stdout.log"
        stderr_path = profile_dir / "stderr.log"
        stdout_file = open(stdout_path, "w")
        stderr_file = open(stderr_path, "w")

        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "main.py"),
            "--dry-run",
            "--capital",
            str(self._capital),
            "--duration",
            str(self._duration_s),
        ]

        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(PROJECT_ROOT),
            stdout=stdout_file,
            stderr=stderr_file,
        )
        logger.info(
            "  Spawned '%s' (PID %d)",
            profile.name,
            proc.pid,
        )
        return proc

    def _spawn_all(self) -> dict[str, subprocess.Popen]:
        """Spawn all profiles, respecting max_parallel limit."""
        processes: dict[str, subprocess.Popen] = {}
        pending = list(self._profiles)

        while pending:
            # How many slots are free?
            active_count = sum(1 for p in processes.values() if p.poll() is None)
            slots = self._max_parallel - active_count

            batch = pending[:slots] if slots > 0 else []
            pending = pending[len(batch) :]

            for profile in batch:
                profile_dir = self._run_dir / profile.name
                proc = self._spawn_process(profile, profile_dir)
                processes[profile.name] = proc
                # Stagger launches to avoid Gamma API burst
                if pending or batch[-1] is not profile:
                    time.sleep(self._LAUNCH_STAGGER_S)

            # If we still have pending profiles, wait a bit before checking slots
            if pending:
                time.sleep(2.0)

        return processes

    def _wait_for_completion(
        self,
        processes: dict[str, subprocess.Popen],
    ) -> None:
        """Wait for all processes to finish, with graceful escalation."""
        deadline = time.monotonic() + self._duration_s + self._GRACE_PERIOD_S

        logger.info(
            "All %d processes running. Waiting up to %ds + %ds grace...",
            len(processes),
            self._duration_s,
            int(self._GRACE_PERIOD_S),
        )

        # Poll until deadline
        while time.monotonic() < deadline:
            all_done = all(p.poll() is not None for p in processes.values())
            if all_done:
                logger.info("All processes completed normally.")
                break
            time.sleep(2.0)

        # SIGTERM for stragglers
        for name, proc in processes.items():
            if proc.poll() is None:
                logger.warning(
                    "Profile '%s' (PID %d) still running — sending SIGTERM",
                    name,
                    proc.pid,
                )
                try:
                    proc.terminate()
                except OSError:
                    pass

        # Final wait with SIGKILL fallback
        for name, proc in processes.items():
            try:
                proc.wait(timeout=self._KILL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                logger.error(
                    "Profile '%s' (PID %d) did not exit — sending SIGKILL",
                    name,
                    proc.pid,
                )
                proc.kill()
                proc.wait()

            # Record exit code
            ec_path = self._run_dir / name / ".exit_code"
            ec_path.write_text(str(proc.returncode))

        logger.info("All processes stopped.")


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parallel test matrix — run multiple bot configs simultaneously",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tools/test_matrix.py --duration 3600\n"
            "  python tools/test_matrix.py --profiles moderate,aggressive -d 300\n"
            "  python tools/test_matrix.py --list-profiles\n"
            "  python tools/test_matrix.py --custom-profiles my.json --duration 600\n"
        ),
    )
    parser.add_argument(
        "--profiles",
        default="all",
        help=(
            "Comma-separated profile names, or 'all' for all built-in " "(default: all)"
        ),
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=int,
        default=3600,
        help="Duration per bot in seconds (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=10_000.0,
        help="Starting capital per instance in USDC (default: 10000)",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=0,
        help="Max concurrent processes (0 = all at once, default: 0)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "matrix_runs"),
        help="Base directory for run artifacts (default: matrix_runs/)",
    )
    parser.add_argument(
        "--custom-profiles",
        default=None,
        help="Path to JSON file with custom profile definitions",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Custom run identifier (default: timestamp)",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List all available profiles and exit",
    )
    return parser.parse_args()


def main() -> None:
    # Basic logging for the orchestrator itself
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()

    # Resolve available profiles
    all_profiles = dict(BUILTIN_PROFILES)
    if args.custom_profiles:
        custom = load_custom_profiles(args.custom_profiles)
        all_profiles.update(custom)
        logger.info(
            "Loaded %d custom profiles from %s", len(custom), args.custom_profiles
        )

    # List and exit
    if args.list_profiles:
        print("\nAvailable profiles:\n")
        for name, p in sorted(all_profiles.items()):
            print(f"  {name:20s} — {p.description}")
            if p.env_overrides:
                for k, v in sorted(p.env_overrides.items()):
                    print(f"    {k}={v}")
            print()
        return

    # Select profiles
    if args.profiles == "all":
        selected = list(all_profiles.values())
    else:
        names = [n.strip() for n in args.profiles.split(",")]
        selected = []
        for n in names:
            if n not in all_profiles:
                print(f"[ERROR] Unknown profile: '{n}'", file=sys.stderr)
                print(
                    f"  Available: {', '.join(sorted(all_profiles))}",
                    file=sys.stderr,
                )
                sys.exit(1)
            selected.append(all_profiles[n])

    if not selected:
        print("[ERROR] No profiles selected.", file=sys.stderr)
        sys.exit(1)

    # Run the matrix
    orchestrator = MatrixOrchestrator(
        profiles=selected,
        duration_s=args.duration,
        capital=args.capital,
        output_dir=args.output_dir,
        max_parallel=args.max_parallel,
        run_id=args.run_id,
    )

    try:
        results = orchestrator.run()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user — results may be partial.")
        sys.exit(130)

    # Exit code: 0 if at least one profile produced trades
    if any(r.total_trades > 0 for r in results.values()):
        sys.exit(0)
    else:
        logger.warning("No profiles produced any trades.")
        sys.exit(1)


if __name__ == "__main__":
    main()
