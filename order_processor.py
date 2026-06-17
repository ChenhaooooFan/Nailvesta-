"""
order_processor.py — Compute all metrics from the full order CSV.

Key definitions (NailVesta):
  - 全量订单     : all rows, deduped by Order ID
  - 0元单        : Order Amount == 0 (influencer free orders)
  - Paid base    : all paid orders = 全量 - 0元 (includes cancelled)
  - 有效付费订单  : paid base - Cancelled = delivered/in-transit paid orders
  - Cancel Rate  : Cancelled / Paid base
  - Items Sold   : sum(Quantity) for all paid orders (Amount > 0, incl. cancelled)
  - Items Canceled: sum(Quantity) for Cancelled paid orders
  - Return Rate  : (Items Canceled + Items Returned) / Items Sold
"""

import pandas as pd
import numpy as np


def process_orders(csv_bytes: bytes, items_returned: int = 0) -> dict:
    """
    Parse the order CSV and return a comprehensive metrics dict.

    Parameters
    ----------
    csv_bytes     : raw bytes of the CSV file
    items_returned: row count from returned report HTML (each row = 1 item)
    """
    df = pd.read_csv(csv_bytes)

    # ── Order-level aggregation ──────────────────────────────────────────────
    order = df.groupby("Order ID").agg(
        status    = ("Order Status",           "first"),
        amount    = ("Order Amount",           "first"),
        created   = ("Created Time",           "first"),
        payment   = ("Payment Method",         "first"),
        total_qty = ("Quantity",               "sum"),   # items per order
        sku_sub   = ("SKU Subtotal After Discount", "sum"),
        orig_price= ("SKU Unit Original Price","first"), # representative price
        product   = ("Product Name",           "first"),
    ).reset_index()

    # ── Segment masks ────────────────────────────────────────────────────────
    is_zero      = order["amount"] == 0
    is_cancelled = order["status"] == "Canceled"
    is_paid      = order["amount"] > 0            # paid (incl. cancelled)
    is_effective = is_paid & ~is_cancelled         # 有效付费订单

    # ── Top-level counts ────────────────────────────────────────────────────
    total_orders     = len(order)
    zero_orders      = is_zero.sum()
    cancelled_orders = (is_paid & is_cancelled).sum()
    paid_base        = is_paid.sum()              # 分母 for cancel rate
    effective_orders = is_effective.sum()

    # ── Item-level counts (NailVesta return rate formula) ────────────────────
    # Items = Quantity (row-level), aggregated at order level
    items_sold     = order.loc[is_paid,      "total_qty"].sum()  # denom
    items_canceled = order.loc[is_paid & is_cancelled, "total_qty"].sum()
    return_rate    = (items_canceled + items_returned) / items_sold if items_sold else None

    # ── GMV & AOV (SKU Sub After Discount, excluding shipping) ──────────────
    gmv = order.loc[is_effective, "sku_sub"].sum()
    aov = gmv / effective_orders if effective_orders else None

    # ── Order Amount totals (incl. shipping — separate from GMV) ────────────
    order_amount_total = order.loc[is_effective, "amount"].sum()
    aov_incl_shipping  = order_amount_total / effective_orders if effective_orders else None

    # ── Cancelled order metrics ──────────────────────────────────────────────
    cancelled_avg_amount = order.loc[is_paid & is_cancelled, "amount"].mean()
    cancelled_gmv        = order.loc[is_paid & is_cancelled, "amount"].sum()

    # ── Cancel rate ─────────────────────────────────────────────────────────
    cancel_rate = cancelled_orders / paid_base if paid_base else None

    # ── SKU Sold & 连带率 ────────────────────────────────────────────────────
    sku_sold   = df.loc[df["Order ID"].isin(order.loc[is_effective, "Order ID"]), "Quantity"].sum()
    upo        = sku_sold / effective_orders if effective_orders else None  # units per order

    # ── ASP (per item price) ────────────────────────────────────────────────
    asp = gmv / sku_sold if sku_sold else None

    # ── 件数结构 ────────────────────────────────────────────────────────────
    qty_bands = {}
    eff = order.loc[is_effective].copy()
    for label, mask in [
        ("1件",   eff["total_qty"] == 1),
        ("2件",   eff["total_qty"] == 2),
        ("3件",   eff["total_qty"] == 3),
        ("4件精确", eff["total_qty"] == 4),
        ("5件+",  eff["total_qty"] >= 5),
    ]:
        sub = eff.loc[mask]
        qty_bands[label] = {
            "count": len(sub),
            "pct":   len(sub) / effective_orders if effective_orders else 0,
            "aov":   sub["amount"].mean() if len(sub) else None,
        }

    # ── 支付方式分布 ────────────────────────────────────────────────────────
    pm_df = df.loc[df["Order ID"].isin(order.loc[is_effective, "Order ID"])
                  ].drop_duplicates("Order ID")[["Payment Method"]]
    payment_dist = pm_df["Payment Method"].value_counts().to_dict()

    # ── AOV 分布 ───────────────────────────────────────────────────────────
    bins   = [0, 20, 30, 40, 60, 80, 120, 9999]
    labels = ["<$20","$20-30","$30-40","$40-60","$60-80","$80-120","$120+"]
    eff_copy = eff.copy()
    eff_copy["aov_bin"] = pd.cut(eff_copy["amount"], bins=bins, labels=labels)
    aov_dist = eff_copy["aov_bin"].value_counts(sort=False).to_dict()
    aov_dist = {str(k): int(v) for k, v in aov_dist.items()}

    # ── 原价区间 GMV (SKU Sub After Discount) ────────────────────────────────
    eff_rows = df.loc[df["Order ID"].isin(order.loc[is_effective, "Order ID"])].copy()
    price_bins   = [0, 30, 35, 40, 45, 50, 55, 9999]
    price_labels = ["≤$29.99","$34.99","$39.99","$44.99","$49.99","$54.99","$55+"]
    eff_rows["price_band"] = pd.cut(
        eff_rows["SKU Unit Original Price"], bins=price_bins, labels=price_labels
    )
    price_gmv = eff_rows.groupby("price_band", observed=True)["SKU Subtotal After Discount"].sum()
    price_gmv = {str(k): float(v) for k, v in price_gmv.items()}
    total_price_gmv = sum(price_gmv.values())

    # ── Cancelled SKU Units (for reports) ────────────────────────────────────
    # Get from original df row-level
    cancelled_ids = order.loc[is_paid & is_cancelled, "Order ID"]
    cancelled_sku_units = df.loc[df["Order ID"].isin(cancelled_ids), "Quantity"].sum()

    # ── Order status breakdown (for 0元 record) ─────────────────────────────
    zero_order_ids = order.loc[is_zero, "Order ID"]
    zero_sku_units = df.loc[df["Order ID"].isin(zero_order_ids), "Quantity"].sum()

    return {
        # ── Volume ──────────────────────────────────────────────────────────
        "total_orders":          int(total_orders),
        "zero_orders":           int(zero_orders),
        "zero_sku_units":        int(zero_sku_units),
        "cancelled_orders":      int(cancelled_orders),
        "paid_base":             int(paid_base),           # total - 0元
        "effective_orders":      int(effective_orders),    # paid - cancelled
        "cancelled_sku_units":   int(cancelled_sku_units),

        # ── Cancel & Return rates ────────────────────────────────────────────
        "cancel_rate":           cancel_rate,              # fraction
        "items_sold":            int(items_sold),
        "items_canceled":        int(items_canceled),
        "items_returned":        items_returned,
        "return_rate":           return_rate,              # fraction

        # ── GMV & AOV (excl. shipping) ───────────────────────────────────────
        "gmv":                   float(gmv),
        "aov":                   float(aov) if aov else None,
        "asp":                   float(asp) if asp else None,
        "sku_sold":              int(sku_sold),
        "upo":                   float(upo) if upo else None,

        # ── Order Amount (incl. shipping) ────────────────────────────────────
        "order_amount_total":    float(order_amount_total),
        "aov_incl_shipping":     float(aov_incl_shipping) if aov_incl_shipping else None,

        # ── Cancelled order detail ───────────────────────────────────────────
        "cancelled_avg_amount":  float(cancelled_avg_amount) if not pd.isna(cancelled_avg_amount) else None,
        "cancelled_gmv":         float(cancelled_gmv),

        # ── Breakdowns ──────────────────────────────────────────────────────
        "qty_bands":             qty_bands,
        "payment_dist":          payment_dist,
        "aov_dist":              aov_dist,
        "price_gmv":             price_gmv,
        "total_price_gmv":       float(total_price_gmv),
    }
