# Querying the Delphi graph in natural language

The user asks a question in plain language; you answer it from the graph with SPARQL, never from
memory. The repository's [`kg-querying/`](../../../kg-querying/README.md) holds the stored queries
and `query.py`, which runs them, prints the results and saves them.

## Workflow

1. **Reuse first.** Run `uv run kg-querying/query.py list`. If a stored query answers the
   question, run it with the right parameters and skip to step 5.
2. **Resolve names to IRIs.** A question names things ("EGFR", "erlotinib"); queries take IRIs.
   Run `find-concepts --param text=<name>`. If there are several matches, show them and ask
   which one is meant; if there are none, say so rather than guessing an IRI.
3. **Check the data model.** Before writing a new query, run `schema-overview --no-save` and
   build on the types and properties it shows, not on what the ontology or this guide suggests
   might be there.
4. **Write the query** to `kg-querying/queries/<kebab-case-name>.rq`, named for what it
   answers, not for the user's wording. Start it with:
   ```sparql
   # question: <the question, phrased generally: "a given concept", not "EGFR">
   # parameters: <name> = <what it takes, with an example>
   # notes: <anything a reader must know: which versions it counts, what it leaves out>
   ```
   Make the specific thing in the question a parameter (`?_concept_iri`, `?_text`) so the
   query serves the next question of the same shape. Show the user the SPARQL.
5. **Run it** with `uv run kg-querying/query.py run <name> --param …`. It queries the test
   instance unless the user asks for the live network (`--live`). Results are saved by default;
   pass `--no-save` for exploratory runs (such as steps 2 and 3).
6. **Answer** the question in a few sentences from the rows, then give the row count, the
   instance (test or live) and the saved file paths. Say plainly when there are no rows, and
   what that does and does not mean (absent from the graph, not false).

A query that errors or times out is fixed and rerun; don't fall back to answering from memory.
Nothing is published: a stored query becomes a published query template only when the user
asks, through the nanopub skill.

## Data model as published

Check it against `schema-overview`; the graph mixes records written for ontology v2.0 with
earlier ones.

| What | Shape |
|---|---|
| Claim | `delphi:Claim` with `rdfs:label`, `delphi:context`, `delphi:hasEvidence`; a relation between concepts is reified with `rdf:subject`, `rdf:predicate` (e.g. `obo:RO_0002436` molecularly interacts with, `obo:RO_0002408` directly inhibits) and `rdf:object` |
| Concept | `delphi:Concept` or plain `skos:Concept`, with `rdfs:label`, `dct:description`, `skos:exactMatch` to the external identifier (`http://identifiers.org/ensembl/…`, `…/chembl.compound/…`) |
| Evidence | `delphi:EvidenceItem` with `rdfs:label`, `dct:description`, `dct:source` (database names as literals, or source IRIs), sometimes `prov:wasDerivedFrom` |
| Assessment | `delphi:LLMClaimAssessment` with `delphi:hasScore`, `delphi:hasVerdict`, `delphi:assessmentType`, `delphi:hasEvidence`, `dct:subject` |
| Source | `prov:wasDerivedFrom` in the nanopub's provenance graph; older claims use `delphi:derivedFromPaper` in the assertion |
| Agent | `prov:wasAttributedTo`; older assessments use `delphi:assessedBy` to a `delphi:Model` |

Deprecated terms stay in published records, so a query that reads the graph must accept both
forms, e.g. `?claim prov:wasDerivedFrom|delphi:derivedFromPaper ?source`, and both concept
types (`VALUES ?type { delphi:Concept skos:Concept }`).

## Writing SPARQL for the Query instance

The instance stores each nanopub's graphs as named graphs and has no inference.

- **Match inside the assertion graph**: `GRAPH ?assertion { … }`, joined to its nanopub with
  `GRAPH npa:graph { ?np np:hasAssertion ?assertion }` (`npa:` = `http://purl.org/nanopub/admin/`).
  Patterns outside a `GRAPH` block see the union of all graphs, which mixes assertions with
  provenance and publication info.
- **Records are linked by IRI, not by nanopub.** A claim and its evidence are separate nanopubs:
  match each in its own `GRAPH` block and join on the shared IRI.
- **Count only current versions.** The admin graph has `npx:introduces`, `dct:created`,
  `npx:invalidates` and `npa:hasValidSignatureForPublicKeyHash` for each nanopub. Keep a
  nanopub when no nanopub signed with the same key invalidates it, and no newer one with the
  same key introduces the same resource; `evidence-for-concept.rq` shows the pattern. Without
  it, a record with two versions appears twice.
- **Put `OPTIONAL` outside `GRAPH`** when the optional part may be in another graph:
  `GRAPH ?g { OPTIONAL { … } }` drops the whole row when the optional part doesn't match.
- **Literals from parameters are untyped strings**: compare with `str()`, `lcase()` and
  `contains()`, and cast for dates (`xsd:dateTime(?_since)`).
- **Return labels next to IRIs** (`?concept ?concept_label`) and the nanopub URI (`?np`), so
  every row can be read and traced.
- Order the rows, and aggregate repeated values with `group_concat(distinct …)` rather than
  returning one row per value.
