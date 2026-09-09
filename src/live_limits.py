#!/usr/bin/env python3
"""Operator limits for the live maker, with hard caps that configuration can only lower.

``config/limits.toml`` is the operator-editable surface.  Every capped key also has a module
constant here, and the loader applies ``min(config, CAP)`` - the pattern of
``exec_probe.py:347`` (``self.max_notional = min(Decimal(max_notional), MAX_NOTIONAL_USDT)``),
generalised.  Editing the TOML can only make a run smaller or safer; there is no path by which
a configuration file, an environment variable or a command line flag widens a limit.  A
missing file, section or key falls back to the default, which is already inside its cap.

    from live_limits import load_limits
    limits = load_limits(Path("config/limits.toml"))
    print(limits.report())          # configured / cap / applied, one row per key
    limits.order.max_notional_usd   # the number the strategy actually uses

**Deviation from PROMPT.md stage 3, approved by the user 2026-09-08.**  PROMPT.md caps the
mainnet stage at "单日最多 20 次触发".  That budget describes the original taker/taker
arbitrage, where one trigger is one complete round trip.  A two-sided maker earns from many
small passive fills instead - a ten minute session prints dozens - so the daily budget here is
counted in FILLS (300 in the shipped config, 200 as the built-in fallback when no config file
is present, hard cap 500) and in Lighter order transactions (40 000 in the config, 20 000 as
the fallback, hard cap 60 000 - at the observed ~23 tx/min a full day needs ~33 000, so a
20 000 budget would stop a healthy session after about 14 hours).  The per-order notional
(50 USD) and total exposure (100 USD) hard caps are unchanged from PROMPT.md.  The fill
counter counts fill EVENTS, so a partially filled clip contributes one per partial.

**Profit-gated fill budget.**  A day that is actually making money may run longer than the
base 300 fills: ``[daily] max_fills_profitable`` (1 500, hard cap 3 000) replaces
``max_fills`` once the day's realised net clears ``profit_gate_usd`` with a trip-net EWMA that
is not negative.  It then stays in force on hysteresis while realised net is merely still
positive, so a session does not flip between the two caps around the gate, and it re-locks the
moment the day gives its profit back - at which point a session already past 300 fills stops
on the next check.  The loss kill switch is untouched by any of this.  Both the fallbacks and
the caps are chosen so that no configuration produces a budget a losing day can reach.

**Opening gates.**  ``[quote] min_spread_ratio`` / ``min_maker_spread_bps`` and
``max_move_bps_per_min`` are the two rails added after the 2026-09-09 mainnet session (see
``src/quote_gates.py`` for the evidence).  Their hard rail is a FLOOR, not a cap, because for
these keys the smaller number is the looser one; a file that asks for less than the floor is
refused rather than quietly raised.  ``max_move_bps_per_min`` carries both, since a larger
number loosens THAT gate.

Read-only, stdlib only (``tomllib`` is 3.11+).
"""

from __future__ import annotations

import sys
import tomllib
import warnings
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from quote_placement import PLACEMENTS  # noqa: E402
from quote_placement import BasisParams  # noqa: E402
from quote_placement import describe as describe_placement  # noqa: E402


# ------------------------------------------------------------------ hard caps
#
# Module constants, deliberately not CLI/env/TOML knobs, so no configuration can widen them.
# Mirrors exec_probe.MAX_NOTIONAL_USDT.  Changing one is a source edit and a code review.

CAP_ORDER_NOTIONAL_USD = 50.0  # PROMPT.md stage 3: single order <= 50 USD notional
CAP_INVENTORY_USD = 100.0
CAP_UNHEDGED_USD = 30.0
CAP_TOTAL_NOTIONAL_USD = 100.0  # PROMPT.md stage 3: total exposure <= 100 USD
CAP_DAILY_FILLS = 500  # base budget; see the deviation note in the module docstring
# The extended budget a profitable day may earn (see DailyLimits.profit_gate_usd).  It is a
# separate, larger hard cap rather than a raised CAP_DAILY_FILLS, so a losing or a flat day can
# never reach it: the base cap still binds unless the day has actually made money.
CAP_DAILY_FILLS_PROFITABLE = 3_000
# ~23 tx/min was the observed quoting rate, so a full trading day needs ~33k transactions;
# 40k of budget with a 60k cap leaves a healthy session room to run the day out.
CAP_DAILY_TX = 60_000
CAP_LOSS_USD = 20.0
CAP_TX_PER_MIN = 40.0  # Lighter allows 40 sendTx/min per L1 address; never quote above it

# ------------------------------------------------------------------ opening-gate rails
#
# The two opening gates (src/quote_gates.py) are rails whose SAFE direction is upwards for the
# spread knobs and downwards for the volatility knob, so they are guarded by FLOORS rather than
# by caps: a configuration file may demand a wider spread or a quieter market than the shipped
# numbers, never a narrower one or a faster one.  A value below the floor is refused outright
# instead of being silently raised, because a gate that has been configured off is not a
# smaller run - it is the 2026-09-09 mainnet session again.
FLOOR_MIN_SPREAD_RATIO = 1.0  # below 1.0 the maker spread would not even cover the hedge
FLOOR_MIN_MAKER_SPREAD_BPS = 5.0
FLOOR_MAX_MOVE_BPS_PER_MIN = 5.0  # a limit under this would never let the gate open
FLOOR_GATE_WINDOW_S = 1.0  # a window has to hold at least one second of samples
# The anchor placement's distance from the hedge mid.  A LARGER number rests further out and
# takes fewer, better fills, so the rail is again a floor: configuration may ask for more edge
# than the shipped 12 bps, never for a quote pressed up against the touch.
FLOOR_ANCHOR_EDGE_BPS = 3.0
# The anchor's basis estimator.  A longer window and more samples are the SAFER settings - the
# median is harder to move and the opening side waits longer - so both are floors.  There is
# deliberately no "off" value here: the 2026-09-07 / 09-08 replays showed an anchor on the raw
# hedge mid is one-sided on this pair, so a live run may lengthen the estimate, never skip it.
FLOOR_BASIS_WINDOW_S = 10.0
FLOOR_BASIS_MIN_N = 10
# ... and the volatility limit additionally carries a cap, because for THAT knob a larger
# number is the looser one: without it a config could raise max_move_bps_per_min to infinity
# and disable the gate, which is exactly what the rest of this module promises cannot happen.
CAP_MAX_MOVE_BPS_PER_MIN = 50.0

# A fully hedged maker holds -q on the hedge venue, so the GROSS two-leg notional the
# [exposure] cap measures is 2 * |inventory|.  The headroom leaves room for mid drift between
# the two venues and for a rounded-up dust hedge, so the exposure kill stays a backstop
# instead of firing during normal operation.  See Limits.effective_inventory_usd.
EXPOSURE_HEADROOM = 0.90

DEFAULT_PATH = Path("config/limits.toml")


class LimitsError(ValueError):
    """Raised when the file exists but cannot produce a usable set of limits."""


class LimitsWarning(UserWarning):
    """A key the loader never asked for - almost always one under the wrong table header.

    The whole gate block lived under ``[hedge]`` from 2026-09-09 to 2026-09-10 because it had
    been appended to the end of the file, below the ``[hedge]`` header.  ``tomllib`` parsed it
    happily, ``r.number("quote", "min_spread_ratio", ...)`` never found it, and the run used
    the built-in defaults while the file said something else - silently, for two days.  Any key
    the resolver did not consume is now reported.
    """


# ------------------------------------------------------------------ resolution rows


@dataclass(frozen=True)
class Row:
    """One resolved key: what the file asked for, its hard rail, and what is used.

    The rail is a cap for almost every key (``applied = min(configured, cap)``) and a floor for
    the opening-gate knobs, where a SMALLER number is the looser one: a value under the floor is
    refused by the loader, so ``applied`` always equals ``configured`` on those rows and the
    report shows the rail as ``>=floor``.
    """

    key: str
    configured: object
    cap: object | None
    applied: object
    floor: object | None = None

    @property
    def capped(self) -> bool:
        return self.cap is not None and self.configured != self.applied

    @staticmethod
    def _fmt(value: object) -> str:
        return f"{value:g}" if isinstance(value, float) else str(value)

    def line(self) -> str:
        if self.cap is not None:
            cap = self._fmt(self.cap)
        elif self.floor is not None:
            cap = ">=" + self._fmt(self.floor)
        else:
            cap = "-"
        got = self._fmt(self.configured)
        use = self._fmt(self.applied)
        mark = "  <- CAPPED" if self.capped else ""
        return (
            f"  {self.key:<38} configured={got:>10}  cap={cap:>10}  applied={use:>10}{mark}"
        )


class _Resolver:
    """Reads one section, applies the cap, and records a row for the report."""

    def __init__(self, data: dict, rows: list[Row]) -> None:
        self._data = data
        self._rows = rows
        # Every (table, key) any resolver asked for, so the loader can name the ones the file
        # holds that nobody read.  See LimitsWarning.
        self.asked: set[tuple[str, str]] = set()

    def _raw(self, section: str, key: str, default):
        self.asked.add((section, key))
        block = self._data.get(section)
        if block is None:
            return default
        if not isinstance(block, dict):
            raise LimitsError(f"[{section}] is not a table")
        return block.get(key, default)

    def number(
        self,
        section: str,
        key: str,
        default: float,
        cap: float | None = None,
        *,
        minimum: float | None = 0.0,
        allow_negative: bool = False,
        floor: float | None = None,
    ) -> float:
        """Resolve one numeric key.

        ``cap`` is the usual hard rail and is APPLIED (``min(config, cap)``).  ``floor`` is the
        rail for the keys where a smaller number is the looser one - the opening gates - and is
        REFUSED rather than applied: silently raising such a value would hide from the operator
        that the run is not doing what the file says.
        """
        raw = self._raw(section, key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise LimitsError(f"[{section}] {key} must be a number, got {raw!r}")
        value = float(raw)
        if not allow_negative and minimum is not None and value < minimum:
            raise LimitsError(f"[{section}] {key} must be >= {minimum:g}, got {value:g}")
        if floor is not None and value < floor:
            raise LimitsError(
                f"[{section}] {key} {value:g} is below the hard floor {floor:g}: this is an "
                f"opening gate, and configuration may only tighten it",
            )
        applied = min(value, cap) if cap is not None else value
        self._rows.append(Row(f"{section}.{key}", value, cap, applied, floor))
        return applied

    def count(
        self,
        section: str,
        key: str,
        default: int,
        cap: int | None = None,
        *,
        minimum: int = 0,
    ) -> int:
        raw = self._raw(section, key, default)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise LimitsError(f"[{section}] {key} must be an integer, got {raw!r}")
        if raw < minimum:
            raise LimitsError(f"[{section}] {key} must be >= {minimum}, got {raw}")
        applied = min(raw, cap) if cap is not None else raw
        self._rows.append(Row(f"{section}.{key}", raw, cap, applied))
        return applied

    def flag(self, section: str, key: str, default: bool) -> bool:
        """A behaviour toggle.  It has no numeric cap, so it is recorded and returned as-is."""
        raw = self._raw(section, key, default)
        if not isinstance(raw, bool):
            raise LimitsError(f"[{section}] {key} must be true or false, got {raw!r}")
        self._rows.append(Row(f"{section}.{key}", raw, None, raw))
        return raw

    def word(self, section: str, key: str, default: str, choices: tuple[str, ...]) -> str:
        """A lower-case enumerated string (the placements); recorded and returned as-is."""
        raw = self._raw(section, key, default)
        if not isinstance(raw, str):
            raise LimitsError(f"[{section}] {key} must be a string, got {raw!r}")
        value = raw.strip().lower()
        if value not in choices:
            raise LimitsError(f"[{section}] {key} must be one of {list(choices)}, got {raw!r}")
        self._rows.append(Row(f"{section}.{key}", value, None, value))
        return value

    def text(self, section: str, key: str, default: str, choices: tuple[str, ...]) -> str:
        raw = self._raw(section, key, default)
        if not isinstance(raw, str):
            raise LimitsError(f"[{section}] {key} must be a string, got {raw!r}")
        value = raw.strip().upper()
        if value not in choices:
            raise LimitsError(f"[{section}] {key} must be one of {list(choices)}, got {raw!r}")
        self._rows.append(Row(f"{section}.{key}", value, None, value))
        return value


# ------------------------------------------------------------------ sections


@dataclass(frozen=True)
class OrderLimits:
    max_notional_usd: float = 20.0
    # 0.0 = derive the floor from the venue minimum at runtime (see ``min_notional_floor``).
    min_notional_usd_floor: float = 0.0

    def min_notional_floor(self, venue_min_usd: float) -> float:
        """The binding per-order minimum: the venue's own, raised by the operator floor."""
        return max(venue_min_usd, self.min_notional_usd_floor)

    def check_venue_minimum(self, venue_min_usd: float, what: str = "venue minimum") -> None:
        """Refuse rather than size up when the smallest allowed order breaks the cap."""
        floor = self.min_notional_floor(venue_min_usd)
        if floor > self.max_notional_usd:
            raise LimitsError(
                f"{what} {floor:.4f} USD exceeds [order] max_notional_usd "
                f"{self.max_notional_usd:.4f} USD (hard cap {CAP_ORDER_NOTIONAL_USD:g}); "
                f"refusing to size up",
            )


@dataclass(frozen=True)
class InventoryLimits:
    max_inventory_usd: float = 60.0
    max_unhedged_usd: float = 15.0


@dataclass(frozen=True)
class ExposureLimits:
    max_total_notional_usd: float = 100.0


@dataclass(frozen=True)
class DailyLimits:
    max_fills: int = 200  # the base budget, in force unless the day has earned the extended one
    max_fills_profitable: int = 1_500
    # Realised net the day must clear before the extended budget unlocks.  Strictly positive:
    # a zero or negative gate would hand a flat day the larger budget.
    profit_gate_usd: float = 1.0
    max_tx: int = 20_000

    def fill_cap(self, *, unlocked: bool) -> int:
        """The fill budget in force, given whether the extended one has been unlocked today."""
        return self.max_fills_profitable if unlocked else self.max_fills


@dataclass(frozen=True)
class KillLimits:
    max_loss_usd: float = 5.0
    max_consecutive_losing_trips: int = 10
    min_trip_net_bps_ewma: float = -2.0
    max_hedge_fail_s: float = 30.0
    trip_window: int = 20  # round trips the EWMA spans; not operator-editable


@dataclass(frozen=True)
class QuoteLimits:
    edge_min_bps: float = 3.0
    reserve_bps: float = 0.0
    requote_min_s: float = 3.0
    tx_per_min: float = 36.0
    improve_ticks: int = 1
    # Where a quote rests: see src/quote_placement.py.  ``improve`` is what the maker ran
    # until 2026-09-09; ``anchor`` prices off the hedge mid instead of following the maker
    # touch, and ``anchor_edge_bps`` is how far off it rests.
    placement: str = "improve"
    anchor_edge_bps: float = 12.0
    # The anchor's fair mid: h_mid * (1 + median over basis_window_s of m_mid / h_mid - 1),
    # usable only once basis_min_n samples have arrived.  See src/quote_placement.py.
    basis_window_s: float = 300.0
    basis_min_n: int = 30
    # Lean both anchor prices by -anchor_skew_bps * (q / q_cap): a long book quotes lower.
    anchor_skew_bps: float = 0.0
    # The two opening gates (src/quote_gates.py).  They stop the side that would GROW the
    # inventory; the closing side keeps quoting, and the kill switch is untouched by them.
    # Spread gate: the maker touch spread, smoothed over spread_window_s, must be at least
    # min_spread_ratio times the hedge round-trip cost (hedge touch spread + both fees) AND at
    # least min_maker_spread_bps.
    min_spread_ratio: float = 2.0
    min_maker_spread_bps: float = 12.0
    spread_window_s: float = 10.0
    # Volatility gate: the maker mid's range over vol_window_s must stay under this.
    max_move_bps_per_min: float = 25.0
    vol_window_s: float = 60.0
    # When a closing clip would be under the maker venue's minimum order, quote it AT that
    # minimum instead of not quoting at all.  A full fill then carries the position through
    # zero by (venue_min - |q|), which is itself under the venue minimum.  False reproduces
    # the offline simulator, which never flips through zero.
    close_min_flip: bool = True

    @property
    def mode(self) -> str:
        """Deprecated alias for :attr:`placement`, kept for older call sites."""
        return self.placement

    @property
    def basis(self) -> BasisParams:
        """The estimator ``quote_placement.BasisTracker`` is built from."""
        return BasisParams(window_s=self.basis_window_s, min_n=self.basis_min_n)


@dataclass(frozen=True)
class HedgeLimits:
    venue: str = "ASTER"
    slippage_bps: float = 20.0
    aggregate_min_notional_usd: float = 5.0
    max_wait_s: float = 5.0

    @property
    def enabled(self) -> bool:
        return self.venue != "NONE"

    def aggregate_floor(self, venue_min_usd: float) -> float:
        """0 in the file means "use the venue's own min_notional"."""
        return self.aggregate_min_notional_usd or venue_min_usd


@dataclass(frozen=True)
class Limits:
    """Everything the live maker is allowed to do, already capped."""

    order: OrderLimits = field(default_factory=OrderLimits)
    inventory: InventoryLimits = field(default_factory=InventoryLimits)
    exposure: ExposureLimits = field(default_factory=ExposureLimits)
    daily: DailyLimits = field(default_factory=DailyLimits)
    kill: KillLimits = field(default_factory=KillLimits)
    quote: QuoteLimits = field(default_factory=QuoteLimits)
    hedge: HedgeLimits = field(default_factory=HedgeLimits)
    rows: tuple[Row, ...] = ()
    source: str = "built-in defaults (no file)"
    # Keys the file holds that no resolver read - see LimitsWarning.  Rendered by report().
    unexpected: tuple[str, ...] = ()

    @property
    def capped_rows(self) -> list[Row]:
        return [r for r in self.rows if r.capped]

    def effective_inventory_usd(self, *, hedged: bool = True) -> float:
        """The binding |inventory| cap: the tighter of [inventory] and the [exposure] share.

        Every maker fill is mirrored on the hedge venue, so a book carrying ``|q|`` USD of
        inventory shows ``2 * |q|`` USD of gross notional across the two venues - which is
        what ``[exposure] max_total_notional_usd`` measures.  The two configured numbers are
        therefore not independent: with the defaults (60 inventory, 100 exposure) a fully
        hedged 60 USD position is already 120 USD gross and trips the exposure kill during
        normal operation.  The tighter of the two wins, exactly as a hard cap does, so an
        operator can leave ``max_inventory_usd`` where it is and the quoting silently stays
        inside whatever the exposure cap really allows.
        """
        legs = 2.0 if hedged else 1.0
        return min(
            self.inventory.max_inventory_usd,
            EXPOSURE_HEADROOM * self.exposure.max_total_notional_usd / legs,
        )

    def report(self) -> str:
        """The dry-run table: configured vs hard cap vs what the strategy will use."""
        lines = [f"limits source: {self.source}", ""]
        lines += [r.line() for r in self.rows]
        capped = self.capped_rows
        lines.append("")
        if self.unexpected:
            lines.append(
                f"  !! {len(self.unexpected)} key(s) in the file were NOT read - almost "
                f"always a key under the wrong table header, which silently leaves the "
                f"built-in default in force:",
            )
            lines += [f"       {name}" for name in self.unexpected]
            lines.append("")
        if capped:
            lines.append(
                f"  {len(capped)} value(s) were LOWERED to their hard cap: "
                + ", ".join(r.key for r in capped),
            )
        else:
            lines.append("  every configured value is inside its hard cap")
        hedged = self.effective_inventory_usd(hedged=True)
        lines.append(
            f"  effective |inventory| cap when hedged: {hedged:g} USD  "
            f"= min(inventory.max_inventory_usd {self.inventory.max_inventory_usd:g}, "
            f"{EXPOSURE_HEADROOM:g} x exposure.max_total_notional_usd "
            f"{self.exposure.max_total_notional_usd:g} / 2 legs) - the hedge doubles the "
            f"gross notional, so the exposure cap binds first",
        )
        lines.append(
            f"  daily fill budget: {self.daily.max_fills} base (hard cap {CAP_DAILY_FILLS})"
            f"  ->  {self.daily.max_fills_profitable} once the day's realised net clears "
            f"{self.daily.profit_gate_usd:g} USD with a non-negative trip EWMA (hard cap "
            f"{CAP_DAILY_FILLS_PROFITABLE}); it re-locks when realised net falls back to 0",
        )
        lines.append(
            "  placement: "
            + describe_placement(
                self.quote.placement, self.quote.anchor_edge_bps,
                basis=self.quote.basis, skew_bps=self.quote.anchor_skew_bps,
            )
            + (f" (hard floor {FLOOR_ANCHOR_EDGE_BPS:g} bps)"
               if self.quote.placement == "anchor" else ""),
        )
        lines.append(
            f"  opening gates: spread >= max({self.quote.min_spread_ratio:g} x hedge "
            f"round-trip cost, {self.quote.min_maker_spread_bps:g} bps) on the median of the "
            f"last {self.quote.spread_window_s:g} s, and the maker mid range over "
            f"{self.quote.vol_window_s:g} s <= {self.quote.max_move_bps_per_min:g} bps; "
            f"hard floors {FLOOR_MIN_SPREAD_RATIO:g} / {FLOOR_MIN_MAKER_SPREAD_BPS:g} bps / "
            f"{FLOOR_MAX_MOVE_BPS_PER_MIN:g} bps (cap {CAP_MAX_MOVE_BPS_PER_MIN:g}). "
            f"They stop only the side that would GROW the inventory - the closing side keeps "
            f"quoting and the kill switch is unchanged",
        )
        lines.append(
            f"  NOTE  [daily] counts FILL EVENTS ({self.daily.max_fills} base, "
            f"{self.daily.max_fills_profitable} when profitable), not the 20 triggers/day of "
            f"PROMPT.md stage 3 - a maker prints many small fills, and a partially filled clip "
            f"counts once per partial. Deviation approved by the user 2026-09-08.",
        )
        return "\n".join(lines)


# ------------------------------------------------------------------ loader


def _placement(r: _Resolver) -> str:
    """``[quote] placement``, honouring the legacy ``improve_ticks = 0`` spelling of "join".

    ``improve_ticks`` predates the placement key and is the only thing that used to choose
    between the two rules, so a file that still says ``improve_ticks = 0`` and never mentions
    ``placement`` must keep resting at the touch.  An explicit ``placement`` always wins - it
    is the newer, more specific key - and ``improve_ticks`` is left in place as the cap it has
    always been (one tick, never more).
    """
    asked = r.word("quote", "placement", "improve", PLACEMENTS)
    ticks = r.count("quote", "improve_ticks", 1, 1, minimum=0)
    if ticks == 0 and asked == "improve":
        return "join"
    return asked


def _unexpected_keys(data: dict, asked: set[tuple[str, str]]) -> tuple[str, ...]:
    """Every key the file holds that no resolver asked for, as ``[table] key`` strings.

    This is the check that would have caught the misplaced gate block: the keys were under
    ``[hedge]``, the resolver only ever asked for them under ``[quote]``, and nothing said so.
    """
    out: list[str] = []
    for table, block in data.items():
        if not isinstance(block, dict):
            out.append(f"{table} (a value outside any table)")
            continue
        out += [f"[{table}] {key}" for key in block if (table, key) not in asked]
    return tuple(out)


def load_limits(path: Path | str | None = None, *, strict: bool = False) -> Limits:
    """Load ``config/limits.toml``; a missing file yields the built-in defaults.

    Every capped key passes through ``min(config, CAP)``: the file can only lower a limit.
    Raises :class:`LimitsError` when a present file is malformed - a limits file that cannot
    be parsed must stop the run, never silently fall back to something looser.

    Any key the file holds that no resolver read is reported: a :class:`LimitsWarning` by
    default (and printed by :meth:`Limits.report`, so the dry-run shows it), or a
    :class:`LimitsError` with ``strict=True``.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    rows: list[Row] = []
    if target.exists():
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise LimitsError(f"cannot read {target}: {exc}") from exc
        source = str(target)
    else:
        data = {}
        source = f"built-in defaults ({target} not found)"

    r = _Resolver(data, rows)
    order = OrderLimits(
        max_notional_usd=r.number(
            "order", "max_notional_usd", 20.0, CAP_ORDER_NOTIONAL_USD, minimum=0.01,
        ),
        min_notional_usd_floor=r.number("order", "min_notional_usd_floor", 0.0),
    )
    inventory = InventoryLimits(
        max_inventory_usd=r.number(
            "inventory", "max_inventory_usd", 60.0, CAP_INVENTORY_USD, minimum=0.01,
        ),
        max_unhedged_usd=r.number(
            "inventory", "max_unhedged_usd", 15.0, CAP_UNHEDGED_USD, minimum=0.0,
        ),
    )
    exposure = ExposureLimits(
        max_total_notional_usd=r.number(
            "exposure", "max_total_notional_usd", 100.0, CAP_TOTAL_NOTIONAL_USD, minimum=0.01,
        ),
    )
    daily = DailyLimits(
        max_fills=r.count("daily", "max_fills", 200, CAP_DAILY_FILLS, minimum=1),
        max_fills_profitable=r.count(
            "daily", "max_fills_profitable", 1_500, CAP_DAILY_FILLS_PROFITABLE, minimum=1,
        ),
        profit_gate_usd=r.number("daily", "profit_gate_usd", 1.0),
        max_tx=r.count("daily", "max_tx", 20_000, CAP_DAILY_TX, minimum=1),
    )
    kill = KillLimits(
        max_loss_usd=r.number("kill", "max_loss_usd", 5.0, CAP_LOSS_USD, minimum=0.0),
        max_consecutive_losing_trips=r.count(
            "kill", "max_consecutive_losing_trips", 10, minimum=1,
        ),
        min_trip_net_bps_ewma=r.number(
            "kill", "min_trip_net_bps_ewma", -2.0, allow_negative=True,
        ),
        max_hedge_fail_s=r.number("kill", "max_hedge_fail_s", 30.0, minimum=0.0),
    )
    placement = _placement(r)
    quote = QuoteLimits(
        edge_min_bps=r.number("quote", "edge_min_bps", 3.0, allow_negative=True),
        reserve_bps=r.number("quote", "reserve_bps", 0.0),
        requote_min_s=r.number("quote", "requote_min_s", 3.0),
        tx_per_min=r.number("quote", "tx_per_min", 36.0, CAP_TX_PER_MIN, minimum=1.0),
        placement=placement,
        improve_ticks=0 if placement == "join" else 1,
        anchor_edge_bps=r.number(
            "quote", "anchor_edge_bps", 12.0, floor=FLOOR_ANCHOR_EDGE_BPS,
        ),
        basis_window_s=r.number(
            "quote", "basis_window_s", 300.0, floor=FLOOR_BASIS_WINDOW_S,
        ),
        basis_min_n=r.count("quote", "basis_min_n", 30, minimum=FLOOR_BASIS_MIN_N),
        anchor_skew_bps=r.number("quote", "anchor_skew_bps", 0.0),
        close_min_flip=r.flag("quote", "close_min_flip", True),
        min_spread_ratio=r.number(
            "quote", "min_spread_ratio", 2.0, floor=FLOOR_MIN_SPREAD_RATIO,
        ),
        min_maker_spread_bps=r.number(
            "quote", "min_maker_spread_bps", 12.0, floor=FLOOR_MIN_MAKER_SPREAD_BPS,
        ),
        spread_window_s=r.number(
            "quote", "spread_window_s", 10.0, floor=FLOOR_GATE_WINDOW_S,
        ),
        max_move_bps_per_min=r.number(
            "quote", "max_move_bps_per_min", 25.0, CAP_MAX_MOVE_BPS_PER_MIN,
            floor=FLOOR_MAX_MOVE_BPS_PER_MIN,
        ),
        vol_window_s=r.number("quote", "vol_window_s", 60.0, floor=FLOOR_GATE_WINDOW_S),
    )
    hedge = HedgeLimits(
        venue=r.text("hedge", "venue", "ASTER", ("ASTER", "NONE")),
        slippage_bps=r.number("hedge", "slippage_bps", 20.0),
        aggregate_min_notional_usd=r.number("hedge", "aggregate_min_notional_usd", 5.0),
        max_wait_s=r.number("hedge", "max_wait_s", 5.0),
    )

    # Cross-checks: a set of individually legal numbers can still be nonsense together.
    if order.max_notional_usd > inventory.max_inventory_usd:
        raise LimitsError(
            f"[order] max_notional_usd {order.max_notional_usd:g} exceeds [inventory] "
            f"max_inventory_usd {inventory.max_inventory_usd:g}: one clip would breach the cap",
        )
    if daily.profit_gate_usd <= 0.0:
        raise LimitsError(
            f"[daily] profit_gate_usd must be strictly positive, got "
            f"{daily.profit_gate_usd:g}: a zero or negative gate would hand the extended fill "
            f"budget to a flat or losing day",
        )
    if daily.max_fills_profitable < daily.max_fills:
        raise LimitsError(
            f"[daily] max_fills_profitable {daily.max_fills_profitable} is below max_fills "
            f"{daily.max_fills}: the profitable budget can only ever be the larger one",
        )
    if inventory.max_inventory_usd > exposure.max_total_notional_usd:
        raise LimitsError(
            f"[inventory] max_inventory_usd {inventory.max_inventory_usd:g} exceeds "
            f"[exposure] max_total_notional_usd {exposure.max_total_notional_usd:g}",
        )

    unexpected = _unexpected_keys(data, r.asked)
    if unexpected:
        detail = ", ".join(unexpected)
        message = (
            f"{target}: {len(unexpected)} key(s) were not read: {detail}. A key under the "
            f"wrong table header parses fine and then does nothing - the built-in default "
            f"stays in force.  Move it under the table the loader reads it from."
        )
        if strict:
            raise LimitsError(message)
        warnings.warn(message, LimitsWarning, stacklevel=2)

    return Limits(
        order=order,
        inventory=inventory,
        exposure=exposure,
        daily=daily,
        kill=kill,
        quote=quote,
        hedge=hedge,
        rows=tuple(rows),
        source=source,
        unexpected=unexpected,
    )


if __name__ == "__main__":  # pragma: no cover - convenience
    import sys

    print(load_limits(Path(sys.argv[1]) if len(sys.argv) > 1 else None).report())
