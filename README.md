# Shopify UI Automation

Standalone Shopify / DTC UI Automation Framework for Mondressy. The framework uses desktop and mobile browser automation to validate core positive shopping journeys.

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

Website Smoke V1 covers three primary journeys:

1. **Direct PDP Purchase**

   `Direct PDP → Variant → Add To Cart → Cart Consistency`

2. **Search Purchase**

   `Home → Search → Search Results → PDP → Variant → Add To Cart → Cart`

3. **Browse Purchase**

   `Home → Navigation → Collection → PDP → Variant → Add To Cart → Cart → Quantity → Checkout`

### Main commands

```powershell
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport both
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport desktop
.\.venv\Scripts\python.exe scripts\run_website_smoke_v1.py --viewport mobile
```

### Exit code contract

- `0` = all functional cases PASS
- `1` = one or more cases FAIL or BLOCKED
- `2` = CLI or configuration error

This contract is intended for the future Jenkins build result.

## Artifacts

Runs write to:

```text
artifacts/<suite>/<run_id>/
```

The output may include `results.json` and failure screenshots. `artifacts/` is intentionally excluded from Git; Jenkins will archive runtime artifacts in the integration phase.

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

All viewport-aware runners accept `--viewport desktop`, `--viewport mobile`, or `--viewport both` where supported.

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

Checkout automation validates checkout entry and core checkout state only. It does not:

- enter payment information;
- complete PayPal or Shop Pay;
- intentionally trigger 3DS;
- submit an order; or
- complete a real payment.

## Frozen baseline

**Website Smoke V1 Baseline**

- Date: `2026-08-20`
- Freeze run: `20260820_173410`
- Desktop: `15/15 PASS`
- Mobile: `15/15 PASS`
- Combined: `30/30 PASS`
- Legacy Smoke: `16/16 PASS`

The frozen scope covers Direct PDP Purchase, Search Purchase, and Browse Purchase across desktop and mobile profiles.

## Jenkins readiness

`Jenkinsfile` is the orchestration entry point. It checks out the repository,
creates `.venv`, installs `requirements.lock.txt`, installs Chromium and WebKit,
runs the offline/runtime/site-access gates, executes one Website Smoke V1 target,
validates the resulting schema, and always archives `artifacts/**`.

The `Secret Leakage Check` stage runs after Website Smoke V1 and scans persisted
result, metadata, error and artifact output for the actual injected secret
values; it reports only variable names and safe paths. Jenkins credential
masking protects the console.

The Jenkins Agent is expected to provide the OS libraries required by Chromium
and WebKit. The pipeline only performs the unprivileged project-level browser
install (`playwright install chromium webkit`); it does not use `sudo`, apt, or
`--with-deps`.

The default/final target is `SMOKE_VIEWPORT=both`; `desktop` and `mobile` are
available for isolated diagnosis. The pipeline binds the three Secret Text
credentials whose IDs match the Signed Request environment variable names. Proxy
variables remain optional and are inherited from the Jenkins environment; when
`SHOPIFY_PROXY_SERVER` is absent, the framework uses direct mode.

The pipeline uses Jenkins `catchError` only to continue to result validation and
artifact archiving; Python Exit 1/2 still sets the Jenkins build to FAILURE. No
credential values are stored in this repository.
