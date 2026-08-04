"""Contract checks for the reusable synthetic classification corpora."""

from __future__ import annotations

from endnotex9_mcp import parser


def test_cross_domain_fixture_covers_long_cjk_and_empty_keywords(cross_domain_xml):
    refs = list(parser.parse_library(cross_domain_xml))
    assert len(refs) == 2
    assert len(refs[0]["abstract"]) > 1_000
    assert any("\u4e00" <= char <= "\u9fff" for char in refs[0]["container_title"])
    assert refs[1]["keywords"] == []
    assert refs[1]["ref_type"] in {
        "Journal Article",
        "Dataset",
        "Archive",
    }
