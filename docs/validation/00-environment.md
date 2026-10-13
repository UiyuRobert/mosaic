# 00 — 本地真实环境预检

- 测试日期：2026-10-12
- Git：`feat/ntrs-aria2` @ `dec523b0c034d09f5667a747f4a52e9cfe42e7d5`（开始基线，后续修复见 git diff）
- OS：macOS (Darwin 27.0.0, Apple Silicon)
- Python：3.12.14（`.venv`）
- MOSAIC：1.5.5，从工作目录源码加载（editable install 确认 `mosaic.__file__` 指向本仓库）
- aria2c：1.37.0（本阶段通过 `brew install aria2` 安装；预检前未安装）
- SQLite：随 Python 3.12 内置（3.5x）
- 磁盘：`/` 余量充足（`df -h` 实测可用 ≥100GB 级，测试另设 `--max-disk-gb` 预算）

## 数据源连通性（2026-10-12 实测，httpx GET）

| 来源 | 结果 | 说明 |
|---|---|---|
| OpenAlex | 200 OK | 可用，已配联系邮箱（git user.email） |
| Crossref | 200 OK | 可用 |
| arXiv | 200 OK | 可用（3s 节流内置于 source） |
| Semantic Scholar | **429** | 无 API key，公共端点限流 → 测试中以熔断冷却使用 |
| **NASA NTRS** | **403** | `ntrs.nasa.gov` 及 `/api/*` 全部 403（nginx 边缘拒绝，任意 UA/路径均同）。DNS → `*.elb.us-gov-west-1.amazonaws.com`。判定为**网络出口限制**（与上次验收记录一致），标记 `SOURCE_BLOCKED`，不做绕过 |
| Unpaywall | 200 OK | 需真实邮箱；`test@example.com` 返回 422。已配置 `22371360@buaa.edu.cn` |
| HAL | 200 OK | 可用 |
| Zenodo | 200 OK | 可用 |
| DOAJ | 200 OK | 可用 |

未配置 API key 的来源（CORE、NASA ADS、IEEE、Springer API、ScienceDirect、Scopus、PubMed/PMC 可选 key）不纳入本轮实采；浏览器会话类来源（sd/sp/scopus browser）不用于批量实采。Europe PMC/PubMed/PMC/bioRxiv/PEDro 为生物医学向来源，默认不用于航发词条（保留作个别方法类词的补充）。

## aria2 真实试采（阶段 0.2 实测结论）

修复前发现并复现 **Risk B**：`aria2c --daemon=true` 使父进程 fork 后立即退出 code 0，`ensure_client` 的 `proc.poll()` 误判为启动失败返回 `None`，同时泄漏一个持随机 secret 的孤儿守护进程（实测残留 PID 953，RPC 拒绝新连接）。

已修复并实测通过（真实守护进程 + 真实 PDF）：

- 受管启动：去 `--daemon`，改直接子进程 + `start_new_session`，`proc.poll()` 恢复语义；`aria2.daemon.json`（0600）记录 pid/secret 供孤儿重连或安全收割；端口被外来监听占用时自动顺延端口，不杀非自有进程。
- 真实下载：arXiv 公开 PDF ×3 并发 → 全部 `verify_pdf` 通过落盘。
- 暂停/恢复：300K 限速下暂停后字节保留，恢复续传完成（180224/776111 → 完成）。
- 重启恢复：人为制造 `DOWNLOADING` 残留 → 新 `DownloadManager` reconcile → 重排队 → 完成。
- 重复入队：`skip_existing` + `%PDF-` 头校验 → 返回空列表。
- DOI-only（无初始 URL）：Unpaywall 解析 → 候选推进 → aria2 下载成功（eLife DOI 实测）。
- GID 丢失：新增 tellStatus "not found" → 重排队（回归测试见 `tests/test_aria2.py`）。

另修复：queue 测试在 `enqueue_papers` 时惰性拉起真实守护进程导致泄漏（测试改为先注入 fake client）；`test_attaches_to_running_daemon` 原样会在有 aria2c 的机器上拉起真实进程，已改为全 mock 密封。

## Risk A（NTRS 附件补取）

代码层面确认：搜索记录仅含 `downloadsAvailable=true` 而无 `downloads` 列表时，候选为空导致误判"无下载"。已修复：`NTRSSource.pdf_candidates_for()`（`GET /citations/{id}/downloads` → 共用候选构造）接入 `_candidates_for` 延迟解析。因 NTRS 在本机 SOURCE_BLOCKED，该项以 mock 回归测试覆盖（`tests/test_ntrs.py`），真实环境待网络恢复后复验。

## 配置关键参数

- `download.backend=auto`（实采脚本显式用 `aria2`）
- `max_concurrent=4`、`per_host_concurrent=1`、`per_host_delay=1.5`（脚本内可调）
- `verify_pdf=true`、`skip_existing=true`、`max_file_mb=300`
- `unpaywall.email` 已配置（上表）

## 敏感信息

报告不包含 API key、登录态、Cookies。Unpaywall 邮箱为用户 git 配置中的学校邮箱，属公开学术联系地址。
