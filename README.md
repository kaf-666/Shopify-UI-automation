# Shopify UI Automation

Shopify / DTC UI automation for **Mondressy, Lavetir and Shirees**. Each site
supports two independent production contracts:

| Contract | Per viewport / both | Behavior | CLI |
| --- | ---: | --- | --- |
| Full (`website_smoke_v1`) | 15 / 30 | Direct, Search and Browse purchase journeys; cart consistency, quantity and checkout entry | `scripts/run_website_smoke_v1.py` |
| Readonly (`website_smoke_readonly_v1`) | 11 / 22 | Direct, Search and Browse readiness; legal variant selection and ATC availability | `scripts/run_website_smoke_readonly_v1.py` |

Readonly never clicks ATC, writes/cleans the cart or enters checkout. Its network
Mutation Guard aborts cart writes and reports `READONLY_MUTATION_VIOLATION`.
The structural `pre_clean` / `cleanup` values `readonly_not_required` do not mean
cleanup ran. Full uses `TRANSACTIONAL_SAFE`: intended cart writes are audited;
unexpected mutations fail the run, high-risk order/payment/account mutations
are blocked, and final cleanup must verify the cart is empty. Full checks
checkout entry only: no payment data, accelerated payment, order submission or
real payment.

## Runtime and installation

Python 3.10.9 is verified locally; Jenkins Linux is also verified on Python 3.13.
Desktop uses Playwright Chromium (1440 × 900); mobile uses WebKit / iPhone 14
(390 × 664). This is an emulated profile, not real-device or branded Safari testing.
Runtime dependencies are pinned in `requirements.lock.txt`; the test lock includes
that runtime lock plus pytest. `requirements.txt` lists direct dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m playwright install chromium webkit
python scripts/validate_site_config.py --all
```

On Unix, activate with `source .venv/bin/activate`. Inject the selected site's
existing credentials into the process environment or Jenkins credentials binding:

| Site | Required variable / credential ID prefix |
| --- | --- |
| Mondressy | `MONDRESSY_US_SHOPIFY_` |
| Lavetir | `LAVETIR_US_SHOPIFY_` |
| Shirees | `SHIREES_US_SHOPIFY_` |

Each prefix requires `SIGNATURE`, `SIGNATURE_INPUT`, `SIGNATURE_AGENT`. Values,
cookies and signing keys must never be committed or printed. Headers are injected
only for the exact hosts in `configs/sites/<site>.yaml`. Proxy defaults to off;
optional settings are `SHOPIFY_PROXY_SERVER`, `SHOPIFY_PROXY_USERNAME`,
`SHOPIFY_PROXY_PASSWORD`, and `SHOPIFY_PROXY_ENABLED`. Keep values external.
Profiles declare suite capabilities, navigation and supported product options;
validation rejects unsupported configurations before creating a browser.

## Run storefront smoke

```powershell
python scripts/run_website_smoke_v1.py --site mondressy --viewport both
python scripts/run_website_smoke_readonly_v1.py --site lavetir --viewport both
python scripts/run_website_smoke_readonly_v1.py --site shirees --viewport mobile
```

Both CLIs accept `--site mondressy|lavetir|shirees` and
`--viewport both|desktop|mobile`; viewport defaults to `both`, omitted site uses
`settings.default_site`. Explicit `--run-id` must be unique. CI also supplies
`--run-context`, reserved before smoke. Existing run directories are never reused.
Full has 3 Direct, 4 Search and 8 Browse cases per viewport; Readonly has 2 Direct,
4 Search and 5 Browse cases. A partial/fatal run retains executed state without
inventing missing cases.

| Exit | Meaning |
| --- | --- |
| 0 | All business cases PASS |
| 1 | FAIL/BLOCKED, runtime failure, cleanup/audit failure or Readonly mutation violation |
| 2 | CLI, viewport, configuration or artifact-directory error |

Full optionally accepts `--traffic-inventory` (read-only network observation).
Lavetir/Shirees Full CI enable it; Mondressy Full does not. Sanitized
`traffic/requests.jsonl`, `summary.json`, `summary.txt` contain no full headers,
bodies, cookies, query values or checkout tokens. It does not change requests or
observe separate APIRequestContext traffic. Offline validation:
`python scripts/validate_traffic_inventory.py`.

Product size models, shared readiness phases, the single 15-second budget and
bounded diagnostics are documented in [purchase readiness](docs/purchase-readiness.md).
Normal-size selection excludes custom measurements. A passing rerun does not
replace an earlier failed observation.

## Current-run artifacts and summary

```text
artifacts/website-smoke-v1/<site>/<run_id>/
artifacts/website-smoke-readonly-v1/<site>/<run_id>/
  results.json
  summary.html / summary.json / summary.css
  <viewport>/<CASE_ID>-failure.png          # when captured
  <viewport>/cart-cleanup.json              # Full
```

Open `summary.html` next to the local result. It displays identity, validity,
case counts, failures, BLOCKED dependency chains, diagnostics, evidence availability
and lifecycle/audit state. Local reports say `LOCAL_UNVALIDATED`; omitted local
commit/build metadata is `UNKNOWN`, not inferred from another run.

The six formal CI jobs reserve `artifacts/ci/<run_id>/run_context.json`. Results
carry the exact suite/site/run/SHA/job/build/viewport identity. Schema, stability
and publication consume that explicit path, never latest-file selection. CI
rebuilds the summary from validated frozen results and publishes only:

```text
artifacts/ci/<run_id>/publish/
  <run_id>/results.json
  <run_id>/summary.html / summary.json / summary.css
  <run_id>/<current evidence and optional stability_record.json>
  run_context.json
  publication.json                          # explicit file manifest
```

Choose **the specific Jenkins build → archived artifacts → summary.html**.
The normal HTML artifact link renders the report; Jenkins `view` shows its source.
The page uses no JavaScript,
remote assets or external services; evidence links remain relative to this run.
All candidate files, including the summary, JSONL/NDJSON and unknown suffixes,
are scanned after final writes. Only a passing snapshot is archived, then
acknowledged. Valid business FAIL keeps its current evidence and failing status.
Missing results, identity errors, failed/unused gates, unreadable/linked files
or a failed final scan publish only `publication-failure.json` and a fixed safe
`summary.html` infrastructure report, never historical output.

Snapshots describe **pre-archive** state. Check the same Jenkins build console
for final scan, archive success and `CI publication: ACKNOWLEDGED`; the archived
HTML cannot claim a later acknowledgement. Bootstrap failure summaries explicitly
say identity/gates were not validated. [Summary fields and troubleshooting](docs/run-summary.md)
explain failures, missing evidence and current-run provenance.

Local explicit schema validation (use the actual run path):

```powershell
python scripts/validate_result_schema.py --suite website_smoke_v1 --results artifacts/website-smoke-v1/mondressy/<run_id>/results.json
```

Historical/manual schema tools still support latest-result reading when no path
is provided. That compatibility mode is not used by CI.

## Jenkins jobs and schedules

The six enabled formal jobs under `Monitoring / Shopify / UI automation` track
`*/main`. This table records job UI verification on **2026-10-19**. Expressions
use the Jenkins scheduler's configured timezone, not the reader's local timezone;
`H` is Jenkins' stable hashed offset. Recheck UI after operational changes.

| Job | SCM script | Cases (`both`) | Cron | Stability |
| --- | --- | ---: | --- | --- |
| Mondressy - Website Smoke - Full | `Jenkinsfile.full` | 30 | `H H * * *` | Full/both |
| Mondressy - Website Smoke - Readonly | `Jenkinsfile.readonly` | 22 | `H */4 * * *` | None |
| Lavetir - Website Smoke - Full | `Jenkinsfile.full.lavetir` | 30 | `12 10 * * *` | Full/both |
| Lavetir - Website Smoke - Readonly | `Jenkinsfile.readonly.lavetir` | 22 | `H H/4 * * *` | None |
| Shirees - Website Smoke - Full | `Jenkinsfile.full.shirees` | 30 | `H 18 * * *` | Full/both |
| Shirees - Website Smoke - Readonly | `Jenkinsfile.readonly.shirees` | 22 | `H 1-23/4 * * *` | None |

All expose `SMOKE_VIEWPORT=both|desktop|mobile`; each binds only its own three
credential IDs above. Lavetir Readonly alone defines source cron and optional
`VARIANT_DIAGNOSTIC=false`; the other schedules are job UI configuration. A
variant diagnostic failure is UNSTABLE and does not replace the formal result.
`Jenkinsfile` and `Jenkinsfile.smoke-integration-manual` remain separate legacy
compatibility entries. Old pilot descriptions do not define current schedules.

The six thin entries load `ci/smoke_pipeline.groovy` and delegate fixed steps to
`scripts/run_smoke_ci.py`: checkout → environment/dependencies → prepare → browsers
→ static → runtime contract → signed access → smoke → secret → schema → publication.
Smoke failure is preserved while gates and post actions continue. Unix/Windows
use the same Python argv protocol. See [CI ownership and rollback](docs/smoke-ci.md).

## Offline framework regression

Install tests and run the same gate as Jenkins without storefront credentials:

```powershell
python -m pip install -r requirements-test.lock.txt
python -m playwright install chromium webkit
python scripts/run_framework_ci.py --report-dir artifacts/framework-ci/local-unique-id
```

Use a new report directory each time. Checks are compile, full pytest, Runtime
Contract and stability validation, using mocks/synthetic DOM/local HTTP servers.
The Jenkins **Framework - Offline Regression** multibranch job uses
`Jenkinsfile.framework-ci`, publishes JUnit and scanned logs, handles branch/PR
webhooks and cancels superseded builds. GitHub main requires
`continuous-integration/jenkins/pr-merge`; do not bypass it for direct main pushes.
See [framework CI setup and checks](docs/framework-ci.md).

## Historical stability and specialized tools

Only complete Full/both runs enter formal stability history, including valid
business FAIL. Readonly and single-viewport runs do not. Per-build archives are
authoritative; `artifacts/website-smoke-v1/<site>/stability-history.jsonl` is an
ignored workspace cache updated only after archive acknowledgement (idempotent).
History remains separate from current summaries and isolated by site/suite/commit
baseline. Missing cache is not a current-run failure.

```powershell
python scripts/summarize_stability.py --site mondressy --last 10
python scripts/validate_stability.py
python scripts/validate_size_option_resolver.py
```

Legacy/specialized runners remain under `scripts/run_*_cases.py` (smoke, search,
navigation, cart quantity, checkout, Direct PDP and expanded PLP). Their scope
and reporting differ from the two formal contracts. Frozen pilot results remain
in [Lavetir historical observation](docs/lavetir-multi-site-pilot-result.md);
they do not assert current production health.
