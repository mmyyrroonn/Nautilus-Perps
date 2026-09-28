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

The [final clean Windows/Ubuntu installation run](https://github.com/mmyyrroonn/Nautilus-Perps/actions/runs/36389550745) checked out application commit a001e903c5cece7075354c59a7106093dfe1ba91. Both runners downloaded the published wheels and provenance from HTTPS using the formal lock, installed into exact uv-managed CPython 3.12.9 environments, verified native origin/import/binary/stubs/Ondo capability and passed all offline tests. The formal lock byte SHA-256 was identical on both platforms: 324d00eaf5e26220aa32a0d39b2b9540c331e5fda2611f71d5d6ede3a7336c27.

| Platform | Install run ID | Integration run ID | Native binary SHA-256 | Result | Records |
| --- | --- | --- | --- | --- | --- |
| Windows amd64 | a43e9a90d67c4e68b98218cf980119ef | 1a9c793601594e458a69bb544616897e | 9bff545f77f0450a7e858719394b9fe9e08b6568bcb40add13cafa319e56c600 | 1188 passed, 97 subtests, 1 warning | [installed](issue2-20260928/portable-windows-installed.json), [integration](issue2-20260928/portable-windows-integration.json) |
| Linux x86_64 | ed276a6f1fab4a1596208777b8b15a7f | 751e2bfb72384c56a97cbd0c70191a17 | e190df33e65b3b4a1ee7a3049698abd9a58bdfa0433860bad3f51595fb93fb40 | 1188 passed, 97 subtests, 1 warning | [installed](issue2-20260928/portable-linux-installed.json), [integration](issue2-20260928/portable-linux-integration.json) |

All four hosted records bind the same application and native commits and have exit code 0. The single warning is an existing PytestReturnNotNoneWarning from tests/test_maker_live.py::test_limits returning a Limits object; it is unrelated to native installation. The evidence JSON files were scanned for credential shapes with zero matches.

The first build workflow's artifact upload omitted hidden .native-cache result files by default, although its job logs record both 1190-test build-stage summaries. The build workflow upload setting was corrected in the application repository. This final portable workflow uploaded its install and integration JSON files successfully.

## Historical evidence

The [local pre-publication record](issue2-20260928-acceptance.md) and the frozen 2026-09-23 candidate remain separate. Matching version strings do not equate their wheel bytes or provenance.