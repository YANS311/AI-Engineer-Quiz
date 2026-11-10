# SUBMISSION — AI 应用开发工程师测评（素材检索与结果导出）

## 1. 运行与验证步骤

### 环境准备与启动

推荐使用 Python 3.10+（或 `uv`）：

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'

export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false

# 生成可复现的 24 个演示素材与 3 个集合（含 harbor 业务场景、相同 created_at 及特殊字符测试数据）
uv run --no-sync python -m keepstack seed

# 启动服务
uv run --no-sync python -m keepstack
```

浏览器访问 <http://localhost:8000>，使用默认账号 `admin` / `admin` 登录。

### 业务场景手工验证步骤

1. **组合筛选**：
   - 在顶部搜索框输入 `harbor`；
   - 在筛选栏 `Collection` 下拉框选择 `Environment & Survey`；
   - 在 `Include tags` 中依次选择/输入 `approved` 和 `campaign`；
   - 在 `Exclude tags` 中选择/输入 `internal`；
   - 页面上方 `Active filters` 区域展示所有生效条件（支持逐项点击 `×` 移除或点击 `Clear all` 一键清空），结果列表返回 3 条匹配素材（排除了同集合中带 `internal` 标签的 `Coastal survey photo (alt)`、缺少 `campaign` 标签的 `River watershed diagram (alt)`，以及位于其他集合中的 `Public transit poster`）。
2. **排序与稳定分页**：
   - 在右上角 `Sort` 切换 `Created: Newest first`（降序）与 `Created: Oldest first`（升序），即使存在关键词 `harbor`，结果仍严格按 `created_at` 排序；
   - 清空关键词并仅保留 `Include: #approved`（共 19 条匹配素材），页面底部显示 `Total: 19 matching assets`、`Page 1 of 2`，点击 `Next` / `Previous` 翻页；其中多条素材具有完全相同的 `created_at`（`2026-07-10T12:00:00+00:00`），翻页结果无重复、无遗漏。
3. **CSV 全量导出**：
   - 点击右上角 `Export CSV` 按钮，下载 `assets_export.csv`；
   - 导出内容不受当前页 `limit` 限制，包含全部匹配素材，顺序与当前列表排序完全一致；
   - 包含 `Coastal survey photo` 中的中文、英文逗号、双引号及换行符，均按 RFC 4180 标准转义。
4. **修改后结果实时更新**：
   - 点击任一素材卡片打开详情抽屉，修改 `Title`、`Description` 或增删 `Tags` 并保存，列表查询与再次导出的 CSV 立即反映最新内容。

### 自动化测试执行

```bash
python -m pytest -q
```

---

## 2. 实测验证结果

1. **原始测试基线**：
   - 修改前执行 `pytest -q`：`22 passed, 2 skipped, 2 warnings`（2 个 skipped 为可选的本地 CLIP 视觉模型与 Tesseract OCR 二进制依赖测试）。
2. **发现并修复的上游缺陷**：
   - **SQL 占位符顺序错位缺陷（`keepstack/search.py`）**：原实现将 `collection_id` 与 `tag` 的 `?` 占位符拼接在 `JOIN` 子句中（位于 `WHERE a.status = ?` 之前），但参数列表 `params` 以 `[status]` 开头，导致传入 `tag` 或 `collection_id` 时参数绑定错位，查询结果恒为空。已将所有带参条件统一收敛至 `WHERE` 子句中按顺序绑定。
   - **有关键词时忽略排序选择（`keepstack/search.py`）**：原实现当 `q.strip()` 非空时强制使用 `ORDER BY bm25(asset_fts)` 覆盖用户所选排序。已修复为始终遵守所选排序。
   - **相同 `created_at` 分页不稳定（`keepstack/search.py`）**：原排序仅使用 `a.created_at DESC/ASC`，当多条记录时间戳相同时跨页顺序不确定。已增加唯一主键决胜列 `a.created_at DESC, a.id DESC` 与 `a.created_at ASC, a.id ASC`。
   - **纯特殊字符关键词导致 FTS5 语法错误（`keepstack/search.py`）**：当输入如 `!!!` 或 `"---"` 等仅含非字母数字字符的非空查询时，`_fts_query(q)` 返回空字符串 `""`，传入 `asset_fts MATCH ''` 会触发 `sqlite3.OperationalError: fts5: syntax error`。已修复为空白输入不限制关键词、无有效分词的非空白特殊字符输入安全返回空结果。
   - **详情抽屉增删标签后未刷新列表（`web/app.js`）**：原前端在添加或删除标签后仅刷新详情抽屉，未触发列表与筛选项更新。已在保存元数据及增删标签后同步刷新列表和筛选项。
3. **新增自动化测试结果（`tests/test_search_export.py`）**：
   - 新增 6 个综合测试函数，覆盖：
     - `test_seed_reproducible_and_business_scenario`：验证可复现 Seed 数据及 README `harbor` + 集合 + 包含/排除标签 + 升降序 + CSV 导出全流程；
     - `test_combined_filtering_semantics`：验证关键词、集合、多包含标签（AND）、多排除标签（OR 排除且保留无标签素材）、大小写不敏感、完整标签名匹配、重复标签去重、包含与排除同标签冲突返回空、空白与特殊字符输入、`media_type` 与 `active` 状态过滤；
     - `test_sorting_and_pagination_stability_with_identical_created_at`：验证 7 条相同 `created_at` 素材在有关键词下的升降序多页遍历无重复、无遗漏，以及非法分页参数（`limit<=0`、`offset<0`、非数字）的 422 校验；
     - `test_csv_export_full_results_escaping_and_empty_header`：验证登录鉴权（未登录 401）、跨页全量导出一致性、0 条结果保留表头、中文/逗号/双引号/换行符 RFC 4180 转义；
     - `test_updates_to_title_description_and_tags_reflect_in_search_and_export`：验证通过 `PATCH /api/assets/{uuid}`、`POST /api/assets/{uuid}/tags`、`DELETE /api/assets/{uuid}/tags/{name}` 修改后，下一次搜索与 CSV 导出立即反映最新值；
     - `test_multipage_seed_pagination_matches_csv_export_and_query_formats`：验证 Seed 数据集多页拼接结果与 CSV 导出完全一致。
   - 最终执行 `pytest -q`：**`28 passed, 2 skipped`**，全部通过。

---

## 3. 主要设计决策与取舍

1. **CSV 多标签字段格式**：
   - 表头固定为：`uuid,title,description,filename,media_type,tags,created_at`。
   - **多标签格式**：同一素材的多个标签按字典序升序排列，并以英文逗号 `,` 连接（例如 `approved,campaign`）。当存在多个标签时，Python 标准库 `csv.writer` 会自动按 RFC 4180 为该单元格加上双引号 `"approved,campaign"`，既标准又便于程序解析。
   - **编码选择**：采用标准 `UTF-8`（`text/csv; charset=utf-8`，不带 BOM），避免标准 CSV 解析器（如 `csv.DictReader`）将首列名误读为 `\ufeffuuid`。
2. **列表与导出共用查询构建器（`_build_query`）**：
   - 在 `keepstack/search.py` 中抽取出统一的 `_build_query()`，同时供分页检索 `search()` 和全量导出 `query_export_assets()` / `export_csv()` 调用，从根源上保证筛选条件与排序规则 100% 一致。
3. **多包含标签与多排除标签 SQL 实现**：
   - 包含标签使用 `a.id IN (SELECT at.asset_id FROM asset_tags at JOIN tags t ON t.id = at.tag_id WHERE t.name IN (...) GROUP BY at.asset_id HAVING COUNT(DISTINCT t.name) = ?)`，避免多表 `JOIN` 造成行膨胀；
   - 排除标签使用 `NOT EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id WHERE at.asset_id = a.id AND t.name IN (...))`，确保没有任何标签的素材不会被误杀；
   - 当归一化后的 `include_tags` 与 `exclude_tags` 存在交集时直接短路返回空结果。
4. **前端请求竞态与界面设计**：
   - 在 `web/app.js` 的 `loadLibrary()` 中同时结合递增请求序号（`latestSearchSeq`）与 `AbortController`，快速切换筛选或排序条件时立即中止旧请求并丢弃过期响应，确保最终渲染结果严格对应最新条件。
   - 默认每页条数设为 `12`，使默认 24 条 Seed 数据开箱即可体验多页翻页。
   - 按题目要求隐藏了顶部语义搜索模式切换框（`#search-mode`），并为侧边栏统计数字标注 `(catalog total)` 以避免被误认为当前筛选匹配数。

---

## 4. 已知限制与范围外问题记录

1. **分词限制**：沿用现有 FTS5 `porter unicode61` 分词器及前缀匹配逻辑，未引入第三方中文分词组件；连续无空格长中文串按 `unicode61` 规则作为整体词元做前缀匹配。
2. **同步全量导出**：当前 CSV 导出在单次 HTTP 请求中同步生成，适合中小规模素材库（万级以内）；若未来扩展至百万级素材，可改为流式 `StreamingResponse` 或后台异步导出任务。
3. **范围外上游问题记录（未修改以保持最小改动范围）**：
   - `keepstack/app.py` 使用了 `@app.on_event("startup")`，在新版 FastAPI 中会产生 `DeprecationWarning`（建议使用 `lifespan` 上下文管理器）。
   - `keepstack/app.py` 的 `add_version` 在开启外层 `conn = get_conn()` 后又进入 `with transaction() as conn:`，虽在同一线程连接下可工作，但属于上游冗余写法。

---

## 5. 未完成项

- 无。题目要求的全部核心功能（组合筛选、稳定排序与分页、请求竞态处理、鉴权全量 CSV 导出、修改后实时生效、可复现数据生成与自动化测试）均已完成并通过验证。
