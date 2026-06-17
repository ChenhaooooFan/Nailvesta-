# NailVesta 中台运营周报生成器

## 文件结构

```
NailVesta_Reporter/
├── app.py                  主程序（Streamlit UI）
├── parsers.py              HTML 报告解析
├── order_processor.py      订单 CSV 指标计算
├── report_generator.py     Word 文档生成
├── monthly.py              月均基准计算与存储
├── requirements.txt        Python 依赖
├── .streamlit/
│   └── config.toml         主题与服务配置
└── data/                   历史指标自动保存（勿删）
    ├── W24_metrics.json
    ├── W25_metrics.json
    └── 2026_05_monthly.json
```

---

## 本地运行（推荐）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
streamlit run app.py

# 浏览器自动打开 http://localhost:8501
```

> **data/ 文件夹**会自动在本地积累每周指标，无需手动备份。

---

## Streamlit Cloud 部署

1. 把整个文件夹（包含 `data/`）推送到 GitHub 私有仓库
2. 登录 [share.streamlit.io](https://share.streamlit.io) → New app → 选择仓库 → Main file: `app.py`
3. 点击 Deploy

**⚠️ 重要**：Streamlit Cloud 的容器会周期性重启，`data/` 里的 JSON 文件会丢失。
解决方法：每次生成报告后，把新增的 `data/W{N}_metrics.json` 手动 commit 回 GitHub 仓库（每周一次，30秒内完成）。

---

## 每周操作流程

| 步骤 | 操作 |
|------|------|
| 1 | TikTok Seller Center 导出本周全量订单 CSV |
| 2 | 4 份 HTML 报告另存为（取消、退货、Auction、Collection） |
| 3 | 打开 Streamlit 程序，上传 5 个文件 |
| 4 | 左侧选择对比周次（自动读取上周） |
| 5 | （可选）选择月均对比模式 |
| 6 | 点击「生成周报」→ 下载 Word 文档 |
| 7 | 本周指标已自动保存，无需其他操作 |

---

## 数据口径（内置）

- **有效付费订单** = 全量 − 0元达人单 − Cancelled
- **Cancel Rate** 分母 = 全量 − 0元单（付费含取消基数）
- **Return Rate** = (Items Canceled + Items Returned) ÷ Items Sold
  - Items = SKU 件数（行级别，一行 = 一件）
- **GMV** = SKU Subtotal After Discount（折后，不含运费）
