# Project Delphi nanopubs

Project Delphi builds a knowledge graph of nanopublications that links mathematics, physics and
chemistry to an EGFR→ERK subgraph in A431 cells, together with reusable cell programs
(reversible binding, activation/deactivation, saturable conversion). Records use the
[ProbKnow](https://w3id.org/spaces/probknow) namespace and will be published using a bot.

This repository is the home of the project's nanopublication work.

## Contents

| Path | Contents |
|---|---|
| [`skills/`](skills/) | Agent skills for building and using the graph |

## Agent skills

The skills follow the open [Agent Skills](https://agentskills.io) format, so they work with any
compatible agent.

| Skill | Purpose |
|---|---|
| [`delphi`](skills/delphi/SKILL.md) | What is settled (ProbKnow namespace and claim templates, external identifiers, publishing by a bot) and the scientific rules every record follows. The data model is still open: the agent proposes anything else to the user rather than inventing it, and records agreed choices in `DECISIONS.md`. |

### Requirements

- The general [nanopub skill](https://github.com/knowledgepixels/nanopub-skill), installed
  alongside these skills.
- The [nanopub-java](https://github.com/Nanopublication/nanopub-java) CLI (`np`, 1.94.0 or
  later) with Java.

### Installation

Copy or symlink each directory under `skills/` into the skills directory of your agent; see
[agentskills.io/clients](https://agentskills.io/clients) for where each client looks. Validate a
skill with [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref):

```
skills-ref validate skills/delphi
```

### Usage

Ask in plain language. Examples:

- "Add BIOMD0000000255 to the Delphi graph."
- "Record the EGF dose-response finding of Chen et al. 2009 as a Delphi claim."

Nothing is published until you explicitly ask.
