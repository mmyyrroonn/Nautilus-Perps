"""Narrow identity quarantines shared by discovery and scanner validation.

This is not a general asset identity registry or certification of other symbols.
"""
from __future__ import annotations


LIGHTER_PRL_QUARANTINE_CODE = "suspected_ticker_collision_asset_identity_unverified"
LIGHTER_PRL_QUARANTINE_REASON = (
    "Lighter PRL (official name Pearl) is quarantined: suspected ticker collision / "
    "asset identity unverified against ticker-only crypto anchors"
)


def identity_quarantine_reason(venue: str, symbol: str, instrument_id: str) -> str | None:
    """Reject Lighter PRL by canonical symbol or native ID, including aliases.

    Official evidence: https://mainnet.zklighter.elliot.ai/api/v1/tokenlist
    Its PRL row names Pearl. This does not establish the identity of Aster PRL,
    nor prove a unit conversion between the two venues. Overrides cannot clear
    this quarantine until an explicit identity review changes the policy.
    """
    if venue == "LIGHTER" and (
        symbol.upper() == "PRL" or instrument_id.upper() == "PRL-PERP.LIGHTER"
    ):
        return LIGHTER_PRL_QUARANTINE_REASON
    return None
