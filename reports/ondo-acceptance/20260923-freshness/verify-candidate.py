"""Bind the freshness-diagnostics wheel, isolated installation and frozen source.

This is the *generator* for this candidate's identity: it records the hashes of the exact
bytes it verified. It uses no network and reads no credential. Run it with the isolated
worktree interpreter, from this directory:

    .venv\\Scripts\\python.exe verify-candidate.py

The DMS/authority suffix hash changes with this candidate by design. Unlike the previous
candidate's checker, this one does not compare the suffix against a frozen value: it records
the new one and states what moved. The release state machine and release predicate are not
part of this candidate's edits; the release tests are unchanged and pass.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import importlib
import json
from pathlib import Path
import sys
import zipfile

REPORT = Path(__file__).resolve().parent
APP = REPORT.parents[2]
FORK = APP.parent / "ondo-btc-native"
PREVIOUS_IDENTITY = APP / "reports/ondo-acceptance/20260922-btc-functional/candidate-identity.json"
CANDIDATE_KIND = "btc-freshness-diagnostics"
CHANGE_NOTE = (
    "relative to the btc-functional candidate: (1) the production admission refusal now names "
    "the failed conditions in a fixed label vocabulary instead of a conflated sentence, and the "
    "decision itself is unchanged (the same conjunction of the same conditions); (2) production "
    "reading freshness is measured from the instant the last reconciliation pass concluded with "
    "a 15-second window, instead of from AccountReading::read_at with a 5-second window that was "
    "shorter than one rate-limited pass; (3) nightly rustfmt normalization of files the previous "
    "candidate left unformatted. No DMS release predicate, DMS state machine, risk bound or "
    "reconciliation judgment was changed."
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--write", action="store_true", help="record candidate-identity.json")
    args = parser.parse_args()

    assert Path(sys.prefix).resolve() == (APP / ".venv").resolve(), "use the isolated venv"
    wheel = args.wheel.resolve()
    dist = metadata.distribution("nautilus_trader")
    assert json.loads(dist.read_text("direct_url.json"))["url"] == wheel.as_uri()

    with zipfile.ZipFile(wheel) as archive:
        binaries = [
            name
            for name in archive.namelist()
            if name.startswith("nautilus_trader/") and name.endswith(".pyd")
        ]
        assert len(binaries) == 1
        binary = archive.read(binaries[0])
        installed_binary = Path(dist.locate_file(binaries[0])).resolve()
        assert installed_binary.is_relative_to((APP / ".venv").resolve())
        assert installed_binary.read_bytes() == binary
        imported_binary = importlib.import_module("nautilus_trader._libnautilus")
        assert Path(imported_binary.__file__).resolve() == installed_binary
        stub_name = "nautilus_trader/adapters/ondo/__init__.pyi"
        stub = archive.read(stub_name)
        assert stub == (FORK / "python" / stub_name).read_bytes()
        assert stub == Path(dist.locate_file(stub_name)).read_bytes()

    source_rows = json.loads(args.source_manifest.read_text(encoding="utf-8-sig"))
    for row in source_rows:
        assert sha((FORK / row["path"]).read_bytes()).lower() == row["sha256"].lower(), row["path"]

    production = (FORK / "crates/adapters/ondo/src/production.rs").read_bytes()
    suffix = production[production.index(b"#[derive(Clone, Debug, Default)]"):]

    previous = json.loads(PREVIOUS_IDENTITY.read_text(encoding="utf-8"))
    app_rows = []
    for row in previous["app_source_files"]:
        app_rows.append({"path": row["path"], "sha256": sha((APP / row["path"]).read_bytes())})

    from nautilus_trader.adapters.ondo import OndoExecutionClientFactory

    factory = OndoExecutionClientFactory()
    assert factory.supports_production_trade_envelope is True
    assert factory.production_trade_snapshot() is None
    assert factory.read_only_snapshot() is None

    identity = {
        "version": "2.0.0rc4",
        "candidate_kind": CANDIDATE_KIND,
        "change_note": CHANGE_NOTE,
        "wheel": str(wheel),
        "wheel_sha256": sha(wheel.read_bytes()),
        "binary_sha256": sha(binary),
        "stub_sha256": sha(stub),
        "native_source_manifest": str(args.source_manifest.resolve()),
        "frozen_source_files_verified": len(source_rows),
        "app_source_files": app_rows,
        "dms_authority_suffix_sha256": sha(suffix),
        "dms_authority_suffix_previous_sha256": previous["dms_authority_suffix_sha256"],
        "dms_release_host_contract_verified": False,
        "production_execution_verified": False,
        "credentials_read": False,
        "network_started": False,
    }
    if args.write:
        out = REPORT / "candidate-identity.json"
        out.write_text(
            json.dumps(identity, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {out}")
        print(f"wheel_sha256={identity['wheel_sha256']}")
        print(f"binary_sha256={identity['binary_sha256']}")
        print(f"stub_sha256={identity['stub_sha256']}")
        print(f"identity_sha256={sha(out.read_bytes())}")
        print(f"dms_authority_suffix_sha256={identity['dms_authority_suffix_sha256']}")
        print(f"frozen_source_files_verified={identity['frozen_source_files_verified']}")
    else:
        print(json.dumps(identity, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
