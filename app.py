"""
app.py — NailVesta 中台运营周报生成器
Run with: streamlit run app.py

历史指标自动保存在 ./data/W{N}_metrics.json，无需手动上传 JSON。
"""

import streamlit as st
import pandas as pd
import json
import io
from pathlib import Path
from datetime import datetime

from parsers import parse_cancelled, parse_returned, parse_auction, parse_collection
from order_processor import process_orders
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
    """Persist this week's metrics to data/W{N}_metrics.json."""
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
    """Return sorted list of week numbers that have saved metrics."""
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
    page_title="NailVesta 周报生成器",
    page_icon="💅",
    layout="wide",
)

# ─── SIDEBAR ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 💅 NailVesta 周报生成器")
    st.markdown("---")

    st.subheader("📅 本周设置")
    week_num   = st.number_input("本周周次（Week Number）", min_value=1, max_value=60, value=25)
    date_start = st.date_input("本周开始日期", value=datetime(2026, 6, 15))
    date_end   = st.date_input("本周结束日期", value=datetime(2026, 6, 21))
    date_range = f"{date_start.strftime('%Y/%m/%d')} – {date_end.strftime('%m/%d')}"

    st.markdown("---")
    st.subheader("📂 上传本周数据（必填）")
    f_order      = st.file_uploader("① 全量订单 CSV", type=["csv"])
    f_cancelled  = st.file_uploader("② 取消订单报告 HTML", type=["html","htm"])
    f_returned   = st.file_uploader("③ 退货订单报告 HTML", type=["html","htm"])
    f_auction    = st.file_uploader("④ Auction 报告 HTML", type=["html","htm"])
    f_collection = st.file_uploader("⑤ Collection 报告 HTML", type=["html","htm"])

    st.markdown("---")
    st.subheader("🔁 WoW 对比周次")

    saved_weeks = list_saved_weeks()

    if saved_weeks:
        # Default: the most recent saved week before current week_num
        default_prev = max([w for w in saved_weeks if w < week_num], default=saved_weeks[-1])
        default_idx  = saved_weeks.index(default_prev) if default_prev in saved_weeks else 0

        compare_week = st.selectbox(
            "选择对比周次",
            options=saved_weeks,
            index=default_idx,
            format_func=lambda w: f"W{w}  ({load_week_metrics(w).get('date_range','') if load_week_metrics(w) else ''})",
            help="从历史已保存的周次中选择，程序自动加载对应指标做 WoW 对比"
        )
        prev_metrics = load_week_metrics(compare_week)
        if prev_metrics:
            st.success(f"✅ 已加载 W{compare_week} 指标（{prev_metrics.get('date_range','')}）")
        else:
            prev_metrics = None
    else:
        st.info("暂无历史数据。本周生成后将自动保存，下周可直接选择对比。")
        compare_week = None
        prev_metrics = None

    st.markdown("---")
    st.subheader("📆 月均基准对比（可选）")

    MONTH_NAMES = {1:"一月",2:"二月",3:"三月",4:"四月",5:"五月",6:"六月",
                   7:"七月",8:"八月",9:"九月",10:"十月",11:"十一月",12:"十二月"}

    mb_mode = st.radio(
        "月均来源",
        ["不对比月均", "自动从历史周数据聚合", "上传整月订单 CSV"],
        index=0,
        help="选择如何获取上月月均基准用于周报对比"
    )

    monthly_baseline = None

    if mb_mode == "自动从历史周数据聚合":
        saved_months = list_saved_months()
        mb_col1, mb_col2 = st.columns(2)
        mb_year  = mb_col1.number_input("年份", value=2026, min_value=2020, max_value=2030, key="mb_year")
        mb_month_n = mb_col2.number_input("月份", value=5, min_value=1, max_value=12, key="mb_month")

        # Check if already saved
        existing = load_monthly(mb_year, mb_month_n)
        if existing:
            monthly_baseline = existing
            weeks_used = existing.get("weeks_included", [])
            st.success(f"✅ 已加载 {mb_year}年{MONTH_NAMES[mb_month_n]}月均（W{weeks_used} 聚合，{existing.get('num_weeks',0)}周）")
        else:
            avail_weeks = weeks_in_month(mb_year, mb_month_n)
            if avail_weeks:
                st.info(f"发现 {len(avail_weeks)} 个可用周：W{avail_weeks} → 点击计算")
                if st.button("📊 计算并保存月均", key="calc_monthly"):
                    monthly_baseline = aggregate_from_weeks(avail_weeks)
                    if monthly_baseline:
                        save_monthly(mb_year, mb_month_n, monthly_baseline)
                        st.success(f"✅ 已计算并保存 {mb_year}年{MONTH_NAMES[mb_month_n]}月均（{len(avail_weeks)}周）")
                    else:
                        st.error("聚合失败，请检查周数据")
            else:
                st.warning(f"data/ 中暂无 {mb_year}年{mb_month_n}月 的周数据。请先跑那几周的报告，或改用「上传整月 CSV」。")

    elif mb_mode == "上传整月订单 CSV":
        f_monthly_csv = st.file_uploader("上传整月订单 CSV（格式同全量订单表）", type=["csv"], key="monthly_csv")
        mb_col3, mb_col4 = st.columns(2)
        mb_year2   = mb_col3.number_input("这个 CSV 是哪一年？", value=2026, min_value=2020, max_value=2030, key="mb_year2")
        mb_month_n2 = mb_col4.number_input("哪一个月？", value=5, min_value=1, max_value=12, key="mb_month_n2")
        mb_ret_cnt = st.number_input("该月退货行总数（来自退货报告，可填 0 后续手动更新）", value=0, min_value=0, key="mb_ret_cnt")

        if f_monthly_csv:
            existing_m = load_monthly(mb_year2, mb_month_n2)
            if existing_m and existing_m.get("source") == "full_month_csv":
                monthly_baseline = existing_m
                st.success(f"✅ 已加载保存的 {mb_year2}年{MONTH_NAMES[mb_month_n2]}月 CSV 月均")
            else:
                if st.button("📊 处理整月 CSV 并保存月均", key="calc_monthly_csv"):
                    with st.spinner("处理整月订单 CSV..."):
                        monthly_baseline = aggregate_from_csv(f_monthly_csv, items_returned_manual=int(mb_ret_cnt))
                        monthly_baseline["source"] = "full_month_csv"
                        # Compute weekly averages: assume month ≈ 4.33 weeks
                        monthly_baseline["weekly_avg"] = {
                            k: monthly_baseline.get(k,0)/4.33
                            for k in ["effective_orders","gmv","cancelled_orders","sku_sold"]
                        }
                        monthly_baseline["num_weeks"] = 4.33
                    save_monthly(mb_year2, mb_month_n2, monthly_baseline)
                    st.success(f"✅ 已处理并保存 {mb_year2}年{MONTH_NAMES[mb_month_n2]}月均")

    if monthly_baseline:
        with st.expander("月均指标预览"):
            st.json({
                "有效订单/月":    f"{monthly_baseline.get('effective_orders',0):,.0f}",
                "有效订单/周均":  f"{monthly_baseline.get('weekly_avg',{}).get('effective_orders',0):,.0f}",
                "GMV/月":         f"${monthly_baseline.get('gmv',0):,.0f}",
                "GMV/周均":       f"${monthly_baseline.get('weekly_avg',{}).get('gmv',0):,.0f}",
                "AOV":            f"${monthly_baseline.get('aov',0):.2f}" if monthly_baseline.get('aov') else "—",
                "Cancel Rate":    f"{monthly_baseline.get('cancel_rate',0)*100:.2f}%",
                "Return Rate":    f"{monthly_baseline.get('return_rate',0)*100:.2f}%",
                "数据来源":       monthly_baseline.get("source",""),
                "周次":           str(monthly_baseline.get("weeks_included",[])),
            })

    st.markdown("---")
    st.subheader("📁 选填文件")
    f_catalog = st.file_uploader("产品图册 CSV（款式名称映射）", type=["csv"])

    st.markdown("---")
    # Show all saved weeks summary
    if saved_weeks:
        with st.expander(f"📊 已保存 {len(saved_weeks)} 周历史数据", expanded=False):
            for w in sorted(saved_weeks, reverse=True):
                wm = load_week_metrics(w)
                if wm:
                    st.markdown(
                        f"**W{w}** {wm.get('date_range','')}  \n"
                        f"GMV ${wm.get('gmv',0):,.0f} · "
                        f"Cancel {wm.get('cancel_rate',0)*100:.2f}% · "
                        f"Return {wm.get('return_rate',0)*100:.2f}%"
                    )

# ─── MAIN AREA ───────────────────────────────────────────────────────────────

st.markdown(f"# 📋 W{week_num} 周报生成  ·  {date_range}")

with st.expander("📖 每周需要上传哪些文件？", expanded=False):
    st.markdown("""
| # | 文件 | 来源 | 必须 |
|---|------|------|------|
| ① | 全量订单 CSV | TikTok Seller Center → 数据 → 订单导出 | ✅ |
| ② | 取消订单报告 HTML | 取消分析报告页面「另存为」 | ✅ |
| ③ | 退货订单报告 HTML | 退货分析报告页面「另存为」 | ✅ |
| ④ | Auction 报告 HTML | Auction 专线报告「另存为」 | ✅ |
| ⑤ | Collection 报告 HTML | Collection 综合分析「另存为」 | ✅ |
| — | **上周对比 JSON** | **程序自动保存，左侧下拉选即可，无需手动上传** | ⭐ |
| 产品图册 CSV | 内部维护 | SKU→款式名称映射 | 可选 |

> 不再需要手动下载/上传 JSON。每次生成后，本周指标自动写入 `data/W{N}_metrics.json`，下周直接从左侧下拉选择对比周次。
    """)

# ─── REQUIRED FILE CHECK ─────────────────────────────────────────────────────

required = {
    "全量订单 CSV": f_order,
    "取消订单报告 HTML": f_cancelled,
    "退货订单报告 HTML": f_returned,
    "Auction 报告 HTML": f_auction,
    "Collection 报告 HTML": f_collection,
}
missing = [name for name, f in required.items() if f is None]

if missing:
    st.warning(f"⬅️ 请在左侧上传以下必填文件：**{'、'.join(missing)}**")
    if prev_metrics:
        st.info(f"已选定对比周次：W{compare_week}，上传文件后点击「生成周报」即可")
    st.stop()

# ─── GENERATE ────────────────────────────────────────────────────────────────

if st.button("🚀 生成 W{} 周报".format(week_num), type="primary", use_container_width=True):

    progress = st.progress(0, "解析取消订单报告...")

    with st.spinner("解析取消订单报告..."):
        can_data = parse_cancelled(f_cancelled.read())
    progress.progress(20, "解析退货报告...")

    with st.spinner("解析退货报告..."):
        ret_data = parse_returned(f_returned.read())
    progress.progress(40, "解析 Auction...")

    with st.spinner("解析 Auction 报告..."):
        auc_data = parse_auction(f_auction.read())
    progress.progress(55, "解析 Collection...")

    with st.spinner("解析 Collection 报告..."):
        coll_data = parse_collection(f_collection.read())
    progress.progress(65, "处理订单 CSV...")

    with st.spinner("处理订单 CSV..."):
        items_returned = int(ret_data.get("total_rows") or 0) or len(ret_data.get("return_reasons", []))
        order_m = process_orders(f_order, items_returned=items_returned)
    progress.progress(80, "生成报告文档...")

    catalog_df = pd.read_csv(f_catalog) if f_catalog else None

    with st.spinner("生成 Word 文档..."):
        docx_bytes = generate_report(
            week_num          = week_num,
            date_range        = date_range,
            order_metrics     = order_m,
            cancelled         = can_data,
            returned          = ret_data,
            auction           = auc_data,
            collection        = coll_data,
            prev_week         = prev_metrics,
            monthly_baseline  = monthly_baseline,
            catalog_df        = catalog_df,
        )
    progress.progress(92, "保存本周指标...")

    # ── AUTO-SAVE THIS WEEK'S METRICS ────────────────────────────────────
    saved_path = save_week_metrics(week_num, order_m, date_range)
    progress.progress(100, "完成！")

    st.success(f"✅ W{week_num} 周报生成完成！本周指标已自动保存至 `{saved_path.name}`")
    if compare_week:
        st.info(f"本次对比周次：W{compare_week}（{prev_metrics.get('date_range','')}）")

    # ─── KEY METRICS DASHBOARD ────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f"### 📊 W{week_num} 关键指标")

    col1, col2, col3, col4, col5 = st.columns(5)

    def _delta_str(curr, prev_key, multiply=1, invert=False, fmt=".2f"):
        if prev_metrics and prev_metrics.get(prev_key) is not None:
            d = (curr - prev_metrics[prev_key]) * multiply
            sign = "+" if d > 0 else ""
            return f"{sign}{d:{fmt}}", "inverse" if invert else "normal"
        return None, "normal"

    col1.metric("有效付费订单",
                f"{order_m['effective_orders']:,}",
                delta=_delta_str(order_m['effective_orders'], 'effective_orders', invert=False)[0],
                delta_color=_delta_str(order_m['effective_orders'], 'effective_orders')[1])
    col2.metric("GMV（不含运费）",
                f"${order_m['gmv']:,.0f}",
                delta=_delta_str(order_m['gmv'], 'gmv', fmt=",.0f")[0])
    col3.metric("AOV（不含运费）",
                f"${order_m['aov']:.2f}" if order_m['aov'] else "—",
                delta=_delta_str(order_m['aov'], 'aov')[0] if order_m['aov'] else None)
    col4.metric("Cancel Rate（付费口径）",
                f"{order_m['cancel_rate']*100:.2f}%",
                delta=f"{(order_m['cancel_rate'] - prev_metrics['cancel_rate'])*100:+.2f}pp" if prev_metrics and prev_metrics.get('cancel_rate') else None,
                delta_color="inverse")
    col5.metric("Return Rate（NailVesta 口径）",
                f"{order_m['return_rate']*100:.2f}%",
                delta=f"{(order_m['return_rate'] - prev_metrics['return_rate'])*100:+.2f}pp" if prev_metrics and prev_metrics.get('return_rate') else None,
                delta_color="inverse")

    # Order structure breakdown
    st.markdown("#### 订单口径明细")
    col_a, col_b = st.columns(2)
    with col_a:
        st.dataframe(pd.DataFrame({
            "分层":  ["全量订单", "0元达人单", "Cancelled", "有效付费订单", "付费基数（Cancel Rate 分母）"],
            "本周":  [order_m["total_orders"], order_m["zero_orders"], order_m["cancelled_orders"], order_m["effective_orders"], order_m["paid_base"]],
            "对比 W{}".format(compare_week if compare_week else "—"): [
                prev_metrics.get("total_orders","—") if prev_metrics else "—",
                prev_metrics.get("zero_orders","—") if prev_metrics else "—",
                prev_metrics.get("cancelled_orders","—") if prev_metrics else "—",
                prev_metrics.get("effective_orders","—") if prev_metrics else "—",
                prev_metrics.get("paid_base","—") if prev_metrics else "—",
            ],
        }), use_container_width=True)

    with col_b:
        st.dataframe(pd.DataFrame({
            "项目":  ["Items Sold", "Items Canceled", "Items Returned", "Return Rate"],
            "本周":  [
                f"{order_m['items_sold']:,}",
                f"{order_m['items_canceled']:,}",
                f"{order_m['items_returned']:,}",
                f"{order_m['return_rate']*100:.2f}%",
            ],
            "口径说明": [
                "所有付费订单 Quantity 合计（含取消件）",
                "取消订单 Quantity 合计",
                "退货报告行总数（每行=1件）",
                f"({order_m['items_canceled']}+{order_m['items_returned']}) ÷ {order_m['items_sold']}",
            ],
        }), use_container_width=True)

    # GMV & 件数 quick view
    st.markdown("#### 件数结构")
    qty_rows = []
    for label, d in order_m["qty_bands"].items():
        prev_d = prev_metrics.get("qty_bands", {}).get(label, {}) if prev_metrics else {}
        qty_rows.append({
            "件数":    label,
            "本周订单数": f"{d['count']:,}",
            "本周占比": f"{d['pct']*100:.1f}%",
            "本周 AOV": f"${d['aov']:.2f}" if d['aov'] else "—",
            f"W{compare_week} 占比" if compare_week else "上周占比": f"{prev_d.get('pct',0)*100:.1f}%" if prev_d else "—",
            f"W{compare_week} AOV" if compare_week else "上周 AOV": f"${prev_d.get('aov',0):.2f}" if prev_d.get('aov') else "—",
        })
    st.dataframe(pd.DataFrame(qty_rows), use_container_width=True)

    # ─── DOWNLOAD ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📥 下载")
    st.download_button(
        label     = f"📄 下载 W{week_num} 周报（Word）",
        data      = docx_bytes,
        file_name = f"NailVesta_W{week_num}_周报_v1.docx",
        mime      = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        use_container_width = True,
        type = "primary",
    )
    st.caption(f"本周指标已自动保存，下周在左侧选择「对比 W{week_num}」即可使用。")
    if monthly_baseline:
        mbl = monthly_baseline
        mb_cr = mbl.get("cancel_rate", 0) or 0
        mb_rr = mbl.get("return_rate", 0) or 0
        mb_aov = mbl.get("aov", 0) or 0
        mb_gmv_w = mbl.get("weekly_avg",{}).get("gmv", 0) or 0
        src_label = mbl.get("source","")
        week_src  = str(mbl.get("weeks_included","")) if src_label=="aggregated_from_weeks" else "整月CSV"
        st.markdown(f"#### 📅 月均对比（{mb_month if 'mb_month' in dir() else '上月'}，来源：{week_src}）")
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("月均 Cancel Rate", f"{mb_cr*100:.2f}%", delta=f"{(order_m['cancel_rate']-mb_cr)*100:+.2f}pp（本周 vs 月均）", delta_color="inverse")
        mc2.metric("月均 Return Rate", f"{mb_rr*100:.2f}%", delta=f"{(order_m['return_rate']-mb_rr)*100:+.2f}pp", delta_color="inverse")
        mc3.metric("月均 AOV", f"${mb_aov:.2f}", delta=f"{order_m['aov']-mb_aov:+.2f}（本周 AOV 差）")
        mc4.metric("月均 GMV/周", f"${mb_gmv_w:,.0f}", delta=f"${order_m['gmv']-mb_gmv_w:+,.0f}（本周 GMV 差）")

    # ─── WEEK HISTORY TABLE ───────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📅 所有已保存周次")
    all_saved = list_saved_weeks()
    if all_saved:
        rows = []
        for w in sorted(all_saved, reverse=True):
            wm = load_week_metrics(w)
            if wm:
                rows.append({
                    "周次": f"W{w}",
                    "日期": wm.get("date_range",""),
                    "有效订单": f"{wm.get('effective_orders',0):,}",
                    "GMV": f"${wm.get('gmv',0):,.0f}",
                    "AOV": f"${wm.get('aov',0):.2f}",
                    "Cancel Rate": f"{wm.get('cancel_rate',0)*100:.2f}%",
                    "Return Rate": f"{wm.get('return_rate',0)*100:.2f}%",
                    "保存时间": wm.get("saved_at","")[:16],
                })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

# ─── FOOTER ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "NailVesta 中台运营周报生成器 · "
    "有效订单 = 全量 − 0元单 − Cancelled · "
    "Return Rate = (Items Canceled + Items Returned) ÷ Items Sold · "
    f"历史数据存储：{METRICS_DIR.resolve()}"
)
