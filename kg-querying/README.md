# Querying the knowledge graph

Ask the Delphi knowledge graph a question in plain language. The agent, using the
[`delphi`](../skills/delphi/SKILL.md) skill with the `nanopub` skill behind it, writes the SPARQL
to [`queries/`](queries/), runs it against a Nanopub Query instance, prints the rows and saves
them to `results/`. The workflow is in
[`skills/delphi/references/querying.md`](../skills/delphi/references/querying.md).

| Path | Contents |
|---|---|
| [`queries/`](queries/) | Stored SPARQL queries, one per question, with the question in a `# question:` header |
| `results/<query>/` | Saved results, one CSV per run, named by its UTC time. Not committed |

Two queries support writing the others: [`schema-overview`](queries/schema-overview.rq) shows the
types and properties the Delphi records use, and [`find-concepts`](queries/find-concepts.rq) turns
a name into a concept IRI.

Parameters follow the [grlc](https://grlc.io) placeholder convention (`?_name`, `?_name_iri`), so
a stored query can be published unchanged as a query template.

## Running a query by hand

Replace each placeholder with its value, then send the query to the instance's `full` repository:

```bash
mkdir -p kg-querying/results/find-concepts
sed 's/?_text/"kinase"/g' kg-querying/queries/find-concepts.rq \
  | curl -s https://test.query.knowledgepixels.com/repo/full \
      --data-urlencode query@- -H "Accept: text/csv" \
  | tee kg-querying/results/find-concepts/$(date -u +%Y%m%dT%H%M%SZ).csv
```

Use `https://query.knowledgepixels.com/repo/full` for the live network instead of the test instance.
