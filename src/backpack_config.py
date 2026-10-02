"""Strict, credential-free planning for native Backpack sessions."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

SYMBOL = re.compile(r"[A-Z0-9]+_USDC_PERP")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
MODES = frozenset({"public", "account-readonly", "paper", "replay"})
SOURCES = frozenset({"Configured", "VenueObserved", "Synthetic"})
MAX_CONFIG_BYTES = 65_536


class BackpackConfigError(ValueError):
    """An invalid plan; no runtime has been constructed."""


def _keys(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise BackpackConfigError(f"invalid or missing fields in {label}")


def _text(value, label):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise BackpackConfigError(f"invalid {label}")
    if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BackpackConfigError(f"invalid {label}")
    return value


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise BackpackConfigError(f"{label} must be an integer in [{low}, {high}]")
    return value


def _decimal(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise BackpackConfigError(f"{label} must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise BackpackConfigError(f"invalid {label}") from None
    digits = result.as_tuple().digits
    scale = max(0, -result.as_tuple().exponent)
    if scale > 28 or len(digits) > 29:
        raise BackpackConfigError(f"{label} exceeds native Decimal precision")
    coefficient = int(''.join(map(str, digits)))
    if coefficient > 79_228_162_514_264_337_593_543_950_335:
        raise BackpackConfigError(f"{label} exceeds native Decimal precision")
    return result


def _loopback(value, schemes, label):
    _text(value, label)
    try:
        parsed = urlsplit(value)
        host = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise BackpackConfigError(f"{label} must be a numeric loopback origin") from None
    if (parsed.scheme not in schemes or not host.is_loopback or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or port == 0 or '%' in value):
        raise BackpackConfigError(f"invalid {label}")
    return value.rstrip('/')


@dataclass(frozen=True)
class Economics:
    margin_init: Decimal
    margin_maint: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    source: str
    source_reference: str

    @classmethod
    def parse(cls, value):
        fields = {"margin_init", "margin_maint", "maker_fee", "taker_fee", "source", "source_reference"}
        _keys(value, fields, fields, "economics")
        result = cls(*(_decimal(value[k], k) for k in (
            "margin_init", "margin_maint", "maker_fee", "taker_fee")),
            _text(value["source"], "economic source"),
            _text(value["source_reference"], "economic source reference"))
        if (not Decimal(0) <= result.margin_maint <= result.margin_init <= Decimal(1)
                or result.maker_fee.copy_abs() > 1 or result.taker_fee.copy_abs() > 1
                or result.source not in SOURCES):
            raise BackpackConfigError("invalid economics or source")
        return result

    def document(self):
        return {"margin_init": format(self.margin_init, "f"), "margin_maint": format(self.margin_maint, "f"),
                "maker_fee": format(self.maker_fee, "f"), "taker_fee": format(self.taker_fee, "f"),
                "source": self.source, "source_reference": self.source_reference}


@dataclass(frozen=True, repr=False)
class AccountScope:
    account_id: str
    venue_account: str
    subaccount: str | None
    credential_env: str

    @classmethod
    def parse(cls, value):
        fields = {"account_id", "venue_account", "subaccount", "credential_env"}
        _keys(value, fields, fields - {"subaccount"}, "account")
        account_id = _text(value["account_id"], "account_id")
        if not re.fullmatch(r"BACKPACK-[A-Za-z0-9_.-]+", account_id):
            raise BackpackConfigError("account_id must use the BACKPACK namespace")
        env = _text(value["credential_env"], "credential environment reference")
        if not ENV_NAME.fullmatch(env):
            raise BackpackConfigError("credential_env must name an environment variable")
        return cls(account_id, _text(value["venue_account"], "venue account"),
                   _text(value["subaccount"], "subaccount") if "subaccount" in value else None, env)

    def __repr__(self):
        return "AccountScope([REDACTED])"


@dataclass(frozen=True)
class BackpackSessionPlan:
    mode: str
    environment: str
    symbols: tuple[str, ...]
    economics: tuple[tuple[str, Economics], ...]
    duration_secs: int
    request_timeout_secs: int
    stale_after_ms: int
    max_report_events: int
    max_report_bytes: int
    output_root: Path
    state_root: Path
    http_origin: str | None
    ws_origin: str | None
    replay_file: Path | None
    account: AccountScope | None = field(repr=False)

    @property
    def namespace(self):
        # Keep mutable credentials and run IDs out of durable identity.
        scope = ["BACKPACK", self.environment, self.http_origin, self.ws_origin,
                 None if self.account is None else self.account.venue_account,
                 None if self.account is None else self.account.subaccount]
        digest = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        return f"backpack-{self.environment}-{digest}"

    @property
    def journal_dir(self):
        return self.state_root / self.namespace

    @property
    def output_dir(self):
        return self.output_root / self.namespace / self.mode

    def document(self):
        return {
            "schema_version": 1, "venue": "BACKPACK", "mode": self.mode,
            "environment": self.environment, "symbols": list(self.symbols),
            "instrument_ids": [f"{symbol}.BACKPACK" for symbol in self.symbols],
            "economics": {symbol: value.document() for symbol, value in self.economics},
            "duration_secs": self.duration_secs, "request_timeout_secs": self.request_timeout_secs,
            "stale_after_ms": self.stale_after_ms, "max_report_events": self.max_report_events,
            "max_report_bytes": self.max_report_bytes,
            "output_dir": str(self.output_dir), "journal_dir": str(self.journal_dir),
            "http_origin": self.http_origin, "ws_origin": self.ws_origin,
            "replay_file": None if self.replay_file is None else str(self.replay_file),
            "namespace": self.namespace, "authenticated_runtime_requested": self.account is not None,
            "account_identity_verified": False, "execution_ready": False,
            "remote_writes_allowed": False, "runtime_started": False,
        }


def parse_plan(document, base_dir: Path) -> BackpackSessionPlan:
    fields = {"schema_version", "mode", "environment", "symbols", "economics", "account",
              "duration_secs", "request_timeout_secs", "stale_after_ms", "max_report_events",
              "max_report_bytes", "output_dir", "state_dir", "base_url_http", "base_url_ws", "replay_file"}
    required = {"schema_version", "mode", "environment", "symbols", "duration_secs", "output_dir", "state_dir"}
    _keys(document, fields, required, "session")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise BackpackConfigError("unsupported schema_version")
    mode, environment = document["mode"], document["environment"]
    if not isinstance(mode, str) or mode not in MODES:
        raise BackpackConfigError("unsupported mode")
    symbols = document["symbols"]
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 32
            or any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise BackpackConfigError("symbols must be a nonempty, unique USDC perpetual allowlist")
    if environment not in ("offline", "production", "loopback"):
        raise BackpackConfigError("unsupported environment")
    if (mode == "replay") != (environment == "offline"):
        raise BackpackConfigError("replay requires offline environment; other modes require a live data origin")
    urls = (document.get("base_url_http"), document.get("base_url_ws"))
    if environment == "loopback":
        http_origin = _loopback(urls[0], {"http", "https"}, "HTTP origin")
        ws_origin = _loopback(urls[1], {"ws", "wss"}, "WS origin")
    else:
        if any(key in document for key in ("base_url_http", "base_url_ws")):
            raise BackpackConfigError("endpoint overrides require explicit loopback environment")
        http_origin = "https://api.backpack.exchange" if environment == "production" else None
        ws_origin = "wss://ws.backpack.exchange" if environment == "production" else None
    account = AccountScope.parse(document["account"]) if "account" in document else None
    if (mode == "account-readonly") != (account is not None):
        raise BackpackConfigError("only account-readonly requires and accepts an account section")
    if (mode == "replay") != ("replay_file" in document):
        raise BackpackConfigError("only replay requires and accepts replay_file")
    econ = document.get("economics", {})
    if not isinstance(econ, dict) or set(econ) != set(symbols):
        raise BackpackConfigError("explicit economics must cover exactly the symbol allowlist")
    def path(key):
        value = Path(_text(document[key], key))
        return (base_dir / value).absolute() if not value.is_absolute() else value
    duration = _integer(document["duration_secs"], 1, 600, "duration_secs")
    timeout = _integer(document.get("request_timeout_secs", 15), 1, 60, "request_timeout_secs")
    if timeout > duration:
        raise BackpackConfigError("request timeout must fit the session duration")
    return BackpackSessionPlan(mode, environment, tuple(symbols),
        tuple((symbol, Economics.parse(econ[symbol])) for symbol in symbols), duration, timeout,
        _integer(document.get("stale_after_ms", 3000), 1, 30_000, "stale_after_ms"),
        _integer(document.get("max_report_events", 10_000), 1, 100_000, "max_report_events"),
        _integer(document.get("max_report_bytes", 16_777_216), 1024, 67_108_864, "max_report_bytes"),
        path("output_dir"), path("state_dir"), http_origin, ws_origin,
        path("replay_file") if mode == "replay" else None, account)


def load_plan(path: Path) -> BackpackSessionPlan:
    # No implicit .env lookup, output creation, replay read or native import.
    if path.name == ".env" or path.name.startswith(".env."):
        raise BackpackConfigError("dotenv files are not session configuration")
    with path.open("rb") as stream:
        payload = stream.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise BackpackConfigError("configuration exceeds 64 KiB")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise BackpackConfigError("invalid configuration encoding or TOML") from None
    return parse_plan(document, path.absolute().parent)
