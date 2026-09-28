"""
dedup.py - Post-Extraction Deduplication & Aliasing Module
Normalizes domains and deduplicates stores AFTER extraction.
Merges partial information across duplicates (e.g. custom domain + *.myshopify.com)
so that no extracted data is prematurely lost.
"""

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def normalize_domain(raw_url: str) -> str:
    """Normalize a domain string by stripping protocols, www, ports, paths, and slashes."""
    if not raw_url:
        return ""
    val = raw_url.strip().lower()
    if not val.startswith(("http://", "https://")):
        val = "https://" + val
    try:
        parsed = urllib.parse.urlparse(val)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = re.sub(r"^www\.", "", domain)
        domain = domain.split(":")[0]  # strip port
        return domain.strip()
    except Exception:
        # Fallback regex
        clean = re.sub(r"^https?://(www\.)?", "", raw_url.strip().lower())
        return clean.split("/")[0].split(":")[0].strip()


def extract_brand_slug(domain: str) -> str:
    """Extract core brand identifier by removing TLDs and common affixes."""
    norm = normalize_domain(domain)
    # Strip common TLDs
    for suffix in [
        ".myshopify.com",
        ".co.in",
        ".net.in",
        ".org.in",
        ".com",
        ".in",
        ".co",
        ".store",
        ".shop",
        ".biz",
        ".net",
        ".org",
    ]:
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)]
            break
    # Remove hyphens and underscores for matching
    slug = re.sub(r"[-_]", "", norm)
    return slug


def merge_records(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two store records, preserving the most complete data for each field."""
    merged = dict(primary)

    # Prefer custom domain over *.myshopify.com for domain_url
    if "myshopify.com" in merged.get("domain_url", "") and "myshopify.com" not in secondary.get("domain_url", ""):
        merged["domain_url"] = secondary["domain_url"]

    # Fill in missing fields from secondary
    for key in [
        "emails",
        "phone_numbers",
        "socials",
        "category",
        "tagline_description",
        "logo_url",
        "state",
    ]:
        val_primary = merged.get(key, "").strip()
        val_secondary = secondary.get(key, "").strip()

        if not val_primary and val_secondary:
            merged[key] = val_secondary
        elif key == "emails" and val_primary and val_secondary and val_secondary not in val_primary:
            merged[key] = f"{val_primary}, {val_secondary}"
        elif key == "phone_numbers" and val_primary and val_secondary and val_secondary not in val_primary:
            merged[key] = f"{val_primary}, {val_secondary}"

    # Combine signals
    sigs_p = set(s.strip() for s in merged.get("signals_matched", "").split(",") if s.strip())
    sigs_s = set(s.strip() for s in secondary.get("signals_matched", "").split(",") if s.strip())
    combined_sigs = sorted(list(sigs_p.union(sigs_s)))
    merged["signals_matched"] = ", ".join(combined_sigs)

    return merged


def deduplicate_records(
    records: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Deduplicate records based on:
    1. Normalized exact domain match
    2. Brand slug match (e.g. brand.myshopify.com vs brand.in)
    Merges records to maximize field completion.

    Returns:
        (deduplicated_records, stats_dict)
    """
    initial_count = len(records)
    by_domain: Dict[str, Dict[str, Any]] = {}
    by_slug: Dict[str, str] = {}  # slug -> normalized domain

    merged_count = 0

    for rec in records:
        dom = normalize_domain(rec.get("domain_url", ""))
        if not dom:
            continue

        slug = extract_brand_slug(dom)

        # 1. Exact domain match
        if dom in by_domain:
            by_domain[dom] = merge_records(by_domain[dom], rec)
            merged_count += 1
            continue

        # 2. Brand slug match (same store hosted on myshopify + custom domain)
        if slug and len(slug) >= 4 and slug in by_slug:
            existing_dom = by_slug[slug]
            if existing_dom in by_domain:
                by_domain[existing_dom] = merge_records(by_domain[existing_dom], rec)
                merged_count += 1
                continue

        # New unique store
        by_domain[dom] = dict(rec)
        by_domain[dom]["domain_url"] = dom
        if slug and len(slug) >= 4:
            by_slug[slug] = dom

    deduped_list = list(by_domain.values())
    final_count = len(deduped_list)

    stats = {
        "initial_count": initial_count,
        "final_count": final_count,
        "duplicates_removed": initial_count - final_count,
        "records_merged": merged_count,
    }

    logger.info(
        f"Deduplication complete: {initial_count} initial -> {final_count} unique "
        f"({stats['duplicates_removed']} duplicates removed/merged)."
    )

    return deduped_list, stats
