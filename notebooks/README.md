# BOJ notebooks

[01 · What does smoothing do to a published rate step?](01_rate_steps_and_trailing_means.ipynb) retrieves the Bank of Japan's daily Basic Discount Rate and Basic Loan Rate for 2024, validates a complete calendar, and compares the original values with a trailing calendar-day mean. The experiment changes the window from 5 to 20 days.

## Run

Requirements: [uv](https://docs.astral.sh/uv/), Git, Python 3.13+ (uv can install it), and internet access to GitHub/package sources and the public BOJ API. No API key or worker checkout is needed.

```sh
cd notebooks
uv sync --locked
uv run --locked python execute.py
```

The notebook environment locks Haybarn 2.0.0rc11 and plotting/Jupyter dependencies. The visible literal worker command is `uvx --from git+https://github.com/Query-farm/vgi-boj@v0.1.0 --with vgi-python==0.51.1 --with vgi-rpc==0.53.1 vgi-boj-worker`. The tag resolves to immutable source commit `3d296c670b302ace974e675a0d3ac41846c435f8`; uv installs it in its cache. Upgrade the release and compatible protocol pins deliberately and rerun both examples.

Optional editor kernel:

```sh
uv run --locked python -m ipykernel install --user --name vgi-boj-notebooks --display-name "Python (vgi-boj notebooks)"
```

Use this directory as working directory, then Restart Kernel → Run All. The runner uses a temporary kernel registration and preserves the previous successful notebook if execution fails. TLS verification stays enabled; if the local Python runtime needs an explicit trusted CA bundle, configure `SSL_CERT_FILE` as described in the worker README.

## Separate experiment

```sh
uv run --locked python execute.py --parameters '{"WINDOW_DAYS":20}' --output-dir artifacts/window-20
```

Overrides require separate output. Branding, docs, runner and environment companions are copied with the experiment. Restore 5 for defaults. Generated artifacts, logs and virtual environments are ignored.

## Source and interpretation

Source: **Bank of Japan**, database IR01, series MADR1Z@D. The Bank does not guarantee this notebook's content. [Official API manual](https://www.stat-search.boj.or.jp/info/api_manual_en.pdf) and [service notice](https://www.stat-search.boj.or.jp/info/api_notice_en.pdf) describe API use and attribution. This notebook is a local analysis, not a hosted API service.

A single bounded observation call requests inclusive month bounds 202401–202412, which is the daily API's native request syntax. `max_rows=0` drains matching pages instead of truncating results. The curated `datasets` discovery is local. Returned frequency/units are checked before parsing YYYYMMDD daily labels. UTC retrieval bounds, original source timestamp (including JST offset), source update date and original numeric text are retained. The current API does not provide a historical revision archive.

The notebook explicitly chooses the official endpoint and clears insecure-fixture configuration. No synthetic or replay fallback is used. Service failures stop execution. Each run retrieves fresh data; compare observation fingerprints before treating the experiment as a pure window change. The fingerprint excludes response-generation timestamps but includes units, frequency, periods and original values.

A daily calendar distinguishes absent rows from present missing values. A trailing mean requires all days in its window; no forward fill or zero substitution occurs. One percentage point equals 100 basis points. The calculated mean is not a separately published rate, a volume-weighted borrowing rate or a forecast. It describes smoothing of the named series without attributing causes or making market recommendations.
