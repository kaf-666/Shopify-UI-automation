# Shared production smoke CI

The six formal Jenkinsfiles remain explicit entry points. They bind only their
site's three Jenkins credentials, declare their suite, viewport choices, existing
options and schedule, and expose the existing stage names. Jenkins loads the
repository-local `ci/smoke_pipeline.groovy` after `checkout scm`; no external
Shared Library or extra Jenkins plugin is needed.

| Site | Full entry | Readonly entry | Credential prefix | Full traffic inventory |
| --- | --- | --- | --- | --- |
| Mondressy | `Jenkinsfile.full` | `Jenkinsfile.readonly` | `MONDRESSY_US_SHOPIFY_` | off |
| Lavetir | `Jenkinsfile.full.lavetir` | `Jenkinsfile.readonly.lavetir` | `LAVETIR_US_SHOPIFY_` | on |
| Shirees | `Jenkinsfile.full.shirees` | `Jenkinsfile.readonly.shirees` | `SHIREES_US_SHOPIFY_` | on |

The Lavetir Readonly entry retains `H H/4 * * *` and the optional
`VARIANT_DIAGNOSTIC=false` parameter. The diagnostic still checks the mobile
search variant independently and marks its own failure UNSTABLE before formal
smoke. Other entries do not introduce source-defined triggers. Existing job
configuration and scheduler settings are not changed by this refactor.

`scripts/run_smoke_ci.py --step <name>` owns the common protocol. It reads
`CI_SITE`, `CI_SUITE`, `CI_VIEWPORT` from the fixed entry, validates them against
the three sites/two suites/three viewport choices and checks the current context
before executing any child process. Prepare also checks the configured credential
**variable names** against the site's profile without reading credential values.
All child commands use Python argv lists with `shell=False` on both platforms;
user parameters are never interpolated into a shell command.

| Step | Responsibility |
| --- | --- |
| `prepare` | Validate profile/mapping; reserve the build's unique run context. |
| `static` | Compile repository sources and validate the selected site/suite. |
| `runtime` | Existing offline Runtime Contract check. |
| `access` | Existing signed-request self-check for both viewports. |
| `variant-diagnostic` | Optional Lavetir Readonly diagnostic only. |
| `smoke` | Select the exact suite CLI, site, viewport, run ID and context; preserve its exit code. |
| `secret` / `schema` | Validate only this context's exact result/evidence paths. |
| `publish` | Reuse finalization, existing stability summaries and the fully scanned publication manifest. |
| `acknowledge` | Advance publication/history only after Jenkins archives successfully. |

The Jenkins adapter owns Python/bootstrap and browser installation, stage/build
status, gate flags, trigger identity and `archiveArtifacts`. Unix uses the
discovered Python path quoted as one executable; Windows retains the `python`
bootstrap and `.venv\Scripts\python.exe`. Both then run the same Python
dispatcher. Dependencies remain pinned by `requirements.lock.txt`.

Smoke failure is caught at its Jenkins stage so the scan/schema gates and
`post(always)` still execute. CLI 0/1/2 and Full 30 / Readonly 22 remain unchanged.
Only a validated, fully scanned current-run snapshot can be archived, followed
by acknowledgement. Valid business FAIL results retain current evidence and
eligible Full/both failure observations. Missing results, failed gates, failed
scan or bootstrap/load errors publish only the constant current-build failure
JSON and safe HTML infrastructure summary; historical results and history are
never substituted. The validated current-run summary and its stylesheet/model
are generated inside the frozen snapshot before the final scan. See
[summary fields and troubleshooting](run-summary.md) for pre-archive semantics,
observed phases, relative evidence links and publication failure handling.

`Jenkinsfile` and `Jenkinsfile.smoke-integration-manual` keep their prior
implementations. Framework CI remains separate and does not bind storefront
credentials. Run `python scripts/run_framework_ci.py --report-dir <new-directory>`
for offline verification, including the 3 sites × 2 suites × 3 viewports matrix,
literal argv handling on the current OS, exit codes, identity mismatch and real
publication/acknowledgement behavior.

Rollout uses the temporary validation task against the candidate commit: one
Readonly pilot, the remaining Readonly profiles, then Full. Compare the result
identity/case count, CLI status, gate statuses, publication manifest and cleanup
audit before merging. The formal job SCM script paths stay unchanged. A complete
rollback reverts the phase's commit (entries and shared files together) to the
previous main implementation, retaining job credentials/schedules and all
build-specific archived evidence. Current-run presentation reads no history and
does not change the business, cleanup or stability contracts.
