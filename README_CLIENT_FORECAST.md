# Monthly client forecast

An additional Streamlit page provides a monthly EUR forecast, client totals,
intermediary deductions, audit detail and CSV export. Existing application
files and dependencies are unchanged.

## Private runtime data

The page reuses the existing app's literal CMS mapping without executing that
app. Additional clients, source IDs, aliases, worksheet names and contract
references come from a private JSON configuration uploaded in the page. Rates
come from an in-session Excel workbook, an optional authorized read-only Sheets
lookup, or an explicit manual entry. No private configuration or source
workbooks belong in GitHub. The local private configuration supplied separately
is for the app's upload control only.

The JSON accepts `rules`, `partner_aliases`, `cms_rates_worksheet`,
`retention_header`, `contract_terms_worksheet` and `contract_assets_worksheet`.
Each rule uses the same columns as the app's configuration editor. Uploading
or removing configuration invalidates previews and previously loaded rates.
Uploaded data is held in the current Streamlit session; the page does not
write uploads to disk, publish them to GitHub, or place them in shared caches.
Use an authenticated deployment for sensitive financial reports.

The new page also requires a separate access code of at least 16 characters,
configured as `access_password` under `[client_split]` in private Streamlit
secrets. It shows no financial controls until that code is supplied. There is
no built-in code or default password. This gate does not change the existing
pages or their authentication. Configure the code privately before deployment.

## Workflow

1. Upload the private configuration and dated-rate workbook.
2. Choose the reporting month or range, then fetch a read-only preview.
3. Review rates, retained commission, source membership, overlaps and totals.
4. Publish only after the explicit confirmations. Writes are confined to the
   dedicated `Client Split Forecast` worksheet; other original tabs are read-only.

Missing rates, incomplete analytics and unverified/overlapping channel groups
block publishing. Carried-forward rates and manual overrides remain visible.
Group calculations retain precision; display values are formatted to cents.
Publishing replaces the chosen months while preserving other months and their
withholding settings. Distribution covers YouTube CMS revenue only.

The dated-rate parser expects reporting month/year in D/E, partner in F, deal
share in H, and retained-commission factor in M. The retention heading and
worksheet name are privately configurable. Contract lookup uses counterparty
and share columns B/E; asset lookup uses counterparty D and shares C/E.

## Validation before deployment

Run `python -m pytest -q tests` from the repository root. Tests use fictional
identities and example amounts, with runtime mappings overridden in fixtures.
Live revenue comparisons and staging-sheet publishing still require authorized
credentials. Keep the pull request in draft until those checks are complete.

Only reviewed new code, documentation and fictional tests should be uploaded.
Never upload private JSON, workbook exports, statements, audit reports or
credentials with the feature. Existing production code is not modified.

Validated locally: 75 offline tests pass, including unauthorized access, private configuration loading/removal, monthly arithmetic, preview-only behavior and publishing boundaries. Live API and staging-sheet checks remain required before deployment.
