"""MESA Croissant: MLCommons Croissant metadata as CyVerse Data Store AVUs.

The codec, generator, importer and validator live in :mod:`mesa_croissant.core`
(pure standard library, no I/O). The MESA-MCP tools live in
``mesa_croissant.mcp_tools`` (optional extra ``mcp``).
"""

from mesa_croissant.core import (
    CONFORMS_CORE,
    CONFORMS_GEO,
    CONFORMS_RAI,
    CONTEXT,
    CONVENTION_VERSION,
    CORE_VERSION,
    PREFIX,
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

__version__ = CORE_VERSION

__all__ = [
    "CONFORMS_CORE",
    "CONFORMS_GEO",
    "CONFORMS_RAI",
    "CONTEXT",
    "CONVENTION_VERSION",
    "CORE_VERSION",
    "PREFIX",
    "__version__",
    "crosswalk",
    "duckdb_type_to_croissant",
    "encoding_format_for",
    "from_jsonld",
    "is_croissant_attr",
    "key_catalog",
    "model_to_avus",
    "parse_avus",
    "suggest_distribution",
    "to_jsonld",
    "validate",
]
