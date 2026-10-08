"""Shop adapters: each turns a URL into a list of items (one per size/variant).

Every item is a dict:
    key        stable id, used to spot changes between scans
    name       product name as the shop shows it
    variant    size/package text ("40g can"), may be ""
    url        link to buy
    available  True / False
    price      float in the site's currency, or None
    brand_hint text that may name the brand (vendor, "Maker: ..."), may be ""

Adapters:
    shopify      any Shopify store (Matcha Miyako, MatchaJP)
    woocommerce  Marukyu Koyamaen official shop (WordPress product pages)
    bigcommerce  BigCommerce stores (TeaLife / japanesetea.sg)
    sazen        Sazen Tea
    html         generic fallback for any other single product page
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MatchaAlert/1.0"
)
REQUEST_DELAY = 0.7  # seconds between requests to the same shop; be polite


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def http_get(url: str, timeout: int = 25, retries: int = 2, data: bytes | None = None, headers: dict | None = None) -> str:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        h = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
             "Accept-Language": "en"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (400, 401, 403, 404, 410):
                break
            time.sleep(5 * (attempt + 1) if e.code == 429 else 2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{last_err}")


def get_json(url: str):
    return json.loads(http_get(url))


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|th|h\d|dt|dd|span|label|option|a|button)>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page)
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", ln).strip() for ln in page.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def page_title(page: str, fallback: str) -> str:
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, page, re.S | re.I)
        if m and strip_tags(m.group(1)):
            return strip_tags(m.group(1))
    return fallback


def _item(key, name, url, available, price=None, variant="", brand_hint=""):
    return {"key": key, "name": name, "variant": variant, "url": url, "available": bool(available),
            "price": price, "brand_hint": brand_hint}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


OUT_OF_STOCK_WORDS = ["out of stock", "sold out", "currently unavailable", "temporarily unavailable",
                      "not available", "no stock", "unavailable"]


# --------------------------------------------------------------------------
# Shopify
# --------------------------------------------------------------------------

def _split(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}", p.path.rstrip("/")


def _shopify_items(base, product, price_in_cents):
    handle, title = product["handle"], product["title"]
    variants = product.get("variants") or []
    out = []
    for v in variants:
        vt = v.get("title") or ""
        if vt == "Default Title":
            vt = ""
        price = _num(v.get("price"))
        if price is not None and price_in_cents:
            price = price / 100
        out.append(_item(
            key=f"{base}/products/{handle}#{v.get('id')}",
            name=title, variant=vt,
            url=f"{base}/products/{handle}" + (f"?variant={v['id']}" if len(variants) > 1 else ""),
            available=v.get("available", False), price=price,
            brand_hint=product.get("vendor", ""),
        ))
    return out


def shopify_product(url, site):
    base, path = _split(url)
    m = re.search(r"/products/([^/?#]+)", path)
    if not m:
        raise ValueError("not a Shopify product URL")
    handle = m.group(1)
    try:
        return _shopify_items(base, get_json(f"{base}/products/{handle}.js"), price_in_cents=True)
    except Exception:
        return _shopify_items(base, get_json(f"{base}/products/{handle}.json")["product"], price_in_cents=False)


def shopify_collection(url, site):
    base, path = _split(url)
    m = re.search(r"/collections/([^/?#]+)", path)
    if not m:
        raise ValueError("not a Shopify collection URL")
    out = []
    for page in range(1, 21):
        products = get_json(f"{base}/collections/{m.group(1)}/products.json?limit=250&page={page}").get("products") or []
        for p in products:
            out.extend(_shopify_items(base, p, price_in_cents=False))
        if len(products) < 250:
            break
        time.sleep(REQUEST_DELAY)
    return out


# --------------------------------------------------------------------------
# WooCommerce / Marukyu Koyamaen official shop
#
# Product pages list each size as "SKU <code> Size <20g can> ¥3,700 ...".
# WooCommerce pages usually also embed data-product_variations JSON with a
# per-size is_in_stock flag; we use it when present. A product where every
# size is gone shows "This product is currently out of stock and unavailable."
# --------------------------------------------------------------------------

def woocommerce_product(url, site):
    page = http_get(url)
    name = page_title(page, url)
    name = re.sub(r"\s*[|｜–-]\s*(Marukyu|丸久).*$", "", name).strip()
    whole_product_out = "currently out of stock and unavailable" in page.lower()

    items = []
    m = re.search(r'data-product_variations="([^"]*)"', page)
    if m and m.group(1) not in ("false", ""):
        try:
            variations = json.loads(html.unescape(m.group(1)))
        except ValueError:
            variations = []
        for v in variations:
            size = " ".join(str(x) for x in (v.get("attributes") or {}).values()).replace("-", " ").strip()
            sku = v.get("sku") or str(v.get("variation_id"))
            items.append(_item(f"{url}#{sku}", name, url, v.get("is_in_stock", False),
                               _num(v.get("display_price")), size or sku))
        if items:
            return items

    text = html_to_text(page)
    rows = re.findall(r"SKU\s*\n\s*([0-9A-Za-z-]+)\s*\n\s*Size\s*\n\s*([^\n]+?)\s*\n\s*¥\s*([\d,]+)", text)
    if rows:
        # Raw HTML chunk for each SKU, to look for a per-size sold-out marker
        for sku, size, yen in rows:
            avail = not whole_product_out
            i = page.find(sku)
            if i >= 0 and avail:
                j = page.find("SKU", i + len(sku))
                chunk = page[i:j if j > 0 else i + 3000].lower()
                if re.search(r"out[-_ ]?of[-_ ]?stock|sold[-_ ]?out|soldout|nostock|no-stock", chunk):
                    avail = False
            items.append(_item(f"{url}#{sku.upper()}", name, url, avail, _num(yen), size))
        return items

    # No size table found: one product-level item
    price = re.search(r"¥\s*([\d,]+)", text)
    return [_item(url, name, url, not whole_product_out, _num(price.group(1)) if price else None)]


def woocommerce_catalog(url, site):
    """Category page -> visit each product page in it."""
    page = http_get(url)
    base, _ = _split(url)
    links = []
    for href, label in re.findall(r'<a[^>]+href="([^"]*/shop/products/[0-9a-z]{8,12}/?)"[^>]*>(.*?)</a>', page, re.S | re.I):
        full = urllib.parse.urljoin(base + "/", href)
        links.append((full.rstrip("/"), strip_tags(label)))
    seen, out = set(), []
    for link, label in links:
        if link in seen:
            continue
        seen.add(link)
        if _excluded(label or link, site):
            continue
        time.sleep(REQUEST_DELAY)
        out.extend(woocommerce_product(link, site))
    return out


# --------------------------------------------------------------------------
# BigCommerce (TeaLife / japanesetea.sg)
#
# A product page has the product id and its size options. For each size we
# ask the shop's own storefront endpoint whether it's in stock and its price,
# the same call the page makes when you click a size.
# --------------------------------------------------------------------------

def _bc_options(page):
    """Return {attribute_id: [(value_id, label), ...]} for the product's options."""
    opts = {}
    for attr, body in re.findall(r'<select[^>]+name="attribute\[(\d+)\]"[^>]*>(.*?)</select>', page, re.S | re.I):
        for val, label in re.findall(r'<option[^>]+value="(\d+)"[^>]*>(.*?)</option>', body, re.S | re.I):
            opts.setdefault(attr, []).append((val, strip_tags(label)))
    for attr, val in re.findall(r'id="attribute_[a-z]+_*(\d+)_(\d+)"', page, re.I):
        lab = re.search(rf'<label[^>]+for="attribute_[a-z]+_*{attr}_{val}"[^>]*>(.*?)</label>', page, re.S | re.I)
        label = strip_tags(lab.group(1)) if lab else val
        if (val, label) not in opts.get(attr, []):
            opts.setdefault(attr, []).append((val, label))
    return opts


def bigcommerce_product(url, site):
    page = http_get(url)
    base, _ = _split(url)
    name = page_title(page, url)
    name = re.sub(r"\s*[-|–]\s*TeaLife.*$", "", name, flags=re.I)
    brand = ""
    bm = re.search(r'<[^>]+class="[^"]*productView-brand[^"]*"[^>]*>(.*?)</', page, re.S | re.I)
    if bm:
        brand = strip_tags(bm.group(1))
    pid = re.search(r'<input[^>]+name="product_id"[^>]+value="(\d+)"', page) or \
        re.search(r'<input[^>]+value="(\d+)"[^>]+name="product_id"', page)
    meta_price = re.search(r'<meta[^>]+property="product:price:amount"[^>]+content="([\d.,]+)"', page)
    page_price = _num(meta_price.group(1)) if meta_price else None
    page_text = html_to_text(page).lower()
    page_out = any(w in page_text for w in ("out of stock", "sold out")) and "add to cart" not in page_text

    opts = _bc_options(page)
    if pid and len(opts) == 1:
        attr, values = next(iter(opts.items()))
        items = []
        for val, label in values:
            body = urllib.parse.urlencode({"action": "add", "product_id": pid.group(1),
                                           f"attribute[{attr}]": val, "qty[]": "1"}).encode()
            try:
                time.sleep(REQUEST_DELAY)
                data = json.loads(http_get(f"{base}/remote/v1/product-attributes/{pid.group(1)}", data=body,
                                           headers={"X-Requested-With": "XMLHttpRequest", "stencil-options": "{}",
                                                    "stencil-config": "{}"}))["data"]
                price = data.get("price") or {}
                p = (price.get("with_tax") or price.get("without_tax") or {}).get("value")
                avail = bool(data.get("instock", True)) and bool(data.get("purchasable", True))
            except Exception:
                p, avail = None, not page_out  # endpoint not usable: fall back to page-level status
            items.append(_item(f"{url}#{attr}-{val}", name, url, avail, _num(p), label, brand))
        if items:
            return items
    return [_item(url, name, url, not page_out, page_price, "", brand)]


def bigcommerce_category(url, site):
    page = http_get(url)
    base, _ = _split(url)
    links = []
    for href in re.findall(r'href="([^"]+)"', page):
        full = urllib.parse.urljoin(url, html.unescape(href)).split("#")[0].split("?")[0]
        if full.startswith(base) and re.search(site.get("product_link_pattern", r"/prd/[^/]+/?$"), full):
            if full not in links:
                links.append(full)
    out = []
    for link in links:
        if _excluded(link, site) or not _link_allowed(link, site):
            continue
        time.sleep(REQUEST_DELAY)
        out.extend(bigcommerce_product(link, site))
    return out


# --------------------------------------------------------------------------
# Sazen Tea
# --------------------------------------------------------------------------

def sazen_product(url, site):
    page = http_get(url)
    text = html_to_text(page)
    low = text.lower()
    name = page_title(page, url)
    name = re.sub(r"^Buy\s+", "", name)
    name = re.sub(r"\s*(Outlet Sale)?\s*-\s*Sazen Tea.*$", "", name).strip()
    maker = re.search(r"Maker:\s*([^\n]+)", text)
    grams = re.search(r"Net weight:\s*(\d+)\s*g", text, re.I)
    pack = re.search(r"\b(can|bag|box|tin)\b", (re.search(r"Unit price:[^\n]*", text) or [""])[0], re.I)
    variant = " ".join(x for x in [f"{grams.group(1)}g" if grams else "", pack.group(1).lower() if pack else ""] if x)
    if "in stock" in low or "ready to ship" in low:
        avail = True
    elif any(w in low for w in OUT_OF_STOCK_WORDS):
        avail = False
    else:
        avail = "add to cart" in low
    price = None
    for pat in (r'data-price="([\d.]+)"', r'itemprop="price"[^>]*content="([\d.]+)"', r'"price"\s*:\s*"?([\d.]+)'):
        pm = re.search(pat, page)
        if pm:
            price = _num(pm.group(1))
            break
    return [_item(url, name, url, avail, price, variant, maker.group(1).strip() if maker else "")]


def sazen_category(url, site):
    page = http_get(url)
    links = []
    for href in re.findall(r'href="([^"]*/en/products/p\d+-[^"]+\.html)"', page):
        full = urllib.parse.urljoin(url, href)
        if full not in links:
            links.append(full)
    out = []
    for link in links:
        if _excluded(link, site) or not _link_allowed(link, site):
            continue
        time.sleep(REQUEST_DELAY)
        out.extend(sazen_product(link, site))
    return out


# --------------------------------------------------------------------------
# Generic single product page
# --------------------------------------------------------------------------

def html_product(url, site):
    page = http_get(url)
    name = page_title(page, url)
    available = None
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        avail = re.findall(r'"availability"\s*:\s*"([^"]+)"', block)
        if avail:
            available = any(a.rstrip("/").endswith(("InStock", "LimitedAvailability", "PreOrder")) for a in avail)
            break
    if available is None:
        m = re.search(r'itemprop="availability"[^>]*(?:content|href)="([^"]+)"', page)
        if m:
            available = m.group(1).rstrip("/").endswith(("InStock", "LimitedAvailability", "PreOrder"))
    if available is None:
        low = html_to_text(page).lower()
        outs = [s.lower() for s in site.get("out_of_stock_text", ["sold out", "out of stock", "currently unavailable"])]
        ins = [s.lower() for s in site.get("in_stock_text", ["add to cart", "add to bag", "buy now"])]
        if any(s in low for s in outs):
            available = False
        elif any(s in low for s in ins):
            available = True
        else:
            raise RuntimeError("couldn't tell stock status; set in_stock_text/out_of_stock_text in config")
    pm = re.search(r'(?:product:price:amount|itemprop="price")[^>]*content="([\d.,]+)"', page)
    return [_item(url, name, url, available, _num(pm.group(1)) if pm else None)]


# --------------------------------------------------------------------------
# Filters and registry
# --------------------------------------------------------------------------

def _excluded(text, site):
    low = text.lower()
    return any(w.lower() in low for w in site.get("exclude_keywords", []))


def _link_allowed(link, site):
    need = site.get("link_must_contain")
    return not need or any(w.lower() in link.lower() for w in need)


ADAPTERS = {
    "shopify": {"product": shopify_product, "collection": shopify_collection},
    "woocommerce": {"product": woocommerce_product, "collection": woocommerce_catalog},
    "bigcommerce": {"product": bigcommerce_product, "collection": bigcommerce_category},
    "sazen": {"product": sazen_product, "collection": sazen_category},
    "html": {"product": html_product},
}
