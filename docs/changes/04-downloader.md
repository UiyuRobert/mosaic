# 04 — 下载管线、队列与 aria2

## 1. 两个后端，一条管线

```
PDFCandidate 列表（源直供 → Unpaywall 全部 OA 位置 → 浏览器会话）
        │
        ▼
  <dest>.part 临时文件下载
        │
        ▼
  verify_pdf（大小/magic/%%EOF/HTML 检测/pymupdf 页数）
        │
        ▼
  os.replace 原子改名 → downloads.status='ok'
```

- `native`：`mosaic/downloader.py:_fetch`（httpx 流式，逐跳校验重定向）。
- `aria2`：`mosaic/aria2.py` JSON-RPC 客户端驱动本地守护进程，同样
  下载到 `.part` 再由调度器 `VERIFYING → rename`。

两后端共享候选解析（`downloader._candidates_for`）、目标命名
（`_destination_for`）、校验（`pdfcheck.verify_pdf`）、完成态记录
（`downloads` 表）与错误分类（`DownloadError.reason`）。

## 2. PDF 校验（`mosaic/pdfcheck.py`）

| 检查 | 失败原因 |
|---|---|
| 文件非空、≥1KB | `empty` / `too_small` |
| 首字节非 HTML/XML | `html_response` |
| `%PDF-` magic | `not_pdf` |
| 尾部 4KB 内含 `%%EOF` | `missing_eof`（截断文件识别） |
| pymupdf 可打开、页数≥1、未加密 | `unparseable` |
| HTTP 状态/超时/DNS | `not_found`/`access_denied`/`rate_limited`/`server_error`/`timeout`/`dns_error`/`unreachable` |
| 磁盘满 | `disk_full` |
| URL 非 http(s) 或指向内网 | `unsafe_url` |
| 无任何候选（无公开 URL 且无 OA 版本） | `no_candidates`（永久，不重试） |
| 用户取消 | `cancelled` |

- `Content-Length` 超过 `max_file_mb`（默认 300MB）即中止。
- 同名冲突：目标已存在且是合法 PDF → `name__2.pdf` 去重，绝不覆盖。
- 文件名经 `sanitize_filename` 兼容 Windows/macOS/Linux（非法字符、
  保留名、长度上限、首尾点/空格）。
- SSRF：默认阻断 file://、回环、私网、链路本地、metadata 端点；
  重定向每一跳重新校验。`download.allow_private_hosts` 仅供测试/
  自建私有源显式打开（默认关闭）。

## 3. `PDFCandidate`（`models.py`）

```python
paper_uid, download_url, landing_url, source, version, license,
is_open_access, confidence, discovered_at
```

候选顺序：`paper.pdf_candidates`（NTRS 附件等源直供）→ `pdf_url` →
`unpaywall.resolve_candidates` 的全部 OA location（best 优先，`url`
落地页降权，仍保留作合法候选）→ 浏览器会话（保持原权限边界，
不扩大反检测能力）。

**延迟解析**：enqueue 只把"免费"候选（`pdf_candidates` + `pdf_url`）写入
`candidates_json`，不做任何网络请求，批量入队不会串行打 Unpaywall。
worker 调度时才调用 `_job_candidates` 合并完整解析结果（按 URL 去重，
存储候选保持优先）。aria2 任务每轮失败也会重算完整候选列表再推进，
因此 DOI 论文在直链失效后仍能走到 Unpaywall 备选。

## 4. `download_jobs` 持久队列

状态机：`QUEUED → DOWNLOADING → VERIFYING → COMPLETED`，支路
`PAUSED / RETRY_WAIT → QUEUED / FAILED / CANCELLED`。

字段含 `job_id, paper_uid, url, backend, aria2_gid, destination,
downloaded_bytes, total_bytes, status, retry_count, last_error,
next_retry_at, host, candidates_json, created_at, updated_at`。

### 调度器（`DownloadManager`）

- 全局并发 ≤ `max_concurrent`（默认 4）。
- 同主机并发 ≤ `per_host_concurrent`（默认 1）。
- 同主机起始间隔 ≥ `per_host_delay`（默认 1.5s）——在调度层实现，
  与 aria2 的 `max-connection-per-server` 双保险。
- FIFO 调度（`created_at` 升序）。
- `RETRY_WAIT` 按 `next_retry_at` 到点回 QUEUED；退避
  `base(5s) × 2^retry`，尊重 `Retry-After`，上限 300s。
- 瞬时错误（timeout/5xx/429/网络）有限重试 `max_tries`（默认 3）；
  永久性错误（404/401/403/校验失败/unsafe_url/disk_full/cancelled）
  直接 FAILED，不浪费重试。
- aria2 后端单 URL 失败时先推进 `candidates_json` 中下一候选，再计重试；
  native 后端单尝试内即遍历全部候选。

### 暂停 / 恢复 / 取消

- aria2：真暂停（`aria2.pause`/`unpause`，GID 保留；守护进程
  `--save-session` 使重启后续传）。
- native：暂停/取消通过 `abort_check` 在流中即时中止，删除 `.part`，
  恢复即重新下载（httpx 不支持断点续传，UI 中文档化）。
- 启动 reconcile：`DOWNLOADING/VERIFYING/RETRY_WAIT` 复位为 QUEUED；
  aria2 任务优先用旧 GID `tellStatus` 重连；`dest` 已是合法 PDF 直接
  COMPLETED 跳过。
- 完成态跳过：enqueue 时 `skip_existing` 校验 "status=ok + 文件存在 +
  头是 `%PDF-`"，三者缺一即重新下载——不盲信 SQLite 记录。

## 5. aria2 接入细节

- `ensure_client`：先连 `aria2_rpc_url` 已有实例；否则 `aria2_auto_start`
  时拉起受管守护进程——仅绑 `127.0.0.1`、`--rpc-secret`（未配置则临时
  生成、只驻内存）、`--save-session`/`--input-file`/`--continue=true`。
- `aria2_fallback_native=false`（默认）：显式选 aria2 而不可用时清晰
  报错，不会静默降级成无限制并发；`backend=auto` 自动探测。
- 每文件 `split/max-connection-per-server` 默认 1（保守，配合
  per-host 调度）；`aria2_max_download_limit` 可设总速度。
- 单文件分段数与整队 per-host 限制是两层不同约束，文档与注释中明确。

## 6. 后端选择与失败语义

- 入队时对 `auto`/`aria2` 做一次惰性 `ensure_aria2`，job 的 `backend`
  标记反映真实生效后端。
- `manager.aria2_available` 暴露 RPC 可用性：`workflows.download_papers`
  在 `backend=aria2` 且不可用且无 `aria2_fallback_native` 时取消任务并
  抛出 `DownloadError("unreachable")`；调度器层同样兜底——stamped
  aria2 任务不可用时按配置降级或 FAILED，不会静默挂起。
- CLI `search --download`/`get`、Web UI 单篇与批量按钮、`/bulk` DOI
  上传页全部收敛到同一队列（`_run_download`/`_run_bulk_download` 调
  `download_papers`），一处管线、一处状态。

## 7. 与原 `downloads` 表的关系

- `downloads`：最终态记录（uid → local_path + status），语义不变，
  Zotero/导出/详情页继续读它。
- `download_jobs`：过程态队列；完成后同步写 `downloads`。
- 两套数据同一 SQLite 文件、同一连接管理，不存在不一致窗口。

## 8. 局限（如实记录）

- aria2 的 per-host 控制基于初始 URL 主机；重定向后换主机无法在
  aria2 内部限流，采用保守默认 `split=1, connections=1` 缓解。
- native 暂停即重下，不续字节。
- Unpaywall `url` 候选可能是 HTML 落地页——校验层兜底，不会存为 PDF，
  但会消耗一次尝试。
