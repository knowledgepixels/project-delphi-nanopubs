# Live publishing

Publishes Delphi nanopub drafts to the nanopub network and keeps track of what has been
published, so a new version of a record supersedes the right URI without anyone looking it up.

Everything is a command line tool, [`publish.py`](publish.py). The local web page
(`publish.py serve`) runs the same code: one button per draft, and a list of what's published.

## How it keeps track

[`ledger.json`](ledger.json) records every version published, keyed by the resource the
nanopub introduces (`npx:introduces`, e.g. `https://w3id.org/delphi/resource/claim/…`). A draft
that introduces nothing of its own is keyed by its file path instead.

For each draft, the tool compares the draft with the record's current version in the ledger.
The comparison leaves out `dct:created` and `npx:supersedes`.

| State | What publishing does |
|---|---|
| `new` | publishes a first version |
| `changed` | publishes a new version with `npx:supersedes` pointing at the current one |
| `unchanged` | nothing |
| `signed` | nothing: the file is already signed, so it isn't a draft; publish the draft it came from |
| `conflict` | nothing: two drafts introduce the same resources, so it can't tell which one is the record |

Before signing, the tool sets `dct:created` to the current time and adds `npx:supersedes` where
it applies, so drafts don't need either. Each published nanopub is kept, as signed, in
[`published/`](published/), named by its artifact code.

**Commit `ledger.json` and `published/` after publishing, and pull before.** The ledger is
shared state: with an old copy, the next version would supersede an old URI and fork the chain.

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

- Python 3.10 or later with `rdflib` (`pip install -r requirements.txt`)
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
| `--server` | `DELPHI_NP_SERVER` | the live network |
| `--np` | `DELPHI_NP` | `~/.nanopub/bin/np`, else `np` |
| `--ledger` | | `ledger.json`, or `ledger.<host>.json` with `--server` |

With `--server`, for example `https://test.registry.knowledgepixels.com/`, the tool uses a
separate ledger and archive (`ledger.<host>.json`, `published.<host>/`). These aren't committed,
so test publications never end up in the live ledger. The two network checks are skipped there,
because they only see the live network.

## Usage

Run from anywhere; paths can be files or directories (searched for `*.trig`). Without paths,
`status` and `serve` look through the whole repository.

```bash
export DELPHI_NP_SIGNER=https://w3id.org/np/RA…/delphi-bot
export DELPHI_NP_KEY=~/.nanopub/delphi-bot_id_rsa

python3 kg-loading/live-publishing/publish.py status kg-building/
python3 kg-loading/live-publishing/publish.py publish --dry-run kg-building/claims/
python3 kg-loading/live-publishing/publish.py publish kg-building/claims/egf-interacts-egfr.trig
python3 kg-loading/live-publishing/publish.py list
```

`--dry-run` signs and validates without publishing anything or recording it. Validation issues
that don't stop publishing (e.g. "Nanopub has no assertion template.") are listed as warnings,
after a dry run and after a real publish, in the CLI and on the page.

### Web page

```bash
python3 kg-loading/live-publishing/publish.py serve kg-building/ --port 8765
```

Then open <http://127.0.0.1:8765/>. The page lists the drafts with their state. **Publish**
(or **Publish new version**) and **Dry run** work on one draft at a time, and the page asks
for confirmation before publishing. Below, it lists every published record with its versions.

The server only listens on 127.0.0.1. It only publishes files it lists, and only on requests
carrying a token from the page it served, so another website open in the same browser can't
trigger a publish.

## Tests

```bash
cd kg-loading/live-publishing && python3 -m unittest discover -s tests
```

The tests use a fake `np` ([`tests/fake_np.py`](tests/fake_np.py)), so they don't need the
network or a key.
