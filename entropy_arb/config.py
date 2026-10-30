"""Configuration: strategy from a YAML file, credentials from .env, market
selection (symbol + primary/hedge venues) from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds credentials,
Telegram settings and the default PRIMARY_VENUE. --primary overrides that
default (RH); --hedge defaults to Lighter mainnet. --symbol is required.
Every YAML key is validated against the schema below, so a typo is an error
rather than a setting that silently does nothing.

Threshold model (numbers the user derives from recorded minute data):

    premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.

    midline_bps: auto replaces the fixed midline with a time-weighted EMA
    of the live mid premium (time constant midline_window_min), so the
    band follows the premium instead of pinning one direction when the
    basis drifts away from a hardcoded center.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import quote

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz")
PRIMARY_VENUES = ("light-rh", "lighter-rh", "lighter", "entropy", "tradexyz")
LEAD_VENUES = ("none", "primary", "hedge")


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None


@dataclass
class MakerConf:
    """Maker mode: post-only quotes on the primary (RH), hedged on the hedge
    venue. See the `maker:` section of config.example.yaml."""
    half_spread_bps: float = 3.0
    quote_usd: float = 50.0
    max_position_usd: float = 200.0
    requote_bps: float = 1.0
    max_actions_per_min: int = 600
    center_window_sec: float = 900.0
    center_warmup_sec: float = 300.0
    maker_fee_bps: float = 0.0
    # volatility guard: any trigger pulls both quotes for pause_sec
    move_window_sec: float = 10.0
    max_move_bps: float = 10.0
    long_move_window_sec: float = 120.0
    max_long_move_bps: float = 30.0
    max_primary_move_bps: float = 15.0
    max_hedge_spread_bps: float = 5.0
    max_basis_dev_bps: float = 15.0
    max_fills_per_min: int = 10
    pause_sec: float = 60.0
    # blackout windows "HH:MM-HH:MM" in blackout_tz (weekdays only)
    blackout_windows: tuple = ()
    blackout_tz: str = "America/New_York"
    # session stop: stop quoting for good once mark-to-market loss exceeds
    max_loss_usd: float = 5.0
    # exchange-side dead man's switch (scheduled cancel-all); 0 = off
    dead_man_sec: float = 60.0
    fills_csv: str = "logs/{symbol}/maker_fills.csv"


@dataclass
class TelegramConf:
    bot_token: Optional[str] = field(default=None, repr=False)
    chat_id: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal); midline_bps is None when midline_auto
    midline_bps: Optional[float]
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    lead_venue: str
    lead_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    hedges_csv: str
    dashboard: bool
    log_file: str
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL
    primary_venue: str = "lighter-rh"
    telegram: TelegramConf = field(default_factory=TelegramConf)
    # auto midline: EMA time constant, and how long it must run before trading
    midline_auto: bool = False
    midline_window_min: float = 60.0
    midline_warmup_min: float = 10.0
    maker: MakerConf = field(default_factory=MakerConf)

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": "float|auto",
        "upper_bps": float,
        "lower_bps": float,
        "midline_window_min": float,
        "midline_warmup_min": float,
    },
    "entropy": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "primary": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "hedge": {
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "lead_venue": str,
        "lead_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
    },
    "maker": {
        "half_spread_bps": float,
        "quote_usd": float,
        "max_position_usd": float,
        "requote_bps": float,
        "max_actions_per_min": int,
        "center_window_sec": float,
        "center_warmup_sec": float,
        "maker_fee_bps": float,
        "move_window_sec": float,
        "max_move_bps": float,
        "long_move_window_sec": float,
        "max_long_move_bps": float,
        "max_primary_move_bps": float,
        "max_hedge_spread_bps": float,
        "max_basis_dev_bps": float,
        "max_fills_per_min": int,
        "pause_sec": float,
        "blackout_windows": "list[str]",
        "blackout_tz": str,
        "max_loss_usd": float,
        "dead_man_sec": float,
        "fills_csv": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "hedges_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")
        elif want == "list[str]":
            if not (isinstance(val, list)
                    and all(isinstance(x, str) for x in val)):
                raise ConfigError(f"'{here}' must be a list of strings, "
                                  f"got {val!r}")
        elif want == "float|auto":
            if not (val == "auto" or (isinstance(val, (int, float))
                                      and not isinstance(val, bool))):
                raise ConfigError(f"'{here}' must be a number or auto, "
                                  f"got {val!r} / 必须是数字或 auto")


def parse_window(text: str):
    """'HH:MM-HH:MM' -> (start_min, end_min); end < start wraps midnight."""
    try:
        a, b = text.split("-")
        out = []
        for part in (a, b):
            h, m = part.strip().split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            out.append(h * 60 + m)
    except ValueError:
        raise ConfigError(f"maker.blackout_windows entry {text!r} must look "
                          f"like '15:55-16:45' / 格式应为 '15:55-16:45'")
    if out[0] == out[1]:
        raise ConfigError(f"maker.blackout_windows entry {text!r} is empty")
    return out[0], out[1]


def _load_maker(raw: dict, symbol: str) -> MakerConf:
    m = raw.get("maker") or {}
    d = MakerConf()
    conf = MakerConf(**{
        k: (tuple(m[k]) if k == "blackout_windows" else type(getattr(d, k))(m[k]))
        for k in m})
    for k in ("half_spread_bps", "quote_usd", "max_position_usd",
              "center_window_sec", "move_window_sec", "long_move_window_sec",
              "max_move_bps", "max_long_move_bps", "max_primary_move_bps",
              "max_hedge_spread_bps", "max_basis_dev_bps", "max_loss_usd"):
        if getattr(conf, k) <= 0:
            raise ConfigError(f"maker.{k} must be > 0 / 必须大于 0")
    for k in ("requote_bps", "center_warmup_sec", "maker_fee_bps",
              "pause_sec", "dead_man_sec"):
        if getattr(conf, k) < 0:
            raise ConfigError(f"maker.{k} must be >= 0 / 不能为负数")
    if conf.max_actions_per_min < 1 or conf.max_fills_per_min < 1:
        raise ConfigError("maker.max_actions_per_min and max_fills_per_min "
                          "must be >= 1")
    if conf.max_position_usd < conf.quote_usd:
        raise ConfigError("maker.max_position_usd must be >= quote_usd / "
                          "仓位上限不能小于单笔报价金额")
    if 0 < conf.dead_man_sec < 15:
        raise ConfigError("maker.dead_man_sec must be 0 (off) or >= 15")
    for w in conf.blackout_windows:
        parse_window(w)
    conf.fills_csv = _symbol_path(conf.fills_csv, symbol)
    return conf


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


def _symbol_path(path: str, symbol: str) -> str:
    """Expand lowercase symbol directories, including legacy fixed paths."""
    if not path:
        return path
    tag = quote(symbol.lower(), safe="")
    if tag in (".", ".."):
        tag = tag.replace(".", "%2E")
    if "{symbol}" in path:
        return path.replace("{symbol}", tag)
    directory, filename = os.path.split(path)
    if os.path.basename(directory) == tag:
        return path
    return os.path.join(directory, tag, filename)


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else None


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, hedge_venue: str = "lighter",
                primary_venue: Optional[str] = None) -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    primary_venue = primary_venue or _env_s("PRIMARY_VENUE") or "light-rh"
    if primary_venue not in PRIMARY_VENUES:
        raise ConfigError(f"--primary / PRIMARY_VENUE must be one of {list(PRIMARY_VENUES)}")
    if primary_venue == "light-rh":
        primary_venue = "lighter-rh"
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge 必须是 {list(HEDGE_VENUES)} 之一")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")
    midline_auto = thr["midline_bps"] == "auto"
    midline_window = float(thr.get("midline_window_min", 60.0))
    midline_warmup = float(thr.get("midline_warmup_min", 10.0))
    if midline_window <= 0 or midline_warmup < 0:
        raise ConfigError("thresholds.midline_window_min must be > 0 and "
                          "midline_warmup_min >= 0 / 窗口必须大于 0，预热不能为负")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    lead_venue = str(_get(raw, "execution", "lead_venue", "none"))
    if lead_venue not in LEAD_VENUES:
        raise ConfigError(f"execution.lead_venue must be one of {list(LEAD_VENUES)}, "
                          f"got {lead_venue!r} / 必须是 {list(LEAD_VENUES)} 之一")
    lead_slippage = float(_get(raw, "execution", "lead_slippage_bps", 0.0))
    if lead_slippage < 0:
        raise ConfigError("execution.lead_slippage_bps must be >= 0 / 不能为负数")

    # Keep the internal "entropy" slot and signal keys for compatibility;
    # it now holds whichever primary venue is selected.
    primary_settings = dict(raw.get("entropy") or {}) if primary_venue == "entropy" else {}
    primary_settings.update(raw.get("primary") or {})

    def make_venue(name: str, key: str, settings: dict) -> VenueConf:
        is_lighter = name in LIGHTER_PROFILES
        conf = VenueConf(
            key=key, kind="lighter" if is_lighter else "hl",
            label={"entropy": "ENTROPY", "tradexyz": "XYZ",
                   "lighter": "LIGHTER", "lighter-rh": "RH"}[name],
            symbol=symbol,
            fee_bps=float(settings.get("taker_fee_bps", 1.0 if name == "tradexyz" else 0.0)),
            cap_usd=float(settings.get("max_position_usd", 1000.0)),
            orders_per_min=int(settings.get("max_orders_per_min", 30 if is_lighter else 120)),
        )
        if is_lighter:
            conf.lighter_profile = LIGHTER_PROFILES[name]
            # RH has separate credentials. Legacy LIGHTER_* fallback remains
            # valid for a single Lighter deployment, never for a two-chain pair.
            prefix = "LIGHTER"
            if name == "lighter-rh":
                rh_configured = any(_env_s("LIGHTER_RH_" + suffix) for suffix in
                                    ("ACCOUNT_INDEX", "API_KEY_INDEX", "API_PRIVATE_KEY"))
                if rh_configured or "lighter" in (primary_venue, hedge_venue):
                    prefix = "LIGHTER_RH"
            conf.lighter_creds = LighterCreds(
                _env_i(prefix + "_ACCOUNT_INDEX"), _env_i(prefix + "_API_KEY_INDEX"),
                _env_s(prefix + "_API_PRIVATE_KEY"))
        else:
            conf.hl_dex = settings.get("dex", "io") if name == "entropy" else "xyz"
            conf.hl_creds = HLCreds(
                (_env_s("HL_PRIVATE_KEY_XYZ") if name == "tradexyz" else None)
                or _env_s("HL_PRIVATE_KEY"),
                (_env_s("HL_ACCOUNT_ADDRESS_XYZ") if name == "tradexyz" else None)
                or _env_s("HL_ACCOUNT_ADDRESS"))
        return conf

    entropy = make_venue(primary_venue, "entropy", primary_settings)
    hedge = make_venue(hedge_venue, "hedge", raw.get("hedge") or {})
    if (primary_venue == hedge_venue or
            (entropy.kind == hedge.kind == "hl" and entropy.hl_dex == hedge.hl_dex)):
        raise ConfigError("the same market on both legs / 两条腿是同一个市场")

    telegram = TelegramConf(_env_s("TELEGRAM_BOT_TOKEN"), _env_s("TELEGRAM_CHAT_ID"))
    if bool(telegram.bot_token) != bool(telegram.chat_id):
        raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together / "
                          "Telegram 的 token 和 chat_id 必须同时填写")

    return Config(
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        primary_venue=primary_venue,
        telegram=telegram,
        midline_bps=None if midline_auto else float(thr["midline_bps"]),
        midline_auto=midline_auto,
        midline_window_min=midline_window,
        midline_warmup_min=midline_warmup,
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        lead_venue=lead_venue,
        lead_slippage_bps=lead_slippage,
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=_symbol_path(
            _get(raw, "recorder", "csv", "logs/{symbol}/minutes.csv"), symbol),
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_symbol_path(
            _get(raw, "logging", "trades_csv", "logs/{symbol}/trades.csv"), symbol),
        hedges_csv=_symbol_path(
            _get(raw, "logging", "hedges_csv", "logs/{symbol}/hedges.csv"), symbol),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_symbol_path(
            _get(raw, "logging", "file", "logs/{symbol}/engine.log"), symbol),
        maker=_load_maker(raw, symbol),
    )
