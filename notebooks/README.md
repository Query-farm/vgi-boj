# BOJ Statistics: rate steps and trailing means

[01 · What does smoothing do to a published rate step?](01_rate_steps_and_trailing_means.ipynb) preserves the existing live Bank of Japan story and covers **all five public SQL names**. It discovers the daily series and its hierarchy, reconciles code and hierarchy retrieval for August 2024, then compares the original 2024 daily rate with a calculated trailing calendar-day mean. The experiment changes the window from **5 to 20 days**.

## Run locally or in Colab

Install [uv](https://docs.astral.sh/uv/) and Git. From `notebooks/`:

```sh
uv sync --locked
uv run --locked vgi-notebooks 01_rate_steps_and_trailing_means.ipynb
```

Use `notebooks/.venv/bin/python` in an editor, with `notebooks/` as working directory. Restart and Run All after editing parameters. The Colab badge opens the same notebook; its brief bootstrap installs the shared **base** package. Local publication uses `[runner]`. No companion files, worker checkout, API key or manual clone are needed. Internet access is required for the public API and initial package/extension downloads.

The visible connection runs the published release in a separate Python 3.13 process:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'boj' (TYPE vgi,
 LOCATION 'uvx --python 3.13 --from git+https://github.com/Query-farm/vgi-boj@v0.1.0 vgi-boj-worker');
```

The release tag resolves to `3d296c670b302ace974e675a0d3ac41846c435f8`. Its declared dependencies work without the previous notebook's extra SDK/RPC overrides. Notebook dependencies are locked separately; worker transitive dependencies remain within its release-declared ranges. Shared setup creates an empty temporary SQL-secret directory, disables persistent secrets and closes the connection at the end. The official endpoint is selected explicitly; insecure fixture configuration is cleared. TLS verification remains enabled.

## Separate experiment

```sh
uv run --locked vgi-notebooks 01_rate_steps_and_trailing_means.ipynb \
  --parameters '{"WINDOW_DAYS":20}' --output-dir validation/window-20
```

The runner requires separate output for overrides and rejects unknown parameters. The notebook rejects booleans and windows outside integer 2–30 before ATTACH. Successful defaults remain intact when execution fails. Experiment copies retain native SVG output; default SVG image links are committed for GitHub. Review PNGs, logs and audit files are ignored.

Compare the observation fingerprints before interpreting the two runs as a pure smoothing experiment. With unchanged observations, increasing the window retains earlier values longer after a source step and requires more initial history. It does not change the published rate. The chart uses a fixed source-derived ±30-day interval around the last change and a fixed basis-point scale.

## Source and meaning

Source: **Bank of Japan**, database IR01, daily series MADR1Z@D. This notebook uses the Bank of Japan Time-Series Data Search API; the Bank does not guarantee its content. The [official API manual](https://www.stat-search.boj.or.jp/info/api_manual_en.pdf) and [service notice](https://www.stat-search.boj.or.jp/info/api_notice_en.pdf) describe API access and attribution. This is a local analytical notebook, not a hosted API service.

All saved values come from **live public requests through the real released worker**. There is no synthetic input, recording, replay or fallback. Upstream failures stop execution. Each run makes two metadata requests, one full-year code request and one bounded August hierarchy request; each is materialized before DataFrame capture. The curated `datasets` function is local. Source pagination is drained with `max_rows=0`, within the explicitly selected series/branch and date bounds.

The two discovered series share a name/unit but differ in DAILY versus MONTHLY frequency. The daily branch `1,1` is validated against the returned hierarchy before use. Its 31 August keys, original numeric text, values, units and frequency must match the corresponding explicit-code observations. A revision between independent requests is surfaced by the check.

Daily API request bounds use `YYYYMM`; returned daily period labels use `YYYYMMDD`. Retrieval UTC timestamps, source timestamps with their original timezone, source update dates and original value strings are retained. These metadata do not reconstruct historical release vintages. The observation fingerprint includes series, period, original numeric text, unit and frequency, excluding response-generation timestamps.

A complete 366-day calendar distinguishes absent rows from present missing values. The trailing mean requires all calendar days in its window: no forward fill, zero substitution or partial-window mean is shown. The mean is equally weighted by calendar day, not by borrowing volume. Differences use **100 basis points per percentage point**. The analysis describes one named historical series; it makes no claim about policy causes, announcement-time knowledge, market borrowing costs or forecasts.

## Coverage and validation

[coverage.json](coverage.json) maps all five names—`datasets`, `series`, `layers`, `observations`, `layer_observations`—to successful visible SQL, displayed results and reviewed interpretations. Complete runtime functions, tables and views exports are saved, including empty table/view exports. The released and current interfaces both contain five names, counted once by schema/name.

Released catalog: v0.1.0 / `3d296c670b302ace974e675a0d3ac41846c435f8`. Independently captured current catalog: `d28726a034530fcb31610f297a6e512571297f78`. The source differences add metadata and composed examples; named interfaces agree. The default receipt identifies the released runtime: reviewed coverage is 5/5 for both catalogs, while exact runtime coverage is 5 for release and 0 for current. No exact-current analytical execution is claimed.

Validation includes the live default, a detached notebook-only 20-day experiment, parameter guards and a fresh Python 3.12 kernel with existing pandas/Matplotlib/IPython/Jupyter versions imported before the base bootstrap. That path runs the complete live notebook without runner or worker libraries in its kernel; it is a local Colab compatibility check, not a hosted-Colab execution claim. Every default/experiment figure is reviewed. Existing [green CI at the unchanged source baseline](https://github.com/Query-farm/vgi-boj/actions/runs/43614416871) is reused; no source rebuild, full test-suite repeat or container publication is needed.

Presentation, branding and execution follow the [shared notebook standard](https://github.com/Query-farm/vgi-example-notebooks/blob/main/docs/standard.md).
