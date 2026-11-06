"""MESA Croissant core: AVUs <-> nested model <-> MLCommons Croissant JSON-LD.

This module implements the MESA Croissant AVU convention, version 1
(``docs/AVU_CONVENTION.md`` in github.com/idss-mesa/mesa-croissant): the
dotted-path codec for ``croissant.*`` iRODS AVUs, the generator of Croissant
Core 1.1 (+ RAI 1.0, + GeoCroissant 1.0) JSON-LD documents, the MESA
crosswalk from ``datacite.*`` / ``dc.*`` / ontology AVUs, a tolerant importer
for Croissant 0.8 / 1.0 / 1.1 documents, a structural validator, and the
encoding-format and DuckDB type maps.

It uses the Python standard library only and performs no I/O, so the same file
runs in MESA-MCP and in the MESA Portal.

VENDORED COPY: this file is copied byte-identical into mesa-portal as
``portal/services/croissant_core.py``. Do not edit it there. Change it in the
mesa-croissant repository (``src/mesa_croissant/core.py``), bump
``CORE_VERSION`` and copy it again; a portal test compares the declared version.

Public API (see ``docs/DESIGN.md``)::

    CONVENTION_VERSION, CORE_VERSION, PREFIX, CONFORMS_CORE, CONFORMS_RAI,
    CONFORMS_GEO, CONTEXT, UNKNOWN_KEY, DUP_KEY
    is_croissant_attr(attr) -> bool
    parse_avus(avus) -> {"model", "unknown", "warnings"}
    model_to_avus(model) -> [{"attribute", "value", "unit"}]
    crosswalk(avus) -> {"<property>": {"value", "source", "avus"}}
    to_jsonld(avus, *, item) -> {"document", "sources", "validation"}
    from_jsonld(doc, *, content_base=None, item=None) -> {"avus", "warnings", "errors"}
    validate(doc) -> {"valid", "errors", "warnings", "conformsTo", "summary"}
    encoding_format_for(name) -> str | None
    duckdb_type_to_croissant(type_name) -> {"dataType", "isArray", "arrayShape"[, "subFields"]}
    suggest_distribution(entries, *, lake=None, tables=None) -> [model entries]
    key_catalog(level="all") -> [{"attribute", "cardinality", "required", "level", ...}]

Messages (warnings and errors) are dicts ``{"path": str, "message": str}``. For
AVU-level messages ``path`` is the attribute; for JSON-LD messages it is a
0-based property path such as ``recordSet[0].field[1].source``.
"""

from __future__ import annotations

import copy
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any
from urllib.parse import quote, unquote

__all__ = [
    "CONVENTION_VERSION",
    "CORE_VERSION",
    "PREFIX",
    "CONFORMS_CORE",
    "CONFORMS_RAI",
    "CONFORMS_GEO",
    "CONTEXT",
    "VALUE_MAX",
    "UNIT_MAX",
    "UNKNOWN_KEY",
    "DUP_KEY",
    "is_croissant_attr",
    "parse_avus",
    "model_to_avus",
    "crosswalk",
    "to_jsonld",
    "from_jsonld",
    "validate",
    "encoding_format_for",
    "duckdb_type_to_croissant",
    "suggest_distribution",
    "key_catalog",
]

CONVENTION_VERSION: int = 1
CORE_VERSION: str = "1.3.0"
PREFIX = "croissant."

CONFORMS_CORE = "http://mlcommons.org/croissant/1.1"
CONFORMS_RAI = "http://mlcommons.org/croissant/RAI/1.0"
CONFORMS_GEO = "http://mlcommons.org/croissant/geo/1.0"

#: Largest AVU value / unit the portal and Terrain accept.
VALUE_MAX = 2700
UNIT_MAX = 250

#: Defaults used when the caller's ``item`` does not say otherwise.
PORTAL_URL = "https://mesa.cyverse.org"
DAV_ANON_URL = "https://data.cyverse.org/dav-anon"
DAV_URL = "https://data.cyverse.org/dav"
PUBLIC_ROOT = "/iplant/home/shared"

_SCHEMA_ORG = "https://schema.org/"

#: The Croissant 1.1 JSON-LD context (spec Appendix 1) plus ``geocr`` and ``prov``.
#: ``@vocab``/``sc`` use ``https://schema.org/`` (as mlcroissant, Hugging Face and
#: every upstream example do): mlcroissant 1.1.1 does not recognise a dataset whose
#: ``@type`` expands to ``http://schema.org/Dataset``. The importer accepts both.
CONTEXT: dict[str, Any] = {
    "@language": "en",
    "@vocab": _SCHEMA_ORG,
    "sc": _SCHEMA_ORG,
    "cr": "http://mlcommons.org/croissant/",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "dct": "http://purl.org/dc/terms/",
    "annotation": "cr:annotation",
    "arrayShape": "cr:arrayShape",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "containedIn": "cr:containedIn",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "equivalentProperty": "cr:equivalentProperty",
    "examples": {"@id": "cr:examples", "@type": "@json"},
    "excludes": "cr:excludes",
    "extract": "cr:extract",
    "field": "cr:field",
    "fileProperty": "cr:fileProperty",
    "fileObject": "cr:fileObject",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "includes": "cr:includes",
    "isArray": "cr:isArray",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "parentField": "cr:parentField",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "readLines": "cr:readLines",
    "sdVersion": "cr:sdVersion",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
    "unArchive": "cr:unArchive",
    "value": "cr:value",
    "geocr": "http://mlcommons.org/croissant/geo/",
    "prov": "http://www.w3.org/ns/prov#",
}

# ---------------------------------------------------------------------------
# Key schema (the convention's sections 2-6), in canonical write order.
# ---------------------------------------------------------------------------

_S = ("S",)  # scalar leaf
_L = ("L",)  # repeatable leaf (prop.N, or prop without index)


def _E(schema: dict) -> tuple:  # repeatable entity (prop.N.<sub>)
    return ("E", schema)


def _G(schema: dict) -> tuple:  # non-repeated group (prop.<sub>)
    return ("G", schema)


_PERSON = {
    "name": _S,
    "type": _S,
    "email": _S,
    "url": _S,
    "identifier": _S,
    "affiliation": _S,
}

#: RAI 1.0 properties: True = many, False = one (convention section 3).
RAI_PROPERTIES: dict[str, bool] = {
    "dataCollection": False,
    "dataCollectionType": True,
    "dataCollectionMissingData": False,
    "dataCollectionRawData": False,
    "dataCollectionTimeframe": True,
    "dataImputationProtocol": False,
    "dataManipulationProtocol": False,
    "dataPreprocessingProtocol": True,
    "dataAnnotationProtocol": False,
    "dataAnnotationPlatform": True,
    "dataAnnotationAnalysis": True,
    "dataReleaseMaintenancePlan": True,
    "personalSensitiveInformation": True,
    "dataSocialImpact": False,
    "dataBiases": True,
    "dataLimitations": True,
    "dataUseCases": True,
    "annotationsPerItem": False,
    "annotatorDemographics": True,
    "machineAnnotationTools": True,
}
_RAI = {k: (_L if many else _S) for k, many in RAI_PROPERTIES.items()}
_RAI_ALIASES = {
    "dataCollectionTimeFrame": "dataCollectionTimeframe",
    "dataDataManipulationProtocol": "dataManipulationProtocol",
}

_GEO = {
    "spatialCoverage": _G({"name": _S, "box": _S, "point": _S}),
    "coordinateReferenceSystem": _S,
    "spatialResolution": _S,
    "temporalResolution": _S,
    "samplingStrategy": _S,
    "spatialBias": _S,
    "recordEndpoint": _S,
    "bandConfiguration": _G({"totalBands": _S, "band": _L}),
}

_DIST = {
    "id": _S,
    "type": _S,
    "name": _S,
    "description": _S,
    "contentUrl": _S,
    "encodingFormat": _S,
    "contentSize": _S,
    "sha256": _S,
    "md5": _S,
    "containedIn": _L,
    "includes": _L,
    "excludes": _L,
}

_FILE = {"name": _S, "description": _S, "encodingFormat": _S, "sha256": _S, "md5": _S}

_FIELD: dict[str, tuple] = {
    "name": _S,
    "id": _S,
    "description": _S,
    "dataType": _L,
    "source": _S,
    "column": _S,
    "jsonPath": _S,
    "fileProperty": _S,
    "regex": _S,
    "separator": _S,
    "transformJsonPath": _S,
    "format": _S,
    "references": _S,
    "isArray": _S,
    "arrayShape": _S,
    "equivalentProperty": _L,
}
_FIELD["subField"] = _E(_FIELD)  # recursive

_RECORDSET = {
    "id": _S,
    "name": _S,
    "description": _S,
    "dataType": _S,
    "key": _L,
    "data": _S,
    "examples": _S,
    "field": _E(_FIELD),
}

_DATASET: dict[str, tuple] = {
    "conformsTo": _L,
    "name": _S,
    "alternateName": _S,
    "description": _S,
    "url": _S,
    "license": _L,
    "creator": _E(_PERSON),
    "publisher": _E(_PERSON),
    "datePublished": _S,
    "dateCreated": _S,
    "dateModified": _S,
    "version": _S,
    "citeAs": _S,
    "keyword": _L,
    "inLanguage": _L,
    "sameAs": _L,
    "identifier": _S,
    "isLiveDataset": _S,
    "sdVersion": _S,
    "sdLicense": _S,
    "temporalCoverage": _S,
    "wasGeneratedBy": _S,
    "wasDerivedFrom": _L,
    "rai": _G(_RAI),
    "geo": _G(_GEO),
    "file": _G(_FILE),
    "distribution": _E(_DIST),
    "recordSet": _E(_RECORDSET),
    "avuConvention": _S,
}

_INDEX_RE = re.compile(r"[0-9]+")

# Data types (convention section 6).
_SC_TYPES = {
    "Boolean", "Date", "DateTime", "Time", "Float", "Integer", "Number", "Text", "URL",
    "ImageObject", "AudioObject", "VideoObject", "GeoShape", "GeoCoordinates",
    "Enumeration",
}
_CR_TYPES = {
    "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64",
    "Float16", "Float32", "Float64", "BoundingBox", "Label", "Split", "SegmentationMask",
}

_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_CURIE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*:[^\s/][^\s]*$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_ISO_DATE_RE = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}(?::?\d{2})?)?)?)?)?$"
)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_ARRAY_SHAPE_RE = re.compile(r"^\s*-?\d+(?:\s*,\s*-?\d+)*\s*$")
_FILE_PROPERTIES = {"fullpath", "filename", "content", "lines", "lineNumbers"}
_NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _msg(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


def _text(value: Any) -> str:
    """Render a JSON scalar as AVU text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _leaf(value: Any, unit: Any = "") -> dict[str, str]:
    return {"value": _text(value), "unit": _text(unit)}


def _as_leaf(node: Any) -> dict[str, str] | None:
    """Accept a model leaf or a bare scalar; ``None`` when empty or whitespace only.

    Only emptiness is tested: a non-blank value is returned unchanged (data-info
    compares AVUs exactly, so values are never trimmed)."""
    if node is None:
        return None
    if isinstance(node, Mapping):
        if "value" not in node:
            return None
        leaf = _leaf(node.get("value"), node.get("unit") or "")
    else:
        leaf = _leaf(node)
    return leaf if leaf["value"].strip() != "" else None


def _is_blank(node: Any) -> bool:
    """A leaf (or scalar) whose text is non-empty but whitespace only."""
    if isinstance(node, Mapping):
        node = node.get("value")
    if node is None or isinstance(node, Mapping | list | tuple):
        return False
    text = _text(node)
    return text != "" and text.strip() == ""


def _as_list(node: Any) -> list:
    if node is None:
        return []
    if isinstance(node, list | tuple):
        return list(node)
    return [node]


def _lv(node: Any) -> str | None:
    """Value of a model leaf (or None)."""
    leaf = _as_leaf(node)
    return leaf["value"] if leaf else None


def _lvs(node: Any) -> list[str]:
    out = []
    for item in _as_list(node):
        v = _lv(item)
        if v is not None:
            out.append(v)
    return out


def _unbox(values: list) -> Any:
    return values[0] if len(values) == 1 else values


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and bool(_URL_RE.match(value.strip()))


def _is_curie(value: Any) -> bool:
    return isinstance(value, str) and not _is_url(value) and bool(_CURIE_RE.match(value))


def _number(value: str) -> int | float | None:
    v = value.strip()
    if not _NUM_RE.match(v):
        return None
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    return float(v)


def _iso_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    m = _ISO_DATE_RE.match(value.strip())
    if not m:
        return False
    year, month, day = m.group(1), m.group(2), m.group(3)
    try:
        if day:
            date(int(year), int(month), int(day))
        elif month and not 1 <= int(month) <= 12:
            return False
        if m.group(4):
            hh, mm = int(m.group(4)), int(m.group(5))
            ss = int(m.group(6) or 0)
            if hh > 23 or mm > 59 or ss > 60:
                return False
    except ValueError:
        return False
    return True


def _interval_ok(value: str) -> bool:
    parts = value.strip().split("/")
    if len(parts) == 1:
        return _iso_ok(parts[0])
    if len(parts) != 2:
        return False
    return all(p == ".." or _iso_ok(p) for p in parts) and parts != ["..", ".."]


def _doi(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", v, flags=re.IGNORECASE)
    return v if _DOI_RE.match(v) else None


def _orcid_url(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if _is_url(v):
        return v
    v = re.sub(r"^orcid:", "", v, flags=re.IGNORECASE)
    return f"https://orcid.org/{v}" if _ORCID_RE.match(v) else None


def _norm_box(value: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", value.strip()) if p]


def _dav_url(base: str, path: str) -> str:
    return base.rstrip("/") + quote(path, safe="/")


def _normalize_avu(avu: Any) -> dict[str, str] | None:
    if isinstance(avu, Mapping):
        attr = avu.get("attribute", avu.get("attr"))
        value = avu.get("value")
        unit = avu.get("unit") or ""
    elif isinstance(avu, list | tuple) and len(avu) >= 2:
        attr, value = avu[0], avu[1]
        unit = avu[2] if len(avu) > 2 and avu[2] is not None else ""
    else:
        return None
    if not isinstance(attr, str):
        return None
    return {"attribute": attr, "value": _text(value), "unit": _text(unit)}


def _iter_avus(avus: Iterable[Any] | None) -> list[dict[str, str]]:
    out = []
    for avu in avus or []:
        norm = _normalize_avu(avu)
        if norm is not None:
            out.append(norm)
    return out


# ---------------------------------------------------------------------------
# Codec: AVUs <-> model
# ---------------------------------------------------------------------------


def is_croissant_attr(attr: str) -> bool:
    """True when ``attr`` is a Croissant AVU attribute (``croissant.<something>``)."""
    return isinstance(attr, str) and attr.startswith(PREFIX) and len(attr) > len(PREFIX)


UNKNOWN_KEY = "__unknown"  # an entity's own unknown sub-keys: [{"suffix", "value", "unit"}]
DUP_KEY = "__dup"  # a list leaf's extra values at the same index: [{"value", "unit"}]
_DUPLICATE_SCALAR = ("duplicate value for a single-valued path; the first one is shown, the "
                     "other is kept on save; delete it in the Raw tab to resolve")
_DUPLICATE_INDEX = ("duplicate value for the same index; the first one is shown, the other "
                    "is kept on save; delete it in the Raw tab to resolve")


def _place(schema: dict, segs: list[str], store: dict, leaf: dict, attr: str,
           warnings: list) -> str:
    """Insert ``leaf`` at ``segs`` below ``store``.

    Returns ok / unknown / zero / duplicate, or ``bound``: an unknown key below an
    entity, kept in that entity's ``__unknown`` list (a duplicate there returns ok).
    A duplicate list index is kept on the leaf it duplicates (``__dup``) and returns
    ok: it then follows that leaf when the list is renumbered.
    """
    name = segs[0]
    spec = schema.get(name)
    if spec is None:
        return "unknown"
    kind, rest = spec[0], segs[1:]
    if kind == "S":
        if rest:
            return "unknown"
        if name in store:
            warnings.append(_msg(attr, _DUPLICATE_SCALAR))
            return "duplicate"
        store[name] = leaf
        return "ok"
    if kind == "L":
        if not rest:
            idx = 1
        elif len(rest) == 1 and _INDEX_RE.fullmatch(rest[0]):
            idx = int(rest[0])
            if idx == 0:
                return "zero"
        else:
            return "unknown"
        bucket = store.setdefault(name, {})
        if idx in bucket:
            warnings.append(_msg(attr, _DUPLICATE_INDEX))
            # Kept with the value it duplicates, so a save writes it under that value's
            # new index (a gap before it would otherwise make it a value of its own).
            bucket[idx].setdefault(DUP_KEY, []).append(
                {"value": leaf["value"], "unit": leaf["unit"]})
            return "ok"
        bucket[idx] = leaf
        return "ok"
    if kind == "E":
        if rest and _INDEX_RE.fullmatch(rest[0]):
            idx = int(rest[0])
            rest = rest[1:]
            if idx == 0:
                return "zero"
        else:
            idx = 1
        if not rest:
            return "unknown"
        bucket = store.setdefault(name, {})
        sub = bucket.setdefault(idx, {})
        result = _place(spec[1], rest, sub, leaf, attr, warnings)
        if result not in ("unknown", "duplicate"):
            return result
        # Kept with its entity, so it follows the entity when the indexes are renumbered.
        sub.setdefault(UNKNOWN_KEY, []).append(
            {"suffix": ".".join(rest), "value": leaf["value"], "unit": leaf["unit"]})
        return "bound" if result == "unknown" else "ok"
    # group
    if not rest:
        return "unknown"
    sub = store.setdefault(name, {})
    return _place(spec[1], rest, sub, leaf, attr, warnings)


def _finalize(schema: dict, store: dict) -> dict:
    out: dict[str, Any] = {}
    for name, spec in schema.items():
        if name not in store:
            continue
        node = store[name]
        kind = spec[0]
        if kind == "S":
            out[name] = node
        elif kind == "L":
            items = [node[i] for i in sorted(node)]
            if items:
                out[name] = items
        elif kind == "E":
            items = [_finalize(spec[1], node[i]) for i in sorted(node)]
            items = [i for i in items if i]
            if items:
                out[name] = items
        else:
            sub = _finalize(spec[1], node)
            if sub:
                out[name] = sub
    if UNKNOWN_KEY in store:
        out[UNKNOWN_KEY] = [dict(u) for u in store[UNKNOWN_KEY]]
    return out


def parse_avus(avus: Iterable[Any]) -> dict[str, Any]:
    """Read the ``croissant.*`` AVUs into the nested model.

    ``avus`` is an iterable of ``{"attribute"|"attr", "value", "unit"}`` dicts (or
    ``(attribute, value, unit)`` tuples); other attributes are ignored.

    Returns ``{"model": tree, "unknown": [avu, ...], "warnings": [msg, ...]}``. In the
    tree a leaf is ``{"value": str, "unit": str}``, a repeatable property is a list
    ordered by index (gaps closed, index 0 ignored), an entity is a dict. Unknown
    ``croissant.*`` keys and index-0 entries are returned in ``unknown`` so editors
    can keep them. An unknown key below an indexed entity (``creator.2.orcidVerified``,
    ``recordSet.1.field.3.transform``) is kept with that entity instead, as
    ``entity["__unknown"] = [{"suffix", "value", "unit"}]``, and :func:`model_to_avus`
    writes it back under the entity's new index. A duplicate single-valued path shows the
    first AVU in the model and keeps the others the same way (in ``unknown``, or with
    their entity), with a warning. A duplicate list index (``license.2`` twice) shows the
    first value and keeps the others on that leaf, ``leaf["__dup"] = [{"value", "unit"}]``:
    :func:`model_to_avus` writes them under the leaf's own attribute, so they stay
    duplicates when the list is renumbered, and removing the leaf removes them too.
    """
    store: dict[str, Any] = {}
    unknown: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for avu in _iter_avus(avus):
        attr = avu["attribute"]
        if not is_croissant_attr(attr):
            continue
        if avu["value"] == "":
            warnings.append(_msg(attr, "empty value ignored"))
            continue
        if avu["value"].strip() == "":
            warnings.append(_msg(attr, "blank value ignored; the AVU is removed on the next "
                                       "save"))
            continue
        if len(avu["value"]) > VALUE_MAX:
            warnings.append(_msg(attr, f"value is longer than {VALUE_MAX} characters"))
        if len(avu["unit"]) > UNIT_MAX:
            warnings.append(_msg(attr, f"unit is longer than {UNIT_MAX} characters"))
        segs = attr[len(PREFIX):].split(".")
        if any(s == "" for s in segs):
            result = "unknown"
        else:
            result = _place(_DATASET, segs, store, _leaf(avu["value"], avu["unit"]), attr,
                            warnings)
        if result in ("unknown", "bound"):
            if result == "unknown":
                unknown.append(dict(avu))
            warnings.append(_msg(attr, "unknown Croissant key (kept, not emitted)"))
        elif result == "duplicate":
            unknown.append(dict(avu))
        elif result == "zero":
            unknown.append(dict(avu))
            warnings.append(_msg(attr, "index 0 is ignored (indexes start at 1)"))
    return {"model": _finalize(_DATASET, store), "unknown": unknown, "warnings": warnings}


def _emit_avus(schema: dict, node: Mapping, prefix: str, out: list, is_field: bool) -> None:
    for name, spec in schema.items():
        if name not in node:
            continue
        value = node[name]
        kind = spec[0]
        if kind == "S":
            leaf = _as_leaf(value)
            if leaf:
                out.append({"attribute": prefix + name, **leaf})
        elif kind == "L":
            leaves = [(lf, v) for v in _as_list(value) if (lf := _as_leaf(v))]
            single = is_field and name == "dataType" and len(leaves) == 1
            for i, (leaf, node_leaf) in enumerate(leaves, 1):
                attr = prefix + name if single else f"{prefix}{name}.{i}"
                out.append({"attribute": attr, **leaf})
                # Its duplicates keep exactly its attribute, so they stay duplicates.
                out.extend({"attribute": attr, **d} for d in _leaf_dups(node_leaf))
        elif kind == "E":
            n = 0
            sub_is_field = spec[1] is _FIELD
            for entity in _as_list(value):
                if not isinstance(entity, Mapping):
                    continue
                tmp: list = []
                sub_prefix = f"{prefix}{name}.{n + 1}."
                _emit_avus(spec[1], entity, sub_prefix, tmp, sub_is_field)
                tmp.extend({"attribute": sub_prefix + u["suffix"], "value": u["value"],
                            "unit": u["unit"]} for u in _entity_unknown(entity))
                if tmp:
                    n += 1
                    out.extend(tmp)
        elif isinstance(value, Mapping):
            _emit_avus(spec[1], value, f"{prefix}{name}.", out, False)


def _leaf_dups(node: Any) -> list[dict[str, str]]:
    """The well-formed ``__dup`` entries of a model list leaf (see :func:`parse_avus`)."""
    if not isinstance(node, Mapping):
        return []
    return [lf for lf in (_as_leaf(d) for d in _as_list(node.get(DUP_KEY))
                          if isinstance(d, Mapping)) if lf]


def _entity_unknown(entity: Mapping) -> list[dict[str, str]]:
    """The well-formed ``__unknown`` entries of a model entity (see :func:`parse_avus`)."""
    out = []
    for u in _as_list(entity.get(UNKNOWN_KEY)):
        if not isinstance(u, Mapping):
            continue
        suffix = u.get("suffix")
        leaf = _as_leaf({"value": u.get("value"), "unit": u.get("unit") or ""})
        if not isinstance(suffix, str) or not suffix or "" in suffix.split(".") or not leaf:
            continue
        out.append({"suffix": suffix, **leaf})
    return out


def _without_dup(leaf: Any) -> Any:
    if isinstance(leaf, Mapping) and DUP_KEY in leaf:
        return {k: v for k, v in leaf.items() if k != DUP_KEY}
    return leaf


def _without_unknown(node: Mapping, schema: dict) -> dict:
    """The model without ``__unknown`` and ``__dup`` keys; an entity left with nothing else
    is dropped."""
    out: dict[str, Any] = {}
    for name, value in node.items():
        if name == UNKNOWN_KEY:
            continue
        spec = schema.get(name)
        kind = spec[0] if spec is not None else None
        if kind == "E":
            items = [_without_unknown(e, spec[1]) for e in _as_list(value)
                     if isinstance(e, Mapping)]
            items = [e for e in items if e]
            if items:
                out[name] = items
        elif kind == "L":
            out[name] = ([_without_dup(v) for v in value] if isinstance(value, list | tuple)
                         else _without_dup(value))
        elif kind == "G" and isinstance(value, Mapping):
            out[name] = _without_unknown(value, spec[1])
        else:
            out[name] = value
    return out


def model_to_avus(model: Mapping[str, Any]) -> list[dict[str, str]]:
    """Write the model back as AVUs (inverse of :func:`parse_avus` for known paths).

    Returns ``[{"attribute", "value", "unit"}]`` in canonical order, indexes from 1
    without gaps. A field's single ``dataType`` is written without an index; every
    other repeatable property is indexed. Leaves may be ``{"value", "unit"}`` dicts or
    bare scalars; empty values are skipped. An entity's ``__unknown`` keys are written
    with it, under its new index, and a list leaf's ``__dup`` values right after it,
    under its attribute; the top-level ``unknown`` AVUs from :func:`parse_avus` are not
    (callers append them themselves).
    """
    out: list[dict[str, str]] = []
    _emit_avus(_DATASET, model or {}, PREFIX, out, False)
    return out


# ---------------------------------------------------------------------------
# Key catalog (for editors and the MCP template tool)
# ---------------------------------------------------------------------------

_DESCRIPTIONS = {
    "conformsTo": "extra conformsTo URI (Core 1.1, RAI and Geo are added automatically)",
    "name": "name of the dataset",
    "alternateName": "alternate name",
    "description": "description of the dataset",
    "url": "landing page URL (defaults to the DOI or the portal metadata page)",
    "license": "license URL (SPDX or Creative Commons recommended)",
    "datePublished": "publication date (ISO 8601)",
    "dateCreated": "creation date (ISO 8601)",
    "dateModified": "modification date (ISO 8601)",
    "version": "version (semantic version recommended)",
    "citeAs": "citation (BibTeX recommended)",
    "keyword": "keyword; unit = optional CURIE or IRI of the term",
    "inLanguage": "language (BCP 47)",
    "sameAs": "URL of the same dataset elsewhere",
    "identifier": "identifier, e.g. a DOI URL",
    "isLiveDataset": "true/false",
    "sdVersion": "version of the metadata",
    "sdLicense": "license of the metadata",
    "temporalCoverage": "ISO 8601 interval, e.g. 2018-01-01/2021-12-31",
    "wasGeneratedBy": "provenance: activity that generated the data (text or URL)",
    "wasDerivedFrom": "provenance: URL of a source dataset",
    "avuConvention": "AVU convention version (absent = 1)",
}
_SUB_DESCRIPTIONS = {
    "person": {
        "name": "name", "type": "Person (default) or Organization", "email": "email",
        "url": "URL", "identifier": "ORCID or ROR URL",
        "affiliation": "organization name (Person only)",
    },
    "distribution": {
        "id": "@id (unique)", "type": "FileObject (default) or FileSet",
        "name": "name (defaults to id)", "description": "description",
        "contentUrl": "absolute URL or path relative to the dataset root",
        "encodingFormat": "MIME type", "contentSize": "bytes; unit optional (default B)",
        "sha256": "SHA-256 hex digest", "md5": "MD5 hex digest",
        "containedIn": "id of the containing FileObject/FileSet",
        "includes": "glob relative to the dataset root (FileSet)",
        "excludes": "glob relative to the dataset root (FileSet)",
    },
    "file": {
        "name": "name of the file", "description": "description of the file",
        "encodingFormat": "MIME type", "sha256": "SHA-256 hex digest", "md5": "MD5 hex digest",
    },
    "recordset": {
        "id": "@id (unique)", "name": "name (defaults to id)", "description": "description",
        "dataType": "e.g. cr:Split, sc:Enumeration", "key": "field id forming the key",
        "data": "inline JSON array of records", "examples": "inline JSON or URL",
    },
    "field": {
        "name": "field name", "id": "defaults to <recordSet id>/<name>",
        "description": "description", "dataType": "sc:Text, sc:Integer, cr:Int32, ... or a CURIE",
        "source": "id of a FileObject, FileSet or record set", "column": "extract.column",
        "jsonPath": "extract.jsonPath", "fileProperty": "extract.fileProperty",
        "regex": "transform.regex", "separator": "transform.separator",
        "transformJsonPath": "transform.jsonPath (e.g. the member of a STRUCT column)",
        "format": "source.format (CLDR pattern)", "references": "referenced field id",
        "isArray": "true/false", "arrayShape": "e.g. -1 or 512,512,6",
        "equivalentProperty": "URL",
    },
    "geo": {
        "spatialCoverage.name": "place name",
        "spatialCoverage.box": "S W N E (lat/lon, space separated)",
        "spatialCoverage.point": "lat lon",
        "coordinateReferenceSystem": "e.g. EPSG:4326",
        "spatialResolution": "number; unit = unit text (m)",
        "temporalResolution": "number; unit = unit text (day)",
        "samplingStrategy": "sampling strategy", "spatialBias": "spatial bias",
        "recordEndpoint": "URL (e.g. OGC API Records)",
        "bandConfiguration.totalBands": "integer", "bandConfiguration.band": "band name",
    },
}
_REQUIRED_DATASET = ("name", "description", "url", "license", "creator", "datePublished")


def key_catalog(level: str = "all") -> list[dict[str, Any]]:
    """The convention's key catalog.

    ``level`` is ``dataset`` (core + persons), ``rai``, ``geo``, ``file``,
    ``distribution``, ``recordset`` (record sets and fields) or ``all``. Each row is
    ``{"attribute", "cardinality" ("one"|"many"), "required", "level", "unit",
    "description"}``; ``N``/``M``/``K`` stand for 1-based indexes.
    """
    rows: list[dict[str, Any]] = []

    def row(attr, card, req, lvl, desc, unit=""):
        rows.append({"attribute": attr, "cardinality": card, "required": req, "level": lvl,
                     "unit": unit, "description": desc})

    for name, spec in _DATASET.items():
        if name in ("rai", "geo", "file", "distribution", "recordSet"):
            continue
        if name in ("creator", "publisher"):
            for sub in _PERSON:
                row(f"croissant.{name}.N.{sub}", "many", name == "creator" and sub == "name",
                    "dataset", f"{name}: {_SUB_DESCRIPTIONS['person'][sub]}")
            continue
        many = spec[0] == "L"
        attr = f"croissant.{name}.N" if many else f"croissant.{name}"
        unit = "optional CURIE or IRI" if name == "keyword" else ""
        row(attr, "many" if many else "one", name in _REQUIRED_DATASET, "dataset",
            _DESCRIPTIONS.get(name, name), unit)
    for name, many in RAI_PROPERTIES.items():
        row(f"croissant.rai.{name}" + (".N" if many else ""), "many" if many else "one",
            False, "rai", f"RAI 1.0 rai:{name}")
    for key, desc in _SUB_DESCRIPTIONS["geo"].items():
        many = key == "bandConfiguration.band"
        unit = "unit text" if key.endswith("Resolution") else ""
        row(f"croissant.geo.{key}" + (".N" if many else ""), "many" if many else "one",
            False, "geo", desc, unit)
    for key, desc in _SUB_DESCRIPTIONS["file"].items():
        row(f"croissant.file.{key}", "one", False, "file", desc)
    for key, desc in _SUB_DESCRIPTIONS["distribution"].items():
        many = key in ("containedIn", "includes", "excludes")
        row(f"croissant.distribution.N.{key}" + (".M" if many else ""),
            "many" if many else "one", key == "id", "distribution", desc,
            "optional unit (default B)" if key == "contentSize" else "")
    for key, desc in _SUB_DESCRIPTIONS["recordset"].items():
        many = key == "key"
        row(f"croissant.recordSet.N.{key}" + (".M" if many else ""),
            "many" if many else "one", key == "id", "recordset", desc)
    for key, desc in _SUB_DESCRIPTIONS["field"].items():
        many = key in ("dataType", "equivalentProperty")
        row(f"croissant.recordSet.N.field.M.{key}" + (".K" if many else ""),
            "many" if many else "one", key == "name", "recordset", desc)
    row("croissant.recordSet.N.field.M.subField.K.<sub>", "many", False, "recordset",
        "nested field with the field sub-keys (one level)")
    if level in ("", "all", None):
        return rows
    return [r for r in rows if r["level"] == level]


# ---------------------------------------------------------------------------
# Crosswalk (convention section 7)
# ---------------------------------------------------------------------------

_RESERVED_ONTOLOGY_PREFIXES = {
    "croissant", "datacite", "dc", "eml", "localcontexts", "mesa", "qa", "neon",
    "ducklake", "ipc-", "irods::", "ipc_", "ipc",
}
_DC_INDEXED_RE = re.compile(r"^datacite\.([A-Za-z]+)\.(\d+)(?:\.([A-Za-z]+))?$", re.IGNORECASE)


class _AvuIndex:
    """Case-insensitive lookup of AVUs by attribute, keeping AVU order."""

    def __init__(self, avus: list[dict[str, str]]):
        self.avus = avus
        self.by_attr: dict[str, list[dict[str, str]]] = {}
        self.families: dict[str, dict[int, dict[str, tuple[str, str]]]] = {}
        for avu in avus:
            attr = avu["attribute"]
            if avu["value"].strip() == "":
                continue
            self.by_attr.setdefault(attr.lower(), []).append(avu)
            m = _DC_INDEXED_RE.match(attr)
            if m:
                fam = m.group(1).lower()
                idx = int(m.group(2))
                sub = (m.group(3) or "").lower()
                entry = self.families.setdefault(fam, {}).setdefault(idx, {})
                entry.setdefault(sub, (avu["value"], attr))

    def first(self, *attrs: str) -> tuple[str, str] | None:
        for attr in attrs:
            items = self.by_attr.get(attr.lower())
            if items:
                return items[0]["value"], items[0]["attribute"]
        return None

    def all(self, attr: str) -> list[dict[str, str]]:
        return self.by_attr.get(attr.lower(), [])

    def family(self, name: str) -> list[tuple[int, dict[str, tuple[str, str]]]]:
        fam = self.families.get(name.lower(), {})
        return [(i, fam[i]) for i in sorted(fam) if i > 0]


def _pick(entry: dict[str, tuple[str, str]], *subs: str) -> tuple[str, str] | None:
    for sub in subs:
        if sub.lower() in entry:
            return entry[sub.lower()]
    return None


def _person_jsonld(name: str, *, org: bool = False, email: str | None = None,
                   url: str | None = None, identifier: str | None = None,
                   affiliation: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"@type": "sc:Organization" if org else "sc:Person", "name": name}
    if email:
        out["email"] = email
    if url:
        out["url"] = url
    if identifier:
        out["identifier"] = identifier
    if affiliation and not org:
        out["affiliation"] = {"@type": "sc:Organization", "name": affiliation}
    return out


def _person_model(p: dict[str, Any]) -> dict[str, Any]:
    m: dict[str, Any] = {"name": _leaf(p["name"])}
    if p.get("@type") == "sc:Organization":
        m["type"] = _leaf("Organization")
    for k in ("email", "url", "identifier"):
        if p.get(k):
            m[k] = _leaf(p[k])
    aff = p.get("affiliation")
    if isinstance(aff, Mapping) and aff.get("name"):
        m["affiliation"] = _leaf(aff["name"])
    return m


def _ontology_terms(ix: _AvuIndex) -> list[tuple[str, str, str]]:
    terms = []
    for avu in ix.avus:
        attr, unit = avu["attribute"], avu["unit"].strip()
        if "." not in attr or not avu["value"] or not unit:
            continue
        prefix = attr.split(".", 1)[0].lower()
        if prefix in _RESERVED_ONTOLOGY_PREFIXES or prefix.startswith("ipc"):
            continue
        if not _is_curie(unit):
            continue
        terms.append((avu["value"], unit, attr))
    return terms


def _place_jsonld(name: str | None, box: list[str] | None,
                  point: list[str] | None) -> dict[str, Any]:
    place: dict[str, Any] = {"@type": "sc:Place"}
    if name:
        place["name"] = name
    geos = []
    if box:
        geos.append({"@type": "sc:GeoShape", "box": " ".join(box)})
    if point and len(point) == 2 and all(_number(p) is not None for p in point):
        geos.append({"@type": "sc:GeoCoordinates", "latitude": _number(point[0]),
                     "longitude": _number(point[1])})
    if geos:
        place["geo"] = _unbox(geos)
    return place


def _prov_obj(value: str, kind: str) -> dict[str, Any]:
    """A provenance value as a PROV-O node: ``{"@id"}`` for an IRI, else a labelled
    ``prov:Activity`` (``kind="activity"``) or ``prov:Entity`` (``kind="entity"``)."""
    v = value.strip()
    if _is_url(v) or re.match(r"^urn:[A-Za-z0-9][A-Za-z0-9-]*:\S+$", v, re.IGNORECASE):
        return {"@id": v}
    return {"@type": "prov:Activity" if kind == "activity" else "prov:Entity",
            "prov:label": value}


def _geo_crosswalk(box: tuple[str, str] | None, point: tuple[str, str] | None,
                   place: tuple[str, str] | None) -> tuple[dict, str, dict] | None:
    """One DataCite geoLocation as ``(spatialCoverage, source AVU, model)``, or None."""
    if not (box or point or place):
        return None
    box_parts = _norm_box(box[0]) if box else None
    if box_parts is not None and len(box_parts) != 4:
        box_parts = None
    point_parts = _norm_box(point[0]) if point else None
    geo_model: dict[str, Any] = {}
    if place:
        geo_model["name"] = _leaf(place[0])
    if box_parts:
        geo_model["box"] = _leaf(" ".join(box_parts))
    if point_parts and len(point_parts) == 2:
        geo_model["point"] = _leaf(" ".join(point_parts))
    if not geo_model:
        return None
    src = (box or point or place)[1]
    return (_place_jsonld(place[0] if place else None, box_parts, point_parts), src,
            {"geo": {"spatialCoverage": geo_model}})


def crosswalk(avus: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """MESA crosswalk defaults (convention section 7).

    Returns ``{"<JSON-LD property>": {"value": <JSON-LD value>, "source": "<AVU>",
    "avus": [<croissant.* AVUs that would copy it into Croissant>]}}`` for every
    property that existing ``datacite.*``, ``dc.*``, ``neon.*``, ``mesa.*`` or
    ontology AVUs can supply. ``croissant.*`` AVUs are not consulted (the generator
    uses the crosswalk only for properties without a Croissant AVU). The name
    fallback to the folder/file name is applied by :func:`to_jsonld`.
    """
    ix = _AvuIndex(_iter_avus(avus))
    out: dict[str, dict[str, Any]] = {}

    def put(prop: str, value: Any, source: str, model: dict) -> None:
        out[prop] = {"value": value, "source": source, "avus": model_to_avus(model)}

    # name
    hit = None
    for _i, entry in ix.family("title"):
        hit = _pick(entry, "value", "title", "")
        if hit:
            break
    hit = hit or ix.first("datacite.title", "dc.title")
    if hit:
        put("name", hit[0], hit[1], {"name": _leaf(hit[0])})

    # description
    hit = None
    for _i, entry in ix.family("description"):
        hit = _pick(entry, "value", "description", "")
        if hit:
            break
    hit = hit or ix.first("datacite.description", "dc.description")
    if hit:
        put("description", hit[0], hit[1], {"description": _leaf(hit[0])})

    # creator
    people: list[dict[str, Any]] = []
    source = ""
    for _i, entry in ix.family("creator"):
        nm = _pick(entry, "name", "")
        if not nm:
            given, family = _pick(entry, "givenName"), _pick(entry, "familyName")
            if family:
                nm = (f"{family[0]}, {given[0]}" if given else family[0], family[1])
        if not nm:
            continue
        name_type = (_pick(entry, "nameType") or ("", ""))[0].lower()
        org = name_type.startswith("organi")
        ident = None
        orcid = _pick(entry, "orcid")
        if orcid:
            ident = _orcid_url(orcid[0])
        if not ident:
            nid = _pick(entry, "nameIdentifier")
            if nid:
                ident = _orcid_url(nid[0]) or (nid[0] if _is_url(nid[0]) else None)
        aff = _pick(entry, "affiliation")
        people.append(_person_jsonld(nm[0], org=org, identifier=ident,
                                     affiliation=aff[0] if aff else None))
        source = source or nm[1]
    if not people:
        for attr in ("datacite.creator", "dc.creator"):
            items = ix.all(attr)
            if items:
                people = [_person_jsonld(a["value"]) for a in items]
                source = items[0]["attribute"]
                break
    if people:
        put("creator", _unbox(people), source,
            {"creator": [_person_model(p) for p in people]})

    # publisher
    for attr in ("datacite.publisher", "dc.publisher"):
        items = ix.all(attr)
        if items:
            pubs = [_person_jsonld(a["value"], org=True) for a in items]
            put("publisher", _unbox(pubs), items[0]["attribute"],
                {"publisher": [_person_model(p) for p in pubs]})
            break

    # license
    lic: list[str] = []
    source = ""
    for _i, entry in ix.family("rights"):
        uri = _pick(entry, "rightsURI")
        val = _pick(entry, "value", "rights", "")
        if uri and _is_url(uri[0]):
            lic.append(uri[0].strip())
            source = source or uri[1]
        elif val and _is_url(val[0]):
            lic.append(val[0].strip())
            source = source or val[1]
    if not lic:
        uri = ix.first("datacite.rightsURI")
        if uri and _is_url(uri[0]):
            lic, source = [uri[0].strip()], uri[1]
    if not lic:
        for a in ix.all("dc.rights"):
            if _is_url(a["value"]):
                lic.append(a["value"].strip())
                source = a["attribute"]
    if lic:
        lic = list(dict.fromkeys(lic))
        put("license", _unbox(lic), source, {"license": [_leaf(v) for v in lic]})

    # datePublished
    hit = ix.first("datacite.publicationYear")
    if not hit:
        for _i, entry in ix.family("date"):
            dtype = _pick(entry, "dateType")
            dval = _pick(entry, "date", "value", "")
            if dtype and dval and dtype[0].strip().lower() == "issued":
                hit = dval
                break
    hit = hit or ix.first("dc.date")
    if hit:
        put("datePublished", hit[0].strip(), hit[1], {"datePublished": _leaf(hit[0].strip())})

    # version
    hit = ix.first("datacite.version")
    if hit:
        put("version", hit[0], hit[1], {"version": _leaf(hit[0])})

    # identifier, url (DOI)
    ident = ix.first("datacite.identifier")
    id_type = ix.first("datacite.identifierType")
    if ident:
        doi = _doi(ident[0])
        type_ok = (id_type is None) or id_type[0].strip().upper() == "DOI"
        if doi and type_ok:
            url = f"https://doi.org/{doi}"
            put("identifier", url, ident[1], {"identifier": _leaf(url)})
            put("url", url, ident[1], {"url": _leaf(url)})

    # keywords
    kws: list[Any] = []
    kw_model: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    srcs: list[str] = []
    for _i, entry in ix.family("subject"):
        hit = _pick(entry, "value", "subject", "")
        if hit and (hit[0], "") not in seen:
            seen.add((hit[0], ""))
            kws.append(hit[0])
            kw_model.append(_leaf(hit[0]))
            if not srcs:
                srcs.append(hit[1])
    for value, unit, _attr in _ontology_terms(ix):
        if (value, unit) in seen:
            continue
        seen.add((value, unit))
        kws.append({"@type": "sc:DefinedTerm", "name": value, "termCode": unit})
        kw_model.append(_leaf(value, unit))
        if "ontology" not in srcs:
            srcs.append("ontology")
    if kws:
        put("keywords", kws, "+".join(srcs), {"keyword": kw_model})

    # temporalCoverage
    hit = ix.first("neon.temporalCoverage")
    if not hit:
        for _i, entry in ix.family("date"):
            dtype = _pick(entry, "dateType")
            dval = _pick(entry, "date", "value", "")
            if dtype and dval and dtype[0].strip().lower() == "collected":
                hit = dval
                break
    if hit:
        put("temporalCoverage", hit[0].strip(), hit[1],
            {"temporalCoverage": _leaf(hit[0].strip())})

    # spatialCoverage: the indexed family first, then the flat (legacy) keys
    geo_hit = None
    for _i, entry in ix.family("geoLocation"):
        geo_hit = _geo_crosswalk(_pick(entry, "box", "geoLocationBox"),
                                 _pick(entry, "point", "geoLocationPoint"),
                                 _pick(entry, "place", "geoLocationPlace", ""))
        if geo_hit:
            break
    if not geo_hit:
        geo_hit = _geo_crosswalk(ix.first("datacite.geoLocationBox"),
                                 ix.first("datacite.geoLocationPoint"),
                                 ix.first("datacite.geoLocationPlace"))
    if geo_hit:
        put("spatialCoverage", *geo_hit)

    # prov:wasGeneratedBy
    hit = ix.first("mesa.generated_by")
    if hit:
        put("prov:wasGeneratedBy", _prov_obj(hit[0], "activity"), hit[1],
            {"wasGeneratedBy": _leaf(hit[0])})
    return out


# ---------------------------------------------------------------------------
# Generator: AVUs -> JSON-LD
# ---------------------------------------------------------------------------


def _norm_datatype(value: str) -> str:
    v = value.strip()
    if v.startswith(("https://schema.org/", "http://schema.org/")):
        return "sc:" + v.rsplit("/", 1)[1]
    if v.startswith("http://mlcommons.org/croissant/") and "/" not in v[31:]:
        return "cr:" + v[31:]
    if v.startswith("schema:"):
        return "sc:" + v[7:]
    if ":" in v:
        return v
    if v in _SC_TYPES:
        return "sc:" + v
    if v in _CR_TYPES:
        return "cr:" + v
    return v


def _bool_text(value: str | None) -> bool | str | None:
    if value is None:
        return None
    low = value.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return value


def _quantity(leaf: Any) -> Any:
    value = _lv(leaf)
    if value is None:
        return None
    unit = (_as_leaf(leaf) or {}).get("unit", "")
    num = _number(value)
    if num is None:
        return value
    # "sc:value", not "value": the Croissant context maps the bare term to cr:value.
    qv: dict[str, Any] = {"@type": "sc:QuantitativeValue", "sc:value": num}
    if unit:
        qv["unitText"] = unit
    return qv


def _content_base(item: Mapping[str, Any]) -> str:
    if item.get("content_base"):
        return str(item["content_base"]).rstrip("/")
    path = str(item.get("path") or "")
    root = path if item.get("is_folder", True) else posixpath.dirname(path)
    public = item.get("public")
    if public is None:
        public = root == PUBLIC_ROOT or root.startswith(PUBLIC_ROOT + "/")
    return _dav_url(DAV_ANON_URL if public else DAV_URL, root)


def _resolve_url(url: str, base: str) -> str:
    u = url.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", u):
        return u
    if u.startswith("/"):
        host = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/]+/[^/]+)", base)
        prefix = host.group(1) if host else base
        return prefix.rstrip("/") + quote(posixpath.normpath(u), safe="/")
    rel = posixpath.normpath(u)
    if rel in (".", ""):
        return base
    return base.rstrip("/") + "/" + quote(rel, safe="/")


def _keyword_jsonld(leaf: dict[str, str], warnings: list, path: str,
                    style: str) -> Any:
    value, unit = leaf["value"], leaf["unit"].strip()
    if not unit or style == "text":
        return value
    if _is_url(unit):
        return {"@type": "sc:DefinedTerm", "name": value, "url": unit}
    if not _is_curie(unit):
        warnings.append(_msg(path, "keyword unit should be a CURIE or IRI"))
    return {"@type": "sc:DefinedTerm", "name": value, "termCode": unit}


def _field_id(field: Mapping, parent_id: str) -> str | None:
    fid = _lv(field.get("id"))
    if fid:
        return fid
    name = _lv(field.get("name"))
    return f"{parent_id}/{name}" if name else None


def _collect_field_ids(fields: list, parent_id: str, out: dict[str, str]) -> None:
    for f in fields:
        if not isinstance(f, Mapping):
            continue
        fid = _field_id(f, parent_id)
        if fid:
            out.setdefault(fid, parent_id)
            _collect_field_ids(_as_list(f.get("subField")), fid, out)


def _source_jsonld(field: Mapping, ids: dict[str, Any], default_source: str | None) -> Any:
    src = _lv(field.get("source")) or default_source
    extract: dict[str, Any] = {}
    for key in ("column", "jsonPath", "fileProperty"):
        v = _lv(field.get(key))
        if v is not None:
            extract[key] = v
    transforms = []
    # transform.jsonPath first: it selects the value the other transforms act on.
    for key in ("transformJsonPath", "regex", "separator"):
        v = _lv(field.get(key))
        if v is not None:
            transforms.append({"jsonPath": v} if key == "transformJsonPath" else {key: v})
    fmt = _lv(field.get("format"))
    if not (src or extract or transforms or fmt):
        return None
    out: dict[str, Any] = {}
    if src:
        if src in ids["dist"]:
            out[ids["dist"][src]] = {"@id": src}
        elif src in ids["rs"]:
            column = extract.get("column")
            if column and f"{src}/{column}" in ids["fields"]:
                # mlcroissant reads record-set sources as a reference to the field.
                out["field"] = {"@id": f"{src}/{column}"}
                extract.pop("column")
            else:
                out["recordSet"] = {"@id": src}
        elif src in ids["fields"]:
            out["field"] = {"@id": src}
        else:
            out["fileObject"] = {"@id": src}
    if extract:
        out["extract"] = extract
    if transforms:
        out["transform"] = _unbox(transforms)
    if fmt is not None:
        out["format"] = fmt
    return out


_EXTRACT_KEYS = ("column", "jsonPath", "fileProperty")
_SOURCE_KEYS = ("source", *_EXTRACT_KEYS, "transformJsonPath", "regex", "separator", "format")


def _field_jsonld(field: Mapping, parent_id: str, ids: dict[str, Any],
                  default_source: str | None, warnings: list, path: str,
                  parent: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One ``cr:Field``. ``parent`` is set for a subField: ``{"source", "column"}`` of
    the enclosing field (its own source, else the one it inherited)."""
    fid = _field_id(field, parent_id) or f"{parent_id}/"
    out: dict[str, Any] = {"@type": "cr:Field", "@id": fid}
    name = _lv(field.get("name"))
    if name is not None:
        out["name"] = name
    desc = _lv(field.get("description"))
    if desc is not None:
        out["description"] = desc
    dtypes = [_norm_datatype(v) for v in _lvs(field.get("dataType"))]
    if dtypes:
        out["dataType"] = _unbox(dtypes)
    is_array = _bool_text(_lv(field.get("isArray")))
    if is_array is not None:
        out["isArray"] = is_array
        if not isinstance(is_array, bool):
            warnings.append(_msg(path + ".isArray", "isArray must be true or false"))
    shape = _lv(field.get("arrayShape"))
    if shape is not None:
        out["arrayShape"] = shape
    equiv = _lvs(field.get("equivalentProperty"))
    if equiv:
        out["equivalentProperty"] = _unbox(equiv)
    ref = _lv(field.get("references"))
    if ref is not None:
        out["references"] = {"field": {"@id": ref}}
    subs = [s for s in _as_list(field.get("subField")) if isinstance(s, Mapping)]
    has_extract = any(_lv(field.get(k)) for k in _EXTRACT_KEYS)
    eff: Mapping = field
    if parent is None:
        default = default_source if has_extract or not subs else None
        inherited = default_source
    else:
        # A subField that says how to read it (an extract, a transform, a format or a
        # source) reads from its parent's source unless it names its own. A member
        # with only transform.jsonPath reads its parent's column too (a STRUCT member).
        # A member that says nothing gets no source (the validator warns).
        says_how = any(_lv(field.get(k)) for k in _SOURCE_KEYS)
        default = parent.get("source") if says_how else None
        inherited = parent.get("source")
        if _lv(field.get("transformJsonPath")) and not has_extract and parent.get("column"):
            eff = {**field, "column": _leaf(parent["column"])}
    source = _source_jsonld(eff, ids, default)
    if source is not None:
        out["source"] = source
    if subs:
        ctx = {"source": _lv(field.get("source")) or inherited,
               "column": _lv(eff.get("column"))}
        out["subField"] = [
            _field_jsonld(s, fid, ids, None, warnings, f"{path}.subField[{i}]", ctx)
            for i, s in enumerate(subs)
        ]
    return out


def _json_value(text: str, path: str, warnings: list, allow_url: bool) -> Any:
    if allow_url and _is_url(text):
        return text
    try:
        return json.loads(text)
    except ValueError:
        warnings.append(_msg(path, "not valid JSON; emitted as text"))
        return text


def _distribution_jsonld(entry: Mapping, base: str, warnings: list,
                         path: str) -> dict[str, Any]:
    kind = (_lv(entry.get("type")) or "FileObject").strip()
    kind_l = kind.lower().replace("cr:", "")
    if kind_l not in ("fileobject", "fileset"):
        warnings.append(_msg(path + ".type", f"unknown distribution type {kind!r}; "
                                             "FileObject used"))
        kind_l = "fileobject"
    is_set = kind_l == "fileset"
    did = _lv(entry.get("id")) or ""
    out: dict[str, Any] = {"@type": "cr:FileSet" if is_set else "cr:FileObject", "@id": did}
    out["name"] = _lv(entry.get("name")) or did
    desc = _lv(entry.get("description"))
    if desc is not None:
        out["description"] = desc
    contained = _lvs(entry.get("containedIn"))
    url = _lv(entry.get("contentUrl"))
    if url is not None and not is_set:
        out["contentUrl"] = url if contained else _resolve_url(url, base)
    elif url is not None:
        warnings.append(_msg(path + ".contentUrl", "contentUrl applies to FileObject only"))
    fmt = _lv(entry.get("encodingFormat"))
    if fmt is None:
        guess = None
        if not is_set and url:
            guess = encoding_format_for(url)
        elif is_set:
            for inc in _lvs(entry.get("includes")):
                guess = encoding_format_for(inc)
                if guess:
                    break
        fmt = guess
    if fmt is not None:
        out["encodingFormat"] = fmt
    size_leaf = _as_leaf(entry.get("contentSize"))
    if size_leaf and not is_set:
        out["contentSize"] = f"{size_leaf['value']} {size_leaf['unit'] or 'B'}"
    for key in ("sha256", "md5"):
        v = _lv(entry.get(key))
        if v is not None and not is_set:
            out[key] = v
    if contained:
        out["containedIn"] = _unbox([{"@id": c} for c in contained])
    for key in ("includes", "excludes"):
        vals = _lvs(entry.get(key))
        if vals and is_set:
            out[key] = _unbox(vals)
        elif vals:
            warnings.append(_msg(f"{path}.{key}", f"{key} applies to FileSet only"))
    return out


def _person_from_model(entry: Mapping, warnings: list, path: str) -> dict[str, Any] | None:
    name = _lv(entry.get("name"))
    if not name:
        warnings.append(_msg(path, "entry without a name is skipped"))
        return None
    kind = (_lv(entry.get("type")) or "Person").strip()
    org = kind.lower().replace("sc:", "") == "organization"
    if not org and kind.lower().replace("sc:", "") != "person":
        warnings.append(_msg(path + ".type", "type must be Person or Organization"))
    aff = _lv(entry.get("affiliation"))
    if aff and org:
        warnings.append(_msg(path + ".affiliation", "affiliation applies to Person only"))
    return _person_jsonld(name, org=org, email=_lv(entry.get("email")),
                          url=_lv(entry.get("url")), identifier=_lv(entry.get("identifier")),
                          affiliation=aff)


def _default_page(item: Mapping[str, Any]) -> str:
    kind = "folder" if item.get("is_folder", True) else "file"
    path = quote(str(item.get("path") or ""), safe="/")
    return f"{PORTAL_URL}/data/metadata/page/?path={path}&type={kind}"


def to_jsonld(avus: Iterable[Any], *, item: Mapping[str, Any]) -> dict[str, Any]:
    """Generate the Croissant JSON-LD document for an item from its AVUs.

    ``avus`` are *all* AVUs of the item (``croissant.*`` plus the ``datacite.*``,
    ``dc.*``, ontology, ``mesa.*`` AVUs the crosswalk reads). ``item`` describes the
    folder or file: ``path`` and ``name`` (required), ``is_folder`` (default True),
    ``url`` (landing page), ``content_base`` (URL that relative ``contentUrl``s are
    resolved against: the dataset folder, or a file's parent folder), ``size``,
    ``md5``, ``doi``; optional extras: ``public`` (bool, picks ``dav-anon`` over
    ``dav`` when ``content_base`` is absent) and ``keyword_style`` (``"text"`` emits
    DefinedTerm keywords as plain names, which mlcroissant 1.1.1 requires).

    Returns ``{"document": dict, "sources": {property: source}, "validation":
    validate(document) + AVU warnings}``. A source is ``"croissant"``, the AVU the
    crosswalk used (e.g. ``"datacite.title.1.value"``), ``"item.name"``,
    ``"item.doi"``, ``"item.url"``, ``"item"`` (file-level distribution),
    ``"default"`` or ``"auto"`` (conformsTo).
    """
    item = dict(item or {})
    all_avus = _iter_avus(avus)
    parsed = parse_avus(all_avus)
    m = _without_unknown(parsed["model"], _DATASET)
    warnings: list[dict[str, str]] = list(parsed["warnings"])
    cw = crosswalk(all_avus)
    sources: dict[str, str] = {}
    is_folder = bool(item.get("is_folder", True))
    style = str(item.get("keyword_style") or "defined_term")
    base = _content_base(item)

    conv = _lv(m.get("avuConvention"))
    if conv is not None and conv.strip() != str(CONVENTION_VERSION):
        warnings.append(_msg("croissant.avuConvention",
                             f"AVU convention {conv!r} is not supported by this version "
                             f"(supports {CONVENTION_VERSION})"))

    doc: dict[str, Any] = {"@context": copy.deepcopy(CONTEXT), "@type": "sc:Dataset"}

    def take(prop: str, value: Any, source: str) -> None:
        if value is None or value == [] or value == "":
            return
        doc[prop] = value
        sources[prop] = source

    def scalar(prop: str, key: str | None = None) -> None:
        v = _lv(m.get(key or prop))
        if v is not None:
            take(prop, v, "croissant")
        elif prop in cw:
            take(prop, cw[prop]["value"], cw[prop]["source"])

    # conformsTo
    user_conf = _lvs(m.get("conformsTo"))
    conf = [CONFORMS_CORE]
    if m.get("rai") or CONFORMS_RAI in user_conf:
        conf.append(CONFORMS_RAI)
    if m.get("geo") or CONFORMS_GEO in user_conf:
        conf.append(CONFORMS_GEO)
    conf += [c for c in user_conf if c not in conf]
    take("conformsTo", _unbox(conf), "croissant" if user_conf else "auto")

    # name
    v = _lv(m.get("name"))
    if v is not None:
        take("name", v, "croissant")
    elif "name" in cw:
        take("name", cw["name"]["value"], cw["name"]["source"])
    else:
        nm = item.get("name") or posixpath.basename(str(item.get("path") or "").rstrip("/"))
        take("name", nm or None, "item.name")
    scalar("alternateName")
    scalar("description")

    lic = _lvs(m.get("license"))
    if lic:
        take("license", _unbox(lic), "croissant")
    elif "license" in cw:
        take("license", cw["license"]["value"], cw["license"]["source"])

    # url
    doi = _doi(str(item.get("doi") or ""))
    v = _lv(m.get("url"))
    if v is not None:
        take("url", v, "croissant")
    elif doi:
        take("url", f"https://doi.org/{doi}", "item.doi")
    elif "url" in cw:
        take("url", cw["url"]["value"], cw["url"]["source"])
    elif item.get("url"):
        take("url", str(item["url"]), "item.url")
    else:
        take("url", _default_page(item), "default")

    v = _lv(m.get("identifier"))
    if v is not None:
        take("identifier", v, "croissant")
    elif doi:
        take("identifier", f"https://doi.org/{doi}", "item.doi")
    elif "identifier" in cw:
        take("identifier", cw["identifier"]["value"], cw["identifier"]["source"])

    for prop in ("creator", "publisher"):
        entries = [e for e in _as_list(m.get(prop)) if isinstance(e, Mapping)]
        if entries:
            people = [
                p for p in (
                    _person_from_model(e, warnings, f"croissant.{prop}.{i}")
                    for i, e in enumerate(entries, 1)
                ) if p
            ]
            take(prop, _unbox(people) if people else None, "croissant")
        elif prop in cw:
            take(prop, cw[prop]["value"], cw[prop]["source"])

    for prop in ("datePublished", "dateCreated", "dateModified", "version", "citeAs"):
        scalar(prop)

    kw_leaves = [lf for lf in (_as_leaf(k) for k in _as_list(m.get("keyword"))) if lf]
    if kw_leaves:
        take("keywords", [
            _keyword_jsonld(lf, warnings, f"croissant.keyword.{i}", style)
            for i, lf in enumerate(kw_leaves, 1)
        ], "croissant")
    elif "keywords" in cw:
        kws = cw["keywords"]["value"]
        if style == "text":
            kws = [k["name"] if isinstance(k, Mapping) else k for k in kws]
        take("keywords", kws, cw["keywords"]["source"])

    for prop in ("inLanguage", "sameAs"):
        vals = _lvs(m.get(prop))
        if vals:
            take(prop, _unbox(vals), "croissant")

    live = _bool_text(_lv(m.get("isLiveDataset")))
    if live is not None:
        take("isLiveDataset", live, "croissant")
        if not isinstance(live, bool):
            warnings.append(_msg("croissant.isLiveDataset", "must be true or false"))
    for prop in ("sdVersion", "sdLicense", "temporalCoverage"):
        scalar(prop)

    # GeoCroissant
    geo = m.get("geo") or {}
    sc = geo.get("spatialCoverage") or {}
    if sc:
        box = _lv(sc.get("box"))
        point = _lv(sc.get("point"))
        box_parts = _norm_box(box) if box else None
        point_parts = _norm_box(point) if point else None
        if box_parts is not None and len(box_parts) != 4:
            warnings.append(_msg("croissant.geo.spatialCoverage.box",
                                 "box must be four numbers 'S W N E'"))
        place = _place_jsonld(_lv(sc.get("name")), box_parts, point_parts)
        if box_parts is not None and len(box_parts) != 4:
            place.setdefault("geo", {"@type": "sc:GeoShape", "box": box})
        if point_parts is not None and "geo" not in place and len(point_parts) != 2:
            warnings.append(_msg("croissant.geo.spatialCoverage.point",
                                 "point must be two numbers 'lat lon'"))
        take("spatialCoverage", place, "croissant")
    elif "spatialCoverage" in cw:
        take("spatialCoverage", cw["spatialCoverage"]["value"], cw["spatialCoverage"]["source"])
    v = _lv(geo.get("coordinateReferenceSystem"))
    if v is not None:
        take("geocr:coordinateReferenceSystem", v, "croissant")
    for key in ("spatialResolution", "temporalResolution"):
        q = _quantity(geo.get(key))
        if q is not None:
            take(f"geocr:{key}", q, "croissant")
    for key in ("samplingStrategy", "spatialBias", "recordEndpoint"):
        v = _lv(geo.get(key))
        if v is not None:
            take(f"geocr:{key}", v, "croissant")
    bands = geo.get("bandConfiguration") or {}
    if bands:
        bc: dict[str, Any] = {"@type": "geocr:BandConfiguration"}
        total = _lv(bands.get("totalBands"))
        if total is not None:
            num = _number(total)
            bc["geocr:totalBands"] = num if isinstance(num, int) else total
            if not isinstance(num, int):
                warnings.append(_msg("croissant.geo.bandConfiguration.totalBands",
                                     "totalBands must be an integer"))
        names = _lvs(bands.get("band"))
        if names:
            bc["geocr:bandNameList"] = names
        take("geocr:bandConfiguration", bc, "croissant")

    # RAI
    rai = m.get("rai") or {}
    for prop, many in RAI_PROPERTIES.items():
        if prop not in rai:
            continue
        vals = _lvs(rai[prop])
        if vals:
            take(f"rai:{prop}", _unbox(vals) if many else vals[0], "croissant")

    # provenance
    v = _lv(m.get("wasGeneratedBy"))
    if v is not None:
        take("prov:wasGeneratedBy", _prov_obj(v, "activity"), "croissant")
    elif "prov:wasGeneratedBy" in cw:
        take("prov:wasGeneratedBy", cw["prov:wasGeneratedBy"]["value"],
             cw["prov:wasGeneratedBy"]["source"])
    vals = _lvs(m.get("wasDerivedFrom"))
    if vals:
        take("prov:wasDerivedFrom", _unbox([_prov_obj(d, "entity") for d in vals]), "croissant")

    # distribution
    dist_entries = [e for e in _as_list(m.get("distribution")) if isinstance(e, Mapping)]
    distribution: list[dict[str, Any]] = []
    default_source = None
    if not is_folder:
        fm = m.get("file") or {}
        fname = str(item.get("name") or posixpath.basename(str(item.get("path") or "")))
        # @id = the file name; whitespace (rejected by mlcroissant) becomes "_".
        fid = _safe_id(fname) or "file"
        fo: dict[str, Any] = {"@type": "cr:FileObject", "@id": fid,
                              "name": _lv(fm.get("name")) or fname}
        desc = _lv(fm.get("description"))
        if desc is not None:
            fo["description"] = desc
        fo["contentUrl"] = _resolve_url(fname, base)
        fmt = _lv(fm.get("encodingFormat")) or encoding_format_for(fname)
        if fmt:
            fo["encodingFormat"] = fmt
        if item.get("size") not in (None, ""):
            fo["contentSize"] = f"{_text(item['size'])} B"
        sha = _lv(fm.get("sha256"))
        if sha:
            fo["sha256"] = sha
        md5 = _lv(fm.get("md5")) or (str(item["md5"]) if item.get("md5") else None)
        if md5:
            fo["md5"] = md5
        distribution.append(fo)
        default_source = fid
        if dist_entries:
            warnings.append(_msg("croissant.distribution",
                                 "a document generated for a file describes only that file; "
                                 "croissant.distribution.* on the file is ignored"))
        sources["distribution"] = "item"
    else:
        distribution = [
            _distribution_jsonld(e, base, warnings, f"croissant.distribution.{i}")
            for i, e in enumerate(dist_entries, 1)
        ]
        if distribution:
            sources["distribution"] = "croissant"
    doc["distribution"] = distribution

    # record sets
    rs_entries = [e for e in _as_list(m.get("recordSet")) if isinstance(e, Mapping)]
    ids: dict[str, Any] = {
        "dist": {d["@id"]: ("fileSet" if d["@type"] == "cr:FileSet" else "fileObject")
                 for d in distribution if d.get("@id")},
        "rs": set(),
        "fields": {},
    }
    for rs in rs_entries:
        rid = _lv(rs.get("id"))
        if rid:
            ids["rs"].add(rid)
            _collect_field_ids(_as_list(rs.get("field")), rid, ids["fields"])
    record_sets = []
    for i, rs in enumerate(rs_entries, 1):
        path = f"croissant.recordSet.{i}"
        rid = _lv(rs.get("id")) or ""
        out: dict[str, Any] = {"@type": "cr:RecordSet", "@id": rid,
                               "name": _lv(rs.get("name")) or rid}
        desc = _lv(rs.get("description"))
        if desc is not None:
            out["description"] = desc
        dt = _lv(rs.get("dataType"))
        if dt is not None:
            out["dataType"] = _norm_datatype(dt)
        keys = _lvs(rs.get("key"))
        if keys:
            out["key"] = _unbox([{"@id": k} for k in keys])
        fields = [f for f in _as_list(rs.get("field")) if isinstance(f, Mapping)]
        out["field"] = [
            _field_jsonld(f, rid, ids, default_source, warnings, f"{path}.field.{j}")
            for j, f in enumerate(fields, 1)
        ]
        data = _lv(rs.get("data"))
        if data is not None:
            out["data"] = _json_value(data, f"{path}.data", warnings, False)
        ex = _lv(rs.get("examples"))
        if ex is not None:
            out["examples"] = _json_value(ex, f"{path}.examples", warnings, True)
        record_sets.append(out)
    if record_sets:
        doc["recordSet"] = record_sets
        sources["recordSet"] = "croissant"

    validation = validate(doc)
    validation["warnings"] = warnings + validation["warnings"]
    return {"document": doc, "sources": sources, "validation": validation}


# ---------------------------------------------------------------------------
# JSON-LD normalisation (shared by the importer and the validator)
# ---------------------------------------------------------------------------

_NS_IRIS = (
    "http://schema.org/",
    "https://schema.org/",
    "http://mlcommons.org/croissant/RAI/",
    "http://mlcommons.org/croissant/geo/1.0/",
    "http://mlcommons.org/croissant/geo/",
    "http://mlcommons.org/croissant/",
    "http://mlcommons.org/schema/",
    "http://www.w3.org/ns/prov#",
    "http://purl.org/dc/terms/",
)
_NS_PREFIXES = ("sc", "schema", "cr", "ml", "rai", "geocr", "prov", "dct")


def _local(key: str) -> str:
    if not isinstance(key, str) or key.startswith("@"):
        return key
    for iri in _NS_IRIS:
        if key.startswith(iri):
            return key[len(iri):]
    if ":" in key:
        pfx, rest = key.split(":", 1)
        if pfx in _NS_PREFIXES and rest and not rest.startswith("//"):
            return rest
    return key


def _n(node: Any) -> dict[str, Any]:
    """One level of key normalisation: prefixed / full-IRI keys -> local names."""
    if not isinstance(node, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in node.items():
        lk = _local(k)
        if lk not in out:
            out[lk] = v
    return out


def _types(node: Any) -> set[str]:
    if not isinstance(node, Mapping):
        return set()
    t = node.get("@type")
    return {_local(x) for x in _as_list(t) if isinstance(x, str)}


def _vals(value: Any) -> list[Any]:
    out = []
    for v in _as_list(value):
        if isinstance(v, Mapping) and "@value" in v:
            v = v["@value"]
        if v is None or v == "":
            continue
        out.append(v)
    return out


def _ref_id(value: Any) -> str | None:
    """``{"@id": x}``, ``{"field": {"@id": x}}``, or a plain string -> ``x``."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        if isinstance(value.get("@id"), str):
            return value["@id"]
        nv = _n(value)
        for key in ("field", "fileObject", "fileSet", "recordSet", "distribution"):
            if key in nv:
                return _ref_id(nv[key])
    return None


def _str_value(value: Any) -> str | None:
    if isinstance(value, Mapping):
        if "@value" in value:
            return _text(value["@value"])
        return None
    if isinstance(value, str | int | float | bool):
        return _text(value)
    return None


# ---------------------------------------------------------------------------
# Importer: JSON-LD -> AVUs
# ---------------------------------------------------------------------------

_DATASET_HANDLED = {
    "@context", "@type", "@id", "conformsTo", "name", "alternateName", "description", "url",
    "license", "creator", "publisher", "datePublished", "dateCreated", "dateModified",
    "version", "citeAs", "citation", "keywords", "inLanguage", "sameAs", "identifier",
    "isLiveDataset", "sdVersion", "sdLicense", "temporalCoverage", "spatialCoverage",
    "wasGeneratedBy", "wasDerivedFrom", "distribution", "recordSet",
    "coordinateReferenceSystem", "spatialResolution", "temporalResolution",
    "samplingStrategy", "spatialBias", "recordEndpoint", "bandConfiguration",
    *RAI_PROPERTIES, *_RAI_ALIASES,
}
_AUTO_CONFORMS = {
    CONFORMS_CORE, CONFORMS_RAI, CONFORMS_GEO,
    "http://mlcommons.org/croissant/1.0", "http://mlcommons.org/croissant/0.8",
    "http://mlcommons.org/croissant/RAI/1.0/", "http://mlcommons.org/croissant/geo/1.0/",
}


class _Importer:
    def __init__(self, content_base: str | None):
        self.warnings: list[dict[str, str]] = []
        self.errors: list[dict[str, str]] = []
        self.base = content_base.rstrip("/") + "/" if content_base else None

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(_msg(path, message))

    def one(self, value: Any, path: str, what: str = "value") -> str | None:
        strs = [_str_value(v) for v in _vals(value)]
        vals = [s for s in strs if s is not None and s.strip() != ""]
        if not vals:
            if any(s is not None and s != "" for s in strs):
                self.warn(path, "blank value ignored")
            elif _vals(value):
                self.warn(path, f"unsupported {what}; dropped")
            return None
        if len(vals) > 1:
            self.warn(path, "several values for a single-valued property; the first is kept")
        return vals[0]

    def many(self, value: Any, path: str) -> list[str]:
        out = []
        for i, v in enumerate(_vals(value)):
            s = _str_value(v)
            if s is None and isinstance(v, Mapping):
                s = _ref_id(v) or (_text(_n(v).get("url")) if _n(v).get("url") else None)
            if s in (None, ""):
                self.warn(f"{path}[{i}]", "unsupported value; dropped")
                continue
            if s.strip() == "":
                self.warn(f"{path}[{i}]", "blank value ignored")
                continue
            out.append(s)
        return out

    def relative_url(self, url: str) -> str:
        """A contentUrl as the generator would write its AVU.

        Below ``content_base`` it becomes the decoded relative path (the generator
        percent-encodes it again), but only when regenerating gives back exactly the
        same URL; otherwise the absolute URL is kept. A relative, percent-encoded URL
        is decoded under the same rule."""
        if self.base and url.startswith(self.base):
            rel = unquote(url[len(self.base):])
            if rel and _resolve_url(rel, self.base) == url:
                return rel
            return url
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url) and not url.startswith("/"):
            rel = unquote(url)
            if rel != url and quote(rel, safe="/") == url:
                return rel
        return url

    # -- persons ---------------------------------------------------------
    def person(self, value: Any, path: str, default_org: bool) -> dict[str, Any] | None:
        if isinstance(value, str):
            m = {"name": _leaf(value)}
            if default_org:
                m["type"] = _leaf("Organization")
            return m
        if not isinstance(value, Mapping):
            self.warn(path, "unsupported person/organization; dropped")
            return None
        nv = _n(value)
        types = _types(value)
        org = "Organization" in types or (default_org and "Person" not in types)
        name = self.one(nv.get("name"), path + ".name")
        if not name:
            given = self.one(nv.get("givenName"), path + ".givenName")
            family = self.one(nv.get("familyName"), path + ".familyName")
            name = " ".join(x for x in (given, family) if x) or None
        if not name:
            self.warn(path, "person/organization without a name; dropped")
            return None
        m: dict[str, Any] = {"name": _leaf(name)}
        if org:
            m["type"] = _leaf("Organization")
        email = self.one(nv.get("email"), path + ".email")
        if email:
            m["email"] = _leaf(email)
        url = self.one(nv.get("url"), path + ".url")
        if url:
            m["url"] = _leaf(url)
        ident = None
        for v in _vals(nv.get("identifier")):
            if isinstance(v, Mapping):
                v = _n(v).get("value") or v.get("@id")
            if isinstance(v, str) and v:
                ident = v
                break
        if not ident:
            for v in _vals(nv.get("sameAs")):
                if isinstance(v, str) and ("orcid.org/" in v or "ror.org/" in v):
                    ident = v
                    break
        if ident:
            m["identifier"] = _leaf(ident)
        affs = _vals(nv.get("affiliation"))
        if affs and not org:
            a = affs[0]
            aname = a if isinstance(a, str) else self.one(_n(a).get("name"),
                                                           path + ".affiliation.name")
            if aname:
                m["affiliation"] = _leaf(aname)
            if len(affs) > 1:
                self.warn(path + ".affiliation", "only the first affiliation is kept")
        return m

    # -- distribution ----------------------------------------------------
    def distribution(self, value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            self.warn(path, "unsupported distribution entry; dropped")
            return None
        nv = _n(value)
        types = _types(value)
        is_set = "FileSet" in types
        if not (is_set or "FileObject" in types or "DataDownload" in types):
            self.warn(path + ".@type", "unknown distribution type; read as FileObject")
        m: dict[str, Any] = {}
        did = value.get("@id") if isinstance(value.get("@id"), str) else None
        name = self.one(nv.get("name"), path + ".name")
        did = did or name
        if not did:
            self.warn(path, "distribution entry without @id or name; dropped")
            return None
        m["id"] = _leaf(did)
        if is_set:
            m["type"] = _leaf("FileSet")
        if name and name != did:
            m["name"] = _leaf(name)
        desc = self.one(nv.get("description"), path + ".description")
        if desc:
            m["description"] = _leaf(desc)
        url = self.one(nv.get("contentUrl"), path + ".contentUrl")
        if url:
            if "containedIn" not in nv:  # a path inside an archive is kept verbatim
                url = self.relative_url(url)
            m["contentUrl"] = _leaf(url)
        fmt = self.one(nv.get("encodingFormat"), path + ".encodingFormat")
        if fmt:
            m["encodingFormat"] = _leaf(fmt)
        size = self.one(nv.get("contentSize"), path + ".contentSize")
        if size:
            sm = re.match(r"^\s*([0-9.]+)\s*([A-Za-z]*)\s*$", size)
            if sm:
                unit = sm.group(2)
                m["contentSize"] = _leaf(sm.group(1), "" if unit == "B" else unit)
            else:
                m["contentSize"] = _leaf(size)
        for key in ("sha256", "md5"):
            v = self.one(nv.get(key), f"{path}.{key}")
            if v:
                m[key] = _leaf(v)
        contained = [c for c in (_ref_id(v) for v in _vals(nv.get("containedIn"))) if c]
        if contained:
            m["containedIn"] = [_leaf(c) for c in contained]
        for key in ("includes", "excludes"):
            vals = self.many(nv.get(key), f"{path}.{key}")
            if vals:
                m[key] = [_leaf(v) for v in vals]
        for key in sorted(set(nv) - {"@type", "@id", "name", "description", "contentUrl",
                                     "encodingFormat", "contentSize", "sha256", "md5",
                                     "containedIn", "includes", "excludes"}):
            self.warn(f"{path}.{key}", "property not represented in the AVU convention; dropped")
        return m

    # -- fields ----------------------------------------------------------
    def field(self, value: Any, parent_id: str, path: str,
              depth: int) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            self.warn(path, "unsupported field; dropped")
            return None
        nv = _n(value)
        fid = value.get("@id") if isinstance(value.get("@id"), str) else None
        name = self.one(nv.get("name"), path + ".name")
        if not name and fid:
            name = fid[len(parent_id) + 1:] if fid.startswith(parent_id + "/") else \
                fid.rsplit("/", 1)[-1]
        if not name:
            self.warn(path, "field without name or @id; dropped")
            return None
        m: dict[str, Any] = {"name": _leaf(name)}
        if fid and fid != f"{parent_id}/{name}":
            m["id"] = _leaf(fid)
        desc = self.one(nv.get("description"), path + ".description")
        if desc:
            m["description"] = _leaf(desc)
        dts = [_norm_datatype(d) for d in self.many(nv.get("dataType"), path + ".dataType")]
        if dts:
            m["dataType"] = [_leaf(d) for d in dts]
        self.source(nv.get("source"), m, path + ".source")
        refs = [r for r in (_ref_id(v) for v in _vals(nv.get("references"))) if r]
        if refs:
            m["references"] = _leaf(refs[0])
            if len(refs) > 1:
                self.warn(path + ".references", "only the first reference is kept")
        is_array = None
        for key in ("isArray", "repeated"):
            v = _vals(nv.get(key))
            if v:
                is_array = v[0]
                break
        if is_array is not None:
            m["isArray"] = _leaf(_text(is_array).lower() if isinstance(is_array, str)
                                 else is_array)
        shape = nv.get("arrayShape")
        if isinstance(shape, list):
            shape = ",".join(_text(x) for x in shape) if shape else None
        elif isinstance(shape, Mapping):
            shape = _str_value(shape)
        if shape not in (None, ""):
            m["arrayShape"] = _leaf(re.sub(r"\s+", "", _text(shape)).strip("[]()"))
        equiv = self.many(nv.get("equivalentProperty"), path + ".equivalentProperty")
        if equiv:
            m["equivalentProperty"] = [_leaf(e) for e in equiv]
        subs = _vals(nv.get("subField"))
        if subs:
            if depth >= 1:
                self.warn(path + ".subField",
                          "nested subFields deeper than one level are not written; dropped")
            else:
                own_id = fid or f"{parent_id}/{name}"
                out = [self.field(s, own_id, f"{path}.subField[{i}]", depth + 1)
                       for i, s in enumerate(subs)]
                out = [s for s in out if s]
                if out:
                    m["subField"] = out
        for key in sorted(set(nv) - {"@type", "@id", "name", "description", "dataType",
                                     "source", "references", "isArray", "repeated",
                                     "arrayShape", "equivalentProperty", "subField"}):
            self.warn(f"{path}.{key}", "property not represented in the AVU convention; dropped")
        return m

    def source(self, value: Any, m: dict[str, Any], path: str) -> None:
        if value is None:
            return
        if isinstance(value, str):
            m["source"] = _leaf(value)
            return
        if isinstance(value, list):
            if len(value) > 1:
                self.warn(path, "several sources; the first is kept")
            value = value[0] if value else None
        if not isinstance(value, Mapping):
            return
        nv = _n(value)
        src = None
        if isinstance(value.get("@id"), str):
            src = value["@id"]
        for key in ("fileObject", "fileSet", "recordSet", "field", "distribution"):
            if key in nv:
                src = _ref_id(nv[key])
                break
        if src:
            m["source"] = _leaf(src)
        extract = _n(nv.get("extract")) if isinstance(nv.get("extract"), Mapping) else {}
        for key in ("column", "jsonPath", "fileProperty"):
            v = self.one(extract.get(key), f"{path}.extract.{key}")
            if v:
                m[key] = _leaf(v)
        for key in sorted(set(extract) - {"column", "jsonPath", "fileProperty"}):
            self.warn(f"{path}.extract.{key}", "extract property not supported; dropped")
        for i, t in enumerate(_vals(nv.get("transform"))):
            tn = _n(t)
            for key, v in tn.items():
                tpath = f"{path}.transform[{i}].{key}"
                if key in ("regex", "separator", "delimiter"):
                    target = "separator" if key == "delimiter" else key
                    sv = _str_value(v)
                    if target in m:
                        self.warn(tpath, f"several {target} transforms; the first is kept")
                    elif sv is not None:
                        m[target] = _leaf(sv)
                elif key == "jsonQuery" and "jsonPath" not in m and "column" not in m:
                    # Croissant 1.0: a jsonQuery on a whole file is the 1.1 extract.jsonPath.
                    sv = _str_value(v)
                    if sv is not None:
                        m["jsonPath"] = _leaf(sv)
                elif key in ("jsonQuery", "jsonPath"):
                    sv = _str_value(v)
                    if "transformJsonPath" in m:
                        self.warn(tpath, "several jsonPath transforms; the first is kept")
                    elif sv is not None:
                        m["transformJsonPath"] = _leaf(sv)
                else:
                    self.warn(tpath, "transform not represented in the AVU convention; "
                                     "dropped")
        fmt = self.one(nv.get("format"), path + ".format")
        if fmt:
            m["format"] = _leaf(fmt)

    # -- record sets -----------------------------------------------------
    def record_set(self, value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            self.warn(path, "unsupported record set; dropped")
            return None
        nv = _n(value)
        rid = value.get("@id") if isinstance(value.get("@id"), str) else None
        name = self.one(nv.get("name"), path + ".name")
        rid = rid or name
        if not rid:
            self.warn(path, "record set without @id or name; dropped")
            return None
        m: dict[str, Any] = {"id": _leaf(rid)}
        if name and name != rid:
            m["name"] = _leaf(name)
        desc = self.one(nv.get("description"), path + ".description")
        if desc:
            m["description"] = _leaf(desc)
        dts = self.many(nv.get("dataType"), path + ".dataType")
        if dts:
            m["dataType"] = _leaf(_norm_datatype(dts[0]))
            if len(dts) > 1:
                self.warn(path + ".dataType", "only the first record-set dataType is kept")
        elif _vals(nv.get("isEnumeration")) and _vals(nv.get("isEnumeration"))[0] is True:
            m["dataType"] = _leaf("sc:Enumeration")
        keys = []
        for k in _vals(nv.get("key")):
            kid = _ref_id(k)
            if kid:
                keys.append(kid if "/" in kid or isinstance(k, Mapping) else f"{rid}/{kid}")
        if keys:
            m["key"] = [_leaf(k) for k in keys]
        for key in ("data", "examples"):
            if key in nv and nv[key] not in (None, "", []):
                v = nv[key]
                text = v if (key == "examples" and isinstance(v, str)) else \
                    json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                m[key] = _leaf(text)
        fields = []
        for i, f in enumerate(_vals(nv.get("field"))):
            fm = self.field(f, rid, f"{path}.field[{i}]", 0)
            if fm:
                fields.append(fm)
        if fields:
            m["field"] = fields
        for key in sorted(set(nv) - {"@type", "@id", "name", "description", "dataType",
                                     "isEnumeration", "key", "data", "examples", "field"}):
            self.warn(f"{path}.{key}", "property not represented in the AVU convention; dropped")
        return m

    # -- dataset ---------------------------------------------------------
    def dataset(self, doc: Mapping[str, Any]) -> dict[str, Any]:
        nv = _n(doc)
        m: dict[str, Any] = {}
        conf = [c for c in self.many(nv.get("conformsTo"), "conformsTo")
                if c not in _AUTO_CONFORMS]
        if conf:
            m["conformsTo"] = [_leaf(c) for c in conf]
        for prop in ("name", "alternateName", "description", "url"):
            v = self.one(nv.get(prop), prop)
            if v is not None:
                m[prop] = _leaf(v)
        lic = []
        for i, v in enumerate(_vals(nv.get("license"))):
            if isinstance(v, Mapping):
                lv = _n(v)
                v = lv.get("url") or v.get("@id") or lv.get("name")
            s = _str_value(v)
            if s:
                lic.append(s)
            else:
                self.warn(f"license[{i}]", "unsupported license value; dropped")
        if lic:
            m["license"] = [_leaf(v) for v in lic]
        for prop in ("creator", "publisher"):
            people = [self.person(p, f"{prop}[{i}]", prop == "publisher")
                      for i, p in enumerate(_vals(nv.get(prop)))]
            people = [p for p in people if p]
            if people:
                m[prop] = people
        for prop in ("datePublished", "dateCreated", "dateModified", "version"):
            v = self.one(nv.get(prop), prop)
            if v is not None:
                m[prop] = _leaf(v)
        cite = self.one(nv.get("citeAs"), "citeAs") or self.one(nv.get("citation"), "citation")
        if cite:
            m["citeAs"] = _leaf(cite)
        kws = []
        for i, k in enumerate(_vals(nv.get("keywords"))):
            if isinstance(k, Mapping):
                kn = _n(k)
                name = self.one(kn.get("name"), f"keywords[{i}].name")
                unit = self.one(kn.get("termCode"), f"keywords[{i}].termCode") or \
                    self.one(kn.get("url"), f"keywords[{i}].url") or \
                    (k.get("@id") if isinstance(k.get("@id"), str) else "")
                if name:
                    kws.append(_leaf(name, unit or ""))
                else:
                    self.warn(f"keywords[{i}]", "keyword without a name; dropped")
            else:
                s = _str_value(k)
                if s:
                    kws.append(_leaf(s))
        if kws:
            m["keyword"] = kws
        for prop in ("inLanguage", "sameAs"):
            vals = self.many(nv.get(prop), prop)
            if vals:
                m[prop] = [_leaf(v) for v in vals]
        ident = None
        for v in _vals(nv.get("identifier")):
            if isinstance(v, Mapping):
                v = _n(v).get("value") or _n(v).get("url") or v.get("@id")
            if isinstance(v, str) and v:
                ident = v
                break
        if ident:
            m["identifier"] = _leaf(ident)
        live = _vals(nv.get("isLiveDataset"))
        if live:
            m["isLiveDataset"] = _leaf(_text(live[0]).lower())
        for prop in ("sdVersion", "sdLicense", "temporalCoverage"):
            v = self.one(nv.get(prop), prop)
            if v is not None:
                m[prop] = _leaf(v)
        gen = self.prov_text(nv.get("wasGeneratedBy"), "prov:wasGeneratedBy")
        if gen:
            m["wasGeneratedBy"] = _leaf(gen[0])
            if len(gen) > 1:
                self.warn("prov:wasGeneratedBy", "only the first activity is kept")
        der = self.prov_text(nv.get("wasDerivedFrom"), "prov:wasDerivedFrom")
        if der:
            m["wasDerivedFrom"] = [_leaf(d) for d in der]
        rai = self.rai(nv)
        if rai:
            m["rai"] = rai
        geo = self.geo(nv)
        if geo:
            m["geo"] = geo
        dist = [self.distribution(d, f"distribution[{i}]")
                for i, d in enumerate(_vals(nv.get("distribution")))]
        dist = [d for d in dist if d]
        if dist:
            m["distribution"] = dist
        rss = [self.record_set(r, f"recordSet[{i}]")
               for i, r in enumerate(_vals(nv.get("recordSet")))]
        rss = [r for r in rss if r]
        if rss:
            m["recordSet"] = rss
        for key in sorted(set(nv) - _DATASET_HANDLED):
            self.warn(key, "property not represented in the AVU convention; dropped")
        return m

    def prov_text(self, value: Any, path: str) -> list[str]:
        out = []
        for i, v in enumerate(_vals(value)):
            if isinstance(v, str):
                out.append(v)
                continue
            if not isinstance(v, Mapping):
                continue
            nv = _n(v)
            text = None
            for key in ("locatedAt", "url", "description", "label", "name"):
                text = _str_value(nv.get(key)) if not isinstance(nv.get(key), list) else \
                    _str_value(nv[key][0]) if nv[key] else None
                if text:
                    break
            text = text or (v.get("@id") if isinstance(v.get("@id"), str) else None)
            if text:
                out.append(text)
                # {"@id"} and a labelled prov:Activity / prov:Entity (what the generator
                # writes) map to the AVU without loss; anything richer is reduced.
                rest = {_local(k) for k in v} - {"@type"}
                type_ok = "@type" not in v or (bool(_types(v))
                                               and _types(v) <= {"Activity", "Entity"})
                lossless = type_ok and (
                    rest == {"@id"} and isinstance(v.get("@id"), str)
                    or rest == {"label"} and isinstance(nv.get("label"), str))
                if not lossless:
                    self.warn(f"{path}[{i}]", "provenance object reduced to text")
        return out

    def rai(self, nv: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in nv.items():
            prop = _RAI_ALIASES.get(key, key)
            if prop not in RAI_PROPERTIES or prop in out:
                continue
            vals = self.many(value, f"rai:{key}")
            if not vals:
                continue
            if RAI_PROPERTIES[prop]:
                out[prop] = [_leaf(v) for v in vals]
            else:
                if len(vals) > 1:
                    self.warn(f"rai:{key}", "several values for a single-valued RAI property; "
                                            "joined with blank lines")
                out[prop] = _leaf("\n\n".join(vals))
        return {k: out[k] for k in RAI_PROPERTIES if k in out}

    def quantity(self, value: Any, path: str) -> dict[str, str] | None:
        vals = _vals(value)
        if not vals:
            return None
        v = vals[0]
        if isinstance(v, Mapping):
            qn = _n(v)
            num = _str_value(qn.get("value"))
            unit = _str_value(qn.get("unitText")) or _str_value(qn.get("unitCode")) or ""
            if num is None:
                self.warn(path, "QuantitativeValue without a value; dropped")
                return None
            return _leaf(num, unit)
        s = _str_value(v)
        if s is None:
            return None
        sm = re.match(r"^\s*([+-]?[0-9.]+)\s*([^\d\s].*)?$", s)
        if sm and _number(sm.group(1)) is not None:
            return _leaf(sm.group(1), (sm.group(2) or "").strip())
        return _leaf(s)

    def geo(self, nv: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        scs = _vals(nv.get("spatialCoverage"))
        if scs:
            if len(scs) > 1:
                self.warn("spatialCoverage", "only the first spatialCoverage is kept")
            sc = scs[0]
            scm: dict[str, Any] = {}
            if isinstance(sc, str):
                scm["name"] = _leaf(sc)
            elif isinstance(sc, Mapping):
                sn = _n(sc)
                name = self.one(sn.get("name"), "spatialCoverage.name")
                if name:
                    scm["name"] = _leaf(name)
                geos = _vals(sn.get("geo"))
                if not geos and _types(sc) & {"GeoShape", "GeoCoordinates"}:
                    geos = [sc]
                for g in geos:
                    gn = _n(g)
                    if "box" in gn and "box" not in scm:
                        box = _str_value(_vals(gn["box"])[0]) if _vals(gn["box"]) else None
                        if box:
                            scm["box"] = _leaf(" ".join(_norm_box(box)))
                    elif "latitude" in gn and "point" not in scm:
                        lat = _str_value(_vals(gn.get("latitude"))[0]) \
                            if _vals(gn.get("latitude")) else None
                        lon = _str_value(_vals(gn.get("longitude"))[0]) \
                            if _vals(gn.get("longitude")) else None
                        if lat and lon:
                            scm["point"] = _leaf(f"{lat} {lon}")
                    else:
                        self.warn("spatialCoverage.geo", "unsupported geometry; dropped")
            if scm:
                out["spatialCoverage"] = {k: scm[k] for k in ("name", "box", "point")
                                          if k in scm}
        crs = self.one(nv.get("coordinateReferenceSystem"), "geocr:coordinateReferenceSystem")
        if crs:
            out["coordinateReferenceSystem"] = _leaf(crs)
        for key in ("spatialResolution", "temporalResolution"):
            q = self.quantity(nv.get(key), f"geocr:{key}")
            if q:
                out[key] = q
        for key in ("samplingStrategy", "spatialBias", "recordEndpoint"):
            v = self.one(nv.get(key), f"geocr:{key}")
            if v:
                out[key] = _leaf(v)
        bcs = _vals(nv.get("bandConfiguration"))
        if bcs and isinstance(bcs[0], Mapping):
            bn = _n(bcs[0])
            bc: dict[str, Any] = {}
            total = self.one(bn.get("totalBands"), "geocr:bandConfiguration.totalBands")
            if total:
                bc["totalBands"] = _leaf(total)
            names = self.many(bn.get("bandNameList"), "geocr:bandConfiguration.bandNameList")
            if names:
                bc["band"] = [_leaf(n) for n in names]
            if bc:
                out["bandConfiguration"] = bc
        return {k: out[k] for k in _GEO if k in out}


def _blank_attrs(schema: dict, node: Mapping, prefix: str, out: list[str]) -> None:
    """Attributes of the model's whitespace-only leaves (model_to_avus skips them)."""
    for name, spec in schema.items():
        if name not in node:
            continue
        value, kind = node[name], spec[0]
        if kind == "S":
            if _is_blank(value):
                out.append(prefix + name)
        elif kind == "L":
            for i, v in enumerate(_as_list(value), 1):
                if _is_blank(v):
                    out.append(f"{prefix}{name}.{i}")
        elif kind == "E":
            for i, e in enumerate(_as_list(value), 1):
                if isinstance(e, Mapping):
                    _blank_attrs(spec[1], e, f"{prefix}{name}.{i}.", out)
        elif isinstance(value, Mapping):
            _blank_attrs(spec[1], value, f"{prefix}{name}.", out)


def _drop_source(fields: list, ident: str) -> None:
    for f in fields:
        if isinstance(f, Mapping):
            if _lv(f.get("source")) == ident:
                f.pop("source")
            _drop_source(_as_list(f.get("subField")), ident)


def _file_from_distribution(model: dict[str, Any], item: Mapping[str, Any],
                            imp: _Importer) -> None:
    """A document generated for a file: its FileObject becomes ``croissant.file.*``.

    The generator makes the file the only FileObject (convention 5.1) and ignores
    ``croissant.distribution.*`` on a file, so the entry that describes the file is
    mapped to ``croissant.file.*`` (generated parts left out) and every other entry is
    reported and dropped."""
    entries = model.pop("distribution", None) or []
    if not entries:
        return
    name = str(item.get("name") or posixpath.basename(str(item.get("path") or "").rstrip("/")))
    fid = _safe_id(name) or "file"
    objects = [e for e in entries
               if (_lv(e.get("type")) or "FileObject").lower().replace("cr:", "") != "fileset"]

    def url_name(e: Mapping) -> str | None:
        url = _lv(e.get("contentUrl"))
        if url is None:
            return None
        return url if "/" not in url else unquote(url.rstrip("/").rsplit("/", 1)[-1])

    pick = next((e for e in objects if _lv(e.get("id")) == fid), None)
    pick = pick or next((e for e in objects if _lv(e.get("contentUrl")) == name), None)
    pick = pick or next((e for e in objects if url_name(e) == name), None)
    if pick is None and len(entries) == 1 and objects:
        pick = objects[0]
    for e in entries:
        if e is not pick:
            imp.warn("distribution", "a file's document describes only the file; "
                                     f"distribution entry {_lv(e.get('id'))!r} dropped")
    if pick is None:
        return
    fm: dict[str, Any] = {}
    shown = _lv(pick.get("name")) or _lv(pick.get("id"))
    if shown and shown not in (name, fid):
        fm["name"] = _leaf(shown)
    for key in ("description", "sha256"):
        if _lv(pick.get(key)):
            fm[key] = pick[key]
    fmt = _lv(pick.get("encodingFormat"))
    if fmt and fmt != encoding_format_for(name):
        fm["encodingFormat"] = pick["encodingFormat"]
    md5 = _lv(pick.get("md5"))
    if md5 and md5 != str(item.get("md5") or ""):
        fm["md5"] = pick["md5"]
    for key in ("containedIn", "includes", "excludes"):
        if pick.get(key):
            imp.warn(f"distribution.{key}", f"{key} does not apply to the file; dropped")
    if fm:
        model["file"] = {k: fm[k] for k in _FILE if k in fm}
    # Fields of a file default to the file: a source naming it is left out.
    for rs in _as_list(model.get("recordSet")):
        if isinstance(rs, Mapping):
            for ident in {_lv(pick.get("id")), fid} - {None}:
                _drop_source(_as_list(rs.get("field")), ident)


def from_jsonld(doc: Any, *, content_base: str | None = None,
                item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Import a Croissant 0.8 / 1.0 / 1.1 (+RAI, +Geo) document as ``croissant.*`` AVUs.

    ``doc`` is a dict or a JSON string; a ``@graph`` document is searched for its
    ``Dataset`` node. Keys may be compact (``name``), prefixed (``sc:``, ``schema:``,
    ``cr:``, ``ml:``, ``rai:``, ``geocr:``, ``prov:``, ``dct:``) or full IRIs (http
    or https schema.org). RAI terms in ``cr:`` (Hugging Face) and the mlcroissant
    spellings are accepted; 1.0 ``repeated`` / ``delimiter`` / ``jsonQuery`` become
    ``isArray`` / ``separator`` / ``jsonPath``. ``content_base`` (optional) turns
    ``contentUrl``s below that URL back into decoded relative paths (only when the
    generator would write the same URL again).

    ``item`` (optional, the shape :func:`to_jsonld` takes: ``path``, ``name``,
    ``is_folder``, ``md5``) names the target. ``content_base`` defaults to its folder (a
    file's parent). For a file (``is_folder`` false) the FileObject that describes the
    file becomes ``croissant.file.*`` (the entry whose ``@id`` is the file's id, else
    whose contentUrl is the file, else the only entry); generated parts (``@id``,
    ``contentUrl``, ``contentSize``, a default ``encodingFormat``, the stat ``md5``) are
    left out and every other distribution entry is reported as a warning.

    Whitespace-only values are left out with a warning (the portal refuses them).

    Returns ``{"avus": [...], "warnings": [...], "errors": [...]}``. Anything the
    convention cannot represent is reported as a warning. Values over the AVU limits
    (2,700 / 250 characters) are reported as errors and kept, not cut.
    """
    errors: list[dict[str, str]] = []
    if isinstance(doc, bytes | bytearray):
        doc = doc.decode("utf-8", "replace")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError as exc:
            return {"avus": [], "warnings": [], "errors": [_msg("", f"not valid JSON: {exc}")]}
    if isinstance(doc, Mapping) and "@graph" in doc and "Dataset" not in _types(doc):
        nodes = [n for n in _as_list(doc.get("@graph")) if "Dataset" in _types(n)]
        if nodes:
            doc = {"@context": doc.get("@context"), **nodes[0]}
    if not isinstance(doc, Mapping):
        return {"avus": [], "warnings": [], "errors": [_msg("", "document must be a JSON object")]}
    if content_base is None and item is not None:
        content_base = _content_base(item)
    imp = _Importer(content_base)
    if "Dataset" not in _types(doc):
        errors.append(_msg("@type", "the document is not a schema.org Dataset"))
        return {"avus": [], "warnings": [], "errors": errors}
    model = imp.dataset(doc)
    if item is not None and not item.get("is_folder", True):
        _file_from_distribution(model, item, imp)
    blank: list[str] = []
    _blank_attrs(_DATASET, model, PREFIX, blank)
    for attr in blank:
        imp.warn(attr, "blank value ignored")
    avus = model_to_avus(model)
    for avu in avus:
        if len(avu["value"]) > VALUE_MAX:
            errors.append(_msg(avu["attribute"],
                               f"value has {len(avu['value'])} characters; the limit is "
                               f"{VALUE_MAX}"))
        if len(avu["unit"]) > UNIT_MAX:
            errors.append(_msg(avu["attribute"],
                               f"unit has {len(avu['unit'])} characters; the limit is "
                               f"{UNIT_MAX}"))
        if "\x00" in avu["value"] or "\x00" in avu["unit"]:
            errors.append(_msg(avu["attribute"], "NUL characters cannot be stored"))
    return {"avus": avus, "warnings": imp.warnings, "errors": errors + imp.errors}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class _Validator:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []
        self.ids: dict[str, str] = {}
        self.dist_kind: dict[str, str] = {}
        self.rs_ids: set[str] = set()
        self.field_ids: set[str] = set()
        self.n_fields = 0
        self.pending: list[tuple[str, str | None, str]] = []

    def err(self, path: str, message: str) -> None:
        self.errors.append(_msg(path, message))

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(_msg(path, message))

    def register(self, ident: str | None, path: str) -> None:
        if not ident:
            return
        if any(c.isspace() for c in ident):
            self.err(path, f"@id {ident!r} contains whitespace (not allowed by mlcroissant)")
        if ident in self.ids:
            self.err(path, f"duplicate @id {ident!r} (also at {self.ids[ident]})")
        else:
            self.ids[ident] = path

    def long_strings(self, node: Any, path: str) -> None:
        if isinstance(node, str):
            if len(node) > VALUE_MAX:
                self.warn(path, f"value has {len(node)} characters; an AVU holds at most "
                                f"{VALUE_MAX}")
        elif isinstance(node, Mapping):
            for k, v in node.items():
                if k == "@context":
                    continue
                lk = _local(k)
                if lk in ("data", "examples") and not isinstance(v, str):
                    text = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                    if len(text) > VALUE_MAX:
                        self.warn(f"{path}.{lk}" if path else lk,
                                  f"inline JSON has {len(text)} characters; an AVU holds at "
                                  f"most {VALUE_MAX}")
                    continue
                self.long_strings(v, f"{path}.{lk}" if path else lk)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self.long_strings(v, f"{path}[{i}]")


def _missing(value: Any) -> bool:
    return not _vals(value)


def _check_box(v: _Validator, box: Any, path: str) -> None:
    if not isinstance(box, str):
        v.err(path, "box must be a string 'S W N E'")
        return
    parts = _norm_box(box)
    nums = [_number(p) for p in parts]
    if len(parts) != 4 or any(n is None for n in nums):
        v.err(path, "box must be four numbers 'S W N E' (lat lon lat lon)")
        return
    s, w, n, e = (float(x) for x in nums)  # type: ignore[arg-type]
    for lat, label in ((s, "south"), (n, "north")):
        if not -90 <= lat <= 90:
            v.err(path, f"{label} latitude {lat:g} is outside -90..90")
    for lon, label in ((w, "west"), (e, "east")):
        if not -180 <= lon <= 180:
            v.err(path, f"{label} longitude {lon:g} is outside -180..180")
    if s > n:
        v.err(path, "south latitude is greater than north latitude (expected 'S W N E')")
    if w > e:
        v.warn(path, "west longitude is greater than east longitude "
                     "(the box crosses the antimeridian)")


def _check_point(v: _Validator, g: Mapping, path: str) -> None:
    gn = _n(g)
    for key, lim in (("latitude", 90), ("longitude", 180)):
        vals = _vals(gn.get(key))
        num = None
        if vals:
            num = vals[0] if isinstance(vals[0], int | float) and not isinstance(vals[0], bool) \
                else _number(_text(vals[0]))
        if num is None:
            v.err(f"{path}.{key}", f"{key} must be a number")
        elif not -lim <= float(num) <= lim:
            v.err(f"{path}.{key}", f"{key} {num:g} is outside -{lim}..{lim}")


def _validate_field(v: _Validator, field: Any, rs_id: str, path: str, rs_has_data: bool,
                    sources: set[str], refs: list[tuple[str, str]],
                    parent_has_source: bool = False) -> None:
    if not isinstance(field, Mapping):
        v.err(path, "field must be an object")
        return
    v.n_fields += 1
    nf = _n(field)
    if "Field" not in _types(field):
        v.warn(path + ".@type", "field should have @type cr:Field")
    fid = field.get("@id") if isinstance(field.get("@id"), str) else None
    name = _str_value(_vals(nf.get("name"))[0]) if _vals(nf.get("name")) else None
    if not fid and not name:
        v.err(path, "field needs an @id or a name")
    subs = _vals(nf.get("subField"))
    dts = [d for d in _vals(nf.get("dataType")) if isinstance(d, str)]
    src = nf.get("source")
    from_field = isinstance(src, Mapping) and "field" in _n(src)
    if not dts and not subs:
        if from_field:
            v.warn(path + ".dataType", "no dataType; it is inherited from the source field")
        else:
            v.err(path + ".dataType", "field needs a dataType (unless it has subFields)")
    for i, d in enumerate(dts):
        nd = _norm_datatype(d)
        known = (nd.startswith("sc:") and nd[3:] in _SC_TYPES) or \
                (nd.startswith("cr:") and nd[3:] in _CR_TYPES)
        if known:
            continue
        if nd.startswith(("sc:", "cr:")) or ":" not in nd:
            p = f"{path}.dataType" + (f"[{i}]" if len(dts) > 1 else "")
            v.warn(p, f"{d!r} is not a known Croissant/schema.org data type")
    has_src = not (_missing(src) and "value" not in nf)
    if not has_src:
        if rs_has_data or subs:
            pass
        elif parent_has_source:
            v.warn(path + ".source", "no source: the member is read from its parent field's "
                                     "source and column; mlcroissant loads it only with its "
                                     "own source and transform.jsonPath (AVU "
                                     "transformJsonPath)")
        else:
            v.err(path + ".source", "field needs a source (unless its record set has inline "
                                    "data)")
    elif isinstance(src, Mapping) or isinstance(src, str):
        _validate_source(v, src, path + ".source", sources)
    for key in ("isArray",):
        vals = _vals(nf.get(key))
        if vals and not isinstance(vals[0], bool):
            v.warn(f"{path}.{key}", "isArray must be a boolean")
    shape = _vals(nf.get("arrayShape"))
    if shape and not (isinstance(shape[0], str) and _ARRAY_SHAPE_RE.match(shape[0])):
        v.warn(path + ".arrayShape", "arrayShape should be comma-separated integers, "
                                     "e.g. -1 or 512,512,6")
    if shape and _vals(nf.get("isArray"))[:1] != [True]:
        v.err(path + ".arrayShape", "arrayShape requires isArray true")
    for r in _vals(nf.get("references")):
        rid = _ref_id(r)
        if rid:
            refs.append((rid, path + ".references"))
    for i, s in enumerate(subs):
        _validate_field(v, s, rs_id, f"{path}.subField[{i}]", rs_has_data, sources, refs,
                        has_src or parent_has_source)


def _validate_source(v: _Validator, src: Any, path: str, sources: set[str]) -> None:
    if isinstance(src, str):
        ref, kind = src, None
    else:
        ns = _n(src)
        ref, kind = None, None
        for key in ("fileObject", "fileSet", "recordSet", "field", "distribution"):
            if key in ns:
                ref, kind = _ref_id(ns[key]), key
                break
        if ref is None and isinstance(src.get("@id"), str):
            ref = src["@id"]
        ex = ns.get("extract")
        if isinstance(ex, Mapping):
            fp = _vals(_n(ex).get("fileProperty"))
            if fp and fp[0] not in _FILE_PROPERTIES:
                v.err(path + ".extract.fileProperty",
                      f"fileProperty must be one of {', '.join(sorted(_FILE_PROPERTIES))}")
    if not ref:
        v.err(path, "source must reference a fileObject, fileSet, recordSet or field")
        return
    v.pending.append((ref, kind, path))
    if kind in ("fileObject", "fileSet", "distribution", None):
        sources.add(ref)


def validate(doc: Any) -> dict[str, Any]:
    """Structural validation of a Croissant JSON-LD document.

    Checks required properties, ``@id`` uniqueness, source / reference / containedIn
    targets, FileSet ``includes``, field ``dataType``, dates, bounding boxes, license
    URLs, value lengths and more. Accepts compact, prefixed or full-IRI keys.

    Returns ``{"valid": bool, "errors": [{"path", "message"}], "warnings": [...],
    "conformsTo": [...], "summary": {"distribution": n, "recordSet": n, "field": n}}``.
    """
    v = _Validator()
    summary = {"distribution": 0, "recordSet": 0, "field": 0}
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError as exc:
            v.err("", f"not valid JSON: {exc}")
    if not isinstance(doc, Mapping):
        if not v.errors:
            v.err("", "document must be a JSON object")
        return {"valid": False, "errors": v.errors, "warnings": [], "conformsTo": [],
                "summary": summary}
    nd = _n(doc)
    if "@context" not in doc:
        v.err("@context", "@context is required")
    if "Dataset" not in _types(doc):
        v.err("@type", "@type must be sc:Dataset")

    conforms = [c for c in (_str_value(x) for x in _vals(nd.get("conformsTo"))) if c]
    if not conforms:
        v.err("conformsTo", "conformsTo is required")
    elif CONFORMS_CORE not in conforms:
        v.warn("conformsTo", f"the document does not declare {CONFORMS_CORE}")
    has_rai = any(k in RAI_PROPERTIES or k in _RAI_ALIASES for k in nd)
    has_geo = any(k in nd for k in ("coordinateReferenceSystem", "spatialResolution",
                                    "temporalResolution", "bandConfiguration",
                                    "samplingStrategy", "spatialBias", "recordEndpoint"))
    if has_rai and CONFORMS_RAI not in conforms:
        v.warn("conformsTo", f"RAI properties are present but {CONFORMS_RAI} is not declared")
    if has_geo and CONFORMS_GEO not in conforms:
        v.warn("conformsTo", f"GeoCroissant properties are present but {CONFORMS_GEO} is "
                             "not declared")

    for prop in ("name", "description", "url", "license", "creator", "datePublished"):
        if _missing(nd.get(prop)):
            v.err(prop, f"{prop} is required")
    url = _vals(nd.get("url"))
    if url and not _is_url(url[0] if isinstance(url[0], str) else ""):
        v.err("url", "url must be an http(s) URL")
    for i, lic in enumerate(_vals(nd.get("license"))):
        lv = lic
        if isinstance(lic, Mapping):
            ln = _n(lic)
            lv = _vals(ln.get("url"))[0] if _vals(ln.get("url")) else lic.get("@id")
        if not _is_url(lv if isinstance(lv, str) else ""):
            v.warn(f"license[{i}]" if len(_vals(nd.get("license"))) > 1 else "license",
                   "license should be a URL (SPDX or Creative Commons recommended)")
    for prop in ("creator", "publisher"):
        entries = _vals(nd.get(prop))
        for i, p in enumerate(entries):
            path = f"{prop}[{i}]" if len(entries) > 1 else prop
            if isinstance(p, Mapping):
                pn = _n(p)
                if _missing(pn.get("name")):
                    v.err(path + ".name", "name is required")
                types = _types(p)
                if types and not types & {"Person", "Organization"}:
                    v.warn(path + ".@type", "should be sc:Person or sc:Organization")
            elif not isinstance(p, str):
                v.err(path, "must be a Person or Organization")
    for prop in ("datePublished", "dateCreated", "dateModified"):
        vals = _vals(nd.get(prop))
        if vals and not _iso_ok(_text(vals[0])):
            v.err(prop, f"{prop} must be an ISO 8601 date or date-time")
    tc = _vals(nd.get("temporalCoverage"))
    if tc and isinstance(tc[0], str) and not _interval_ok(tc[0]):
        v.warn("temporalCoverage", "temporalCoverage should be an ISO 8601 date or interval "
                                   "(2018-01-01/2021-12-31)")
    ver = _vals(nd.get("version"))
    if ver and not _SEMVER_RE.match(_text(ver[0])):
        v.warn("version", "version should follow MAJOR.MINOR.PATCH")
    for i, s in enumerate(_vals(nd.get("sameAs"))):
        if not _is_url(s if isinstance(s, str) else ""):
            v.warn(f"sameAs[{i}]", "sameAs should be a URL")
    live = _vals(nd.get("isLiveDataset"))
    is_live = bool(live) and live[0] is True
    if live and not isinstance(live[0], bool):
        v.warn("isLiveDataset", "isLiveDataset must be a boolean")
    for i, k in enumerate(_vals(nd.get("keywords"))):
        if isinstance(k, Mapping) and _missing(_n(k).get("name")):
            v.warn(f"keywords[{i}]", "DefinedTerm keyword without a name")
    for i, t in enumerate(_vals(nd.get("dataCollectionTimeframe") or
                                nd.get("dataCollectionTimeFrame"))):
        if isinstance(t, str) and not _interval_ok(t):
            v.warn(f"rai:dataCollectionTimeframe[{i}]", "should be an ISO 8601 date-time")

    # spatial coverage
    scs = _vals(nd.get("spatialCoverage"))
    for i, sc in enumerate(scs):
        base = "spatialCoverage" + (f"[{i}]" if len(scs) > 1 else "")
        if not isinstance(sc, Mapping):
            continue
        sn = _n(sc)
        geos = _vals(sn.get("geo"))
        if not geos and _types(sc) & {"GeoShape", "GeoCoordinates"}:
            geos = [sc]
        for j, g in enumerate(geos):
            gpath = f"{base}.geo" + (f"[{j}]" if len(geos) > 1 else "")
            if not isinstance(g, Mapping):
                continue
            gn = _n(g)
            if "box" in gn:
                bv = _vals(gn["box"])
                _check_box(v, bv[0] if bv else None, gpath + ".box")
            elif "latitude" in gn or "longitude" in gn:
                _check_point(v, g, gpath)
    bcs = _vals(nd.get("bandConfiguration"))
    if bcs and isinstance(bcs[0], Mapping):
        bn = _n(bcs[0])
        total = _vals(bn.get("totalBands"))
        names = _vals(bn.get("bandNameList"))
        if total and not (isinstance(total[0], int) and not isinstance(total[0], bool)):
            v.warn("geocr:bandConfiguration.totalBands", "totalBands should be an integer")
        elif total and names and total[0] != len(names):
            v.warn("geocr:bandConfiguration", "totalBands does not match bandNameList")

    # distribution
    dist = _vals(nd.get("distribution"))
    summary["distribution"] = len(dist)
    if not dist:
        v.warn("distribution", "distribution is empty (Croissant expects FileObjects or "
                               "FileSets)")
    contained_refs: list[tuple[str, str]] = []
    for i, d in enumerate(dist):
        path = f"distribution[{i}]"
        if not isinstance(d, Mapping):
            v.err(path, "distribution entry must be an object")
            continue
        dn = _n(d)
        types = _types(d)
        is_set = "FileSet" in types
        if not (is_set or "FileObject" in types):
            v.err(path + ".@type", "@type must be cr:FileObject or cr:FileSet")
        did = d.get("@id") if isinstance(d.get("@id"), str) else None
        if not did:
            name = _vals(dn.get("name"))
            did = name[0] if name and isinstance(name[0], str) else None
            v.err(path + ".@id", "@id is required")
        v.register(did, path + ".@id")
        if did:
            v.dist_kind[did] = "fileSet" if is_set else "fileObject"
        if _missing(dn.get("encodingFormat")):
            v.err(path + ".encodingFormat", "encodingFormat is required")
        contained = [c for c in (_ref_id(x) for x in _vals(dn.get("containedIn"))) if c]
        for c in contained:
            contained_refs.append((c, path + ".containedIn"))
        if is_set:
            if _missing(dn.get("includes")):
                v.err(path + ".includes", "a FileSet needs includes")
        else:
            if _missing(dn.get("contentUrl")):
                v.err(path + ".contentUrl", "a FileObject needs a contentUrl")
            if not contained and not is_live and _missing(dn.get("sha256")) and \
                    _missing(dn.get("md5")):
                v.warn(path, "no sha256 or md5 (mlcroissant requires one unless "
                             "isLiveDataset is true)")
    for ref, path in contained_refs:
        if ref not in v.dist_kind:
            v.err(path, f"containedIn references unknown distribution @id {ref!r}")

    # record sets
    rss = _vals(nd.get("recordSet"))
    summary["recordSet"] = len(rss)
    refs: list[tuple[str, str]] = []
    keys: list[tuple[str, str]] = []
    for i, rs in enumerate(rss):
        path = f"recordSet[{i}]"
        if not isinstance(rs, Mapping):
            v.err(path, "record set must be an object")
            continue
        rn = _n(rs)
        if "RecordSet" not in _types(rs):
            v.warn(path + ".@type", "record set should have @type cr:RecordSet")
        rid = rs.get("@id") if isinstance(rs.get("@id"), str) else None
        if not rid:
            v.err(path + ".@id", "@id is required")
            name = _vals(rn.get("name"))
            rid = name[0] if name and isinstance(name[0], str) else f"#{i}"
        v.register(rid, path + ".@id")
        v.rs_ids.add(rid)
        has_data = not _missing(rn.get("data"))
        fields = _vals(rn.get("field"))
        if not fields:
            v.warn(path + ".field", "record set has no fields")
        sources: set[str] = set()
        local_refs: list[tuple[str, str]] = []
        for j, f in enumerate(fields):
            _collect_validator_ids(v, f, rid, f"{path}.field[{j}]")
        for j, f in enumerate(fields):
            _validate_field(v, f, rid, f"{path}.field[{j}]", has_data, sources, local_refs)
        refs += local_refs
        if len(sources) > 1 and not local_refs:
            v.warn(path, "fields draw from several sources but no field declares references "
                         "(mlcroissant requires a join)")
        for k in _vals(rn.get("key")):
            kid = _ref_id(k)
            if kid:
                keys.append((kid, path + ".key"))
    summary["field"] = v.n_fields
    for ref, kind, path in v.pending:
        if kind == "fileObject" and v.dist_kind.get(ref) == "fileSet":
            v.warn(path, f"{ref!r} is a FileSet; use fileSet")
        elif kind == "fileSet" and v.dist_kind.get(ref) == "fileObject":
            v.warn(path, f"{ref!r} is a FileObject; use fileObject")
        if kind == "field":
            if ref not in v.field_ids:
                v.err(path, f"source references unknown field @id {ref!r}")
        elif kind == "recordSet":
            if ref not in v.rs_ids:
                v.err(path, f"source references unknown record set @id {ref!r}")
            else:
                v.warn(path, "mlcroissant 1.1 reads only field, fileObject or fileSet sources; "
                             "point the source at a field id (<recordSet>/<field>)")
        elif ref not in v.dist_kind and ref not in v.rs_ids and ref not in v.field_ids:
            v.err(path, f"source references unknown @id {ref!r}")
    for ref, path in refs:
        if ref not in v.field_ids:
            v.err(path, f"references points to unknown field @id {ref!r}")
    for ref, path in keys:
        if ref not in v.field_ids:
            v.warn(path, f"key {ref!r} is not a field @id")
    v.long_strings(doc, "")
    return {"valid": not v.errors, "errors": v.errors, "warnings": v.warnings,
            "conformsTo": conforms, "summary": summary}


def _collect_validator_ids(v: _Validator, field: Any, parent: str, path: str) -> None:
    if not isinstance(field, Mapping):
        return
    nf = _n(field)
    fid = field.get("@id") if isinstance(field.get("@id"), str) else None
    if not fid:
        names = _vals(nf.get("name"))
        fid = f"{parent}/{names[0]}" if names and isinstance(names[0], str) else None
    if fid:
        v.register(fid, path + ".@id")
        v.field_ids.add(fid)
    for i, s in enumerate(_vals(nf.get("subField"))):
        _collect_validator_ids(v, s, fid or parent, f"{path}.subField[{i}]")


# ---------------------------------------------------------------------------
# Encoding formats and DuckDB types
# ---------------------------------------------------------------------------

_ENCODINGS = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "tab": "text/tab-separated-values",
    "json": "application/json",
    "jsonl": "application/jsonlines",
    "ndjson": "application/jsonlines",
    "jsonld": "application/ld+json",
    "geojson": "application/geo+json",
    "parquet": "application/x-parquet",
    "pq": "application/x-parquet",
    "geoparquet": "application/x-parquet",
    "arrow": "application/vnd.apache.arrow.file",
    "feather": "application/vnd.apache.arrow.file",
    "avro": "application/avro",
    "orc": "application/x-orc",
    "ducklake": "application/x-ducklake",
    "duckdb": "application/x-duckdb",
    "sqlite": "application/vnd.sqlite3",
    "db": "application/vnd.sqlite3",
    "gpkg": "application/geopackage+sqlite3",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "jp2": "image/jp2",
    "svg": "image/svg+xml",
    "dcm": "image/dicom",
    "nc": "application/x-netcdf",
    "nc4": "application/x-netcdf",
    "netcdf": "application/x-netcdf",
    "h5": "application/x-hdf5",
    "hdf5": "application/x-hdf5",
    "he5": "application/x-hdf5",
    "hdf": "application/x-hdf",
    "zarr": "application/vnd+zarr",
    "zip": "application/zip",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "tgz": "application/gzip",
    "bz2": "application/x-bzip2",
    "xz": "application/x-xz",
    "7z": "application/x-7z-compressed",
    "txt": "text/plain",
    "text": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "htm": "text/html",
    "xml": "application/xml",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "ttl": "text/turtle",
    "pdf": "application/pdf",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp4": "video/mp4",
    "npy": "application/x-npy",
    "npz": "application/x-npz",
    "fasta": "text/x-fasta",
    "fa": "text/x-fasta",
    "fastq": "text/x-fastq",
    "las": "application/vnd.las",
    "laz": "application/vnd.laszip",
    "shp": "application/x-esri-shape",
    "kml": "application/vnd.google-earth.kml+xml",
    "arff": "https://ml.cms.waikato.ac.nz/weka/arff.html",
}


def encoding_format_for(name: str) -> str | None:
    """MIME type (``encodingFormat``) for a file or folder name, by extension.

    ``parquet`` -> ``application/x-parquet``, ``tif``/``tiff`` -> ``image/tiff``,
    ``nc`` -> ``application/x-netcdf``, ``h5``/``hdf5`` -> ``application/x-hdf5``,
    ``ducklake`` -> ``application/x-ducklake``, ``duckdb`` -> ``application/x-duckdb``,
    ``geojson`` -> ``application/geo+json``, ``zarr`` -> ``application/vnd+zarr`` and
    so on. Query strings and fragments are ignored. ``None`` when unknown.
    """
    if not isinstance(name, str) or not name:
        return None
    base = name.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    base = posixpath.basename(base).lower()
    if "." not in base:
        return None
    ext = base.rsplit(".", 1)[1]
    return _ENCODINGS.get(ext)


_DUCK_INT = {
    "TINYINT": "cr:Int8", "INT1": "cr:Int8",
    "SMALLINT": "cr:Int16", "INT2": "cr:Int16", "SHORT": "cr:Int16",
    "INTEGER": "cr:Int32", "INT": "cr:Int32", "INT4": "cr:Int32", "SIGNED": "cr:Int32",
    "BIGINT": "cr:Int64", "INT8": "cr:Int64", "LONG": "cr:Int64",
    "UTINYINT": "cr:UInt8", "USMALLINT": "cr:UInt16", "UINTEGER": "cr:UInt32",
    "UBIGINT": "cr:UInt64",
    "FLOAT": "cr:Float32", "FLOAT4": "cr:Float32", "REAL": "cr:Float32",
    "DOUBLE": "cr:Float64", "FLOAT8": "cr:Float64", "DOUBLE PRECISION": "cr:Float64",
    "BOOLEAN": "sc:Boolean", "BOOL": "sc:Boolean", "LOGICAL": "sc:Boolean",
    "DATE": "sc:Date",
    "TIME": "sc:Time", "TIMETZ": "sc:Time", "TIME WITH TIME ZONE": "sc:Time",
    "TIME_NS": "sc:Time",
    "DATETIME": "sc:DateTime",
    "HUGEINT": "sc:Float", "UHUGEINT": "sc:Float", "INT128": "sc:Float",
    "UINT128": "sc:Float",
    "GEOMETRY": "sc:GeoShape", "POINT_2D": "sc:GeoShape", "LINESTRING_2D": "sc:GeoShape",
    "POLYGON_2D": "sc:GeoShape", "BOX_2D": "sc:GeoShape",
}
_ARROW = {
    "int8": "cr:Int8", "int16": "cr:Int16", "int32": "cr:Int32", "int64": "cr:Int64",
    "uint8": "cr:UInt8", "uint16": "cr:UInt16", "uint32": "cr:UInt32", "uint64": "cr:UInt64",
    "float16": "cr:Float16", "halffloat": "cr:Float16", "float32": "cr:Float32",
    "float": "cr:Float32", "float64": "cr:Float64", "double": "cr:Float64",
    "bool": "sc:Boolean", "boolean": "sc:Boolean",
    "string": "sc:Text", "large_string": "sc:Text", "utf8": "sc:Text",
    "large_utf8": "sc:Text", "binary": "sc:Text", "large_binary": "sc:Text",
    "date32": "sc:Date", "date64": "sc:Date", "null": "sc:Text",
}


def _split_top(text: str) -> list[str]:
    parts, depth, cur, quote_ch = [], 0, [], None
    for ch in text:
        if quote_ch:
            cur.append(ch)
            if ch == quote_ch:
                quote_ch = None
            continue
        if ch in "\"'":
            quote_ch = ch
        elif ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _strip_member_name(member: str) -> tuple[str, str]:
    """``"a b" INTEGER`` / ``a INTEGER`` / ``a: int32`` -> (name, type)."""
    m = re.match(r'^\s*"((?:[^"]|"")*)"\s*:?\s*(.+)$', member)
    if m:
        return m.group(1).replace('""', '"'), m.group(2).strip()
    m = re.match(r"^\s*([^\s:]+)\s*:\s*(.+)$", member)
    if m:
        return m.group(1), m.group(2).strip()
    m = re.match(r"^\s*(\S+)\s+(.+)$", member)
    if m:
        return m.group(1), m.group(2).strip()
    return member.strip(), "VARCHAR"


def _unwrap(text: str, opener: str) -> str | None:
    """``LIST(INTEGER)`` -> ``INTEGER`` (for opener ``LIST``), case-insensitive."""
    m = re.match(rf"^{opener}\s*([(<])(.*)([)>])$", text, re.IGNORECASE | re.DOTALL)
    return m.group(2).strip() if m else None


def duckdb_type_to_croissant(type_name: str) -> dict[str, Any]:
    """Croissant field typing for a DuckDB (or Arrow) column type (convention section 9).

    Returns ``{"dataType": str | None, "isArray": bool, "arrayShape": str | None}``.
    ``T[]`` / ``LIST(T)`` give ``isArray`` with shape ``-1``, ``T[n]`` gives ``n``
    (nested arrays give e.g. ``-1,3``). A ``STRUCT(...)`` gives ``dataType: None`` and a
    ``"subFields"`` list of ``{"name", "dataType", "isArray", "arrayShape"[, "subFields"]}``.
    Unknown types map to ``sc:Text``.
    """
    t = (type_name or "").strip()
    shape: list[str] = []
    while True:
        m = re.search(r"\[(\d*)\]\s*$", t)
        if not m:
            break
        shape.append(m.group(1) or "-1")
        t = t[:m.start()].rstrip()
    inner = None
    for opener in ("LIST", "LARGE_LIST", "FIXED_SIZE_LIST"):
        inner = _unwrap(t, opener)
        if inner is not None:
            if opener != "FIXED_SIZE_LIST" or not shape:
                shape.append("-1")
            break
    if inner is not None:
        # Arrow writes list<item: int32>; strip the member name.
        if re.match(r"^[A-Za-z_][\w]*\s*:", inner):
            inner = inner.split(":", 1)[1].strip()
        sub = duckdb_type_to_croissant(inner)
        out = dict(sub)
        if sub["isArray"] and sub["arrayShape"]:
            shape += sub["arrayShape"].split(",")
        out["isArray"] = True
        out["arrayShape"] = ",".join(shape)
        return out
    result: dict[str, Any] = {"dataType": "sc:Text", "isArray": bool(shape),
                              "arrayShape": ",".join(shape) if shape else None}
    struct = _unwrap(t, "STRUCT")
    if struct is not None:
        subs = []
        for member in _split_top(struct):
            name, mtype = _strip_member_name(member)
            subs.append({"name": name, **duckdb_type_to_croissant(mtype)})
        result["dataType"] = None
        result["subFields"] = subs
        return result
    if t in _ARROW:
        result["dataType"] = _ARROW[t]
        return result
    upper = re.sub(r"\s+", " ", t.upper())
    base = re.sub(r"\s*[(\[].*$", "", upper).strip()
    if base in _DUCK_INT:
        result["dataType"] = _DUCK_INT[base]
    elif base.startswith(("DECIMAL", "NUMERIC")):
        result["dataType"] = "sc:Float"
    elif base.startswith("TIMESTAMP"):
        result["dataType"] = "sc:DateTime"
    elif base.startswith("DATE"):
        result["dataType"] = "sc:Date"
    elif base.startswith("TIME"):
        result["dataType"] = "sc:Time"
    return result


# ---------------------------------------------------------------------------
# Distribution suggestions (convention sections 5 and 8)
# ---------------------------------------------------------------------------

_SAFE_ID_RE = re.compile(r"\s+")


def _safe_id(text: str) -> str:
    return _SAFE_ID_RE.sub("_", text.strip())


def _unique(ident: str, used: set[str]) -> str:
    out, n = ident, 2
    while out in used:
        out = f"{ident}-{n}"
        n += 1
    used.add(out)
    return out


def _table_names(tables: Any) -> list[tuple[str, str]]:
    out = []
    for t in _as_list(tables):
        if isinstance(t, Mapping):
            name = str(t.get("table") or t.get("name") or "")
            schema = str(t.get("schema") or "main")
        else:
            text = str(t)
            schema, _, name = text.rpartition(".")
            schema = schema or "main"
        if name:
            out.append((schema, name))
    return list(dict.fromkeys(out))


def suggest_distribution(entries: Iterable[Mapping[str, Any]], *,
                         lake: Mapping[str, Any] | None = None,
                         tables: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """Suggest model-shaped ``distribution`` entries for a dataset folder.

    ``entries`` come from one listing of the folder (recursing at most into ``data/``):
    ``[{"name", "path" (relative to the dataset root), "is_folder", "size", "md5"}]``.
    ``lake`` is a portal ``lake_registry.json`` entry (``catalog`` and ``data`` relative
    to the root) or None. ``tables`` (or ``lake["tables"]``) lists the lake's tables as
    ``"schema.table"`` / ``"table"`` / ``{"schema", "table"}``.

    With a lake: a FileObject for the catalog (``@id`` = its file name,
    ``application/x-ducklake``) and a
    FileSet ``ducklake-<table>`` per table (``<data>/<schema>/<table>/**/*.parquet``), or
    one ``ducklake-data`` FileSet when no table names are known. Other files: one
    FileSet per encoding format for more than 5 files of the same format under
    ``data/``, otherwise one FileObject per file. Hidden files and ``.mesa/`` are skipped.
    Entries are model-shaped (leaves ``{"value", "unit"}``), ready for
    :func:`model_to_avus` under ``distribution``.
    """
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    folders: list[dict[str, Any]] = []
    for e in entries or []:
        if not isinstance(e, Mapping):
            continue
        path = str(e.get("path") or e.get("name") or "").strip("/")
        if not path or any(seg.startswith(".") for seg in path.split("/")):
            continue
        rec = {"path": path, "name": str(e.get("name") or posixpath.basename(path)),
               "size": e.get("size"), "md5": e.get("md5")}
        (folders if e.get("is_folder") else files).append(rec)

    def file_object(rec: dict[str, Any], fmt: str | None) -> dict[str, Any]:
        ident = _safe_id(rec.get("id") or rec["path"])
        ent: dict[str, Any] = {"id": _leaf(_unique(ident, used)), "type": _leaf("FileObject")}
        if rec["name"] != ent["id"]["value"]:
            ent["name"] = _leaf(rec["name"])
        ent["contentUrl"] = _leaf(rec["path"])
        ent["encodingFormat"] = _leaf(fmt or "application/octet-stream")
        if rec.get("size") not in (None, ""):
            ent["contentSize"] = _leaf(_text(rec["size"]), "B")
        if rec.get("md5"):
            ent["md5"] = _leaf(str(rec["md5"]))
        return ent

    covered: list[str] = []
    if lake:
        catalog = str(lake.get("catalog") or "").strip("/")
        data_path = str(lake.get("data") or "data").strip("/")
        if catalog:
            rec = next((f for f in files if f["path"] == catalog),
                       {"path": catalog, "name": posixpath.basename(catalog)})
            entry = file_object(dict(rec, id=posixpath.basename(catalog)),
                                "application/x-ducklake")
            out.append(entry)
            covered.append(catalog)
        names = _table_names(tables if tables is not None else lake.get("tables"))
        counts: dict[str, int] = {}
        for _, name in names:
            counts[name] = counts.get(name, 0) + 1
        if names:
            for schema, name in names:
                ident = f"ducklake-{name}" if counts[name] == 1 else \
                    f"ducklake-{schema}-{name}"
                out.append({
                    "id": _leaf(_unique(_safe_id(ident), used)),
                    "type": _leaf("FileSet"),
                    "description": _leaf(f"Parquet files of DuckLake table {schema}.{name}"),
                    "encodingFormat": _leaf("application/x-parquet"),
                    "includes": [_leaf(f"{data_path}/{schema}/{name}/**/*.parquet")],
                })
        else:
            out.append({
                "id": _leaf(_unique("ducklake-data", used)),
                "type": _leaf("FileSet"),
                "description": _leaf("Parquet data files of the DuckLake"),
                "encodingFormat": _leaf("application/x-parquet"),
                "includes": [_leaf(f"{data_path}/**/*.parquet")],
            })
        covered.append(data_path + "/")

    def is_covered(path: str) -> bool:
        return any(path == c or (c.endswith("/") and path.startswith(c)) for c in covered)

    rest = [f for f in files if not is_covered(f["path"])
            and not (lake and f["path"].endswith(".ducklake"))]
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in rest:
        if f["path"].startswith("data/"):
            fmt = encoding_format_for(f["name"])
            if fmt:
                groups.setdefault(fmt, []).append(f)
    fileset_paths: set[str] = set()
    for fmt in sorted(groups):
        members = groups[fmt]
        if len(members) <= 5:
            continue
        exts = sorted({m["name"].rsplit(".", 1)[1].lower() for m in members})
        ident = _unique(f"{exts[0]}-files", used)
        out.append({
            "id": _leaf(ident),
            "type": _leaf("FileSet"),
            "description": _leaf(f"{len(members)} {exts[0]} files under data/"),
            "encodingFormat": _leaf(fmt),
            "includes": [_leaf(f"data/**/*.{ext}") for ext in exts],
        })
        fileset_paths.update(m["path"] for m in members)
    for f in sorted(folders, key=lambda r: r["path"]):
        if f["name"].lower().endswith(".zarr") and not is_covered(f["path"] + "/"):
            out.append({
                "id": _leaf(_unique(_safe_id(f["path"]), used)),
                "type": _leaf("FileSet"),
                "encodingFormat": _leaf("application/vnd+zarr"),
                "includes": [_leaf(f"{f['path']}/**")],
            })
    for f in sorted(rest, key=lambda r: r["path"]):
        if f["path"] in fileset_paths:
            continue
        out.append(file_object(f, encoding_format_for(f["name"])))
    return out
