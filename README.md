# Hetero-MDV: FAHES-style disguised missing-value detection

This project applies and adapts the ideas in **FAHES: A Robust Disguised Missing Values
Detector** to CSV, JSON, XML, and RDF. The command-line tool is
[`fahes_structured.py`](./fahes_structured.py). It detects likely disguised
missing values (DMVs), reports the candidate values and detector scores, and
writes a cleaned copy of the input.

[`fahes_structured_v2.py`](./fahes_structured_v2.py) uses the same detector but
deletes XML elements whose text is identified as a DMV.

## How FAHES is used in this work

FAHES was designed for CSV tables. For CSV, this script profiles the headered
columns directly, using the same table-and-column shape as FAHES, although its
detectors are a Python adaptation rather than the original FAHES binary. For
JSON, XML, and RDF, we apply the same detection ideas to scalar values in each
format and group them into fields:

| Format | Field | Record (row) |
| --- | --- | --- |
| CSV | Column header | CSV record |
| JSON | Nested key path, such as `response.docs.abstract` | JSON object |
| XML | Element path or attribute path, such as `mondial/country/@population` | XML element/record |
| RDF | Predicate IRI | RDF subject |

For each profiled field, the script applies four Fahes-inspired signals and
unions their candidates. XML also has a cross-field placeholder signal:
repeated bracketed text shared by at least three element or attribute paths is
treated as a missing-value marker and its text or attribute value is cleared.
This supports common published placeholders such as `[Données non publiées]`,
including fields whose values are all placeholders.

1. **Syntactic outliers:** values whose character-class pattern (alphanumeric
   runs, spaces, and punctuation) is rare among distinct field values. This
   signal is used only when a field has a clearly dominant pattern, avoiding
   rare-pattern false positives in highly variable free-text fields. A
   candidate must also occur in at least 5% of the field values or be a short
   symbolic marker.
2. **Repeated patterns:** long values dominated by repeated characters or
   substrings.
3. **Numeric density:** frequent numeric candidates whose leave-one-value-out
   Gaussian-kernel density is low compared with the field's estimated peak
   density. At least 50 numeric observations are required; scores use the
   median across multiple bandwidths, and contiguous integer domains are
   treated as discrete values rather than outliers.
4. **RandomDMVD-style sampling:** frequent values are tested to see whether
   records containing them resemble the overall data in selected other fields,
   using the paper's mutual-information comparison idea. Sampling is limited to
   fields with at least 20 values and candidates occurring in 5%–25% of them,
   avoiding small-field and near-constant-category false positives.

Scores are on a 0–1 scale; higher scores are stronger candidate signals. The
default cutoff is **strictly greater than 0.99**, following the paper's
recommendation for strict thresholds. This is an unsupervised heuristic, not a
guarantee that every reported candidate is missing or that every DMV will be
found. Review the report and cleaned output before using them downstream.

This is an adaptation, not a line-for-line implementation of FAHES. In
particular, syntactic pattern aggregation is simplified, and RandomDMVD uses a
greedy selection of promising fields rather than an exact port of the original
implementation. The results can therefore differ from FAHES on CSV data.
Empty/null values are not profiled as DMV candidates.

## Requirements

- Python 3.10 or newer
- `rdflib` for RDF input and output

Install the RDF dependency with:

```bash
python -m pip install -r requirements.txt
```

CSV, JSON, and XML use Python's standard library and need no extra packages.

## CSV input

CSV files are treated as headered tables: each column is profiled independently
and each data row is one record. The first row must contain column names. The
script supports standard CSV quoting, including quoted fields with commas and
newlines, and reads UTF-8 files (an optional UTF-8 BOM is accepted). Empty
cells are left unchanged; detected DMV cells are written as empty fields.

For example:

```bash
python fahes_structured.py data/dblp/cwurData.csv \
  --output data/dblp/cwurData.cleaned.csv
```

The source CSV is not changed unless `--in-place` is supplied. The cleaned
output keeps the header and record order, but CSV quoting/spacing may be
normalized when it is written.

## Run it

By default, the input is preserved and a sibling file named
`<input-name>.cleaned.<extension>` is created:

```bash
python fahes_structured.py data/dblp/cwurData.csv
python fahes_structured.py data/nytimes/nytimes.json
python fahes_structured.py data/mondial/mondial.xml
python fahes_structured.py data/dblp/all-clean-original-direction.nt
```

Use version 2 to remove detected XML text elements rather than retaining empty
elements:

```bash
python fahes_structured_v2.py data/mondial/mondial.xml
```

Version 2 keeps the original cleaning behavior for CSV, JSON, and RDF.

Choose a specific output path with `-o` / `--output`:

```bash
python fahes_structured.py data/mondial/mondial.xml \
  --output /tmp/mondial-cleaned.xml
```

Overwrite the input only when you explicitly request it:

```bash
python fahes_structured.py data/nytimes/nytimes.json --in-place
```

Adjust the candidate score cutoff if needed:

```bash
python fahes_structured.py data/dblp/all-clean-original-direction.nt \
  --threshold 0.995
```

The threshold must be between 0 and 1. Candidates are printed as a JSON report
to standard output, including the field, original value, detector names, and
scores. The report also gives the number of distinct candidates and the number
of value occurrences removed. Errors (including malformed input and missing
RDF dependencies) are printed to standard error and return a non-zero exit
status.

## What cleaning changes

- **JSON:** each detected scalar occurrence is set to `null`; arrays and
  surrounding objects are retained.
- **CSV:** each detected cell is replaced with an empty field; the header and
  row order are retained.
- **XML:** detected text is emptied; a detected attribute is removed. The
  element and its other content are retained.
- **XML with version 2:** a detected text element is removed from its parent;
  detected attributes are removed as in the original version. The document
  root itself cannot be removed, so detected root text is emptied.
- **RDF:** triples with a detected predicate/object value are removed. Both
  literal and resource objects can be considered.

Serialization may normalize formatting (for example, CSV quoting, JSON
indentation, or XML whitespace). The output preserves the parsed data
structure, not the original byte-for-byte formatting. CSV inputs must have a
header row; empty CSV cells are treated as explicit missing values and are not
profiled as disguised values.

Supported RDF extensions are `.nt`, `.n3`, `.ttl`, `.rdf`, `.owl`, `.trig`,
`.nq`, and `.jsonld`.

## Test

Run the focused tests from the project directory:

```bash
python -m unittest -v test_fahes_structured
```

The RDF test runs when `rdflib` is installed; otherwise it is skipped.

## Sources

- Qahtan et al., “FAHES: A Robust Disguised Missing Values Detector,” KDD 2018,
  [DOI: 10.1145/3219819.3220109](https://doi.org/10.1145/3219819.3220109).
- [QCRI Fahes_Demo repository](https://github.com/qcri/Fahes_Demo/tree/master).
