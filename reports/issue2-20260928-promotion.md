# Issue 2 native candidate promotion, 2026-09-28

## Source and published inputs

- Native source: mmyyrroonn/nautilus_trader commit eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1, tree 348ec51bb837297ee4b706c7cc9dcbab6a9621f9. Both provenance files report source_binding=verified and dirty_count=0.
- Controlled builder SHA-256: 7f94cb247ddbc432779b03e97f87a75703df882fa39bee359413ae2e69d98b70. Both builds used the release profile and the same maturin feature list.
- [Clean Windows and Ubuntu build plus offline tests](https://github.com/mmyyrroonn/Nautilus-Perps/actions/runs/36380868905): both jobs succeeded, each reporting 1190 tests and 97 subtests passed against application commit 389e4efadbda8ccb445b28f991a9d6f33dc0c4f1.
- [Public native release](https://github.com/mmyyrroonn/nautilus_trader/releases/tag/native-candidate-eeeb8eefc7) contains both wheels, platform-specific provenance and separate build-input JSON files. Four JSON release inputs were scanned for credential shapes with zero matches.
- Windows cp312-cp312-win_amd64 wheel SHA-256: 802e8eead8ca24ca52324eade773b20ea9dd5f9c8701ccfd628c32bca42a0783.
- Linux cp312-cp312-manylinux_2_39_x86_64 wheel SHA-256: c8a09a3e9c04e735a8b3177fe2d85925afaf56ecaa2658089a6834c66c4dfa73. Supported Linux is x86_64 with glibc 2.39 or newer.
- [Formal two-platform candidate lock](../config/native-candidate.lock.json) pins the public wheel and provenance HTTPS URLs plus SHA-256 values. The lock generator checked actual wheel bytes, tags, matching source fingerprints, builder, features and profile.

## Formal download path, local verification

- Windows: a separate application venv downloaded the hosted Windows wheel and provenance from the public release, installed with the exact CPython 3.12.9 interpreter and verified source binding, origin, ABI, native binary, adapter stubs and Ondo capability. The default-branch integration worktree passed 1188 tests and 97 subtests (one pytest warning).
- Linux: a separate uv-managed CPython 3.12.9 WSL/Ubuntu environment downloaded the hosted Linux wheel and provenance from the public release and passed the same install checks. The application acceptance branch passed 1190 tests and 97 subtests.
- The WSL run against a Windows-created Git worktree could not read that worktree's E: Git metadata; the successful Linux run used the normal application Git checkout. This was a local test-directory limitation after the wheel had installed.

## Portable hosted installation

The dedicated clean Windows/Ubuntu installation workflow will run against the committed formal lock. Its run IDs and separate installed/integration JSON records will be added here after completion. The first build workflow's artifact upload omitted the hidden .native-cache result files by default; its job logs establish both test summaries. The workflow upload setting has been corrected for later runs.

## Historical evidence

The [local pre-publication record](issue2-20260928-acceptance.md) and the frozen 2026-09-23 candidate remain separate. Matching version strings do not equate their wheel bytes or provenance.