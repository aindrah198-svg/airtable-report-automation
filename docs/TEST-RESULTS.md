# Verification record

Verified 2026-11-03 (Asia/Jakarta), Python 3.12, local Chrome PDF rendering.

- pytest: **49 tests passed**. Includes injected Airtable pagination, offsets, repeated IDs, page limits, malformed payloads, 401/403/404, 429 Retry-After boundaries, monthly-quota failure, bounded network/5xx retries and safe error messages. No real token or live Airtable base was used.
- Retry tests replace sleep with a recorder, verifying 30-second minimum waits without blocking the test suite.
- Reporting tests cover required fields, invalid dates/statuses, zero/negative/non-finite monetary values, duplicate Order IDs, Decimal totals, HTML escaping and complete bundle contents.
- Actual PDF extraction checks all 24 sample Order IDs, paid total and per-page numbering. The final sample PDF has two A4 pages, both rendered to PNG and visually inspected. Repeated table headers, text alignment and explanatory notes are readable; no clipping or overlap observed.
- Background-job tests cover lock contention, successful archive publication, failure history/logs, secret-safe unexpected errors, interrupted-run recovery and blocked downloads for failed runs.
- CLI tests confirm failure logs are produced without a partial PDF and existing run directories are preserved.
- The unique-output runner was executed successfully in sample mode.
- Browser workflow passed: start sample report, wait for completion, verify 24 records / 3 source pages / $2728.00 paid, download ZIP, preserve history after reload and inspect 390px mobile layout with no document overflow. No JavaScript exceptions observed.
- pip check: no broken requirements.

Sample: 24 orders, 16 paid, $2728.00 collected, $595.50 pending, $760.50 refunded. The three source pages are fixtures, not live Airtable pagination; live pagination is covered by injected client responses.

No authenticated live-base request was made because credentials were not supplied. This is not a production scheduler, source-data authenticity audit, full accessibility review or distributed rate-limit coordinator. GitHub Actions runs the suite independently with Playwright Chromium.
