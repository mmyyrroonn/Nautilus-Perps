# Build ownership release

Astra releases native source and build ownership to Pi for control/pi-build-task.md.

Native final review is **accepted**: review/final-native-review.md. Production source is frozen; all implementation agents have released it. Full crate901 passed; Python-feature6 passed; four ownership regressions independently rerun by Astra; strengthened actual-disconnect/journal-restart/original-ID recovery test independently rerun by Astra (1 passed), and its whole private_runtime target49 passed. Windows Cargo test warning exception remains documented.

Proceed with the release build, generated stubs, dedicated candidate environment, full app tests, runtime capability and bounded public installation probe. You alone own the release build cache. Do not alter accepted production code; if build finds a defect, report the concrete cause and coordinate a narrow correction before rebuilding.

Do not install into canonical main venv or apply source changes to main checkouts. Those are Astra's final integration gate. No real private/sandbox requests or writes. This is a build release, not a trading authorization.
