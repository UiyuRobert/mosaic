# 02 — 增强架构设计

目标：在**不重写** MOSAIC 的前提下，把"搜索 → 全文解析 → 受控并发下载 →
本地持久化"链路打通。复用优先级：`downloader.download()` 单文件逻辑 > 新增
`DownloadManager` 队列 > CLI/UI 两端薄封装。

## 1. 模块划分

```
mosaic/
├── models.py            # Paper + PDFCandidate + PaperStatus + ntrs_id 等字段
├── sources/
│   ├── ntrs.py          # NASA NTRS（POST /citations/search）
│   └── unpaywall.py     # resolve() 保留 + resolve_candidates() 多候选
├── pdfcheck.py          # PDF 校验、文件名安全、URL 安全（SSRF 防护）
├── downloader.py        # 单篇下载：候选解析 → 临时文件 → 校验 → 原子改名
├── aria2.py             # aria2 JSON-RPC 客户端 + 受管进程启动
├── dlqueue.py           # download_jobs 持久化队列 + DownloadManager 调度
├── workflows.py         # download_papers() 收口为队列入口（CLI/UI 共用）
└── db.py                # 版本化迁移 + paper_identifiers + download_jobs
```

## 2. 数据模型

### 2.1 Paper 扩展（追加字段，向后兼容）

```python
ntrs_id: str | None        # NTRS 11 位文档 ID
report_number: str | None  # 报告号（NASA/TM-xxxx 等）
pub_type: str | None       # 文献类型标签（Technical Memorandum 等）
pdf_candidates: list[dict] # 搜索时已知的 PDF 候选（可选持久化）
```

`uid` 优先级：`doi:` > `arxiv:` > `pii:` > `ntrs:` > `title:`。
`Paper.identifiers` 返回全部已知标识符集合，供别名合并。

### 2.2 paper_identifiers 别名表

```sql
CREATE TABLE paper_identifiers (
    identifier TEXT PRIMARY KEY,   -- 'doi:x' | 'arxiv:x' | 'pii:x' | 'ntrs:x'
    uid        TEXT NOT NULL       -- 主 papers.uid
);
```

- `Cache.save(paper)`：解析 paper.identifiers，若任一已映射到别的 uid，则合并
  到既有 uid（标识符优先级更高者胜出）并回填缺失字段；同时登记新标识符。
- `Cache.resolve_uid(uid)`：别名→主 uid，供 downloads/exports 查询。
- `search_all` 的 `seen` 合并改用同一套 identifier 索引：NTRS 记录 later 获
  DOI 时两 uid 合一，保留下载记录归属主 uid。

### 2.3 download_jobs 表

```sql
CREATE TABLE download_jobs (
    job_id           TEXT PRIMARY KEY,   -- uuid
    paper_uid        TEXT NOT NULL,
    url              TEXT,               -- 解析后的当前候选 URL
    backend          TEXT,               -- 'native' | 'aria2'
    aria2_gid        TEXT,
    destination      TEXT,
    downloaded_bytes INTEGER DEFAULT 0,
    total_bytes      INTEGER DEFAULT 0,
    status           TEXT NOT NULL,      -- 见下
    retry_count      INTEGER DEFAULT 0,
    last_error       TEXT,
    next_retry_at    REAL,               -- epoch；RETRY_WAIT 用
    host             TEXT,               -- 限速/并发分组
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);
```

状态机：

```
QUEUED → DOWNLOADING → VERIFYING → COMPLETED
   ↓          ↓                        ↑
 PAUSED    FAILED?                  （校验失败 → RETRY_WAIT/FAILED）
   ↓          ↓
 QUEUED   RETRY_WAIT → QUEUED（retry_count<max）
             ↓
           FAILED（达到上限）/ CANCELLED（用户）
```

- `PAUSED`：native 下视为"取消但保留已下字节信息"（重启续传不支持，文档说明）；
  aria2 下为真暂停（GID 保留，daemon 有 session 文件可断点续传）。
- 启动 reconcile：DOWNLOADING/VERIFYING/RETRY_WAIT 统一复位为 QUEUED
  （native）或凭 aria2 session 恢复（aria2）；孤儿 .part 文件清理。

### 2.4 PDFCandidate

```python
@dataclass
class PDFCandidate:
    paper_uid: str
    download_url: str
    landing_url: str = ""
    source: str = ""          # 'publisher'|'ntrs'|'unpaywall'|'browser'
    version: str = ""         # unpaywall: submittedVersion/acceptedVersion/publishedVersion
    license: str = ""
    is_open_access: bool = True
    confidence: float = 0.5   # 0..1，排序用
    discovered_at: str = ""   # ISO
```

解析顺序（`downloader._candidates_for(paper, cfg)`）：
`paper.pdf_url/pdf_candidates`（源直供）→ `unpaywall.resolve_candidates` 全部
OA location（best 优先）→ 浏览器会话兜底。

## 3. 下载管线（两后端共享）

```
resolve_candidates → 取首个可行 → fetch（native|aria2）
  → 临时文件 dest.part → verify_pdf（magic+EOF+size+pymupdf 页数可选）
  → sha256 → os.replace 原子改名 → downloads.status='ok'
失败 → 分类错误 → RETRY_WAIT（指数退避 + Retry-After）→ 换下一候选 → 上限后 FAILED
```

错误分类：`http_404 / http_401_403 / rate_limited_429 / server_5xx /
timeout / dns / invalid_pdf / html_response / disk_full / exists / cancelled /
unreachable`。

- `_fetch`：流式写 `.part`，逐跳校验重定向目标（scheme=http/https、
  非内网/回环/链路本地 IP）；Content-Type 非 pdf 时仍下载但交给
  verify 判定（有些服务器乱标 type，但内容是 PDF 就放行）。
- `verify_pdf`：`%PDF-` 头 + 尾部 `%%EOF`（容忍 512B 尾噪声）+ 最小 1KB；
  pymupdf 可用时附加页数≥1。`config.download.verify_pdf` 可关（仍查 magic）。
- 文件名：`safe_filename` 不变；若目标已存在且属于别的 uid，追加
  `__{uid8}` 后缀去重，杜绝误覆盖。

## 4. DownloadManager（调度器）

单例（按 db_path），后台线程 + ThreadPoolExecutor：

```
┌──────────┐   enqueue   ┌───────────────┐
│ CLI / UI │────────────→│ download_jobs │
└──────────┘             └──────┬────────┘
                                │ poll
                    ┌───────────▼───────────┐
                    │  scheduler tick (1s)  │
                    │  - 全局并发 ≤ N        │
                    │  - per-host 并发 ≤ M   │
                    │  - host 起始间隔 ≥ T   │
                    └───────────┬───────────┘
              ┌─────────────────┼─────────────────┐
         backend=native    backend=aria2      reconcile
        ThreadPool 内       aria2.addUri        启动时核对
        执行管线            + tellStatus 轮询    DB/aria2/文件
```

- 配置（`config.toml [download]`，均可在 UI/CLI 改）：
  `backend(auto|native|aria2)`、`max_concurrent=4`、`per_host_concurrent=1`、
  `per_host_delay=1.5`、`max_tries=3`、`max_split=1`、`max_connections=1`、
  `verify_pdf=true`、`skip_existing=true`、
  `aria2_rpc_url=http://127.0.0.1:6800/jsonrpc`、`aria2_rpc_secret=`、
  `aria2_auto_start=true`、`aria2_fallback_native=false`。
- `backend=auto`：检测到可用 aria2（已运行或可启动）→ aria2；否则 native。
- aria2 选项：`dir`、`out`、`split`、`max-connection-per-server`、
  `max-tries`、`retry-wait`、`max-download-limit`（可选）、`continue=true`。
- per-host 统计基于**最终重定向后主机**：native 管线在跟随重定向前先做
  `is_safe_url` 校验并更新 job.host；aria2 无法逐跳限制，故 aria2 后端
  per_host 用初始主机 + `max-connection-per-server=per_host` 双保险，
  文档注明限制。

## 5. UI 集成

- `/downloads` 页面：队列表（标题/状态/进度/速度/大小/目录/错误）+ 操作
  （暂停/恢复/重试/取消/清理已完成），HTMX 每 2s 局部刷新。
- 结果列表：每行复选框 + 顶部"下载所选 (n)" + "下载全部"；POST
  `/downloads/enqueue` 入队；行内沿用既有单篇 Download 按钮（改走队列）。
- 设置页：新增 Download 区块（backend、并发、限速、重试、verify、
  skip_existing、aria2 RPC）；NTRS 出现在 sources 开关列表（自动）。
- 搜索失败隔离已有（per-source try/except），保留。

## 6. CLI 集成

```
mosaic search "..." --source ntrs --max 30 --download
mosaic downloads list [--status X] [--limit N]
mosaic downloads status
mosaic downloads pause <job_id>|--all
mosaic downloads resume <job_id>|--all
mosaic downloads retry <job_id>|--failed
mosaic downloads cancel <job_id>|--all
mosaic downloads clear --completed|--failed|--all
mosaic config --download-backend aria2 --max-concurrent 4 ...
```

`search --download` / `get` / `get --from` 走 `workflows.download_papers`：
- `backend=native`：保持原有顺序体验（向后兼容），但管线已带校验。
- `backend=aria2|auto`：入队后前台等待，渲染进度，返回 pdf_map（Zotero
  附件兼容）。

## 7. 兼容策略

- `download()` 签名与返回语义不变；`workflows.download_papers` 返回
  `{uid: path}` 不变。
- `downloads` 表语义不变（仍是"最终态"记录）；`download_jobs` 是过程态。
- 老库升级：`user_version` 迁移，不丢数据；重复执行幂等。
- `ui` 仍绑 `127.0.0.1`；aria2 RPC 也只绑回环。

## 8. 已知取舍

- native 后端的"暂停"实为取消+重下（httpx 不支持断点），UI 标注。
- aria2 管理进程退出时默认 `--save-session` 持久化，重启后恢复队列。
- NTRS 检索在本机被 NASA 边缘 WAF 拦截（403），联网验收受限——验收
  文档如实标注。
