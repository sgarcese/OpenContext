"""Security tests for parsing ArcGIS item metadata XML (XXE, entity expansion)."""

import pytest

from plugins.arcgis.metadata_xml import parse_entities

pytestmark = pytest.mark.security

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE metadata [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<metadata><eainfo><detailed>
  <attr><attrlabl>A</attrlabl><attrdef>&secret;</attrdef></attr>
</detailed></eainfo></metadata>"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<metadata><eainfo><detailed>
  <attr><attrlabl>A</attrlabl><attrdef>&lol3;</attrdef></attr>
</detailed></eainfo></metadata>"""


@pytest.mark.parametrize("document", [XXE, BILLION_LAUGHS, XXE.lower()])
def test_documents_with_a_dtd_are_refused_before_parsing(document):
    assert parse_entities(document) == []
