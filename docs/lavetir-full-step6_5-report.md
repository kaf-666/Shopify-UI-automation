# Step 6.5 — Lavetir Full validation record

This record captures the local Step 6.5 gates completed on the
`feat/multi-site-pilot` worktree. It does not claim a merge, a Jenkins run, or
a scheduled observation that was not executed from this environment.

## 6.5A — Full capability

- Lavetir `website_smoke_v1` is enabled in site configuration.
- Cart and Checkout selectors are configuration-driven and are consumed by the
  existing public `CartDrawer`, `ShoppingFlow`, and website-smoke runner.
- The shared `TRANSACTIONAL_SAFE` policy classifies cart add/change/update and
  checkout entry as `EXPECTED_MUTATION`; account, customer, order, payment,
  wallet, subscription, purchase, and checkout-session mutations are
  `HIGH_RISK_MUTATION`; other first-party mutations are `UNEXPECTED_MUTATION`.
- No site-name branch or duplicate Lavetir runner/page-object/case family was
  added.

## 6.5B — Local evidence

| Site / suite | Viewport evidence | Result | Mutation evidence |
| --- | --- | --- | --- |
| Lavetir Full | `artifacts/website-smoke-v1/lavetir/20260914_132621/results.json` | Desktop 15/15 PASS | Expected 11; Unexpected 0; High-risk 0 |
| Lavetir Full | `artifacts/website-smoke-v1/lavetir/20260914_131529/results.json` | Mobile 15/15 PASS | Expected 11; Unexpected 0; High-risk 0 |
| Mondressy Full | `artifacts/website-smoke-v1/mondressy/20260914_130347/results.json` | Both 30/30 PASS | Expected 14; Unexpected 0; High-risk 0 |
| Lavetir Readonly | `artifacts/website-smoke-readonly-v1/lavetir/20260914_131955/results.json` | Both 22/22 PASS | Readonly violations 0 |
| Mondressy Readonly | `artifacts/website-smoke-readonly-v1/mondressy/20260914_132010/results.json` | Both 22/22 PASS | Readonly violations 0 |

Offline gates also passed: `pytest` (163 passed), site-config validation for
both sites, runtime contract (`--check full`), result-schema validation for all
five evidence files, traffic-inventory validation, stability validation,
secret-leakage scan, `compileall`, and `git diff --check`.

## 6.5C–6.5F — Delivery state

- **6.5C main merge:** not executed. The worktree remains on
  `feat/multi-site-pilot`; no merge or push was performed.
- **6.5D main manual Jenkins run:** not executed; Jenkins access is not
  available in this environment.
- **6.5E formal Lavetir jobs:** `Jenkinsfile.full.lavetir` is prepared as a
  manual-only pipeline with no cron trigger. It is not marked observed until a
  real Jenkins job runs it.
- **6.5F joint observation:** pending. The required four-job, six-scheduled-run
  observation has `0/6` runs recorded here, so final stability flags remain
  `PENDING`.

