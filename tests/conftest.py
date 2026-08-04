"""Shared fixtures.

SAMPLE_XML mirrors the shape of a real EndNote X9 export rather than a
minimal one: every value is wrapped in the `<style>` element EndNote emits,
attachments carry the `internal-pdf://<folder-id>/` form, and the records
cover the cases this library actually contains — an article with an article
number instead of page range, a Chinese-language record, and a book section.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

import pytest

from endnotex9_mcp import db, indexer


def _styled(value: str) -> str:
    return f'<style face="normal" font="default" size="100%">{value}</style>'


SAMPLE_XML = f"""<?xml version="1.0" encoding="UTF-8" ?><xml><records>
<record>
  <database name="Test.enl" path="C:\\Test.enl">Test.enl</database>
  <source-app name="EndNote" version="19.0">EndNote</source-app>
  <rec-number>1</rec-number>
  <ref-type name="Journal Article">17</ref-type>
  <contributors><authors>
    <author>{_styled("Li, D.")}</author>
    <author>{_styled("Li, Z. B.")}</author>
    <author>{_styled("Zhang, X. L.")}</author>
    <author>{_styled("Liu, C.")}</author>
  </authors></contributors>
  <titles>
    <title>{_styled("Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys")}</title>
    <secondary-title>{_styled("ACS Applied Materials &amp; Interfaces")}</secondary-title>
  </titles>
  <periodical>
    <full-title>{_styled("ACS Applied Materials &amp; Interfaces")}</full-title>
    <abbr-1>{_styled("ACS Appl. Mater. Interfaces")}</abbr-1>
  </periodical>
  <pages>{_styled("1505-1518")}</pages>
  <volume>{_styled("14")}</volume>
  <number>{_styled("1")}</number>
  <keywords>
    <keyword>{_styled("elastocaloric effect")}</keyword>
    <keyword>{_styled("martensitic transformation")}</keyword>
  </keywords>
  <dates><year>{_styled("2022")}</year></dates>
  <abstract>{_styled("A large lattice volume change boosts the elastocaloric effect.")}</abstract>
  <label>{_styled("dli2022acs")}</label>
  <language>{_styled("English")}</language>
  <isbn>{_styled("1944-8244")}</isbn>
  <urls>
    <related-urls><url>{_styled("https://example.org/paper")}</url></related-urls>
    <pdf-urls><url>internal-pdf://0740201097/2022-Li-ACS.pdf</url></pdf-urls>
  </urls>
  <electronic-resource-num>{_styled("https://doi.org/10.1021/acsami.1c21109")}</electronic-resource-num>
</record>
<record>
  <rec-number>2</rec-number>
  <ref-type name="Journal Article">17</ref-type>
  <contributors><authors><author>{_styled("Smith, John Robert")}</author></authors></contributors>
  <titles>
    <title>{_styled("Martensitic transformation in shape memory alloys")}</title>
    <secondary-title>{_styled("Acta Materialia")}</secondary-title>
  </titles>
  <dates><year>{_styled("2020")}</year></dates>
  <volume>{_styled("200")}</volume>
  <section>{_styled("106545")}</section>
  <language>{_styled("en")}</language>
  <electronic-resource-num>{_styled("10.1016/j.actamat.2020.106545")}</electronic-resource-num>
</record>
<record>
  <rec-number>3</rec-number>
  <ref-type name="Journal Article">17</ref-type>
  <contributors><authors>
    <author>{_styled("张三")}</author>
    <author>{_styled("李四")}</author>
  </authors></contributors>
  <titles>
    <title>{_styled("Ti-Nb-Zr-Sn形状记忆合金马氏体相变与弹热性能")}</title>
    <secondary-title>{_styled("金属学报")}</secondary-title>
  </titles>
  <dates><year>{_styled("2019")}</year></dates>
  <volume>{_styled("55")}</volume>
  <number>{_styled("3")}</number>
  <pages>{_styled("321-330")}</pages>
  <language>{_styled("zh")}</language>
  <abstract>{_styled("研究了形状记忆合金材料的马氏体相变行为与弹热制冷性能。")}</abstract>
</record>
<record>
  <rec-number>4</rec-number>
  <ref-type name="Book Section">5</ref-type>
  <contributors><authors><author>{_styled("Otsuka, K.")}</author></authors></contributors>
  <titles>
    <title>{_styled("Introduction to shape memory materials")}</title>
    <secondary-title>{_styled("Shape Memory Materials")}</secondary-title>
  </titles>
  <dates><year>{_styled("1998")}</year></dates>
  <publisher>{_styled("Cambridge University Press")}</publisher>
  <pub-location>{_styled("Cambridge")}</pub-location>
  <pages>{_styled("1-26")}</pages>
</record>
</records></xml>"""


def make_synthetic_xml(tmp_path: Path, domain: str) -> Path:
    """Create a small cross-domain EndNote export for profile/classification tests.

    The records intentionally exercise long abstracts, CJK text, empty keyword
    lists, and a nonstandard reference type.  Keeping construction here makes
    tests independent of any real user library.
    """
    records = {
        "materials": (
            ("17", "Journal Article", "Ni-Mn-Ga elastocaloric alloys", "材料相变与弹热效应"),
            (
                "17",
                "Journal Article",
                "Perovskite barocaloric cooling",
                "Pressure-driven phase transitions",
            ),
        ),
        "biology": (
            ("17", "Journal Article", "CRISPR editing in Arabidopsis", "基因编辑与植物免疫"),
            ("42", "Dataset", "Single-cell RNA sequencing atlas", "细胞转录组数据集"),
        ),
        "history": (
            ("5", "Book Section", "Trade routes in Ming China", "明代海上丝绸之路"),
            ("99", "Archive", "Oral histories of migration", "移民口述史档案"),
        ),
    }
    if domain not in records:
        raise ValueError(f"unknown synthetic domain: {domain}")

    rows = []
    for number, (code, kind, title, secondary) in enumerate(records[domain], 1):
        keywords = (
            "<keywords><keyword>placeholder</keyword></keywords>" if number == 1 else "<keywords/>"
        )
        long_abstract = (
            " ".join([f"{domain} abstract segment {i}." for i in range(80)])
            if number == 1
            else f"{secondary}。研究摘要。"
        )
        rows.append(
            f"""<record><rec-number>{number}</rec-number>
<ref-type name="{kind}">{code}</ref-type>
<contributors><authors><author>{_styled("Doe, A.")}</author></authors></contributors>
<titles><title>{_styled(escape(title))}</title>
<secondary-title>{_styled(escape(secondary))}</secondary-title></titles>
<dates><year>{_styled(str(2020 + number))}</year></dates>
{keywords}<abstract>{_styled(escape(long_abstract))}</abstract>
<language>{_styled("zh" if number == 1 else "en")}</language></record>"""
        )
    path = tmp_path / f"{domain}.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8" ?><xml><records>'
        + "".join(rows)
        + "</records></xml>",
        encoding="utf-8",
    )
    return path


@pytest.fixture(params=("materials", "biology", "history"))
def cross_domain_xml(tmp_path: Path, request) -> Path:
    return make_synthetic_xml(tmp_path, request.param)


@pytest.fixture
def xml_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.xml"
    path.write_text(SAMPLE_XML, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_path: Path, xml_path: Path):
    connection = db.connect(tmp_path / "library.db")
    indexer.build_index(xml_path, connection, full=True)
    yield connection
    connection.close()
