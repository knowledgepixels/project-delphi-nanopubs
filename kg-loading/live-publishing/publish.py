#!/usr/bin/env python3
"""Publish Project Delphi nanopub drafts and keep track of what has been published.

Every published nanopub is recorded in a ledger (``ledger.json`` next to this script), keyed by
the resource the nanopub introduces. Publishing a draft whose record is already in the ledger
publishes a new version that ``npx:supersedes`` the current one, so nobody has to look up
which URI to supersede. Publishing a draft that has not changed since its current version does
nothing.

Commands::

    publish.py status  [PATH ...]          what each draft would do: new, changed, unchanged
    publish.py list                        what has been published, with every version
    publish.py publish PATH ... [--dry-run]
    publish.py serve   [PATH ...]          local web page with the same three things

Signing and publishing go through the nanopub-java CLI (``np``). See README.md.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DCTERMS, RDF, RDFS, XSD

NP = Namespace("http://www.nanopub.org/nschema#")
NPX = Namespace("http://purl.org/nanopub/x/")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_LEDGER = HERE / "ledger.json"
DEFAULT_ARCHIVE = HERE / "published"
UI_PAGE = HERE / "ui.html"

LIVE_QUERY = "https://query.knowledgepixels.com/"
TEST_REGISTRY = "https://test.registry.knowledgepixels.com/"
TEST_QUERY = "https://test.query.knowledgepixels.com/"
LIVE = "live"
ADMIN_GRAPH = "http://purl.org/nanopub/admin/graph"
ARTIFACT_CODE = "http://purl.org/nanopub/admin/artifactCode"
READY_QUERY_STATES = {"READY", "LOADING_UPDATES"}
LATEST_VERSION_QUERY = "RAiRsB2YywxjsBMkVRTREJBooXhf2ZOHoUs5lxciEl37I/get-latest-version-of-np"
USER_INTROS_QUERY = "RAjHh6P11QFUaoPiMRBavdAnTq4YMJW4PB85oVFSBfYjU/get-all-user-intros"
VOLATILE_PUBINFO_PREDICATES = {DCTERMS.created, NPX.supersedes}
NANOPUB_PLACEHOLDER = "~nanopub~"
JAVA_NP_INSTALL = Path.home() / ".nanopub" / "bin" / "np"


class PublishError(Exception):
    """A draft that cannot be published as asked, with the reason why."""


@dataclass(frozen=True)
class Draft:
    """An unpublished nanopub file, as far as publishing it is concerned.

    Attributes:
        path: the file.
        nanopub: the nanopub's (temporary) IRI.
        pubinfo: the IRI of its publication info graph.
        key: what identifies the record across versions: the resources it introduces, or the
            file's path in the repository when it introduces none of its own.
        label: a human-readable name for lists.
        fingerprint: a hash of its content, leaving out what changes with every version.
        signed: whether the file is already signed, and so not a draft.
    """

    path: Path
    nanopub: URIRef
    pubinfo: URIRef
    key: str
    label: str
    fingerprint: str
    signed: bool


@dataclass
class Version:
    """One published version of a record.

    Attributes:
        uri: the trusty URI it was published under.
        fingerprint: the fingerprint of the draft it was made from.
        published: when it was published, in UTC.
        source: the draft's path in the repository.
        signer: the IRI it was signed as.
        pubkey: an identifier of the public key it was signed with.
        server: the registry that accepted it.
        supersedes: the URI of the version it replaced, if any.
    """

    uri: str
    fingerprint: str
    published: str
    source: str
    signer: str
    pubkey: str
    server: str
    supersedes: str | None = None


@dataclass
class Settings:
    """How to sign and publish, and where to keep track of it.

    Attributes:
        np: the command that runs the nanopub-java CLI.
        key: the private key file to sign with; its public key is the sibling ``.pub`` file.
        signer: the IRI to sign as, e.g. the bot's IRI. Needed only to publish.
        server: the registry to publish to, or None for the live network. The test registry by
            default, so that nothing reaches the live network unless asked to.
        ledger: the ledger file.
        archive: the directory each published nanopub is kept in, as signed.
        check_head: whether to make sure, before superseding, that the ledger's current version
            is still the newest one on the network.
        check_key: whether to make sure the key is declared for the signer before publishing.
        query: the Query instance to ask which versions it has loaded, and, off the live network,
            which version is the newest. None when there is none to ask.
    """

    np: list[str] = field(default_factory=lambda: default_np())
    key: Path = Path.home() / ".nanopub" / "id_rsa"
    signer: str | None = None
    server: str | None = TEST_REGISTRY
    ledger: Path = field(default_factory=lambda: ledger_for(TEST_REGISTRY))
    archive: Path = field(default_factory=lambda: archive_for(TEST_REGISTRY))
    check_head: bool = True
    check_key: bool = True
    query: str | None = TEST_QUERY

    @property
    def live(self) -> bool:
        """Whether publishing goes to the live network."""
        return self.server is None


def default_np() -> list[str]:
    """The nanopub-java CLI as its installer sets it up, or whatever ``np`` is on the path.

    nanopub-py also installs a command called ``np``, which may come first on the path, so the
    installer's own copy is preferred.

    Returns:
        the command.
    """
    return [str(JAVA_NP_INSTALL)] if JAVA_NP_INSTALL.is_file() else ["np"]


def now_utc() -> str:
    """The current time in UTC, to the second, as an xsd:dateTime string.

    Returns:
        the timestamp, e.g. ``2026-10-26T13:21:59Z``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def graph_name(context) -> URIRef:
    """The name of a graph, as rdflib returns it from a quad.

    Args:
        context: the fourth element of a quad: a graph or its identifier, depending on the
            rdflib version.

    Returns:
        the graph's IRI.
    """
    return getattr(context, "identifier", context)


def load_dataset(path: Path) -> Dataset:
    """Parse a TriG file.

    Args:
        path: the file.

    Returns:
        the dataset.

    Raises:
        PublishError: if the file is not valid TriG.
    """
    dataset = Dataset()
    try:
        dataset.parse(str(path), format="trig")
    except Exception as ex:
        raise PublishError(f"{path}: not valid TriG: {ex}") from ex
    return dataset


def quads(dataset: Dataset):
    """Every quad of a dataset, with plain graph names.

    Args:
        dataset: the dataset.

    Yields:
        (subject, predicate, object, graph name) tuples.
    """
    for s, p, o, c in dataset.quads((None, None, None, None)):
        yield s, p, o, graph_name(c)


def find_nanopub(dataset: Dataset, path: Path) -> tuple[URIRef, URIRef]:
    """Find the nanopub a file holds, and its publication info graph.

    Args:
        dataset: the parsed file.
        path: the file, for messages.

    Returns:
        the nanopub's IRI and its pubinfo graph's IRI.

    Raises:
        PublishError: if the file does not hold exactly one nanopub with a pubinfo graph.
    """
    nanopubs = {s for s, p, o, _ in quads(dataset) if p == RDF.type and o == NP.Nanopublication}
    if len(nanopubs) != 1:
        raise PublishError(f"{path}: expected one nanopub, found {len(nanopubs)}")
    nanopub = nanopubs.pop()
    pubinfos = {o for s, p, o, _ in quads(dataset) if s == nanopub and p == NP.hasPublicationInfo}
    if len(pubinfos) != 1:
        raise PublishError(f"{path}: the nanopub has no single publication info graph")
    return nanopub, pubinfos.pop()


def is_own_iri(iri, nanopub: URIRef) -> bool:
    """Whether an IRI is the nanopub's own or one minted under it.

    Args:
        iri: the IRI.
        nanopub: the nanopub's IRI.

    Returns:
        true if the IRI is the nanopub's or lies under it.
    """
    base = str(nanopub).rstrip("/")
    value = str(iri)
    return isinstance(iri, URIRef) and (value.rstrip("/") == base or value.startswith(base + "/"))


def normalise(term, nanopub: URIRef):
    """Replace the nanopub's own IRI in a term with a placeholder, so that a draft and the
    versions made from it compare equal whatever URI they were given.

    Args:
        term: an RDF term.
        nanopub: the nanopub's IRI.

    Returns:
        the term, with the nanopub's IRI replaced.
    """
    if not is_own_iri(term, nanopub):
        return term
    rest = str(term)[len(str(nanopub).rstrip("/")):].lstrip("/")
    return URIRef(f"urn:{NANOPUB_PLACEHOLDER}/{rest}")


def fingerprint(dataset: Dataset, nanopub: URIRef, pubinfo: URIRef) -> str:
    """Hash a nanopub's content, leaving out its creation time and what it supersedes.

    Blank nodes are labelled canonically per graph, so they do not change the hash.

    Args:
        dataset: the parsed nanopub.
        nanopub: its IRI.
        pubinfo: its publication info graph's IRI.

    Returns:
        the hex SHA-256 of the sorted, normalised quads.
    """
    graphs: dict[URIRef, Graph] = {}
    for s, p, o, g in quads(dataset):
        if g == pubinfo and s == nanopub and p in VOLATILE_PUBINFO_PREDICATES:
            continue
        graph = graphs.setdefault(g, Graph())
        graph.add((normalise(s, nanopub), normalise(p, nanopub), normalise(o, nanopub)))
    lines = []
    for name, graph in graphs.items():
        label = normalise(name, nanopub).n3()
        for triple in to_canonical_graph(graph):
            lines.append(" ".join(term.n3() for term in triple) + " " + label)
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def repo_path(path: Path) -> str:
    """A file's path relative to the repository, or its absolute path if it lies outside.

    Args:
        path: the file.

    Returns:
        the path, with forward slashes.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def record_key(dataset: Dataset, nanopub: URIRef, pubinfo: URIRef, path: Path) -> str:
    """What identifies a record across its versions.

    Args:
        dataset: the parsed nanopub.
        nanopub: its IRI.
        pubinfo: its publication info graph's IRI.
        path: the file it was read from.

    Returns:
        the resources it introduces, sorted and joined by spaces; or, when it introduces
        nothing that outlives the nanopub, ``file:`` and its path in the repository.
    """
    introduced = sorted(
        str(o) for s, p, o, g in quads(dataset)
        if g == pubinfo and s == nanopub and p == NPX.introduces and not is_own_iri(o, nanopub)
    )
    return " ".join(introduced) if introduced else "file:" + repo_path(path)


def record_label(dataset: Dataset, nanopub: URIRef, path: Path) -> str:
    """A human-readable name for a nanopub.

    Args:
        dataset: the parsed nanopub.
        nanopub: its IRI.
        path: the file it was read from.

    Returns:
        the nanopub's rdfs:label, or the file name if it has none.
    """
    for s, p, o, _ in quads(dataset):
        if s == nanopub and p == RDFS.label:
            return str(o)
    return path.name


def read_draft(path: Path) -> Draft:
    """Read a nanopub file.

    Args:
        path: the file.

    Returns:
        the draft.

    Raises:
        PublishError: if the file holds no single nanopub.
    """
    dataset = load_dataset(path)
    nanopub, pubinfo = find_nanopub(dataset, path)
    signed = any(p == NPX.hasSignature for _, p, _, _ in quads(dataset))
    return Draft(
        path=path,
        nanopub=nanopub,
        pubinfo=pubinfo,
        key=record_key(dataset, nanopub, pubinfo, path),
        label=record_label(dataset, nanopub, path),
        fingerprint=fingerprint(dataset, nanopub, pubinfo),
        signed=signed,
    )


def prepare(draft: Draft, target: Path, supersedes: str | None) -> None:
    """Write the draft as it is to be signed: created now, superseding the given version.

    Args:
        draft: the draft.
        target: where to write it.
        supersedes: the URI of the version it replaces, or None for a first version.
    """
    dataset = load_dataset(draft.path)
    pubinfo = dataset.graph(draft.pubinfo)
    for predicate in VOLATILE_PUBINFO_PREDICATES:
        pubinfo.remove((draft.nanopub, predicate, None))
    pubinfo.add((draft.nanopub, DCTERMS.created, Literal(now_utc(), datatype=XSD.dateTime)))
    if supersedes:
        pubinfo.add((draft.nanopub, NPX.supersedes, URIRef(supersedes)))
    dataset.serialize(destination=str(target), format="trig")


GRAPH_ROLES = (
    ("assertion", NP.hasAssertion),
    ("provenance", NP.hasProvenance),
    ("pubinfo", NP.hasPublicationInfo),
)


def describe_term(term, dataset: Dataset, nanopub: URIRef) -> dict:
    """An RDF term as the preview shows it.

    Args:
        term: the term.
        dataset: the dataset it comes from, whose prefixes shorten IRIs.
        nanopub: the nanopub's IRI; it and the IRIs minted under it are shown as ``this:``
            and ``sub:…``, whatever prefixes the file declares.

    Returns:
        ``text``: the term in Turtle notation, with the file's prefixes; ``kind``: ``iri``,
        ``literal`` or ``bnode``; and, for an IRI, ``iri``: the full IRI.
    """
    if is_own_iri(term, nanopub):
        rest = str(term)[len(str(nanopub).rstrip("/")):].lstrip("/")
        return {"text": "sub:" + rest if rest else "this:", "kind": "iri", "iri": str(term)}
    if isinstance(term, URIRef):
        try:
            text = dataset.namespace_manager.normalizeUri(term)
        except Exception:
            text = term.n3()
        return {"text": text, "kind": "iri", "iri": str(term)}
    if isinstance(term, BNode):
        return {"text": "_:" + str(term)[:8], "kind": "bnode"}
    return {"text": term.n3(dataset.namespace_manager), "kind": "literal"}


def preview(path: Path) -> dict:
    """A nanopub file's content, triple by triple, in its named graphs.

    Args:
        path: the file.

    Returns:
        ``graphs``: the assertion, provenance and publication info graphs, in that order,
        each with its ``role``, ``name`` and sorted ``triples``; any other graph except the
        head, which only links the three, comes last with the role ``other``. ``source``: the
        file's text.

    Raises:
        PublishError: if the file holds no single nanopub.
    """
    dataset = load_dataset(path)
    nanopub, _ = find_nanopub(dataset, path)
    names = {}
    for role, predicate in GRAPH_ROLES:
        for s, p, o, _ in quads(dataset):
            if s == nanopub and p == predicate:
                names[o] = role
    for s, p, o, g in quads(dataset):
        if s == nanopub and p == RDF.type and o == NP.Nanopublication:
            names.setdefault(g, "head")
    order = ["assertion", "provenance", "pubinfo"]
    graphs: dict[URIRef, list] = {}
    for s, p, o, g in quads(dataset):
        graphs.setdefault(g, []).append((s, p, o))
    result = []
    for name, triples in graphs.items():
        rows = sorted(([describe_term(t, dataset, nanopub) for t in triple] for triple in triples),
                      key=lambda row: (row[0]["text"], row[1]["kind"] != "iri" or row[1]["iri"] != str(RDF.type),
                                       row[1]["text"], row[2]["text"]))
        role = names.get(name, "other")
        if role == "head":
            continue
        result.append({"role": role, "name": describe_term(name, dataset, nanopub)["text"],
                       "triples": [{"s": r[0], "p": r[1], "o": r[2]} for r in rows]})
    result.sort(key=lambda graph: order.index(graph["role"]) if graph["role"] in order else len(order))
    return {"graphs": result, "source": path.read_text()}


def collect_files(paths: list[Path], archive: Path) -> list[Path]:
    """The TriG files named, or found under the directories named.

    Args:
        paths: files and directories.
        archive: the archive directory, whose files are published nanopubs and never drafts.

    Returns:
        the files, sorted and without duplicates.
    """
    archive = archive.resolve()
    found = set()
    for path in paths:
        candidates = sorted(path.rglob("*.trig")) if path.is_dir() else [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if archive not in resolved.parents:
                found.add(resolved)
    return sorted(found)


def public_key(key: Path) -> str:
    """The public key that goes with a private key file, as one line of base64.

    Args:
        key: the private key file.

    Returns:
        the content of the sibling ``.pub`` file, without PEM header lines or whitespace.

    Raises:
        PublishError: if there is no public key file.
    """
    pub = key.with_name(key.name + ".pub")
    if not pub.is_file():
        raise PublishError(f"no public key next to the signing key: {pub}")
    lines = [line.strip() for line in pub.read_text().splitlines() if not line.startswith("-----")]
    return "".join(lines)


def key_id(pubkey: str) -> str:
    """A short identifier of a public key, to tell keys apart in the ledger.

    Args:
        pubkey: the public key as one line of base64.

    Returns:
        the first 16 hex digits of its SHA-256.
    """
    return hashlib.sha256(pubkey.encode("ascii")).hexdigest()[:16]


class Ledger:
    """The record of every version published, keyed by record key, kept in a JSON file."""

    def __init__(self, path: Path):
        """Load a ledger, or start an empty one if the file does not exist yet.

        Args:
            path: the ledger file.
        """
        self.path = path
        self.records: dict[str, dict] = {}
        if path.is_file():
            self.records = json.loads(path.read_text()).get("records", {})

    def versions(self, key: str) -> list[Version]:
        """Every version of a record, oldest first.

        Args:
            key: the record key.

        Returns:
            the versions, empty if the record was never published.
        """
        return [Version(**v) for v in self.records.get(key, {}).get("versions", [])]

    def current(self, key: str) -> Version | None:
        """The newest version of a record.

        Args:
            key: the record key.

        Returns:
            the version, or None if the record was never published.
        """
        versions = self.versions(key)
        return versions[-1] if versions else None

    def add(self, key: str, label: str, version: Version) -> None:
        """Record a newly published version and save the ledger.

        Args:
            key: the record key.
            label: the record's current label.
            version: the version.
        """
        record = self.records.setdefault(key, {"versions": []})
        record["label"] = label
        record["versions"].append(asdict(version))
        self.save()

    def save(self) -> None:
        """Write the ledger, replacing the file only once it is completely written."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"records": self.records}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, suffix=".tmp") as tmp:
            tmp.write(content)
        os.replace(tmp.name, self.path)


@dataclass
class Planned:
    """What publishing a file would do.

    Attributes:
        path: the file.
        state: ``new``, ``changed``, ``unchanged``, ``signed`` (not a draft), ``conflict``
            (another draft introduces the same resources) or ``unreadable``.
        draft: the draft, unless the file is unreadable.
        current: the record's current version, if it has one.
        error: why the file cannot be published, if it cannot.
    """

    path: Path
    state: str
    draft: Draft | None = None
    current: Version | None = None
    error: str | None = None

    def to_json(self) -> dict:
        """This plan as plain data, for the web page.

        Returns:
            a JSON-serialisable dictionary.
        """
        return {
            "file": repo_path(self.path),
            "path": str(self.path),
            "state": self.state,
            "key": self.draft.key if self.draft else None,
            "label": self.draft.label if self.draft else self.path.name,
            "current": asdict(self.current) if self.current else None,
            "error": self.error,
        }


class Publisher:
    """Signs and publishes drafts, superseding the versions the ledger has, and records them."""

    def __init__(self, settings: Settings, runner=subprocess.run, opener=urllib.request.urlopen):
        """Create a publisher.

        Args:
            settings: how to sign and publish.
            runner: what runs commands; replaceable for tests.
            opener: what sends HTTP requests to the Query instance; replaceable for tests.
        """
        self.settings = settings
        self.runner = runner
        self.opener = opener
        self.ledger = Ledger(settings.ledger)
        self._declared_keys: set[tuple[str, str]] | None = None

    def plan(self, paths: list[Path]) -> list[Planned]:
        """Work out what publishing each file would do.

        Args:
            paths: files and directories to look at.

        Returns:
            one plan per TriG file.
        """
        self.ledger = Ledger(self.settings.ledger)
        plans = []
        for path in collect_files(paths, self.settings.archive):
            try:
                draft = read_draft(path)
            except PublishError as ex:
                plans.append(Planned(path, "unreadable", error=str(ex)))
                continue
            current = self.ledger.current(draft.key)
            plans.append(Planned(path, self.state_of(draft, current), draft, current))
        by_key: dict[str, list[Planned]] = {}
        for plan in plans:
            if plan.draft and not plan.draft.signed:
                by_key.setdefault(plan.draft.key, []).append(plan)
        for same in by_key.values():
            if len(same) > 1:
                others = ", ".join(repo_path(p.path) for p in same)
                for plan in same:
                    plan.state = "conflict"
                    plan.error = f"these drafts introduce the same resources: {others}"
        return plans

    @staticmethod
    def state_of(draft: Draft, current: Version | None) -> str:
        """What publishing a draft would do, given its record's current version.

        Args:
            draft: the draft.
            current: the current version, or None.

        Returns:
            ``signed``, ``new``, ``unchanged`` or ``changed``.
        """
        if draft.signed:
            return "signed"
        if current is None:
            return "new"
        return "unchanged" if current.fingerprint == draft.fingerprint else "changed"

    def run(self, *args: str) -> str:
        """Run the nanopub-java CLI.

        Args:
            args: its arguments.

        Returns:
            what it wrote to standard output and standard error.

        Raises:
            PublishError: if it fails.
        """
        command = [*self.settings.np, *args]
        try:
            result = self.runner(command, capture_output=True, text=True)
        except FileNotFoundError as ex:
            raise PublishError(f"cannot run {shlex.join(self.settings.np)}: {ex}") from ex
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            if "No such command" in output:
                raise PublishError(
                    f"{shlex.join(self.settings.np)} is not the nanopub-java CLI (nanopub-py installs a command "
                    "called np too). Point --np or DELPHI_NP at nanopub-java, e.g. 'java -jar nanopub-1.95.0-jar-"
                    "with-dependencies.jar'"
                )
            raise PublishError(f"{shlex.join(command)} failed:\n{output.strip()}")
        return output

    def query(self, query_id: str, **params: str) -> list[dict[str, str]]:
        """Run a published query through the CLI, which only asks ready Query instances.

        Args:
            query_id: the query's artifact code and name.
            params: the query parameters.

        Returns:
            the result rows.
        """
        args = ["query", query_id]
        if params:
            args += ["-p", " ".join(f"{k}={v}" for k, v in params.items())]
        output = self.run(*args)
        lines = [line for line in output.splitlines() if line.startswith('"')]
        return list(csv.DictReader(io.StringIO("\n".join(lines))))

    def latest_version(self, uri: str) -> str:
        """The newest version on the network of the nanopub chain a URI belongs to.

        Args:
            uri: a version's URI.

        Returns:
            the URI of the chain's head.

        Raises:
            PublishError: if the network does not know the URI, or the chain has forked.
        """
        if self.settings.live:
            rows = self.query(LATEST_VERSION_QUERY, np=uri)
        else:
            rows = self.fetch_csv(f"api/{LATEST_VERSION_QUERY}", {"np": uri})
        heads = sorted({row["latest"] for row in rows if row.get("latest")})
        if not heads:
            raise PublishError(
                f"{self.network_name()} does not show {uri} yet. If it was published only minutes ago, wait for "
                "the Query instance to load it; otherwise check it before superseding it"
            )
        if len(heads) > 1:
            raise PublishError(f"the chain of {uri} has forked into {', '.join(heads)}; resolve that by hand first")
        return heads[0]

    def network_name(self) -> str:
        """How messages name the network being checked.

        Returns:
            ``the network``, or the Query instance's host off the live network.
        """
        return "the network" if self.settings.live else (urlparse(self.settings.query or "").hostname or "the network")

    def fetch_csv(self, path: str, params: dict[str, str]) -> list[dict[str, str]]:
        """Ask the configured Query instance for CSV, once it has said it is ready to answer.

        An instance that is still loading answers too, but from an incomplete store, so its
        answer would look like "not there" when it only means "not loaded yet".

        Args:
            path: the path under the instance's URL, e.g. ``repo/meta`` or ``api/<query>``.
            params: the query string parameters.

        Returns:
            the result rows.

        Raises:
            PublishError: if there is no instance to ask, it is not ready, or it cannot be reached.
        """
        base = self.settings.query
        if not base:
            raise PublishError("no Query instance to ask: pass --query")
        base = base.rstrip("/") + "/"
        try:
            with self.opener(urllib.request.Request(base, method="HEAD"), timeout=30) as response:
                state = response.headers.get("Nanopub-Query-Status", "")
            if state.upper() not in READY_QUERY_STATES:
                raise PublishError(f"{base} is not ready to answer (status {state or 'unknown'}); try again later")
            request = urllib.request.Request(base + path + "?" + urlencode(params), headers={"Accept": "text/csv"})
            with self.opener(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as ex:
            raise PublishError(f"cannot ask {base}: {ex}") from ex
        return list(csv.DictReader(io.StringIO(body)))

    def loaded(self, uris: list[str]) -> dict[str, bool]:
        """Which of the given versions the Query instance has loaded, looked up by artifact code
        in the admin graph of its meta repository.

        Args:
            uris: the versions' URIs.

        Returns:
            for each URI, whether the instance has it.

        Raises:
            PublishError: if the instance cannot be asked.
        """
        codes = {uri: artifact_code(uri) for uri in uris}
        found: set[str] = set()
        unique = sorted(set(codes.values()))
        for start in range(0, len(unique), 100):
            values = " ".join(json.dumps(code) for code in unique[start:start + 100])
            query = (f"select ?code where {{ graph <{ADMIN_GRAPH}> {{ ?np <{ARTIFACT_CODE}> ?code }} "
                     f"values ?code {{ {values} }} }}")
            found |= {row["code"] for row in self.fetch_csv("repo/meta", {"query": query})}
        return {uri: code in found for uri, code in codes.items()}

    def is_key_declared(self, pubkey: str, signer: str) -> bool:
        """Whether an introduction nanopub on the network declares the key for the signer.

        Args:
            pubkey: the public key as one line of base64.
            signer: the IRI the nanopub is signed as.

        Returns:
            true if some introduction declares that key for that IRI.
        """
        if self._declared_keys is None:
            rows = self.query(USER_INTROS_QUERY)
            self._declared_keys = {(row.get("user", ""), row.get("pubkey", "")) for row in rows}
        return (signer, pubkey) in self._declared_keys

    def publish(self, path: Path, dry_run: bool = False) -> dict:
        """Sign and publish one draft: a first version if its record is not in the ledger,
        otherwise a new version superseding the current one.

        Args:
            path: the draft.
            dry_run: sign it and let the CLI validate it, but publish and record nothing.

        Returns:
            what was done: ``status`` (``published``, ``superseded``, ``unchanged`` or
            ``dry-run``), ``key``, ``label``, ``uri``, ``supersedes``, and ``warnings``: the
            issues the CLI's validation found, which do not stop it from publishing.

        Raises:
            PublishError: if the draft cannot be published as it is.
        """
        settings = self.settings
        self.ledger = Ledger(settings.ledger)
        draft = read_draft(path)
        if draft.signed:
            raise PublishError(f"{repo_path(path)} is already signed; publish the draft it was made from instead")
        current = self.ledger.current(draft.key)
        if current and current.fingerprint == draft.fingerprint:
            return self.outcome("unchanged", draft, current.uri, current.supersedes, server=current.server)
        if not settings.signer:
            raise PublishError("no signer: pass --signer or set DELPHI_NP_SIGNER")
        pubkey = public_key(settings.key)
        if current:
            self.check_can_supersede(current, key_id(pubkey))
        if settings.live and settings.check_key and not self.is_key_declared(pubkey, settings.signer):
            raise PublishError(
                f"no introduction declares the key {settings.key} for {settings.signer}, so the nanopub would "
                "show as published by an unknown agent. Publish the key's introduction first"
            )
        supersedes = current.uri if current else None
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "prepared.trig"
            signed = Path(tmp) / "signed.trig"
            prepare(draft, prepared, supersedes)
            self.run("sign", "-k", str(settings.key), "-s", settings.signer, "-o", str(signed), str(prepared))
            uri = str(find_nanopub(load_dataset(signed), signed)[0])
            output = self.run("publish", *self.publish_args(dry_run), str(signed))
            warnings = self.warnings_in(output)
            if dry_run:
                return self.outcome("dry-run", draft, uri, supersedes, warnings)
            server = self.accepted_by(output)
            self.archive(signed, uri)
        self.ledger.add(draft.key, draft.label, Version(
            uri=uri,
            fingerprint=draft.fingerprint,
            published=now_utc(),
            source=repo_path(path),
            signer=settings.signer,
            pubkey=key_id(pubkey),
            server=server,
            supersedes=supersedes,
        ))
        return self.outcome("superseded" if supersedes else "published", draft, uri, supersedes, warnings, server)

    def check_can_supersede(self, current: Version, pubkey_id: str) -> None:
        """Make sure a new version can supersede the ledger's current one.

        Args:
            current: the current version.
            pubkey_id: the identifier of the key the new version would be signed with.

        Raises:
            PublishError: if the keys differ, or the network has a newer version than the ledger.
        """
        if current.pubkey != pubkey_id:
            raise PublishError(
                f"{current.uri} was signed with another key ({current.pubkey}); a version signed with this key "
                f"({pubkey_id}) would not count as superseding it"
            )
        if self.settings.check_head and (self.settings.live or self.settings.query):
            head = self.latest_version(current.uri)
            if head != current.uri:
                raise PublishError(
                    f"the ledger's current version is {current.uri}, but the newest {self.network_name()} has is "
                    f"{head}. "
                    "Pull the latest ledger, or record that version, before publishing another"
                )

    def publish_args(self, dry_run: bool) -> list[str]:
        """The options for the CLI's publish command.

        Args:
            dry_run: whether to only simulate.

        Returns:
            the options.
        """
        args = ["--dry-run"] if dry_run else []
        if self.settings.server:
            args += ["-u", self.settings.server]
        return args

    @staticmethod
    def accepted_by(output: str) -> str:
        """The registry the CLI reports having published to.

        Args:
            output: what the publish command printed.

        Returns:
            the registry URL.

        Raises:
            PublishError: if the output does not report the nanopub as published.
        """
        match = re.search(r"\b1 nanopub published at (\S+)", output)
        if not match:
            raise PublishError(f"the nanopub was not published:\n{output.strip()}")
        return match.group(1)

    @staticmethod
    def warnings_in(output: str) -> list[str]:
        """The warnings the CLI printed, one per issue.

        The CLI reports validation issues as one log line listing them all, e.g.
        ``WARN … - Verification of nanopub <uri> shows some issues: [Nanopub has no assertion
        template., …]``; those are split into one warning each.

        Args:
            output: what the CLI printed.

        Returns:
            the warnings, in the order printed.
        """
        warnings = []
        for line in output.splitlines():
            if " WARN " not in line:
                continue
            message = line.split(" - ", 1)[-1].strip()
            issues = re.search(r"shows some issues: \[(.*)\]$", message)
            if issues:
                warnings += [issue.strip() for issue in re.split(r"(?<=\.), ", issues.group(1)) if issue.strip()]
            else:
                warnings.append(message)
        return warnings

    def archive(self, signed: Path, uri: str) -> None:
        """Keep a copy of a published nanopub, named by its artifact code.

        Args:
            signed: the signed file.
            uri: its trusty URI.
        """
        self.settings.archive.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(signed, self.settings.archive / (uri.rstrip("/").rsplit("/", 1)[-1] + ".trig"))

    @staticmethod
    def outcome(status: str, draft: Draft, uri: str, supersedes: str | None,
                warnings: list[str] | None = None, server: str | None = None) -> dict:
        """Describe what publishing a draft did.

        Args:
            status: what happened.
            draft: the draft.
            uri: the URI of the version concerned.
            supersedes: the URI of the version it replaces, if any.
            warnings: the issues the CLI's validation found, if it ran.
            server: the registry that holds the version, if it is published.

        Returns:
            the description.
        """
        return {"status": status, "key": draft.key, "label": draft.label, "uri": uri, "supersedes": supersedes,
                "warnings": warnings or [], "server": server}

    def preview(self, path: Path) -> dict:
        """A draft's content, and what publishing it would add.

        Args:
            path: the draft.

        Returns:
            what :func:`preview` returns, plus ``file``, ``label``, ``state`` and ``adds``: the
            statements publishing would add to the publication info, as text.

        Raises:
            PublishError: if the file holds no single nanopub.
        """
        self.ledger = Ledger(self.settings.ledger)
        draft = read_draft(path)
        current = self.ledger.current(draft.key)
        state = self.state_of(draft, current)
        adds = []
        if state in ("new", "changed"):
            adds.append("dct:created set to the time of signing")
        if state == "changed" and current:
            adds.append(f"npx:supersedes <{current.uri}>")
        if state in ("new", "changed"):
            adds.append("the signature, and trusty URIs in place of the temporary ones")
        return {**preview(path), "file": repo_path(path), "label": draft.label, "state": state, "adds": adds}

    def published(self) -> list[dict]:
        """Every record in the ledger, with all its versions, newest record first.

        Returns:
            the records as plain data.
        """
        self.ledger = Ledger(self.settings.ledger)
        records = [
            {"key": key, "label": record.get("label", key), "versions": record.get("versions", [])}
            for key, record in self.ledger.records.items()
        ]
        return sorted(records, key=lambda r: r["versions"][-1]["published"] if r["versions"] else "", reverse=True)


def artifact_code(uri: str) -> str:
    """The artifact code of a trusty URI.

    Args:
        uri: the URI, e.g. ``https://w3id.org/np/RA…``.

    Returns:
        its last path segment.
    """
    return uri.rstrip("/").rsplit("/", 1)[-1]


def query_for(server: str | None) -> str | None:
    """The Query instance that loads what a registry holds, by the naming convention of the
    test deployments: ``test.registry.<domain>`` is paired with ``test.query.<domain>``.

    Args:
        server: the registry URL, or None for the live network.

    Returns:
        the Query instance's URL, or None if none is known for that registry.
    """
    if server is None:
        return LIVE_QUERY
    host = urlparse(server).hostname or ""
    if host.startswith("test.registry."):
        return "https://test.query." + host[len("test.registry."):] + "/"
    return None


def ledger_for(server: str | None) -> Path:
    """The default ledger for a target, so that test publications never mix with live ones.

    Args:
        server: the registry URL, or None for the live network.

    Returns:
        ``ledger.json`` for the live network, ``ledger.<host>.json`` for any other registry.
    """
    if server is None:
        return DEFAULT_LEDGER
    return HERE / f"ledger.{urlparse(server).hostname or 'server'}.json"


def archive_for(server: str | None) -> Path:
    """The default archive directory for a target.

    Args:
        server: the registry URL, or None for the live network.

    Returns:
        ``published/`` for the live network, ``published.<host>/`` for any other registry.
    """
    if server is None:
        return DEFAULT_ARCHIVE
    return HERE / f"published.{urlparse(server).hostname or 'server'}"


def target_server(live: bool, server: str | None) -> str | None:
    """The registry to publish to.

    Args:
        live: whether the live network was asked for.
        server: the registry named on the command line or in the environment, ``live`` for the
            live network, or None.

    Returns:
        None for the live network, otherwise the registry URL; the test registry when neither
        was named.

    Raises:
        PublishError: if both the live network and a registry were named.
    """
    if live and server and server != LIVE:
        raise PublishError("--live publishes to the live network, so it cannot be combined with --server")
    if live or server == LIVE:
        return None
    return server or TEST_REGISTRY


def settings_from(args: argparse.Namespace) -> Settings:
    """Build the settings from the command line, falling back to environment variables.

    Args:
        args: the parsed command line.

    Returns:
        the settings.
    """
    server = target_server(args.live, args.server or os.environ.get("DELPHI_NP_SERVER"))
    query = args.query or os.environ.get("DELPHI_NP_QUERY") or query_for(server)
    return Settings(
        np=shlex.split(args.np or os.environ["DELPHI_NP"]) if (args.np or os.environ.get("DELPHI_NP")) else default_np(),
        key=Path(args.key or os.environ.get("DELPHI_NP_KEY", str(Path.home() / ".nanopub" / "id_rsa"))).expanduser(),
        signer=args.signer or os.environ.get("DELPHI_NP_SIGNER") or None,
        server=server,
        ledger=Path(args.ledger).resolve() if args.ledger else ledger_for(server),
        archive=archive_for(server),
        check_head=not args.skip_head_check,
        check_key=not args.skip_key_check,
        query=query,
    )


def print_status(plans: list[Planned]) -> None:
    """Print one line per file: its state, its file and, where it has one, its current version.

    Args:
        plans: the plans.
    """
    for plan in plans:
        detail = plan.current.uri if plan.current else ""
        if plan.error:
            detail = plan.error
        print(f"{plan.state:<10} {repo_path(plan.path)}  {detail}".rstrip())


def print_published(records: list[dict], loaded: dict[str, bool] | None = None) -> None:
    """Print every record and its versions, newest version last.

    Args:
        records: the records.
        loaded: for each version's URI, whether the Query instance has it; None to leave that out.
    """
    for record in records:
        print(f"{record['label']}  ({record['key']})")
        for version in record["versions"]:
            mark = ""
            if loaded is not None:
                mark = "  loaded" if loaded.get(version["uri"]) else "  NOT LOADED"
            print(f"    {version['published']}  {version['uri']}{mark}")


class Handler(BaseHTTPRequestHandler):
    """Serves the web page and the JSON API it uses. Only answers on the loopback interface.

    Connections are served on threads of their own, so an idle connection a browser opens in
    advance cannot hold up the others; everything that reads or writes the ledger takes
    ``lock``, so two publishes never write it at once.
    """

    publisher: Publisher
    paths: list[Path]
    token: str
    lock = threading.Lock()

    def do_GET(self) -> None:
        """Serve the page, or one of the API's answers."""
        if self.path == "/":
            page = UI_PAGE.read_text().replace("__TOKEN__", self.token)
            self.reply(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        with self.lock:
            self.answer_api_get()

    def answer_api_get(self) -> None:
        """Answer a GET request to the API: the state, a preview, or which versions are loaded."""
        if self.path == "/api/state":
            self.reply_json(HTTPStatus.OK, self.state())
        elif self.path.startswith("/api/preview?"):
            try:
                requested = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                self.reply_json(HTTPStatus.OK, self.publisher.preview(self.known_draft(requested)))
            except PublishError as ex:
                self.reply_json(HTTPStatus.CONFLICT, {"error": str(ex)})
        elif self.path == "/api/loaded":
            try:
                uris = [v["uri"] for record in self.publisher.published() for v in record["versions"]]
                self.reply_json(HTTPStatus.OK, {"query": self.publisher.settings.query,
                                                "loaded": self.publisher.loaded(uris) if uris else {}})
            except PublishError as ex:
                self.reply_json(HTTPStatus.BAD_GATEWAY, {"error": str(ex)})
        else:
            self.reply_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        """Publish one draft, if the request comes from the page this server served."""
        if self.path != "/api/publish":
            self.reply_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not secrets.compare_digest(self.headers.get("X-Publish-Token", ""), self.token):
            self.reply_json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token; reload the page"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with self.lock:
                path = self.known_draft(body.get("path", ""))
                result = self.publisher.publish(path, dry_run=bool(body.get("dryRun")))
            self.reply_json(HTTPStatus.OK, result)
        except PublishError as ex:
            self.reply_json(HTTPStatus.CONFLICT, {"error": str(ex)})

    def known_draft(self, path: str) -> Path:
        """Accept only a file the page was shown, so the API cannot be pointed at any file.

        Args:
            path: the path the page sent.

        Returns:
            the file.

        Raises:
            PublishError: if it is not one of the files being served.
        """
        candidate = Path(path).resolve()
        if candidate not in collect_files(self.paths, self.publisher.settings.archive):
            raise PublishError(f"not a draft this page lists: {path}")
        return candidate

    def state(self) -> dict:
        """Everything the page shows.

        Returns:
            the drafts, the published records and the settings in use.
        """
        settings = self.publisher.settings
        return {
            "drafts": [plan.to_json() for plan in self.publisher.plan(self.paths)],
            "published": self.publisher.published(),
            "settings": {
                "target": settings.server or "live network",
                "live": settings.live,
                "signer": settings.signer,
                "key": str(settings.key),
                "ledger": repo_path(settings.ledger),
                "query": settings.query,
            },
        }

    def reply_json(self, status: HTTPStatus, data) -> None:
        """Send a JSON response.

        Args:
            status: the status.
            data: the body.
        """
        self.reply(status, json.dumps(data).encode("utf-8"), "application/json")

    def reply(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        """Send a response.

        Args:
            status: the status.
            body: the body.
            content_type: its media type.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(publisher: Publisher, paths: list[Path], port: int) -> None:
    """Serve the web page on localhost until interrupted.

    Args:
        publisher: the publisher the page's button uses.
        paths: the files and directories whose drafts the page lists.
        port: the port.
    """
    handler = type("BoundHandler", (Handler,), {
        "publisher": publisher,
        "paths": paths,
        "token": secrets.token_urlsafe(24),
        "lock": threading.Lock(),
    })
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Serving on http://127.0.0.1:{port}/ (publishing to {publisher.settings.server or 'the live network'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: the arguments, without the program name.

    Returns:
        the parsed arguments.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--signer", help="IRI to sign as (default: $DELPHI_NP_SIGNER)")
    common.add_argument("--key", help="private key file (default: $DELPHI_NP_KEY or ~/.nanopub/id_rsa)")
    common.add_argument("--live", action="store_true",
                        help="publish to the live nanopub network instead of the test registry")
    common.add_argument("--server", help="registry to publish to (default: $DELPHI_NP_SERVER, else "
                                         f"{TEST_REGISTRY}; 'live' means the same as --live)")
    common.add_argument("--ledger", help="ledger file (default: ledger.<host>.json, or ledger.json with --live)")
    common.add_argument("--query", help="Query instance to check against (default: $DELPHI_NP_QUERY, else the "
                                        "one paired with the registry: test.query.<domain> for "
                                        "test.registry.<domain>, query.knowledgepixels.com with --live)")
    common.add_argument("--np", help="command running the nanopub-java CLI (default: $DELPHI_NP, else "
                                     "~/.nanopub/bin/np if it exists, else np)")
    common.add_argument("--skip-head-check", action="store_true",
                        help="supersede without checking that the ledger's version is still the newest")
    common.add_argument("--skip-key-check", action="store_true",
                        help="publish without checking that the key is declared for the signer")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", parents=[common], help="what publishing each draft would do")
    status.add_argument("paths", nargs="*", type=Path, default=[REPO_ROOT])
    listing = commands.add_parser("list", parents=[common], help="what has been published")
    listing.add_argument("--check", action="store_true",
                         help="ask the Query instance which versions it has loaded")
    publish = commands.add_parser("publish", parents=[common], help="sign and publish drafts")
    publish.add_argument("paths", nargs="+", type=Path)
    publish.add_argument("--dry-run", action="store_true", help="sign and validate, but publish and record nothing")
    ui = commands.add_parser("serve", parents=[common], help="local web page to publish drafts one by one")
    ui.add_argument("paths", nargs="*", type=Path, default=[REPO_ROOT])
    ui.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: the arguments, without the program name; defaults to sys.argv.

    Returns:
        the exit status: 0 if everything asked for was done, 1 otherwise.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        publisher = Publisher(settings_from(args))
    except PublishError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    if args.command == "status":
        print_status(publisher.plan(args.paths))
    elif args.command == "list":
        records = publisher.published()
        loaded = None
        if args.check:
            try:
                loaded = publisher.loaded([v["uri"] for r in records for v in r["versions"]])
            except PublishError as ex:
                print(f"cannot check: {ex}", file=sys.stderr)
                return 1
        print_published(records, loaded)
        if loaded is not None and not all(loaded.values()):
            return 1
    elif args.command == "serve":
        serve(publisher, args.paths, args.port)
    else:
        return publish_all(publisher, args.paths, args.dry_run)
    return 0


def publish_all(publisher: Publisher, paths: list[Path], dry_run: bool) -> int:
    """Publish every draft among the files named, printing one line per file. Signed files are
    skipped; drafts that cannot be told apart from another draft are not published.

    Args:
        publisher: the publisher.
        paths: files and directories.
        dry_run: whether to only sign and validate.

    Returns:
        the exit status: 0 if every draft was published or unchanged, 1 otherwise.
    """
    failed = False
    for plan in publisher.plan(paths):
        file = repo_path(plan.path)
        if plan.state == "signed":
            print(f"{'skipped':<10} {file}  already signed, not a draft")
            continue
        if plan.error:
            print(f"{'failed':<10} {file}: {plan.error}", file=sys.stderr)
            failed = True
            continue
        try:
            result = publisher.publish(plan.path, dry_run=dry_run)
        except PublishError as ex:
            print(f"{'failed':<10} {file}: {ex}", file=sys.stderr)
            failed = True
            continue
        extra = f" (supersedes {result['supersedes']})" if result["supersedes"] else ""
        print(f"{result['status']:<10} {file}  {result['uri']}{extra}")
        for warning in result["warnings"]:
            print(f"{'':<10} warning: {warning}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
