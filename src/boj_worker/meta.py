"""Discovery metadata: the one file to edit when you rename or extend the worker.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Keep every object documented and ``make lint`` stays at 100.

* Catalog-level tags (title, license, author, links, long description) live in
  ``CATALOG_TAGS`` below.
* Every function and table carries ``object_tags(...)``: a title, a category
  from ``SCHEMA_CATEGORIES``, search keywords, and LLM- and human-facing docs.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
from vgi.metadata import FunctionExample

CATALOG_NAME = "boj"

SOURCE_URL = "https://github.com/Query-farm/vgi-boj"

# Categories group functions in docs and agent tools. Every object's
# ``vgi.category`` must name one of these.
SCHEMA_CATEGORIES = [
    {
        "name": "api",
        "title": "Central Bank Statistics",
        "description": (
            "Discover Bank of Japan statistical series and retrieve observations with original "
            "periods, units and source metadata."
        ),
    }
]


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


CATALOG_DOC_LLM = (
    "Discover Bank of Japan statistical series and retrieve observations with original "
    "periods, units and source metadata. The upstream public data service needs no API "
    "key. SQL NULL arguments are rejected; missing source values become NULL. Use "
    "function arguments for source filtering; SQL WHERE filters are local. HTTP worker "
    "access can require a bearer token."
)
CATALOG_DOC_MD = (
    "# vgi-boj\n\n"
    + CATALOG_DOC_LLM
    + (
        "\n\nQueries use bounded HTTP responses and retries. Credentials stay out of "
        "continuation state. See README.md for deployment and operational limits."
    )
)
SCHEMA_DOC_LLM = CATALOG_DOC_LLM
SCHEMA_DOC_MD = (
    "## main\n\n"
    + CATALOG_DOC_LLM
    + (
        "\n\nDiscover source metadata before querying observations. Use named arguments to "
        "select upstream data and ORDER BY for deterministic output."
    )
)
CATALOG_TAGS = {
    "vgi.title": "BOJ Statistics",
    "vgi.subtitle": (
        "Discover Bank of Japan statistical series and retrieve observations with original "
        "periods, units and source metadata."
    ),
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query.Farm contributors",
    "vgi.keywords": keywords_array("boj, economics, public data, time series, duckdb, sql"),
    "vgi.support_contact": "https://github.com/Query-farm/vgi-boj/issues",
    "vgi.support_policy_url": SOURCE_URL + "/blob/main/README.md",
    "vgi.executable_examples": json.dumps(
        [
            {
                "name": "discover_datasets",
                "description": "Discover available functions without credentials or API requests",
                "sql": (
                    "SELECT function_name FROM duckdb_functions() WHERE database_name = 'boj' AND "
                    "function_name = 'datasets' ORDER BY function_name"
                ),
                "expected_result": [{"function_name": "datasets"}],
            }
        ]
    ),
}


def function_meta(name: str, title: str, description: str, schema: pa.Schema, sql: str) -> Any:
    """Build consistent, discoverable metadata for an authenticated table function."""
    types = {
        "string": "VARCHAR",
        "int64": "BIGINT",
        "double": "DOUBLE",
        "date32[day]": "DATE",
        "bool": "BOOLEAN",
        "timestamp[us, tz=UTC]": "TIMESTAMP WITH TIME ZONE",
    }
    docs = (
        description
        + " Returns typed columns "
        + ", ".join(schema.names)
        + ". "
        + (
            "The public upstream API needs no key. Amounts retain source scales; original source strings "
            "remain available in JSON. NULL arguments are rejected. Missing optional response fields "
            "return NULL. Upstream errors fail the query; SQL WHERE filters are local."
        )
    )

    class Meta:
        name: str
        description: str
        tags: dict[str, str]
        examples: list[FunctionExample]

    Meta.name = name
    Meta.description = description
    Meta.tags = {
        **object_tags(
            title=title,
            category="api",
            keywords=f"boj, economics, time series, {name}",
            doc_llm=docs,
            doc_md=f"## {name}\n\n{docs}",
        ),
        "vgi.result_columns_schema": json.dumps(
            [
                {
                    "name": f.name,
                    "type": (
                        f"DECIMAL({f.type.precision}, {f.type.scale})"
                        if pa.types.is_decimal(f.type)
                        else types.get(str(f.type), str(f.type).upper())
                    ),
                    "description": (f.metadata or {}).get(b"comment", b"").decode(),
                }
                for f in schema
            ]
        ),
    }
    Meta.examples = [FunctionExample(sql=sql, description=title)]
    return Meta
