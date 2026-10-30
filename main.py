#!/usr/bin/env python3
"""entropy-arb entry point.

    # collect minute data only — no strategy, no credentials needed
    python3 main.py --record-only --symbol LIT

    # LIVE trading: real orders, real money (needs .env credentials)
    python3 main.py --symbol LIT

--symbol is required. --primary (or PRIMARY_VENUE in .env) defaults to
light-rh (RH); --hedge defaults to lighter mainnet. Add --cn for a Chinese
dashboard. There is no paper mode. Collect data with --record-only, set
your thresholds with tools/analyze.py, then go live with small position
caps.

On a terminal the bot shows a live Rich dashboard (books, signal, positions,
PnL, last executions) and writes log lines to logging.file; use
--no-dashboard for plain console logs (nohup/systemd). Strategy lives in
config.yaml, credentials in .env — see the README (English) /
README.zh-CN.md (中文).
"""
import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from entropy_arb.config import HEDGE_VENUES, PRIMARY_VENUES, ConfigError, load_config
from entropy_arb.engine import Engine


def setup_logging(level: str, log_file: str = None,
                  extra_handler: logging.Handler = None,
                  console: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    if log_file:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        h = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        h.setFormatter(fmt)
        root.addHandler(h)
    if console or not log_file:
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        root.addHandler(h)
    if extra_handler is not None:
        root.addHandler(extra_handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def run_event_loop(coro, use_uvloop: bool = True):
    """Run the bot's coroutine on uvloop when it is installed (a faster
    drop-in asyncio event loop), otherwise on the standard asyncio loop."""
    if use_uvloop:
        try:
            import uvloop
        except ImportError:  # optional dependency
            uvloop = None
        if uvloop is not None:
            if hasattr(uvloop, "run"):
                return uvloop.run(coro)
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    return asyncio.run(coro)


async def amain(cfg, record_only: bool, use_dashboard: bool, force_tty: bool,
                log_buffer, lang: str, mode: str = "taker") -> None:
    loop = asyncio.get_running_loop()
    logging.getLogger("main").info(
        "event loop: %s | mode: %s",
        "uvloop" if type(loop).__module__.startswith("uvloop") else "asyncio",
        mode)
    if mode == "maker":
        from entropy_arb.maker import MakerEngine
        eng = MakerEngine(cfg, record_only=record_only)
    else:
        eng = Engine(cfg, record_only=record_only)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.request_stop)
    if not use_dashboard:
        await eng.run()
        return
    from entropy_arb.dashboard import Dashboard
    dash = Dashboard(eng, log_buffer, cfg.log_file, force_terminal=force_tty,
                     lang=lang)
    dash_task = asyncio.create_task(dash.run(), name="dashboard")
    try:
        await eng.run()
    finally:
        eng.request_stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dash_task, timeout=5)
        if not dash_task.done():
            dash_task.cancel()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage with a selectable primary venue. "
                    "Without --record-only, "
                    "real orders are sent.")
    p.add_argument("--symbol", required=True,
                   help="symbol traded on both venues, e.g. SNDK / "
                        "两个交易所共同交易的品种")
    p.add_argument("--primary", choices=PRIMARY_VENUES, metavar="VENUE",
                   help="primary venue (default: PRIMARY_VENUE in .env, or light-rh / RH)")
    p.add_argument("--hedge", default="lighter", choices=HEDGE_VENUES,
                   metavar="VENUE",
                   help=f"hedge venue, one of: {', '.join(HEDGE_VENUES)} / "
                        f"对冲腿，默认 lighter 主网")
    p.add_argument("--config", default="config.yaml",
                   help="strategy config (default: config.yaml)")
    p.add_argument("--env-file", default=".env",
                   help="credentials file (default: .env)")
    p.add_argument("--record-only", action="store_true",
                   help="only collect minute data, run no strategy, send no "
                        "orders (needs no credentials)")
    p.add_argument("--cn", action="store_true",
                   help="display the dashboard in Chinese / 仪表盘使用中文")
    disp = p.add_mutually_exclusive_group()
    disp.add_argument("--dashboard", action="store_true",
                      help="force the Rich dashboard even without a tty")
    disp.add_argument("--no-dashboard", action="store_true",
                      help="plain console logs instead of the dashboard")
    p.add_argument("--mode", choices=("taker", "maker"), default="taker",
                   help="taker: cross both books on a premium signal "
                        "(default); maker: post-only quotes on the primary, "
                        "hedged on the hedge venue / maker: 在主腿挂单，"
                        "成交后在对冲腿吃单对冲")
    p.add_argument("--no-uvloop", action="store_true",
                   help="use the standard asyncio event loop even when "
                        "uvloop is installed / 不使用 uvloop")
    args = p.parse_args()

    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, hedge_venue=args.hedge,
                          primary_venue=args.primary)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    use_dashboard = (cfg.dashboard or args.dashboard) and not args.no_dashboard
    if args.mode == "maker" and use_dashboard:
        # the dashboard renders the taker strategy's state
        print("maker mode: dashboard off, plain logs / 挂单模式不显示仪表盘",
              file=sys.stderr)
        use_dashboard = False
    force_tty = args.dashboard
    if use_dashboard and not (sys.stdout.isatty() or force_tty):
        use_dashboard = False

    log_buffer = None
    if use_dashboard:
        try:
            from entropy_arb.dashboard import BufferLogHandler
        except ImportError:
            print("`rich` is not installed — falling back to plain logs "
                  "(pip install -r requirements.txt)", file=sys.stderr)
            use_dashboard = False
    if use_dashboard:
        log_buffer = BufferLogHandler()
        setup_logging(cfg.log_level, log_file=cfg.log_file,
                      extra_handler=log_buffer)
    else:
        # Keep a durable file log while retaining the plain terminal output
        # expected from --no-dashboard, nohup and other non-TTY runs.
        setup_logging(cfg.log_level, log_file=cfg.log_file, console=True)

    try:
        run_event_loop(amain(cfg, record_only=args.record_only,
                             use_dashboard=use_dashboard, force_tty=force_tty,
                             log_buffer=log_buffer,
                             lang="zh" if args.cn else "en",
                             mode=args.mode),
                       use_uvloop=not args.no_uvloop)
    except RuntimeError as e:
        # startup failures (missing credentials, market not found, venue
        # unreachable) — a clean message, not a traceback
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
