"""
app.py — NailVesta 中台运营数据周报
Run with: streamlit run app.py
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
import json
import io
from pathlib import Path
from datetime import datetime

from parsers import parse_cancelled, parse_returned, parse_auction, parse_collection
from order_processor import process_orders, analyze_b_chain, analyze_new_styles, analyze_by_supplier
from report_generator import generate_report
from monthly import (
    weeks_in_month, aggregate_from_weeks, aggregate_from_csv,
    save_monthly, load_monthly, list_saved_months
)

# ─── PERSISTENT METRICS STORAGE ──────────────────────────────────────────────

METRICS_DIR = Path(__file__).parent / "data"
METRICS_DIR.mkdir(exist_ok=True)


def _serialise(obj):
    import numpy as np
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, np.ndarray): return obj.tolist()
    raise TypeError(f"Not serializable: {type(obj)}")


def save_week_metrics(week_num: int, metrics: dict, date_range: str = ""):
    snapshot = dict(metrics)
    snapshot["week_num"]   = week_num
    snapshot["date_range"] = date_range
    snapshot["saved_at"]   = datetime.now().isoformat()
    path = METRICS_DIR / f"W{week_num}_metrics.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, default=_serialise, ensure_ascii=False, indent=2)
    return path


def load_week_metrics(week_num: int) -> dict | None:
    path = METRICS_DIR / f"W{week_num}_metrics.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def list_saved_weeks() -> list[int]:
    weeks = []
    for p in sorted(METRICS_DIR.glob("W*_metrics.json")):
        try:
            n = int(p.stem.lstrip("W").split("_")[0])
            weeks.append(n)
        except ValueError:
            pass
    return sorted(weeks)


# ─── PAGE CONFIG ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="NailVesta 中台运营数据周报",
    page_icon="💅",
    layout="wide",
)

# ─── CUSTOM CSS ──────────────────────────────────────────────────────────────

st.markdown("""
<style>
/* ── 全局字体 ── */
html, body, [class*="css"] { font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; }

/* ── Sidebar 背景 ── */
[data-testid="stSidebar"] > div:first-child { background: #F3F6FB; }

/* ── 主区域顶部留白 ── */
[data-testid="stAppViewContainer"] > .main .block-container { padding-top: 1.4rem; }

/* ── 标题卡片 ── */
.page-header {
    background: linear-gradient(135deg, #1A3E6C 0%, #2E75B6 100%);
    border-radius: 14px;
    padding: 22px 28px 18px;
    color: white;
    margin-bottom: 20px;
    box-shadow: 0 2px 12px rgba(30,70,120,0.18);
}
.page-header .app-name {
    font-size: 13px; font-weight: 600; letter-spacing: 1.5px;
    text-transform: uppercase; opacity: 0.75; margin-bottom: 4px;
}
.page-header .week-title {
    font-size: 26px; font-weight: 700; margin: 0 0 4px 0; line-height: 1.2;
}
.page-header .date-sub {
    font-size: 14px; opacity: 0.82; margin-top: 2px;
}

/* ── 文件清单 ── */
.file-checklist { margin: 0; padding: 0; list-style: none; }
.file-row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 14px; border-radius: 8px; margin: 5px 0;
    font-size: 13.5px; font-weight: 500;
}
.file-ok   { background: #EBF5EC; border-left: 3px solid #43A047; color: #1B5E20; }
.file-miss { background: #F7F7F7; border-left: 3px solid #CFCFCF; color: #888; }
.file-opt-ok   { background: #FFF9E6; border-left: 3px solid #F9A825; color: #5D4037; }
.file-opt-miss { background: #F7F7F7; border-left: 3px solid #CFCFCF; color: #AAA; }
.file-row .badge {
    font-size: 11px; padding: 1px 7px; border-radius: 10px;
    font-weight: 600; margin-left: auto; white-space: nowrap;
}
.badge-ok   { background: #C8E6C9; color: #1B5E20; }
.badge-miss { background: #EEEEEE; color: #999; }
.badge-opt  { background: #FFE082; color: #795548; }

/* ── 章节标签 ── */
.section-tag {
    font-size: 11px; font-weight: 700; letter-spacing: 1.2px;
    text-transform: uppercase; color: #5B7FA6;
    margin: 20px 0 8px 2px;
}

/* ── 结果分区卡片 ── */
.result-card {
    background: #F8FAFD; border: 1px solid #DDE6F0;
    border-radius: 12px; padding: 18px 22px; margin: 10px 0;
}

/* ── 侧栏应用名 ── */
.sidebar-brand {
    text-align: center; padding: 12px 0 8px;
}
.sidebar-brand .icon { font-size: 30px; line-height: 1; }
.sidebar-brand .name { font-size: 14px; font-weight: 700; color: #1F4E79; margin-top: 5px; }
.sidebar-brand .sub  { font-size: 11px; color: #888; margin-top: 2px; }

/* ── 侧栏分区标题 ── */
.sb-section {
    font-size: 11px; font-weight: 700; letter-spacing: 1px;
    text-transform: uppercase; color: #5B7FA6;
    margin: 6px 0 6px 2px;
}

/* ── 历史周次卡片 ── */
.week-card {
    background: white; border: 1px solid #E4ECF4;
    border-radius: 8px; padding: 8px 12px; margin: 4px 0;
    font-size: 12.5px;
}
.week-card .wnum { font-weight: 700; color: #1F4E79; }
.week-card .wdate { color: #888; font-size: 11px; }
.week-card .wstats { color: #444; margin-top: 2px; }
</style>
""", unsafe_allow_html=True)

# ─── SIDEBAR ─────────────────────────────────────────────────────────────────

with st.sidebar:
    # 品牌 header
    st.markdown("""
    <div class="sidebar-brand">
        <div class="icon">💅</div>
        <div class="name">NailVesta</div>
        <div class="sub">中台运营数据周报</div>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    # ── 本周设置 ──
    st.markdown('<div class="sb-section">📅 本周设置</div>', unsafe_allow_html=True)
    week_num   = st.number_input("周次（Week Number）", min_value=1, max_value=60, value=25, label_visibility="visible")
    c1, c2 = st.columns(2)
    date_start = c1.date_input("开始日期", value=datetime(2026, 6, 15), label_visibility="visible")
    date_end   = c2.date_input("结束日期",  value=datetime(2026, 6, 21), label_visibility="visible")
    date_range = f"{date_start.strftime('%Y/%m/%d')} – {date_end.strftime('%m/%d')}"
    st.divider()

    # ── 上传文件 ──
    st.markdown('<div class="sb-section">📂 上传本周数据</div>', unsafe_allow_html=True)
    f_order      = st.file_uploader("① 全量订单 CSV",          type=["csv"],        label_visibility="visible")
    f_cancelled  = st.file_uploader("② 取消订单报告 HTML",      type=["html","htm"], label_visibility="visible")
    f_returned   = st.file_uploader("③ 退货订单报告 HTML",      type=["html","htm"], label_visibility="visible")
    f_auction    = st.file_uploader("④ Auction 报告 HTML",      type=["html","htm"], label_visibility="visible")
    f_collection = st.file_uploader("⑤ Collection 报告 HTML",  type=["html","htm"], label_visibility="visible")
    st.divider()

    # ── WoW 对比 ──
    st.markdown('<div class="sb-section">🔁 WoW 对比周次</div>', unsafe_allow_html=True)
    saved_weeks = list_saved_weeks()

    if saved_weeks:
        default_prev = max([w for w in saved_weeks if w < week_num], default=saved_weeks[-1])
        default_idx  = saved_weeks.index(default_prev) if default_prev in saved_weeks else 0
        compare_week = st.selectbox(
            "选择对比周次",
            options=saved_weeks,
            index=default_idx,
            format_func=lambda w: f"W{w}  ({load_week_metrics(w).get('date_range','') if load_week_metrics(w) else ''})",
            label_visibility="collapsed",
        )
        prev_metrics = load_week_metrics(compare_week)
        if prev_metrics:
            st.success(f"✅ W{compare_week}（{prev_metrics.get('date_range','')}）")
        else:
            prev_metrics = None
    else:
        st.caption("暂无历史数据。本周生成后自动保存，下周可选。")
        compare_week = None
        prev_metrics = None

    st.divider()

    # ── 月均基准 ──
    st.markdown('<div class="sb-section">📆 月均基准对比（可选）</div>', unsafe_allow_html=True)

    MONTH_NAMES = {1:"一月",2:"二月",3:"三月",4:"四月",5:"五月",6:"六月",
                   7:"七月",8:"八月",9:"九月",10:"十月",11:"十一月",12:"十二月"}

    mb_mode = st.radio(
        "月均来源",
        ["不对比", "从历史周数据聚合", "上传整月 CSV"],
        index=0,
        label_visibility="collapsed",
    )

    monthly_baseline = None

    if mb_mode == "从历史周数据聚合":
        mb_col1, mb_col2 = st.columns(2)
        mb_year    = mb_col1.number_input("年份", value=2026, min_value=2020, max_value=2030, key="mb_year")
        mb_month_n = mb_col2.number_input("月份", value=5,    min_value=1,    max_value=12,   key="mb_month")
        existing = load_monthly(mb_year, mb_month_n)
        if existing:
            monthly_baseline = existing
            st.success(f"✅ {mb_year}年{MONTH_NAMES[mb_month_n]}月均已加载（{existing.get('num_weeks',0)}周）")
        else:
            avail_weeks = weeks_in_month(mb_year, mb_month_n)
            if avail_weeks:
                st.info(f"发现 {len(avail_weeks)} 个可用周 → 点击计算")
                if st.button("📊 计算并保存月均", key="calc_monthly"):
                    monthly_baseline = aggregate_from_weeks(avail_weeks)
                    if monthly_baseline:
                        save_monthly(mb_year, mb_month_n, monthly_baseline)
                        st.success(f"✅ 已保存 {mb_year}年{MONTH_NAMES[mb_month_n]}月均")
                    else:
                        st.error("聚合失败，请检查周数据")
            else:
                st.warning(f"暂无 {mb_year}/{mb_month_n} 月周数据")

    elif mb_mode == "上传整月 CSV":
        f_monthly_csv = st.file_uploader("整月订单 CSV", type=["csv"], key="monthly_csv")
        mb_col3, mb_col4 = st.columns(2)
        mb_year2    = mb_col3.number_input("年份", value=2026, min_value=2020, max_value=2030, key="mb_year2")
        mb_month_n2 = mb_col4.number_input("月份", value=5,    min_value=1,    max_value=12,   key="mb_month_n2")
        mb_ret_cnt  = st.number_input("该月退货行总数", value=0, min_value=0, key="mb_ret_cnt")
        if f_monthly_csv:
            existing_m = load_monthly(mb_year2, mb_month_n2)
            if existing_m and existing_m.get("source") == "full_month_csv":
                monthly_baseline = existing_m
                st.success(f"✅ {mb_year2}年{MONTH_NAMES[mb_month_n2]}月均已加载")
            else:
                if st.button("📊 处理并保存月均", key="calc_monthly_csv"):
                    with st.spinner("处理中..."):
                        monthly_baseline = aggregate_from_csv(f_monthly_csv, items_returned_manual=int(mb_ret_cnt))
                        monthly_baseline["source"] = "full_month_csv"
                        monthly_baseline["weekly_avg"] = {
                            k: monthly_baseline.get(k, 0) / 4.33
                            for k in ["effective_orders", "gmv", "cancelled_orders", "sku_sold"]
                        }
                        monthly_baseline["num_weeks"] = 4.33
                    save_monthly(mb_year2, mb_month_n2, monthly_baseline)
                    st.success(f"✅ 已保存月均")

    if monthly_baseline:
        with st.expander("月均指标预览", expanded=False):
            st.json({
                "有效订单/周均":  f"{monthly_baseline.get('weekly_avg',{}).get('effective_orders',0):,.0f}",
                "GMV/周均":       f"${monthly_baseline.get('weekly_avg',{}).get('gmv',0):,.0f}",
                "AOV":            f"${monthly_baseline.get('aov',0):.2f}" if monthly_baseline.get('aov') else "—",
                "Cancel Rate":    f"{monthly_baseline.get('cancel_rate',0)*100:.2f}%",
                "Return Rate":    f"{monthly_baseline.get('return_rate',0)*100:.2f}%",
            })

    st.divider()

    # ── 产品图册 ──
    st.markdown('<div class="sb-section">📁 产品图册（可选）</div>', unsafe_allow_html=True)
    st.caption("上传后自动生成新款表现 + 供应商分析两个章节")
    f_catalog = st.file_uploader("产品图册 CSV", type=["csv"], label_visibility="collapsed")

    # ── 历史数据汇总 ──
    if saved_weeks:
        st.divider()
        with st.expander(f"📊 历史数据（{len(saved_weeks)} 周）", expanded=False):
            for w in sorted(saved_weeks, reverse=True):
                wm = load_week_metrics(w)
                if wm:
                    cr = wm.get('cancel_rate', 0) * 100
                    rr = wm.get('return_rate', 0) * 100
                    st.markdown(
                        f'<div class="week-card">'
                        f'<span class="wnum">W{w}</span> '
                        f'<span class="wdate">{wm.get("date_range","")}</span>'
                        f'<div class="wstats">GMV ${wm.get("gmv",0):,.0f} &nbsp;·&nbsp; '
                        f'Cancel {cr:.2f}% &nbsp;·&nbsp; Return {rr:.2f}%</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

# ─── MAIN AREA ───────────────────────────────────────────────────────────────

# 标题卡片
st.markdown(f"""
<div class="page-header">
    <div class="app-name">NailVesta · 中台运营数据周报</div>
    <div class="week-title">W{week_num} 周报生成</div>
    <div class="date-sub">📅 {date_range}
    {"&nbsp;&nbsp;·&nbsp;&nbsp;对比 W" + str(compare_week) if compare_week else ""}
    {"&nbsp;&nbsp;·&nbsp;&nbsp;已加载月均基准" if monthly_baseline else ""}
    </div>
</div>
""", unsafe_allow_html=True)

# ─── 文件上传状态面板 ────────────────────────────────────────────────────────

required_files = [
    ("① 全量订单 CSV",         f_order,      "必填"),
    ("② 取消订单报告 HTML",    f_cancelled,  "必填"),
    ("③ 退货订单报告 HTML",    f_returned,   "必填"),
    ("④ Auction 报告 HTML",    f_auction,    "必填"),
    ("⑤ Collection 报告 HTML", f_collection, "必填"),
]
optional_files = [
    ("产品图册 CSV",  f_catalog, "可选·新款+供应商分析"),
]

uploaded_count = sum(1 for _, f, _ in required_files if f is not None)
all_ready = uploaded_count == len(required_files)

col_files, col_status = st.columns([3, 2])

with col_files:
    st.markdown('<div class="section-tag">上传文件状态</div>', unsafe_allow_html=True)
    html_rows = ""
    for name, f, tag in required_files:
        if f:
            html_rows += f'<div class="file-row file-ok">✅ {name}<span class="badge badge-ok">已上传</span></div>'
        else:
            html_rows += f'<div class="file-row file-miss">⬜ {name}<span class="badge badge-miss">待上传</span></div>'
    for name, f, tag in optional_files:
        if f:
            html_rows += f'<div class="file-row file-opt-ok">📋 {name}<span class="badge badge-opt">{tag} ✅</span></div>'
        else:
            html_rows += f'<div class="file-row file-opt-miss">📋 {name}<span class="badge badge-miss">{tag}</span></div>'
    st.markdown(html_rows, unsafe_allow_html=True)

with col_status:
    st.markdown('<div class="section-tag">就绪状态</div>', unsafe_allow_html=True)
    if all_ready:
        st.success(f"✅ 全部 {len(required_files)} 个必填文件已就绪")
        if compare_week and prev_metrics:
            st.info(f"🔁 WoW 对比：W{compare_week}（{prev_metrics.get('date_range','')}）")
        if f_catalog:
            st.info("📋 产品图册已加载，将生成新款 & 供应商分析")
        if monthly_baseline:
            st.info("📆 月均基准已加载")
    else:
        remaining = len(required_files) - uploaded_count
        st.warning(f"⬅️ 还需在左侧上传 {remaining} 个文件")
        with st.expander("📖 文件来源说明", expanded=False):
            st.markdown("""
| 文件 | 来源 |
|------|------|
| 全量订单 CSV | TikTok Seller Center → 数据 → 订单导出 |
| 取消订单 HTML | 取消分析报告页面「另存为」 |
| 退货订单 HTML | 退货分析报告页面「另存为」 |
| Auction HTML | Auction 专线报告「另存为」 |
| Collection HTML | Collection 综合分析「另存为」 |
| 产品图册 CSV | 内部维护，包含 SKU / 厂家 / 上架时间 |

> 历史 WoW 数据由程序自动保存，无需手动上传 JSON。
            """)
        st.stop()

st.markdown("")

# ─── 生成按钮 ────────────────────────────────────────────────────────────────

if st.button(
    f"🚀  生成 W{week_num} 周报",
    type="primary",
    use_container_width=True,
):
    progress = st.progress(0, "解析取消订单报告...")

    with st.spinner("解析取消订单报告..."):
        can_data = parse_cancelled(f_cancelled.read())
    progress.progress(15, "解析退货报告...")

    with st.spinner("解析退货报告..."):
        ret_data = parse_returned(f_returned.read())
    progress.progress(30, "解析 Auction 报告...")

    with st.spinner("解析 Auction 报告..."):
        auc_data = parse_auction(f_auction.read())
    progress.progress(45, "解析 Collection 报告...")

    with st.spinner("解析 Collection 报告..."):
        coll_data = parse_collection(f_collection.read())
    progress.progress(55, "处理订单 CSV...")

    with st.spinner("处理订单 CSV..."):
        items_returned = int(ret_data.get("total_rows") or 0) or len(ret_data.get("return_reasons", []))
        f_order.seek(0)
        order_m = process_orders(f_order, items_returned=items_returned)
        f_order.seek(0)
        b_chain_data = analyze_b_chain(f_order)
        f_order.seek(0)
        orders_raw_df = pd.read_csv(f_order)
    progress.progress(68, "加载产品图册...")

    catalog_df = pd.read_csv(f_catalog) if f_catalog else None
    new_styles_data = None
    supplier_data   = None
    if catalog_df is not None:
        ref_dt = datetime.combine(date_end, datetime.min.time())
        with st.spinner("分析新款表现..."):
            new_styles_data = analyze_new_styles(orders_raw_df, catalog_df, days=28, ref_date=ref_dt)
        with st.spinner("分析供应商表现..."):
            supplier_data = analyze_by_supplier(orders_raw_df, catalog_df)
    progress.progress(80, "生成 Word 文档...")

    with st.spinner("生成 Word 文档..."):
        docx_bytes = generate_report(
            week_num         = week_num,
            date_range       = date_range,
            order_metrics    = order_m,
            cancelled        = can_data,
            returned         = ret_data,
            auction          = auc_data,
            collection       = coll_data,
            prev_week        = prev_metrics,
            monthly_baseline = monthly_baseline,
            b_chain          = b_chain_data,
            catalog_df       = catalog_df,
            new_styles       = new_styles_data,
            supplier_data    = supplier_data,
        )
    progress.progress(92, "保存本周指标...")

    saved_path = save_week_metrics(week_num, order_m, date_range)
    progress.progress(100, "✅ 完成！")

    # ─── 生成成功提示 ────────────────────────────────────────────────────────
    st.success(f"✅ W{week_num} 周报生成完成！指标已自动保存 → `{saved_path.name}`")
    if compare_week:
        st.info(f"本次对比：W{compare_week}（{prev_metrics.get('date_range','')}）")

    st.markdown("---")

    # ─── 核心指标看板 ────────────────────────────────────────────────────────
    st.markdown('<div class="section-tag">📊 本周核心指标</div>', unsafe_allow_html=True)

    def _delta(curr, prev_key, invert=False, fmt=".2f"):
        if prev_metrics and prev_metrics.get(prev_key) is not None:
            d = curr - prev_metrics[prev_key]
            sign = "+" if d > 0 else ""
            return f"{sign}{d:{fmt}}", "inverse" if invert else "normal"
        return None, "normal"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("有效付费订单",
              f"{order_m['effective_orders']:,}",
              delta=_delta(order_m['effective_orders'], 'effective_orders')[0],
              delta_color=_delta(order_m['effective_orders'], 'effective_orders')[1])
    c2.metric("GMV（不含运费）",
              f"${order_m['gmv']:,.0f}",
              delta=_delta(order_m['gmv'], 'gmv', fmt=",.0f")[0])
    c3.metric("AOV（不含运费）",
              f"${order_m['aov']:.2f}" if order_m['aov'] else "—",
              delta=_delta(order_m['aov'], 'aov')[0] if order_m['aov'] else None)
    c4.metric("Cancel Rate（SKU口径）",
              f"{order_m['cancel_rate']*100:.2f}%",
              delta=f"{(order_m['cancel_rate'] - prev_metrics['cancel_rate'])*100:+.2f}pp"
                    if prev_metrics and prev_metrics.get('cancel_rate') is not None else None,
              delta_color="inverse")
    c5.metric("Return Rate（SKU口径）",
              f"{order_m['return_rate']*100:.2f}%",
              delta=f"{(order_m['return_rate'] - prev_metrics['return_rate'])*100:+.2f}pp"
                    if prev_metrics and prev_metrics.get('return_rate') is not None else None,
              delta_color="inverse")

    # ─── 订单结构 & 件数 ─────────────────────────────────────────────────────
    st.markdown('<div class="section-tag">📋 订单口径明细</div>', unsafe_allow_html=True)
    col_a, col_b = st.columns(2)

    with col_a:
        st.dataframe(pd.DataFrame({
            "分层": ["全量订单", "0元达人单", "Cancelled 订单数", "有效付费订单",
                     "Items Sold（Cancel/Return 分母）"],
            "本周": [order_m["total_orders"], order_m["zero_orders"],
                     order_m["cancelled_orders"], order_m["effective_orders"],
                     order_m["items_sold"]],
            f"W{compare_week}" if compare_week else "上周": [
                prev_metrics.get("total_orders", "—") if prev_metrics else "—",
                prev_metrics.get("zero_orders", "—")   if prev_metrics else "—",
                prev_metrics.get("cancelled_orders","—") if prev_metrics else "—",
                prev_metrics.get("effective_orders","—") if prev_metrics else "—",
                prev_metrics.get("items_sold", "—")     if prev_metrics else "—",
            ],
        }), use_container_width=True)

    with col_b:
        cr_formula = f"{order_m['items_canceled']} ÷ {order_m['items_sold']}"
        rr_formula = f"({order_m['items_canceled']}+{order_m['items_returned']}) ÷ {order_m['items_sold']}"
        st.dataframe(pd.DataFrame({
            "指标": ["Items Sold", "Items Canceled", "Items Returned",
                     "Cancel Rate", "Return Rate"],
            "本周": [
                f"{order_m['items_sold']:,}",
                f"{order_m['items_canceled']:,}",
                f"{order_m['items_returned']:,}",
                f"{order_m['cancel_rate']*100:.2f}%",
                f"{order_m['return_rate']*100:.2f}%",
            ],
            "公式/说明": [
                "付费订单 Quantity 合计（含取消件）",
                "取消订单 Quantity 合计",
                "退货报告行总数（每行=1件）",
                cr_formula,
                rr_formula,
            ],
        }), use_container_width=True)

    # ─── 件数结构 ────────────────────────────────────────────────────────────
    st.markdown('<div class="section-tag">📦 件数结构</div>', unsafe_allow_html=True)
    qty_rows = []
    for label, d in order_m["qty_bands"].items():
        prev_d = prev_metrics.get("qty_bands", {}).get(label, {}) if prev_metrics else {}
        qty_rows.append({
            "件数":       label,
            "本周订单数": f"{d['count']:,}",
            "本周占比":   f"{d['pct']*100:.1f}%",
            "本周 AOV":   f"${d['aov']:.2f}" if d['aov'] else "—",
            f"W{compare_week} 占比" if compare_week else "上周占比":
                f"{prev_d.get('pct',0)*100:.1f}%" if prev_d else "—",
            f"W{compare_week} AOV" if compare_week else "上周 AOV":
                f"${prev_d.get('aov',0):.2f}" if prev_d.get('aov') else "—",
        })
    st.dataframe(pd.DataFrame(qty_rows), use_container_width=True)

    # ─── 月均对比（如有）────────────────────────────────────────────────────
    if monthly_baseline:
        st.markdown('<div class="section-tag">📅 月均对比</div>', unsafe_allow_html=True)
        mbl     = monthly_baseline
        mb_cr   = mbl.get("cancel_rate", 0) or 0
        mb_rr   = mbl.get("return_rate", 0) or 0
        mb_aov  = mbl.get("aov", 0) or 0
        mb_gmv_w = mbl.get("weekly_avg", {}).get("gmv", 0) or 0
        src_label = mbl.get("source", "")
        week_src  = str(mbl.get("weeks_included", "")) if src_label == "aggregated_from_weeks" else "整月CSV"
        st.caption(f"月均来源：{week_src}")
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("月均 Cancel Rate", f"{mb_cr*100:.2f}%",
                   delta=f"{(order_m['cancel_rate']-mb_cr)*100:+.2f}pp", delta_color="inverse")
        mc2.metric("月均 Return Rate", f"{mb_rr*100:.2f}%",
                   delta=f"{(order_m['return_rate']-mb_rr)*100:+.2f}pp", delta_color="inverse")
        mc3.metric("月均 AOV", f"${mb_aov:.2f}",
                   delta=f"{order_m['aov']-mb_aov:+.2f}" if order_m['aov'] else None)
        mc4.metric("月均 GMV/周", f"${mb_gmv_w:,.0f}",
                   delta=f"${order_m['gmv']-mb_gmv_w:+,.0f}")

    # ─── 下载按钮 ────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-tag">📥 下载报告</div>', unsafe_allow_html=True)
    st.download_button(
        label               = f"📄  下载 W{week_num} 周报（Word .docx）",
        data                = docx_bytes,
        file_name           = f"NailVesta_W{week_num}_运营数据周报.docx",
        mime                = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        use_container_width = True,
        type                = "primary",
    )
    st.caption(f"本周指标已自动保存，下周在左侧选择「对比 W{week_num}」即可直接使用。")

    # ─── 历史周次表 ──────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-tag">📅 历史已保存周次</div>', unsafe_allow_html=True)
    all_saved = list_saved_weeks()
    if all_saved:
        rows = []
        for w in sorted(all_saved, reverse=True):
            wm = load_week_metrics(w)
            if wm:
                rows.append({
                    "周次":        f"W{w}",
                    "日期":        wm.get("date_range", ""),
                    "有效订单":    f"{wm.get('effective_orders',0):,}",
                    "GMV":         f"${wm.get('gmv',0):,.0f}",
                    "AOV":         f"${wm.get('aov',0):.2f}",
                    "Cancel Rate": f"{wm.get('cancel_rate',0)*100:.2f}%",
                    "Return Rate": f"{wm.get('return_rate',0)*100:.2f}%",
                    "保存时间":    wm.get("saved_at","")[:16],
                })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ─── FOOTER ──────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    "NailVesta 中台运营数据周报 · "
    "有效订单 = 全量 − 0元单 − Cancelled · "
    "Cancel Rate = SKU Canceled ÷ Items Sold · "
    "Return Rate = (SKU Canceled + SKU Returned) ÷ Items Sold · "
    f"数据存储：{METRICS_DIR.resolve()}"
)
