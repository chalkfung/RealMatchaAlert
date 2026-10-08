#!/usr/bin/env python3
"""Matcha Alert: watch product stock on online stores and send Telegram alerts.

Pure Python standard library. No AI or paid API is involved in a scan.

Usage:
    python matcha_alert.py                 # scan once, alert on changes
    python matcha_alert.py --dry-run       # scan once, print results, send nothing
    python matcha_alert.py --summary       # also send a full stock summary
    python matcha_alert.py --test-telegram # send a test message and exit

Environment variables:
    TELEGRAM_BOT_TOKEN   token from @BotFather
    TELEGRAM_CHAT_ID     your chat id (see README)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_STATE = HERE / "state.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MatchaAlert/1.0"
)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url: str, timeout: int = 20, retries: int = 2) -> str:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 410):
                break
            # 429 = rate limited, back off a bit longer
            time.sleep(5 * (attempt + 1) if e.code == 429 else 2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last_err}")


def get_json(url: str):
    return json.loads(http_get(url))


# --------------------------------------------------------------------------
# Item model: every adapter returns a list of dicts like
#   {"key": "<stable id>", "name": str, "url": str, "available": bool, "price": str}
# One dict per variant (e.g. size), so a product with 2 sizes gives 2 items.
# --------------------------------------------------------------------------

def _item(key, name, url, available, price=""):
    return {"key": key, "name": name, "url": url, "available": bool(available), "price": price}


# --------------------------------------------------------------------------
# Shopify adapter (works for any Shopify store)
# --------------------------------------------------------------------------

def _shopify_split(url: str):
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    path = p.path.rstrip("/")
    return base, path


def _shopify_variant_items(base, product, price_in_cents: bool):
    handle = product["handle"]
    title = product["title"]
    variants = product.get("variants") or []
    items = []
    for v in variants:
        vtitle = v.get("title") or ""
        name = title if vtitle in ("", "Default Title") or len(variants) == 1 else f"{title} ({vtitle})"
        price = v.get("price")
        if price is not None and price_in_cents:
            price = f"{int(price) / 100:.2f}"
        items.append(_item(
            key=f"{base}/products/{handle}#{v.get('id')}",
            name=name,
            url=f"{base}/products/{handle}" + (f"?variant={v['id']}" if len(variants) > 1 else ""),
            available=v.get("available", False),
            price=str(price or ""),
        ))
    return items


def shopify_product(url: str):
    """Product page URL -> its variants via the public <url>.js endpoint."""
    base, path = _shopify_split(url)
    m = re.search(r"/products/([^/?#]+)", path)
    if not m:
        raise ValueError(f"Not a Shopify product URL: {url}")
    handle = m.group(1)
    try:
        product = get_json(f"{base}/products/{handle}.js")  # prices in cents
        return _shopify_variant_items(base, product, price_in_cents=True)
    except Exception:
        # Fallback: .json endpoint (prices as strings, "available" sometimes missing)
        product = get_json(f"{base}/products/{handle}.json")["product"]
        return _shopify_variant_items(base, product, price_in_cents=False)


def shopify_collection(url: str):
    """Collection URL -> every product variant in it via products.json (paged)."""
    base, path = _shopify_split(url)
    m = re.search(r"/collections/([^/?#]+)", path)
    if not m:
        raise ValueError(f"Not a Shopify collection URL: {url}")
    handle = m.group(1)
    items = []
    for page in range(1, 21):
        data = get_json(f"{base}/collections/{handle}/products.json?limit=250&page={page}")
        products = data.get("products") or []
        if not products:
            break
        for p in products:
            items.extend(_shopify_variant_items(base, p, price_in_cents=False))
        if len(products) < 250:
            break
    return items


# --------------------------------------------------------------------------
# Generic HTML adapter (for non-Shopify stores)
# Looks at schema.org JSON-LD "availability" first, then text markers you
# can set per site in config.json ("in_stock_text" / "out_of_stock_text").
# --------------------------------------------------------------------------

def html_product(url: str, site: dict):
    page = http_get(url)
    name = url
    t = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    if t:
        name = html.unescape(t.group(1)).strip()

    available = None
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        avail = re.findall(r'"availability"\s*:\s*"([^"]+)"', block)
        if avail:
            available = any(a.rstrip("/").endswith(("InStock", "LimitedAvailability", "PreOrder")) for a in avail)
            n = re.search(r'"name"\s*:\s*"([^"]+)"', block)
            if n:
                name = html.unescape(n.group(1))
            break

    if available is None:
        low = page.lower()
        out_markers = [s.lower() for s in site.get("out_of_stock_text", ["sold out", "out of stock", "currently unavailable"])]
        in_markers = [s.lower() for s in site.get("in_stock_text", ["add to cart", "add to bag", "buy now"])]
        if any(s in low for s in out_markers):
            available = False
        elif any(s in low for s in in_markers):
            available = True
        else:
            raise RuntimeError(f"Could not tell stock status for {url}; set in_stock_text/out_of_stock_text in config")
    return [_item(key=url, name=name, url=url, available=available)]


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def scan_site(site: dict):
    adapter = site.get("adapter", "shopify")
    items, errors = [], []
    targets = [("collection", u) for u in site.get("collections", [])] + \
              [("product", u) for u in site.get("products", [])]
    for kind, url in targets:
        try:
            if adapter == "shopify":
                got = shopify_collection(url) if kind == "collection" else shopify_product(url)
            elif adapter == "html":
                if kind == "collection":
                    raise ValueError("html adapter supports product URLs only")
                got = html_product(url, site)
            else:
                raise ValueError(f"Unknown adapter '{adapter}'")
            for it in got:
                it["site"] = site.get("name", urllib.parse.urlparse(url).netloc)
                it["watched"] = kind == "product"
            items.extend(got)
        except Exception as e:  # keep going; report at the end
            errors.append(f"{site.get('name', '?')}: {url} -> {e}")
        time.sleep(site.get("delay_seconds", 1))
    return items, errors


def merge_items(items):
    """Same variant can appear from both a collection and a product link."""
    merged = {}
    for it in items:
        prev = merged.get(it["key"])
        if prev:
            prev["watched"] = prev["watched"] or it["watched"]
        else:
            merged[it["key"]] = it
    return merged


def diff(prev_state: dict, current: dict, cfg_notify: dict):
    events = []
    first_run = not prev_state
    for key, it in current.items():
        old = prev_state.get(key)
        if old is None:
            if not first_run and cfg_notify.get("new_product", True):
                events.append(("new", it))
            continue
        if not old["available"] and it["available"] and cfg_notify.get("back_in_stock", True):
            events.append(("restock", it))
        elif old["available"] and not it["available"] and cfg_notify.get("sold_out", True):
            events.append(("soldout", it))
    if not first_run and cfg_notify.get("removed", False):
        for key, old in prev_state.items():
            if key not in current:
                events.append(("removed", old))
    return events


def fmt_item(it):
    price = f" · {it['price']}" if it.get("price") else ""
    return f'<a href="{html.escape(it["url"])}">{html.escape(it["name"])}</a>{html.escape(price)}'


def build_alert(events):
    labels = {
        "restock": "🟢 BACK IN STOCK",
        "soldout": "🔴 Sold out",
        "new": "🆕 New listing",
        "removed": "⚪ Removed from store",
    }
    lines = ["🍵 <b>Matcha Alert</b>"]
    # Restocks first, they matter most
    order = {"restock": 0, "new": 1, "soldout": 2, "removed": 3}
    for kind, it in sorted(events, key=lambda e: order[e[0]]):
        extra = " (in stock)" if kind == "new" and it["available"] else (" (sold out)" if kind == "new" else "")
        lines.append(f"{labels[kind]}{extra}: {fmt_item(it)}")
    return "\n".join(lines)


def build_summary(current: dict, errors):
    lines = ["🍵 <b>Matcha Alert: stock summary</b>"]
    by_site = {}
    for it in current.values():
        by_site.setdefault(it["site"], []).append(it)
    for site, its in by_site.items():
        lines.append(f"\n<b>{html.escape(site)}</b>")
        for it in sorted(its, key=lambda x: (not x["watched"], not x["available"], x["name"])):
            star = "⭐ " if it["watched"] else ""
            mark = "✅" if it["available"] else "❌"
            lines.append(f"{mark} {star}{fmt_item(it)}")
    if errors:
        lines.append("\n⚠️ Errors:\n" + "\n".join(html.escape(e) for e in errors))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; message not sent:\n" + text)
        return False
    # Telegram limit is 4096 chars per message; split on lines
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for chunk in chunks:
        body = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            print(f"Telegram error {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
            return False
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan stores for matcha stock and alert via Telegram.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--dry-run", action="store_true", help="print results, send nothing, don't save state")
    ap.add_argument("--summary", action="store_true", help="also send a full stock summary")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    args = ap.parse_args(argv)

    if args.test_telegram:
        ok = send_telegram("🍵 Matcha Alert is connected. You'll get stock alerts here.")
        print("Sent." if ok else "Failed.")
        return 0 if ok else 1

    cfg = load_json(Path(args.config), None)
    if cfg is None:
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2
    state_path = Path(args.state)
    state = load_json(state_path, {})
    prev_items = state.get("items", {})

    all_items, errors = [], []
    for site in cfg.get("sites", []):
        if site.get("enabled", True) is False:
            continue
        its, errs = scan_site(site)
        all_items.extend(its)
        errors.extend(errs)
    current = merge_items(all_items)

    # If a whole fetch failed, keep the old entries so we don't fire false
    # "removed"/"new" alerts when the site comes back.
    if errors:
        for k, v in prev_items.items():
            current.setdefault(k, {**v, "stale": True})

    notify_cfg = cfg.get("notify", {})
    only_watched = notify_cfg.get("only_watched_products", False)
    events = diff(prev_items, current, notify_cfg)
    if only_watched:
        events = [e for e in events if e[1].get("watched")]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{now}] scanned {len(current)} items, {len(events)} change(s), {len(errors)} error(s)")
    for it in sorted(current.values(), key=lambda x: (x["site"], x["name"])):
        print(f"  {'IN ' if it['available'] else 'OUT'} {'*' if it['watched'] else ' '} {it['name']}  {it['url']}")
    for e in errors:
        print("  ERROR", e, file=sys.stderr)

    if args.dry_run:
        if events:
            print("\nWould send:\n" + build_alert(events))
        return 0

    first_run = not prev_items
    if events:
        send_telegram(build_alert(events))
    if args.summary or (first_run and notify_cfg.get("summary_on_first_run", True)):
        send_telegram(build_summary(current, errors))

    # Alert about persistent errors once, not every 15 minutes
    err_sig = "|".join(sorted(e.split(" -> ")[0] for e in errors))
    if errors and err_sig != state.get("error_sig") and notify_cfg.get("errors", True):
        send_telegram("⚠️ <b>Matcha Alert</b> couldn't read:\n" + "\n".join(html.escape(e) for e in errors))

    new_state = {
        "updated_at": now if (events or first_run or err_sig != state.get("error_sig")) else state.get("updated_at", now),
        "error_sig": err_sig,
        "items": {k: {kk: vv for kk, vv in v.items() if kk != "stale"} for k, v in current.items()},
    }
    new_text = json.dumps(new_state, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    old_text = state_path.read_text(encoding="utf-8") if state_path.exists() else ""
    if new_text != old_text:  # only touch the file on real changes (keeps git history quiet)
        state_path.write_text(new_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
