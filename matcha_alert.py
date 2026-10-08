#!/usr/bin/env python3
"""Matcha Alert: watch matcha stock on online shops and send Telegram alerts.

Pure Python standard library. No AI or paid API is involved in a scan.

Usage:
    python matcha_alert.py                 # scan once, alert on changes
    python matcha_alert.py --dry-run       # scan once, print results, send nothing
    python matcha_alert.py --summary       # also send a full stock summary
    python matcha_alert.py --test-telegram # send a test message and exit
    python matcha_alert.py --only "TeaLife" --dry-run   # test one shop

Environment variables:
    TELEGRAM_BOT_TOKEN   token from @BotFather
    TELEGRAM_CHAT_ID     optional: your own chat id(s), comma-separated.
                         Groups that add the bot subscribe automatically.

Files:
    config.json        shops and products to watch
    list_prices.json   manufacturer list prices (Marukyu is also read live
                       from its official shop each scan)
    state.json         written by the script: last stock seen, subscribers
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import ADAPTERS, REQUEST_DELAY  # noqa: E402
from pricing import build_reference, detect_brand, fx_to_jpy, match_list_price, price_note  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_PRICES = HERE / "list_prices.json"
DEFAULT_STATE = HERE / "state.json"


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def scan_site(site: dict, brands: dict, watch_brands: list):
    adapter = ADAPTERS.get(site.get("adapter", "shopify"))
    items, errors = [], []
    if adapter is None:
        return items, [f"{site.get('name', '?')}: unknown adapter '{site.get('adapter')}'"]
    targets = [("collection", u) for u in site.get("collections", [])] + \
              [("product", u) for u in site.get("products", [])]
    for kind, url in targets:
        try:
            fn = adapter.get(kind)
            if fn is None:
                raise ValueError(f"{site.get('adapter')} adapter supports product links only")
            got = fn(url, site)
        except Exception as e:  # keep going; report at the end
            errors.append(f"{site.get('name', '?')}: {url} -> {e}")
            continue
        for it in got:
            if any(w.lower() in (it["name"] + " " + it["variant"]).lower() for w in site.get("exclude_keywords", [])):
                continue
            it["site"] = site.get("name", urllib.parse.urlparse(url).netloc)
            it["brand"] = detect_brand(it, site, brands)
            it["currency"] = it.get("currency") or site.get("currency", "USD")
            it["watched"] = kind == "product"
            it["official"] = bool(site.get("official_for"))
            it.pop("brand_hint", None)
            if watch_brands and it["brand"] not in watch_brands:
                continue
            items.append(it)
        time.sleep(REQUEST_DELAY)
    errors.extend(site.pop("_errors", []))
    return items, errors


def merge_items(items):
    """The same variant can come from both a collection and a product link."""
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


# --------------------------------------------------------------------------
# Messages: grouped by shop, then by brand
# --------------------------------------------------------------------------

LABELS = {"restock": "🟢 Back in stock", "soldout": "🔴 Sold out", "new": "🆕 New", "removed": "⚪ Removed"}
EVENT_ORDER = {"restock": 0, "new": 1, "soldout": 2, "removed": 3}


def item_line(it, rates):
    name = it["name"] + (f" · {it['variant']}" if it.get("variant") else "")
    line = f'<a href="{html.escape(it["url"])}">{html.escape(name)}</a>'
    note = price_note(it, rates)
    return line + (f" · {html.escape(note)}" if note else "")


def grouped(rows, site_order, brand_order):
    """rows: [(item, text)] -> lines grouped by site then brand."""
    by_site = {}
    for it, text in rows:
        by_site.setdefault(it["site"], {}).setdefault(it.get("brand") or "Other", []).append(text)
    lines = []
    s_rank = {s: i for i, s in enumerate(site_order)}
    b_rank = {b: i for i, b in enumerate(brand_order)}
    for site in sorted(by_site, key=lambda s: (s_rank.get(s, 99), s)):
        lines.append(f"\n🏪 <b>{html.escape(site)}</b>")
        for brand in sorted(by_site[site], key=lambda b: (b_rank.get(b, 99), b)):
            lines.append(f"  <i>{html.escape(brand)}</i>")
            lines.extend(f"  {t}" for t in by_site[site][brand])
    return lines


def build_alert(events, rates, site_order, brand_order):
    rows = [(it, f"{LABELS[k]}: {item_line(it, rates)}")
            for k, it in sorted(events, key=lambda e: (EVENT_ORDER[e[0]], e[1]["name"]))]
    return "\n".join(["🍵 <b>Matcha Alert</b>"] + grouped(rows, site_order, brand_order))


def build_summary(current, errors, rates, site_order, brand_order, show_sold_out=True):
    rows = []
    for it in sorted(current.values(), key=lambda x: (not x["available"], x["name"], x.get("variant", ""))):
        if not it["available"] and not show_sold_out:
            continue
        mark = "✅" if it["available"] else "❌"
        rows.append((it, f"{mark} {'⭐ ' if it['watched'] else ''}{item_line(it, rates)}"))
    lines = ["🍵 <b>Matcha Alert: stock summary</b>",
             "<i>list = manufacturer's price in Japan incl. tax; % = shop price vs list</i>"]
    lines += grouped(rows, site_order, brand_order)
    if errors:
        lines.append("\n⚠️ Couldn't read:\n" + "\n".join(html.escape(e) for e in errors))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
#
# Who gets messages ("subscribers"):
#   * TELEGRAM_CHAT_ID, if set (your own chat; comma-separate several ids)
#   * every group the bot is added to, and every person who sends it /start.
#     The bot picks these up from Telegram's getUpdates at the start of each
#     scan and remembers them in state.json. Removing the bot from a group,
#     or sending /stop, unsubscribes.
# --------------------------------------------------------------------------

class TelegramError(Exception):
    def __init__(self, code, desc, params=None):
        super().__init__(f"{code}: {desc}")
        self.code, self.desc, self.params = code, desc, params or {}


def tg_api(method: str, params: dict):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())["result"]
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {}
        raise TelegramError(e.code, err.get("description", str(e)), err.get("parameters"))


def _chat_label(chat: dict):
    return chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
        or chat.get("username") or str(chat.get("id"))


def sync_subscribers(state: dict, allow_private: bool = True):
    """Read new Telegram updates; add/remove subscriber chats.

    Returns the list of chat ids that just joined (they get a welcome + summary).
    """
    subs = state.setdefault("subscribers", {})
    joined = []
    try:
        updates = tg_api("getUpdates", {
            "offset": state.get("tg_offset", 0),
            "timeout": 0,
            "allowed_updates": json.dumps(["message", "my_chat_member"]),
        })
    except TelegramError as e:
        print(f"Telegram getUpdates failed ({e}); using saved subscribers", file=sys.stderr)
        return joined

    def add(chat):
        cid = str(chat["id"])
        if cid not in subs:
            joined.append(cid)
        subs[cid] = {"type": chat.get("type", "")}  # no names: state.json may sit in a public repo
        print(f"Subscribed chat {cid} ({_chat_label(chat)})")

    def remove(cid):
        subs.pop(str(cid), None)
        if str(cid) in joined:
            joined.remove(str(cid))

    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        mcm = u.get("my_chat_member")
        if mcm:  # the bot itself was added to / removed from a chat
            status = mcm["new_chat_member"]["status"]
            chat = mcm["chat"]
            if status in ("member", "administrator"):
                if chat.get("type") != "private" or allow_private:
                    add(chat)
            elif status in ("left", "kicked"):
                remove(chat["id"])
            continue
        msg = u.get("message")
        if not msg:
            continue
        chat = msg["chat"]
        if msg.get("migrate_to_chat_id"):  # group upgraded to supergroup: new id
            remove(chat["id"])
            add({**chat, "id": msg["migrate_to_chat_id"], "type": "supergroup"})
            if str(msg["migrate_to_chat_id"]) in joined:  # same group, not a new subscriber
                joined.remove(str(msg["migrate_to_chat_id"]))
            continue
        text = (msg.get("text") or "").strip().lower()
        cmd = text.split("@")[0].split(" ")[0]
        if cmd == "/start" and (chat.get("type") != "private" or allow_private):
            add(chat)
        elif cmd == "/stop":
            remove(chat["id"])
            try:
                tg_api("sendMessage", {"chat_id": chat["id"], "text": "Unsubscribed from Matcha Alert. Send /start to subscribe again."})
            except TelegramError:
                pass
        elif cmd == "/status" and str(chat["id"]) in subs:
            joined.append(str(chat["id"]))  # treat as a request for the summary
    return joined


def all_recipients(state: dict):
    fixed = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    return list(dict.fromkeys(fixed + list(state.get("subscribers", {}))))


def _chunks(text: str):
    # Telegram limit is 4096 chars per message; split on lines
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_telegram(text: str, chat_ids, state: dict | None = None):
    """Send text to each chat. Chats that blocked/removed the bot are dropped."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        print("TELEGRAM_BOT_TOKEN not set; message not sent:\n" + text)
        return False
    if not chat_ids:
        print("No Telegram recipients yet (set TELEGRAM_CHAT_ID or add the bot to a group); not sent:\n" + text)
        return False
    ok_any = False
    for cid in chat_ids:
        for chunk in _chunks(text):
            try:
                tg_api("sendMessage", {"chat_id": cid, "text": chunk, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"})
                ok_any = True
            except TelegramError as e:
                new_id = e.params.get("migrate_to_chat_id")
                if state is not None and new_id:
                    sub = state.get("subscribers", {}).pop(str(cid), {"type": "supergroup"})
                    state["subscribers"][str(new_id)] = sub
                    print(f"Chat {cid} moved to {new_id}; will use the new id next time", file=sys.stderr)
                elif state is not None and e.code in (400, 403) and str(cid) in state.get("subscribers", {}):
                    # bot was kicked / blocked / chat deleted
                    print(f"Dropping subscriber {cid}: {e.desc}", file=sys.stderr)
                    state["subscribers"].pop(str(cid), None)
                else:
                    print(f"Telegram error for chat {cid}: {e}", file=sys.stderr)
                break
    return ok_any


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_state(path: Path, state: dict):
    new_text = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    if new_text != old_text:  # only touch the file on real changes (keeps git history quiet)
        path.write_text(new_text, encoding="utf-8")


STATE_FIELDS = ("site", "brand", "name", "variant", "url", "available", "price", "currency", "watched", "official")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan shops for matcha stock and alert via Telegram.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--prices", default=str(DEFAULT_PRICES))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--dry-run", action="store_true", help="print results, send nothing, don't save state")
    ap.add_argument("--summary", action="store_true", help="also send a full stock summary")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    ap.add_argument("--only", help="scan only the shop with this name (for testing; implies --dry-run)")
    args = ap.parse_args(argv)
    if args.only:
        args.dry_run = True

    cfg = load_json(Path(args.config), None)
    if cfg is None:
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2
    prices_cfg = load_json(Path(args.prices), {})
    state_path = Path(args.state)
    state = load_json(state_path, {})
    prev_items = state.get("items", {})
    notify_cfg = cfg.get("notify", {})
    brands = prices_cfg.get("brands", {})
    watch_brands = cfg.get("watch_brands", [])
    sites = [s for s in cfg.get("sites", []) if s.get("enabled", True) is not False
             and (not args.only or s.get("name", "").lower() == args.only.lower())]
    site_order = [s.get("name") for s in cfg.get("sites", [])]
    brand_order = watch_brands or list(brands)

    joined = []
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and not args.dry_run:
        joined = sync_subscribers(state, allow_private=notify_cfg.get("allow_private_chats", True))
        if notify_cfg.get("group_subscriptions", True) is False:
            state["subscribers"] = {}
            joined = []

    if args.test_telegram:
        ok = send_telegram("🍵 Matcha Alert is connected. You'll get stock alerts here.", all_recipients(state), state)
        print("Sent." if ok else "Failed.")
        save_state(state_path, state)
        return 0 if ok else 1

    # Official shops first, so their prices are ready for the comparison
    sites.sort(key=lambda s: not s.get("official_for"))
    all_items, errors = [], []
    for site in sites:
        print(f"Scanning {site.get('name')} ...", flush=True)
        its, errs = scan_site(site, brands, watch_brands)
        all_items.extend(its)
        errors.extend(errs)
    current = merge_items(all_items)

    # If something failed, keep its old entries so we don't fire false
    # "removed"/"new" alerts when the site comes back.
    if errors and not args.only:
        for k, v in prev_items.items():
            current.setdefault(k, {**v, "stale": True})

    # Manufacturer list prices + exchange rates
    official = {}
    for it in current.values():
        if it.get("official") and not it.get("stale"):
            site = next((s for s in cfg.get("sites", []) if s.get("name") == it["site"]), {})
            official.setdefault(site.get("official_for", it["brand"]), []).append(it)
    ref = build_reference(prices_cfg, official, state)
    rates = fx_to_jpy(state)
    aliases = prices_cfg.get("aliases", {})
    for it in current.values():
        it["list_price"] = None if it.get("official") else match_list_price(it, ref, aliases)

    events = diff(prev_items, current, notify_cfg) if not args.only else []
    if notify_cfg.get("only_watched_products", False):
        events = [e for e in events if e[1].get("watched")]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{now}] {len(current)} items, {len(events)} change(s), {len(errors)} error(s)")
    for it in sorted(current.values(), key=lambda x: (x["site"], x.get("brand") or "", x["name"], x.get("variant", ""))):
        print(f"  {'IN ' if it['available'] else 'OUT'} {'*' if it['watched'] else ' '} [{it['site']} / {it.get('brand')}] "
              f"{it['name']} {it.get('variant', '')}  {price_note(it, rates)}")
    for e in errors:
        print("  ERROR", e, file=sys.stderr)

    if args.dry_run:
        if events:
            print("\nWould send:\n" + build_alert(events, rates, site_order, brand_order))
        return 0

    first_run = not prev_items
    recipients = all_recipients(state)
    if events:
        send_telegram(build_alert(events, rates, site_order, brand_order), [c for c in recipients if c not in joined], state)
    summary = build_summary(current, errors, rates, site_order, brand_order,
                            notify_cfg.get("summary_show_sold_out", True))
    if args.summary or (first_run and notify_cfg.get("summary_on_first_run", True)):
        send_telegram(summary, all_recipients(state), state)
    elif joined:
        send_telegram("👋 This chat is now subscribed to <b>Matcha Alert</b>. You'll get a message here when "
                      "stock changes. Remove the bot (or send /stop) to unsubscribe.\n\n" + summary, joined, state)

    # Alert about persistent errors once, not on every scan
    err_sig = "|".join(sorted(e.split(" -> ")[0] for e in errors))
    if errors and err_sig != state.get("error_sig") and notify_cfg.get("errors", True):
        send_telegram("⚠️ <b>Matcha Alert</b> couldn't read:\n" + "\n".join(html.escape(e) for e in errors),
                      all_recipients(state), state)

    state.update({
        "updated_at": now if (events or first_run or err_sig != state.get("error_sig")) else state.get("updated_at", now),
        "error_sig": err_sig,
        "items": {k: {f: v.get(f) for f in STATE_FIELDS} for k, v in current.items()},
    })
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
