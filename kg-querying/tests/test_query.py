"""Offline tests for query.py: placeholders, layout, saving, and a run against a local server."""

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import query  # noqa: E402
from query import QueryError, QueryResult, StoredQuery  # noqa: E402

EVIDENCE_QUERY = """# question: Which evidence supports claims about a concept?
# parameters: concept = a concept IRI
SELECT ?claim ?evidence WHERE {
  ?claim <https://w3id.org/delphi/ontology/hasEvidence> ?evidence ; ?p ?_concept_iri .
  OPTIONAL { ?claim <http://www.w3.org/2000/01/rdf-schema#label> ?__label }
}
"""

RESULTS = {
    "head": {"vars": ["claim", "evidence"]},
    "results": {"bindings": [
        {"claim": {"type": "uri", "value": "https://w3id.org/delphi/resource/claim/a"},
         "evidence": {"type": "uri", "value": "https://w3id.org/delphi/resource/evidence/a"}},
        {"claim": {"type": "uri", "value": "https://w3id.org/delphi/resource/claim/b"}},
    ]},
}


def stored(text: str = EVIDENCE_QUERY, name: str = "evidence") -> StoredQuery:
    """Build a stored query from text, as if read from ``queries/<name>.rq``.

    Args:
        text: the query text.
        name: the query's name.

    Returns:
        the parsed query.
    """
    return StoredQuery(path=Path(f"{name}.rq"), sparql=text, header=query.parse_header(text),
                       placeholders=query.find_placeholders(text))


class HeaderAndPlaceholderTest(unittest.TestCase):
    """Reading a query file's header and its grlc placeholders."""

    def test_header_fields_are_read_until_the_first_line_of_sparql(self) -> None:
        """The leading ``# key: value`` lines become header fields, keyed in lower case."""
        header = query.parse_header("# Question: What?\n# notes: n\nSELECT * {}\n# late: no\n")
        self.assertEqual(header, {"question": "What?", "notes": "n"})

    def test_placeholders_are_found_with_their_kind(self) -> None:
        """A single underscore is required, a double one optional, and ``_iri`` marks an IRI."""
        placeholders = query.find_placeholders(EVIDENCE_QUERY)
        self.assertEqual(set(placeholders), {"concept", "label"})
        self.assertTrue(placeholders["concept"].iri)
        self.assertFalse(placeholders["concept"].optional)
        self.assertTrue(placeholders["label"].optional)
        self.assertFalse(placeholders["label"].iri)

    def test_placeholders_named_only_in_comments_are_ignored(self) -> None:
        """A variable mentioned in a header comment is not a parameter."""
        self.assertEqual(query.find_placeholders("# uses ?_other_iri\nSELECT * { ?s ?p ?_x }"),
                         {"x": query.Placeholder(name="x", variable="?_x", optional=False, iri=False)})

    def test_ordinary_variables_are_not_placeholders(self) -> None:
        """Variables without a leading underscore, such as ``?claim_np``, are left alone."""
        self.assertEqual(query.find_placeholders("SELECT ?claim_np { ?claim_np ?p ?o }"), {})


class FillPlaceholderTest(unittest.TestCase):
    """Substituting parameter values into a query."""

    def test_an_iri_parameter_is_written_in_angle_brackets(self) -> None:
        """An IRI value replaces every occurrence of its placeholder."""
        sparql = query.fill_placeholders(stored(), {"concept": "https://example.org/c"})
        self.assertIn("?p <https://example.org/c> .", sparql)
        self.assertNotIn("?_concept_iri", query.strip_comments(sparql))

    def test_a_literal_parameter_is_quoted_and_escaped(self) -> None:
        """Quotes, backslashes and line breaks in a literal can't break out of the string."""
        sparql = query.fill_placeholders(stored(), {"concept": "https://example.org/c",
                                                    "label": 'say "hi"\\\n'})
        self.assertIn('"say \\"hi\\"\\\\\\n"', sparql)

    def test_a_missing_required_parameter_is_refused(self) -> None:
        """Running without a required parameter names it."""
        with self.assertRaisesRegex(QueryError, "needs --param for: concept"):
            query.fill_placeholders(stored(), {})

    def test_an_unknown_parameter_is_refused(self) -> None:
        """A misspelt parameter is reported along with the ones the query takes."""
        with self.assertRaisesRegex(QueryError, "no parameter concpt.*concept \\(IRI\\)"):
            query.fill_placeholders(stored(), {"concpt": "https://example.org/c"})

    def test_a_value_that_is_not_an_iri_is_refused(self) -> None:
        """An IRI parameter must be absolute and free of characters that would end it."""
        for value in ["EGFR", "https://example.org/a b", "https://example.org/>"]:
            with self.subTest(value=value), self.assertRaises(QueryError):
                query.fill_placeholders(stored(), {"concept": value})


class LayoutTest(unittest.TestCase):
    """Showing results in the terminal."""

    def test_known_namespaces_are_shortened(self) -> None:
        """Delphi, nanopub and other common IRIs get their usual prefixes."""
        self.assertEqual(query.compact("https://w3id.org/delphi/resource/claim/a"), "delphir:claim/a")
        self.assertEqual(query.compact("https://w3id.org/np/RAabc"), "np:RAabc")
        self.assertEqual(query.compact("https://example.org/x"), "https://example.org/x")

    def test_narrow_results_are_shown_as_a_table(self) -> None:
        """Results that fit stay a table, with unbound values left empty."""
        text = query.format_results(query.parse_results(RESULTS), 120, "auto")
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["claim", "evidence"])
        self.assertEqual(lines[2].split(), ["delphir:claim/a", "delphir:evidence/a"])
        self.assertEqual(lines[3].split(), ["delphir:claim/b"])

    def test_results_too_wide_for_a_table_are_shown_as_records(self) -> None:
        """When a table would cut columns below a readable width, each row gets its own block."""
        text = query.format_results(query.parse_results(RESULTS), 30, "auto")
        self.assertIn("── 1 ", text)
        self.assertIn("evidence  delphir:evidence/a", text)

    def test_a_construct_result_is_refused(self) -> None:
        """Only SELECT results can be laid out and saved."""
        with self.assertRaisesRegex(QueryError, "only SELECT"):
            query.parse_results({"boolean": True, "head": {}})


class SaveTest(unittest.TestCase):
    """Saving results with how they were obtained."""

    def test_results_are_saved_as_csv_and_json_with_their_provenance(self) -> None:
        """The JSON names the question, the parameters, the SPARQL and the instance state."""
        state = query.InstanceState(status="READY", load_counter="7", checksum="abc", nanopub_count="7")
        result = query.parse_results(RESULTS)
        executed_at = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            first = query.save_results(stored(), "SELECT 1", {"concept": "https://example.org/c"},
                                       "https://q/repo/full", state, result, executed_at, Path(folder))
            second = query.save_results(stored(), "SELECT 1", {}, "https://q/repo/full", state, result,
                                        executed_at, Path(folder))
            record = json.loads(first[1].read_text())
            csv_lines = first[0].read_text().splitlines()
        self.assertEqual([p.name for p in first], ["20261027T120000Z.csv", "20261027T120000Z.json"])
        self.assertEqual([p.name for p in second], ["20261027T120000Z-2.csv", "20261027T120000Z-2.json"])
        self.assertEqual(record["question"], "Which evidence supports claims about a concept?")
        self.assertEqual(record["parameters"], {"concept": "https://example.org/c"})
        self.assertEqual(record["instance"]["load_counter"], "7")
        self.assertEqual(record["row_count"], 2)
        self.assertEqual(record["sparql"], "SELECT 1")
        self.assertEqual(csv_lines[1], "https://w3id.org/delphi/resource/claim/a,"
                                       "https://w3id.org/delphi/resource/evidence/a")


class FakeQueryInstance(BaseHTTPRequestHandler):
    """A stand-in Query instance: reports its state on HEAD and answers every query with RESULTS."""

    status = "READY"
    received: list[str] = []

    def do_HEAD(self) -> None:
        """Report the instance state in the response headers."""
        self.send_response(200)
        self.send_header("Nanopub-Query-Status", self.status)
        self.send_header("Nanopub-Query-Load-Counter", "42")
        self.end_headers()

    def do_POST(self) -> None:
        """Record the query and answer it with RESULTS."""
        length = int(self.headers["Content-Length"])
        FakeQueryInstance.received.append(parse_qs(self.rfile.read(length).decode())["query"][0])
        body = json.dumps(RESULTS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet."""


class RunTest(unittest.TestCase):
    """Running a stored query end to end against a local Query instance."""

    def setUp(self) -> None:
        """Start the fake instance and a folder with one stored query."""
        FakeQueryInstance.status = "READY"
        FakeQueryInstance.received = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeQueryInstance)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.instance = f"http://127.0.0.1:{self.server.server_port}/"
        self.folder = tempfile.TemporaryDirectory()
        self.query_file = Path(self.folder.name) / "evidence.rq"
        self.query_file.write_text(EVIDENCE_QUERY)

    def tearDown(self) -> None:
        """Stop the fake instance and remove the folder."""
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def run_cli(self, *extra: str) -> tuple[int, str, str]:
        """Run ``query.py run`` on the stored query against the fake instance.

        Args:
            extra: further command-line arguments.

        Returns:
            the exit code, standard output and standard error.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = query.main(["run", str(self.query_file), "--instance", self.instance,
                               "--results-dir", str(Path(self.folder.name) / "results"), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_a_query_runs_with_its_parameters_and_its_results_are_saved(self) -> None:
        """The filled-in SPARQL reaches the instance, and the results are printed and saved."""
        code, out, _ = self.run_cli("--param", "concept=https://example.org/c")
        self.assertEqual(code, 0)
        self.assertIn("<https://example.org/c>", FakeQueryInstance.received[0])
        self.assertIn("delphir:claim/a", out)
        self.assertIn("2 rows from", out)
        saved = sorted(p.suffix for p in (Path(self.folder.name) / "results" / "evidence").iterdir())
        self.assertEqual(saved, [".csv", ".json"])

    def test_no_save_only_prints(self) -> None:
        """With --no-save nothing is written."""
        code, _, _ = self.run_cli("--param", "concept=https://example.org/c", "--no-save")
        self.assertEqual(code, 0)
        self.assertFalse((Path(self.folder.name) / "results").exists())

    def test_an_instance_that_is_still_loading_is_not_queried(self) -> None:
        """A loading instance would answer from an incomplete store, so the run stops."""
        FakeQueryInstance.status = "LOADING_INITIAL"
        code, _, err = self.run_cli("--param", "concept=https://example.org/c")
        self.assertEqual(code, 1)
        self.assertIn("LOADING_INITIAL, not ready", err)
        self.assertEqual(FakeQueryInstance.received, [])


if __name__ == "__main__":
    unittest.main()
