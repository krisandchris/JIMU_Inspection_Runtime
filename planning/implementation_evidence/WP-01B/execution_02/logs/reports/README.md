# Reports and import-boundary targeted verification

TEST_PASSED: final targeted pytest 42 passed, zero failures/errors/skips. Ruff check and format check exit 0. Diagnostic source mypy exit 0. See commands.jsonl for exact commands, UTC times, cwd, original stdout/stderr and dependency versions; snapshot.json records final owned source/test hashes and observed shared lock hash.

Scope: finite nonpersistent validation reports and explicit local Python import graph. No Registry, Producer/Authority admission, Rule AST, model or service execution. Runtime Acceptance N/A; RUNTIME_NOT_RUN. Initial mypy alias error and Ruff import-order error were corrected and their original outputs retained. Earlier pytest results remain in the append-only command log; final JUnit is pytest_reports_boundaries_final.xml.

Import graph distinguishes actual submodules from exported symbols, follows explicit package reexports, rejects unresolved imports and cycles, and avoids invented implicit initializer/self edges. Existing CPU/runtime dependency assertions retain their forbidden packages and public-index checks, with only the approved pinned RFC8785 addition and F1-only import placement.
