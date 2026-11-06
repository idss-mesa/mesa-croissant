"""Tests for mesa_croissant.core (AVU convention version 1)."""

from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path

import pytest
from conftest import (
    FIXTURES,
    IMPORTS,
    UPSTREAM,
    golden_paths,
    import_paths,
    load_json,
    upstream_paths,
)

from mesa_croissant import core
from mesa_croissant.core import (
    CONFORMS_CORE,
    CONFORMS_GEO,
    CONFORMS_RAI,
    crosswalk,
    duckdb_type_to_croissant,
    encoding_format_for,
    from_jsonld,
    is_croissant_attr,
    key_catalog,
    model_to_avus,
    parse_avus,
    suggest_distribution,
    to_jsonld,
    validate,
)

FOLDER = {"path": "/iplant/home/shared/mesa/demo", "name": "demo", "is_folder": True}
PRIVATE = {"path": "/iplant/home/jdoe/private set", "name": "private set", "is_folder": True}


def A(attr, value, unit=""):
    return {"attribute": attr, "value": value, "unit": unit}


def triples(avus):
    return [(a["attribute"], a["value"], a["unit"]) for a in avus]


def messages(items):
    return [(m["path"], m["message"]) for m in items]


def has_msg(items, path=None, contains=""):
    return any((path is None or m["path"] == path) and contains in m["message"] for m in items)


MINIMAL = [
    A("croissant.name", "Minimal"),
    A("croissant.description", "A minimal dataset."),
    A("croissant.license.1", "https://creativecommons.org/licenses/by/4.0/"),
    A("croissant.creator.1.name", "Jane Doe"),
    A("croissant.datePublished", "2026-11-06"),
    A("croissant.distribution.1.id", "f"),
    A("croissant.distribution.1.contentUrl", "f.csv"),
    A("croissant.distribution.1.sha256", "ab" * 32),
    A("croissant.recordSet.1.id", "rs"),
    A("croissant.recordSet.1.field.1.name", "x"),
    A("croissant.recordSet.1.field.1.dataType", "sc:Integer"),
    A("croissant.recordSet.1.field.1.source", "f"),
    A("croissant.recordSet.1.field.1.column", "x"),
]


def gen(avus, item=None):
    return to_jsonld(avus, item=item or FOLDER)


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModule:
    def test_versions_and_uris(self):
        assert core.CONVENTION_VERSION == 1
        assert core.CORE_VERSION == "1.3.0"
        assert core.PREFIX == "croissant."
        assert CONFORMS_CORE == "http://mlcommons.org/croissant/1.1"
        assert CONFORMS_RAI == "http://mlcommons.org/croissant/RAI/1.0"
        assert CONFORMS_GEO == "http://mlcommons.org/croissant/geo/1.0"

    def test_context(self):
        ctx = core.CONTEXT
        assert ctx["@vocab"] == ctx["sc"] == "https://schema.org/"
        assert ctx["cr"] == "http://mlcommons.org/croissant/"
        assert ctx["rai"] == "http://mlcommons.org/croissant/RAI/"
        assert ctx["geocr"] == "http://mlcommons.org/croissant/geo/"
        assert ctx["prov"] == "http://www.w3.org/ns/prov#"
        assert ctx["dct"] == "http://purl.org/dc/terms/"
        assert ctx["dataType"] == {"@id": "cr:dataType", "@type": "@vocab"}
        assert ctx["data"] == {"@id": "cr:data", "@type": "@json"}
        for term in (
            "arrayShape",
            "isArray",
            "readLines",
            "unArchive",
            "value",
            "annotation",
            "equivalentProperty",
            "excludes",
            "sdVersion",
            "subField",
        ):
            assert ctx[term].startswith("cr:")

    def test_document_context_is_a_copy(self):
        doc = gen(MINIMAL)["document"]
        doc["@context"]["sc"] = "changed"
        assert core.CONTEXT["sc"] == "https://schema.org/"

    def test_docstring_says_vendored(self):
        doc = core.__doc__
        assert "portal/services/croissant_core.py" in doc
        assert "byte-identical" in doc
        assert "Do not edit it there" in doc

    def test_stdlib_only(self):
        tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        assert mods <= set(sys.stdlib_module_names) | {"__future__"}

    def test_package_exports(self):
        import mesa_croissant

        for name in (
            "parse_avus",
            "model_to_avus",
            "crosswalk",
            "to_jsonld",
            "from_jsonld",
            "validate",
            "encoding_format_for",
            "duckdb_type_to_croissant",
            "suggest_distribution",
            "is_croissant_attr",
            "CONTEXT",
        ):
            assert getattr(mesa_croissant, name) is getattr(core, name)
        assert mesa_croissant.__version__ == core.CORE_VERSION


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


class TestIsCroissantAttr:
    @pytest.mark.parametrize(
        "attr,ok",
        [
            ("croissant.name", True),
            ("croissant.recordSet.1.field.2.dataType", True),
            ("croissant.", False),
            ("croissant", False),
            ("Croissant.name", False),
            ("datacite.title", False),
            ("croissantx.name", False),
            (None, False),
            (5, False),
        ],
    )
    def test_cases(self, attr, ok):
        assert is_croissant_attr(attr) is ok


class TestParseAvus:
    def test_demo_model_shape(self):
        demo = load_json(FIXTURES / "golden_demo_folder.json")
        p = parse_avus(demo["avus"])
        m = p["model"]
        assert p["unknown"] == [] and p["warnings"] == []
        assert m["name"] == {"value": "Demo ratings", "unit": ""}
        assert m["license"] == [
            {"value": "https://creativecommons.org/licenses/by/4.0/", "unit": ""}
        ]
        assert m["creator"][0]["identifier"]["value"].startswith("https://orcid.org/")
        assert m["keyword"] == [{"value": "grassland", "unit": "ENVO:00000106"}]
        field = m["recordSet"][0]["field"][0]
        assert field["dataType"] == [{"value": "sc:Integer", "unit": ""}]
        assert field["source"]["value"] == "ratings-csv"

    def test_only_croissant_avus_are_read(self):
        p = parse_avus([A("datacite.title", "x"), A("ipc_UUID", "u"), A("croissant.name", "n")])
        assert p["model"] == {"name": {"value": "n", "unit": ""}}
        assert p["unknown"] == []

    def test_input_shapes(self):
        p = parse_avus(
            [
                {"attr": "croissant.name", "value": "n"},
                ("croissant.description", "d"),
                ("croissant.keyword.1", "k", "ENVO:1"),
                ["croissant.version", 2, None],
                {"attribute": "croissant.isLiveDataset", "value": True, "unit": None},
                "garbage",
                {"attribute": 5, "value": "x"},
            ]
        )
        m = p["model"]
        assert m["name"]["value"] == "n"
        assert m["description"]["value"] == "d"
        assert m["keyword"][0] == {"value": "k", "unit": "ENVO:1"}
        assert m["version"] == {"value": "2", "unit": ""}
        assert m["isLiveDataset"]["value"] == "true"

    def test_unindexed_repeatables(self):
        p = parse_avus(
            [
                A("croissant.license", "https://a"),
                A("croissant.creator.name", "Jane"),
                A("croissant.creator.email", "j@x.org"),
                A("croissant.recordSet.id", "rs"),
                A("croissant.recordSet.field.name", "f"),
                A("croissant.distribution.includes", "data/*.csv"),
            ]
        )
        m = p["model"]
        assert m["license"] == [{"value": "https://a", "unit": ""}]
        assert m["creator"] == [
            {"name": {"value": "Jane", "unit": ""}, "email": {"value": "j@x.org", "unit": ""}}
        ]
        assert m["recordSet"][0]["field"][0]["name"]["value"] == "f"
        assert m["distribution"][0]["includes"][0]["value"] == "data/*.csv"
        assert triples(model_to_avus(m))[0] == ("croissant.license.1", "https://a", "")

    def test_unindexed_and_index_one_are_the_same_path(self):
        p = parse_avus([A("croissant.license", "https://a"), A("croissant.license.1", "https://b")])
        assert [v["value"] for v in p["model"]["license"]] == ["https://a"]
        assert has_msg(p["warnings"], "croissant.license.1", "duplicate")

    def test_gaps_and_numeric_sort(self):
        p = parse_avus(
            [
                A("croissant.license.10", "https://ten"),
                A("croissant.license.2", "https://two"),
                A("croissant.license.7", "https://seven"),
                A("croissant.creator.5.name", "E"),
                A("croissant.creator.3.name", "C"),
            ]
        )
        assert [v["value"] for v in p["model"]["license"]] == [
            "https://two",
            "https://seven",
            "https://ten",
        ]
        assert [c["name"]["value"] for c in p["model"]["creator"]] == ["C", "E"]
        out = triples(model_to_avus(p["model"]))
        assert out == [
            ("croissant.license.1", "https://two", ""),
            ("croissant.license.2", "https://seven", ""),
            ("croissant.license.3", "https://ten", ""),
            ("croissant.creator.1.name", "C", ""),
            ("croissant.creator.2.name", "E", ""),
        ]

    def test_leading_zero_index(self):
        p = parse_avus(
            [A("croissant.license.01", "https://a"), A("croissant.license.1", "https://b")]
        )
        assert [v["value"] for v in p["model"]["license"]] == ["https://a"]
        assert has_msg(p["warnings"], "croissant.license.1", "duplicate")

    def test_index_zero_is_ignored_but_kept(self):
        p = parse_avus(
            [
                A("croissant.license.0", "https://zero"),
                A("croissant.creator.0.name", "Z"),
                A("croissant.license.1", "https://one"),
            ]
        )
        assert [v["value"] for v in p["model"]["license"]] == ["https://one"]
        assert "creator" not in p["model"]
        assert triples(p["unknown"]) == [
            ("croissant.license.0", "https://zero", ""),
            ("croissant.creator.0.name", "Z", ""),
        ]
        assert has_msg(p["warnings"], "croissant.license.0", "index 0")

    def test_duplicate_scalar_keeps_first(self):
        p = parse_avus([A("croissant.name", "first"), A("croissant.name", "second")])
        assert p["model"]["name"]["value"] == "first"
        assert messages(p["warnings"]) == [
            ("croissant.name", "duplicate value for a single-valued path; the first one is shown, "
                               "the other is kept on save; delete it in the Raw tab to resolve")
        ]
        # kept for editors: a save re-emits it instead of deleting it
        assert p["unknown"] == [A("croissant.name", "second")]

    def test_duplicate_index_and_variant_spelling_are_kept(self):
        p = parse_avus([A("croissant.license", "L0"), A("croissant.license.1", "L1")])
        # kept on the value it duplicates (rule 6), not in the flat unknown list
        assert p["model"]["license"] == [
            {"value": "L0", "unit": "", "__dup": [{"value": "L1", "unit": ""}]}]
        assert p["unknown"] == []
        assert triples(model_to_avus(p["model"])) == [
            ("croissant.license.1", "L0", ""), ("croissant.license.1", "L1", "")]

    def test_duplicate_nested_scalar(self):
        p = parse_avus(
            [
                A("croissant.recordSet.1.field.1.name", "a"),
                A("croissant.recordSet.1.field.1.name", "b"),
            ]
        )
        assert p["model"]["recordSet"][0]["field"][0]["name"]["value"] == "a"
        assert has_msg(p["warnings"], "croissant.recordSet.1.field.1.name", "duplicate")

    @pytest.mark.parametrize(
        "attr",
        [
            "croissant.colour",
            "croissant.name.1",
            "croissant.creator.1",
            "croissant.license.1.url",
            "croissant.license.x",
            "croissant.rai.dataCollectionTimeFrame.1",
            "croissant.geo.box",
            "croissant.geo.spatialCoverage",
            "croissant.name..x",
            "croissant..name",
        ],
    )
    def test_unknown_keys_kept(self, attr):
        avu = A(attr, "v", "u")
        p = parse_avus([avu, A("croissant.name", "n")])
        assert p["unknown"] == [avu]
        assert has_msg(p["warnings"], attr, "unknown Croissant key")
        assert p["model"] == {"name": {"value": "n", "unit": ""}}

    @pytest.mark.parametrize(
        "attr, path, suffix",
        [
            ("croissant.creator.1.orcid", ("creator", 0), "orcid"),
            ("croissant.recordSet.1.field.1.transform", ("recordSet", 0, "field", 0), "transform"),
            ("croissant.distribution.1.containedIn.a", ("distribution", 0), "containedIn.a"),
        ],
    )
    def test_unknown_keys_below_an_entity_are_kept_with_it(self, attr, path, suffix):
        avu = A(attr, "v", "u")
        p = parse_avus([avu, A("croissant.name", "n")])
        assert p["unknown"] == []
        assert has_msg(p["warnings"], attr, "unknown Croissant key")
        node = p["model"]
        for step in path:
            node = node[step]
        assert node["__unknown"] == [{"suffix": suffix, "value": "v", "unit": "u"}]
        assert avu in model_to_avus(p["model"])

    def test_entity_unknown_keys_follow_the_entity_when_renumbered(self):
        p = parse_avus([
            A("croissant.creator.2.name", "Ana"),
            A("croissant.creator.2.orcidVerified", "yes"),
            A("croissant.creator.5.name", "Bo"),
            A("croissant.creator.5.orcidVerified", "no", "u"),
            A("croissant.recordSet.3.name", "rs"),
            A("croissant.recordSet.3.field.4.name", "f"),
            A("croissant.recordSet.3.field.4.transform", "t"),
        ])
        assert p["unknown"] == []
        out = triples(model_to_avus(p["model"]))
        assert ("croissant.creator.1.orcidVerified", "yes", "") in out
        assert ("croissant.creator.2.orcidVerified", "no", "u") in out
        assert ("croissant.recordSet.1.field.1.transform", "t", "") in out
        # removing an entity removes its unknown keys; the next one takes its index
        del p["model"]["creator"][0]
        out = triples(model_to_avus(p["model"]))
        assert [t for t in out if t[0].startswith("croissant.creator.")] == [
            ("croissant.creator.1.name", "Bo", ""),
            ("croissant.creator.1.orcidVerified", "no", "u"),
        ]

    def test_entity_with_only_unknown_keys_keeps_its_index_but_is_not_emitted(self):
        avus = MINIMAL + [
            A("croissant.creator.2.orcidVerified", "yes"),
            A("croissant.creator.3.name", "Ana"),
            A("croissant.distribution.2.checksumAlgo", "crc32"),
        ]
        p = parse_avus(avus)
        assert p["model"]["creator"][1] == {
            "__unknown": [{"suffix": "orcidVerified", "value": "yes", "unit": ""}]}
        out = triples(model_to_avus(p["model"]))
        assert ("croissant.creator.2.orcidVerified", "yes", "") in out
        assert ("croissant.creator.3.name", "Ana", "") in out
        assert ("croissant.distribution.2.checksumAlgo", "crc32", "") in out
        res = gen(avus)
        doc = res["document"]
        assert "__unknown" not in json.dumps(res)
        assert [c.get("name") for c in core._as_list(doc.get("creator"))] == ["Jane Doe", "Ana"]
        assert len(core._as_list(doc.get("distribution"))) == 1
        assert res["validation"]["valid"]

    def test_duplicate_below_an_entity_is_kept_with_it(self):
        p = parse_avus([A("croissant.creator.1.name", "first"),
                        A("croissant.creator.1.name", "second"),
                        A("croissant.recordSet.1.field.1.dataType.1", "sc:Text"),
                        A("croissant.recordSet.1.field.1.dataType.1", "sc:Integer")])
        assert p["unknown"] == []
        assert p["model"]["creator"][0]["name"]["value"] == "first"
        assert p["model"]["creator"][0]["__unknown"] == [
            {"suffix": "name", "value": "second", "unit": ""}]
        assert has_msg(p["warnings"], "croissant.creator.1.name", "kept on save")
        assert has_msg(p["warnings"], "croissant.recordSet.1.field.1.dataType.1", "kept on save")
        assert not has_msg(p["warnings"], "croissant.creator.1.name", "unknown Croissant key")
        out = model_to_avus(p["model"])
        assert A("croissant.creator.1.name", "second") in out
        # a field's single dataType is written unindexed, and its duplicate with it
        assert A("croissant.recordSet.1.field.1.dataType", "sc:Integer") in out
        # stable: a save of the parsed model parses back to the same model
        assert parse_avus(out)["model"] == p["model"]

    # -- a duplicate list index follows the value it duplicates (rule 6, ROADMAP item 10)

    @staticmethod
    def _save(avus):
        p = parse_avus(avus)
        return p, model_to_avus(p["model"]) + p["unknown"]

    def _assert_fixed_point(self, saved):
        p2, again = self._save(saved)
        assert triples(again) == triples(saved)
        assert has_msg(p2["warnings"], None, "duplicate value for the same index")
        return p2

    def test_duplicate_after_a_gap_stays_a_duplicate(self):
        p, saved = self._save([A("croissant.license.2", "A"), A("croissant.license.2", "B")])
        assert [lf["value"] for lf in p["model"]["license"]] == ["A"]
        assert p["unknown"] == []
        assert has_msg(p["warnings"], "croissant.license.2", "kept on save")
        assert triples(saved) == [("croissant.license.1", "A", ""),
                                  ("croissant.license.1", "B", "")]
        again = self._assert_fixed_point(saved)
        assert [lf["value"] for lf in again["model"]["license"]] == ["A"]

    def test_duplicate_after_a_gap_keeps_its_neighbour(self):
        p, saved = self._save([A("croissant.license.2", "A"), A("croissant.license.2", "B"),
                               A("croissant.license.3", "C")])
        assert [lf["value"] for lf in p["model"]["license"]] == ["A", "C"]
        assert triples(saved) == [("croissant.license.1", "A", ""),
                                  ("croissant.license.1", "B", ""),
                                  ("croissant.license.2", "C", "")]
        self._assert_fixed_point(saved)

    @pytest.mark.parametrize("path", ["keyword", "rai.dataBiases",
                                      "geo.bandConfiguration.band",
                                      "distribution.1.includes"])
    def test_duplicate_after_a_gap_in_every_list(self, path):
        stem = core.PREFIX + path
        _, saved = self._save([A(f"{stem}.1", "one"), A(f"{stem}.3", "two"),
                               A(f"{stem}.3", "dup")])
        assert triples(saved)[-3:] == [(f"{stem}.1", "one", ""), (f"{stem}.2", "two", ""),
                                       (f"{stem}.2", "dup", "")]
        self._assert_fixed_point(saved)

    def test_duplicate_list_index_inside_an_entity_follows_its_value(self):
        p, saved = self._save([A("croissant.recordSet.1.id", "rs"),
                               A("croissant.recordSet.1.key.2", "k1"),
                               A("croissant.recordSet.1.key.2", "k2")])
        assert "__unknown" not in p["model"]["recordSet"][0]
        assert triples(saved) == [("croissant.recordSet.1.id", "rs", ""),
                                  ("croissant.recordSet.1.key.1", "k1", ""),
                                  ("croissant.recordSet.1.key.1", "k2", "")]
        self._assert_fixed_point(saved)

    def test_duplicate_field_data_type_after_a_gap(self):
        base = [A("croissant.recordSet.1.id", "rs"), A("croissant.recordSet.1.field.1.name", "f")]
        _, saved = self._save(base + [A("croissant.recordSet.1.field.1.dataType.2", "sc:Text"),
                                      A("croissant.recordSet.1.field.1.dataType.2", "sc:Integer")])
        assert triples(saved)[2:] == [("croissant.recordSet.1.field.1.dataType", "sc:Text", ""),
                                      ("croissant.recordSet.1.field.1.dataType", "sc:Integer", "")]
        again = self._assert_fixed_point(saved)
        assert [lf["value"] for lf in again["model"]["recordSet"][0]["field"][0]["dataType"]] \
            == ["sc:Text"]
        # an unindexed dataType and dataType.1 are the same path: still one type
        _, saved = self._save(base + [A("croissant.recordSet.1.field.1.dataType", "sc:Text"),
                                      A("croissant.recordSet.1.field.1.dataType.1", "sc:Integer")])
        self._assert_fixed_point(saved)

    def test_removing_a_value_removes_its_duplicates(self):
        p = parse_avus([A("croissant.license.1", "A"), A("croissant.license.2", "B"),
                        A("croissant.license.2", "C")])
        del p["model"]["license"][0]
        saved = model_to_avus(p["model"]) + p["unknown"]
        assert triples(saved) == [("croissant.license.1", "B", ""),
                                  ("croissant.license.1", "C", "")]
        assert [lf["value"] for lf in parse_avus(saved)["model"]["license"]] == ["B"]
        del p["model"]["license"][0]
        assert model_to_avus(p["model"]) == []

    def test_only_the_first_value_reaches_the_document(self):
        out = gen(MINIMAL + [A("croissant.keyword.2", "soil"), A("croissant.keyword.2", "hidden")])
        assert "__dup" not in json.dumps(out)
        assert "hidden" not in json.dumps(out["document"])
        assert "soil" in json.dumps(out["document"]["keywords"])

    def test_malformed_dup_entries_are_not_written(self):
        model = {"license": [{"value": "A", "unit": "", "__dup": [
            {"value": "ok", "unit": "u"}, {"value": "  "}, {"unit": "x"}, "junk", None,
        ]}, {"value": "", "__dup": [{"value": "orphan"}]}, {"value": "B", "__dup": "x"}]}
        assert triples(model_to_avus(model)) == [
            ("croissant.license.1", "A", ""), ("croissant.license.1", "ok", "u"),
            ("croissant.license.2", "B", "")]

    def test_malformed_entity_unknown_entries_are_not_written(self):
        model = {"creator": [{"name": "Ana", "__unknown": [
            {"suffix": "ok", "value": "v"},
            {"suffix": "", "value": "v"},
            {"suffix": "a..b", "value": "v"},
            {"suffix": "blank", "value": "  "},
            {"suffix": 3, "value": "v"},
            "not a mapping",
        ]}]}
        assert triples(model_to_avus(model)) == [
            ("croissant.creator.1.name", "Ana", ""),
            ("croissant.creator.1.ok", "v", ""),
        ]

    def test_unknown_keys_not_emitted_and_reported(self):
        out = gen(MINIMAL + [A("croissant.colour", "blue")])
        assert "colour" not in json.dumps(out["document"])
        assert has_msg(out["validation"]["warnings"], "croissant.colour", "unknown")
        assert out["validation"]["valid"]

    def test_empty_value_ignored(self):
        p = parse_avus([A("croissant.name", ""), A("croissant.description", "d")])
        assert "name" not in p["model"]
        assert has_msg(p["warnings"], "croissant.name", "empty value")
        assert p["unknown"] == []

    def test_unicode(self):
        avus = [
            A("croissant.name", "Données sur les prairies — \U0001f33e"),
            A("croissant.creator.1.name", "José Álvarez"),
            A("croissant.keyword.1", "pâturage", "ENVO:00000106"),
        ]
        p = parse_avus(avus)
        assert triples(model_to_avus(p["model"])) == triples(avus)
        doc = gen(avus)["document"]
        assert doc["name"] == avus[0]["value"]
        back = from_jsonld(json.loads(json.dumps(doc, ensure_ascii=False)))
        assert ("croissant.name", avus[0]["value"], "") in triples(back["avus"])

    def test_limits_warn(self):
        p = parse_avus(
            [A("croissant.description", "x" * 2701), A("croissant.keyword.1", "k", "U" * 251)]
        )
        assert has_msg(p["warnings"], "croissant.description", "longer than 2700")
        assert has_msg(p["warnings"], "croissant.keyword.1", "longer than 250")
        assert p["model"]["description"]["value"] == "x" * 2701
        ok = parse_avus([A("croissant.description", "x" * 2700)])
        assert ok["warnings"] == []

    def test_field_datatype_single_and_many(self):
        p = parse_avus(
            [
                A("croissant.recordSet.1.id", "rs"),
                A("croissant.recordSet.1.field.1.name", "a"),
                A("croissant.recordSet.1.field.1.dataType", "sc:Text"),
                A("croissant.recordSet.1.field.2.name", "b"),
                A("croissant.recordSet.1.field.2.dataType.2", "wd:Q48277"),
                A("croissant.recordSet.1.field.2.dataType.1", "sc:URL"),
            ]
        )
        f1, f2 = p["model"]["recordSet"][0]["field"]
        assert [d["value"] for d in f1["dataType"]] == ["sc:Text"]
        assert [d["value"] for d in f2["dataType"]] == ["sc:URL", "wd:Q48277"]
        out = triples(model_to_avus(p["model"]))
        assert ("croissant.recordSet.1.field.1.dataType", "sc:Text", "") in out
        assert ("croissant.recordSet.1.field.2.dataType.1", "sc:URL", "") in out
        assert ("croissant.recordSet.1.field.2.dataType.2", "wd:Q48277", "") in out

    def test_subfields(self):
        avus = [
            A("croissant.recordSet.1.id", "rs"),
            A("croissant.recordSet.1.field.1.name", "s"),
            A("croissant.recordSet.1.field.1.subField.1.name", "a"),
            A("croissant.recordSet.1.field.1.subField.1.dataType", "sc:Text"),
            A("croissant.recordSet.1.field.1.subField.2.name", "b"),
            A("croissant.recordSet.1.field.1.subField.2.subField.1.name", "deep"),
        ]
        p = parse_avus(avus)
        s = p["model"]["recordSet"][0]["field"][0]
        assert [x["name"]["value"] for x in s["subField"]] == ["a", "b"]
        assert s["subField"][1]["subField"][0]["name"]["value"] == "deep"
        assert triples(model_to_avus(p["model"])) == triples(avus)

    @pytest.mark.parametrize("path", golden_paths(), ids=lambda p: p.stem)
    def test_round_trip_golden_avus(self, path):
        fixture = load_json(path)
        croissant = [a for a in fixture["avus"] if is_croissant_attr(a["attribute"])]
        p = parse_avus(fixture["avus"])
        assert p["unknown"] == [] and p["warnings"] == []
        assert triples(model_to_avus(p["model"])) == triples(croissant)


class TestModelToAvus:
    def test_bare_scalars_and_order(self):
        model = {
            "recordSet": [{"id": "rs", "field": [{"name": "x", "dataType": "sc:Text"}]}],
            "name": "N",
            "license": ["https://a", {"value": "https://b", "unit": ""}],
            "isLiveDataset": True,
            "geo": {"spatialResolution": {"value": 30, "unit": "m"}},
        }
        assert triples(model_to_avus(model)) == [
            ("croissant.name", "N", ""),
            ("croissant.license.1", "https://a", ""),
            ("croissant.license.2", "https://b", ""),
            ("croissant.isLiveDataset", "true", ""),
            ("croissant.geo.spatialResolution", "30", "m"),
            ("croissant.recordSet.1.id", "rs", ""),
            ("croissant.recordSet.1.field.1.name", "x", ""),
            ("croissant.recordSet.1.field.1.dataType", "sc:Text", ""),
        ]

    def test_empty_entities_do_not_take_an_index(self):
        model = {
            "creator": [{}, {"name": ""}, {"name": "B"}, "not-a-dict"],
            "license": ["", None, "https://x"],
        }
        assert triples(model_to_avus(model)) == [
            ("croissant.license.1", "https://x", ""),
            ("croissant.creator.1.name", "B", ""),
        ]

    def test_unknown_model_keys_ignored(self):
        assert model_to_avus({"colour": "x", "rai": {"nope": "x"}}) == []
        assert model_to_avus({}) == []
        assert model_to_avus(None) == []

    def test_every_key_round_trips(self):
        rows = [r for r in key_catalog() if "<sub>" not in r["attribute"]]
        avus = []
        for i, row in enumerate(rows):
            attr = row["attribute"].replace(".N", ".1").replace(".M", ".1").replace(".K", ".1")
            if attr.endswith("field.1.dataType.1"):
                attr = attr[:-2]
            unit = "ENVO:1" if attr == "croissant.keyword.1" else ""
            avus.append(A(attr, f"v{i}", unit))
        p = parse_avus(avus)
        assert p["unknown"] == [], p["unknown"]
        assert sorted(triples(model_to_avus(p["model"]))) == sorted(triples(avus))


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class TestGolden:
    def test_golden_documents(self, golden):
        out = to_jsonld(golden["avus"], item=golden["item"])
        assert out["document"] == golden["document"]
        assert out["sources"] == golden["sources"]
        assert out["validation"]["valid"] is golden["valid"]

    def test_demo_matches_convention_section_10(self):
        demo = load_json(FIXTURES / "golden_demo_folder.json")
        doc = to_jsonld(demo["avus"], item=demo["item"])["document"]
        assert list(doc) == [
            "@context",
            "@type",
            "conformsTo",
            "name",
            "description",
            "license",
            "url",
            "creator",
            "datePublished",
            "keywords",
            "distribution",
            "recordSet",
        ]
        assert doc["url"] == (
            "https://mesa.cyverse.org/data/metadata/page/"
            "?path=/iplant/home/shared/mesa/demo&type=folder"
        )
        assert doc["distribution"][0]["contentUrl"] == (
            "https://data.cyverse.org/dav-anon/iplant/home/shared/mesa/demo/ratings.csv"
        )
        assert doc["recordSet"][0]["field"][0] == {
            "@type": "cr:Field",
            "@id": "ratings/user_id",
            "name": "user_id",
            "dataType": "sc:Integer",
            "source": {"fileObject": {"@id": "ratings-csv"}, "extract": {"column": "userId"}},
        }

    @pytest.mark.parametrize("name", ["demo_folder", "rai_geo"])
    def test_round_trip_from_jsonld(self, name):
        fixture = load_json(FIXTURES / f"golden_{name}.json")
        out = to_jsonld(fixture["avus"], item=fixture["item"])
        base = core._content_base(fixture["item"])
        back = from_jsonld(out["document"], content_base=base)
        assert back["errors"] == [] and back["warnings"] == []
        expected = parse_avus(fixture["avus"])["model"]
        # Generated defaults become explicit AVUs; everything else is unchanged.
        for prop, source in out["sources"].items():
            if source != "croissant" and prop == "url":
                expected["url"] = {"value": out["document"]["url"], "unit": ""}
        assert triples(back["avus"]) == triples(model_to_avus(expected))
        if name == "rai_geo":
            assert triples(back["avus"]) == triples(fixture["avus"])

    def test_ducklake_crosswalk_sources(self):
        fixture = load_json(FIXTURES / "golden_ducklake_crosswalk.json")
        assert not any(
            a["attribute"].startswith("croissant.")
            and a["attribute"].split(".")[1] not in ("distribution", "recordSet")
            for a in fixture["avus"]
        )
        s = fixture["sources"]
        assert s["name"] == "datacite.title.1.value"
        assert s["creator"] == "datacite.creator.1.name"
        assert s["license"] == "datacite.rights.1.rightsURI"
        assert s["url"] == s["identifier"] == "datacite.identifier"
        assert s["keywords"] == "datacite.subject.1.value+ontology"
        assert s["spatialCoverage"] == "datacite.geoLocation.1.box"
        assert s["prov:wasGeneratedBy"] == "mesa.generated_by"
        assert s["distribution"] == s["recordSet"] == "croissant"


class TestConformsTo:
    def test_core_only(self):
        assert gen(MINIMAL)["document"]["conformsTo"] == CONFORMS_CORE

    def test_rai_and_geo_added(self):
        doc = gen(
            MINIMAL
            + [
                A("croissant.rai.dataBiases.1", "b"),
                A("croissant.geo.coordinateReferenceSystem", "EPSG:4326"),
            ]
        )["document"]
        assert doc["conformsTo"] == [CONFORMS_CORE, CONFORMS_RAI, CONFORMS_GEO]

    def test_user_values_deduped_and_kept(self):
        out = gen(
            MINIMAL
            + [
                A("croissant.conformsTo.1", CONFORMS_CORE),
                A("croissant.conformsTo.2", "https://bioschemas.org/profiles/Dataset/1.0-RELEASE"),
                A("croissant.conformsTo.3", CONFORMS_GEO),
            ]
        )
        assert out["document"]["conformsTo"] == [
            CONFORMS_CORE,
            CONFORMS_GEO,
            "https://bioschemas.org/profiles/Dataset/1.0-RELEASE",
        ]
        assert out["sources"]["conformsTo"] == "croissant"


class TestUrlAndIdentifier:
    DC = [A("datacite.identifier", "10.1234/abc"), A("datacite.identifierType", "DOI")]

    def test_precedence(self):
        item = dict(FOLDER, url="https://portal/x", doi="doi:10.9999/item")
        out = gen(MINIMAL + self.DC + [A("croissant.url", "https://own")], item)
        assert (out["document"]["url"], out["sources"]["url"]) == ("https://own", "croissant")
        out = gen(MINIMAL + self.DC, item)
        assert (out["document"]["url"], out["sources"]["url"]) == (
            "https://doi.org/10.9999/item",
            "item.doi",
        )
        assert out["document"]["identifier"] == "https://doi.org/10.9999/item"
        out = gen(MINIMAL + self.DC, dict(FOLDER, url="https://portal/x"))
        assert (out["document"]["url"], out["sources"]["url"]) == (
            "https://doi.org/10.1234/abc",
            "datacite.identifier",
        )
        out = gen(MINIMAL, dict(FOLDER, url="https://portal/x"))
        assert (out["document"]["url"], out["sources"]["url"]) == ("https://portal/x", "item.url")
        assert "identifier" not in out["document"]
        out = gen(MINIMAL, PRIVATE)
        assert out["sources"]["url"] == "default"
        assert out["document"]["url"] == (
            "https://mesa.cyverse.org/data/metadata/page/"
            "?path=/iplant/home/jdoe/private%20set&type=folder"
        )

    def test_croissant_identifier_wins(self):
        out = gen(MINIMAL + self.DC + [A("croissant.identifier", "https://hdl.handle.net/x")])
        assert out["document"]["identifier"] == "https://hdl.handle.net/x"
        assert out["sources"]["identifier"] == "croissant"

    def test_file_default_page(self):
        item = {"path": "/iplant/home/shared/a/b.csv", "name": "b.csv", "is_folder": False}
        assert gen(MINIMAL, item)["document"]["url"].endswith(
            "?path=/iplant/home/shared/a/b.csv&type=file"
        )


class TestContentUrls:
    def avus(self, url, extra=()):
        return [
            A("croissant.distribution.1.id", "f"),
            A("croissant.distribution.1.contentUrl", url),
            *extra,
        ]

    def url(self, url, item=FOLDER, extra=()):
        return gen(self.avus(url, extra), item)["document"]["distribution"][0]["contentUrl"]

    def test_public_and_private(self):
        assert self.url("data/x y.csv") == (
            "https://data.cyverse.org/dav-anon/iplant/home/shared/mesa/demo/data/x%20y.csv"
        )
        assert self.url("x.csv", PRIVATE) == (
            "https://data.cyverse.org/dav/iplant/home/jdoe/private%20set/x.csv"
        )

    def test_item_overrides(self):
        assert (
            self.url("x.csv", dict(FOLDER, content_base="https://h/base/"))
            == "https://h/base/x.csv"
        )
        assert self.url("x.csv", dict(PRIVATE, public=True)).startswith(
            "https://data.cyverse.org/dav-anon/"
        )
        assert self.url("x.csv", dict(FOLDER, public=False)).startswith(
            "https://data.cyverse.org/dav/iplant"
        )

    def test_normalisation(self):
        assert self.url("./data/../x.csv").endswith("/mesa/demo/x.csv")
        assert self.url("https://example.org/a.csv") == "https://example.org/a.csv"
        assert self.url("s3://bucket/a.csv") == "s3://bucket/a.csv"
        assert self.url("/iplant/home/shared/other/a.csv") == (
            "https://data.cyverse.org/dav-anon/iplant/home/shared/other/a.csv"
        )

    def test_contained_in_stays_relative(self):
        assert (
            self.url("inner/a.csv", extra=[A("croissant.distribution.1.containedIn.1", "zip")])
            == "inner/a.csv"
        )

    def test_file_item_base_is_parent(self):
        item = {"path": "/iplant/home/shared/p/f.csv", "name": "f.csv", "is_folder": False}
        assert core._content_base(item) == "https://data.cyverse.org/dav-anon/iplant/home/shared/p"


class TestDistribution:
    def test_defaults_and_guesses(self):
        doc = gen(
            [
                A("croissant.distribution.1.id", "a"),
                A("croissant.distribution.1.contentUrl", "data/a.parquet"),
                A("croissant.distribution.1.contentSize", "100"),
                A("croissant.distribution.1.md5", "0" * 32),
                A("croissant.distribution.2.id", "b"),
                A("croissant.distribution.2.type", "FileSet"),
                A("croissant.distribution.2.includes.1", "img/*.tif"),
                A("croissant.distribution.2.includes.2", "img/*.tiff"),
                A("croissant.distribution.2.containedIn.1", "a"),
                A("croissant.distribution.3.id", "c"),
                A("croissant.distribution.3.contentUrl", "c.nc"),
                A("croissant.distribution.3.contentSize", "4.7", "MB"),
            ]
        )["document"]
        a, b, c = doc["distribution"]
        assert a["encodingFormat"] == "application/x-parquet"
        assert a["name"] == "a" and a["contentSize"] == "100 B"
        assert b == {
            "@type": "cr:FileSet",
            "@id": "b",
            "name": "b",
            "encodingFormat": "image/tiff",
            "containedIn": {"@id": "a"},
            "includes": ["img/*.tif", "img/*.tiff"],
        }
        assert c["encodingFormat"] == "application/x-netcdf" and c["contentSize"] == "4.7 MB"

    def test_misplaced_properties_warn(self):
        out = gen(
            [
                A("croissant.distribution.1.id", "a"),
                A("croissant.distribution.1.type", "FileSet"),
                A("croissant.distribution.1.contentUrl", "x.csv"),
                A("croissant.distribution.1.includes.1", "*.csv"),
                A("croissant.distribution.2.id", "b"),
                A("croissant.distribution.2.type", "Folder"),
                A("croissant.distribution.2.contentUrl", "b.csv"),
                A("croissant.distribution.2.includes.1", "*.csv"),
            ]
        )
        w = out["validation"]["warnings"]
        assert has_msg(w, "croissant.distribution.1.contentUrl", "FileObject only")
        assert has_msg(w, "croissant.distribution.2.type", "unknown distribution type")
        assert has_msg(w, "croissant.distribution.2.includes", "FileSet only")
        assert out["document"]["distribution"][1]["@type"] == "cr:FileObject"


class TestRecordSets:
    BASE = [
        A("croissant.distribution.1.id", "csv"),
        A("croissant.distribution.1.contentUrl", "a.csv"),
        A("croissant.distribution.2.id", "files"),
        A("croissant.distribution.2.type", "FileSet"),
        A("croissant.distribution.2.includes.1", "*.jpg"),
        A("croissant.recordSet.1.id", "base"),
        A("croissant.recordSet.1.field.1.name", "id"),
        A("croissant.recordSet.1.field.1.dataType", "Integer"),
        A("croissant.recordSet.1.field.1.source", "csv"),
        A("croissant.recordSet.1.field.1.column", "id"),
    ]

    def fields(self, extra):
        doc = gen(self.BASE + extra)["document"]
        return doc, doc["recordSet"][-1]["field"]

    def test_ids_types_and_sources(self):
        doc, fields = self.fields(
            [
                A("croissant.recordSet.2.id", "img"),
                A("croissant.recordSet.2.name", "Images"),
                A("croissant.recordSet.2.dataType", "Split"),
                A("croissant.recordSet.2.key.1", "img/path"),
                A("croissant.recordSet.2.field.1.name", "path"),
                A("croissant.recordSet.2.field.1.dataType", "Text"),
                A("croissant.recordSet.2.field.1.source", "files"),
                A("croissant.recordSet.2.field.1.fileProperty", "fullpath"),
                A("croissant.recordSet.2.field.1.regex", "^(.*)\\.jpg$"),
                A("croissant.recordSet.2.field.1.separator", "/"),
                A("croissant.recordSet.2.field.2.name", "base_id"),
                A("croissant.recordSet.2.field.2.id", "img/custom"),
                A("croissant.recordSet.2.field.2.dataType", "Int32"),
                A("croissant.recordSet.2.field.2.source", "base"),
                A("croissant.recordSet.2.field.2.column", "id"),
                A("croissant.recordSet.2.field.2.references", "base/id"),
                A("croissant.recordSet.2.field.3.name", "raw"),
                A("croissant.recordSet.2.field.3.dataType", "https://schema.org/Text"),
                A("croissant.recordSet.2.field.3.source", "base"),
                A("croissant.recordSet.2.field.3.column", "missing"),
                A("croissant.recordSet.2.field.4.name", "via_field"),
                A("croissant.recordSet.2.field.4.dataType", "http://mlcommons.org/croissant/Int64"),
                A("croissant.recordSet.2.field.4.source", "base/id"),
                A("croissant.recordSet.2.field.4.format", "0"),
            ]
        )
        rs = doc["recordSet"][1]
        assert rs["name"] == "Images" and rs["dataType"] == "cr:Split"
        assert rs["key"] == {"@id": "img/path"}
        assert doc["recordSet"][0]["field"][0]["dataType"] == "sc:Integer"
        f1, f2, f3, f4 = fields
        assert f1["@id"] == "img/path"
        assert f1["source"] == {
            "fileSet": {"@id": "files"},
            "extract": {"fileProperty": "fullpath"},
            "transform": [{"regex": "^(.*)\\.jpg$"}, {"separator": "/"}],
        }
        assert f2["@id"] == "img/custom" and f2["dataType"] == "cr:Int32"
        assert f2["source"] == {"field": {"@id": "base/id"}}
        assert f2["references"] == {"field": {"@id": "base/id"}}
        assert f3["dataType"] == "sc:Text"
        assert f3["source"] == {"recordSet": {"@id": "base"}, "extract": {"column": "missing"}}
        assert f4["source"] == {"field": {"@id": "base/id"}, "format": "0"}
        assert f4["dataType"] == "cr:Int64"
        errors = gen(self.BASE)["validation"]["errors"]
        assert not [e for e in errors if e["path"].startswith(("recordSet", "distribution"))]

    def test_unknown_source_is_a_validation_error(self):
        out = gen(
            self.BASE
            + [
                A("croissant.recordSet.1.field.2.name", "y"),
                A("croissant.recordSet.1.field.2.dataType", "sc:Text"),
                A("croissant.recordSet.1.field.2.source", "nope"),
            ]
        )
        f = out["document"]["recordSet"][0]["field"][1]
        assert f["source"] == {"fileObject": {"@id": "nope"}}
        assert has_msg(
            out["validation"]["errors"], "recordSet[0].field[1].source", "unknown @id 'nope'"
        )

    def test_extract_without_source(self):
        out = gen(
            self.BASE
            + [
                A("croissant.recordSet.1.field.2.name", "y"),
                A("croissant.recordSet.1.field.2.dataType", "sc:Text"),
                A("croissant.recordSet.1.field.2.column", "y"),
            ]
        )
        assert out["document"]["recordSet"][0]["field"][1]["source"] == {"extract": {"column": "y"}}
        assert has_msg(
            out["validation"]["errors"], "recordSet[0].field[1].source", "must reference"
        )

    def test_arrays_equivalent_subfields(self):
        _, fields = self.fields(
            [
                A("croissant.recordSet.2.id", "nest"),
                A("croissant.recordSet.2.field.1.name", "vec"),
                A("croissant.recordSet.2.field.1.dataType", "cr:Float32"),
                A("croissant.recordSet.2.field.1.source", "csv"),
                A("croissant.recordSet.2.field.1.column", "vec"),
                A("croissant.recordSet.2.field.1.isArray", "TRUE"),
                A("croissant.recordSet.2.field.1.arrayShape", "-1,3"),
                A("croissant.recordSet.2.field.1.equivalentProperty.1", "https://schema.org/value"),
                A("croissant.recordSet.2.field.2.name", "s"),
                A("croissant.recordSet.2.field.2.subField.1.name", "a"),
                A("croissant.recordSet.2.field.2.subField.1.dataType", "sc:Text"),
                A("croissant.recordSet.2.field.2.subField.1.source", "csv"),
                A("croissant.recordSet.2.field.2.subField.1.column", "a"),
            ]
        )
        vec, s = fields
        assert vec["isArray"] is True and vec["arrayShape"] == "-1,3"
        assert vec["equivalentProperty"] == "https://schema.org/value"
        assert "dataType" not in s and "source" not in s
        assert s["subField"] == [
            {
                "@type": "cr:Field",
                "@id": "nest/s/a",
                "name": "a",
                "dataType": "sc:Text",
                "source": {"fileObject": {"@id": "csv"}, "extract": {"column": "a"}},
            }
        ]

    def test_bad_is_array_warns(self):
        out = gen(self.BASE + [A("croissant.recordSet.1.field.1.isArray", "yes")])
        assert has_msg(
            out["validation"]["warnings"], "croissant.recordSet.1.field.1.isArray", "true or false"
        )

    def test_data_and_examples(self):
        doc = gen(
            self.BASE
            + [
                A("croissant.recordSet.1.data", '[{"base/id": 1}]'),
                A("croissant.recordSet.1.examples", "https://example.org/ex.json"),
            ]
        )["document"]
        assert doc["recordSet"][0]["data"] == [{"base/id": 1}]
        assert doc["recordSet"][0]["examples"] == "https://example.org/ex.json"
        out = gen(
            self.BASE
            + [
                A("croissant.recordSet.1.data", "[{oops"),
                A("croissant.recordSet.1.examples", '{"a": 1}'),
            ]
        )
        assert out["document"]["recordSet"][0]["data"] == "[{oops"
        assert out["document"]["recordSet"][0]["examples"] == {"a": 1}
        assert has_msg(
            out["validation"]["warnings"], "croissant.recordSet.1.data", "not valid JSON"
        )


class TestDatasetProperties:
    def test_persons(self):
        doc = gen(
            MINIMAL
            + [
                A("croissant.creator.2.name", "Org"),
                A("croissant.creator.2.type", "Organization"),
                A("croissant.creator.2.affiliation", "ignored"),
                A("croissant.creator.3.email", "nobody@x.org"),
                A("croissant.creator.4.name", "P"),
                A("croissant.creator.4.type", "Robot"),
                A("croissant.publisher.1.name", "Pub"),
                A("croissant.publisher.1.type", "sc:Organization"),
            ]
        )
        d = doc["document"]
        assert d["creator"] == [
            {"@type": "sc:Person", "name": "Jane Doe"},
            {"@type": "sc:Organization", "name": "Org"},
            {"@type": "sc:Person", "name": "P"},
        ]
        assert d["publisher"] == {"@type": "sc:Organization", "name": "Pub"}
        w = doc["validation"]["warnings"]
        assert has_msg(w, "croissant.creator.2.affiliation", "Person only")
        assert has_msg(w, "croissant.creator.3", "without a name")
        assert has_msg(w, "croissant.creator.4.type", "Person or Organization")

    def test_keywords(self):
        out = gen(
            MINIMAL
            + [
                A("croissant.keyword.1", "plain"),
                A("croissant.keyword.2", "soil", "ENVO:00001998"),
                A("croissant.keyword.3", "biome", "http://purl.obolibrary.org/obo/ENVO_00000428"),
                A("croissant.keyword.4", "odd", "free text"),
            ]
        )
        assert out["document"]["keywords"] == [
            "plain",
            {"@type": "sc:DefinedTerm", "name": "soil", "termCode": "ENVO:00001998"},
            {
                "@type": "sc:DefinedTerm",
                "name": "biome",
                "url": "http://purl.obolibrary.org/obo/ENVO_00000428",
            },
            {"@type": "sc:DefinedTerm", "name": "odd", "termCode": "free text"},
        ]
        assert has_msg(out["validation"]["warnings"], "croissant.keyword.4", "CURIE or IRI")
        text = to_jsonld(
            MINIMAL + [A("croissant.keyword.1", "soil", "ENVO:00001998")],
            item=dict(FOLDER, keyword_style="text"),
        )
        assert text["document"]["keywords"] == ["soil"]
        cw = to_jsonld(
            MINIMAL + [A("envo.soil", "soil", "ENVO:00001998")],
            item=dict(FOLDER, keyword_style="text"),
        )
        assert cw["document"]["keywords"] == ["soil"]

    def test_scalars_and_lists(self):
        d = gen(
            MINIMAL
            + [
                A("croissant.inLanguage.1", "en"),
                A("croissant.inLanguage.2", "es"),
                A("croissant.sameAs.1", "https://same"),
                A("croissant.isLiveDataset", "True"),
                A("croissant.wasDerivedFrom.1", "https://a"),
                A("croissant.wasDerivedFrom.2", "https://b"),
                A("croissant.wasGeneratedBy", "pipeline"),
            ]
        )["document"]
        assert d["inLanguage"] == ["en", "es"] and d["sameAs"] == "https://same"
        assert d["isLiveDataset"] is True
        # PROV-O ranges are objects: an IRI becomes {"@id"}, text a labelled node.
        assert d["prov:wasDerivedFrom"] == [{"@id": "https://a"}, {"@id": "https://b"}]
        assert d["prov:wasGeneratedBy"] == {"@type": "prov:Activity", "prov:label": "pipeline"}
        bad = gen(MINIMAL + [A("croissant.isLiveDataset", "maybe")])
        assert bad["document"]["isLiveDataset"] == "maybe"
        assert has_msg(bad["validation"]["warnings"], "croissant.isLiveDataset")

    def test_rai(self):
        d = gen(
            MINIMAL
            + [
                A("croissant.rai.dataCollection", "c"),
                A("croissant.rai.dataBiases.1", "b1"),
                A("croissant.rai.dataLimitations.1", "l1"),
                A("croissant.rai.dataLimitations.2", "l2"),
            ]
        )["document"]
        assert d["rai:dataCollection"] == "c"
        assert d["rai:dataBiases"] == "b1"
        assert d["rai:dataLimitations"] == ["l1", "l2"]

    def test_geo(self):
        d = gen(
            MINIMAL
            + [
                A("croissant.geo.spatialCoverage.name", "Somewhere"),
                A("croissant.geo.spatialCoverage.box", "31.7,-111.1, 31.9 ,-110.8"),
                A("croissant.geo.spatialCoverage.point", "31.8 -110.9"),
                A("croissant.geo.spatialResolution", "30", "m"),
                A("croissant.geo.temporalResolution", "0.5"),
                A("croissant.geo.samplingStrategy", "random"),
                A("croissant.geo.bandConfiguration.totalBands", "2"),
                A("croissant.geo.bandConfiguration.band.1", "R"),
                A("croissant.geo.bandConfiguration.band.2", "NIR"),
            ]
        )["document"]
        assert d["spatialCoverage"] == {
            "@type": "sc:Place",
            "name": "Somewhere",
            "geo": [
                {"@type": "sc:GeoShape", "box": "31.7 -111.1 31.9 -110.8"},
                {"@type": "sc:GeoCoordinates", "latitude": 31.8, "longitude": -110.9},
            ],
        }
        # "sc:value": the bare term "value" is cr:value in the Croissant 1.1 context.
        assert d["geocr:spatialResolution"] == {
            "@type": "sc:QuantitativeValue",
            "sc:value": 30,
            "unitText": "m",
        }
        assert d["geocr:temporalResolution"] == {"@type": "sc:QuantitativeValue", "sc:value": 0.5}
        assert d["geocr:samplingStrategy"] == "random"
        assert d["geocr:bandConfiguration"] == {
            "@type": "geocr:BandConfiguration",
            "geocr:totalBands": 2,
            "geocr:bandNameList": ["R", "NIR"],
        }

    def test_geo_bad_values(self):
        out = gen(
            MINIMAL
            + [
                A("croissant.geo.spatialCoverage.box", "31 -111 32"),
                A("croissant.geo.spatialCoverage.point", "north"),
                A("croissant.geo.spatialResolution", "about 30 m"),
                A("croissant.geo.bandConfiguration.totalBands", "four"),
            ]
        )
        d, v = out["document"], out["validation"]
        assert d["spatialCoverage"]["geo"] == {"@type": "sc:GeoShape", "box": "31 -111 32"}
        assert d["geocr:spatialResolution"] == "about 30 m"
        assert d["geocr:bandConfiguration"]["geocr:totalBands"] == "four"
        assert has_msg(v["warnings"], "croissant.geo.spatialCoverage.box", "four numbers")
        assert has_msg(v["warnings"], "croissant.geo.bandConfiguration.totalBands", "integer")
        assert has_msg(v["errors"], "spatialCoverage.geo.box", "four numbers")
        point_only = gen(MINIMAL + [A("croissant.geo.spatialCoverage.point", "1 2 3")])
        assert has_msg(
            point_only["validation"]["warnings"],
            "croissant.geo.spatialCoverage.point",
            "two numbers",
        )

    def test_name_fallbacks(self):
        assert gen([A("croissant.description", "d")], PRIVATE)["sources"]["name"] == "item.name"
        out = to_jsonld([], item={"path": "/iplant/home/u/folder/"})
        assert out["document"]["name"] == "folder"
        assert out["sources"]["name"] == "item.name"

    def test_avu_convention_version(self):
        out = gen(MINIMAL + [A("croissant.avuConvention", "2")])
        assert has_msg(out["validation"]["warnings"], "croissant.avuConvention", "not supported")
        assert "avuConvention" not in out["document"]
        ok = gen(MINIMAL + [A("croissant.avuConvention", "1")])
        assert not has_msg(ok["validation"]["warnings"], "croissant.avuConvention")

    def test_deterministic(self):
        a = json.dumps(gen(MINIMAL)["document"])
        b = json.dumps(gen(list(MINIMAL))["document"])
        assert a == b

    def test_empty_avus(self):
        out = to_jsonld([], item=FOLDER)
        assert out["document"]["distribution"] == []
        assert "recordSet" not in out["document"]
        assert not out["validation"]["valid"]


class TestFileLevel:
    ITEM = {
        "path": "/iplant/home/jdoe/x/data.parquet",
        "name": "data.parquet",
        "is_folder": False,
        "size": 10,
        "md5": "a" * 32,
    }

    def test_file_is_only_file_object(self):
        out = to_jsonld(
            [
                A("croissant.file.name", "Data"),
                A("croissant.file.encodingFormat", "application/vnd.apache.parquet"),
                A("croissant.file.sha256", "b" * 64),
                A("croissant.file.md5", "c" * 32),
                A("croissant.distribution.1.id", "other"),
                A("croissant.distribution.1.contentUrl", "other.csv"),
            ],
            item=self.ITEM,
        )
        assert out["document"]["distribution"] == [
            {
                "@type": "cr:FileObject",
                "@id": "data.parquet",
                "name": "Data",
                "contentUrl": "https://data.cyverse.org/dav/iplant/home/jdoe/x/data.parquet",
                "encodingFormat": "application/vnd.apache.parquet",
                "contentSize": "10 B",
                "sha256": "b" * 64,
                "md5": "c" * 32,
            }
        ]
        assert has_msg(out["validation"]["warnings"], "croissant.distribution", "ignored")
        assert out["sources"]["distribution"] == "item"

    def test_without_file_keys(self):
        item = {
            "path": "/iplant/home/shared/x/readme.txt",
            "name": "readme.txt",
            "is_folder": False,
        }
        fo = to_jsonld([], item=item)["document"]["distribution"][0]
        assert fo == {
            "@type": "cr:FileObject",
            "@id": "readme.txt",
            "name": "readme.txt",
            "contentUrl": "https://data.cyverse.org/dav-anon/iplant/home/shared/x/readme.txt",
            "encodingFormat": "text/plain",
        }

    def test_file_name_with_spaces(self):
        item = {"path": "/iplant/home/shared/x/my data.csv", "name": "my data.csv",
                "is_folder": False}
        out = to_jsonld([A("croissant.recordSet.1.id", "t"),
                         A("croissant.recordSet.1.field.1.name", "a"),
                         A("croissant.recordSet.1.field.1.dataType", "sc:Text"),
                         A("croissant.recordSet.1.field.1.column", "a")], item=item)
        fo = out["document"]["distribution"][0]
        assert fo["@id"] == "my_data.csv" and fo["name"] == "my data.csv"
        assert fo["contentUrl"].endswith("/x/my%20data.csv")
        field = out["document"]["recordSet"][0]["field"][0]
        assert field["source"]["fileObject"] == {"@id": "my_data.csv"}
        assert not [e for e in out["validation"]["errors"] if "@id" in e["path"]]

    def test_fields_default_to_the_file(self):
        doc = to_jsonld(
            [
                A("croissant.recordSet.1.id", "t"),
                A("croissant.recordSet.1.field.1.name", "a"),
                A("croissant.recordSet.1.field.1.dataType", "sc:Text"),
                A("croissant.recordSet.1.field.1.column", "a"),
                A("croissant.recordSet.1.field.2.name", "b"),
                A("croissant.recordSet.1.field.2.dataType", "sc:Text"),
                A("croissant.recordSet.1.field.2.source", "t"),
            ],
            item=self.ITEM,
        )["document"]
        a, b = doc["recordSet"][0]["field"]
        assert a["source"] == {"fileObject": {"@id": "data.parquet"}, "extract": {"column": "a"}}
        assert b["source"] == {"recordSet": {"@id": "t"}}


# ---------------------------------------------------------------------------
# Crosswalk
# ---------------------------------------------------------------------------


class TestCrosswalk:
    def test_name_and_description_order(self):
        cw = crosswalk(
            [
                A("dc.title", "dc"),
                A("datacite.title", "legacy"),
                A("datacite.title.1.value", "indexed"),
                A("dc.description", "dd"),
            ]
        )
        assert cw["name"]["value"] == "indexed"
        assert cw["name"]["source"] == "datacite.title.1.value"
        assert cw["name"]["avus"] == [A("croissant.name", "indexed")]
        assert cw["description"] == {
            "value": "dd",
            "source": "dc.description",
            "avus": [A("croissant.description", "dd")],
        }
        assert (
            crosswalk([A("datacite.title", "legacy"), A("dc.title", "dc")])["name"]["source"]
            == "datacite.title"
        )
        assert crosswalk([A("datacite.title.1", "portal")])["name"]["value"] == "portal"
        assert (
            crosswalk([A("datacite.title.2.title", "two")])["name"]["source"]
            == "datacite.title.2.title"
        )

    def test_ignores_croissant_avus(self):
        assert crosswalk([A("croissant.name", "x")]) == {}

    def test_creators(self):
        cw = crosswalk(
            [
                A("datacite.creator.1.name", "Doe, Jane"),
                A("datacite.creator.1.orcid", "0000-0002-1825-0097"),
                A("datacite.creator.1.affiliation", "UNM"),
                A("datacite.creator.2.name", "Lab"),
                A("datacite.creator.2.nameType", "Organizational"),
                A("datacite.creator.3.familyName", "Roe"),
                A("datacite.creator.3.givenName", "Ann"),
                A("datacite.creator.3.nameIdentifier", "https://isni.org/isni/000000012345"),
                A("datacite.creator.4.nameType", "Personal"),
            ]
        )
        assert cw["creator"]["value"] == [
            {
                "@type": "sc:Person",
                "name": "Doe, Jane",
                "identifier": "https://orcid.org/0000-0002-1825-0097",
                "affiliation": {"@type": "sc:Organization", "name": "UNM"},
            },
            {"@type": "sc:Organization", "name": "Lab"},
            {
                "@type": "sc:Person",
                "name": "Roe, Ann",
                "identifier": "https://isni.org/isni/000000012345",
            },
        ]
        assert cw["creator"]["source"] == "datacite.creator.1.name"
        assert triples(cw["creator"]["avus"]) == [
            ("croissant.creator.1.name", "Doe, Jane", ""),
            ("croissant.creator.1.identifier", "https://orcid.org/0000-0002-1825-0097", ""),
            ("croissant.creator.1.affiliation", "UNM", ""),
            ("croissant.creator.2.name", "Lab", ""),
            ("croissant.creator.2.type", "Organization", ""),
            ("croissant.creator.3.name", "Roe, Ann", ""),
            ("croissant.creator.3.identifier", "https://isni.org/isni/000000012345", ""),
        ]

    def test_legacy_creators(self):
        cw = crosswalk(
            [A("datacite.creator", "A"), A("datacite.creator", "B"), A("dc.creator", "C")]
        )
        assert [p["name"] for p in cw["creator"]["value"]] == ["A", "B"]
        assert crosswalk([A("dc.creator", "C")])["creator"]["value"] == {
            "@type": "sc:Person",
            "name": "C",
        }

    def test_publisher(self):
        cw = crosswalk([A("dc.publisher", "D"), A("datacite.publisher", "CyVerse")])
        assert cw["publisher"]["value"] == {"@type": "sc:Organization", "name": "CyVerse"}
        assert cw["publisher"]["avus"] == [
            A("croissant.publisher.1.name", "CyVerse"),
            A("croissant.publisher.1.type", "Organization"),
        ]

    def test_license(self):
        cw = crosswalk(
            [
                A("datacite.rights.1.value", "CC BY"),
                A("datacite.rights.1.rightsURI", "https://creativecommons.org/licenses/by/4.0/"),
                A("datacite.rights.2.value", "https://spdx.org/licenses/MIT.html"),
                A("datacite.rights.3.value", "All rights reserved"),
                A("dc.rights", "https://ignored"),
            ]
        )
        assert cw["license"]["value"] == [
            "https://creativecommons.org/licenses/by/4.0/",
            "https://spdx.org/licenses/MIT.html",
        ]
        assert cw["license"]["source"] == "datacite.rights.1.rightsURI"
        assert crosswalk([A("dc.rights", "CC0"), A("dc.rights", "https://cc0")])["license"] == {
            "value": "https://cc0",
            "source": "dc.rights",
            "avus": [A("croissant.license.1", "https://cc0")],
        }
        assert "license" not in crosswalk([A("datacite.rights.1.value", "Proprietary")])
        assert (
            crosswalk([A("datacite.rights.1.rights", "https://x")])["license"]["value"]
            == "https://x"
        )

    def test_date_published(self):
        both = [
            A("datacite.date.1.date", "2024-03-01"),
            A("datacite.date.1.dateType", "Issued"),
            A("datacite.publicationYear", "2024"),
        ]
        assert crosswalk(both)["datePublished"]["source"] == "datacite.publicationYear"
        assert crosswalk(both[:2])["datePublished"]["value"] == "2024-03-01"
        assert (
            crosswalk([A("datacite.publicationyear", "2023")])["datePublished"]["value"] == "2023"
        )
        assert crosswalk([A("dc.date", "2020-01-01")])["datePublished"]["source"] == "dc.date"
        created = [A("datacite.date.1.date", "2024"), A("datacite.date.1.dateType", "Created")]
        assert "datePublished" not in crosswalk(created)

    def test_doi(self):
        for ident, typ in (
            ("10.1234/x", "DOI"),
            ("doi:10.1234/x", "doi"),
            ("https://doi.org/10.1234/x", None),
        ):
            avus = [A("datacite.identifier", ident)]
            if typ:
                avus.append(A("datacite.identifierType", typ))
            cw = crosswalk(avus)
            assert cw["identifier"]["value"] == cw["url"]["value"] == "https://doi.org/10.1234/x"
        assert "url" not in crosswalk(
            [A("datacite.identifier", "10.1234/x"), A("datacite.identifierType", "Handle")]
        )
        assert "url" not in crosswalk([A("datacite.identifier", "not a doi")])

    def test_keywords(self):
        cw = crosswalk(
            [
                A("datacite.subject.1.value", "soil"),
                A("datacite.subject.2", "water"),
                A("envo.grassland", "grassland", "ENVO:00000106"),
                A("envo.dup", "grassland", "ENVO:00000106"),
                A("mesa.role", "x", "MESA:1"),
                A("datacite.subject.3.value", "y", "ENVO:1"),
                A("ncit.free", "x", "not a curie"),
                A("obi.iri", "x", "http://purl.obolibrary.org/obo/OBI_1"),
                A("ipc_UUID", "x", "a:b"),
                A("localcontexts.label", "x", "LC:1"),
            ]
        )
        assert cw["keywords"]["value"] == [
            "soil",
            "water",
            "y",
            {"@type": "sc:DefinedTerm", "name": "grassland", "termCode": "ENVO:00000106"},
        ]
        assert cw["keywords"]["source"] == "datacite.subject.1.value+ontology"
        assert cw["keywords"]["avus"][-1] == A("croissant.keyword.4", "grassland", "ENVO:00000106")
        only_terms = crosswalk([A("envo.grassland", "grassland", "ENVO:00000106")])
        assert only_terms["keywords"]["source"] == "ontology"

    def test_temporal_coverage(self):
        avus = [A("datacite.date.1.date", "2019/2020"), A("datacite.date.1.dateType", "Collected")]
        assert crosswalk(avus)["temporalCoverage"]["value"] == "2019/2020"
        cw = crosswalk(avus + [A("neon.temporalCoverage", "2018-01-01/2021-12-31")])
        assert cw["temporalCoverage"]["source"] == "neon.temporalCoverage"

    def test_spatial_coverage(self):
        cw = crosswalk(
            [
                A("datacite.geoLocation.1.place", "Tucson"),
                A("datacite.geoLocation.1.box", "32.0,-111.1,32.4,-110.7"),
                A("datacite.geoLocation.2.box", "1,2,3,4"),
            ]
        )
        assert cw["spatialCoverage"]["value"] == {
            "@type": "sc:Place",
            "name": "Tucson",
            "geo": {"@type": "sc:GeoShape", "box": "32.0 -111.1 32.4 -110.7"},
        }
        assert triples(cw["spatialCoverage"]["avus"]) == [
            ("croissant.geo.spatialCoverage.name", "Tucson", ""),
            ("croissant.geo.spatialCoverage.box", "32.0 -111.1 32.4 -110.7", ""),
        ]
        point = crosswalk([A("datacite.geoLocation.1.geoLocationPoint", "32.2, -110.9")])
        assert point["spatialCoverage"]["value"]["geo"] == {
            "@type": "sc:GeoCoordinates",
            "latitude": 32.2,
            "longitude": -110.9,
        }
        place = crosswalk([A("datacite.geoLocation.3.geoLocationPlace", "Moab")])
        assert place["spatialCoverage"]["value"] == {"@type": "sc:Place", "name": "Moab"}
        bad = crosswalk([A("datacite.geoLocation.1.box", "1,2,3")])
        assert "spatialCoverage" not in bad

    def test_generated_by(self):
        cw = crosswalk([A("mesa.generated_by", "neon-ducklake")])
        assert cw["prov:wasGeneratedBy"]["avus"] == [A("croissant.wasGeneratedBy", "neon-ducklake")]

    def test_croissant_avu_wins(self):
        out = gen(
            MINIMAL + [A("datacite.title.1.value", "DataCite"), A("datacite.subject.1.value", "s")]
        )
        assert out["document"]["name"] == "Minimal"
        assert out["sources"]["keywords"] == "datacite.subject.1.value"
        own = gen(MINIMAL + [A("croissant.keyword.1", "own"), A("envo.x", "t", "ENVO:1")])
        assert own["document"]["keywords"] == ["own"]

    def test_copy_avus_regenerate_the_same_value(self):
        fixture = load_json(FIXTURES / "golden_ducklake_crosswalk.json")
        cw = crosswalk(fixture["avus"])
        copied = [a for prop in cw.values() for a in prop["avus"]]
        croissant = [a for a in fixture["avus"] if is_croissant_attr(a["attribute"])]
        out = to_jsonld(croissant + copied, item=fixture["item"])
        doc = dict(out["document"])
        # Copying spatialCoverage writes croissant.geo.*, which declares GeoCroissant.
        assert doc.pop("conformsTo") == [CONFORMS_CORE, CONFORMS_GEO]
        expected = dict(fixture["document"])
        expected.pop("conformsTo")
        assert doc == expected
        assert set(out["sources"].values()) <= {"croissant", "auto"}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def valid_doc():
    return copy.deepcopy(gen(MINIMAL)["document"])


class TestValidate:
    def test_valid_minimal(self):
        r = validate(valid_doc())
        assert r == {
            "valid": True,
            "errors": [],
            "warnings": [],
            "conformsTo": [CONFORMS_CORE],
            "summary": {"distribution": 1, "recordSet": 1, "field": 1},
        }

    def test_input_types(self):
        assert validate(json.dumps(valid_doc()))["valid"]
        assert messages(validate("{not json")["errors"])[0][1].startswith("not valid JSON")
        assert validate([])["errors"] == [{"path": "", "message": "document must be a JSON object"}]
        r = validate({})
        assert not r["valid"]
        paths = [p for p, _ in messages(r["errors"])]
        for p in (
            "@context",
            "@type",
            "conformsTo",
            "name",
            "description",
            "url",
            "license",
            "creator",
            "datePublished",
        ):
            assert p in paths

    @pytest.mark.parametrize(
        "prop", ["name", "description", "url", "license", "creator", "datePublished"]
    )
    def test_required(self, prop):
        doc = valid_doc()
        del doc[prop]
        r = validate(doc)
        assert {"path": prop, "message": f"{prop} is required"} in r["errors"]

    def test_empty_values_are_missing(self):
        doc = valid_doc()
        doc["license"] = []
        doc["name"] = ""
        r = validate(doc)
        assert has_msg(r["errors"], "license", "required")
        assert has_msg(r["errors"], "name", "required")

    def test_empty_distribution_is_a_warning(self):
        doc = valid_doc()
        doc["distribution"] = []
        del doc["recordSet"]
        r = validate(doc)
        assert r["valid"]
        assert has_msg(r["warnings"], "distribution", "empty")

    def test_conforms_to(self):
        doc = valid_doc()
        doc["conformsTo"] = "http://mlcommons.org/croissant/1.0"
        doc["rai:dataBiases"] = "b"
        doc["geocr:coordinateReferenceSystem"] = "EPSG:4326"
        r = validate(doc)
        assert r["valid"] and r["conformsTo"] == ["http://mlcommons.org/croissant/1.0"]
        assert has_msg(r["warnings"], "conformsTo", "does not declare")
        assert has_msg(r["warnings"], "conformsTo", "RAI properties")
        assert has_msg(r["warnings"], "conformsTo", "GeoCroissant properties")

    def test_duplicate_and_whitespace_ids(self):
        doc = valid_doc()
        doc["recordSet"][0]["@id"] = "f"
        doc["recordSet"][0]["field"][0]["@id"] = "rs/x y"
        r = validate(doc)
        assert {
            "path": "recordSet[0].@id",
            "message": "duplicate @id 'f' (also at distribution[0].@id)",
        } in r["errors"]
        assert has_msg(r["errors"], "recordSet[0].field[0].@id", "whitespace")

    def test_sources_and_references(self):
        doc = valid_doc()
        f = doc["recordSet"][0]["field"][0]
        f["source"] = {"fileSet": {"@id": "f"}, "extract": {"fileProperty": "size"}}
        f["references"] = {"field": {"@id": "rs/nope"}}
        doc["recordSet"][0]["key"] = {"@id": "rs/missing"}
        doc["distribution"][0]["containedIn"] = {"@id": "zip"}
        r = validate(doc)
        assert has_msg(r["warnings"], "recordSet[0].field[0].source", "is a FileObject")
        assert has_msg(r["errors"], "recordSet[0].field[0].source.extract.fileProperty")
        assert has_msg(r["errors"], "recordSet[0].field[0].references", "'rs/nope'")
        assert has_msg(r["warnings"], "recordSet[0].key", "'rs/missing'")
        assert has_msg(r["errors"], "distribution[0].containedIn", "'zip'")
        f["source"] = {"recordSet": {"@id": "zz"}}
        assert has_msg(validate(doc)["errors"], "recordSet[0].field[0].source", "record set")
        f["source"] = {"field": {"@id": "zz/a"}}
        assert has_msg(validate(doc)["errors"], "recordSet[0].field[0].source", "field @id")
        f["source"] = {"extract": {"column": "x"}}
        assert has_msg(validate(doc)["errors"], "recordSet[0].field[0].source", "must reference")

    def test_field_rules(self):
        doc = valid_doc()
        f = doc["recordSet"][0]["field"][0]
        del f["dataType"]
        r = validate(doc)
        assert {
            "path": "recordSet[0].field[0].dataType",
            "message": "field needs a dataType (unless it has subFields)",
        } in r["errors"]
        f["subField"] = [
            {
                "@type": "cr:Field",
                "@id": "rs/x/a",
                "dataType": "sc:Text",
                "source": {"fileObject": {"@id": "f"}},
            }
        ]
        r = validate(doc)
        assert r["valid"] and r["summary"]["field"] == 2
        del f["source"]
        del f["subField"]
        f["dataType"] = "sc:Text"
        assert has_msg(validate(doc)["errors"], "recordSet[0].field[0].source", "needs a source")
        doc["recordSet"][0]["data"] = [{"rs/x": "a"}]
        assert validate(doc)["valid"]

    def test_field_inherits_datatype_from_source_field(self):
        doc = valid_doc()
        doc["recordSet"].append(
            {
                "@type": "cr:RecordSet",
                "@id": "j",
                "field": [
                    {"@type": "cr:Field", "@id": "j/x", "source": {"field": {"@id": "rs/x"}}}
                ],
            }
        )
        r = validate(doc)
        assert r["valid"]
        assert has_msg(r["warnings"], "recordSet[1].field[0].dataType", "inherited")

    def test_datatypes(self):
        doc = valid_doc()
        f = doc["recordSet"][0]["field"][0]
        f["dataType"] = ["sc:Integer", "wd:Q1", "cr:Int8", "Integer", "sc:Nope", "nope"]
        r = validate(doc)
        assert r["valid"]
        flagged = [p for p, _ in messages(r["warnings"]) if "dataType" in p]
        assert flagged == ["recordSet[0].field[0].dataType[4]", "recordSet[0].field[0].dataType[5]"]

    def test_arrays(self):
        doc = valid_doc()
        f = doc["recordSet"][0]["field"][0]
        f["arrayShape"] = "512,512,6"
        assert has_msg(validate(doc)["errors"], "recordSet[0].field[0].arrayShape", "isArray")
        f["isArray"] = True
        assert validate(doc)["valid"]
        f["arrayShape"] = "(512, x)"
        assert has_msg(validate(doc)["warnings"], "recordSet[0].field[0].arrayShape")
        f["isArray"] = "true"
        assert has_msg(validate(doc)["warnings"], "recordSet[0].field[0].isArray")

    def test_distribution_rules(self):
        doc = valid_doc()
        doc["distribution"].append({"@type": "cr:FileSet", "@id": "s", "encodingFormat": "x"})
        doc["distribution"].append({"@type": "cr:FileObject", "@id": "o"})
        doc["distribution"].append(
            {"@type": "sc:Thing", "name": "t", "encodingFormat": "x", "contentUrl": "u", "md5": "m"}
        )
        r = validate(doc)
        assert has_msg(r["errors"], "distribution[1].includes", "needs includes")
        assert has_msg(r["errors"], "distribution[2].contentUrl")
        assert has_msg(r["errors"], "distribution[2].encodingFormat")
        assert has_msg(r["warnings"], "distribution[2]", "sha256 or md5")
        assert has_msg(r["errors"], "distribution[3].@type")
        assert has_msg(r["errors"], "distribution[3].@id", "required")
        doc["isLiveDataset"] = True
        assert not has_msg(validate(doc)["warnings"], "distribution[2]", "sha256")

    def test_join_warning(self):
        doc = valid_doc()
        doc["distribution"].append(
            {
                "@type": "cr:FileObject",
                "@id": "g",
                "contentUrl": "g.csv",
                "encodingFormat": "text/csv",
                "md5": "0",
            }
        )
        doc["recordSet"][0]["field"].append(
            {
                "@type": "cr:Field",
                "@id": "rs/y",
                "dataType": "sc:Text",
                "source": {"fileObject": {"@id": "g"}},
            }
        )
        assert has_msg(validate(doc)["warnings"], "recordSet[0]", "join")
        doc["recordSet"][0]["field"][1]["references"] = {"field": {"@id": "rs/x"}}
        assert not has_msg(validate(doc)["warnings"], "recordSet[0]", "join")

    @pytest.mark.parametrize(
        "value,ok",
        [
            ("2026", True),
            ("2026-11", True),
            ("2026-11-06", True),
            ("2026-11-06T12:00:00Z", True),
            ("2026-11-06T12:00:00.123+02:00", True),
            ("2026-11-06 12:00", True),
            ("2026-13", False),
            ("2026-02-30", False),
            ("06/11/2026", False),
            ("2026-11-06T25:00", False),
            ("yesterday", False),
            ("", False),
        ],
    )
    def test_dates(self, value, ok):
        doc = valid_doc()
        doc["datePublished"] = value
        doc["dateModified"] = value
        r = validate(doc)
        if not value:
            assert has_msg(r["errors"], "datePublished", "required")
            return
        assert has_msg(r["errors"], "datePublished", "ISO 8601") is (not ok)
        assert has_msg(r["errors"], "dateModified", "ISO 8601") is (not ok)

    def test_temporal_coverage_and_timeframes(self):
        doc = valid_doc()
        doc["temporalCoverage"] = "2018-01-01/.."
        doc["rai:dataCollectionTimeframe"] = ["2019-01-01T00:00:00Z", "soon"]
        r = validate(doc)
        assert not has_msg(r["warnings"], "temporalCoverage")
        assert has_msg(r["warnings"], "rai:dataCollectionTimeframe[1]")
        doc["temporalCoverage"] = "2018 to 2020"
        assert has_msg(validate(doc)["warnings"], "temporalCoverage")

    @pytest.mark.parametrize(
        "box,errors,warns",
        [
            ("31.7 -111.1 31.9 -110.8", [], []),
            ("31.7,-111.1,31.9,-110.8", [], []),
            ("31.7 -111.1 31.9", ["four numbers"], []),
            ("a b c d", ["four numbers"], []),
            ("95 -111 96 -110", ["south latitude 95", "north latitude 96"], []),
            ("31 -200 32 190", ["west longitude -200", "east longitude 190"], []),
            ("33 -111 32 -110", ["south latitude is greater"], []),
            ("31 170 32 -170", [], ["antimeridian"]),
        ],
    )
    def test_bbox(self, box, errors, warns):
        doc = valid_doc()
        doc["spatialCoverage"] = {"@type": "sc:Place", "geo": {"@type": "sc:GeoShape", "box": box}}
        r = validate(doc)
        got = [m for p, m in messages(r["errors"]) if p == "spatialCoverage.geo.box"]
        assert len(got) == len(errors)
        for e in errors:
            assert any(e in m for m in got), (e, got)
        for w in warns:
            assert has_msg(r["warnings"], "spatialCoverage.geo.box", w)

    def test_point_and_bands(self):
        doc = valid_doc()
        doc["spatialCoverage"] = {
            "@type": "sc:Place",
            "geo": [
                {"@type": "sc:GeoShape", "box": "1 2 3 4"},
                {"@type": "sc:GeoCoordinates", "latitude": 91, "longitude": "x"},
            ],
        }
        doc["geocr:bandConfiguration"] = {"geocr:totalBands": 3, "geocr:bandNameList": ["a"]}
        r = validate(doc)
        assert has_msg(r["errors"], "spatialCoverage.geo[1].latitude", "outside")
        assert has_msg(r["errors"], "spatialCoverage.geo[1].longitude", "number")
        assert has_msg(r["warnings"], "geocr:bandConfiguration", "does not match")

    def test_misc_warnings(self):
        doc = valid_doc()
        doc["license"] = ["MIT", {"@type": "CreativeWork", "url": "https://x"}]
        doc["version"] = "v1"
        doc["sameAs"] = "not a url"
        doc["url"] = "mesa.cyverse.org"
        doc["keywords"] = [{"@type": "sc:DefinedTerm"}]
        doc["creator"] = [{"@type": "sc:Person"}, {"@type": "sc:Robot", "name": "R"}, 5]
        doc["isLiveDataset"] = "yes"
        r = validate(doc)
        assert has_msg(r["warnings"], "license[0]", "URL")
        assert not has_msg(r["warnings"], "license[1]")
        assert has_msg(r["warnings"], "version", "MAJOR.MINOR.PATCH")
        assert has_msg(r["warnings"], "sameAs[0]")
        assert has_msg(r["errors"], "url", "http(s)")
        assert has_msg(r["warnings"], "keywords[0]", "without a name")
        assert has_msg(r["errors"], "creator[0].name")
        assert has_msg(r["warnings"], "creator[1].@type")
        assert has_msg(r["errors"], "creator[2]")
        assert has_msg(r["warnings"], "isLiveDataset")

    def test_value_length(self):
        doc = valid_doc()
        doc["description"] = "x" * 2701
        doc["recordSet"][0]["data"] = [{"rs/x": "y" * 2700}]
        r = validate(doc)
        assert r["valid"]
        assert has_msg(r["warnings"], "description", "2701 characters")
        assert has_msg(r["warnings"], "recordSet[0].data", "inline JSON")

    def test_prefixed_keys(self):
        doc = {
            "@context": {},
            "@type": "https://schema.org/Dataset",
            "schema:name": "n",
            "http://schema.org/description": "d",
            "dct:conformsTo": CONFORMS_CORE,
            "sc:url": "https://x",
            "license": "https://l",
            "creator": "c",
            "datePublished": "2020",
            "cr:distribution": [
                {
                    "@type": "cr:FileObject",
                    "@id": "f",
                    "contentUrl": "u",
                    "encodingFormat": "text/csv",
                    "md5": "m",
                }
            ],
        }
        r = validate(doc)
        assert r["valid"], r["errors"]

    @pytest.mark.parametrize("path", upstream_paths() + import_paths(), ids=lambda p: p.stem)
    def test_never_crashes_on_real_documents(self, path):
        r = validate(load_json(path))
        assert set(r) == {"valid", "errors", "warnings", "conformsTo", "summary"}
        assert r["summary"]["distribution"] > 0


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


def avu_map(avus):
    return {a["attribute"]: (a["value"], a["unit"]) for a in avus}


UPSTREAM_EXPECT = {
    # file: (min AVUs, max AVUs, record sets, required attributes)
    "v1.1_huggingface-squad_v2": (
        60,
        110,
        2,
        {
            "croissant.distribution.2.type": ("FileSet", ""),
            "croissant.distribution.2.containedIn.1": ("repo", ""),
            "croissant.recordSet.1.dataType": ("cr:Split", ""),
            "croissant.recordSet.1.key.1": ("squad_v2_splits/split_name", ""),
            "croissant.recordSet.2.field.1.references": ("squad_v2_splits/split_name", ""),
            "croissant.recordSet.2.field.1.fileProperty": ("fullpath", ""),
            "croissant.recordSet.2.field.6.subField.1.isArray": ("true", ""),
            "croissant.recordSet.2.field.6.subField.2.dataType": ("cr:Int32", ""),
            "croissant.alternateName": ("rajpurkar/squad_v2", ""),
            "croissant.wasDerivedFrom.1": ("https://huggingface.co/datasets/rajpurkar/squad", ""),
        },
    ),
    "v1.1_huggingface-mnist": (
        35,
        70,
        2,
        {
            "croissant.distribution.2.includes.2": ("mnist/test-*.parquet", ""),
            "croissant.recordSet.1.data": (
                '[{"mnist_splits/split_name":"train"},{"mnist_splits/split_name":"test"}]',
                "",
            ),
            "croissant.citeAs": None,
        },
    ),
    "v1.1_zenodo-head-mri": (
        35,
        60,
        1,
        {
            "croissant.distribution.1.contentSize": ("4.698", "MB"),
            "croissant.distribution.1.md5": ("e5cbd0bca91f1787d057b0eac2572bde", ""),
            "croissant.creator.1.type": ("Organization", ""),
        },
    ),
    "v1.0_titanic": (
        120,
        180,
        3,
        {
            "croissant.recordSet.1.field.1.dataType.2": ("sc:name", ""),
            "croissant.recordSet.1.dataType": ("sc:Enumeration", ""),
            "croissant.distribution.1.contentSize": ("117743", ""),
            "croissant.version": ("1.0.0", ""),
        },
    ),
    "v1.0_movielens": (
        100,
        160,
        4,
        {
            "croissant.recordSet.1.field.3.isArray": ("true", ""),
            "croissant.recordSet.1.field.3.separator": ("|", ""),
            "croissant.recordSet.2.field.2.references": ("movies/movie_id", ""),
            "croissant.recordSet.4.field.1.source": ("movies/movie_id", ""),
        },
    ),
    "v0.8_titanic": (
        110,
        170,
        3,
        {
            "croissant.distribution.1.id": ("passengers.csv", ""),
            "croissant.recordSet.1.key.1": ("genders/label", ""),
            "croissant.recordSet.1.dataType": ("sc:Enumeration", ""),
            "croissant.recordSet.1.field.1.source": ("genders.csv", ""),
            "croissant.recordSet.3.field.2.references": ("genders/label", ""),
            "croissant.citeAs": None,
        },
    ),
}


class TestFromJsonldUpstream:
    @pytest.mark.parametrize("path", upstream_paths(), ids=lambda p: p.stem)
    def test_upstream(self, path):
        lo, hi, n_rs, expect = UPSTREAM_EXPECT[path.stem]
        r = from_jsonld(load_json(path))
        assert r["errors"] == []
        assert lo <= len(r["avus"]) <= hi, len(r["avus"])
        assert all(is_croissant_attr(a["attribute"]) for a in r["avus"])
        got = avu_map(r["avus"])
        for attr, want in expect.items():
            assert attr in got, attr
            if want is not None:
                assert got[attr] == want, (attr, got[attr])
        p = parse_avus(r["avus"])
        assert p["unknown"] == [] and p["warnings"] == []
        assert len(p["model"]["recordSet"]) == n_rs
        regen = to_jsonld(r["avus"], item={"path": "/iplant/home/shared/u", "name": "u"})
        src = validate(load_json(path))
        assert regen["validation"]["summary"]["recordSet"] == src["summary"]["recordSet"]
        # The only errors left are properties the upstream file itself lacks.
        for e in regen["validation"]["errors"]:
            assert e["path"] in ("creator", "datePublished", "license") or e["path"].endswith(
                ".dataType"
            ), e

    def test_squad_warnings(self):
        r = from_jsonld(load_json(UPSTREAM / "v1.1_huggingface-squad_v2.json"))
        w = r["warnings"]
        assert has_msg(w, "alternateName", "first is kept")
        assert has_msg(w, "prov:wasGeneratedBy[0]", "reduced to text")
        # transform.jsonPath is kept (transformJsonPath), no longer dropped.
        assert not has_msg(w, "recordSet[1].field[5].subField[0].source.transform[0].jsonPath")
        assert has_msg(w, "isAssociatedWith", "not represented")

    def test_squad_relative_content_base(self):
        doc = load_json(UPSTREAM / "v1.1_huggingface-squad_v2.json")
        r = from_jsonld(doc, content_base="https://huggingface.co/datasets/rajpurkar")
        # Decoded, "refs%2Fconvert%2Fparquet" would regenerate as "refs/convert/parquet"
        # (another URL), so the absolute URL is kept.
        assert (
            avu_map(r["avus"])["croissant.distribution.1.contentUrl"][0]
            == "https://huggingface.co/datasets/rajpurkar/squad_v2/tree/refs%2Fconvert%2Fparquet"
        )
        r = from_jsonld(
            doc, content_base="https://huggingface.co/datasets/rajpurkar/squad_v2/tree"
        )
        assert avu_map(r["avus"])["croissant.distribution.1.contentUrl"][0] == (
            "https://huggingface.co/datasets/rajpurkar/squad_v2/tree/refs%2Fconvert%2Fparquet"
        )


class TestFromJsonld:
    def test_geocroissant_example(self):
        r = from_jsonld(load_json(IMPORTS / "geocroissant_example.json"))
        assert r["errors"] == []
        got = avu_map(r["avus"])
        assert got["croissant.geo.spatialCoverage.box"] == ("31.0 -114.0 34.0 -110.5", "")
        assert got["croissant.geo.spatialCoverage.point"] == ("32.5 -112.25", "")
        assert got["croissant.geo.spatialResolution"] == ("250", "m")
        assert got["croissant.geo.temporalResolution"] == ("16", "day")
        assert got["croissant.geo.samplingStrategy"][0].startswith("All cloud-free")
        assert got["croissant.geo.bandConfiguration.totalBands"] == ("1", "")
        assert got["croissant.geo.bandConfiguration.band.1"] == ("NDVI", "")
        assert got["croissant.license.1"] == ("https://creativecommons.org/licenses/by/4.0/", "")
        assert got["croissant.creator.1.name"] == ("Ana Garcia", "")
        assert got["croissant.creator.1.identifier"] == (
            "https://orcid.org/0000-0001-2345-6789",
            "",
        )
        assert got["croissant.version"] == ("2", "")
        assert got["croissant.recordSet.1.field.1.arrayShape"] == ("1,512,512", "")
        assert got["croissant.recordSet.1.field.1.dataType"] == ("sc:ImageObject", "")
        assert "croissant.conformsTo.1" not in got
        assert messages(r["warnings"]) == [
            ("spectralBandMetadata", "property not represented in the AVU convention; dropped")
        ]
        doc = to_jsonld(r["avus"], item=FOLDER)["document"]
        assert doc["conformsTo"] == [CONFORMS_CORE, CONFORMS_GEO]

    def test_mixed_dialects(self):
        r = from_jsonld(load_json(IMPORTS / "mixed_dialects.json"))
        assert r["errors"] == []
        got = avu_map(r["avus"])
        assert got["croissant.name"] == ("Mixed dialect test document", "")
        assert got["croissant.description"][0].startswith("Written for")
        assert got["croissant.license.2"] == ("https://spdx.org/licenses/Apache-2.0.html", "")
        assert got["croissant.creator.1.name"] == ("Plain Name", "")
        assert got["croissant.creator.2.type"] == ("Organization", "")
        assert got["croissant.publisher.1.type"] == ("Organization", "")
        assert got["croissant.keyword.2"] == ("soil", "ENVO:00001998")
        assert got["croissant.keyword.3"] == (
            "biome",
            "http://purl.obolibrary.org/obo/ENVO_00000428",
        )
        assert got["croissant.isLiveDataset"] == ("true", "")
        assert got["croissant.rai.dataCollection"] == ("Scraped from a test fixture.", "")
        assert got["croissant.rai.dataBiases.1"] == ("None known.", "")
        assert got["croissant.rai.personalSensitiveInformation.2"] == ("No locations.", "")
        assert got["croissant.rai.dataCollectionTimeframe.2"] == ("2024-02-01", "")
        assert got["croissant.rai.dataManipulationProtocol"] == ("Deduplicated.", "")
        assert got["croissant.wasDerivedFrom.1"] == ("https://example.org/source", "")
        assert got["croissant.distribution.1.contentSize"] == ("2048", "")
        assert got["croissant.recordSet.1.data"] == (
            '[{"colors/name":"red"},{"colors/name":"blue"}]',
            "",
        )
        assert got["croissant.recordSet.2.field.1.isArray"] == ("true", "")
        assert got["croissant.recordSet.2.field.1.separator"] == (";", "")
        assert got["croissant.recordSet.2.field.1.dataType"] == ("sc:Text", "")
        assert got["croissant.recordSet.2.field.2.dataType"] == ("sc:Text", "")
        assert got["croissant.recordSet.2.field.2.references"] == ("colors/name", "")
        assert got["croissant.recordSet.2.field.3.subField.1.name"] == ("a", "")
        assert got["croissant.recordSet.3.field.1.jsonPath"] == ("$.items[*].title", "")
        assert not any("deep" in a for a in got)
        assert "croissant.conformsTo.1" not in got
        w = r["warnings"]
        assert has_msg(w, "recordSet[1].field[2].subField[0].subField", "deeper than one level")
        assert has_msg(w, "funder", "not represented")
        regen = to_jsonld(r["avus"], item=FOLDER)
        assert regen["validation"]["valid"], regen["validation"]["errors"]
        assert regen["document"]["conformsTo"] == [CONFORMS_CORE, CONFORMS_RAI]

    def test_input_forms_and_errors(self):
        doc = valid_doc()
        assert from_jsonld(json.dumps(doc))["avus"] == from_jsonld(doc)["avus"]
        assert from_jsonld(json.dumps(doc).encode())["avus"] == from_jsonld(doc)["avus"]
        assert from_jsonld("{nope")["errors"][0]["message"].startswith("not valid JSON")
        assert from_jsonld([1])["errors"] == [
            {"path": "", "message": "document must be a JSON object"}
        ]
        assert from_jsonld({"@type": "sc:Thing"})["errors"] == [
            {"path": "@type", "message": "the document is not a schema.org Dataset"}
        ]
        graph = {
            "@context": doc["@context"],
            "@graph": [{"@type": "sc:Thing"}, {k: v for k, v in doc.items() if k != "@context"}],
        }
        assert from_jsonld(graph)["avus"] == from_jsonld(doc)["avus"]

    def test_http_and_https_schema_org(self):
        a = {"@type": "http://schema.org/Dataset", "http://schema.org/name": "n"}
        b = {"@type": ["https://schema.org/Dataset"], "https://schema.org/name": {"@value": "n"}}
        assert (
            triples(from_jsonld(a)["avus"])
            == triples(from_jsonld(b)["avus"])
            == [("croissant.name", "n", "")]
        )

    def test_limits_are_errors_and_kept(self):
        doc = valid_doc()
        doc["description"] = "x" * 2701
        doc["keywords"] = [{"@type": "sc:DefinedTerm", "name": "k", "termCode": "C:" + "1" * 250}]
        doc["name"] = "bad\x00name"
        r = from_jsonld(doc)
        assert ("croissant.description", "x" * 2701, "") in triples(r["avus"])
        assert has_msg(r["errors"], "croissant.description", "2701 characters")
        assert has_msg(r["errors"], "croissant.keyword.1", "unit has 252")
        assert has_msg(r["errors"], "croissant.name", "NUL")

    def test_single_values_vs_lists_and_odd_values(self):
        doc = {
            "@type": "sc:Dataset",
            "name": ["first", "second"],
            "license": [{"@id": "https://l"}],
            "keywords": [{"@type": "sc:DefinedTerm"}, 7],
            "inLanguage": [{"x": 1}],
            "identifier": {"@type": "PropertyValue", "value": "doi:10.1/x"},
            "datePublished": {"@value": "2020"},
            "url": {"weird": True},
        }
        r = from_jsonld(doc)
        got = avu_map(r["avus"])
        assert got["croissant.name"] == ("first", "")
        assert got["croissant.license.1"] == ("https://l", "")
        assert got["croissant.keyword.1"] == ("7", "")
        assert got["croissant.identifier"] == ("doi:10.1/x", "")
        assert got["croissant.datePublished"] == ("2020", "")
        assert "croissant.url" not in got
        w = r["warnings"]
        assert has_msg(w, "name", "first is kept")
        assert has_msg(w, "keywords[0]", "without a name")
        assert has_msg(w, "inLanguage[0]", "dropped")
        assert has_msg(w, "url", "unsupported")

    def test_rai_single_valued_joined(self):
        r = from_jsonld({"@type": "sc:Dataset", "rai:dataCollection": ["a", "b"]})
        assert avu_map(r["avus"])["croissant.rai.dataCollection"] == ("a\n\nb", "")
        assert has_msg(r["warnings"], "rai:dataCollection", "joined")

    def test_conforms_to_profiles_kept(self):
        r = from_jsonld(
            {
                "@type": "sc:Dataset",
                "conformsTo": [
                    CONFORMS_CORE,
                    CONFORMS_RAI,
                    "https://bioschemas.org/profiles/Dataset/1.0-RELEASE",
                ],
            }
        )
        assert triples(r["avus"]) == [
            ("croissant.conformsTo.1", "https://bioschemas.org/profiles/Dataset/1.0-RELEASE", "")
        ]

    def test_content_base_not_applied_inside_containers(self):
        doc = {
            "@type": "sc:Dataset",
            "distribution": [
                {"@type": "cr:FileObject", "@id": "z", "contentUrl": "https://h/root/a.zip"},
                {
                    "@type": "cr:FileObject",
                    "@id": "i",
                    "contentUrl": "https://h/root/in.csv",
                    "containedIn": {"@id": "z"},
                },
                {"@type": "cr:FileObject", "@id": "o", "contentUrl": "https://other/x.csv"},
            ],
        }
        got = avu_map(from_jsonld(doc, content_base="https://h/root/")["avus"])
        assert got["croissant.distribution.1.contentUrl"] == ("a.zip", "")
        assert got["croissant.distribution.2.contentUrl"] == ("https://h/root/in.csv", "")
        assert got["croissant.distribution.3.contentUrl"] == ("https://other/x.csv", "")

    def test_field_names_ids_and_sources(self):
        doc = {
            "@type": "sc:Dataset",
            "recordSet": [
                {
                    "@type": "cr:RecordSet",
                    "@id": "rs",
                    "field": [
                        {"@id": "rs/a", "dataType": "sc:Text", "source": "f"},
                        {
                            "@id": "elsewhere/b",
                            "dataType": "sc:Text",
                            "source": [{"fileObject": {"@id": "f"}}, {"fileObject": {"@id": "g"}}],
                        },
                        {"name": "c", "dataType": "sc:Text", "source": {"@id": "f"}},
                        {"description": "no name"},
                        "junk",
                        {
                            "@id": "rs/d",
                            "dataType": "sc:Text",
                            "source": {
                                "fileObject": {"@id": "f"},
                                "extract": {"column": "d", "x": 1},
                                "transform": [{"regex": "a"}, {"regex": "b"}, {"unArchive": True}],
                            },
                            "value": 3,
                            "isArray": False,
                            "arrayShape": "-1",
                        },
                    ],
                }
            ],
        }
        r = from_jsonld(doc)
        got = avu_map(r["avus"])
        assert got["croissant.recordSet.1.field.1.name"] == ("a", "")
        assert "croissant.recordSet.1.field.1.id" not in got
        assert got["croissant.recordSet.1.field.1.source"] == ("f", "")
        assert got["croissant.recordSet.1.field.2.name"] == ("b", "")
        assert got["croissant.recordSet.1.field.2.id"] == ("elsewhere/b", "")
        assert got["croissant.recordSet.1.field.3.source"] == ("f", "")
        assert got["croissant.recordSet.1.field.4.regex"] == ("a", "")
        assert got["croissant.recordSet.1.field.4.isArray"] == ("false", "")
        w = r["warnings"]
        assert has_msg(w, "recordSet[0].field[1].source", "first is kept")
        assert has_msg(w, "recordSet[0].field[3]", "without name")
        assert has_msg(w, "recordSet[0].field[4]", "unsupported field")
        assert has_msg(w, "recordSet[0].field[5].source.extract.x")
        assert has_msg(w, "recordSet[0].field[5].source.transform[1].regex", "first is kept")
        assert has_msg(w, "recordSet[0].field[5].source.transform[2].unArchive")
        assert has_msg(w, "recordSet[0].field[5].value", "not represented")

    def test_entries_without_ids_dropped(self):
        r = from_jsonld(
            {
                "@type": "sc:Dataset",
                "distribution": [{"@type": "cr:FileObject"}, 3],
                "recordSet": [{"@type": "cr:RecordSet"}],
                "creator": [{"@type": "Person"}],
            }
        )
        assert r["avus"] == []
        w = r["warnings"]
        assert has_msg(w, "distribution[0]", "without @id")
        assert has_msg(w, "distribution[1]", "unsupported")
        assert has_msg(w, "recordSet[0]", "without @id")
        assert has_msg(w, "creator[0]", "without a name")

    def test_quantities_and_geo_variants(self):
        doc = {
            "@type": "sc:Dataset",
            "spatialCoverage": ["Tucson", "Phoenix"],
            "geocr:spatialResolution": "30 m",
            "geocr:temporalResolution": {"@type": "QuantitativeValue", "unitText": "day"},
            "geocr:coordinateReferenceSystem": "EPSG:4326",
        }
        r = from_jsonld(doc)
        got = avu_map(r["avus"])
        assert got["croissant.geo.spatialCoverage.name"] == ("Tucson", "")
        assert got["croissant.geo.spatialResolution"] == ("30", "m")
        assert "croissant.geo.temporalResolution" not in got
        assert has_msg(r["warnings"], "spatialCoverage", "first spatialCoverage")
        assert has_msg(r["warnings"], "geocr:temporalResolution", "without a value")
        shape = from_jsonld(
            {"@type": "sc:Dataset", "spatialCoverage": {"@type": "GeoShape", "box": "1 2 3 4"}}
        )
        assert avu_map(shape["avus"])["croissant.geo.spatialCoverage.box"] == ("1 2 3 4", "")
        poly = from_jsonld(
            {
                "@type": "sc:Dataset",
                "spatialCoverage": {
                    "@type": "Place",
                    "geo": {"@type": "GeoShape", "polygon": "1 2 3 4 1 2"},
                },
            }
        )
        assert poly["avus"] == []
        assert has_msg(poly["warnings"], "spatialCoverage.geo", "unsupported geometry")


# ---------------------------------------------------------------------------
# Encoding formats, DuckDB types, suggestions, catalog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,fmt",
    [
        ("a.csv", "text/csv"),
        ("A.CSV", "text/csv"),
        ("a.tsv", "text/tab-separated-values"),
        ("a.json", "application/json"),
        ("a.jsonl", "application/jsonlines"),
        ("a.parquet", "application/x-parquet"),
        ("x/y/a.tif", "image/tiff"),
        ("a.tiff", "image/tiff"),
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.zip", "application/zip"),
        ("a.tar", "application/x-tar"),
        ("a.tar.gz", "application/gzip"),
        ("a.tgz", "application/gzip"),
        ("a.gz", "application/gzip"),
        ("a.nc", "application/x-netcdf"),
        ("a.h5", "application/x-hdf5"),
        ("a.hdf5", "application/x-hdf5"),
        ("cube.zarr", "application/vnd+zarr"),
        ("cube.zarr/", "application/vnd+zarr"),
        ("a.geojson", "application/geo+json"),
        ("lake.ducklake", "application/x-ducklake"),
        ("db.duckdb", "application/x-duckdb"),
        ("a.txt", "text/plain"),
        ("README.md", "text/markdown"),
        ("a.pdf", "application/pdf"),
        ("https://h/x.csv?raw=1#frag", "text/csv"),
        ("noext", None),
        ("a.unknownext", None),
        ("", None),
        (None, None),
        (".hidden", None),
    ],
)
def test_encoding_format_for(name, fmt):
    if name == ".hidden":
        assert encoding_format_for(name) is None
        return
    assert encoding_format_for(name) == fmt


def dt(t, shape=None, is_array=None):
    return {
        "dataType": t,
        "isArray": bool(shape) if is_array is None else is_array,
        "arrayShape": shape,
    }


@pytest.mark.parametrize(
    "type_name,expected",
    [
        ("BOOLEAN", dt("sc:Boolean")),
        ("bool", dt("sc:Boolean")),
        ("TINYINT", dt("cr:Int8")),
        ("SMALLINT", dt("cr:Int16")),
        ("INTEGER", dt("cr:Int32")),
        ("INT", dt("cr:Int32")),
        ("BIGINT", dt("cr:Int64")),
        ("INT8", dt("cr:Int64")),
        ("int8", dt("cr:Int8")),
        ("UTINYINT", dt("cr:UInt8")),
        ("USMALLINT", dt("cr:UInt16")),
        ("UINTEGER", dt("cr:UInt32")),
        ("UBIGINT", dt("cr:UInt64")),
        ("uint16", dt("cr:UInt16")),
        ("HUGEINT", dt("sc:Float")),
        ("UHUGEINT", dt("sc:Float")),
        ("DECIMAL(18,3)", dt("sc:Float")),
        ("decimal128(10, 2)", dt("sc:Float")),
        ("NUMERIC", dt("sc:Float")),
        ("FLOAT", dt("cr:Float32")),
        ("REAL", dt("cr:Float32")),
        ("DOUBLE", dt("cr:Float64")),
        ("double", dt("cr:Float64")),
        ("halffloat", dt("cr:Float16")),
        ("DATE", dt("sc:Date")),
        ("date32[day]", dt("sc:Date")),
        ("TIME", dt("sc:Time")),
        ("TIMETZ", dt("sc:Time")),
        ("TIME WITH TIME ZONE", dt("sc:Time")),
        ("time64[us]", dt("sc:Time")),
        ("TIMESTAMP", dt("sc:DateTime")),
        ("TIMESTAMP_NS", dt("sc:DateTime")),
        ("TIMESTAMPTZ", dt("sc:DateTime")),
        ("TIMESTAMP WITH TIME ZONE", dt("sc:DateTime")),
        ("timestamp[us, tz=UTC]", dt("sc:DateTime")),
        ("VARCHAR", dt("sc:Text")),
        ("VARCHAR(20)", dt("sc:Text")),
        ("UUID", dt("sc:Text")),
        ("INTERVAL", dt("sc:Text")),
        ("BLOB", dt("sc:Text")),
        ("JSON", dt("sc:Text")),
        ("ENUM('a', 'b')", dt("sc:Text")),
        ("MAP(VARCHAR, INTEGER)", dt("sc:Text")),
        ("string", dt("sc:Text")),
        ("GEOMETRY", dt("sc:GeoShape")),
        ("", dt("sc:Text")),
        (None, dt("sc:Text")),
        ("INTEGER[]", dt("cr:Int32", "-1")),
        ("VARCHAR[3]", dt("sc:Text", "3")),
        ("LIST(DOUBLE)", dt("cr:Float64", "-1")),
        ("list<item: int32>", dt("cr:Int32", "-1")),
        ("large_list<element: string>", dt("sc:Text", "-1")),
        ("fixed_size_list<item: float>[3]", dt("cr:Float32", "3")),
        ("INTEGER[][]", dt("cr:Int32", "-1,-1")),
        ("FLOAT[3][2]", dt("cr:Float32", "2,3")),
        ("LIST(INTEGER[4])", dt("cr:Int32", "-1,4")),
    ],
)
def test_duckdb_types(type_name, expected):
    assert duckdb_type_to_croissant(type_name) == expected


def test_duckdb_struct():
    out = duckdb_type_to_croissant(
        'STRUCT(speed DOUBLE, "wind dir" SMALLINT, tags VARCHAR[], pos STRUCT(x INT, y INT))'
    )
    assert out["dataType"] is None and out["isArray"] is False
    assert [s["name"] for s in out["subFields"]] == ["speed", "wind dir", "tags", "pos"]
    assert out["subFields"][0]["dataType"] == "cr:Float64"
    assert out["subFields"][1]["dataType"] == "cr:Int16"
    assert out["subFields"][2] == {
        "name": "tags",
        "dataType": "sc:Text",
        "isArray": True,
        "arrayShape": "-1",
    }
    assert [s["dataType"] for s in out["subFields"][3]["subFields"]] == ["cr:Int32", "cr:Int32"]
    arrow = duckdb_type_to_croissant("struct<a: int32, b: list<item: string>>")
    assert [(s["name"], s["dataType"], s["isArray"]) for s in arrow["subFields"]] == [
        ("a", "cr:Int32", False),
        ("b", "sc:Text", True),
    ]
    lst = duckdb_type_to_croissant("STRUCT(a INTEGER)[]")
    assert (
        lst["isArray"] is True and lst["arrayShape"] == "-1" and lst["subFields"][0]["name"] == "a"
    )


class TestSuggestDistribution:
    LAKE = {
        "alias": "neon",
        "root": "/iplant/home/shared/mesa/lakehouse/neon/neon-ducklake",
        "catalog": "ducklake/neon_catalog.ducklake",
        "data": "ducklake/data/",
    }

    def test_lake_with_tables(self):
        entries = [
            {
                "name": "neon_catalog.ducklake",
                "path": "ducklake/neon_catalog.ducklake",
                "is_folder": False,
                "size": 4096,
                "md5": "f" * 32,
            },
            {"name": "README.md", "path": "README.md", "is_folder": False, "size": 10},
        ]
        out = suggest_distribution(
            entries, lake=self.LAKE, tables=["main.sites", "met", {"schema": "raw", "table": "met"}]
        )
        ids = [e["id"]["value"] for e in out]
        assert ids == [
            "neon_catalog.ducklake",
            "ducklake-sites",
            "ducklake-main-met",
            "ducklake-raw-met",
            "README.md",
        ]
        cat = out[0]
        assert cat["encodingFormat"]["value"] == "application/x-ducklake"
        assert cat["contentUrl"]["value"] == "ducklake/neon_catalog.ducklake"
        assert cat["contentSize"] == {"value": "4096", "unit": "B"}
        assert cat["md5"]["value"] == "f" * 32
        assert out[1]["type"]["value"] == "FileSet"
        assert out[1]["encodingFormat"]["value"] == "application/x-parquet"
        assert [i["value"] for i in out[1]["includes"]] == ["ducklake/data/main/sites/**/*.parquet"]
        assert out[3]["includes"][0]["value"] == "ducklake/data/raw/met/**/*.parquet"
        assert out[4]["encodingFormat"]["value"] == "text/markdown"
        avus = model_to_avus({"distribution": out})
        assert avus[0] == A("croissant.distribution.1.id", "neon_catalog.ducklake")
        doc = to_jsonld(avus, item={"path": self.LAKE["root"], "name": "neon-ducklake"})
        assert doc["validation"]["summary"]["distribution"] == 5
        assert not [e for e in doc["validation"]["errors"] if e["path"].startswith("distribution")]

    def test_lake_tables_from_registry_entry(self):
        out = suggest_distribution([], lake=dict(self.LAKE, tables=["t1"]))
        assert [e["id"]["value"] for e in out] == ["neon_catalog.ducklake", "ducklake-t1"]
        assert out[0]["contentUrl"]["value"] == "ducklake/neon_catalog.ducklake"
        assert "contentSize" not in out[0]

    def test_lake_without_tables(self):
        entries = [
            {
                "name": f"p{i}.parquet",
                "path": f"ducklake/data/main/t/p{i}.parquet",
                "is_folder": False,
            }
            for i in range(8)
        ]
        out = suggest_distribution(entries, lake=self.LAKE)
        assert [e["id"]["value"] for e in out] == ["neon_catalog.ducklake", "ducklake-data"]
        assert out[1]["includes"][0]["value"] == "ducklake/data/**/*.parquet"

    def test_filesets_for_more_than_five(self):
        entries = [
            {"name": f"t{i}.tif", "path": f"data/tiles/t{i}.tif", "is_folder": False}
            for i in range(4)
        ]
        entries += [
            {"name": f"u{i}.tiff", "path": f"data/u{i}.tiff", "is_folder": False} for i in range(2)
        ]
        entries += [
            {"name": f"c{i}.csv", "path": f"data/c{i}.csv", "is_folder": False, "size": i}
            for i in range(5)
        ]
        entries += [
            {"name": f"r{i}.parquet", "path": f"r{i}.parquet", "is_folder": False} for i in range(7)
        ]
        out = suggest_distribution(entries)
        sets = [e for e in out if e["type"]["value"] == "FileSet"]
        assert len(sets) == 1
        assert sets[0]["id"]["value"] == "tif-files"
        assert sets[0]["encodingFormat"]["value"] == "image/tiff"
        assert [i["value"] for i in sets[0]["includes"]] == ["data/**/*.tif", "data/**/*.tiff"]
        objects = [e for e in out if e["type"]["value"] == "FileObject"]
        assert len(objects) == 12
        assert objects[0]["id"]["value"] == "data/c0.csv"
        assert objects[0]["name"]["value"] == "c0.csv"
        assert objects[0]["contentSize"] == {"value": "0", "unit": "B"}

    def test_skips_and_special_cases(self):
        entries = [
            {"name": ".hidden", "path": ".hidden", "is_folder": False},
            {"name": "x", "path": ".mesa/ducklake/snap.parquet", "is_folder": False},
            {"name": "data", "path": "data", "is_folder": True},
            {"name": "cube.zarr", "path": "data/cube.zarr", "is_folder": True},
            {"name": "my file.bin", "path": "my file.bin", "is_folder": False},
            {"name": "dup", "path": "a b", "is_folder": False},
            {"name": "dup", "path": "a_b", "is_folder": False},
            "garbage",
            {"name": "", "path": "", "is_folder": False},
        ]
        out = suggest_distribution(entries)
        ids = [e["id"]["value"] for e in out]
        assert ids == ["data/cube.zarr", "a_b", "a_b-2", "my_file.bin"]
        assert out[0]["includes"][0]["value"] == "data/cube.zarr/**"
        assert out[3]["encodingFormat"]["value"] == "application/octet-stream"
        assert out[3]["contentUrl"]["value"] == "my file.bin"
        assert suggest_distribution([]) == []


class TestKeyCatalog:
    def test_levels(self):
        rows = key_catalog()
        levels = {r["level"] for r in rows}
        assert levels == {"dataset", "rai", "geo", "file", "distribution", "recordset"}
        for level in levels:
            assert all(r["level"] == level for r in key_catalog(level))
        assert key_catalog("nope") == []

    def test_required_and_cardinality(self):
        rows = {r["attribute"]: r for r in key_catalog()}
        for attr in (
            "croissant.name",
            "croissant.description",
            "croissant.url",
            "croissant.license.N",
            "croissant.creator.N.name",
            "croissant.datePublished",
            "croissant.distribution.N.id",
            "croissant.recordSet.N.id",
            "croissant.recordSet.N.field.M.name",
        ):
            assert rows[attr]["required"], attr
        assert rows["croissant.license.N"]["cardinality"] == "many"
        assert rows["croissant.name"]["cardinality"] == "one"
        assert rows["croissant.rai.dataBiases.N"]["cardinality"] == "many"
        assert rows["croissant.rai.dataCollection"]["cardinality"] == "one"
        assert rows["croissant.keyword.N"]["unit"] == "optional CURIE or IRI"
        assert rows["croissant.geo.spatialResolution"]["unit"] == "unit text"
        assert len([r for r in rows.values() if r["level"] == "rai"]) == 20


def test_fixture_files_are_well_formed():
    for path in golden_paths():
        f = load_json(path)
        assert set(f) == {
            "case",
            "description",
            "item",
            "avus",
            "document",
            "sources",
            "valid",
            "mlcroissant",
            "roundtrip",
        }
        assert f["mlcroissant"] in ("complete", "incomplete")
    assert len(golden_paths()) >= 4
    notice = " ".join((UPSTREAM / "NOTICE").read_text(encoding="utf-8").split())
    assert "Apache License, Version 2.0" in notice
    for path in upstream_paths():
        assert path.name in notice


# ---------------------------------------------------------------------------
# Regression tests for the review findings (core 1.1.0)
# ---------------------------------------------------------------------------

DS = [
    A("croissant.name", "N"),
    A("croissant.description", "D"),
    A("croissant.license.1", "https://creativecommons.org/licenses/by/4.0/"),
    A("croissant.creator.1.name", "J"),
    A("croissant.datePublished", "2026-01-01"),
]


def field_by_id(doc, fid):
    for rs in doc.get("recordSet", []):
        for f in rs.get("field", []):
            if f["@id"] == fid:
                return f
            for s in f.get("subField", []):
                if s["@id"] == fid:
                    return s
    raise KeyError(fid)


class TestContentUrlRoundTrip:
    """Relative contentUrls are stored decoded, so a round trip does not re-encode them."""

    ITEM = {"path": "/iplant/home/shared/mesa/my data #é", "name": "my data #é"}
    RAW = "sub dir/a b#1 é.csv"

    def _avus(self):
        return DS + [
            A("croissant.distribution.1.id", "f"),
            A("croissant.distribution.1.contentUrl", self.RAW),
            A("croissant.distribution.1.md5", "d41d8cd98f00b204e9800998ecf8427e"),
        ]

    def test_export_import_export_is_stable(self):
        first = to_jsonld(self._avus(), item=self.ITEM)["document"]
        url1 = first["distribution"][0]["contentUrl"]
        assert url1 == (
            "https://data.cyverse.org/dav-anon/iplant/home/shared/mesa/"
            "my%20data%20%23%C3%A9/sub%20dir/a%20b%231%20%C3%A9.csv"
        )
        back = from_jsonld(first, content_base=core._content_base(self.ITEM))
        assert avu_map(back["avus"])["croissant.distribution.1.contentUrl"] == (self.RAW, "")
        second = to_jsonld(back["avus"], item=self.ITEM)["document"]
        assert second["distribution"][0]["contentUrl"] == url1

    def test_item_gives_the_content_base(self):
        first = to_jsonld(self._avus(), item=self.ITEM)["document"]
        back = from_jsonld(first, item=dict(self.ITEM, is_folder=True))
        assert avu_map(back["avus"])["croissant.distribution.1.contentUrl"][0] == self.RAW

    def test_relative_percent_encoded_url_is_decoded(self):
        doc = to_jsonld(self._avus(), item=self.ITEM)["document"]
        doc["distribution"][0]["contentUrl"] = "a%20b.csv"
        back = from_jsonld(doc)
        assert avu_map(back["avus"])["croissant.distribution.1.contentUrl"][0] == "a b.csv"
        again = to_jsonld(back["avus"], item=self.ITEM)["document"]
        assert again["distribution"][0]["contentUrl"].endswith("/my%20data%20%23%C3%A9/a%20b.csv")

    def test_urls_that_would_change_stay_absolute(self):
        base = core._content_base(self.ITEM)
        doc = to_jsonld(self._avus(), item=self.ITEM)["document"]
        for url in (base + "/x%2Fy.csv", base + "/a(b).csv", "https://elsewhere.org/a%20b"):
            doc["distribution"][0]["contentUrl"] = url
            back = from_jsonld(doc, content_base=base)
            assert avu_map(back["avus"])["croissant.distribution.1.contentUrl"][0] == url
            regen = to_jsonld(back["avus"], item=self.ITEM)["document"]
            assert regen["distribution"][0]["contentUrl"] == url


class TestQuantitativeValue:
    AVUS = DS + [A("croissant.geo.spatialResolution", "30", "m")]

    def test_emits_schema_org_value(self):
        d = gen(self.AVUS)["document"]
        assert d["geocr:spatialResolution"] == {
            "@type": "sc:QuantitativeValue",
            "sc:value": 30,
            "unitText": "m",
        }
        # Both spellings import.
        assert avu_map(from_jsonld(d)["avus"])["croissant.geo.spatialResolution"] == ("30", "m")
        d["geocr:spatialResolution"] = {"@type": "QuantitativeValue", "value": 30, "unitText": "m"}
        assert avu_map(from_jsonld(d)["avus"])["croissant.geo.spatialResolution"] == ("30", "m")

    def test_rdf_predicate_is_schema_value(self):
        rdflib = pytest.importorskip("rdflib")
        doc = gen(self.AVUS)["document"]
        g = rdflib.Graph()
        g.parse(data=json.dumps(doc), format="json-ld", base="https://example.org/base/")
        sv = rdflib.URIRef("https://schema.org/value")
        cv = rdflib.URIRef("http://mlcommons.org/croissant/value")
        qv = [s for s, _p, o in g.triples((None, sv, None)) if o.toPython() == 30]
        assert qv
        assert list(g.triples((qv[0], cv, None))) == []


class TestTransformJsonPath:
    def test_key_is_in_the_catalog_and_round_trips(self):
        rows = {r["attribute"]: r for r in key_catalog("recordset")}
        assert "croissant.recordSet.N.field.M.transformJsonPath" in rows
        avus = [
            A("croissant.recordSet.1.id", "r"),
            A("croissant.recordSet.1.field.1.name", "x"),
            A("croissant.recordSet.1.field.1.transformJsonPath", "bytes"),
        ]
        p = parse_avus(avus)
        assert p["unknown"] == [] and p["warnings"] == []
        assert triples(model_to_avus(p["model"])) == triples(avus)

    def test_mnist_image_keeps_its_transform(self):
        src = load_json(UPSTREAM / "v1.1_huggingface-mnist.json")
        r = from_jsonld(src)
        assert avu_map(r["avus"])["croissant.recordSet.2.field.2.transformJsonPath"] == (
            "bytes",
            "",
        )
        assert not has_msg(r["warnings"], contains="transform not represented")
        d = to_jsonld(r["avus"], item=FOLDER)["document"]
        assert field_by_id(d, "default/image")["source"] == field_by_id(src, "default/image")[
            "source"
        ]

    def test_squad_answers_keep_their_members(self):
        src = load_json(UPSTREAM / "v1.1_huggingface-squad_v2.json")
        d = to_jsonld(from_jsonld(src)["avus"], item=FOLDER)["document"]
        for member in ("text", "answer_start"):
            fid = f"squad_v2/answers/{member}"
            assert field_by_id(d, fid)["source"] == field_by_id(src, fid)["source"]
            assert field_by_id(d, fid)["source"]["transform"] == {"jsonPath": member}

    def test_second_json_path_transform_warns(self):
        doc = valid_doc()
        doc["recordSet"][0]["field"][0]["source"]["transform"] = [
            {"jsonPath": "a"},
            {"jsonPath": "b"},
        ]
        r = from_jsonld(doc)
        assert [v for k, v in avu_map(r["avus"]).items() if k.endswith("transformJsonPath")] == [
            ("a", "")
        ]
        assert has_msg(r["warnings"], contains="several jsonPath transforms")

    def test_json_query_on_a_whole_file_is_still_extract_json_path(self):
        doc = valid_doc()
        doc["recordSet"][0]["field"][0]["source"] = {
            "fileObject": {"@id": "f"},
            "transform": {"jsonQuery": "$.x"},
        }
        m = avu_map(from_jsonld(doc)["avus"])
        assert m["croissant.recordSet.1.field.1.jsonPath"] == ("$.x", "")
        assert not any(k.endswith("transformJsonPath") for k in m)

    def test_struct_members_with_transform_inherit_source_and_column(self):
        typed = duckdb_type_to_croissant("STRUCT(a INTEGER, b VARCHAR)")
        model = parse_avus(DS)["model"]
        model["distribution"] = [
            {
                "id": "t",
                "type": "FileSet",
                "encodingFormat": "application/x-parquet",
                "includes": ["data/*.parquet"],
            }
        ]
        model["recordSet"] = [
            {
                "id": "tbl",
                "field": [
                    {
                        "name": "s",
                        "source": "t",
                        "column": "s",
                        "subField": [
                            {
                                "name": s["name"],
                                "dataType": s["dataType"],
                                "transformJsonPath": s["name"],
                            }
                            for s in typed["subFields"]
                        ],
                    }
                ],
            }
        ]
        out = to_jsonld(model_to_avus(model), item=FOLDER)
        assert out["validation"]["valid"], out["validation"]["errors"]
        assert not has_msg(out["validation"]["warnings"], contains="no source")
        sub = field_by_id(out["document"], "tbl/s/a")
        assert sub["source"] == {
            "fileSet": {"@id": "t"},
            "extract": {"column": "s"},
            "transform": {"jsonPath": "a"},
        }


class TestSubFieldSources:
    def test_file_level_subfields_get_the_file(self):
        avus = DS + [
            A("croissant.recordSet.1.id", "pts"),
            A("croissant.recordSet.1.field.1.name", "gps"),
            A("croissant.recordSet.1.field.1.dataType", "sc:GeoCoordinates"),
            A("croissant.recordSet.1.field.1.subField.1.name", "latitude"),
            A("croissant.recordSet.1.field.1.subField.1.dataType", "sc:Float"),
            A("croissant.recordSet.1.field.1.subField.1.column", "lat"),
            A("croissant.recordSet.1.field.1.subField.2.name", "longitude"),
            A("croissant.recordSet.1.field.1.subField.2.dataType", "sc:Float"),
            A("croissant.recordSet.1.field.1.subField.2.column", "lon"),
        ]
        item = {"path": "/iplant/home/shared/x/pts.csv", "name": "pts.csv", "is_folder": False}
        out = to_jsonld(avus, item=item)
        assert out["validation"]["errors"] == []
        gps = out["document"]["recordSet"][0]["field"][0]
        assert "source" not in gps
        assert gps["subField"][0]["source"] == {
            "fileObject": {"@id": "pts.csv"},
            "extract": {"column": "lat"},
        }

    def test_own_subfield_source_wins(self):
        avus = MINIMAL + [
            A("croissant.distribution.2.id", "g"),
            A("croissant.distribution.2.contentUrl", "g.csv"),
            A("croissant.distribution.2.sha256", "cd" * 32),
            A("croissant.recordSet.1.field.2.name", "p"),
            A("croissant.recordSet.1.field.2.source", "f"),
            A("croissant.recordSet.1.field.2.subField.1.name", "q"),
            A("croissant.recordSet.1.field.2.subField.1.dataType", "sc:Text"),
            A("croissant.recordSet.1.field.2.subField.1.source", "g"),
            A("croissant.recordSet.1.field.2.subField.1.column", "q"),
        ]
        sub = field_by_id(gen(avus)["document"], "rs/p/q")
        assert sub["source"] == {"fileObject": {"@id": "g"}, "extract": {"column": "q"}}

    def _struct(self, parent_source=True, data=False):
        avus = MINIMAL + [
            A("croissant.recordSet.1.field.2.name", "s"),
            A("croissant.recordSet.1.field.2.subField.1.name", "a"),
            A("croissant.recordSet.1.field.2.subField.1.dataType", "cr:Int32"),
        ]
        if parent_source:
            avus += [
                A("croissant.recordSet.1.field.2.source", "f"),
                A("croissant.recordSet.1.field.2.column", "s"),
            ]
        if data:
            avus.append(A("croissant.recordSet.1.data", '[{"rs/x": 1}]'))
        return gen(avus)

    def test_struct_members_without_source_warn(self):
        # The from_schema / DuckDB viewer STRUCT shape (convention section 9).
        out = self._struct()
        v = out["validation"]
        assert v["valid"], v["errors"]
        assert not has_msg(v["errors"], contains="needs a source")
        assert has_msg(v["warnings"], "recordSet[0].field[1].subField[0].source", "parent field")
        assert "source" not in field_by_id(out["document"], "rs/s/a")
        # The file-level form of the same shape.
        item = {"path": "/iplant/home/shared/x/t.parquet", "name": "t.parquet", "is_folder": False}
        avus = DS + [
            A("croissant.recordSet.1.id", "t"),
            A("croissant.recordSet.1.field.1.name", "s"),
            A("croissant.recordSet.1.field.1.column", "s"),
            A("croissant.recordSet.1.field.1.subField.1.name", "a"),
            A("croissant.recordSet.1.field.1.subField.1.dataType", "cr:Int32"),
        ]
        fv = to_jsonld(avus, item=item)["validation"]
        assert fv["errors"] == []
        assert has_msg(fv["warnings"], "recordSet[0].field[0].subField[0].source", "parent")

    def test_member_without_any_source_is_still_an_error(self):
        v = self._struct(parent_source=False)["validation"]
        assert has_msg(v["errors"], "recordSet[0].field[1].subField[0].source", "needs a source")
        assert self._struct(parent_source=False, data=True)["validation"]["valid"]


class TestProvenanceObjects:
    def test_shapes_and_rdf(self):
        d = gen(
            MINIMAL
            + [
                A("croissant.wasGeneratedBy", "https://example.org/run/1"),
                A("croissant.wasDerivedFrom.1", "https://example.org/src"),
                A("croissant.wasDerivedFrom.2", "urn:dataset:ImageNet"),
                A("croissant.wasDerivedFrom.3", "a field survey"),
            ]
        )["document"]
        assert d["prov:wasGeneratedBy"] == {"@id": "https://example.org/run/1"}
        assert d["prov:wasDerivedFrom"] == [
            {"@id": "https://example.org/src"},
            {"@id": "urn:dataset:ImageNet"},
            {"@type": "prov:Entity", "prov:label": "a field survey"},
        ]
        rdflib = pytest.importorskip("rdflib")
        g = rdflib.Graph()
        g.parse(data=json.dumps(d), format="json-ld", base="https://example.org/base/")
        prov = rdflib.Namespace("http://www.w3.org/ns/prov#")
        derived = set(g.objects(None, prov.wasDerivedFrom))
        assert rdflib.URIRef("https://example.org/src") in derived
        generated = set(g.objects(None, prov.wasGeneratedBy))
        assert rdflib.URIRef("https://example.org/run/1") in generated

    def test_own_export_reimports_without_loss(self):
        avus = MINIMAL + [
            A("croissant.wasGeneratedBy", "pipeline run"),
            A("croissant.wasDerivedFrom.1", "https://example.org/src"),
            A("croissant.wasDerivedFrom.2", "a field survey"),
        ]
        back = from_jsonld(gen(avus)["document"], content_base=core._content_base(FOLDER))
        assert not has_msg(back["warnings"], contains="reduced to text")
        m = avu_map(back["avus"])
        assert m["croissant.wasGeneratedBy"] == ("pipeline run", "")
        assert m["croissant.wasDerivedFrom.1"] == ("https://example.org/src", "")
        assert m["croissant.wasDerivedFrom.2"] == ("a field survey", "")

    def test_crosswalk_generated_by_is_an_iri_node(self):
        cw = crosswalk([A("mesa.generated_by", "https://github.com/idss-mesa/neon-ducklake")])
        assert cw["prov:wasGeneratedBy"]["value"] == {
            "@id": "https://github.com/idss-mesa/neon-ducklake"
        }
        assert triples(cw["prov:wasGeneratedBy"]["avus"]) == [
            ("croissant.wasGeneratedBy", "https://github.com/idss-mesa/neon-ducklake", "")
        ]


class TestFlatGeoLocation:
    def test_flat_box_and_place(self):
        cw = crosswalk(
            [
                A("datacite.geoLocationBox", "31.7,-110.9,31.9,-110.7"),
                A("datacite.geoLocationPlace", "SRER"),
            ]
        )
        sc = cw["spatialCoverage"]
        assert sc["value"] == {
            "@type": "sc:Place",
            "name": "SRER",
            "geo": {"@type": "sc:GeoShape", "box": "31.7 -110.9 31.9 -110.7"},
        }
        assert sc["source"] == "datacite.geoLocationBox"
        assert triples(sc["avus"]) == [
            ("croissant.geo.spatialCoverage.name", "SRER", ""),
            ("croissant.geo.spatialCoverage.box", "31.7 -110.9 31.9 -110.7", ""),
        ]
        point = crosswalk([A("datacite.geoLocationPoint", "31.8,-110.8")])["spatialCoverage"]
        assert point["value"]["geo"] == {
            "@type": "sc:GeoCoordinates",
            "latitude": 31.8,
            "longitude": -110.8,
        }

    def test_indexed_entry_wins(self):
        cw = crosswalk(
            [
                A("datacite.geoLocationBox", "1,2,3,4"),
                A("datacite.geoLocationPlace", "Flat"),
                A("datacite.geoLocation.1.place", "Indexed"),
            ]
        )
        assert cw["spatialCoverage"]["value"] == {"@type": "sc:Place", "name": "Indexed"}
        assert cw["spatialCoverage"]["source"] == "datacite.geoLocation.1.place"


class TestRecordSetSourceWarning:
    def test_record_set_sources_warn(self):
        avus = MINIMAL + [
            A("croissant.recordSet.2.id", "r2"),
            A("croissant.recordSet.2.field.1.name", "c"),
            A("croissant.recordSet.2.field.1.dataType", "sc:Text"),
            A("croissant.recordSet.2.field.1.source", "rs"),
            A("croissant.recordSet.2.field.2.name", "d"),
            A("croissant.recordSet.2.field.2.dataType", "sc:Text"),
            A("croissant.recordSet.2.field.2.source", "rs"),
            A("croissant.recordSet.2.field.2.column", "nope"),
            A("croissant.recordSet.2.field.3.name", "e"),
            A("croissant.recordSet.2.field.3.dataType", "sc:Integer"),
            A("croissant.recordSet.2.field.3.source", "rs"),
            A("croissant.recordSet.2.field.3.column", "x"),
        ]
        out = gen(avus)
        v = out["validation"]
        assert v["valid"], v["errors"]
        for j in (0, 1):
            assert has_msg(v["warnings"], f"recordSet[1].field[{j}].source", "mlcroissant 1.1")
        # A column naming a field of the record set becomes a field reference: no warning.
        assert not has_msg(v["warnings"], "recordSet[1].field[2].source", "mlcroissant 1.1")
        assert out["document"]["recordSet"][1]["field"][2]["source"] == {"field": {"@id": "rs/x"}}

    def test_unknown_record_set_errors_without_the_warning(self):
        doc = valid_doc()
        doc["recordSet"][0]["field"][0]["source"] = {"recordSet": {"@id": "missing"}}
        v = validate(doc)
        assert has_msg(v["errors"], "recordSet[0].field[0].source", "unknown record set")
        assert not has_msg(v["warnings"], "recordSet[0].field[0].source", "mlcroissant 1.1")


class TestFileDocumentImport:
    ITEM = {
        "path": "/iplant/home/shared/mesa/demo/ratings file.csv",
        "name": "ratings file.csv",
        "is_folder": False,
        "size": 1234,
        "md5": "0" * 32,
    }
    AVUS = DS + [
        A("croissant.file.description", "Ratings, one row per rating."),
        A("croissant.file.sha256", "ab" * 32),
        A("croissant.recordSet.1.id", "ratings"),
        A("croissant.recordSet.1.field.1.name", "user_id"),
        A("croissant.recordSet.1.field.1.dataType", "sc:Integer"),
        A("croissant.recordSet.1.field.1.column", "userId"),
    ]

    def test_file_keys_survive_a_round_trip(self):
        doc = to_jsonld(self.AVUS, item=self.ITEM)["document"]
        back = from_jsonld(doc, item=self.ITEM)
        assert back["errors"] == []
        assert not has_msg(back["warnings"], "distribution")
        got = [t for t in triples(back["avus"]) if t[0] != "croissant.url"]
        assert got == triples(self.AVUS)
        assert not any(a["attribute"].startswith("croissant.distribution.") for a in back["avus"])
        regen = to_jsonld(back["avus"], item=self.ITEM)["document"]
        assert regen["distribution"] == doc["distribution"]
        assert regen["recordSet"] == doc["recordSet"]

    def test_only_the_file_entry_is_kept(self):
        doc = to_jsonld(self.AVUS, item=self.ITEM)["document"]
        other = {
            "@type": "cr:FileObject",
            "@id": "other.csv",
            "name": "other.csv",
            "contentUrl": "https://example.org/other.csv",
            "encodingFormat": "text/csv",
            "md5": "1" * 32,
        }
        doc["distribution"] = [other, doc["distribution"][0]]
        doc["distribution"][1]["md5"] = "2" * 32  # not the stat md5: kept
        doc["distribution"][1]["encodingFormat"] = "text/plain"  # not the default: kept
        back = from_jsonld(doc, item=self.ITEM)
        m = avu_map(back["avus"])
        assert m["croissant.file.description"] == ("Ratings, one row per rating.", "")
        assert m["croissant.file.md5"] == ("2" * 32, "")
        assert m["croissant.file.encodingFormat"] == ("text/plain", "")
        assert not any(k.startswith("croissant.distribution.") for k in m)
        assert [w for w in back["warnings"] if w["path"] == "distribution"] == [
            {
                "path": "distribution",
                "message": "a file's document describes only the file; distribution entry "
                "'other.csv' dropped",
            }
        ]

    def test_single_foreign_entry_is_the_file(self):
        doc = valid_doc()  # one FileObject "f" with fields reading it
        back = from_jsonld(doc, item={"path": "/iplant/home/x/data.csv", "name": "data.csv",
                                      "is_folder": False})
        m = avu_map(back["avus"])
        assert m["croissant.file.name"] == ("f", "")
        assert not any(k.endswith(".source") for k in m)  # fields default to the file
        regen = to_jsonld(back["avus"], item={"path": "/iplant/home/x/data.csv",
                                              "name": "data.csv", "is_folder": False})
        assert regen["validation"]["errors"] == []

    def test_folder_import_is_unchanged(self):
        doc = to_jsonld(self.AVUS, item=self.ITEM)["document"]
        back = from_jsonld(doc)
        assert any(a["attribute"].startswith("croissant.distribution.1.") for a in back["avus"])


class TestBlankValues:
    def test_import_leaves_blank_values_out(self):
        doc = valid_doc()
        doc["alternateName"] = " "
        doc["keywords"] = ["  ", "ok", {"@type": "DefinedTerm", "name": "\t", "termCode": "X:1"}]
        doc["creator"] = [{"@type": "sc:Person", "name": " "}, {"@type": "sc:Person", "name": "B"}]
        doc["license"] = ["https://creativecommons.org/licenses/by/4.0/", " "]
        r = from_jsonld(doc)
        assert r["errors"] == []
        assert all(a["value"].strip() for a in r["avus"])
        m = avu_map(r["avus"])
        assert "croissant.alternateName" not in m
        assert [v for k, v in m.items() if k.startswith("croissant.keyword")] == [("ok", "")]
        assert m["croissant.creator.1.name"] == ("B", "")
        assert "croissant.creator.2.name" not in m
        assert has_msg(r["warnings"], "alternateName", "blank value ignored")
        assert has_msg(r["warnings"], "creator[0].name", "blank value ignored")
        assert has_msg(r["warnings"], contains="blank value ignored")

    def test_codec_drops_blank_values(self):
        avus = [
            A("croissant.name", "N"),
            A("croissant.alternateName", "   "),
            A("croissant.keyword.1", " "),
            A("croissant.creator.1.name", "\t"),
        ]
        p = parse_avus(avus)
        assert p["model"] == {"name": {"value": "N", "unit": ""}}
        assert p["unknown"] == []
        for attr in ("croissant.alternateName", "croissant.keyword.1", "croissant.creator.1.name"):
            assert has_msg(p["warnings"], attr, "removed on the next save")
        assert triples(model_to_avus({"name": "N", "alternateName": " ", "keyword": [" "]})) == [
            ("croissant.name", "N", "")
        ]
        # Non-blank values are never trimmed.
        assert triples(model_to_avus({"name": " N "})) == [("croissant.name", " N ", "")]
