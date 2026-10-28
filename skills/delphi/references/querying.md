# Querying the Delphi graph in natural language

The user asks a question in plain language; you answer it from the graph with SPARQL, never from
memory. Queries run as ad-hoc SPARQL against a Nanopub Query repository, as described in the
`nanopub` skill. Stored queries live in `kg-querying/queries/`, saved results in
`kg-querying/results/`.

## Workflow

1. **Reuse first.** Read the `# question:` headers in `kg-querying/queries/`. If a stored query
   answers the question, run it and skip to step 5.
2. **Resolve names to IRIs** with `find-concepts.rq` (`?_text` = the name). If there are several
   matches, ask which one is meant; if there are none, say so rather than guessing an IRI.
3. **Check the data model** with `schema-overview.rq`, and build on the types and properties it
   returns, not on what the ontology suggests might be there.
4. **Write the query** to `kg-querying/queries/<kebab-case-name>.rq`, named for what it answers,
   starting with:
   ```sparql
   # question: <the question, phrased generally: "a given concept", not a specific one>
   # parameters: <name> = <what it takes>
   # notes: <which versions it counts, what it leaves out>
   ```
   Make the specific thing in the question a grlc placeholder (`?_concept_iri`, `?_text`) so the
   query serves the next question of the same shape. Show the user the SPARQL.
5. **Run it.** Substitute each placeholder (an IRI as `<…>`, a literal as `"…"`), send it to the
   test instance unless the user asks for the live one, and save the rows:
   ```bash
   mkdir -p kg-querying/results/<name>
   sed 's|?_concept_iri|<IRI>|g' kg-querying/queries/<name>.rq \
     | curl -s https://test.query.knowledgepixels.com/repo/full \
         --data-urlencode query@- -H "Accept: text/csv" \
     | tee kg-querying/results/<name>/$(date -u +%Y%m%dT%H%M%SZ).csv
   ```
   Live: `https://query.knowledgepixels.com/repo/full`. Skip `tee` for exploratory runs (steps 2
   and 3).
6. **Answer** from the rows in a few sentences, with the row count, the instance and the saved
   file. With no rows, say the graph holds nothing on it, not that it is false.

A query that errors or times out is fixed and rerun. Nothing is published: a stored query becomes
a query template only when the user asks, through the `nanopub` skill.

## Writing SPARQL for the Query instance

The instance stores each nanopub's graphs as named graphs and has no inference.

- **Match inside the assertion graph**: `GRAPH ?assertion { … }`, joined with
  `GRAPH npa:graph { ?np np:hasAssertion ?assertion }` (`npa:` = `http://purl.org/nanopub/admin/`).
- **Records are linked by IRI, not by nanopub**: match a claim and its evidence in separate
  `GRAPH` blocks and join on the shared IRI.
- **Count only current versions**: in `npa:graph`, keep a nanopub when no nanopub with the same
  `npa:hasValidSignatureForPublicKeyHash` `npx:invalidates` it, and no newer one (`dct:created`)
  with the same key `npx:introduces` the same resource.
- **Accept both old and v2.0 terms**: published records keep deprecated terms, e.g.
  `prov:wasDerivedFrom|delphi:derivedFromPaper`, `VALUES ?type { delphi:Concept skos:Concept }`.
- **Put `OPTIONAL` outside `GRAPH`** when the optional part may sit in another graph.
- **Return labels next to IRIs, and the nanopub URI**, so every row can be read and traced.
