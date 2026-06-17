"""
parsers.py — HTML report parsers for TikTok Shop analytics reports.
Each parser returns a typed dict of extracted metrics.
"""

import re
import json
from bs4 import BeautifulSoup


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _soup(html_bytes):
    return BeautifulSoup(html_bytes, "lxml")


def _extract_number(text: str) -> float | None:
    """Pull the first number (int or float, possibly with commas/$%) from a string."""
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("%", "").strip()
    m = re.search(r"[\d]+\.?[\d]*", text)
    return float(m.group()) if m else None


def _find_chart_data(soup, keyword: str) -> list | None:
    """Search <script> tags for Chart.js array data matching a keyword context."""
    for script in soup.find_all("script"):
        src = script.string or ""
        if keyword.lower() not in src.lower():
            continue
        # Try to find data arrays like: data: [1,2,3,...]
        for m in re.finditer(r'data\s*:\s*\[([^\]]+)\]', src):
            try:
                vals = json.loads("[" + m.group(1) + "]")
                if vals:
                    return vals
            except Exception:
                pass
    return None


def _extract_card_numbers(soup) -> list[float]:
    """Extract all standalone number-looking card values from the page."""
    results = []
    for tag in soup.find_all(["td", "th", "span", "div", "p", "h1", "h2", "h3", "h4"]):
        text = (tag.get_text(strip=True))
        if re.match(r'^[\$]?[\d,]+\.?\d*%?$', text) and len(text) < 20:
            n = _extract_number(text)
            if n is not None:
                results.append(n)
    return results


def _table_to_rows(soup, min_cols=2) -> list[list[str]]:
    """Return all table rows as lists of stripped cell text strings."""
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) >= min_cols:
            rows.append(cells)
    return rows


# ─── CANCELLED REPORT PARSER ─────────────────────────────────────────────────

def parse_cancelled(html_bytes: bytes) -> dict:
    """
    Returns:
      total_orders, cancelled, cancel_rate_pct, cancelled_sku_units,
      live_sessions: [{name, cancelled, total, rate_pct, share_pct}],
      hourly_counts: list[int] (len 24),
      cancel_reasons: [{reason, count, pct}],
      collection_cancel: [{collection, channel, cancelled, share_pct}],
      sku_cancel: [{sku, units, share_pct}],
      live_aov, non_live_aov, live_cancel_rate, non_live_cancel_rate
    """
    soup = _soup(html_bytes)
    full_text = soup.get_text(" ", strip=True)
    rows = _table_to_rows(soup)

    result = {
        "total_orders": None,
        "cancelled": None,
        "cancel_rate_pct": None,
        "cancelled_sku_units": None,
        "live_sessions": [],
        "hourly_counts": [],
        "cancel_reasons": [],
        "collection_cancel": [],
        "sku_cancel": [],
        "live_total_orders": None,
        "live_cancelled": None,
        "live_cancel_rate_pct": None,
        "non_live_total_orders": None,
        "non_live_cancelled": None,
        "non_live_cancel_rate_pct": None,
        "live_aov": None,
        "non_live_aov": None,
    }

    # ── Summary card numbers ──
    # Look for patterns like "Total Orders\n1,721" in the text blocks
    for tag in soup.find_all(["div", "section", "article"]):
        t = tag.get_text("\n", strip=True)
        # Total Orders
        m = re.search(r'Total\s*Orders?\s*\n?\s*([\d,]+)', t, re.I)
        if m and result["total_orders"] is None:
            result["total_orders"] = int(m.group(1).replace(",", ""))
        # Cancelled
        m = re.search(r'Cancelled?\s*Orders?\s*\n?\s*([\d,]+)', t, re.I)
        if m and result["cancelled"] is None:
            result["cancelled"] = int(m.group(1).replace(",", ""))
        # Cancel Rate
        m = re.search(r'Cancel(?:lation)?\s*Rate\s*\n?\s*([\d.]+)\s*%', t, re.I)
        if m and result["cancel_rate_pct"] is None:
            result["cancel_rate_pct"] = float(m.group(1))
        # Cancelled SKU Units
        m = re.search(r'Cancelled?\s*SKU\s*Units?\s*\n?\s*([\d,]+)', t, re.I)
        if m and result["cancelled_sku_units"] is None:
            result["cancelled_sku_units"] = int(m.group(1).replace(",", ""))

    # Fallback: scan all text with broader pattern
    if result["total_orders"] is None:
        m = re.search(r'Total\s*Orders?\D{0,10}([\d,]+)', full_text, re.I)
        if m: result["total_orders"] = int(m.group(1).replace(",", ""))
    if result["cancelled"] is None:
        m = re.search(r'Cancelled?\s+Orders?\D{0,10}([\d,]+)', full_text, re.I)
        if m: result["cancelled"] = int(m.group(1).replace(",", ""))
    if result["cancel_rate_pct"] is None:
        m = re.search(r'Cancel\w*\s+Rate\D{0,10}([\d.]+)\s*%', full_text, re.I)
        if m: result["cancel_rate_pct"] = float(m.group(1))
    if result["cancelled_sku_units"] is None:
        m = re.search(r'Cancelled?\s+SKU\s+Units?\D{0,10}([\d,]+)', full_text, re.I)
        if m: result["cancelled_sku_units"] = int(m.group(1).replace(",", ""))

    # ── Hourly distribution from chart script ──
    for script in soup.find_all("script"):
        src = script.string or ""
        if "hour" not in src.lower() and "time" not in src.lower():
            continue
        m = re.search(r'data\s*:\s*\[([^\]]+)\]', src)
        if m:
            try:
                vals = json.loads("[" + m.group(1) + "]")
                if len(vals) == 24:
                    result["hourly_counts"] = [int(v) for v in vals]
                    break
            except Exception:
                pass

    # ── Cancel reasons from table rows ──
    in_reason_section = False
    for row in rows:
        row_text = " ".join(row).lower()
        if "reason" in row_text and ("count" in row_text or "orders" in row_text or "cancel" in row_text):
            in_reason_section = True
            continue
        if in_reason_section and len(row) >= 2:
            reason = row[0]
            # Detect end of reasons section
            if any(kw in reason.lower() for kw in ["collection", "link", "sku", "product", "segment"]):
                in_reason_section = False
                continue
            count = _extract_number(row[1])
            pct = _extract_number(row[2]) if len(row) > 2 else None
            if count and reason and len(reason) > 3:
                result["cancel_reasons"].append({
                    "reason": reason, "count": int(count), "pct": pct
                })

    # ── Live vs non-live ──
    for row in rows:
        joined = " ".join(row).lower()
        if "live" in joined and ("cancel" in joined or "order" in joined):
            # Live segments
            if any(kw in row[0].lower() for kw in ["live①", "live①", "直播①", "live 1", "session 1", "segment 1", "livestream①"]):
                result["live_sessions"].append({
                    "name": row[0],
                    "cancelled": _extract_number(row[1]),
                    "total": _extract_number(row[2]) if len(row) > 2 else None,
                    "rate_pct": _extract_number(row[3]) if len(row) > 3 else None,
                    "share_pct": _extract_number(row[4]) if len(row) > 4 else None,
                })
            elif any(kw in row[0].lower() for kw in ["live合计", "live total", "livestream total", "直播合计"]):
                result["live_cancelled"] = _extract_number(row[1])
                result["live_total_orders"] = _extract_number(row[2]) if len(row) > 2 else None
                result["live_cancel_rate_pct"] = _extract_number(row[3]) if len(row) > 3 else None
            elif any(kw in row[0].lower() for kw in ["非直播", "non-live", "non live", "non_live"]):
                result["non_live_cancelled"] = _extract_number(row[1])
                result["non_live_total_orders"] = _extract_number(row[2]) if len(row) > 2 else None
                result["non_live_cancel_rate_pct"] = _extract_number(row[3]) if len(row) > 3 else None

    # ── Collection cancel distribution ──
    in_collection = False
    for row in rows:
        if any(kw in row[0].lower() for kw in ["dreamwear", "square", "almond", "buy4", "buy 4", "auction", "collection"]):
            in_collection = True
        if in_collection and len(row) >= 3:
            cancelled = _extract_number(row[1])
            share = _extract_number(row[2]) if len(row) > 2 else None
            if cancelled is not None:
                result["collection_cancel"].append({
                    "collection": row[0], "cancelled": int(cancelled), "share_pct": share
                })

    # ── SKU cancel ──
    in_sku = False
    for row in rows:
        if "sku" in row[0].lower() and "cancel" in " ".join(row).lower():
            in_sku = True
            continue
        if in_sku and len(row) >= 2:
            units = _extract_number(row[1])
            share = _extract_number(row[2]) if len(row) > 2 else None
            if units is not None and row[0] and len(row[0]) > 2:
                result["sku_cancel"].append({
                    "sku": row[0], "units": int(units), "share_pct": share
                })

    return result


# ─── RETURNED REPORT PARSER ──────────────────────────────────────────────────

def parse_returned(html_bytes: bytes) -> dict:
    """
    Returns:
      total_rows, deduped_packages,
      seller_fault_rows, seller_fault_pct,
      refund_only, request_cancelled, shipped_return,
      customer_fault_packages, seller_fault_packages,
      return_reasons: [{reason, count, pct}],
      style_returns: [{style, count, pct}],
      collection_returns: [{collection, rows, pct}],
      live_returns, non_live_returns, unknown_returns,
      total_return_amount, avg_return_amount
    """
    soup = _soup(html_bytes)
    full_text = soup.get_text(" ", strip=True)
    rows = _table_to_rows(soup)

    result = {
        "total_rows": None,
        "deduped_packages": None,
        "seller_fault_rows": None,
        "seller_fault_pct_rows": None,
        "refund_only": None,
        "request_cancelled": None,
        "shipped_return": None,
        "customer_fault_packages": None,
        "seller_fault_packages": None,
        "return_reasons": [],
        "style_returns": [],
        "collection_returns": [],
        "live_returns": None,
        "non_live_returns": None,
        "unknown_returns": None,
        "total_return_amount": None,
        "avg_return_amount": None,
    }

    # Card metrics
    for pattern, key in [
        (r'Returned?\s*Packages?\D{0,10}([\d,]+)', "deduped_packages"),
        (r'Seller\s*Fault\D{0,15}([\d,]+)', "seller_fault_rows"),
        (r'Refund\s*Only\D{0,10}([\d,]+)', "refund_only"),
        (r'Request\s*Cancelled?\D{0,10}([\d,]+)', "request_cancelled"),
        (r'(?:已寄出|Shipped\s*Return)\D{0,10}([\d,]+)', "shipped_return"),
        (r'Total\s*Rows?\D{0,10}([\d,]+)', "total_rows"),
        (r'Total\s*Return\s*Amount\D{0,10}\$([\d,.]+)', "total_return_amount"),
        (r'Avg\w*\s*Return\D{0,15}\$([\d,.]+)', "avg_return_amount"),
    ]:
        m = re.search(pattern, full_text, re.I)
        if m and result[key] is None:
            val = m.group(1).replace(",", "")
            result[key] = float(val)

    if result["total_rows"] is None and result["deduped_packages"]:
        # Try counting return rows from reasons
        pass

    # Return reasons — use regex on full text (more reliable than table parsing)
    reason_map = [
        ("No longer needed",                  "No longer needed"),
        ("Missing package",                   "Missing package"),
        (r"doesn.t match description",        "Item doesn't match description"),
        ("Wrong item was sent",               "Wrong item was sent"),
        ("Damaged item or packaging",         "Damaged item or packaging"),
        ("does not meet expectations",        "Item does not meet expectations"),
        ("Defective item",                    "Defective item"),
        ("Missing parts",                     "Missing parts"),
        ("Missing items",                     "Missing items"),
        ("wouldn.t arrive on time",           "Product wouldn't arrive on time"),
        ("arrived too late",                  "Item arrived too late"),
    ]
    seen_reasons = set()
    for pattern, label in reason_map:
        # Find the largest number associated with this reason (skip percentage values)
        matches = re.findall(rf'{pattern}.{{0,30}}?(\d+)', full_text, re.I)
        if not matches:
            matches = re.findall(rf'(\d+).{{0,30}}?{pattern}', full_text, re.I)
        if matches and label not in seen_reasons:
            # Take the largest number that's plausibly a count (< 500)
            counts = [int(x) for x in matches if int(x) < 500]
            if counts:
                count = max(counts)
                seen_reasons.add(label)
                result["return_reasons"].append({"reason": label, "count": count, "pct": None})

    # Style returns (look for nail style names)
    style_patterns = ["petal", "nectar", "blossom", "promise", "paradise",
                      "nights", "garden", "bloom", "champagne", "treasure",
                      "fairy", "rosé", "rose", "silk", "pinky", "island",
                      "arabian", "aloha", "royal"]
    for row in rows:
        joined = row[0].lower() if row else ""
        if any(p in joined for p in style_patterns) and len(row) >= 2:
            count = _extract_number(row[1])
            pct = _extract_number(row[2]) if len(row) > 2 else None
            if count is not None:
                result["style_returns"].append({
                    "style": row[0], "count": int(count), "pct": pct
                })

    # Collection returns
    coll_patterns = ["dreamwear", "buy4", "buy 4", "almond", "square", "final sale",
                     "secret", "new drop", "next gen", "stiletto", "summer shine",
                     "organizer", "top trend", "spring bloom"]
    for row in rows:
        if any(p in row[0].lower() for p in coll_patterns) and len(row) >= 2:
            count = _extract_number(row[1])
            pct = _extract_number(row[2]) if len(row) > 2 else None
            if count is not None:
                result["collection_returns"].append({
                    "collection": row[0], "rows": int(count), "pct": pct
                })

    # Live attribution
    for row in rows:
        joined = " ".join(row).lower()
        if "unknown" in joined or "无法归因" in joined:
            n = _extract_number(row[1]) if len(row) > 1 else None
            if n: result["unknown_returns"] = int(n)
        if "非直播" in joined or "non-live" in joined:
            n = _extract_number(row[1]) if len(row) > 1 else None
            if n: result["non_live_returns"] = int(n)
        if ("直播" in joined or "live" in joined) and "非" not in joined and "unknown" not in joined:
            n = _extract_number(row[1]) if len(row) > 1 else None
            if n and result["live_returns"] is None:
                result["live_returns"] = int(n)

    return result


# ─── AUCTION REPORT PARSER ───────────────────────────────────────────────────

def parse_auction(html_bytes: bytes) -> dict:
    soup = _soup(html_bytes)
    full_text = soup.get_text(" ", strip=True)
    rows = _table_to_rows(soup)

    result = {
        "total_orders": None,
        "cancelled": None,
        "cancel_rate_pct": None,
        "effective_orders": None,
        "aov": None,
        "returns": None,
        "return_rate_pct": None,
        "return_aov": None,
        "cancel_reason": "Customer overdue to pay",
    }

    for pattern, key in [
        (r'(?:Total\s+)?Orders?\s*\n?\s*([\d,]+)', "total_orders"),
        (r'Cancelled?\D{0,10}([\d,]+)', "cancelled"),
        (r'Cancel\w*\s*Rate\D{0,10}([\d.]+)\s*%', "cancel_rate_pct"),
        (r'(?:Effective|Valid|有效)\s*Orders?\D{0,10}([\d,]+)', "effective_orders"),
        (r'(?:Effective|Valid|有效)\s*AOV\D{0,10}\$([\d.]+)', "aov"),
        (r'Return\s*Rate\D{0,10}([\d.]+)\s*%', "return_rate_pct"),
        (r'Return\w*\s*AOV\D{0,10}\$([\d.]+)', "return_aov"),
    ]:
        m = re.search(pattern, full_text, re.I)
        if m and result[key] is None:
            val = m.group(1).replace(",", "")
            result[key] = float(val) if "." in val or "rate" in pattern.lower() or "aov" in pattern.lower() else int(val)

    # Effective orders = total - cancelled
    if result["effective_orders"] is None and result["total_orders"] and result["cancelled"]:
        result["effective_orders"] = int(result["total_orders"]) - int(result["cancelled"])

    # Returns count
    m = re.search(r'(?:申请退货|Returned?\s*Orders?)\D{0,10}([\d]+)', full_text, re.I)
    if m: result["returns"] = int(m.group(1))

    return result


# ─── COLLECTION REPORT PARSER ────────────────────────────────────────────────

def parse_collection(html_bytes: bytes) -> dict:
    """
    Returns:
      links: [{collection, channel, return_rows, return_pct, cancelled, cancel_pct, diff_pp}]
      channel_summary: [{channel, return_rows, return_pct, cancelled, cancel_pct}]
    """
    soup = _soup(html_bytes)
    rows = _table_to_rows(soup)

    result = {"links": [], "channel_summary": []}

    coll_keywords = ["dreamwear", "buy4", "buy 4", "almond", "square", "final sale",
                     "secret", "new drop", "next gen", "stiletto", "summer shine",
                     "organizer", "top trend", "spring bloom", "clearance", "best seller",
                     "auction", "toolkits"]
    channel_keywords = ["达人带货", "官号视频", "直播间", "influencer", "official", "live"]

    for row in rows:
        if not row:
            continue
        joined_lower = " ".join(row).lower()
        first = row[0].lower()

        # Collection links
        if any(kw in first for kw in coll_keywords) and len(row) >= 4:
            nums = [_extract_number(c) for c in row[1:]]
            nums = [n for n in nums if n is not None]
            result["links"].append({
                "collection": row[0],
                "channel": row[1] if len(row) > 5 else "",
                "return_rows": int(nums[0]) if len(nums) > 0 else None,
                "return_pct": nums[1] if len(nums) > 1 else None,
                "cancelled": int(nums[2]) if len(nums) > 2 else None,
                "cancel_pct": nums[3] if len(nums) > 3 else None,
                "diff_pp": nums[4] if len(nums) > 4 else None,
            })

        # Channel summary
        elif any(kw in joined_lower for kw in channel_keywords) and len(row) >= 3:
            nums = [_extract_number(c) for c in row[1:]]
            nums = [n for n in nums if n is not None]
            result["channel_summary"].append({
                "channel": row[0],
                "return_rows": int(nums[0]) if len(nums) > 0 else None,
                "return_pct": nums[1] if len(nums) > 1 else None,
                "cancelled": int(nums[2]) if len(nums) > 2 else None,
                "cancel_pct": nums[3] if len(nums) > 3 else None,
            })

    return result
