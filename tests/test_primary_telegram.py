"""Venue selection, independent credentials and nonblocking trade notices."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan
from entropy_arb.config import ConfigError, TelegramConf, load_config
from entropy_arb.engine import Engine
from entropy_arb.telegram import TelegramNotifier, trade_message


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in ("PRIMARY_VENUE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY_XYZ",
                 "HL_ACCOUNT_ADDRESS_XYZ", "LIGHTER_ACCOUNT_INDEX",
                 "LIGHTER_API_KEY_INDEX", "LIGHTER_API_PRIVATE_KEY",
                 "LIGHTER_RH_ACCOUNT_INDEX", "LIGHTER_RH_API_KEY_INDEX",
                 "LIGHTER_RH_API_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")

    def load(**kwargs):
        return load_config(str(path), str(tmp_path / "missing.env"),
                           symbol="LIT", **kwargs)
    return load


def test_default_pair_and_env_cli_precedence(config, monkeypatch):
    cfg = config()
    assert cfg.primary_venue == "lighter-rh"
    assert cfg.entropy.label == "RH" and cfg.hedge.label == "LIGHTER"
    assert cfg.entropy.lighter_profile.chain_id == 466324
    assert not cfg.telegram.enabled
    monkeypatch.setenv("PRIMARY_VENUE", "entropy")
    assert config().entropy.kind == "hl"
    assert config(primary_venue="light-rh").entropy.label == "RH"


@pytest.mark.parametrize("primary,hedge", [
    ("lighter-rh", "lighter-rh"), ("lighter", "lighter"),
    ("tradexyz", "tradexyz"), ("invalid", "lighter"),
])
def test_invalid_or_identical_markets_rejected(config, primary, hedge):
    with pytest.raises(ConfigError):
        config(primary_venue=primary, hedge_venue=hedge)


def test_lighter_credentials_are_independent(config, monkeypatch):
    monkeypatch.setenv("LIGHTER_ACCOUNT_INDEX", "11")
    monkeypatch.setenv("LIGHTER_API_KEY_INDEX", "2")
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "main-key")
    cfg = config()
    assert not cfg.creds_complete  # Never silently use mainnet keys on RH.
    monkeypatch.setenv("LIGHTER_RH_ACCOUNT_INDEX", "22")
    monkeypatch.setenv("LIGHTER_RH_API_KEY_INDEX", "3")
    monkeypatch.setenv("LIGHTER_RH_API_PRIVATE_KEY", "rh-key")
    cfg = config()
    assert cfg.creds_complete
    assert cfg.entropy.lighter_creds.account_index == 22
    assert cfg.hedge.lighter_creds.account_index == 11
    assert cfg.entropy.lighter_creds.api_private_key == "rh-key"
    assert cfg.hedge.lighter_creds.api_private_key == "main-key"
    swapped = config(primary_venue="lighter", hedge_venue="lighter-rh")
    assert swapped.entropy.lighter_creds.account_index == 11
    assert swapped.hedge.lighter_creds.account_index == 22


def test_legacy_single_rh_credentials_and_primary_settings(config, monkeypatch):
    monkeypatch.setenv("LIGHTER_ACCOUNT_INDEX", "11")
    cfg = config(primary_venue="entropy", hedge_venue="lighter-rh")
    assert cfg.hedge.lighter_creds.account_index == 11
    monkeypatch.setenv("LIGHTER_RH_API_PRIVATE_KEY", "rh-only")
    # Partial RH settings must not be mixed with legacy mainnet credentials.
    assert config().entropy.lighter_creds.account_index is None


def test_primary_settings_override_legacy_without_changing_hedge(config, tmp_path):
    with (tmp_path / "config.yaml").open("a") as fh:
        fh.write("entropy:\n  max_position_usd: 500\n"
                 "primary:\n  max_position_usd: 800\n  max_orders_per_min: 21\n"
                 "hedge:\n  max_position_usd: 300\n")
    for primary in ("entropy", "light-rh", "tradexyz"):
        cfg = config(primary_venue=primary)
        assert cfg.entropy.cap_usd == 800
        assert cfg.entropy.orders_per_min == 21
        assert cfg.hedge.cap_usd == 300


def test_telegram_settings_must_be_paired(config, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    with pytest.raises(ConfigError, match="must be set together"):
        config()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    assert config().telegram.enabled
    assert "secret-token" not in repr(config().telegram)


def test_message_matches_requested_template():
    message = trade_message(
        symbol="LIT", direction="sell_primary", state="完成",
        buy_name="LIGHTER", sell_name="RH", buy_status="filled", sell_status="filled",
        buy_fill=7.77, sell_fill=7.77, qty=7.77, notional=46.02, profit=-0.0047,
        total_notional=8985.25, total_profit=-0.6478, hedges=3,
        balances=[("RH", 3866.18, 6906.60), ("LIGHTER", 7416.65, 9711.40)],
        account_delta=-28.8806)
    assert message == """交易通知 · LIT
状态：完成 · 方向：sell_primary
买入：LIGHTER filled，成交 7.77
卖出：RH filled，成交 7.77
计划数量：7.77
本次成交额（两边合计）：$46.02 · 本次盈利：$-0.0047
累计成交额（两边合计）：$8,985.25 · 累计盈利：$-0.6478
对冲笔数：3
RH余额：可用 $3,866.18 · 权益 $6,906.60
LIGHTER余额：可用 $7,416.65 · 权益 $9,711.40
账户权益变化：$-28.8806"""


@pytest.mark.parametrize("outcome,state", [
    ("filled", "完成"), ("partial", "部分成交"), ("timeout", "待确认"),
    ("error", "异常"), ("unknown_price", "完成"), ("empty", None),
])
def test_execution_accounting_and_notification(config, tmp_path, outcome, state):
    async def go():
        cfg = config()
        cfg.telegram = TelegramConf("test-token", "123")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        eng = Engine(cfg)
        plan = ArbPlan(7.77, 3.0, 3.01, 23.31, 23.3877, 7.77, 23.31,
                       33.33, 33.33, 0.0001, 0.0002)

        def venue(key, name, is_buy):
            qty = 0.0 if outcome == "empty" else 7.77
            if not is_buy and outcome in ("partial", "timeout", "error"):
                qty = 3.0 if outcome == "partial" else 0.0
            price = (3.0 if is_buy else 3.01) if qty else None
            if outcome == "unknown_price" and not is_buy:
                price = None
            status = "filled" if qty == 7.77 else "canceled"
            unresolved = outcome == "timeout" and not is_buy
            if unresolved:
                status = "timeout"
            async def send_taker(**kwargs):
                return {"status": status, "filled_base": qty, "avg_px": price,
                        "err": "failure" if outcome == "error" and not is_buy else None,
                        "unresolved": unresolved}
            return SimpleNamespace(
                key=key, name=name, position=0.0, cash=0.0, volume_usd=0.0,
                cap_usd=1000.0, fee_bps=0.0, equity=100.0, free=80.0,
                start_equity=101.0, last_traded_ts=0.0,
                px_round=lambda px, round_up: px, send_taker=send_taker)

        buy, sell = venue("hedge", "LIGHTER", True), venue("entropy", "RH", False)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        cfg.inventory_scale_bps = 0.0
        unresolved = await eng._execute(buy, sell, plan)
        assert unresolved == (outcome == "timeout")
        assert buy.position == (0.0 if outcome == "empty" else 7.77)
        if state is None:
            assert eng.telegram.queue.empty()
            return
        msg = eng.telegram.queue.get_nowait()
        assert f"状态：{state} · 方向：sell_primary" in msg
        assert "账户权益变化：$-2.0000" in msg
        if outcome in ("timeout", "unknown_price"):
            assert "本次盈利：—" in msg
        if outcome == "filled":
            expected = 7.77 * (3.01 * (1 - 0.0002) - 3.0 * (1 + 0.0001))
            assert eng.total_fill_edge == pytest.approx(expected)
            assert f"本次盈利：${expected:.4f}" in msg
            assert "本次成交额（两边合计）：$46.70" in msg
            assert sell.position == -7.77
    asyncio.run(go())


def test_failed_telegram_send_does_not_stop_worker_or_expose_token(caplog):
    async def go():
        calls = []
        class Response:
            status = 200
            async def __aenter__(self):
                if len(calls) == 1:
                    raise RuntimeError("URL contains secret-token")
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self):
                return {"ok": True}
        class Session:
            def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response()
        notifier = TelegramNotifier(TelegramConf("secret-token", "-100123"))
        notifier.start(Session())
        notifier.enqueue("first")
        notifier.enqueue("second")
        await notifier.close()
        assert len(calls) == 2
        assert calls[1][1]["json"] == {"chat_id": "-100123", "text": "second"}
        assert "secret-token" not in caplog.text
    asyncio.run(go())


@pytest.mark.parametrize("primary,hedge", [
    ("light-rh", "lighter"), ("light-rh", "tradexyz"),
    ("entropy", "tradexyz"), ("tradexyz", "lighter"),
])
def test_venue_startup_handles_hl_specific_methods(config, primary, hedge, tmp_path):
    async def go():
        cfg = config(primary_venue=primary, hedge_venue=hedge)
        for conf in (cfg.entropy, cfg.hedge):
            if conf.kind == "lighter":
                conf.lighter_creds.account_index = 1
                conf.lighter_creds.api_key_index = 2
                conf.lighter_creds.api_private_key = "fake"
            else:
                conf.hl_creds.private_key = "fake"
        cfg.recorder_enabled = False
        eng = Engine(cfg)
        shared = []
        def make(conf):
            async def nothing(*args, **kwargs):
                pass
            v = SimpleNamespace(
                conf=conf, key=conf.key, name=conf.label, kind=conf.kind,
                size_decimals=4, min_base=0.01, min_quote=10, fee_bps=conf.fee_bps,
                position=0, init_signer=lambda: None, load_market=nothing,
                start_tasks=lambda *args: [], close=nothing)
            if conf.kind == "hl":
                v.share_nonces_with = lambda other: shared.append(other)
                v._query_address = lambda: "same-account"
                v.include_core_equity = True
            return v
        eng._make_venue = make
        async def reconcile(**kwargs):
            pass
        eng._reconcile_positions = reconcile
        eng.stop.set()
        await eng._run_inner()
        assert eng.markets_ready
        assert bool(shared) == (cfg.entropy.kind == cfg.hedge.kind == "hl")
        if shared:
            assert eng.hedge.include_core_equity is False
    asyncio.run(go())
