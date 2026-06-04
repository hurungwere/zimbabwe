"""
VitaCare Placeholder Replacer
==============================
Detects downloaded images that are actually the default "400x400" placeholder image
(MD5: 688b552a8cde78d033f1e47d9ba853fa, Size: 3269 bytes).
For each detected placeholder, it:
1. Deletes the local placeholder file.
2. Searches the web (DuckDuckGo, Bing, Google, Open Food Facts, Open Beauty Facts, Wikimedia)
   using the product name and category.
3. Downloads candidate images and hashes them to ensure they aren't also placeholders.
4. If a valid new image is found, saves it and updates the DB.
5. If no valid image is found, updates the DB to NULL/empty so Kivy uses its default fallback.
"""

import os
import sqlite3
import requests
import urllib.parse
import re
import time
import hashlib
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Tuple

# ─── Configuration ────────────────────────────────────────────────────────────

DB_PATH = "products.db"
OUTPUT_DIR = "assets/products"
TIMEOUT = 10
WORKERS = 8
RETRY_DELAY = 1.0

# Identified placeholder fingerprint
PLACEHOLDER_HASH = "688b552a8cde78d033f1e47d9ba853fa"
PLACEHOLDER_SIZE = 3269

# Words in image URLs that suggest irrelevant / bad matches
IRRELEVANT_URL_PATTERNS = [
    r"ultrasound", r"anatomy", r"medical.illustration", r"stock.photo",
    r"placeholder", r"no.?image", r"coming.?soon", r"sample",
    r"darth", r"star.?wars",
]
IRRELEVANT_RE = re.compile("|".join(IRRELEVANT_URL_PATTERNS), re.I)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ─── Helper Functions ─────────────────────────────────────────────────────────

def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,image/png,image/jpeg,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def clean_name(name: str) -> str:
    """Simplify product name for search queries."""
    # Remove sizes/weights (e.g. "200ML", "100G", "2PCK 0-6MTHS")
    name = re.sub(r"\b\d+(?:[.-]\d+)?\s*(?:ML|G|KG|MG|L|PCK|PC|PCS|MTHS|S|PACK)\b", "", name, flags=re.I)
    # Remove short all-caps codes that are internal SKU-like tokens
    name = re.sub(r"\b[A-Z]{1,3}\b", "", name)
    # Remove trailing/leading whitespace and reduce spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name


def build_query(name: str, category: str = "") -> str:
    """Build a targeted search query using product name and category hints."""
    q = clean_name(name)
    cat_lower = (category or "").lower()
    if any(k in cat_lower for k in ["food", "formula", "snack", "confection"]):
        q += " food product"
    elif any(k in cat_lower for k in ["skin", "beauty", "care", "makeup", "lip", "hair"]):
        q += " beauty product"
    elif any(k in cat_lower for k in ["pharma", "medicine", "counter", "rehab"]):
        q += " pharmacy product"
    else:
        q += " product"
    return q


def is_relevant_url(url: str) -> bool:
    return not IRRELEVANT_RE.search(url)


# ─── Image Source Searchers ───────────────────────────────────────────────────

def search_open_food_facts(name: str, session: requests.Session, **kwargs) -> List[str]:
    query = urllib.parse.quote(clean_name(name))
    url = f"https://world.openfoodfacts.org/cgi/search.pl?search_terms={query}&json=1&page_size=5"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        data = resp.json()
        imgs = []
        for prod in data.get("products", []):
            img = prod.get("image_front_url") or prod.get("image_url")
            if img and img.startswith("http") and is_relevant_url(img):
                imgs.append(img)
        return imgs
    except Exception:
        pass
    return []


def search_open_beauty_facts(name: str, session: requests.Session, **kwargs) -> List[str]:
    query = urllib.parse.quote(clean_name(name))
    url = f"https://world.openbeautyfacts.org/cgi/search.pl?search_terms={query}&json=1&page_size=5"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        data = resp.json()
        imgs = []
        for prod in data.get("products", []):
            img = prod.get("image_front_url") or prod.get("image_url")
            if img and img.startswith("http") and is_relevant_url(img):
                imgs.append(img)
        return imgs
    except Exception:
        pass
    return []


def search_duckduckgo_images(name: str, session: requests.Session, category: str = "") -> List[str]:
    q = build_query(name, category)
    query = urllib.parse.quote(q)
    try:
        resp = session.get(f"https://duckduckgo.com/?q={query}&iax=images&ia=images", timeout=TIMEOUT)
        vqd_match = re.search(r'vqd=([\d-]+)', resp.text)
        if not vqd_match:
            return []
        vqd = vqd_match.group(1)

        api_url = f"https://duckduckgo.com/i.js?q={query}&o=json&vqd={vqd}&f=,,,,,&l=wt-wt"
        resp2 = session.get(api_url, timeout=TIMEOUT)
        data = resp2.json()
        imgs = []
        for result in data.get("results", [])[:10]:
            img = result.get("image")
            if img and img.startswith("http") and is_relevant_url(img):
                imgs.append(img)
        return imgs
    except Exception:
        pass
    return []


def search_bing_images(name: str, session: requests.Session, category: str = "") -> List[str]:
    q = build_query(name, category)
    query = urllib.parse.quote(q)
    url = f"https://www.bing.com/images/search?q={query}&first=1&count=10"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        matches = re.findall(r'murl&quot;:&quot;(https?://[^&"]+?)&quot;', resp.text)
        if not matches:
            matches = re.findall(r'"murl":"(https?://[^"]+)"', resp.text)
        imgs = []
        for m in matches:
            if any(ext in m.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                if is_relevant_url(m):
                    imgs.append(m)
        return imgs
    except Exception:
        pass
    return []


def search_google_images(name: str, session: requests.Session, category: str = "") -> List[str]:
    q = build_query(name, category)
    query = urllib.parse.quote(q)
    url = f"https://www.google.com/search?tbm=isch&q={query}&num=10"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        matches = re.findall(r'"(https?://(?!encrypted)[^"]+\.(?:jpg|jpeg|png|webp))"', resp.text)
        imgs = []
        for m in matches:
            if is_relevant_url(m):
                imgs.append(m)
        return imgs
    except Exception:
        pass
    return []


def search_wikimedia(name: str, session: requests.Session, **kwargs) -> List[str]:
    query = urllib.parse.quote(clean_name(name))
    url = (
        f"https://commons.wikimedia.org/w/api.php?action=query&list=search"
        f"&srsearch={query}&srnamespace=6&srlimit=3&format=json"
    )
    try:
        resp = session.get(url, timeout=TIMEOUT)
        data = resp.json()
        imgs = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "").replace(" ", "_")
            if title.startswith("File:"):
                title_enc = urllib.parse.quote(title)
                info_url = (
                    f"https://commons.wikimedia.org/w/api.php?action=query"
                    f"&titles={title_enc}&prop=imageinfo&iiprop=url&format=json"
                )
                info_resp = session.get(info_url, timeout=TIMEOUT)
                info_data = info_resp.json()
                pages = info_data.get("query", {}).get("pages", {})
                for page in pages.values():
                    img = page.get("imageinfo", [{}])[0].get("url")
                    if img:
                        imgs.append(img)
        return imgs
    except Exception:
        pass
    return []


SOURCES = [
    ("Open Food Facts",   search_open_food_facts),
    ("Open Beauty Facts", search_open_beauty_facts),
    ("DuckDuckGo Images", search_duckduckgo_images),
    ("Bing Images",       search_bing_images),
    ("Google Images",     search_google_images),
    ("Wikimedia Commons", search_wikimedia),
]

# ─── Orchestration ────────────────────────────────────────────────────────────

def get_extension_from_headers(headers: dict, url: str) -> str:
    content_type = headers.get("Content-Type", "").lower()
    if "image/png" in content_type:
        return ".png"
    elif "image/jpeg" in content_type or "image/jpg" in content_type:
        return ".jpg"
    elif "image/webp" in content_type:
        return ".webp"
    elif "image/gif" in content_type:
        return ".gif"
    
    path_ext = os.path.splitext(url.split("?")[0])[1].lower()
    if path_ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
        return path_ext
    return ".jpg"


def test_and_download_image(url: str, product_id: str, session: requests.Session) -> Optional[str]:
    """Downloads an image from URL, verifies it is not a placeholder, and returns relative local path."""
    try:
        resp = session.get(url, timeout=TIMEOUT, stream=True)
        if resp.status_code != 200:
            return None
        
        # Quick pre-check on Content-Length
        cl = resp.headers.get("Content-Length")
        if cl and int(cl) == PLACEHOLDER_SIZE:
            return None
        if cl and int(cl) < 2048: # reject tiny files
            return None

        # Read content
        content = resp.content
        
        # Check md5 hash of the downloaded content
        h = hashlib.md5(content).hexdigest()
        if h == PLACEHOLDER_HASH or len(content) == PLACEHOLDER_SIZE or len(content) < 2048:
            return None

        # Determine extension and write
        ext = get_extension_from_headers(resp.headers, url)
        filename = f"{product_id}{ext}"
        relative_path = os.path.join(OUTPUT_DIR, filename)
        absolute_path = os.path.join(os.getcwd(), relative_path)

        with open(absolute_path, "wb") as f:
            f.write(content)

        return relative_path
    except Exception:
        pass
    return None


def process_broken_product(row: tuple) -> dict:
    """Takes a product row, finds a replacement image, downloads it, and returns result status."""
    product_id, product_name, product_category, current_path = row
    session = get_session()

    result = {
        "product_id": product_id,
        "product_name": product_name,
        "status": "failed",
        "new_path": "",
        "source": ""
    }

    # Delete local placeholder file if it exists
    if current_path:
        full_path = os.path.join(os.getcwd(), current_path)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except Exception:
                pass

    # Gather search candidate URLs from all sources
    for source_name, search_fn in SOURCES:
        try:
            import inspect
            sig = inspect.signature(search_fn)
            if "category" in sig.parameters:
                urls = search_fn(product_name, session, category=product_category or "")
            else:
                urls = search_fn(product_name, session)
            
            for url in urls:
                # Try downloading and validating this candidate image URL
                local_path = test_and_download_image(url, product_id, session)
                if local_path:
                    result["status"] = "success"
                    result["new_path"] = local_path
                    result["source"] = source_name
                    return result
        except Exception:
            pass
        time.sleep(RETRY_DELAY)

    return result


def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 1. Identify products whose current image path is a placeholder
    c.execute("SELECT product_id, product_name, product_category, product_image FROM products")
    all_products = c.fetchall()

    placeholders_to_fix = []
    for pid, name, cat, img_path in all_products:
        if img_path:
            full_path = os.path.join(os.getcwd(), img_path)
            if os.path.exists(full_path):
                # Check size and MD5 hash
                sz = os.path.getsize(full_path)
                if sz == PLACEHOLDER_SIZE:
                    placeholders_to_fix.append((pid, name, cat, img_path))
                else:
                    try:
                        with open(full_path, "rb") as f:
                            h = hashlib.md5(f.read()).hexdigest()
                        if h == PLACEHOLDER_HASH:
                            placeholders_to_fix.append((pid, name, cat, img_path))
                    except Exception:
                        pass
            else:
                # File is missing locally - treat it as a placeholder to fix!
                placeholders_to_fix.append((pid, name, cat, img_path))
        else:
            # No image URL/path at all - fix it!
            placeholders_to_fix.append((pid, name, cat, img_path))

    print(f"\nFound {len(placeholders_to_fix)} products to find real images for.")
    if not placeholders_to_fix:
        print("No products need fixing. Exiting.")
        conn.close()
        return

    print(f"Running fixes using {WORKERS} concurrent workers...\n")

    updates = []
    failures = 0
    successes = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(process_broken_product, row): row for row in placeholders_to_fix}

        for i, future in enumerate(as_completed(futures), 1):
            res = future.result()
            row = futures[future]
            
            if res["status"] == "success":
                successes += 1
                print(f"[{i:3}/{len(placeholders_to_fix)}] ✔ FIXED [{res['source']}]: {res['product_name'][:40]} -> {res['new_path']}")
                updates.append((res["new_path"], res["product_id"]))
            else:
                failures += 1
                print(f"[{i:3}/{len(placeholders_to_fix)}] ✗ FAILED: {res['product_name'][:40]} (No valid image found)")
                # Set path to empty string so it falls back to noimage.jpg in the Kivy app
                updates.append(("", res["product_id"]))

    # Update database
    if updates:
        print(f"\nUpdating {len(updates)} database records...")
        c_write = conn.cursor()
        c_write.executemany(
            "UPDATE products SET product_image = ? WHERE product_id = ?",
            updates
        )
        conn.commit()
        print("✔ Database updated successfully.")

    conn.close()

    print("\n" + "="*50)
    print("  REPLACEMENT COMPLETE")
    print("="*50)
    print(f"  Successfully Replaced  : {successes}")
    print(f"  Failed (set to default): {failures}")
    print("="*50)


if __name__ == "__main__":
    main()
