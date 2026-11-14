# vgi-boj

Python VGI worker for Bank of Japan statistics. It exposes five typed SQL
functions with discovery metadata, original source values and HTTP bearer authentication.

## Run locally

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```sh
cd /absolute/path/to/vgi-boj
make build
```

Stock DuckDB 2.0.0 with the signed Query.Farm VGI extension:

```sql
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm
  WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;
ATTACH 'boj' (TYPE vgi,
  LOCATION '/absolute/path/to/vgi-boj/.venv/bin/vgi-boj-worker');

SELECT period, value, value_text, unit
FROM boj.main.observations('IR01', ['MADR1Z@D'],
  start_period := '202401', end_period := '202401')
ORDER BY period;
```

For Haybarn 2.0.0rc11, use its VGI extension and the same `ATTACH`/`SELECT` statements.
The worker can also use `launch:/absolute/path/to/worker` for Unix-socket pooling.
Diagnostics go to stderr; stdout belongs to Arrow IPC in stdio mode.

## SQL interface

| Function | Purpose |
|---|---|
| `datasets(query := '')` | Ten curated official database starting points. |
| `series(database, query := '', language := 'en')` | Full database metadata with optional local filtering. |
| `layers(database)` | Hierarchy headings and layer numbers. |
| `observations(database, series_codes)` | Same-frequency series with automatic source pagination. |
| `layer_observations(database, frequency, layer)` | Source hierarchy and frequency selection with pagination. |

`datasets` is curated; all valid official database identifiers can be used. The
API manual has the complete database list. `language` accepts `en` or `jp` and
preserves English/Japanese fields when supplied. `include_headings := true` on
`series` includes metadata headings with NULL series codes. `layers` always returns
only headings; its inherited `include_headings` argument has no effect.

`series_codes` accepts 1–1,250 distinct codes from one database and frequency,
without a database prefix (use `MADR1Z@D`, not `IR01'MADR1Z@D`). Codes are normalized
to uppercase. For hierarchy queries, frequency is `CY`, `FY`, `CH`, `FH`, `Q`, `M`,
`W` or `D`; layer is one to five comma-separated integers or `*` wildcards.
The source limits layer matches to 1,250 **before** frequency filtering. Narrow
large selections or split large code lists if a server rejects a long request URL.

Named `start_period`/`end_period` are inclusive native request bounds: annual `YYYY`,
half-year `YYYY01`/`YYYY02`, quarterly `YYYY01`–`YYYY04`, or `YYYYMM` for monthly,
weekly **and daily** requests. Returned daily periods remain `YYYYMMDD` strings;
other native periods are also preserved, with their frequency, without inventing dates.
`W` can return several weekly subtypes; code queries cannot mix their frequencies.

The API returns at most 250 series / 60,000 observations per page. The worker
follows `NEXTPOSITION` exactly and rejects repeated series, non-advancing cursors,
invalid array alignment and inconsistent metadata. `max_rows := 0` (default)
retrieves all matching pages; a positive value is an explicit cap, up to 10 million.
A cap may end inside one source page. Continuation state contains source positions,
counts and at most 1,250 series identifiers, never observations or credentials.
`last_update` and `source_timestamp` describe current source data, not historical
vintages. This API does not provide an ALFRED-equivalent revision archive.

Uses the Bank of Japan Time-Series Data Search API. The Bank of Japan does not
guarantee this worker's content.

Official references: [API manual](https://www.stat-search.boj.or.jp/info/api_manual_en.pdf),
[service notice](https://www.stat-search.boj.or.jp/info/api_notice_en.pdf).
The notice asks operators releasing a service that uses the API to notify the Bank
at `post.rsd17@boj.or.jp` with subject `[Release of service using API]` and to provide
source attribution. This repository does not send that notification automatically.

## Authentication and deployment

The official upstream API is public and needs no API key. HTTP access to this
worker is a separate authentication boundary. Configure one or more strong bearer
tokens through `VGI_BEARER_TOKENS`, using `token=principal` pairs separated by commas:

```sh
# Inject real values from your deployment secret manager; do not commit them.
export VGI_BEARER_TOKENS="${WORKER_BEARER_TOKEN}=analytics"
.venv/bin/vgi-boj-worker --http --host 127.0.0.1 --port 8000
```

Tokens must contain 32–4096 characters from the documented bearer alphabet
(letters, digits, `.`, `_`, `~`, `+`, `/`, `-`), without `=`, commas or whitespace.
Malformed configuration stops startup without printing the token. Unset bearer
configuration leaves the SDK in its default anonymous mode: set it for shared
services. The SDK also supports OIDC/JWT configuration; this project's measured
authentication contract covers static bearer tokens, including multiple principals.
`/health` stays unauthenticated for readiness checks; RPC endpoints require the token.

```sql
INSTALL httpfs FROM core;
LOAD httpfs;
ATTACH 'boj' (TYPE vgi, LOCATION 'https://your-worker.example',
  bearer_token '<injected-worker-token>');
```

Use TLS at your reverse proxy for remote access, with restricted network ingress,
request/concurrency limits and a deployment secret manager. Process-local request
spacing does not coordinate across replicas; allocate a shared source request budget
at the deployment layer. No credentials are sent to the central bank or serialized
into continuation state. Query failures omit upstream response bodies and URLs.

```sh
docker build -t vgi-boj:local .
docker run --rm -p 127.0.0.1:8000:8000 \
  --env VGI_BEARER_TOKENS vgi-boj:local
# Stdio container transport:
docker run --rm -i vgi-boj:local stdio
```

The container uses Python 3.13, locked dependencies, an unprivileged UID and a
health check. Add platform resource limits appropriate to query concurrency and
response sizes. Do not publish unauthenticated containers directly to the Internet.

## Operations and data semantics

NULL SQL arguments are rejected. Missing observations become SQL NULL; original
numeric strings remain in `value_text` for explicit DECIMAL conversion. `value`
is a DOUBLE convenience column and has ordinary binary floating-point limits.
Units, frequency, seasonal-adjustment categories and source period conventions are
preserved, not harmonized across banks. Use function arguments for source filtering;
SQL `WHERE` clauses run after retrieval. An explicit `max_rows` cap intentionally
returns a subset; it is not an implicit source pagination limit.

Each source request has a timeout, bounded response size, retry budget, process-local
spacing and retries for 429/5xx/network failures. Retry-After is respected; a delay
beyond the budget fails the query instead of retrying early. Redirects are rejected.
Malformed or inconsistent responses fail visibly rather than becoming partial success.
There is no persistent cache or automatic background polling. SQL consumers should
use transactions/staging tables when persisting streamed results from an API.

| Environment setting | Default | Meaning |
|---|---|---|
| `BOJ_API_BASE_URL` | Official API endpoint | Operator-controlled HTTPS override; no URL credentials, query or fragment. |
| `BOJ_TIMEOUT_SECONDS` | 30 | Socket timeout (0.1–120 seconds). |
| `BOJ_MAX_ATTEMPTS` | 4 | Attempts per page/request (1–6). |
| `BOJ_RETRY_BUDGET_SECONDS` | 120 | Total request/retry budget (1–600 seconds). |
| `BOJ_MIN_INTERVAL_SECONDS` | 1 | Minimum spacing between requests in one process (0–60 seconds). |
| `BOJ_MAX_RESPONSE_BYTES` | 16 MiB | Response bound (1 KiB–64 MiB); narrow source selection on overflow. |
| `BOJ_ALLOW_INSECURE_HTTP` | unset | `1` permits only explicit loopback HTTP fixtures. |

TLS certificate verification is always enabled. If a managed Python installation
cannot find your system CA store, point `SSL_CERT_FILE` at the trusted CA bundle;
do not disable verification. ECB and BoE XML parsers reject DTDs, entities and
external references. BoJ JSON parsing rejects non-finite numbers and preserves
fractional numeric lexemes before Arrow conversion.

## Verification and release

```sh
make check        # Ruff format/lint, mypy strict, pydoclint, pytest, real SQL, metadata
make smoke-live   # Explicit live checks against the official source
make package      # Wheel and source archive in dist/
uv run python ci/verify-artifacts.py --python 3.13
```

`make check` runs original starter and product SQL tests over Haybarn stdio, Unix
launch and HTTP, followed by metadata assurance L2 and stock DuckDB 2.0.0 over stdio
and authenticated HTTP. Fixtures are deterministic and do not require real keys.
The metadata gate fails on info findings and has no waivers. CI repeats checks on
Linux/Windows and Python 3.13/3.14, plus container and installed artifact contracts.
Local verification reports live in `build/`; see [VALIDATION.md](VALIDATION.md).

Starter: [Query.Farm Python starter](https://github.com/Query-farm/vgi-starter-python),
revision `9cca9105474d6e0052b8e343c8ea21f3d1a7503c`, template 1.3.0.
The locked SDK is `vgi-python 0.51.1` with `vgi-rpc 0.53.1`; keep those protocol
versions aligned with the tested extension. Original examples/tests remain under
`examples/starter_samples`, separate from this worker's public catalog.

The included release workflow gates tagged releases on CI and produces signed
artifacts and provenance. No remote repository, package publication or hosted
service is created by a local build. Source code is 0BSD; the banks' data terms
and attribution requirements apply separately. Source/API changes may require a
worker update. Report reproducible failures with a redacted query and source period,
without credentials or private server URLs.

## Notebooks

[Rate steps and trailing means](notebooks/README.md) — an executed public-data walkthrough of daily BOJ rates, complete calendar windows and smoothing gaps in basis points.
