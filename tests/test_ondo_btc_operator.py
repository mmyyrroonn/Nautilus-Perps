"""Offline launcher tests. No live child, credentials or network is permitted."""

import builtins
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ondo_btc_operator as op
from ondo_trade_limits import load_trade_limits
from ondo_trade_probe import authorization_problems, parse_trade_plan, plan_sha256

STAMP = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
CANDIDATE = {"test_only": True, "dms_release_host_contract_verified": False}


def capture(path, *, bid="85900", ask="86100", tick="1", step="0.0001", **meta_changes):
    path.mkdir(parents=True, exist_ok=True)
    payloads = {
        "markets": {"perps": {"tradingPairs": [{
            "market": "BTC-USD.P", "quoteIncrement": tick,
            "baseIncrement": step, "maxPositionBaseSize": "13",
        }]}},
        "contracts": [{"market": "BTC-USD.P", "bid": bid, "ask": ask,
                       "disabled": False, "isClosed": False}],
        "instruments": [{"symbol": "BTC", "instrument_id": "BTC-USD-PERP.ONDO",
                         "raw_market": "BTC-USD.P", "market_status": "active",
                         "price_increment": tick, "size_increment": step,
                         "quote_currency": "USD", "settlement_currency": "USDC"}],
    }
    manifest = {
        "run_id": "synthetic", "run_dir": "runs/synthetic", "complete": True,
        "environment": "production", "requested_symbols": ["BTC"],
        "started_at_utc": STAMP.isoformat(), "fetched_at_utc": STAMP.isoformat(), "files": {},
    }
    for name, value in payloads.items():
        op.write_once(path / f"{name}.json", value)
        raw = (path / f"{name}.json").read_bytes()
        manifest["files"][name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    manifest.update(meta_changes)
    op.write_once(path / "meta.json", manifest)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("No real subprocess, credentials or network in launcher tests")
    monkeypatch.setattr(op.subprocess, "run", forbidden)
    monkeypatch.setattr(op.subprocess, "Popen", forbidden)
    monkeypatch.setattr(op, "verify_candidate", lambda: copy.deepcopy(CANDIDATE))
    monkeypatch.setattr(op, "now", lambda: STAMP)
    monkeypatch.setattr(op, "OUTPUT", tmp_path / "operator")
    monkeypatch.setattr(op, "CLAIM", tmp_path / "operator/MAINNET_ATTEMPT_CLAIMED.json")
    monkeypatch.setattr(op, "ENV_FILE", tmp_path / "nonexistent.env")
    monkeypatch.setattr(op, "fetch_public", capture)
    import ondo_trade_probe
    monkeypatch.setattr(ondo_trade_probe, "_load_environment", forbidden)
    monkeypatch.setattr(op.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(op.sys.stdout, "isatty", lambda: True)


def latest_draft():
    return next(op.OUTPUT.glob("*/draft-plan.json"))


def confirm(monkeypatch, action=None):
    def answer(prompt):
        plan = op.read_json(latest_draft())
        if action:
            action(plan)
        return f"{op.PHRASE} {plan['plan_sha256']}"
    monkeypatch.setattr(builtins, "input", answer)


def test_default_prepares_unapproved_draft_without_private_dispatch(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("Preparation must not prompt for authorization"))
    assert op.main(["--side", "buy"]) == 0
    plan = op.read_json(latest_draft())
    assert plan["authorization"] == {"user_phrase_上主网_present": False, "approved_plan_sha256": None}
    assert plan["plan_sha256"] == plan_sha256(plan)
    assert plan["candidate_evidence"] == CANDIDATE
    assert not op.CLAIM.exists()
    assert not list(op.OUTPUT.glob("*/approved-plan.json"))


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_plan_rounds_down_size_and_bounds_prices(tmp_path, side):
    capture(tmp_path / "public")
    plan = op.make_plan(tmp_path / "public", tmp_path, side, CANDIDATE)
    parsed = parse_trade_plan(plan, load_trade_limits(op.LIMITS))
    assert parsed.entry.quantity == Decimal("0.0001")  # ~8.6 USD, NOT sized up to 17.2
    prices = (parsed.entry.limit_price, parsed.close.limit_price)
    assert max(prices) == Decimal("86272")
    assert min(prices) == Decimal("85729")
    assert parsed.entry.quantity * max(prices) <= Decimal("15")
    assert parsed.entry.side != parsed.close.side
    assert parsed.close.reduce_only and parsed.close.max_close_attempts == 2
    assert parsed.envelope.max_gross_exposure_usd == Decimal("20")
    assert parsed.envelope.max_notional_per_order_usd == Decimal("20")
    assert parsed.envelope.require_flat_start
    assert parsed.envelope.deadline_secs == 120
    assert plan["dms"]["mode"] == "trading"
    assert plan["dms"]["renewal_message_verified"] is False
    assert plan["instrument"]["min_notional"] is None
    assert authorization_problems(parsed)


@pytest.mark.parametrize("values", [
    {"bid": "0"}, {"ask": "NaN"}, {"ask": "Infinity"}, {"bid": "90000"},
    {"step": "0.001"}, {"tick": "0"}, {"bid": 85900},
    {"complete": False}, {"environment": "sandbox"}, {"requested_symbols": ["NVDA"]},
    {"started_at_utc": (STAMP - timedelta(seconds=61)).isoformat()},
    {"fetched_at_utc": (STAMP + timedelta(seconds=1)).isoformat()},
    {"started_at_utc": STAMP.replace(tzinfo=None).isoformat()},
])
def test_invalid_or_stale_public_evidence_refuses(tmp_path, values):
    capture(tmp_path / "public", **values)
    with pytest.raises((op.Refused, ArithmeticError)):
        op.make_plan(tmp_path / "public", tmp_path, "buy", CANDIDATE)


def test_public_payload_hash_is_verified(tmp_path):
    capture(tmp_path / "public")
    with (tmp_path / "public/contracts.json").open("a") as output:
        output.write(" ")
    with pytest.raises(op.Refused, match="differs"):
        op.load_capture(tmp_path / "public")


@pytest.mark.parametrize("answer", ["", "yes", "上主网", "上主网 wrong-hash"])
def test_wrong_confirmation_never_dispatches(monkeypatch, answer):
    monkeypatch.setattr(builtins, "input", lambda _: answer)
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert not op.CLAIM.exists()
    assert not list(op.OUTPUT.glob("*/approved-plan.json"))


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_noninteractive_execution_refuses_before_public_fetch(monkeypatch, stream):
    monkeypatch.setattr(getattr(op.sys, stream), "isatty", lambda: False)
    monkeypatch.setattr(op, "fetch_public", lambda _: pytest.fail("No fetch for piped execution"))
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert not op.OUTPUT.exists()


def test_expired_confirmation_stops_without_refresh(monkeypatch):
    confirm(monkeypatch, lambda _: monkeypatch.setattr(op, "now", lambda: STAMP + timedelta(seconds=61)))
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert len(list(op.OUTPUT.glob("*/draft-plan.json"))) == 1
    assert not op.CLAIM.exists()


def test_candidate_change_during_prompt_refuses(monkeypatch):
    confirm(monkeypatch, lambda _: monkeypatch.setattr(op, "verify_candidate", lambda: {"changed": True}))
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert not op.CLAIM.exists()


def test_draft_change_during_prompt_refuses(monkeypatch):
    def tamper(plan):
        plan["entry"]["quantity"] = "0.1"
        latest_draft().write_text(json.dumps(plan), encoding="utf-8")
    confirm(monkeypatch, tamper)
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert not op.CLAIM.exists()


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_one_exact_approved_dispatch_and_no_retry(monkeypatch, exit_code):
    confirm(monkeypatch)
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        assert op.CLAIM.is_file()  # Claim must be durable BEFORE dispatch.
        assert command[1] == "-I" and command[2].endswith("ondo_trade_probe.py")
        assert "--confirm-production-trade" in command
        assert "--acknowledge-mainnet-authorization" in command
        assert kwargs["env"]["ONDO_PROBE_ENV_FILE"] == str(op.ENV_FILE)
        approved = op.read_json(Path(command[command.index("--plan") + 1]))
        assert approved["authorization"]["source"] == "interactive_local_operator"
        assert not authorization_problems(parse_trade_plan(approved, load_trade_limits(op.LIMITS)))
        assert approved["plan_sha256"] == op.read_json(latest_draft())["plan_sha256"]
        return SimpleNamespace(wait=lambda: exit_code)
    monkeypatch.setattr(op.subprocess, "Popen", fake_run)
    assert op.main(["--side", "sell", "--execute"]) == exit_code
    assert len(calls) == 1
    result = op.read_json(latest_draft().parent / "operator-result.json")
    assert result["child_exit_code"] == exit_code
    assert result["automatic_retry"] is False
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert len(calls) == 1


def test_racing_claim_prevents_dispatch(monkeypatch):
    confirm(monkeypatch, lambda _: op.write_once(op.CLAIM, {"other_attempt": True}))
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert op.read_json(op.CLAIM) == {"other_attempt": True}


def test_dispatch_os_error_keeps_claim(monkeypatch):
    confirm(monkeypatch)
    def failure(*args, **kwargs):
        raise OSError("synthetic process startup failure")
    monkeypatch.setattr(op.subprocess, "Popen", failure)
    assert op.main(["--side", "buy", "--execute"]) == 2
    assert op.CLAIM.exists()


def test_ctrl_c_waits_for_existing_probe_without_kill_or_restart(monkeypatch):
    confirm(monkeypatch)
    starts, waits = [], []
    def wait():
        waits.append(True)
        if len(waits) == 1:
            raise KeyboardInterrupt
        return 3
    def start(*args, **kwargs):
        starts.append(True)
        # Deliberately no kill()/terminate(): either call would fail this test.
        return SimpleNamespace(wait=wait)
    monkeypatch.setattr(op.subprocess, "Popen", start)
    assert op.main(["--side", "buy", "--execute"]) == 3
    assert len(starts) == 1 and len(waits) == 2
    assert op.CLAIM.exists()


def test_exclusive_write_never_overwrites(tmp_path):
    path = tmp_path / "claim.json"
    op.write_once(path, {"original": True})
    with pytest.raises(FileExistsError):
        op.write_once(path, {"original": False})
    assert op.read_json(path) == {"original": True}


def test_child_environment_uses_canonical_credentials_only_after_confirmation(monkeypatch):
    monkeypatch.setenv("ONDO_MAINNET_API_KEY", "synthetic-do-not-inherit")
    monkeypatch.setenv("ONDO_MAINNET_ACCOUNT_ID", "synthetic-other-account")
    monkeypatch.setenv("ONDO_PROBE_ENV_FILE", "synthetic-other-file")
    monkeypatch.setenv("PYTHONPATH", "synthetic-other-install")
    assert not any(key.upper().startswith(("ONDO_", "PYTHON")) for key in op.child_environment())
    private = op.child_environment(private=True)
    assert {key for key in private if key.upper().startswith("ONDO_")} == {"ONDO_PROBE_ENV_FILE"}
    assert private["ONDO_PROBE_ENV_FILE"] == str(op.ENV_FILE)


def test_every_preparation_has_a_new_hash_and_journal():
    assert op.main(["--side", "buy"]) == 0
    assert op.main(["--side", "buy"]) == 0
    plans = [op.read_json(path) for path in op.OUTPUT.glob("*/draft-plan.json")]
    assert len({plan["plan_sha256"] for plan in plans}) == 2
    assert len({plan["account"]["journal_path"] for plan in plans}) == 2


def _fresh_plan(side="buy", name="run"):
    run_dir = op.OUTPUT / name
    run_dir.mkdir(parents=True)
    capture(run_dir / "public")
    plan = op.make_plan(run_dir / "public", run_dir, side, CANDIDATE)
    op.write_once(run_dir / "draft-plan.json", plan)
    return plan, run_dir


def test_chat_confirmation_dispatches_without_a_tty_and_records_its_source(monkeypatch):
    # The operator confirmed this exact hash in the current conversation: no local prompt, and the
    # plan records which source the confirmation came from.
    monkeypatch.setattr(op.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(op.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(
        builtins, "input", lambda _: pytest.fail("A confirmed hash must not prompt"),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert op.CLAIM.is_file()
        approved = op.read_json(Path(command[command.index("--plan") + 1]))
        assert approved["authorization"]["source"] == "chat_operator_confirmation"
        assert approved["authorization"]["user_phrase_上主网_present"] is True
        assert not authorization_problems(parse_trade_plan(approved, load_trade_limits(op.LIMITS)))
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(op.subprocess, "Popen", fake_run)
    plan, run_dir = _fresh_plan()
    assert op.execute_once(
        plan, run_dir, supersede=False, confirmed_plan_sha256=plan["plan_sha256"],
    ) == 0
    assert len(calls) == 1
    assert op.read_json(op.CLAIM)["plan_sha256"] == plan["plan_sha256"]


def test_a_hash_that_is_not_this_plan_never_dispatches(monkeypatch):
    monkeypatch.setattr(op.subprocess, "Popen", lambda *a, **k: pytest.fail("must not dispatch"))
    plan, run_dir = _fresh_plan()
    with pytest.raises(op.Refused):
        op.execute_once(plan, run_dir, supersede=False, confirmed_plan_sha256="0" * 64)
    assert not op.CLAIM.exists()
    assert not (run_dir / "approved-plan.json").exists()


def test_supersede_archives_the_reviewed_claim_before_the_new_one(monkeypatch):
    old_run = op.OUTPUT / "old-run"
    old_run.mkdir(parents=True)
    op.write_once(op.CLAIM, {
        "plan_sha256": "a" * 64, "run_dir": str(old_run),
        "state": "dispatch_claimed_not_proof_of_submission",
    })
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        approved = op.read_json(Path(command[command.index("--plan") + 1]))
        superseded = approved["authorization"]["supersedes"]
        assert Path(superseded["superseded_claim_path"]).parent == old_run
        assert superseded["superseded_plan_sha256"] == "a" * 64
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(op.subprocess, "Popen", fake_run)
    plan, run_dir = _fresh_plan(side="sell", name="new-run")
    assert op.execute_once(
        plan, run_dir, supersede=True, confirmed_plan_sha256=plan["plan_sha256"],
    ) == 0
    assert len(calls) == 1
    assert op.read_json(op.CLAIM)["plan_sha256"] == plan["plan_sha256"]
    archived = list(old_run.glob("MAINNET_ATTEMPT_CLAIMED.superseded-*.json"))
    assert len(archived) == 1
    assert op.read_json(archived[0])["plan_sha256"] == "a" * 64


def test_an_existing_claim_still_refuses_without_supersede(monkeypatch):
    old_run = op.OUTPUT / "old-run"
    old_run.mkdir(parents=True)
    op.write_once(op.CLAIM, {"plan_sha256": "a" * 64, "run_dir": str(old_run)})
    monkeypatch.setattr(op.subprocess, "Popen", lambda *a, **k: pytest.fail("must not dispatch"))
    plan, run_dir = _fresh_plan()
    with pytest.raises(op.Refused):
        op.execute_once(plan, run_dir, supersede=False, confirmed_plan_sha256=plan["plan_sha256"])
    assert op.read_json(op.CLAIM)["plan_sha256"] == "a" * 64
    assert not list(old_run.glob("MAINNET_ATTEMPT_CLAIMED.superseded-*.json"))


def test_policy_confirmation_dispatches_one_fresh_plan_and_records_the_bounds(monkeypatch):
    # The operator authorized the reviewed policy, not a hash that did not exist when they spoke:
    # the approved plan carries the source and the exact bounds the authorization covered.
    monkeypatch.setattr(op.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(op.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(
        builtins, "input", lambda _: pytest.fail("A policy confirmation must not prompt"),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        approved = op.read_json(Path(command[command.index("--plan") + 1]))
        authorization = approved["authorization"]
        assert authorization["source"] == "chat_operator_confirmation"
        assert authorization["user_phrase_上主网_present"] is True
        assert authorization["approved_plan_sha256"] == approved["plan_sha256"]
        assert authorization["confirmed_policy"] == op.reviewed_policy(approved)
        assert authorization["confirmed_policy"]["instrument_id"] == "BTC-USD-PERP.ONDO"
        assert authorization["confirmed_policy"]["entry_side"] == "buy"
        assert authorization["confirmed_policy"]["max_gross_exposure_usd"] == "20"
        assert "plan_sha256" not in authorization["confirmed_policy"]
        assert not authorization_problems(parse_trade_plan(approved, load_trade_limits(op.LIMITS)))
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(op.subprocess, "Popen", fake_run)
    plan, run_dir = _fresh_plan()
    assert op.execute_once(
        plan, run_dir, supersede=False, confirmed_policy=True,
    ) == 0
    assert len(calls) == 1
    assert op.read_json(op.CLAIM)["plan_sha256"] == plan["plan_sha256"]


def test_hash_and_policy_confirmations_cannot_be_combined(monkeypatch):
    monkeypatch.setattr(op.subprocess, "Popen", lambda *a, **k: pytest.fail("must not dispatch"))
    plan, run_dir = _fresh_plan()
    assert op.main(["--side", "buy", "--execute", "--operator-confirmed-policy",
                    "--operator-confirmed-plan-sha256", plan["plan_sha256"]]) == 2
    assert not op.CLAIM.exists()
    assert not list(op.OUTPUT.glob("*/approved-plan.json"))
