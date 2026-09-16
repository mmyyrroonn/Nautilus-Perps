#!/usr/bin/env python3
"""
Acceptance tests for ``src/ondo_probe.py`` (Ondo Perps probe, stage R4).

These tests are written against the **frozen contract**, not against the implementation.
Where the two disagree the test is supposed to go red: that is the finding. Nothing here
is relaxed to make a module pass.

Everything is offline and credential-free. The adapter, the node factory, the process
environment and the clock are all injected through the five keyword seams of ``main`` -
no test mutates the real process environment, opens a socket, or reads the repository's
``.env`` (which exists and is gitignored; it is never opened, not even to prove it holds
no ``ONDO_*`` key - absence is constructed instead).

Two consequences worth stating up front, because they are the contract and not a quirk of
this file:

* a **refusal is a startup rejection**, not a failure and not a pass. It returns
  ``EXIT_REFUSED`` before a client exists, and it must never be written up as "the probe
  ran but returned no data";
* ``protocol_verified`` and ``exit_code_zero_means_clean_account`` are **false by
  construction** in R4 - no request has ever reached the real venue, and the adapter's
  converging stop is unreachable from Python (``OndoAccountRuntime::stop_and_wait`` is
  called only from ``crates/adapters/ondo/tests/private_runtime.rs``).

The last section persists its stability evidence as JSON rather than printing it (R3
acceptance report, section 7). It lands in ``tmp_path`` by default; point
``ONDO_PROBE_STABILITY_OUT`` at the run directory to keep it with a real acceptance run.

    .venv\\Scripts\\python.exe -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import enum
import hashlib
import importlib.util
import itertools
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ondo_preflight  # noqa: E402  (the run-id/staging vocabulary the probe mirrors)
import ondo_probe  # noqa: E402


# --------------------------------------------------------------- frozen contract values

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2
EXIT_TIMEOUT = 3

SANDBOX_AUTHORITY = "api.ondoperps-sandbox.xyz"
SANDBOX_REST_URL = f"https://{SANDBOX_AUTHORITY}"

# The eleven flags, exactly. A twelfth flag is as much a deviation as a missing one.
CONTRACT_FLAGS = frozenset({
    "--mode", "--symbols", "--minutes", "--out", "--log-level", "--dry-run",
    "--instrument", "--notional-usd", "--max-orders", "--max-exposure-usd",
    "--allow-sandbox-orders",
})

# Every top-level group ``probe.json`` must expose, each separately readable.
CONTRACT_GROUPS = (
    "submitted", "acked", "filled", "partial", "canceled", "unknown", "no_trade",
    "pending_ids", "account_reconciled", "synthetic", "protocol_verified",
    "exit_code_zero_means_clean_account", "mode", "environment", "read_capable",
    "write_capable", "stop_condition", "caps", "converging_stop_available",
    "outstanding_orders",
)

# Groups that count events. Each is a collection a reader can inspect on its own; none of
# them may be folded into a single total ("submitted" is not "acked", "partial" is not
# "filled", "unknown" is not "no_trade").
COUNT_GROUPS = (
    "submitted", "acked", "filled", "partial", "canceled", "unknown", "no_trade",
    "pending_ids", "outstanding_orders",
)

# The verdicts that must not move between identical runs.
FROZEN_VERDICTS = (
    "mode", "environment", "read_capable", "write_capable", "protocol_verified",
    "exit_code_zero_means_clean_account", "converging_stop_available",
)

# A canonical token per cap, so a row can be found whatever the JSON spells it. The
# contract fixes the *constants* and the clamp; it does not fix the report's spelling.
CAP_TOKENS = ("notional", "exposure", "orders", "minutes")

# Order-submitting calls, by name. Anything here is a write request.
WRITE_METHODS = frozenset({
    "submit_order", "submit_orders", "submit_order_list", "cancel_order", "cancel_orders",
    "cancel_all_orders", "modify_order", "modify_orders", "close_position",
    "close_all_positions", "submit_quote", "modify_quote", "delete_order",
})

CREDENTIAL_VARS = (
    "ONDO_SANDBOX_API_KEY", "ONDO_SANDBOX_API_SECRET", "ONDO_SANDBOX_ACCOUNT_ID",
)

# Sentinels, not real credentials. If one of these shows up in a stream or a report, the
# assertion is about the leak, not about the value.
SECRET_SENTINELS = {
    "ONDO_SANDBOX_API_KEY": "SENTINEL-API-KEY-2f4c9a",
    "ONDO_SANDBOX_API_SECRET": "SENTINEL-API-SECRET-8b1d7e",
    "ONDO_SANDBOX_ACCOUNT_ID": "SENTINEL-ACCOUNT-3a6f20",
}

DEFAULT_SYMBOLS = "NVDA,TSLA"
ONDO_ID = "NVDA-USD-PERP.ONDO"


# ------------------------------------------------------------------------- test doubles


class Calls:
    """Records every method a fake client or node receives, in order.

    Recording happens at *call* time, not inside the coroutine body, so an unwrapped
    coroutine still leaves evidence. ``aio`` mirrors the real runtime shape (the adapter's
    methods are coroutines) while ``aio=False`` covers a stub that prints them as plain
    ``def`` - the probe must survive both.
    """

    def __init__(self, *, fail=None, answers=None, aio=True) -> None:
        self._fail = fail or (lambda name: None)
        self._answers = dict(answers or {})
        self._aio = aio
        self.calls: list[tuple[str, tuple, dict]] = []

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    @property
    def writes(self) -> list[str]:
        return [name for name in self.names if name in WRITE_METHODS]

    def matching(self, *needles: str) -> list[str]:
        """Call names containing any of ``needles``, case-insensitively."""
        return [n for n in self.names if any(x in n.lower() for x in needles)]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            failure = self._fail(name)
            if failure is not None:
                if self._aio:
                    async def boom():
                        raise failure
                    return boom()
                raise failure
            value = self._answers.get(name)
            if self._aio:
                async def done():
                    return value
                return done()
            return value

        return record


def redact(value):
    """Keep a sentinel out of a repr: a failing assertion must not print a credential."""
    return "<redacted>" if value in SECRET_SENTINELS.values() else value


class Built:
    """One construction: which class, with which arguments."""

    def __init__(self, name: str, args: tuple, kwargs: dict) -> None:
        self.name = name
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        rendered = {k: redact(v) for k, v in sorted(self.kwargs.items())}
        return f"Built({self.name}, args={len(self.args)}, kwargs={rendered})"


class FakeBuilder:
    """The framework node builder the module configures, with every call recorded.

    Its methods return the builder: the module chains ``with_logging(...)``,
    ``with_risk_engine_config(...)``, ``with_timeout_connection(...)`` and the client
    registrations into one expression, so a double that returned ``None`` would break the
    chain rather than record it.
    """

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.calls: list[tuple[str, tuple, dict]] = []

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return record


def fake_live_node(registry: Registry):
    """A stand-in for ``nautilus_trader.live.LiveNode``.

    ``build_node`` configures a *real* ``LiveNode.builder(...)``, and that builder extracts
    the factories it is handed against the real Rust classes - an injected fake adapter can
    never satisfy it. Patching the framework symbol keeps everything the module itself does
    under test (registration by mode, the data/exec split, the plan gate, the banner) while
    removing a check that is about the framework's own objects rather than this contract.
    Only the symbol is replaced; ``node_factory`` is still the seam that produces the node.
    """

    class FakeLiveNode:
        @staticmethod
        def builder(*args, **kwargs):
            registry.builder_args.append((args, kwargs))
            return FakeBuilder(registry)

    return FakeLiveNode


class Registry:
    """Every class the probe asked the adapter for, and every instance it built."""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.built: list[Built] = []
        self.nodes: list[object] = []
        self.builder_args: list[tuple[tuple, dict]] = []

    @property
    def built_names(self) -> list[str]:
        return [item.name for item in self.built]

    def named(self, *needles: str) -> list[Built]:
        low = [n.lower() for n in needles]
        return [b for b in self.built if any(x in b.name.lower() for x in low)]

    @property
    def all_writes(self) -> list[str]:
        writes: list[str] = []
        for node in self.nodes:
            writes.extend(node.writes)
        return writes

    @property
    def executor_calls(self) -> list[tuple[str, tuple, dict]]:
        """Every call made on an adapter class other than a config: the client surface."""
        calls: list[tuple[str, tuple, dict]] = []
        for built in self.built:
            if is_config(built.name):
                continue
            calls.extend(getattr(built, "calls", []).calls)
        return calls


def _environment_enum(name: str):
    """An enum-like stand-in: the probe compares members, it does not build them.

    Both spellings are present because the real enum's case is not part of the contract;
    equal values make the second spelling an alias, so ``SANDBOX is Sandbox``.
    """
    return enum.Enum(name, {
        "Production": "production", "PRODUCTION": "production",
        "Sandbox": "sandbox", "SANDBOX": "sandbox",
        "Testnet": "testnet", "TESTNET": "testnet",
        "Development": "development", "DEVELOPMENT": "development",
    })


class FakeAdapter:
    """What ``ondo_probe`` loads from ``nautilus_trader.adapters.ondo``.

    Any attribute the probe asks for is generated on demand and records its construction,
    so the fake never has to guess the adapter's surface. Classes whose name mentions
    *Environment* come back as an enum, because the probe compares members rather than
    instantiating them.
    """

    def __init__(self, registry: Registry, *, aio=True, fail=None, answers=None,
                 config_base_url=None) -> None:
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_aio", aio)
        object.__setattr__(self, "_fail", fail)
        object.__setattr__(self, "_answers", answers)
        object.__setattr__(self, "_config_base_url", config_base_url)
        object.__setattr__(self, "_cache", {})

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        cache = self._cache
        if name not in cache:
            self._registry.requested.append(name)
            if "Environment" in name:
                cache[name] = _environment_enum(name)
            else:
                cache[name] = self._class_for(name)
        return cache[name]

    def _class_for(self, name: str):
        registry, aio = self._registry, self._aio
        fail, answers = self._fail, self._answers
        config_base_url = self._config_base_url
        is_config = "Config" in name

        class Generated:
            """A generated adapter class: records construction, then records calls.

            A *config* is a value, not a client: reading an attribute it does not carry
            yields ``None`` rather than a callable, because the module probes configs with
            ``getattr(obj, name, None)`` - a fake that answered every question with a
            function would make the module's own "no credential on this object" guard fire.
            Every other class is a client or a factory, and its methods are recorded.
            """

            def __init__(self, *args, **kwargs):
                registry.built.append(Built(name, args, dict(kwargs)))
                self.calls = Calls(fail=fail, answers=answers, aio=aio)
                # The real execution config takes api_key/api_secret as ctor kwargs and
                # exposes no attribute for them; the fake mirrors that, so a config repr
                # cannot hand a secret to a report either.
                for key, value in kwargs.items():
                    if key.lower() in {"api_key", "api_secret", "key", "secret"}:
                        continue
                    try:
                        setattr(self, key, value)
                    except AttributeError:
                        pass
                # The real config resolves its own endpoint from its environment; an
                # injected one is how a test asks "what if the client dialled elsewhere?".
                if is_config and config_base_url is not None:
                    self.base_url_http = config_base_url

            def __getattr__(self, attr):
                if attr.startswith("_"):
                    raise AttributeError(attr)
                if is_config:
                    return None
                return getattr(self.calls, attr)

            def name(self) -> str:
                """A factory's name is its venue, and the node builder calls it for a string.

                Recording it would hand the builder a coroutine instead of the venue name,
                which is how this fake first failed.
                """
                return "ONDO" if "Factory" in name else f"<{name} (fake)>"

            def __repr__(self) -> str:
                return f"<{name} (fake)>"

        Generated.__name__ = name
        Generated.__qualname__ = name
        return Generated


class FakeCache:
    """An empty framework cache: the run observed nothing, which is a fact to publish."""

    def orders(self, *_args, **_kwargs):
        return []

    def positions(self, *_args, **_kwargs):
        return []

    def account(self, *_args, **_kwargs):
        return None

    def instruments(self, *_args, **_kwargs):
        return []


class FakeHandle:
    """``LiveNodeHandle``: a ``stop`` method, ``is_running``/``is_stopping`` getters.

    The asymmetry is the framework's own (``crates/live/src/python/node.rs``): ``stop`` is a
    plain method and the two state reads are ``#[getter]``s, which is why a stop is delivered
    by *calling* the handle. ``stop`` signals the run and returns immediately - the run's
    shutdown then completes on its own thread - so this releases ``run`` without pretending
    the shutdown was synchronous.
    """

    def __init__(self, node: "FakeNode", *, stop_delay_off_thread: float = 0.0) -> None:
        self.node = node
        self.stop_calls = 0
        self.stop_delay_off_thread = stop_delay_off_thread
        self._stopping = threading.Event()

    def stop(self) -> None:
        self.stop_calls += 1
        # A schedule rather than a sleep for its own sake. The watchdog is the only stop that
        # does not come from the owning thread, and an off-thread stop that finishes *after*
        # the owning thread's final stop is exactly the ordering that used to decide whether
        # the report carried the watchdog entry at all.
        if self.stop_delay_off_thread and threading.current_thread() is not threading.main_thread():
            time.sleep(self.stop_delay_off_thread)
        self._stopping.set()
        self.node.release()

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    @property
    def is_running(self) -> bool:
        return self.node.is_running


class NoHandleNode:
    """A node with no ``handle`` attribute at all, for the fallback path.

    A real ``LiveNode`` always has one, so this shape exists only so the fallback cannot be
    deleted without a test going red - and so the reason it is safe becomes checkable: a
    double is not a PyO3 object, so reading it from another thread trips nothing.
    """

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1

    @property
    def is_running(self) -> bool:
        return False


class FakeNode:
    """A node the probe may build, register clients on, run and stop.

    ``run`` returns at once, which is the shape of a session that ends on its own terms -
    the alternative (blocking until the watchdog fires) would make every bounded run take
    its whole deadline. ``is_running`` is a real ``False`` so the bounded stop does not
    have to poll for a value the fake cannot produce.

    ``failure`` is raised from ``run``: a real ``node.run()`` blocks for the length of the
    session, so it is the one call a transport timeout, a rejected login or an operator
    interrupt reaches the module through.

    ``handle`` is a *method* here because it is a method on the real ``LiveNode`` - see the
    note there. ``with_handle=False`` is the node whose ``handle()`` yields nothing, which is
    how ``resolve_stop_target`` is meant to fall back rather than refuse to stop.
    """

    def __init__(self, registry: Registry, *, failure: BaseException | None = None,
                 blocking: bool = False, with_handle: bool = True,
                 handle_stop_delay: float = 0.0) -> None:
        self.registry = registry
        self.failure = failure
        self.blocking = blocking
        self.with_handle = with_handle
        self.cache = FakeCache()
        self.stop_calls = 0
        self.the_handle = (
            FakeHandle(self, stop_delay_off_thread=handle_stop_delay) if with_handle else None
        )
        self._release = threading.Event()
        self._running = bool(blocking)
        self.calls: list[tuple[str, tuple, dict]] = []
        registry.nodes.append(self)

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    @property
    def writes(self) -> list[str]:
        return [name for name in self.names if name in WRITE_METHODS]

    @property
    def is_running(self) -> bool:
        """True while a blocking ``run`` is in flight, so a stop has something to release."""
        return self._running

    def handle(self):
        """The framework's control handle - a *method*, as on the real node.

        ``cache``, ``portfolio`` and ``is_running`` on ``PyLiveNode`` carry ``#[getter]``;
        ``handle`` does not (``crates/live/src/python/node.rs``). A double that exposed the
        handle as an attribute would test a shape the real node does not have, and would let
        a ``resolve_stop_target`` that only accepted attributes pass here while falling back
        to the node - the panic - in production.
        """
        self.calls.append(("handle", (), {}))
        return self.the_handle

    def run(self) -> None:
        self.calls.append(("run", (), {}))
        if self.failure is not None:
            raise self.failure
        if self.blocking:
            # A real node.run() blocks for the length of the session, so this is the only
            # shape in which a deadline can end a run at all.
            self._release.wait(timeout=30.0)
        self._running = False

    def release(self) -> None:
        """End the hosted run: what a stop does to the run it is stopping, from any thread."""
        self._running = False
        self._release.set()

    def stop(self) -> None:
        self.calls.append(("stop", (), {}))
        self.stop_calls += 1
        self.release()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return record


class NodeFactory:
    """The ``node_factory`` seam: records how the node was asked for."""

    def __init__(self, registry: Registry, *, failure: BaseException | None = None,
                 blocking: bool = False, with_handle: bool = True) -> None:
        self.registry = registry
        self.failure = failure
        self.blocking = blocking
        self.with_handle = with_handle
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeNode(self.registry, failure=self.failure, blocking=self.blocking,
                        with_handle=self.with_handle)


class SlowWatchdogFactory:
    """Builds the node whose *off-thread* stop finishes last.

    The watchdog is the only stop that comes from a thread other than the owning one, so a
    handle that sleeps off-thread is the schedule in which the watchdog's own entry is
    appended to ``stops`` after the run has already finished - the ordering the report used
    to be built in the middle of.
    """

    def __init__(self, registry: Registry, *, delay: float) -> None:
        self.registry = registry
        self.delay = delay
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeNode(self.registry, handle_stop_delay=self.delay)


class FailingFactory:
    """A node factory that raises: nothing was built, so there is no stop target to resolve.

    A real session can fail this early (a venue that refuses the connection, a builder that
    rejects the config), and the report still has to account for the stop it did not perform.
    """

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise self.exc


ATTEMPTS = itertools.count()


class Clock:
    """A clock that cannot be outrun: every reading jumps past any sane deadline.

    ``main`` takes ``now`` as a seam precisely so a test never waits out ``--minutes``.
    Each invocation starts one second after the last, because run ids are minted from this
    clock and two attempts a real second apart must not collide.
    """

    def __init__(self, *, step_seconds: float = 600.0) -> None:
        base = datetime.datetime(2026, 9, 16, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self.moment = base + datetime.timedelta(seconds=next(ATTEMPTS))
        self.step = datetime.timedelta(seconds=step_seconds)
        self.readings = 0

    def __call__(self):
        self.readings += 1
        moment = self.moment
        self.moment = self.moment + self.step
        return moment


class Result:
    """One ``main`` invocation: its exit code, its streams, and what it built."""

    def __init__(self, code, out, err, log, registry, factory, clock) -> None:
        self.code = code
        self.out = out
        self.err = err
        self.log = log
        self.registry = registry
        self.factory = factory
        self.clock = clock

    @property
    def streams(self) -> str:
        return self.out + "\n" + self.err + "\n" + self.log

    @property
    def writes(self) -> list[str]:
        return self.registry.all_writes


class _LogCapture(logging.Handler):
    """Captures records through the logging system, not through ``sys.stderr``.

    A handler bound to a stream would miss anything a library logger emits after the
    redirect ends. The point of the secret-leak tests is that nothing escapes by any
    route, so records are collected where they are produced.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # a broken format must not hide the test's own failure
            self.lines.append(repr(record.msg))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@contextlib.contextmanager
def capture_logs():
    handler = _LogCapture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


# ------------------------------------------------------------------------------- run it


def clean_environ(**overrides) -> dict:
    """A process environment with every ``ONDO_*`` key removed.

    The repository's ``.env`` may hold real sandbox credentials and a runner may have
    exported them, so absence is *constructed* here rather than assumed.
    """
    environ = {k: v for k, v in os.environ.items() if not k.startswith("ONDO_")}
    environ.update(overrides)
    return environ


def run_probe(argv, *, environ=None, registry=None, adapter=None, factory=None,
              node_failure: BaseException | None = None, blocking_node: bool = False,
              with_handle: bool = True, **fake) -> Result:
    """Call ``main`` through all five seams and hand back everything it produced.

    ``node_failure`` is raised from the node's ``run()``: a real session blocks there, so
    that is where a transport timeout, a rejected login or an interrupt reaches the module.
    """
    registry = registry if registry is not None else Registry()
    adapter = adapter if adapter is not None else FakeAdapter(registry, **fake)
    factory = factory if factory is not None else NodeFactory(
        registry, failure=node_failure, blocking=blocking_node, with_handle=with_handle)
    clock = Clock()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with capture_logs() as handler:
            with patch.object(ondo_probe, "LiveNode", fake_live_node(registry)):
                code = ondo_probe.main(
                    list(argv),
                    adapter=adapter,
                    node_factory=factory,
                    environ=environ if environ is not None else clean_environ(),
                    now=clock,
                )
    return Result(code, out.getvalue(), err.getvalue(), handler.text, registry, factory, clock)


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def probe_report(out_dir: Path) -> dict:
    return read_json(Path(out_dir) / "probe.json")


def manifest(out_dir: Path) -> dict:
    return read_json(Path(out_dir) / "meta.json")


def credentials(**overrides) -> dict:
    values = dict(SECRET_SENTINELS)
    values.update(overrides)
    return values


def sandbox_argv(out_dir: Path, *dropped: str) -> list[str]:
    """A complete sandbox command; ``dropped`` names the bounds to leave out."""
    argv = [
        "--mode", "sandbox", "--allow-sandbox-orders", "--symbols", "NVDA",
        "--minutes", "0.5", "--out", str(out_dir),
    ]
    supplied = [
        ("--instrument", [ONDO_ID]),
        ("--notional-usd", ["10"]),
        ("--max-orders", ["2"]),
        ("--max-exposure-usd", ["25"]),
    ]
    for flag, values in supplied:
        if flag not in dropped:
            argv += [flag, *values]
    return argv


def test_the_sandbox_helper_builds_a_complete_command(tmp_path):
    """A guard on the fixture itself: a helper that silently drops a bound would make
    every 'missing bound' test pass for the wrong reason."""
    argv = sandbox_argv(tmp_path)
    assert set(argv) >= {"--mode", "--allow-sandbox-orders", "--instrument",
                         "--notional-usd", "--max-orders", "--max-exposure-usd"}
    for flag in ("--instrument", "--notional-usd", "--max-orders", "--max-exposure-usd"):
        assert flag not in sandbox_argv(tmp_path, flag), f"{flag} was not dropped"
        assert flag in sandbox_argv(tmp_path, *[f for f in
                                               ("--instrument", "--notional-usd",
                                                "--max-orders", "--max-exposure-usd")
                                               if f != flag])


# ------------------------------------------------------- reading the module's own code


def _source() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "ondo_probe.py").read_text(
        encoding="utf-8",
    )


def _code() -> str:
    """The module's code with docstrings removed (comments never reach the AST).

    The hygiene checks below are about what the module *does*. A docstring that names the
    thing it refuses to do - "the app never signs a REST call", "no POST retry" - must not
    trip them, and a comment must not be able to hide anything either.
    """
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)




# ================================================================== 1. the CLI surface


def test_the_default_command_is_the_contract_default():
    args = ondo_probe.parse_args([])
    assert args.mode == "public"
    assert args.symbols == DEFAULT_SYMBOLS
    assert float(args.minutes) == 2.0, "the default run is two minutes, not unlimited"
    assert args.out == "reports/ondo-probe"
    assert args.log_level == "INFO"
    assert args.dry_run is False
    assert args.allow_sandbox_orders is False
    assert not args.instrument, "there is no default order parameter"


def test_the_cli_carries_exactly_the_eleven_contract_flags():
    parser = getattr(ondo_probe, "build_parser", None)
    if parser is None:
        # No parser seam: read the flags out of the module's own argument declarations.
        flags = set(re.findall(r"""['"](--[a-z][a-z0-9-]*)['"]""", _source()))
    else:
        flags = {option for action in parser()._actions for option in action.option_strings
                 if option.startswith("--")}
    assert flags == CONTRACT_FLAGS, (
        f"missing={sorted(CONTRACT_FLAGS - flags)} extra={sorted(flags - CONTRACT_FLAGS)}"
    )


def test_the_mode_choices_are_exactly_the_four_modes():
    parser = getattr(ondo_probe, "build_parser", None)
    if parser is None:
        for mode in ("public", "account-readonly", "paper", "sandbox"):
            assert f'"{mode}"' in _source(), mode
        return
    action = next(a for a in parser()._actions if "--mode" in a.option_strings)
    assert set(action.choices) == {"public", "account-readonly", "paper", "sandbox"}


def test_the_four_exit_codes_are_the_contract():
    assert ondo_probe.EXIT_OK == EXIT_OK
    assert ondo_probe.EXIT_FAILURE == EXIT_FAILURE
    assert ondo_probe.EXIT_REFUSED == EXIT_REFUSED
    assert ondo_probe.EXIT_TIMEOUT == EXIT_TIMEOUT


def test_the_four_caps_are_the_contract_and_money_is_decimal():
    assert ondo_probe.MAX_NOTIONAL_PER_ORDER_USD_CAP == Decimal("50")
    assert ondo_probe.MAX_EXPOSURE_USD_CAP == Decimal("100")
    assert ondo_probe.MAX_ORDERS_CAP == 10
    assert ondo_probe.MAX_MINUTES_CAP == 60.0
    for name in ("MAX_NOTIONAL_PER_ORDER_USD_CAP", "MAX_EXPOSURE_USD_CAP"):
        assert isinstance(getattr(ondo_probe, name), Decimal), f"{name} is money"


def test_the_allowlist_names_the_real_sandbox_authority():
    assert SANDBOX_AUTHORITY in _source(), (
        "the sandbox allowlist must name the venue's sandbox authority"
    )


# ===================================================================== 2. the refusals
#
# Every refusal below is a startup rejection: EXIT_REFUSED, before any client exists, with
# zero write requests. A refusal is neither a failure nor a pass.


def is_config(name: str) -> bool:
    """A config object is a value, not a client: it carries no transport and sends nothing.

    The contract says a refusal lands "before any client is constructed"; resolving the
    plan legitimately reads the endpoint back off a config object first, so the assertion
    below is about clients and factories.
    """
    return "Config" in name


def assert_refused(result: Result, *, why: str) -> None:
    assert result.code == EXIT_REFUSED, (
        f"{why}: expected EXIT_REFUSED ({EXIT_REFUSED}), got {result.code}\n"
        f"stdout={result.out!r}\nstderr={result.err!r}"
    )
    clients = [built for built in result.registry.built if not is_config(built.name)]
    assert clients == [], (
        f"{why}: a refusal happens before any client is constructed, but these were "
        f"built: {clients}"
    )
    assert result.writes == [], f"{why}: a refusal issues no write requests"


@pytest.mark.parametrize("mode", ["public", "account-readonly", "paper"])
@pytest.mark.parametrize("flag,value", [
    ("--allow-sandbox-orders", None),
    ("--instrument", ONDO_ID),
    ("--notional-usd", "10"),
    ("--max-orders", "2"),
    ("--max-exposure-usd", "25"),
])
def test_a_sandbox_write_parameter_outside_sandbox_is_refused(tmp_path, mode, flag, value):
    argv = ["--mode", mode, "--out", str(tmp_path / "out"), "--dry-run", flag]
    if value is not None:
        argv.append(value)
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert_refused(result, why=f"{flag} in {mode}")
    assert not (tmp_path / "out").exists(), "a refusal publishes no report"


@pytest.mark.parametrize("flag,value", [
    ("--allow-sandbox-orders", None),
    ("--instrument", ONDO_ID),
    ("--notional-usd", "10"),
    ("--max-orders", "2"),
    ("--max-exposure-usd", "25"),
])
def test_a_production_mode_with_a_write_parameter_is_refused(tmp_path, flag, value):
    """A production mode combined with any write parameter: refused, whatever it is."""
    argv = ["--mode", "public", "--out", str(tmp_path / "out"), flag]
    if value is not None:
        argv.append(value)
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert_refused(result, why=f"production + {flag}")


def test_sandbox_without_the_allow_flag_is_refused(tmp_path):
    argv = ["--mode", "sandbox", "--out", str(tmp_path / "out"), "--symbols", "NVDA",
            "--instrument", ONDO_ID, "--notional-usd", "10", "--max-orders", "2",
            "--max-exposure-usd", "25"]
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert_refused(result, why="sandbox without --allow-sandbox-orders")


@pytest.mark.parametrize("missing", [
    "--instrument", "--notional-usd", "--max-orders", "--max-exposure-usd",
])
def test_sandbox_missing_any_one_bound_is_refused(tmp_path, missing):
    argv = sandbox_argv(tmp_path / "out", missing)
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert_refused(result, why=f"sandbox without {missing}")


NON_ALLOWLISTED_ENDPOINTS = (
    "https://api.ondoperps.xyz",                     # production, not sandbox
    "https://ondoperps.xyz",                         # the production apex
    "https://api.ondoperps-sandbox.xyz.evil.com",    # suffix attack
    "https://evil-api.ondoperps-sandbox.xyz",        # prefix attack
    "https://api.ondoperps-sandbox.xyz@evil.com",    # userinfo attack
    "https://API.ONDOPERPS-SANDBOX.XYZ.evil.com",    # case plus a suffix
    "http://api.ondoperps-sandbox.xyz",              # wrong scheme for the authority
    "https://api.ondoperps-sandbox.xyz:8443",        # non-default port
    "not-a-url",
    # The loopback family: the one authority where the adapter and this gate are meant to
    # differ. The adapter admits a loopback mock so its own offline tests can dial one; a
    # session aimed at a mock must never be reported as a sandbox session, so this gate is
    # strictly narrower. Written as escapes so the homoglyph is a different host by
    # inspection and not by eyesight.
    "http://127.0.0.1:8080",                         # a loopback address
    "https://[::1]:8443",                            # the loopback address, v6
    "http://localhost:8080",                         # a loopback *name*, which is not an address
    "https://аpi.ondoperps-sandbox.xyz",        # a homoglyph of the authority
    # Alternate spellings of the loopback address. A WHATWG parser folds all three onto
    # 127.0.0.1; ``urlsplit`` does not, so here they are simply authorities outside the
    # allowlist. The refusal holds under either reading - folded, they reach the loopback
    # branch, which refuses them too - so these pin the outcome without pinning the parser.
    "http://0.0.0.0:8080",                           # unspecified, not loopback
    "http://2130706433",                             # 127.0.0.1 as a decimal integer
    "http://127.1",                                  # 127.0.0.1 in short form
)


@pytest.mark.parametrize("url", NON_ALLOWLISTED_ENDPOINTS)
def test_the_allowlist_refuses_a_host_that_merely_contains_the_authority(url):
    """A positive allowlist of parsed scheme + normalized host - not a substring check.

    Most of the URLs above *contain* the authority as a substring, so a substring check
    would wave them through. Checked on the gate itself, so the refusal cannot be an
    accident of some other fault in the run.
    """
    refusal = ondo_probe.sandbox_endpoint_refusal(url)
    assert refusal, f"{url!r} was admitted as a sandbox endpoint"
    assert isinstance(refusal, str) and refusal.strip()


def test_the_allowlist_admits_the_authority_it_names_and_normalizes_it():
    """The positive direction: a guard that refused everything would pass the test above."""
    assert ondo_probe.sandbox_endpoint_refusal(SANDBOX_REST_URL) is None, (
        f"the allowlist does not admit the authority it names: {SANDBOX_REST_URL}"
    )
    # One authority, however it is spelled: scheme and host are compared parsed and
    # normalized, so these are the same endpoint and none of them is a new authority.
    for spelling in (f"HTTPS://{SANDBOX_AUTHORITY.upper()}",
                     f"https://{SANDBOX_AUTHORITY}.",
                     f"https://{SANDBOX_AUTHORITY}:443"):
        verdict = ondo_probe.classify_endpoint(spelling)
        assert verdict.allowed, f"{spelling} is the allowlisted authority: {verdict.reason}"
        assert verdict.host == SANDBOX_AUTHORITY, "the host is normalized before comparison"


@pytest.mark.parametrize("base_url", (
    "https://api.ondoperps-sandbox.xyz.evil.com",    # a host that contains the authority
    "http://127.0.0.1:8080",                         # the loopback the adapter admits
))
def test_a_config_carrying_a_non_allowlisted_endpoint_is_refused_before_any_client(tmp_path, base_url):
    """The endpoint a credentialed mode would sign for is the endpoint the client dials.

    The adapter's own config resolves it, so that is where the test steers it - an
    environment variable would not reach this decision at all. The loopback case is the
    sharp one end to end: it is admitted by the adapter's own policy, so a probe that
    inherited that policy verbatim would build a client here instead of refusing.
    """
    env = clean_environ(**credentials())
    result = run_probe(sandbox_argv(tmp_path / "out"), environ=env,
                       config_base_url=base_url)
    assert_refused(result, why=f"a config carrying {base_url}")
    assert "allowlist" in result.streams.lower(), result.streams


def test_the_allowlisted_endpoint_through_a_config_is_not_refused(tmp_path):
    """The same seam, with the authority the allowlist names: the run proceeds."""
    result = run_probe(sandbox_argv(tmp_path / "out"),
                       environ=clean_environ(**credentials()),
                       config_base_url=SANDBOX_REST_URL)
    assert result.code == EXIT_OK, (
        f"the allowlisted authority was refused through the config seam: {result.err!r}"
    )


@pytest.mark.parametrize("var", CREDENTIAL_VARS)
@pytest.mark.parametrize("mode", ["account-readonly", "sandbox"])
def test_a_missing_or_empty_credential_is_refused(tmp_path, mode, var):
    for value in (None, ""):
        environ = clean_environ(**credentials())
        if value is None:
            environ.pop(var, None)
        else:
            environ[var] = value
        argv = (sandbox_argv(tmp_path / "out") if mode == "sandbox"
                else ["--mode", mode, "--symbols", "NVDA", "--minutes", "0.5",
                      "--out", str(tmp_path / "out")])
        result = run_probe(argv, environ=environ)
        assert_refused(result, why=f"{mode} with {var}={value!r}")


@pytest.mark.parametrize("mode", ["account-readonly", "sandbox"])
def test_a_missing_credential_is_a_startup_rejection_not_an_empty_run(tmp_path, mode):
    """ 'The probe ran but returned no data' is the exact misreading to prevent.

    The refusal names the *variable*, never a value, and publishes no report - so no
    reader can mistake it for a run that read the account and found nothing.
    """
    out_dir = tmp_path / "out"
    environ = clean_environ(**credentials())
    environ.pop("ONDO_SANDBOX_API_KEY", None)
    argv = (sandbox_argv(out_dir) if mode == "sandbox"
            else ["--mode", mode, "--symbols", "NVDA", "--minutes", "0.5",
                  "--out", str(out_dir)])
    result = run_probe(argv, environ=environ)
    assert result.code == EXIT_REFUSED
    assert "ONDO_SANDBOX_API_KEY" in result.streams, "the refusal names the missing variable"
    for value in SECRET_SENTINELS.values():
        assert value not in result.streams, "a refusal never quotes a credential value"
    assert not out_dir.exists(), (
        "a startup rejection publishes no run directory: there is no attempt here to write "
        "up as 'ran, no data'"
    )
    assert "nothing was constructed" in result.streams, (
        "the refusal says out loud that nothing was constructed, sent or written - that "
        f"sentence is what stops it being read as a run that returned no data: "
        f"{result.streams!r}"
    )


def test_a_command_with_several_faults_refuses_rather_than_proceeding(tmp_path):
    environ = clean_environ(**credentials())
    environ.pop("ONDO_SANDBOX_API_SECRET", None)
    result = run_probe(
        ["--mode", "sandbox", "--symbols", "NVDA", "--out", str(tmp_path / "out")],
        environ=environ,
    )
    assert_refused(result, why="sandbox with no bounds and no secret")


# ========================================================================= 3. the caps
#
# min(flag, CAP). Configuration may tighten a bound and may never widen it.


def caps_view(payload: dict) -> dict:
    """Normalize the ``caps`` group to ``{token: {configured, cap, applied}}``.

    The contract fixes the constants and the clamp; the JSON spelling of a row is not
    pinned, so rows are located by token while the *values* are asserted strictly.
    """
    group = payload.get("caps")
    assert group, "the report must carry a caps group"
    rows: list[dict] = []
    if isinstance(group, dict):
        for key, value in group.items():
            if isinstance(value, dict):
                rows.append({"key": key, **value})
            else:
                rows.append({"key": key, "applied": value})
    else:
        rows = list(group)

    hints = (("notional", "notional"), ("exposure", "exposure"),
             ("max_order", "orders"), ("orders", "orders"), ("order", "orders"),
             ("minute", "minutes"), ("duration", "minutes"))
    view: dict[str, dict] = {}
    for row in rows:
        low = str(row.get("key", "")).lower()
        for needle, token in hints:
            if needle in low:
                view[token] = row
                break
    return view


def assert_three_way(row: dict, token: str) -> None:
    for field in ("configured", "cap", "applied"):
        assert field in row, f"the {token} row must report {field}: {row}"


def test_the_caps_group_reports_configured_cap_and_applied(tmp_path):
    out_dir = tmp_path / "out"
    result = run_probe(sandbox_argv(out_dir), environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    view = caps_view(probe_report(out_dir))
    assert set(view) == set(CAP_TOKENS), (
        f"caps rows found: {sorted(view)}; the four bounds are reported in a frozen "
        f"three-way shape, and a missing row is as much a defect as a wrong value"
    )
    for token, row in view.items():
        assert_three_way(row, token)


@pytest.mark.parametrize("flag,token,flag_value,cap_value", [
    ("--notional-usd", "notional", "5000", "50"),
    ("--max-exposure-usd", "exposure", "5000", "100"),
    ("--max-orders", "orders", "500", "10"),
])
def test_a_flag_far_above_the_cap_is_clamped(tmp_path, flag, token, flag_value, cap_value):
    """Far above, not merely above: a clamp that only works near the boundary is not one."""
    out_dir = tmp_path / "out"
    argv = sandbox_argv(out_dir)
    argv[argv.index(flag) + 1] = flag_value
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    row = caps_view(probe_report(out_dir))[token]
    assert Decimal(str(row["configured"])) == Decimal(flag_value)
    assert Decimal(str(row["cap"])) == Decimal(cap_value)
    assert Decimal(str(row["applied"])) == Decimal(cap_value), (
        f"{flag}={flag_value} was not clamped to the cap {cap_value}"
    )


def test_minutes_far_above_the_cap_is_clamped(tmp_path):
    out_dir = tmp_path / "out"
    argv = ["--mode", "public", "--symbols", "NVDA", "--minutes", "100000",
            "--out", str(out_dir)]
    result = run_probe(argv)
    assert result.code == EXIT_OK, result.streams
    row = caps_view(probe_report(out_dir))["minutes"]
    assert float(row["configured"]) == 100000.0
    assert float(row["cap"]) == 60.0
    assert float(row["applied"]) == 60.0, "--minutes was not clamped to MAX_MINUTES_CAP"


def test_a_flag_inside_the_cap_is_kept_as_given(tmp_path):
    out_dir = tmp_path / "out"
    argv = ["--mode", "public", "--symbols", "NVDA", "--minutes", "0.5",
            "--out", str(out_dir)]
    result = run_probe(argv)
    assert result.code == EXIT_OK, result.streams
    row = caps_view(probe_report(out_dir))["minutes"]
    assert float(row["applied"]) == 0.5, "a bound inside the cap is not changed"
    assert float(row["cap"]) == 60.0


def test_money_keeps_exact_decimals_through_the_clamp(tmp_path):
    """No f64 anywhere on a money path: 0.1 must round-trip as 0.1."""
    out_dir = tmp_path / "out"
    argv = sandbox_argv(out_dir)
    argv[argv.index("--notional-usd") + 1] = "0.1"
    argv[argv.index("--max-exposure-usd") + 1] = "0.3"
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    view = caps_view(probe_report(out_dir))
    assert Decimal(str(view["notional"]["applied"])) == Decimal("0.1"), (
        "0.1 came back as something else: money went through a float"
    )
    assert Decimal(str(view["exposure"]["applied"])) == Decimal("0.3")


# ============================================================== 4. the mode semantics
#
# public          read only, production data client, no exec client, credentials never read
# account-readonly read only, real exec client (Sandbox, account_read_only=True), no DMS
# paper           no remote write at all, simulated exec client, everything synthetic
# sandbox         the only writing mode, bounded


def test_the_default_command_issues_zero_write_requests(tmp_path):
    out_dir = tmp_path / "out"
    result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert result.code == EXIT_OK, result.streams
    assert result.writes == [], f"the default probe wrote: {result.writes}"
    payload = probe_report(out_dir)
    assert payload["mode"] == "public"
    assert payload["write_capable"] is False


def test_public_registers_no_execution_client(tmp_path):
    result = run_probe(["--symbols", "NVDA", "--minutes", "0.5",
                        "--out", str(tmp_path / "out")])
    assert result.code == EXIT_OK, result.streams
    assert result.registry.named("Execution") == [], (
        f"public mode registers no exec client at all, but built: "
        f"{result.registry.built_names}"
    )


def test_public_never_hands_a_credential_to_a_config(tmp_path):
    """Even handed the secrets, public mode does not put them into anything it builds."""
    result = run_probe(["--symbols", "NVDA", "--minutes", "0.5",
                        "--out", str(tmp_path / "out")],
                       environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    for built in result.registry.built:
        for key, value in built.kwargs.items():
            assert value not in SECRET_SENTINELS.values(), (
                f"public mode passed a credential as {key}: {built}"
            )


def test_account_readonly_arms_no_dms_and_issues_no_delete(tmp_path):
    """Arming the dead man's switch cancels resting orders - read-only must not."""
    out_dir = tmp_path / "out"
    argv = ["--mode", "account-readonly", "--symbols", "NVDA", "--minutes", "0.5",
            "--out", str(out_dir)]
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams

    for built in result.registry.built:
        calls = getattr(built, "calls", None)
        if calls is None:
            continue
        assert calls.matching("dms", "dead_man", "deadman") == [], (
            f"account-readonly armed the dead man's switch: {calls.matching('dms')}"
        )
        assert calls.matching("cancel", "delete", "close_position", "close_all") == [], (
            f"account-readonly issued a DELETE-style request: "
            f"{calls.matching('cancel', 'delete')}"
        )
    assert result.writes == [], f"account-readonly wrote: {result.writes}"

    payload = probe_report(out_dir)
    assert payload["mode"] == "account-readonly"
    assert payload["read_capable"] is True
    assert payload["write_capable"] is False
    assert payload["account_reconciled"] is not None, "the group is readable either way"
    assert not payload["canceled"], "nothing was canceled in a read-only run"
    assert not payload["synthetic"], (
        "an account read is real and read-only; it is not a simulated result"
    )


def test_account_readonly_is_configured_read_only(tmp_path):
    out_dir = tmp_path / "out"
    argv = ["--mode", "account-readonly", "--symbols", "NVDA", "--minutes", "0.5",
            "--out", str(out_dir)]
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    # Which object receives the setting is not pinned; that it is set is.
    assert any(
        built.kwargs.get("account_read_only") is True
        or getattr(built, "account_read_only", None) is True
        for built in result.registry.built
    ), (
        f"account-readonly did not set account_read_only=True anywhere: "
        f"{result.registry.built}"
    )


def test_paper_performs_no_remote_write(tmp_path):
    out_dir = tmp_path / "out"
    argv = ["--mode", "paper", "--symbols", "NVDA", "--minutes", "0.5",
            "--out", str(out_dir)]
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    assert result.writes == [], f"paper wrote to the venue: {result.writes}"
    payload = probe_report(out_dir)
    assert payload["synthetic"], "paper runs against a simulated execution side"
    assert payload["write_capable"] is False


def test_paper_marks_every_result_synthetic(tmp_path):
    out_dir = tmp_path / "out"
    argv = ["--mode", "paper", "--symbols", "NVDA", "--minutes", "0.5",
            "--out", str(out_dir)]
    result = run_probe(argv, environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    payload = probe_report(out_dir)
    assert payload["synthetic"], (
        "a paper result that is not marked synthetic is a report defect, not a fill"
    )
    assert payload["mode"] == "paper"


def test_sandbox_is_the_only_mode_that_may_write(tmp_path):
    payloads = {}
    for mode in ("public", "account-readonly", "paper", "sandbox"):
        out_dir = tmp_path / mode
        argv = (sandbox_argv(out_dir) if mode == "sandbox"
                else ["--mode", mode, "--symbols", "NVDA", "--minutes", "0.5",
                      "--out", str(out_dir)])
        result = run_probe(argv, environ=clean_environ(**credentials()))
        assert result.code == EXIT_OK, (mode, result.streams)
        payloads[mode] = probe_report(out_dir)
    assert payloads["sandbox"]["write_capable"] is True
    for mode in ("public", "account-readonly", "paper"):
        assert payloads[mode]["write_capable"] is False, f"{mode} claims it may write"


# ============================================================== 5. the report contract


def test_probe_json_exposes_every_contract_group(tmp_path):
    out_dir = tmp_path / "out"
    result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert result.code == EXIT_OK, result.streams
    payload = probe_report(out_dir)
    missing = [group for group in CONTRACT_GROUPS if group not in payload]
    assert not missing, f"probe.json is missing these groups: {missing}"


def test_the_two_frozen_verdicts_are_false_and_say_why(tmp_path):
    """Both are false by construction in R4, and the reason is part of the contract."""
    out_dir = tmp_path / "out"
    result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert result.code == EXIT_OK, result.streams
    payload = probe_report(out_dir)

    assert payload["protocol_verified"] is False, (
        "no request has ever reached the real Ondo venue: REST auth header names, WS login "
        "signature order, private frame shapes and DMS renewal semantics are documented, "
        "not host-confirmed"
    )
    assert payload["exit_code_zero_means_clean_account"] is False, (
        "OndoAccountRuntime::stop_and_wait is unreachable from Python - the client "
        "lifecycle never calls it, so shutdown drops the transport and leaves orders behind"
    )
    assert payload["converging_stop_available"] is False

    reason = payload.get("converging_stop_reason")
    if reason is None:
        reason = next((value for key, value in payload.items()
                       if "converging" in key and isinstance(value, str)), None)
    assert isinstance(reason, str) and reason.strip(), (
        "a false verdict carries its reason; a bare false is not a report"
    )


def test_the_count_groups_are_separately_readable_not_folded_into_a_total(tmp_path):
    """Submitted is not acked, partial is not filled, unknown is not no-trade."""
    out_dir = tmp_path / "out"
    result = run_probe(sandbox_argv(out_dir), environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    payload = probe_report(out_dir)
    for group in COUNT_GROUPS:
        assert group in payload, group
        assert isinstance(payload[group], (list, dict, tuple)), (
            f"{group} is a bare scalar ({payload[group]!r}); a reader cannot inspect it, "
            f"and these groups must not be folded into one total"
        )
    assert len(set(COUNT_GROUPS)) == len(COUNT_GROUPS)


def test_a_run_publishes_an_immutable_directory_per_attempt(tmp_path):
    out_dir = tmp_path / "out"
    result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert result.code == EXIT_OK, result.streams
    meta = manifest(out_dir)
    run_id = meta["run_id"]
    assert run_id
    runs = out_dir / ondo_preflight.RUNS_DIRNAME
    assert runs.is_dir(), "every attempt gets its own directory under runs/"
    run_dir = runs / run_id
    assert run_dir.is_dir()
    assert (run_dir / "meta.json").exists(), "the run directory holds its own manifest"
    assert (run_dir / "probe.json").exists(), "and the payloads it published"
    assert meta["run_dir"] == f"{ondo_preflight.RUNS_DIRNAME}/{run_id}"
    assert not list(runs.glob(f"{ondo_preflight.STAGING_PREFIX}*")), (
        "no staging directory survives a published attempt"
    )


def test_run_ids_are_unique_per_attempt_and_sort_by_start(tmp_path):
    out_dir = tmp_path / "out"
    ids = []
    for _ in range(3):
        result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
        assert result.code == EXIT_OK, result.streams
        ids.append(manifest(out_dir)["run_id"])
    assert len(set(ids)) == 3, f"run ids repeat: {ids}"
    assert ids == sorted(ids), "run ids sort by when the attempt started"
    stamp = datetime.datetime.strptime(ids[0], "%Y%m%dT%H%M%S%fZ").replace(
        tzinfo=datetime.timezone.utc)
    assert ids[0] == ondo_preflight.new_run_id(stamp), (
        "the probe shares the preflight's run-id vocabulary, verbatim"
    )
    runs = sorted(p.name for p in (out_dir / ondo_preflight.RUNS_DIRNAME).iterdir())
    assert runs == sorted(ids), "each attempt keeps its own directory"


def test_meta_json_is_replaced_last_by_temp_file_and_rename(tmp_path):
    """The commit point is the atomic replacement of <out>/meta.json.

    Nothing may be written in place: every path is staged and renamed, and the published
    manifest is the final rename, after every payload it describes is in place.
    """
    out_dir = tmp_path / "out"
    replacements: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(source, target):
        replacements.append((str(source), str(target)))
        return real_replace(source, target)

    with patch("os.replace", side_effect=spy):
        result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert result.code == EXIT_OK, result.streams

    published = [target for _, target in replacements]
    assert str(out_dir / "probe.json") in published, (
        "the payload is published by rename, never written in place"
    )
    assert str(out_dir / "meta.json") in published, (
        "the manifest is committed by temp-file + rename"
    )
    order = [target for target in published if target.startswith(str(out_dir) + os.sep)]
    assert order[-1] == str(out_dir / "meta.json"), (
        f"meta.json is the last thing published, but the order was {order}"
    )
    assert not list(out_dir.glob("*.tmp")), "no temporary file survives a completed run"


def test_a_failed_attempt_never_leaves_an_earlier_complete_true_standing(tmp_path):
    """A failure writes its own record; it does not inherit the last success's verdict."""
    out_dir = tmp_path / "out"
    good = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert good.code == EXIT_OK, good.streams
    first = manifest(out_dir)["run_id"]
    assert manifest(out_dir)["complete"] is True and probe_report(out_dir)

    boom = TimeoutError("the venue never answered")
    bad = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)],
                    node_failure=boom)
    assert bad.code in (EXIT_FAILURE, EXIT_TIMEOUT), (
        f"an attempt that could not read the venue exited {bad.code}"
    )
    meta = manifest(out_dir)
    assert meta["run_id"] != first, "the published view is the attempt that just ran"
    assert meta["complete"] is False, (
        "the earlier complete:true must not survive as the state of this attempt"
    )
    assert meta.get("failure"), "a failed attempt states why it failed"

    runs = out_dir / ondo_preflight.RUNS_DIRNAME
    assert (runs / first / "meta.json").exists(), (
        "the earlier attempt stays readable under its own run id"
    )
    assert read_json(runs / first / "meta.json")["complete"] is True
    assert read_json(runs / meta["run_id"] / "meta.json")["complete"] is False


def test_a_run_reports_outstanding_orders_rather_than_claiming_a_clean_account(tmp_path):
    """Exit code 0 does not mean the account was cleaned - the report says what is left."""
    out_dir = tmp_path / "out"
    result = run_probe(sandbox_argv(out_dir), environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    payload = probe_report(out_dir)
    assert "outstanding_orders" in payload
    assert "pending_ids" in payload
    assert payload["exit_code_zero_means_clean_account"] is False
    assert payload["stop_condition"], (
        "the report says why the probe stopped, not just that it did"
    )


# ================================================================ 6. the failure modes
#
# A timeout, an interrupt and a login failure each still publish a report, and no report,
# stream or log line ever carries a credential value.


def assert_no_secret_anywhere(result: Result, out_dir: Path) -> None:
    for value in SECRET_SENTINELS.values():
        assert value not in result.streams, f"a credential value reached a stream: {value}"
        assert value not in result.log, f"a credential value reached the log: {value}"
    # Every document the attempt published, including the immutable run copy.
    for path in sorted(Path(out_dir).rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        for value in SECRET_SENTINELS.values():
            assert value not in text, f"a credential value reached {path.name}: {value}"


def test_a_timeout_still_publishes_its_own_report(tmp_path):
    out_dir = tmp_path / "out"
    boom = TimeoutError("no answer within the login deadline")
    result = run_probe(["--mode", "account-readonly", "--symbols", "NVDA",
                        "--minutes", "0.5", "--out", str(out_dir)],
                       environ=clean_environ(**credentials()), node_failure=boom)
    # The frozen contract pins that a timeout still publishes a *complete report*; it does
    # not say which timeout maps to which code, so the code is bounded rather than fixed.
    assert result.code in (EXIT_TIMEOUT, EXIT_FAILURE), (
        f"a timed-out run exited {result.code}; a timeout is never a success and never a "
        f"refusal: {result.err!r}"
    )
    assert out_dir.exists(), "a timeout still publishes a report"
    assert (out_dir / "probe.json").exists()
    meta = manifest(out_dir)
    assert "complete" in meta, "the manifest carries a verdict even when the run timed out"
    assert_no_secret_anywhere(result, out_dir)


def test_an_interrupt_still_publishes_its_own_report(tmp_path):
    """Ctrl-C is an outcome the run reports, not an exception it leaks."""
    out_dir = tmp_path / "out"
    result = run_probe(["--mode", "account-readonly", "--symbols", "NVDA",
                        "--minutes", "0.5", "--out", str(out_dir)],
                       environ=clean_environ(**credentials()),
                       node_failure=KeyboardInterrupt())
    assert isinstance(result.code, int), "the interrupt is translated into an exit code"
    assert result.code != EXIT_OK, "an interrupted run is not a clean run"
    assert out_dir.exists(), "an interrupted run still publishes a report"
    assert (out_dir / "probe.json").exists()
    assert manifest(out_dir)["complete"] is False, "an interrupted attempt is not complete"
    assert_no_secret_anywhere(result, out_dir)


def test_a_run_stopped_by_its_own_deadline_exits_timeout(tmp_path):
    """``EXIT_TIMEOUT`` has to be reachable, or it is a constant nobody can rely on.

    A real ``node.run()`` blocks, so a deadline is the only thing that can end a session
    that never returns on its own - which is why the blocking node is the honest shape for
    this test and not a convenience.
    """
    out_dir = tmp_path / "out"
    argv = sandbox_argv(out_dir)
    argv[argv.index("--minutes") + 1] = "0.01"
    result = run_probe(argv, environ=clean_environ(**credentials()), blocking_node=True)
    assert result.code == EXIT_TIMEOUT, (
        f"a run ended by its own deadline exited {result.code}, not EXIT_TIMEOUT "
        f"({EXIT_TIMEOUT}): {result.err!r}"
    )
    meta = manifest(out_dir)
    assert meta["complete"] is False, "a deadline-stopped attempt is not a complete run"
    assert (out_dir / "probe.json").exists(), "and it still publishes its own report"
    assert_no_secret_anywhere(result, out_dir)


def test_a_login_failure_still_publishes_its_own_report(tmp_path):
    out_dir = tmp_path / "out"
    denial = RuntimeError("login rejected: signature_mismatch")
    result = run_probe(["--mode", "account-readonly", "--symbols", "NVDA",
                        "--minutes", "0.5", "--out", str(out_dir)],
                       environ=clean_environ(**credentials()), node_failure=denial)
    assert result.code != EXIT_OK, "a rejected login is not a successful run"
    assert out_dir.exists(), "a login failure still publishes a report"
    assert (out_dir / "probe.json").exists()
    meta = manifest(out_dir)
    assert meta["complete"] is False
    assert meta.get("failure"), "the manifest names what went wrong"
    assert_no_secret_anywhere(result, out_dir)


# ------------------------------------- the stop target: never the node from a foreign thread
#
# A `LiveNode` is declared `unsendable` in PyO3, and that check is a *thread id* check: a
# deadline stop that read an attribute of the node from the watchdog thread tripped a Rust
# assertion, which arrives in Python as a `PanicException` - a `BaseException`, so no
# `except Exception` caught it. The blocking `run()` then never returned, the `finally` never
# ran, and nothing was ever published. The fix is the framework's thread-safe
# `LiveNodeHandle`, resolved on the owning thread *before* `run()` blocks and handed to the
# watchdog in place of the node.
#
# A real `LiveNode` cannot be built in a unit test and the panic is not catchable, so the
# regression test is the target the stop is *delivered to*: that is the mechanism which makes
# the panic impossible.

STOP_OUTCOME_KEYS = frozenset({
    "label", "attempted", "requested", "stopped", "error", "iterations", "stop_target",
    "cancels_own_orders", "confirms_cancels", "releases_dead_mans_switch",
})


def test_a_stop_outcome_names_the_target_it_used():
    """Every stop says what it was delivered to - and what it does not claim to have done."""
    outcome = ondo_probe.bounded_stop(None, label="final", via="none")
    assert set(outcome) == STOP_OUTCOME_KEYS, (
        f"the stop outcome's keys are part of the report: {sorted(outcome)}"
    )
    assert outcome["stop_target"] == "none", "the report names the target, not just that none was there"
    assert outcome["attempted"] is False, "nothing was there to stop, and the report says so"
    assert outcome["requested"] is False
    assert outcome["stopped"] is False
    assert outcome["cancels_own_orders"] is False, (
        "a framework stop does not cancel this run's orders: the report must never imply it did"
    )


def test_the_stop_target_is_the_handle_when_the_node_has_one():
    node = FakeNode(Registry())
    handle = node.the_handle
    assert handle is not None
    target, via = ondo_probe.resolve_stop_target(node)
    assert via == "handle", (
        "a node exposing handle() must be stopped through it: reading the node itself from "
        "the watchdog thread is what killed the run this fix replaced"
    )
    assert target is handle


def test_the_stop_target_is_the_node_when_there_is_no_handle():
    """The fallback is safe for a double and must not be lost - but it is never a real node."""
    node = NoHandleNode()
    target, via = ondo_probe.resolve_stop_target(node)
    assert (target, via) == (node, "node")
    outcome = ondo_probe.bounded_stop(target, label="final", via=via)
    assert node.stop_calls == 1, "a node with no handle is still stopped, through its own stop()"
    assert outcome["requested"] is True
    assert outcome["stop_target"] == "node"


def test_the_stop_target_is_none_when_there_is_nothing_to_stop():
    target, via = ondo_probe.resolve_stop_target(None)
    assert (target, via) == (None, "none")
    outcome = ondo_probe.bounded_stop(target, label="final", via=via)
    assert outcome["stop_target"] == "none"
    assert outcome["attempted"] is False, "there was nothing to stop, and the report says so"


def test_a_completed_run_stops_through_the_handle_and_never_touches_the_node(tmp_path):
    """The regression test for the defect, end to end.

    A run that returns on its own still has a watchdog: ``finally`` sets the done event, which
    wakes it, so the stop can arrive from two places - the watchdog's thread and the owning
    thread's final stop. Both must be delivered to whatever ``resolve_stop_target`` resolved
    on the owning thread. The node's own ``stop`` is never called: on a real node that call
    *from the watchdog thread* is the panic, and it is the one thing this pins.
    """
    out_dir = tmp_path / "out"
    result = run_probe(["--mode", "public", "--symbols", "NVDA", "--minutes", "0.5",
                        "--out", str(out_dir)])
    assert result.code == EXIT_OK, result.streams
    assert result.registry.nodes, "the run built a node"
    node = result.registry.nodes[-1]
    handle = node.the_handle
    assert handle is not None
    assert node.stop_calls == 0, (
        f"the node itself was stopped {node.stop_calls} time(s). A LiveNode read from the "
        f"watchdog thread trips PyO3's unsendable assertion, which is a BaseException: the "
        f"blocking run() never returns and nothing is ever published"
    )
    # Exactly two, and deterministic: the run returns on its own, `finally` wakes the watchdog
    # and joins it before the owning thread performs its own final stop. One stop each.
    assert handle.stop_calls == 2, "the run was stopped once by the watchdog and once by the run"
    stops = probe_report(out_dir)["stops"]
    assert stops, "the report names the stops it performed"
    for entry in stops:
        assert set(entry) == STOP_OUTCOME_KEYS, sorted(entry)
        assert entry["stop_target"] == "handle", (
            f"a stop was delivered to something other than the handle: {entry}"
        )
    assert "final" in [entry["label"] for entry in stops], (
        "the owning thread's own stop is in the report"
    )


def test_the_watchdog_stop_is_delivered_to_the_handle_too(tmp_path):
    """The watchdog is the thread that cannot touch the node, so it must be given the handle.

    Driven end to end rather than by calling the watchdog directly: the module's own closure
    is what is under test, and a test that rebuilt that closure would prove the copy instead.

    The blocking node makes this airtight - ``run()`` returns only when something releases it,
    and the only thing that can is the stop. So ``run()`` returning at all, with the node's own
    ``stop`` never called, *is* the proof that the watchdog went through the handle. The
    report's own watchdog entry is required: the watchdog thread is joined before the document
    is built, so the stop that actually ended this run is always in ``stops``.
    """
    out_dir = tmp_path / "out"
    argv = sandbox_argv(out_dir)
    argv[argv.index("--minutes") + 1] = "0.01"
    result = run_probe(argv, environ=clean_environ(**credentials()), blocking_node=True)
    assert result.code == EXIT_TIMEOUT, result.streams
    node = result.registry.nodes[-1]
    handle = node.the_handle
    assert handle is not None
    assert node.stop_calls == 0, (
        "the watchdog stopped the node itself: that is the defect this replaced, and on a "
        "real node it is a cross-thread read that panics instead of stopping"
    )
    assert handle.stop_calls >= 1, "the deadline released the run, so a stop was delivered"
    stops = probe_report(out_dir)["stops"]
    assert all(entry["stop_target"] == "handle" for entry in stops), stops
    watchdog_entries = [entry for entry in stops if entry["label"] == "watchdog"]
    assert watchdog_entries, f"the stop that ended the run is missing from the report: {stops}"
    assert watchdog_entries[0]["requested"] is True, watchdog_entries[0]
    assert_no_secret_anywhere(result, out_dir)


def test_the_watchdog_entry_is_in_the_report_even_when_its_stop_finishes_last(tmp_path):
    """The document is built from a settled ``stops`` list, not from whichever thread won.

    A run that returns on its own leaves the watchdog still waiting: ``finally`` sets the done
    event, which wakes it, and the entry it appends is written from that thread. Build the
    document without waiting for it and the same run publishes two different reports - one
    with the watchdog entry and one without - which is a report that cannot be cited as
    evidence of what the run did.

    The 0.3 s is the schedule, not a slow machine: it puts the watchdog's append after the
    owning thread's final stop every time. What is asserted is the invariant, not the timing
    - every stop that was performed is in the list.
    """
    out_dir = tmp_path / "out"
    registry = Registry()
    factory = SlowWatchdogFactory(registry, delay=0.3)
    result = run_probe(["--mode", "public", "--symbols", "NVDA", "--minutes", "0.5",
                        "--out", str(out_dir)], registry=registry, factory=factory)
    assert result.code == EXIT_OK, result.streams
    stops = probe_report(out_dir)["stops"]
    labels = sorted(entry["label"] for entry in stops)
    assert labels == ["final", "watchdog"], (
        f"the report's stops list is {labels}, and both stops were performed: the watchdog's "
        f"entry is appended from the watchdog thread, so a document built without joining it "
        f"records the stop that ran last as one that never happened"
    )


def test_a_node_without_a_handle_is_stopped_and_the_report_says_which_target(tmp_path):
    out_dir = tmp_path / "out"
    result = run_probe(["--mode", "public", "--symbols", "NVDA", "--minutes", "0.5",
                        "--out", str(out_dir)], with_handle=False)
    assert result.code == EXIT_OK, result.streams
    node = result.registry.nodes[-1]
    assert node.stop_calls >= 1, "a node with no usable handle is stopped through its own stop()"
    stops = probe_report(out_dir)["stops"]
    assert stops and {entry["stop_target"] for entry in stops} == {"node"}, stops


def test_an_attempt_that_never_built_a_node_reports_no_stop_target(tmp_path):
    """A failure before the node exists is still reported as a stop that was not performed."""
    out_dir = tmp_path / "out"
    result = run_probe(["--mode", "public", "--symbols", "NVDA", "--minutes", "0.5",
                        "--out", str(out_dir)],
                       factory=FailingFactory(RuntimeError("the venue refused a session")))
    assert result.code == EXIT_FAILURE, result.streams
    meta = manifest(out_dir)
    assert meta["complete"] is False, "the failed attempt publishes its own verdict"
    stops = probe_report(out_dir)["stops"]
    assert stops, "the report still accounts for the stop it did not perform"
    for entry in stops:
        assert entry["stop_target"] == "none", entry
        assert entry["attempted"] is False, entry


def test_the_execution_config_exposes_no_secret_attribute():
    """The real config takes api_key/api_secret as ctor kwargs and keeps no attribute.

    The real class is checked when the adapter wheel is installed - a class-level check
    needs no construction, so it works without knowing which fields are required. When it
    is not installed, the same property is asserted on the mirror this file's fake models.
    """
    try:
        from nautilus_trader.adapters.ondo import OndoExecutionClientConfig as real
    except Exception:
        real = None

    if real is not None:
        for name in ("api_key", "api_secret"):
            assert not hasattr(real, name), (
                f"the real {real.__name__} exposes {name} as an attribute; a secret must not "
                f"be readable off the object"
            )
        return

    config = FakeAdapter(Registry()).OndoExecutionClientConfig(
        api_key=SECRET_SENTINELS["ONDO_SANDBOX_API_KEY"],
        api_secret=SECRET_SENTINELS["ONDO_SANDBOX_API_SECRET"],
        account_id=SECRET_SENTINELS["ONDO_SANDBOX_ACCOUNT_ID"],
    )
    assert not hasattr(config, "api_secret"), "a secret is not readable off the object"
    assert not hasattr(config, "api_key")
    rendered = str(vars(config)) + repr(config)
    for value in SECRET_SENTINELS.values():
        assert value not in rendered


def test_a_dry_run_opens_no_socket(tmp_path):
    out_dir = tmp_path / "out"
    with patch.object(socket.socket, "connect",
                      side_effect=AssertionError("dry-run must not open a socket")), \
            patch("socket.create_connection",
                  side_effect=AssertionError("dry-run must not open a socket")), \
            patch("socket.getaddrinfo",
                  side_effect=AssertionError("dry-run must not resolve a host")):
        result = run_probe(["--mode", "public", "--symbols", "NVDA",
                            "--out", str(out_dir), "--dry-run"])
    assert result.code == EXIT_OK, result.streams
    assert not out_dir.exists(), "dry-run publishes nothing"


def test_a_dry_run_never_reads_dotenv(tmp_path):
    """``.env`` exists in this repository and may hold real sandbox credentials.

    It is loaded by other entry scripts through ``python-dotenv``; the dry run must not
    trigger that. If python-dotenv is not installed the patches cannot be installed, and
    the source-level check in the hygiene section is what carries the guarantee.
    """
    with contextlib.ExitStack() as stack:
        if importlib.util.find_spec("dotenv") is not None:
            for name in ("load_dotenv", "dotenv_values", "find_dotenv"):
                stack.enter_context(patch(
                    f"dotenv.{name}",
                    side_effect=AssertionError("the probe must never read .env"),
                ))
        result = run_probe(["--mode", "public", "--symbols", "NVDA",
                            "--out", str(tmp_path / "out"), "--dry-run"])
    assert result.code == EXIT_OK, result.streams


def test_a_dry_run_constructs_no_client_and_builds_no_node(tmp_path):
    result = run_probe(["--mode", "public", "--symbols", "NVDA",
                        "--out", str(tmp_path / "out"), "--dry-run"])
    assert result.code == EXIT_OK, result.streams
    clients = [built for built in result.registry.built if not is_config(built.name)]
    assert clients == [], f"a dry run constructs no client, built: {clients}"
    assert result.registry.nodes == [], "a dry run builds no node"
    assert result.registry.builder_args == [], "and it configures no framework builder"


def test_a_dry_run_of_the_writing_mode_still_opens_no_socket(tmp_path):
    """``sandbox`` is the only mode that may write - and a dry run of it still may not
    reach the network, whatever the bounds say."""
    out_dir = tmp_path / "out"
    with patch.object(socket.socket, "connect",
                      side_effect=AssertionError("dry-run must not open a socket")), \
            patch("socket.create_connection",
                  side_effect=AssertionError("dry-run must not open a socket")):
        result = run_probe(sandbox_argv(out_dir) + ["--dry-run"],
                           environ=clean_environ(**credentials()))
    assert result.code == EXIT_OK, result.streams
    clients = [built for built in result.registry.built if not is_config(built.name)]
    assert clients == [], f"a dry run built a client: {clients}"
    assert result.registry.nodes == [], "a dry run builds no node"
    assert result.writes == [], "a dry run issues no write request"
    assert not out_dir.exists(), "a dry run publishes nothing"


# ========================================================================== 7. hygiene


def test_the_module_carries_no_signing_implementation():
    """Submission goes through the native client; the app never signs a REST call."""
    code = _code()
    for forbidden in ("hmac", "ONDO-SIGN", "ONDO-KEY-ID", "ONDO-TIMESTAMP",
                      "X-API-KEY", "Authorization", "hmac.new"):
        assert forbidden not in code, (
            f"ondo_probe.py carries its own signing implementation ({forbidden!r}); "
            f"REST signing belongs to the adapter"
        )
    definitions = re.findall(r"def\s+(\w+)\s*\(", code)
    signing = [name for name in definitions if "sign" in name.lower()]
    assert signing == [], f"the module defines signing helpers: {signing}"


def test_the_module_builds_no_second_http_client():
    """Parsing a URL is not opening a connection: ``urlsplit`` is required by the allowlist,
    a second HTTP client is not."""
    code = _code()
    # Word-boundary matches: "requests_sent" is a count in the dry-run document, not the
    # ``requests`` library, and a substring check cannot tell them apart.
    for forbidden in ("requests", "aiohttp", "httpx", "urlopen", "urllib.request",
                      "urlretrieve", "httpx.Client"):
        assert not re.search(rf"\b{re.escape(forbidden)}\b", code), (
            f"the app must not open its own HTTP client ({forbidden!r})"
        )


def test_a_dry_run_never_reaches_dotenv_even_through_the_real_default_path(tmp_path):
    """``environ=None`` is the path a real command line takes, and the only one where
    ``.env`` is reachable at all: a dry run must not get there."""
    with contextlib.ExitStack() as stack:
        if importlib.util.find_spec("dotenv") is not None:
            for name in ("load_dotenv", "dotenv_values", "find_dotenv"):
                stack.enter_context(patch(
                    f"dotenv.{name}",
                    side_effect=AssertionError("--dry-run must not read .env"),
                ))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ondo_probe.main(
                ["--mode", "public", "--symbols", "NVDA",
                 "--out", str(tmp_path / "out"), "--dry-run"],
                adapter=FakeAdapter(Registry()),
                node_factory=NodeFactory(Registry()),
                environ=None,
                now=Clock(),
            )
    assert code == EXIT_OK, err.getvalue()
    assert not (tmp_path / "out").exists()


def test_dotenv_is_confined_to_the_environment_seam():
    """``.env`` is the entry script's convenience, and it may only be reached where the
    injected environment is absent - never inside credential resolution or a report."""
    seam = getattr(ondo_probe, "load_environment", None)
    assert callable(seam), (
        "the probe resolves its environment in one named seam, so the .env convenience has "
        "exactly one place to live"
    )
    tree = ast.parse(_source())
    carriers = [node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "dotenv" in ast.unparse(node)]
    assert carriers == ["load_environment"], (
        f"dotenv is reachable from {carriers}; it belongs in load_environment alone"
    )
    assert "load_dotenv" in _source(), "and it is there because the house convention reads .env"


def test_the_module_never_retries_a_submission():
    """A retried POST is how one order gets submitted twice."""
    code = _code().lower()
    for forbidden in ("max_retries", "max_attempts", "retry_on", "tenacity",
                      "for attempt in range"):
        assert forbidden not in code, (
            f"{forbidden!r} suggests a submission retry loop in the app layer"
        )


def test_the_module_mirrors_the_preflight_publishing_vocabulary():
    """It mirrors the logic rather than importing it: preflight's PAYLOAD_FILES is fixed
    to its own five documents, so the probe carries its own payload set."""
    code = _code()
    assert ondo_probe.PAYLOAD_FILES == ("probe",), (
        f"the probe publishes exactly one document, got {ondo_probe.PAYLOAD_FILES!r}"
    )
    assert ondo_preflight.PAYLOAD_FILES != ondo_probe.PAYLOAD_FILES, (
        "if these ever match, the probe is reusing preflight's payload set and would "
        "publish the wrong documents"
    )
    imported = {
        alias.name
        for node in ast.walk(ast.parse(_source()))
        if isinstance(node, ast.ImportFrom) and node.module == "ondo_preflight"
        for alias in node.names
    }
    assert {"RUNS_DIRNAME", "STAGING_PREFIX", "new_run_id"} <= imported, (
        f"the run-directory, staging and run-id vocabulary is imported from "
        f"ondo_preflight, verbatim; got {sorted(imported)}"
    )
    # ...and not re-defined locally, which is how the two would drift.
    for name in ("RUNS_DIRNAME", "STAGING_PREFIX"):
        assert not re.search(rf"^{name}\s*=", code, re.M), (
            f"{name} is redefined here instead of imported, so it can drift from the "
            f"preflight's"
        )
    assert "os.replace" in code, "publishing is a rename, never an in-place write"


def test_the_read_only_mode_is_a_real_setting_not_a_label():
    """``account-readonly`` must reach the client as ``account_read_only``.

    A mode that only prints "read-only" and arms the private stream anyway is the failure
    this guards; the setting is what the adapter acts on.
    """
    assert "account_read_only" in _code()


# ======================================================================= 8. stability
#
# Written to disk, not printed (R3 acceptance report, section 7). Point
# ONDO_PROBE_STABILITY_OUT at a run directory to keep the evidence with a real run.


def stability_dir(tmp_path: Path) -> Path:
    override = os.environ.get("ONDO_PROBE_STABILITY_OUT")
    return Path(override) if override else Path(tmp_path)


def write_stability(tmp_path: Path, name: str, evidence: dict) -> Path:
    target = stability_dir(tmp_path) / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target


def test_repeated_refusals_are_deterministic(tmp_path):
    """Ten identical refusals, ten identical verdicts - a gate that flips is not a gate."""
    codes, built = [], []
    for _ in range(10):
        result = run_probe(sandbox_argv(tmp_path / "out", "--max-orders"),
                           environ=clean_environ(**credentials()))
        codes.append(result.code)
        built.append(len(result.registry.built))
    assert set(codes) == {EXIT_REFUSED}, f"refusal verdicts varied: {codes}"
    assert set(built) == {0}, f"a refusal built clients on some attempts: {built}"

    evidence = {
        "check": "repeated_refusals_are_deterministic",
        "attempts": len(codes),
        "exit_codes": codes,
        "clients_built": built,
        "verdict": "deterministic",
    }
    path = write_stability(tmp_path, "refusal-stability", evidence)
    assert read_json(path)["exit_codes"] == codes, "the evidence on disk is what was observed"


def test_repeated_runs_are_independent_and_distinct(tmp_path):
    """Five runs of the same command: distinct run ids, identical frozen verdicts."""
    out_dir = tmp_path / "out"
    run_ids, write_counts, digests = [], [], []
    for _ in range(5):
        result = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
        assert result.code == EXIT_OK, result.streams
        run_ids.append(manifest(out_dir)["run_id"])
        write_counts.append(len(result.writes))
        payload = probe_report(out_dir)
        # The run id is the volatile part; the verdicts are what must not move.
        verdicts = {key: payload[key] for key in FROZEN_VERDICTS}
        digests.append(hashlib.sha256(
            json.dumps(verdicts, sort_keys=True).encode("utf-8")).hexdigest())

    assert len(set(run_ids)) == 5, f"run ids repeated across attempts: {run_ids}"
    assert set(write_counts) == {0}, f"some run wrote: {write_counts}"
    assert len(set(digests)) == 1, "the frozen verdicts moved between identical runs"

    evidence = {
        "check": "repeated_runs_are_independent_and_distinct",
        "attempts": len(run_ids),
        "run_ids": run_ids,
        "write_requests_per_run": write_counts,
        "frozen_verdicts": list(FROZEN_VERDICTS),
        "verdict_digest": digests[0],
        "verdict": "stable",
    }
    path = write_stability(tmp_path, "run-stability", evidence)
    assert read_json(path)["run_ids"] == run_ids, "the evidence on disk is what was observed"


def test_a_failing_attempt_does_not_corrupt_the_next_one(tmp_path):
    """Failure, then success, at the same --out: the published view follows the attempt."""
    out_dir = tmp_path / "out"
    boom = RuntimeError("the venue closed the socket")
    bad = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)],
                    node_failure=boom)
    assert bad.code in (EXIT_FAILURE, EXIT_TIMEOUT)
    assert manifest(out_dir)["complete"] is False

    good = run_probe(["--symbols", "NVDA", "--minutes", "0.5", "--out", str(out_dir)])
    assert good.code == EXIT_OK, good.streams
    meta = manifest(out_dir)
    assert meta["complete"] is True, "the recovered attempt publishes its own success"
    assert probe_report(out_dir)["mode"] == "public"

    evidence = {
        "check": "a_failing_attempt_does_not_corrupt_the_next_one",
        "failed_exit_code": bad.code,
        "recovered_exit_code": good.code,
        "completed_run_id": meta["run_id"],
        "verdict": "recovered",
    }
    path = write_stability(tmp_path, "recovery-stability", evidence)
    assert read_json(path)["recovered_exit_code"] == EXIT_OK


if __name__ == "__main__":
    import unittest

    unittest.main()
