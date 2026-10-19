"""Complete proofs stay conservative while full-universe validation scales linearly."""
from collections import Counter
from dataclasses import asdict, replace
from decimal import Decimal as D

import pytest

from test_opportunity_trade import NOW, SECOND, make_plan
from opportunity_live import LiveTradeState
from opportunity_preflight import AccountSnapshot
from opportunity_trade import TradePolicy


def empty_state(symbols=("BTC", "ETH"), venues=("HL", "ASTER")):
    return LiveTradeState(make_plan(symbols=symbols, venues=venues), TradePolicy(),
                          max_pairs=2, max_gross_notional_usd=D(500))


def proof(live, instrument, stamp=NOW, *, positions=(), orders=()):
    return AccountSnapshot(live.markets[instrument].venue, instrument, None, None, stamp,
        dict(positions).get(instrument, D(0)), account_positions=positions, account_open_orders=orders)


def install_proofs(live):
    for instruments in live.account_scopes.values():
        instrument = live._account_representative(instruments)
        live.on_account(proof(live, instrument), NOW)


def test_full_account_validation_work_is_bounded_by_unique_markets(monkeypatch):
    live = empty_state(tuple(f"S{i}" for i in range(160)))
    install_proofs(live)
    instrument = live.account_scopes[("ASTER", "")][0]
    rows = tuple((i, D(0)) for i in live.account_scopes[("ASTER", "")]) + tuple(
        (f"OUTSIDE{i}.ASTER", D(0)) for i in range(474))
    observation = proof(live, instrument, positions=rows)
    live.on_account(observation, NOW)
    assert all(live.accounts[i].account_positions is observation.account_positions
               for i in live.account_scopes[("ASTER", "")])
    calls = []
    original = live.expected_native
    def expected(instrument):
        calls.append(instrument)
        return original(instrument)
    monkeypatch.setattr(live, "expected_native", expected)
    live.reconcile_accounts(NOW)
    assert len(calls) == 160 + 634 and max(Counter(calls).values()) == 1
    assert not live.account_pauses and not live.outbox
    metrics = live.account_check_metrics["ASTER"]
    assert metrics["proofs_checked"] == 1 and metrics["position_rows_checked"] == 634
    assert metrics["unconfigured_position_rows"] == 474


@pytest.mark.parametrize(("changes", "reason"), [
    ({"position_quantity": D(1)}, "inconsistent_coverage"),
    ({"authoritative": False}, "not_authoritative"),
    ({"connected": False}, "not_authoritative"),
    ({"observed_ns": NOW + 1}, "stale_or_future"),
    ({"observed_ns": NOW - 6 * SECOND}, "stale_or_future"),
])
def test_shared_proof_does_not_hide_individual_projection_failures(changes, reason):
    live = empty_state()
    install_proofs(live)
    live.accounts["ETH.HL"] = replace(live.accounts["ETH.HL"], **changes)
    assert not live._account_domain_ready("HL", NOW)
    assert live.account_pauses["HL"]["reason"] == reason


def test_same_timestamp_different_payloads_are_checked_independently():
    live = empty_state()
    install_proofs(live)
    live.accounts["ETH.HL"] = proof(live, "ETH.HL", positions=(("OUTSIDE.HL", D(1)),))
    assert not live._account_domain_ready("HL", NOW)
    assert live.account_pauses["HL"]["reason"] == "position_mismatch"
    assert live.account_check_metrics["HL"]["proofs_checked"] == 2


def test_same_payload_different_read_times_do_not_cross_activity_fence():
    live = empty_state()
    install_proofs(live)
    live.accounts["BTC.HL"] = replace(live.accounts["BTC.HL"], observed_ns=NOW + 2)
    live.fences["BTC.HL"] = NOW + 1
    assert not live._account_domain_ready("HL", NOW + 2)
    assert "HL" not in live.account_pauses  # Existing predates-activity recovery rule.
    assert live.outbox and live.account_check_metrics["HL"]["proofs_checked"] == 2


def test_shared_payload_cannot_cross_hyperliquid_dex_boundary():
    live = empty_state(("BTC", "xyz:ABC"))
    install_proofs(live)
    live.fences["xyz:ABC.HL"] = NOW + 1
    proofs, expected = {}, {}
    assert live._account_problem("BTC.HL", NOW + 1, proofs=proofs, expected=expected) is None
    assert live._account_problem("xyz:ABC.HL", NOW + 1, proofs=proofs, expected=expected) == "predates_activity"
    assert len(proofs) == 2


def test_next_pass_rechecks_inventory_and_fences(monkeypatch):
    live = empty_state()
    install_proofs(live)
    assert live._account_domain_ready("HL", NOW)
    original = live.expected_native
    monkeypatch.setattr(live, "expected_native", lambda i: D(1) if i == "ETH.HL" else original(i))
    assert not live._account_domain_ready("HL", NOW)
    assert live.account_pauses["HL"]["reason"] == "position_mismatch"
    monkeypatch.setattr(live, "expected_native", original)
    live.fences["BTC.HL"] = NOW + 1
    assert live._account_problem("ETH.HL", NOW + 1) == "predates_activity"


def test_next_pass_rechecks_owned_order_terminal_state():
    live = empty_state()
    install_proofs(live)
    live.orders["owned"] = {"instrument_id": "BTC.HL", "terminal_ns": None,
                             "purpose": "exit", "status": "submitted"}
    live.on_account(proof(live, "BTC.HL", orders=(("BTC.HL", "owned"),)), NOW)
    assert live._account_domain_ready("HL", NOW)
    live.orders["owned"]["terminal_ns"] = NOW + 1
    assert not live._account_domain_ready("HL", NOW + 1)
    assert live.account_pauses["HL"]["reason"] == "unowned_order"


def test_same_positions_with_different_order_evidence_are_not_merged():
    live = empty_state()
    install_proofs(live)
    live.accounts["ETH.HL"] = replace(live.accounts["ETH.HL"],
        account_open_orders=(("OUTSIDE.HL", "foreign"),))
    assert not live._account_domain_ready("HL", NOW)
    assert live.account_pauses["HL"]["reason"] == "unowned_order"


def test_account_payload_sharing_keeps_input_immutable_and_serializable():
    live = empty_state()
    positions, orders = [["BTC.HL", "0"]], [["BTC.HL", "owned"]]
    observation = proof(live, "BTC.HL", positions=positions, orders=orders)
    positions[0][1] = "2"
    orders[0][1] = "external"
    assert observation.account_positions == (("BTC.HL", D(0)),)
    assert observation.account_open_orders == (("BTC.HL", "owned"),)
    projected = replace(observation, instrument_id="ETH.HL", position_quantity=D(0), open_order_ids=())
    assert projected.account_positions is observation.account_positions
    assert projected.account_open_orders is observation.account_open_orders
    assert asdict(projected)["account_positions"] == (("BTC.HL", D(0)),)
    changed = replace(projected, account_positions=(("BTC.HL", D(2)),))
    assert changed.account_positions is not observation.account_positions
    with pytest.raises(ValueError, match="finite"):
        replace(projected, account_positions=(("BTC.HL", D("NaN")),))
