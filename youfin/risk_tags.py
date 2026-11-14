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
    "litigation": (2, [r"lawsuit", r"\bsued\b", r"class action", r"complaint filed"]),
    "financing":  (0, [r"securitization", r"issuance", r"assigns? (preliminary )?ratings?",
                       r"closes? \$"]),
}

_COMPILED = {cat: (w, [re.compile(p, re.I) for p in pats]) for cat, (w, pats) in RULES.items()}


def tag(text: str) -> tuple[int, list[str], list[str]]:
    """Return (severity, categories, matched_terms)."""
    severity, cats, terms = 0, [], []
    for cat, (weight, patterns) in _COMPILED.items():
        hits = [m.group(0) for p in patterns if (m := p.search(text))]
        if hits:
            cats.append(cat)
            terms.extend(hits)
            severity = max(severity, weight)
    return severity, cats, sorted({t.lower() for t in terms})
