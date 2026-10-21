# Project Delphi nanopubs

An [Agent Skill](https://agentskills.io) for building and using the Project Delphi knowledge
graph as nanopublications: core sciences linked to an EGFR→ERK subgraph in A431 cells, plus
reusable cell programs (reversible binding, activation/deactivation, saturable conversion).
Records use the [ProbKnow](https://w3id.org/spaces/probknow) namespace.

The skill follows the open Agent Skills format, so it works with any compatible agent. Its
instructions are plain prose and shell commands; no host-specific tools are required.

## What it does

One skill, [`delphi`](skills/delphi/SKILL.md), holding what is settled (ProbKnow namespace and
claim templates, external identifiers, publishing by a bot) and the scientific rules every
record follows. The data model is still open: the skill tells the agent to propose anything
else to the user rather than invent it, and to record agreed choices in `DECISIONS.md`.

## Requirements

- The general [nanopub skill](https://github.com/knowledgepixels/nanopub-skill), installed
  alongside this one.
- The [nanopub-java](https://github.com/Nanopublication/nanopub-java) CLI (`np`, 1.94.0 or
  later) with Java.

## Installation

Copy or symlink `skills/delphi` into the skills directory of your agent; see
[agentskills.io/clients](https://agentskills.io/clients) for where each client looks. Validate it
with [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref):

```
skills-ref validate skills/delphi
```

## Usage

Ask in plain language. Examples:

- "Add BIOMD0000000255 to the Delphi graph."
- "Record the EGF dose-response finding of Chen et al. 2009 as a Delphi claim."

Nanopubs will be published using a bot. Nothing is published until you explicitly ask.
