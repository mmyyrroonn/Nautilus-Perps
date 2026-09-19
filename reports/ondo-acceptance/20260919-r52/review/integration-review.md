# Astra application integration review

Status: accepted at source/test level; new native wheel build and candidate runtime validation remain required.

- Reviewed the native read-only factory capability getter and application detection. Missing/false/wrong-type/error cases remain unavailable; no support inference from package version, filename or source checkout.
- Required and verified correction of zero-orders/DMS conflation: unobserved native cancel/confirm/release actions now use null, because the probe has no native per-run StopReport telemetry. Capability is separate from a clean-account result.
- Required and verified that private protocol uncertainty no longer denies already-observed public connectivity. Protocol acceptance and clean-account verdict remain false.
- Independently reran probe tests from the app worktree with the canonical R5 interpreter and dotenv disabled: **132 passed in 2.32 seconds**, exit 0 (`integration-probe-tests.txt`). Pi's full corrected app run: 836 passed, 97 subtests, one existing maker-test warning.
- App `git diff --check` passed. Old R5 installed-wheel dry-run retains capability unavailable; new native getter still needs generated stub and real candidate-wheel verification.

The data acceptance source modules and historical inputs were not changed by this reporting integration.
