# Querying the knowledge graph

Ask the Delphi knowledge graph a question in plain language. The agent, using the
[`delphi`](../skills/delphi/SKILL.md) skill with the nanopub skill behind it, writes the SPARQL,
stores it here as a `.rq` file, runs it with [`query.py`](query.py), shows the results in the
terminal and saves them.

```
you:    Which drugs inhibit EGFR, and what is the evidence?
agent:  finds the concept IRI for EGFR (find-concepts)
        reuses or writes queries/<name>.rq, with your question in its header
        runs:  uv run kg-querying/query.py run <name> --param concept=…
        prints the results and where it saved them
```

The agent first reuses a stored query when one answers the question, and writes a new one only
when none does. Every query and every result says which question it answers.

## Layout

| Path | Contents |
|---|---|
| [`queries/`](queries/) | Stored SPARQL queries, one per question, with the question in their header |
| `results/<query>/` | Saved results, one CSV and one JSON per run, named by the run's UTC time. Not committed (see below) |
| [`query.py`](query.py) | Runs a stored query, prints the results and saves them |
| [`tests/`](tests/) | Offline tests for `query.py` |

## Stored queries

A query file starts with header lines, then the SPARQL:

```sparql
# question: Which claims involve a given concept, and what evidence supports each of them?
# parameters: concept = the concept IRI, e.g. https://w3id.org/delphi/resource/concept/ENSG00000146648 (EGFR)
# notes: shows the current version of each claim and evidence item

PREFIX …
SELECT … WHERE { … ?_concept_iri … }
```

Parameters use the [grlc](https://grlc.io) placeholder convention of published nanopub query
templates, so a stored query can later be published as one unchanged: `?_name` is a required
literal, `?_name_iri` a required IRI, and `?__name` (two underscores) an optional one.

| Query | Question |
|---|---|
| [`schema-overview`](queries/schema-overview.rq) | Which types and properties the Delphi records use, with example values. Run it before writing a new query |
| [`find-concepts`](queries/find-concepts.rq) | Which concepts match a name, e.g. `EGFR`; turns a name into the IRI other queries take |
| [`evidence-for-concept`](queries/evidence-for-concept.rq) | Which claims involve a concept, and the evidence for each |

## Running queries

Requires [uv](https://docs.astral.sh/uv/); `query.py` uses only the standard library.

```bash
uv run kg-querying/query.py list
uv run kg-querying/query.py run find-concepts --param text=EGFR
uv run kg-querying/query.py run evidence-for-concept \
    --param concept=https://w3id.org/delphi/resource/concept/ENSG00000146648
uv run kg-querying/query.py run kg-querying/queries/schema-overview.rq --no-save
```

A query is named by its file, or by its name in `queries/`.

| Option | Default |
|---|---|
| `--param NAME=VALUE` | none; repeat for each parameter |
| `--live` | off: query the test instance `https://test.query.knowledgepixels.com/` (or `DELPHI_QUERY_INSTANCE`) |
| `--instance URL` | any other Query instance; overrides `--live` |
| `--repo` | `full`; any repository of the instance, e.g. `meta` or `text` |
| `--layout` | `auto`: a table, or one block per row when a table would be too narrow to read |
| `--no-save` | off: results are saved |

The test instance holds what was published to the test registry; `--live` queries the live
network. The script refuses an instance that reports itself still loading, since it would
answer from an incomplete store.

## Saved results

Each run writes `results/<query>/<UTC time>.csv` with the full values, and a `.json` file with
the same rows plus how they were obtained: the question, the query file, the parameters, the
exact SPARQL that ran, the endpoint, and the Query instance's load counter and checksum. The
results of a run can be traced back to the state of the graph they came from.

`results/` is not committed: results go stale as the graph grows, and anyone can produce them
again from the stored query. To share a result, add it explicitly (`git add -f
kg-querying/results/<query>/<file>`).

## Tests

```bash
cd kg-querying && uv run python -m unittest discover -s tests
```

They run against a local stand-in for a Query instance, so they don't need the network.
