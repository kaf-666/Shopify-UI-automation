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

## Jenkins next step

The next infrastructure step is Jenkins integration. The planned pipeline will cover Git checkout, Python environment bootstrap, Playwright installation, runtime secret injection, proxy configuration, Website Smoke V1 execution, artifact archive, exit-code build result, and scheduling.

`Jenkinsfile` is intentionally not included in this baseline; Jenkins design and integration are the next phase.

### JENKINS_PORTABILITY_TODO

The current local Signed Request secret source and proxy settings are environment-specific. The Jenkins phase must replace local machine assumptions with injected credentials and CI-managed proxy configuration without changing the frozen Website Smoke business cases.
