"""
Configuration for the Polymarket BTC Latency Arbitrage Bot.

All secrets are loaded from environment variables or a .env file.
Tune thresholds, risk limits, and connection parameters here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


# ---------------------------------------------------------------------------
# Polymarket CLOB API
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PolymarketConfig:
    # REST (CLOB API — for trading)
    rest_url: str = os.getenv("POLY_REST_URL", "https://clob.polymarket.com")
    # WebSocket (CLOB API — for real-time book updates)
    ws_url: str = os.getenv(
        "POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    )
    # Max tokens per WebSocket connection (Polymarket enforces ~500 limit)
    ws_max_tokens_per_connection: int = int(
        os.getenv("POLY_WS_MAX_TOKENS_PER_CONN", "500")
    )
    # WebSocket reconnect settings (seconds)
    ws_initial_reconnect_delay: float = float(
        os.getenv("POLY_WS_INITIAL_RECONNECT_DELAY", "2.0")
    )
    ws_max_reconnect_delay: float = float(
        os.getenv("POLY_WS_MAX_RECONNECT_DELAY", "30.0")
    )
    # Gamma API (unauthenticated — for market discovery)
    gamma_url: str = os.getenv("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    chain_id: int = int(os.getenv("POLY_CHAIN_ID", "137"))  # Polygon mainnet
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    # Static condition IDs (optional — auto-discovery is preferred for
    # short-duration markets).  Leave POLY_BTC_CONDITION_IDS empty in .env
    # and enable discovery instead.
    btc_condition_ids: list[str] = field(
        default_factory=lambda: [
            cid.strip()
            for cid in os.getenv("POLY_BTC_CONDITION_IDS", "").split(",")
            if cid.strip()
        ]
    )


# ---------------------------------------------------------------------------
# Market Discovery (for 5m / 15m / 1h / 4h BTC Up/Down markets)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiscoveryConfig:
    """Auto-discover short-duration BTC Up/Down markets from Gamma API.

    Uses **predictive scheduling**: calculates when the next 5m/15m market
    window opens and polls aggressively around that time (burst mode),
    with a slower background poll in between as a safety net.
    """

    # Enable/disable auto-discovery.
    enabled: bool = os.getenv("DISCOVERY_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    # Assets to discover markets for (comma-separated).
    assets: list[str] = field(
        default_factory=lambda: [
            a.strip()
            for a in os.getenv("DISCOVERY_ASSETS", "BTC").split(",")
            if a.strip()
        ]
    )
    # Timeframes to include (comma-separated: 5m,15m,1h,4h).
    timeframes: list[str] = field(
        default_factory=lambda: [
            t.strip()
            for t in os.getenv("DISCOVERY_TIMEFRAMES", "5m,15m").split(",")
            if t.strip()
        ]
    )
    # Background poll interval between burst windows (seconds).
    interval_s: float = float(os.getenv("DISCOVERY_INTERVAL_S", "15"))
    # Fast poll interval during burst window (seconds).
    burst_poll_interval: float = float(os.getenv("DISCOVERY_BURST_INTERVAL_S", "2.0"))
    # Duration of the burst polling window around each boundary (seconds).
    burst_window: float = float(os.getenv("DISCOVERY_BURST_WINDOW_S", "15"))
    # Start burst polling this many seconds BEFORE the boundary.
    lead_time: float = float(os.getenv("DISCOVERY_LEAD_TIME_S", "5"))
    # Minimum seconds before resolution to consider a market tradeable.
    min_seconds_to_resolution: int = int(os.getenv("DISCOVERY_MIN_SECONDS", "60"))


# ---------------------------------------------------------------------------
# External Prediction Sources
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PredictionSourcesConfig:
    # Binance WebSocket for real-time BTC price
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    # CryptoCompare / CoinDesk Data Streamer — WebSocket for CCCAGG index
    cryptocompare_api_key: str = os.getenv("CRYPTOCOMPARE_API_KEY", "")
    cryptocompare_ws_url: str = os.getenv(
        "CRYPTOCOMPARE_WS_URL", "wss://data-streamer.cryptocompare.com/v2"
    )
    # CryptoCompare REST fallback (used when no API key is set)
    cryptocompare_url: str = (
        "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD"
    )
    # CoinGecko (free, no key)
    coingecko_url: str = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
    )
    # Polling interval for REST-based feeds (seconds)
    rest_poll_interval: float = float(os.getenv("REST_POLL_INTERVAL", "1.0"))


# ---------------------------------------------------------------------------
# Strategy Parameters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyConfig:
    # Minimum probability-space edge to trigger a trade.
    # Edge = fair_value - execution_price (best_ask for BUY).
    # 0.02 = require at least 2% probability advantage after spread cost.
    min_edge_threshold: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.02"))
    # Maximum probability-space edge — anything above this is suspect.
    # Near-expiry markets can legitimately show large edges (0.10–0.25)
    # so this ceiling is generous to avoid filtering real opportunities.
    max_edge_threshold: float = float(os.getenv("MAX_EDGE_THRESHOLD", "0.30"))
    # Maximum bid-ask spread to consider a market tradeable.
    # Markets with wider spreads are skipped entirely.
    # 0.10 = 10% spread maximum (e.g. bid=0.45, ask=0.55).
    max_spread: float = float(os.getenv("MAX_SPREAD", "0.10"))
    # Maker mode: when True, place limit orders inside the spread instead
    # of crossing at best_ask. The limit price = fair_value - min_edge,
    # which sits between mid and best_ask, improving execution but risking
    # non-fill. When False, always cross the spread (taker mode).
    maker_mode: bool = os.getenv("MAKER_MODE", "true").lower() in ("true", "1", "yes")
    # Prediction horizon in seconds (15 minutes).
    prediction_horizon_s: int = int(os.getenv("PREDICTION_HORIZON_S", "900"))
    # Exponential-moving-average spans for trend detection.
    ema_fast_span: int = int(os.getenv("EMA_FAST_SPAN", "12"))
    ema_slow_span: int = int(os.getenv("EMA_SLOW_SPAN", "26"))
    # Volatility look-back window (number of ticks).
    volatility_window: int = int(os.getenv("VOLATILITY_WINDOW", "60"))
    # Confidence-weighted sizing: scale position by signal strength.
    confidence_scale: bool = True
    # --- Time-to-expiry bucketed thresholds ---
    # When enabled, edge thresholds and sizing vary by time-to-expiry.
    # Near (<near_expiry_s): aggressive, lower thresholds (binary sharpens near expiry)
    # Mid (near..far): standard thresholds (uses min/max_edge_threshold above)
    # Far (>far_expiry_s): conservative, higher thresholds (more uncertainty)
    expiry_buckets_enabled: bool = os.getenv(
        "EXPIRY_BUCKETS_ENABLED", "false"
    ).lower() in ("true", "1", "yes")
    near_expiry_s: float = float(os.getenv("NEAR_EXPIRY_S", "120"))
    far_expiry_s: float = float(os.getenv("FAR_EXPIRY_S", "600"))
    # Near bucket: lower min_edge (more aggressive), allow large edges near resolution.
    near_min_edge: float = float(os.getenv("NEAR_MIN_EDGE", "0.01"))
    near_max_edge: float = float(os.getenv("NEAR_MAX_EDGE", "0.40"))
    near_size_mult: float = float(os.getenv("NEAR_SIZE_MULT", "1.2"))
    # Far bucket: higher min_edge (more conservative), tighter max edge.
    far_min_edge: float = float(os.getenv("FAR_MIN_EDGE", "0.03"))
    far_max_edge: float = float(os.getenv("FAR_MAX_EDGE", "0.20"))
    far_size_mult: float = float(os.getenv("FAR_SIZE_MULT", "0.7"))


# ---------------------------------------------------------------------------
# Risk Management
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RiskConfig:
    # Maximum fraction of total capital risked per single trade.
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "0.005"))  # 0.5%
    # Maximum daily drawdown before the bot stops trading.
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.02"))  # 2%
    # Maximum number of open positions at once.
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
    # Maximum total exposure as fraction of capital.
    max_total_exposure_pct: float = float(
        os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.10")
    )  # 10%
    # Cool-down after a losing streak (consecutive losses).
    cooldown_after_losses: int = int(os.getenv("COOLDOWN_AFTER_LOSSES", "5"))
    cooldown_duration_s: float = float(os.getenv("COOLDOWN_DURATION_S", "30.0"))
    # Sideways-market multiplier (reduce size when volatility is low).
    sideways_size_multiplier: float = float(
        os.getenv("SIDEWAYS_SIZE_MULTIPLIER", "0.4")
    )
    # Trending-market multiplier.
    trend_size_multiplier: float = float(os.getenv("TREND_SIZE_MULTIPLIER", "1.0"))


# ---------------------------------------------------------------------------
# Execution / Latency
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionConfig:
    # Target detection-to-trade latency budget (ms).
    max_latency_ms: int = int(os.getenv("MAX_LATENCY_MS", "100"))
    # Order rate cap (orders / second) to avoid rate-limit bans.
    max_orders_per_second: int = int(os.getenv("MAX_ORDERS_PER_SECOND", "50"))
    # Retry policy for transient failures.
    max_retries: int = 3
    retry_backoff_base_ms: int = 10
    # Connection pool size for REST requests.
    http_pool_size: int = int(os.getenv("HTTP_POOL_SIZE", "20"))


# ---------------------------------------------------------------------------
# Dry-Run / Paper Trading
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DryRunConfig:
    """Parameters for synthetic book generation and paper order execution."""

    # Synthetic book generator: how often to emit new books (seconds).
    book_update_interval: float = float(os.getenv("DRYRUN_BOOK_INTERVAL", "1.0"))
    # EMA smoothing for the synthetic market's price view.
    # Lower = more lag vs. real predictions = bigger edges.
    book_ema_alpha: float = float(os.getenv("DRYRUN_BOOK_EMA_ALPHA", "0.05"))
    # Gaussian noise std dev in probability space (e.g. 0.02 = 2%).
    book_noise_std: float = float(os.getenv("DRYRUN_BOOK_NOISE_STD", "0.02"))
    # Bid-ask spread as fraction of mid-price.
    book_spread_pct: float = float(os.getenv("DRYRUN_BOOK_SPREAD_PCT", "0.04"))
    # Paper order execution: simulated fill rate (0.0–1.0).
    fill_rate: float = float(os.getenv("DRYRUN_FILL_RATE", "0.95"))
    # Slippage applied to fill price (fraction, e.g. 0.005 = 0.5%).
    slippage_pct: float = float(os.getenv("DRYRUN_SLIPPAGE_PCT", "0.005"))
    # Simulated order latency (milliseconds).
    latency_ms: float = float(os.getenv("DRYRUN_LATENCY_MS", "50"))


# ---------------------------------------------------------------------------
# Machine Learning
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MLConfig:
    """Machine learning prediction model configuration."""

    # Enable ML prediction source (requires a trained model file).
    enabled: bool = os.getenv("ML_ENABLED", "false").lower() in ("true", "1", "yes")
    # Path to the trained model artifact (.pkl).
    model_path: str = os.getenv("ML_MODEL_PATH", "models/btc_5m_v3.pkl")
    # Rolling buffer size for the feature engine (seconds of history).
    feature_window: int = int(os.getenv("ML_FEATURE_WINDOW", "4000"))
    # How often to emit ML predictions (seconds).
    prediction_interval: float = float(os.getenv("ML_PREDICTION_INTERVAL", "0.25"))
    # Minimum confidence to emit a prediction (filters noise).
    min_confidence: float = float(os.getenv("ML_MIN_CONFIDENCE", "0.1"))
    # Maximum predicted return magnitude (caps extreme predictions).
    max_predicted_return: float = float(os.getenv("ML_MAX_PREDICTED_RETURN", "0.01"))
    # Prediction horizon in seconds (must match training labels).
    horizon_s: int = int(os.getenv("ML_HORIZON_S", "300"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoggingConfig:
    level: str = os.getenv("LOG_LEVEL", "INFO")
    log_dir: str = os.getenv("LOG_DIR", str(Path(__file__).parent / "logs"))
    log_file: str = "bot.log"
    trade_log_file: str = "trades.jsonl"
    max_bytes: int = 50_000_000  # 50 MB
    backup_count: int = 10


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AppConfig:
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    predictions: PredictionSourcesConfig = field(
        default_factory=PredictionSourcesConfig
    )
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    dry_run: DryRunConfig = field(default_factory=DryRunConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config() -> AppConfig:
    """Build and validate the application config."""
    cfg = AppConfig()
    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    if not cfg.polymarket.api_key:
        raise ValueError("POLY_API_KEY is required. Set it in .env or environment.")
    if not cfg.polymarket.private_key:
        raise ValueError("POLY_PRIVATE_KEY is required for signing orders.")
    # Condition IDs are only required when discovery is disabled.
    if not cfg.discovery.enabled and not cfg.polymarket.btc_condition_ids:
        raise ValueError(
            "POLY_BTC_CONDITION_IDS must list at least one condition ID "
            "when auto-discovery is disabled (DISCOVERY_ENABLED=false)."
        )
