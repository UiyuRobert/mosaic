# 06 — 验收报告

日期：2026-10-12　分支：`feat/ntrs-aria2`（基线：`main` @ 64b9919）

## 1. 测试结论

- **全量测试：1021 通过 / 12 跳过 / 0 失败**（`pytest`，Python 3.12）。
- **Lint：`ruff check` + `ruff format --check` 全部干净。**
- 新增测试：`test_ntrs.py`(18) `test_pdfcheck.py` `test_dlqueue.py`(17)
  `test_unpaywall.py` `test_aria2.py` `test_e2e_pipeline.py`(7)
  `test_ui.py` 下载队列用例(9)。

## 2. 验收案例结果（对应规格 §10.4）

| 案例 | 结果 | 方式 |
|---|---|---|
| A. NASA 技术报告检索+下载 | ✅ 通过（离线） | `tests/test_e2e_pipeline.py::test_case_a_ntrs_search_to_pdf`：本地模拟 NTRS API 返回官方 schema 记录 → 队列下载 → `verify_pdf` → 保存 `.pdf` 字节一致 |
| B. 多来源合并去重 | ✅ 通过 | `test_same_doi_merges` + `test_db_alias_resolves_download`：NTRS-only 记录与带 DOI 记录合并为单一规范 uid，下载记录双向别名可达 |
| C. Unpaywall 回退 | ✅ 通过 | `test_unpaywall.py`：best_oa_location + oa_locations 多候选、落地页降权、去重；下载链路按序尝试全部候选 |
| D. 批量下载+失败处理 | ✅ 通过 | `test_case_d_bad_response_fails_cleanly`：HTML 冒充 PDF → `FAILED: html_response`，最终路径无残留；并发/暂停/恢复见 `test_dlqueue.py` |
| E. 重启恢复 | ✅ 通过 | `test_case_e_restart_recovers_queued_job`：模拟崩溃后新实例 reconcile → 重新入队并完成 |
| F. 重复检索跳过 | ✅ 通过 | `test_case_f_repeat_search_skips_downloaded`：第二次入队返回空列表 |

## 3. ⚠️ 联网验收未完成（如实声明）

本开发环境访问 `ntrs.nasa.gov` **持续返回 HTTP 403**
（`api.ntrs.nasa.gov` 解析至 AWS GovCloud ELB，疑为边缘 WAF/网络
限制）：

```text
curl https://ntrs.nasa.gov/api/health            → 403
curl -X POST .../api/citations/search -d '{"q":"turbine"}' → 403
```

因此"真实 NTRS 端到端"未能执行。缓解措施：

- 实现严格依据 NASA 官方 OpenAPI 文档与数据字典（已存档并提取）。
- 全部 NTRS 请求/响应测试使用按文档构造的 mock 数据与本地模拟服务器。
- 在有正常网络出口的机器上，`mosaic search "..." --source ntrs` 即可
  直接验证；代码路径与离线验收完全一致。
- 同理，真实 aria2 守护进程集成测试未跑（`aria2` 客户端与调度已用
  mock RPC 覆盖）；本机安装 aria2 后 `download.backend=aria2` 即可实测。

## 4. P0–P3 完成度

- **P0** ✅ NTRS source、附件解析、`ntrs:{id}` 稳定 uid、临时文件→
  校验→原子改名、`mosaic search --source ntrs --download` 链路
  （离线验证）。
- **P1** ✅ PDFCandidate 多候选、aria2 JSON-RPC（受管守护进程/
  session 续传）、`download_jobs` SQLite 队列、并发/单主机限速、
  指数退避+Retry-After、暂停/恢复/取消、启动 reconcile、跳过已下载、
  错误分类。
- **P2** ✅ Web UI：NTRS 出现在来源勾选（注册表驱动）、结果复选框+
  批量下载按钮、`/downloads` 队列页（HTMX 轮询/操作/清理）、
  Config 下载区块（含折叠 aria2 高级项）、`mosaic downloads` 子命令。
- **P3** ✅ 标识符别名去重（`paper_identifiers` + `migrate_uid`）、
  `PRAGMA user_version` 版本化迁移（迁移前自动备份 `.bak-v*`）、
  中文使用文档、错误分类。

## 5. 修改文件清单

```
新增： mosaic/sources/ntrs.py      mosaic/pdfcheck.py
       mosaic/aria2.py             mosaic/dlqueue.py
       mosaic/ui/templates/downloads.html
       mosaic/ui/templates/partials/downloads_table.html
       tests/test_{ntrs,pdfcheck,dlqueue,unpaywall,aria2,e2e_pipeline}.py
       docs/changes/{01..06}-*.md
修改： mosaic/models.py（ntrs_id/report_number/pub_type/pdf_candidates,
       PDFCandidate, PaperStatus, identifiers, uid 规则）
       mosaic/db.py（版本化迁移、paper_identifiers、download_jobs、
       别名解析、迁移前备份）
       mosaic/services.py（别名感知 merge_papers）
       mosaic/search.py, mosaic/similar.py（ident_index）
       mosaic/downloader.py（候选管线、校验、原子改名、SSRF、进度回调）
       mosaic/sources/unpaywall.py（resolve_candidates）
       mosaic/workflows.py（download_papers 走队列）
       mosaic/cli.py（downloads 子命令、config 下载项、--source ntrs）
       mosaic/ui/routes.py（下载队列路由、批量入队、config 保存）
       mosaic/ui/__init__.py（downloaded_uids 上下文）
       mosaic/ui/templates/{base,config,partials/results}.html
       mosaic/config.py（[download] 段、ntrs 源、校验）
       mosaic/source_registry.py / sources/__init__.py（注册）
       mosaic/errors.py（DownloadError.reason + TRANSIENT_REASONS）
       tests/test_{downloader,ui}.py（适配新语义）
文档： README.md、CLAUDE.md、mosaic/data/SKILL.md（22 sources、ntrs、
       downloads 命令）、docs/guide/{sources,configuration,
       cli-reference,web-ui,installation}.md、docs/index.md
```

## 6. 风险与遗留

- NTRS 真实 API 403（环境限制，见 §3）。
- native 后端暂停=取消重下（无断点续传，文档已注明）。
- aria2 per-host 限制基于初始 URL 主机，重定向换主机不可控
  （保守默认 split=1/connections=1 缓解）。
- `merge_papers` 的别名合并只认强标识符（doi/arxiv/pii/ntrs），
  不做标题相似度猜测合并（规格要求）。
- PyInstaller 打包清单未验证新模块（`mosaic/aria2.py` 等）——
  `build.py` 的 hiddenimports 需在发版前确认。

## 7. 建议下一步

- 在正常网络环境执行真实 NTRS + aria2 验收（同一命令即验证）。
- `search_all` 并发上限可配置化（现硬编码 8）。
- 下载队列与 `searches`/`papers` 的级联清理策略。
- UI 单篇详情页增加队列状态直链。
- 导出格式纳入 `download_jobs` 失败原因。
