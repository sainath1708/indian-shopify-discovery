"""
detect.py - Shopify & India Presence Confirmation Module
Implements multi-signal heuristic verification:
1. Shopify Confirmation:
   - GET /products.json check (valid Shopify products payload)
   - HTML detection (cdn.shopify.com, Shopify.theme, shopify-checkout, window.Shopify)
   - Meta generator tag containing "Shopify"
   - Assigns confidence: 'high' vs 'medium'

2. India Presence Confirmation:
   - Strong Signals: WHOIS registrant country == India (optional), .in TLD,
     GSTIN 15-char code, Indian address with 6-digit pincode.
   - Weak Signals: +91 phone, ₹ / INR currency, Indian state mentioned,
     "India" in footer copyright.
   - Verification rule: >= 1 Strong OR >= 2 Weak signals.
"""

import json
import logging
import re
import urllib.parse
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Optional WHOIS import
try:
    import whois
except ImportError:
    whois = None

INDIAN_STATES = [
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    "Delhi",
    "New Delhi",
    "Chandigarh",
    "Puducherry",
    "Jammu and Kashmir",
    "Ladakh",
]

MAJOR_INDIAN_METROS = [
    "Bengaluru",
    "Bangalore",
    "Mumbai",
    "Delhi",
    "New Delhi",
    "Chennai",
    "Kolkata",
    "Hyderabad",
    "Pune",
    "Ahmedabad",
    "Jaipur",
    "Surat",
    "Lucknow",
    "Gurgaon",
    "Gurugram",
    "Noida",
    "Kochi",
    "Cochin",
    "Indore",
    "Chandigarh",
    "Coimbatore",
    "Vadodara",
    "Ludhiana",
    "Agra",
    "Nashik",
]

GSTIN_PATTERN = re.compile(
    r"\b[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}\b"
)
PINCODE_PATTERN = re.compile(r"\b[1-9][0-9]{5}\b")
INDIAN_PHONE_PATTERN = re.compile(
    r"(?:\+91[\s-]?)?[6789]\d{9}\b|(?:\+91[\s-]?)?\d{5}[\s-]?\d{5}\b"
)
INR_CURRENCY_PATTERN = re.compile(
    r"(?:₹|\bINR\b|\bRs\.?\s*\d+)", re.IGNORECASE
)


# ----------------------------------------------------------------------
# STEP 2 — CONFIRM SHOPIFY
# ----------------------------------------------------------------------
def confirm_shopify(
    domain: str,
    html_text: Optional[str] = None,
    soup: Optional[BeautifulSoup] = None,
    session: Optional[requests.Session] = None,
    timeout: int = 8,
) -> Tuple[bool, str, List[str]]:
    """Verify if domain is a Shopify store.

    Returns:
        (is_shopify, confidence_level, matched_signals)
    """
    signals: List[str] = []

    if session is None:
        session = requests.Session()

    base_url = f"https://{domain}" if not domain.startswith("http") else domain

    # 1. Direct myshopify.com domain signal
    if "myshopify.com" in domain:
        signals.append("myshopify_subdomain")

    # 2. Check HTML markers
    if html_text:
        html_lower = html_text.lower()
        if "cdn.shopify.com" in html_lower:
            signals.append("cdn_shopify")
        if "shopify.theme" in html_text or "window.Shopify" in html_text:
            signals.append("shopify_theme_js")
        if (
            "shopify-checkout" in html_lower
            or "checkout.shopify.com" in html_lower
        ):
            signals.append("shopify_checkout")
        if "shopify-features" in html_lower:
            signals.append("shopify_features")

        # Meta generator tag
        if soup is not None:
            gen = soup.find("meta", attrs={"name": "generator"})
            if (
                gen
                and gen.get("content")
                and "shopify" in gen.get("content", "").lower()
            ):
                signals.append("meta_generator_shopify")
        elif "generator" in html_lower and "shopify" in html_lower:
            signals.append("meta_generator_shopify")

    # 3. Check /products.json endpoint if HTML signals are weak or to confirm high confidence
    # (Shopify standard public endpoint)
    if "cdn_shopify" in signals or len(signals) >= 1 or not html_text:
        products_url = f"{base_url.rstrip('/')}/products.json?limit=1"
        try:
            resp = session.get(products_url, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, dict) and "products" in data:
                        signals.append("products_json_valid")
                except Exception:
                    pass
        except Exception:
            pass

    # Score confidence
    if "products_json_valid" in signals or len(signals) >= 2:
        confidence = "high"
        is_shopify = True
    elif len(signals) == 1:
        confidence = "medium"
        is_shopify = True
    else:
        confidence = "none"
        is_shopify = False

    return is_shopify, confidence, signals


# ----------------------------------------------------------------------
# STEP 3 — CONFIRM INDIAN
# ----------------------------------------------------------------------
def check_whois_india(domain: str, timeout: int = 5) -> bool:
    """Optional WHOIS registrant country check for India."""
    if whois is None:
        return False
    try:
        clean_dom = re.sub(
            r"^https?://", "", domain
        ).split("/")[0].split(":")[0]
        w = whois.whois(clean_dom)
        country = w.get("country")
        if country and (
            "IN" in str(country).upper() or "INDIA" in str(country).upper()
        ):
            return True
    except Exception:
        pass
    return False


def confirm_india(
    domain: str,
    html_text: str,
    soup: Optional[BeautifulSoup] = None,
    session: Optional[requests.Session] = None,
    skip_whois: bool = True,
    timeout: int = 8,
) -> Tuple[bool, List[str], Dict[str, str]]:
    """Verify if the store has strong or weak signals indicating Indian presence.

    Requires:
      At least ONE Strong signal OR at least TWO Weak signals.

    Strong Signals:
      - WHOIS country = India (optional / skippable)
      - .in / .co.in / .org.in TLD
      - GSTIN pattern detected (15-char code starting with 2-digit state code)
      - Indian address with 6-digit PIN code on contact/footer/about

    Weak Signals:
      - +91 phone number
      - ₹ / INR currency symbol
      - Indian state or major city name mentioned
      - "India" in footer copyright or company registration text

    Returns:
      (is_indian, matched_signals, signal_details)
    """
    strong_signals: List[str] = []
    weak_signals: List[str] = []
    details: Dict[str, str] = {}

    clean_dom = (
        re.sub(r"^https?://", "", domain).split("/")[0].split(":")[0].lower()
    )

    # 1. STRONG SIGNAL: .in TLD
    in_tlds = (
        ".in",
        ".co.in",
        ".net.in",
        ".org.in",
        ".ind.in",
        ".gen.in",
        ".firm.in",
    )
    if any(clean_dom.endswith(tld) for tld in in_tlds):
        weak_signals.append("in_tld")
        details["tld"] = clean_dom

    # 2. STRONG SIGNAL: WHOIS registrant country (if enabled)
    if not skip_whois:
        if check_whois_india(clean_dom, timeout=timeout):
            strong_signals.append("whois_registrant_india")
            details["whois"] = "IN"

    if not html_text:
        is_indian = len(strong_signals) >= 1
        return is_indian, strong_signals, details

    text_content = (
        soup.get_text(separator=" ", strip=True) if soup else html_text
    )

    # 3. STRONG SIGNAL: GSTIN pattern
    gstin_match = GSTIN_PATTERN.search(html_text)
    if gstin_match:
        gstin_val = gstin_match.group(0)
        strong_signals.append("gstin_found")
        details["gstin"] = gstin_val

    # 4. STRONG SIGNAL: 6-digit Indian PIN code near Indian context
    pincode_matches = PINCODE_PATTERN.findall(html_text)
    valid_pincode = None
    if pincode_matches:
        # Contextual check: ensure it's near an Indian state or city or 'Pin' / 'Postal'
        for pin in pincode_matches:
            # Indian pincodes start with 1-8 (except 9 for army postal)
            if pin[0] in "12345678":
                idx = html_text.find(pin)
                snippet = html_text[max(0, idx - 100) : min(len(html_text), idx + 100)].lower()
                if any(
                    k in snippet
                    for k in [
                        "india",
                        "pin",
                        "postal",
                        "state",
                        "delhi",
                        "mumbai",
                        "road",
                        "nagar",
                        "sector",
                        "street",
                    ]
                ):
                    valid_pincode = pin
                    strong_signals.append("indian_address_pincode")
                    details["pincode"] = pin
                    break

    # 5. WEAK SIGNAL: +91 Phone Number
    if "+91" in html_text or "091" in html_text:
        phone_match = INDIAN_PHONE_PATTERN.search(html_text)
        if phone_match:
            weak_signals.append("phone_plus91")
            details["phone"] = phone_match.group(0)

    # 6. WEAK SIGNAL: ₹ / INR Currency
    if (
        "₹" in html_text
        or "INR" in html_text
        or INR_CURRENCY_PATTERN.search(html_text)
    ):
        weak_signals.append("inr_currency")
        details["currency"] = "INR/₹"

    # 7. WEAK SIGNAL: Indian State / City Mentioned
    found_state = None
    for state in INDIAN_STATES:
        # Match as whole word
        if re.search(r"\b" + re.escape(state) + r"\b", text_content, re.IGNORECASE):
            found_state = state
            break

    if not found_state:
        for metro in MAJOR_INDIAN_METROS:
            if re.search(
                r"\b" + re.escape(metro) + r"\b", text_content, re.IGNORECASE
            ):
                found_state = metro
                break

    if found_state:
        weak_signals.append("indian_state_mentioned")
        details["state_signal"] = found_state

    # 8. WEAK SIGNAL: "India" in footer copyright / address
    footer_text = ""
    if soup:
        footer_elem = soup.find(["footer", "div"], class_=re.compile(r"footer|copyright", re.I))
        if footer_elem:
            footer_text = footer_elem.get_text()

    if not footer_text:
        # Fallback to last 2000 chars of HTML text
        footer_text = text_content[-2000:]

    if re.search(r"\bIndia\b", footer_text, re.IGNORECASE):
        weak_signals.append("india_in_footer")
        details["footer"] = "India"

    # Decision rule: >= 1 Strong OR >= 2 Weak signals
    all_matched = strong_signals + weak_signals
    is_indian = len(strong_signals) >= 1 or len(weak_signals) >= 2

    return is_indian, all_matched, details


# ----------------------------------------------------------------------
# Combined Store Verification
# ----------------------------------------------------------------------
def verify_store(
    domain: str,
    session: requests.Session,
    skip_whois: bool = True,
    timeout: int = 8,
) -> Tuple[bool, bool, str, List[str], Optional[str], Optional[BeautifulSoup]]:
    """Complete verification pipeline for a single store candidate.

    Fetches homepage once, then runs Shopify and India verification.

    Returns:
        (is_shopify, is_indian, confidence, all_signals, html_text, soup)
    """
    base_url = f"https://{domain}" if not domain.startswith("http") else domain
    html_text = None
    soup = None

    try:
        resp = session.get(base_url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            html_text = resp.text
            soup = BeautifulSoup(html_text, "html.parser")
    except Exception as e:
        logger.debug(f"Failed to fetch homepage for {domain}: {e}")

    # Check Shopify
    is_shopify, confidence, shopify_signals = confirm_shopify(
        domain, html_text, soup, session, timeout=timeout
    )

    if not is_shopify:
        return False, False, confidence, shopify_signals, html_text, soup

    # Check India
    is_indian, india_signals, _ = confirm_india(
        domain, html_text, soup, session, skip_whois=skip_whois, timeout=timeout
    )

    all_signals = shopify_signals + india_signals
    return is_shopify, is_indian, confidence, all_signals, html_text, soup


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    test_domains = ["snitch.co.in", "bewakoof.com", "google.com"]
    for d in test_domains:
        shop, ind, conf, sigs, _, _ = verify_store(d, s)
        print(
            f"Domain: {d} | Shopify: {shop} ({conf}) | India: {ind} | Signals:"
            f" {sigs}"
        )
