"""Field definitions from an ArcGIS item's formal metadata document.

ArcGIS Online and Enterprise items can carry a metadata document
(``/sharing/rest/content/items/{id}/info/metadata/metadata.xml``) in the
ArcGIS, FGDC or ISO profile. Its entity-and-attribute section
(``eainfo/detailed/attr``) defines each field with a label, a definition,
units and sometimes an enumerated or range domain: more than a field alias
usually says.

Parsing is defensive: documents over :data:`MAX_METADATA_BYTES` or with a
DTD (``<!DOCTYPE``/``<!ENTITY``) are refused before parsing. Both XXE and
entity-expansion ("billion laughs") attacks need a DTD, so the stdlib parser
never sees one; ``defusedxml`` is not a runtime dependency of the Lambda
package. Any parse failure yields no definitions.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

logger = logging.getLogger(__name__)

# HUD's LIHTC document is ~92 KB; a 2 MB cap leaves room for large schemas.
MAX_METADATA_BYTES = 2_000_000
# Enumerated values kept per field (the formatter caps what it shows).
_MAX_ENUM_VALUES = 200

# A Hub dataset id is the 32-hex item id, optionally with a ``_<layer>`` suffix.
_ITEM_ID = re.compile(r"^([0-9a-fA-F]{32})(?:_\d+)?$")
_DTD = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def item_id_from(dataset_id: Any) -> str | None:
    """Return the 32-hex ArcGIS item id in a Hub dataset id, or ``None``."""
    if not isinstance(dataset_id, str):
        return None
    match = _ITEM_ID.match(dataset_id.strip())
    return match.group(1).lower() if match else None


def _text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _domain(attr: ET.Element) -> dict[str, Any] | None:
    """Translate FGDC ``rdom``/``edom`` into the Feature Service domain shape."""
    rdom = attr.find("attrdomv/rdom")
    if rdom is not None:
        low, high = _text(rdom, "rdommin"), _text(rdom, "rdommax")
        if low or high:
            return {"type": "range", "range": [low or None, high or None]}
    values = []
    for edom in attr.findall("attrdomv/edom")[:_MAX_ENUM_VALUES]:
        code = _text(edom, "edomv")
        if code:
            values.append({"code": code, "name": _text(edom, "edomvd")})
    if values:
        return {"type": "codedValue", "codedValues": values}
    return None


def _attributes(detailed: ET.Element) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for attr in detailed.findall("attr"):
        label = _text(attr, "attrlabl")
        if not label or label.lower() in fields:
            continue
        info: dict[str, Any] = {}
        definition = _text(attr, "attrdef")
        if definition:
            info["definition"] = definition
        units = _text(attr, "attrdomv/rdom/attrunit") or _text(attr, "attrunit")
        if units:
            info["units"] = units
        domain = _domain(attr)
        if domain:
            info["domain"] = domain
        if info:
            fields[label.lower()] = info
    return fields


def parse_entities(document: bytes) -> list[tuple[str, dict[str, dict[str, Any]]]]:
    """Parse a metadata document into ``[(entity label, {field: info})]``.

    ``field`` is the lower-cased attribute label; ``info`` holds whichever
    of ``definition``, ``units`` and ``domain`` the document gives. Returns
    ``[]`` for oversized documents, documents with a DTD, malformed XML, or
    documents without an entity-and-attribute section.
    """
    if not document or len(document) > MAX_METADATA_BYTES:
        return []
    if _DTD.search(document):
        logger.warning("Refusing metadata document with a DTD")
        return []
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return []
    entities = []
    for detailed in root.iter("detailed"):
        fields = _attributes(detailed)
        if fields:
            entities.append((_text(detailed, "enttyp/enttypl"), fields))
    return entities


def definitions_for_layer(
    entities: list[tuple[str, dict[str, dict[str, Any]]]],
    layer_name: str | None,
    *,
    default_layer: bool = True,
) -> dict[str, dict[str, Any]]:
    """Pick the field definitions that describe one layer.

    An entity whose label matches the layer name wins; it is compared
    ignoring case and anything but letters and digits, and also matches a
    label that adds a suffix such as ``(Feature)``. Without a match, a
    document with a single entity is used for the item's default layer
    (labels often differ from layer names); otherwise nothing is returned,
    so one layer never shows another layer's definitions.

    Args:
        entities: Output of :func:`parse_entities`.
        layer_name: The layer's ``name`` from its service metadata.
        default_layer: ``False`` when the caller asked for a specific layer.
    """

    def norm(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    wanted = norm(layer_name)
    if wanted:
        for label, fields in entities:
            if label and norm(label).startswith(wanted):
                return fields
    if len(entities) == 1 and default_layer:
        return entities[0][1]
    return {}
