"""
main.py - Pipeline Orchestrator for Indian Shopify Store Discovery
Coordinates candidate sourcing, polite crawling (robots.txt & rate limiting),
Shopify + India verification, field extraction, deduplication, and export.
Goal: 700-1000+ high-quality verified Indian Shopify merchant records.
"""

import collections
import concurrent.futures
import csv
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.robotparser
from typing import Any, Dict, List, Optional

import requests
from dedup import deduplicate_records, normalize_domain
from detect import verify_store
from extract import extract_store_data
from sources import clean_domain, get_candidate_domains, get_default_session

# ----------------------------------------------------------------------
# PIPELINE CONFIGURATION
# ----------------------------------------------------------------------
CONFIG = {
    # Target number of verified Indian Shopify stores to output
    "TARGET_VERIFIED_STORES": 1000,
    # Maximum candidate domains to source and evaluate
    "MAX_CANDIDATES": 2500,
    # HTTP request timeout in seconds
    "REQUEST_TIMEOUT": 8,
    # Delay in seconds between successive requests to the SAME host (Etiquette)
    "PER_HOST_DELAY": 1.0,
    # Global requests per second limit across all worker threads
    "GLOBAL_RATE_LIMIT": 35,
    # Worker concurrency for network I/O
    "CONCURRENCY": 20,
    # Toggle WHOIS lookup (True = fast page-content signals only, False = slow WHOIS)
    "SKIP_WHOIS": True,
    # Respect robots.txt before fetching domains
    "RESPECT_ROBOTS_TXT": True,
    # Output paths
    "OUTPUT_DIR": "output",
    "CSV_FILENAME": "results.csv",
    "JSON_FILENAME": "results.json",
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("Pipeline")


# ----------------------------------------------------------------------
# Rate Limiting & Etiquette Classes
# ----------------------------------------------------------------------
class RateLimiter:
    """Thread-safe per-host delay tracker and global rate limiter."""

    def __init__(self, per_host_delay: float = 1.0, global_rate: int = 35):
        self.per_host_delay = per_host_delay
        self.min_global_interval = 1.0 / max(1, global_rate)
        self.last_host_time: Dict[str, float] = {}
        self.last_global_time: float = 0.0
        self.lock = threading.Lock()

    def wait_for_host(self, host: str):
        with self.lock:
            now = time.time()
            # 1. Global throttle
            elapsed_global = now - self.last_global_time
            if elapsed_global < self.min_global_interval:
                time.sleep(self.min_global_interval - elapsed_global)

            # 2. Per-host throttle
            last_time = self.last_host_time.get(host, 0.0)
            elapsed_host = now - last_time
            if elapsed_host < self.per_host_delay:
                sleep_needed = self.per_host_delay - elapsed_host
                time.sleep(sleep_needed)

            updated_now = time.time()
            self.last_global_time = updated_now
            self.last_host_time[host] = updated_now


class RobotsChecker:
    """Thread-safe robots.txt cache and etiquette validator."""

    def __init__(self, session: requests.Session, timeout: int = 5):
        self.session = session
        self.timeout = timeout
        self.cache: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self.lock = threading.Lock()

    def is_allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc or parsed.path.split("/")[0]
        if not host:
            return True

        with self.lock:
            if host in self.cache:
                parser = self.cache[host]
                if parser is None:
                    return True
                try:
                    return parser.can_fetch("*", url)
                except Exception:
                    return True

        # Fetch robots.txt outside lock
        robots_url = f"https://{host}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None
        except Exception:
            rp = None

        with self.lock:
            self.cache[host] = rp

        if rp is None:
            return True
        try:
            return rp.can_fetch("*", url)
        except Exception:
            return True


# ----------------------------------------------------------------------
# Worker Execution
# ----------------------------------------------------------------------
def process_candidate(
    domain: str,
    rate_limiter: RateLimiter,
    robots_checker: RobotsChecker,
    config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Process a single candidate domain through the pipeline stages:
    1. Robots.txt check
    2. Rate limiting pause
    3. Shopify & India confirmation
    4. Structured field extraction
    """
    clean_dom = clean_domain(domain)
    if not clean_dom:
        return None

    store_url = f"https://{clean_dom}"

    # 1. Robots.txt etiquette check
    if config["RESPECT_ROBOTS_TXT"]:
        if not robots_checker.is_allowed(store_url):
            logger.debug(f"Skipping {clean_dom}: Disallowed by robots.txt")
            return None

    # 2. Rate limiter wait
    rate_limiter.wait_for_host(clean_dom)

    # 3. Create thread-local session
    thread_session = get_default_session()

    # 4. Verify Shopify + India presence
    try:
        (
            is_shopify,
            is_indian,
            confidence,
            signals,
            html_text,
            soup,
        ) = verify_store(
            clean_dom,
            thread_session,
            skip_whois=config["SKIP_WHOIS"],
            timeout=config["REQUEST_TIMEOUT"],
        )
    except Exception as e:
        logger.debug(f"Verification error on {clean_dom}: {e}")
        return None

    if not is_shopify or not is_indian:
        return None

    # 5. Extract structured fields
    try:
        data = extract_store_data(
            domain=clean_dom,
            session=thread_session,
            html_text=html_text,
            soup=soup,
            signals_matched=signals,
            timeout=config["REQUEST_TIMEOUT"],
        )
        return data
    except Exception as e:
        logger.debug(f"Extraction error on {clean_dom}: {e}")
        return None


# ----------------------------------------------------------------------
# Output and Reporting Exporters
# ----------------------------------------------------------------------
def export_results(records: List[Dict[str, Any]], config: Dict[str, Any]):
    """Export records to CSV and JSON formats."""
    output_dir = config["OUTPUT_DIR"]
    os.makedirs(output_dir, exist_ok=True)

    csv_path = os.path.join(output_dir, config["CSV_FILENAME"])
    json_path = os.path.join(output_dir, config["JSON_FILENAME"])

    fieldnames = [
        "domain_url",
        "emails",
        "phone_numbers",
        "socials",
        "category",
        "tagline_description",
        "logo_url",
        "state",
        "signals_matched",
    ]

    # Write CSV
    with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for rec in records:
            # Guarantee every field is present
            row = {col: rec.get(col, "") for col in fieldnames}
            writer.writerow(row)

    # Write JSON
    with open(json_path, mode="w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    logger.info(f"Successfully exported {len(records)} records to:")
    logger.info(f" - CSV:  {csv_path}")
    logger.info(f" - JSON: {json_path}")


def print_data_quality_report(records: List[Dict[str, Any]]):
    """Generate and display missing field percentage statistics."""
    total = len(records)
    if total == 0:
        logger.warning("No records to report on.")
        return

    fields = [
        "domain_url",
        "emails",
        "phone_numbers",
        "socials",
        "category",
        "tagline_description",
        "logo_url",
        "state",
    ]

    print("\n" + "=" * 65)
    print("DATA QUALITY & FIELD COMPLETION REPORT")
    print("=" * 65)
    print(f"Total Verified Stores Extracted: {total}\n")
    print(f"{'Field Name':<25} | {'Present':<10} | {'Missing %':<10} | {'Status'}")
    print("-" * 65)

    for field in fields:
        present_count = sum(1 for r in records if r.get(field, "").strip())
        missing_count = total - present_count
        missing_pct = (missing_count / total) * 100
        status = "Healthy" if missing_pct < 40 else "Expected Gap"
        print(
            f"{field:<25} | {present_count:<10} | {missing_pct:>8.1f}% | {status}"
        )

    # Category distribution
    categories = collections.Counter(r.get("category", "Unknown") for r in records)
    print("\nCategory Breakdown (Top 8):")
    for cat, cnt in categories.most_common(8):
        print(f"  • {cat:<25}: {cnt} stores ({(cnt/total)*100:.1f}%)")

    # State distribution
    states = collections.Counter(r.get("state", "Unspecified") for r in records if r.get("state"))
    print("\nTop Merchant Geographic States:")
    for st, cnt in states.most_common(6):
        print(f"  • {st:<25}: {cnt} stores")

    print("=" * 65 + "\n")


# ----------------------------------------------------------------------
# Main Orchestration Loop
# ----------------------------------------------------------------------
def main():
    start_time = time.time()
    print("=" * 65)
    print("INDIAN SHOPIFY STORE DISCOVERY & EXTRACTION PIPELINE")
    print(f"Target Verified Records: {CONFIG['TARGET_VERIFIED_STORES']}+")
    print(f"Max Candidates Sourced:  {CONFIG['MAX_CANDIDATES']}")
    print(f"Concurrency Workers:     {CONFIG['CONCURRENCY']}")
    print(f"Robots.txt Etiquette:    {CONFIG['RESPECT_ROBOTS_TXT']}")
    print(f"Per-Host Delay:          {CONFIG['PER_HOST_DELAY']}s")
    print("=" * 65)

    session = get_default_session()

    # Step 1: Candidate Domain Discovery
    logger.info("Step 1: Sourcing candidate domains...")
    candidates = get_candidate_domains(
        session=session,
        target_count=CONFIG["MAX_CANDIDATES"],
        enable_search=True,
        enable_cc=True,
        enable_showcases=True,
    )

    total_candidates = min(len(candidates), CONFIG["MAX_CANDIDATES"])
    candidates = candidates[:total_candidates]
    logger.info(f"Sourced {total_candidates} unique candidate domains to evaluate.")

    rate_limiter = RateLimiter(
        per_host_delay=CONFIG["PER_HOST_DELAY"],
        global_rate=CONFIG["GLOBAL_RATE_LIMIT"],
    )
    robots_checker = RobotsChecker(
        session=session,
        timeout=CONFIG["REQUEST_TIMEOUT"],
    )

    verified_extracted_records: List[Dict[str, Any]] = []
    processed_count = 0
    target_goal = CONFIG["TARGET_VERIFIED_STORES"]

    # Step 2-4: Parallel Verification & Extraction
    logger.info("Steps 2-4: Executing verification & extraction pool...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["CONCURRENCY"]) as executor:
        future_to_domain = {
            executor.submit(
                process_candidate, dom, rate_limiter, robots_checker, CONFIG
            ): dom
            for dom in candidates
        }

        for future in concurrent.futures.as_completed(future_to_domain):
            dom = future_to_domain[future]
            processed_count += 1
            try:
                result = future.result()
                if result:
                    verified_extracted_records.append(result)
            except Exception as e:
                logger.debug(f"Candidate {dom} execution error: {e}")

            # Live progress output
            if processed_count % 25 == 0 or processed_count == total_candidates:
                verified_so_far = len(verified_extracted_records)
                pct = (processed_count / total_candidates) * 100
                logger.info(
                    f"Progress: Checked {processed_count}/{total_candidates} ({pct:.1f}%) | "
                    f"Verified: {verified_so_far} stores | "
                    f"Target: {target_goal}"
                )

            # Early exit if target goal reached with comfortable safety margin
            if len(verified_extracted_records) >= target_goal + 50:
                logger.info(
                    f"Target goal of {target_goal} verified stores reached! Finalizing queue..."
                )
                break

    # Step 5: Post-Extraction Deduplication
    logger.info("Step 5: Deduplicating and aliasing verified records...")
    deduped_records, dedup_stats = deduplicate_records(verified_extracted_records)

    # Step 6: Export results
    logger.info("Step 6: Exporting results to CSV and JSON...")
    export_results(deduped_records, CONFIG)

    # Data Quality Report
    print_data_quality_report(deduped_records)

    elapsed = time.time() - start_time
    logger.info(
        f"Pipeline execution finished in {elapsed:.1f}s ({elapsed/60:.2f} mins). "
        f"Total verified records saved: {len(deduped_records)}"
    )


if __name__ == "__main__":
    main()
