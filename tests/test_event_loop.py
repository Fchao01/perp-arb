"""main.run_event_loop must work with and without uvloop.

Run:  python3 -m pytest tests/test_event_loop.py
"""
import asyncio
import importlib.util
import os
import warnings


def _load_main():
    path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    spec = importlib.util.spec_from_file_location("ea_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _which_loop():
    await asyncio.sleep(0)
    return type(asyncio.get_running_loop()).__module__


def test_run_event_loop_without_uvloop():
    main = _load_main()
    assert not main.run_event_loop(_which_loop(),
                                   use_uvloop=False).startswith("uvloop")


def test_run_event_loop_with_uvloop_if_installed():
    main = _load_main()
    module = main.run_event_loop(_which_loop(), use_uvloop=True)
    try:
        import uvloop  # noqa: F401
        assert module.startswith("uvloop")
    except ImportError:
        assert module.startswith("asyncio")
    # uvloop.run() never touches the global policy; only the fallback for
    # uvloop releases without run() installs one, so undo that if it did
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        if type(asyncio.get_event_loop_policy()).__module__.startswith("uvloop"):
            asyncio.set_event_loop_policy(None)
