"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge, primary_venue="entropy")


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh", primary_venue="entropy")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True


def test_lead_venue_option():
    cfg = load(MINIMAL)
    assert cfg.lead_venue == "none" and cfg.lead_slippage_bps == 0.0
    cfg = load(MINIMAL + "execution:\n  lead_venue: primary\n"
               "  lead_slippage_bps: 1.5\n")
    assert cfg.lead_venue == "primary" and cfg.lead_slippage_bps == 1.5
    for bad in ("  lead_venue: rh\n", "  lead_slippage_bps: -1\n"):
        try:
            load(MINIMAL + "execution:\n" + bad)
        except ConfigError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def test_log_paths_are_separate_for_each_symbol():
    for symbol in ("LIT", "BTC", "ETH"):
        cfg = load(MINIMAL, symbol=symbol)
        assert cfg.recorder_csv == f"logs/{symbol.lower()}/minutes.csv"
        assert cfg.trades_csv == f"logs/{symbol.lower()}/trades.csv"
        assert cfg.log_file == f"logs/{symbol.lower()}/engine.log"
    assert load(MINIMAL, symbol="BTC").trades_csv == load(
        MINIMAL, symbol="btc").trades_csv


def test_log_templates_and_legacy_fixed_paths():
    cfg = load(MINIMAL + '''
recorder:
  csv: "data/{symbol}/minutes.csv"
logging:
  trades_csv: "custom/trades.csv"
  file: "custom/engine.log"
''', symbol="LIT")
    assert cfg.recorder_csv == "data/lit/minutes.csv"
    assert cfg.trades_csv == "custom/lit/trades.csv"
    assert cfg.log_file == "custom/lit/engine.log"
    assert load(MINIMAL + '\nlogging:\n  trades_csv: "logs/lit/trades.csv"\n',
                symbol="LIT").trades_csv == "logs/lit/trades.csv"
    # Empty file continues to select console logging.
    assert load(MINIMAL + '\nlogging:\n  file: ""\n').log_file == ""


def test_symbol_cannot_escape_log_directory_or_collide_with_encoded_name():
    cfg = load(MINIMAL + '\nrecorder:\n  csv: "logs/{symbol}/minutes.csv"\n',
               symbol="../BTC/USD")
    assert cfg.recorder_csv == "logs/..%2Fbtc%2Fusd/minutes.csv"
    assert cfg.trades_csv == "logs/..%2Fbtc%2Fusd/trades.csv"
    assert load(MINIMAL, symbol="BTC/USD").trades_csv != load(
        MINIMAL, symbol="BTC%2FUSD").trades_csv
    assert load(MINIMAL, symbol="..").trades_csv == "logs/%2E%2E/trades.csv"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
