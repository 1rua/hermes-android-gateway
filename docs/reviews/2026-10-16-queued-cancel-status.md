# Queued batch cancellation status review

The queued cancellation path fenced delivery and settled the generation, but a direct SQL update only marked the leader dispatch row failed. It did not publish terminal `conversation.message.status` events for every member of a batch. Cancellation settlement now uses the dispatch status API, which emits the terminal status for each member. The test covers both batch members, the generation ID, `AGENT_UNAVAILABLE`, idempotent HTTP replay, and the queued claim fence.

The first full-suite run exposed a test-clock issue: events were created at the fixed `NOW` timestamp, while `read_after(None)` used wall-clock time and pruned records older than the event retention window. The test now reads at `NOW`, matching the timestamps used to create and cancel the batch; its lifecycle assertions remain strict.

The CI native matrix now includes review boundary coverage for unrelated asynchronous deliveries and cancellation callbacks arriving before or after cancellation. Published progress content may be closed after cancellation, while late content remains blocked.

Validation with the pinned Hermes SDK `75b083e9399115b12513385252811f09c3503c90`:

- Targeted queued-cancel boundary: 1 passed.
- Standalone CI test set (excluding the three SDK-only native history/stream files): 389 passed, 9 skipped.
- Full native matrix, including review boundaries: 43 passed.
- `git diff --check` and plugin manifest checks: passed.

The standalone and native matrices were rerun from source `a70bcf7dc5c58a94e52832957eb4b6ac9b32f642` on 2026-10-16. Fresh console logs and JUnit XML are `/workspace/scratch/hermes-final-a70-standalone.log`, `/workspace/scratch/hermes-final-a70-standalone.xml`, `/workspace/scratch/hermes-final-a70-native.log`, and `/workspace/scratch/hermes-final-a70-native.xml`. The native run forced `OAI_REQUIRE_NATIVE_HERMES=1` and included `tests/test_native_turn_review_boundaries.py`.

The SDK tests use the real Hermes adapter/session database and asynchronous lifecycle. External model inference is not part of this test matrix.
