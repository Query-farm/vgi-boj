"""Typed discovery and paginated observations for the Bank of Japan API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, OutputCollector, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc import ArrowSerializableDataclass

from .api import DATABASE, FREQUENCIES, codes, layer, metadata, observations, period, request
from .meta import column, function_meta
from .transport import ApiError
from .values import integer, source_json, text

DATASETS = pa.schema(
    [
        column("database", pa.string(), "Official database code accepted by the API."),
        column("name", pa.string(), "Curated database name; this catalog is not exhaustive."),
        column("source_url", pa.string(), "Official API manual with the complete database list."),
    ]
)
SERIES = pa.schema(
    [
        column("database", pa.string(), "Requested source database."),
        column("series_code", pa.string(), "Source series code; NULL for hierarchy headings."),
        column("name", pa.string(), "English series name when supplied."),
        column("name_ja", pa.string(), "Japanese series name when supplied."),
        column("unit", pa.string(), "English unit label in original source scale."),
        column("unit_ja", pa.string(), "Japanese unit label when supplied."),
        column("frequency", pa.string(), "Native frequency label; weekly subtypes remain distinct."),
        column("category", pa.string(), "English source category."),
        column("category_ja", pa.string(), "Japanese source category when supplied."),
        column("last_update", pa.date32(), "Source update date, not an observation vintage."),
        column("start_period", pa.string(), "First recorded native period from metadata."),
        column("end_period", pa.string(), "Last recorded native period from metadata."),
        column("notes", pa.string(), "Source notes when supplied."),
        *[
            column(f"layer{i}", pa.int64(), f"Source hierarchy level {i}; zero indicates an unused level.")
            for i in range(1, 6)
        ],
        column("source_timestamp", pa.string(), "Original JST response or metadata creation timestamp."),
        column("metadata_json", pa.string(), "All source series fields except observation arrays."),
    ]
)
OBSERVATIONS = pa.schema(
    [
        *SERIES,
        column("period", pa.string(), "Native survey period code; interpret using frequency without inventing a date."),
        column("value", pa.float64(), "Numeric observation in published units; NULL for source missing values."),
        column("value_text", pa.string(), "Original JSON numeric lexeme or string for exact decimal conversion."),
    ]
)


# Composed examples: each combines two of this catalog's functions in one query.
# Defined once here and attached to the function that drives the query; the
# schema-level example list is collected from the functions in worker.py.
SERIES_BY_HEADING = FunctionExample(
    description=(
        "Group FM08 foreign exchange series under their hierarchy headings from layers, with "
        "the layer selection string to pass to layer_observations for each branch"
    ),
    sql=(
        "SELECT top.name AS section, sub.name AS subsection, s.frequency, "
        "CASE WHEN sub.name IS NULL THEN s.layer1::VARCHAR ELSE s.layer1 || ',' || s.layer2 END AS layer, "
        "count(*) AS series_count "
        "FROM boj.main.series('FM08') AS s "
        "JOIN boj.main.layers('FM08') AS top ON top.layer1 = s.layer1 AND top.layer2 = 0 "
        "LEFT JOIN boj.main.layers('FM08') AS sub "
        "ON sub.layer1 = s.layer1 AND sub.layer2 = s.layer2 AND sub.layer2 <> 0 AND sub.layer3 = 0 "
        "GROUP BY ALL ORDER BY layer"
    ),
)
CALL_RATE_VS_BASIC_LOAN_RATE = FunctionExample(
    description=(
        "Compare each business day's uncollateralized overnight call rate (average, highest and "
        "lowest from FM01 layer 1) with the Basic Loan Rate from IR01 in March 2024, when the "
        "call rate turned positive, with the day's highest call rate's headroom below the Basic "
        "Loan Rate in basis points"
    ),
    sql=(
        "SELECT c.period, "
        "max(c.value) FILTER (WHERE c.series_code = 'STRDCLUCON') AS call_avg, "
        "max(c.value) FILTER (WHERE c.series_code = 'STRDCLUCONH') AS call_high, "
        "max(c.value) FILTER (WHERE c.series_code = 'STRDCLUCONL') AS call_low, "
        "any_value(b.value) AS basic_loan_rate, "
        "round((any_value(b.value) - max(c.value) FILTER (WHERE c.series_code = 'STRDCLUCONH')) * 100, 1) "
        "AS headroom_bp "
        "FROM boj.main.layer_observations('FM01', 'D', '1', start_period := '202403', end_period := '202403') AS c "
        "JOIN boj.main.observations('IR01', ['MADR1Z@D'], start_period := '202403', end_period := '202403') AS b "
        "USING (period) WHERE c.value IS NOT NULL GROUP BY c.period ORDER BY c.period"
    ),
)


@dataclass(frozen=True, kw_only=True)
class BaseArgs:
    """Output limits common to discovery and data queries."""

    max_rows: Annotated[
        int,
        Arg(
            "max_rows",
            default=0,
            ge=0,
            le=10000000,
            doc=(
                "Explicit output cap; zero retrieves all pages within the source selection. Filtering "
                "happens at the source only through function arguments."
            ),
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class DatasetArgs(BaseArgs):
    """Curated database search."""

    query: Annotated[
        str,
        Arg(
            "query",
            default="",
            doc=("Case-insensitive local substring of database code or name; empty lists all curated entries."),
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class DatabaseArgs(BaseArgs):
    """One official database and response language."""

    database: Annotated[str, Arg(0, pattern=DATABASE, doc="Official database code, for example IR01 or FM08.")]
    language: Annotated[
        str,
        Arg(
            "language",
            default="en",
            choices=("en", "jp"),
            doc="Response language: en for English or jp for Japanese labels.",
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class MetadataArgs(DatabaseArgs):
    """Local filtering of a complete metadata response."""

    query: Annotated[
        str, Arg("query", default="", doc="Case-insensitive local substring of series code or English/Japanese name.")
    ]
    include_headings: Annotated[
        bool, Arg("include_headings", default=False, doc="Include hierarchy headings that have no series code.")
    ]


@dataclass(frozen=True, kw_only=True)
class DataArgs(DatabaseArgs):
    """Native source period bounds."""

    start_period: Annotated[
        str,
        Arg(
            "start_period",
            default="",
            pattern=r"^(?:|[1-9][0-9]{3}(?:0[1-9]|1[0-2])?)$",
            doc=(
                "Inclusive native bound: YYYY, YYYYHH or YYYYQQ; monthly, daily and weekly requests "
                "use YYYYMM. Empty starts at source history."
            ),
        ),
    ]
    end_period: Annotated[
        str,
        Arg(
            "end_period",
            default="",
            pattern=r"^(?:|[1-9][0-9]{3}(?:0[1-9]|1[0-2])?)$",
            doc=("Inclusive native upper bound in the same frequency format; empty ends at latest source data."),
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class CodeArgs(DataArgs):
    """Explicit source-code selection."""

    series_codes: Annotated[
        list[str],
        Arg(
            1,
            arrow_type=pa.list_(pa.string()),
            doc=("One to 1250 distinct source codes of the same frequency, without the database prefix."),
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class LayerArgs(DataArgs):
    """Source hierarchy and frequency selection."""

    frequency: Annotated[
        str,
        Arg(
            1,
            choices=FREQUENCIES,
            doc=("Frequency abbreviation CY, FY, CH, FH, Q, M, W or D; W includes available weekly subtypes."),
        ),
    ]
    layer: Annotated[
        str,
        Arg(
            2,
            pattern=r"^(?:\*|[0-9]{1,8})(?:,(?:\*|[0-9]{1,8})){0,4}$",
            doc=(
                "One to five comma-separated layer numbers or '*' wildcards. Narrow selections to at "
                "most 1250 matching series before frequency filtering."
            ),
        ),
    ]


@dataclass(kw_only=True)
class State(ArrowSerializableDataclass):
    """Source cursor and returned series identifiers, without observations or credentials."""

    position: int = 1
    emitted: int = 0
    seen_series_json: str = "[]"
    frequency: str = ""
    done: bool = False


@init_single_worker
class BojFunction[T: BaseArgs](TableFunctionGenerator[T, State]):
    """Common validation and one source page per Arrow batch."""

    FIXED_SCHEMA: ClassVar[pa.Schema]

    @classmethod
    def on_bind(cls, params: BindParams[T]) -> BindResponse:
        """Reject invalid source selections before network access."""
        args = params.args
        if isinstance(args, DatabaseArgs) and re.fullmatch(DATABASE, args.database) is None:
            raise ApiError("Invalid BoJ database identifier")
        if isinstance(args, CodeArgs):
            codes(args.series_codes)
        if isinstance(args, LayerArgs):
            layer(args.layer)
        if isinstance(args, DataArgs):
            frequency = args.frequency if isinstance(args, LayerArgs) else ""
            period(args.start_period, frequency)
            period(args.end_period, frequency)
            if (
                args.start_period
                and args.end_period
                and (len(args.start_period) != len(args.end_period) or args.start_period > args.end_period)
            ):
                raise ApiError("start_period must not be after end_period and both must use the same format")
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[T]) -> State:
        """Start at the first source series position."""
        return State()

    @classmethod
    def fetch(cls, args: T, state: State) -> list[dict[str, Any]]:
        """Fetch one complete metadata response or one observation page."""
        raise NotImplementedError

    @classmethod
    def process(cls, params: ProcessParams[T], state: State, out: OutputCollector) -> None:
        """Emit typed rows and respect explicit output caps across continuation pages."""
        if state.done:
            out.finish()
            return
        rows = cls.fetch(params.args, state)
        if params.args.max_rows:
            rows = rows[: max(0, params.args.max_rows - state.emitted)]
        try:
            batch = pa.RecordBatch.from_pylist(rows, schema=params.output_schema)
        except (pa.ArrowException, ValueError, OverflowError):
            raise ApiError("BoJ response cannot be represented by the declared schema") from None
        state.emitted += len(rows)
        if params.args.max_rows and state.emitted >= params.args.max_rows:
            state.done = True
        out.emit(batch)


class Datasets(BojFunction[DatasetArgs]):
    """Curated official database starting points."""

    FIXED_SCHEMA = DATASETS
    Meta = function_meta(
        "datasets",
        "Discover BoJ database examples",
        (
            "Search curated database starting points without a network call. The official API "
            "manual lists all databases. Any valid official database code can be passed to series "
            "and observation functions."
        ),
        DATASETS,
        "SELECT database, name FROM boj.main.datasets(query := 'rate') ORDER BY database",
    )

    @classmethod
    def fetch(cls, args: DatasetArgs, state: State) -> list[dict[str, Any]]:
        """Return a small local catalog of official identifiers."""
        state.done = True
        entries = [
            ("IR01", "Basic Discount Rates and Basic Loan Rates"),
            ("IR02", "Deposit interest rates"),
            ("IR04", "Average contract interest rates on loans"),
            ("FM01", "Uncollateralized overnight call rate"),
            ("FM08", "Foreign exchange rates"),
            ("FM09", "Effective exchange rate"),
            ("MD01", "Monetary base"),
            ("MD02", "Money stock"),
            ("BS01", "Bank of Japan accounts"),
            ("FF", "Flow of funds"),
        ]
        return [
            {"database": code, "name": name, "source_url": "https://www.stat-search.boj.or.jp/info/api_manual_en.pdf"}
            for code, name in entries
            if args.query.casefold() in (code + " " + name).casefold()
        ]


def fetch_metadata(args: MetadataArgs, state: State, *, headings_only: bool = False) -> list[dict[str, Any]]:
    """Read metadata and filter headings and labels locally."""
    result = request("getMetadata", [("db", args.database), ("lang", args.language)])
    if result.get("DB") != args.database:
        raise ApiError("BoJ metadata refers to an unexpected database")
    rows = []
    seen: set[str] = set()
    for record in result["RESULTSET"]:
        row = metadata(record, args.database, result.get("DATE"))
        code = row["series_code"]
        if code:
            if code in seen:
                raise ApiError("BoJ metadata contains duplicate series codes")
            seen.add(code)
        if (headings_only and code) or (not headings_only and not args.include_headings and not code):
            continue
        if (
            args.query.casefold()
            not in " ".join(str(row[key] or "") for key in ("series_code", "name", "name_ja")).casefold()
        ):
            continue
        rows.append(row)
    state.done = True
    return rows


class Series(BojFunction[MetadataArgs]):
    """Discover available series and hierarchy metadata."""

    FIXED_SCHEMA = SERIES
    Meta = function_meta(
        "series",
        "Discover BoJ series metadata",
        (
            "Retrieve one database's full metadata, then optionally filter names or codes "
            "locally. English/Japanese labels, units, native period ranges, notes and layers are "
            "preserved. database is positional; language and filters are named."
        ),
        SERIES,
        "SELECT series_code, name, frequency FROM boj.main.series('IR01') ORDER BY series_code",
        composed=(SERIES_BY_HEADING,),
    )

    @classmethod
    def fetch(cls, args: MetadataArgs, state: State) -> list[dict[str, Any]]:
        """List series and optionally hierarchy headings."""
        return fetch_metadata(args, state)


class Layers(BojFunction[MetadataArgs]):
    """Hierarchy headings for constructing layer selections."""

    FIXED_SCHEMA = SERIES
    Meta = function_meta(
        "layers",
        "Discover BoJ hierarchy headings",
        (
            "Return metadata rows without series codes to discover hierarchy headings and layer "
            "numbers. include_headings has no effect because this function always returns "
            "headings only. database is positional; query and language are named."
        ),
        SERIES,
        "SELECT name, layer1, layer2 FROM boj.main.layers('IR01') ORDER BY layer1, layer2, name",
    )

    @classmethod
    def fetch(cls, args: MetadataArgs, state: State) -> list[dict[str, Any]]:
        """List only source hierarchy headings."""
        return fetch_metadata(args, state, headings_only=True)


def fetch_data(args: CodeArgs | LayerArgs, state: State) -> list[dict[str, Any]]:
    """Follow source NEXTPOSITION, rejecting stalled, repeated or inconsistent pages."""
    query = [("db", args.database), ("lang", args.language), ("startPosition", str(state.position))]
    for key, value in (("startDate", args.start_period), ("endDate", args.end_period)):
        if value:
            query.append((key, value))
    if isinstance(args, CodeArgs):
        endpoint = "getDataCode"
        query.append(("code", ",".join(codes(args.series_codes))))
    else:
        endpoint = "getDataLayer"
        query.extend([("frequency", args.frequency), ("layer", args.layer)])
    result = request(endpoint, query)
    parameters = result.get("PARAMETER")
    if not isinstance(parameters, dict) or parameters.get("DB") != args.database:
        raise ApiError("BoJ data refers to an unexpected database")
    returned_position = integer(parameters.get("STARTPOSITION"), optional=True)
    if returned_position is not None and returned_position != state.position:
        raise ApiError("BoJ returned a different pagination position")
    records = result["RESULTSET"]
    seen = set(json.loads(state.seen_series_json))
    for record in records:
        code = text(record.get("SERIES_CODE"), required=True)
        if code in seen:
            raise ApiError("BoJ pagination repeated a series")
        if isinstance(args, CodeArgs) and code not in codes(args.series_codes):
            raise ApiError("BoJ returned an unexpected series code")
        seen.add(code)
        frequency = text(record.get("FREQUENCY"), required=True)
        assert frequency is not None
        if isinstance(args, CodeArgs):
            if state.frequency and state.frequency != frequency:
                raise ApiError("BoJ returned mixed frequencies for a code selection")
            state.frequency = frequency
    if len(records) > 250 or len(seen) > 1250:
        raise ApiError("BoJ response exceeds its documented series limits")
    next_position = integer(result.get("NEXTPOSITION"), optional=True)
    if next_position is not None:
        if not records or not state.position < next_position <= 1250:
            raise ApiError("BoJ pagination stalled or returned an invalid next position")
        state.position = next_position
    else:
        state.done = True
    state.seen_series_json = source_json(sorted(seen))
    return observations(records, args.database, result.get("DATE"))


class Observations(BojFunction[CodeArgs]):
    """Observations selected by database and series codes."""

    FIXED_SCHEMA = OBSERVATIONS
    Meta = function_meta(
        "observations",
        "Read BoJ observations by code",
        (
            "Retrieve selected same-frequency series and automatically follow NEXTPOSITION until "
            "complete or max_rows is reached. Preserve missing values, exact numeric text and "
            "native periods. The source supplies current data, not historical vintages. database "
            "and series_codes are positional; bounds and controls are named."
        ),
        OBSERVATIONS,
        (
            "SELECT period, value, unit FROM boj.main.observations('IR01', ['MADR1Z@D'], "
            "start_period := '202401', end_period := '202401') ORDER BY period"
        ),
    )

    @classmethod
    def fetch(cls, args: CodeArgs, state: State) -> list[dict[str, Any]]:
        """Fetch the next source-selected code page."""
        return fetch_data(args, state)


class LayerObservations(BojFunction[LayerArgs]):
    """Observations selected by hierarchy layers and frequency."""

    FIXED_SCHEMA = OBSERVATIONS
    Meta = function_meta(
        "layer_observations",
        "Read BoJ observations by hierarchy",
        (
            "Retrieve series matching layer numbers and frequency, following source pagination. "
            "The API rejects more than 1250 layer matches before frequency filtering; narrow the "
            "hierarchy when necessary. W includes available weekly subtypes. database, frequency "
            "and layer are positional; bounds and controls are named."
        ),
        OBSERVATIONS,
        (
            "SELECT series_code, period, value FROM boj.main.layer_observations('IR01', 'D', "
            "'1,1', start_period := '202401', end_period := '202401') ORDER BY series_code, period"
        ),
        composed=(CALL_RATE_VS_BASIC_LOAN_RATE,),
    )

    @classmethod
    def fetch(cls, args: LayerArgs, state: State) -> list[dict[str, Any]]:
        """Fetch the next source-selected hierarchy page."""
        return fetch_data(args, state)


FUNCTIONS: list[type[Any]] = [Datasets, Series, Layers, Observations, LayerObservations]
