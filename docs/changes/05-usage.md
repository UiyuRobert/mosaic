# 05 — 使用指南（中文）

面向最终用户：安装、启动、检索 NTRS、批量下载、队列管理。

## 1. 安装

```bash
# 克隆你的 fork
git clone https://github.com/UiyuRobert/mosaic.git
cd mosaic

# 安装（需要 Python ≥3.11，推荐 uv）
uv sync --extra all          # 全部可选依赖
# 或： pip install -e ".[all]"

mosaic --version             # 自检
```

## 2. 可选：配置 Unpaywall 邮箱（强烈建议）

Unpaywall 是免费的合法开放获取解析服务，要求一个联系邮箱：

```bash
mosaic config --unpaywall-email you@example.com
# 或在 Web UI 的 Config 页面填写 Unpaywall email
```

没有邮箱时跳过 Unpaywall，只用来源直供的 PDF 链接。

## 3. 可选：安装 aria2（推荐，获得受控并发与断点续传）

```bash
brew install aria2           # macOS
sudo apt install aria2       # Debian/Ubuntu
```

无需手动启动——MOSAIC 会在需要时自动拉起一个只绑 `127.0.0.1` 的
受管守护进程（RPC 端口默认 6800）。

## 4. 启动 Web UI（推荐主路径）

```bash
mosaic ui                    # 打开 http://127.0.0.1:8042
```

页面导航：**Search / Similar / Bulk / Downloads / NotebookLM /
History / Sessions / AI / Config**。

- **Search**：输入任意关键词 → 勾选来源（含 **NASA NTRS**）→ 搜索。
  某源失败不影响其他来源。
- **结果列表**：勾选单篇/全选 → `Selected (n)` 或 `All` 按钮批量入队。
  `Local` 列的 💾 表示已有本地 PDF。
- **Downloads**：队列页——状态、进度、目标目录、错误信息；支持
  暂停/恢复/重试/取消/清理记录（清理不删 PDF）。
- **Bulk**：上传 `.bib`/`.csv` 批量 DOI 下载——同样走持久队列，
  任务会出现在 Downloads 页。
- **Config**：下载目录、后端（auto/aria2/native）、并发数、单主机
  并发与起始间隔、重试次数、PDF 校验、跳过已下载、aria2 RPC 与限速。
  高级项折叠在 "aria2 settings" 下。

## 5. CLI 使用

```bash
# 检索 NTRS
mosaic search "turbine blade cooling" --source ntrs --max 30

# 检索并自动下载可获取的 PDF
mosaic search "turbine film cooling" --source ntrs --max 30 --download

# 多来源联合检索
mosaic search "compressor stall" --max 30      # 默认全部启用源

# DOI 直取（原功能）
mosaic get 10.xxxx/example
mosaic get --from references.bib                # BibTeX/CSV 批量 DOI
```

### 下载队列管理

```bash
mosaic downloads list            # 任务表（状态/进度/错误）
mosaic downloads status          # 聚合统计 + 后端健康
mosaic downloads pause <job_id>  # 或 --all
mosaic downloads resume <job_id> # 或 --all
mosaic downloads retry <job_id>  # 或 --failed
mosaic downloads cancel <job_id> # 或 --all
mosaic downloads clear --completed   # 清理记录，不删 PDF
```

### 下载相关配置

```bash
mosaic config --download-backend aria2 --max-concurrent 4 \
              --per-host-concurrent 1 --per-host-delay 1.5 --max-tries 3
mosaic config --verify-pdf --skip-existing
mosaic config --download-dir ~/my-papers
```

所有设置也可在 Web UI Config 页修改，等价写入
`~/.config/mosaic/config.toml` 的 `[download]` 段。

## 6. 文件与数据位置（默认值）

| 内容 | 位置 | 修改方式 |
|---|---|---|
| PDF 下载目录 | `~/mosaic-papers/` | Config 页或 `--download-dir` |
| SQLite 缓存 | `~/.local/share/mosaic/cache.db` | `--db-path` |
| 配置 | `~/.config/mosaic/config.toml` | Config 页 / `mosaic config` |
| aria2 session | `~/.local/share/mosaic/aria2.session` | 自动管理 |

下载中的临时文件是 `目标名.part`（aria2 另有 `.aria2` 控制文件），
校验通过后才原子改名为最终 `.pdf`——`.part` 残留即代表未完成。

## 7. 失败原因速查

Downloads 页/CLI `downloads list` 的 Error 列前缀即分类：

- `not_found`：404 资源不存在
- `access_denied`：401/403 受限
- `rate_limited`：429 限流（会按 Retry-After 自动退避重试）
- `server_error`：5xx
- `timeout` / `dns_error` / `unreachable`：网络问题
- `html_response`：服务器返回了错误页而非 PDF
- `not_pdf` / `missing_eof` / `unparseable`：校验失败（文件不完整或不可解析）
- `disk_full`：磁盘不足
- `unsafe_url`：URL 协议或目标主机被安全策略拒绝
- `cancelled`：用户取消

## 8. 常见工作流

1. `mosaic config --unpaywall-email me@uni.edu --download-backend aria2`
2. `mosaic ui` → 搜 "film cooling" → 勾 NTRS + OpenAlex + Crossref
3. 结果页全选 → "All N papers" → Downloads 页观察进度
4. 暂停某条 → 恢复；失败的点 retry
5. 关闭应用再开——未完任务自动回到队列继续
6. `mosaic cache export papers.csv` 导出题录（含本地路径）

## 9. 注意

- 仅下载**公开可得**的全文：来源直供公开 PDF、NTRS 官方公开附件、
  Unpaywall 报告的 OA 版本。不会尝试绕过付费墙。
- NASA NTRS API 限额 500 req/15min，已内置节流与退避。
- `backend=native` 时暂停等同取消重下；aria2 才支持真正断点续传。
