"""Brands, sizes, manufacturer list prices and currency conversion."""
from __future__ import annotations

import re
import time

from adapters import get_json

SYMBOLS = {"USD": "US$", "SGD": "S$", "JPY": "¥", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "HKD": "HK$"}


# --------------------------------------------------------------------------
# Brand and size detection
# --------------------------------------------------------------------------

def detect_brand(item: dict, site: dict, brands: dict) -> str:
    if site.get("brand"):
        return site["brand"]
    text = " ".join([item.get("brand_hint", ""), item.get("name", ""), item.get("url", "")]).lower()
    for brand, info in brands.items():
        if any(k.lower() in text for k in info.get("keywords", [brand])):
            return brand
    return item.get("brand_hint") or site.get("brand_default") or "Other"


def parse_size(*texts):
    """('Kinrin 40g matcha', '100g bag') -> (grams, pack). Later texts win."""
    grams = pack = None
    for t in texts:
        t = (t or "").lower()
        m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g|gr|gram|grams)\b", t)
        if m:
            grams = int(float(m.group(1)) * (1000 if m.group(2) == "kg" else 1))
        p = re.search(r"\b(can|tin|bag|pack|pouch|box)\b", t)
        if p:
            pack = {"tin": "can", "pack": "bag", "pouch": "bag"}.get(p.group(1), p.group(1))
    return grams, pack


def norm(s: str, aliases: dict | None = None) -> str:
    s = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for a, b in (aliases or {}).items():
        s = s.replace(a, b)
    return s


def core_name(name: str) -> str:
    """'Urasenke School Favored Matcha Seijo no Shiro' -> 'Seijo no Shiro'."""
    n = re.sub(r"\(.*?\)", "", name)
    m = re.search(r"(?:favou?red|cold water|for cooking)\s+matcha\s+(.+)$|matcha for cold water\s+(.+)$", n, re.I)
    if m:
        n = m.group(1) or m.group(2)
    n = re.sub(r"\b(matcha|powder|principal|ceremonial|grade|by|marukyu|yamamasa|koyamaen)\b", " ", n, flags=re.I)
    return n.strip()


# --------------------------------------------------------------------------
# List prices
# --------------------------------------------------------------------------

def build_reference(cfg_prices: dict, official_items: dict, state: dict):
    """Return {brand: [entry, ...]}; entry = {core, grams, pack, jpy, label}.

    jpy is the manufacturer's price INCLUDING Japanese consumption tax, so it
    compares fairly with what a shop charges.
    """
    aliases = cfg_prices.get("aliases", {})
    ref = {}
    saved = state.setdefault("list_prices", {})
    for brand, info in cfg_prices.get("brands", {}).items():
        mult = 1 + float(info.get("tax_rate", 0.08)) if info.get("tax") == "excluded" else 1.0
        entries = []
        for p in info.get("products", []):
            names = [p["name"]] + p.get("aliases", [])
            for size, yen in (p.get("prices") or {}).items():
                if not yen:
                    continue
                g, pk = parse_size(size)
                for n in names:
                    entries.append({"core": norm(core_name(n), aliases), "grams": g, "pack": pk,
                                    "jpy": round(yen * mult), "label": f"{p['name']} {size}"})
        # Prices learned from the brand's own shop this scan (Marukyu official)
        learned = [
            {"core": norm(core_name(it["name"]), aliases), "grams": parse_size(it["variant"])[0],
             "pack": parse_size(it["variant"])[1], "jpy": round(it["price"]), "label": f"{it['name']} {it['variant']}"}
            for it in official_items.get(brand, []) if it.get("price")
        ]
        if learned:
            saved[brand] = learned
        entries.extend(saved.get(brand, []))
        ref[brand] = [e for e in entries if e["core"]]
    return ref


def match_list_price(item: dict, ref: dict, aliases: dict):
    entries = ref.get(item.get("brand"), [])
    if not entries:
        return None
    title = norm(item["name"] + " " + item.get("variant", ""), aliases)
    grams, pack = parse_size(item["name"], item.get("variant", ""))
    if not grams:
        return None
    names = sorted({e["core"] for e in entries}, key=len, reverse=True)
    core = next((c for c in names if c in title), None)
    if not core:
        return None
    same = [e for e in entries if e["core"] == core and e["grams"] == grams]
    if not same:
        return None
    if pack:
        exact = [e for e in same if e["pack"] == pack]
        if exact:
            return exact[0]
    cans = [e for e in same if e["pack"] == "can"]
    return (cans or same)[0]


# --------------------------------------------------------------------------
# Currency
# --------------------------------------------------------------------------

def fx_to_jpy(state: dict, max_age_hours: int = 12):
    """Return {currency: yen per 1 unit}. Cached in state; free API, no key."""
    cache = state.get("fx") or {}
    if cache.get("rates") and time.time() - cache.get("ts", 0) < max_age_hours * 3600:
        return cache["rates"]
    try:
        data = get_json("https://open.er-api.com/v6/latest/JPY")
        rates = {cur: round(1 / r, 4) for cur, r in data["rates"].items() if r}
        state["fx"] = {"ts": int(time.time()), "rates": rates}
        return rates
    except Exception as e:
        print(f"Exchange rates unavailable ({e}); using last known", flush=True)
        return cache.get("rates", {"JPY": 1.0})


def money(amount, currency):
    if amount is None:
        return ""
    sym = SYMBOLS.get(currency, currency + " ")
    return f"{sym}{amount:,.0f}" if currency == "JPY" else f"{sym}{amount:,.2f}"


def price_note(item: dict, rates: dict):
    """'S$54.80 (≈¥5,610) · list ¥5,720 · -2%'"""
    cur, price = item.get("currency", "JPY"), item.get("price")
    if price is None:
        return ""
    parts = [money(price, cur)]
    lp = item.get("list_price")
    if item.get("official") or not lp:
        return parts[0]
    rate = rates.get(cur)
    if not rate:
        return parts[0] + f" · list ¥{lp['jpy']:,}"
    yen = price * rate
    if cur != "JPY":
        parts[0] += f" (≈¥{yen:,.0f})"
    diff = (yen / lp["jpy"] - 1) * 100
    parts.append(f"list ¥{lp['jpy']:,}")
    parts.append(f"{diff:+.0f}%")
    return " · ".join(parts)
