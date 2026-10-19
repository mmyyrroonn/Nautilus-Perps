"""Native order routing and receipt barriers; imports never connect or trade.

Only an operator-started runtime may dispatch mutation intents. Tests inject
strategies/transports and do not execute any financial requests.
"""
from __future__ import annotations

from opportunity_timing import record_latency, span, start_latency, timed

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import os
import json
import re
from pathlib import Path
import time

from opportunity_account_reader import AccountReadError, ReadOnlyAccountReader
from opportunity_preflight import ExecutionReceipt


SUPPORTED_VENUES = frozenset({"HL", "HYPERLIQUID", "LIGHTER", "ASTER", "ONDO", "BACKPACK"})
CANCEL_REPLACE_VENUES = frozenset({"ASTER", "ONDO", "BACKPACK"})
MAX_PENDING_SUBMIT_TIMINGS = 1024


def _native():
    from nautilus_trader import model
    return model


@timed("startup.execution_clients")
def execution_clients(scan_plan, environment="mainnet", *, environ=None,
                      state_directory=None, run_id=None, order_notional_usd="100",
                      max_gross_notional_usd="500", duration_secs=0,
                      backpack_public_config=None, backpack_quota=None,
                      native_controls=None, read_only=False):
    """Construct configs only; credentials are resolved when explicitly called.

    Aster has a separate application mainnet namespace. Lighter and HL use their
    adapter namespaces. Ondo and Backpack register genuine native writer factories,
    with durable identity/economic state and explicit continuous admission limits.
    Backpack shares the registered public config and quota; a replacement config
    cannot stand in for the public owner's actual telemetry.
    Read-only sessions use Ondo account_read_only and Backpack's account factory,
    without constructing either venue's production writer or trading authority.
    """
    if environment not in {"mainnet", "testnet"}:
        raise ValueError("environment must be mainnet or testnet")
    env = os.environ if environ is None else environ
    groups = {}
    for market in scan_plan.markets:
        if market.venue not in SUPPORTED_VENUES:
            raise ValueError(f"unsupported execution venue {market.venue}")
        groups.setdefault("HYPERLIQUID" if market.venue in {"HL", "HYPERLIQUID"} else market.venue, []).append(market)
    if groups.keys() & {"ONDO", "BACKPACK"}:
        if state_directory is None or not Path(state_directory).is_absolute() or not run_id:
            raise ValueError("native account clients require an absolute state directory and durable run ID")
        directory = Path(state_directory)
        if not read_only:
            order_cap, gross_cap = _decimal(order_notional_usd), _decimal(max_gross_notional_usd)
            if not 0 < order_cap <= 100 or not order_cap <= gross_cap <= 500:
                raise ValueError("native continuous caps must fit USD 100 per order and USD 500 gross")
        if type(duration_secs) is not int or duration_secs < 0:
            raise ValueError("native session durations must be exact nonnegative seconds")
    model = _native()
    result = []
    for client, markets in groups.items():
        account_id = model.AccountId.from_str(client + "-001")
        if client == "HYPERLIQUID":
            from nautilus_trader.adapters.hyperliquid import (
                HyperliquidEnvironment, HyperliquidExecutionClientConfig, HyperliquidExecutionClientFactory)
            prefix = "HYPERLIQUID_" if environment == "mainnet" else "HYPERLIQUID_TESTNET_"
            address = env.get(prefix + "ACCOUNT_ADDRESS")
            if not address:
                raise ValueError("explicit Hyperliquid ACCOUNT_ADDRESS is required for independent account reconciliation")
            if not env.get(prefix + "PK"):
                raise ValueError("explicit Hyperliquid signing key is required")
            config = HyperliquidExecutionClientConfig(account_id=account_id,
                environment=HyperliquidEnvironment.MAINNET if environment == "mainnet" else HyperliquidEnvironment.TESTNET,
                private_key=env[prefix + "PK"], vault_address=env.get(prefix + "VAULT"),
                account_address=address, include_builder_attribution=False)
            factory = HyperliquidExecutionClientFactory()
        elif client == "LIGHTER":
            from nautilus_trader.adapters.lighter import (
                LighterEnvironment, LighterExecutionClientConfig, LighterExecutionClientFactory,
                LighterExecutionControl)
            from opportunity_account_scope import lighter_account_index
            prefix = "LIGHTER_" if environment == "mainnet" else "LIGHTER_TESTNET_"
            if any(not env.get(prefix + suffix) for suffix in ("API_SECRET", "API_KEY_INDEX")):
                raise ValueError("explicit Lighter execution credentials are required")
            control = LighterExecutionControl()
            config = LighterExecutionClientConfig(account_id=account_id, control=control,
                environment=LighterEnvironment.MAINNET if environment == "mainnet" else LighterEnvironment.TESTNET,
                account_index=lighter_account_index(env, environment),
                api_key_index=int(env[prefix + "API_KEY_INDEX"]), private_key=env[prefix + "API_SECRET"],
                sendtx_quota_per_min=36)
            factory = LighterExecutionClientFactory()
        elif client == "ASTER":
            from nautilus_trader.adapters.aster import (
                AsterEnvironment, AsterExecutionClientConfig, AsterExecutionClientFactory)
            from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig
            prefix = "ASTER_MAINNET_" if environment == "mainnet" else "ASTER_"
            names = [prefix + suffix for suffix in ("USER_ADDRESS", "SIGNER_ADDRESS", "SIGNER_PRIVATE_KEY")]
            if any(not env.get(name) for name in names):
                raise ValueError("explicit Aster environment credentials are required")
            config = AsterExecutionClientConfig(account_id=account_id,
                environment=AsterEnvironment.MAINNET if environment == "mainnet" else AsterEnvironment.TESTNET,
                user_address=env[names[0]], signer_address=env[names[1]], signer_private_key=env[names[2]],
                instrument_provider=BinanceInstrumentProviderConfig(load_all=False,
                    load_ids=[m.instrument_id for m in markets]), assume_one_way_mode_when_unconfirmed=False)
            factory = AsterExecutionClientFactory()
        elif client == "ONDO":
            from nautilus_trader.adapters import ondo
            factory = ondo.OndoExecutionClientFactory()
            if not read_only and (not getattr(factory, "supports_continuous_execution", False) or getattr(factory, "supports_native_amend", True)):
                raise ValueError("install the source-bound Ondo continuous execution wheel")
            if not read_only and not getattr(factory, "supports_complete_account_snapshots", False):
                raise ValueError("install the source-bound Ondo complete account snapshot wheel")
            prefix = "ONDO_MAINNET_" if environment == "mainnet" else "ONDO_SANDBOX_"
            names = [prefix + suffix for suffix in ("API_KEY", "API_SECRET", "ACCOUNT_ID")]
            if any(not env.get(name) for name in names):
                raise ValueError("explicit Ondo credentials and expected venue account identity are required")
            continuous = ondo.OndoContinuousExecutionConfig(
                instrument_ids=[model.InstrumentId.from_str(m.instrument_id) for m in markets],
                max_notional_per_order_usd=format(order_cap, "f"),
                max_gross_exposure_usd=format(gross_cap, "f"), min_available_margin_usdc="25",
                max_live_orders=16, max_requests_per_second=8, max_unresolved_orders=1) if not read_only else None
            config = ondo.OndoExecutionClientConfig(account_id=account_id,
                environment=ondo.OndoEnvironment.PRODUCTION if environment == "mainnet" else ondo.OndoEnvironment.SANDBOX,
                api_key=env[names[0]], api_secret=env[names[1]], expected_venue_account_id=env[names[2]],
                account_read_only=read_only, allow_production_orders=not read_only and environment == "mainnet",
                diagnostics_run_id=str(run_id), journal_path=str(directory / "ondo" / "execution.jsonl"),
                continuous_execution=continuous, dms_timeout_secs=30, reconcile_interval_secs=1,
                http_timeout_secs=15)
        else:
            if environment != "mainnet":
                raise ValueError("Backpack has no configured native testnet writer")
            from nautilus_trader.adapters import backpack as bp
            if not read_only and not hasattr(bp, "BackpackProductionExecutionClientConfig"):
                raise ValueError("install the source-bound Backpack production execution wheel")
            if not read_only and (backpack_public_config is None or backpack_quota is None):
                raise ValueError("Backpack requires its registered public config and shared native quota")
            if not env.get("BACKPACK_MAINNET_API_SECRET") or not env.get("BACKPACK_MAINNET_API_KEY"):
                raise ValueError("explicit Backpack mainnet seed and public key are required")
            # Validates the public key/seed match without exposing either credential.
            import base64
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            try:
                seed = base64.b64decode(env["BACKPACK_MAINNET_API_SECRET"], validate=True)
                public = base64.b64decode(env["BACKPACK_MAINNET_API_KEY"], validate=True)
                if Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) != public:
                    raise ValueError
            except Exception:
                raise ValueError("Backpack seed and public key did not pass the credential check") from None
            credential = bp.BackpackCredential(env["BACKPACK_MAINNET_API_SECRET"])
            symbols = [m.instrument_id.removesuffix(".BACKPACK") for m in markets]
            account = bp.BackpackExecutionClientConfig(symbols, credential, str(account_id),
                env["BACKPACK_MAINNET_API_KEY"], str(directory / "backpack" / "identity"),
                quota=backpack_quota if backpack_quota is not None else bp.BackpackQuota(),
                connect_timeout_ms=60000, recovery_interval_ms=1000,
                recovery_lookback_ms=86400000)
            if read_only:
                result.append((client, bp.BackpackExecutionClientFactory(), account))
                continue
            authority = bp.BackpackProductionExecutionAuthority(
                expires_at_ms=time.time_ns() // 1_000_000 + 300_000,
                max_account_age_ms=15000, max_market_age_ms=2000,
                max_order_notional=format(order_cap, "f"), max_reserved_notional=format(gross_cap, "f"),
                max_reserved_margin=format(gross_cap, "f"), max_unsettled_orders=16,
                allow_new_risk=True, allow_reduction=True, allow_owned_cancel=True)
            config = bp.BackpackProductionExecutionClientConfig(account, backpack_public_config,
                authority=authority, mutation_budget_ms=10000, receive_window_ms=5000,
                economic_state_directory=str(directory / "backpack" / "economics"))
            if any(not hasattr(config.control, name) for name in ("account_snapshot_json", "renew_authority")):
                raise ValueError("install the source-bound Backpack account snapshot and authority renewal wheel")
            factory = bp.BackpackProductionExecutionClientFactory()
        if native_controls is not None:
            if client == "ONDO":
                native_controls[client] = factory
            elif client == "LIGHTER":
                native_controls[client] = control
            elif client == "BACKPACK":
                native_controls[client] = config.control
        result.append((client, factory, config))
    return result


def _decimal(value):
    if hasattr(value, "as_decimal"):
        value = value.as_decimal()
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError("native receipt requires exact decimal fields")
    value = Decimal(value)
    if not value.is_finite():
        raise ValueError("native receipt has a nonfinite amount")
    return value


def _fill_signature(fill):
    """Normalize persisted JSON decimal strings to the live callback shape."""
    quantity = _decimal(fill.get("native_qty", fill.get("quantity")))
    price = _decimal(fill.get("native_price", fill.get("price")))
    commission = fill.get("commission_amount")
    return {"client_order_id": str(fill["client_order_id"]), "trade_id": str(fill["trade_id"]),
        "instrument_id": str(fill["instrument_id"]), "venue": str(fill["venue"]), "side": str(fill["side"]),
        "execution_id": str(fill["venue"]) + ":" + str(fill["client_order_id"]) + ":" + str(fill["trade_id"]),
        "native_qty": quantity, "native_price": price, "quantity": quantity, "price": price,
        "commission_amount": _decimal(commission) if commission is not None else None,
        "commission_currency": fill.get("commission_currency")}


class NativeOrderGateway:
    def __init__(self, strategy, markets, on_receipt, on_account, on_fill, *,
                 reader=None, environment="mainnet", model=None, timeout_secs=10,
                 quote_provider=None, native_controls=None, account_timeout_secs=None, environ=None,
                 account_max_age_ms=15000):
        self.strategy = strategy
        rows = markets.values() if isinstance(markets, dict) else markets
        self.markets = {m.instrument_id: m for m in rows}
        self.on_receipt, self.on_account, self.on_fill = on_receipt, on_account, on_fill
        self.reader = reader or ReadOnlyAccountReader(environment=environment, environ=environ,
            max_native_snapshot_age_ms=account_max_age_ms)
        self.environment = environment
        self.model = model or _native()
        self.timeout_ns = timeout_secs * 1_000_000_000
        read_timeout = timeout_secs if account_timeout_secs is None else account_timeout_secs
        if type(read_timeout) is not int or read_timeout <= 0:
            raise ValueError("native account read timeout must be positive integer seconds")
        self.account_timeout_ns = read_timeout * 1_000_000_000
        self.quote_provider = quote_provider
        self.native_controls = dict(native_controls or {})
        self.next_authority_renewal_ms = 0
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-readonly")
        self.reads = {}
        self.native_reads = {}
        self.requests = {}
        self.submit_timings = {}
        self.orders = {}
        self.active_request_map = {}
        self.seen_fills = {}
        self.seen_events = set()
        self.terminals = {}
        self.emitted = set()
        self.evidence_receipts = {}
        self.last_error = None
        self.closed = False

    def _client(self, venue):
        return self.model.ClientId.from_str("HYPERLIQUID" if venue in {"HL", "HYPERLIQUID"} else venue)

    def query_balance(self, venue):
        """Request native account refresh without any order mutation."""
        from opportunity_balances import NativeBalanceReader
        NativeBalanceReader(self.strategy, self.model).query_balance(venue)

    def read_balance(self, venue, now_ns, max_age_ms):
        from opportunity_balances import NativeBalanceReader
        return NativeBalanceReader(self.strategy, self.model).read_balance(venue, now_ns, max_age_ms)

    def read_balance_since(self, venue, now_ns, after_ns):
        from opportunity_balances import NativeBalanceReader
        return NativeBalanceReader(self.strategy, self.model).read_balance_since(venue, now_ns, after_ns)

    def _receipt(self, intent, kind, *, execution_id="", quantity=Decimal(0), retry_after_secs=0,
                 final_quantity=None, final_source=""):
        evidence = kind in {"terminal_evidence", "reconciliation_required"}
        signature = (kind, final_quantity, final_source)
        if evidence and self.evidence_receipts.get(intent.request_id) == signature:
            return True
        key = (intent.request_id, kind, execution_id)
        if not evidence and key in self.emitted:
            return
        if not evidence:
            self.emitted.add(key)
        self._record_outcome(intent, kind)
        self.on_receipt(ExecutionReceipt(intent.request_id, intent.client_order_id, kind, execution_id, quantity,
                                        retry_after_secs=retry_after_secs,
                                        final_filled_quantity=final_quantity, final_fill_source=final_source))
        if evidence:
            self.evidence_receipts[intent.request_id] = signature
        return True

    def _record_outcome(self, intent, kind):
        request = self.requests.get(intent.request_id)
        if request is None or request.get("timing_start") is None:
            return
        seen = request.setdefault("timing_outcomes", set())
        if kind not in seen:
            seen.add(kind)
            action = intent.action if intent.action in {"connect", "reconcile", "submit", "modify", "cancel"} else "other"
            record_latency(request["timing_start"], "gateway." + action + ".response",
                           venue=intent.venue, outcome=kind,
                           error=kind in {"unknown", "rejected", "denied", "modify_rejected", "cancel_rejected"})
            record_latency(self.submit_timings.get(intent.request_id), "gateway.submit_to_outcome",
                           venue=intent.venue, outcome=kind,
                           error=kind in {"unknown", "rejected", "denied"})
            if kind in {"terminal", "canceled", "expired", "rejected", "denied"}:
                self.submit_timings.pop(intent.request_id, None)

    def _reject(self, intent, reason):
        self.last_error = reason
        self._receipt(intent, "rejected")

    def _cached(self, cid):
        return self.strategy.cache.order(self.model.ClientOrderId.from_str(cid))

    def _invalidate_account(self, venue):
        invalidate = getattr(self.reader, "invalidate", None)
        if callable(invalidate):
            invalidate(venue)

    def _quote(self, instrument_id, now_ns):
        if self.quote_provider is not None:
            bid, ask, stamp = self.quote_provider(instrument_id, now_ns)
        else:
            quote = self.strategy.cache.quote_tick(self.model.InstrumentId.from_str(instrument_id))
            if quote is None:
                raise ValueError("current native quote is unavailable")
            bid, ask, stamp = quote.bid_price, quote.ask_price, quote.ts_event
        if not 0 < stamp <= now_ns or now_ns - stamp > 2_000_000_000:
            raise ValueError("native account evidence requires a fresh quote")
        bid, ask = _decimal(bid), _decimal(ask)
        if not 0 < bid < ask:
            raise ValueError("native quote is invalid")
        return bid, ask, stamp

    @timed("gateway.dispatch", labels=lambda self, intent, *a, **kw: (intent.venue, intent.action))
    def dispatch(self, intent, now_ns, *, candidate_marker=None):
        if self.closed:
            raise RuntimeError("native gateway is closed")
        if intent.request_id in self.requests:
            if self.requests[intent.request_id]["intent"] != intent:
                raise ValueError("request ID was reused with a different intent")
            return
        market = self.markets.get(intent.instrument_id)
        if market is None or market.venue != intent.venue:
            self._reject(intent, "intent market/venue is not configured")
            return
        if intent.venue not in SUPPORTED_VENUES:
            self._reject(intent, "unsupported venue")
            return
        self.requests[intent.request_id] = {"intent": intent, "sent_ns": now_ns, "queried_ns": now_ns}
        marker = start_latency()
        if marker is not None:
            self.requests[intent.request_id]["timing_start"] = marker
        if intent.action in {"connect", "reconcile"}:
            # A timed-out worker can still be running. Retrying the state check
            # must not enqueue another blocking read for the same instrument.
            if intent.account_check and any(self.requests[rid]["intent"].instrument_id == intent.instrument_id
                   for rid in (*self.reads, *self.native_reads)):
                self._receipt(intent, "unknown")
                return
            if intent.venue in {"ONDO", "BACKPACK"}:
                self.native_reads[intent.request_id] = intent
                self._start_native_read(intent, now_ns)
                return
            try:
                bid, ask = (None, None) if intent.account_check else self._quote(intent.instrument_id, now_ns)[:2]
                # Snapshot time is the read's start fence, not the older quote's
                # event time and never the response completion time.
                self.reads[intent.request_id] = self.worker.submit(self.reader.read, market, bid, ask, now_ns)
            except Exception:
                self._reject(intent, "account read could not start" if intent.account_check else
                             "account read could not start with a valid current quote")
            return
        if intent.action not in {"submit", "modify", "cancel"}:
            self._reject(intent, "unsupported gateway action")
            return
        if not intent.client_order_id:
            self._reject(intent, "mutation requires an explicit persisted client order ID")
            return
        cid, client = intent.client_order_id, self._client(intent.venue)
        if intent.venue == "ONDO" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
            self._reject(intent, "Ondo client order ID must use at most 64 ASCII letters, digits, underscores or hyphens")
            return
        if intent.action == "modify" and intent.venue in CANCEL_REPLACE_VENUES:
            self._reject(intent, "native modification is unsupported; use confirmed cancel/replace")
            return
        if intent.action == "submit":
            if cid in self.orders:
                self._reject(intent, "client order ID already belongs to a submission")
                return
            build_marker = start_latency()
            try:
                quantity, price = _decimal(intent.quantity), _decimal(intent.price)
                if quantity <= 0 or price <= 0 or intent.side not in {"BUY", "SELL"}:
                    raise ValueError("invalid order amounts/side")
                if intent.time_in_force not in {"IOC", "GTC"} or type(intent.post_only) is not bool or type(intent.reduce_only) is not bool:
                    raise ValueError("invalid order flags")
                order = self.strategy.order_factory.limit(
                    instrument_id=self.model.InstrumentId.from_str(intent.instrument_id),
                    order_side=getattr(self.model.OrderSide, intent.side),
                    quantity=self.model.Quantity.from_str(format(quantity, "f")),
                    price=self.model.Price.from_str(format(price, "f")),
                    time_in_force=getattr(self.model.TimeInForce, intent.time_in_force),
                    post_only=intent.post_only, reduce_only=intent.reduce_only,
                    client_order_id=self.model.ClientOrderId.from_str(cid))
            except Exception:
                record_latency(build_marker, "gateway.construct_order", venue=intent.venue, error=True)
                self._reject(intent, "native order construction refused")
                return
            record_latency(build_marker, "gateway.construct_order", venue=intent.venue)
            self.orders[cid] = {"intent": intent, "processed_qty": Fraction(0), "native": order}
        elif cid not in self.orders or self.orders[cid]["intent"].instrument_id != intent.instrument_id:
            self._reject(intent, "only owned orders may be modified or canceled")
            return
        self.active_request_map[(cid, intent.action)] = intent
        self._invalidate_account(intent.venue)
        try:
            if intent.action == "submit":
                # This is the Python/native command boundary. Native queueing,
                # signing and transport occur after it and are not network RTT.
                marker = start_latency()
                if marker is not None:
                    if len(self.submit_timings) >= MAX_PENDING_SUBMIT_TIMINGS:
                        self.submit_timings.pop(next(iter(self.submit_timings)))
                    self.submit_timings[intent.request_id] = marker
                record_latency(candidate_marker, "entry.candidate_to_submit", venue=intent.venue,
                               ended_ns=marker[1] if marker is not None else None)
                with span("gateway.submit_handoff", venue=intent.venue):
                    self.strategy.submit_order(order, client_id=client)
            elif intent.action == "cancel":
                self.strategy.cancel_order(self.model.ClientOrderId.from_str(cid), client_id=client)
            else:
                quantity, price = _decimal(intent.quantity), _decimal(intent.price)
                if quantity <= 0 or price <= 0:
                    raise ValueError("invalid modify amounts")
                self.strategy.modify_order(self.model.ClientOrderId.from_str(cid),
                    quantity=self.model.Quantity.from_str(format(quantity, "f")),
                    price=self.model.Price.from_str(format(price, "f")), client_id=client)
        except Exception:
            # A raised send may have crossed the dispatch boundary. Preserve its ID.
            self.last_error = "native dispatch outcome is unknown; original ID retained"
            self._receipt(intent, "unknown")

    @timed("account.native_read", labels=lambda self, intent, *a, **kw: (intent.venue, ""))
    def _start_native_read(self, intent, now_ns):
        """Wait for a complete native cycle begun after this request's send fence."""
        try:
            bid, ask = (None, None) if intent.account_check else self._quote(intent.instrument_id, now_ns)[:2]
            control = self.native_controls[intent.venue]
            # PyO3 handles are unsendable. Capture evidence on the node thread,
            # then validate detached JSON through the independent reader.
            if intent.venue == "ONDO":
                payload = control.production_trade_snapshot()
                if isinstance(payload, str):
                    payload = json.loads(payload)
            else:
                payload = json.loads(control.account_snapshot_json())
            if not isinstance(payload, dict):
                return
            started = payload.get("snapshot_started_ns", 0)
            if type(started) is not int or started < self.requests[intent.request_id]["sent_ns"]:
                return
            completed = payload.get("snapshot_completed_ns")
            if type(completed) is int and 0 < started <= completed and completed > now_ns:
                # The native clock can advance past this poll's sampled wall time
                # (notably CPython 3.12's coarser Windows clock). Keep the original
                # request pending until time catches up; never relabel or admit a
                # future snapshot. The existing account deadline remains in force.
                return
            complete = payload.get("coverage_complete") if intent.venue == "ONDO" else payload.get("complete")
            connected = payload.get("connected_ready") if intent.venue == "ONDO" else payload.get("connected")
            if connected is False:
                self.native_reads.pop(intent.request_id, None)
                self.last_error = "native account connection is not ready"
                self._receipt(intent, "unknown")
                return
            if complete is not True or connected is not True:
                return
            self.reader.capture_native_evidence(intent.venue, payload, native_environment=self.environment)
            market = self.markets[intent.instrument_id]
            instrument = self.strategy.cache.instrument(self.model.InstrumentId.from_str(intent.instrument_id))
            if instrument is None:
                return
            step = _decimal(instrument.size_increment)
            minimum = instrument.min_quantity
            minimum_notional = instrument.min_notional
            if minimum is None and intent.venue == "BACKPACK":
                return
            self.reader.register_native_instrument(intent.instrument_id,
                size_increment=step, price_increment=_decimal(instrument.price_increment),
                min_quantity=step if minimum is None else _decimal(minimum),
                min_notional=Decimal(0) if minimum_notional is None else _decimal(minimum_notional),
                multiplier=_decimal(getattr(instrument, "multiplier", Decimal(1))))
            self.reads[intent.request_id] = self.worker.submit(self.reader.read, market, bid, ask, now_ns)
            self.native_reads.pop(intent.request_id, None)
        except AccountReadError as exc:
            # A rejected observation is a failed read, not a cycle still being
            # assembled. Retire it before publishing failure to the strategy.
            self.native_reads.pop(intent.request_id, None)
            self.last_error = str(exc)
            self._receipt(intent, "unknown", retry_after_secs=exc.retry_after_secs)
        except (RuntimeError, ValueError, KeyError):
            # Startup/recovery may not yet have a complete authoritative cycle.
            # Keep the request pending until its account deadline. Reporting a
            # failure here would invalidate the cycle we are still waiting for.
            self.last_error = "native account evidence is not yet complete or fresh"

    def _expire_account_reads(self, now_ns):
        for request_id in tuple(self.native_reads) + tuple(self.reads):
            request = self.requests[request_id]
            if request.get("read_timed_out") or now_ns - request["sent_ns"] < self.account_timeout_ns:
                continue
            request["read_timed_out"] = True
            self.native_reads.pop(request_id, None)
            future = self.reads.get(request_id)
            # A running worker cannot be canceled. Retain it only to prevent
            # overlapping reads, and discard its result when it finishes.
            if future is not None and (future.cancel() or future.done()):
                self.reads.pop(request_id)
            self.last_error = "account read timed out; late result will not be used"
            self._receipt(request["intent"], "unknown")

    def restore_pending(self, intent, processed_fills, now_ns):
        """Adopt a journaled submission for queries only; never resubmit it.

        Startup reconciliation must supply the original cached order. If it is
        missing, a reconstructed Initialized order would not prove recovery, so
        the request stays unknown for operator reconciliation.
        """
        if self.closed or intent.action != "submit" or not intent.client_order_id:
            raise ValueError("recovery requires an original submission intent")
        market = self.markets.get(intent.instrument_id)
        if market is None or market.venue != intent.venue or intent.venue not in SUPPORTED_VENUES:
            self._reject(intent, "recovery market is unsupported or mismatched")
            return False
        cid = intent.client_order_id
        if cid in self.orders or intent.request_id in self.requests:
            raise ValueError("pending submission is already registered")
        cached = self._cached(cid)
        self.requests[intent.request_id] = {"intent": intent, "sent_ns": now_ns, "queried_ns": now_ns}
        if cached is None or str(cached.instrument_id) != intent.instrument_id:
            self.last_error = "original pending order is absent from authoritative native recovery; no submission was resent"
            self._receipt(intent, "unknown")
            return False
        processed = Fraction(0)
        for fill in processed_fills:
            if fill["client_order_id"] != cid or fill["instrument_id"] != intent.instrument_id or fill["venue"] != intent.venue:
                raise ValueError("persisted fill differs from its original submission")
            signature = _fill_signature(fill)
            key = (intent.venue, cid, fill["trade_id"])
            if key in self.seen_fills:
                if self.seen_fills[key] != signature:
                    raise ValueError("conflicting persisted execution identity")
                continue
            self.seen_fills[key] = signature
            processed += Fraction(signature["native_qty"])
        self.orders[cid] = {"intent": intent, "processed_qty": processed, "native": cached}
        self.active_request_map[(cid, "submit")] = intent
        try:
            self.strategy.query_order(cached, client_id=self._client(intent.venue))
        except Exception:
            self.last_error = "original pending order query failed; journaled identity retained"
        self._receipt(intent, "unknown")
        return True

    @timed("gateway.native_event", labels=lambda self, kind, *a, **kw: ("", kind))
    def on_native_event(self, kind, event, now_ns):
        cid = str(event.client_order_id)
        owned = self.orders.get(cid)
        if owned is None:
            return
        kind = kind.removeprefix("on_order_").removeprefix("Order").lower()
        self._invalidate_account(owned["intent"].venue)
        event_id = str(getattr(event, "event_id", "") or "")
        if kind != "filled" and event_id:
            identity = (cid, kind, event_id)
            if identity in self.seen_events:
                return
            self.seen_events.add(identity)
        submit = owned["intent"]
        if str(getattr(event, "instrument_id", submit.instrument_id)) != submit.instrument_id:
            raise ValueError("native event market differs from its owned order")
        if kind == "filled":
            trade_id = str(getattr(event, "trade_id", "") or "")
            quantity, price = _decimal(event.last_qty), _decimal(event.last_px)
            side = str(event.order_side).split(".")[-1].upper()
            if not trade_id or quantity <= 0 or price <= 0 or side != submit.side:
                raise ValueError("native fill identity/amount/side is invalid")
            commission = getattr(event, "commission", None)
            fee = _decimal(commission) if commission is not None else None
            currency = str(commission.currency) if commission is not None else None
            fill = {"client_order_id": cid, "trade_id": trade_id, "instrument_id": submit.instrument_id,
                "venue": submit.venue, "side": side, "native_qty": quantity, "native_price": price,
                "execution_id": submit.venue + ":" + cid + ":" + trade_id,
                "quantity": quantity, "price": price,
                "commission_amount": fee, "commission_currency": currency, "received_ns": now_ns}
            key = (submit.venue, cid, trade_id)
            signature = _fill_signature(fill)
            if key in self.seen_fills:
                if self.seen_fills[key] != signature:
                    raise ValueError("conflicting replay of native execution identity")
                return
            self.on_fill(fill)
            self.seen_fills[key] = signature
            owned["processed_qty"] += Fraction(quantity)
            self._receipt(submit, "fill", execution_id=fill["execution_id"], quantity=quantity)
            if any((submit.request_id, terminal, "") in self.emitted for terminal in ("terminal", "rejected")):
                owned.pop("final_query_started", None)
                self._request_final_evidence(owned, self._cached(cid) or owned["native"], now_ns)
            cached = self._cached(cid)
            if cached is not None and cached.is_closed and cid not in self.terminals:
                self.terminals[cid] = ("terminal", submit)
            self._flush_terminal(cid, now_ns)
        elif kind == "accepted":
            self._receipt(submit, "accepted")
        elif kind == "updated":
            intent = self.active_request_map.get((cid, "modify"))
            if intent:
                self._receipt(intent, "updated")
        elif kind in {"modifyrejected", "modify_rejected", "cancelrejected", "cancel_rejected"}:
            action = "modify" if kind.startswith("modify") else "cancel"
            intent = self.active_request_map.get((cid, action))
            if intent:
                self._receipt(intent, "rejected")
        elif kind in {"canceled", "expired", "rejected", "denied"}:
            intent = self.active_request_map.get((cid, "cancel"), submit) if kind == "canceled" else submit
            receipt_kind = ("canceled" if kind == "canceled" and intent is not submit else
                            "rejected" if kind in {"rejected", "denied"} else "terminal")
            self.terminals[cid] = (receipt_kind, intent)
            self._flush_terminal(cid, now_ns)

    def _request_final_evidence(self, owned, cached, now_ns):
        owned["needs_final_query"] = True
        if owned.get("final_query_started"):
            return
        owned["final_query_started"] = True
        submit = owned["intent"]
        self.requests[submit.request_id]["queried_ns"] = now_ns
        try:
            self.strategy.query_order(cached, client_id=self._client(submit.venue))
        except Exception:
            self.last_error = "final original-order query failed; reconciliation remains fenced"

    def _flush_terminal(self, cid, now_ns):
        pending = self.terminals.get(cid)
        if pending is None:
            return
        cached = self._cached(cid)
        owned = self.orders[cid]
        if cached is None or not cached.is_closed:
            return
        if Fraction(_decimal(cached.filled_qty)) != owned["processed_qty"]:
            if any((owned["intent"].request_id, kind, "") in self.emitted
                   for kind in ("terminal", "rejected")):
                self._receipt(owned["intent"], "reconciliation_required")
            return
        final_quantity, final_source = None, ""
        submit = owned["intent"]
        if submit.venue == "LIGHTER":
            # Cache closure and even 0 == 0 are not exchange cumulative evidence.
            # The control is shared with this exact execution client/account.
            control = self.native_controls.get("LIGHTER")
            try:
                raw = control.terminal_fill_evidence_json(cid) if control is not None else None
                proof = json.loads(raw) if raw else None
                if (not proof or proof.get("client_order_id") != cid
                        or proof.get("instrument_id") != submit.instrument_id
                        or proof.get("source") not in {"websocket_terminal_order", "http_order_status"}):
                    self._request_final_evidence(owned, cached, now_ns)
                    return
                if proof.get("conflicting") is not False:
                    self._receipt(submit, "reconciliation_required")
                    self._request_final_evidence(owned, cached, now_ns)
                    return
                if proof["source"] != "http_order_status":
                    self._request_final_evidence(owned, cached, now_ns)
                    return
                final_quantity = _decimal(proof["final_filled_quantity"])
                if (final_quantity < 0 or Fraction(final_quantity) > Fraction(submit.quantity)
                        or Fraction(final_quantity) != owned["processed_qty"]):
                    if any((submit.request_id, kind, "") in self.emitted for kind in ("terminal", "rejected")):
                        self._receipt(submit, "reconciliation_required")
                    return
                final_source = "lighter_" + proof["source"]
            except (AttributeError, RuntimeError, TypeError, ValueError, KeyError, InvalidOperation):
                self.last_error = "Lighter final cumulative fill evidence is unavailable; order remains unresolved"
                self._request_final_evidence(owned, cached, now_ns)
                return
        elif owned["processed_qty"] == Fraction(submit.quantity):
            final_quantity, final_source = _decimal(submit.quantity), "fully_filled_order"
        kind, intent = pending
        already_terminal = any((submit.request_id, k, "") in self.emitted for k in ("terminal", "rejected"))
        self._receipt(intent, kind, final_quantity=final_quantity, final_source=final_source)
        if kind != "rejected" and intent.request_id != owned["intent"].request_id:
            self._receipt(owned["intent"], "terminal", final_quantity=final_quantity, final_source=final_source)
        if final_source == "lighter_http_order_status":
            if not already_terminal:
                self.evidence_receipts[submit.request_id] = ("terminal_evidence", final_quantity, final_source)
                owned["needs_final_query"] = False
            elif self._receipt(submit, "terminal_evidence", final_quantity=final_quantity, final_source=final_source):
                owned["needs_final_query"] = False

    @timed("gateway.poll")
    def poll(self, now_ns):
        if self.closed:
            return
        # Expire before consuming a newly available result: completion after
        # the deadline cannot revive a failed request or satisfy a newer one.
        self._expire_account_reads(now_ns)
        control = self.native_controls.get("BACKPACK")
        if control is not None:
            # Native fill consumption durably checkpoints real cache/receipt facts.
            # Without this ACK Backpack intentionally retains outstanding reservations.
            try:
                wall_ms = time.time_ns() // 1_000_000
                if wall_ms >= self.next_authority_renewal_ms:
                    control.renew_authority(wall_ms + 300_000)
                    self.next_authority_renewal_ms = wall_ms + 30_000
                with span("gateway.backpack.persist_economics"):
                    control.persist_economics()
                with span("gateway.backpack.reconcile_terminals"):
                    control.reconcile_terminal_evidence()
                with span("gateway.backpack.admission_status"):
                    control.admission_status_json()
            except RuntimeError:
                self.last_error = "native economic checkpoint is not ready; admission remains fenced"
        for intent in tuple(self.native_reads.values()):
            self._start_native_read(intent, now_ns)
        for request_id, future in tuple(self.reads.items()):
            if not future.done():
                continue
            if self.requests[request_id].get("read_timed_out"):
                self.reads.pop(request_id)
                continue
            intent = self.requests[request_id]["intent"]
            try:
                snapshot = future.result()
                if not intent.account_check:
                    bid, ask, stamp = self._quote(intent.instrument_id, now_ns)
                    snapshot = replace(snapshot, bid=bid, ask=ask, book_observed_ns=stamp)
            except Exception as exc:
                if future.exception() is None:
                    # Retain the completed account read until a current BBO is
                    # available; never report an aged quote as startup success.
                    continue
                self.reads.pop(request_id)
                self.last_error = str(exc) if type(exc).__name__ == "AccountReadError" else "authoritative account read failed"
                self._receipt(intent, "unknown", retry_after_secs=getattr(exc, "retry_after_secs", 0))
                continue
            self.reads.pop(request_id)
            if not snapshot.authoritative or not snapshot.connected or snapshot.venue != intent.venue or snapshot.instrument_id != intent.instrument_id:
                self._reject(intent, "account reader returned unverified or mismatched evidence")
                continue
            mapping = {}
            for cid, owned in self.orders.items():
                if owned["intent"].venue != intent.venue:
                    continue
                cached = self._cached(cid)
                if cached is not None and cached.venue_order_id is not None:
                    mapping[(owned["intent"].instrument_id, "venue:" + str(cached.venue_order_id))] = cid
            snapshot = replace(snapshot, open_order_ids=tuple(
                mapping.get((intent.instrument_id, i), i) for i in snapshot.open_order_ids),
                account_open_orders=None if snapshot.account_open_orders is None else tuple(
                    (instrument, mapping.get((instrument, cid), cid))
                    for instrument, cid in snapshot.account_open_orders))
            if intent.action == "connect":
                self._receipt(intent, "connected")
            else:
                self._record_outcome(intent, "account")
                self.on_account(snapshot)
        for cid, owned in tuple(self.orders.items()):
            cached = self._cached(cid)
            if cid not in self.terminals and cached is not None and cached.is_closed:
                status = str(cached.status).split(".")[-1].upper()
                if status in {"REJECTED", "DENIED"}:
                    self.terminals[cid] = ("rejected", owned["intent"])
                elif status == "CANCELED" and (cid, "cancel") in self.active_request_map:
                    self.terminals[cid] = ("canceled", self.active_request_map[(cid, "cancel")])
                else:
                    self.terminals[cid] = ("terminal", owned["intent"])
        for cid in tuple(self.terminals):
            self._flush_terminal(cid, now_ns)
        for request in tuple(self.requests.values()):
            intent = request["intent"]
            if intent.action in {"connect", "reconcile"}:
                continue
            passive_accepted = intent.action == "submit" and intent.time_in_force == "GTC" and (intent.request_id, "accepted", "") in self.emitted
            if not self.orders.get(intent.client_order_id, {}).get("needs_final_query") and (
                    passive_accepted or (intent.request_id, "updated", "") in self.emitted or any(
                    (intent.request_id, k, "") in self.emitted for k in ("canceled", "terminal", "rejected"))):
                continue
            if now_ns - request["queried_ns"] >= self.timeout_ns and intent.client_order_id in self.orders:
                request["queried_ns"] = now_ns
                self._receipt(intent, "unknown")
                try:
                    order = self._cached(intent.client_order_id) or self.orders[intent.client_order_id]["native"]
                    self.strategy.query_order(order, client_id=self._client(intent.venue))
                except Exception:
                    self.last_error = "original-ID order query failed; submission remains unresolved"

    @timed("shutdown.gateway_close")
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.submit_timings.clear()
        for future in self.reads.values():
            future.cancel()
        self.worker.shutdown(wait=False, cancel_futures=True)
