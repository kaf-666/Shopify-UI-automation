# Shopify UI Automation

Standalone Shopify / DTC UI Automation Framework for Mondressy and Lavetir.
The repository provides two independent production smoke contracts:

- **Website Smoke V1 (Full)** validates positive shopping journeys through cart
  and checkout entry.
- **Website Smoke Readonly V1** is a high-frequency storefront health check
  that never writes to the cart and never enters checkout.

## Runtime

- Python 3.10.9
- Desktop: Playwright Chromium, 1440 × 900
- Mobile: Playwright WebKit, iPhone 14

The mobile profile uses Playwright WebKit; it does not represent a branded Safari device or real-device Safari execution.

## Installation (Windows)

```powershell
python -m venv .venv
\.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium webkit
```

`requirements.txt` pins the direct dependencies; `requirements.lock.txt` records the
verified `.venv` dependency set used by this repository.

## Website Smoke V1

Website Smoke V1 is the **Full** shopping contract. It covers three positive
journeys:

1. **Direct PDP Purchase**

   `Direct PDP → Variant → Add To Cart → Cart Consistency`

2. **Search Purchase**

   `Home → Search → Search Results → PDP → Variant → Add To Cart → Cart`

3. **Browse Purchase**

   `Home → Navigation → Collection → PDP → Variant → Add To Cart → Cart → Quantity → Checkout`

The Full case contract contains 3 Direct cases, 4 Search cases, and 8 Browse
cases: **15 cases per viewport** and **30 cases for `both`**.

Full validates legal variant selection, Add To Cart, the cart drawer, cart
identity/variant consistency, quantity changes, and checkout entry. It does
not enter payment information, use accelerated payment, submit an order, or
complete a real payment.

Full runs use the `TRANSACTIONAL_SAFE` mutation policy. Cart Add/Change/Clear
requests are recorded as `EXPECTED_MUTATION`; known Shopify/analytics transport
posts are observed but excluded from the business mutation count. Any other
first-party state-changing request is `UNEXPECTED_MUTATION`, while order,
payment, account/customer, and checkout-session mutations are
`HIGH_RISK_MUTATION` and are aborted before reaching the site. A Full result
contains per-viewport and aggregate `mutation_summary` fields; PASS requires
`unexpected_mutation=0` and `high_risk_mutation=0`.

### Main commands

```powershell
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport both
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport desktop
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport mobile
```

The default viewport is `both`.

## Versioned site profiles and capability gates

Every `configs/sites/<site>.yaml` profile declares `schema_version: 1` and a
`capabilities` contract. The contract records the suites, navigation strategy,
collection-card features, and product-option features the framework actually
automates for that site. It is not a claim about every storefront feature the
site may expose.

Validate profiles without starting Playwright or reading Signed Request values:

```powershell
python scripts/validate_site_config.py --site mondressy
python scripts/validate_site_config.py --site lavetir
python scripts/validate_site_config.py --all
```

Formal runners apply the matching suite gate before creating a browser. Lavetir
now declares both `website_smoke_readonly_v1` and the transactional-safe
`website_smoke_v1` capability; incomplete profiles still exit with
`SITE_CAPABILITY_UNSUPPORTED` rather than starting a partial run.

## Website Smoke Readonly V1

Readonly is a high-frequency, non-cart-writing storefront health check. It is
an independent Readonly Contract; it is **not** Full running halfway and then
stopping.

The three Readonly journeys are:

- **Direct:** `Direct PDP → Variant readiness → ATC availability`
- **Search:** `Home → Search → Results → PDP → Variant readiness → ATC availability`
- **Browse:** `Home → Navigation → Collection → PDP → Variant readiness → ATC availability`

Selecting a legal Color and Size is allowed. Add To Cart is only inspected for
existence, visibility, and purchase-area readiness after a valid variant is
selected. The Add To Cart control is never clicked.

### Readonly case contract

- Direct: 2 cases
- Search: 4 cases
- Browse: 5 cases
- Total: **11 cases per viewport**, **22 cases for `both`**

The stable case IDs are:

```text
RSMOKE-DIRECT-01
RSMOKE-DIRECT-02
RSMOKE-SEARCH-01
RSMOKE-SEARCH-02
RSMOKE-SEARCH-03
RSMOKE-SEARCH-04
RSMOKE-HOME-01
RSMOKE-NAV-01
RSMOKE-PLP-01
RSMOKE-PDP-01
RSMOKE-PDP-02
```

### Readonly mutation safety

Readonly uses a network-layer Mutation Guard. It protects cart mutation
endpoints for mutation-capable methods (`POST`, `PUT`, `PATCH`, and `DELETE`):

```text
/cart/add
/cart/add.js
/cart/change
/cart/change.js
/cart/update
/cart/update.js
/cart/clear
/cart/clear.js
```

When a matching request is found, the guard records the safe method/path
diagnostic and calls Playwright `route.abort()`. The request does not reach
Shopify. Non-matching requests use `route.fallback()`, preserving the
Signed Request routing chain.

If a mutation occurs during a Readonly case, that case fails with
`READONLY_MUTATION_VIOLATION` and the run exits with code 1. A normal Readonly
run reports **Readonly Mutation Violations = 0**.

Readonly does not read or clean the cart and does not navigate to checkout. Its
result contract keeps structural lifecycle fields for compatibility:

```text
pre_clean: PASS / readonly_not_required
cleanup:   PASS / readonly_not_required
```

These fields do not mean that cart cleanup was called.

### Readonly commands

```powershell
.\.venv\Scripts\python.exe scripts\run_website_smoke_readonly_v1.py --viewport both
.\.venv\Scripts\python.exe scripts\run_website_smoke_readonly_v1.py --viewport desktop
.\.venv\Scripts\python.exe scripts\run_website_smoke_readonly_v1.py --viewport mobile
```

The default viewport is `both`.

## Lavetir q4h Readonly Pilot

`Jenkinsfile.readonly.lavetir` is the fixed-site Readonly pipeline for the
Lavetir observation pilot. It runs the same Readonly contract as the local
runner with:

```text
site: lavetir
viewport default: both
diagnostic default: false
schedule: H H/4 * * *
```

The `H` tokens let Jenkins distribute the approximate four-hour trigger time.
The pipeline has no `pollSCM`, upstream, webhook, or retry trigger. Its only
automatic source is the Declarative Pipeline cron above; manual Build with
Parameters runs remain available for activation or investigation.

Each scheduled build must be a real single run and must pass all of these
gates:

- Desktop: `11/11 PASS`
- Mobile: `11/11 PASS`
- Combined: `22/22 PASS`
- Readonly Mutation Violations: `0`
- Lavetir Site Access, Secret Leakage, and Result Schema: `PASS`
- Jenkins result: `SUCCESS`

The completed observation counted timer-triggered builds with
`SMOKE_VIEWPORT=both` and `VARIANT_DIAGNOSTIC=false`; manual and diagnostic
builds were excluded. Jenkins Build History remains its only observation
source and no new Stability framework or Stability record was created. The
final result is frozen at Jenkins `#57–#74` on
`2e57992bb8beaa2ab7e1c59ace9b7ea4814b8e1d`: `18` consecutive scheduled
successes against the `12`-run gate, `22/22 PASS` Readonly, and `0` mutations.
The detailed immutable phase baseline is recorded in
[docs/lavetir-multi-site-pilot-result.md](docs/lavetir-multi-site-pilot-result.md).
The observation work is now closed; Step 6 hardens the shared architecture and
does not add a third site.

The pipeline is intentionally fixed to Lavetir and binds only the three
`LAVETIR_US_SHOPIFY_SIGNATURE*` Jenkins credentials. Existing Mondressy
pipelines and their schedules are independent.

## Exit code contract

The exit-code contract is consumed directly by Jenkins.

- `0` = all cases PASS
- `1` = one or more cases FAIL/BLOCKED, a runtime failure, or a Readonly
  mutation violation
- `2` = CLI, viewport, configuration, or artifact-directory error

## Artifacts

Full and Readonly use separate artifact roots:

```text
artifacts/website-smoke-v1/<site>/<run_id>/
artifacts/website-smoke-readonly-v1/<site>/<run_id>/
```

`<site>` is the validated configured site identifier (for example, `mondressy`
or `lavetir`), not a value encoded into `run_id`. Each run contains `results.json`. Failed cases may also contain a
viewport-scoped failure screenshot or diagnostics. Full `both` runs may
additionally contain `stability_record.json`; Readonly does not generate a
Stability record. Local run IDs include a random suffix; an existing run
directory is never reused.

The six formal Jenkins pipelines reserve `artifacts/ci/<run_id>/run_context.json`
before site access. Results carry the same `execution_context` identity: suite,
site, run ID, checked-out commit, Jenkins job/build, and requested viewport.
Schema validation and CI stability collection require that exact result path;
historical/manual result readers remain compatible with older files.

CI publishes only `artifacts/ci/<run_id>/publish/**`. This snapshot contains
`<run_id>/results.json`, the current evidence, `run_context.json`, and
`publication.json` with its explicit file list. All candidate files are scanned
after the final writes, including JSONL/NDJSON and unknown suffixes. A missing
result, failed/unused gate, unreadable file, link, or failed final scan publishes
only a fixed `publication-failure.json`; historical workspace output is excluded.
Ordinary business failures retain their current evidence when validation and
scanning pass. Full `both` stability history is updated only after Jenkins
successfully archives the snapshot; repeating acknowledgement is idempotent.

CI protocol entry points are `scripts/prepare_run_context.py`, the appropriate
smoke CLI with `--run-id` / `--run-context`, explicit schema validation with
`--results` / `--run-context`, and `scripts/finalize_ci_run.py`. The finalizer's
`--acknowledge` step runs after successful archival. A failed build without a
current result is recorded as infrastructure failure, never as a previous PASS.

## Specialized suites

| Suite | Runner |
| --- | --- |
| Legacy Smoke | `scripts/run_smoke_cases.py` |
| Search | `scripts/run_search_cases.py` |
| Navigation | `scripts/run_navigation_cases.py` |
| Cart Quantity | `scripts/run_cart_quantity_cases.py` |
| Checkout Entry | `scripts/run_checkout_cases.py` |
| Direct PDP | `scripts/run_direct_pdp_cases.py` |
| Expanded PLP | `scripts/run_expanded_plp_cases.py` |
| Website Smoke V1 | `scripts/run_website_smoke_v1.py` |
| Website Smoke Readonly V1 | `scripts/run_website_smoke_readonly_v1.py` |

All viewport-aware runners accept `--viewport desktop`, `--viewport mobile`, or `--viewport both` where supported.

## Result schema

The formal result suites are independent of business PASS/FAIL:

- `website_smoke_v1`: 15 cases per viewport
- `website_smoke_readonly_v1`: 11 cases per viewport

Validate the latest local result for either suite with:

```powershell
python scripts/validate_result_schema.py --suite website_smoke_v1
python scripts/validate_result_schema.py --suite website_smoke_readonly_v1
```

Schema validation checks the structure, case ordering, summaries, evidence
paths, and safe-output markers. It is separate from the business result: for
example, a results file containing `READONLY_MUTATION_VIOLATION` can be
structurally valid while the business run is still FAIL.

## Traffic inventory

Website Smoke V1 can attach an optional, read-only BrowserContext network
observer. It is disabled by default and does not intercept, block, fulfil, or
modify requests. Signed Request handling and the normal Website Smoke result
contract remain unchanged.

After offline validation, the single production inventory command is:

```powershell
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport both --traffic-inventory
```

When explicitly enabled, sanitized artifacts are written under the run's
`traffic/` directory:

```text
requests.jsonl
summary.json
summary.txt
```

Stored URLs contain only scheme, host, a sanitized path, a query-presence flag,
and a SHA-256 key derived without query values or fragments. Request/response
bodies, complete headers, cookies, authorization data, Signed Request fields,
and checkout tokens are never persisted. Classifications and
`CACHE_REPEAT_CANDIDATE` are analysis labels only; the inventory observer does
not perform traffic reduction. BrowserContext events do not expose the
separate APIRequestContext calls used by cart pre-clean and backend assertions,
so those few requests are outside this inventory even though their surrounding
runner scope is marked as `infrastructure`.

Offline validation:

```powershell
.\.venv\Scripts\python.exe scripts\validate_traffic_inventory.py
```

## PDP size option resolver

`ProductPage` exposes one model-independent size contract through
`available_size_count()`, `first_available_size()`, `select_size()`, and
`get_selected_size()`. The implementation in `pages/size_option_resolver.py`
first detects the Size Group associated with the main Add To Cart form, then
normalizes its options. It does not scan global radios or retain DOM handles.

The Mondressy configuration currently describes:

- `SIZE_MODEL_01`: SPB property radios (`properties[Size]`);
- `SIZE_MODEL_02`: Shopify native variant radios (`name=Size`); and
- `SIZE_MODEL_03`: metadata for the conditional `Free Custom Size`
  measurement widget.

`Free Custom Size` remains part of the compatible available-option count, but
automatic normal-size selection always excludes it. The framework never fills
custom measurements. Native unavailable options support the real `disabled`
property, `aria-disabled`, and the observed `disabled` class token. Every
resolver operation re-detects the live group so SPB/theme DOM replacement and
model switches do not reuse stale nodes.

Run the offline synthetic regression suite with:

```powershell
python scripts/validate_size_option_resolver.py
```

## Secrets and access

Runtime secrets are not stored in this repository. Mondressy Signed Request credentials must be injected or provided externally at runtime. Credential values, cookies, private keys, proxy credentials, and signed-request values must never be committed.

The default Signed Request source is the process environment:

```text
MONDRESSY_US_SHOPIFY_SIGNATURE
MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT
MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT
```

The default proxy is disabled. Jenkins or local CI can opt in with:

```text
SHOPIFY_PROXY_SERVER
SHOPIFY_PROXY_USERNAME
SHOPIFY_PROXY_PASSWORD
SHOPIFY_PROXY_ENABLED=true   # optional; a server value also enables the proxy
```

Proxy credentials are passed to Playwright in memory and are never written to
console output or result artifacts. Signed Request headers are injected only for
the exact hosts in `configs/sites/mondressy.yaml`.

## Checkout safety boundary

Full checkout automation validates checkout entry and core checkout state only.
It does not:

- enter payment information;
- complete PayPal or Shop Pay;
- intentionally trigger 3DS;
- submit an order; or
- complete a real payment.

Readonly never enters checkout.

## Frozen baseline

**Website Smoke V1 Baseline**

- Date: `2026-08-20`
- Freeze run: `20260820_173410`
- Desktop: `15/15 PASS`
- Mobile: `15/15 PASS`
- Combined: `30/30 PASS`
- Legacy Smoke: `16/16 PASS`

The frozen scope covers Direct PDP Purchase, Search Purchase, and Browse Purchase across desktop and mobile profiles.

## Current verified Jenkins baseline

- Date: `2026-09-08`
- Verified commit: `7bfd5f356cfd5295ba66fa096befa118e70ec0a0`

Readonly Jenkins manual gate:

- Desktop: `11/11 PASS`
- Mobile: `11/11 PASS`
- Combined: `22/22 PASS`
- Mutation Violations: `0`

Full Jenkins manual gate:

- Desktop: `15/15 PASS`
- Mobile: `15/15 PASS`
- Combined: `30/30 PASS`

Step 5A Manual Jenkins Pilot completed successfully on the same revision:

- `#34`: mobile, `11/11 PASS`, Mutation `0`
- `#35`: both, `22/22 PASS`, Mutation `0`
- `#36`: both, `22/22 PASS`, Mutation `0`
- `#37`: both, `22/22 PASS`, Mutation `0`

The three consecutive `both` runs used `VARIANT_DIAGNOSTIC=false` and all
passed Search-04, Site Access, Secret Leakage, and Result Schema gates. The
Step 5B q4h counter starts separately at `0/12` after the schedule activation
build and does not include these manual builds.

## Jenkins

### Offline framework regression

`Jenkinsfile.framework-ci` is independent of the six storefront smoke pipelines.
It installs `requirements-test.lock.txt` (including the existing runtime lock),
Chromium and WebKit, then runs static compilation, the entire pytest suite,
Runtime Contract and stability validation. Test subprocesses receive only OS and
browser runtime settings; Shopify credentials, proxy settings, user pytest
options and third-party pytest plugin auto-loading are excluded. The tests use
mocks, synthetic DOM and local HTTP servers, without running storefront smoke.

The Jenkins multibranch job is **Framework - Offline Regression** under
`Monitoring / Shopify / UI automation`. Its GitHub source discovers branches
without open PRs and tests repository-origin PRs merged with their target branch.
Each branch/PR cancels its previous unfinished build when a new one starts.
Fork discovery is disabled. Checkout uses the existing GitHub SCM credential;
no storefront signing credentials are bound to test execution.

Reports are isolated in `artifacts/framework-ci/build-<number>-<start-time>/`.
`pytest.xml` is published through Jenkins JUnit, alongside `summary.json` and
sanitized logs for each check. A failing test keeps a failing build while still
publishing scanned diagnostics. Missing/invalid JUnit cannot pass the build.
Scan/infrastructure failures publish only a fixed safe error JSON. Dependency
and browser installation require network access; the regression checks
themselves are offline.

Run the same checks locally after installing the locked dependencies and browsers:

```powershell
python -m pip install -r requirements-test.lock.txt
python -m playwright install chromium webkit
python scripts/run_framework_ci.py --report-dir artifacts/framework-ci/local-unique-id
```

Use a new report directory for each invocation. GitHub trigger/status and main
branch protection details are recorded in `docs/framework-ci.md` after live
integration validation.

The repository currently has two Mondressy formal Jenkins Jobs and one fixed
site Lavetir Readonly pilot Job. Lavetir Full is locally validated and its
manual-only pipeline definition is prepared separately; no new cron is enabled
until the joint observation gate is complete:

| Job | Pipeline | Suite | Cases (`both`) | Schedule | Stability |
| --- | --- | --- | ---: | --- | --- |
| Mondressy - Website Smoke - Full | `Jenkinsfile.full` | `website_smoke_v1` | 30 | Daily | YES |
| Mondressy - Website Smoke - Readonly | `Jenkinsfile.readonly` | `website_smoke_readonly_v1` | 22 | Every 4 hours | NO |
| Monitoring / Shopify / UI automation / test | `Jenkinsfile.readonly.lavetir` | Lavetir `website_smoke_readonly_v1` pilot | 22 | `H H/4 * * *` | NO |
| Lavetir - Website Smoke - Full | `Jenkinsfile.full.lavetir` | `website_smoke_v1` | 30 | Manual (pending formal Job) | YES (pending 6-run gate) |

The two Mondressy schedules remain configured at the Jenkins Job level / UI:

- Readonly: `H */4 * * *`
- Full: `H H * * *`

`Jenkinsfile.full` and `Jenkinsfile.readonly` contain no `triggers`, `cron`, or
`pollSCM` configuration. The Lavetir pilot is the only repository pipeline
with a trigger, and its only trigger is `cron('H H/4 * * *')`. The repository
retains the original `Jenkinsfile` as a historical and rollback reference.
There is no second UI cron for the Lavetir pilot.

### Jenkins pipeline stages

The formal pipelines use the following stages:

```text
Checkout
Environment
Install Dependencies
Prepare Run
Install Playwright Browsers
Static Validation
Runtime Contract
Signed Request / Site Access
Smoke
Secret Leakage Check
Result Validation
Current Run Snapshot / Final Scan / Artifact Archive / Acknowledge
```

The Full pipeline also records Stability after the build. Readonly does not
execute `record_stability.py` and does not enter Full Stability history. The
Lavetir q4h pilot uses Jenkins Build History only; it does not add
`record_stability.py`, `STABILITY_*`, or a stability history file. If the Smoke
stage fails, the pipeline preserves the failure result while allowing Secret
Leakage Check, Result Validation, and artifact archiving to run.

The Checkout stage records the checked-out workspace SHA. Jenkins credentials
are bound by credential ID and their values are redacted from logs; no
credential value is documented here.

## Stability tracking

Stability belongs only to Website Smoke V1 / Full. The Full Job uses the UI
schedule `H H * * *` (daily). Readonly runs are not included in Full Stability
history.

The Full CI finalizer consumes the exact current-context `results.json` after a
build. Only a complete `both` run is eligible for a formal stability sample. It writes a safe
`artifacts/website-smoke-v1/<site>/<run_id>/stability_record.json` and atomically
updates the ignored runtime cache only after the scanned snapshot is archived
and acknowledged:
`artifacts/website-smoke-v1/<site>/stability-history.jsonl`. The per-build
`results.json` and `stability_record.json` archives remain the source of truth;
the JSONL file is only a workspace cache and may be absent after cleanup.
Stability identity is scoped by suite and site, so results from Mondressy and
Lavetir never enter the same baseline or streak.

The stability metadata contract uses the actual checked-out workspace HEAD:
Jenkins captures `checkout scm`'s `GIT_COMMIT` (with `git rev-parse HEAD` as a
workspace-only fallback) into `GIT_COMMIT_SHA` and binds it to the run context.
Strict CI rejects a missing or mismatched commit. For legacy/manual collection,
a record with no commit SHA is retained for diagnosis but is not
eligible for a formal baseline. Schema and secret gates are initialized as
`NOT_RUN`, then set to `PASS` or `FAIL` from their real process exit status;
the collector never promotes `NOT_RUN` or overwrites `FAIL`.

The summary reports the existing stability statuses:

```text
COLLECTING
STABLE
ACCESS_UNSTABLE
FLAKY
UNSTABLE
MIXED_BASELINE
MIXED_SITE
```

Stability is observational: it does not change the Website Smoke V1 exit-code
or Jenkins result contract.

Offline summary and validation commands:

```powershell
python scripts/summarize_stability.py --site mondressy --last 10
python scripts/validate_stability.py
```
