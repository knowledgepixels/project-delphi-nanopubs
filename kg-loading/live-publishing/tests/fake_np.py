"""A stand-in for the nanopub-java CLI, so the publishing workflow can be tested offline.

It keeps its state in the JSON file named by ``FAKE_NP_STATE``:

- ``calls``: every command it was run with;
- ``published``: the URIs it has published;
- ``heads``: chain heads to report instead of the URI asked about;
- ``intros``: the (user, public key) pairs introductions declare;
- ``refuse_publish``: when true, publishing prints no success line.
"""

import hashlib
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

from rdflib import Dataset, Literal, Namespace, URIRef  # noqa: E402
from rdflib.namespace import RDF  # noqa: E402

NP = Namespace("http://www.nanopub.org/nschema#")
NPX = Namespace("http://purl.org/nanopub/x/")


def load_state() -> dict:
    """Read the fake's state.

    Returns:
        the state.
    """
    path = Path(os.environ["FAKE_NP_STATE"])
    return json.loads(path.read_text()) if path.is_file() else {}


def save_state(state: dict) -> None:
    """Write the fake's state.

    Args:
        state: the state.
    """
    Path(os.environ["FAKE_NP_STATE"]).write_text(json.dumps(state, indent=2))


def option(args: list[str], name: str) -> str:
    """The value of a command-line option.

    Args:
        args: the arguments.
        name: the option, e.g. ``-o``.

    Returns:
        its value.
    """
    return args[args.index(name) + 1]


def sign(args: list[str]) -> None:
    """Give the nanopub a trusty-looking URI derived from its content, and add a signature.

    Args:
        args: the sign command's arguments.
    """
    source, target, signer = Path(args[-1]), Path(option(args, "-o")), option(args, "-s")
    dataset = Dataset()
    dataset.parse(str(source), format="trig")
    nanopub = next(s for s, p, o, _ in dataset.quads((None, RDF.type, NP.Nanopublication, None)))
    base = str(nanopub).rstrip("/")
    code = "RAfake" + hashlib.sha256(source.read_bytes()).hexdigest()[:37]
    trusty = "https://w3id.org/np/" + code

    def rename(term):
        value = str(term)
        if isinstance(term, URIRef) and (value.rstrip("/") == base or value.startswith(base + "/")):
            rest = value[len(base):].lstrip("/")
            return URIRef(trusty + ("/" + rest if rest else ""))
        return term

    signed = Dataset()
    pubinfo = None
    for s, p, o, g in dataset.quads((None, None, None, None)):
        name = rename(getattr(g, "identifier", g))
        if p == NP.hasPublicationInfo:
            pubinfo = rename(o)
        signed.graph(name).add((rename(s), rename(p), rename(o)))
    signature = URIRef(trusty + "/sig")
    signed.graph(pubinfo).add((signature, NPX.hasSignature, Literal("fake")))
    signed.graph(pubinfo).add((signature, NPX.signedBy, URIRef(signer)))
    signed.serialize(destination=str(target), format="trig")


def publish(args: list[str], state: dict) -> None:
    """Pretend to publish, printing what the real CLI prints.

    Args:
        args: the publish command's arguments.
        state: the fake's state.
    """
    dataset = Dataset()
    dataset.parse(args[-1], format="trig")
    uri = str(next(s for s, p, o, _ in dataset.quads((None, RDF.type, NP.Nanopublication, None))))
    if "--dry-run" in args:
        print(f"Nanopub NOT published: --dry-run, np-uri={uri}")
        return
    if state.get("refuse_publish"):
        print("FAILED TO PUBLISH NANOPUBS", file=sys.stderr)
        return
    state.setdefault("published", []).append(uri)
    server = option(args, "-u") if "-u" in args else "https://registry.example.org/"
    print(f"1 nanopub published at {server}")


def query(args: list[str], state: dict) -> None:
    """Answer the two queries the workflow asks, as CSV.

    Args:
        args: the query command's arguments.
        state: the fake's state.
    """
    if args[0].endswith("/get-all-user-intros"):
        print('"user","pubkey"')
        for user, pubkey in state.get("intros", []):
            print(f'"{user}","{pubkey}"')
        return
    uri = option(args, "-p").split("=", 1)[1]
    print('"latest","date"')
    if uri in state.get("published", []):
        print(f'"{state.get("heads", {}).get(uri, uri)}","2026-10-26T00:00:00Z"')


def main() -> None:
    """Run one command."""
    command, args = sys.argv[1], sys.argv[2:]
    state = load_state()
    state.setdefault("calls", []).append([command, *args])
    if command == "sign":
        sign(args)
    elif command == "publish":
        publish(args, state)
    elif command == "query":
        query(args, state)
    save_state(state)


if __name__ == "__main__":
    main()
