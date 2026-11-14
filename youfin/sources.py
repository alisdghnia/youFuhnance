"""Source-quality model used by the Verifier.

Tier 0 = primary documents (regulators, courts, rating agencies)
Tier 1 = major financial / legal press
Tier 2 = trade press, wires, syndication (default)
Tier 3 = aggregators, AI-written content, social media
"""
from __future__ import annotations

TIER0 = {
    "sec.gov", "pacermonitor.com", "courtlistener.com", "inforuptcy.com", "uscourts.gov",
    "kbra.com", "spglobal.com", "moodys.com", "fitchratings.com", "dbrsmorningstar.com",
    "federalreserve.gov", "cfpb.gov", "fdic.gov",
}
TIER1 = {
    "bloomberg.com", "bloomberglaw.com", "bloombergtax.com", "ft.com", "wsj.com", "reuters.com",
    "cnbc.com", "nytimes.com", "law360.com", "americanbanker.com", "autonews.com",
    "crainsdetroit.com", "apnews.com", "barrons.com", "axios.com", "octus.com",
    "ionanalytics.com", "9fin.com", "pitchbook.com", "privatedebtinvestor.com",
}
TIER3 = {
    "ainvest.com", "biztoc.com", "franetic.com", "newsdirectory3.com", "memesita.com",
    "indiapodcast.com", "stockinvest.us", "newsminimalist.com", "youtube.com", "x.com",
    "twitter.com", "threads.com", "threads.net", "reddit.com", "linkedin.com", "medium.com",
    "fullavantenews.com", "indexbox.io", "archive.ph", "news.futunn.com", "futunn.com",
}

# Outlets that syndicate each other: count them once.
PUBLISHER_GROUPS = {
    "bloomberglaw.com": "bloomberg", "bloombergtax.com": "bloomberg", "bloomberg.com": "bloomberg",
    "westlaw.com": "reuters", "reuters.com": "reuters",
    "dealershipguy.com": "cardealershipguy", "x.com": "cardealershipguy",
    "threads.com": "cardealershipguy",
}

TIER_NAMES = {0: "primary", 1: "major press", 2: "trade/wire", 3: "aggregator/social"}


def registrable(domain: str) -> str:
    """news.bloomberglaw.com -> bloomberglaw.com; uk.investing.com -> investing.com."""
    parts = domain.lower().removeprefix("www.").split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def tier(domain: str) -> int:
    d = registrable(domain)
    if d in TIER0:
        return 0
    if d in TIER1:
        return 1
    if d in TIER3:
        return 3
    return 2


def publisher(domain: str) -> str:
    d = registrable(domain)
    return PUBLISHER_GROUPS.get(d, d)
