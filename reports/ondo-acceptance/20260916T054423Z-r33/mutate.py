"""Apply one R3.3 mutation, or verify the tree is byte-identical to the snapshot.

Usage: python mutate.py <check|M9|M10|M11|M12|M13>

Every replacement is asserted to match exactly once, so a mutation that silently
failed to apply (or applied to the wrong place) aborts instead of producing a
run that looks green for the wrong reason.
"""

import pathlib
import sys

ROOT = pathlib.Path("E:/nautilus_trader/crates/adapters/ondo")

R = "src/reconciliation.rs"
E = "src/execution.rs"

MUTATIONS = {
    # The lapse projection in state_at: an armed switch whose deadline has passed.
    "M9": (
        R,
        """        match &self.state {
            DeadMansSwitchState::Armed if self.has_expired(now) => DeadMansSwitchState::Lapsed {
                expired_at: self.expires_at.unwrap_or(now),
            },
            state => state.clone(),
        }""",
        """        let _ = now;
        self.state.clone()""",
    ),
    # The write that moves the deadline: dropped, so only the confirmation moves it.
    "M10": (
        R,
        """    pub fn note_renew_sent(&mut self, now: UnixNanos) {
        self.failed_renewals = 0;

        if matches!(self.state, DeadMansSwitchState::Armed) {
            self.expires_at = Some(UnixNanos::from(
                now.as_u64() + self.timeout_seconds * 1_000_000_000,
            ));
            self.renewals += 1;
        }
    }""",
        """    pub fn note_renew_sent(&mut self, now: UnixNanos) {
        let _ = now;
        self.failed_renewals = 0;
    }""",
    ),
    # The release gate: the switch is released whether or not the writes settled.
    "M11": (
        E,
        """        if report.steps.contains(&StopStep::ReleaseDeadMansSwitch)
            && report.unconfirmed_cancels.is_empty()
            && report.unknown_submissions.is_empty()
        {""",
        """        if report.steps.contains(&StopStep::ReleaseDeadMansSwitch) {""",
    ),
    # The stop's own record: the outstanding writes are no longer checkpointed.
    "M12": (
        E,
        """        self.account.persist_journal(self.account.now());

        report
    }""",
        """        report
    }""",
    ),
    # The read-only marking: a read-only config is no longer marked, so nothing stops it.
    "M13": (
        E,
        """        if config.account_read_only {
            reconciliation.write().mark_account_read_only();
        }

""",
        "",
    ),
}


def main() -> int:
    which = sys.argv[1]

    if which == "check":
        ok = True
        for name, (rel, old, _) in MUTATIONS.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            count = text.count(old)
            status = "intact" if count == 1 else f"NOT FOUND x{count}"
            if count != 1:
                ok = False
            print(f"  {name}: {rel} anchor {status}")
        return 0 if ok else 1

    rel, old, new = MUTATIONS[which]
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    count = text.count(old)

    if count != 1:
        print(f"ABORT: {which} anchor found {count} times in {rel}")
        return 2

    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"applied {which} to {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
