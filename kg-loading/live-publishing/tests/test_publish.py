"""Tests of the publishing workflow, against a fake nanopub-java CLI and drafts written on the fly."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
import re
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import publish  # noqa: E402
from publish import Handler, PublishError, Publisher, Settings  # noqa: E402

SIGNER = "https://w3id.org/np/RAbot/delphi-bot"
PUBKEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtest"
CLAIM = "https://w3id.org/delphi/resource/claim/egf-interacts-egfr"

DRAFT = """@prefix this: <http://purl.org/nanopub/temp/np001/> .
@prefix sub: <http://purl.org/nanopub/temp/np001/> .
@prefix np: <http://www.nanopub.org/nschema#> .
@prefix npx: <http://purl.org/nanopub/x/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix delphi: <https://w3id.org/delphi/ontology/> .

sub:Head {
  this: a np:Nanopublication ;
    np:hasAssertion sub:assertion ;
    np:hasProvenance sub:provenance ;
    np:hasPublicationInfo sub:pubinfo .
}

sub:assertion {
  <CLAIM> a delphi:Claim ;
    rdfs:label "LABEL" .
}

sub:provenance {
  sub:assertion prov:wasAttributedTo <https://orcid.org/0000-0000-0000-0000> .
}

sub:pubinfo {
  this: dct:created "CREATED"^^xsd:dateTime ;
    rdfs:label "LABEL" ;
    npx:introduces <CLAIM> .
}
"""


class FakeQuery(BaseHTTPRequestHandler):
    """A stand-in for a Query instance and its registry, answering from the fake CLI's state: it
    has loaded what the fake CLI published, minus the URIs listed under ``not_loaded``; as a
    registry, it holds what the fake CLI published plus the URIs listed under ``on_registry``."""

    state_file: Path

    def state(self) -> dict:
        """The fake CLI's state.

        Returns:
            the state.
        """
        return json.loads(self.state_file.read_text())

    def do_HEAD(self) -> None:
        """Report the instance's status, READY unless the state says otherwise."""
        self.send_response(200)
        self.send_header("Nanopub-Query-Status", self.state().get("query_status", "READY"))
        self.end_headers()

    def do_GET(self) -> None:
        """Answer the meta repository's artifact-code lookup and the latest-version query."""
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        state = self.state()
        self.state_file.write_text(json.dumps({**state, "query_requests": state.get("query_requests", []) + [url.path]}))
        loaded = [uri for uri in state.get("published", []) if uri not in state.get("not_loaded", [])]
        if url.path.startswith("/np/"):
            code = url.path[len("/np/"):]
            held = state.get("published", []) + state.get("on_registry", [])
            self.send_response(200 if any(u.endswith("/" + code) for u in held) else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path == "/repo/meta":
            asked = re.findall(r'"([^"]+)"', params["query"])
            body = "code\n" + "".join(code + "\n" for code in asked if any(u.endswith("/" + code) for u in loaded))
        elif url.path.endswith("/get-latest-version-of-np"):
            uri = params["np"]
            body = "latest,date\n" + (f"{state.get('heads', {}).get(uri, uri)},2026-10-26T00:00:00Z\n" if uri in loaded else "")
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        """Keep the test output quiet."""


class Workspace:
    """A temporary directory with drafts, a key pair, a ledger, the fake CLI's state and a fake
    Query instance."""

    def __init__(self, root: Path):
        """Set up the directory.

        Args:
            root: the temporary directory.
        """
        self.root = root
        self.drafts = root / "drafts"
        self.drafts.mkdir()
        self.state_file = root / "fake-np-state.json"
        self.key = root / "id_rsa"
        self.key.write_text("private")
        self.key.with_name("id_rsa.pub").write_text(PUBKEY + "\n")
        self.save_state({"intros": [[SIGNER, PUBKEY]]})
        handler = type("BoundFakeQuery", (FakeQuery,), {"state_file": self.state_file})
        self.query_server = HTTPServer(("127.0.0.1", 0), handler)
        self.query_url = f"http://127.0.0.1:{self.query_server.server_address[1]}/"
        threading.Thread(target=self.query_server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop the fake Query instance."""
        self.query_server.shutdown()
        self.query_server.server_close()

    def draft(self, name: str, label: str = "EGF interacts with EGFR", created: str = "2026-10-23T13:21:59Z",
              claim: str = CLAIM) -> Path:
        """Write a draft.

        Args:
            name: its file name.
            label: its label.
            created: its creation time.
            claim: the claim it introduces.

        Returns:
            the file.
        """
        path = self.drafts / name
        path.write_text(DRAFT.replace("LABEL", label).replace("CREATED", created).replace("CLAIM", claim))
        return path

    def state(self) -> dict:
        """The fake CLI's state.

        Returns:
            the state.
        """
        return json.loads(self.state_file.read_text())

    def save_state(self, state: dict) -> None:
        """Replace the fake CLI's state.

        Args:
            state: the state.
        """
        self.state_file.write_text(json.dumps(state))

    def calls(self, command: str) -> list[list[str]]:
        """The fake CLI's calls of one command.

        Args:
            command: the command, e.g. ``sign``.

        Returns:
            the calls.
        """
        return [call for call in self.state().get("calls", []) if call[0] == command]

    def publisher(self, **overrides) -> Publisher:
        """A publisher that uses the fake CLI and this workspace.

        Args:
            overrides: settings to change.

        Returns:
            the publisher.
        """
        settings = Settings(
            np=[sys.executable, str(HERE / "fake_np.py")],
            key=self.key,
            signer=SIGNER,
            ledger=self.root / "ledger.json",
            archive=self.root / "published",
            server=None,
            query=self.query_url,
        )
        for name, value in overrides.items():
            setattr(settings, name, value)
        return Publisher(settings, runner=self.run)

    def run(self, command, **kwargs):
        """Run a command with the fake CLI's state file in its environment.

        Args:
            command: the command.
            kwargs: passed on to subprocess.run.

        Returns:
            the completed process.
        """
        return subprocess.run(command, env={**os.environ, "FAKE_NP_STATE": str(self.state_file),
                                            "PYTHONWARNINGS": "ignore"}, **kwargs)


class PublishTest(unittest.TestCase):
    """Publishing drafts, and what the ledger keeps of it."""

    def setUp(self):
        """Create a fresh workspace."""
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))

    def tearDown(self):
        """Remove the workspace."""
        self.ws.close()
        self.tmp.cleanup()

    def test_a_new_draft_is_published_and_recorded(self):
        """A draft whose record is not in the ledger is published as a first version."""
        path = self.ws.draft("claim.trig")
        publisher = self.ws.publisher()

        result = publisher.publish(path)

        self.assertEqual("published", result["status"])
        self.assertEqual(CLAIM, result["key"])
        self.assertIsNone(result["supersedes"])
        versions = publisher.ledger.versions(CLAIM)
        self.assertEqual([result["uri"]], [v.uri for v in versions])
        self.assertEqual("https://registry.example.org/", versions[0].server)
        self.assertEqual("https://registry.example.org/", result["server"])
        self.assertEqual(SIGNER, versions[0].signer)
        self.assertTrue((self.ws.root / "published" / (result["uri"].rsplit("/", 1)[-1] + ".trig")).is_file())
        self.assertEqual([result["uri"]], self.ws.state()["published"])

    def test_an_unchanged_draft_is_not_published_again(self):
        """Publishing a draft again, even with a new creation time, does nothing."""
        publisher = self.ws.publisher()
        first = publisher.publish(self.ws.draft("claim.trig"))

        again = publisher.publish(self.ws.draft("claim.trig", created="2026-10-25T09:00:00Z"))

        self.assertEqual("unchanged", again["status"])
        self.assertEqual(first["uri"], again["uri"])
        self.assertEqual(1, len(self.ws.calls("sign")))
        self.assertEqual(1, len(publisher.ledger.versions(CLAIM)))

    def test_a_changed_draft_supersedes_the_current_version(self):
        """A changed draft is published as a new version superseding the ledger's current one."""
        publisher = self.ws.publisher()
        first = publisher.publish(self.ws.draft("claim.trig"))

        second = publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

        self.assertEqual("superseded", second["status"])
        self.assertEqual(first["uri"], second["supersedes"])
        self.assertEqual([first["uri"], second["uri"]], [v.uri for v in publisher.ledger.versions(CLAIM)])
        archived = (self.ws.root / "published" / (second["uri"].rsplit("/", 1)[-1] + ".trig")).read_text()
        self.assertIn(first["uri"], archived)
        self.assertIn("supersedes", archived)

    def test_a_third_version_supersedes_the_second(self):
        """Each new version supersedes the one before it, not the first."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("claim.trig"))
        second = publisher.publish(self.ws.draft("claim.trig", label="v2"))

        third = publisher.publish(self.ws.draft("claim.trig", label="v3"))

        self.assertEqual(second["uri"], third["supersedes"])

    def test_the_creation_time_is_refreshed_before_signing(self):
        """The signed nanopub carries the time it was signed, not the draft's."""
        result = self.ws.publisher().publish(self.ws.draft("claim.trig", created="2020-01-01T00:00:00Z"))

        archived = (self.ws.root / "published" / (result["uri"].rsplit("/", 1)[-1] + ".trig")).read_text()
        self.assertNotIn("2020-01-01", archived)

    def test_a_stale_ledger_is_not_superseded(self):
        """If the network has a newer version than the ledger, nothing is published."""
        publisher = self.ws.publisher()
        first = publisher.publish(self.ws.draft("claim.trig"))
        state = self.ws.state()
        state["heads"] = {first["uri"]: "https://w3id.org/np/RAsomeoneelse"}
        self.ws.save_state(state)

        with self.assertRaisesRegex(PublishError, "RAsomeoneelse"):
            publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))
        self.assertEqual(1, len(publisher.ledger.versions(CLAIM)))
        self.assertEqual(1, len(self.ws.calls("publish")))

    def test_a_version_the_network_cannot_see_is_not_superseded(self):
        """If no Query instance shows the current version, superseding it waits."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("claim.trig"))
        state = self.ws.state()
        state["published"] = []
        self.ws.save_state(state)

        with self.assertRaisesRegex(PublishError, "does not show"):
            publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

    def test_the_head_check_can_be_skipped(self):
        """With the head check off, a version the network cannot see is superseded anyway."""
        publisher = self.ws.publisher(check_head=False)
        first = publisher.publish(self.ws.draft("claim.trig"))
        self.ws.save_state({**self.ws.state(), "published": []})

        second = publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

        self.assertEqual(first["uri"], second["supersedes"])

    def test_an_undeclared_key_is_refused(self):
        """A key no introduction declares for the signer is not used."""
        self.ws.save_state({"intros": [["https://orcid.org/someone-else", PUBKEY]]})

        with self.assertRaisesRegex(PublishError, "no introduction declares"):
            self.ws.publisher().publish(self.ws.draft("claim.trig"))
        self.assertEqual([], self.ws.calls("sign"))

    def test_another_key_cannot_supersede(self):
        """A version signed with a different key would not count as superseding, so it is refused."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("claim.trig"))
        self.ws.key.with_name("id_rsa.pub").write_text("ANOTHERKEY\n")
        self.ws.save_state({**self.ws.state(), "intros": [[SIGNER, "ANOTHERKEY"]]})

        with self.assertRaisesRegex(PublishError, "another key"):
            publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

    def test_a_dry_run_records_nothing(self):
        """A dry run signs and validates, but leaves the ledger and archive alone."""
        publisher = self.ws.publisher()

        result = publisher.publish(self.ws.draft("claim.trig"), dry_run=True)

        self.assertEqual("dry-run", result["status"])
        self.assertTrue(result["uri"].startswith("https://w3id.org/np/RAfake"))
        self.assertFalse((self.ws.root / "ledger.json").exists())
        self.assertFalse((self.ws.root / "published").exists())
        self.assertIn("--dry-run", self.ws.calls("publish")[0])

    def test_a_dry_run_reports_the_validation_issues(self):
        """The issues the CLI's validation finds come back one by one with the dry run."""
        self.ws.save_state({**self.ws.state(), "issues": ["Nanopub has no assertion template.",
                                                          "Nanopub has no provenance template."]})

        result = self.ws.publisher().publish(self.ws.draft("claim.trig"), dry_run=True)

        self.assertEqual(["Nanopub has no assertion template.", "Nanopub has no provenance template."],
                         result["warnings"])

    def test_a_publish_reports_the_validation_issues_too(self):
        """A real publish also returns the issues, though they did not stop it."""
        self.ws.save_state({**self.ws.state(), "issues": ["Nanopub has no assertion template."]})

        result = self.ws.publisher().publish(self.ws.draft("claim.trig"))

        self.assertEqual("published", result["status"])
        self.assertEqual(["Nanopub has no assertion template."], result["warnings"])

    def test_a_clean_dry_run_has_no_warnings(self):
        """Without issues the warnings are empty."""
        result = self.ws.publisher().publish(self.ws.draft("claim.trig"), dry_run=True)

        self.assertEqual([], result["warnings"])

    def test_warnings_are_read_from_the_cli_output(self):
        """The CLI's own output, as nanopub-java 1.94.0 prints it, is split into its issues."""
        output = (
            "[main] WARN org.nanopub.extra.server.PublishNanopub - Verification of nanopub "
            "https://w3id.org/np/RAUUk03ERPPfWF0DSGW69DMjBn_16y6nTEjvZjPo4oVHw shows some issues: "
            "[Nanopub creation time is older than one hour., Nanopub has no assertion template., "
            "Nanopub has no provenance template.]\n"
            "[main] INFO org.nanopub.extra.server.NanopubServerUtils - Using 3 registry instance(s)\n"
            "[main] WARN org.example.Other - Something else, worth knowing\n"
            "Nanopub NOT published: --dry-run, np-uri=https://w3id.org/np/RAUUk03E\n"
        )

        self.assertEqual(["Nanopub creation time is older than one hour.", "Nanopub has no assertion template.",
                          "Nanopub has no provenance template.", "Something else, worth knowing"],
                         Publisher.warnings_in(output))

    def test_a_publish_the_cli_does_not_confirm_is_not_recorded(self):
        """If the CLI does not report the nanopub as published, the ledger is left alone."""
        self.ws.save_state({**self.ws.state(), "refuse_publish": True})
        publisher = self.ws.publisher()

        with self.assertRaisesRegex(PublishError, "not published"):
            publisher.publish(self.ws.draft("claim.trig"))
        self.assertEqual([], publisher.ledger.versions(CLAIM))

    def test_a_test_server_has_its_own_ledger_and_query_instance(self):
        """Test publications keep a separate ledger, and are checked against the test Query instance."""
        server = "https://test.registry.knowledgepixels.com/"
        self.assertEqual("ledger.test.registry.knowledgepixels.com.json", publish.ledger_for(server).name)
        self.assertEqual("ledger.json", publish.ledger_for(None).name)
        self.assertEqual("https://test.query.knowledgepixels.com/", publish.query_for(server))
        self.assertEqual(publish.LIVE_QUERY, publish.query_for(None))
        self.assertIsNone(publish.query_for("http://localhost:9292/"))

    def test_without_flags_the_test_pair_is_used(self):
        """Nothing reaches the live network unless asked: by default the tool publishes to the test
        registry and checks against the test Query instance."""
        for name in ("DELPHI_NP_SERVER", "DELPHI_NP_QUERY"):
            os.environ.pop(name, None)

        settings = publish.settings_from(publish.parse_args(["status"]))

        self.assertEqual("https://test.registry.knowledgepixels.com/", settings.server)
        self.assertEqual("https://test.query.knowledgepixels.com/", settings.query)
        self.assertEqual("ledger.test.registry.knowledgepixels.com.json", settings.ledger.name)
        self.assertFalse(settings.live)
        self.assertEqual("https://test.registry.knowledgepixels.com/", Settings().server)

    def test_live_must_be_asked_for(self):
        """--live, or the server named 'live', publishes to the live network with the live ledger."""
        os.environ.pop("DELPHI_NP_QUERY", None)
        for argv in (["status", "--live"], ["status", "--server", "live"]):
            settings = publish.settings_from(publish.parse_args(argv))

            self.assertTrue(settings.live, argv)
            self.assertEqual("https://query.knowledgepixels.com/", settings.query)
            self.assertEqual("ledger.json", settings.ledger.name)

    def test_live_and_another_registry_cannot_both_be_asked_for(self):
        """Asking for the live network and a registry at once is refused rather than guessed."""
        self.assertEqual(2, publish.main(["status", "--live", "--server", "https://registry.example.org/"]))

    def test_off_the_live_network_the_query_instance_decides_the_newest_version(self):
        """Superseding on a test registry asks its Query instance, not the live network, and skips
        the key check, which only the live network can answer."""
        self.ws.save_state({"intros": []})
        publisher = self.ws.publisher(server="https://test.registry.knowledgepixels.com/")
        first = publisher.publish(self.ws.draft("claim.trig"))
        self.ws.save_state({**self.ws.state(), "heads": {first["uri"]: "https://w3id.org/np/RAnewerontest"}})

        with self.assertRaisesRegex(PublishError, "RAnewerontest"):
            publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))
        self.assertEqual([], self.ws.calls("query"))
        self.assertIn("/api/" + publish.LATEST_VERSION_QUERY, self.ws.state()["query_requests"])
        self.assertEqual(["-u", "https://test.registry.knowledgepixels.com/"], self.ws.calls("publish")[0][1:3])

    def test_off_the_live_network_a_version_the_query_instance_lacks_is_not_superseded(self):
        """If the test Query instance has not loaded the current version, superseding it waits."""
        publisher = self.ws.publisher(server="https://test.registry.knowledgepixels.com/")
        first = publisher.publish(self.ws.draft("claim.trig"))
        self.ws.save_state({**self.ws.state(), "not_loaded": [first["uri"]]})

        with self.assertRaisesRegex(PublishError, "127.0.0.1 does not show"):
            publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

    def test_without_a_query_instance_the_head_check_is_skipped(self):
        """A registry with no known Query instance can only be superseded without the head check."""
        publisher = self.ws.publisher(server="http://localhost:9292/", query=None)
        first = publisher.publish(self.ws.draft("claim.trig"))

        second = publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

        self.assertEqual(first["uri"], second["supersedes"])

    def test_loaded_versions_are_found_by_artifact_code(self):
        """The Query instance is asked, by artifact code, which versions it has loaded."""
        publisher = self.ws.publisher()
        first = publisher.publish(self.ws.draft("claim.trig"))
        second = publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))
        self.ws.save_state({**self.ws.state(), "not_loaded": [second["uri"]]})

        loaded = publisher.loaded([first["uri"], second["uri"], "https://w3id.org/np/RAneverpublished"])

        self.assertEqual({first["uri"]: True, second["uri"]: False, "https://w3id.org/np/RAneverpublished": False},
                         loaded)
        self.assertIn("/repo/meta", self.ws.state()["query_requests"])

    def test_a_query_instance_still_loading_is_not_trusted(self):
        """An instance that is not ready would answer from an incomplete store, so it is not asked."""
        self.ws.save_state({**self.ws.state(), "query_status": "LOADING_INITIAL"})

        with self.assertRaisesRegex(PublishError, "not ready"):
            self.ws.publisher().loaded(["https://w3id.org/np/RAanything"])

    def test_an_unreachable_query_instance_is_reported(self):
        """A Query instance that cannot be reached gives a clear error."""
        with self.assertRaisesRegex(PublishError, "cannot ask"):
            self.ws.publisher(query="http://127.0.0.1:9/").loaded(["https://w3id.org/np/RAanything"])

    def test_a_published_signed_file_is_listed_as_published(self):
        """A signed copy of a recorded version is published already, so publishing it does nothing."""
        publisher = self.ws.publisher()
        first = publisher.publish(self.ws.draft("claim.trig"))
        signed = next((self.ws.root / "published").iterdir())
        copy = self.ws.drafts / "signed.trig"
        copy.write_text(signed.read_text())

        result = publisher.publish(copy)

        self.assertEqual("unchanged", result["status"])
        self.assertEqual(first["uri"], result["uri"])
        self.assertEqual(1, len(self.ws.calls("publish")))
        states = {plan.path.name: plan.state for plan in publisher.plan([self.ws.drafts])}
        self.assertEqual("published", states["signed.trig"])
        self.assertEqual("unchanged", states["claim.trig"])

    def test_a_signed_file_not_published_yet_is_published_as_it_is(self):
        """A signed file the ledger does not know is published without signing it again."""
        signed = self.signed_elsewhere()
        uri = publish.read_signed(signed).uri
        publisher = self.ws.publisher()
        self.assertEqual("signed", {p.path.name: p.state for p in publisher.plan([self.ws.drafts])}[signed.name])
        signs_before = len(self.ws.calls("sign"))

        result = publisher.publish(signed)

        self.assertEqual("published", result["status"])
        self.assertEqual(uri, result["uri"])
        self.assertEqual(signs_before, len(self.ws.calls("sign")))
        self.assertEqual(str(signed), self.ws.calls("publish")[0][-1])
        version = publisher.ledger.current(CLAIM)
        self.assertEqual(uri, version.uri)
        self.assertEqual(SIGNER, version.signer)
        self.assertEqual("published", {p.path.name: p.state for p in publisher.plan([self.ws.drafts])}[signed.name])

    def test_a_signed_file_beside_the_current_version_is_refused(self):
        """A signed file that does not supersede the record's current version would fork it."""
        publisher = self.ws.publisher()
        current = publisher.publish(self.ws.draft("claim.trig"))
        signed = self.signed_elsewhere(label="another wording")

        with self.assertRaisesRegex(PublishError, "does not supersede the current version"):
            publisher.publish(signed)
        self.assertEqual([current["uri"]], [v.uri for v in publisher.ledger.versions(CLAIM)])

    def test_a_signed_file_superseding_the_current_version_is_published(self):
        """A signed file made as the next version is published and becomes the current one."""
        publisher = self.ws.publisher()
        current = publisher.publish(self.ws.draft("claim.trig"))
        draft = self.ws.root / "hand" / "next.trig"
        draft.parent.mkdir(exist_ok=True)
        draft.write_text(DRAFT.replace("LABEL", "EGF binds EGFR").replace("CREATED", "2026-10-26T00:00:00Z")
                         .replace("CLAIM", CLAIM).replace("npx:introduces <" + CLAIM + "> .",
                                                          "npx:introduces <" + CLAIM + "> ;\n    npx:supersedes <"
                                                          + current["uri"] + "> ."))
        signed = self.ws.drafts / "signed-next.trig"
        self.ws.run([sys.executable, str(HERE / "fake_np.py"), "sign", "-k", str(self.ws.key), "-s", SIGNER,
                     "-o", str(signed), str(draft)], capture_output=True, check=True)

        result = publisher.publish(signed)

        self.assertEqual("superseded", result["status"])
        self.assertEqual(current["uri"], result["supersedes"])
        self.assertEqual(result["uri"], publisher.ledger.current(CLAIM).uri)

    def test_two_drafts_of_one_record_conflict(self):
        """Two drafts introducing the same claim cannot both be published."""
        self.ws.draft("one.trig")
        self.ws.draft("two.trig", label="another wording")

        plans = self.ws.publisher().plan([self.ws.drafts])

        self.assertEqual({"conflict"}, {plan.state for plan in plans})
        self.assertIn("one.trig", plans[0].error)

    def test_the_plan_tells_new_changed_and_unchanged_apart(self):
        """Status shows what publishing each draft would do."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("published.trig"))
        publisher.publish(self.ws.draft("edited.trig", claim=CLAIM + "-2"))
        self.ws.draft("edited.trig", claim=CLAIM + "-2", label="edited")
        self.ws.draft("fresh.trig", claim=CLAIM + "-3")
        (self.ws.drafts / "broken.trig").write_text("not trig")

        states = {plan.path.name: plan.state for plan in publisher.plan([self.ws.drafts])}

        self.assertEqual({"published.trig": "unchanged", "edited.trig": "changed", "fresh.trig": "new",
                          "broken.trig": "unreadable"}, states)

    def test_the_archive_is_never_read_as_drafts(self):
        """Published copies kept in the archive are not offered for publishing."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("claim.trig"))

        plans = publisher.plan([self.ws.root])

        self.assertEqual(["claim.trig"], [plan.path.name for plan in plans])

    def test_a_draft_introducing_only_its_own_parts_is_keyed_by_its_file(self):
        """Without an introduced resource of its own, the file's path identifies the record."""
        path = self.ws.draft("own.trig", claim="http://purl.org/nanopub/temp/np001/thing")

        draft = publish.read_draft(path)

        self.assertTrue(draft.key.startswith("file:"))
        self.assertTrue(draft.key.endswith("own.trig"))

    def test_published_lists_every_version(self):
        """The published list has each record with all its versions."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("claim.trig"))
        publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

        records = publisher.published()

        self.assertEqual(1, len(records))
        self.assertEqual("EGF binds EGFR", records[0]["label"])
        self.assertEqual(2, len(records[0]["versions"]))

    def test_the_preview_groups_the_triples_by_graph(self):
        """The preview lists assertion, provenance and publication info, leaving out the head,
        with the nanopub's own IRIs shown as this: and sub:."""
        preview = publish.preview(self.ws.draft("claim.trig"))

        self.assertEqual(["assertion", "provenance", "pubinfo"], [g["role"] for g in preview["graphs"]])
        assertion = preview["graphs"][0]
        self.assertEqual("sub:assertion", assertion["name"])
        self.assertEqual(["rdf:type", "rdfs:label"], [t["p"]["text"] for t in assertion["triples"]])
        self.assertEqual(CLAIM, assertion["triples"][0]["s"]["iri"])
        self.assertEqual("literal", assertion["triples"][1]["o"]["kind"])
        self.assertEqual({"this:"}, {t["s"]["text"] for t in preview["graphs"][2]["triples"]})
        self.assertIn("sub:pubinfo", preview["source"])

    def test_the_preview_says_what_publishing_adds(self):
        """For a changed draft, the preview names the version the new one would supersede."""
        publisher = self.ws.publisher()
        first = publisher.publish(self.ws.draft("claim.trig"))

        changed = publisher.preview(self.ws.draft("claim.trig", label="EGF binds EGFR"))
        unchanged = publisher.preview(self.ws.draft("claim.trig"))

        self.assertEqual("changed", changed["state"])
        self.assertIn(f"npx:supersedes <{first['uri']}>", changed["adds"])
        self.assertEqual([], unchanged["adds"])

    def signed_elsewhere(self, label: str = "EGF interacts with EGFR", created: str = "2026-10-20T10:00:00Z") -> Path:
        """Sign a draft by hand, as if it had been published without this tool.

        Args:
            label: the draft's label.
            created: its creation time.

        Returns:
            the signed file, in a ``signed`` directory next to the drafts.
        """
        draft = self.ws.root / "hand" / "claim.trig"
        draft.parent.mkdir(exist_ok=True)
        draft.write_text(DRAFT.replace("LABEL", label).replace("CREATED", created).replace("CLAIM", CLAIM))
        signed = self.ws.drafts / "signed" / f"claim-{created[:10]}.trig"
        signed.parent.mkdir(exist_ok=True)
        self.ws.run([sys.executable, str(HERE / "fake_np.py"), "sign", "-k", str(self.ws.key), "-s", SIGNER,
                     "-o", str(signed), str(draft)], capture_output=True, check=True)
        return signed

    def test_a_draft_and_its_signed_nanopub_have_the_same_fingerprint(self):
        """The signature is left out, so a signed nanopub matches the draft it was made from."""
        signed = publish.read_signed(self.signed_elsewhere(created="2026-10-23T13:21:59Z"))
        draft = publish.read_draft(self.ws.draft("claim.trig"))

        self.assertEqual(draft.fingerprint, signed.draft.fingerprint)
        self.assertEqual(draft.key, signed.draft.key)
        self.assertEqual(SIGNER, signed.signer)
        self.assertEqual("2026-10-23T13:21:59Z", signed.created)

    def test_sync_records_signed_files_the_registry_holds(self):
        """A signed file the registry already has joins the ledger, and its draft is unchanged."""
        signed = self.signed_elsewhere()
        uri = publish.read_signed(signed).uri
        self.ws.save_state({**self.ws.state(), "on_registry": [uri]})
        self.ws.draft("claim.trig", created="2026-10-20T10:00:00Z")
        publisher = self.ws.publisher(server=self.ws.query_url)

        result = publisher.sync([self.ws.drafts])

        self.assertEqual([uri], [item["uri"] for item in result["added"]])
        version = publisher.ledger.current(CLAIM)
        self.assertEqual(uri, version.uri)
        self.assertEqual("2026-10-20T10:00:00Z", version.published)
        self.assertEqual(self.ws.query_url, version.server)
        self.assertEqual(publish.key_id(PUBKEY), version.pubkey)
        states = {plan.path.name: plan.state for plan in publisher.plan([self.ws.drafts])}
        self.assertEqual("unchanged", states["claim.trig"])

    def test_sync_leaves_out_signed_files_the_registry_lacks(self):
        """A signed file the registry does not hold is reported, not recorded."""
        self.signed_elsewhere()
        publisher = self.ws.publisher(server=self.ws.query_url)

        result = publisher.sync([self.ws.drafts])

        self.assertEqual([], result["added"])
        self.assertEqual(1, len(result["missing"]))
        self.assertIsNone(publisher.ledger.current(CLAIM))

    def test_sync_records_each_version_once_and_in_order(self):
        """Running sync again adds nothing, and an older version found later goes before the newer one."""
        newer = publish.read_signed(self.signed_elsewhere(label="v2", created="2026-10-22T00:00:00Z")).uri
        self.ws.save_state({**self.ws.state(), "on_registry": [newer]})
        publisher = self.ws.publisher(server=self.ws.query_url)
        publisher.sync([self.ws.drafts])
        older = publish.read_signed(self.signed_elsewhere(label="v1", created="2026-10-21T00:00:00Z")).uri
        self.ws.save_state({**self.ws.state(), "on_registry": [newer, older]})

        again = publisher.sync([self.ws.drafts])
        once_more = publisher.sync([self.ws.drafts])

        self.assertEqual([older], [item["uri"] for item in again["added"]])
        self.assertEqual([], once_more["added"])
        self.assertEqual([older, newer], [v.uri for v in publisher.ledger.versions(CLAIM)])

    def test_a_synced_version_is_superseded_by_the_next_publish(self):
        """After sync, a changed draft supersedes the signed version that was found."""
        signed = self.signed_elsewhere()
        uri = publish.read_signed(signed).uri
        self.ws.save_state({**self.ws.state(), "on_registry": [uri]})
        publisher = self.ws.publisher(server=self.ws.query_url, check_head=False)
        publisher.sync([self.ws.drafts])

        result = publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

        self.assertEqual(uri, result["supersedes"])

    def test_the_cli_publishes_drafts_and_skips_signed_files(self):
        """The publish command publishes each draft once and leaves signed files alone."""
        self.ws.draft("claim.trig")
        args = ["--live", "--np", f"{sys.executable} {HERE / 'fake_np.py'}", "--key", str(self.ws.key),
                "--signer", SIGNER, "--ledger", str(self.ws.root / "ledger.json"), "--query", self.ws.query_url]
        default_archive = publish.DEFAULT_ARCHIVE
        publish.DEFAULT_ARCHIVE = self.ws.root / "published"
        os.environ["FAKE_NP_STATE"] = str(self.ws.state_file)
        try:
            self.assertEqual(0, publish.main(["publish", str(self.ws.drafts), *args]))
            signed = next((self.ws.root / "published").iterdir())
            (self.ws.drafts / "signed.trig").write_text(signed.read_text())
            self.assertEqual(0, publish.main(["publish", str(self.ws.drafts), *args]))
        finally:
            publish.DEFAULT_ARCHIVE = default_archive
            del os.environ["FAKE_NP_STATE"]
        self.assertEqual(1, len(self.ws.calls("publish")))


class ServerTest(unittest.TestCase):
    """The local web page's API."""

    def setUp(self):
        """Start a server on a free port, over a fresh workspace."""
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name))
        self.draft = self.ws.draft("claim.trig")
        handler = type("TestHandler", (Handler,), {
            "publisher": self.ws.publisher(),
            "paths": [self.ws.drafts],
            "token": "secret-token",
            "log_message": lambda *args: None,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        """Stop the server and remove the workspace."""
        self.server.shutdown()
        self.server.server_close()
        self.ws.close()
        self.tmp.cleanup()

    def request(self, path: str, body: dict | None = None, token: str | None = "secret-token"):
        """Send a request to the server.

        Args:
            path: the path.
            body: a JSON body to POST, or None to GET.
            token: the token header to send, or None for none.

        Returns:
            the status and the decoded JSON or text body.
        """
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data)
        if token:
            request.add_header("X-Publish-Token", token)
        try:
            with urllib.request.urlopen(request) as response:
                status, raw, kind = response.status, response.read(), response.headers["Content-Type"]
        except urllib.error.HTTPError as ex:
            status, raw, kind = ex.code, ex.read(), ex.headers["Content-Type"]
        return status, json.loads(raw) if "json" in kind else raw.decode()

    def test_the_page_carries_the_token(self):
        """The page is served with the token its requests need."""
        status, page = self.request("/")

        self.assertEqual(200, status)
        self.assertIn('"secret-token"', page)

    def test_the_state_lists_drafts_and_settings(self):
        """The state has each draft with its state, and the settings in use."""
        status, state = self.request("/api/state")

        self.assertEqual(200, status)
        self.assertEqual(["new"], [d["state"] for d in state["drafts"]])
        self.assertEqual(SIGNER, state["settings"]["signer"])
        self.assertEqual([], state["published"])

    def test_the_button_publishes_a_draft(self):
        """Publishing through the API publishes the draft and lists it as published."""
        status, result = self.request("/api/publish", {"path": str(self.draft.resolve())})

        self.assertEqual(200, status)
        self.assertEqual("published", result["status"])
        _, state = self.request("/api/state")
        self.assertEqual(["unchanged"], [d["state"] for d in state["drafts"]])
        self.assertEqual(result["uri"], state["published"][0]["versions"][0]["uri"])

    def test_a_request_without_the_token_is_refused(self):
        """Another site cannot make the browser publish: requests without the token fail."""
        status, result = self.request("/api/publish", {"path": str(self.draft.resolve())}, token=None)

        self.assertEqual(403, status)
        self.assertEqual([], self.ws.calls("sign"))

    def test_only_listed_drafts_can_be_published(self):
        """The API cannot be pointed at a file the page does not list."""
        outside = self.ws.root / "outside.trig"
        outside.write_text(self.draft.read_text())

        status, result = self.request("/api/publish", {"path": str(outside)})

        self.assertEqual(409, status)
        self.assertIn("not a draft this page lists", result["error"])

    def test_a_dry_run_through_the_page_returns_the_warnings(self):
        """The page gets the validation issues to show."""
        self.ws.save_state({**self.ws.state(), "issues": ["Nanopub has no assertion template."]})

        status, result = self.request("/api/publish", {"path": str(self.draft.resolve()), "dryRun": True})

        self.assertEqual(200, status)
        self.assertEqual("dry-run", result["status"])
        self.assertEqual(["Nanopub has no assertion template."], result["warnings"])

    def test_the_page_learns_which_versions_are_loaded(self):
        """The page's check reports each published version as loaded or not."""
        _, result = self.request("/api/publish", {"path": str(self.draft.resolve())})

        status, answer = self.request("/api/loaded")

        self.assertEqual(200, status)
        self.assertEqual({result["uri"]: True}, answer["loaded"])
        self.assertEqual(self.ws.query_url, answer["query"])

    def test_the_page_is_told_when_the_check_fails(self):
        """If the Query instance cannot be asked, the page gets the reason."""
        self.request("/api/publish", {"path": str(self.draft.resolve())})
        self.ws.save_state({**self.ws.state(), "query_status": "LOADING_INITIAL"})

        status, answer = self.request("/api/loaded")

        self.assertEqual(502, status)
        self.assertIn("not ready", answer["error"])

    def test_the_page_previews_a_listed_draft(self):
        """The preview endpoint returns a listed draft's graphs, and nothing for other files."""
        from urllib.parse import quote
        outside = self.ws.root / "outside.trig"
        outside.write_text(self.draft.read_text())

        status, preview = self.request("/api/preview?path=" + quote(str(self.draft.resolve())))
        refused, answer = self.request("/api/preview?path=" + quote(str(outside)))

        self.assertEqual(200, status)
        self.assertEqual("assertion", preview["graphs"][0]["role"])
        self.assertEqual("new", preview["state"])
        self.assertEqual(409, refused)
        self.assertIn("not a draft this page lists", answer["error"])

    def test_an_idle_connection_does_not_hold_up_the_page(self):
        """A connection a browser opens in advance and leaves idle does not block other requests."""
        import socket
        idle = socket.create_connection(("127.0.0.1", self.server.server_address[1]))
        try:
            with urllib.request.urlopen(self.base + "/api/state", timeout=5) as response:
                self.assertEqual(200, response.status)
        finally:
            idle.close()

    def test_the_page_can_find_published_signed_files_with_the_token_only(self):
        """Find published needs the page's token, and reports what it found."""
        refused, _ = self.request("/api/sync", {}, token=None)
        status, result = self.request("/api/sync", {})

        self.assertEqual(403, refused)
        self.assertEqual(200, status)
        self.assertEqual({"added": [], "missing": [], "errors": []}, result)

    def test_a_failure_is_reported_with_its_reason(self):
        """A refusal comes back as an error message for the page to show."""
        self.ws.save_state({"intros": []})

        status, result = self.request("/api/publish", {"path": str(self.draft.resolve())})

        self.assertEqual(409, status)
        self.assertIn("no introduction declares", result["error"])


if __name__ == "__main__":
    unittest.main()
