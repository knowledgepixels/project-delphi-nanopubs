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
import warnings
from http.server import HTTPServer
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


class Workspace:
    """A temporary directory with drafts, a key pair, a ledger and the fake CLI's state."""

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

    def test_a_test_server_uses_its_own_ledger_and_skips_the_network_checks(self):
        """Publishing to a test registry keeps a separate ledger, and asks the live network nothing."""
        server = "https://test.registry.knowledgepixels.com/"
        self.assertEqual("ledger.test.registry.knowledgepixels.com.json", publish.ledger_for(server).name)
        self.assertEqual("ledger.json", publish.ledger_for(None).name)
        self.ws.save_state({"intros": []})
        publisher = self.ws.publisher(server=server)
        publisher.publish(self.ws.draft("claim.trig"))

        second = publisher.publish(self.ws.draft("claim.trig", label="EGF binds EGFR"))

        self.assertEqual("superseded", second["status"])
        self.assertEqual([], self.ws.calls("query"))
        self.assertEqual(["-u", server], self.ws.calls("publish")[0][1:3])

    def test_a_signed_file_is_not_a_draft(self):
        """An already signed file is listed as signed and refused for publishing."""
        publisher = self.ws.publisher()
        publisher.publish(self.ws.draft("claim.trig"))
        signed = next((self.ws.root / "published").iterdir())
        copy = self.ws.drafts / "signed.trig"
        copy.write_text(signed.read_text())

        with self.assertRaisesRegex(PublishError, "already signed"):
            publisher.publish(copy)
        states = {plan.path.name: plan.state for plan in publisher.plan([self.ws.drafts])}
        self.assertEqual("signed", states["signed.trig"])
        self.assertEqual("unchanged", states["claim.trig"])

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

    def test_the_cli_publishes_drafts_and_skips_signed_files(self):
        """The publish command publishes each draft once and leaves signed files alone."""
        self.ws.draft("claim.trig")
        args = ["--np", f"{sys.executable} {HERE / 'fake_np.py'}", "--key", str(self.ws.key), "--signer", SIGNER,
                "--ledger", str(self.ws.root / "ledger.json")]
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
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        """Stop the server and remove the workspace."""
        self.server.shutdown()
        self.server.server_close()
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

    def test_a_failure_is_reported_with_its_reason(self):
        """A refusal comes back as an error message for the page to show."""
        self.ws.save_state({"intros": []})

        status, result = self.request("/api/publish", {"path": str(self.draft.resolve())})

        self.assertEqual(409, status)
        self.assertIn("no introduction declares", result["error"])


if __name__ == "__main__":
    unittest.main()
