"""Typed configuration. YAML files in ./config, secrets from .env only.

Loading is strict on purpose: an unknown key or a bad type must fail at startup,
not silently change how real money is traded later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core.money import D

DEFAULT_CONFIG_DIR = Path("config")


class Mode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class FeedKind(StrEnum):
    SYNTHETIC = "synthetic"
    CCXT = "ccxt"
    STREAM = "stream"


class _Strict(BaseModel):
    """Reject unknown keys: a typo in a risk limit must never be silently ignored."""

    model_config = ConfigDict(extra="forbid")


class SyntheticConfig(_Strict):
    seed: int = 7
    speed: float = 60.0
    start_ts: str = "2024-01-01T00:00:00Z"
    equity_regime: str = "mixed"


class CcxtConfig(_Strict):
    exchange_id: str = "binance"
    testnet: bool = False
    depth_levels: int = 20
    rest_poll_seconds: int = 5


class PaperConfig(_Strict):
    maker_fee: Decimal = D("0.001")
    taker_fee: Decimal = D("0.001")
    latency_ms: tuple[int, int] = (100, 500)
    api_fail_rate: Decimal = D("0.001")
    reject_rate_min_notional: bool = True
    book_levels: int = 20
    book_depth_usdt: Decimal = D("250000")

    @field_validator("latency_ms")
    @classmethod
    def _latency_order(cls, v: tuple[int, int]) -> tuple[int, int]:
        lo, hi = v
        if lo < 0 or hi < lo:
            raise ValueError("latency_ms must be [lo, hi] with 0 <= lo <= hi")
        return (lo, hi)


class FeeConfig(_Strict):
    maker: Decimal = D("0.001")
    taker: Decimal = D("0.001")


class DataQualityConfig(_Strict):
    stale_symbol_seconds: int = 15
    stale_all_seconds: int = 60
    spike_pct: Decimal = D("0.05")
    ntp_max_drift_seconds: float = 1.0


class ReportConfig(_Strict):
    day_boundary_utc: str = "00:00"
    display_timezone: str = "Asia/Kolkata"
    dir: str = "reports"
    send_within_seconds: int = 300


class AlertConfig(_Strict):
    min_level: str = "WARN"
    dedupe_seconds: int = 120


class LiveGuardsConfig(_Strict):
    require_confirmation: bool = True
    go_nogo_max_age_days: int = 14
    require_withdrawals_disabled: bool = True
    require_ip_whitelist: bool = True
    ramp_multipliers: tuple[Decimal, ...] = (D("0.10"), D("0.25"), D("0.50"), D("1.0"))
    ramp_min_days: tuple[int, ...] = (7, 7, 7, 0)
    ramp_max_cost_slippage_ratio: Decimal = D("1.5")
    require_reconciliation_clean: bool = True


class PortfolioConfig(_Strict):
    max_open_positions: int = 5
    correlated_buckets: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "major": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "alt": ["BNB/USDT", "XRP/USDT"],
        }
    )
    strategy_capital_weights: dict[str, Decimal] = Field(default_factory=dict)

    def bucket_of(self, symbol: str) -> str:
        for bucket, symbols in self.correlated_buckets.items():
            if symbol in symbols:
                return bucket
        return f"single:{symbol}"


class ExecutionConfig(_Strict):
    entry_timeout_seconds: int = 120
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.5
    stop_placement_deadline_seconds: float = 3.0
    health_check_seconds: float = 5.0
    equity_snapshot_bars: int = 1
    order_status_poll_seconds: float = 5.0
    # An L1 pause caused by a transient condition lifts only after this much sustained
    # health (plan section: "auto-resume after at least 10 minutes of sustained health").
    auto_resume_min_healthy_seconds: float = 600.0


class WatchdogConfig(_Strict):
    heartbeat_seconds: float = 2.0
    heartbeat_lost_seconds: float = 10.0


class LoggingConfig(_Strict):
    level: str = "INFO"
    json_logs: bool = Field(default=True, alias="json")
    file: str = "logs/tradebot.log"


class Settings(_Strict):
    mode: Mode = Mode.PAPER
    environment: str = "dev"
    quote_currency: str = "USDT"
    base_timeframe: str = "1m"
    strategy_timeframes: tuple[str, ...] = ("15m", "1h")
    starting_capital_usdt: Decimal = D("1000")
    max_capital_usdt: Decimal = D("250")
    feed: FeedKind = FeedKind.SYNTHETIC
    synthetic: SyntheticConfig = SyntheticConfig()
    ccxt: CcxtConfig = CcxtConfig()
    paper: PaperConfig = PaperConfig()
    live_fees: FeeConfig = FeeConfig()
    data_quality: DataQualityConfig = DataQualityConfig()
    report: ReportConfig = ReportConfig()
    alerts: AlertConfig = AlertConfig()
    live_guards: LiveGuardsConfig = LiveGuardsConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    execution: ExecutionConfig = ExecutionConfig()
    watchdog: WatchdogConfig = WatchdogConfig()
    logging: LoggingConfig = LoggingConfig()

    # runtime paths, filled by the loader (not in YAML)
    config_dir: Path = DEFAULT_CONFIG_DIR
    data_dir: Path = Path("data")
    state_dir: Path = Path("data/state")

    @property
    def db_path(self) -> Path:
        """Paper, testnet and live never share a database (section 3)."""
        return self.state_dir / f"tradebot_{self.mode.value}.db"

    @property
    def reports_dir(self) -> Path:
        return Path(self.report.dir) / self.mode.value



class RiskLimits(_Strict):
    risk_per_trade: Decimal = D("0.005")
    per_symbol_exposure: Decimal = D("0.20")
    total_exposure: Decimal = D("0.60")
    correlated_bucket_exposure: Decimal = D("0.40")
    max_open_positions: int = 5
    compounding_cap_mult: Decimal = D("1.5")
    max_spread_pct: Decimal = D("0.0015")
    fat_finger_pct: Decimal = D("0.01")
    min_notional: Decimal = D("10")
    max_notional: Decimal = D("150")
    max_order_book_pct: Decimal = D("0.10")
    orders_per_minute: int = 10
    orders_per_day: int = 300
    consecutive_losses_halve: int = 4
    consecutive_losses_pause: int = 6
    consecutive_losses_pause_hours: int = 24
    daily_loss_stop_new_entries: Decimal = D("-0.02")
    daily_loss_flatten: Decimal = D("-0.03")
    weekly_loss_pause: Decimal = D("-0.05")
    max_drawdown_lockdown: Decimal = D("-0.10")
    cooldown_minutes_after_stop: int = 15
    time_stop_bars: int = 64
    partial_tp_r: Decimal = D("1.5")
    partial_tp_fraction: Decimal = D("0.5")
    breakeven_after_partial: bool = True
    trail_atr_mult: Decimal = D("3.0")
    profit_lock_half_size_at: Decimal = D("0.03")
    profit_lock_no_entries_at: Decimal = D("0.05")
    atr_stop_mult: Decimal = D("1.75")
    min_stop_atr_mult: Decimal = D("1.5")
    max_stop_atr_mult: Decimal = D("2.0")
    stop_slippage_alert_mult: Decimal = D("2.0")
    tax_reserve_pct: Decimal = D("0.30")
    tds_pct_on_sales: Decimal = D("0.01")
    exchange_error_rate_limit: Decimal = D("0.20")
    exchange_error_rate_window_seconds: int = 300
    # Below this many *attempts* in the window a ratio means nothing: one failure out
    # of two is 50%, but it must not pause trading. Sample size first, then ratio.
    exchange_error_rate_min_samples: int = 10
    rejects_limit: int = 5
    rejects_window_seconds: int = 300
    clock_drift_max_seconds: float = 1.0
    disk_warn_pct: float = 0.80
    disk_pause_pct: float = 0.95

    # Hard safety envelope: no config file may exceed these. Loosening requires
    # a code change plus review, not an edit of a YAML file (section 16, item 20).
    ABSOLUTE_MAX_RISK_PER_TRADE: Decimal = D("0.02")
    ABSOLUTE_MAX_TOTAL_EXPOSURE: Decimal = D("1.0")
    ABSOLUTE_MAX_OPEN_POSITIONS: int = 10

    def validate_envelope(self) -> None:
        if self.risk_per_trade > self.ABSOLUTE_MAX_RISK_PER_TRADE:
            raise ValueError(
                f"risk_per_trade {self.risk_per_trade} exceeds hard envelope "
                f"{self.ABSOLUTE_MAX_RISK_PER_TRADE}"
            )
        if self.total_exposure > self.ABSOLUTE_MAX_TOTAL_EXPOSURE:
            raise ValueError("total_exposure exceeds hard envelope")
        if self.max_open_positions > self.ABSOLUTE_MAX_OPEN_POSITIONS:
            raise ValueError("max_open_positions exceeds hard envelope")


class StrategyConfig(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    class_name: str = Field(alias="class")
    enabled: bool = True
    timeframes: tuple[str, ...] = ("15m",)
    regimes: tuple[str, ...] = ("trend", "range", "high_vol")
    priority: int = 100
    params: dict[str, Any] = Field(default_factory=dict)


class RegimeConfig(_Strict):
    adx_period: int = 14
    adx_trend: float = 25
    adx_range_max: float = 20
    vol_lookback: int = 96
    high_vol_percentile: float = 0.85


class ExitManagementConfig(_Strict):
    time_stop_bars: int = 64
    partial_tp_r: float = 1.5
    partial_tp_fraction: float = 0.5
    trail_atr_mult: float = 3.0
    breakeven_after_partial: bool = True


class StrategiesFile(_Strict):
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)
    regime: RegimeConfig = RegimeConfig()
    exit_management: ExitManagementConfig = ExitManagementConfig()


class SymbolConfig(_Strict):
    symbol: str
    enabled: bool = True
    strategy_timeframes: tuple[str, ...] = ("15m",)
    tick_size: Decimal = D("0.01")
    lot_step: Decimal = D("0.001")
    min_qty: Decimal = D("0.001")
    min_notional: Decimal = D("5")
    basis: str = ""


class SymbolsFile(_Strict):
    symbols: tuple[SymbolConfig, ...] = ()

    @property
    def enabled(self) -> list[SymbolConfig]:
        return [s for s in self.symbols if s.enabled]

    def names(self) -> list[str]:
        return [s.symbol for s in self.symbols]


@dataclass(frozen=True)
class Secrets:
    live_trading_enabled: bool = False
    exchange_id: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    testnet_api_key: str = ""
    testnet_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_monthly_budget_usd: Decimal = D("2")
    dashboard_user: str = "admin"
    dashboard_password: str = ""

    def redacted(self) -> dict[str, Any]:
        return {
            "live_trading_enabled": self.live_trading_enabled,
            "exchange_id": self.exchange_id,
            "api_key": "set" if self.api_key else "missing",
            "telegram": "set" if self.telegram_bot_token else "missing",
            "llm": "set" if self.llm_api_key else "missing",
        }


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=value, # comments, optional quotes. No interpolation."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_env(config_dir: Path = DEFAULT_CONFIG_DIR, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Real environment wins over .env; .env is only a convenience for local runs."""
    env = dict(parse_dotenv((config_dir.parent / ".env").read_text())) if (config_dir.parent / ".env").exists() else {}
    env.update(os.environ if environ is None else environ)
    return env


def load_secrets(env: dict[str, str]) -> Secrets:
    def flag(name: str) -> bool:
        return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")

    return Secrets(
        live_trading_enabled=flag("LIVE_TRADING_ENABLED"),
        exchange_id=env.get("EXCHANGE_ID", "binance"),
        api_key=env.get("EXCHANGE_API_KEY", ""),
        api_secret=env.get("EXCHANGE_API_SECRET", ""),
        testnet_api_key=env.get("TESTNET_API_KEY", ""),
        testnet_api_secret=env.get("TESTNET_API_SECRET", ""),
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        llm_provider=env.get("LLM_PROVIDER", ""),
        llm_api_key=env.get("LLM_API_KEY", ""),
        llm_monthly_budget_usd=D(env.get("LLM_MONTHLY_BUDGET_USD", "2") or "2"),
        dashboard_user=env.get("DASHBOARD_USER", "admin"),
        dashboard_password=env.get("DASHBOARD_PASSWORD", ""),
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


@dataclass
class Config:
    settings: Settings
    risk: RiskLimits
    strategies: StrategiesFile
    symbols: SymbolsFile
    secrets: Secrets

    @property
    def mode(self) -> Mode:
        return self.settings.mode

    def symbol_names(self) -> list[str]:
        return self.symbols.names()

    def strategy(self, name: str) -> StrategyConfig:
        return self.strategies.strategies[name]

    @property
    def enabled_strategies(self) -> dict[str, StrategyConfig]:
        return {k: v for k, v in self.strategies.strategies.items() if v.enabled}

    def ensure_dirs(self) -> None:
        for p in (self.settings.data_dir, self.settings.state_dir, self.settings.reports_dir):
            Path(p).mkdir(parents=True, exist_ok=True)


def load_config(
    config_dir: Path | str = DEFAULT_CONFIG_DIR,
    *,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
    risk_overrides: dict[str, Any] | None = None,
) -> Config:
    """Load and validate every config file. Raises on any problem; nothing is guessed."""
    cdir = Path(config_dir)
    settings_raw = _read_yaml(cdir / "settings.yaml")
    if overrides:
        settings_raw = {**settings_raw, **overrides}
    settings = Settings(**settings_raw)
    settings.config_dir = cdir
    settings.state_dir = Path(settings.data_dir) / "state"

    risk_raw = _read_yaml(cdir / "risk.yaml")
    if risk_overrides:
        # only ever used to scale wall-clock *rate* limits for an accelerated feed;
        # the hard envelope below is still enforced afterwards
        risk_raw = {**risk_raw, **risk_overrides}
    risk = RiskLimits(**risk_raw)
    risk.validate_envelope()
    strategies = StrategiesFile(**_read_yaml(cdir / "strategies.yaml"))
    symbols = SymbolsFile(**_read_yaml(cdir / "symbols.yaml"))
    secrets = load_secrets(load_env(cdir, environ))

    enabled = [s.symbol for s in symbols.enabled]
    if not enabled:
        raise ValueError("no enabled symbols in symbols.yaml")
    for sym, cfg in strategies.strategies.items():
        if cfg.enabled:
            bad = set(cfg.timeframes) - set(settings.strategy_timeframes)
            if bad:
                raise ValueError(f"{sym} uses timeframes not derived in settings: {sorted(bad)}")

    return Config(settings=settings, risk=risk, strategies=strategies, symbols=symbols, secrets=secrets)
