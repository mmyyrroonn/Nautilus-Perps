# Issue 3 local acceptance, 2026-09-28

## Candidate

- Primary Issue 3 patch base: current main `b3c067457b3c868a07f063e941fe7199b734f56d`; the Issue 3 changes are local and uncommitted. The existing PR #7 branch `b66e83279d4440843217f2ce390b79060ce9877b` was also checked separately.
- Native commit: `eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1`, from the published source-bound candidate.
- Windows wheel SHA-256: `802e8eead8ca24ca52324eade773b20ea9dd5f9c8701ccfd628c32bca42a0783`.
- The formal wheel was installed into an isolated CPython 3.12.9 environment from the local release-stage artifact and matching provenance. The installer verified source identity, wheel origin, ABI, imported native binary, adapter stubs and Ondo capability. A separate worktree at the exact native SHA provided the local source check.
- The default application `.venv` still contains the historical wheel `8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985` and was not used as positive candidate evidence.

## Checks

| Check | Result | Boundary |
| --- | --- | --- |
| Installed-wheel LiveNode against loopback Aster HTTP/WS, including the existing application account reporter | Passed in a fresh subprocess with the formal candidate | No external venue or real order; fault, stop/dispose, listen-key DELETE and zero order requests observed |
| Complete application suite in a clean worktree without `.env` | `1191 passed, 97 subtests passed`, zero skips and warnings | Windows local check; CI matrix still required |
| Main plus only the Issue 3 patch, including the false pytest helper rename | `1188 passed, 97 subtests passed`, zero skips and warnings; required LiveNode node collected | Primary proposed change; formal Windows wheel, no `.env`; local checkout still uncommitted |
| Mainline merge preview (`origin/main` `b3c0674` plus PR #7 `b66e832`, no commit) with the new LiveNode test | `1191 passed, 97 subtests passed`, zero skips | Alternative branch combination checked; no merge conflict, no `.env` |
| Enhanced integration runner with the same wheel and both expected full SHAs | Passed; JUnit, collection record and full stdout/stderr logs written under ignored `.native-cache` | Local run used no enforced network namespace or firewall; result records `not-enforced` |
| Wrong expected wheel SHA-256 | Failed at `candidate_identity`, before pytest, with a machine-readable result | Negative identity check |
| Detailed joint audit with hosted native-check and network-guard records absent | Failed in `native_checks` and `offline_network` layers; local build/install/integration layers passed | Confirms local evidence cannot satisfy the hosted gate |
| Local Windows offline guard smoke | Failed closed before the guarded command; cleanup confirmed no rule remained | This local account lacks firewall administration permission (`Access is denied`); hosted Windows runner must prove the guard |
| Historical wheel against the new LiveNode fixture | Explicitly skipped with both wheel hashes | A skip is not candidate acceptance |

The full-suite run initially exposed process state left by an earlier client-less LiveNode test: the new lifecycle fixture failed only when run afterward. The fixture now executes its real native lifecycle in a fresh child interpreter, and the complete suite passed. The default credential-free workflow requires the exact LiveNode test node, at least 1188 passing items and 97 subtests, and zero skipped tests. The 1188-item primary patch count includes the new LiveNode test and removes one pre-existing pytest-collected helper that returned a value and emitted a warning.

## Remaining acceptance

The new two-ref Windows/Linux workflow has not yet run. Its hosted artifacts must bind resolved application/native SHAs, the wheel and imported native module hashes, native checks, the complete application count, zero skips, and logs. Historical Issue 2 hosted runs prove portable installation for an earlier application SHA; they do not prove this new LiveNode fixture. No private DMS ACK or live venue write is claimed here.