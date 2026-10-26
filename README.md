# Real Exchange Rate Explorer

A Python 3.12 Streamlit app using monthly IMF CPI and exchange rates from January 1976 where available.

## Deploy without installing Python

1. Create a public GitHub repository named `real-exchange-rate-app`.
2. Put `app.py`, `requirements.txt`, and this `README.md` in the repository root, on `main`.
3. Sign in to https://share.streamlit.io using GitHub. Create an app, select the repository, branch `main`, and entry point `app.py`.
4. In Advanced settings select Python 3.12. Leave Secrets empty. Deploy.
5. Save the resulting Streamlit URL and repository URL for the submission.

## Economics

IMF FX inputs are units of local currency per USD. Therefore:

- `S = FX2 / FX1`: Country 2 currency per Country 1 currency.
- `RER = S * CPI1 / CPI2`.
- `RER_reference_mean = mean(RER)` over valid months in the selected sample.
- `real_index = 100 * RER / RER_reference_mean`.

Changing sample dates recomputes the mean. CPI and FX must be positive and present; gaps remain missing. The CSV includes every selected month, even months with missing values. Nominal FX can be charted wherever FX exists, even when CPI is missing. Neither nominal rates nor CPI inputs are normalized. Country 1 real appreciation increases the real index under this definition. A sample average is not an equilibrium estimate.

For UK → U.S., S is USD per GBP: if GBP/USD input is 0.8 GBP per USD, S = 1/0.8 = 1.25 USD per GBP. The U.S. dollar's rate against itself is 1 by definition, not an imputed market observation.

## Data and coverage

Official API information: https://data.imf.org/en/Resource-Pages/IMF-API

Base: `https://api.imf.org/external/sdmx/3.0`.

- CPI: `/data/dataflow/IMF.STA/CPI/+/{ISO3}.CPI._T.IX.M`
- FX: `/data/dataflow/IMF.STA/ER/+/{ISO3}.XDC_USD.PA_RT.M`
- Parameters: `startPeriod=1976-01`, `endPeriod=YYYY-MM`.
- Accept: `application/vnd.sdmx.data+csv;version=2.0.0`.
- Country metadata: `/structure/dataflow/IMF.STA/CPI?references=all`, using the CL_COUNTRY codelist in the JSON response. If metadata fails, the app labels its ISO-country fallback explicitly.
- Euro area: `G163`. Coverage varies; no artificial historical euro series is constructed.

Live API availability and response formats must be checked after deployment. Errors are displayed rather than replaced with another source or simulated values. The app retries transient HTTP failures, caches downloads for six hours, and provides a refresh button. Currency redenominations and changes in monetary arrangements may limit comparisons across long samples. SCALE values (or UNIT_MULT, if supplied) scale observation levels. SDMX monthly dates such as 2024-M01 are converted to month timestamps.

## Required checks after deployment

1. Select United Kingdom (GBR) and United States (USA). Confirm nominal units say USD per GBP.
2. Select a period with valid data; download the CSV.
3. For a valid row, calculate `fx2_lcu_per_usd / fx1_lcu_per_usd`, multiply by `cpi1 / cpi2`, and compare with `rer`.
4. Average non-missing `real_index` values: the result must be 100, within floating point rounding.
5. Change both sample endpoints, download again, and repeat the average check.
6. Try Real, Nominal, and Both. Both uses separate real and nominal axes; nominal stays in actual currency units.
7. Test Euro area, a country with limited coverage, a period without overlap, and a simulated network failure. Check that missing data are not filled.
8. Submit the exported CSV and a document with both URLs, actual prompt/chat evidence, model/date, and a manual verification from that CSV.

## Validation status

Tested October 26, 2026 with live IMF data and the deployed Python 3.12 app:

- UK as Country 1 and U.S. as Country 2: nominal units are USD per GBP.
- January 2000–October 2026 CSV: 322 rows, 320 valid RER months, mean real index 100.0. Every valid exported S, RER and real index matches an independent calculation.
- February 2000–September 2026 CSV: 320 rows, 319 valid RER months, mean real index 100.0. The reference RER changes when sample dates change.
- Real, Nominal and Both controls work. Both uses separate axes; Nominal is in actual currency units.
- The JSON IMF country metadata loads successfully.

Example from January 2000: CPI1 = 72.6, CPI2 = 77.41148469082412, FX1 = 0.60922 GBP per USD, FX2 = 1. S = 1.6414431568234793 USD per GBP and RER = 1.539419811689907.

## Optional local run

With Python 3.12: `pip install -r requirements.txt`, then `streamlit run app.py`.
