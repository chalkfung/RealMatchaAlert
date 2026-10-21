"""Brand + item catalog across shops, and each chat's personal subscriptions.

An "item" is a matcha grade of one brand, e.g. Marukyu Koyamaen · Wako. The
same item sold by several shops (in several sizes) is grouped together, so an
alert can show every shop's price and stock for it.

Item names come from, longest match first:
    * "catalog" in config.json (grades plus other spellings shops use)
    * product names in list_prices.json
    * product names on the brand's official shop (Marukyu, read each scan)
Anything else gets a name cleaned up from the shop's product title.

A chat's subscriptions ("follow" in state.json) is a list of:
    "*"                        everything
    "Marukyu Koyamaen"         a whole brand
    "Marukyu Koyamaen/Wako"    one item
    "-..."                     an exception to one of the above
A chat with no "follow" list gets everything.
"""
from __future__ import annotations

import html
import re

from pricing import core_name, norm

NOISE = re.compile(r"\b(ceremonial|grade|uji|powder|outlet|sale|for|cooking|seasonal|premium|limited|product|"
                   r"bag|can|tin|kyoto|japan|of)\b", re.I)
SIZE = re.compile(r"\d+(?:\.\d+)?\s*(?:kg|g|gr|grams?)\b", re.I)


# --------------------------------------------------------------------------
# Naming items
# --------------------------------------------------------------------------

def known_items(cfg: dict, prices_cfg: dict, items) -> dict:
    """{brand: {item name: [normalised spellings]}}"""
    aliases = prices_cfg.get("aliases", {})
    known = {}

    def add(brand, name, extra=()):
        spellings = known.setdefault(brand, {}).setdefault(name, [])
        for s in [name, *extra]:
            n = norm(core_name(s), aliases)
            if n and n not in spellings:
                spellings.append(n)

    for brand, entries in cfg.get("catalog", {}).items():
        if brand.startswith("_"):
            continue
        for name, extra in (entries.items() if isinstance(entries, dict) else ((e, []) for e in entries)):
            add(brand, name, extra)
    for brand, info in prices_cfg.get("brands", {}).items():
        for p in info.get("products", []):
            add(brand, p["name"].replace("-", " ").title().replace(" No ", " no "), p.get("aliases", []))
    for it in items:
        if it.get("official") and it.get("brand"):
            if not any(norm(it["name"], aliases) in sp for sp in known.get(it["brand"], {}).values()):
                add(it["brand"], it["name"])
    return known


def clean_name(name: str) -> str:
    """'Hatsu No Mori Matcha Powder by Marukyu Koyamaen' -> 'Hatsu No Mori'."""
    n = core_name(name.split("|")[0])
    n = NOISE.sub(" ", SIZE.sub(" ", n))
    n = re.sub(r"[,()\[\]]", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" -·")
    return n or "Other"


def assign_items(items, known: dict, aliases: dict):
    """Set it["item"] on each item."""
    fallback = {}  # norm -> first name seen, so shops spelling it alike group together
    for it in items:
        brand = it.get("brand") or "Other"
        title = norm(it["name"], aliases)
        best = None
        for name, spellings in known.get(brand, {}).items():
            for s in spellings:
                if s in title and (best is None or len(s) > best[0]):
                    best = (len(s), name)
        if best:
            it["item"] = best[1]
        else:
            name = clean_name(it["name"])
            it["item"] = fallback.setdefault((brand, norm(name, aliases)), name)


def catalog(items) -> dict:
    """{brand: {item: {"shops": set, "in_stock": bool}}} from scanned items."""
    out = {}
    for it in items:
        if not it.get("item"):
            continue
        c = out.setdefault(it.get("brand") or "Other", {}).setdefault(it["item"], {"shops": set(), "in_stock": False})
        c["shops"].add(it["site"])
        c["in_stock"] = c["in_stock"] or bool(it.get("available"))
    return out


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------

def brand_slug(brand: str) -> str:
    return norm(brand.split()[0]) if brand else "other"


def tap(prefix: str, brand: str, item: str | None = None) -> str:
    """A command Telegram shows as a link, e.g. /sub_marukyu_wako."""
    return f"/{prefix}_{brand_slug(brand)}" + (f"_{norm(item)}" if item else "")


def _key(entry: str):
    brand, _, item = entry.lstrip("-").partition("/")
    return brand, norm(item) if item else None


def _covers(entry: str, brand: str, item: str | None) -> bool:
    if entry.lstrip("-") == "*":
        return True
    b, i = _key(entry)
    return b == brand and (i is None or i == (norm(item) if item else None))


def wants(follow, it: dict) -> bool:
    follow = ["*"] if follow is None else follow
    brand, item = it.get("brand") or "Other", it.get("item")
    pos = [e for e in follow if not e.startswith("-")]
    neg = [e for e in follow if e.startswith("-")]
    return any(_covers(e, brand, item) for e in pos) and not any(_covers(e, brand, item) for e in neg)


def describe(entry: str) -> str:
    return "everything" if entry == "*" else entry.replace("/", " · ")


def resolve(text: str, cat: dict):
    """Free text ('wako', 'marukyu', 'yamamasa seiun', 'marukyu_wako') -> (entries, error)."""
    p = norm(text)
    if p in ("all", "everything", ""):
        return ["*"], None
    brands = list(cat)
    for b in brands:
        if p in (norm(b), brand_slug(b)) or (len(p) >= 4 and norm(b).startswith(p)):
            return [b], None
    scope = brands
    for b in brands:
        for prefix in (norm(b), brand_slug(b)):
            if p.startswith(prefix) and len(p) > len(prefix):
                scope, p = [b], p[len(prefix):]
                break
        if scope != brands:
            break
    exact = [(b, i) for b in scope for i in cat[b] if norm(i) == p]
    found = exact or ([(b, i) for b in scope for i in cat[b] if p in norm(i)] if len(p) >= 3 else [])
    if len(found) == 1:
        return [f"{found[0][0]}/{found[0][1]}"], None
    if not found:
        return [], f"I couldn't find “{html.escape(text)}”. Send /items to see the names."
    return [], (f"“{html.escape(text)}” matches several items, tap the one you want:\n" +
                "\n".join(f"  {html.escape(b)} · {html.escape(i)}  {tap('sub', b, i)}" for b, i in found[:15]))


def subscribe(sub: dict, entry: str) -> str:
    follow = sub.get("follow")
    if entry == "*":
        sub["follow"] = ["*"]
        return "You'll get alerts for <b>everything</b>."
    if follow is None:  # first pick: switch from "everything" to just this
        sub["follow"] = [entry]
        return f"You'll now get alerts only for <b>{html.escape(describe(entry))}</b>. Add more the same way."
    if "-" + entry in follow:
        follow.remove("-" + entry)
        return f"Alerts back on for <b>{html.escape(describe(entry))}</b>."
    brand, item = _key(entry)
    if entry in follow or (item and wants(follow, {"brand": brand, "item": item})):
        return f"You already get alerts for <b>{html.escape(describe(entry))}</b>."
    follow.append(entry)
    return f"Added <b>{html.escape(describe(entry))}</b>."


def unsubscribe(sub: dict, entry: str) -> str:
    follow = sub.get("follow")
    follow = sub["follow"] = ["*"] if follow is None else follow
    if entry == "*":
        sub["follow"] = []
        return "You won't get any alerts until you /subscribe to something. (/stop leaves completely.)"
    # removing a brand also removes its items and exceptions
    brand, item = _key(entry)
    before = list(follow)
    follow[:] = [e for e in follow if not (_key(e)[0] == brand and (item is None or _key(e) == (brand, item)))]
    if wants(follow, {"brand": brand, "item": item}):  # still covered by "*" or the whole brand
        follow.append("-" + entry)
    if follow == before:
        return f"You weren't getting alerts for <b>{html.escape(describe(entry))}</b>."
    return f"Stopped alerts for <b>{html.escape(describe(entry))}</b>."


def list_text(sub: dict) -> str:
    follow = sub.get("follow")
    follow = ["*"] if follow is None else follow
    pos = [e for e in follow if not e.startswith("-")]
    neg = [e[1:] for e in follow if e.startswith("-")]
    if not pos:
        return "You're not following anything, so no alerts. Send /brands or /items to pick some."
    lines = ["<b>Your alerts</b>"]
    for e in pos:
        b, _, i = e.partition("/")
        lines.append(f"✅ {html.escape(describe(e))}" + ("" if e == "*" else f"  {tap('unsub', b, i or None)}"))
    for e in neg:
        b, _, i = e.partition("/")
        lines.append(f"🚫 except {html.escape(describe(e))}  {tap('sub', b, i or None)}")
    lines.append("\nTap a command to change it, or /subscribe all to get everything.")
    return "\n".join(lines)


HELP = """🍵 <b>Matcha Alert</b> tells you when matcha comes back in stock, with every shop's price.

/brands  brands I watch
/items  every item (tap one to subscribe)
/subscribe <i>name</i>  e.g. <code>/subscribe wako</code>, <code>/subscribe yamamasa</code>, <code>/subscribe all</code>
/unsubscribe <i>name</i>  stop one
/list  what you're following
/status  full stock list for what you follow
/stop  leave

New chats get everything until they subscribe to something. I check the shops about once an hour, so replies can take up to an hour."""


def brands_text(cat: dict) -> str:
    lines = ["<b>Brands</b>"]
    for b in cat:
        n = len(cat[b])
        lines.append(f"\n<b>{html.escape(b)}</b> ({n} item{'s' * (n != 1)})\n  all of it: {tap('sub', b)}  ·  items: {tap('items', b)}")
    return "\n".join(lines)


def items_text(cat: dict, only: str | None = None) -> str:
    lines = []
    for b in cat:
        if only and norm(only) not in (brand_slug(b), norm(b)) and not norm(b).startswith(norm(only)):
            continue
        lines.append(f"\n<b>{html.escape(b)}</b>  (whole brand: {tap('sub', b)})")
        for i in sorted(cat[b], key=str.lower):
            c = cat[b][i]
            mark = "✅" if c["in_stock"] else "❌"
            shops = len(c["shops"])
            lines.append(f"{mark} {html.escape(i)} · {shops} shop{'s' * (shops != 1)}  {tap('sub', b, i)}")
    if not lines:
        return "No items yet. The list fills in after the first scan."
    return "<b>Items</b> (✅ in stock somewhere)" + "\n".join(lines)


def handle(cmd: str, arg: str, sub: dict, cat: dict) -> str | None:
    """Run one command for a chat; return the reply (HTML) or None."""
    for prefix, real in (("/sub_", "/subscribe"), ("/unsub_", "/unsubscribe"), ("/items_", "/items")):
        if cmd.startswith(prefix):
            cmd, arg = real, cmd[len(prefix):]
    if cmd == "/help":
        return HELP
    if cmd == "/brands":
        return brands_text(cat)
    if cmd == "/items":
        return items_text(cat, arg or None)
    if cmd == "/list":
        return list_text(sub)
    if cmd in ("/subscribe", "/unsubscribe"):
        if not arg.strip():
            return f"Tell me what, e.g. <code>{cmd} wako</code>. Send /items to see the names."
        replies = []
        for part in [a for a in re.split(r"[,\n]", arg) if a.strip()]:
            entries, err = resolve(part, cat)
            if err:
                replies.append(err)
            for e in entries:
                replies.append((subscribe if cmd == "/subscribe" else unsubscribe)(sub, e))
        return "\n".join(replies) + "\n\n/list shows everything you follow."
    return None
