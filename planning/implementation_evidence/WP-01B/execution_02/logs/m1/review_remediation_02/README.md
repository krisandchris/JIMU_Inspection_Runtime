# Independent M1 all-context preflight remediation

Every supplied MappingTypeContext is now checked before per-step lookup, including
unused contexts and contexts attached to an empty Plan: full Plan four-tuple must match
required caller plan_ref, and step identity must belong to this Plan. Existing one-exact-
Profile restriction and missing actual step-context incomplete semantics remain intact.
Empty Plan with no contexts remains locally valid; producer/Policy admission stays deferred.

Before fix8new cases failed, covering context order, empty/nonempty Plan and correct/
conflicting digest on unknown step; before_fix.json retained. Final43targeted tests pass,
no skips; mypy/Ruff/format final logs exit0. An intermediate mypy assignment conflict
from shared loop variable name was fixed by renaming the preflight variable; original
mypy.json failure preserved alongside mypy_final.json. No weakening/type ignore.
Only owned validator/unit tests changed. New after_sha256.json is separate from earlier
phase hashes; parent digest authenticity is still caller trust, not computed here.
Status FIXED_PENDING_INDEPENDENT_RECHECK. Runtime N/A; no Git/model/GPU/DB/Rule changes.
