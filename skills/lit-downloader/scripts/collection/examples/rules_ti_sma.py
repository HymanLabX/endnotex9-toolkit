"""Example topic rules for merge.py: Ni-free Ti-based shape memory / superelastic alloys.

Tiers: A = core (a Ti-X system plus shape-memory wording); B = related metastable-beta Ti work
(martensite, omega, TRIP/TWIP) without shape-memory wording; X = excluded (Ni-containing,
alpha+beta alloys such as Ti-6Al-4V, other systems). Copy this file into the project folder and
rewrite the patterns for a new topic; the user reviews the result in tiers.csv.
"""

from __future__ import annotations

import re

SMA = re.compile(
    r"shape[- ]memory|super[- ]?elastic|pseudo[- ]?elastic|nonlinear elasticity|strain glass|"
    r"elastocaloric|recover(y|able) strain|shape recovery|reversible martensit|thermoelastic",
    re.I,
)
MART = re.compile(
    r"martensit|α''|α″|alpha''|alpha double prime|omega phase|ω phase|ω-phase|TRIP|TWIP|"  # noqa: RUF001
    r"twinning-induced|transformation-induced",
    re.I,
)
# Element symbols are case-sensitive ("Timoshenko" must not read as Ti-Mo, "titanium" as Ti-Ta).
_X = r"(Nb|Zr|Mo|Ta|Au|Pd|Pt|Hf|V|Cr|Sn|Fe|Mn|Al)"
_U = r"(\s?(at\.?|wt\.?|mass|mol)\s?(%|pct))?\s?"
CHEM = re.compile(
    r"(?<![A-Za-z])Ti\s?[-/]?\s?x?\d*(\.\d+)?" + _U + r"-?" + _X + r"(?![a-z])"
    r"|(?<![A-Za-z])Ti\d+(\.\d+)?" + _X + r"(?![a-z])"
)
PHRASE = re.compile(
    r"\b(beta|β)[- ]?(type )?(Ti|titanium)\b|metastable (beta|β)[- ]?(Ti|titanium|type)|"
    r"gum ?metal|\bTi2448\b|(Ni|nickel)-free (Ti|titanium|shape|superelastic|beta|β)|"
    r"Ti-based (shape|superelastic|SMA)|titanium-based (shape|superelastic)|Ti1-xNbx|Ti-xTM|"
    r"Ti-X (alloys|system)|binary (Ti|titanium) alloys|\d\s?(at\.?|wt\.?)?\s?%\s?(Nb|Zr|Mo|Ta)-Ti\b|"
    r"(Ti|titanium)[- ]based[^.;]{0,30}shape[- ]memory|"
    r"titanium[- ](niobium|zirconium|molybdenum|tantalum|vanadium|gold|palladium|platinum|"
    r"chromium|manganese|hafnium)|Beta III|"
    r"(shape[- ]memory|superelastic|pseudoelastic)[^.;]{0,30}(Ti|titanium) alloys?|"
    r"(Ti|titanium) alloys? with[^.;]{0,20}(shape[- ]memory|superelastic)|metastable (Ti|titanium)|"
    r"(α''|α″|alpha''|alpha double prime|omega|ω)[ -](phase|martensite)?[^.;]{0,40}"  # noqa: RUF001
    r"\b(Ti|titanium) alloys?",
    re.I,
)
NI = re.compile(
    r"(?<![A-Z])Ni(?![a-z])|NiTi|TiNi|[Nn]itinol|[Nn]ickel[- ][Tt]itanium|[Tt]itanium [Nn]ickelide"
)
NI_FREE = re.compile(
    r"Ni-free|nickel-free|free of Ni|without Ni|nickel allergy|Ni allergy|Ni hypersensitivity|"
    r"nickel hypersensitivity|toxicity of Ni|Ni toxicity|nickel toxicity",
    re.I,
)
AB_TITLE = re.compile(r"Ti-6Al-4V|Ti6Al4V|Ti-64\b|Ti64|\bTC4\b|Ti-6Al-7Nb|weld", re.I)


def _comp(text: str) -> bool:
    return bool(CHEM.search(text) or PHRASE.search(text))


def classify(rec: dict[str, list[str]]) -> tuple[str, str]:
    ti = " ".join(rec.get("TI", []) + rec.get("T1", []))
    ab = " ".join(rec.get("AB", []))
    head = ti + " ; " + "; ".join(rec.get("KW", []))
    text = head + " " + ab
    sma = bool(SMA.search(text))
    comp_head, comp_ab = _comp(head), _comp(ab)
    ni_head, ni_ab, ni_free = bool(NI.search(head)), bool(NI.search(ab)), bool(NI_FREE.search(text))
    if ni_head and not ni_free:
        return "X", "Ni-containing in title/keywords"
    if AB_TITLE.search(ti) and not _comp(AB_TITLE.sub("", ti)):
        return "X", "alpha+beta alloy (Ti-6Al-4V etc.) / welding"
    if sma and comp_head:
        return "A", "Ti-X system in title/keywords + SMA wording"
    if sma and comp_ab and not (ni_ab and not ni_free):
        return "A", "Ti-X system in abstract + SMA wording"
    if (
        sma
        and comp_ab
        and (_comp(ti) or re.search(r"Ti-base|beta-type|β-type", ti, re.I))
        and not NI.search(ti)
    ):
        return "A", "Ti-X system in title + SMA wording (Ni only mentioned in abstract)"
    if sma and comp_ab:
        return "X", "generic SMA / Ni-containing (Ti-X only in abstract, Ni also mentioned)"
    if comp_head and MART.search(text):
        return "B", "metastable-beta Ti martensite/omega/TRIP, no SMA wording"
    if (
        re.search(r"(titanium|\bTi) alloys?", ti, re.I)
        and re.search(r"martensit|α''|α\"|α″|ω|omega|TRIP|TWIP", ti, re.I)  # noqa: RUF001
        and not AB_TITLE.search(ti)
    ):
        return "B", "Ti alloy martensite/omega/TRIP in title"
    if comp_head:
        return "B", "Ti-X system in title/keywords; SMA/martensite wording only in Keywords Plus"
    return "X", "weak match" if comp_ab else "no specific Ni-free Ti system"
