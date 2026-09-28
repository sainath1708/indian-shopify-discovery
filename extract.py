"""
extract.py - Field Extraction Module
Extracts 8 structured fields for each verified Indian Shopify store:
1. domain_url (custom domain if present, else *.myshopify.com)
2. emails (regex on footer/contact/about pages)
3. phone_numbers (regex, normalized to +91 format)
4. socials (deduplicated per platform: Instagram, Facebook, Twitter/X, LinkedIn, YouTube)
5. category (inferred from meta description, title, nav categories)
6. tagline_description (meta description or hero text)
7. logo_url (header/nav logo img, fallback to og:image)
8. state (parsed from address or pincode-to-state mapping)
"""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# PINCODE PREFIX TO STATE MAPPING
# ----------------------------------------------------------------------
PINCODE_PREFIX_TO_STATE = {
    "11": "Delhi",
    "12": "Haryana",
    "13": "Haryana",
    "14": "Punjab",
    "15": "Punjab",
    "16": "Punjab",
    "17": "Himachal Pradesh",
    "18": "Jammu and Kashmir",
    "19": "Jammu and Kashmir",
    "20": "Uttar Pradesh",
    "21": "Uttar Pradesh",
    "22": "Uttar Pradesh",
    "23": "Uttar Pradesh",
    "24": "Uttarakhand",
    "25": "Uttar Pradesh",
    "26": "Uttarakhand",
    "27": "Uttar Pradesh",
    "28": "Uttar Pradesh",
    "30": "Rajasthan",
    "31": "Rajasthan",
    "32": "Rajasthan",
    "33": "Rajasthan",
    "34": "Rajasthan",
    "36": "Gujarat",
    "37": "Gujarat",
    "38": "Gujarat",
    "39": "Gujarat",
    "40": "Maharashtra",
    "41": "Maharashtra",
    "42": "Maharashtra",
    "43": "Maharashtra",
    "44": "Maharashtra",
    "45": "Madhya Pradesh",
    "46": "Madhya Pradesh",
    "47": "Madhya Pradesh",
    "48": "Madhya Pradesh",
    "49": "Chhattisgarh",
    "50": "Telangana",
    "51": "Andhra Pradesh",
    "52": "Andhra Pradesh",
    "53": "Andhra Pradesh",
    "56": "Karnataka",
    "57": "Karnataka",
    "58": "Karnataka",
    "59": "Karnataka",
    "60": "Tamil Nadu",
    "61": "Tamil Nadu",
    "62": "Tamil Nadu",
    "63": "Tamil Nadu",
    "64": "Tamil Nadu",
    "67": "Kerala",
    "68": "Kerala",
    "69": "Kerala",
    "70": "West Bengal",
    "71": "West Bengal",
    "72": "West Bengal",
    "73": "West Bengal",
    "74": "West Bengal",
    "75": "Odisha",
    "76": "Odisha",
    "77": "Odisha",
    "78": "Assam",
    "79": "Meghalaya",  # NE region
    "80": "Bihar",
    "81": "Bihar",
    "82": "Jharkhand",
    "83": "Jharkhand",
    "84": "Bihar",
    "85": "Bihar",
}

INDIAN_STATES_LIST = [
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

CATEGORY_KEYWORDS = {
    "Apparel & Fashion": [
        "clothing",
        "apparel",
        "fashion",
        "wear",
        "dress",
        "shirt",
        "t-shirt",
        "jeans",
        "kurti",
        "saree",
        "ethnic",
        "lingerie",
        "denim",
        "hoodie",
        "streetwear",
        "trousers",
        "suit",
        "jacket",
        "kurta",
        "lehenga",
    ],
    "Beauty & Skincare": [
        "skincare",
        "beauty",
        "cosmetics",
        "serum",
        "makeup",
        "skin",
        "haircare",
        "shampoo",
        "lotion",
        "cream",
        "lip",
        "glow",
        "dermatology",
        "organic beauty",
        "body care",
        "sunscreen",
    ],
    "Jewellery & Accessories": [
        "jewellery",
        "jewelry",
        "silver",
        "gold",
        "diamond",
        "necklace",
        "earring",
        "bracelet",
        "ring",
        "gemstone",
        "ornaments",
        "bangle",
    ],
    "Electronics & Gadgets": [
        "electronics",
        "gadget",
        "audio",
        "earbuds",
        "headphone",
        "smartwatch",
        "speaker",
        "charger",
        "cable",
        "phone",
        "mobile",
        "appliance",
    ],
    "Food & Beverages": [
        "food",
        "coffee",
        "tea",
        "snack",
        "sweet",
        "beverage",
        "spices",
        "organic food",
        "honey",
        "cookies",
        "chai",
        "nutrition",
        "chocolate",
        "millet",
        "roasters",
    ],
    "Health & Wellness": [
        "wellness",
        "health",
        "ayurveda",
        "ayurvedic",
        "supplement",
        "protein",
        "vitamin",
        "fitness",
        "immunity",
        "herbal",
        "workout",
    ],
    "Home & Living": [
        "home decor",
        "furniture",
        "mattress",
        "furnishing",
        "bedsheet",
        "pillow",
        "cushion",
        "kitchenware",
        "cookware",
        "decor",
        "curtain",
        "tableware",
        "fragrance",
        "candle",
    ],
    "Footwear": [
        "footwear",
        "shoes",
        "sneakers",
        "sandals",
        "slippers",
        "loafers",
        "boots",
        "chappal",
        "heels",
    ],
    "Bags & Luggage": [
        "bag",
        "backpack",
        "luggage",
        "wallet",
        "handbag",
        "suitcase",
        "tote",
        "duffel",
    ],
    "Eyewear": [
        "eyewear",
        "glasses",
        "sunglasses",
        "frames",
        "spectacles",
        "lenses",
        "optical",
    ],
    "Pet Care": ["pet", "dog", "cat", "puppy", "kitten", "pet food"],
    "Baby & Kids": ["baby", "kids", "infant", "toddler", "maternity", "toys"],
}

EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"
)
PHONE_REGEX = re.compile(
    r"(?:(?:\+|0{0,2})91[\s-]?)?[6789]\d{4}[\s-]?\d{5}\b|(?:0\d{2,4}[\s-]?)?\d{6,8}\b"
)
PINCODE_REGEX = re.compile(r"\b[1-9][0-9]{5}\b")


# ----------------------------------------------------------------------
# Helper extraction functions
# ----------------------------------------------------------------------
def resolve_url(base_url: str, link: str) -> str:
    """Resolve relative URLs to absolute HTTP/HTTPS links."""
    if not link:
        return ""
    link = link.strip()
    if link.startswith("//"):
        return "https:" + link
    if link.startswith("/"):
        return urllib.parse.urljoin(base_url, link)
    if not link.startswith(("http://", "https://")):
        return urllib.parse.urljoin(base_url, "/" + link)
    return link


def extract_domain_url(
    domain: str, soup: Optional[BeautifulSoup] = None
) -> str:
    """Extract canonical/custom domain if present, else original domain."""
    if soup:
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            parsed = urllib.parse.urlparse(canonical["href"])
            if parsed.netloc and "myshopify.com" not in parsed.netloc:
                netloc = re.sub(r"^www\.", "", parsed.netloc.lower())
                return netloc
    return re.sub(r"^www\.", "", domain.lower().split("/")[0])


def extract_emails(
    html_text: str, soup: Optional[BeautifulSoup] = None
) -> List[str]:
    """Extract email addresses, filtering out bogus files/assets."""
    emails: Set[str] = set()

    # 1. Check mailto: links in soup
    if soup:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                email = href.split("mailto:")[1].split("?")[0].strip()
                if EMAIL_REGEX.match(email):
                    emails.add(email.lower())

    # 2. Check regex on text
    matches = EMAIL_REGEX.findall(html_text)
    invalid_endings = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".js",
        ".css",
    )
    invalid_domains = ("example.com", "domain.com", "sentry.io", "wix.com")

    for match in matches:
        m_lower = match.lower()
        if any(m_lower.endswith(ext) for ext in invalid_endings):
            continue
        if any(dom in m_lower for dom in invalid_domains):
            continue
        emails.add(m_lower)

    return sorted(list(emails))


def normalize_phone_number(raw_phone: str) -> Optional[str]:
    """Clean and normalize phone string to Indian '+91 XXXXX XXXXX' format."""
    digits = re.sub(r"\D", "", raw_phone)
    # Check for +91 prefix
    if digits.startswith("91") and len(digits) == 12:
        num = digits[2:]
        if num[0] in "6789":
            return f"+91 {num[:5]} {num[5:]}"
    elif digits.startswith("0") and len(digits) == 11:
        num = digits[1:]
        if num[0] in "6789":
            return f"+91 {num[:5]} {num[5:]}"
    elif len(digits) == 10 and digits[0] in "6789":
        return f"+91 {digits[:5]} {digits[5:]}"
    elif len(digits) >= 8 and len(digits) <= 12:
        return raw_phone.strip()
    return None


def extract_phone_numbers(
    html_text: str, soup: Optional[BeautifulSoup] = None
) -> List[str]:
    """Extract and normalize phone numbers."""
    phones: Set[str] = set()

    # 1. Check tel: links
    if soup:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("tel:"):
                raw_tel = href.split("tel:")[1].split("?")[0].strip()
                norm = normalize_phone_number(raw_tel)
                if norm:
                    phones.add(norm)

    # 2. Check regex on text
    matches = PHONE_REGEX.findall(html_text)
    for m in matches:
        norm = normalize_phone_number(m)
        if norm:
            phones.add(norm)

    return sorted(list(phones))


def extract_socials(
    soup: Optional[BeautifulSoup] = None,
) -> Dict[str, str]:
    """Extract and deduplicate social media profile links."""
    socials: Dict[str, str] = {}
    if not soup:
        return socials

    platforms = {
        "instagram": "instagram.com/",
        "facebook": "facebook.com/",
        "twitter": ("twitter.com/", "x.com/"),
        "linkedin": "linkedin.com/",
        "youtube": ("youtube.com/", "youtu.be/"),
    }

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        href_lower = href.lower()

        # Skip share / intent / policy links
        if any(
            skip in href_lower
            for skip in [
                "sharer",
                "intent",
                "/share",
                "policies",
                "dialog",
                "watch?v=",
            ]
        ):
            continue

        for plat, patterns in platforms.items():
            if plat in socials:
                continue
            if isinstance(patterns, tuple):
                if any(p in href_lower for p in patterns):
                    socials[plat] = href
            else:
                if patterns in href_lower:
                    socials[plat] = href

    return socials


def infer_category(
    title: str, description: str, text: str, soup: Optional[BeautifulSoup] = None
) -> str:
    """Infer store category based on title, description, and keywords."""
    combined = f"{title} {description}".lower()

    if soup:
        # Check nav bar links or menu items for strong category signals
        nav = soup.find(["nav", "header"])
        if nav:
            nav_text = nav.get_text(separator=" ").lower()
            combined += f" {nav_text[:500]}"

    scores: Dict[str, int] = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            # Check for keyword occurrences
            if re.search(r"\b" + re.escape(kw) + r"\b", combined):
                score += 1
        if score > 0:
            scores[cat] = score

    if scores:
        best_cat = max(scores.items(), key=lambda x: x[1])[0]
        return best_cat

    return "General E-Commerce"


def extract_tagline(
    soup: Optional[BeautifulSoup], html_text: str
) -> str:
    """Extract meta description or hero tagline text."""
    if soup:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if (
            meta_desc
            and meta_desc.get("content")
            and len(meta_desc["content"].strip()) > 5
        ):
            return meta_desc["content"].strip().replace("\n", " ")

        og_desc = soup.find("meta", property="og:description")
        if (
            og_desc
            and og_desc.get("content")
            and len(og_desc["content"].strip()) > 5
        ):
            return og_desc["content"].strip().replace("\n", " ")

        # Fallback to first H1 or H2
        h1 = soup.find(["h1", "h2"])
        if h1 and h1.get_text():
            tag = h1.get_text(strip=True)
            if 10 < len(tag) < 200:
                return tag

    return ""


def extract_logo_url(
    base_url: str, soup: Optional[BeautifulSoup]
) -> str:
    """Find actual store logo URL from header/nav img or og:image fallback."""
    if not soup:
        return ""

    # 1. Search inside header or nav
    header_nav = soup.find(["header", "nav"]) or soup.find(
        class_=re.compile(r"header|navbar|site-header", re.I)
    )
    candidates = []

    if header_nav:
        for img in header_nav.find_all("img"):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-srcset")
                or ""
            )
            alt = img.get("alt", "").lower()
            cls = " ".join(img.get("class", [])).lower()
            img_id = img.get("id", "").lower()

            if any(
                "logo" in k
                for k in [alt, cls, img_id, src.lower()]
            ):
                resolved = resolve_url(base_url, src)
                if resolved and not resolved.endswith(".svg"):
                    return resolved
                elif resolved:
                    candidates.append(resolved)

    # 2. Look across whole page for logo img
    for img in soup.find_all("img"):
        alt = img.get("alt", "").lower()
        cls = " ".join(img.get("class", [])).lower()
        src = (
            img.get("src")
            or img.get("data-src")
            or ""
        )
        if "logo" in alt or "logo" in cls or "logo" in src.lower():
            resolved = resolve_url(base_url, src)
            if resolved:
                return resolved

    if candidates:
        return candidates[0]

    # 3. Fallback: og:image
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        return resolve_url(base_url, og_img["content"].strip())

    return ""


def extract_state(
    html_text: str, soup: Optional[BeautifulSoup] = None
) -> str:
    """Parse Indian state from address text or fallback to 6-digit pincode mapping."""
    text_content = (
        soup.get_text(separator=" ", strip=True) if soup else html_text
    )

    # 1. Search for state names in contact / footer text
    for state in INDIAN_STATES_LIST:
        if re.search(
            r"\b" + re.escape(state) + r"\b", text_content, re.IGNORECASE
        ):
            return state

    # 2. Fallback: Search 6-digit pincode and map first 2 digits
    pins = PINCODE_REGEX.findall(html_text)
    for p in pins:
        prefix = p[:2]
        if prefix in PINCODE_PREFIX_TO_STATE:
            return PINCODE_PREFIX_TO_STATE[prefix]

    return ""


# ----------------------------------------------------------------------
# Main Field Extraction Function
# ----------------------------------------------------------------------
def extract_store_data(
    domain: str,
    session: requests.Session,
    html_text: Optional[str] = None,
    soup: Optional[BeautifulSoup] = None,
    signals_matched: Optional[List[str]] = None,
    timeout: int = 8,
) -> Dict[str, Any]:
    """Extract all 8 required fields + debug signals for a verified store."""
    base_url = f"https://{domain}" if not domain.startswith("http") else domain

    if not html_text or not soup:
        try:
            resp = session.get(base_url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                html_text = resp.text
                soup = BeautifulSoup(html_text, "html.parser")
        except Exception as e:
            logger.debug(f"Failed to fetch {domain} for extraction: {e}")

    # Fallbacks for empty page
    if not html_text:
        html_text = ""
    title = soup.title.string.strip() if (soup and soup.title and soup.title.string) else ""

    # 1. domain_url
    domain_url = extract_domain_url(domain, soup)

    # 2. emails
    emails = extract_emails(html_text, soup)

    # 3. phone_numbers
    phone_numbers = extract_phone_numbers(html_text, soup)

    # 4. socials
    socials_dict = extract_socials(soup)
    socials_str = json.dumps(socials_dict, ensure_ascii=False) if socials_dict else ""

    # 5. tagline_description
    tagline_description = extract_tagline(soup, html_text)

    # 6. category
    category = infer_category(title, tagline_description, html_text, soup)

    # 7. logo_url
    logo_url = extract_logo_url(base_url, soup)

    # 8. state
    state = extract_state(html_text, soup)

    # If contact info (email/phone/state) is missing, check contact page
    if not emails or not phone_numbers or not state:
        for path in ["/pages/contact", "/contact-us", "/pages/contact-us", "/about-us"]:
            try:
                contact_url = urllib.parse.urljoin(base_url, path)
                c_resp = session.get(contact_url, timeout=5)
                if c_resp.status_code == 200:
                    c_soup = BeautifulSoup(c_resp.text, "html.parser")
                    if not emails:
                        emails = extract_emails(c_resp.text, c_soup)
                    if not phone_numbers:
                        phone_numbers = extract_phone_numbers(c_resp.text, c_soup)
                    if not state:
                        state = extract_state(c_resp.text, c_soup)
                    if emails and phone_numbers and state:
                        break
            except Exception:
                pass

    return {
        "domain_url": domain_url,
        "emails": ", ".join(emails) if emails else "",
        "phone_numbers": ", ".join(phone_numbers) if phone_numbers else "",
        "socials": socials_str,
        "category": category,
        "tagline_description": tagline_description,
        "logo_url": logo_url,
        "state": state,
        "signals_matched": ", ".join(signals_matched) if signals_matched else "",
    }
