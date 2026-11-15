"""Validate the generated golden JSON-LD documents with mlcroissant.

Runs only when ``mlcroissant`` is importable (it is not a dependency of
mesa-croissant). Last run against mlcroissant 1.1.1 (PyPI, 2026-11-06), see
``TESTED_WITH``. Validation is offline: the documents carry an inline ``@context``
and ``mlc.Dataset`` only parses and validates until records are requested.

Known mlcroissant 1.1.1 behaviour, accepted here:

* ``keywords`` given as ``sc:DefinedTerm`` objects are rejected (mlcroissant types
  ``keywords`` as text), although Croissant 1.1 allows DefinedTerm. Documents built
  with ``item["keyword_style"] = "text"`` load without that error.
* A FileObject without ``sha256``/``md5`` is an error unless ``isLiveDataset``.
* Warnings it always prints: the context differs from its own (``path``,
  ``repeated``, ``replace``, ``samplingRate`` are not in the Croissant 1.1 context),
  and ``citeAs`` / ``version`` are recommended when absent.
* A field is read only with a data type it knows (``sc:GeoShape`` alone is refused), a
  Parquet MAP reads as (key, value) pairs, and ``includes`` match with ``fnmatch``. The
  Parquet tests at the end (they need pyarrow, ``mlcroissant[parquet]``) load records.
"""

from __future__ import annotations

import json
import re

import pytest

from mesa_croissant import core

mlc = pytest.importorskip("mlcroissant")

TESTED_WITH = "1.1.1"

KNOWN = {
    "defined_term": re.compile(
        r'"@type": "https://schema\.org/Text"\. Got '
        r"https://schema\.org/DefinedTerm"
    ),
    "checksum": re.compile(
        r"At least one of these properties should be defined: "
        r"\['md5', 'sha256'\]"
    ),
}


def _mlc_errors(doc: dict, tmp_path) -> tuple[list[str], list[str]]:
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    try:
        ds = mlc.Dataset(jsonld=str(path))
    except mlc.ValidationError as exc:
        report = str(exc)
        errors_part = report.split("warning(s) during the validation", 1)[0]
        errors = [
            line.strip()[3:] for line in errors_part.splitlines() if line.strip().startswith("-  ")
        ]
        # The warnings block follows the errors block in the same report.
        return errors or [report], []
    return [], sorted(ds.metadata.ctx.issues.warnings)


def _unexpected(errors: list[str], allowed: set[str]) -> list[str]:
    return [e for e in errors if not any(KNOWN[k].search(e) for k in allowed)]


def test_mlcroissant_version_recorded():
    from importlib.metadata import version

    installed = version("mlcroissant")
    assert installed.split(".")[0] == TESTED_WITH.split(".")[0]


def test_golden_documents_with_mlcroissant(golden, tmp_path):
    doc = golden["document"]
    has_terms = any(isinstance(k, dict) for k in doc.get("keywords", []))
    allowed = {"defined_term"} if has_terms else set()
    if golden["mlcroissant"] == "incomplete":
        allowed.add("checksum")
    errors, _ = _mlc_errors(doc, tmp_path)
    assert _unexpected(errors, allowed) == []
    if golden["mlcroissant"] == "complete" and not has_terms:
        assert errors == []


def test_golden_documents_text_keywords_load(golden, tmp_path):
    item = dict(golden["item"], keyword_style="text")
    doc = core.to_jsonld(golden["avus"], item=item)["document"]
    assert all(isinstance(k, str) for k in doc.get("keywords", []))
    errors, warnings = _mlc_errors(doc, tmp_path)
    allowed = {"checksum"} if golden["mlcroissant"] == "incomplete" else set()
    assert _unexpected(errors, allowed) == []
    if golden["mlcroissant"] == "complete":
        assert errors == []
        # Only the documented warnings remain.
        for w in warnings:
            assert ("is recommended, but does not exist" in w) or ("@context" in w), w


def test_known_errors_are_still_errors(tmp_path):
    """If mlcroissant starts accepting DefinedTerm keywords, revisit keyword_style."""
    golden = json.loads(
        (
            __import__("pathlib").Path(__file__).parent
            / "fixtures"
            / "golden_ducklake_crosswalk.json"
        ).read_text(encoding="utf-8")
    )
    errors, _ = _mlc_errors(golden["document"], tmp_path)
    assert any(KNOWN["defined_term"].search(e) for e in errors)


def test_generated_upstream_round_trip_loads(tmp_path):
    """An upstream example imported to AVUs and regenerated still loads in mlcroissant
    once the properties Croissant 1.1 requires (and the upstream file lacks) are added."""
    from conftest import UPSTREAM, load_json

    doc = load_json(UPSTREAM / "v1.1_zenodo-head-mri.json")
    avus = core.from_jsonld(doc)["avus"]
    avus.append({"attribute": "croissant.datePublished", "value": "2015-01-01", "unit": ""})
    out = core.to_jsonld(avus, item={"path": "/iplant/home/shared/x", "name": "x"})
    errors, _ = _mlc_errors(out["document"], tmp_path)
    assert errors == []


BASE_AVUS = [
    {"attribute": "croissant.name", "value": "x", "unit": ""},
    {"attribute": "croissant.description", "value": "d", "unit": ""},
    {"attribute": "croissant.license", "value": "https://creativecommons.org/licenses/by/4.0/",
     "unit": ""},
    {"attribute": "croissant.creator.1.name", "value": "J", "unit": ""},
    {"attribute": "croissant.datePublished", "value": "2026-01-01", "unit": ""},
]


def _a(attr, value, unit=""):
    return {"attribute": attr, "value": value, "unit": unit}


def test_struct_members_with_transform_json_path_load(tmp_path):
    """A STRUCT column whose members carry only transformJsonPath (they inherit the
    parent's source and column) is valid for core and for mlcroissant."""
    typed = core.duckdb_type_to_croissant("STRUCT(a INTEGER, b VARCHAR)")
    avus = BASE_AVUS + [
        _a("croissant.distribution.1.id", "t-files"),
        _a("croissant.distribution.1.type", "FileSet"),
        _a("croissant.distribution.1.encodingFormat", "application/x-parquet"),
        _a("croissant.distribution.1.includes.1", "data/*.parquet"),
        _a("croissant.recordSet.1.id", "t"),
        _a("croissant.recordSet.1.field.1.name", "s"),
        _a("croissant.recordSet.1.field.1.source", "t-files"),
        _a("croissant.recordSet.1.field.1.column", "s"),
    ]
    for k, sub in enumerate(typed["subFields"], 1):
        p = f"croissant.recordSet.1.field.1.subField.{k}."
        avus += [_a(p + "name", sub["name"]), _a(p + "dataType", sub["dataType"]),
                 _a(p + "transformJsonPath", sub["name"])]
    out = core.to_jsonld(avus, item={"path": "/iplant/home/shared/x", "name": "x"})
    assert out["validation"]["valid"], out["validation"]["errors"]
    errors, _ = _mlc_errors(out["document"], tmp_path)
    assert errors == []


def test_file_level_nested_fields_load(tmp_path):
    avus = BASE_AVUS + [
        _a("croissant.file.md5", "d41d8cd98f00b204e9800998ecf8427e"),
        _a("croissant.recordSet.1.id", "pts"),
        _a("croissant.recordSet.1.field.1.name", "gps"),
        _a("croissant.recordSet.1.field.1.dataType", "sc:GeoCoordinates"),
        _a("croissant.recordSet.1.field.1.subField.1.name", "latitude"),
        _a("croissant.recordSet.1.field.1.subField.1.dataType", "sc:Float"),
        _a("croissant.recordSet.1.field.1.subField.1.column", "lat"),
        _a("croissant.recordSet.1.field.1.subField.2.name", "longitude"),
        _a("croissant.recordSet.1.field.1.subField.2.dataType", "sc:Float"),
        _a("croissant.recordSet.1.field.1.subField.2.column", "lon"),
    ]
    out = core.to_jsonld(avus, item={"path": "/iplant/home/shared/x/pts.csv",
                                     "name": "pts.csv", "is_folder": False})
    assert out["validation"]["errors"] == []
    errors, _ = _mlc_errors(out["document"], tmp_path)
    assert errors == []


def test_record_set_source_warning_matches_mlcroissant(tmp_path):
    """core warns about a bare recordSet source because mlcroissant 1.1 rejects it."""
    avus = BASE_AVUS + [
        _a("croissant.distribution.1.id", "f"),
        _a("croissant.distribution.1.contentUrl", "f.csv"),
        _a("croissant.distribution.1.md5", "d41d8cd98f00b204e9800998ecf8427e"),
        _a("croissant.recordSet.1.id", "r1"),
        _a("croissant.recordSet.1.field.1.name", "a"),
        _a("croissant.recordSet.1.field.1.dataType", "sc:Text"),
        _a("croissant.recordSet.1.field.1.source", "f"),
        _a("croissant.recordSet.1.field.1.column", "a"),
        _a("croissant.recordSet.2.id", "r2"),
        _a("croissant.recordSet.2.field.1.name", "c"),
        _a("croissant.recordSet.2.field.1.dataType", "sc:Text"),
        _a("croissant.recordSet.2.field.1.source", "r1"),
    ]
    out = core.to_jsonld(avus, item={"path": "/iplant/home/shared/x", "name": "x"})
    assert out["validation"]["valid"]
    assert any("mlcroissant 1.1" in w["message"] for w in out["validation"]["warnings"])
    errors, _ = _mlc_errors(out["document"], tmp_path)
    assert any("Source should have one of the following properties" in e for e in errors)


def test_provenance_objects_load(tmp_path):
    avus = BASE_AVUS + [
        _a("croissant.distribution.1.id", "f"),
        _a("croissant.distribution.1.contentUrl", "f.csv"),
        _a("croissant.distribution.1.md5", "d41d8cd98f00b204e9800998ecf8427e"),
        _a("croissant.wasGeneratedBy", "a pipeline"),
        _a("croissant.wasDerivedFrom.1", "https://example.org/src"),
    ]
    out = core.to_jsonld(avus, item={"path": "/iplant/home/shared/x", "name": "x"})
    errors, _ = _mlc_errors(out["document"], tmp_path)
    assert errors == []


def _leaves(node):
    if isinstance(node, str):
        return {"value": node, "unit": ""}
    if isinstance(node, list):
        return [_leaves(v) for v in node]
    return {k: _leaves(v) for k, v in node.items()}


def test_schema_parity_record_set_loads_its_records(tmp_path):
    """The record set both MESA writers build for one schema (``schema_parity.json``) loads
    its records: STRUCT members read through transformJsonPath (a quoted name too), a
    LIST(STRUCT) column gives a list of member objects, and the nested STRUCT member typed
    sc:Text has no transform. With one, mlcroissant 1.1 fails the whole record set casting
    the object to text; without it the member reads as null. A MAP column gives a list of
    key / value objects, and a GEOMETRY column typed [sc:GeoShape, sc:Text] reads as
    text."""
    import hashlib

    from conftest import FIXTURES, load_json

    fx = load_json(FIXTURES / "schema_parity.json")
    model = {
        "name": _leaves("T"), "description": _leaves("t"),
        "license": [_leaves("https://creativecommons.org/licenses/by/4.0/")],
        "creator": [{"name": _leaves("MESA"), "type": _leaves("Organization")}],
        "datePublished": _leaves("2026-11-06"),
        "distribution": [{"id": _leaves("t.parquet"), "type": _leaves("FileObject"),
                          "contentUrl": _leaves("t.jsonl"),
                          "encodingFormat": _leaves("application/jsonlines")}],
        "recordSet": [_leaves(fx["record_set"])],
    }
    item = {"path": "/iplant/home/shared/x", "name": "x", "is_folder": True,
            "keyword_style": "text"}
    out = core.to_jsonld(core.model_to_avus(model), item=item)
    assert out["validation"]["errors"] == []
    doc = out["document"]
    data = "".join(json.dumps(r) + "\n" for r in fx["rows"])
    (tmp_path / "t.jsonl").write_text(data, encoding="utf-8")
    dist = doc["distribution"][0]
    # A local file (the generator points contentUrl at the Data Store).
    dist.update({"name": "t.jsonl", "contentUrl": "t.jsonl",
                 "md5": hashlib.md5(data.encode()).hexdigest()})
    (tmp_path / "metadata.json").write_text(json.dumps(doc), encoding="utf-8")
    records = list(mlc.Dataset(jsonld=str(tmp_path / "metadata.json")).records("t"))
    assert len(records) == 2
    first = records[0]
    assert int(first["t/id"]) == 1
    assert int(first["t/s"]["t/s/a"]) == 1
    assert first["t/s"]["t/s/geo_id"] in (b"x", "x")
    assert first["t/s"]["t/s/n"] is None
    assert [(int(m["t/l/a"]), float(m["t/l/it's"])) for m in first["t/l"]] == [
        (1, 1.5), (2, 2.5)]
    assert list(first["t/tags"]) in ([b"x", b"y"], ["x", "y"])
    assert [(m["t/m/key"], int(m["t/m/value"])) for m in first["t/m"]] == [
        (b"k1", 1), (b"k2", 2)]
    assert first["t/g"] == b"0101000000000000000000f03f0000000000000040"


# ---------------------------------------------------------------------------
# Parquet record loading (needs pyarrow: pip install "mlcroissant[parquet]")
# ---------------------------------------------------------------------------

_NON_LOADABLE = ["sc:GeoShape", "sc:GeoCoordinates", "sc:Number", "sc:Enumeration",
                 "cr:Label", "cr:Split", "cr:SegmentationMask"]


@pytest.mark.parametrize("data_type", _NON_LOADABLE)
def test_core_warns_where_mlcroissant_refuses_the_data_type(tmp_path, data_type):
    """A field whose only dataType mlcroissant 1.1.1 cannot read: mlcroissant refuses the
    document, core warns (the type is valid Croissant). With sc:Text next to it both
    accept it."""
    def doc_for(types):
        avus = BASE_AVUS + [
            _a("croissant.distribution.1.id", "f"),
            _a("croissant.distribution.1.contentUrl", "f.csv"),
            _a("croissant.distribution.1.md5", "d41d8cd98f00b204e9800998ecf8427e"),
            _a("croissant.recordSet.1.id", "r"),
            _a("croissant.recordSet.1.field.1.name", "a"),
            _a("croissant.recordSet.1.field.1.source", "f"),
            _a("croissant.recordSet.1.field.1.column", "a"),
        ] + [_a(f"croissant.recordSet.1.field.1.dataType.{k}", t)
             for k, t in enumerate(types, 1)]
        return core.to_jsonld(avus, item={"path": "/iplant/home/shared/x", "name": "x",
                                          "keyword_style": "text"})

    alone = doc_for([data_type])
    assert alone["validation"]["valid"]
    assert any("mlcroissant 1.1.1" in w["message"] for w in alone["validation"]["warnings"])
    errors, _ = _mlc_errors(alone["document"], tmp_path)
    assert any("does not specify a valid" in e for e in errors), errors
    paired = doc_for([data_type, "sc:Text"])
    assert not any("mlcroissant 1.1.1" in w["message"]
                   for w in paired["validation"]["warnings"])
    assert _mlc_errors(paired["document"], tmp_path)[0] == []


def _parquet_doc(tmp_path, columns, includes, table="t"):
    """A document from from_schema-style typing of ``columns`` [(name, duckdb type)] read
    from a FileSet with ``includes``; returns the path of metadata.json in tmp_path."""
    fields = []
    for name, ctype in columns:
        fields.append(_field_model_like(name, core.duckdb_type_to_croissant(ctype), table))
    model = {
        "name": _leaves("T"), "description": _leaves("t"),
        "license": [_leaves("https://creativecommons.org/licenses/by/4.0/")],
        "creator": [{"name": _leaves("MESA"), "type": _leaves("Organization")}],
        "datePublished": _leaves("2026-11-15"),
        "isLiveDataset": _leaves("true"),
        "distribution": [{"id": _leaves("fs"), "type": _leaves("FileSet"),
                          "encodingFormat": _leaves("application/x-parquet"),
                          "includes": [{"value": i, "unit": ""} for i in includes]}],
        "recordSet": [{"id": _leaves(table), "field": fields}],
    }
    out = core.to_jsonld(core.model_to_avus(model),
                         item={"path": "/iplant/home/shared/x", "name": "x",
                               "keyword_style": "text"})
    assert out["validation"]["errors"] == []
    assert not any("mlcroissant 1.1.1" in w["message"] for w in out["validation"]["warnings"])
    doc = out["document"]
    for d in doc["distribution"]:
        d.pop("contentUrl", None)
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _field_model_like(name, typed, parent):
    """What mesa_croissant_from_schema writes for one column (one level of subFields)."""
    f = {"name": _leaves(name), "source": _leaves("fs"), "column": _leaves(name)}
    if typed.get("dataTypes"):
        f["dataType"] = [_leaves(t) for t in typed["dataTypes"]]
    if typed["isArray"]:
        f["isArray"] = _leaves("true")
        f["arrayShape"] = _leaves(typed["arrayShape"])
    if typed.get("subFields"):
        f["subField"] = [
            {"name": _leaves(s["name"]),
             "dataType": [_leaves(t) for t in s["dataTypes"]],
             "transformJsonPath": _leaves(s.get("transformJsonPath") or s["name"])}
            for s in typed["subFields"]
        ]
    return f


def _write_tables(pa, pq, root, layout):
    """One table with the column kinds the NEON DuckLake has, written the way DuckDB
    writes them (GEOMETRY as WKB, JSON as text, TIMESTAMPTZ in UTC, MAP, LIST), flat
    (<t>/x.parquet) or hive-partitioned (<t>/k=a/x.parquet)."""
    import datetime

    wkb = bytes.fromhex("0101000000000000000000f03f0000000000000040")
    tz = datetime.UTC
    table = pa.table({
        "id": pa.array([1, 2, 3], pa.int64()),
        "props": pa.array([[("a", "1"), ("b", "2")], [], [("c", "3")]],
                          pa.map_(pa.string(), pa.string())),
        "geometry": pa.array([wkb, wkb, None], pa.binary()),
        "doc": pa.array(['{"a": 1}', "[1, 2]", None], pa.string()),
        "seen": pa.array([datetime.datetime(2024, 1, 1, 12, tzinfo=tz)] * 3,
                         pa.timestamp("us", tz="UTC")),
        "tags": pa.array([["x"], [], ["y", "z"]], pa.list_(pa.string())),
    })
    folder = root / "data" / "main" / "t"
    if layout == "flat":
        folder.mkdir(parents=True)
        pq.write_table(table, folder / "ducklake-0001.parquet")
    else:
        for k, rows in (("a", [0, 1]), ("b", [2])):
            (folder / f"k={k}").mkdir(parents=True)
            pq.write_table(table.take(rows), folder / f"k={k}" / "ducklake-0001.parquet")


_COLUMNS = [("id", "BIGINT"), ("props", "MAP(VARCHAR, VARCHAR)"), ("geometry", "GEOMETRY"),
            ("doc", "JSON"), ("seen", "TIMESTAMP WITH TIME ZONE"), ("tags", "VARCHAR[]")]


@pytest.mark.parametrize("layout", ["flat", "partitioned"])
def test_suggested_ducklake_fileset_and_type_map_load_parquet_records(tmp_path, layout):
    """suggest_distribution's includes find the files of an unpartitioned and of a
    partitioned table once each, and the section-9 typing of MAP, GEOMETRY, JSON,
    TIMESTAMPTZ and LIST columns loads their records."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    _write_tables(pa, pq, tmp_path, layout)
    suggested = core.suggest_distribution([], lake={"catalog": "c.ducklake", "data": "data",
                                                    "tables": ["main.t"]})
    includes = [i["value"] for i in suggested[1]["includes"]]
    assert includes == ["data/main/t/*.parquet", "data/main/t/**/*.parquet"]
    path = _parquet_doc(tmp_path, _COLUMNS, includes)
    records = sorted(mlc.Dataset(jsonld=str(path)).records("t"), key=lambda r: int(r["t/id"]))
    assert [int(r["t/id"]) for r in records] == [1, 2, 3]
    first = records[0]
    assert [(m["t/props/key"], m["t/props/value"]) for m in first["t/props"]] == [
        (b"a", b"1"), (b"b", b"2")]
    assert first["t/geometry"] == bytes.fromhex("0101000000000000000000f03f0000000000000040")
    assert first["t/doc"] == b'{"a": 1}'
    assert str(first["t/seen"]).startswith("2024-01-01 12:00:00")
    assert list(first["t/tags"]) == [b"x"]


def test_recursive_glob_alone_misses_an_unpartitioned_table(tmp_path):
    """Why the suggestion has two includes: mlcroissant matches with fnmatch, where
    '<table>/**/*.parquet' needs a sub-folder."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    _write_tables(pa, pq, tmp_path, "flat")
    path = _parquet_doc(tmp_path, [("id", "BIGINT")], ["data/main/t/**/*.parquet"])
    assert list(mlc.Dataset(jsonld=str(path)).records("t")) == []
