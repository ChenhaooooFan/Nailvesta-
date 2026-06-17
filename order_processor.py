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

from __future__ import annotations

import pandas as pd
import numpy as np

# ─── LIVE-ORDER CLASSIFICATION ────────────────────────────────────────────────

def _classify_live_orders(df: pd.DataFrame) -> pd.Series:
    """
    Returns a boolean Series (same index as df): True = row belongs to a live-stream order.

    Live order criteria — BOTH must hold at the ORDER level:
      1. Created Time (converted to LA/Pacific) falls within 10:30–18:00 or 19:00–23:00.
      2. Every SKU line in the order has unit paid price ≤ 65 % of original price
         (i.e. discount ≥ 35 % off).
         unit_paid  = SKU Subtotal After Discount ÷ Quantity
         unit_orig  = SKU Unit Original Price
    """
    from zoneinfo import ZoneInfo
    LA = ZoneInfo("America/Los_Angeles")

    TIME_COL = "Created Time"
    M_COL    = "SKU Unit Original Price"       # original (M column)
    Q_COL    = "SKU Subtotal After Discount"   # paid subtotal (Q column)
    QTY_COL  = "Quantity"

    if TIME_COL not in df.columns:
        return pd.Series(False, index=df.index)

    # ── 1. Time window ───────────────────────────────────────────────────────
    # TikTok Shop CSV: "06/14/2026 11:25:41 PM" (UTC, may have trailing whitespace)
    cleaned = df[TIME_COL].astype(str).str.strip()
    raw = pd.to_datetime(cleaned, format="%m/%d/%Y %I:%M:%S %p", errors="coerce")
    if raw.isna().all():
        raw = pd.to_datetime(cleaned, errors="coerce")
    if raw.dt.tz is None:
        raw = raw.dt.tz_localize("UTC")
    dt_la  = raw.dt.tz_convert(LA)
    hr     = dt_la.dt.hour + dt_la.dt.minute / 60
    in_win = ((hr >= 10.5) & (hr < 18.0)) | ((hr >= 19.0) & (hr < 23.0))

    # ── 2. Discount ≥ 35 % off per SKU row ──────────────────────────────────
    if not all(c in df.columns for c in [M_COL, Q_COL, QTY_COL]):
        return pd.Series(False, index=df.index)

    qty       = pd.to_numeric(df[QTY_COL], errors="coerce").replace(0, np.nan)
    unit_paid = pd.to_numeric(df[Q_COL],   errors="coerce") / qty
    unit_orig = pd.to_numeric(df[M_COL],   errors="coerce").replace(0, np.nan)
    disc_ok   = (unit_paid / unit_orig) <= 0.65

    tmp = df.assign(_win=in_win.values, _disc=disc_ok.fillna(False).values)

    # ── 3. Order-level gate: time (first row) + ALL SKUs pass discount ───────
    grp        = tmp.groupby("Order ID")
    order_win  = grp["_win"].first()
    order_disc = grp["_disc"].all()
    live_ids   = order_win.index[order_win & order_disc]

    return df["Order ID"].isin(live_ids)


def _segment_metrics(df_sub: pd.DataFrame) -> dict:
    """
    Compute core metrics for a subset of the raw order CSV.
    Mirrors the key calculations in process_orders().
    """
    _empty = {
        "effective_orders": 0, "cancelled_orders": 0, "paid_base": 0,
        "items_sold": 0, "items_canceled": 0, "items_returned": 0,
        "cancel_rate": None, "return_rate": None,
        "gmv": 0.0, "aov": None, "sku_sold": 0,
    }
    if df_sub.empty:
        return _empty

    order = df_sub.groupby("Order ID").agg(
        status  = ("Order Status",                "first"),
        amount  = ("Order Amount",                "first"),
        qty     = ("Quantity",                    "sum"),
        sku_sub = ("SKU Subtotal After Discount", "sum"),
    ).reset_index()

    is_paid      = order["amount"] > 0
    is_cancelled = order["status"] == "Canceled"
    is_effective = is_paid & ~is_cancelled

    eff_count  = int(is_effective.sum())
    can_count  = int((is_paid & is_cancelled).sum())
    paid_base  = int(is_paid.sum())

    items_sold     = int(order.loc[is_paid,               "qty"].sum())
    items_canceled = int(order.loc[is_paid & is_cancelled,"qty"].sum())

    ret_col = "Sku Quantity of return"
    if ret_col in df_sub.columns:
        paid_ids = set(order.loc[is_paid, "Order ID"])
        items_returned = int(
            pd.to_numeric(
                df_sub.loc[df_sub["Order ID"].isin(paid_ids), ret_col],
                errors="coerce",
            ).fillna(0).sum()
        )
    else:
        items_returned = 0

    cancel_rate = items_canceled / items_sold if items_sold else None
    return_rate = (items_canceled + items_returned) / items_sold if items_sold else None
    gmv         = float(order.loc[is_effective, "sku_sub"].sum())
    aov         = gmv / eff_count if eff_count else None
    eff_ids     = set(order.loc[is_effective, "Order ID"])
    sku_sold    = int(df_sub.loc[df_sub["Order ID"].isin(eff_ids), "Quantity"].sum())

    return {
        "effective_orders": eff_count,
        "cancelled_orders": can_count,
        "paid_base":        paid_base,
        "items_sold":       items_sold,
        "items_canceled":   items_canceled,
        "items_returned":   items_returned,
        "cancel_rate":      cancel_rate,
        "return_rate":      return_rate,
        "gmv":              gmv,
        "aov":              aov,
        "sku_sold":         sku_sold,
    }


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
    paid_base        = is_paid.sum()              # 付费订单数（含取消），仅用于展示订单量层级
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

    # ── Cancel rate（SKU件数口径：取消件数 ÷ 付费总件数，与 Return Rate 分母一致）───
    cancel_rate = items_canceled / items_sold if items_sold else None

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

    # ── Live vs Store channel split ──────────────────────────────────────────
    has_time_col  = "Created Time" in df.columns
    if has_time_col:
        live_row_mask = _classify_live_orders(df)
        live_m  = _segment_metrics(df[live_row_mask])
        store_m = _segment_metrics(df[~live_row_mask])
        live_order_pct  = (live_m["effective_orders"]  / effective_orders
                           if effective_orders else None)
        store_order_pct = (store_m["effective_orders"] / effective_orders
                           if effective_orders else None)
    else:
        live_m = store_m = None
        live_order_pct = store_order_pct = None

    live_store_split = {
        "has_time_col":    has_time_col,
        "live":            live_m,
        "store":           store_m,
        "live_order_pct":  live_order_pct,
        "store_order_pct": store_order_pct,
    }

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

        # ── Channel split ────────────────────────────────────────────────────
        "live_store_split":      live_store_split,
    }


# ─── B链产品分析 ──────────────────────────────────────────────────────────────

B_CHAIN_PREFIXES = ("NOB", "NVT")   # 可扩展：NOB001, NOB002, NVT001, NVT002 ...


def analyze_b_chain(
    csv_bytes,
    b_prefixes: tuple[str, ...] = B_CHAIN_PREFIXES,
    items_returned_b: int = 0,       # 来自退货报告中 B链 SKU 退货件数（手动传入或解析）
) -> dict:
    """
    从全量订单 CSV 中提取 B链产品表现。
    B链定义：Seller SKU 以 b_prefixes 中任一前缀开头。
    口径与全店一致：有效订单 = 付费 − Cancelled，Return Rate = SKU 件数口径。
    """
    import pandas as pd

    df = pd.read_csv(csv_bytes)

    # ── B链行过滤 ──────────────────────────────────────────────────────────────
    b_mask = df["Seller SKU"].str.match(
        "^(" + "|".join(b_prefixes) + ")", na=False
    )
    b_df = df[b_mask].copy()

    if b_df.empty:
        return {"has_data": False}

    # ── 订单级聚合（一个 Order 可能含多条 B链 SKU 行）─────────────────────────
    order_agg = b_df.groupby("Order ID").agg(
        status        = ("Order Status",                "first"),
        order_amount  = ("Order Amount",                "first"),   # Z列 整单支付
        b_qty         = ("Quantity",                    "sum"),     # B链 件数
        b_sub_after   = ("SKU Subtotal After Discount", "sum"),     # B链 折后小计
        b_sub_before  = ("SKU Subtotal Before Discount","sum"),
        b_return_qty  = ("Sku Quantity of return",      "sum"),
        skus          = ("Seller SKU", lambda x: ", ".join(sorted(set(x)))),
        variation     = ("Variation",                   "first"),
        payment       = ("Payment Method",               "first"),
    ).reset_index()

    # ── 分层 ──────────────────────────────────────────────────────────────────
    is_zero_order = order_agg["order_amount"] == 0
    is_paid       = ~is_zero_order
    is_cancelled  = (order_agg["status"] == "Canceled") & is_paid
    is_effective  = is_paid & ~is_cancelled

    total_orders      = int(is_paid.sum())
    zero_orders       = int(is_zero_order.sum())
    cancelled_orders  = int(is_cancelled.sum())
    effective_orders  = int(is_effective.sum())

    # ── SKU 件数口径（Return Rate）──────────────────────────────────────────────
    items_sold     = int(b_df[b_df["Order Amount"] > 0]["Quantity"].sum())
    items_canceled = int(
        b_df[(b_df["Order Amount"] > 0) & (b_df["Order Status"] == "Canceled")]["Quantity"].sum()
    )
    items_returned_csv = int(b_df["Sku Quantity of return"].sum())   # CSV 列有值时用这个
    items_returned_final = max(items_returned_b, items_returned_csv) # 以较大值为准（退货报告更全）
    return_rate = (
        (items_canceled + items_returned_final) / items_sold
        if items_sold else None
    )

    # ── 收入口径 ─────────────────────────────────────────────────────────────
    eff_orders = order_agg[is_effective]
    b_gmv      = float(eff_orders["b_sub_after"].sum())   # B链 SKU 贡献 GMV
    b_aov_sku  = b_gmv / effective_orders if effective_orders else None

    # ── 按 SKU 型号拆分 ───────────────────────────────────────────────────────
    sku_breakdown = []
    for sku_prefix in b_df["Seller SKU"].unique():
        sub = b_df[b_df["Seller SKU"] == sku_prefix]
        s_paid  = sub[sub["Order Amount"] > 0]
        s_eff   = s_paid[s_paid["Order Status"] != "Canceled"]
        s_can   = s_paid[s_paid["Order Status"] == "Canceled"]
        s_items_sold = int(s_paid["Quantity"].sum())
        s_items_can  = int(s_can["Quantity"].sum())
        s_items_ret  = int(sub["Sku Quantity of return"].sum())
        s_rr = (s_items_can + s_items_ret) / s_items_sold if s_items_sold else None
        s_gmv = float(s_eff["SKU Subtotal After Discount"].sum())
        sku_breakdown.append({
            "sku":             sku_prefix,
            "variation":       sub["Variation"].iloc[0] if not sub.empty else "—",
            "effective_orders":int(s_eff["Order ID"].nunique()),
            "cancelled_orders":int(s_can["Order ID"].nunique()),
            "items_sold":      s_items_sold,
            "items_canceled":  s_items_can,
            "items_returned":  s_items_ret,
            "return_rate":     s_rr,
            "gmv":             s_gmv,
        })

    # ── 顾客支付明细（逐订单，含Z列整单金额）──────────────────────────────────
    order_detail = []
    for _, row in eff_orders.iterrows():
        order_detail.append({
            "order_id":      str(row["Order ID"]),
            "skus":          row["skus"],
            "b_qty":         int(row["b_qty"]),
            "b_sub_after":   float(row["b_sub_after"]),    # B链 SKU 贡献
            "order_amount":  float(row["order_amount"]),   # Z列：整单实付
            "note":          "含A链其他商品" if float(row["order_amount"]) > float(row["b_sub_after"]) + 5 else "B链为主",
            "payment":       str(row["payment"]),
        })

    return {
        "has_data":          True,
        "b_prefixes":        list(b_prefixes),
        # 订单量
        "total_orders":      total_orders,
        "zero_orders":       zero_orders,
        "cancelled_orders":  cancelled_orders,
        "effective_orders":  effective_orders,
        # Return Rate（SKU件数口径）
        "items_sold":        items_sold,
        "items_canceled":    items_canceled,
        "items_returned":    items_returned_final,
        "return_rate":       return_rate,
        # 收入
        "b_gmv":             b_gmv,
        "b_aov_sku":         b_aov_sku,
        # 拆分
        "sku_breakdown":     sku_breakdown,
        "order_detail":      order_detail,
    }


# ─── 新款表现分析 ─────────────────────────────────────────────────────────────

def analyze_new_styles(csv_input, catalog_df, days: int = 28, ref_date=None) -> dict:
    """
    分析近 days 天内上架款式的销售表现。
    catalog_df 需含列：SKU、款式英文名称、定价、上架时间、上架状态
    csv_input 可以是 bytes/file-like 或已读取的 DataFrame。
    顾客实付件单价 = SKU Subtotal After Discount ÷ Quantity（折后小计除以件数）
    原价 = SKU Unit Original Price（M列）
    """
    from datetime import datetime, timedelta

    if ref_date is None:
        ref_date = datetime.today()
    cutoff = ref_date - timedelta(days=days)

    cat = catalog_df[['SKU', '款式英文名称', '定价', '上架时间', '上架状态']].copy()
    cat = cat.dropna(subset=['SKU'])
    cat['SKU'] = cat['SKU'].astype(str).str.strip()
    cat['_listed_dt'] = pd.to_datetime(cat['上架时间'], errors='coerce')
    new_cat = cat[cat['_listed_dt'] >= pd.Timestamp(cutoff)].copy()

    if new_cat.empty:
        return {"has_data": False, "days": days, "num_new_styles": 0}

    df = csv_input if isinstance(csv_input, pd.DataFrame) else pd.read_csv(csv_input)
    df = df.copy()
    df['Seller SKU'] = df['Seller SKU'].astype(str).str.strip()

    new_skus = set(new_cat['SKU'])
    new_df = df[df['Seller SKU'].isin(new_skus)].copy()

    if new_df.empty:
        return {
            "has_data": True,
            "days": days,
            "ref_date": ref_date.strftime("%Y/%m/%d"),
            "num_new_styles": len(new_cat),
            "style_breakdown": [],
            "total_orders": 0, "cancelled_orders": 0, "effective_orders": 0,
            "items_sold": 0, "items_canceled": 0, "items_returned": 0,
            "return_rate": None, "gmv": 0.0,
        }

    order_agg = new_df.groupby("Order ID").agg(
        status=("Order Status", "first"),
        amount=("Order Amount", "first"),
    ).reset_index()

    is_paid      = order_agg["amount"] > 0
    is_cancelled = (order_agg["status"] == "Canceled") & is_paid
    is_effective = is_paid & ~is_cancelled

    total_orders     = int(is_paid.sum())
    cancelled_orders = int(is_cancelled.sum())
    effective_orders = int(is_effective.sum())

    paid_rows      = new_df[new_df["Order Amount"] > 0]
    items_sold     = int(paid_rows["Quantity"].sum())
    items_canceled = int(paid_rows[paid_rows["Order Status"] == "Canceled"]["Quantity"].sum())
    items_returned = int(new_df["Sku Quantity of return"].sum())
    cancel_rate    = items_canceled / items_sold if items_sold else None
    return_rate    = (items_canceled + items_returned) / items_sold if items_sold else None

    eff_ids = set(order_agg.loc[is_effective, "Order ID"])
    eff_rows = new_df[new_df["Order ID"].isin(eff_ids)]
    gmv = float(eff_rows["SKU Subtotal After Discount"].sum())

    style_rows = []
    for _, cat_row in new_cat.iterrows():
        sku  = cat_row['SKU']
        sub  = new_df[new_df['Seller SKU'] == sku]
        s_paid = sub[sub["Order Amount"] > 0]
        s_eff  = s_paid[s_paid["Order Status"] != "Canceled"]
        s_can  = s_paid[s_paid["Order Status"] == "Canceled"]

        s_items_sold = int(s_paid["Quantity"].sum())
        s_items_can  = int(s_can["Quantity"].sum())
        s_items_ret  = int(sub["Sku Quantity of return"].sum())
        s_cr   = s_items_can / s_items_sold if s_items_sold else None
        s_rr   = (s_items_can + s_items_ret) / s_items_sold if s_items_sold else None
        s_gmv  = float(s_eff["SKU Subtotal After Discount"].sum())

        # 顾客实付件单价 = SKU Subtotal After Discount ÷ Quantity（有效订单行）
        qty_eff = s_eff["Quantity"].sum() if len(s_eff) > 0 else 0
        paid_unit_price = float(s_eff["SKU Subtotal After Discount"].sum() / qty_eff) if qty_eff > 0 else None

        # 原价：SKU Unit Original Price（CSV M列）
        csv_orig_price = None
        if "SKU Unit Original Price" in s_paid.columns and len(s_paid) > 0:
            csv_orig_price = float(s_paid["SKU Unit Original Price"].dropna().mean()) if s_paid["SKU Unit Original Price"].notna().any() else None

        catalog_price = cat_row.get('定价')

        style_rows.append({
            "sku":             sku,
            "name":            str(cat_row.get('款式英文名称') or ''),
            "listed_date":     str(cat_row.get('上架时间') or ''),
            "catalog_price":   float(catalog_price) if pd.notna(catalog_price) else None,
            "csv_orig_price":  csv_orig_price,
            "paid_unit_price": paid_unit_price,
            "effective_orders": int(s_eff["Order ID"].nunique()),
            "cancelled_orders": int(s_can["Order ID"].nunique()),
            "items_sold":      s_items_sold,
            "items_canceled":  s_items_can,
            "items_returned":  s_items_ret,
            "cancel_rate":     s_cr,
            "return_rate":     s_rr,
            "gmv":             s_gmv,
        })

    style_rows.sort(key=lambda x: -x["effective_orders"])

    return {
        "has_data":        True,
        "days":            days,
        "ref_date":        ref_date.strftime("%Y/%m/%d"),
        "num_new_styles":  len(new_cat),
        "style_breakdown": style_rows,
        "total_orders":    total_orders,
        "cancelled_orders": cancelled_orders,
        "effective_orders": effective_orders,
        "items_sold":      items_sold,
        "items_canceled":  items_canceled,
        "items_returned":  items_returned,
        "cancel_rate":     cancel_rate,
        "return_rate":     return_rate,
        "gmv":             gmv,
    }


# ─── 供应商分析 ───────────────────────────────────────────────────────────────

def analyze_by_supplier(csv_input, catalog_df) -> list:
    """
    按厂家（供应商）汇总销售与退货表现。
    catalog_df 需含列：SKU、厂家、款式英文名称、上架状态
    csv_input 可以是 bytes/file-like 或已读取的 DataFrame。
    """
    cat = catalog_df[['SKU', '厂家', '款式英文名称', '上架状态']].copy()
    cat = cat.dropna(subset=['SKU'])
    cat['SKU'] = cat['SKU'].astype(str).str.strip()

    df = csv_input if isinstance(csv_input, pd.DataFrame) else pd.read_csv(csv_input)
    df = df.copy()
    df['Seller SKU'] = df['Seller SKU'].astype(str).str.strip()

    merged = df.merge(
        cat.rename(columns={
            'SKU':      'Seller SKU',
            '厂家':     '_supplier',
            '款式英文名称': '_style_name',
            '上架状态':  '_status',
        }),
        on='Seller SKU', how='left'
    )
    merged['_supplier'] = merged['_supplier'].fillna('未知供应商')

    results = []
    for supplier, grp in merged.groupby('_supplier'):
        order_agg = grp.groupby("Order ID").agg(
            status=("Order Status", "first"),
            amount=("Order Amount", "first"),
        ).reset_index()

        is_paid      = order_agg["amount"] > 0
        is_cancelled = (order_agg["status"] == "Canceled") & is_paid
        is_effective = is_paid & ~is_cancelled

        effective_orders = int(is_effective.sum())
        cancelled_orders = int(is_cancelled.sum())

        paid_rows    = grp[grp["Order Amount"] > 0]
        items_sold   = int(paid_rows["Quantity"].sum())
        items_canceled = int(paid_rows[paid_rows["Order Status"] == "Canceled"]["Quantity"].sum())
        items_returned = int(grp["Sku Quantity of return"].sum())
        return_rate    = (items_canceled + items_returned) / items_sold if items_sold else None

        eff_ids = set(order_agg.loc[is_effective, "Order ID"])
        gmv = float(grp[grp["Order ID"].isin(eff_ids)]["SKU Subtotal After Discount"].sum())

        # 产品图册维度统计
        sup_cat = catalog_df[catalog_df.get('厂家', pd.Series(dtype=str)) == supplier] if '厂家' in catalog_df.columns else pd.DataFrame()
        active_sku_count = int((sup_cat['上架状态'] != '已下架').sum()) if len(sup_cat) > 0 else 0
        total_sku_count  = len(sup_cat)

        # 本周有销量款数（有效订单中出现的 SKU）
        sold_sku_count = int(
            paid_rows[paid_rows["Order Status"] != "Canceled"]["Seller SKU"].nunique()
        )

        # 退货款式 Top 5
        ret_grp = grp[grp["Sku Quantity of return"] > 0].copy()
        if len(ret_grp) > 0:
            ret_agg = ret_grp.groupby("Seller SKU").agg(
                name=("_style_name", "first"),
                returned=("Sku Quantity of return", "sum"),
            ).reset_index().sort_values("returned", ascending=False)
            return_styles = [
                {"sku": r["Seller SKU"], "name": str(r["name"] or ''), "returned": int(r["returned"])}
                for _, r in ret_agg.head(5).iterrows()
            ]
        else:
            return_styles = []

        results.append({
            "supplier":        supplier,
            "effective_orders": effective_orders,
            "cancelled_orders": cancelled_orders,
            "items_sold":      items_sold,
            "items_canceled":  items_canceled,
            "items_returned":  items_returned,
            "return_rate":     return_rate,
            "gmv":             gmv,
            "total_sku_count": total_sku_count,
            "active_sku_count": active_sku_count,
            "sold_sku_count":  sold_sku_count,
            "return_styles":   return_styles,
        })

    return sorted(results, key=lambda x: -x["effective_orders"])
