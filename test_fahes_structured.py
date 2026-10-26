import contextlib
import csv
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import fahes_structured as fahes


class StructuredFahesTests(unittest.TestCase):
    def test_csv_cleans_syntactic_outlier_and_preserves_header_and_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.csv"
            output = Path(directory) / "cleaned.csv"
            rows = [["code", "note"]]
            rows.extend(
                [f"ITEM-{index:03}", f"kept, note {index}"]
                for index in range(120)
            )
            rows.append(["???", "kept, note 120"])
            with source.open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream).writerows(rows)
            original = source.read_text(encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                fahes.process(source, output, False, 0.99)

            self.assertEqual(source.read_text(encoding="utf-8"), original)
            with output.open("r", encoding="utf-8", newline="") as stream:
                cleaned = list(csv.reader(stream))
            self.assertEqual(cleaned[0], rows[0])
            self.assertEqual(cleaned[-1], ["", "kept, note 120"])

    def test_json_replaces_syntactic_outlier_without_mutating_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.json"
            output = Path(directory) / "cleaned.json"
            document = {
                "records": [
                    {"code": f"ITEM-{index:03}", "group": f"G{index % 3}"}
                    for index in range(120)
                ]
            }
            document["records"].append({"code": "???", "group": "G0"})
            source.write_text(json.dumps(document), encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                fahes.process(source, output, False, 0.99)

            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), document)
            cleaned = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsNone(cleaned["records"][-1]["code"])
            self.assertEqual(cleaned["records"][-1]["group"], "G0")

    def test_xml_removes_syntactic_outlier_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.xml"
            output = Path(directory) / "cleaned.xml"
            records = "".join(
                f'<record><code source="ITEM-{index:03}">ITEM-{index:03}</code>'
                f"<group>G{index % 3}</group></record>"
                for index in range(120)
            )
            source.write_text(
                f'<records>{records}<record><code source="???">???</code>'
                "<group>G0</group></record></records>",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                fahes.process(source, output, False, 0.99)

            cleaned = ET.parse(output).getroot()
            self.assertIsNone(cleaned[-1].find("code").text)
            self.assertIsNone(cleaned[-1].find("code").get("source"))
            self.assertEqual(cleaned[-1].findtext("group"), "G0")

    def test_numeric_detector_uses_leave_one_value_out_density(self) -> None:
        values = [0] * 8 + list(range(20, 120))
        scores = fahes._numeric_outlier_scores(values)
        self.assertGreater(scores["0"], 0.99)

    def test_random_sample_detector_finds_frequent_inlier_candidate(self) -> None:
        document = [
            {
                "status": "missing" if index % 10 in {0, 1} else f"normal-{index % 7}",
                "group": index % 4,
                "section": index % 6,
            }
            for index in range(120)
        ]
        profile = fahes._json_profile(document)
        scores = fahes._random_sample_scores(
            profile, "status", [row["status"] for row in document]
        )
        self.assertGreater(scores["missing"], 0.99)

    def test_repeated_pattern_clears_all_duplicate_array_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "values.json"
            output = Path(directory) / "cleaned.json"
            source.write_text(
                json.dumps(["99999999", "99999999", "12345678", "87654321"]),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                fahes.process(source, output, False, 0.99)

            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                [None, None, "12345678", "87654321"],
            )

    def test_rdf_literal_triple_is_removed_when_rdflib_is_installed(self) -> None:
        try:
            from rdflib import Graph
        except ImportError:
            self.skipTest("rdflib is installed from requirements.txt for RDF support")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.nt"
            output = Path(directory) / "cleaned.nt"
            triples = []
            for index in range(120):
                triples.append(
                    f'<urn:record:{index}> <urn:code> "ITEM-{index:03}" .'
                )
                triples.append(
                    f'<urn:record:{index}> <urn:group> "G{index % 3}" .'
                )
            triples.append('<urn:record:dmv> <urn:code> "???" .')
            triples.append('<urn:record:dmv> <urn:group> "G0" .')
            source.write_text("\n".join(triples) + "\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                fahes.process(source, output, False, 0.99)

            graph = Graph()
            graph.parse(output, format="nt")
            self.assertEqual(len(list(graph.triples((None, None, None)))), 241)
            self.assertFalse(
                any(str(obj) == "???" for _, _, obj in graph)
            )


if __name__ == "__main__":
    unittest.main()
