---
name: delphi
description: "Build and use the Project Delphi knowledge graph as nanopublications — core-science concepts linked to an EGFR→ERK subgraph in A431 cells, with reusable cell programs. Use for anything Delphi: adding a source or model to the graph, recording claims and their evidence, defining vocabulary or templates, defining or verifying programs, querying the graph, or preparing a release."
license: MIT
compatibility: Requires the nanopub skill, the nanopub-java CLI (np) with Java, and network access to fetch sources.
metadata:
  author: knowledgepixels
  version: "0.2.0"
---

# Project Delphi

Delphi is a knowledge graph in which every record is a nanopublication. It connects
mathematics, physics and chemistry to a narrow cell biology domain, keeps source evidence, and
stores reusable mathematical programs that can be applied in a specific cellular context.

Nanopub mechanics come from the `nanopub` skill; load it too.

## Settled

- One record per nanopub.
- Terms come from the [Project Delphi ontology](https://github.com/fractastical/project-delphi-ontology)
  v2.0 ([`delphi.ttl`](https://github.com/fractastical/project-delphi-ontology/blob/main/delphi.ttl)):
  prefix `delphi:` = `https://w3id.org/delphi/ontology/`. Individuals are `delphir:<kind>/<id>`
  under `https://w3id.org/delphi/resource/`. Existing Delphi terms and templates are reused first.
  The legacy `https://w3id.org/probknow/...` IRIs are never used in new records.
- Claims are `delphi:Claim` (a subclass of `delphi:Hypothesis`), created with the Project Delphi
  claim template, with free-text context in `delphi:context`. Their evidence is
  `delphi:EvidenceItem`, linked with `delphi:hasEvidence` and created with the Project Delphi
  evidence item template. Referenced entities such as genes or drugs are `delphi:Concept`.
- Deprecated terms are not used in new records: `delphi:Paper` (use the source's
  `https://doi.org/` IRI, typed `fabio:ScholarlyWork` if a type is needed), `delphi:Model` (an
  assessing `prov:SoftwareAgent` with `delphi:usesModel` to a `delphi:LanguageModel`),
  `delphi:assessedBy` (use `prov:wasAttributedTo`) and `delphi:derivedFromPaper` (use
  `prov:wasDerivedFrom`).
- External identifiers (Cellosaurus, BioModels, UniProt, GO, ChEBI, SBO, ECO, QUDT) are used
  unchanged. A matching label never makes two things the same.
- Nanopubs will be published using a bot. **Nothing is published** unless the user explicitly
  asks.

## Not settled

Anything else — new classes or properties, templates, identifiers, file layout — is open. Reuse
what already exists; otherwise propose the choice to the user and wait. Record agreed choices in
`DECISIONS.md` in the working directory and follow them afterwards.

## Rules

1. Keep observations, model assertions, assumptions, derived claims and simulation results
   distinct. A model reaction or parameter is not a measurement.
2. Every claim keeps its source, its location in the source, the kind of evidence and its
   context, or says explicitly that the context is unknown.
3. Unknown stays unknown: never invent values, units or provenance. Set uncertain items aside
   with a reason.
4. A synthetic run shows a program is executable, nothing more.
5. Acceptance comes from checks that run (`np check`, template conformance, queries, numerical
   tests), never from the agent's own judgment.
