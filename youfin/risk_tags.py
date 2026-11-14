"""Deterministic first-pass risk tagger.

Cheap and explainable: every tag records the exact terms that triggered it, so a
human (or the Verifier agent) can check the receipt. An LLM pass can refine later.
"""
from __future__ import annotations

import re

# category -> (severity weight, regex patterns)
RULES: dict[str, tuple[int, list[str]]] = {
    "insolvency": (5, [r"bankrupt", r"chapter (7|11)", r"insolven", r"receivership",
                       r"liquidat", r"wind(ing)? down", r"ceases? operations"]),
    "lender_action": (5, [r"seiz(e|es|ed|ing|ure)", r"takes? (control|over)\b", r"takeover",
                          r"toma control", r"foreclos", r"sweeps? (cash|collections)"]),
    "fraud":      (5, [r"fraud", r"double[- ]pledg", r"misrepresent", r"falsif",
                       r"subpoena", r"indict", r"embezzl", r"irregularit"]),
    "operations": (4, [r"furlough", r"suspend(s|ed|ing)? (its )?operations", r"shut(s|ting)? (down|operations)",
                       r"layoffs?", r"lays? off", r"halt(s|ed)? (lending|originations)",
                       r"job cuts?", r"cut(s|ting)? (\w+ ){0,3}jobs",
                       r"paus(e|es|ed) (new )?(loan )?originations", r"goes quiet",
                       r"licencia temporal"]),
    "diligence_red_flag": (4, [r"quality[- ]of[- ]earnings", r"off[- ]balance[- ]sheet",
                               r"(appoint|hire|retain)\w* \w+ to review",
                               r"review\w* (of )?(its )?(invoice|factoring|receivables|supply[- ]chain)",
                               r"(invoice|receivables) (factoring|financ\w*)\W+.{0,40}review",
                               r"doubts? (about|over|on)", r"skeptic\w* (of|over|about)",
                               r"accounting (questions|concerns|irregular)",
                               r"zweifel", r"prüft"]),
    "lender_disclosure": (4, [r"loan commitments? to", r"exposure to", r"exposed to",
                              r"credit (hit|loss)",
                              r"charge[- ]?off", r"provision for"]),
    "credit":     (3, [r"downgrad", r"negative (watch|outlook)", r"\bdefault(?! rating)", r"covenant",
                       r"breach", r"write[- ]?down", r"impair", r"bonds? (drop|plunge|fall)",
                       r"loans? (drop|plunge|fall|slump)", r"distress", r"restructur",
                       r"short position", r"bets? against"]),
    "regulatory": (3, [r"\bcfpb\b", r"attorney general", r"consent order", r"enforcement action",
                       r"\bsec\b (probe|investigation)"]),
    "funding":    (2, [r"refinanc\w* (pulled|paused|on pause|delayed)", r"margin call",
                       r"hires? (restructuring )?advisers?", r"creditors"]),
    "litigation": (2, [r"lawsuit", r"\bsued\b", r"class action", r"complaint filed",
                       r"securities (fraud )?(suit|litigation|class)", r"shareholder (suit|lawsuit|litigation)",
                       r"investors? (say|allege|claim)\w*\b.{0,80}\b(lied|misled|mislead\w*|deceiv\w*)"]),
    "financing":  (0, [r"securitization", r"issuance", r"assigns? (preliminary )?ratings?",
                       r"closes? \$"]),
}

# Generic (soft) fraud wording. Hard fraud terms (double-pledge, falsif, embezzl, indict...) stay at 5.
_SOFT_FRAUD = re.compile(r"fraud|misrepresent|irregularit|subpoena", re.I)
# Shareholder / securities suits allege misstatements; they are litigation, not company fraud.
_SECURITIES_SUIT = re.compile(
    r"class[- ]action|securities (fraud )?(suit|litigation|class)|shareholder (suit|lawsuit|litigation)"
    r"|investors? (say|allege|claim)\w*\b.{0,80}\b(lied|misled|mislead\w*|deceiv\w*|misrepresent\w*)"
    r"|\blead plaintiff\b|investors? who (bought|purchased)", re.I)
# Consumer-scam stories: the brand is a backdrop/victim, not the fraudster.
_CONSUMER_SCAM = re.compile(r"\bscam(s|med|mers?)?\b|\bvictims?\b|\bphishing\b|identity theft"
                            r"|(car|vehicle) (buyers?|shoppers?)", re.I)
# Industry-wide fraud statistics: the company is one name in a list, not the subject.
_INDUSTRY_STAT = re.compile(r"(auto |loan |lending )?fraud (has |just )?(hit|hits|rises?|rose|surges?)\b"
                            r"|fraud losses", re.I)
_LENDER_REG = re.compile(r"lenders?|creditors?|warehouse|credit facility|securitization|auditors?"
                         r"|\bdoj\b|justice department|\bsec\b|\bfbi\b|prosecutors?|indict|bondholders?"
                         r"|noteholders?", re.I)
_YEAR = re.compile(r"\b(20[012]\d)\b")

STALE_CAP = 2


def _stale(text: str, pub_year: int | None) -> bool:
    """True when every year in the text is >=2 years before the publish year (a retrospective)."""
    if not pub_year:
        return False
    years = [int(y) for y in _YEAR.findall(text)]
    return bool(years) and max(years) <= pub_year - 2


_COMPILED = {cat: (w, [re.compile(p, re.I) for p in pats]) for cat, (w, pats) in RULES.items()}


def tag(text: str, pub_year: int | None = None) -> tuple[int, list[str], list[str]]:
    """Return (severity, categories, matched_terms)."""
    suit = bool(_SECURITIES_SUIT.search(text))
    scam = (bool(_CONSUMER_SCAM.search(text)) and not _LENDER_REG.search(text)) \
        or bool(_INDUSTRY_STAT.search(text))
    severity, cats, terms, softened = 0, [], [], False
    for cat, (weight, patterns) in _COMPILED.items():
        hits = [m.group(0) for p in patterns if (m := p.search(text))]
        if cat == "fraud" and (suit or scam):
            hard = [h for h in hits if not _SOFT_FRAUD.search(h)]
            if scam and not suit and len(hard) < len(hits):
                cats.append("consumer_fraud")
                terms.extend(hits)
                severity = max(severity, 1)
            elif suit and len(hard) < len(hits):
                softened = True
            hits = hard
        if hits:
            cats.append(cat)
            terms.extend(hits)
            severity = max(severity, weight)
    if softened:  # soft fraud wording inside a shareholder suit: litigation, capped at 3
        if "litigation" not in cats:
            cats.append("litigation")
        severity = max(severity, 3)
    if _stale(text, pub_year) and severity > STALE_CAP:
        severity = STALE_CAP
        cats.append("stale_context")
    return severity, cats, sorted({t.lower() for t in terms})
