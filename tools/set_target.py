#!/usr/bin/env python3
"""Set inventory.target_primary_usd / target_band_usd in many config files
at once (the position the bot holds on the primary venue; see
config.example.yaml).

By default the position may be anywhere between --floor (0) and the file's
position cap (the smaller max_position_usd of the two legs) on the side
given by --side: long = the primary (RH) stays long, short = it stays
short. The bot turns over exactly as before, it just never crosses to the
other side. (In the file this is target = the middle of that range, band =
half of it.) Files with a position cap of 0 (close-only markets) are left
alone. Only the two target lines change; comments and every other setting
stay as they are, and each changed file is first copied to <name>.bak.

    # show what would change, write nothing
    python3 tools/set_target.py --side long *.yaml
    # write it
    python3 tools/set_target.py --side long --apply *.yaml
    # this week's equities go the other way
    python3 tools/set_target.py --side short --apply nvda.yaml mu.yaml
    # explicit numbers for one file / back to turning over around flat
    python3 tools/set_target.py --target 8000 --band 4000 --apply btc.yaml
    python3 tools/set_target.py --off --apply *.yaml

A running bot reads its config only at start: restart each market whose
file changed.

中文：一次修改多个配置文件里的目标仓位。默认让仓位保持在 --floor（默认 0）
到该文件仓位上限之间，方向由 --side 决定：long = 主腿（RH）始终做多，short =
始终做空。机器人照常来回周转，只是不会跑到反方向。（写进文件的是：目标 =
这个范围的中点，摆幅 = 范围的一半。）仓位上限为 0 的"只平不开"市场不动。
只改这两行，其他设置和注释不变，改之前先备份成 <文件名>.bak。
不加 --apply 只显示将要修改的内容。改完需要重启对应市场才生效。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from typing import List, Optional, Tuple

import yaml

KEYS = ("target_primary_usd", "target_band_usd")


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def caps_and_order_size(raw: dict) -> Tuple[float, float]:
    """(position cap, largest order size) of a parsed config: the smaller
    max_position_usd of the two legs, and the largest notional one order
    can have (max_order_notional_usd or the largest persist tier)."""
    primary = dict(raw.get("entropy") or {})
    primary.update(raw.get("primary") or {})
    hedge = raw.get("hedge") or {}
    cap = min(float(primary.get("max_position_usd", 1000.0)),
              float(hedge.get("max_position_usd", 1000.0)))
    sizing = raw.get("sizing") or {}
    order = float(sizing.get("max_order_notional_usd", 500.0))
    tiers = sizing.get("persist_tiers") or []
    if tiers:
        order = max(float(usd) for _, usd in tiers)
    return cap, order


def default_target(cap: float, floor: float, side: str) -> Tuple[float, float]:
    """(target, band) that keeps the position between floor and cap on the
    given side: the middle of that range and half its width."""
    mid, half = (cap + floor) / 2.0, (cap - floor) / 2.0
    return (mid if side == "long" else -mid), half


def set_keys(text: str, target: Optional[float], band: Optional[float]) -> str:
    """Return the YAML text with the two inventory keys set (or removed when
    both are None). Line-based on purpose: comments and layout survive."""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines)
                  if re.match(r"^inventory\s*:\s*(#.*)?$", ln)), None)
    values = [] if target is None else [("target_primary_usd", target),
                                        ("target_band_usd", band)]
    if start is None:
        if not values:
            return text
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("inventory:")
        lines += [f"  {k}: {_num(v)}" for k, v in values]
        lines.append("")
        return "\n".join(lines)
    end = start + 1
    while end < len(lines) and not re.match(r"^\S", lines[end]):
        end += 1
    block = lines[start + 1:end]
    indent = "  "
    for ln in block:
        m = re.match(r"^(\s+)[A-Za-z_]", ln)
        if m:
            indent = m.group(1)
            break
    key_re = re.compile(r"^\s+(%s)\s*:" % "|".join(KEYS))
    block = [ln for ln in block if not key_re.match(ln)]
    # insert after the last real setting, before trailing blanks/comments
    at = len(block)
    while at > 0 and (block[at - 1].strip() == ""
                      or block[at - 1].lstrip().startswith("#")):
        at -= 1
    block[at:at] = [f"{indent}{k}: {_num(v)}" for k, v in values]
    return "\n".join(lines[:start + 1] + block + lines[end:])


def plan_file(path: str, args) -> dict:
    """What to do with one file: {"action": "set"|"off"|"skip", ...}."""
    with open(path) as fh:
        text = fh.read()
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("not a config file (no settings at the top level)")
    inv = raw.get("inventory") or {}
    old = (inv.get("target_primary_usd"), inv.get("target_band_usd"))
    cap, order = caps_and_order_size(raw)
    out = {"path": path, "text": text, "raw": raw, "old": old, "cap": cap,
           "order": order}
    if args.off:
        if old == (None, None):
            return dict(out, action="skip", why="no target set / 没有设置目标")
        return dict(out, action="off", target=None, band=None)
    if cap <= 0:
        return dict(out, action="skip",
                    why="position cap is 0 (close-only) / 仓位上限为 0，只平不开")
    if args.target is not None:
        target = abs(args.target) if args.side == "long" else -abs(args.target)
        band = args.band if args.band is not None else min(
            abs(target), cap - abs(target))
    else:
        if args.floor >= cap:
            return dict(out, action="skip",
                        why=f"floor {_num(args.floor)} is not below the "
                            f"position cap {_num(cap)} / 下限不低于仓位上限")
        target, band = default_target(cap, args.floor, args.side)
        if args.band is not None:
            band = args.band
    if abs(target) > cap:
        return dict(out, action="skip",
                    why=f"target {_num(abs(target))} is above the position cap "
                        f"{_num(cap)} / 目标超过仓位上限")
    if band > abs(target):
        return dict(out, action="skip",
                    why=f"band {_num(band)} is larger than the target "
                        f"{_num(abs(target))}: the position could cross to "
                        f"the other side / 摆幅大于目标，仓位可能跑到反方向")
    if band < order:
        out["note"] = (f"the position can only move {_num(2 * band)} in "
                       f"total, orders are up to {_num(order)}: expect "
                       f"little volume / 可摆动的空间比单笔金额大不了多少，"
                       f"成交量会很少")
    return dict(out, action="set", target=target, band=band)


def new_text(p: dict) -> str:
    text = set_keys(p["text"], p.get("target"), p.get("band"))
    # the edit must change the two keys and nothing else
    before = dict(p["raw"])
    after = yaml.safe_load(text) or {}
    inv_b = dict(before.get("inventory") or {})
    inv_a = dict(after.get("inventory") or {})
    for k in KEYS:
        inv_b.pop(k, None)
        got = inv_a.pop(k, None)
        want = {"target_primary_usd": p.get("target"),
                "target_band_usd": p.get("band")}[k]
        if (got is None) != (want is None) or (
                got is not None and abs(float(got) - float(want)) > 1e-9):
            raise RuntimeError(f"edit check failed for {k}")
    before = {k: v for k, v in before.items() if k != "inventory"}
    after = {k: v for k, v in after.items() if k != "inventory"}
    if before != after or inv_b != inv_a:
        raise RuntimeError("edit check failed: other settings would change")
    return text


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Set the target position in many config files at once / "
                    "一次修改多个配置文件的目标仓位")
    ap.add_argument("files", nargs="+", help="config files, e.g. *.yaml")
    ap.add_argument("--side", choices=("long", "short"), default="long",
                    help="side to hold on the primary (RH) / 主腿持仓方向")
    ap.add_argument("--floor", type=float, default=0.0,
                    help="smallest position to keep, USD (default 0) / "
                         "最少保留的仓位，默认 0")
    ap.add_argument("--target", type=float, default=None,
                    help="explicit target in USD for every file / 手动指定目标")
    ap.add_argument("--band", type=float, default=None,
                    help="explicit band in USD for every file / 手动指定摆幅")
    ap.add_argument("--off", action="store_true",
                    help="remove the target (turn over around flat) / 去掉目标")
    ap.add_argument("--apply", action="store_true",
                    help="write the files (default: only show) / 写入文件")
    args = ap.parse_args(argv)
    if args.target is not None and args.target <= 0:
        ap.error("--target must be > 0; use --side short for a short target")
    if args.band is not None and args.band < 0:
        ap.error("--band must be >= 0")
    if args.floor < 0:
        ap.error("--floor must be >= 0")

    changed = failed = 0
    for path in args.files:
        if path.endswith(".bak") or "example" in path.rsplit("/", 1)[-1]:
            continue
        try:
            p = plan_file(path, args)
            if p["action"] == "skip":
                print(f"{path}: skipped — {p['why']}")
                continue
            text = new_text(p)
            old_t, old_b = p["old"]
            was = ("none" if old_t is None
                   else f"{_num(float(old_t)):>7} / {_num(float(old_b or 0))}")
            now = ("none" if p["action"] == "off"
                   else f"{_num(p['target']):>7} / {_num(p['band'])}")
            if text == p["text"]:
                print(f"{path}: already {now.strip()}")
                continue
            lo_hi = ""
            if p["action"] == "set":
                t, b = p["target"], p["band"]
                lo, hi = max(-p["cap"], t - b), min(p["cap"], t + b)
                lo_hi = f"  -> position ${_num(lo)} .. ${_num(hi)}"
            print(f"{path}: target/band {was.strip()} -> {now.strip()}{lo_hi}"
                  f"  (cap {_num(p['cap'])}, order {_num(p['order'])})")
            if p.get("note"):
                print(f"    note: {p['note']}")
            if args.apply:
                shutil.copy2(path, path + ".bak")
                with open(path, "w") as fh:
                    fh.write(text)
            changed += 1
        except Exception as e:  # one bad file must not stop the rest
            failed += 1
            print(f"{path}: FAILED, left unchanged — {e}")
    if args.apply:
        print(f"\n{changed} file(s) written (backups: <name>.bak). Restart "
              f"those markets. / 已写入 {changed} 个文件，重启对应市场后生效。")
    else:
        print(f"\n{changed} file(s) would change. Nothing written — add "
              f"--apply. / 以上仅为预览，加 --apply 才会写入。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
