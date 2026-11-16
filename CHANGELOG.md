# Changelog

## 0.1.3 (2026-11-16)

- Bump vgi-python to 0.52.2.

## 0.1.2 (unreleased)

- Docs link to the BOJ Time-Series Data Search API: the catalog documentation now ends with a
  "Sources and references" section linking the upstream service and its API
  documentation (VGI183).
- The `main` schema comment is now a short one-line summary, so the schema's
  `vgi.doc_md` adds detail instead of repeating it (VGI102).
- The catalog's executable example now calls the worker instead of only
  querying DuckDB's own catalog (`duckdb_functions()`): `boj.main.datasets(query := 'rate')` lists the curated interest- and exchange-rate databases.

## 0.1.1 (2026-11-14)

- The catalog comment is now one plain-text sentence saying what the worker covers (rule
  VGI001). The longer Markdown description stays in the catalog's `vgi.doc_md` and
  `vgi.doc_llm` tags.

## 0.1.0

Initial Bank of Japan worker with five typed SQL functions, authenticated HTTP,
source-aware parsing, bounded retries and reproducible engine/artifact contracts.
