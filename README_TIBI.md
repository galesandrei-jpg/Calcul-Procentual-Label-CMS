# Tibi — monthly CMS Deals and Distribution client forecast (review patch)

## What this patch changes

This is **one additive Streamlit page**. It does not modify `app.py`, `pages/cms_deal_report.py`, the legacy YouTube/Sheets helpers, Google Sheets data, deployment configuration, or any existing file in `main`. The new page appears in Streamlit's `pages/` navigation when the patch is installed.

| New file | Purpose |
| --- | --- |
| `pages/client_split_forecast.py` | Source configuration, read-only YouTube preview, audit, controlled publish |
| `src/client_split.py` | Dated rates, contract safeguards, arithmetic, overlap checks, isolated Sheets writer |
| `tests/test_client_split.py` | Existing feature invariants, safe write and formula handling |
| `tests/test_page_offline.py` | Page compilation and summary assertions |
| `tests/test_monthly_cms_rates.py` | Date-specific rate changes and intermediary calculations |

All changes are additive and intended for review on a feature branch. Do not merge or deploy until the validation and finance review below are complete.

## Tibi's requested output

* For each month and client, show gross YouTube CMS revenue **EUR**, US-only revenue, the existing 10% US-revenue withholding estimate, the share payable to the client, HaHaHa's gross commission, any intermediary fee, and **final HaHaHa retained revenue**.
* Support both **CMS Deals** and **Distributie**. A client with multiple legitimate CMS groups is summarized in one row, with underlying group-by-group detail retained.
* Reconcile gross CMS Deals and Distribution totals against the existing monthly master sheet, **read-only**.
* A preview with warning flags, audit detail and CSV export is available before writes.
* Publishing creates/updates only the new tab `Client Split Forecast` in the **existing** Google Spreadsheet, never the original `Calcul Procentual Venituri Luna` tab.

The report tab has three managed regions, with intentional blank separator columns:

| Region | Contents |
| --- | --- |
| `A:R` | Monthly client/group calculation detail with transparent formulas |
| `T:AA` | User-approved group mappings, contract references and manual rate adjustments |
| `AC:AR` | Tibi's client/month/category totals, after intermediary fees |

This is a **forecast**, not an instruction to pay invoices. For each group the financial convention is:

```
US withholding estimate     = 10% × US-only estimatedRevenue (EUR)
Distributable after tax      = gross estimatedRevenue (EUR) − withholding
Client payable              = distributable × (1 − HaHaHa deal share)
HaHaHa gross commission     = distributable − client payable
Intermediary fee            = gross commission × (1 − HaHaHa post-intermediation factor)
HaHaHa final net            = gross commission − intermediary fee
```

For CMS Deals, the post-intermediation factor must be explicitly sourced or manually verified. It applies only to HaHaHa's gross commission. Missing client rates or retention factors block publishing. No private contractual rates are bundled.

## Mappings preloaded from Tibi's message

The eight original CMS Deals groups remain exactly as in `app.py`. In addition, the new page preloads the following **Distribution** configuration (no percentages baked into GitHub source):

| Client / source | Group/channel source | Safety note |
| --- | --- | --- |
| Zurli | `ZhkeOTUjhr8` | Standard ZURLI contract rate can be looked up |
| Zurli + Ha co-production | `a0FL5zlfU1M` | Do not presume the standard Zurli rate applies |
| Zurli CID (SR + AT) | `DXRapvE6aOc` | Confirm scope and whether it overlaps co-production/other groups |
| Soundfeed (SR + AT + MV) | `a—b-ocG_6s` | **Disabled.** Contains Unicode em dash; copy exact ID from YouTube CMS before enabling |
| LaLa Planet | `CKAXSy2vZUc` | Confirm Creator Records versus Soundfeed asset scope, rates and overlap |
| National TV | `channels:UCEiSdg0rO7AyJ7ky_vhzMBg` | Single YouTube channel ID, not a CMS group |
| JUNO / Elixir | `JCbxlxAweg0` | Confirm ELIXIR vs RAKISS contract scope before applying rate |
| Alina Sorescu | `Vlp5PkaizOU` | Verify the applicable contract rate for the reporting month |

Mixton is intentionally excluded. If a client has multiple group rows with the **same legitimate legal client**, use an identical Client label to aggregate them, but do not assign identical percentages merely because the label is the same. **Every selected group's membership is checked for channel overlap; unresolved/failed checks block publishing**. Use `channels:UCxxx,UCyyy` for an explicit channel list; YouTube Analytics permits up to 500 IDs in one filter.

## Reading private rates, not hardcoding them into a public repo

The original **VIETNAM Mixes Tracking** workbook has a `Tabel Raportari` tab. **Simplest option: upload its `.xlsx` file to the new Streamlit page**, which extracts the dated percentages for the current private session. **No extra Google connection, API credentials, or sharing of the original workbook is necessary for this option.** Do not commit the private workbook, its export, amounts, or invoices to this public repository.

An **optional** live-refresh mode uses the app's existing Google Sheets service account. Set `cms_rates_sheet_id` in private Streamlit secrets or paste the original Google Sheet URL/ID; share it with the app service account as Viewer. A live connection is useful only if rates are frequently edited and the app should pick them up without another upload. **The uploaded Excel file takes precedence** when both are supplied; remove it to use the live source.

Both import paths share one strict parser. It reads `Partner` (F), `Deal%` (H), `Luna` (D), `AN` (E) and `Procent Ha dupa intermediere` (M). Numerical percentage cells (`0.13`) become 13%. Zero-percent accounting entries are not automatically taken as a binding zero-commission contract; such exceptional terms require explicit finance confirmation. The parser matches confirmed partner labels for **Unicorn, BG Music, Melosy, Huta Media, Magic and Pine**. It has no mapping for Malwin or Unicorn 2 because no corresponding agreed-rate history was found in the provided workbook. Nor does it assume Unitech Media is Malwin.

A rate is applied to its **own reporting month**, not globally. Historical changes, including BG Music, are selected by month from the uploaded reference. When the selected month is newer than the last rate recorded, the most recent past rate is shown as a **carried-forward assumption** and explicitly requires human confirmation before publish. Missing, conditional or contradictory rates cannot become automatic payment figures. Manual override fields remain available and visible.

For Distribution contract rates, optionally provide the **Informatii Deal Distributie** Google Sheet ID in the separate `Deal-reference spreadsheet ID` field. The existing `Identificare contract` and `Informatii Deal Distributie` tabs can be read (never edited). Those contract rates typically represent **HaHaHa's share**, not the client payout share. Asset-level exceptions, differing shares and temporal deals must be split into appropriate groups or manually approved.

Recommended Streamlit Community Cloud secret entries (set through the deployment UI, **not** in GitHub):

```toml
[client_split]
# Optional: only needed for automated rate refresh without re-uploading Excel.
cms_rates_sheet_id = "YOUR_PRIVATE_VIETNAM_MIXES_GOOGLE_SHEET_ID"
contract_sheet_id = "YOUR_PRIVATE_DISTRIBUTION_CONTRACTS_GOOGLE_SHEET_ID"
us_withholding_rate = 10.0
```

## Safe flow

1. Create a new Git branch (do not edit production `main`), add the files in this patch, and run offline tests.
2. Check the Streamlit page prefilled group IDs, **correct Soundfeed**, and add/verify any missing deal percentages including Malwin/Unicorn 2, Zurli co-production/CID and JUNO/LaLa scope.
3. Select one month first (August 2026 is a useful historical comparison) and click **Fetch YouTube data and preview (no writes)**. Group membership, overlap, source freshness, missing analytics rows and reconciliation are checked here.
4. Compare result with your known partner statements and the master monthly values. Distinct groups covering the same channel are blocked until corrected; missing group membership and unknown rates block publishing. Non-matching gross totals must be consciously reviewed.
5. If every blocking issue is resolved, explicitly check the confirmations and publish **only** to `Client Split Forecast`. An existing unfamiliar tab with that title is refused. Other months previously generated in that tab are retained, with their original tax parameters; selected months are replaced, not duplicated.
6. Open the new `AC:AR` summary and review with Tibi before using it for invoicing.

### Important limits

- YouTube CMS Analytics **does not provide income from Spotify, Apple Music or other DSPs**. Distribution here means Distribution *within the YouTube CMS*; it does not replace a full digital-distribution accounting report.
- The existing main worksheet's Distribution value is a residual calculated from content-owner totals, and may contain assets not covered by configured client groups. Reconciliation will show the difference; it will **never** allocate it to guessed clients.
- The attachment `CMS Distr client report.xlsx` from the email thread has not been used as a precise formatting template. The financial fields and client-level reporting request are implemented, but this is not a visual replica of that attachment.
- An uploaded Vietnam workbook is a point-in-time snapshot. Upload a new copy after rate edits; a live Sheets connection is optional for automatic refresh.
- No live YouTube API or production Sheets write has been exercised in this offline environment. Verify with a staging/test spreadsheet or a single known month before production use.

## Apply without touching current app files

```bash
git switch main
git pull --ff-only
git switch -c feat/tibi-client-split
# From repository root, apply the corresponding .patch file:
git apply --check /path/to/tibi_client_split_v3.patch
git apply /path/to/tibi_client_split_v3.patch
python -m pytest -q tests/test_client_split.py tests/test_page_offline.py tests/test_monthly_cms_rates.py
git diff --stat
# Stage only the new files:
git add pages/client_split_forecast.py src/client_split.py README_TIBI.md tests/
git commit -m "feat: add isolated client-level CMS and distribution forecast"
git push -u origin feat/tibi-client-split
```

Open a **draft pull request** and leave it unmerged until finance approves the source mapping, applicable withholding assumption and intermediary fees.

## Review corrections and validation

- EUR is mandatory for both group and channel queries; failures never retry in YouTube’s default USD currency. See [YouTube query currency documentation](https://developers.google.com/youtube/analytics/reference/reports/query#currency).
- Queries include the last calendar day of the final month.
- Missing total or US analytics rows block payout publication; an explicitly returned zero remains valid.
- Calculations and detail formulas retain precision; currency formatting and client summaries display cents. Independently rounded displayed components may differ by a cent.
- Every new preview clears prior approval checkboxes. Changing the live rate source reloads any still-selected Excel snapshot.
- Tests use synthetic financial amounts and rate fixtures; these are not contractual records.
- Before merge: confirm unknown contracts, verify current group membership against historical scope, run an August 2026 read-only comparison, and exercise publish against a staging spreadsheet.
