#!/usr/bin/env python3
"""Detect and remove FAHES-style disguised missing values in CSV, JSON, XML, and RDF.

Examples:
    python fahes_structured.py input.csv
    python fahes_structured.py input.json
    python fahes_structured.py input.xml -o cleaned.xml
    python fahes_structured.py graph.nt --in-place

The detection procedure applies FAHES-inspired syntactic-pattern,
repeated-pattern, numeric-density, and RandomDMVD modules to table columns,
structured fields, XML records, and RDF subject/predicate pairs. Pattern
aggregation is intentionally compact rather than a line-for-line port of the
original C++ implementation.

CSV values are replaced with empty fields, JSON values with null, XML text
values are emptied (and matching attributes removed), and matching RDF triples
are removed. The input is left untouched unless --in-place is specified.

Sources: https://github.com/qcri/Fahes_Demo/tree/master and the FAHES KDD 2018
paper (locally provided as fahes.md and fahes.pdf; DOI 10.1145/3219819.3220109).
RDF parsing and serialization uses rdflib (see requirements.txt).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass
class Observation:
    field: str
    value: Any
    row: int
    clear: Callable[[], None]


@dataclass
class Profile:
    observations: list[Observation] = field(default_factory=list)
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)

    def add(self, observation: Observation) -> None:
        self.observations.append(observation)
        self.rows.setdefault(observation.row, {})[observation.field] = observation.value


def _json_profile(document: Any) -> Profile:
    profile = Profile()

    def walk(value: Any, path: str, row: int | None = None) -> None:
        if isinstance(value, dict):
            row = id(value)
            profile.rows.setdefault(row, {})
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(child, (dict, list)) or child is None:
                    walk(child, child_path, row)
                else:
                    profile.add(
                        Observation(
                            child_path,
                            child,
                            row,
                            lambda parent=value, item=key: parent.__setitem__(item, None),
                        )
                    )
        elif isinstance(value, list):
            profile.rows.setdefault(row if row is not None else id(value), {})
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    walk(child, path, row)
                elif child is not None:
                    parent_row = row if row is not None else id(value)
                    profile.add(
                        Observation(
                            path or "[]",
                            child,
                            parent_row,
                            lambda values=value, item_index=index: values.__setitem__(
                                item_index, None
                            ),
                        )
                    )

    walk(document, "")
    return profile


def _csv_profile(rows: list[list[str]], headers: list[str]) -> Profile:
    profile = Profile()
    header_counts = Counter(headers)
    field_names = [
        header if header_counts[header] == 1 else f"{header} [column {index + 1}]"
        for index, header in enumerate(headers)
    ]
    for row_index, values in enumerate(rows):
        profile.rows[row_index] = {
            field_names[index]: value
            for index, value in enumerate(values[: len(headers)])
            if value != ""
        }
        for column_index, value in enumerate(values[: len(headers)]):
            if value == "":
                continue
            profile.add(
                Observation(
                    field_names[column_index],
                    value,
                    row_index,
                    lambda row=values, index=column_index: row.__setitem__(index, ""),
                )
            )
    return profile


def _xml_profile(root: ET.Element, delete_labels: bool = False) -> Profile:
    profile = Profile()
    parents = {
        child: element
        for element in root.iter()
        for child in element
    }

    def clear_text(node: ET.Element) -> None:
        if delete_labels and node in parents:
            parents[node].remove(node)
        else:
            node.text = None

    def walk(element: ET.Element, path: str) -> None:
        row = id(element)
        profile.rows.setdefault(row, {})
        for name, value in element.attrib.items():
            field_name = f"{path}/@{name}"
            profile.add(
                Observation(
                    field_name,
                    value,
                    row,
                    lambda node=element, attribute=name: node.attrib.pop(attribute, None),
                )
            )

        children = list(element)
        repeated_tags = Counter(child.tag for child in children if isinstance(child.tag, str))
        if element.text and element.text.strip():
            field_name = f"{path}/#text"
            profile.add(
                Observation(
                    field_name,
                    element.text.strip(),
                    row,
                    lambda node=element: clear_text(node),
                )
            )
        for child in children:
            if not isinstance(child.tag, str):
                continue
            child_path = f"{path}/{child.tag}"
            if list(child):
                walk(child, child_path)
            else:
                child_row = id(child) if repeated_tags[child.tag] > 1 else row
                profile.rows.setdefault(child_row, {})
                for name, value in child.attrib.items():
                    field_name = f"{child_path}/@{name}"
                    profile.add(
                        Observation(
                            field_name,
                            value,
                            child_row,
                            lambda node=child, attribute=name: node.attrib.pop(
                                attribute, None
                            ),
                        )
                    )
                if child.text and child.text.strip():
                    profile.add(
                        Observation(
                            child_path,
                            child.text.strip(),
                            child_row,
                            lambda node=child: clear_text(node),
                        )
                    )

    walk(root, root.tag)
    return profile


def _rdflib_format(path: Path) -> str:
    suffix = path.suffix.lower()
    formats = {
        ".nt": "nt",
        ".n3": "n3",
        ".ttl": "turtle",
        ".rdf": "xml",
        ".owl": "xml",
        ".trig": "trig",
        ".nq": "nquads",
        ".jsonld": "json-ld",
    }
    if suffix not in formats:
        raise ValueError(f"Unsupported RDF file extension: {suffix or '(none)'}")
    return formats[suffix]


def _rdf_profile(graph: Any) -> Profile:
    from rdflib import Literal

    profile = Profile()
    subject_rows: dict[Any, int] = {}
    for subject, predicate, obj in graph:
        field_name = str(predicate)
        row = subject_rows.setdefault(subject, len(subject_rows))
        profile.rows.setdefault(row, {})
        value = obj.toPython() if isinstance(obj, Literal) else str(obj)
        if isinstance(value, (dict, list)) or value is None:
            value = str(obj)
        triple = (subject, predicate, obj)
        profile.add(
            Observation(
                field_name,
                value,
                row,
                lambda target=graph, statement=triple: target.remove(statement),
            )
        )
    return profile


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _value_key(value: Any) -> str:
    return _value_text(value)


def _pattern(value: str) -> str:
    parts: list[str] = []
    for character in value:
        if character.isalnum():
            current = "a"
        elif character.isspace():
            current = " "
        else:
            current = character
        if parts and parts[-1][0] == current:
            parts[-1] += current
        else:
            parts.append(current)
    return "".join(part[0] for part in parts)


def _repeated_pattern_score(value: str, longest: int) -> float:
    if len(value) < 6 or longest < 1:
        return 0.0
    covered = 0
    for period in range(1, len(value) // 2 + 1):
        repeats = len(value) // period
        if repeats >= 2 and value[: period * repeats] == value[:period] * repeats:
            covered = max(covered, period * repeats)
    for match in re.finditer(r"(.)\1{3,}", value):
        covered = max(covered, len(match.group()))
    return min(1.0, (covered / len(value)) * (covered / longest))


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = _value_text(value)
        if not text or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _numeric_outlier_scores(values: list[Any]) -> dict[str, float]:
    numbers = [_numeric_value(value) for value in values]
    if any(number is None for number in numbers):
        return {}
    numeric = [number for number in numbers if number is not None]
    frequencies = Counter(numeric)
    distinct = sorted(frequencies)
    if len(distinct) < 6 or len(numeric) < 50:
        return {}
    if (
        all(number.is_integer() for number in distinct)
        and distinct[-1] - distinct[0] + 1 == len(distinct)
    ):
        return {}

    standard_deviation = statistics.pstdev(numeric)
    if standard_deviation == 0:
        return {}
    bandwidth = 1.06 * standard_deviation * len(numeric) ** (-0.2)
    if bandwidth <= 0:
        return {}

    def density(point: float, support: list[float], counts: Counter[float], width: float) -> float:
        sample_count = sum(counts.values())
        scale = 1.0 / (sample_count * width * math.sqrt(2.0 * math.pi))
        return scale * sum(
            counts[value] * math.exp(-0.5 * ((point - value) / width) ** 2)
            for value in support
        )

    low, high = min(distinct), max(distinct)
    if low == high:
        return {}
    grid = [low + (high - low) * index / 127 for index in range(128)]
    global_peak = max(density(point, distinct, frequencies, bandwidth) for point in grid)
    if global_peak <= 0:
        return {}

    scores: dict[str, float] = {}
    for candidate in distinct:
        if frequencies[candidate] < 2:
            continue
        support = [value for value in distinct if value != candidate]
        remaining = frequencies.copy()
        del remaining[candidate]
        if len(support) < 2:
            continue
        sample = [value for value in numeric if value != candidate]
        if len(sample) < 2:
            continue
        width = 1.06 * statistics.pstdev(sample) * len(sample) ** (-0.2)
        if width <= 0:
            continue
        candidate_density = statistics.median(
            density(candidate, support, remaining, width * scale)
            for scale in (1.0, 0.8, 0.6, 0.4, 0.2)
        )
        score = max(0.0, min(1.0, 1.0 - candidate_density / global_peak))
        for original, number in zip(values, numbers):
            if number == candidate:
                scores[_value_key(original)] = score
    return scores


def _random_sample_scores(profile: Profile, field_name: str, values: list[Any]) -> dict[str, float]:
    if len(values) < 20:
        return {}
    other_fields = sorted(
        {
            name
            for row in profile.rows.values()
            for name in row
            if name != field_name
        }
    )
    if len(other_fields) < 2:
        return {}

    rows = [row for row in profile.rows.values() if field_name in row]
    scores: dict[str, float] = {}
    counts = Counter(_value_key(value) for value in values)
    all_field_count = len(values)
    for candidate, frequency in counts.items():
        frequency_ratio = frequency / all_field_count
        if frequency < 2 or frequency_ratio < 0.05 or frequency_ratio > 0.25:
            continue
        subset = [row for row in rows if _value_key(row[field_name]) == candidate]
        if len(subset) < 2:
            continue

        ranked: list[tuple[float, str]] = []
        for name in other_fields:
            full_values = [row[name] for row in rows if name in row]
            subset_values = [row[name] for row in subset if name in row]
            if not full_values or not subset_values:
                continue
            full_ratio = len({_value_key(value) for value in full_values}) / len(full_values)
            subset_ratio = len({_value_key(value) for value in subset_values}) / len(subset_values)
            ranked.append((abs((subset_ratio / full_ratio if full_ratio else 0.0) - 1.0), name))
        ranked.sort()
        selected: list[str] = []
        best = 0.0
        for _, name in ranked:
            selected.append(name)
            if len(selected) < 2:
                continue
            full_rows = [row for row in rows if all(field in row for field in selected)]
            subset_rows = [row for row in subset if all(field in row for field in selected)]
            if not subset_rows or not full_rows:
                continue

            def pmi_counts(source: list[dict[str, Any]]) -> tuple[Counter[tuple[str, ...]], list[Counter[str]]]:
                tuples = [tuple(_value_key(row[field]) for field in selected) for row in source]
                joint = Counter(tuples)
                marginals = [
                    Counter(_value_key(row[field]) for row in source) for field in selected
                ]
                return joint, marginals

            full_joint, full_marginals = pmi_counts(full_rows)
            subset_joint, subset_marginals = pmi_counts(subset_rows)
            total_full, total_subset = len(full_rows), len(subset_rows)
            distribution_score = 0.0
            for tuple_value, amount in subset_joint.items():
                full_probability = full_joint[tuple_value] / total_full
                subset_probability = amount / total_subset
                full_pmi = math.log(full_probability) - sum(
                    math.log(marginal[part] / total_full)
                    for marginal, part in zip(full_marginals, tuple_value)
                )
                subset_pmi = math.log(subset_probability) - sum(
                    math.log(marginal[part] / total_subset)
                    for marginal, part in zip(subset_marginals, tuple_value)
                )
                distribution_score += subset_probability / (1.0 + abs(full_pmi - subset_pmi))
            best = max(best, distribution_score)
        scores[candidate] = best
    return scores


def detect_dmvs(profile: Profile, threshold: float = 0.99) -> dict[str, dict[str, dict[str, float]]]:
    """Return field -> stringified value -> detector scores for DMV candidates."""
    by_field: dict[str, list[Observation]] = defaultdict(list)
    for observation in profile.observations:
        by_field[observation.field].append(observation)

    detected: dict[str, dict[str, dict[str, float]]] = {}
    for field_name, observations in by_field.items():
        values = [observation.value for observation in observations]
        distinct = {_value_key(value) for value in values}
        if not distinct:
            continue

        pattern_counts = Counter(_pattern(value) for value in distinct)
        if max(pattern_counts.values()) / len(distinct) < 0.25:
            continue
        value_counts = Counter(_value_key(value) for value in values)
        for value in distinct:
            pattern = _pattern(value)
            score = 1.0 - pattern_counts[pattern] / len(distinct)
            numeric_text = value.replace(" ", "")
            if re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)%?", numeric_text):
                continue
            is_short_marker = len(value) <= 4 and not value.isalnum()
            is_frequent_value = value_counts[value] / len(values) >= 0.05
            if (
                (is_frequent_value or is_short_marker)
                and score > threshold
                and pattern_counts[pattern] / len(distinct) < 0.01
            ):
                detected.setdefault(field_name, {}).setdefault(value, {})["syntactic"] = score

        longest = max((len(value) for value in distinct), default=0)
        for value in distinct:
            score = _repeated_pattern_score(value, longest)
            if score > threshold:
                detected.setdefault(field_name, {}).setdefault(value, {})["repeated-pattern"] = score

        for value, score in _numeric_outlier_scores(values).items():
            if score > threshold:
                detected.setdefault(field_name, {}).setdefault(value, {})["numeric-density"] = score

        for value, score in _random_sample_scores(profile, field_name, values).items():
            if score > threshold:
                detected.setdefault(field_name, {}).setdefault(value, {})["random-sample"] = score

    return detected


def _xml_placeholder_candidates(profile: Profile) -> dict[str, dict[str, dict[str, float]]]:
    by_marker: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for observation in profile.observations:
        value = _value_key(observation.value)
        if re.fullmatch(r"\[[^\[\]\r\n]+\]", value):
            by_marker[value.casefold()][observation.field][value] += 1

    candidates: dict[str, dict[str, dict[str, float]]] = {}
    for fields in by_marker.values():
        supporting_fields = sum(
            sum(value_counts.values()) >= 2 for value_counts in fields.values()
        )
        if supporting_fields < 3:
            continue
        for field_name, value_counts in fields.items():
            for value in value_counts:
                candidates.setdefault(field_name, {}).setdefault(value, {})[
                    "xml-placeholder"
                ] = 1.0
    return candidates


def _load(
    path: Path, delete_xml_labels: bool = False
) -> tuple[Any, Profile, Callable[[Path], None]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            try:
                headers = next(reader)
            except StopIteration as error:
                raise ValueError("CSV input must contain a header row") from error
            rows = list(reader)
        if not headers:
            raise ValueError("CSV input must contain at least one column header")

        def write_csv(output: Path) -> None:
            with output.open("w", encoding="utf-8", newline="") as destination:
                writer = csv.writer(destination)
                writer.writerow(headers)
                writer.writerows(rows)

        return rows, _csv_profile(rows, headers), write_csv
    if suffix == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        return document, _json_profile(document), lambda output: output.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if suffix == ".xml":
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        root = ET.parse(path, parser=parser).getroot()

        def write_xml(output: Path) -> None:
            tree = ET.ElementTree(root)
            tree.write(
                output,
                encoding="utf-8",
                xml_declaration=True,
                short_empty_elements=False,
            )

        return root, _xml_profile(root, delete_xml_labels), write_xml
    if suffix in {".nt", ".n3", ".ttl", ".rdf", ".owl", ".trig", ".nq", ".jsonld"}:
        try:
            from rdflib import Graph
        except ImportError as error:
            raise RuntimeError(
                "RDF support requires rdflib. Install it with: python -m pip install rdflib"
            ) from error
        graph = Graph()
        graph.parse(path, format=_rdflib_format(path))
        rdf_format = _rdflib_format(path)

        def write_rdf(output: Path) -> None:
            graph.serialize(destination=str(output), format=rdf_format)

        return graph, _rdf_profile(graph), write_rdf
    raise ValueError(f"Unsupported input format: {suffix or '(no extension)'}")


def process(
    input_path: Path,
    output_path: Path,
    in_place: bool,
    threshold: float,
    delete_xml_labels: bool = False,
) -> int:
    _, profile, write_output = _load(input_path, delete_xml_labels)
    candidates = detect_dmvs(profile, threshold)
    if input_path.suffix.lower() == ".xml":
        for field_name, values in _xml_placeholder_candidates(profile).items():
            for value, scores in values.items():
                candidates.setdefault(field_name, {}).setdefault(value, {}).update(scores)
    selected = {
        (field_name, value)
        for field_name, values in candidates.items()
        for value in values
    }
    removed = 0
    for observation in profile.observations:
        if (observation.field, _value_key(observation.value)) in selected:
            observation.clear()
            removed += 1

    if not in_place:
        write_output(output_path)
    report = {
        "input": str(input_path),
        "output": str(input_path if in_place else output_path),
        "distinct_candidates": sum(len(values) for values in candidates.values()),
        "values_removed": removed,
        "candidates": [
            {
                "field": field_name,
                "value": value,
                "detectors": scores,
            }
            for field_name, values in sorted(candidates.items())
            for value, scores in sorted(values.items())
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return removed


def main(
    argv: Iterable[str] | None = None, *, delete_xml_labels: bool = False
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input CSV, JSON, XML, or RDF file")
    parser.add_argument("-o", "--output", type=Path, help="Cleaned output path")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file after detecting and removing candidates",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.99,
        help="Strict DMV score threshold in [0, 1] (default: 0.99)",
    )
    arguments = parser.parse_args(argv)
    if not 0.0 <= arguments.threshold <= 1.0:
        parser.error("--threshold must be between 0 and 1")
    if arguments.in_place and arguments.output:
        parser.error("--output cannot be combined with --in-place")

    input_path = arguments.input
    output_path = arguments.output or input_path.with_name(
        f"{input_path.stem}.cleaned{input_path.suffix}"
    )
    try:
        process(
            input_path,
            output_path,
            arguments.in_place,
            arguments.threshold,
            delete_xml_labels,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        ET.ParseError,
        json.JSONDecodeError,
        csv.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
