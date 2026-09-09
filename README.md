# SJFX 数据分析 Agent

SJFX 是一个面向本地资料包的可审计分析系统：先建立文件目录和原始证据索引，再按用户选择完成解析、摘要、关系挖掘、情报概览与报告导出。系统重点是结果可回溯、任务可恢复、范围可补充，适用于普通资料包，也适用于数百到数千份邮件组成的大型数据集。

详细讲解见 [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md)。

## 当前能力

- 扫描目录并持久化文件、目录节点、压缩包成员、大小、类型和哈希。
- 解析 PDF、DOCX、PPTX、XLSX、CSV、JSON、TXT、Markdown、HTML、图片及 ZIP/TAR。
- 保存带文件、页码/段落/表格、字符范围、解析版本和哈希的证据。
- 普通导入支持先选一批文件做初步摘要，之后返回补充剩余文件；支持暂停、继续、停止和切换数据包。
- 大数据包使用隔离队列完成清单、快速解析、深度解析、文件/节点摘要、情报概览及 JSON/Word 导出。
- 结构化资料支持字段、缺失值、重复值、异常值、时间范围和统计汇总。
- 构建文件、实体、主题、事项和证据关系图；每条关系保存来源、置信度、理由和证据片段。
- 提供对话式检索、证据问答、研究方向推荐和可回溯报告。

## 系统架构

```text
浏览器（HTML/CSS/JavaScript）
        │
FastAPI + web_compat（app.py）
        │ 认证、目录 API、任务控制、状态和导出
SQLite WAL（任务、文件状态、检查点、摘要、证据、关系）
        ├───────────────┐
普通 Worker            大数据包 Worker
worker.py              large_package_worker.py
        └───────┬───────┘
解析/证据/摘要/关系服务 → 本地 vLLM（兼容 Ollama）
```

Web 进程负责请求和编排，耗时工作由 Worker 执行。每个长任务都有持久化状态、心跳和检查点，进程重启或用户中断后可以继续。

## 普通导入流程

```text
扫描目录 → 用户选范围 → 解析文件 → 文件初步摘要
→ 节点初步摘要 → 初步情报概览 → 深度文件/节点摘要
→ 用户补充范围 → 正式情报概览 → Word/JSON/证据包
```

补充操作只增加新的选中文件，旧摘要和证据保留。继续任务只重建失败、取消或未完成的子任务；切换数据包时旧任务轮询会解绑，不会覆盖新数据包页面。

## 大数据包流程

```text
inventory → quick parse → 深度范围选择 → deep parse
→ 文件/节点摘要 → 情报概览 → JSON / DOCX 报告
```

选择页面显示完整相对路径、类型、大小、处理状态和摘要片段，可只看待补充文件。暂停后不会继续领取新文件，继续或重新选择时会同步恢复队列行和文件状态。

## 邮件关系挖掘

邮件和同构文档会抽取日期、发件人、收件人、抄送、主题、Message-ID、In-Reply-To、References、事项号、附件，以及邮箱/电话/IP/URL/案件号等实体。关系按证据强度分层：

| 关系 | 依据 |
| --- | --- |
| `reply_to` | In-Reply-To 与 Message-ID 精确匹配 |
| `references` | References 头或正文明确文号引用 |
| `same_matter` | 事项号、项目号或合同号一致 |
| `correspondence_flow` | 参与者方向、主题变体和时间顺序 |
| `shared_entity` | 多封邮件共享实体并有正文证据 |

每条边都保存来源文件、目标文件、关系类型、置信度、理由和证据定位。当前算法使用倒排索引和有界候选比较，最多派生 50,000 条关系、每个源文件 500 条边、主题候选 200,000 对；这些是保护阈值，不是邮件数量上限。几千份邮件应按批次解析、保存检查点并增量合并关系，模型集中处理关键线程，而不是一次性发送全部正文。

调查级增强方向包括实体消歧、线程聚类、附件联动、事件时间线、中心性/桥接节点分析和人工关系复核。

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web/API | Python 3.10+、FastAPI、Uvicorn、Jinja2、web_compat |
| 前端 | 原生 JavaScript、HTML、CSS |
| 持久化 | SQLite、WAL、事务队列、检查点 |
| 文档解析 | Docling、RapidOCR、pypdf、python-docx、python-pptx、openpyxl、pandas |
| 模型 | 本地 vLLM；兼容 Ollama；结构化 JSON 摘要 |
| 统计/相似度 | NumPy、scikit-learn |
| 导出 | python-docx、JSON、ZIP |
| 测试/运维 | pytest、独立 Worker、日志、进程锁、自动恢复 |

## 目录结构

```text
app.py                         Web API 和任务编排
web_compat.py                 FastAPI 兼容边界
worker.py                     普通 Worker
large_package_worker.py       大数据包 Worker
services/storage.py            SQLite 队列和状态
services/unified_parser.py    多格式统一解析
services/homogeneous_documents.py 邮件字段、实体和关系
services/evidence.py          证据选择和校验
services/reporting.py         概览上下文
services/exporter.py          Word/JSON/ZIP 导出
static/ templates/             前端资源和页面
```

## 运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -u app.py                         # Web
python -u worker.py                       # 普通任务
python -u large_package_worker.py         # 大数据包任务
```

生产环境建议使用 `requirements.lock.txt`，通过环境变量设置访问令牌、允许的数据根目录、模型地址和状态目录。数据库、原始邮件、模型权重、日志和报告不应提交到 Git。

## 下一步

1. 邮箱、姓名、签名和别名的可解释实体消歧。
2. 缺失邮件头时的线程和事项聚类。
3. 邮件附件与独立文件的哈希/内容联动。
4. 可增量重算、可撤销的关系图存储。
5. 人工确认、排除、批注和审计日志。
6. 几千份邮件批次的吞吐、内存、GPU 和失败率监控。
