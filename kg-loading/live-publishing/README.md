# Live publishing

Publishes Delphi nanopub drafts to the nanopub network and keeps track of what has been
published, so a new version of a record supersedes the right URI without anyone looking it up.

Everything is a command line tool, [`publish.py`](publish.py). The local web page
(`publish.py serve`) runs the same code: one button per draft, and a list of what's published.

**By default it publishes to the test network**: the test registry
`https://test.registry.knowledgepixels.com/`, checked against the test Query instance
`https://test.query.knowledgepixels.com/`. Nothing reaches the live network unless you pass
`--live`, and the page then says so in red.

## How it keeps track

[`ledger.json`](ledger.json) records every version published to the live network, keyed by the resource the
nanopub introduces (`npx:introduces`, e.g. `https://w3id.org/delphi/resource/claim/…`). A draft
that introduces nothing of its own is keyed by its file path instead.

For each draft, the tool compares the draft with the record's current version in the ledger.
The comparison leaves out `dct:created` and `npx:supersedes`.

| State | What publishing does |
|---|---|
| `new` | publishes a first version |
| `changed` | publishes a new version with `npx:supersedes` pointing at the current one |
| `unchanged` | nothing: the draft is identical to the version already published, ignoring `dct:created` and the signature |
| `signed` | a signed file not published yet: publishes it exactly as signed, if it is the record's first version or supersedes its current one |
| `published` | nothing: a signed file that is already published (it is in the ledger); listed under Published, not Drafts |
| `conflict` | nothing: two drafts introduce the same resources, so it can't tell which one is the record |

Before signing, the tool sets `dct:created` to the current time and adds `npx:supersedes` where
it applies, so drafts don't need either. Each published nanopub is kept, as signed, in
[`published/`](published/), named by its artifact code.

**Commit `ledger.json` and `published/` after publishing, and pull before.** The ledger is
shared state: with an old copy, the next version would supersede an old URI and fork the chain.

### Nanopubs published without the tool

Signed files can already be on the registry, published by hand before the tool was used.
`sync` looks each signed file up on the registry (`<registry>/np/<artifact code>`). If the
registry holds it and the ledger doesn't, `sync` records it as a published version, using the
file's `dct:created` as its publication time and its signer and key. The fingerprint leaves
out the signature, so the draft a signed file was made from then shows as `unchanged`, and the
next changed version supersedes it. The server runs `sync` when it starts, and the page's
**Find published** button runs it again.

Run `sync` before publishing drafts that may already be out there. Otherwise a draft is
published as `new`, next to the existing nanopub and without superseding it.

### Checks before publishing

- **The signing key is declared** for the signer by an introduction nanopub. Otherwise the
  nanopub shows as published by an unknown agent. The tool looks this up once per run, and
  the lookup takes up to a minute.
- **The current version is still the newest on the network**, before superseding it. This
  uses `get-latest-version-of-np` through `np query`, which only asks Query instances that
  are ready. If the network shows a newer version, the ledger is out of date and nothing is
  published. Right after a publish the network may not show the new version yet; wait a few
  minutes, or pass `--skip-head-check` if you're sure.
- **The same key signs every version.** A version signed with another key does not count as
  superseding the previous one.

## Requirements

- [uv](https://docs.astral.sh/uv/). `publish.py` declares its Python version (3.10 or later) and
  its dependency (`rdflib`) inline, so `uv run` sets up both on first use.
- The [nanopub-java](https://github.com/Nanopublication/nanopub-java) CLI, 1.95.0 or later.
  By default the tool runs `~/.nanopub/bin/np` if it exists, else `np`. nanopub-py also
  installs a command called `np`, so if that one comes first on your path, set `DELPHI_NP`
  (e.g. `DELPHI_NP="java -jar /path/to/nanopub-1.95.0-jar-with-dependencies.jar"`).
- A key pair and the IRI to sign as. Delphi nanopubs are published by a bot, so that is the
  bot's key and the bot's IRI (see the nanopub skill on setting up a bot identity).

## Configuration

Each setting is a command-line option, or an environment variable so it doesn't have to be
repeated:

| Option | Environment | Default |
|---|---|---|
| `--signer` | `DELPHI_NP_SIGNER` | none, required to publish |
| `--key` | `DELPHI_NP_KEY` | `~/.nanopub/id_rsa` (its public key is `<key>.pub`) |
| `--live` | `DELPHI_NP_SERVER=live` | off: publish to the test registry |
| `--server` | `DELPHI_NP_SERVER` | `https://test.registry.knowledgepixels.com/` |
| `--query` | `DELPHI_NP_QUERY` | `test.query.<domain>` for `test.registry.<domain>`; `https://query.knowledgepixels.com/` with `--live` |
| `--np` | `DELPHI_NP` | `~/.nanopub/bin/np`, else `np` |
| `--ledger` | | `ledger.<host>.json`; `ledger.json` with `--live` |

On the test registry (the default) or any other `--server`, the tool uses a
separate ledger and archive (`ledger.<host>.json`, `published.<host>/`). These aren't committed,
so test publications never end up in the live ledger. The tool then checks against the Query
instance paired with that registry (`https://test.query.knowledgepixels.com/` for the one
above): before superseding, it asks that instance for the newest version. The declared-key
check is skipped, because only the live network knows the key introductions. A registry with
no known Query instance gets neither check unless you pass `--query`.

### Is it published?

`list --check` (and the **Published** list on the page) asks the Query instance which
versions it has loaded. It looks up their artifact codes (`npa:artifactCode` in the admin
graph of `/repo/meta`) in one query. A version shows as *not loaded yet* for a few seconds to
a minute after publishing, until the instance has loaded it. `list --check` exits with 1 while
any version is missing. The tool only asks an instance that reports itself ready
(`Nanopub-Query-Status`), since one that is still loading answers from an incomplete store.

## Usage

Run from anywhere; paths can be files or directories (searched for `*.trig`). Without paths,
`status` and `serve` look through the whole repository.

```bash
export DELPHI_NP_SIGNER=https://w3id.org/np/RA…/delphi-bot
export DELPHI_NP_KEY=~/.nanopub/delphi-bot_id_rsa

uv run kg-loading/live-publishing/publish.py status kg-building/
uv run kg-loading/live-publishing/publish.py publish --dry-run kg-building/claims/
uv run kg-loading/live-publishing/publish.py publish kg-building/claims/egf-interacts-egfr.trig
uv run kg-loading/live-publishing/publish.py sync examples/
uv run kg-loading/live-publishing/publish.py list --check
```

These go to the test network. For the live network, add `--live`:

```bash
uv run kg-loading/live-publishing/publish.py publish --live kg-building/claims/
```

`--dry-run` signs and validates without publishing anything or recording it. Validation issues
that don't stop publishing (e.g. "Nanopub has no assertion template.") are listed as warnings,
after a dry run and after a real publish, in the CLI and on the page.

### Web page

```bash
uv run kg-loading/live-publishing/publish.py serve kg-building/ --port 8765
```

Then open <http://127.0.0.1:8765/>. The page lists the drafts with their state. **Preview** opens a draft's triples, grouped into assertion, provenance and publication info (with the file's own prefixes, `this:`/`sub:` for the nanopub's own IRIs, and a toggle for the TriG source), and says what publishing would add; `#preview=<file>` opens one directly. **Publish**
(or **Publish new version**) and **Dry run** work on one draft at a time, and the page asks
for confirmation before publishing. Below, it lists every published record with its versions.

The server only listens on 127.0.0.1. It only publishes files it lists, and only on requests
carrying a token from the page it served, so another website open in the same browser can't
trigger a publish.

## Tests

```bash
cd kg-loading/live-publishing && uv run --with "rdflib>=6" python -m unittest discover -s tests
```

The tests use a fake `np` ([`tests/fake_np.py`](tests/fake_np.py)), so they don't need the
network or a key.
