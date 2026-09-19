# Canonical source and runtime integration

Completed by Astra after candidate acceptance on 2026-09-19.

- Verified canonical app HEAD `aef07e4e7d7e32fedf9acd8a6cba63700d092b6e` and fork HEAD `ff57243f74d730b1ababca7ee8e7b37e7c840287` had not moved. The explicitly selected target files had no concurrent content changes.
- Exported `control/app-final.patch` and `control/fork-final.patch`; both passed `git apply --check` against canonical checkouts. Copied only the approved 5 app paths and 7 fork paths. Every copied file is byte-identical to the validated worktree (`canonical-source-sync.json`). No commit, branch merge, push or worktree deletion.
- Existing unrelated files were outside the copy allowlist. The pre-copy `git diff --name-only HEAD` reported zero unrelated content differences despite earlier CRLF-only status entries; it did not yield a byte-hash baseline for those entries. Preservation rests on the explicit path allowlist, not a claimed 40-file hash comparison.
- Corrected a stale `strict build (0 warnings)` comment in `pyproject.toml` and two inaccurate build-report statements before copying. The actual strict attempt failed on the MSVC linker-message warning; scoped per-command allowances are recorded in scripts, with no global warning configuration change. These were documentation-only corrections.
- Installed only `nautilus_trader` into `E:/Nautilus-Perps/.venv` using `uv pip install --python <canonical python> --no-deps --reinstall-package nautilus_trader <verified wheel>`. Other dependencies were not replaced. The old `dist-r5` artifact remains available.
- Independently verified main interpreter/package origin, exact wheel SHA256, installed native binary and stub hashes against the wheel, read-only boolean capability, and the no-client/no-request public dry-run (`canonical-runtime-identity.json`).
- Ran full tests from the canonical app directory and canonical interpreter with dotenv disabled: **836 passed, 1 pre-existing warning, 97 subtests passed in 16.00 s**, exit 0 (`canonical-app-tests.txt`).

Final artifact: `E:/nautilus_trader/dist-r52/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`, SHA256 `4d19c5df5bec73391a760d4b8643e2f8eaa53ce183dca88ee917454e2b91d642`.
Native installed binary SHA256: `8a0b775a94f17234c117550d3fc3e9df0f63f3cca04b3afe940317598e49dc05`.
Installed Ondo stub SHA256: `aefa6efeed20bdba711117b051c98e48a531b3f150246ca421bad9007aac76f8`.

This establishes local source/runtime delivery and offline/public acceptance. Private sandbox protocol and execution remain unverified; no live services or private venue actions were started.
