# Current-run summaries and troubleshooting

`summary.html` is a static index over one Full/Readonly run, accompanied by
`summary.json` (version 1) and local `summary.css`. No scripts, external requests,
history queries or latest-file discovery are used. Jenkins serves artifact HTML
with restrictive CSP; the report is semantic HTML, with a same-directory stylesheet.
Open the normal **summary.html** artifact link to render it. Jenkins `view`
(`/*view*/`) intentionally shows escaped source; do not use it for presentation.

## Paths and validity

| Situation | Location | Meaning |
| --- | --- | --- |
| Local CLI wrote results | `<suite-root>/<site>/<run_id>/summary.html` | `LOCAL_UNVALIDATED`; local execution only, no CI gates inferred |
| CI accepted current results | `artifacts/ci/<run_id>/publish/<run_id>/summary.html` | `VALIDATED_CURRENT_RUN`; exact context identity and schema checked |
| CI publication rejected | `artifacts/ci/<run_id>/summary.html` | Infrastructure/publication failure; no result or evidence linked |
| Checkout/helper bootstrap failed | Same current control directory | Fixed error summary; identity/gates `NOT_VALIDATED`, surrounding Jenkins build supplies provenance |
| Local CLI failed before creating a run | No summary | Use CLI exit/error; no historical result is a substitute |

`summary.json.identity` contains suite, site, viewport, run ID, checked-out SHA,
Jenkins job and build. Local-only missing values are explicit UNKNOWN/LOCAL.
CI replaces any copied/local summary with a model built from the validated
snapshot. Links to evidence, cleanup audit, results and the current stability
record are relative to that exact run directory. Missing/rejected evidence has
an explicit status and no clickable target. An unavailable diagnostic or
screenshot capture error is shown separately from the business case status.

## Reading the report

1. Check Identity, `result_validity`, phase and gates before interpreting PASS.
2. Start with **Failures and blocked cases**. Each BLOCKED chain is scoped to
   its viewport and points from the blocked case back to upstream dependencies.
3. Expand that case's diagnostic; readiness failures retain shared stage budgets,
   elapsed times and bounded polling data. Follow only AVAILABLE evidence links.
4. Check fatal/partial execution, pre-clean, cleanup and mutation audit. Zero
   failed cases does not make a partial/fatal/lifecycle failure successful.
5. Compare with the same build's console for final publication and ACK.

`CASE_OR_LIFECYCLE_FAILURE` means valid business cases or lifecycle/audit failed.
`EXECUTION_FAILURE` means results recorded a fatal/partial runtime problem; the
executed cases remain visible and missing ones are not fabricated. Publication
rejection is a separate infrastructure report with `Business result:
NOT_AVAILABLE`; a suppressed result must not be presented as business PASS.

The summary lists observed `prepare/static/runtime/access/smoke/secret/schema`
statuses. NOT_RECORDED means no step observation was supplied (for example a
manual compatibility invocation), not PASS. RUNNING means the process was
interrupted before completion. Optional Lavetir variant diagnostic remains a
separate Jenkins stage. The report records `last_observed_step`, not a guessed
stage reconstructed from case counts.

CI summary phase is **PRE_ARCHIVE**. Schema and initial secret gate statuses are
real process outcomes. `FINAL_SCAN_AND_ARCHIVE_REQUIRED` does not claim that the
final scan/archive/ACK have happened yet. The full generated snapshot is scanned
last, then Jenkins archives it, then ACK updates history. The archived snapshot
is never rewritten to claim a later ACK. A later publication/ACK failure makes
Jenkins fail and also creates the safe infrastructure summary. Check console:

```text
CI publication: READY (... scanned files)
Archiving artifacts
CI publication: ACKNOWLEDGED
```

A prior READY or partial archive alone does not prove ACK. A business FAIL may
correctly have READY, ACK and schema/scan PASS while the build remains FAILURE.

## Failure guide

| Symptom / classification | Inspect / action |
| --- | --- |
| `CURRENT_RESULTS_MISSING` | Checkout, dependency/browser install, prepare and last observed stage; confirm this invocation reached smoke. Never select an older results file. |
| `CURRENT_SECRET_SCAN_NOT_PASSED` | Current secret gate/log; resolve the output leak or unreadable file. Keep values out of tickets and logs. |
| `CURRENT_SCHEMA_NOT_PASSED` | Exact context/result identity, case arithmetic, evidence existence, audit and partial/fatal shape. Do not patch old output into the current directory. |
| `CURRENT_RUN_PUBLICATION_FAILED` | Finalizer/final-scan/archive/ACK logs. Covers identity mismatch, summary write failure and other rejection; fixed error summary deliberately omits raw exception text. |
| `RUN_*IDENTITY_MISMATCH` | Compare suite/site/run/SHA/job/build/viewport; check actual checkout SHA and context. Re-run with a new run ID after correcting the invocation. |
| `PURCHASE_AREA_FAILURE` / `SEARCH_PURCHASE_AREA_NOT_AVAILABLE` | Expand readiness diagnostic; compare initial/title_price/confirmation budget and size model against screenshot. Missing/late/rerendered size group is not evidence of success. |
| `READONLY_MUTATION_VIOLATION` | Mutation guard method/path and owning case; Readonly must not write or clean the cart. |
| Cleanup / mutation audit FAIL | Full audit JSON, final empty-cart verification and unexpected/high-risk counts; do not infer success from case counts alone. |
| `SCREENSHOT_CAPTURE_FAILED` / missing evidence | Treat the capture failure separately; use the diagnostic and current console. Missing required evidence can reject schema/publication. |

Phase-four controlled runs observed mobile readiness failures on Mondressy and
Shirees. Some single-viewport reruns passed, including old-main/candidate
Mondressy mobile comparison; this does not identify the root cause or invalidate
prior both-viewport FAIL samples. Keep the existing 15-second budget and original
observations while investigating storefront/agent timing; do not replace failed
samples with retries.

## History, maintenance and rollback

Current reports never read `stability-history.jsonl`. Run
`python scripts/summarize_stability.py --site <site> --last 10` separately for
history. Only complete Full/both observations are eligible; validated business
FAIL remains eligible and is not relabelled PASS. The workspace cache can be
absent after cleanup; per-build results/records remain authoritative.

When changing output fields, update summary-model/link tests, run the full
`run_framework_ci.py` gate, then verify a controlled Jenkins PASS, a business
FAIL and a pre-smoke failure. Confirm exact SHA/run/build identity, correct
relative links, final scan and archive-before-ACK ordering. Existing six-job
credentials and schedules are operational settings; verify UI before documenting
changes. A rollback reverts the phase's summary generator, CLI/finalizer hooks
and Jenkins error-report changes together through the required PR check. Existing
results schemas, business exit codes, cleanup contracts and legacy integration
entry points are retained.
