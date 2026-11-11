"""tools/set_target.py: bulk-set the target position in config files.

Run:  python3 -m pytest tests/test_set_target.py
"""
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import set_target  # noqa: E402

BTC = """# BTC
thresholds:
  midline_bps: auto
  upper_bps: 0.75
  lower_bps: 0.75
primary:
  max_position_usd: 12000
hedge:
  max_position_usd: 10000   # hedge leg
sizing:
  max_order_notional_usd: 500
  persist_tiers:
    - [0, 400]
    - [1.0, 1500]
inventory:
  scale_bps: 0      # ladder off
  floor_frac: 0
  # a comment that stays

execution:
  premium_persist_sec: 0
"""


def write(text, name="btc.yaml"):
    path = os.path.join(tempfile.mkdtemp(), name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def test_defaults_come_from_the_cap_and_the_largest_order():
    assert set_target.caps_and_order_size(yaml.safe_load(BTC)) == (10000.0, 1500.0)
    # anywhere between 0 and the cap, on the chosen side
    assert set_target.default_target(10000, 0, "long") == (5000.0, 5000.0)
    assert set_target.default_target(10000, 0, "short") == (-5000.0, 5000.0)
    # with a floor of 2000: 2000 .. 10000
    assert set_target.default_target(10000, 2000, "long") == (6000.0, 4000.0)


def test_apply_changes_only_the_two_keys_and_keeps_comments():
    path = write(BTC)
    assert set_target.main(["--side", "long", "--apply", path]) == 0
    with open(path) as fh:
        text = fh.read()
    cfg = yaml.safe_load(text)
    assert cfg["inventory"] == {"scale_bps": 0, "floor_frac": 0,
                                "target_primary_usd": 5000,
                                "target_band_usd": 5000}
    assert "# ladder off" in text and "# a comment that stays" in text
    assert "# hedge leg" in text
    want = yaml.safe_load(BTC)
    want["inventory"] = cfg["inventory"]
    assert cfg == want
    assert load(path + ".bak") == yaml.safe_load(BTC)
    # running it again replaces the values instead of adding lines
    assert set_target.main(["--side", "short", "--apply", path]) == 0
    with open(path) as fh:
        text = fh.read()
    assert text.count("target_primary_usd") == 1
    assert load(path)["inventory"]["target_primary_usd"] == -5000
    set_target.main(["--floor", "2000", "--apply", path])
    assert load(path)["inventory"]["target_primary_usd"] == 6000
    assert load(path)["inventory"]["target_band_usd"] == 4000


def test_preview_writes_nothing():
    path = write(BTC)
    assert set_target.main([path]) == 0
    with open(path) as fh:
        assert fh.read() == BTC
    assert not os.path.exists(path + ".bak")


def test_explicit_numbers_and_off():
    path = write(BTC)
    set_target.main(["--target", "8000", "--band", "4000", "--apply", path])
    assert load(path)["inventory"]["target_primary_usd"] == 8000
    assert load(path)["inventory"]["target_band_usd"] == 4000
    set_target.main(["--off", "--apply", path])
    assert load(path) == yaml.safe_load(BTC)


def test_file_without_an_inventory_section():
    text = "thresholds:\n  upper_bps: 1\n  lower_bps: 1\n"
    path = write(text)
    set_target.main(["--apply", path])
    cfg = load(path)
    assert cfg["thresholds"] == {"upper_bps": 1, "lower_bps": 1}
    assert cfg["inventory"] == {"target_primary_usd": 500,
                                "target_band_usd": 500}   # default cap 1000


def test_skips_close_only_markets_and_unsafe_values(capsys):
    closing = BTC.replace("max_position_usd: 12000", "max_position_usd: 0")
    path = write(closing)
    assert set_target.main(["--apply", path]) == 0
    assert load(path) == yaml.safe_load(closing)
    path = write(BTC)
    set_target.main(["--target", "20000", "--apply", path])    # above the cap
    set_target.main(["--target", "8000", "--band", "9000", "--apply", path])
    assert load(path) == yaml.safe_load(BTC)
    out = capsys.readouterr().out
    assert out.count("skipped") == 3


def test_result_loads_in_the_bot():
    from entropy_arb.config import load_config
    path = write(BTC)
    set_target.main(["--apply", path])
    cfg = load_config(path, os.path.join(tempfile.gettempdir(), "none.env"),
                      symbol="BTC", hedge_venue="lighter",
                      primary_venue="light-rh")
    assert cfg.inventory_target_usd == 5000.0
    assert cfg.inventory_target_band_usd == 5000.0
