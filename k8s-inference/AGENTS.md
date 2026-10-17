# For agents working in k8s-inference

Read `docs/ENGINEERING.md` before changing anything, and apply it to your own changes:

1. `services/api/LOC_BUDGET` is a ceiling that only goes down; `make check` enforces it. Adding code means removing
   at least as much elsewhere. No new module without retiring one.
2. No option without two callers, no incident workaround without a named expiry, no second implementation of an
   installed primitive (see `clusters/common/apps/`), no second vocabulary, no legacy path.
3. Routes are thin; one state machine per concept; functions under 60 lines; tests test contracts.
4. Substrate only. Application workflow requirements go to the application above, not here.
5. Report lines, modules and routes before and after in every receipt.
