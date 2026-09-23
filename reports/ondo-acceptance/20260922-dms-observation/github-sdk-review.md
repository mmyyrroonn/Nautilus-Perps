# Public SDK investigation — 2026-09-22

Scope: read public GitHub source only. No third-party package was installed or executed, no
private trace was downloaded, and no account/DMS request was made. SDK behavior is evidence
about that implementation, not an exchange guarantee.

## LI.FI TypeScript provider

Repository: [lifinance/perps-sdk](https://github.com/lifinance/perps-sdk).
Reviewed commit: `e830784734ea829629e6a11d8f09cdbf1944a859` (2026-09-21).

- [OndoWsProvider.ts, releaseWire](https://github.com/lifinance/perps-sdk/blob/e830784734ea829629e6a11d8f09cdbf1944a859/packages/perps-sdk-provider-ondo/src/websocket/OndoWsProvider.ts#L349):
  removing the last local reference sends a normal unsubscribe request. The function does not
  wait for an acknowledgement; the final authenticated wire also cycles the connection.
- [Message dispatch](https://github.com/lifinance/perps-sdk/blob/e830784734ea829629e6a11d8f09cdbf1944a859/packages/perps-sdk-provider-ondo/src/websocket/OndoWsProvider.ts#L593):
  non-error messages other than updates are ignored. The update dispatch has no DMS case.
- [Wire types](https://github.com/lifinance/perps-sdk/blob/e830784734ea829629e6a11d8f09cdbf1944a859/packages/perps-sdk-provider-ondo/src/types/ws.ts#L78)
  include a generic unsubscribed variant, but define no DMS update payload.
- [Unit tests](https://github.com/lifinance/perps-sdk/blob/e830784734ea829629e6a11d8f09cdbf1944a859/packages/perps-sdk-provider-ondo/src/websocket/OndoWsProvider.unit.spec.ts#L243)
  assert ordinary market-channel teardown sends an unsubscribe request. The reviewed 1,336-line
  test file has no DMS channel or timeout field, and supplies no DMS release ACK fixture.

Conclusion: this is a real third-party Ondo provider, but it does not implement or verify the
DMS lifecycle. Copying its teardown behavior would not solve our acceptance requirement.

## RITMEX TypeScript exchange gateway

Repository: [discountry/ritmex-bot](https://github.com/discountry/ritmex-bot).
Reviewed commit: `0a757985b87cd9e0733800da8bb584820ed749de`.

The [Ondo gateway message handler](https://github.com/discountry/ritmex-bot/blob/0a757985b87cd9e0733800da8bb584820ed749de/src/exchanges/ondoperps/gateway.ts#L637)
handles login, errors, and channel updates. Its channel dispatch and subscription setup include
market data, orders, positions, and balance, but no DMS operation. The complete reviewed gateway
file has neither the DMS channel nor the timeout field. It does not supply a release-ACK parser
or an independent successful-release fixture. It is useful as another integration reference,
not as confirmation of DMS semantics.

## DD-PERP Python client

Repository: [Dazmon88/DD-PERP-Strategy](https://github.com/Dazmon88/DD-PERP-Strategy).
Reviewed commit: `fe5d6d606e2f2f0820b06f30796402f9da02d71d`.

The [private-channel allowlist](https://github.com/Dazmon88/DD-PERP-Strategy/blob/fe5d6d606e2f2f0820b06f30796402f9da02d71d/exchange/exchange_ondoperp/ondoperp_protocol/perps_wss.py#L35)
contains the DMS channel, and generic subscription methods accept extra fields. However,
[unsubscribe](https://github.com/Dazmon88/DD-PERP-Strategy/blob/fe5d6d606e2f2f0820b06f30796402f9da02d71d/exchange/exchange_ondoperp/ondoperp_protocol/perps_wss.py#L198)
sends the request and immediately removes the channel callback, without waiting for a response.
The [message dispatcher](https://github.com/Dazmon88/DD-PERP-Strategy/blob/fe5d6d606e2f2f0820b06f30796402f9da02d71d/exchange/exchange_ondoperp/ondoperp_protocol/perps_wss.py#L143)
forwards messages by type/channel; it has no DMS state, deadline, or release-ACK validation.
Thus a supported channel name is not a verified DMS lifecycle implementation.

## Other leads and search limits

- `api-evangelist/ondo-finance`, `Amal-David/docingest`: indexed matches are copied/generated
  specifications or document mirrors, not independent DMS runtime evidence.
- A security-research changelog mentions the channel, but it is not an SDK or a release-ACK
  implementation. Its private traces were not fetched and no security probes were reproduced.

Searches included the exact DMS channel, its timeout field, the WebSocket login-signing literal,
and the production API hostname. No independent verified DMS release ACK implementation was
found in the reviewed results. This is a bounded search result, not proof that none exists.

## Official cross-check

The [shared WebSocket schema](https://docs.ondoperps.xyz/api-reference/ws-spec.json) includes
generic subscribed/unsubscribed messages and their channel field. The DMS endpoint separately
lists an update with unstructured data. Neither proves an arbitrary update acknowledges release.

The [integration guide](https://docs.ondoperps.xyz/api-reference/integration_guide.md) links an
[older Notion WebSocket page](https://www.notion.so/Common-WebSocket-API-2ff278059c7f81c9ac10c8ffeb8b64dc?pvs=21).
Its public HTML contained only the application shell; an unauthenticated read of that page's
content returned no blocks. No login or private-workspace access was attempted.
