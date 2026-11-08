# Current publication-candidate verification — 2026-11-08

Executed against this repaired source tree, with network namespaces and no providers/game/model downloads:
- Focused backend + adapter suite: **308 passed, 35 subtests passed**, 2 third-party dependency warnings. Includes 20 environment-only remote adapter tests using actual SDK classes with MockTransport.
- Native guard, plugin gates and topic patch tests: zero failures, ASan/UBSan and TSan pass.
- Native HTTP: **204 core checks + 81 dispatcher checks**, zero failures; table-only ID checks, ASan/UBSan/TSan, declaration and forbidden-construct gates pass.
- Proprietary EXE/address-library/official script inputs unset; those optional checks explicitly skipped. No Windows compile or game run in this preparation.
- Existing component license files byte-equal to the initial snapshot. All 42 backend/20 native inventoried delta hashes reconciled to the reviewed source; already hosted repairs retained via ancestry, only residual source/test files imported.
- Gitleaks 8.30.0 official standalone release, verified against its official SHA-256 checksum, used outside the repository. Full-ref/history and exact-tree scans plus supplemental raw-object/manual privacy checks are performed by the publisher; private review artifacts and backups are not shipped in Git. Scanner results do not prove the absence of every possible secret.

Current scope, limitations and parameterized rerun commands: [EXPERIMENTAL.md](EXPERIMENTAL.md). Source manifest: [REPAIR-SOURCE-MANIFEST.json](REPAIR-SOURCE-MANIFEST.json). Full upstream suite not rerun; no fresh-from-zero dependency install or installed-DLL byte-pairing claim. Independent publisher review remains required before any remote update/visibility change.

---
## Historical snapshot verification (not current repair acceptance)

# Publication verification — 2026-11-04

Verified from the isolated sanitized snapshot, not the live development worktrees:

- Native guard suite: exit 0, topic-patch 0 failures, TSan OK, guard ASan/UBSan OK.
- Native HTTP suite: exit 0, **195 portable checks / 0 failures**, ID table-only 0 failures, ASan/UBSan/TSan OK, declaration and forbidden-construct gates pass.
- Proprietary EXE/address-library/official Papyrus inputs intentionally unset: their checks were **SKIPPED**. Historical 298-check/9-ID evidence in REVIEW-STATUS.md used separately owned local inputs; do not confuse the two runs.
- Backend focused adapter/per-character tests: network namespace isolated, bytecode/cache disabled, **72 passed**, 2 third-party dependency warnings, 1.68 s.
- Uses the existing Python 3.11 test environment; TESTED-PYTHON-ENV.txt records installed versions, not credentials or filesystem paths. No fresh-from-zero dependency install was attempted in this publication.
- Exact production source bytes match the source manifest except a machine-path comment sanitization in offsets240.h and the targeted backend ignore-rule maintenance change; native HTTP README overclaim corrected, backend README renamed. No bug-fixing or build-toolchain changes.
- No Windows plugin compile, engine/UI lifetime proof, real Gateway acceptance, mod staging or game run. **NOT Windows-compiled / NOT game-tested.**

Pre-push checks inspect the complete staged file list, sizes, text/binary types, source provenance and suspicious secret/path candidates. Private-network URL in test_openclaw_adapter.py is an intentional synthetic rejection fixture, not a live connection target. No raw logs/history are included.

Independent pre-push privacy/license/portability review: **PASS** for the 292-file private source-only staged snapshot. All 284 manifest imports accounted for; no detected credentials, personal filesystem paths, vendor/game binaries or history. Component licenses and unresolved acceptance limitations preserved. The nonblocking inherited ignore rule for function-action JSON was corrected for maintainability after review. Existing imported source whitespace is preserved (git diff --check reports upstream trailing whitespace); publication-only documents have no whitespace errors.
