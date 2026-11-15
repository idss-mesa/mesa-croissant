"""The mesa_croissant_* MESA-MCP tools, called directly with iRODS and DuckLake mocked.

Follows mesa-mcp's tests/test_datacite_tools.py pattern: the handlers are plain async
callables; assert_allowed, default_pool, resolve_path_target, list/add/delete and
record_avu_changes (and the plugin's exact-triple _remove_exact) are monkeypatched on the
plugin module, so nothing touches iRODS.
Skipped when mesa-mcp is not installed (the core tests do not need it).
"""

from __future__ import annotations

import asyncio
import copy
import json
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import FIXTURES, golden_paths, import_paths, load_json, upstream_paths

ROOT = Path(__file__).resolve().parent.parent


def test_entry_point_is_declared() -> None:
    """The plugin is wired through the mesa_mcp.tools entry-point group."""
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    eps = meta["project"]["entry-points"]["mesa_mcp.tools"]
    assert eps == {"croissant": "mesa_croissant.mcp_tools"}
    assert any(d.startswith("mesa-mcp") for d in meta["project"]["optional-dependencies"]["mcp"])


mesa_mcp = pytest.importorskip("mesa_mcp", reason="mesa-mcp is not installed")

from mesa_mcp.auth.models import AuthValue  # noqa: E402
from mesa_mcp.ducklake.client import DuckLakeMirrorError  # noqa: E402
from mesa_mcp.errors import ToolError  # noqa: E402

import mesa_croissant.mcp_tools as mt  # noqa: E402
from mesa_croissant import core  # noqa: E402

FOLDER = "/iplant/home/shared/mesa/demo"
FILE = "/iplant/home/shared/mesa/demo/ratings.csv"
AUTH = AuthValue(username="tswetnam", zone="iplant", password="x")
GOLDEN = load_json(FIXTURES / "golden_demo_folder.json")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def avu(attribute: str, value: str, unit: str = "") -> dict[str, str]:
    return {"attribute": attribute, "value": value, "unit": unit}


class FakeIrods:
    """In-memory AVU store standing in for one iRODS path."""

    def __init__(self, avus: list[dict[str, str]], target: str = "collection",
                 size: int | None = None, checksum: str | None = None):
        self.avus = [dict(a) for a in avus]
        self.target = target
        self.added: list[dict[str, str]] = []
        self.removed: list[dict[str, str]] = []
        self.fail_add: set[str] = set()
        self.fail_remove: set[str] = set()
        self.session = MagicMock(name="iRODSSession")
        obj = MagicMock(size=size, checksum=checksum)
        self.session.data_objects.get.return_value = obj

    def list(self, session: Any, path: str, target: str) -> list[dict[str, Any]]:
        return [{"id": i, **a} for i, a in enumerate(self.avus, 1)]

    def add(self, session: Any, path: str, target: str, a: dict[str, str]) -> dict[str, str]:
        if a["attribute"] in self.fail_add:
            raise ToolError(code="irods_error", message=f"add {a['attribute']} failed")
        w = {"attribute": a["attribute"].strip(), "value": a["value"].strip(),
             "unit": (a.get("unit") or "").strip()}
        self.avus.append(w)
        self.added.append(w)
        return w

    def delete(self, session: Any, path: str, target: str, a: dict[str, str]) -> dict[str, str]:
        """Exact-triple removal, like mt._remove_exact (iRODS raises for a missing AVU)."""
        if a["attribute"] in self.fail_remove:
            raise ToolError(code="irods_error", message=f"remove {a['attribute']} failed")
        self.avus.remove({"attribute": a["attribute"], "value": a["value"], "unit": a["unit"]})
        self.removed.append(dict(a))
        return dict(a)


@pytest.fixture
def irods(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {}

    def install(avus: list[dict[str, str]], **kw: Any) -> FakeIrods:
        fake = FakeIrods(avus, **kw)
        state["fake"] = fake
        monkeypatch.setattr(mt, "assert_allowed", lambda target, auth: target.rstrip("/"))
        monkeypatch.setattr(mt, "default_pool", lambda: MagicMock(get=lambda a: fake.session))
        monkeypatch.setattr(mt, "resolve_path_target", lambda s, p: fake.target)
        monkeypatch.setattr(mt, "list_avus_for_path", fake.list)
        monkeypatch.setattr(mt, "add_avu_to_irods", fake.add)
        monkeypatch.setattr(mt, "_remove_exact", fake.delete)
        mirror = AsyncMock(return_value=None)
        monkeypatch.setattr(mt, "record_avu_changes", mirror)
        fake.mirror = mirror  # type: ignore[attr-defined]
        return fake

    return install


def triples(avus: list[dict[str, str]]) -> set[tuple[str, str, str]]:
    return {(a["attribute"], a["value"], a["unit"]) for a in avus}


# -- registration ------------------------------------------------------------------------------


def test_tools_registered_with_croissant_surface() -> None:
    from mesa_mcp.server import get_tool

    for name in mt.TOOL_NAMES:
        spec = get_tool(name)
        assert spec.meta == {"io.mesa/surface": "croissant"}
        assert spec.input_model is not None and spec.output_model is not None


def test_tool_definitions_pass_mesa_mcp_conformance() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from mesa_mcp.config import Config
    from mesa_mcp.server import MesaServer

    defs = {t.name: t for t in MesaServer(config=Config())._tool_definitions()}
    for name in mt.TOOL_NAMES:
        t = defs[name]
        assert t.input_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        jsonschema.Draft202012Validator.check_schema(t.input_schema)
        jsonschema.Draft202012Validator.check_schema(t.output_schema)
        assert t.meta["io.mesa/surface"] == "croissant"
    apply_props = defs[mt.APPLY].input_schema["properties"]
    assert apply_props["dry_run"]["default"] is True
    assert apply_props["mode"]["default"] == "merge"


def test_template_through_the_server_matches_its_output_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from mesa_mcp.config import Config
    from mesa_mcp.server import MesaServer

    server = MesaServer(config=Config())
    spec = next(t for t in server._tool_definitions() if t.name == mt.TEMPLATE)
    payload = run(server.call(mt.TEMPLATE, {"level": "dataset"}))
    jsonschema.Draft202012Validator(spec.output_schema).validate(payload)
    with pytest.raises(ToolError) as exc:
        run(server.call(mt.TEMPLATE, {"level": "nope"}))
    assert exc.value.code == "invalid_argument"


# -- mesa_croissant_template -------------------------------------------------------------------


def test_template_lists_required_dataset_keys() -> None:
    res = run(mt.handle_template(mt.TemplateInput(level="dataset")))
    mt.TemplateOutput.model_validate(res)
    required = {k["attribute"] for k in res["keys"] if k["required"]}
    assert {"croissant.name", "croissant.description", "croissant.url", "croissant.license.N",
            "croissant.creator.N.name", "croissant.datePublished"} <= required
    assert {k["level"] for k in res["keys"]} == {"dataset"}
    assert res["convention_version"] == core.CONVENTION_VERSION
    assert res["conforms_to"]["core"] == "http://mlcommons.org/croissant/1.1"


def test_template_all_levels() -> None:
    res = run(mt.handle_template(mt.TemplateInput()))
    levels = {k["level"] for k in res["keys"]}
    assert {"dataset", "rai", "geo", "file", "distribution", "recordset"} <= levels
    rs = run(mt.handle_template(mt.TemplateInput(level="recordset")))
    attrs = {k["attribute"] for k in rs["keys"]}
    assert "croissant.recordSet.N.field.M.dataType.K" in attrs
    assert len(rs["keys"]) < len(res["keys"])


# -- mesa_croissant_export ---------------------------------------------------------------------


def test_export_folder_matches_the_golden_document(irods) -> None:
    fake = irods(GOLDEN["avus"] + [avu("ipc_UUID", "abc"), avu("mesa.enabled", "true")])
    res = run(mt.handle_export(mt.ExportInput(path=FOLDER), auth_value=AUTH))
    mt.ExportOutput.model_validate(res)
    assert res["document"] == GOLDEN["document"]
    assert res["sources"] == GOLDEN["sources"]
    assert res["validation"]["valid"] is True
    assert res["croissant_avu_count"] == len(GOLDEN["avus"])
    assert res["path_target_type"] == "collection"
    fake.session.data_objects.get.assert_not_called()


def test_export_file_uses_stat_size_and_md5(irods) -> None:
    golden = load_json(FIXTURES / "golden_file_level.json")
    irods(golden["avus"], target="data_object", size=golden["item"]["size"],
          checksum=golden["item"]["md5"])
    res = run(mt.handle_export(mt.ExportInput(path=FILE), auth_value=AUTH))
    assert res["document"] == golden["document"]
    assert res["path_target_type"] == "data_object"


def test_export_ignores_sha2_checksums(irods) -> None:
    irods([avu("croissant.name", "x")], target="data_object", size=10,
          checksum="sha2:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=")
    res = run(mt.handle_export(mt.ExportInput(path=FILE), auth_value=AUTH))
    fo = res["document"]["distribution"]
    fo = fo[0] if isinstance(fo, list) else fo
    assert "md5" not in fo


def test_export_url_and_keyword_style(irods) -> None:
    irods(GOLDEN["avus"])
    res = run(mt.handle_export(
        mt.ExportInput(path=FOLDER, url="https://example.org/demo", keyword_style="text"),
        auth_value=AUTH))
    assert res["document"]["url"] == "https://example.org/demo"
    assert res["sources"]["url"] == "item.url"
    assert res["document"]["keywords"] in ("grassland", ["grassland"])


def test_export_requires_authentication(irods) -> None:
    irods([])
    with pytest.raises(ToolError) as exc:
        run(mt.handle_export(mt.ExportInput(path=FOLDER)))
    assert exc.value.code == "unauthenticated"


# -- mesa_croissant_validate -------------------------------------------------------------------


def test_validate_document_object_and_text() -> None:
    res = run(mt.handle_validate(mt.ValidateInput(document=GOLDEN["document"])))
    mt.ValidateOutput.model_validate(res)
    assert res["valid"] is True and res["checked"] == "document"
    assert res["summary"] == {"distribution": 1, "recordSet": 1, "field": 1}
    assert "mlcroissant validate" in res["hint"]

    broken = copy.deepcopy(GOLDEN["document"])
    del broken["name"]
    broken["recordSet"][0]["field"][0]["source"]["fileObject"]["@id"] = "missing"
    res = run(mt.handle_validate(mt.ValidateInput(document=json.dumps(broken))))
    assert res["valid"] is False
    paths = {e["path"] for e in res["errors"]}
    assert "name" in paths
    assert any(p.startswith("recordSet[0].field[0]") for p in paths)

    res = run(mt.handle_validate(mt.ValidateInput(document="{not json")))
    assert res["valid"] is False and "not valid JSON" in res["errors"][0]["message"]


def test_validate_path(irods) -> None:
    irods([a for a in GOLDEN["avus"] if a["attribute"] != "croissant.datePublished"])
    res = run(mt.handle_validate(mt.ValidateInput(path=FOLDER), auth_value=AUTH))
    assert res["checked"] == "path" and res["path"] == FOLDER
    assert res["valid"] is False
    assert any(e["path"] == "datePublished" for e in res["errors"])


def test_validate_needs_exactly_one_input() -> None:
    for args in ({}, {"document": {}, "path": FOLDER}):
        with pytest.raises(ToolError) as exc:
            run(mt.handle_validate(mt.ValidateInput(**args), auth_value=AUTH))
        assert exc.value.code == "invalid_argument"


# -- mesa_croissant_apply ----------------------------------------------------------------------


def test_apply_dry_run_is_the_default_and_writes_nothing(irods) -> None:
    fake = irods([avu("datacite.title.1.value", "T")])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=GOLDEN["document"]),
                              auth_value=AUTH))
    mt.ApplyOutput.model_validate(res)
    assert res["dry_run"] is True
    assert triples(res["diff"]["add"]) == triples(GOLDEN["avus"])
    assert res["diff"]["remove"] == [] and res["diff"]["unchanged"] == []
    assert res["validation"]["valid"] is True
    # The generated default landing page is not stored; a real URL is.
    assert any(w["path"] == "croissant.url" for w in res["warnings"])
    assert fake.added == [] and fake.removed == []
    fake.mirror.assert_not_awaited()
    doc = {**GOLDEN["document"], "url": "https://example.org/demo"}
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=doc), auth_value=AUTH))
    assert avu("croissant.url", "https://example.org/demo") in res["diff"]["add"]


def test_apply_document_round_trips_the_golden_avus(irods) -> None:
    fake = irods([avu("datacite.title.1.value", "T")])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document=GOLDEN["document"], dry_run=False),
        auth_value=AUTH))
    assert res["written"] == {"added": len(GOLDEN["avus"]), "removed": 0}
    assert res["errors"] == [] and res["import_errors"] == []
    assert triples(a for a in fake.avus if a["attribute"].startswith("croissant.")) == \
        triples(GOLDEN["avus"])
    # The relative contentUrl comes back relative (dav-anon base of the public folder).
    assert avu("croissant.distribution.1.contentUrl", "ratings.csv") in fake.avus
    fake.mirror.assert_awaited_once()
    kw = fake.mirror.await_args.kwargs
    assert kw["tool_name"] == "mesa_croissant_apply"
    assert kw["irods_path"] == FOLDER and kw["target_type"] == "collection"
    assert {c[3] for c in kw["changes"]} == {"add"} and len(kw["changes"]) == len(GOLDEN["avus"])
    # Applying the same document again is a no-op.
    again = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document=GOLDEN["document"], dry_run=False),
        auth_value=AUTH))
    assert again["counts"]["add"] == 0 and again["counts"]["remove"] == 0
    assert fake.mirror.await_count == 1


REPLACE_CURRENT = [
    avu("croissant.name", "Old"),
    avu("croissant.description", "Keep me"),
    avu("croissant.version", "0.1.0"),
    avu("croissant.custom.thing", "unknown key"),
    avu("datacite.title.1.value", "DataCite title"),
    avu("envo.biome", "grassland", "ENVO:00000106"),
]


def test_apply_replace_removes_absent_croissant_avus_only(irods) -> None:
    fake = irods(REPLACE_CURRENT)
    new = [avu("croissant.name", "New"), avu("croissant.description", "Keep me")]
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=new, mode="replace", dry_run=False), auth_value=AUTH))
    assert triples(res["diff"]["add"]) == {("croissant.name", "New", "")}
    assert triples(res["diff"]["remove"]) == {
        ("croissant.name", "Old", ""), ("croissant.version", "0.1.0", "")}
    # Convention rule 7: the unknown key is kept, and named in a warning.
    assert triples(res["diff"]["unchanged"]) == {("croissant.description", "Keep me", ""),
                                                 ("croissant.custom.thing", "unknown key", "")}
    assert any(w["path"] == "croissant.custom.thing" for w in res["warnings"])
    assert triples(fake.avus) == {
        ("croissant.name", "New", ""), ("croissant.description", "Keep me", ""),
        ("croissant.custom.thing", "unknown key", ""),
        ("datacite.title.1.value", "DataCite title", ""),
        ("envo.biome", "grassland", "ENVO:00000106")}
    changes = fake.mirror.await_args.kwargs["changes"]
    assert sorted(c[3] for c in changes) == ["add", "delete", "delete"]
    assert fake.mirror.await_count == 1


def test_apply_replace_drop_unknown_removes_unknown_keys(irods) -> None:
    fake = irods(REPLACE_CURRENT)
    new = [avu("croissant.name", "New"), avu("croissant.description", "Keep me")]
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=new, mode="replace", drop_unknown=True, dry_run=False),
        auth_value=AUTH))
    assert ("croissant.custom.thing", "unknown key", "") in triples(res["diff"]["remove"])
    assert triples(a for a in fake.avus if a["attribute"].startswith("croissant.")) == \
        triples(new)


def test_apply_replace_with_a_document_keeps_unknown_and_index_zero_keys(irods) -> None:
    extra = [avu("croissant.futureKey", "from a later convention"),
             avu("croissant.creator.0.name", "index zero")]
    fake = irods(GOLDEN["avus"][:3] + extra + [avu("croissant.name", "Stale name")])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document=GOLDEN["document"], mode="replace",
                      dry_run=False), auth_value=AUTH))
    assert res["errors"] == []
    croissant = triples(a for a in fake.avus if a["attribute"].startswith("croissant."))
    assert croissant == triples(GOLDEN["avus"]) | triples(extra)
    assert {"croissant.futureKey", "croissant.creator.0.name"} <= {
        w["path"] for w in res["warnings"]}
    dropped = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document=GOLDEN["document"], mode="replace",
                      drop_unknown=True, dry_run=False), auth_value=AUTH))
    assert triples(dropped["diff"]["remove"]) == triples(extra)
    assert triples(a for a in fake.avus if a["attribute"].startswith("croissant.")) == \
        triples(GOLDEN["avus"])


def test_apply_merge_adds_and_changes_but_keeps_other_keys(irods) -> None:
    current = [
        avu("croissant.name", "Old"),
        avu("croissant.version", "0.1.0"),
        avu("croissant.creator.1.name", "A"),
        avu("croissant.creator.2.name", "B"),
    ]
    fake = irods(current)
    new = [avu("croissant.name", "New"), avu("croissant.license.1", "https://x.org/l"),
           avu("croissant.creator.1.name", "A")]
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=new, mode="merge", dry_run=False), auth_value=AUTH))
    assert triples(res["diff"]["add"]) == {("croissant.name", "New", ""),
                                           ("croissant.license.1", "https://x.org/l", "")}
    assert triples(res["diff"]["remove"]) == {("croissant.name", "Old", "")}
    assert triples(fake.avus) == {
        ("croissant.name", "New", ""), ("croissant.version", "0.1.0", ""),
        ("croissant.creator.1.name", "A", ""), ("croissant.creator.2.name", "B", ""),
        ("croissant.license.1", "https://x.org/l", "")}
    stale = [w for w in res["warnings"] if w["path"] == "croissant.creator.2"]
    assert stale and "mode=replace" in stale[0]["message"]


def test_apply_merge_replaces_an_entity_as_a_whole(irods) -> None:
    """creator.1 = Bob must not inherit Alice's email and ORCID."""
    fake = irods([
        avu("croissant.creator.1.name", "Alice"),
        avu("croissant.creator.1.email", "alice@x.org"),
        avu("croissant.creator.1.identifier", "0000-0001-2345-6789"),
        avu("croissant.creator.2.name", "Carol"),
        avu("croissant.name", "Keep"),
    ])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=[avu("croissant.creator.1.name", "Bob")],
                      dry_run=False), auth_value=AUTH))
    assert triples(res["diff"]["remove"]) == {
        ("croissant.creator.1.name", "Alice", ""),
        ("croissant.creator.1.email", "alice@x.org", ""),
        ("croissant.creator.1.identifier", "0000-0001-2345-6789", "")}
    assert triples(fake.avus) == {("croissant.creator.1.name", "Bob", ""),
                                  ("croissant.creator.2.name", "Carol", ""),
                                  ("croissant.name", "Keep", "")}
    paths = {w["path"] for w in res["warnings"]}
    assert {"croissant.creator.1", "croissant.creator.2"} <= paths


def test_apply_merge_field_properties_do_not_stick_to_a_new_field(irods) -> None:
    irods([
        avu("croissant.recordSet.1.field.2.name", "old"),
        avu("croissant.recordSet.1.field.2.description", "old description"),
        avu("croissant.recordSet.1.field.2.regex", "^x$"),
        avu("croissant.recordSet.1.field.2.source", "old-src"),
        avu("croissant.recordSet.1.field.1.name", "keep"),
    ])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=[
        avu("croissant.recordSet.1.field.2.name", "new"),
        avu("croissant.recordSet.1.field.2.source", "new-src")]), auth_value=AUTH))
    assert {a["attribute"] for a in res["diff"]["remove"]} == {
        "croissant.recordSet.1.field.2.name", "croissant.recordSet.1.field.2.description",
        "croissant.recordSet.1.field.2.regex", "croissant.recordSet.1.field.2.source"}
    assert triples(res["diff"]["unchanged"]) == {("croissant.recordSet.1.field.1.name", "keep",
                                                  "")}


def test_apply_merge_nested_field_leftovers(irods) -> None:
    current = [avu("croissant.recordSet.1.id", "t")] + [
        avu(f"croissant.recordSet.1.field.{m}.name", f"f{m}") for m in (1, 2, 3)] + [
        avu("croissant.recordSet.1.field.3.description", "third")]
    fields = [avu("croissant.recordSet.1.field.1.name", "f1"),
              avu("croissant.recordSet.1.field.2.name", "f2")]
    # Only fields of record set 1: field 3 stays, with a warning (it was silent before).
    irods(current)
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=fields), auth_value=AUTH))
    assert res["diff"]["remove"] == [] and res["diff"]["add"] == []
    stale = [w for w in res["warnings"] if w["path"] == "croissant.recordSet.1.field.3"]
    assert stale and "mode=replace" in stale[0]["message"]
    # The whole record set (as from_schema / a document gives it): field 3 is removed.
    irods(current)
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.recordSet.1.id", "t")] + fields), auth_value=AUTH))
    assert triples(res["diff"]["remove"]) == {
        ("croissant.recordSet.1.field.3.name", "f3", ""),
        ("croissant.recordSet.1.field.3.description", "third", "")}
    # Nested subfields of a replaced field are kept and named.
    irods([avu("croissant.recordSet.1.field.1.name", "old"),
           avu("croissant.recordSet.1.field.1.subField.1.name", "s")])
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.recordSet.1.field.1.name", "new")]), auth_value=AUTH))
    assert any(w["path"] == "croissant.recordSet.1.field.1.subField.1" for w in res["warnings"])


def test_apply_merge_unindexed_field_datatype_replaces_the_indexed_one(irods) -> None:
    fake = irods([avu("croissant.recordSet.1.field.1.name", "n"),
                  avu("croissant.recordSet.1.field.1.dataType.1", "sc:Text")])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=[
        avu("croissant.recordSet.1.field.1.name", "n"),
        avu("croissant.recordSet.1.field.1.dataType", "cr:Int64")], dry_run=False),
        auth_value=AUTH))
    assert triples(res["diff"]["remove"]) == {
        ("croissant.recordSet.1.field.1.dataType.1", "sc:Text", "")}
    model = core.parse_avus(fake.avus)["model"]
    assert model["recordSet"][0]["field"][0]["dataType"] == [{"value": "cr:Int64", "unit": ""}]


def test_apply_merge_indexed_license_replaces_the_unindexed_one(irods) -> None:
    fake = irods([avu("croissant.license", "https://old.org/l")])
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.license.1", "https://new.org/l")], dry_run=False),
        auth_value=AUTH))
    assert triples(res["diff"]["remove"]) == {("croissant.license", "https://old.org/l", "")}
    assert triples(fake.avus) == {("croissant.license.1", "https://new.org/l", "")}
    # A failed add keeps the old value of the same canonical path.
    fake = irods([avu("croissant.license", "https://old.org/l")])
    fake.fail_add.add("croissant.license.1")
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.license.1", "https://new.org/l")], dry_run=False),
        auth_value=AUTH))
    assert res["kept"] == [avu("croissant.license", "https://old.org/l")]
    assert fake.avus == [avu("croissant.license", "https://old.org/l")]


def test_apply_whitespace_only_difference_is_a_no_op(irods) -> None:
    fake = irods([avu("croissant.description", "Ratings.\n")])
    for mode in ("merge", "replace"):
        res = run(mt.handle_apply(mt.ApplyInput(
            path=FOLDER, avus=[avu("croissant.description", "Ratings.")], mode=mode,
            dry_run=False), auth_value=AUTH))
        assert res["counts"]["add"] == 0 and res["counts"]["remove"] == 0, mode
        assert res["written"] == {"added": 0, "removed": 0}
    assert fake.avus == [avu("croissant.description", "Ratings.\n")]
    fake.mirror.assert_not_awaited()


def test_apply_replace_removes_an_unstripped_twin_by_its_exact_value(irods) -> None:
    fake = irods([avu("croissant.description", "Ratings.\n"),
                  avu("croissant.description", "Ratings.")])
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.description", "Ratings.")], mode="replace",
        dry_run=False), auth_value=AUTH))
    assert res["diff"]["remove"] == [avu("croissant.description", "Ratings.\n")]
    assert res["diff"]["add"] == []
    assert fake.removed == [avu("croissant.description", "Ratings.\n")]
    assert fake.avus == [avu("croissant.description", "Ratings.")]
    assert fake.mirror.await_args.kwargs["changes"] == [
        ("croissant.description", "Ratings.\n", "", "delete")]


def test_remove_exact_does_not_strip() -> None:
    from irods.models import Collection, DataObject

    session = MagicMock()
    r = avu("croissant.description", " Ratings.\n", " B ")
    assert mt._remove_exact(session, FOLDER, "collection", r) == r
    model, path, meta = session.metadata.remove.call_args.args
    assert model is Collection and path == FOLDER
    assert (meta.name, meta.value, meta.units) == ("croissant.description", " Ratings.\n", " B ")
    mt._remove_exact(session, FILE, "data_object", r)
    assert session.metadata.remove.call_args.args[0] is DataObject
    session.metadata.remove.side_effect = RuntimeError("CAT_SUCCESS_BUT_WITH_NO_INFO")
    with pytest.raises(ToolError) as exc:
        mt._remove_exact(session, FOLDER, "collection", r)
    assert exc.value.code == "irods_error"


def test_apply_merge_unit_change_is_a_change(irods) -> None:
    irods([avu("croissant.keyword.1", "grassland", "ENVO:1")])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=[avu("croissant.keyword.1", "grassland", "ENVO:2")]),
        auth_value=AUTH))
    assert triples(res["diff"]["add"]) == {("croissant.keyword.1", "grassland", "ENVO:2")}
    assert triples(res["diff"]["remove"]) == {("croissant.keyword.1", "grassland", "ENVO:1")}


def test_apply_refuses_non_croissant_avus(irods) -> None:
    fake = irods([])
    bad = [avu("datacite.title.1.value", "T"), avu("croissant.name", " "),
           avu("croissant.description", "x" * (core.VALUE_MAX + 1))]
    dry = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=bad), auth_value=AUTH))
    paths = {e["path"] for e in dry["import_errors"]}
    assert {"datacite.title.1.value", "croissant.name", "croissant.description"} <= paths
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=bad, dry_run=False),
                            auth_value=AUTH))
    assert exc.value.code == "croissant_invalid"
    assert fake.added == [] and fake.removed == []
    fake.mirror.assert_not_awaited()


def test_apply_refuses_a_document_with_import_errors(irods) -> None:
    fake = irods([avu("croissant.name", "Old")])
    doc = copy.deepcopy(GOLDEN["document"])
    doc["description"] = "y" * (core.VALUE_MAX + 5)
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=doc, mode="replace",
                                          dry_run=False), auth_value=AUTH))
    assert exc.value.code == "croissant_invalid"
    assert fake.avus == [avu("croissant.name", "Old")]
    not_dataset = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document={"@type": "x"}),
                                      auth_value=AUTH))
    assert not_dataset["import_errors"]


def test_apply_keeps_the_old_value_when_its_replacement_fails(irods) -> None:
    fake = irods([avu("croissant.name", "Old"), avu("croissant.version", "1")])
    fake.fail_add.add("croissant.name")
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=[avu("croissant.name", "New")], mode="replace",
                      dry_run=False), auth_value=AUTH))
    assert res["errors"] == [{"op": "add", "attribute": "croissant.name",
                              "error": "add croissant.name failed"}]
    assert res["kept"] == [avu("croissant.name", "Old")]
    assert res["written"] == {"added": 0, "removed": 1}
    assert triples(fake.avus) == {("croissant.name", "Old", "")}
    assert fake.mirror.await_args.kwargs["changes"] == [("croissant.version", "1", "", "delete")]


def test_apply_reports_a_failed_mirror_as_partial_failure(irods) -> None:
    fake = irods([])
    fake.mirror.side_effect = DuckLakeMirrorError("catalog down", project_id="p-1")
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, avus=[avu("croissant.name", "N")], dry_run=False),
        auth_value=AUTH))
    mt.ApplyOutput.model_validate(res)
    assert res["written"]["added"] == 1
    assert res["partial_failure"] == {"code": "ducklake_mirror_failed",
                                      "message": "catalog down", "project_id": "p-1"}


def test_apply_avus_on_a_file_warn_about_distribution(irods) -> None:
    irods([], target="data_object", size=5)
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FILE, avus=[avu("croissant.distribution.1.id", "x")]),
        auth_value=AUTH))
    assert any(w["path"] == "croissant.distribution" for w in res["warnings"])


def test_apply_file_export_round_trips_as_file_avus(irods) -> None:
    """A file's exported document comes back as croissant.file.*: item mode, the stat md5,
    contentUrl, contentSize and default encodingFormat left out; re-applying is a no-op."""
    golden = load_json(FIXTURES / "golden_file_level.json")
    fake = irods(golden["avus"], target="data_object", size=golden["item"]["size"],
                 checksum=golden["item"]["md5"])
    res = run(mt.handle_apply(mt.ApplyInput(path=FILE, document=golden["document"]),
                              auth_value=AUTH))
    assert res["import_errors"] == []
    assert res["counts"]["add"] == 0 and res["counts"]["remove"] == 0, res["diff"]
    assert not any(a["attribute"].startswith("croissant.distribution.")
                   for a in res["diff"]["add"] + res["diff"]["unchanged"])
    assert not any(w["path"] == "croissant.distribution" for w in res["warnings"])
    assert res["validation"]["valid"] is True
    assert fake.session.data_objects.get.call_count == 1  # one stat for import and preview

    # The folder document applied to its CSV: that FileObject becomes the file's own.
    irods([], target="data_object", size=5)
    res = run(mt.handle_apply(mt.ApplyInput(path=FILE, document=GOLDEN["document"]),
                              auth_value=AUTH))
    attrs = {a["attribute"] for a in res["diff"]["add"]}
    assert "croissant.file.name" in attrs
    assert not any(a.startswith("croissant.distribution.") for a in attrs)
    assert "croissant.recordSet.1.field.1.source" not in attrs  # fields default to the file
    assert not any(w["path"] == "croissant.distribution" for w in res["warnings"])


def test_apply_document_on_a_file_reports_other_distribution_entries(irods) -> None:
    irods([], target="data_object", size=5)
    doc = copy.deepcopy(GOLDEN["document"])
    doc["distribution"].append({"@type": "cr:FileObject", "@id": "other",
                                "contentUrl": "other.csv", "encodingFormat": "text/csv"})
    res = run(mt.handle_apply(mt.ApplyInput(path=FILE, document=doc), auth_value=AUTH))
    assert any(w["path"] == "distribution" and "'other' dropped" in w["message"]
               for w in res["warnings"])
    assert not any(a["attribute"].startswith("croissant.distribution.")
                   for a in res["diff"]["add"])


DOI_AVUS = [avu("datacite.identifier", "10.1234/demo"),
            avu("datacite.identifierType", "DOI")]


def test_apply_drops_the_doi_url_and_identifier(irods) -> None:
    irods(DOI_AVUS)
    doc = {**GOLDEN["document"], "url": "https://doi.org/10.1234/demo",
           "identifier": "https://doi.org/10.1234/demo"}
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=doc), auth_value=AUTH))
    attrs = {a["attribute"] for a in res["diff"]["add"]}
    assert "croissant.url" not in attrs and "croissant.identifier" not in attrs
    msgs = {w["path"]: w["message"] for w in res["warnings"]}
    assert msgs["croissant.url"] == mt._MSG_DEFAULT_URL
    assert msgs["croissant.identifier"] == mt._MSG_DOI_ID
    # Another identifier, or a DOI URL without the item's DOI, is stored.
    irods([])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=doc), auth_value=AUTH))
    assert avu("croissant.url", "https://doi.org/10.1234/demo") in res["diff"]["add"]
    assert avu("croissant.identifier", "https://doi.org/10.1234/demo") in res["diff"]["add"]


def test_apply_drops_values_equal_to_the_crosswalk(irods) -> None:
    """A property the crosswalk (or the item name) gives exactly is not pinned; the
    portal's importer (croissant_service.drop_generated) applies the same rule."""
    irods([avu("datacite.title.1.value", "Demo ratings"),
           avu("datacite.description.1.value", "Something else")])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=GOLDEN["document"]),
                              auth_value=AUTH))
    attrs = {a["attribute"] for a in res["diff"]["add"]}
    assert "croissant.name" not in attrs
    assert "croissant.description" in attrs  # differs from the crosswalk value
    warning = next(w for w in res["warnings"] if w["path"] == "croissant.name")
    assert "datacite.title.1.value" in warning["message"]
    assert res["validation"]["valid"] is True

    # The item name is a generated default too.
    irods([])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document={**GOLDEN["document"], "name": "demo"}),
        auth_value=AUTH))
    assert "croissant.name" not in {a["attribute"] for a in res["diff"]["add"]}
    assert "item.name" in next(w for w in res["warnings"]
                               if w["path"] == "croissant.name")["message"]


def test_apply_keywords_exported_as_text_match_the_ontology_default(irods) -> None:
    irods([avu("envo.biome", "grassland", "ENVO:00000106")])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document={**GOLDEN["document"], "keywords": ["grassland"]}),
        auth_value=AUTH))
    assert not any(a["attribute"].startswith("croissant.keyword")
                   for a in res["diff"]["add"])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document={**GOLDEN["document"], "keywords": ["forest"]}),
        auth_value=AUTH))
    assert avu("croissant.keyword.1", "forest") in res["diff"]["add"]


def test_apply_merge_removes_a_pinned_value_the_document_gives_as_generated(irods) -> None:
    fake = irods([avu("croissant.name", "Old"), avu("croissant.url", "https://old.example"),
                  avu("croissant.name.0", "index zero"),
                  avu("datacite.title.1.value", "Demo ratings")])
    res = run(mt.handle_apply(
        mt.ApplyInput(path=FOLDER, document=GOLDEN["document"], dry_run=False),
        auth_value=AUTH))
    removed = triples(res["diff"]["remove"])
    assert ("croissant.name", "Old", "") in removed
    assert ("croissant.url", "https://old.example", "") in removed
    assert avu("croissant.name.0", "index zero") in fake.avus  # outside the convention
    assert {"croissant.name", "croissant.url"} <= {
        w["path"] for w in res["warnings"] if "merge removes the current value" in w["message"]}
    out = core.to_jsonld(fake.avus, item={"path": FOLDER, "name": "demo"})
    assert out["document"]["name"] == "Demo ratings"
    assert out["sources"]["name"] == "datacite.title.1.value"
    assert out["sources"]["url"] == "default"


def test_drop_generated_matches_the_portal_rule() -> None:
    """Direct check of the rule shared with the portal: url/identifier always, a property
    only when its AVUs generate exactly the default."""
    item = {"path": FOLDER, "name": "demo", "is_folder": True}
    current = DOI_AVUS + [avu("datacite.rights.1.rightsURI",
                              "https://creativecommons.org/licenses/by/4.0/")]
    imported = [avu("croissant.url", core._default_page(item)),
                avu("croissant.identifier", "https://doi.org/10.1234/demo"),
                avu("croissant.license.1", "https://creativecommons.org/licenses/by/4.0/"),
                avu("croissant.version", "1.0.0")]
    kept, warnings, stems = mt.drop_generated(imported, item, current)
    assert kept == [avu("croissant.version", "1.0.0")]
    assert set(stems) == {"croissant.url", "croissant.identifier", "croissant.license"}
    assert {w["path"] for w in warnings} == set(stems)


def test_apply_input_checks(irods) -> None:
    irods([])
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER), auth_value=AUTH))
    assert exc.value.code == "invalid_argument"
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=[])))
    assert exc.value.code == "unauthenticated"
    anon = AuthValue(username="anonymous", zone="iplant")
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=[], dry_run=False),
                            auth_value=anon))
    assert exc.value.code == "forbidden"


def test_apply_replace_with_empty_set_warns(irods) -> None:
    irods([avu("croissant.name", "Old")])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=[], mode="replace"),
                              auth_value=AUTH))
    assert res["counts"]["remove"] == 1
    assert any("empty set" in w["message"] for w in res["warnings"])


def test_compute_diff_duplicates_in_current() -> None:
    current = [avu("croissant.name", "A"), avu("croissant.name", "B"), avu("other.x", "1")]
    add, remove, keep, _ = mt.compute_diff(current, [avu("croissant.name", "B")], "merge")
    assert add == [] and remove == [avu("croissant.name", "A")]
    assert keep == [avu("croissant.name", "B")]


def _fields_of(avus: list[dict[str, str]]) -> list[dict]:
    return core.parse_avus(avus)["model"]["recordSet"][0]["field"]


MERGE_CURRENT = [avu("croissant.recordSet.1.id", "t"),
                 avu("croissant.recordSet.1.field.1.name", "a"),
                 avu("croissant.recordSet.1.field.2.name", "b"),
                 avu("croissant.recordSet.1.field.3.name", "c"),
                 avu("croissant.recordSet.1.field.3.transform", "T")]
MERGE_NEW = [avu("croissant.recordSet.1.id", "t"),
             avu("croissant.recordSet.1.field.1.name", "a2"),
             avu("croissant.recordSet.1.field.2.name", "b2")]


def test_compute_diff_merge_removes_a_leftover_entry_with_its_unknown_keys() -> None:
    """Convention rule 7: field 3 goes as a whole, so its unknown transform goes too."""
    add, remove, keep, warnings = mt.compute_diff(MERGE_CURRENT, MERGE_NEW, "merge")
    assert avu("croissant.recordSet.1.field.3.transform", "T") in remove
    assert avu("croissant.recordSet.1.field.3.name", "c") in remove
    assert keep == [avu("croissant.recordSet.1.id", "t")]
    # No entity of unknown keys only is left behind.
    fields = _fields_of(keep + add)
    assert [f["name"]["value"] for f in fields] == ["a2", "b2"]
    assert all(core.UNKNOWN_KEY not in f for f in fields)
    msg = {w["path"]: w["message"] for w in warnings}
    assert msg["croissant.recordSet.1.field.3"].startswith("merge removes this entry")
    assert "croissant.recordSet.1.field" in msg["croissant.recordSet.1.field.3"]
    # Field 1, touched by the new set, keeps the "replaces" wording.
    current = [*MERGE_CURRENT, avu("croissant.recordSet.1.field.1.description", "d")]
    _a, remove, _k, warnings = mt.compute_diff(current, MERGE_NEW, "merge")
    assert avu("croissant.recordSet.1.field.1.description", "d") in remove
    msg = {w["path"]: w["message"] for w in warnings}
    assert msg["croissant.recordSet.1.field.1"].startswith("merge replaces this entry")


def test_compute_diff_merge_unknown_key_before_its_entitys_known_key() -> None:
    current = [MERGE_CURRENT[4], *MERGE_CURRENT[:4]]
    add, remove, keep, _ = mt.compute_diff(current, MERGE_NEW, "merge")
    assert avu("croissant.recordSet.1.field.3.transform", "T") in remove
    assert len(_fields_of(keep + add)) == 2
    # Results keep the current order.
    assert remove == [a for a in current if a in remove]


@pytest.mark.parametrize("attr", [
    "croissant.recordSet.1.field.03.transform",        # 03 = 3
    "croissant.recordSet.field.3.transform",           # recordSet = recordSet.1
    "croissant.recordSet.1.field.3.subField.1.foo",    # below an entity of field 3
    "croissant.recordSet.1.field.3.subField.0.name",   # index 0 below field 3
    "croissant.recordSet.1.field.3.dataType.x",        # a bad sub-key of field 3
])
def test_compute_diff_merge_unknown_spellings_follow_their_entity(attr) -> None:
    current = [*MERGE_CURRENT[:4], avu(attr, "T")]
    _add, remove, _keep, _ = mt.compute_diff(current, MERGE_NEW, "merge")
    assert avu(attr, "T") in remove


def test_compute_diff_merge_keeps_unknown_keys_of_kept_and_touched_entities() -> None:
    current = [*MERGE_CURRENT,
               avu("croissant.recordSet.1.transform", "R"),           # touched record set
               avu("croissant.recordSet.1.field.1.transform", "F1"),  # touched field
               avu("croissant.recordSet.1.field.4.transform", "F4"),  # unknown keys only
               avu("croissant.recordSet.2.field.1.transform", "X"),   # untouched record set
               avu("croissant.recordSet.1.field.3", "bare"),          # belongs to recordSet.1
               avu("croissant.custom.thing", "u")]
    _add, remove, keep, _ = mt.compute_diff(current, MERGE_NEW, "merge")
    assert {a["attribute"] for a in remove} == {
        "croissant.recordSet.1.field.1.name", "croissant.recordSet.1.field.2.name",
        "croissant.recordSet.1.field.3.name", "croissant.recordSet.1.field.3.transform"}
    for value in ("R", "F1", "F4", "X", "bare", "u"):
        assert any(a["value"] == value for a in keep)


def test_compute_diff_merge_top_level_entries_keep_their_unknown_keys() -> None:
    current = [avu("croissant.creator.1.name", "A"), avu("croissant.creator.2.name", "B"),
               avu("croissant.creator.2.orcidVerified", "true")]
    _add, remove, keep, _ = mt.compute_diff(current, [avu("croissant.creator.1.name", "A")],
                                            "merge")
    assert remove == [] and keep == current


def test_compute_diff_replace_removes_unknown_keys_of_a_removed_entity() -> None:
    current = [*MERGE_CURRENT,
               avu("croissant.creator.2.name", "B"),
               avu("croissant.creator.2.orcidVerified", "true"),
               avu("croissant.creator.3.futureProp", "only unknown"),
               avu("croissant.recordSet.1.field.1.transform", "F1"),
               avu("croissant.creator.0.name", "index zero"),
               avu("croissant.custom.thing", "u")]
    add, remove, keep, warnings = mt.compute_diff(current, MERGE_NEW, "replace")
    removed = {a["attribute"] for a in remove}
    assert {"croissant.recordSet.1.field.3.transform",
            "croissant.creator.2.orcidVerified"} <= removed
    assert {(a["attribute"], a["value"]) for a in keep} == {
        ("croissant.recordSet.1.id", "t"),
        ("croissant.creator.3.futureProp", "only unknown"),   # a document cannot carry it
        ("croissant.recordSet.1.field.1.transform", "F1"),    # field 1 stays
        ("croissant.creator.0.name", "index zero"),
        ("croissant.custom.thing", "u")}
    msg = {w["path"]: w["message"] for w in warnings}
    assert msg["croissant.recordSet.1.field.3"].startswith("replace removes this entry")
    assert msg["croissant.creator.2"].startswith("replace removes this entry")
    parsed = core.parse_avus(keep + add)["model"]
    assert len(parsed["recordSet"][0]["field"]) == 2
    # drop_unknown still removes every unknown key.
    _a, remove, keep, _w = mt.compute_diff(current, MERGE_NEW, "replace", drop_unknown=True)
    assert keep == [avu("croissant.recordSet.1.id", "t")]


def test_canon_index_segment_must_be_digits_only() -> None:
    """``$`` matches before a final newline; core uses fullmatch."""
    assert mt._canon("croissant.license.1\n") is None
    assert mt._canon("croissant.creator.1\n.name") is None
    assert mt._canon("croissant.license.1").path == "croissant.license.1"
    stored = [{"attribute": "croissant.license.1\n", "value": "x", "unit": ""}]
    add, remove, keep, warnings = mt.compute_diff(
        stored, [avu("croissant.name", "n")], mode="replace")
    assert remove == [] and keep == stored and add == [avu("croissant.name", "n")]
    assert any(w["path"] == "croissant.license.1\n" and "unknown Croissant key" in w["message"]
               for w in warnings)


def _core_place(attr: str) -> tuple[str, str | None]:
    """What core does with one AVU: its _place result and, when bound, the owning entity."""
    segs = attr[len(core.PREFIX):].split(".")
    if "" in segs:
        return "unknown", None
    store: dict = {}
    result = core._place(core._DATASET, segs, store, {"value": "x", "unit": ""}, attr, [])

    def owner(schema: dict, node: dict, prefix: str) -> str | None:
        for name, sub in node.items():
            spec = schema.get(name)
            if spec is None:
                continue
            if spec[0] == "E":
                for idx, ent in sub.items():
                    ep = f"{prefix}{name}.{idx}"
                    if core.UNKNOWN_KEY in ent:
                        return ep
                    found = owner(spec[1], ent, ep + ".")
                    if found:
                        return found
            elif spec[0] == "G":
                found = owner(spec[1], sub, f"{prefix}{name}.")
                if found:
                    return found
        return None

    return result, owner(core._DATASET, store, core.PREFIX) if result == "bound" else None


def test_canon_and_entity_chain_agree_with_core() -> None:
    import random

    vocab = ["creator", "recordSet", "field", "subField", "distribution", "geo", "name",
             "transform", "license", "dataType", "keyword", "containedIn", "1", "2", "03",
             "0", "", "1\n", "x"]
    rng = random.Random(20261106)
    attrs = {"croissant." + ".".join(rng.choice(vocab) for _ in range(rng.randint(1, 7)))
             for _ in range(20000)}
    bound = 0
    for attr in sorted(attrs):
        result, owner = _core_place(attr)
        assert (mt._canon(attr) is not None) == (result == "ok"), attr
        chain = mt._entity_chain(attr)
        if result == "bound":
            bound += 1
            assert chain and chain[-1][1] == owner, attr
        elif result == "unknown" and ".." not in attr and not attr.endswith("."):
            assert chain == (), attr  # not below an entity (an empty segment ends the walk)
        if mt._canon(attr) is not None:
            assert [ep for k, _lp, ep in mt._canon(attr).entries if k == "E"] == [
                ep for _lp, ep in chain], attr
    assert bound > 100


# -- mesa_croissant_from_schema ----------------------------------------------------------------


def test_from_schema_builds_record_set_avus() -> None:
    cols = [
        {"name": "user_id", "type": "BIGINT"},
        {"column_name": "tags", "column_type": "VARCHAR[]"},
        {"name": "loc", "type": "STRUCT(lat DOUBLE, lon DOUBLE, meta STRUCT(a INTEGER))"},
        {"name": "rated at", "data_type": "TIMESTAMP WITH TIME ZONE", "description": "when"},
        {"name": "user_id", "type": "INTEGER"},
    ]
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=cols, record_set_id="ratings", source_id="ratings-parquet",
        description="Ratings table")))
    mt.FromSchemaOutput.model_validate(res)
    got = {a["attribute"]: a["value"] for a in res["avus"]}
    assert got["croissant.recordSet.1.id"] == "ratings"
    assert got["croissant.recordSet.1.description"] == "Ratings table"
    assert got["croissant.recordSet.1.field.1.name"] == "user_id"
    assert got["croissant.recordSet.1.field.1.dataType"] == "cr:Int64"
    assert got["croissant.recordSet.1.field.1.source"] == "ratings-parquet"
    assert got["croissant.recordSet.1.field.1.column"] == "user_id"
    assert got["croissant.recordSet.1.field.2.dataType"] == "sc:Text"
    assert got["croissant.recordSet.1.field.2.isArray"] == "true"
    assert got["croissant.recordSet.1.field.2.arrayShape"] == "-1"
    assert "croissant.recordSet.1.field.3.dataType" not in got
    assert got["croissant.recordSet.1.field.3.subField.1.name"] == "lat"
    assert got["croissant.recordSet.1.field.3.subField.2.dataType"] == "cr:Float64"
    assert got["croissant.recordSet.1.field.3.subField.3.dataType"] == "sc:Text"
    # STRUCT members read the parent's column through transformJsonPath (mlcroissant);
    # a nested STRUCT typed sc:Text gets none, as the portal DuckDB viewer writes it.
    assert [got[f"croissant.recordSet.1.field.3.subField.{k}.transformJsonPath"]
            for k in (1, 2)] == ["lat", "lon"]
    assert "croissant.recordSet.1.field.3.subField.3.transformJsonPath" not in got
    assert not any(a.startswith("croissant.recordSet.1.field.3.subField.")
                   and a.rsplit(".", 1)[-1] in ("source", "column") for a in got)
    assert "croissant.recordSet.1.field.1.transformJsonPath" not in got
    assert got["croissant.recordSet.1.field.4.id"] == "ratings/rated_at"
    assert got["croissant.recordSet.1.field.4.dataType"] == "sc:DateTime"
    assert got["croissant.recordSet.1.field.4.description"] == "when"
    assert "croissant.recordSet.1.field.5.name" not in got
    warned = {w["path"] for w in res["warnings"]}
    # A skipped column has no field: its warning names the input position.
    assert "columns[4]" in warned and "croissant.recordSet.1.field.5" not in warned
    assert "croissant.recordSet.1.field.3.subField.3" in warned
    assert all(a["unit"] == "" for a in res["avus"])
    # The AVUs parse back under the convention without unknown keys.
    parsed = core.parse_avus(res["avus"])
    assert parsed["unknown"] == [] and parsed["warnings"] == []


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        if set(node) == {"value", "unit"}:
            return node["value"]
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def test_from_schema_matches_the_shared_parity_fixture() -> None:
    """The record set the portal DuckDB viewer builds for the same schema (the portal's
    tests/js/duckdb_viewer.test.js checks dvCroissantFromFile against this fixture, and
    test_mlcroissant_compat.py loads its records): a plain member, a quoted member name, a
    nested STRUCT member typed sc:Text without a transform, and a LIST(STRUCT) column."""
    fx = load_json(FIXTURES / "schema_parity.json")
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=fx["columns"], record_set_id="t", source_id="t.parquet")))
    parsed = core.parse_avus(res["avus"])
    assert parsed["unknown"] == [] and parsed["warnings"] == []
    assert _plain(parsed["model"]["recordSet"][0]) == fx["record_set"]
    assert [w["path"] for w in res["warnings"]] == fx["warning_paths"]
    nested = "croissant.recordSet.1.field.2.subField.3"
    assert f"{nested}.dataType" in {a["attribute"] for a in res["avus"]}
    assert f"{nested}.transformJsonPath" not in {a["attribute"] for a in res["avus"]}


def test_from_schema_member_json_paths_match_the_duckdb_viewer() -> None:
    """Members whose names are not plain identifiers are quoted the way the portal's
    DuckDB viewer (dvCrJsonPath) quotes them."""
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "s", "type": 'STRUCT("rated at" INTEGER, "it\'s" VARCHAR, '
                                       '"where" INT, ok_1 INT)'}],
        record_set_id="t", source_id="t-files")))
    got = {a["attribute"]: a["value"] for a in res["avus"]}
    assert [got[f"croissant.recordSet.1.field.1.subField.{k}.transformJsonPath"]
            for k in (1, 2, 3, 4)] == ["$['rated at']", "$['it\\'s']", "$['where']", "ok_1"]
    assert mt._member_json_path("a\\b") == "$['a\\\\b']"


def test_from_schema_avus_generate_a_valid_document() -> None:
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "userId", "type": "INTEGER"}, {"name": "rating", "type": "FLOAT"},
                 {"name": "loc", "type": "STRUCT(lat DOUBLE, lon DOUBLE)"}],
        record_set_id="ratings", source_id="ratings-csv", record_set_index=1)))
    base = [a for a in GOLDEN["avus"] if not a["attribute"].startswith("croissant.recordSet.")]
    out = core.to_jsonld(base + res["avus"], item=GOLDEN["item"])
    assert out["validation"]["valid"] is True, out["validation"]["errors"]
    assert not any("recordSet" in w["path"] for w in out["validation"]["warnings"]), \
        out["validation"]["warnings"]
    fields = out["document"]["recordSet"][0]["field"]
    assert [f["@id"] for f in fields] == ["ratings/userId", "ratings/rating", "ratings/loc"]
    assert fields[1]["dataType"] == "cr:Float32"
    lat = fields[2]["subField"][0]
    assert lat["source"] == {"fileObject": {"@id": "ratings-csv"},
                             "extract": {"column": "loc"},
                             "transform": {"jsonPath": "lat"}}


def test_from_schema_map_geometry_and_spatial_columns() -> None:
    """MAP: an isArray field with subFields key / value read with $[0] / $[1] (what
    mlcroissant 1.1.1 loads from Parquet). GEOMETRY: [sc:GeoShape, sc:Text]. POINT_2D: its
    STRUCT layout under sc:GeoCoordinates. POLYGON_2D (a list of lists of STRUCTs, which
    mlcroissant does not load) gets a warning. The document has no readability warning."""
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "props", "type": "MAP(VARCHAR, VARCHAR)"},
                 {"name": "geometry", "type": "GEOMETRY"},
                 {"name": "pt", "type": "POINT_2D"},
                 {"name": "poly", "type": "POLYGON_2D"}],
        record_set_id="plots", source_id="ratings-csv")))
    got = {a["attribute"]: a["value"] for a in res["avus"]}
    f = "croissant.recordSet.1.field."
    assert got[f + "1.isArray"] == "true" and got[f + "1.arrayShape"] == "-1"
    assert f + "1.dataType" not in got
    assert [(got[f"{f}1.subField.{k}.name"], got[f"{f}1.subField.{k}.transformJsonPath"])
            for k in (1, 2)] == [("key", "$[0]"), ("value", "$[1]")]
    assert (got[f + "2.dataType.1"], got[f + "2.dataType.2"]) == ("sc:GeoShape", "sc:Text")
    assert got[f + "3.dataType"] == "sc:GeoCoordinates"
    assert [got[f"{f}3.subField.{k}.transformJsonPath"] for k in (1, 2)] == ["x", "y"]
    assert [w["path"] for w in res["warnings"]] == [f + "4"]
    assert "list of lists" in res["warnings"][0]["message"]
    base = [a for a in GOLDEN["avus"] if not a["attribute"].startswith("croissant.recordSet.")]
    out = core.to_jsonld(base + res["avus"], item=GOLDEN["item"])
    assert out["validation"]["valid"] is True, out["validation"]["errors"]
    assert not any("mlcroissant 1.1.1" in w["message"] for w in out["validation"]["warnings"])
    fields = out["document"]["recordSet"][0]["field"]
    assert fields[1]["dataType"] == ["sc:GeoShape", "sc:Text"]
    assert fields[0]["subField"][1]["source"]["transform"] == {"jsonPath": "$[1]"}


def test_from_schema_index_and_input_checks() -> None:
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "a", "type": "BOOLEAN"}], record_set_id="t2", source_id="s",
        record_set_index=3, record_set_name="Table two")))
    attrs = [a["attribute"] for a in res["avus"]]
    assert attrs and all(a.startswith("croissant.recordSet.3.") for a in attrs)
    assert {"attribute": "croissant.recordSet.3.name", "value": "Table two", "unit": ""} \
        in res["avus"]
    for kw in ({"record_set_id": "has space", "source_id": "s"},
               {"record_set_id": "t", "source_id": " "}):
        with pytest.raises(ToolError):
            run(mt.handle_from_schema(mt.FromSchemaInput(
                columns=[{"name": "a", "type": "INTEGER"}], **kw)))
    with pytest.raises(ToolError):
        run(mt.handle_from_schema(mt.FromSchemaInput(columns=[], record_set_id="t",
                                                     source_id="s")))


# -- 1.3.1: from_schema ids, exact columns, limits ---------------------------------------------


def test_from_schema_ids_are_unique_and_columns_exact() -> None:
    """A whitespace-normalised id never takes a real column's default id (live finding: 'a b'
    and 'a_b' both got t/a_b); the column keeps the exact name while the field name is
    trimmed; skipped columns are named by their input position, not by an emitted field."""
    cols = [
        {"name": "a b", "type": "VARCHAR"},       # field 1: explicit id, a_b is taken
        {"name": "a_b", "type": "VARCHAR"},       # field 2: keeps its default id t/a_b
        {"name": "a  b", "type": "INTEGER"},      # field 3: t/a_b-3 (t/a_b-2 is taken)
        {"name": "a", "type": "INTEGER"},         # field 4
        {"name": "a", "type": "BIGINT"},          # columns[4]: duplicate, skipped
        {"name": "  ", "type": "INTEGER"},        # columns[5]: blank, skipped
        {"name": "  padded  ", "type": "INTEGER"},  # field 5
        {"name": "s", "type": 'STRUCT("x y" INTEGER, x_y INTEGER)'},  # field 6
    ]
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=cols, record_set_id="t", source_id="ratings-csv")))
    got = {a["attribute"]: a["value"] for a in res["avus"]}
    f = "croissant.recordSet.1.field."
    assert got[f + "1.id"] == "t/a_b-2" and got[f + "1.column"] == "a b"
    assert f + "2.id" not in got and got[f + "2.name"] == "a_b"
    assert got[f + "3.id"] == "t/a_b-3"
    assert got[f + "4.column"] == "a" and got[f + "4.dataType"] == "cr:Int32"
    assert got[f + "5.name"] == "padded" and got[f + "5.column"] == "  padded  "
    assert f + "5.id" not in got
    assert got[f + "6.subField.1.id"] == "t/s/x_y-2"
    assert got[f + "6.subField.1.transformJsonPath"] == "$['x y']"
    assert f + "6.subField.2.id" not in got
    assert f + "7.name" not in got
    paths = [w["path"] for w in res["warnings"]]
    assert paths == ["columns[4]", "columns[5]", f + "5.column"], res["warnings"]
    assert "croissant.recordSet.1.field.4" in res["warnings"][0]["message"]
    assert "rename the column" in res["warnings"][2]["message"]
    base = [a for a in GOLDEN["avus"] if not a["attribute"].startswith("croissant.recordSet.")]
    out = core.to_jsonld(base + res["avus"], item=GOLDEN["item"])
    assert out["validation"]["valid"] is True, out["validation"]["errors"]
    ids = [fl["@id"] for fl in out["document"]["recordSet"][0]["field"]]
    assert ids == ["t/a_b-2", "t/a_b", "t/a_b-3", "t/a", "t/padded", "t/s"]
    assert out["document"]["recordSet"][0]["field"][4]["source"]["extract"] == {
        "column": "  padded  "}


def test_from_schema_ids_are_deterministic() -> None:
    cols = [{"name": n, "type": "INTEGER"} for n in ("x y", "x_y", "x\ty", "x_y-2")]
    runs = [run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=cols, record_set_id="t", source_id="s")))["avus"] for _ in range(2)]
    assert runs[0] == runs[1]
    ids = [core._field_id(fl, "t") for fl in
           core.parse_avus(runs[0])["model"]["recordSet"][0]["field"]]
    assert len(set(ids)) == 4 and ids[1] == "t/x_y" and ids[3] == "t/x_y-2"


def test_from_schema_warns_about_values_over_the_avu_limits() -> None:
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "long", "type": "INTEGER", "description": "x" * 3000},
                 {"name": "c" * (core.VALUE_MAX + 1), "type": "INTEGER"}],
        record_set_id="t", source_id="s")))
    msgs = {w["path"]: w["message"] for w in res["warnings"]}
    assert "3000 characters" in msgs["croissant.recordSet.1.field.1.description"]
    assert "refuses" in msgs["croissant.recordSet.1.field.1.description"]
    assert "croissant.recordSet.1.field.2.column" in msgs
    assert "croissant.recordSet.1.field.2.name" in msgs


def test_from_schema_column_cap_follows_the_avu_limit() -> None:
    assert mt.MAX_COLUMNS == (mt.MAX_AVUS - 3) // 4 == 1249
    cols = [{"name": f"c{i}", "type": "INTEGER"} for i in range(mt.MAX_COLUMNS)]
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=cols, record_set_id="t", source_id="s", record_set_name="T",
        description="d")))
    assert len(res["avus"]) <= mt.MAX_AVUS
    with pytest.raises(ToolError) as exc:
        run(mt.handle_from_schema(mt.FromSchemaInput(
            columns=[*cols, {"name": "one more", "type": "INTEGER"}],
            record_set_id="t", source_id="s")))
    assert exc.value.code == "invalid_argument"
    # STRUCT columns give more AVUs per column: a result over MAX_AVUS is refused.
    wide = [{"name": f"s{i}", "type": "STRUCT(a INTEGER, b INTEGER, c INTEGER)"}
            for i in range(600)]
    with pytest.raises(ToolError) as exc:
        run(mt.handle_from_schema(mt.FromSchemaInput(columns=wide, record_set_id="t",
                                                     source_id="s")))
    assert exc.value.code == "invalid_argument"
    assert str(mt.MAX_AVUS) in exc.value.message


def test_from_schema_empty_struct_is_text_with_a_warning() -> None:
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "u", "type": "STRUCT()"}, {"name": "v", "type": "STRUCT()[]"}],
        record_set_id="t", source_id="s")))
    got = {a["attribute"]: a["value"] for a in res["avus"]}
    assert got["croissant.recordSet.1.field.1.dataType"] == "sc:Text"
    assert got["croissant.recordSet.1.field.2.dataType"] == "sc:Text"
    assert got["croissant.recordSet.1.field.2.isArray"] == "true"
    assert not any(".subField." in a for a in got)
    assert [w["path"] for w in res["warnings"]] == [
        "croissant.recordSet.1.field.1", "croissant.recordSet.1.field.2"]
    assert "STRUCT without members" in res["warnings"][0]["message"]


def test_from_schema_avus_from_1_3_0_still_parse_and_export() -> None:
    """Backward compatibility: what 1.3.0 wrote for 'a b' (id t/a_b, column trimmed) and a
    duplicate id it could produce still parse and export; validate names the duplicate."""
    old = [
        avu("croissant.recordSet.1.id", "t"),
        avu("croissant.recordSet.1.field.1.name", "a b"),
        avu("croissant.recordSet.1.field.1.id", "t/a_b"),
        avu("croissant.recordSet.1.field.1.dataType", "sc:Text"),
        avu("croissant.recordSet.1.field.1.source", "ratings-csv"),
        avu("croissant.recordSet.1.field.1.column", "a b"),
        avu("croissant.recordSet.1.field.2.name", "a_b"),
        avu("croissant.recordSet.1.field.2.dataType", "sc:Text"),
        avu("croissant.recordSet.1.field.2.source", "ratings-csv"),
        avu("croissant.recordSet.1.field.2.column", "a_b"),
    ]
    parsed = core.parse_avus(old)
    assert parsed["unknown"] == [] and parsed["warnings"] == []
    assert sorted(triples(core.model_to_avus(parsed["model"]))) == sorted(triples(old))
    base = [a for a in GOLDEN["avus"] if not a["attribute"].startswith("croissant.recordSet.")]
    out = core.to_jsonld(base + old, item=GOLDEN["item"])
    assert [f["@id"] for f in out["document"]["recordSet"][0]["field"]] == ["t/a_b", "t/a_b"]
    assert any("duplicate @id" in e["message"] for e in out["validation"]["errors"])
    # The 1.3.0 AVUs are canonical keys: apply takes them as a new set.
    assert mt._key_errors(old) == ([], set())


# -- 1.3.1: apply refuses non-canonical keys and duplicate values -------------------------------


@pytest.mark.parametrize(("attrs", "path", "text"), [
    (["croissant.license.01"], "croissant.license.01", "not canonical"),
    (["croissant.creator.007.name"], "croissant.creator.007.name", "not canonical"),
    (["croissant.creator.0.name"], "croissant.creator.0.name", "not canonical"),
    (["croissant.distribution.-1.id"], "croissant.distribution.-1.id", "not canonical"),
    (["croissant.recordSet.1.field.+2.name"], "croissant.recordSet.1.field.+2.name",
     "not canonical"),
    (["croissant..name"], "croissant..name", "empty segment"),
    (["croissant.name."], "croissant.name.", "empty segment"),
    (["croissant.keyword.1", "croissant.keyword.3"], "croissant.keyword", "gap at 2"),
    (["croissant.recordSet.2.id"], "croissant.recordSet", "gap at 1"),
    (["croissant.recordSet.1.id", "croissant.recordSet.1.field.2.name"],
     "croissant.recordSet.1.field", "gap at 1"),
    (["croissant.recordSet.1.field.1.name", "croissant.recordSet.1.field.1.subField.2.name"],
     "croissant.recordSet.1.field.1.subField", "gap at 1"),
])
def test_apply_refuses_non_canonical_keys(irods, attrs, path, text) -> None:
    fake = irods([])
    new = [avu(a, f"v{i}") for i, a in enumerate(attrs)]
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
    hit = [e for e in res["import_errors"] if e["path"] == path]
    assert hit and text in hit[0]["message"], res["import_errors"]
    # The codec's own warning about the same key is not repeated.
    assert not any(w["path"] == path and "unknown Croissant key" in w["message"]
                   for w in res["warnings"])
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new, dry_run=False),
                            auth_value=AUTH))
    assert exc.value.code == "croissant_invalid"
    assert fake.added == []


@pytest.mark.parametrize("new", [
    [avu("croissant.name", "A"), avu("croissant.name", "B")],
    [avu("croissant.license", "https://a.org/l"), avu("croissant.license.1", "https://b.org/l")],
    [avu("croissant.license", "https://a.org/l"), avu("croissant.license.1", "https://a.org/l")],
    [avu("croissant.keyword.1", "grass", "ENVO:1"), avu("croissant.keyword.1", "grass", "ENVO:2")],
    [avu("croissant.recordSet.1.id", "t"), avu("croissant.recordSet.1.field.1.name", "x"),
     avu("croissant.recordSet.1.field.1.dataType", "sc:Text"),
     avu("croissant.recordSet.1.field.1.dataType.1", "cr:Int64")],
    [avu("croissant.creator.name", "A"), avu("croissant.creator.1.name", "B")],
])
def test_apply_refuses_two_values_for_one_path(irods, new) -> None:
    irods([])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
    errs = [e for e in res["import_errors"] if "rule 6" in e["message"]]
    assert len(errs) == 1 and len(res["import_errors"]) == 1, res["import_errors"]
    for a in new[-2:]:  # the two values of one path
        assert a["attribute"] in errs[0]["message"]
    assert not any("duplicate value" in w["message"] for w in res["warnings"])


def test_apply_live_finding_bad_index_set_is_refused(irods) -> None:
    """The live test's 13-AVU set (13 adds, no import error under 1.3.0)."""
    irods([])
    bad = [
        avu("croissant.description", "ok"), avu("croissant.creator.0.name", "Zero"),
        avu("croissant.license.01", "https://example.org/l"), avu("croissant.keyword.1", "a"),
        avu("croissant.keyword.3", "c"), avu("croissant.recordSet.0.id", "rs0"),
        avu("croissant.recordSet.2.field.5.name", "gapfield"),
        avu("croissant.creator.007.name", "Bond"), avu("croissant.distribution.-1.id", "neg"),
        avu("croissant.license.1.extra", "x"), avu("croissant.name.1", "x"),
        avu("croissant..name", "x"), avu("croissant.foo", "unknown"),
    ]
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=bad), auth_value=AUTH))
    paths = {e["path"] for e in res["import_errors"]}
    assert {"croissant.creator.0.name", "croissant.license.01", "croissant.keyword",
            "croissant.recordSet.0.id", "croissant.recordSet",
            "croissant.recordSet.2.field", "croissant.creator.007.name",
            "croissant.distribution.-1.id", "croissant..name"} == paths
    # Unknown keys stay accepted, with the codec's warning (rule 7).
    warned = {w["path"] for w in res["warnings"]}
    assert {"croissant.license.1.extra", "croissant.name.1", "croissant.foo"} <= warned


def test_apply_accepts_canonical_sets(irods) -> None:
    irods([])
    new = [avu("croissant.license", "https://a.org/l"), avu("croissant.keyword.1", "a"),
           avu("croissant.keyword.2", "b"), avu("croissant.creator.name", "A"),
           avu("croissant.creator.2.name", "B"), avu("croissant.custom.thing", "unknown"),
           avu("croissant.creator.3.orcidVerified", "true"),
           avu("croissant.name", "N"), avu("croissant.name", "N")]
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
    assert res["import_errors"] == []
    assert res["counts"]["new"] == 8  # identical triples are written once


def test_key_checks_pass_every_imported_document() -> None:
    """Documents imported by from_jsonld (folder and file item mode) never trip them."""
    docs = [load_json(p)["document"] for p in golden_paths()]
    docs += [load_json(p) for p in upstream_paths() + import_paths()]
    assert len(docs) >= 10
    for doc in docs:
        for item in (None, {"path": FOLDER, "name": "demo", "is_folder": True},
                     {"path": FILE, "name": "ratings.csv", "is_folder": False}):
            avus = core.from_jsonld(doc, item=item)["avus"]
            assert mt._key_errors(avus) == ([], set()), (doc.get("name"), item)


def test_apply_document_path_runs_the_key_checks(irods, monkeypatch) -> None:
    irods([])
    real = core.from_jsonld

    def broken(*a, **kw):
        out = real(*a, **kw)
        out["avus"].append(avu("croissant.license.01", "https://x.org/l"))
        return out

    monkeypatch.setattr(mt.core, "from_jsonld", broken)
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=GOLDEN["document"]),
                              auth_value=AUTH))
    assert any(e["path"] == "croissant.license.01" for e in res["import_errors"])


def test_apply_current_avus_from_1_3_0_are_not_refused(irods) -> None:
    """Backward compatibility: non-canonical AVUs already on an item (1.3.0 accepted them)
    still export and can be replaced; only the new set is checked."""
    stored = [avu("croissant.license.01", "https://old.org/l"),
              avu("croissant.keyword.1", "a"), avu("croissant.keyword.3", "c"),
              avu("croissant.name", "A"), avu("croissant.name", "B")]
    irods(stored)
    exp = run(mt.handle_export(mt.ExportInput(path=FOLDER), auth_value=AUTH))
    assert exp["document"]["name"] == "A"
    assert exp["document"]["keywords"] == ["a", "c"]
    for mode in ("merge", "replace"):
        res = run(mt.handle_apply(mt.ApplyInput(
            path=FOLDER, avus=[avu("croissant.name", "C"), avu("croissant.license.1",
                                                                 "https://new.org/l")],
            mode=mode), auth_value=AUTH))
        assert res["import_errors"] == [], mode
        removed = triples(res["diff"]["remove"])
        assert {("croissant.name", "A", ""), ("croissant.name", "B", ""),
                ("croissant.license.01", "https://old.org/l", "")} <= removed, mode


def test_apply_refuses_more_than_max_avus_even_in_a_dry_run(irods) -> None:
    irods([])
    new = [avu("croissant.recordSet.1.id", "t")] + [
        avu(f"croissant.recordSet.1.field.{i}.name", f"f{i}") for i in range(1, mt.MAX_AVUS + 1)]
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
    assert exc.value.code == "invalid_argument"
    assert exc.value.details == {"count": mt.MAX_AVUS + 1, "limit": mt.MAX_AVUS}
    ok = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new[:mt.MAX_AVUS]),
                             auth_value=AUTH))
    assert ok["counts"]["new"] == mt.MAX_AVUS


# -- 1.3.1: trimming, deep JSON, template ------------------------------------------------------


def test_apply_warns_when_it_trims(irods) -> None:
    irods([])
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=[
        avu("croissant.description", "  Line one.\nLine two.\n"),
        avu(" croissant.version ", " 1.0.0 "),
        avu("croissant.keyword.1", "grass", " ENVO:1 "),
        avu("croissant.name", "Untouched")]), auth_value=AUTH))
    assert triples(res["diff"]["add"]) == {
        ("croissant.description", "Line one.\nLine two.", ""),
        ("croissant.version", "1.0.0", ""), ("croissant.keyword.1", "grass", "ENVO:1"),
        ("croissant.name", "Untouched", "")}
    trimmed = [w["path"] for w in res["warnings"] if "trimmed" in w["message"]]
    assert trimmed == ["croissant.description", "croissant.version", "croissant.keyword.1"]
    doc = {**GOLDEN["document"], "description": "Ratings.\n"}
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, document=doc), auth_value=AUTH))
    assert any(w["path"] == "croissant.description" and "trimmed" in w["message"]
               for w in res["warnings"])


@pytest.mark.parametrize("deep", [
    "[" * 100000 + "]" * 100000,
    "[" * 101 + "]" * 101,
    '{"a":' * 500 + "1" + "}" * 500,
])
def test_validate_and_apply_refuse_deeply_nested_json(irods, deep) -> None:
    irods([])
    for call in (lambda d: mt.handle_validate(mt.ValidateInput(document=d)),
                 lambda d: mt.handle_apply(mt.ApplyInput(path=FOLDER, document=d),
                                           auth_value=AUTH)):
        with pytest.raises(ToolError) as exc:
            run(call(deep))
        assert exc.value.code == "invalid_argument"
        assert "nested more than 100 levels" in exc.value.message
    obj: Any = {"@type": "sc:Dataset"}
    for _ in range(200):
        obj = {"x": obj}
    with pytest.raises(ToolError):
        run(mt.handle_validate(mt.ValidateInput(document=obj)))


def test_validate_accepts_nesting_up_to_the_limit() -> None:
    res = run(mt.handle_validate(mt.ValidateInput(document="[" * 100 + "]" * 100)))
    assert res["valid"] is False and res["errors"][0]["message"] == \
        "document must be a JSON object"
    res = run(mt.handle_validate(mt.ValidateInput(document="{not json")))
    assert "not valid JSON" in res["errors"][0]["message"]


def test_template_required_flags_and_datatype_forms() -> None:
    res = run(mt.handle_template(mt.TemplateInput(level="all")))
    rows = {k["attribute"]: k for k in res["keys"]}
    for attr in ("croissant.creator.N.name", "croissant.publisher.N.name",
                 "croissant.distribution.N.id", "croissant.recordSet.N.id",
                 "croissant.recordSet.N.field.M.name"):
        assert rows[attr]["required"], attr
    assert not rows["croissant.publisher.N.email"]["required"]
    bare = rows["croissant.recordSet.N.field.M.dataType"]
    indexed = rows["croissant.recordSet.N.field.M.dataType.K"]
    assert (bare["cardinality"], indexed["cardinality"]) == ("one", "many")
    assert "dataType.1" in bare["description"]
    assert any("required:" in n for n in res["notes"])
    # The bare form is what from_schema writes.
    fs = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "a", "type": "INTEGER"}], record_set_id="t", source_id="s")))
    assert "croissant.recordSet.1.field.1.dataType" in {a["attribute"] for a in fs["avus"]}


# -- 1.3.1 (pass 3): merge checks gaps on the result, refused values clipped, real ids --------

#: A dataset with two creators, two keywords, one license and record set 1 with fields 1-3.
_LAKE = [
    avu("croissant.name", "N"), avu("croissant.description", "D"),
    avu("croissant.license.1", "https://x.org/l"), avu("croissant.datePublished", "2026"),
    avu("croissant.creator.1.name", "A"), avu("croissant.creator.2.name", "B"),
    avu("croissant.keyword.1", "k1"), avu("croissant.keyword.2", "k2"),
    avu("croissant.distribution.1.id", "s"), avu("croissant.distribution.1.type", "FileSet"),
    avu("croissant.distribution.1.encodingFormat", "text/csv"),
    avu("croissant.distribution.1.includes.1", "*.csv"),
    avu("croissant.recordSet.1.id", "t1"),
] + [x for m in (1, 2, 3) for x in (
    avu(f"croissant.recordSet.1.field.{m}.name", f"f{m}"),
    avu(f"croissant.recordSet.1.field.{m}.dataType", "sc:Text"),
    avu(f"croissant.recordSet.1.field.{m}.source", "s"),
    avu(f"croissant.recordSet.1.field.{m}.column", f"f{m}"))]


@pytest.mark.parametrize("new", [
    [avu("croissant.creator.2.name", "Bee")],
    [avu("croissant.recordSet.2.id", "t2"), avu("croissant.recordSet.2.field.1.name", "g"),
     avu("croissant.recordSet.2.field.1.dataType", "sc:Text"),
     avu("croissant.recordSet.2.field.1.source", "s"),
     avu("croissant.recordSet.2.field.1.column", "g")],
    [avu("croissant.keyword.3", "k3")],
    [avu("croissant.recordSet.1.field.3.name", "f3"),
     avu("croissant.recordSet.1.field.3.description", "third"),
     avu("croissant.recordSet.1.field.3.dataType", "sc:Text"),
     avu("croissant.recordSet.1.field.3.source", "s"),
     avu("croissant.recordSet.1.field.3.column", "f3")],
    [avu("croissant.recordSet.1.field.4.name", "f4"),
     avu("croissant.recordSet.1.field.4.dataType", "sc:Text"),
     avu("croissant.recordSet.1.field.4.source", "s")],
    [avu("croissant.license.2", "https://y.org/l")],
], ids=["creator-2", "record-set-2", "keyword-3", "field-3", "field-4", "license-2"])
def test_apply_merge_accepts_partial_updates(irods, new) -> None:
    """Regression of pass 2: merge refused every update starting at index 2 or higher as a
    gap, though the result has none (1.3.0 accepted them)."""
    fake = irods(_LAKE)
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
    assert res["import_errors"] == [] and res["validation"]["valid"] is True
    out = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new, dry_run=False),
                              auth_value=AUTH))
    assert res["counts"]["add"] >= 1
    assert out["written"]["added"] == res["counts"]["add"] == len(fake.added)


def test_apply_merge_takes_from_schema_record_set_2(irods) -> None:
    """The documented DuckLake workflow: one from_schema + one merge apply per table."""
    fake = irods(_LAKE)
    fs = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=[{"name": "siteID", "type": "VARCHAR"}, {"name": "n", "type": "INTEGER"}],
        record_set_id="plots", source_id="s", record_set_index=2)))
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=fs["avus"], dry_run=False),
                              auth_value=AUTH))
    assert res["import_errors"] == [] and len(fake.added) == len(fs["avus"])
    exp = run(mt.handle_export(mt.ExportInput(path=FOLDER), auth_value=AUTH))
    assert [r["@id"] for r in exp["document"]["recordSet"]] == ["t1", "plots"]


@pytest.mark.parametrize(("current", "new", "path", "gap"), [
    # The item lacks the entries before the new one: the result has the gap.
    ([], [avu("croissant.recordSet.2.id", "t2")], "croissant.recordSet", 1),
    ([avu("croissant.keyword.1", "a")], [avu("croissant.keyword.3", "c")],
     "croissant.keyword", 2),
    # merge replaces recordSet.1 (the new set gives its id): fields 1-2 go, field 3 stays new.
    (_LAKE, [avu("croissant.recordSet.1.id", "t1"),
             avu("croissant.recordSet.1.field.3.name", "f3")],
     "croissant.recordSet.1.field", 1),
    # A new index after a gap the item already has.
    ([avu("croissant.keyword.1", "a"), avu("croissant.keyword.3", "c")],
     [avu("croissant.keyword.4", "d")], "croissant.keyword", 2),
])
def test_apply_merge_refuses_a_gap_in_the_result(irods, current, new, path, gap) -> None:
    fake = irods(current)
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
    hit = [e for e in res["import_errors"] if e["path"] == path]
    assert hit and f"gap at {gap} of the result" in hit[0]["message"], res["import_errors"]
    with pytest.raises(ToolError) as exc:
        run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new, dry_run=False),
                            auth_value=AUTH))
    assert exc.value.code == "croissant_invalid" and fake.added == []


def test_apply_replace_checks_gaps_on_the_new_set_alone(irods) -> None:
    irods(_LAKE)
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.creator.2.name", "Bee")], mode="replace"),
        auth_value=AUTH))
    assert [e["path"] for e in res["import_errors"]] == ["croissant.creator"]
    assert "of the result" not in res["import_errors"][0]["message"]


def test_apply_merge_with_1_3_0_avus_and_partial_sets(irods) -> None:
    """Backward compatibility: AVUs 1.3.0 accepted (a gap, leading zeros) stay usable. An
    entry after a gap the item already has can be edited; 01 counts as index 1 the way
    readers read it; the other checks still look at the new set only."""
    stored = [avu("croissant.license.01", "https://old.org/l"),
              avu("croissant.keyword.1", "a"), avu("croissant.keyword.3", "c"),
              avu("croissant.creator.01.name", "A")]
    irods(stored)
    for new in ([avu("croissant.keyword.3", "see")],
                [avu("croissant.license.2", "https://two.org/l")],
                [avu("croissant.creator.2.name", "B")]):
        res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, avus=new), auth_value=AUTH))
        assert res["import_errors"] == [], (new, res["import_errors"])
    exp = run(mt.handle_export(mt.ExportInput(path=FOLDER), auth_value=AUTH))
    assert exp["document"]["keywords"] == ["a", "c"]
    # Canonical keys stay required in the new set.
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.keyword.02", "b")]), auth_value=AUTH))
    assert [e["path"] for e in res["import_errors"]] == ["croissant.keyword.02"]


def test_key_errors_gap_check_is_bounded() -> None:
    """An index can be any number: the check never walks up to it."""
    errs, bad = mt._key_errors([avu("croissant.keyword.999999999999", "x")],
                               before=[], after=[])
    assert "gap at 1" in errs[0]["message"] and bad == {"croissant.keyword.999999999999"}
    errs, _ = mt._key_errors([avu("croissant.keyword.999999999999", "x")],
                             before=["croissant.keyword.999999999999"],
                             after=[])
    assert errs == []  # an entry the item has, after a gap it already has


@pytest.mark.parametrize("via", ["avus", "document"])
def test_apply_refused_dry_run_clips_over_limit_values(irods, via) -> None:
    """A refused value is not echoed whole (live: a 1,200,000-character description made a
    2.4 MB dry run); the import error keeps its length."""
    irods([])
    long = "x" * 1_200_000
    if via == "avus":
        arg = {"avus": [avu("croissant.name", "N"), avu("croissant.description", long),
                        avu("croissant.keyword.1", "k", "u" * 300)]}
    else:
        doc = copy.deepcopy(GOLDEN["document"])
        doc["description"] = long
        arg = {"document": doc}
    res = run(mt.handle_apply(mt.ApplyInput(path=FOLDER, **arg), auth_value=AUTH))
    assert any("1200000 characters" in e["message"] for e in res["import_errors"])
    add = {a["attribute"]: a for a in res["diff"]["add"]}
    desc = add["croissant.description"]["value"]
    assert len(desc) < 300 and desc.startswith("x" * mt._CLIP_SHOWN)
    assert "clipped: 1200000 characters" in desc
    assert len(json.dumps(res)) < 50_000
    if via == "avus":
        assert add["croissant.name"]["value"] == "N"
        assert "clipped: 300 characters" in add["croissant.keyword.1"]["unit"]


def test_apply_does_not_clip_values_within_the_limits(irods) -> None:
    irods([])
    value = "y" * core.VALUE_MAX
    res = run(mt.handle_apply(mt.ApplyInput(
        path=FOLDER, avus=[avu("croissant.description", value, "u" * core.UNIT_MAX)]),
        auth_value=AUTH))
    assert res["import_errors"] == []
    assert res["diff"]["add"] == [avu("croissant.description", value, "u" * core.UNIT_MAX)]


def _ids(cols: list[dict[str, str]]) -> list[tuple[str, str]]:
    res = run(mt.handle_from_schema(mt.FromSchemaInput(
        columns=cols, record_set_id="t", source_id="s")))
    out = core.to_jsonld([avu("croissant.distribution.1.id", "s"),
                          avu("croissant.distribution.1.type", "FileObject")] + res["avus"],
                         item={"path": FOLDER, "name": "demo"})
    pairs: list[tuple[str, str]] = []

    def walk(fs: list[dict[str, Any]]) -> None:
        for f in fs:
            pairs.append((f["name"], f["@id"]))
            walk(f.get("subField", []))

    walk(out["document"]["recordSet"][0]["field"])
    assert len({i for _, i in pairs}) == len(pairs), pairs
    assert not any(k.startswith("_") for a in res["avus"] for k in a), res["avus"]
    assert not any("._" in a["attribute"] for a in res["avus"])
    return pairs


@pytest.mark.parametrize(("cols", "expected"), [
    # A renamed STRUCT's real member keeps <new id>/<name>; the whitespace member yields.
    ([{"name": "s t", "type": 'STRUCT("x y" INTEGER, x_y INTEGER)'},
      {"name": "s_t", "type": "INTEGER"}],
     [("s t", "t/s_t-2"), ("x y", "t/s_t-2/x_y-2"), ("x_y", "t/s_t-2/x_y"), ("s_t", "t/s_t")]),
    # A trimmed name is not real: the exact column keeps t/a.
    ([{"name": "a ", "type": "INTEGER"}, {"name": "a", "type": "INTEGER"}],
     [("a", "t/a-2"), ("a", "t/a")]),
    # A real column name with '/' equal to a member's path keeps it.
    ([{"name": "s t", "type": "STRUCT(a INTEGER)"}, {"name": "s_t/a", "type": "INTEGER"}],
     [("s t", "t/s_t"), ("a", "t/s_t/a-2"), ("s_t/a", "t/s_t/a")]),
    # Unchanged from pass 2.
    ([{"name": "a", "type": "INTEGER"}, {"name": "a-2", "type": "INTEGER"},
      {"name": "a ", "type": "INTEGER"}],
     [("a", "t/a"), ("a-2", "t/a-2"), ("a", "t/a-3")]),
    ([{"name": "s", "type": 'STRUCT("x y" INTEGER, x_y INTEGER)'}],
     [("s", "t/s"), ("x y", "t/s/x_y-2"), ("x_y", "t/s/x_y")]),
    # Two real fields with one default: the first in order keeps it.
    ([{"name": "s/a", "type": "INTEGER"}, {"name": "s", "type": "STRUCT(a INTEGER)"}],
     [("s/a", "t/s/a"), ("s", "t/s"), ("a", "t/s/a-2")]),
])
def test_from_schema_real_names_keep_their_default_ids(cols, expected) -> None:
    assert _ids(cols) == expected
