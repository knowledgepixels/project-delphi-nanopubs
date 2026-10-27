#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run stored SPARQL queries against the Project Delphi knowledge graph and keep their results.

Queries live in ``queries/`` as ``.rq`` files. A file starts with header lines such as
``# question: Which evidence supports claims about EGFR?``, which are carried into the results.
Placeholders follow the grlc convention used by published nanopub query templates:
``?_name`` is a required literal, ``?_name_iri`` a required IRI, and a double underscore
(``?__name``) makes one optional.

Commands::

    query.py list                                   the stored queries and their questions
    query.py run QUERY [--param NAME=VALUE ...]     run one, print the results and save them

Results are printed as a table and saved to ``results/<query>/<timestamp>.csv`` and ``.json``.
The JSON file also records the question, the exact SPARQL that ran, the parameters, the
endpoint and the state of the Query instance, so a result can be traced back later.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUERIES_DIR = HERE / "queries"
RESULTS_DIR = HERE / "results"

TEST_INSTANCE = "https://test.query.knowledgepixels.com/"
LIVE_INSTANCE = "https://query.knowledgepixels.com/"
DEFAULT_REPO = "full"
READY_STATES = {"READY", "LOADING_UPDATES"}
TIMEOUT_SECONDS = 120
MIN_READABLE_WIDTH = 16

HEADER_LINE = re.compile(r"^#\s*([A-Za-z][\w-]*)\s*:\s*(.*)$")
PLACEHOLDER = re.compile(r"\?(__?)([A-Za-z][A-Za-z0-9_]*?)(_multi)?(_iri)?\b")

PREFIXES = {
    "https://w3id.org/delphi/ontology/": "delphi:",
    "https://w3id.org/delphi/resource/": "delphir:",
    "https://w3id.org/np/": "np:",
    "https://orcid.org/": "orcid:",
    "http://purl.obolibrary.org/obo/": "obo:",
    "http://identifiers.org/": "idorg:",
    "http://www.w3.org/2004/02/skos/core#": "skos:",
    "http://purl.org/dc/terms/": "dct:",
    "http://www.w3.org/ns/prov#": "prov:",
}


class QueryError(Exception):
    """A query could not be prepared or run; the message says why."""


@dataclass
class Placeholder:
    """A grlc placeholder found in a query.

    Attributes:
        name: the parameter name users pass, e.g. ``concept`` for ``?_concept_iri``.
        variable: the variable as written in the SPARQL, e.g. ``?_concept_iri``.
        optional: whether the query runs without a value for it.
        iri: whether its value is an IRI rather than a literal.
    """

    name: str
    variable: str
    optional: bool
    iri: bool


@dataclass
class StoredQuery:
    """A ``.rq`` file with its header fields and placeholders.

    Attributes:
        path: where the file is.
        sparql: the file's full text.
        header: the ``# key: value`` lines at its top.
        placeholders: the grlc placeholders it contains, by parameter name.
    """

    path: Path
    sparql: str
    header: dict[str, str] = field(default_factory=dict)
    placeholders: dict[str, Placeholder] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Return the query's name, which is its file name without ``.rq``.

        Returns:
            the name used for the results folder.
        """
        return self.path.stem


@dataclass
class InstanceState:
    """What a Query instance reports about itself in its response headers.

    Attributes:
        status: ``Nanopub-Query-Status``, e.g. ``READY``.
        load_counter: ``Nanopub-Query-Load-Counter``.
        checksum: ``Nanopub-Query-Loaded-Nanopub-Checksum``.
        nanopub_count: ``Nanopub-Query-Loaded-Nanopub-Count``.
    """

    status: str | None
    load_counter: str | None
    checksum: str | None
    nanopub_count: str | None


@dataclass
class QueryResult:
    """The outcome of running a query.

    Attributes:
        columns: the result variables, in the order the query selects them.
        rows: one dict per solution, mapping a column to its value; unbound columns are absent.
    """

    columns: list[str]
    rows: list[dict[str, str]]


def load_query(path: Path) -> StoredQuery:
    """Read a ``.rq`` file with its header fields and placeholders.

    Args:
        path: the file to read.

    Returns:
        the parsed query.

    Raises:
        QueryError: if the file does not exist.
    """
    if not path.is_file():
        raise QueryError(f"no such query file: {path}")
    sparql = path.read_text(encoding="utf-8")
    return StoredQuery(path=path, sparql=sparql, header=parse_header(sparql),
                       placeholders=find_placeholders(sparql))


def parse_header(sparql: str) -> dict[str, str]:
    """Read the ``# key: value`` lines at the top of a query.

    Args:
        sparql: the query text.

    Returns:
        the header fields, keyed by lower-case name; reading stops at the first line that is
        not a comment.
    """
    header: dict[str, str] = {}
    for line in sparql.splitlines():
        if not line.startswith("#"):
            break
        match = HEADER_LINE.match(line)
        if match:
            header[match.group(1).lower()] = match.group(2).strip()
    return header


def find_placeholders(sparql: str) -> dict[str, Placeholder]:
    """Find the grlc placeholders in a query.

    Args:
        sparql: the query text.

    Returns:
        the placeholders by parameter name.
    """
    placeholders: dict[str, Placeholder] = {}
    for match in PLACEHOLDER.finditer(strip_comments(sparql)):
        underscores, name, multi, iri = match.groups()
        if multi:
            continue
        placeholders[name] = Placeholder(name=name, variable=match.group(0),
                                         optional=underscores == "__", iri=bool(iri))
    return placeholders


def strip_comments(sparql: str) -> str:
    """Remove whole-line comments from a query, so placeholders named in them are not counted.

    Args:
        sparql: the query text.

    Returns:
        the text without lines that start with ``#``.
    """
    return "\n".join(line for line in sparql.splitlines() if not line.lstrip().startswith("#"))


def fill_placeholders(query: StoredQuery, params: dict[str, str]) -> str:
    """Substitute parameter values into a query's placeholders.

    Args:
        query: the query to fill.
        params: values by parameter name.

    Returns:
        the SPARQL with every given placeholder replaced by an IRI or a literal.

    Raises:
        QueryError: if a required parameter is missing, a parameter is unknown, or an IRI is
            not valid.
    """
    unknown = sorted(set(params) - set(query.placeholders))
    if unknown:
        raise QueryError(f"{query.name} has no parameter {', '.join(unknown)}; "
                         f"it takes: {describe_parameters(query) or 'none'}")
    missing = sorted(name for name, placeholder in query.placeholders.items()
                     if not placeholder.optional and name not in params)
    if missing:
        raise QueryError(f"{query.name} needs --param for: {', '.join(missing)}")
    sparql = query.sparql
    for name, value in params.items():
        placeholder = query.placeholders[name]
        term = iri_term(value) if placeholder.iri else literal_term(value)
        sparql = re.sub(re.escape(placeholder.variable) + r"\b", lambda _: term, sparql)
    return sparql


def describe_parameters(query: StoredQuery) -> str:
    """Describe a query's parameters for a message.

    Args:
        query: the query.

    Returns:
        e.g. ``concept (IRI), label (literal, optional)``; empty if there are none.
    """
    parts = []
    for placeholder in query.placeholders.values():
        kind = "IRI" if placeholder.iri else "literal"
        parts.append(f"{placeholder.name} ({kind}{', optional' if placeholder.optional else ''})")
    return ", ".join(parts)


def iri_term(value: str) -> str:
    """Write a value as a SPARQL IRI.

    Args:
        value: an absolute IRI, with or without angle brackets.

    Returns:
        the IRI in angle brackets.

    Raises:
        QueryError: if the value is not an absolute IRI or contains characters an IRI can't.
    """
    iri = value.strip()
    if iri.startswith("<") and iri.endswith(">"):
        iri = iri[1:-1]
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", iri) or re.search(r'[\s<>"{}|\\^`]', iri):
        raise QueryError(f"not an absolute IRI: {value}")
    return f"<{iri}>"


def literal_term(value: str) -> str:
    """Write a value as a SPARQL string literal.

    Args:
        value: any text.

    Returns:
        the text quoted, with quotes, backslashes and line breaks escaped.
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r"))
    return f'"{escaped}"'


def check_instance(instance: str) -> InstanceState:
    """Ask a Query instance for its state, and refuse one that is still loading.

    Args:
        instance: the instance's base URL.

    Returns:
        the state it reports.

    Raises:
        QueryError: if it can't be reached or is not ready; an instance that is still loading
            answers from an incomplete store.
    """
    request = urllib.request.Request(instance, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            headers = response.headers
    except (urllib.error.URLError, TimeoutError) as error:
        raise QueryError(f"can't reach {instance}: {error}") from error
    state = InstanceState(status=headers.get("Nanopub-Query-Status"),
                          load_counter=headers.get("Nanopub-Query-Load-Counter"),
                          checksum=headers.get("Nanopub-Query-Loaded-Nanopub-Checksum"),
                          nanopub_count=headers.get("Nanopub-Query-Loaded-Nanopub-Count"))
    if state.status is not None and state.status not in READY_STATES:
        raise QueryError(f"{instance} is {state.status}, not ready; its results would be incomplete")
    return state


def run_sparql(endpoint: str, sparql: str) -> QueryResult:
    """Run a SELECT query against a SPARQL endpoint.

    Args:
        endpoint: the repository URL, e.g. ``https://query.knowledgepixels.com/repo/full``.
        sparql: the query.

    Returns:
        the result columns and rows.

    Raises:
        QueryError: if the endpoint rejects the query or does not answer in time.
    """
    body = urllib.parse.urlencode({"query": sparql}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            document = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()
        raise QueryError(f"{endpoint} rejected the query ({error.code}): {detail[:1000]}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise QueryError(f"can't run the query on {endpoint}: {error}") from error
    return parse_results(document)


def parse_results(document: dict) -> QueryResult:
    """Turn a SPARQL JSON results document into columns and rows.

    Args:
        document: the parsed ``application/sparql-results+json`` response.

    Returns:
        the result columns and rows.

    Raises:
        QueryError: if the response is not the result of a SELECT query.
    """
    if "results" not in document:
        raise QueryError("only SELECT queries are supported")
    columns = document["head"]["vars"]
    rows = [{column: binding["value"] for column, binding in solution.items()}
            for solution in document["results"]["bindings"]]
    return QueryResult(columns=columns, rows=rows)


def compact(value: str) -> str:
    """Shorten an IRI with a well-known prefix, for display.

    Args:
        value: a result value.

    Returns:
        the value with a known namespace replaced by its prefix, otherwise unchanged.
    """
    for namespace, prefix in PREFIXES.items():
        if value.startswith(namespace):
            return prefix + value[len(namespace):]
    return value


def format_results(result: QueryResult, width: int, layout: str) -> str:
    """Lay results out for the terminal, as a table or as one block per row.

    Args:
        result: the results to show.
        width: the terminal width in characters.
        layout: ``table``, ``records``, or ``auto`` for a table unless it would cut columns
            narrower than ``MIN_READABLE_WIDTH``.

    Returns:
        the formatted results.
    """
    cells = [[one_line(compact(row.get(column, ""))) for column in result.columns]
             for row in result.rows]
    widths = column_widths(result.columns, cells, width)
    if layout == "records" or (layout == "auto" and is_cramped(result.columns, cells, widths)):
        return format_records(result.columns, cells, width)
    return format_table(result.columns, cells, widths)


def is_cramped(columns: list[str], cells: list[list[str]], widths: list[int]) -> bool:
    """Tell whether fitting a table cut some column below a readable width.

    Args:
        columns: the column headings.
        cells: the table's cells, row by row.
        widths: the widths the columns were given.

    Returns:
        True if a column was narrowed below both its content and ``MIN_READABLE_WIDTH``.
    """
    natural = column_widths(columns, cells, sys.maxsize)
    return any(given < min(full, MIN_READABLE_WIDTH) for given, full in zip(widths, natural))


def format_table(columns: list[str], cells: list[list[str]], widths: list[int]) -> str:
    """Lay results out as a plain-text table.

    Args:
        columns: the column headings.
        cells: the table's cells, row by row.
        widths: the width of each column.

    Returns:
        the table, with long cells cut short and marked with an ellipsis.
    """
    lines = [join_cells(columns, widths), join_cells(["-" * w for w in widths], widths)]
    lines.extend(join_cells(row, widths) for row in cells)
    return "\n".join(lines)


def format_records(columns: list[str], cells: list[list[str]], width: int) -> str:
    """Lay results out as one block per row, a line per column, for results too wide for a table.

    Args:
        columns: the column headings.
        cells: the table's cells, row by row.
        width: the terminal width in characters.

    Returns:
        the blocks, separated by a numbered rule; empty values are left out.
    """
    label_width = max(len(column) for column in columns)
    value_width = max(width - label_width - 2, 20)
    blocks = []
    for number, row in enumerate(cells, start=1):
        lines = [f"── {number} " + "─" * max(width - len(str(number)) - 4, 0)]
        lines.extend(f"{column.ljust(label_width)}  {fit(value, value_width).rstrip()}"
                     for column, value in zip(columns, row) if value)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def one_line(value: str) -> str:
    """Collapse whitespace so a value fits on one table line.

    Args:
        value: a result value.

    Returns:
        the value with runs of whitespace replaced by single spaces.
    """
    return " ".join(value.split())


def column_widths(columns: list[str], cells: list[list[str]], width: int) -> list[int]:
    """Choose a width per column so the table fits, narrowing the widest columns first.

    Args:
        columns: the column headings.
        cells: the table's cells, row by row.
        width: the available width in characters.

    Returns:
        the width of each column.
    """
    widths = [max([len(column)] + [len(row[i]) for row in cells]) for i, column in enumerate(columns)]
    available = max(width - 2 * (len(columns) - 1), 8 * len(columns))
    while sum(widths) > available:
        widest = max(range(len(widths)), key=widths.__getitem__)
        if widths[widest] <= 8:
            break
        widths[widest] -= 1
    return widths


def join_cells(values: list[str], widths: list[int]) -> str:
    """Join one table row, padding or shortening each cell to its column's width.

    Args:
        values: the row's cells.
        widths: the width of each column.

    Returns:
        the row as one line.
    """
    return "  ".join(fit(value, w) for value, w in zip(values, widths)).rstrip()


def fit(value: str, width: int) -> str:
    """Pad or shorten a cell to a width.

    Args:
        value: the cell text.
        width: the column width.

    Returns:
        the text padded with spaces, or cut short with an ellipsis.
    """
    return value.ljust(width) if len(value) <= width else value[:width - 1] + "…"


def save_results(query: StoredQuery, sparql: str, params: dict[str, str], endpoint: str,
                 state: InstanceState, result: QueryResult, executed_at: datetime,
                 results_dir: Path) -> list[Path]:
    """Save results as CSV, and as JSON together with how they were obtained.

    Args:
        query: the stored query that ran.
        sparql: the SPARQL that ran, with parameters filled in.
        params: the parameter values given.
        endpoint: the repository URL it ran against.
        state: the Query instance's state when it ran.
        result: the results.
        executed_at: when it ran.
        results_dir: the folder that holds one subfolder of results per query.

    Returns:
        the files written.
    """
    folder = results_dir / query.name
    folder.mkdir(parents=True, exist_ok=True)
    stem = unused_stem(folder, executed_at.strftime("%Y%m%dT%H%M%SZ"))
    csv_path = folder / f"{stem}.csv"
    json_path = folder / f"{stem}.json"
    write_csv(csv_path, result)
    record = {
        "question": query.header.get("question"),
        "query_file": relative_path(query.path),
        "parameters": params,
        "endpoint": endpoint,
        "instance": {"status": state.status, "load_counter": state.load_counter,
                     "loaded_nanopubs": state.nanopub_count, "checksum": state.checksum},
        "executed_at": executed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "row_count": len(result.rows),
        "columns": result.columns,
        "rows": result.rows,
        "sparql": sparql,
    }
    json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return [csv_path, json_path]


def unused_stem(folder: Path, stem: str) -> str:
    """Pick a file name stem that no earlier result in the folder uses.

    Args:
        folder: the query's results folder.
        stem: the preferred stem, the run's timestamp.

    Returns:
        the stem, with ``-2``, ``-3``… appended if a run in the same second already used it.
    """
    candidate, number = stem, 1
    while (folder / f"{candidate}.json").exists() or (folder / f"{candidate}.csv").exists():
        number += 1
        candidate = f"{stem}-{number}"
    return candidate


def write_csv(path: Path, result: QueryResult) -> None:
    """Write results to a CSV file with full, uncompacted values.

    Args:
        path: the file to write.
        result: the results.
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=result.columns)
        writer.writeheader()
        writer.writerows(result.rows)


def relative_path(path: Path) -> str:
    """Show a path relative to the current directory when it is inside it.

    Args:
        path: any path.

    Returns:
        the relative path, or the absolute one if it is elsewhere.
    """
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path.resolve())


def resolve_query_path(name: str) -> Path:
    """Find a query by file path, or by name in ``queries/``.

    Args:
        name: a path to a ``.rq`` file, or the name of one in ``queries/`` with or without ``.rq``.

    Returns:
        the path of the query file.
    """
    given = Path(name)
    if given.is_file():
        return given
    stored = QUERIES_DIR / (name if name.endswith(".rq") else f"{name}.rq")
    return stored if stored.is_file() else given


def parse_params(pairs: list[str]) -> dict[str, str]:
    """Read ``NAME=VALUE`` parameters from the command line.

    Args:
        pairs: the ``--param`` values.

    Returns:
        the values by name.

    Raises:
        QueryError: if one has no ``=``.
    """
    params: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise QueryError(f"--param takes NAME=VALUE, not {pair}")
        params[name.strip()] = value
    return params


def choose_instance(args: argparse.Namespace) -> str:
    """Pick the Query instance from the options and environment.

    Args:
        args: the parsed command line.

    Returns:
        the instance's base URL, ending in ``/``.
    """
    if args.instance:
        instance = args.instance
    elif args.live:
        instance = LIVE_INSTANCE
    else:
        instance = os.environ.get("DELPHI_QUERY_INSTANCE", TEST_INSTANCE)
    return instance if instance.endswith("/") else instance + "/"


def command_list(args: argparse.Namespace) -> int:
    """Print the stored queries with their questions and parameters.

    Args:
        args: the parsed command line.

    Returns:
        the exit code.
    """
    paths = sorted(QUERIES_DIR.glob("*.rq"))
    if not paths:
        print(f"no queries in {relative_path(QUERIES_DIR)}")
    for path in paths:
        query = load_query(path)
        print(query.name)
        print(f"    {query.header.get('question', '(no question recorded)')}")
        parameters = describe_parameters(query)
        if parameters:
            print(f"    parameters: {parameters}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    """Run a stored query, print its results, and save them unless told not to.

    Args:
        args: the parsed command line.

    Returns:
        the exit code.
    """
    query = load_query(resolve_query_path(args.query))
    params = parse_params(args.param)
    sparql = fill_placeholders(query, params)
    instance = choose_instance(args)
    endpoint = urllib.parse.urljoin(instance, f"repo/{args.repo}")
    state = check_instance(instance)
    executed_at = datetime.now(timezone.utc).replace(microsecond=0)
    result = run_sparql(endpoint, sparql)
    if query.header.get("question"):
        print(query.header["question"])
    print(format_results(result, shutil.get_terminal_size((160, 24)).columns, args.layout))
    print(f"\n{len(result.rows)} rows from {endpoint} "
          f"({state.nanopub_count or '?'} nanopubs loaded, load {state.load_counter or '?'})")
    if not args.no_save:
        for path in save_results(query, sparql, params, endpoint, state, result, executed_at,
                                 Path(args.results_dir)):
            print(f"saved {relative_path(path)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Define the command line.

    Returns:
        the argument parser.
    """
    parser = argparse.ArgumentParser(description="Query the Project Delphi knowledge graph.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the stored queries").set_defaults(handler=command_list)
    run = commands.add_parser("run", help="run a query, print and save its results")
    run.add_argument("query", help="a .rq file, or the name of one in queries/")
    run.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                     help="a value for a placeholder, e.g. concept=https://…; repeatable")
    run.add_argument("--live", action="store_true",
                     help=f"query the live network ({LIVE_INSTANCE}) instead of the test one")
    run.add_argument("--instance", help="the Query instance's base URL; overrides --live")
    run.add_argument("--repo", default=DEFAULT_REPO, help=f"the repository to query (default {DEFAULT_REPO})")
    run.add_argument("--layout", choices=["auto", "table", "records"], default="auto",
                     help="a table, one block per row, or a table unless it is too wide (default)")
    run.add_argument("--no-save", action="store_true", help="print the results without saving them")
    run.add_argument("--results-dir", default=str(RESULTS_DIR), help="where to save results")
    run.set_defaults(handler=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: the arguments, without the program name; ``sys.argv`` when omitted.

    Returns:
        the exit code: 0 on success, 1 when a query fails.
    """
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except QueryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
