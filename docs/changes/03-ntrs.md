# 03 — NASA NTRS 数据源

`mosaic/sources/ntrs.py` 实现了 NASA Scientific and Technical Information
Repository（NTRS）官方 OpenAPI，独立于现有 NASA ADS（天文学文献）。

## 接入的接口

| 方法 | 端点 | 用途 |
|---|---|---|
| POST | `https://ntrs.nasa.gov/api/citations/search` | 元数据检索（JSON body） |
| GET | `https://ntrs.nasa.gov/api/citations/{id}` | 单条记录 |
| GET | `https://ntrs.nasa.gov/api/citations/{id}/downloads` | 附件列表 |

所有请求构造、字段名、分页和附件结构均依据 NASA 官方文档
（`STI_Open_API_Documentation_20210426.pdf` + 数据字典 2021-06），
未凭空假设字段。

## 检索字段映射

| `SearchFilters` | NTRS API | 说明 |
|---|---|---|
| `query` / `raw_query` | `q` | 自由文本（raw_query 原样传递） |
| `field=title` | `title` | 标题限定 |
| `field=abstract` | `abstract` | 摘要限定 |
| `year_from`/`year_to` | `published.gte`/`published.lte` | 年份范围（服务端） |
| `years[]` | 同上取 min..max | 精确年份由框架后置过滤收口 |
| `authors[0]` | `author` | API 只接受单个、大小写敏感的作者 |
| `journal` | —（无对应字段） | 仅本地后置过滤 |
| `keyword` | `keyword` | 关键词 |
| 分页 | `page.size`(≤100) / `page.from` | 自动翻页至 `max_results` |

明确的服务端过滤缺失项（`journal`、精确年份列表、标题之外的作者
组合）由 `search_all`/`filter_papers` 后置过滤承担，见 02 文档。

## 元数据映射 → `Paper`

| NTRS 字段 | Paper 字段 |
|---|---|
| `id` | `ntrs_id`（→ uid `ntrs:{id}` 当无 DOI） |
| `title` / `abstract` | 同名 |
| `authorAffiliations[].meta.author.name` | `authors`（按 sequence 排序） |
| `publications[].doi` 或 `sourceIdentifiers[type=DOI].number` | `doi` |
| `publications[].publicationDate` → `distributionDate` → `created` | `year` |
| `publications[].publicationName` | `journal` |
| `otherReportNumbers` | `report_number` |
| `stiType`(+`stiTypeDetails`) | `pub_type`（枚举→可读标签） |
| `center.name` | 仅内部记录（未映射独立字段） |
| `downloads[]` | `pdf_url` + `pdf_candidates` |
| `https://ntrs.nasa.gov/citations/{id}` | `url`（官方永久引用页） |
| `distribution==PUBLIC` 且有附件 | `is_open_access` |

## PDF 附件选择策略

在 `downloads[]` 中：

1. 排除 `draft` 附件与非 PUBLIC 记录（`distribution != "PUBLIC"` 整条丢弃）。
2. 优先 `type == "STI"`（正文）而非 `ABSTRACT` 附件。
3. 优先 `links.pdf`；缺失时仅当 `mimetype`/`name` 明确是 PDF 才用 `links.original`。
4. 相对链接统一以 `https://ntrs.nasa.gov` 拼接为绝对地址，绝不自行
   推断附件 URL。
5. 所有候选写入 `paper.pdf_candidates`（`PDFCandidate` 序列化字典），
   下载管线按序尝试。
6. `downloadsAvailable=false` 或 `disseminated=METADATA_ONLY` 的记录保留
   题录（`pdf_url=None`，`is_open_access=False`），不因无全文而消失。

## 稳定 ID 与去重

- 无 DOI 的 NASA 报告：`uid = ntrs:{id}`。
- 有 DOI：`uid = doi:{doi}`，`ntrs:{id}` 记入 `identifiers` 与
  `paper_identifiers` 别名表 —— 同一文献经 NTRS 与 OpenAlex 双源命中
  时合并为一条记录，下载记录归属规范 uid（详见 `db.py` 的
  `migrate_uid`/`resolve_uid` 与 `services.merge_papers` 的别名索引）。

## 限额与合规

- API 限额 **500 req / 15 min**；`rate_limit_delay`（默认 1s，可配）做
  请求间隔节流；HTTP 429 按 `Retry-After` 退避，5xx 指数退避，
  最多 3 次后抛 `SourceError`。
- NASA 要求定期检查公开可得性变化：我们不缓存"可下载"结论超过本次
  下载周期；`downloads` 表记录的是落盘结果而非可得性断言。
- 访问状态变化处理：`downloads` 记录不阻塞重新下载——若官方下线了某
  PDF，下次解析的候选列表自然不再包含它。

## 已知限制

- 开发环境访问 `ntrs.nasa.gov` 返回 **HTTP 403**（边缘 WAF/网络限制），
  真实联网验收未执行；测试全部使用按官方 schema 编写的 mock 数据。
- `sort` 参数未使用（默认相关性排序）。
- NTRS 的 `subjectCategory`、`stiType` 服务端过滤暂未暴露到 CLI/UI
  （通用工具定位，不硬编码学科）。
