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
from conftest import FIXTURES, load_json

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
    assert "croissant.recordSet.1.field.5" in warned
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
