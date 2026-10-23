# Shared purchase readiness

`ProductPage` owns the current DOM contract. Full Direct/Search/Browse and
Readonly use `validate_purchase_area()` before selecting options, then
`wait_purchase_ready(phase="post_selection", expected_color=..., expected_size=...)`
after selection. ShoppingFlow and runners keep their own failure categories,
case dependencies, selections and transactions.

| Phase | Required conditions |
| --- | --- |
| `pre_selection` | Purchase area attached, visible title, actionable color, detected size model with a normal available size, visible ATC. ATC may be disabled before selection. |
| `post_selection` | The same structural conditions, enabled ATC, actual checked color and size matching the request continuously for 200 ms. |
| `legacy` | Existing strict enabled-ATC contract; callers are not silently switched to pre-selection semantics. |

Custom measurement triggers do not substitute for a normal saleable size in
either phased contract. Availability counts are lower bounds (existence), not
complete option inventories. Selection APIs still enumerate actual options.
Locators and the size model are resolved from the current DOM on each sample;
the readiness path does not cache element handles or models.

Size readiness reads configured group attributes, accessible group names, form
association, controls and all checked values in one read-only DOM task. It no
longer chains per-attribute 50 ms protocol reads, whose timeouts could discard
an otherwise usable group under mobile renderer load. Actual option selection
still uses the existing resolver. Playwright's native disabled check remains
authoritative (fieldset/ARIA/explicit roles), with at most 250 ms and only the
remaining shared deadline; it does not grant another readiness budget. Polls
that finish after the deadline cannot pass. Waiting for size availability no
longer re-enters the serial metadata detector between samples.

`size_probe_status` distinguishes a completed DOM observation (`OK`), a skipped
probe, deadline exhaustion and DOM/native-probe errors. A probe error remains
fail-closed and adds `size_probe_error` to failed gates; candidate counts from
an unsuccessful read must not be interpreted as proof of an absent size group.

`validate_purchase_area(timeout_ms=15000)` shares one deadline across `initial`,
`title_price` and `confirmation`. Empty title/price text can hydrate within that
budget. Confirmation re-resolves the form and reads current text again. The
returned dictionary includes title, price, availability counts, ATC enabled
state and `readiness_stages` (stage name, remaining budget and elapsed time).
Repeated confirmation stages are bounded. No stage receives a fresh 15 seconds.

Each low-level wait exposes `last_readiness_report`: phase/stage, budget,
elapsed time, poll count, first failed gates and a timeline capped at 12 samples
(first sample plus the latest 11). Failures include the same bounded timeline
and first failure, using the active phase's gates. Selected-state reads also
use the remaining deadline instead of full business-option enumeration.
The optional diagnostic hook remains observational and cannot alter the result
by raising an exception. No title/price text is added to readiness diagnostics.

Readonly waits for the selected state to settle rather than rejecting an early
checked-state sample. Its separate ATC case revalidates the stored requested
variant through the same post-selection contract. It never clicks ATC.
Full still performs a fresh post-selection check immediately before its real
cart action, even when an earlier PDP case already verified the variant.
Mutation auditing, variant identity verification and cleanup are unchanged.

Mondressy's `product.variant_sync` profile also checks the theme's master
`[data-product-select]` value against `[data-variant-json]` and the checked
native options, plus the displayed color label. These are read together from
the main product container, excluding recommendation widgets. The submitted
variant must match continuously during the same 200 ms stability window;
asynchronous updates use the existing deadline. Missing metadata, ambiguous
options and stale variant IDs fail before ATC, with `variant_sync_status`,
`submitted_variant_id` and `expected_variant_id` in the bounded diagnostics.
The check only observes state; it never sets a variant ID or reselects a swatch.

Mondressy also opts into `variant_sync.initialization: archetype_product`.
Pre-selection waits for the theme's registered product/variant controller to
own the current product root and form. Native radios can already be visible
and enabled before the theme binds their change handlers; clicking in that
window changes `checked` without updating the color label or submitted ID.
The readiness gate shares the existing deadline, records `theme_ready_status`,
and rejects stale instances after a form replacement. `select_color()` checks
the same condition before its first click, including callers that do not use
the full readiness flow. This automation change prevents premature test
interaction; it does not change the published storefront theme or establish
that initialization delay caused every observed production mismatch.

Offline regressions run against Chromium and WebKit synthetic DOMs: delayed
text/size initialization, replaced groups and buttons, model changes, unavailable
normal sizes, custom-only options, checked-state reset/recovery and persistently
disabled ATC. Run `python scripts/run_framework_ci.py --report-dir <new-directory>`
for the complete offline gate. Full remains 30 cases and Readonly 22 for `both`;
the existing CLI exit codes (0/1/2) remain unchanged.
