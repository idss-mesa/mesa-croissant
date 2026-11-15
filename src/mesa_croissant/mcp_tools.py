"""``mesa_croissant_*``: MLCommons Croissant tools inside the MESA-MCP tool registry.

Importing this module registers the tools. MESA-MCP imports it through the
``mesa_mcp.tools`` entry point (``croissant = "mesa_croissant.mcp_tools"`` in this
package's ``pyproject.toml``); every tool declares ``meta={"io.mesa/surface": "croissant"}``.

* ``mesa_croissant_template``: the AVU key catalog of the convention (no I/O).
* ``mesa_croissant_export``: reads a path's AVUs and generates the Croissant JSON-LD.
* ``mesa_croissant_validate``: validates a JSON-LD document, or the document of a path.
* ``mesa_croissant_apply``: imports a document (or takes ``croissant.*`` AVUs), diffs them
  against the path's ``croissant.*`` AVUs and, unless ``dry_run`` (the default), writes the
  diff and mirrors it into the DuckLake AVU history as one snapshot.
* ``mesa_croissant_from_schema``: record-set AVUs from a DuckDB / Arrow column schema.

The codec itself is :mod:`mesa_croissant.core` (the same file the MESA Portal vendors), so a
dataset annotated by an agent and one annotated in the portal carry identical AVUs.
See ``docs/AVU_CONVENTION.md`` and ``docs/DESIGN.md``.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
import re
from typing import Any, Literal, NamedTuple

from mesa_mcp.auth.models import AuthValue
from mesa_mcp.ducklake.client import DuckLakeMirrorError, record_avu_changes
from mesa_mcp.errors import ToolError
from mesa_mcp.irods._avu_helpers import (
    add_avu_to_irods,
    list_avus_for_path,
    resolve_path_target,
)
from mesa_mcp.irods.access import assert_allowed
from mesa_mcp.irods.client_pool import default_pool
from mesa_mcp.server import register_tool
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from mesa_croissant import core

TOOL_SURFACE = "croissant"
META = {"io.mesa/surface": TOOL_SURFACE}

TEMPLATE = "mesa_croissant_template"
EXPORT = "mesa_croissant_export"
VALIDATE = "mesa_croissant_validate"
APPLY = "mesa_croissant_apply"
FROM_SCHEMA = "mesa_croissant_from_schema"
TOOL_NAMES = (TEMPLATE, EXPORT, VALIDATE, APPLY, FROM_SCHEMA)

#: Most AVUs one apply call may write (the portal's per-request limit).
MAX_AVUS = 5000
#: AVUs a plain column gives in from_schema (name, dataType, source, column).
_AVUS_PER_COLUMN = 4
#: Most columns one from_schema call accepts: derived from MAX_AVUS (less the record set's
#: id, name and description), so the result of a full call still fits one apply call.
#: STRUCT and MAP columns give more AVUs; a result over MAX_AVUS is refused.
MAX_COLUMNS = (MAX_AVUS - 3) // _AVUS_PER_COLUMN

_WS_RE = re.compile(r"\s+")
#: An index segment, matched with ``fullmatch`` as core does (``$`` would accept a final "\n").
_INDEX_SEG_RE = core._INDEX_RE

KeywordStyle = Literal["defined_term", "text"]


def _msg(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


# -- shared schemas --------------------------------------------------------------------------------
class AvuItem(BaseModel):
    """One AVU triple."""

    attribute: str
    value: str
    unit: str = ""


class Message(BaseModel):
    """A validation or import message. ``path`` is a JSON-LD path or an AVU attribute."""

    path: str
    message: str


class Summary(BaseModel):
    distribution: int = 0
    recordSet: int = 0
    field: int = 0


class ValidationReport(BaseModel):
    """:func:`mesa_croissant.core.validate` result."""

    valid: bool
    errors: list[Message]
    warnings: list[Message]
    conformsTo: list[str]
    summary: Summary


# -- mesa_croissant_template -----------------------------------------------------------------------
class TemplateInput(BaseModel):
    level: Literal["dataset", "rai", "geo", "file", "distribution", "recordset", "all"] = Field(
        default="all",
        description=(
            "Which keys to list: dataset (core properties, creators, publishers), rai, geo, "
            "file (croissant.file.*), distribution, recordset (record sets and fields) or all."
        ),
    )


class KeyRow(BaseModel):
    attribute: str
    cardinality: Literal["one", "many"]
    required: bool = Field(
        ...,
        description=(
            "Required where the key lives: in the dataset for a dataset-level key, in each "
            "given entry for a key below an entry (N, M, K)."
        ),
    )
    level: str
    unit: str
    description: str


class TemplateOutput(BaseModel):
    convention_version: int
    core_version: str
    prefix: str
    level: str
    conforms_to: dict[str, str]
    keys: list[KeyRow]
    notes: list[str]


_TEMPLATE_NOTES = [
    "N, M and K stand for 1-based indexes, numbered without gaps (croissant.creator.1.name); "
    "mesa_croissant_apply refuses index 0, leading zeros, gaps and two values for one path. "
    "In merge mode the gap check covers the result (the new set plus the item's entries that "
    "stay), so a set may start at index 2 or higher when the item has the entries before it "
    "(creator.2 alone, record set 2 from mesa_croissant_from_schema).",
    "A repeatable key may also be written without its index (index 1). Writers use the "
    "indexed form, except a field's single dataType, written bare: both dataType rows are "
    "listed; give one form or the other.",
    "required: required in the dataset for a dataset-level key; for a key below an entry "
    "(N, M, K), required in each entry of that list that is given (publisher is optional, "
    "but each publisher needs a name).",
    "Values are text; booleans are true/false; dates are ISO 8601; a value is never empty.",
    "Values are at most 2,700 characters and units at most 250. Writers store values "
    "trimmed of leading and trailing whitespace (line breaks inside a value are kept).",
    "Units are empty except croissant.keyword.N (CURIE or IRI of the term), "
    "croissant.geo.*Resolution (unit text) and croissant.distribution.N.contentSize (default B).",
    "Required properties without a croissant.* AVU are filled from datacite.*, dc.* and "
    "ontology AVUs when the document is generated (the crosswalk); they are never copied.",
    "Unknown croissant.* keys and extra duplicate values are kept by every editor (with "
    "their entity) but not emitted.",
]


@register_tool(
    TEMPLATE,
    "Return the MESA Croissant AVU key catalog (attribute, cardinality, required, unit, "
    "description) for dataset, RAI, geo, file, distribution or record-set metadata, to drive "
    "a complete Croissant 1.1 annotation of a Data Store folder or file. No I/O.",
    input_model=TemplateInput,
    output_model=TemplateOutput,
    meta=META,
)
async def handle_template(args: TemplateInput) -> dict[str, Any]:
    keys = core.key_catalog(args.level)
    return TemplateOutput(
        convention_version=core.CONVENTION_VERSION,
        core_version=core.CORE_VERSION,
        prefix=core.PREFIX,
        level=args.level,
        conforms_to={
            "core": core.CONFORMS_CORE,
            "rai": core.CONFORMS_RAI,
            "geo": core.CONFORMS_GEO,
        },
        keys=[KeyRow(**row) for row in keys],
        notes=list(_TEMPLATE_NOTES),
    ).model_dump()


# -- path helpers ----------------------------------------------------------------------------------
def _require_auth(auth_value: AuthValue | None, tool: str) -> AuthValue:
    if auth_value is None:
        raise ToolError(code="unauthenticated", message=f"{tool} requires authentication.")
    return auth_value


def _open_path(path: str, auth_value: AuthValue) -> tuple[str, Any, str]:
    """Access-check ``path``; return ``(normalised path, session, path target type)``."""
    norm = assert_allowed(path, auth_value)
    session = default_pool().get(auth_value)
    target = resolve_path_target(session, norm)
    return norm, session, target


def _clean_avus(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "attribute": str(r.get("attribute") or ""),
            "value": "" if r.get("value") is None else str(r.get("value")),
            "unit": str(r.get("unit") or ""),
        }
        for r in rows
    ]


def _md5_hex(checksum: Any) -> str | None:
    """An iRODS checksum as an MD5 hex digest, or None (``sha2:`` checksums are not MD5)."""
    text = str(checksum or "").strip()
    if text.lower().startswith("md5:"):
        text = text[4:]
    return text.lower() if re.fullmatch(r"[0-9a-fA-F]{32}", text) else None


def _item_for(session: Any, norm: str, target: str, url: str | None,
              keyword_style: KeywordStyle) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": norm,
        "name": posixpath.basename(norm.rstrip("/")) or norm,
        "is_folder": target == "collection",
    }
    if url:
        item["url"] = url
    if keyword_style != "defined_term":
        item["keyword_style"] = keyword_style
    if target == "data_object":
        with contextlib.suppress(Exception):  # stat extras are optional
            obj = session.data_objects.get(norm)
            size = getattr(obj, "size", None)
            if isinstance(size, int) and size >= 0:
                item["size"] = size
            md5 = _md5_hex(getattr(obj, "checksum", None))
            if md5:
                item["md5"] = md5
    return item


def _generate(path: str, auth_value: AuthValue, url: str | None,
              keyword_style: KeywordStyle) -> tuple[str, str, dict[str, Any], int]:
    norm, session, target = _open_path(path, auth_value)
    avus = _clean_avus(list_avus_for_path(session, norm, target))
    item = _item_for(session, norm, target, url, keyword_style)
    generated = core.to_jsonld(avus, item=item)
    count = sum(1 for a in avus if core.is_croissant_attr(a["attribute"]))
    return norm, target, generated, count


# -- mesa_croissant_export -------------------------------------------------------------------------
class ExportInput(BaseModel):
    path: str = Field(..., description="iRODS path of the dataset folder or the file.")
    url: str | None = Field(
        default=None,
        description=(
            "Landing page URL used when the item has no croissant.url AVU and no DOI "
            "(default: the MESA Portal metadata page)."
        ),
    )
    keyword_style: KeywordStyle = Field(
        default="defined_term",
        description=(
            "defined_term (default, Croissant 1.1) writes ontology keywords as sc:DefinedTerm; "
            "text writes plain names, which mlcroissant 1.1.1 requires to load the document."
        ),
    )


class ExportOutput(BaseModel):
    path: str
    path_target_type: Literal["data_object", "collection"]
    document: dict[str, Any]
    sources: dict[str, str]
    validation: ValidationReport
    croissant_avu_count: int


@register_tool(
    EXPORT,
    "Generate the MLCommons Croissant 1.1 JSON-LD document (with RAI and GeoCroissant when "
    "annotated) of a Data Store folder or file from its croissant.* AVUs, filling missing "
    "required properties from DataCite / Dublin Core / ontology AVUs, and validate it.",
    input_model=ExportInput,
    output_model=ExportOutput,
    meta=META,
)
async def handle_export(
    args: ExportInput, *, auth_value: AuthValue | None = None
) -> dict[str, Any]:
    auth = _require_auth(auth_value, EXPORT)
    norm, target, generated, count = _generate(args.path, auth, args.url, args.keyword_style)
    return {
        "path": norm,
        "path_target_type": target,
        "document": generated["document"],
        "sources": generated["sources"],
        "validation": generated["validation"],
        "croissant_avu_count": count,
    }


# -- mesa_croissant_validate -----------------------------------------------------------------------
class ValidateInput(BaseModel):
    document: dict[str, Any] | str | None = Field(
        default=None, description="A Croissant JSON-LD document (object or JSON text)."
    )
    path: str | None = Field(
        default=None,
        description="Or: the iRODS path whose generated document is validated.",
    )


class ValidateOutput(ValidationReport):
    checked: Literal["document", "path"]
    path: str | None = None
    hint: str


_MLCROISSANT_HINT = (
    "This is mesa-croissant's structural check. For the full MLCommons check run "
    "`mlcroissant validate --jsonld <file>` (pip install mlcroissant)."
)


@register_tool(
    VALIDATE,
    "Validate a Croissant JSON-LD document (or the document generated for an iRODS path): "
    "required properties, @id uniqueness, field sources and references, data types, dates, "
    "bounding boxes and AVU limits. Returns errors, warnings, conformsTo and counts.",
    input_model=ValidateInput,
    output_model=ValidateOutput,
    meta=META,
)
async def handle_validate(
    args: ValidateInput, *, auth_value: AuthValue | None = None
) -> dict[str, Any]:
    if (args.document is None) == (args.path is None):
        raise ToolError(
            code="invalid_argument",
            message="Give exactly one of 'document' or 'path'.",
        )
    if args.document is not None:
        report = core.validate(_document_arg(args.document))
        return {**report, "checked": "document", "path": None, "hint": _MLCROISSANT_HINT}
    auth = _require_auth(auth_value, VALIDATE)
    norm, _target, generated, _count = _generate(str(args.path), auth, None, "defined_term")
    return {**generated["validation"], "checked": "path", "path": norm,
            "hint": _MLCROISSANT_HINT}


# -- mesa_croissant_apply --------------------------------------------------------------------------
class ApplyInput(BaseModel):
    path: str = Field(..., description="iRODS path of the dataset folder or the file.")
    document: dict[str, Any] | str | None = Field(
        default=None,
        description=(
            "A Croissant 0.8 / 1.0 / 1.1 JSON-LD document (object or JSON text), imported "
            "as croissant.* AVUs. For a file, the FileObject that describes it becomes "
            "croissant.file.* and other distribution entries are reported. Generated "
            "values are not stored: croissant.url equal to the default landing page or "
            "the DOI URL, croissant.identifier equal to the DOI URL, and any property "
            "equal to what the crosswalk (datacite.*, dc.*, ontology AVUs) or the item "
            "name gives; merge then also removes the item's current value of it."
        ),
    )
    avus: list[AvuItem] | None = Field(
        default=None,
        description=(
            "Or: croissant.* AVUs to apply (e.g. from mesa_croissant_from_schema), trimmed "
            "of surrounding whitespace (with a warning). Refused: other attributes, empty "
            "values, values over the AVU limits, and keys that are not canonical: index 0, "
            "leading zeros (license.01), signs, empty segments, gaps in a list (keyword.1 "
            "and keyword.3; in merge mode checked on the result, so keyword.3 alone is fine "
            "when the item has keyword.1 and keyword.2) and two values for one single-valued "
            "path or index "
            "(croissant.name twice, croissant.license next to croissant.license.1). Unknown "
            "croissant.* keys are accepted with a warning."
        ),
    )
    mode: Literal["merge", "replace"] = Field(
        default="merge",
        description=(
            "merge (per entry, on canonical paths: croissant.license = croissant.license.1): "
            "a path of the new set replaces its current value; an entity the new set gives a "
            "property of (creator.N, publisher.N, distribution.N, recordSet.N, "
            "recordSet.N.field.M, ...subField.K) replaces the current entity of that index "
            "as a whole (its other properties, and its nested entries of a list the new set "
            "also gives, are removed); every other croissant.* AVU stays, and a warning names "
            "kept entries of lists the new set gives. replace: the item's croissant.* AVUs "
            "become exactly the new set plus any unknown croissant.* keys (and index-0 "
            "entries), which are kept unless drop_unknown. In both modes an unknown key below "
            "an entity the diff removes is removed with it. Non-croissant AVUs are never "
            "touched."
        ),
    )
    drop_unknown: bool = Field(
        default=False,
        description=(
            "replace only: also remove current croissant.* keys outside the convention "
            "(unknown keys, index-0 entries), which a document cannot carry."
        ),
    )
    dry_run: bool = Field(
        default=True, description="Report the diff without writing (default true)."
    )
    content_base: str | None = Field(
        default=None,
        description=(
            "Document import only: contentUrls under this URL are stored as paths relative to "
            "the dataset root (default: the path's dav-anon / dav URL)."
        ),
    )


class Diff(BaseModel):
    add: list[AvuItem]
    remove: list[AvuItem]
    unchanged: list[AvuItem]


class WriteError(BaseModel):
    op: Literal["add", "remove"]
    attribute: str
    error: str


class PartialFailure(BaseModel):
    code: str
    message: str
    project_id: str | None = None


class ApplyOutput(BaseModel):
    path: str
    path_target_type: Literal["data_object", "collection"]
    mode: Literal["merge", "replace"]
    dry_run: bool
    diff: Diff
    counts: dict[str, int]
    written: dict[str, int]
    errors: list[WriteError]
    kept: list[AvuItem]
    warnings: list[Message]
    import_errors: list[Message]
    validation: ValidationReport
    partial_failure: PartialFailure | None = None


def _triple(a: dict[str, str]) -> tuple[str, str, str]:
    return (a["attribute"], a["value"], a["unit"])


def _avu(t: tuple[str, str, str]) -> dict[str, str]:
    return {"attribute": t[0], "value": t[1], "unit": t[2]}


def _remove_exact(session: Any, norm: str, target: str, r: dict[str, str]) -> dict[str, str]:
    """Remove the AVU with exactly this stored triple (no stripping).

    mesa-mcp's ``delete_avu_from_irods`` strips the triple, so for a stored value with
    surrounding whitespace it would remove a different AVU (or the one just added).
    """
    from irods.meta import iRODSMeta  # type: ignore[import-not-found,import-untyped,unused-ignore]
    from irods.models import Collection, DataObject  # type: ignore[import-not-found,import-untyped,unused-ignore]

    model = DataObject if target == "data_object" else Collection
    try:
        session.metadata.remove(model, norm, iRODSMeta(r["attribute"], r["value"], r["unit"]))
    except Exception as exc:  # noqa: BLE001 - python-irodsclient raises many
        raise ToolError(
            code="irods_error",
            message=f"Failed to delete AVU from {norm!r}: {exc}",
            details={"path": norm, "target_type": target, "attribute": r["attribute"],
                     "value": r["value"], "unit": r["unit"]},
        ) from exc
    return {"attribute": r["attribute"], "value": r["value"], "unit": r["unit"]}


_TRIMMED = ("leading or trailing whitespace trimmed: MESA writers store values and units "
            "trimmed (whitespace inside the value, line breaks included, is kept)")


def _check_new_avus(
    items: list[AvuItem],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Normalise caller AVUs the way the iRODS helper writes them.

    Returns ``(avus, errors, warnings)``. Attribute, value and unit are trimmed (mesa-mcp's
    ``add_avu_to_irods`` strips every triple, so nothing else can be written); a value or
    unit that loses whitespace gets a warning (convention rule 3). Identical triples are
    written once.
    """
    out: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    trimmed: set[str] = set()
    for it in items:
        attr, value, unit = it.attribute.strip(), it.value.strip(), it.unit.strip()
        if not core.is_croissant_attr(attr):
            errors.append(_msg(attr or "(empty)",
                                    "only croissant.* attributes can be applied"))
            continue
        if not value:
            errors.append(_msg(attr, "empty value (iRODS refuses empty values)"))
            continue
        if (value != it.value or unit != it.unit or attr != it.attribute) \
                and attr not in trimmed:
            trimmed.add(attr)
            warnings.append(_msg(attr, _TRIMMED))
        t = (attr, value, unit)
        if t in seen:
            continue
        seen.add(t)
        out.append(_avu(t))
    return out, errors, warnings


_INDEX_LIKE_RE = re.compile(r"[+-]?[0-9]+")
_CANONICAL_INDEX_RE = re.compile(r"[1-9][0-9]*")
_LISTED = 10  # offending attributes named in one message


def _named(attrs: list[str]) -> str:
    shown = ", ".join(attrs[:_LISTED])
    return shown + (f" and {len(attrs) - _LISTED} more" if len(attrs) > _LISTED else "")


def _index_problem(attr: str, lists: dict[str, dict[int, list[str]]],
                   stored: bool = False) -> str | None:
    """Why ``attr`` is not a canonical key, or None; records the list indexes it uses.

    Canonical: no empty segment, and every index segment of a list (repeatable leaf or
    entity) is ``1``, ``2``, ... without leading zeros or a sign. A repeatable key without
    its index is canonical (rule 5: index 1). Unknown keys are not checked past the point
    where they leave the key schema (they are kept with a warning, rule 7). ``lists`` maps a
    canonical list prefix (``croissant.recordSet.1.field``) to its indexes and attributes.

    ``stored``: ``attr`` is already on the item, and its indexes are recorded the way
    readers place it (rule 5): ``01`` is index 1; index 0 and signed indexes are not
    recorded (readers ignore index 0 and treat a sign as an unknown key).
    """
    segs = attr[len(core.PREFIX):].split(".")
    if any(s == "" for s in segs):
        return "empty segment in the attribute (two dots in a row, or a dot at either end)"
    schema: dict = core._DATASET
    prefix = core.PREFIX
    while segs:
        name, rest = segs[0], segs[1:]
        spec = schema.get(name)
        if spec is None or spec[0] == "S":
            return None
        if spec[0] == "G":
            if not rest:
                return None
            schema, segs, prefix = spec[1], rest, f"{prefix}{name}."
            continue
        idx = 1
        if rest and _INDEX_LIKE_RE.fullmatch(rest[0]):
            if stored and not _CANONICAL_INDEX_RE.fullmatch(rest[0]):
                if not rest[0].isdigit() or int(rest[0]) == 0:
                    return None
            elif not _CANONICAL_INDEX_RE.fullmatch(rest[0]):
                return (f"index {rest[0]!r} is not canonical: indexes are 1, 2, 3, ... "
                        "without leading zeros or signs (rule 5)")
            idx, rest = int(rest[0]), rest[1:]
        if spec[0] == "L":
            if rest:  # croissant.license.1.x: an unknown key
                return None
            lists.setdefault(prefix + name, {}).setdefault(idx, []).append(attr)
            return None
        if not rest:  # an entity without a sub-key: an unknown key
            return None
        lists.setdefault(prefix + name, {}).setdefault(idx, []).append(attr)
        schema, segs, prefix = spec[1], rest, f"{prefix}{name}.{idx}."
    return None


def _key_errors(
    avus: list[dict[str, str]], before: list[str] | None = None,
    after: list[str] | None = None,
) -> tuple[list[dict[str, str]], set[str]]:
    """The new set's non-canonical keys (convention rules 5 and 6) as errors.

    Refused: an empty segment; an index that is not ``1``, ``2``, ... (``0``, ``01``,
    ``007``, ``-1``); a list entry that would leave a gap (``keyword.1`` and ``keyword.3``);
    two or more values for one single-valued path or one index (``croissant.name`` twice,
    ``croissant.license`` next to ``croissant.license.1``). Unknown keys are not errors
    (rule 7). Returns ``(errors, offending attributes)``; an error's ``path`` is the
    attribute, the canonical path or the list prefix, and its message names the attributes.

    Gaps (merge): ``before`` holds the attributes of the item's ``croissant.*`` AVUs before
    the write and ``after`` those that stay after it (the diff's ``unchanged``). The check
    then runs on the result, ``after`` + the new set, so a partial update (``creator.2``
    alone, ``recordSet.2`` from ``from_schema(record_set_index=2)``, ``...field.3``) passes
    when the item keeps the entries before it. A new-set entry at index ``n`` is refused
    when the result lacks an index ``i < n`` and the write makes that gap: ``n`` is a new
    index, or the item had ``i``. An entry the item already has may still be edited after a
    gap the item already had. Stored keys are read the way readers place them. Without
    ``before`` / ``after`` (replace) the new set is the result. Every other check looks at
    the new set alone. Documents imported by :func:`mesa_croissant.core.from_jsonld` never
    trip these checks: ``model_to_avus`` writes canonical keys.
    """
    errors: list[dict[str, str]] = []
    bad: set[str] = set()
    lists: dict[str, dict[int, list[str]]] = {}
    paths: dict[str, list[str]] = {}

    def stored(attrs: list[str] | None) -> dict[str, dict[int, list[str]]]:
        out: dict[str, dict[int, list[str]]] = {}
        for attr in attrs or []:
            # Read as readers read them; their own problems are not errors of this call.
            if core.is_croissant_attr(attr.strip()):
                _index_problem(attr.strip(), out, stored=True)
        return out

    had, kept = stored(before), stored(after)
    for a in avus:
        attr = a["attribute"]
        problem = _index_problem(attr, lists)
        if problem:
            errors.append(_msg(attr, problem))
            bad.add(attr)
            continue
        info = _canon(attr)
        if info is not None:
            paths.setdefault(info.path, []).append(attr)
    for path, attrs in paths.items():
        if len(attrs) < 2:
            continue
        head, _, last = path.rpartition(".")
        where = (f"index {last} of {head}" if _INDEX_SEG_RE.fullmatch(last)
                 else "a single-valued path")
        errors.append(_msg(path, f"{len(attrs)} values for {where} (rule 6: one value per "
                                 f"path): {_named(attrs)}"))
        bad.update(attrs)
    for prefix, indexes in lists.items():
        result = set(kept.get(prefix, ())) | set(indexes)
        old = set(had.get(prefix, ()))
        # Gaps this write makes: an index the item had and loses, below a new-set index;
        # or the first missing index, below a new-set index the item did not have. (Never
        # a scan up to the largest index: an index can be any number.)
        made = [i for i in old - result if i < max(indexes)]
        first = next(i for i in range(1, len(result) + 2) if i not in result)
        if any(n > first and n not in old for n in indexes):
            made.append(first)
        if not made:
            continue
        missing = min(made)
        after_gap = [x for n in sorted(indexes) if n > missing and (missing in old or n not in old)
                     for x in indexes[n]]
        where = " of the result (with the item's entries that stay)" if after is not None else ""
        errors.append(_msg(prefix, f"indexes of {prefix} leave a gap at {missing}{where} "
                                   f"(rule 5: writers number from 1 without gaps): "
                                   f"{_named(after_gap)}"))
        bad.update(after_gap)
    return errors, bad


_TOO_DEEP = f"the document is nested more than {core.MAX_JSON_DEPTH} levels deep"


def _document_arg(document: dict[str, Any] | str) -> dict[str, Any] | str:
    """A ``document`` argument, parsed; ``invalid_argument`` when it is nested too deeply.

    JSON text is parsed here (text that is not JSON is returned as it is, for core to
    report), so a deeply nested document is refused before anything walks it: Python's
    JSON parser raises RecursionError at about 1,000 levels, and core's walk is recursive.
    """
    doc: Any = document
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except RecursionError:
            raise ToolError(code="invalid_argument", message=_TOO_DEEP,
                            details={"max_depth": core.MAX_JSON_DEPTH}) from None
        except ValueError:
            return document
    if core._too_deep(doc):
        raise ToolError(code="invalid_argument", message=_TOO_DEEP,
                        details={"max_depth": core.MAX_JSON_DEPTH})
    return doc


def _limit_errors(avus: list[dict[str, str]]) -> list[dict[str, str]]:
    errors = []
    for a in avus:
        if len(a["value"]) > core.VALUE_MAX:
            errors.append(_msg(a["attribute"], f"value has {len(a['value'])} characters; "
                                                    f"the limit is {core.VALUE_MAX}"))
        if len(a["unit"]) > core.UNIT_MAX:
            errors.append(_msg(a["attribute"], f"unit has {len(a['unit'])} characters; "
                                                    f"the limit is {core.UNIT_MAX}"))
        if "\x00" in a["value"] or "\x00" in a["unit"]:
            errors.append(_msg(a["attribute"], "NUL characters cannot be stored"))
    return errors


#: Characters of a refused (over-limit) value that a result still shows.
_CLIP_SHOWN = 200


def _clip_refused(avus: list[dict[str, str]]) -> list[dict[str, str]]:
    """``avus`` with values over ``VALUE_MAX`` and units over ``UNIT_MAX`` clipped.

    Such a value is always an import error (nothing is written), so the diff of a refused
    dry run shows its first ``_CLIP_SHOWN`` characters and its length instead of echoing it
    whole (a 1.2-million-character description made a 2.4 MB result). Other AVUs are
    returned as they are.
    """
    def clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return (f"{text[:_CLIP_SHOWN]}... [clipped: {len(text)} characters, over the "
                f"{limit}-character limit]")

    return [a if len(a["value"]) <= core.VALUE_MAX and len(a["unit"]) <= core.UNIT_MAX
            else {**a, "value": clip(a["value"], core.VALUE_MAX),
                  "unit": clip(a["unit"], core.UNIT_MAX)}
            for a in avus]


class _Canon(NamedTuple):
    """Where an attribute sits in the convention's key schema."""

    path: str  #: canonical attribute: indexes added where the convention allows none
    entries: tuple[tuple[str, str, str], ...]  #: (kind E|L, list prefix, entry prefix)
    owner: str | None  #: deepest entity prefix (croissant.recordSet.1.field.2), None = dataset


def _canon(attr: str) -> _Canon | None:
    """Canonical form of a known ``croissant.*`` attribute; None for unknown / index-0 keys.

    A repeatable leaf or an entity without an index means index 1 (convention rule 5), so
    ``croissant.license`` becomes ``croissant.license.1`` and a field's ``dataType`` becomes
    ``dataType.1``; ``01`` becomes ``1``. Mirrors :func:`mesa_croissant.core._place`.
    """
    if not core.is_croissant_attr(attr):
        return None
    segs = attr[len(core.PREFIX):].split(".")
    if any(s == "" for s in segs):
        return None
    entries: list[tuple[str, str, str]] = []
    owner: list[str] = []
    schema: dict = core._DATASET
    prefix = core.PREFIX
    while True:
        name, rest = segs[0], segs[1:]
        spec = schema.get(name)
        if spec is None:
            return None
        kind = spec[0]
        if kind == "S":
            return None if rest else _Canon(prefix + name, tuple(entries),
                                            owner[-1] if owner else None)
        if kind == "L":
            if not rest:
                idx = 1
            elif len(rest) == 1 and _INDEX_SEG_RE.fullmatch(rest[0]):
                idx = int(rest[0])
            else:
                return None
            if idx == 0:
                return None
            entry = f"{prefix}{name}.{idx}"
            entries.append(("L", prefix + name, entry))
            return _Canon(entry, tuple(entries), owner[-1] if owner else None)
        if kind == "E":
            idx = 1
            if rest and _INDEX_SEG_RE.fullmatch(rest[0]):
                idx = int(rest[0])
                rest = rest[1:]
            if idx == 0 or not rest:
                return None
            entry = f"{prefix}{name}.{idx}"
            entries.append(("E", prefix + name, entry))
            owner.append(entry)
            schema, segs, prefix = spec[1], rest, entry + "."
            continue
        if not rest:  # group
            return None
        schema, segs, prefix = spec[1], rest, f"{prefix}{name}."


def _entity_chain(attr: str) -> tuple[tuple[str, str], ...]:
    """The entities a ``croissant.*`` attribute is below: ``(list prefix, entry prefix)`` pairs.

    Walks the key schema as far as the attribute reaches it, the way
    :func:`mesa_croissant.core._place` binds an unknown key to the deepest entity it
    reaches (``croissant.recordSet.1.field.03.transform`` and
    ``croissant.recordSet.field.3.transform`` both end at
    ``croissant.recordSet.1.field.3``). Entry prefixes have the canonical form
    :func:`_canon` gives. An index-0 entry or an empty segment ends the walk too, so such a
    key belongs to the entity above it.
    """
    if not core.is_croissant_attr(attr):
        return ()
    segs = attr[len(core.PREFIX):].split(".")
    chain: list[tuple[str, str]] = []
    schema: dict = core._DATASET
    prefix = core.PREFIX
    while segs:
        name, rest = segs[0], segs[1:]
        spec = schema.get(name)
        if spec is None or spec[0] in ("S", "L") or not rest:
            break
        if spec[0] == "E":
            idx = 1
            if _INDEX_SEG_RE.fullmatch(rest[0]):
                idx = int(rest[0])
                rest = rest[1:]
            if idx == 0 or not rest:
                break
            entry = f"{prefix}{name}.{idx}"
            chain.append((prefix + name, entry))
            schema, segs, prefix = spec[1], rest, entry + "."
            continue
        schema, segs, prefix = spec[1], rest, f"{prefix}{name}."  # group
    return tuple(chain)


def _path_of(attr: str) -> str:
    """The canonical attribute, or the attribute itself for keys outside the convention."""
    c = _canon(attr)
    return c.path if c else attr


def _key(a: dict[str, str]) -> tuple[str, str, str]:
    """A triple as the iRODS helper writes it (stripped)."""
    return (a["attribute"].strip(), a["value"].strip(), (a.get("unit") or "").strip())


def compute_diff(
    current: list[dict[str, str]], new: list[dict[str, str]], mode: str,
    drop_unknown: bool = False,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Diff the item's ``croissant.*`` AVUs against the new set.

    Returns ``(add, remove, unchanged, warnings)``. ``unchanged`` lists every current AVU that
    stays (as stored), so after a successful write the item's ``croissant.*`` AVUs are
    ``unchanged + add``; ``remove`` holds the exact stored triples.

    Triples are compared the way iRODS stores what the tool writes (stripped): a current
    AVU that differs from a new one only by surrounding whitespace is unchanged, and of
    several current AVUs matching one new triple only one stays.

    ``merge`` works on canonical paths (``croissant.license`` = ``croissant.license.1``)
    and per entity. A path of the new set replaces every current value of that path. An
    entity (``creator.N``, ``distribution.N``, ``recordSet.N``, ``...field.M``,
    ``...subField.K``) that the new set gives a property of replaces the current entity of
    that index: its other properties are removed, and so are its nested entries of a list
    the new set also gives (``recordSet.1.field.3`` when the new ``recordSet.1`` has fields
    1-2); such a removed entry loses its unknown keys too (convention rule 7). Everything
    else stays; a kept entry of a list the new set gives, or a kept nested entry of a
    replaced entity, is named in a warning.

    ``replace`` makes the item's ``croissant.*`` AVUs the new set plus the current unknown
    keys and index-0 entries (convention rule 7: editors keep them; a document cannot carry
    them), unless ``drop_unknown``. An unknown attribute the new set gives is replaced.

    In both modes an unknown key (or index-0 entry) belongs to the deepest entity its
    attribute reaches (:func:`_entity_chain`, as the codec binds it). When the diff removes
    that entity, or one above it, because of its known keys, the unknown key is removed with
    it, so no entity of unknown keys only is left behind. An entity that already holds only
    unknown keys is not removed (a document cannot carry it).
    """
    cur = [c for c in current if core.is_croissant_attr(c["attribute"])]
    new_keys = {_key(a) for a in new}

    # Each new triple claims one current AVU: an exact match first, then a stripped one.
    claimed: dict[tuple[str, str, str], int] = {}
    for i, c in enumerate(cur):
        t = _triple(c)
        if t in new_keys and t not in claimed:
            claimed[t] = i
    for i, c in enumerate(cur):
        k = _key(c)
        if k in new_keys and k not in claimed and i not in claimed.values():
            claimed[k] = i
    matched = set(claimed.values())

    keep: list[dict[str, str]] = []
    remove: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    warned: set[str] = set()

    def warn(path: str, message: str) -> None:
        if path not in warned:
            warned.add(path)
            warnings.append(_msg(path, message))

    new_attrs = {a["attribute"].strip() for a in new}
    new_paths: set[str] = set()
    new_entries: set[str] = set()
    new_lists: set[str] = set()
    touched: set[str] = set()
    for a in new:
        info = _canon(a["attribute"].strip())
        if info is None:
            new_paths.add(a["attribute"].strip())
            continue
        new_paths.add(info.path)
        for _kind, lp, ep in info.entries:
            new_lists.add(lp)
            new_entries.add(ep)
        if info.owner:
            touched.add(info.owner)

    def removed_owner(attr: str, removed: dict[str, str]) -> str | None:
        """The removed entity (this key's owner or one above it) an unknown key goes with."""
        return next((ep for _lp, ep in _entity_chain(attr) if ep in removed), None)

    if mode == "replace":
        # Entities of the current set (by their known keys) that the new set does not give.
        gone: dict[str, str] = {}
        for c in cur:
            info = _canon(c["attribute"])
            for kind, lp, ep in info.entries if info else ():
                if kind == "E" and ep not in new_entries:
                    gone.setdefault(ep, lp)
        with_entity: set[str] = set()
        for i, c in enumerate(cur):
            if i in matched:
                keep.append(c)
            elif (not drop_unknown and _canon(c["attribute"]) is None
                  and c["value"].strip() and _key(c) not in new_keys
                  and c["attribute"].strip() not in new_attrs):
                ent = removed_owner(c["attribute"], gone)
                if ent:
                    remove.append(c)
                    with_entity.add(ent)
                    continue
                keep.append(c)
                warn(c["attribute"], "unknown Croissant key (or index 0) kept: replace keeps "
                                     "keys outside the convention unless drop_unknown=true")
            else:
                remove.append(c)
        for ent in sorted(with_entity):
            warn(ent, "replace removes this entry (the new set does not give it), including "
                      "its unknown keys (convention rule 7)")
    else:
        order = {id(c): i for i, c in enumerate(cur)}
        replaced: set[str] = set()
        removed_entries: dict[str, str] = {}  # entry prefix -> its list prefix
        deferred: list[dict[str, str]] = []  # unknown keys: decided once every entry is known
        for i, c in enumerate(cur):
            if i in matched:
                keep.append(c)
                continue
            if _key(c) in new_keys:  # a second copy of a value the new set keeps
                remove.append(c)
                continue
            info = _canon(c["attribute"].strip())
            if info is None:
                if c["attribute"].strip() in new_paths:
                    remove.append(c)
                else:  # its entity may be removed by a known key later in the list
                    deferred.append(c)
                continue
            if info.path in new_paths:
                remove.append(c)
                continue
            if info.owner in touched:
                remove.append(c)
                replaced.add(info.owner)
                continue
            # A nested entry of a list the new set gives, inside an entity it replaces.
            leftover, parent = None, None
            for kind, lp, ep in info.entries:
                if kind != "E":
                    continue
                if parent in touched and lp in new_lists and ep not in new_entries:
                    leftover = (lp, ep)
                    break
                parent = ep
            if leftover:
                remove.append(c)
                removed_entries.setdefault(leftover[1], leftover[0])
                continue
            keep.append(c)
            stale = next(((lp, ep) for _k, lp, ep in info.entries
                          if lp in new_lists and ep not in new_entries), None)
            if stale:
                warn(stale[1], f"merge keeps this entry of {stale[0]} although the new set has "
                               "other entries; use mode=replace to make the item match the "
                               "new set exactly")
                continue
            ents = [ep for kind, _lp, ep in info.entries if kind == "E"]
            nested = next((ents[j + 1] for j in range(len(ents) - 1) if ents[j] in touched),
                          None)
            if nested:
                warn(nested, f"merge keeps this nested entry of {nested.rsplit('.', 2)[0]}, "
                             "which the new set replaces; use mode=replace, or give the "
                             "nested entries too")
        for c in deferred:
            # Convention rule 7: removing an entity removes its unknown keys too.
            (remove if removed_owner(c["attribute"].strip(), removed_entries)
             else keep).append(c)
        keep.sort(key=lambda c: order[id(c)])
        remove.sort(key=lambda c: order[id(c)])
        for ent in sorted(replaced):
            warn(ent, "merge replaces this entry as a whole: its current properties the new "
                      "set does not give are removed")
        for ent in sorted(removed_entries):
            warn(ent, f"merge removes this entry (the new set gives other entries of "
                      f"{removed_entries[ent]}), including its unknown keys")

    have = set(claimed)
    add = [a for a in new if _key(a) not in have]
    return add, remove, keep, warnings


# Generated properties the crosswalk or the item supplies, and the croissant.* stem that would
# pin each one (convention section 7: writers never copy them). The MESA Portal's importer
# (portal/services/croissant_service.py) leaves out the same values, so both agree.
CROSSWALK_STEMS = {
    "name": "croissant.name",
    "description": "croissant.description",
    "license": "croissant.license",
    "creator": "croissant.creator",
    "publisher": "croissant.publisher",
    "datePublished": "croissant.datePublished",
    "version": "croissant.version",
    "keywords": "croissant.keyword",
    "temporalCoverage": "croissant.temporalCoverage",
    "spatialCoverage": "croissant.geo.spatialCoverage",
    "prov:wasGeneratedBy": "croissant.wasGeneratedBy",
    "url": "croissant.url",
    "identifier": "croissant.identifier",
}
#: croissant.* AVUs of one property compared with its default (bounds the cost).
MAX_COMPARED = 200

_MSG_DEFAULT_URL = "the default landing page URL is generated, not stored"
_MSG_DOI_ID = "the DOI URL is taken from the item's datacite.identifier, not stored"


def _has_stem(attribute: str, stem: str) -> bool:
    return attribute == stem or attribute.startswith(stem + ".")


def _doi_url(others: list[dict[str, str]]) -> str | None:
    """The item's DOI URL, from its datacite.identifier AVU (the crosswalk's rule)."""
    hit = core.crosswalk(others).get("identifier")
    value = hit["value"] if hit else None
    return value if isinstance(value, str) and value.startswith("https://doi.org/") else None


def _same_default(prop: str, others: list[dict[str, str]], subset: list[dict[str, str]],
                  item: dict[str, Any], default: Any) -> bool:
    """True when ``subset`` (one property's ``croissant.*`` AVUs) generates ``default``.

    Keywords without units (a document exported with ``keyword_style=text``) also match
    when they give the same plain names: the default keeps the term codes they lost.
    """
    if core.to_jsonld(others + subset, item=item)["document"].get(prop) == default:
        return True
    if prop != "keywords" or any(a.get("unit") for a in subset):
        return False
    text_item = {**item, "keyword_style": "text"}
    return (core.to_jsonld(others + subset, item=text_item)["document"].get(prop)
            == core.to_jsonld(others, item=text_item)["document"].get(prop))


def drop_generated(
    avus: list[dict[str, str]], item: dict[str, Any], current: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Imported ``croissant.*`` AVUs without the values the generator makes.

    Returns ``(kept, warnings, stems)``; ``stems`` are the ``croissant.*`` stems left out.
    A document generated for the item carries its defaults (the landing page or DOI URL
    as ``url``, the DOI URL as ``identifier``, the item name, and whatever the crosswalk
    takes from ``datacite.*``, ``dc.*`` or ontology AVUs). Storing them would pin them: a
    later DataCite edit or DOI would no longer reach the document. So:

    * ``croissant.url`` equal to the default landing page (or ``item["url"]``) or to the
      DOI URL, and ``croissant.identifier`` equal to the DOI URL, are always left out;
    * a property whose ``croissant.*`` AVUs generate exactly the value the item gets
      without them (from ``item`` and the non-Croissant AVUs of ``current``) is left out,
      with a warning naming the source.

    The same rule as the MESA Portal's ``croissant_service.drop_generated``.
    """
    others = [a for a in current if not core.is_croissant_attr(a["attribute"])]
    pages = {core._default_page(item)} | ({str(item["url"])} if item.get("url") else set())
    doi_url = _doi_url(others)
    warnings: list[dict[str, str]] = []
    stems: list[str] = []
    kept: list[dict[str, str]] = []
    dropped_url = dropped_id = False
    for a in avus:
        attr, value = a["attribute"], a["value"]
        if attr == "croissant.url" and (value in pages or (doi_url and value == doi_url)):
            dropped_url = True
            continue
        if attr == "croissant.identifier" and doi_url and value == doi_url:
            dropped_id = True
            continue
        kept.append(a)
    if dropped_url:
        warnings.append(_msg("croissant.url", _MSG_DEFAULT_URL))
        stems.append("croissant.url")
    if dropped_id:
        warnings.append(_msg("croissant.identifier", _MSG_DOI_ID))
        stems.append("croissant.identifier")

    base = core.to_jsonld(others, item=item)
    for prop, stem in CROSSWALK_STEMS.items():
        source = base["sources"].get(prop)
        if prop not in base["document"] or source in (None, "croissant", "auto"):
            continue
        subset = [a for a in kept if _has_stem(a["attribute"], stem)]
        if not subset or len(subset) > MAX_COMPARED:
            continue
        if not _same_default(prop, others, subset, item, base["document"][prop]):
            continue
        kept = [a for a in kept if not _has_stem(a["attribute"], stem)]
        warnings.append(_msg(stem, f"the same as the value taken from {source}; not stored, "
                                   "so later changes there still reach the document"))
        stems.append(stem)
    return kept, warnings, stems


@register_tool(
    APPLY,
    "Write Croissant metadata to a Data Store folder or file as croissant.* AVUs: imports a "
    "Croissant JSON-LD document (or takes croissant.* AVUs), diffs them against the item's "
    "croissant.* AVUs (merge: per path and per entity; replace: the new set plus unknown "
    "croissant.* keys; a document's generated defaults are not stored) and, unless "
    "dry_run (default true), writes the diff and mirrors it into the MESA DuckLake AVU "
    "history as one snapshot. Returns the diff {add, remove, unchanged} and the validation of "
    "the resulting document.",
    input_model=ApplyInput,
    output_model=ApplyOutput,
    meta=META,
)
async def handle_apply(
    args: ApplyInput, *, auth_value: AuthValue | None = None
) -> dict[str, Any]:
    auth = _require_auth(auth_value, APPLY)
    if (args.document is None) == (args.avus is None):
        raise ToolError(code="invalid_argument",
                        message="Give exactly one of 'document' or 'avus'.")
    if not args.dry_run and auth.is_anonymous():
        raise ToolError(code="forbidden", message="Anonymous callers cannot write AVUs.")
    document = _document_arg(args.document) if args.document is not None else None

    norm, session, target = _open_path(args.path, auth)
    item = _item_for(session, norm, target, None, "defined_term")
    all_current = _clean_avus(list_avus_for_path(session, norm, target))
    current = [a for a in all_current if core.is_croissant_attr(a["attribute"])]

    warnings: list[dict[str, str]] = []
    import_errors: list[dict[str, str]] = []
    generated: list[str] = []
    if document is not None:
        # Item mode: for a file, the FileObject that describes it becomes croissant.file.*
        # (its generated parts and a checksum equal to the stat md5 left out).
        imported = core.from_jsonld(document, content_base=args.content_base or None,
                                    item=item)
        new, caller_errors, trimmed = _check_new_avus(
            [AvuItem(**a) for a in imported["avus"]])
        warnings += imported["warnings"] + trimmed
        # A document exported for this path carries the generated defaults (landing page,
        # DOI, crosswalk values); storing them would pin them.
        new, dropped, generated = drop_generated(new, item, all_current)
        warnings += dropped
        import_errors += imported["errors"] + caller_errors
        codec_warnings: list[dict[str, str]] = []
    else:
        new, caller_errors, trimmed = _check_new_avus(list(args.avus or []))
        import_errors += caller_errors + _limit_errors(new)
        warnings += trimmed
        codec_warnings = core.parse_avus(new)["warnings"]
    codec_at = len(warnings)
    if target == "data_object" and any(a["attribute"].startswith("croissant.distribution.")
                                       for a in new):
        warnings.append(_msg(
            "croissant.distribution",
            "a document generated for a file ignores croissant.distribution.*; the file is "
            "its only FileObject (describe it with croissant.file.*)",
        ))
    if len(new) > MAX_AVUS:
        # Refused before the diff: a dry run would otherwise return the whole diff and the
        # validation of every entry (megabytes for a set nobody can write).
        raise ToolError(
            code="invalid_argument",
            message=f"{len(new)} croissant.* AVUs; at most {MAX_AVUS} per call.",
            details={"count": len(new), "limit": MAX_AVUS},
        )

    add, remove, unchanged, diff_warnings = compute_diff(current, new, args.mode,
                                                         drop_unknown=args.drop_unknown)
    warnings += diff_warnings
    if args.mode == "merge" and generated:
        # The document gives the generated value of these properties: a current pinned
        # value would hide it, so merge removes it too (replace already does).
        stale = [c for c in unchanged if _canon(c["attribute"].strip()) is not None
                 and any(_has_stem(c["attribute"].strip(), s) for s in generated)]
        if stale:
            unchanged = [c for c in unchanged if not any(c is x for x in stale)]
            remove += stale
            for stem in sorted({s for s in generated
                                for c in stale if _has_stem(c["attribute"].strip(), s)}):
                warnings.append(_msg(stem, "merge removes the current value: the document "
                                           "gives the generated one, which is not stored"))
    if args.mode == "replace" and not new and current:
        warnings.append(_msg("", "replace with an empty set removes every croissant.* AVU "
                                 "of the convention"))
    # Rules 5 and 6 (from_jsonld writes canonical keys; checked anyway, so both paths refuse
    # the same). Merge checks gaps on the result: the new set plus the entries that stay.
    key_errors, bad = _key_errors(new) if args.mode == "replace" else _key_errors(
        new, before=[c["attribute"] for c in current],
        after=[c["attribute"] for c in unchanged])
    import_errors += key_errors
    # The codec's warnings about the refused keys would only repeat the errors.
    warnings[codec_at:codec_at] = [w for w in codec_warnings if w["path"] not in bad]

    # Validation of the document the item would generate after the write.
    others = [a for a in all_current if not core.is_croissant_attr(a["attribute"])]
    preview = core.to_jsonld(others + unchanged + add, item=item)

    result: dict[str, Any] = {
        "path": norm,
        "path_target_type": target,
        "mode": args.mode,
        "dry_run": args.dry_run,
        # A refused value is shown clipped: a dry run would otherwise echo it whole.
        "diff": {"add": _clip_refused(add), "remove": remove, "unchanged": unchanged},
        "counts": {"add": len(add), "remove": len(remove), "unchanged": len(unchanged),
                   "current": len(current), "new": len(new)},
        "written": {"added": 0, "removed": 0},
        "errors": [],
        "kept": [],
        "warnings": warnings,
        "import_errors": import_errors,
        "validation": preview["validation"],
    }
    if args.dry_run:
        return result
    if import_errors:
        raise ToolError(
            code="croissant_invalid",
            message="The new Croissant AVUs cannot be written; nothing was changed.",
            details={"errors": import_errors, "warnings": warnings},
        )

    changes: list[tuple[str, str, str, Literal["add", "delete"]]] = []
    errors: list[dict[str, str]] = []
    failed_paths: set[str] = set()
    # Add first: if an add fails, the old value of that (canonical) path is kept below.
    for a in add:
        try:
            w = add_avu_to_irods(session, norm, target, a)
        except ToolError as exc:
            errors.append({"op": "add", "attribute": a["attribute"], "error": str(exc)})
            failed_paths.add(_path_of(a["attribute"]))
            continue
        result["written"]["added"] += 1
        changes.append((w["attribute"], w["value"], w["unit"], "add"))
    kept: list[dict[str, str]] = []
    for r in remove:
        if _path_of(r["attribute"]) in failed_paths:
            kept.append(r)
            continue
        try:
            d = _remove_exact(session, norm, target, r)
        except ToolError as exc:
            errors.append({"op": "remove", "attribute": r["attribute"], "error": str(exc)})
            continue
        result["written"]["removed"] += 1
        changes.append((d["attribute"], d["value"], d["unit"], "delete"))
    result["errors"] = errors
    result["kept"] = kept

    if changes:
        try:
            await record_avu_changes(
                auth_value=auth,
                irods_path=norm,
                target_type=target,  # type: ignore[arg-type]
                changes=changes,
                tool_name=APPLY,
                session=session,
            )
        except DuckLakeMirrorError as exc:
            result["partial_failure"] = {
                "code": "ducklake_mirror_failed",
                "message": str(exc),
                "project_id": str(exc.project_id) if exc.project_id else None,
            }
    return result


# -- mesa_croissant_from_schema --------------------------------------------------------------------
class ColumnIn(BaseModel):
    """A column of a DuckDB / Arrow schema (DESCRIBE or information_schema.columns rows work)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., validation_alias=AliasChoices("name", "column_name"))
    type: str = Field(
        ..., validation_alias=AliasChoices("type", "column_type", "data_type", "dtype")
    )
    description: str | None = None


class FromSchemaInput(BaseModel):
    columns: list[ColumnIn] = Field(..., description="Columns as {name, type} (DuckDB or Arrow).")
    record_set_id: str = Field(..., description="@id of the record set (e.g. the table name).")
    source_id: str = Field(
        ...,
        description="@id of the distribution entry (FileObject / FileSet) the columns come from.",
    )
    record_set_index: int = Field(
        default=1, ge=1, description="N of croissant.recordSet.N.* (1-based)."
    )
    record_set_name: str | None = None
    description: str | None = None


class FromSchemaOutput(BaseModel):
    record_set_id: str
    avus: list[AvuItem]
    model: dict[str, Any]
    warnings: list[Message]
    next_step: str


_PLAIN_MEMBER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _member_json_path(name: str) -> str:
    """The JSONPath of a STRUCT member: the bare name, else ``$['...']`` (quotes escaped).

    The same rule as the MESA Portal DuckDB viewer's ``dvCrJsonPath``, so both write the
    same AVUs for one schema.
    """
    if _PLAIN_MEMBER_RE.fullmatch(name) and name != "where":
        return name
    return "$['" + name.replace("\\", "\\\\").replace("'", "\\'") + "']"


def _field_model(name: str, typed: dict[str, Any], *, source: str | None, column: str | None,
                 path: str, warnings: list[dict[str, str]], depth: int = 0) -> dict[str, Any]:
    """The model of one column (``depth`` 0) or STRUCT member (``depth`` 1), without ``id``.

    ``name`` is the column or member name exactly as the schema gives it: the field's
    ``name`` is it trimmed, while ``column`` (passed by the caller) and a member's
    ``transformJsonPath`` keep it exact, since they must match the data. Field ids are
    assigned afterwards for the whole record set (:func:`_assign_ids`).

    A member reads its value from the parent's column with ``transformJsonPath`` = its
    name (:func:`_member_json_path`): it inherits the parent's source and column
    (convention section 6), and mlcroissant needs the transform to load a subField of a
    STRUCT column. A member that is itself a STRUCT is typed ``sc:Text`` and gets no
    transform (convention section 9, STRUCT row; the portal DuckDB viewer writes the same):
    with it mlcroissant 1.1 fails the whole record set casting the object to text, without
    it the member reads as null and core only warns. A MAP entry's ``key`` / ``value`` read
    ``$[0]`` / ``$[1]`` (``typed["transformJsonPath"]``), and every type of ``dataTypes``
    is written (GEOMETRY: ``sc:GeoShape`` and ``sc:Text``). A column typed ``STRUCT()``
    (no members; DuckDB never reports one) is typed ``sc:Text``, with a warning.
    """
    f: dict[str, Any] = {"name": name.strip() or name}
    if f["name"] != name:
        f[_TRIMMED_MARK] = True  # read and removed by _assign_ids
    types = typed.get("dataTypes") or ([typed["dataType"]] if typed.get("dataType") else [])
    subs = typed.get("subFields")
    if subs is not None and not subs and depth == 0:
        warnings.append(_msg(path, "STRUCT without members (a malformed type): typed sc:Text"))
        types, subs = ["sc:Text"], None
    if types:
        f["dataType"] = list(types)
    if source:
        f["source"] = source
    if column:
        f["column"] = column
    if depth >= 1 and subs is None:
        # A MAP entry's key / value come with their positional path ($[0] / $[1]).
        f["transformJsonPath"] = typed.get("transformJsonPath") or _member_json_path(name)
    if typed.get("isArray"):
        f["isArray"] = "true"
        if typed.get("arrayShape"):
            f["arrayShape"] = typed["arrayShape"]
    if subs is not None:
        if depth >= 1:
            # MESA tools write one level of nesting (convention section 6).
            f.pop("isArray", None)
            f.pop("arrayShape", None)
            f["dataType"] = ["sc:Text"]
            warnings.append(_msg(path, "nested STRUCT below the first level is typed sc:Text"))
        else:
            if "," in (typed.get("arrayShape") or ""):
                warnings.append(_msg(path, "mlcroissant 1.1.1 cannot read subFields under a "
                                           f"list of lists (arrayShape {typed['arrayShape']}): "
                                           "loading this record set fails"))
            f["subField"] = [
                _field_model(str(s["name"]), s, source=None, column=None,
                             path=f"{path}.subField.{k}", warnings=warnings, depth=depth + 1)
                for k, s in enumerate(subs, 1)
            ]
    return f


_TRIMMED_MARK = "_trimmed"
_REAL_MARK = "_real"


def _assign_ids(fields: list[dict[str, Any]], rs_id: str) -> None:
    """Give fields whose default ``@id`` would not do an explicit, unique ``id``.

    A field's default ``@id`` is ``<parent @id>/<name>`` (core, convention section 6), the
    parent's explicit id included. A field is *real* when its name is the column or member
    name as given: no whitespace inside and none trimmed (``_field_model`` marks a trimmed
    name). A field that is not real gets its default with each run of whitespace replaced
    by ``_`` (a trimmed name: the default itself), or, when that id is taken or belongs to
    a real field, the first free ``-2``, ``-3``, ... suffix. So:

    * every real field whose parents are real too keeps its default id: those ids are
      reserved before anything is assigned (``a b`` next to a column ``a_b``, ``a `` next
      to ``a``, a member ``a`` of ``s t`` next to a column ``s_t/a``);
    * the real members of a renamed STRUCT keep ``<its new id>/<name>`` unless a real field
      of the line above already holds it: fields are assigned level by level, the real
      ones of a level first;
    * two real fields with the same default (a column ``s/a`` and a member ``a`` of ``s``):
      the first in order keeps it.

    Deterministic: the result depends only on the names and their order.
    """
    reserved: set[str] = set()

    def mark(fs: list[dict[str, Any]], parent: str | None) -> None:
        for f in fs:
            f[_REAL_MARK] = not f.pop(_TRIMMED_MARK, False) and not _WS_RE.search(f["name"])
            chain = f"{parent}/{f['name']}" if parent is not None and f[_REAL_MARK] else None
            if chain is not None:
                reserved.add(chain)
            mark(f.get("subField") or [], chain)

    mark(fields, rs_id)
    used: set[str] = set()
    level = [(fields, i, rs_id, True) for i in range(len(fields))]
    while level:
        eff: dict[tuple[int, int], str] = {}
        for fs, i, parent, chain in level:  # real fields claim their defaults first
            default = f"{parent}/{fs[i]['name']}"
            if fs[i][_REAL_MARK] and default not in used \
                    and (default not in reserved or chain):
                eff[(id(fs), i)] = default
                used.add(default)
        for fs, i, parent, _chain in level:
            if (id(fs), i) in eff:
                continue
            base = out = _WS_RE.sub("_", f"{parent}/{fs[i]['name']}")
            n = 2
            while out in used or out in reserved:
                out = f"{base}-{n}"
                n += 1
            eff[(id(fs), i)] = out
            used.add(out)
        below = []
        for fs, i, parent, chain in level:
            f, out = fs[i], eff[(id(fs), i)]
            keeps = f.pop(_REAL_MARK) and out == f"{parent}/{f['name']}"
            if out != f"{parent}/{f['name']}":
                fs[i] = f = {"name": f["name"], "id": out,
                             **{k: v for k, v in f.items() if k not in ("name", "id")}}
            subs = f.get("subField") or []
            below += [(subs, k, out, chain and keeps) for k in range(len(subs))]
        level = below


@register_tool(
    FROM_SCHEMA,
    "Build Croissant record-set AVUs (croissant.recordSet.N.*) from a DuckDB or Arrow column "
    "schema: one field per column with source, column and dataType from the DuckDB type map "
    "(lists become isArray, STRUCTs become subFields read with transformJsonPath = the "
    "member name, quoted as $['...'] unless a plain identifier; a STRUCT member that is "
    "itself a STRUCT is typed sc:Text without a transform; a MAP becomes an isArray field "
    "with subFields key and value read with $[0] / $[1]; GEOMETRY (WKB) is typed "
    "[sc:GeoShape, sc:Text]). column keeps the exact column name; field names are trimmed, "
    "and a name with whitespace gets an explicit id (whitespace runs become _, made unique). "
    f"At most {MAX_COLUMNS} columns and {MAX_AVUS} AVUs (one apply call). Writes nothing; "
    "pass the AVUs to mesa_croissant_apply.",
    input_model=FromSchemaInput,
    output_model=FromSchemaOutput,
    meta=META,
)
async def handle_from_schema(args: FromSchemaInput) -> dict[str, Any]:
    rs_id = args.record_set_id.strip()
    src = args.source_id.strip()
    if not rs_id or not src:
        raise ToolError(code="invalid_argument",
                        message="record_set_id and source_id must not be empty.")
    if _WS_RE.search(rs_id) or _WS_RE.search(src):
        raise ToolError(code="invalid_argument",
                        message="record_set_id and source_id must not contain whitespace.")
    if not args.columns:
        raise ToolError(code="invalid_argument", message="columns must not be empty.")
    if len(args.columns) > MAX_COLUMNS:
        raise ToolError(code="invalid_argument",
                        message=f"{len(args.columns)} columns; at most {MAX_COLUMNS} (one "
                                f"apply call writes at most {MAX_AVUS} AVUs).",
                        details={"count": len(args.columns), "limit": MAX_COLUMNS})

    n = args.record_set_index
    warnings: list[dict[str, str]] = []
    fields: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for i, col in enumerate(args.columns):
        # Skipped columns have no field: their messages name the input (columns[i]).
        raw = col.name
        if not raw.strip():
            warnings.append(_msg(f"columns[{i}]", "column without a name skipped"))
            continue
        if raw in seen:
            warnings.append(_msg(f"columns[{i}]", f"duplicate column {raw!r} skipped (it is "
                                                  f"croissant.recordSet.{n}.field.{seen[raw]})"))
            continue
        m = len(fields) + 1
        seen[raw] = m
        path = f"croissant.recordSet.{n}.field.{m}"
        typed = core.duckdb_type_to_croissant(col.type)
        # The exact column name, so the field reads the real column.
        f = _field_model(raw, typed, source=src, column=raw, path=path, warnings=warnings)
        if raw != raw.strip():
            warnings.append(_msg(f"{path}.column", (
                f"column name {raw!r} has leading or trailing whitespace: the field's column "
                "keeps it, but MESA writers (mesa_croissant_apply, the portal editor) store "
                "values trimmed, so the stored column would not match; rename the column")))
        if col.description and col.description.strip():
            f["description"] = col.description.strip()
        fields.append(f)
    _assign_ids(fields, rs_id)

    rs: dict[str, Any] = {"id": rs_id}
    if args.record_set_name and args.record_set_name.strip():
        rs["name"] = args.record_set_name.strip()
    if args.description and args.description.strip():
        rs["description"] = args.description.strip()
    rs["field"] = fields
    avus = core.model_to_avus({"recordSet": [rs]})
    if n != 1:
        prefix = "croissant.recordSet.1."
        avus = [{**a, "attribute": f"croissant.recordSet.{n}." + a["attribute"][len(prefix):]}
                for a in avus]
    if len(avus) > MAX_AVUS:
        raise ToolError(code="invalid_argument",
                        message=f"these columns give {len(avus)} AVUs; one apply call writes "
                                f"at most {MAX_AVUS}: give fewer columns",
                        details={"count": len(avus), "limit": MAX_AVUS})
    # The AVU limits apply refuses: reported here already, on the attribute.
    warnings += [_msg(e["path"], f"{e['message']}; mesa_croissant_apply refuses it")
                 for e in _limit_errors(avus)]
    return {
        "record_set_id": rs_id,
        "avus": avus,
        "model": {"recordSet": [rs]},
        "warnings": warnings,
        "next_step": (
            f"mesa_croissant_apply(path=<dataset folder>, avus=<these avus>, mode='merge') "
            f"adds record set {rs_id!r}; its source {src!r} must be a "
            "croissant.distribution.N.id on the same item"
        ),
    }


__all__ = [
    "META",
    "TOOL_NAMES",
    "TOOL_SURFACE",
    "compute_diff",
    "handle_apply",
    "handle_export",
    "handle_from_schema",
    "handle_template",
    "handle_validate",
]
