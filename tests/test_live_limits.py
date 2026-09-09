#!/usr/bin/env python3
"""
Offline regression tests for ``src/live_limits.py`` (operator limits + hard caps).

No network, no credentials, no LiveNode: every test writes a small TOML into a temporary
directory and checks what the loader made of it.  The property under test throughout is that
configuration can only ever LOWER a limit.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
import warnings
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import live_limits  # noqa: E402
from live_limits import CAP_DAILY_FILLS  # noqa: E402
from live_limits import CAP_DAILY_FILLS_PROFITABLE  # noqa: E402
from live_limits import CAP_DAILY_TX  # noqa: E402
from live_limits import CAP_INVENTORY_USD  # noqa: E402
from live_limits import CAP_LOSS_USD  # noqa: E402
from live_limits import CAP_ORDER_NOTIONAL_USD  # noqa: E402
from live_limits import CAP_TOTAL_NOTIONAL_USD  # noqa: E402
from live_limits import CAP_TX_PER_MIN  # noqa: E402
from live_limits import CAP_MAX_MOVE_BPS_PER_MIN  # noqa: E402
from live_limits import CAP_UNHEDGED_USD  # noqa: E402
from live_limits import FLOOR_ANCHOR_EDGE_BPS  # noqa: E402
from live_limits import FLOOR_GATE_WINDOW_S  # noqa: E402
from live_limits import FLOOR_MAX_MOVE_BPS_PER_MIN  # noqa: E402
from live_limits import FLOOR_MIN_MAKER_SPREAD_BPS  # noqa: E402
from live_limits import FLOOR_MIN_SPREAD_RATIO  # noqa: E402
from live_limits import EXPOSURE_HEADROOM  # noqa: E402
from live_limits import LimitsError  # noqa: E402
from live_limits import LimitsWarning  # noqa: E402
from live_limits import load_limits  # noqa: E402


REPO = Path(__file__).resolve().parents[1]


def write_toml(text: str) -> Path:
    """Write ``text`` to a fresh temporary limits file and return its path."""
    directory = Path(tempfile.mkdtemp(prefix="limits-"))
    path = directory / "limits.toml"
    path.write_text(text, encoding="utf-8")
    return path


def quote_toml(*lines: str) -> Path:
    """A temporary limits file holding nothing but a ``[quote]`` section."""
    return write_toml("\n".join(("[quote]", *lines)) + "\n")


# --------------------------------------------------------------------------- defaults


class TestDefaults(unittest.TestCase):
    """A missing file must produce the built-in defaults, all inside their caps."""

    def test_missing_file_falls_back_to_defaults(self) -> None:
        limits = load_limits(Path(tempfile.mkdtemp()) / "nope.toml")
        self.assertEqual(limits.order.max_notional_usd, 20.0)
        self.assertEqual(limits.inventory.max_inventory_usd, 60.0)
        self.assertEqual(limits.inventory.max_unhedged_usd, 15.0)
        self.assertEqual(limits.exposure.max_total_notional_usd, 100.0)
        self.assertEqual(limits.daily.max_fills, 200)  # fallback stays under the shipped 300
        self.assertEqual(limits.daily.max_tx, 20_000)
        self.assertEqual(limits.kill.max_loss_usd, 5.0)
        self.assertEqual(limits.quote.tx_per_min, 36.0)
        self.assertEqual(limits.hedge.venue, "ASTER")
        self.assertIn("not found", limits.source)

    def test_every_default_is_inside_its_cap(self) -> None:
        limits = load_limits(Path(tempfile.mkdtemp()) / "nope.toml")
        self.assertEqual(limits.capped_rows, [])

    def test_repo_config_loads_and_is_inside_every_cap(self) -> None:
        """The file actually shipped in the repo must load and never need capping."""
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertEqual([r.key for r in limits.capped_rows], [])
        self.assertLessEqual(limits.order.max_notional_usd, CAP_ORDER_NOTIONAL_USD)
        self.assertLessEqual(limits.quote.tx_per_min, CAP_TX_PER_MIN)

    def test_report_flags_the_prompt_deviation(self) -> None:
        report = load_limits(REPO / "config" / "limits.toml").report()
        self.assertIn("20 triggers/day", report)
        self.assertIn("FILL EVENTS", report)
        self.assertIn("approved by the user", report)

    def test_shipped_config_carries_the_profit_gated_budget(self) -> None:
        """300 base, 1500 once the day has made more than 1 USD; hard caps 500 / 3000."""
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertEqual(limits.daily.max_fills, 300)
        self.assertEqual(limits.daily.max_fills_profitable, 1500)
        self.assertEqual(limits.daily.profit_gate_usd, 1.0)
        self.assertEqual(CAP_DAILY_FILLS_PROFITABLE, 3000)
        self.assertLess(limits.daily.max_fills_profitable, CAP_DAILY_FILLS_PROFITABLE)

    def test_fill_cap_selects_the_budget(self) -> None:
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertEqual(limits.daily.fill_cap(unlocked=False), 300)
        self.assertEqual(limits.daily.fill_cap(unlocked=True), 1500)

    def test_shipped_config_carries_a_full_day_of_transactions(self) -> None:
        """~23 tx/min observed x 24 h is ~33k, so 20k would stop a healthy session early."""
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertEqual(limits.daily.max_tx, 40_000)
        self.assertEqual(CAP_DAILY_TX, 60_000)
        self.assertGreater(limits.daily.max_tx, 23 * 60 * 24)

    def test_config_cannot_raise_the_profitable_budget_past_its_cap(self) -> None:
        limits = load_limits(write_toml("[daily]\nmax_fills_profitable = 999999\n"))
        self.assertEqual(limits.daily.max_fills_profitable, CAP_DAILY_FILLS_PROFITABLE)
        self.assertIn("daily.max_fills_profitable",
                      [r.key for r in limits.capped_rows])

    def test_config_cannot_raise_the_transaction_budget_past_its_cap(self) -> None:
        limits = load_limits(write_toml("[daily]\nmax_tx = 999999\n"))
        self.assertEqual(limits.daily.max_tx, CAP_DAILY_TX)

    def test_the_profit_gate_must_be_strictly_positive(self) -> None:
        """A zero or negative gate would hand the extended budget to a flat day."""
        for value in ("0.0", "-1.0", "-0.0001"):
            with self.subTest(value=value), self.assertRaises(LimitsError):
                load_limits(write_toml(f"[daily]\nprofit_gate_usd = {value}\n"))
        self.assertEqual(
            load_limits(write_toml("[daily]\nprofit_gate_usd = 0.01\n")).daily.profit_gate_usd,
            0.01,
        )

    def test_the_profitable_budget_cannot_be_smaller_than_the_base(self) -> None:
        with self.assertRaises(LimitsError):
            load_limits(write_toml("[daily]\nmax_fills = 300\nmax_fills_profitable = 100\n"))

    def test_the_report_states_both_budgets_and_the_gate(self) -> None:
        report = load_limits(REPO / "config" / "limits.toml").report()
        self.assertIn("daily fill budget: 300 base", report)
        self.assertIn("1500 once the day", report)
        self.assertIn("re-locks", report)

    def test_shipped_config_carries_the_approved_fill_budget(self) -> None:
        """The user approved 300/day on 2026-09-08; the hard cap stays 500."""
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertEqual(limits.daily.max_fills, 300)
        self.assertEqual(CAP_DAILY_FILLS, 500)
        self.assertLess(limits.daily.max_fills, CAP_DAILY_FILLS)

    def test_close_min_flip_is_on_in_the_shipped_config(self) -> None:
        self.assertTrue(load_limits(REPO / "config" / "limits.toml").quote.close_min_flip)

    def test_close_min_flip_can_be_turned_off(self) -> None:
        limits = load_limits(write_toml("[quote]\nclose_min_flip = false\n"))
        self.assertFalse(limits.quote.close_min_flip)

    def test_close_min_flip_must_be_a_boolean(self) -> None:
        with self.assertRaises(LimitsError):
            load_limits(write_toml('[quote]\nclose_min_flip = "yes"\n'))


# --------------------------------------------------------------------------- shipped file


class TestShippedConfig(unittest.TestCase):
    """What ``config/limits.toml`` actually says must be what the loader actually uses.

    The bug this class exists for: from 2026-09-09 to 2026-09-10 the whole opening-gate block
    sat BELOW the ``[hedge]`` header, because it had been appended to the end of the file.
    ``tomllib`` read it as ``hedge.min_spread_ratio`` and friends, ``r.number("quote", ...)``
    never found it, and every run used the built-in defaults 2 / 12 / 10 / 25 / 60 while the
    file said 1.0 / 5 / 10 / 50 / 60.  Nothing complained.

    So these tests do not hard-code the shipped numbers where they can avoid it: they parse
    the file independently with ``tomllib`` and require the loaded value to equal the parsed
    one, key by key.  A key that drifts under the wrong table fails here.
    """

    SHIPPED = REPO / "config" / "limits.toml"

    @classmethod
    def parsed(cls) -> dict:
        """The file as tomllib sees it - the loader's input, not its output."""
        return tomllib.loads(cls.SHIPPED.read_text(encoding="utf-8"))

    # Every [quote] key, and the QuoteLimits attribute the loader must land it on.  Listed
    # explicitly so a new key has to be added here too.
    QUOTE_KEYS = (
        "edge_min_bps", "reserve_bps", "requote_min_s", "tx_per_min", "improve_ticks",
        "placement", "anchor_edge_bps", "basis_window_s", "basis_min_n", "anchor_skew_bps",
        "close_min_flip", "min_spread_ratio", "min_maker_spread_bps", "spread_window_s",
        "max_move_bps_per_min", "vol_window_s",
    )

    def test_every_quote_key_in_the_file_reaches_the_loader(self) -> None:
        quote = load_limits(self.SHIPPED).quote
        block = self.parsed()["quote"]
        for key in self.QUOTE_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, block, f"[quote] {key} is missing from the shipped file")
                got = getattr(quote, key)
                want = block[key]
                if isinstance(got, float):
                    self.assertAlmostEqual(got, float(want), places=9)
                else:
                    self.assertEqual(got, want)

    def test_the_five_gate_keys_are_under_quote_and_not_under_hedge(self) -> None:
        """The exact shape of the 2026-09-10 bug, asserted directly."""
        data = self.parsed()
        gates = ("min_spread_ratio", "min_maker_spread_bps", "spread_window_s",
                 "max_move_bps_per_min", "vol_window_s")
        for key in gates:
            with self.subTest(key=key):
                self.assertIn(key, data["quote"])
                self.assertNotIn(key, data["hedge"])

    def test_the_shipped_file_has_no_key_the_loader_ignores(self) -> None:
        limits = load_limits(self.SHIPPED)
        self.assertEqual(limits.unexpected, ())
        load_limits(self.SHIPPED, strict=True)  # must not raise

    def test_no_quote_key_silently_falls_back_to_a_built_in_default(self) -> None:
        """A misplaced key looks exactly like "the file did not mention it", so compare the
        shipped load against the no-file load and require the file to have MOVED something."""
        shipped = load_limits(self.SHIPPED).quote
        builtin = load_limits(Path(tempfile.mkdtemp()) / "nope.toml").quote
        differing = [k for k in self.QUOTE_KEYS
                     if getattr(shipped, k) != getattr(builtin, k)]
        self.assertTrue(differing, "the shipped file changes nothing - is it being read?")

    def test_what_the_file_ships_today(self) -> None:
        """The current operating point, so a change to it is a deliberate edit here too."""
        quote = load_limits(self.SHIPPED).quote
        self.assertEqual(quote.placement, "anchor")
        self.assertEqual(quote.anchor_edge_bps, 20.0)
        self.assertEqual(quote.anchor_skew_bps, 0.0)
        self.assertEqual(quote.basis_window_s, 300.0)
        self.assertEqual(quote.basis_min_n, 30)
        self.assertEqual(quote.min_spread_ratio, 1.0)
        self.assertEqual(quote.min_maker_spread_bps, 5.0)
        self.assertEqual(quote.spread_window_s, 10.0)
        self.assertEqual(quote.max_move_bps_per_min, 50.0)
        self.assertEqual(quote.vol_window_s, 60.0)


class TestMisplacedKeys(unittest.TestCase):
    """A key under a table the loader never reads it from must be reported, not ignored."""

    @staticmethod
    def misplaced() -> Path:
        """The 2026-09-10 file: the gate block appended below the [hedge] header."""
        return write_toml("\n".join((
            "[quote]", "edge_min_bps = 3.0",
            "[hedge]", 'venue = "ASTER"',
            "min_spread_ratio = 1.0", "min_maker_spread_bps = 5.0",
            "max_move_bps_per_min = 50.0", "",
        )))

    def test_a_misplaced_key_is_named_rather_than_ignored(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            limits = load_limits(self.misplaced())
        self.assertEqual(
            set(limits.unexpected),
            {"[hedge] min_spread_ratio", "[hedge] min_maker_spread_bps",
             "[hedge] max_move_bps_per_min"},
        )
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, LimitsWarning)
        self.assertIn("[hedge] min_spread_ratio", str(caught[0].message))

    def test_the_misplaced_keys_did_not_reach_the_gates(self) -> None:
        """The point of the warning: the run silently used the defaults, not the file."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LimitsWarning)
            quote = load_limits(self.misplaced()).quote
        self.assertEqual(quote.min_spread_ratio, 2.0)  # the built-in, not the file's 1.0
        self.assertEqual(quote.max_move_bps_per_min, 25.0)  # not the file's 50.0

    def test_strict_refuses_to_load_at_all(self) -> None:
        with self.assertRaises(LimitsError) as caught:
            load_limits(self.misplaced(), strict=True)
        self.assertIn("[hedge] min_spread_ratio", str(caught.exception))

    def test_an_outright_unknown_key_is_reported_too(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            limits = load_limits(quote_toml("edge_min_bps = 3.0", "typo_here = 1"))
        self.assertEqual(limits.unexpected, ("[quote] typo_here",))

    def test_the_report_shows_them_so_the_dry_run_does(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LimitsWarning)
            report = load_limits(self.misplaced()).report()
        self.assertIn("were NOT read", report)
        self.assertIn("[hedge] min_spread_ratio", report)

    def test_a_clean_file_warns_about_nothing(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            limits = load_limits(quote_toml("edge_min_bps = 3.0"))
        self.assertEqual(limits.unexpected, ())
        self.assertEqual(caught, [])


# --------------------------------------------------------------------------- placement


class TestPlacement(unittest.TestCase):
    """``[quote] placement`` and the anchor edge, plus the legacy ``improve_ticks`` spelling."""

    def test_the_mode_alias_follows_the_placement(self) -> None:
        quote = load_limits(quote_toml('placement = "anchor"')).quote
        self.assertEqual(quote.mode, quote.placement)

    def test_every_placement_can_be_asked_for(self) -> None:
        for name in ("improve", "join", "anchor"):
            with self.subTest(name=name):
                limits = load_limits(quote_toml(f'placement = "{name}"'))
                self.assertEqual(limits.quote.placement, name)

    def test_an_unknown_placement_is_refused(self) -> None:
        with self.assertRaises(LimitsError):
            load_limits(quote_toml('placement = "peg"'))

    def test_the_placement_is_case_insensitive(self) -> None:
        self.assertEqual(load_limits(quote_toml('placement = "ANCHOR"')).quote.placement,
                         "anchor")

    def test_the_legacy_improve_ticks_zero_still_means_join(self) -> None:
        """An old file that never heard of `placement` must keep resting at the touch."""
        limits = load_limits(quote_toml("improve_ticks = 0"))
        self.assertEqual(limits.quote.placement, "join")

    def test_an_explicit_placement_wins_over_improve_ticks(self) -> None:
        limits = load_limits(quote_toml('placement = "anchor"', "improve_ticks = 0"))
        self.assertEqual(limits.quote.placement, "anchor")

    def test_the_anchor_edge_may_be_widened_but_not_narrowed(self) -> None:
        limits = load_limits(quote_toml("anchor_edge_bps = 25.0"))
        self.assertEqual(limits.quote.anchor_edge_bps, 25.0)
        with self.assertRaises(LimitsError):
            load_limits(quote_toml("anchor_edge_bps = 2.9"))
        edge = load_limits(quote_toml("anchor_edge_bps = 3.0")).quote.anchor_edge_bps
        self.assertEqual(edge, FLOOR_ANCHOR_EDGE_BPS)

    def test_the_basis_params_are_built_from_the_two_keys(self) -> None:
        quote = load_limits(quote_toml("basis_window_s = 120", "basis_min_n = 40")).quote
        self.assertEqual(quote.basis.window_s, 120.0)
        self.assertEqual(quote.basis.min_n, 40)
        self.assertTrue(quote.basis.on)

    def test_the_basis_estimate_may_be_lengthened_but_not_shortened(self) -> None:
        """A longer window and more samples are the safer settings, so both are floors -
        and there is no value that switches the estimate off: the 09-07 / 09-08 replays
        showed an anchor on the raw hedge mid is one-sided on this pair."""
        limits = load_limits(quote_toml("basis_window_s = 900", "basis_min_n = 120"))
        self.assertEqual(limits.quote.basis_window_s, 900.0)
        self.assertEqual(limits.quote.basis_min_n, 120)
        for line in ("basis_window_s = 0", "basis_window_s = 9.9", "basis_min_n = 0",
                     "basis_min_n = 9"):
            with self.subTest(line=line), self.assertRaises(LimitsError):
                load_limits(quote_toml(line))

    def test_the_inventory_skew_is_off_by_default_and_can_be_set(self) -> None:
        self.assertEqual(load_limits(quote_toml("anchor_skew_bps = 8.0")).quote.anchor_skew_bps,
                         8.0)
        with self.assertRaises(LimitsError):
            load_limits(quote_toml("anchor_skew_bps = -1.0"))

    def test_the_report_names_the_placement(self) -> None:
        report = load_limits(quote_toml('placement = "anchor"')).report()
        self.assertIn("quote.placement", report)
        self.assertIn("quote.anchor_edge_bps", report)
        self.assertIn("anchor 12 bps off a fair mid", report)
        self.assertIn("basis: median of 300 s, at least 30 samples", report)


# --------------------------------------------------------------------------- gates


class TestOpeningGates(unittest.TestCase):
    """``[quote]`` opening gates: the file may tighten one, never switch one off.

    Their rail is a FLOOR rather than a cap, because for these keys a SMALLER number is the
    looser one, and a value under the floor is refused rather than silently raised - an
    operator who wrote 0.5 must be told the run will not do that, not have it corrected.
    """

    def test_the_built_in_defaults_still_gate(self) -> None:
        """A run with no file at all must still gate, at the built-in numbers."""
        quote = load_limits(Path(tempfile.mkdtemp()) / "nope.toml").quote
        self.assertEqual(quote.min_spread_ratio, 2.0)
        self.assertEqual(quote.min_maker_spread_bps, 12.0)
        self.assertEqual(quote.max_move_bps_per_min, 25.0)

    def test_the_file_may_tighten_every_gate(self) -> None:
        limits = load_limits(quote_toml(
            "min_spread_ratio = 3.5",
            "min_maker_spread_bps = 20.0",
            "max_move_bps_per_min = 10.0",
            "spread_window_s = 30",
            "vol_window_s = 120",
        ))
        self.assertEqual(limits.quote.min_spread_ratio, 3.5)
        self.assertEqual(limits.quote.min_maker_spread_bps, 20.0)
        self.assertEqual(limits.quote.max_move_bps_per_min, 10.0)
        self.assertEqual(limits.quote.spread_window_s, 30.0)
        self.assertEqual(limits.quote.vol_window_s, 120.0)

    def test_a_spread_ratio_under_the_floor_is_refused(self) -> None:
        with self.assertRaises(LimitsError) as caught:
            load_limits(quote_toml("min_spread_ratio = 0.5"))
        self.assertIn("hard floor", str(caught.exception))

    def test_the_spread_gate_cannot_be_switched_off(self) -> None:
        for key in ("min_spread_ratio", "min_maker_spread_bps"):
            with self.subTest(key=key), self.assertRaises(LimitsError):
                load_limits(quote_toml(f"{key} = 0.0"))

    def test_a_maker_spread_floor_under_the_hard_floor_is_refused(self) -> None:
        with self.assertRaises(LimitsError):
            load_limits(quote_toml("min_maker_spread_bps = 4.9"))
        limits = load_limits(quote_toml("min_maker_spread_bps = 5.0"))
        self.assertEqual(limits.quote.min_maker_spread_bps, FLOOR_MIN_MAKER_SPREAD_BPS)

    def test_a_volatility_limit_under_the_hard_floor_is_refused(self) -> None:
        """Below 5 bps of range the gate would never open, which is not a usable run."""
        with self.assertRaises(LimitsError):
            load_limits(quote_toml("max_move_bps_per_min = 1.0"))

    def test_the_volatility_limit_is_also_capped(self) -> None:
        """For THIS knob a larger number is the looser one, so it needs a cap as well."""
        limits = load_limits(quote_toml("max_move_bps_per_min = 5000.0"))
        self.assertEqual(limits.quote.max_move_bps_per_min, CAP_MAX_MOVE_BPS_PER_MIN)

    def test_a_window_must_hold_at_least_a_second(self) -> None:
        for key in ("spread_window_s", "vol_window_s"):
            with self.subTest(key=key), self.assertRaises(LimitsError):
                load_limits(quote_toml(f"{key} = 0"))

    def test_the_floors_are_module_constants(self) -> None:
        self.assertEqual(FLOOR_MIN_SPREAD_RATIO, 1.0)
        self.assertEqual(FLOOR_MIN_MAKER_SPREAD_BPS, 5.0)
        self.assertEqual(FLOOR_MAX_MOVE_BPS_PER_MIN, 5.0)
        self.assertEqual(FLOOR_GATE_WINDOW_S, 1.0)

    def test_the_report_shows_the_gates_and_their_rails(self) -> None:
        report = load_limits(REPO / "config" / "limits.toml").report()
        self.assertIn("quote.min_spread_ratio", report)
        self.assertIn("quote.max_move_bps_per_min", report)
        self.assertIn(">=1", report)  # the floor is rendered in the cap column
        self.assertIn("opening gates:", report)


# --------------------------------------------------------------------------- lowering


class TestConfigLowers(unittest.TestCase):
    """A smaller number in the file must be honoured exactly."""

    def test_config_lowers_every_capped_key(self) -> None:
        path = write_toml(
            """
            [order]
            max_notional_usd = 7.5
            [inventory]
            max_inventory_usd = 30.0
            max_unhedged_usd = 4.0
            [exposure]
            max_total_notional_usd = 40.0
            [daily]
            max_fills = 12
            max_tx = 900
            [kill]
            max_loss_usd = 1.5
            [quote]
            tx_per_min = 10.0
            """,
        )
        limits = load_limits(path)
        self.assertEqual(limits.order.max_notional_usd, 7.5)
        self.assertEqual(limits.inventory.max_inventory_usd, 30.0)
        self.assertEqual(limits.inventory.max_unhedged_usd, 4.0)
        self.assertEqual(limits.exposure.max_total_notional_usd, 40.0)
        self.assertEqual(limits.daily.max_fills, 12)
        self.assertEqual(limits.daily.max_tx, 900)
        self.assertEqual(limits.kill.max_loss_usd, 1.5)
        self.assertEqual(limits.quote.tx_per_min, 10.0)
        self.assertEqual(limits.capped_rows, [])

    def test_partial_file_keeps_defaults_for_absent_keys(self) -> None:
        path = write_toml("[order]\nmax_notional_usd = 9.0\n")
        limits = load_limits(path)
        self.assertEqual(limits.order.max_notional_usd, 9.0)
        self.assertEqual(limits.inventory.max_inventory_usd, 60.0)  # default survives
        self.assertEqual(limits.daily.max_fills, 200)  # fallback stays under the shipped 300


# --------------------------------------------------------------------------- raising


class TestConfigCannotRaise(unittest.TestCase):
    """The whole point: no value in the file may widen a hard cap."""

    def test_absurd_values_are_all_clamped_to_the_caps(self) -> None:
        path = write_toml(
            """
            [order]
            max_notional_usd = 100000.0
            [inventory]
            max_inventory_usd = 100000.0
            max_unhedged_usd = 100000.0
            [exposure]
            max_total_notional_usd = 100000.0
            [daily]
            max_fills = 10000000
            max_tx = 10000000
            [kill]
            max_loss_usd = 100000.0
            [quote]
            tx_per_min = 100000.0
            improve_ticks = 99
            """,
        )
        limits = load_limits(path)
        self.assertEqual(limits.order.max_notional_usd, CAP_ORDER_NOTIONAL_USD)
        self.assertEqual(limits.inventory.max_inventory_usd, CAP_INVENTORY_USD)
        self.assertEqual(limits.inventory.max_unhedged_usd, CAP_UNHEDGED_USD)
        self.assertEqual(limits.exposure.max_total_notional_usd, CAP_TOTAL_NOTIONAL_USD)
        self.assertEqual(limits.daily.max_fills, CAP_DAILY_FILLS)
        self.assertEqual(limits.daily.max_tx, CAP_DAILY_TX)
        self.assertEqual(limits.kill.max_loss_usd, CAP_LOSS_USD)
        self.assertEqual(limits.quote.tx_per_min, CAP_TX_PER_MIN)
        self.assertEqual(limits.quote.improve_ticks, 1)

    def test_capped_keys_are_named_in_the_report(self) -> None:
        path = write_toml("[quote]\ntx_per_min = 999.0\n")
        limits = load_limits(path)
        keys = [row.key for row in limits.capped_rows]
        self.assertIn("quote.tx_per_min", keys)
        self.assertIn("CAPPED", limits.report())

    def test_tx_per_min_can_never_exceed_the_venue_budget(self) -> None:
        """Lighter allows 40 sendTx/min per L1 address; the file must not be able to ask for more."""
        for asked in (41.0, 60.0, 1e9):
            with self.subTest(asked=asked):
                limits = load_limits(write_toml(f"[quote]\ntx_per_min = {asked}\n"))
                self.assertLessEqual(limits.quote.tx_per_min, 40.0)


# --------------------------------------------------------------------------- validation


class TestValidation(unittest.TestCase):
    """A limits file that cannot be trusted must stop the run, not degrade to something looser."""

    def test_malformed_toml_raises(self) -> None:
        path = write_toml("[order\nmax_notional_usd = 1\n")
        with self.assertRaises(LimitsError):
            load_limits(path)

    def test_wrong_type_raises(self) -> None:
        path = write_toml('[order]\nmax_notional_usd = "twenty"\n')
        with self.assertRaises(LimitsError):
            load_limits(path)

    def test_negative_value_raises(self) -> None:
        path = write_toml("[order]\nmax_notional_usd = -5.0\n")
        with self.assertRaises(LimitsError):
            load_limits(path)

    def test_unknown_hedge_venue_raises(self) -> None:
        path = write_toml('[hedge]\nvenue = "BINANCE"\n')
        with self.assertRaises(LimitsError):
            load_limits(path)

    def test_clip_larger_than_the_inventory_cap_raises(self) -> None:
        path = write_toml(
            "[order]\nmax_notional_usd = 40.0\n[inventory]\nmax_inventory_usd = 10.0\n",
        )
        with self.assertRaises(LimitsError):
            load_limits(path)

    def test_inventory_above_the_exposure_cap_raises(self) -> None:
        path = write_toml(
            "[inventory]\nmax_inventory_usd = 90.0\n"
            "[exposure]\nmax_total_notional_usd = 50.0\n",
        )
        with self.assertRaises(LimitsError):
            load_limits(path)

    def test_a_negative_ewma_floor_is_allowed(self) -> None:
        """min_trip_net_bps_ewma is legitimately negative and must not trip the sign check."""
        limits = load_limits(write_toml("[kill]\nmin_trip_net_bps_ewma = -7.5\n"))
        self.assertEqual(limits.kill.min_trip_net_bps_ewma, -7.5)


# --------------------------------------------------------------------------- derived


class TestDerivedLimits(unittest.TestCase):
    """The numbers the strategy actually quotes against."""

    def test_effective_inventory_is_capped_by_the_exposure_share(self) -> None:
        """A hedged book shows 2x its inventory as gross notional, so [exposure] binds."""
        limits = load_limits(REPO / "config" / "limits.toml")
        hedged = limits.effective_inventory_usd(hedged=True)
        self.assertAlmostEqual(
            hedged, EXPOSURE_HEADROOM * limits.exposure.max_total_notional_usd / 2.0,
        )
        self.assertLess(hedged, limits.inventory.max_inventory_usd)
        # And the resulting gross exposure really does stay under the cap.
        self.assertLess(2.0 * hedged, limits.exposure.max_total_notional_usd)

    def test_effective_inventory_unhedged_uses_the_whole_exposure_cap(self) -> None:
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertEqual(
            limits.effective_inventory_usd(hedged=False),
            limits.inventory.max_inventory_usd,  # 60 < 0.9 * 100
        )

    def test_effective_inventory_still_honours_a_small_configured_inventory(self) -> None:
        limits = load_limits(
            write_toml(
                "[order]\nmax_notional_usd = 4.0\n[inventory]\nmax_inventory_usd = 8.0\n",
            ),
        )
        self.assertEqual(limits.effective_inventory_usd(hedged=True), 8.0)

    def test_venue_minimum_above_the_order_cap_is_refused(self) -> None:
        """Refuse rather than size up, exactly as exec_probe.plan_order does."""
        limits = load_limits(write_toml("[order]\nmax_notional_usd = 12.0\n"))
        limits.order.check_venue_minimum(10.0)  # fine
        with self.assertRaises(LimitsError):
            limits.order.check_venue_minimum(15.0)

    def test_operator_floor_raises_the_minimum_but_not_the_cap(self) -> None:
        limits = load_limits(
            write_toml("[order]\nmax_notional_usd = 20.0\nmin_notional_usd_floor = 14.0\n"),
        )
        self.assertEqual(limits.order.min_notional_floor(5.0), 14.0)
        self.assertEqual(limits.order.min_notional_floor(18.0), 18.0)
        with self.assertRaises(LimitsError):
            limits.order.check_venue_minimum(21.0)

    def test_quote_mode_follows_improve_ticks(self) -> None:
        self.assertEqual(load_limits(write_toml("[quote]\nimprove_ticks = 1\n")).quote.mode,
                         "improve")
        self.assertEqual(load_limits(write_toml("[quote]\nimprove_ticks = 0\n")).quote.mode,
                         "join")

    def test_hedge_aggregate_floor_defaults_to_the_venue_minimum(self) -> None:
        limits = load_limits(write_toml("[hedge]\naggregate_min_notional_usd = 0.0\n"))
        self.assertEqual(limits.hedge.aggregate_floor(5.0), 5.0)
        limits = load_limits(write_toml("[hedge]\naggregate_min_notional_usd = 9.0\n"))
        self.assertEqual(limits.hedge.aggregate_floor(5.0), 9.0)

    def test_hedge_none_disables_hedging(self) -> None:
        limits = load_limits(write_toml('[hedge]\nvenue = "none"\n'))
        self.assertEqual(limits.hedge.venue, "NONE")
        self.assertFalse(limits.hedge.enabled)


# --------------------------------------------------------------------------- hard caps


class TestHardCapsAreConstants(unittest.TestCase):
    """The caps must live in code, where widening them is a source edit and a review."""

    def test_caps_match_prompt_md_stage_three(self) -> None:
        self.assertEqual(CAP_ORDER_NOTIONAL_USD, 50.0)  # PROMPT.md: single order <= 50 USD
        self.assertEqual(CAP_TOTAL_NOTIONAL_USD, 100.0)  # PROMPT.md: exposure <= 100 USD

    def test_no_environment_variable_can_move_a_cap(self) -> None:
        """There is no env-var path into the caps at all - they are plain module constants."""
        source = (REPO / "src" / "live_limits.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_loader_applies_min_not_max(self) -> None:
        """Guard against the classic inversion: min(config, CAP), never max."""
        source = (REPO / "src" / "live_limits.py").read_text(encoding="utf-8")
        self.assertIn("min(value, cap)", source)
        self.assertIn("min(raw, cap)", source)
        self.assertNotIn("max(value, cap)", source)
        self.assertNotIn("max(raw, cap)", source)


if __name__ == "__main__":
    unittest.main()
