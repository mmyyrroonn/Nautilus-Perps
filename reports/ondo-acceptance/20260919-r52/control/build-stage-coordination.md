# Astra coordinator update: avoid invalidating the release cache for stub generation

This is an authorized coordination update for the active Pi build task. Keep the current release build running to completion; do not restart or kill it.

Before launching run-stubs.sh, inspect the repository generator and BUILD_WINDOWS.md. The documented working Windows stub procedure uses NAUTILUS_STUB_PROFILE=nextest. Stub signatures should not depend on optimization profile. If this is confirmed from the generator, use that established nextest profile with a SEPARATE existing target cache (E:/nautilus_trader/target) for the generator, leaving E:/nautilus_trader/.worktrees/task-ondo-r5/target reserved for the release wheel. All prior native test owners have released the former target.

Purpose: the prior R5 sequence spent roughly22 minutes generating optimized stubs and another22 minutes rebuilding the release library after switching feature sets in one cache. Those extra optimization passes do not improve the generated API declarations. Use the repository's generator unchanged; no manual stub edits, no source changes, no cache deletion and no reduction in candidate tests. Record exact chosen profiles/targets and verify the generated supports_ordered_shutdown getter plus final runtime/stub agreement.

If inspection shows the separate nextest generator would change the API output or is incompatible, explain the evidence and retain the original build plan. In either case keep the initial strict failure and platform-exception classification. The final wheel itself MUST remain release profile.

Please acknowledge this coordination note in build/progress.md so Astra knows it was read. Then continue the existing build task; no new user approval is needed for this implementation choice within the authorized build.
