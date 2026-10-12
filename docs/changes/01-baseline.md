# 01 — MOSAIC 1.5.5 基线审计

> 审计对象：`UiyuRobert/mosaic` fork @ `main`（上游 `szaghi/mosaic` 1.5.5）
> 开发分支：`feat/ntrs-aria2`（所有改动只针对 fork，不回推上游）
> 审计日期：2026-10-12

## 0. 仓库与 Git 状态

- `origin` 已确认指向 `https://github.com/UiyuRobert/mosaic.git`（fork）。
- 另添加 `upstream` = `https://github.com/szaghi/mosaic.git`，仅用于只读比较。
- `CLAUDE.md` 工作流规则仍然有效：**禁止自动 `git commit`**（GPG 签名需要 TTY）。
  本阶段所有改动保留在工作区，最后附人工提交命令。
- 数据目录约定：`~/.config/mosaic/config.toml`（配置）、
  `~/.local/share/mosaic/cache.db`（SQLite）、`~/mosaic-papers/`（PDF）。

## 1. 技术问题逐项回答

### 1.1 Source 如何注册？

四件套，缺一不可：

| 位置 | 作用 |
|---|---|
| `mosaic/sources/__init__.py` | 导出 Source 类 |
| `source_registry.py` | `SRC_MAP`（shorthand→显示名）、`SHORTHAND_TO_CFG_KEY`、`_SOURCE_REGISTRY`（config_key→工厂）；`build_sources(cfg)` 按 enabled 实例化 |
| `config.py` | `_DEFAULTS["sources"][key]`（默认配置）、`_KNOWN_SOURCES`（config validate 白名单） |
| `cli.py` | `--source` 帮助文本 / `config --enable-source` 名称列表（硬编码字符串） |

Web UI 勾选列表由 `routes.py:search_page` 用 `SRC_MAP` + `enabled` 状态自动生成，
注册表加好即自动出现。

### 1.2 多源检索是否已并发？上限可配置吗？

已并发：`search_all()` 用 `ThreadPoolExecutor`，`workers = min(len(active), 8)`。
**并发上限 8 是硬编码，不可配置**（`search.py:44`）。

### 1.3 Web UI 和 CLI 是否共用搜索与下载逻辑？

- 搜索：是。CLI `search` 与 UI `_run_search` 都调 `search_all()`。
- 下载：核心相同（都调 `downloader.download()`），但编排各自重复：
  - CLI：`cli.py:_download_all` / `_bulk_download`（顺序、无重试）
  - UI：`routes.py:_run_download` / `_run_bulk_download` / `_run_notebook_from_query`
- 结论是"同一根函数、多套循环"，适合在 `workflows.py` 层收拢为统一队列入口。

### 1.4 原有 Unpaywall 如何解析？

`sources/unpaywall.py:resolve(doi, email)` —— GET `api.unpaywall.org/v2/{doi}?email=`，
返回 `best_oa_location.url_for_pdf or best_oa_location.url`。

**缺陷**：
- 只看 `best_oa_location`，丢弃 `oa_locations[]` 其余候选；
- `url` 字段常为出版社落地页（HTML），直接被当 PDF 保存（见 1.5）；
- 未区分 `url_for_pdf` / `url_for_landing_page` / `version` / `license` / `host_type`。

### 1.5 原有 PDF 下载能防止 HTML 错误页被存为 PDF 吗？

**不能。** `downloader._fetch` 直接 `open(dest,"wb")` 流式写最终文件，
唯一检查是 `raise_for_status()`。无 Content-Type、无 `%PDF` magic、无页面数校验。
Unpaywall 返回 HTML 落地页时会被原样保存为 `.pdf` 并标记 `status='ok'`。

### 1.6 下载过程是否直接写正式目标文件？

**是。** 无临时文件、无原子改名。中途断网/超时会在目标路径留下截断的
`.pdf`；后续运行时 `downloads.status='ok'` 记录缺失倒不会误认为已完成
（失败路径不写 ok），但残留残缺文件会阻碍排查，且同名文件会被覆盖。

### 1.7 SQLite 如何记录下载成功/失败？

`downloads` 表（`db.py:70-75`）：`uid PK, local_path, status, downloaded_at`。
`status='ok'` 或 `'error: …'`。`download()` 开头凭 `status='ok'` + 文件存在跳过。
**不校验文件内容**——残缺/损坏文件只要有 ok 记录就永久跳过。

### 1.8 无 DOI 的 NASA 技术报告如何保持稳定唯一 ID？

现状做不到：`Paper.uid` 退化到 `title:{slug}`，标题变体即产生新条目。
需要给 `Paper` 增加 `ntrs_id` 字段，uid 链中加入 `ntrs:{id}`；
并通过新的标识符关联机制处理"先有 NTRS 记录、后发现 DOI"的合并。

### 1.9 Web UI 异步任务管理能否复用于下载队列？

`ui/jobs.py:JobManager` 是**纯内存**实现：结果存 `app.config`、
完成任务 15 分钟后 GC、进程退出即丢失。适合搜索这类短任务，
**不适合**需要重启续传的下载队列。本次将保留 JobManager 管搜索/导出，
另建 SQLite 持久化 `download_jobs` 队列 + `DownloadManager` 调度。

### 1.10 数据库迁移机制能否安全升级老用户数据？

现状：`CREATE TABLE IF NOT EXISTS` + 逐条 `ALTER TABLE` try/except
（`db.py:_init`），无 `PRAGMA user_version`。优点是所有语句幂等；
缺点是无版本记录、无回滚能力、顺序依赖隐含。本次将引入
`user_version` 版本化迁移，并把旧 try/except 段保留为 v1 基线（老库平滑升级）。

## 2. 其他审计发现（影响采集可靠性）

| 问题 | 位置 | 影响 |
|---|---|---|
| 同名论文文件互相覆盖 | `Paper.safe_filename` + `downloader` | 两篇标题相似的论文映射到同一文件名 |
| 无 SSRF/协议限制 | `_fetch` `follow_redirects=True` | 重定向到 `file://`、内网、metadata 端点无防护 |
| 下载无并发控制与限速 | `_download_all` 顺序循环 | 慢；但对站点反而算"礼貌" |
| 无重试/退避 | 全链路 | 429/5xx 直接失败 |
| `merge_papers` 只按 uid 精确匹配 | `services.py` | 同一论文以不同标识符出现时产生重复条目 |
| NTRS 整站对本机 IP 403 | 网络环境 | 真实 API 验证需离线 mock（见验收文档说明） |

## 3. NTRS API 事实（来自官方文档，2022-11-22 版）

- 根：`https://ntrs.nasa.gov/api`；无需认证。
- 限额：**500 req / 15 min**；单查询最多取 10,000 条；`page.size ≤ 100`。
- `POST /citations/search`，JSON body：`q,title,abstract,author,keyword,center,
  stiType,subjectCategory,reportNumber,published{gt,gte,lt,lte},disseminated,
  distribution,sort{field,order},page{size,from},highlight`。
- `GET /citations/{id}` → 单条 citation；`GET /citations/{id}/downloads` →
  附件列表；`GET /citations/{id}/downloads/{filename}` → 文件本体。
- 附件结构：`downloads[] = {draft, mimetype, name, type(STI|ABSTRACT),
  links:{original,pdf,fulltext}}`；另有 `downloadsAvailable`、
  `disseminated`(NONE/METADATA_ONLY/DOCUMENT_AND_METADATA)、
  `distribution`(PUBLIC/DO_NOT_DISTRIBUTE)。
- ToS 要点：redistributions 端点需定期（≤每周）核查可得性变化；
  引用优先用 `ntrs.nasa.gov/citations/{id}` 落地页链接。
- 元数据：`authorAffiliations[].meta.author.name`（作者）、
  `publications[].doi` / `sourceIdentifiers[type=DOI].number`（DOI）、
  `publications[].publicationName`（期刊/会议来源）、
  `otherReportNumbers`（报告号）、`stiType/stiTypeDetails`（文献类型）、
  `subjectCategories`（NASA 学科分类）、`center.name`（机构）、
  `keywords`（关键词）、`distributionDate/created`（日期）。

## 4. 兼容性与风险

- `Paper` 新字段追加在 dataclass **末尾**，避免影响位置参数构造；
  `row_to_paper` 用 `"col" in keys` 防御旧表（沿用 openalex_id 先例）。
- `downloads` 表保持原语义；新 `download_jobs` 表独立，不与其冲突。
- `download()` 保持 `(paper, dir, cache, email, pattern) -> str|None` 签名，
  CLI/UI/测试的既有调用点不变。
- 迁移采用 `PRAGMA user_version`：老库（无版本号）按 v1 处理，执行 v1→v2
  迁移；迁移前建议用户备份 `cache.db`（文档中说明）。

## 5. 下一步

P0：`mosaic/sources/ntrs.py` + Paper.ntrs_id + 校验式下载管线；
P1：`PDFCandidate`、aria2 RPC、`download_jobs` 队列、`DownloadManager`；
P2：Web UI 下载页/批量勾选/设置；P3：别名合并、文档、全量测试。
详细设计见 `02-architecture.md`。
