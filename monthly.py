"""
monthly.py — Monthly baseline computation and storage.

Two ways to build a monthly baseline:
  A. Auto-aggregate from stored weekly JSONs in data/
  B. Process a full-month order CSV directly (more accurate)

Stored as data/{YYYY_MM}_monthly.json
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
import numpy as np

METRICS_DIR = Path(__file__).parent / "data"


def _ser(obj):
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, np.ndarray): return obj.tolist()
    raise TypeError(type(obj))


# ─── A. Aggregate from weekly JSONs ──────────────────────────────────────────

def weeks_in_month(year: int, month: int) -> list[int]:
    """Return week numbers that have stored metrics and fall within year/month."""
    available = []
    for p in sorted(METRICS_DIR.glob("W*_metrics.json")):
        try:
            n = int(p.stem.lstrip("W").split("_")[0])
            wm = json.loads(p.read_text(encoding="utf-8"))
            dr = wm.get("date_range", "")
            # Parse start date from date_range like "2026/06/08 – 06/14"
            try:
                start_str = dr.split("–")[0].strip()
                if "/" in start_str:
                    parts = start_str.split("/")
                    if len(parts) == 3:
                        y, m, _ = int(parts[0]), int(parts[1]), int(parts[2])
                        if y == year and m == month:
                            available.append(n)
            except Exception:
                pass
        except Exception:
            pass
    return sorted(available)


def aggregate_from_weeks(week_nums: list[int]) -> dict | None:
    """
    Build monthly average metrics from a list of stored weekly JSONs.
    Uses weighted averages for rates; simple averages for absolute values per week.
    """
    if not week_nums:
        return None

    weeks_data = []
    for w in week_nums:
        p = METRICS_DIR / f"W{w}_metrics.json"
        if p.exists():
            weeks_data.append(json.loads(p.read_text(encoding="utf-8")))

    if not weeks_data:
        return None

    n = len(weeks_data)

    def _sum(key):
        return sum(d.get(key, 0) or 0 for d in weeks_data)

    def _avg(key):
        vals = [d.get(key) for d in weeks_data if d.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    # Absolute totals (sum)
    total_orders   = _sum("total_orders")
    zero_orders    = _sum("zero_orders")
    cancelled_orders = _sum("cancelled_orders")
    effective_orders = _sum("effective_orders")
    paid_base        = _sum("paid_base")
    sku_sold         = _sum("sku_sold")
    items_sold       = _sum("items_sold")
    items_canceled   = _sum("items_canceled")
    items_returned   = _sum("items_returned")
    gmv              = _sum("gmv")
    order_amount_total = _sum("order_amount_total")

    # Weighted rates (from totals, not avg of rates)
    cancel_rate  = cancelled_orders / paid_base if paid_base else None
    return_rate  = (items_canceled + items_returned) / items_sold if items_sold else None
    aov          = gmv / effective_orders if effective_orders else None
    asp          = gmv / sku_sold if sku_sold else None
    upo          = sku_sold / effective_orders if effective_orders else None
    aov_incl_shipping = order_amount_total / effective_orders if effective_orders else None

    # Weekly averages (per-week averages for planning)
    weekly_avg = {
        "effective_orders": effective_orders / n,
        "gmv":              gmv / n,
        "cancelled_orders": cancelled_orders / n,
        "sku_sold":         sku_sold / n,
    }

    # Qty bands: weighted average
    qty_bands = {}
    for label in ["1件", "2件", "3件", "4件精确", "5件+"]:
        total_cnt = sum(d.get("qty_bands", {}).get(label, {}).get("count", 0) for d in weeks_data)
        total_eff = sum(d.get("effective_orders", 0) for d in weeks_data)
        aov_vals  = [d.get("qty_bands", {}).get(label, {}).get("aov") for d in weeks_data
                     if d.get("qty_bands", {}).get(label, {}).get("aov")]
        qty_bands[label] = {
            "count": total_cnt / n,
            "pct":   total_cnt / total_eff if total_eff else 0,
            "aov":   sum(aov_vals) / len(aov_vals) if aov_vals else None,
        }

    # AOV distribution: aggregate
    aov_dist = {}
    for lbl in ["<$20","$20-30","$30-40","$40-60","$60-80","$80-120","$120+"]:
        aov_dist[lbl] = sum(d.get("aov_dist", {}).get(lbl, 0) for d in weeks_data)

    # Price GMV: sum
    price_gmv = {}
    for lbl in ["≤$29.99","$34.99","$39.99","$44.99","$49.99","$54.99","$55+"]:
        price_gmv[lbl] = sum(d.get("price_gmv", {}).get(lbl, 0) for d in weeks_data)
    total_price_gmv = sum(price_gmv.values())

    # Payment dist: sum then normalise
    payment_combined: dict[str, int] = {}
    for d in weeks_data:
        for method, cnt in (d.get("payment_dist") or {}).items():
            payment_combined[method] = payment_combined.get(method, 0) + cnt

    return {
        # Source info
        "source":          "aggregated_from_weeks",
        "weeks_included":  week_nums,
        "num_weeks":       n,

        # Volume (monthly totals)
        "total_orders":       total_orders,
        "zero_orders":        zero_orders,
        "cancelled_orders":   cancelled_orders,
        "effective_orders":   effective_orders,
        "paid_base":          paid_base,
        "sku_sold":           sku_sold,
        "items_sold":         items_sold,
        "items_canceled":     items_canceled,
        "items_returned":     items_returned,

        # Weekly averages (for comparison in report)
        "weekly_avg":         weekly_avg,

        # Rates (weighted from totals)
        "cancel_rate":        cancel_rate,
        "return_rate":        return_rate,

        # Value metrics
        "gmv":                gmv,
        "aov":                aov,
        "asp":                asp,
        "upo":                upo,
        "order_amount_total": order_amount_total,
        "aov_incl_shipping":  aov_incl_shipping,

        # Breakdowns
        "qty_bands":          qty_bands,
        "aov_dist":           aov_dist,
        "price_gmv":          price_gmv,
        "total_price_gmv":    total_price_gmv,
        "payment_dist":       payment_combined,
    }


# ─── B. Process full-month order CSV ─────────────────────────────────────────

def aggregate_from_csv(csv_bytes, items_returned_manual: int = 0) -> dict:
    """Process a full-month order CSV. Returns same structure as weekly order_metrics."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from order_processor import process_orders
    m = process_orders(csv_bytes, items_returned=items_returned_manual)
    m["source"] = "full_month_csv"
    return m


# ─── Save / load monthly baseline ────────────────────────────────────────────

def save_monthly(year: int, month: int, metrics: dict) -> Path:
    metrics["year"]  = year
    metrics["month"] = month
    metrics["saved_at"] = datetime.now().isoformat()
    path = METRICS_DIR / f"{year}_{month:02d}_monthly.json"
    path.write_text(json.dumps(metrics, default=_ser, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_monthly(year: int, month: int) -> dict | None:
    path = METRICS_DIR / f"{year}_{month:02d}_monthly.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def list_saved_months() -> list[tuple[int, int]]:
    result = []
    for p in sorted(METRICS_DIR.glob("????_??_monthly.json")):
        try:
            parts = p.stem.split("_")
            result.append((int(parts[0]), int(parts[1])))
        except Exception:
            pass
    return sorted(result)
