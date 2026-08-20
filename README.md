# 数据分析智能体

面向“拿到一批未知资料，先判断里面有什么、是否值得继续研究”的本地数据分析系统。

它不以生成一篇泛泛报告为目标，而是先建立可浏览、可追溯、可继续深挖的资料认知：从数据包概览进入主题、子方向、文档和原文证据；当用户确定关注范围后，再导出可直接交给整编 Agent 的资料包。

> 适用场景：研究资料、政策文件、项目文档、调研材料、会议资料、混合格式归档包等未知数据集合的内容概览与证据化分析。

## 核心能力

- 支持 PDF、Word、PowerPoint、Excel、图片 OCR、TXT、Markdown、CSV、JSON、HTML 等常见资料格式。
- 递归扫描目录，生成文件数量、目录数量、体积、格式分布和原始目录树。
- 统一解析正文、标题、页码/章节、表格、图片 OCR 信息与可回溯证据。
- 精确重复识别（SHA-256）、相似资料聚类、主题识别和本地证据检索。
- 生成可下钻的语义目录：`主题 → 子方向 → 文档 → 原文证据`。
- 以“关键结论 → 支撑证据”组织分析结果，而不是只罗列相关文档。
- 为物理目录、主题和子方向提供摘要、代表文件、证据链和分析覆盖信息。
- 支持选中主题、目录、文档或部分证据后组合导出；重复源文件会自动去重。
- 自动生成数据包情况概览 Word，并可导出带整编任务说明的 ZIP 交接包。

## 当前工程化与安全能力

本项目当前采用“FastAPI 模块化单体 Web + 独立本地 Worker + SQLite WAL”的单机部署架构，重点保证未知数据分析过程可控、可恢复、可追溯：

- 所有 API 和成果下载均支持 `X-SJFX-Token` 或 `Authorization: Bearer ...` 鉴权；扫描、任务和成果按令牌指纹隔离。
- 扫描目录受 `SCAN_ALLOWED_ROOTS` 白名单限制，并防止路径越界；扫描跳过符号链接，限制最大深度、文件数和单文件资源。
- ZIP/TAR 等压缩包具有成员数量、单成员大小和总解压大小上限，降低压缩炸弹风险。
- 长耗时分析采用“提交任务—后台 Worker—轮询状态”，任务记录进 SQLite，支持心跳、失败状态、取消和恢复。
- 模型生成在共享 GPU 上默认单并发，避免多个 KV Cache 同时占满显存；扫描、解析、结构化画像等非模型步骤仍按边界进行处理。
- 模型 JSON 输出使用稳健解析、最小字段校验和本地降级结果；日志滚动且不记录 Token、提示词和完整用户正文。
- 导出成果也受鉴权保护；浏览器下载会自动携带 Token，不需要把 Token 放进 URL。

扫描提交后会先展示原始目录骨架；目录盘点结束即可查看完整物理目录，后续解析和主题分析在后台继续，分析完成后仍可在“原始目录”和“主题目录”之间切换。

## 运行架构

FastAPI Web 进程只负责提交任务、查询状态和提供下载；长耗时扫描、模型调用、报告与多 GB 导出由独立 worker.py 处理。SQLite WAL 保存任务状态、进度、心跳、取消标记和检查点，项目不要求 Redis/RQ/Celery。Worker 使用 data/worker.lock 保证单实例，避免多个任务同时挤占共享 Ollama GPU。

## 分析流程

```text
扫描目录
  ↓
统一解析与证据提取
  ↓
去重、聚类与主题识别
  ↓
主题 → 子方向 → 文档 → 原文证据
  ↓
关键结论 → 支撑证据
  ↓
概览 Word / 多节点待整编交接包
```

## 主要输出

### 1. 数据包情况概览

系统自动生成 Word 概览，包含资料构成、主题目录、主要发现、推荐进一步分析方向、结论—证据链和分析边界说明。

### 2. 语义目录树

分析树不是简单的文件夹列表，而是按实际内容组织：

```text
主题
└── 子方向
    └── 文档
        └── 原文证据（页码/章节/文本片段）
```

主题和子方向节点均带有成员文件、代表资料、关键结论、证据和覆盖率信息。

### 3. 结论—证据链

每个重点结论均对应可回查的证据项，证据保留来源文件、页码或章节、原文片段、内容哈希等信息，便于人工复核。

### 4. 待整编交接包

导出的 ZIP 包可交给报告整编 Agent 或人工写作人员，主要包含：

- 去重后的原始资料文件；
- 整编任务说明；
- 节点/组合摘要；
- `结论-证据链.json`；
- `解析覆盖率清单.json`；
- 统一文档索引；
- 去重与聚类清单；
- 本地检索证据。

导出前必须填写“整编任务主题”，系统不会把自动摘要擅自当成用户任务要求。

## 大数据包模式

当数据包达到以下任一条件时，系统自动进入大数据包模式：

- 总体积达到 1 GB；或
- 文件数量达到 3000 个。

该模式不会假装已完整读完所有内容，而是采用“全量清单 + 有界首轮分析 + 按需补充”的方式：

1. 扫描全量文件、格式和体积；
2. 对有代表性的资料做首轮内容概览；
3. 明确显示已分析、待处理、失败和部分覆盖的数量及比例；
4. 用户选中感兴趣的主题或物理目录后，使用“补充分析当前范围”分批继续处理；
5. 已完成文件通过指纹和缓存复用，服务重启后可继续检查点任务；
6. 大正文以压缩侧存保存，目录重建、全局检索和导出默认读取轻量投影，避免将大量全文再次载入内存。

默认参数可在 `.env` 中调整：

```env
MAX_SCAN_FILES=50000
MAX_EXPORT_BYTES=5368709120

LARGE_PACKAGE_THRESHOLD_BYTES=1073741824
LARGE_PACKAGE_THRESHOLD_FILES=3000
LARGE_PACKAGE_INITIAL_PARSE_FILES=700
LARGE_PACKAGE_DEEPEN_BATCH_FILES=500
LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE=30000
```

### 结构化数据画像与精确问答

CSV、TSV、JSON、JSONL、XLSX 和 XLSM 会在本地生成字段类型、缺失值、重复行、数值分布、异常值、时间范围、敏感字段以及人物/地点/事件字段提示。概览中会给出质量评分、价值判断和下一步建议。

对已完成画像的数据，可以调用精确问答接口；接口只接受可验证的“合计/平均/最大/最小/数量”问题，并返回字段、表名、行范围和源文件证据：

```text
POST /api/ask
{"scan_id":"...", "question":"销售额的总和是多少？", "path":"."}
```

这里的“精确问答”是结构化数据统计，不是面向任意文本的通用聊天；没有已完成的表格画像时，接口会明确提示适用范围。

所有 API 访问都需要 `X-SJFX-Token` 或 `Authorization: Bearer ...`。新建扫描和任务会绑定当前令牌的不可逆指纹，其他令牌不能读取或取消该任务；扫描范围仍必须落在 `SCAN_ALLOWED_ROOTS` 白名单内。

### 关于 4–5 GB 数据包

当前实现已经具备面向多 GB 数据包的分层处理、覆盖率透明、缓存侧存和按需深挖机制；实际处理耗时仍取决于文件数量、PDF 是否扫描件、OCR 比例、Office 图片数量和服务器硬件。部署到新环境后，应先用真实样本进行压力测试，再根据数据特征调整首轮样本数、单批深挖数和解析模式。

## 快速开始

### 1. 环境要求

- Python 3.10+（服务器当前使用 Python 3.12）；
- 建议 Linux 服务器部署；
- 可选：本地 Ollama，用于主题命名、深度摘要和报告增强；
- 可选：本地 Docling 与 RapidOCR 模型，用于高精度文档解析和 OCR。

### 2. 创建环境并安装依赖

```bash
git clone git@github.com:woshizhoulingjie/sjfx-data-analysis-agent.git
cd sjfx-data-analysis-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. 配置环境变量

复制模板：

```bash
cp .env.example .env
```

至少确认以下配置：

```env
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=qwen-agent:latest
OLLAMA_EMBED_MODEL=qwen-embed:latest

HOST=0.0.0.0
PORT=18000
```

默认情况下，模型和文档内容在本地服务器处理。若启用云端能力，必须由使用者明确确认数据传输范围。

### 4. 准备本地模型（可选但推荐）

模型目录不随 Git 仓库提交。根据实际部署，将模型放置在：

```text
models/
├── Qwen2.5-7B-Instruct-AWQ/
├── rapidocr/
└── docling/
```

如果暂未准备模型，系统仍可完成扫描、基础解析、本地规则摘要、目录和证据组织；模型增强能力会显示为降级或未启用。

### 5. 启动服务

```bash
source .venv/bin/activate
mkdir -p .logs
nohup python -u app.py </dev/null > .logs/app.log 2>&1 &
nohup python -u worker.py </dev/null > .logs/worker.log 2>&1 &
```

扫描、深度摘要、报告和导出接口均采用“提交后轮询”：接口先返回 202 与 job_id，前端通过 /api/jobs/<job_id> 获取进度和结果。开发调试时也可以直接运行 python app.py，但生产环境必须同时运行 worker.py。

浏览器访问：

```text
http://服务器地址:18000
```

端口由 `.env` 的 `PORT` 决定；未配置时请以服务启动日志为准。

## 使用方式

1. 在页面输入服务器上的数据包目录，例如 `/data/incoming/package-a`。
2. 选择快速解析或高精度解析。
3. 点击“导入并分析”，等待目录、主题树、证据和概览 Word 生成。
4. 在“主题目录”中逐级展开主题、子方向、文档和证据。
5. 选择节点查看摘要、结论—证据链，或输入问题执行本地 RAG 检索。
6. 对大数据包中的关注范围点击“补充分析当前范围”。
7. 勾选多个主题、目录、文档或证据，点击“导出待整编数据包”。
8. 输入明确的整编任务主题，下载 ZIP 交接包。

## 项目结构

```text
.
├── app.py                       # FastAPI/Uvicorn API、任务提交与 Worker 可调用用例
├── web_compat.py                # 旧同步视图的 FastAPI 渐进迁移边界
├── worker.py                    # 独立 SQLite 任务 Worker（单实例）
├── config.py                    # 配置项
├── services/
│   ├── unified_parser.py         # 统一文档解析、OCR 与证据抽取
│   ├── agent_runtime.py          # PydanticAI 类型化 Agent 运行时边界
│   ├── package_analysis.py       # 数据包分析、主题树与结论—证据
│   ├── large_package.py          # 大数据包策略与覆盖率计算
│   ├── retrieval.py              # 本地证据检索
│   ├── exporter.py               # Word / ZIP 交接包导出
│   ├── storage.py                # SQLite 元数据与压缩侧存
│   └── reporting.py              # 概览报告生成
├── static/                       # 前端脚本和样式
├── templates/                    # 页面模板
├── tests/                        # 回归测试
├── docs/                         # 需求与方案资料
└── .env.example                  # 环境变量模板
```

## 测试

在项目根目录执行：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

测试覆盖核心解析、主题树、证据检索、导出约束、大数据包首轮分析、覆盖率、检查点复用和组合导出等关键路径。

## 数据、安全与仓库边界

本仓库只提交源码、测试、文档和配置模板。以下内容必须保留在服务器、NAS 或对象存储中，不应提交到 GitHub：

```text
.env
models/
data/
outputs/
logs/
.cache/
wx/
.codex-backups/
vendor_packages/
```

- 不要在代码、README、Issue 或提交记录中写入 API Key、密码或真实数据路径。
- 不要将模型文件、SQLite 数据库、原始数据包和导出结果提交到普通 Git 仓库。
- 导出整编包前应确认资料的保密等级、版权要求和数据传输范围。

## 已知边界

- 复杂扫描件、低清图片、大量嵌入式 Office 图片会显著增加 OCR 时间。
- 大数据包模式优先保证系统可运行和分析范围透明；首轮结果是概览，不等同于对所有文件的完整阅读。
- 主题命名可以由本地模型增强，但文件成员关系和证据选择仍尽量保持可复核、可追溯。
- 本项目聚焦数据分析与资料理解，不替代人工对关键事实、引用、保密要求和最终报告结论的复核。
