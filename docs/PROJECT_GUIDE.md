# SJFX 数据分析 Agent：完整系统说明与快速部署

SJFX 是一个本地运行、证据可回溯、任务可恢复的数据分析系统。它把目录扫描、多格式文件解析、摘要、智能目录、情报概览、邮件关系挖掘、翻译、结构化分析、交互式对话和报告导出组织成一个完整工作流。

本文同时作为 README 和新成员培训文档，目标是让读者能够：

- 理解系统目前已经实现的模块和模块之间的关系。
- 在一台 Linux 服务器或本地电脑上快速启动系统。
- 知道一次普通导入和一次大数据包处理会发生什么。
- 知道邮件关系挖掘目前能做到什么，几千份邮件应该怎样运行。
- 知道哪些结果可以作为证据，哪些只是待复核线索。
- 知道代码从哪里读、出问题时从哪一层排查。

## 一、系统能解决什么问题

传统做法通常是把全部资料一次性交给大模型，然后得到一段无法复核的文字。这样会遇到四个问题：文件太多导致上下文不足；一个文件失败拖垮整批；处理中断后无法继续；结论无法回到原文。

SJFX 的做法是把分析拆成可持久化的阶段和小任务：

```text
原始目录
  → 全量清单和文件指纹
  → 用户选择范围
  → 文件解析和证据索引
  → 文件摘要 / 节点摘要
  → 智能目录 / 情报概览
  → 关系挖掘 / 交互式检索
  → 研究方向和正式报告
```

每个长任务都有状态、心跳、尝试次数和检查点。用户可以暂停、继续、结束本次运行、切换数据包、补充剩余文件，已经完成的结果不会被清空。

## 二、功能模块总览

| 模块 | 当前已实现 | 适合解决的问题 | 主要入口 |
| --- | --- | --- | --- |
| 交互式对话 | 基于当前数据包、摘要和证据问答；支持检索、实体、主题和时间范围 | “这个结论来自哪里？”、“某人涉及哪些事项？” | `services/conversation.py`、`services/turn_runtime.py` |
| 文件导入与解析 | 目录扫描、多格式解析、压缩包成员、哈希和检查点 | 把散落资料变成可检索的统一文档 | `services/unified_parser.py`、`package_analysis.py` |
| 智能目录 | 原始目录、逻辑目录、主题/实体/事项节点和版本 | 看清数据包结构和资料之间的组织关系 | `services/folder_analysis.py`、`package_overview.py` |
| 摘要生成 | 文件初步摘要、深度摘要、节点摘要、跨文件概览 | 先快速浏览，再深挖重点 | `app.py`、`services/reporting.py` |
| 大数据包 | inventory、quick、deep、summary、report 隔离队列 | 数百到数千文件的可恢复分阶段处理 | `large_package_worker.py` |
| 邮件关系挖掘 | 回复链、引用链、事项链、通信流、共享实体 | 深挖邮件联系和事项网络 | `services/homogeneous_documents.py` |
| 翻译 | 本地翻译、翻译记忆、导入阶段工作译文和质量复核 | 让跨语言资料进入同一分析流程 | `services/translation.py`、`offline_translation.py` |
| 证据校验 | 原文定位、引用、哈希、支持状态和置信度 | 防止模型猜测成为正式结论 | `services/evidence.py`、`claim_verifier.py` |
| 结构化分析 | CSV/XLSX/JSON 字段、缺失、重复、异常和汇总 | 处理表格和清单型资料 | `services/structured_profile.py`、`homogeneous_documents.py` |
| 报告导出 | JSON、Word、ZIP 证据包 | 交付机器结果和人工报告 | `services/exporter.py`、`reporting.py` |
| 任务控制 | 暂停、继续、重试、补充、切换、前台优先 | 保证系统长时间运行且可控 | `worker.py`、`storage.py` |

下面按模块解释“现在做到什么”和“预期还能做到什么”。

## 三、交互式对话

### 当前实现

交互式对话不是通用聊天机器人，而是绑定当前数据包的检索和分析入口。系统会先根据用户问题确定范围，再从证据索引、文件摘要、节点摘要、关系图和已生成的概览中检索上下文。

当前可以进行：

- 按关键词、文件、目录、主题、实体、事项和时间范围检索。
- 询问某条结论的来源，并返回文件、页码、段落或证据编号。
- 询问某个人、机构、邮箱或事项涉及的文件集合。
- 对已经生成的摘要做跨文件比较和归纳。
- 在不改变原始证据的前提下生成研究方向候选。
- 保留对话轮次、上下文和失败/取消状态。

### 处理边界

对话可以解释已经落盘的证据和摘要，但不能凭空证明资料中不存在的事实。没有足够证据时，系统应返回“证据不足”或“需要补充文件”，而不是用模型常识填空。

### 预期增强

下一步可以加入对话中直接创建分析范围、保存查询视图、引用多条证据、标记待核查事项、把对话结论转成审计任务，以及对实体关系图进行自然语言切片。

## 四、文件导入与解析

### 导入范围

系统支持 PDF、DOCX、PPTX、XLSX、CSV、JSON、TXT、Markdown、HTML、图片和 ZIP/TAR 等压缩包。解析器根据格式提取文本、表格、标题、页码、段落、图片、附件和元数据；压缩包成员使用逻辑路径保存。

### 解析结果

每个逻辑文件会保留：

- 相对路径、显示名称、文件类型和大小。
- 源文件 SHA-256 和解析版本。
- 完整文本或受限制的文本片段。
- 页码、段落、表格、字符范围等定位信息。
- 解析错误、部分完成和重试状态。
- 文件摘要、实体、主题和证据索引。

原文件不会被改写。解析临时文件、数据库和缓存应放在本地磁盘，避免 NAS/NFS 锁和性能问题。

### 普通导入流程

```text
扫描目录
  → waiting_for_selection
  → 用户选择文件
  → parsing_selected
  → parsed_overview
  → preliminary_summarizing
  → preliminary_nodes
  → preliminary_overview
  → deep_summarizing_files
  → deep_summarizing_nodes
  → deep_update_available
  → deep_overview_updating
  → completed / partial
```

普通导入适合用户逐步判断：先选 20 个文件生成初步摘要，完成后从“待补充文件”中再选剩余 10 个。补充只追加新范围，已完成的摘要和证据保留。

## 五、智能目录与情报概览

系统同时维护两种目录：

1. **原始目录**：真实文件、目录、压缩包和成员的完整结构。
2. **智能目录**：按主题、实体、事项、文件类型和关系重新组织的逻辑结构。

智能目录的节点不是简单文件夹，而是带有摘要、代表性文件、关键证据、成员数量和版本的分析节点。目录生成过程中会去重、合并相似主题、保留来源路径，并标记需要人工确认的边界。

情报概览会综合：

- 当前范围内的文件和节点摘要。
- 已验证和候选关系。
- 重要实体和事项统计。
- 证据覆盖率、失败项和未完成项。
- 可继续研究的问题和建议的下一批文件。

正式概览只使用允许阶段中已经完成的结果，不把后台尚未完成的深度摘要当作正式结论。

## 六、摘要生成

### 两级摘要

| 阶段 | 作用 | 可信边界 |
| --- | --- | --- |
| 初步摘要 | 快速了解文件主题、参与者、事项和待核查点 | 用于筛选和导航，不能代替完整深度核验 |
| 深度摘要 | 读取更完整内容，补充细节、证据和跨文件联系 | 可进入正式概览，但仍必须保留原文证据 |

文件摘要之后会生成节点摘要，再生成跨文件情报概览。摘要任务按批次执行，避免一次请求超过模型上下文。模型输出使用结构化字段，便于后续检索、比较和导出。

### 模型的职责

模型负责语言归纳、主题解释、摘要和研究方向候选；扫描、解析、哈希、字段抽取、去重、证据定位和关系强证据匹配由确定性代码完成。这样模型不可用时，系统仍然可以生成目录、字段、证据和基础关系。

## 七、大数据包处理

### 为什么独立

大数据包可能包含数百到数千个文件。若和普通导入共用一个队列，长任务会阻塞用户的新操作。因此大包使用独立任务表、文件队列和 Worker。

### 阶段

```text
inventory
  → quick parse
  → 用户按文件名/目录/类型选择深度范围
  → deep parse
  → 文件摘要和节点摘要
  → 情报概览
  → JSON / DOCX / ZIP
```

页面显示完整相对路径、类型、大小、快速状态、深度状态和摘要片段。用户可以只看待补充文件，也可以查看全部文件。

### 大包的实际上限

系统对单个内容对象设置统一的 10 GiB 安全上限；默认一次大包深度批次最多 500 个文件，重型文件会自动降低批次大小。扫描、目录、压缩包成员、结构化记录和报告也有独立边界。达到边界时系统会标记清单不完整或范围受限，而不是静默丢弃文件。

大包的目标是“可持续”，不是承诺任何硬件都能在短时间内深度理解几千个文件。吞吐取决于磁盘、解析耗时、模型吞吐、显存和文件格式。

## 八、邮件关系挖掘

邮件关系是系统中的一个模块，建立在统一解析、证据索引和任务队列之上。

### 8.1 抽取的字段

对 `.eml`、`.msg`、`.mbox` 以及具有类似字段结构的文档，系统会抽取：

- 日期。
- From、To、Cc、Bcc、Reply-To。
- Subject。
- Message-ID、In-Reply-To、References。
- 文号、事项号、项目号、合同号。
- 附件名称和附件数量。
- 邮箱、电话、IP、URL、案件号等实体。
- 正文摘要、动作类型、语言和来源哈希。

### 8.2 关系构建

关系从强证据到弱证据依次构建：

1. `In-Reply-To → Message-ID`：建立高置信度 `reply_to`。
2. `References` 头或正文明确文号：建立 `references`。
3. 事项号、项目号、合同号完全一致：建立 `same_matter`。
4. 发件人/收件人方向、主题变体和时间顺序：推导 `correspondence_flow`。
5. 邮箱、电话、IP、URL、案件号等共享实体：建立 `shared_entity` 候选。
6. 每条边保存来源文件、目标文件、理由、置信度和证据片段。

`validated` 表示有明确字段或文号证据；`candidate` 表示主题、时间或共享实体等弱信号；`derived` 表示多项特征推导。共享一个名字不会直接被当作确定关系。

### 8.3 几千份邮件怎样处理

不能把几千份邮件一次性发送给模型。推荐拆成可恢复批次：

```text
A：解析邮件头、正文和附件元数据
B：抽取实体、身份线索和附件指纹
C：建立回复链、引用链和事项链
D：恢复缺失邮件头的主题/参与者/时间线程
E：只对关键线程做深度摘要、时间线和调查报告
```

当前关系算法使用倒排索引和有界候选比较，保护上限为最多 50,000 条派生关系、每个源文件最多 500 条边、主题候选最多 200,000 对。这些阈值限制派生图的规模，不限制原始邮件数量，也不删除原始证据。

### 8.4 当前可以回答什么

- 一封邮件回复了哪一封邮件。
- 哪些邮件引用同一文件或事项。
- 某事项的邮件时间顺序和参与者。
- 某个邮箱、机构或实体在哪些文件中出现。
- 哪些联系是明确证据，哪些只是候选线索。

### 8.5 调查级增强方向

还可以继续增加实体消歧、缺少 Message-ID 时的线程聚类、附件与独立文件联动、事件时间线、通信网络中心性/桥接节点分析、关系人工确认和审计日志。

## 九、翻译模块

翻译模块支持两类场景：

1. 用户对单个文档或选定内容发起翻译。
2. 导入阶段构建受限的中文工作译文，用于主题、目录和证据检索。

翻译结果会保留源语言、译文、段落对应关系、翻译版本和质量状态。导入翻译有文件数、字符数和单元数边界，避免翻译任务占满分析队列。翻译是分析辅助层，正式证据仍然保留原文；重要结论应同时展示原文片段。

## 十、证据校验与可信度

系统把证据分为原文片段、字段证据、关系证据和模型解释。每条证据尽量包含：

- 来源文件和逻辑路径。
- 页码、段落、表格、字符范围或邮件头字段。
- 原文片段和证据类型。
- 源文件哈希、解析版本和生成时间。
- `supported`、`partially_supported` 或 `insufficient` 状态。

报告中的结论必须能回到证据。无法验证的结论会降低置信度或保留为待核查问题。

## 十一、结构化数据分析

CSV、XLSX、JSON 和 JSONL 会先做结构画像：列名和类型、行数、缺失值、重复值、异常值、时间范围、唯一值分布和关联字段。多个同构文件可以做字段对齐、汇总和差异分析。

系统会保留每个统计结果的文件范围和排除原因，避免把格式不一致、重复数据或缺失字段静默纳入计算。结构化结果可以进入智能目录、实体统计、关系图和报告。

## 十二、技术栈

| 层 | 技术 |
| --- | --- |
| Web/API | Python 3.10+、FastAPI、Uvicorn、Jinja2、`web_compat.py` |
| 前端 | 原生 JavaScript、HTML、CSS |
| 解析 | Docling、RapidOCR、pypdf、python-docx、python-pptx、openpyxl、pandas |
| 邮件 | Python email 生态、字段规则、实体正则和结构化关系算法 |
| 模型 | 本地 vLLM OpenAI-compatible API；兼容 Ollama；结构化 JSON 摘要 |
| 统计 | NumPy、scikit-learn |
| 持久化 | SQLite WAL、事务队列、检查点和状态机 |
| 导出 | python-docx、JSON、ZIP |
| 运维 | 独立 Worker、进程锁、心跳、超时、日志和自动恢复 |
| 测试 | pytest、Python/JavaScript 语法检查、API 和大包回归 |

## 十三、部署环境

### 13.1 推荐环境

- Linux x86_64，Python 3.10 或更高版本。
- 普通 CPU 解析建议 8 GB 以上内存；启用 Docling/OCR 建议 16 GB 以上。
- 本地磁盘用于 SQLite、缓存、解析临时文件和报告；原始资料可以放在 NAS。
- 如果启用本地大模型，显存和模型大小决定摘要吞吐。
- 生产环境不要把 SQLite 放在 NFS/CIFS 上；项目已经支持将状态目录切换到本地磁盘。

### 13.2 从零启动

```bash
git clone https://github.com/woshizhoulingjie/sjfx-data-analysis-agent.git
cd sjfx-data-analysis-agent

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

生产服务器可以使用锁定依赖：

```bash
pip install -r requirements.lock.txt
```

复制配置并至少修改访问令牌和扫描根目录：

```bash
cp .env.example .env
# 编辑 .env：
# SJFX_API_ACCESS_TOKEN=一段随机长令牌
# SCAN_ALLOWED_ROOTS=/path/to/your/datasets
# VLLM_BASE_URL=http://127.0.0.1:8001/v1
# VLLM_MODEL=你的模型名
```

### 13.3 模型服务

生产默认使用本机 vLLM 的 OpenAI-compatible 接口：

```env
LLM_BACKEND=vllm
ENABLE_VLLM=1
VLLM_BASE_URL=http://127.0.0.1:8001/v1
VLLM_MODEL=your-model
VLLM_API_KEY=
```

没有 vLLM 时可以明确切换到兼容 Ollama 路径：

```env
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=your-model
```

模型地址默认只允许 `127.0.0.1`、`localhost` 或 `::1`，保证资料不被意外发往公网。翻译可以使用独立的本地模型配置。

### 13.4 启动三个运行单元

开发环境打开三个终端：

```bash
# 终端一：Web 页面和 API
python -u app.py

# 终端二：普通导入、摘要、对话和翻译任务
python -u worker.py

# 终端三：大数据包任务
python -u large_package_worker.py
```

默认页面地址是 `http://127.0.0.1:18000`。启用认证后，API 请求需要：

```http
Authorization: Bearer <SJFX_API_ACCESS_TOKEN>
```

生产环境可参考 `deploy/sjfx-web.service.example`、`deploy/sjfx-worker.service.example` 和 `deploy/sjfx-large-worker.service.example` 使用 systemd 管理。

### 13.5 重要配置

| 配置 | 作用 |
| --- | --- |
| `SJFX_API_ACCESS_TOKEN` | API 访问令牌 |
| `SCAN_ALLOWED_ROOTS` | 允许扫描的数据根目录 |
| `SJFX_STATE_DIR` | SQLite、缓存和检查点目录 |
| `SJFX_PARSE_TEMP_DIR` | 解析临时目录 |
| `MAX_CONTENT_BYTES` | 单个内容对象总上限，默认 10 GiB |
| `MAX_PARSE_SECONDS` | 单文件解析时间上限 |
| `MAX_WORKER_MEMORY_MB` | Worker 内存保护 |
| `LARGE_PACKAGE_BATCH_FILES` | 大包单批文件数 |
| `MAX_ANALYSIS_JOBS` | 普通队列并发数 |
| `SJFX_SQLITE_BUSY_TIMEOUT_MS` | SQLite 忙等待时间 |
| `ENABLE_TRANSLATION` | 是否启用翻译 |
| `IMPORT_TRANSLATION_MAX_FILES` | 导入阶段工作译文文件上限 |
| `JOB_*_TIMEOUT_SECONDS` | 各类任务运行时间上限 |
| `MAX_JOB_RESUME_ATTEMPTS` | 检查点自动续批次数 |

## 十四、仓库结构

```text
app.py                         API、任务编排、摘要和报告入口
web_compat.py                 FastAPI 与历史处理器兼容边界
worker.py                     普通 Worker
large_package_worker.py       大数据包 Worker
services/storage.py            SQLite 队列、状态和结果
services/unified_parser.py    多格式统一解析
services/package_analysis.py  普通数据包解析
services/large_package*.py    大包策略、隔离存储和报告
services/homogeneous_documents.py
                               邮件/同构文档字段、实体和关系
services/evidence.py          证据选择、引用和校验
services/reporting.py         情报概览上下文
services/exporter.py          Word/JSON/ZIP 导出
services/translation.py       翻译服务和翻译记忆
services/conversation*.py     对话上下文和运行时
static/                       前端资源
templates/                    页面模板
docs/                         项目说明和验收记录
tests/                        自动化测试
```

## 十五、验证和排错

代码检查：

```bash
python -m py_compile app.py worker.py large_package_worker.py services/storage.py
node --check static/app.js
node --check static/large-package.js
node --check static/large-package-result.js
PYTHONPATH=. pytest -q
```

功能验收至少覆盖：

1. 普通导入扫描、选择、初步摘要和情报概览。
2. 普通任务暂停、继续、停止和切换数据包。
3. 普通任务完成后返回并补充剩余文件。
4. 大包 inventory、quick、deep、summary 和 report。
5. 大包暂停、继续、只看剩余文件和报告下载。
6. 邮件回复链、事项链、共享实体和证据回溯。
7. 翻译结果与原文定位。
8. 对话回答的证据来源和不足提示。

遇到文件失败时，按四层排查：数据包状态 → 文件状态 → 队列状态 → 运行日志。遇到概览按钮不可用时，先检查前置摘要任务是否完成、状态机是否推进，不要直接删除数据包重来。

常见原因包括：扫描根目录未授权、文件权限不足、解析临时磁盘不足、Docling/OCR 原生依赖缺失、本地模型服务不可用、SQLite 放在网络盘、模型请求超时和历史任务处于取消中。短暂 SQLite 锁现在会保存检查点并自动重试。

## 十六、当前能力边界与路线

当前系统已经形成“目录 → 解析 → 摘要 → 证据 → 关系 → 概览 → 报告”的工程闭环。以下是明确的后续方向：

- 邮件实体消歧：合并同一人的多个邮箱、姓名、别名和签名。
- 缺失头部时的线程聚类和事项聚类。
- 邮件附件与独立文件的哈希、文号和内容关联。
- 事件抽取和可回溯时间线。
- 按人物、机构、事项、时间窗口切片的关系网络分析。
- 关系人工确认、排除、批注和审计日志。
- 关系图增量合并、撤销和局部重算。
- 几千份邮件的吞吐、内存、GPU、失败率和重试监控。

这些增强都应保持现有原则：原文保留、证据可回溯、强关系和候选线索分开、长任务可恢复、用户可以随时控制范围。

## 十七、给新成员的阅读路线

建议按以下顺序阅读代码：

1. `app.py`：路由、状态推进、摘要和报告编排。
2. `services/storage.py`：数据库、任务队列和检查点。
3. `worker.py`：任务领取、子进程监督、取消、超时和恢复。
4. `services/unified_parser.py`：格式解析和统一文档对象。
5. `services/evidence.py`：证据索引和校验。
6. `services/homogeneous_documents.py`：邮件字段、实体和关系。
7. `services/translation.py`：翻译计划、记忆和质量控制。
8. `services/reporting.py`、`services/exporter.py`：概览和报告。
9. `static/app.js`、`static/large-package*.js`：前端轮询、补充和切换。

## 十八、一句话总结

SJFX 的核心不是让模型直接猜答案，而是把海量资料拆成可恢复批次，把确定性解析、证据和关系先落盘，再让模型在受控上下文中生成摘要和解释，最后让用户可以补充、暂停、切换、复核并导出结果。
