# SJFX 数据分析智能体操作手册

SJFX 用来处理“一批拿回来但还不知道里面有什么”的本地资料。系统会先建立原始目录和数据概览，再生成主题目录、价值判断、问题—回答—证据链，最后把用户选中的主题、文档或证据导出为可交给整编人员/整编 Agent 的资料包。

本手册面向第一次接触项目的使用者。按照“5 分钟启动”配置后，即可在浏览器完成导入、分析、深挖、检索和导出。

当前版本已经完成四项核心加固：大资料包按 **20–50 个文件一批持续处理并断点续跑**；中文主题目录采用稳定节点身份和主归属校验；证据链升级为逐结论核验；推荐研究方向改为基于正式分析树和可回查证据的十维评分。模型只负责受控的命名、摘要和语言增强，不替代本地解析、建树、检索与证据校验。

### 本轮交互式对话与大数据包改修

- 对话质量不再用单一百分比混淆不同范围，接口和页面分别显示范围文件、候选文件、实际检查、深析完成和未检查数量；候选集结论会明确标注，不能冒充全包结论。
- 独立短问题不会因为字数少而继承上一轮；只有“上述、那这个、继续”等明确指代或延续信号才按追问处理。
- “字段值是多少”走定向证据检索；只有求和、统计、分组和跨文件计算才进入结构化聚合，避免简单问答扫描整个数据包。
- 多路检索按最终有效证据合并覆盖率，空的辅助查询不会把成功主查询降为 0；重新核验后会清理已经失效的“没有证据”或“数字无法核对”警告。
- 对话审计历史完整保留并分页加载，模型上下文只使用滚动摘要和最近轮次；回答、消息和摘要在同一事务中提交。
- 历史文档可通过后台任务直接重建对话证据索引，不依赖已经删除的原始目录。重建状态保存版本、预期/已处理/失败文档数、断点和完成时间；重建期间禁止对话读取半成品索引。
- 大包轻量预览预算在 Worker 切片之间累计，任务让出或重启后从持久化游标继续；默认关闭无条件全文后台补齐，采用“代表文件 + 用户问题/选择触发深析”。
- 自动深析达到本轮上限时会显示剩余候选数，用户可以继续深析、接受阶段性结果或缩小范围。

## 1. 你能用它完成什么

- 递归盘点服务器目录，立即显示原始目录树、文件数、体积和格式分布。
- 解析 PDF、Word、PowerPoint、Excel、CSV、JSON、图片、TXT、Markdown、HTML，以及受限解压的 ZIP/TAR 压缩包。
- 对完全相同的文件建立“规范文档—重复副本”关系；聚类、检索、证据独立来源和价值评分只计算规范文档，原始目录仍保留并标记每个副本。
- 建立 `主题 → 子方向 → 文档 → 原文证据` 的可下钻目录；自动分析时每份已解析文档只有一个计数主归属，次要主题仅作为关联引用，不会重复抬高统计数量。
- 生成内容概览、价值判断、推荐问题及可继续研究的方向。
- 形成“问题 → 谨慎回答 → 可核验结论 → 有效正文证据”的证据链，并逐项检查数字、绝对化措辞、否定关系及主客体方向。
- 从正式分析目录和当前范围的持久化检索证据中生成候选研究方向；无证据、未分类或仅有宽泛标签的主题不能成为正式推荐。
- 对候选方向按规模、主题集中度、信息丰富度、独立来源、时效、异常信号、技术影响、可成稿性、新颖性和任务相关性评分，显示分数、优先级、置信度、研究问题、方法和证据编号。
- 支持人工整理主题目录：文件可挂载到多个主题，主题可合并、拆分、改名和确认，并可撤销/恢复。
- 提供低置信度、未分类、解析失败和人工已确认的筛选视图，方便优先处理需要复核的资料。
- 对 CSV/XLSX/JSON 等结构化数据生成字段类型、缺失值、重复值、异常值、时间范围和质量评分。
- 对同构 CSV/XLSX/JSON 执行跨文件合计、加权平均、最大、最小和总行数问答，返回全部参与及排除来源；同名字段但结构不兼容时拒绝静默混算。
- 勾选多个主题、目录、文档或证据组合导出；相同源文件自动去重。
- 生成概览 Word 和包含原始资料、分析成果、覆盖率及交接说明的 ZIP 包。
- 小于 500 份规范文档使用自适应平均链接聚类；更大规模自动切换 MiniBatchKMeans，并按内容哈希持久化复用本地 Embedding 和规范证据索引。
- 大资料包的轻量预览按有界 Worker 切片遍历清点范围，深析默认以 30 个文件为一个持久化批次（允许 20–50）；每个完成文件立即保存检查点，后续按代表性、用户问题或人工选择继续晋升。

## 2. 使用前先理解三个概念

### 原始目录

原始目录完全按照服务器磁盘中的文件夹结构显示。目录盘点完成后就会出现，不需要等模型分析结束。

### 主题目录

主题目录按照资料内容重新组织。它不是磁盘文件夹，分析完成后才出现，可以逐层查看主题、子方向、文档和证据。自动分析结果把“计数主归属”和“次要主题关联”分开：每份已解析文档只能属于一个主计数组，相关的其他方向以非计数引用展示，避免一份资料在多个自动主题里重复计数。人工挂载会单独留下人工操作标记，不能与自动主归属统计混为一谈。

目录节点 ID 由稳定身份生成，不依赖中文显示名称。自动改名或模型命名不会破坏人工整理记录；成员发生实质变化时生成新的目录版本。系统会校验重复主归属、缺失成员和意外成员，并把校验结果写入分析成果。模型不可用时仍使用可解释的中文回退名称，不把文件编号、英文碎片或“相关资料”直接当成最终主题。

### 问题—回答—证据链

证据不是“和问题看起来相似的文字”。系统会排除文件名、标题、章节名、问题复述和过短主题词，只保留能够陈述事实、机制、因果、数字、影响或结论的正文；对于具体结论，还要求正文与结论存在直接概念匹配、可靠间接信号或足够强的语义关联。证据不足时系统会明确显示不足，而不是用无关片段凑数。

每条证据会尽量显示：

- 来源文件；
- 页码、章节或表格/行范围；
- 原文片段与最关键的“支撑原句”；
- 直接证据、间接证据或语义证据类型；
- `supported`、`partially_supported` 或 `insufficient` 的结论支撑状态；
- 入选原因、源文件 SHA-256、内容哈希和解析器版本；
- 可用时的页码、章节、段落/块编号、字符范围、版面坐标及压缩包成员路径。

证据校验契约当前为 `claim-evidence/3.0`。它不仅检查关键词重合，还会核对结论中的数字是否在原文出现、绝对化表述是否被原文支持、否定含义是否一致，以及“谁对谁做了什么”的主客体关系是否被反转。系统同时给出唯一证据数和独立来源数，避免同一份文件的多个片段被误当成多个独立信源。

### 推荐研究方向

推荐方向不是让模型自由发挥。系统先把正式分析树中的已分类主题作为候选，再补充当前主题范围内已经持久化的本地检索证据；没有合格正文证据的候选会被淘汰。剩余候选采用固定十维权重进行可解释排序，并返回：

- 推荐标题、总分、优先级和置信度；
- 十个评分维度及权重；
- 代表性文档、合格证据数和独立来源数；
- 可继续验证的研究问题和建议方法；
- 可直接回查的 `evidence_id` 与局限性。

本地模型可以改善标题和说明文字，但不能改变候选范围、排名、分数或证据编号。模型若给出无法由已有证据支撑的方向，系统会拒绝该结果并保留本地可解释推荐。

### 容量与三类覆盖率

资料包总量和单个内容对象上限是两个不同概念：资料包总量可以达到数十或数百 GiB，由
大量目录和文件组成；普通单文件、压缩包容器、压缩包单成员、单个压缩包累计实际解压内容
以及一次导出原始内容的硬上限统一为 10 GiB（`10737418240` 字节）。数百 GiB 资料不会
一次性装入内存或模型，而是按批清点、解析、保存检查点，并对用户选中范围按需深入。

页面和报告分别显示：

- **清点覆盖率**：约定扫描范围内是否 100% 发现；任何读取错误或文件、目录、节点、深度
  上限命中都会明确标记为不完整；
- **内容解析覆盖率**：完整解析、部分解析、失败和待处理文件各有多少；
- **语义分析覆盖率**：全文深度分析、有限语义投影和尚未深入文件各有多少。

因此“清点完成”不能写成“全文分析完成”。大资料包的首轮目标是完整清点、所有文件进入
明确终态并快速形成有界概览，重点文件再执行高精度深挖。

完整清点不能依赖默认忽略目录来“减少数量”。扫描范围内的普通、隐藏、不支持格式和敏感
扩展名文件都进入元数据清单；敏感或不支持内容可以不解析，但必须显示原因。符号链接按安全
策略不跟随并单独计数，不把链接目标偷偷算入 100%。

## 3. 运行架构

```text
浏览器
  ↓
FastAPI / Uvicorn（app.py，接收请求和返回状态）
  ↓
SQLite WAL（任务、进度、结果、检查点）
  ↓
独立 Worker（worker.py，扫描、解析、分析、报告、导出）
  ↓
本地 Ollama / 本地解析器
```

Web 与 Worker 必须同时运行。只启动 `app.py` 时页面可以打开，但分析任务不会被执行。Worker 启动时不再导入 Web 应用；只有真正领取任务时才懒加载分析执行器，因此可选 Docling/OCR 依赖损坏不会让任务队列连启动都失败。项目默认只允许一个 Worker，以免多个任务同时占用共享 GPU。

## 4. 环境要求

推荐环境：

- Linux 服务器；
- Python 3.10 或更高版本（当前服务器使用 Python 3.12）；
- 至少 8 GB 内存，处理 Docling/OCR 或大包时建议 16 GB 以上；
- 足够存放原始资料、解析侧存和导出包的磁盘空间；
- 可选的本地 Ollama，用于主题命名和深度摘要；
- 可选的本地 Docling/RapidOCR 模型，用于高精度版面、表格和 OCR。

Ubuntu/Debian 服务器建议先安装解析器所需的系统库（没有图像/PDF任务时也可以先跳过）：

```bash
sudo apt-get update
sudo apt-get install -y libgl1 libglib2.0-0 libsm6 libxext6 libxrender1
# 只有需要损坏 PDF 修复时才需要：
sudo apt-get install -y qpdf
```

`libgl1`/`libglib2.0-0` 用于 ONNX/OCR 的动态库加载；缺少它们时，普通 TXT/CSV
仍可工作，但图片或扫描 PDF 可能在导入阶段失败。生产部署应把 Python 直接依赖
和系统库一起写入镜像/运维脚本，不要在任务运行时临时联网安装。

没有 Ollama 时，扫描、基础解析、本地规则概览、目录和证据组织仍可工作，但模型增强摘要会降级。没有 Docling 离线模型时，系统会尝试其他可用解析器并标记解析覆盖情况。

## 5. 5 分钟启动

### 第一步：下载项目

使用 HTTPS：

```bash
git clone https://github.com/woshizhoulingjie/sjfx-data-analysis-agent.git
cd sjfx-data-analysis-agent
```

已经配置 GitHub SSH Key 时也可以使用：

```bash
git clone git@github.com:woshizhoulingjie/sjfx-data-analysis-agent.git
cd sjfx-data-analysis-agent
```

### 第二步：创建 Python 环境

Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.lock.txt
```

Windows PowerShell（仅建议用于开发验证）：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

安装后建议检查原生依赖是否冲突：

```bash
python -m pip check
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
python -c "import onnxruntime as o; print(o.get_available_providers())"
```

Docling 会带来 PyTorch/ONNX 等原生依赖。不要在同一个虚拟环境中随意混装不同
CUDA 版 `torch`、`onnxruntime-gpu` 或 FAISS；本项目默认使用 CPU Docling/RapidOCR，
Qwen 的 GPU 由本地 Ollama 独占。更换芯片或 CUDA 后应先单独验证上述命令。

### 第三步：创建配置

```bash
cp .env.example .env
```

生成一个随机访问 Token：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

把生成结果复制到 `.env` 的 `SJFX_API_ACCESS_TOKEN=` 后面。不要把真实 Token 提交到 GitHub。

最小的服务器配置示例：

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=qwen-agent:latest
OLLAMA_EMBED_MODEL=qwen-embed:latest

# 专用 Ollama 可以设为 1；多人共享 Ollama 时先保持 0，避免抢占他人 GPU。
ENABLE_SHARED_OLLAMA=1
ENABLE_SHARED_OLLAMA_EMBEDDINGS=0
LLM_MAX_CONCURRENCY=1

HOST=0.0.0.0
PORT=18000
AUTH_REQUIRED=1
SJFX_API_ACCESS_TOKEN=替换为刚才生成的随机Token
# 稳定的数据归属标识。以后轮换访问 Token 时不要随意修改。
SJFX_OWNER_ID=primary

# 只能分析这些目录及其子目录。Linux 多个根目录使用冒号分隔。
SCAN_ALLOWED_ROOTS=/data/incoming:/home/your-user/datasets

# SQLite/WAL 和解析临时文件放在专用本地卷，不要使用容量很小的系统 /tmp。
SJFX_STATE_DIR=/var/lib/sjfx-data-analysis-agent
SJFX_PARSE_TEMP_DIR=/var/tmp/sjfx-data-analysis-agent-parse
PARSE_TEMP_DISK_RESERVE_BYTES=1073741824

# 每个普通文件、归档容器/成员/累计解压内容和一次导出的统一 10 GiB 硬上限。
MAX_CONTENT_BYTES=10737418240
```

重要说明：

- 浏览器填写的是服务器上的绝对路径，不是你自己电脑上的路径。
- `SCAN_ALLOWED_ROOTS` 必须覆盖待分析目录，否则页面会提示路径不在白名单。
- 非回环 `HOST`（例如 `0.0.0.0`）必须显式设置 `SCAN_ALLOWED_ROOTS`，否则服务拒绝启动；只授权
  具体数据入口，如 `/data/incoming:/data/research`，不要配置整个 `/home` 或文件系统根目录。
- 绑定 `0.0.0.0` 时必须开启鉴权并设置强 Token。
- `SJFX_OWNER_ID` 是历史扫描、任务和成果的稳定归属；可以轮换访问 Token，但不要随 Token
  一起修改该值。需要迁移归属时应先备份状态库并按迁移流程处理。
- `SJFX_PARSE_TEMP_DIR` 所在卷应至少容纳一次接近 10 GiB 的受控展开和保留空间；解析器超时、
  取消或崩溃后由父进程和陈旧目录清理机制回收项目自有临时项。
- `.env` 修改后需要重启 Web 和 Worker 才会生效。

### 第四步：检查 Ollama（需要模型增强时）

```bash
ollama list
curl http://127.0.0.1:11434/api/tags
```

`.env` 中的 `OLLAMA_MODEL` 必须与 `ollama list` 显示的模型名称完全一致，包括标签。若使用专用模型，可按自己的部署方式创建或拉取；项目不会把大模型文件提交到 GitHub。

### 第五步：启动 Web 和 Worker

```bash
source .venv/bin/activate
mkdir -p logs
nohup .venv/bin/python -u app.py > logs/app.log 2>&1 &
nohup .venv/bin/python -u worker.py > logs/worker.log 2>&1 &
```

检查进程：

```bash
ps -ef | grep -E 'app.py|worker.py' | grep -v grep
```

检查接口。开启鉴权时需要携带 Token：

```bash
curl -H 'X-SJFX-Token: 你的Token' http://127.0.0.1:18000/api/status
```

看到 JSON 中的 `"ok": true` 表示 Web 正常。再确认 `logs/worker.log` 中出现 Worker 启动信息。

浏览器打开：

```text
http://服务器IP:18000
```

首次调用接口时页面会询问 `SJFX API Token`，输入 `.env` 中的 `SJFX_API_ACCESS_TOKEN`，不是 SSH 密码、GitHub Token 或模型密码。

## 6. 第一次完整操作

### 6.1 导入数据包

1. 在“服务器本地数据包目录”输入服务器绝对路径。
2. 首次建议选择“快速解析（推荐）”。
3. 若本机 Ollama 允许被本项目调用，将 `.env` 中 `ENABLE_SHARED_OLLAMA` 设为 `1`。
4. 点击“导入并分析”。
5. 页面会持续轮询后台任务，显示阶段、进度和说明。

目录盘点结束后，原始目录会优先显示；解析、去重、主题分析和概览报告继续在后台运行。不要因为原始目录已经出现就重复点击“导入并分析”。

大资料包不会只清点首批文件。系统会按有界切片为清点范围建立轻量预览、文件状态和可检索证据；深度解析默认先处理代表文件，并按用户问题、人工选择或“继续深析”请求分批晋升。停止或重启 Worker 后，预览字节预算、游标和已经完成的单文件检查点都会被复用。只有明确设置 `LARGE_PACKAGE_BACKGROUND_BACKFILL=1` 时，系统才会在后台继续尝试全量深析。

### 6.2 选择解析模式

“快速解析”适合首轮概览：

- 常见可复制文本优先快速提取；
- 扫描型 PDF 只做有界预览/OCR；
- 速度快、资源占用较低，适合大数据包首轮分析。

“高精度解析”适合重点资料：

- 使用 Docling 做版面分析；
- 加强 OCR、TableFormer 表格识别和 Office 内嵌图片识别；
- 耗时和内存占用明显更高。

大包首轮固定采用快速解析并按批落盘，以便尽快形成完整清点和有限语义概览；高精度
Docling/OCR 留给用户选中的主题、目录或文件范围。对于扫描件比例很高的资料包，应控制
`LARGE_PACKAGE_BATCH_FILES` 和 OCR 并发，避免同时占满 CPU、内存和专用临时磁盘。

### 6.3 查看原始目录和主题目录

- 点击“原始目录”：核对磁盘目录、文件层级和文件是否完整出现。
- 点击“主题目录”：按内容浏览主题、子方向、文档和证据。
- 点击任意节点：右侧显示本地摘要、成员文件、覆盖率和证据。
- 勾选节点：加入组合导出清单；点击节点本身只负责查看，不等于勾选。

自动主题目录中的数量以“主归属文档数”为准。一个文件可以显示在多个相关主题中，但自动产生的次要主题项带关联语义，不会参与该主题的文件总数、覆盖率或方向评分。人工挂载是显式调整，应结合操作记录复核。若自动目录校验发现重复、缺失或意外成员，应先查看校验状态并重新分析，不应继续把该树用于正式报告。

原始目录和主题目录都按节点分页读取。首次接口只返回根节点和一页直接子项，展开节点时才
继续取下一页；摘要和证据也独立分页。因此浏览十万级目录时不需要让接口返回一个超大 JSON，
也不会让浏览器一次创建全部 DOM 节点。页内显示的数量不能替代清点总数，应以清点覆盖率为准。

`GET /api/scan/<id>` 和 `GET /api/analysis/<id>` 默认就是 bounded 概览与浅层树；目录使用
`GET /api/tree/<id>`、摘要使用 `GET /api/summaries/<id>` 继续分页。`?full=1` 只为受控诊断
兼容旧载荷，不供前端日常调用，也不应用于大资料包。

### 6.3.1 人工整理主题目录

主题目录的自动分析结果不会被直接覆盖。人工操作会以带操作者和时间的操作记录保存，
每次读取目录时回放这些记录；重新分析后仍会保留有效的人工决定。

- **挂载文件**：把原始目录中的文件拖到主题节点；同一个文件可以挂载到多个相关主题。
- **合并主题**：把一个主题拖到另一个主题，或勾选多个主题后点击“合并”，再填写合并后的名称。
- **改名与确认**：双击主题名称可改名；右键主题可改名或标记为“人工已确认”。
- **拆分主题**：右键主题选择“拆分主题”，在弹窗中把文件拖到不同子主题，编辑子主题名称后保存；至少需要两个有文件的子主题。
- **撤销/恢复**：目录工具栏的 ↶ 和 ↷ 只针对人工目录操作，不删除历史记录。
- **筛选复核**：使用“低置信度、未分类、解析失败、人工已确认”筛选，快速定位需要人工检查的节点。

人工目录操作只改变主题归属和展示组织，不修改原始文件、解析正文或证据来源；挂载文件必须是
已经完成解析并属于当前数据包的文件。完成整理后再生成摘要或导出，导出清单会记录人工目录结果。

### 6.4 生成深度摘要

1. 在原始目录或主题目录中选择一个节点。
2. 点击“生成模型深度摘要”。
3. Worker 会仅对当前节点对应的文件范围执行任务。
4. 完成后查看问题、价值、回答、证据链及局限性。

如果当前模型不可用，系统可能返回本地保底摘要并明确标注降级。主题节点会通过 `node_id` 绑定真实成员文件，不会退回成根目录摘要。

长文档采用按 Token 预算、结构边界和少量重叠切分的 Map-Reduce 流程：先逐块执行“文档分块分析”，再执行一次“全文分块汇总”。`MAX_DOCUMENT_CHUNKS` 是告警软阈值，不是静默截断开关；只要文档仍在 `MAX_FULL_DOCUMENT_CHARS` 的允许范围内，系统会保留覆盖全文所需的全部分块，并记录块数、字符范围、预估 Token、失败块和最终汇总覆盖率。超过全文硬上限或解析器本身只能提供有限正文时，页面必须显示部分覆盖，不能标成全文完成。

### 6.5 判断证据是否有效

一条合格证据应同时满足：

- 它是正文陈述，而不是标题、目录、文件名或问题本身；
- 它能直接或可靠地间接解释当前问题；
- 它保留来源文件和位置；
- “支撑原句”能独立表达事实、机制、因果、数字、影响或结论。

查看证据链时还应检查每个结论的支撑状态：`supported` 表示现有证据通过核验；`partially_supported` 表示只有部分主张得到支持；`insufficient` 表示不能据此形成可靠结论。相同源文件中的多个片段可以提高定位精度，但不会增加独立来源数。

例如，问题“开源软件有哪些优势？”下面：

- `开源软件有哪些优势.docx`：只是文件名，不是证据；
- `开源软件的特点`：只是章节名，不是证据；
- `开源软件有哪些优势？`：只是复述问题，不是证据；
- `允许查看、修改和再分发源代码，因此能够降低采购成本并提高可定制性`：是能够回答问题的正文证据。

### 6.6 本地证据检索（RAG）

1. 先选择一个主题、目录或文档作为范围；不选择时检索整个数据包。
2. 在“本地证据检索（RAG）”输入具体问题。
3. 点击“检索证据”。

检索使用本地 BM25 和 TF-IDF，检索过程不调用生成模型。检索结果同样经过证据质量门槛，不会因为标题重复了问题词就自动排在前面。连续检索可以基于上一次结果进一步收窄。

### 6.7 结构化数据精确统计

该入口不是通用聊天，只针对已经生成画像的 CSV/XLSX/JSON 数值字段。系统会在当前范围内
寻找字段结构兼容的所有数据表并进行跨文件聚合，而不是只取第一个文件；结果会列出参与文件、
排除文件、行数/有效数和覆盖状态。结构不兼容、单位不明或某个参与表缺少计算所需统计量时，
系统会拒绝猜测。

可使用的问题示例：

- `销售额的总和是多少？`
- `订单金额的平均值是多少？`
- `最高温度是多少？`
- `记录数量是多少？`

结果会返回操作类型、字段、表/成员、数值以及来源证据。若问题无法映射到确定字段，系统会拒绝猜测。

### 6.8 重新分析当前范围

大包按有限批次持续分析全部文件。每个文件完成后立即保存检查点；中断或 Worker 重启后会跳过未变化的已完成文件并继续剩余批次。

全量任务完成后，仍可在主题目录或原始目录选中范围并重新分析。系统会复用未变化文件的检查点，只处理新增、变化或失败的文件。

系统会复用已解析文件和检查点，不会故意重复处理同一内容。
结构化 CSV/JSON 画像如果达到行数或字节上限，会标为 `partial`，仍可用于概览和
精确统计，但结果会明确带“有界采样”警告，不能把样本统计误当成全量结论。

### 6.9 重试失败文件

分析完成后若统计中存在失败文件，点击“重试失败文件”。系统仅重新处理失败项。若文件损坏、加密、格式伪装或超过资源上限，重试后仍可能失败，此时应根据日志和失败原因处理源文件。

### 6.10 导出待整编数据包

1. 在树节点左侧勾选一个或多个主题、目录、文档或证据。
2. 确认页面显示“已勾选 N 个节点”。
3. 点击“导出待整编数据包”。
4. 输入明确的整编任务主题，这是必填项。
5. 等待 Worker 打包并点击下载链接。

导出会按照源文件 SHA-256 去掉完全相同的文件，并在清单中记录被合并的原路径。ZIP 通常包含：

- 去重后的原始资料；
- 整编任务说明；
- 节点或组合摘要；
- 结论—证据链 JSON；
- 解析覆盖率清单；
- 文档索引、重复组和聚类清单；
- 本地检索结果及分析成果。

## 7. 数十/数百 GiB 资料包与单项 10 GiB 怎么使用

达到 1 GiB 或 3000 个文件时，系统默认进入大数据包模式：

- 扫描器先在配置的文件、目录、节点、单目录条目和深度安全边界内完成清点；任何边界命中
  都会设置 `truncated` 或相应计数，本次清点不得宣称 100%；
- 已清点文件按有界切片建立轻量预览和可检索状态；代表文件、问题命中文件和人工选择文件进入深析队列，默认每批 30 个；
- 完整解析结果逐文件写入 sidecar，包级内存只保留有限内容画像；
- 大包默认跳过高相似度和 embedding 语义聚类，但仍根据全部已解析文件的正文头、中、尾和章节生成内容主题树；
- 可通过 `LARGE_PACKAGE_BATCH_FILES` 调整批量大小，默认 30，代码强制范围为 20–50；共享服务器上应优先保持单模型并发和较低 CPU 解析并发，而不是盲目放大批量或并发。

```text
安全边界内的完整盘点
  ↓
有界切片完成全量轻量预览
  ↓
代表文件按 20–50 个一批深析并逐文件保存检查点
  ↓
分页展示物理树 + 有限投影生成主题树
  ↓
问题/人工选择触发高精度深挖 + 不超过 10 GiB 的组合导出
```

它的目标是让数十或数百 GiB 总资料包也能用稳定内存完成清点、渐进概览和断点续跑，
不是把数百 GiB 正文一次性送入模型。单个文件、归档容器、归档成员、单归档累计解压内容和
一次导出的硬上限统一为 10 GiB。单个文件可能因正文长度、损坏、加密或格式能力而标记为
部分覆盖或失败；实际耗时主要受文件数量、扫描件比例、OCR 页数、复杂表格、Office 图片和
磁盘 IO 影响。

常用边界配置：

```env
MAX_SCAN_FILES=50000
MAX_SCAN_DEPTH=32
MAX_SCAN_DIRECTORIES=50000
MAX_SCAN_NODES=100001
MAX_SCAN_ENTRIES_PER_DIRECTORY=50000
MAX_CONTENT_BYTES=10737418240
MAX_SINGLE_FILE_BYTES=10737418240
MAX_PARSE_SECONDS=300
SOURCE_STABILITY_SECONDS=2
MAX_WORKER_MEMORY_MB=8192
MAX_PARSE_PROCESS_MEMORY_MB=8192
ENABLE_PARSE_PROCESS_ISOLATION=1
SJFX_PARSE_START_METHOD=spawn
MAX_STRUCTURED_PROFILE_ROWS=100000
MAX_STRUCTURED_PROFILE_BYTES=268435456
MAX_STRUCTURED_JSON_RECORD_BYTES=16777216
MAX_STRUCTURED_JSON_RECORD_CHARS=16777216
MAX_EXPORT_BYTES=10737418240
EXPORT_DISK_RESERVE_BYTES=1073741824

# 大归档必须使用专用本地临时卷；超时/取消/崩溃后的项目临时项会被回收。
SJFX_PARSE_TEMP_DIR=/var/tmp/sjfx-data-analysis-agent-parse
PARSE_TEMP_DISK_RESERVE_BYTES=1073741824
PARSE_TEMP_STALE_SECONDS=21600

# 归档对象上限统一为 10 GiB，同时保留成员数、压缩比和路径深度防护。
MAX_ARCHIVE_ENTRIES=5000
MAX_ARCHIVE_FILE_BYTES=10737418240
MAX_ARCHIVE_MEMBER_BYTES=10737418240
MAX_ARCHIVE_UNCOMPRESSED_BYTES=10737418240
MAX_ARCHIVE_COMPRESSION_RATIO=200
MAX_ARCHIVE_MEMBER_PATH_DEPTH=32

# Docling/RapidOCR 默认只使用 CPU，给本地 Qwen/ Ollama 留出 GPU 显存。
DOCLING_DEVICE=cpu
DOCLING_CPU_THREADS=4
# 仅控制 CPU 文档解析池；默认 2，硬上限 8。Ollama/Qwen 推理仍为 1。
PARSE_MAX_CONCURRENCY=2

# SQLite WAL 维护（Worker 启动和周期性执行 checkpoint）。
SJFX_SQLITE_BUSY_TIMEOUT_MS=30000
SJFX_SQLITE_WAL_AUTOCHECKPOINT=1000
SJFX_SQLITE_JOURNAL_SIZE_LIMIT=67108864
SJFX_SQLITE_CHECKPOINT_INTERVAL=60

LARGE_PACKAGE_THRESHOLD_BYTES=1073741824
LARGE_PACKAGE_THRESHOLD_FILES=3000
LARGE_PACKAGE_INITIAL_PARSE_FILES=700
LARGE_PACKAGE_DEEPEN_BATCH_FILES=30
LARGE_PACKAGE_BATCH_FILES=30
LARGE_PACKAGE_BACKGROUND_BACKFILL=0
LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE=4000
LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE=6
LARGE_PACKAGE_PREVIEW_BYTES_PER_FILE=98304
LARGE_PACKAGE_PREVIEW_TOTAL_BYTES=8589934592
LARGE_PACKAGE_PREVIEW_SLICE_FILES=100
LARGE_PACKAGE_PREVIEW_SLICE_SECONDS=30
```

修改这些数值前先用真实样本压测。扫描安全边界可以根据机器和资料规模提高，但命中任何边界
都必须保持“清点不完整”标记；不能为了显示 100% 而取消所有保护。提高批量、并发或每文件
投影大小会同时增加解析时间、内存、侧存空间和模型等待时间。

### CPU 并行解析的边界

系统可以同时解析多个独立文件，但并发只发生在 Docling、文本、表格和 OCR
解析阶段。每个解析线程拥有独立的进程隔离解析器，并继续执行单文件超时和内存上限；
SQLite 结果由主 Worker 统一提交。`PARSE_MAX_CONCURRENCY` 默认是 2，最高 8，不能
简单改成“CPU 核心数减 2”，因为每个 Docling 进程可能加载数 GB 的运行时和模型。

本地 Qwen/Ollama、embedding 和主题命名不参与这个并行池，仍由共享 GPU 的串行调度器
保护。如果将 `DOCLING_DEVICE` 改为 `cuda` 或 `auto`，系统会自动把解析并发降为 1，
避免解析器与本地模型争抢 3090 显存。任务状态中的“当前阶段/当前文件”会在前端显示，
取消任务时会停止继续提交新文件，并终止正在等待的 CPU 解析子进程。

## 8. 安全配置

### Token 与访问隔离

- API 支持 `X-SJFX-Token` 或 `Authorization: Bearer ...`。
- 扫描、任务、结果和下载按稳定的 `SJFX_OWNER_ID` 隔离；Token 只负责证明访问权限。
- 浏览器只把 Token 保存在当前标签会话的 sessionStorage 中，关闭标签后不长期保留；下载时
  先换取 30–600 秒有效的一次性票据，再由浏览器导航直接流式落盘，不会把 10 GiB 成果
  整体缓冲成内存 Blob。票据默认 120 秒，可用 `DOWNLOAD_TICKET_TTL_SECONDS` 调整。
- 不同 owner 无法读取彼此的扫描和成果。
- 可选设置 `SJFX_API_TOKEN_EXPIRES_AT`（UTC epoch 或 ISO-8601 时间）让泄露的 Token
  自动失效；修改 Token 或过期时间后必须重启 Web 和 Worker。未设置时仍建议定期更换
  `.env` 中的 Token。

轮换 `SJFX_API_ACCESS_TOKEN` 时保持 `SJFX_OWNER_ID` 不变，历史扫描、任务和导出仍属于同一
逻辑用户。不要把 Token 哈希或每次生成的新随机值用作永久 owner；确需改变 owner 时先备份
SQLite/sidecar，并执行明确的数据归属迁移。

如果当前标签保存了错误 Token，按 `F12` 打开控制台并执行：

```javascript
sessionStorage.removeItem('sjfx_api_token')
```

刷新页面后重新输入正确 Token。

### 扫描白名单

`SCAN_ALLOWED_ROOTS` 限制系统只能读取明确授权的目录。Linux 多个根目录使用冒号分隔：

```env
SCAN_ALLOWED_ROOTS=/data/incoming:/data/research
```

对外绑定 `0.0.0.0` 时该配置是强制项；不要使用 `/`、整个 `/home` 或包含其他用户私有目录的
上层路径。回环开发模式虽可使用项目父目录默认值，正式部署仍建议显式给出最小授权范围。

`SCAN_IGNORED_DIRS` 和 `SCAN_IGNORED_FILES` 默认必须为空。只有经过审批的已知噪声才应显式
排除；一旦实际命中，系统会记录排除数量并把 `inventory_coverage.complete` 标为 false，
不能在验收时仍宣称 100% 清点。

Windows 使用分号分隔（例如 `C:\data;D:\research`），不会把盘符中的冒号误当成分隔符。

系统登记符号链接自身但不会跟随链接目标，并限制目录深度、文件总数、单文件体积、解析时间
和 Worker 内存。
每个真实文档默认在独立解析进程中执行；超过 `MAX_PARSE_SECONDS` 或
`MAX_PARSE_PROCESS_MEMORY_MB` 会终止该子进程并把文件标记为可重试失败，主 Worker
不会被损坏的 PDF/OCR 调用永久卡住。需要排查兼容性时可临时设
`ENABLE_PARSE_PROCESS_ISOLATION=0`，验证后应恢复为 `1`。
Linux 默认使用 `fork` 以减少子进程启动成本；如果未来在 Worker 内预加载了 CUDA/PyTorch，
建议改为 `SJFX_PARSE_START_METHOD=spawn`，避免原生运行时继承状态。

### 压缩包边界

```env
MAX_CONTENT_BYTES=10737418240
MAX_ARCHIVE_ENTRIES=5000
MAX_ARCHIVE_FILE_BYTES=10737418240
MAX_ARCHIVE_MEMBER_BYTES=10737418240
MAX_ARCHIVE_UNCOMPRESSED_BYTES=10737418240
MAX_ARCHIVE_COMPRESSION_RATIO=200
MAX_ARCHIVE_MEMBER_PATH_DEPTH=32
SJFX_PARSE_TEMP_DIR=/var/tmp/sjfx-data-analysis-agent-parse
PARSE_TEMP_DISK_RESERVE_BYTES=1073741824
```

普通源文件、压缩容器、单个成员和单个归档累计实际解压内容都受统一 10 GiB 硬上限保护；
组件环境变量只能进一步降低自己的预算。10 GiB 不是“全部直接解压到内存”，解析器会逐成员
复制到项目专用临时根并同时检查成员数、声明/实际大小、压缩比、路径深度和剩余磁盘。加密成员
会单独标记为失败或跳过，不会让同一归档中其他安全成员一起失败。达到任一边界时必须显示部分
覆盖，不能把限制改成无限。

每个压缩包都会生成成员级覆盖清单，记录成员总数、已解析、跳过、失败、截断及原因。只要一个成员未完成，压缩包和上层节点就会显示“部分覆盖”；外层节点检索仍可正确命中 `archive.zip::member.pdf` 形式的成员证据。

开始分析前应确认大压缩包已经复制完成。系统会比较扫描时与解析时的文件大小、修改时间，并对压缩包做短暂稳定性观察；若文件仍在增长，会明确标记为可恢复失败，提示等待复制完成后重新导入，而不会把不完整结果写成成功分析。

任务运行时，“数据包管理”的当前任务卡和“任务中心”都提供取消按钮。任务中心会列出当前用户全部运行、排队和取消中的任务，并显示队列位置、阻塞任务、当前文件与 Worker 心跳：

- 取消排队任务会立即生效并写入结束状态；
- 取消运行任务会先通知解析流程停止提交新文件，再由监管进程终止仍阻塞的 NAS、Docling 或 Ollama 调用；
- 已经完成的文件检查点会保留，重新提交时可以继续复用；
- 页面刷新、短暂断网或一次 502/503 不会终止任务，前端恢复连接后会自动继续轮询；
- 新的数据包导入和补充分析优先于尚未开始的可选摘要/报告任务，但不会粗暴抢占一个已经运行的任务。

扫描完成后的分析进度映射到后续区间并保持单调，不会从 15%倒退到 2%。长时间的单文件解析、模型生成和报告阶段由父 Worker 独立刷新心跳，因此“百分比暂时不变”不再等于 Worker 失联。

### 真实 1 / 5 / 10 GiB 性能验收

项目将覆盖范围分为“全量盘点、内容解析、全文深度分析”三档，不再用一个完成状态混淆不同分析深度。真实负载的固定测试矩阵、资源指标、验收门槛和命令见 [docs/PERFORMANCE_ACCEPTANCE.md](docs/PERFORMANCE_ACCEPTANCE.md)。

### 不应提交到 GitHub 的内容

```text
.env
models/
data/
outputs/
logs/
work/
wx/
vendor_packages/
```

不要在 README、Issue、日志或提交记录中写入服务器密码、API Token、模型密钥、真实保密路径或用户资料。

## 9. 停止、重启和更新

### 查看当前项目进程

```bash
ps -ef | grep -E 'app.py|worker.py' | grep -v grep
```

记录 `app.py` 和 `worker.py` 的 PID，只终止本项目进程：

```bash
kill <APP_PID> <WORKER_PID>
```

不要停止实验室共享的 Ollama，也不要使用可能误杀他人进程的宽泛命令。

### 更新代码

```bash
cd /path/to/sjfx-data-analysis-agent
git status
git pull --ff-only origin main
source .venv/bin/activate
pip install -r requirements.lock.txt
python -m pytest
```

测试通过后，再按第 5 节重新启动 Web 和 Worker。

`requirements.lock.txt` 是在受控 Linux/Python 环境中从项目直接依赖闭包生成的精确版本清单；
开发机或不同平台可先使用 `requirements.txt`。升级依赖并完成全量测试后，使用
`python scripts/generate_requirements_lock.py` 重新生成锁文件，不要直接提交整个虚拟环境的
`pip freeze` 输出。

### 查看日志

```bash
tail -f "${SJFX_STATE_DIR:-$HOME/.local/state/sjfx-data-analysis-agent}/logs/app.log"
tail -f "${SJFX_STATE_DIR:-$HOME/.local/state/sjfx-data-analysis-agent}/logs/worker.log"
```

Web 日志主要看启动、接口和端口；Worker 日志主要看扫描、解析、模型、报告和导出任务。

### 清理历史数据

历史清理默认关闭。先执行只读预览，再明确应用：

```bash
python scripts/cleanup_history.py --retention-days 30 --max-scans 100
python scripts/cleanup_history.py --retention-days 30 --max-scans 100 --apply
```

清理一个扫描会在同一数据库事务中删除它的任务、文档、主题、对话、证据和制品登记，随后
安全删除对应 sidecar 与输出文件；运行中任务不会被删除。也可以调用已鉴权的
`DELETE /api/scan/<scan_id>`。设置 `HISTORY_RETENTION_DAYS` 或 `HISTORY_MAX_SCANS` 后，Worker
会按 `HISTORY_CLEANUP_INTERVAL_SECONDS` 分批执行相同清理。迁移服务器状态目录时，应在确认
新目录完整可用后再人工处理旧的 `/var/tmp` 副本，程序不会猜测并删除未知目录。

生产部署不提供 `/docs`、`/redoc` 和 `/openapi.json`。仅在 `HOST=127.0.0.1`、
`AUTH_REQUIRED=0` 且 `ENABLE_API_DOCS=1` 的本机开发环境中开放交互式 API 文档；非回环监听
关闭鉴权会直接导致配置校验失败。

## 10. 常见问题排查

### 页面返回“未授权访问”

原因通常是浏览器 Token 与 `.env` 不一致。

1. 检查 `.env` 的 `SJFX_API_ACCESS_TOKEN`；
2. 确认 Web 和 Worker 已在修改配置后重启；
3. 清除浏览器保存的旧 Token；
4. 使用带请求头的 `curl /api/status` 验证。

### 显示“模型不存在”或“模型不属于当前用户”

本地 Ollama 场景通常是模型名不一致：

```bash
ollama list
```

把 `.env` 的 `OLLAMA_MODEL` 改成列表中完全一致的名称，然后重启两个项目进程。不要仅修改页面。

### “测试本地模型”可连接，但不能生成深度摘要

依次检查：

- `worker.py` 是否运行；
- `.env` 是否允许项目调用该 Ollama（专用实例可设 `ENABLE_SHARED_OLLAMA=1`）；
- `.env` 是否设置 `ENABLE_SHARED_OLLAMA=1`；
- `OLLAMA_MODEL` 是否存在；
- `logs/worker.log` 是否有超时、内存或模型错误。

多人共享 GPU 时，串行是有意的安全策略。它避免多个 Qwen 上下文/KV Cache 同时占满显存，不代表扫描和文件盘点都只能串行。

### 原始目录没有显示

- 确认 Worker 正在运行；
- 确认输入的是服务器绝对路径；
- 确认路径位于 `SCAN_ALLOWED_ROOTS`；
- 查看进度是否到达“目录盘点完成”；
- 检查 `logs/worker.log` 中的权限或目录深度错误。

### 进度很快结束并显示解析失败

快速失败通常不是“模型太快”，而是文件在预检阶段被拒绝。检查：

- 文件是否损坏或加密；
- 扩展名和真实格式是否一致；
- 单文件是否超过大小限制；
- Worker 当前内存是否超过上限；
- Docling 离线模型是否存在；
- 是否可以先用“快速解析”，再对重点文件做补充高精度分析。

修复源文件或配置后，点击“重试失败文件”。

### 概览 Word 生成失败

报告写入前会自动过滤原始资料中 XML 不允许的控制字符、孤立代理项和 Unicode 非字符，
不会修改原始资料本身。如果历史任务是在旧版本报告阶段失败，请重新点击“生成概览报告”
或重新执行分析；新任务会复用已经保存的解析结果，不需要重新导入原始目录。

### 单个损坏文件超时，其他文件是否会继续

会。真实文档默认在独立解析子进程中运行；达到单文件时间或内存上限后，子进程会
被终止，当前文件进入失败清单，Worker 继续处理队列。先查看失败原因，再使用“重试
失败文件”或切换快速解析模式。不要为了一个异常文件把 `MAX_PARSE_SECONDS` 和
`MAX_PARSE_PROCESS_MEMORY_MB` 无限调大。

### 证据链出现标题或问题复述

当前版本已在证据生成和本地检索两条路径统一过滤标题、章节名、疑问句和短主题短语。若旧扫描仍展示历史缓存结果，请重新执行本地完整分析；若仍出现，请记录问题文本、证据 ID、来源文件和位置，以便构造回归样本。

### Worker 提示“已有 SJFX Worker 正在运行”

这是正常保护，说明已有 Worker 持有 `data/worker.lock`。先通过 `ps` 检查现有进程，不要重复启动。

### 端口被占用

```bash
ss -ltnp | grep 18000
```

结束确认属于本项目的旧 Web 进程，或修改 `.env` 的 `PORT` 后重启。

## 11. 测试与验收

执行完整回归测试：

```bash
source .venv/bin/activate
python -m pytest
```

一次最小验收应确认：

1. `/api/status` 返回 `ok: true`；
2. 页面能输入 Token 并访问；
3. 导入目录后先显示原始目录；
4. 完整分析后出现主题目录和概览 Word；
5. 主题节点深度摘要只分析该主题成员；
6. 标题、章节名和问题复述不会进入证据链；
7. 正文证据显示支撑原句、证据类型、入选原因和来源位置；
8. 相同内容的重复文件在组合导出中只保留一个规范副本；
9. 导出清单记录被合并路径和覆盖率；
10. 重启服务后历史任务与结果仍可从 SQLite 恢复；轮换 Token 且保持 `SJFX_OWNER_ID` 后历史数据仍可访问；
11. 首次扫描/分析接口返回分页树，展开节点才能继续加载子项，分析过程中可见首版概览；
12. 清点、内容解析、语义分析三类覆盖率分别显示，不把有限投影写成全文完成；
13. 两个同构数据表可以跨文件正确求和/加权平均，异构同名字段被拒绝；
14. 加密归档成员、损坏文件和超限成员只影响自身，专用解析临时目录在失败后可回收；
15. 使用真实 1/5/10 GiB 样本完成性能、检索、检查点重入和导出验收。
16. 自动分析结果中每份已解析文档恰好有一个计数主归属；次要主题引用不增加节点文件数，目录校验没有重复、缺失或意外成员；
17. 中文主题在模型不可用时仍保持可理解名称，改名后稳定节点 ID 和人工操作记录不丢失；
18. 结论证据校验能够识别数字缺失、绝对化扩写、否定不一致和主客体反转，并返回逐结论支撑状态；
19. 推荐研究方向来自正式分析树，显示十维得分、证据数和独立来源数；无证据或未分类主题不会进入正式推荐；
20. 超过单块预算的长文档执行完整分块分析与汇总，软分块阈值不会导致静默丢弃中间正文。

回归数量以 `python -m pytest` 的当次输出为准；CI 使用相同的测试入口，并先执行 Ruff 正确性检查。真实服务器上线前仍应按本节和性能验收文档对目标机器、模型和资料类型重新验收。

完整容量验收命令、阈值和 JSON 字段见
[真实 1 / 5 / 10 GiB 性能与恢复验收](docs/PERFORMANCE_ACCEPTANCE.md)。测试脚本会独立复算
文件实际字节并拒绝稀疏样本，不允许用修改 size 元数据的方式冒充 10 GiB。

本轮代码落点、已完成项与仍待服务器实测的边界见
[V1–V3 改修结果与交付边界](docs/V1-V3改修结果.md)。

## 12. 目录与数据位置

```text
.
├── app.py                         # FastAPI/Uvicorn Web 入口
├── worker.py                      # 独立任务 Worker
├── config.py                      # 环境变量与资源边界
├── services/
│   ├── unified_parser.py          # 文档解析、OCR、压缩包处理
│   ├── package_analysis.py        # 数据包分析和主题树
│   ├── evidence.py                # 证据质量、问题匹配和结论绑定
│   ├── retrieval.py               # 本地证据检索
│   ├── structured_profile.py      # 结构化数据画像
│   ├── structured_qa.py           # 同构数据表的跨文件精确统计
│   ├── exporter.py                # Word/ZIP 导出与内容去重
│   └── storage.py                 # SQLite WAL 和压缩侧存
├── static/ 与 templates/          # 浏览器界面
├── tests/                         # 自动回归测试
├── scripts/benchmark_package.py   # 真实 1/5/10 GiB 性能与恢复验收
├── scripts/cleanup_history.py     # 历史扫描、sidecar 与输出清理（默认预览）
├── deploy/                        # systemd 示例
└── ${SJFX_STATE_DIR}/             # 生产运行状态（默认 ~/.local/state/...）
    ├── agent.db                   # 本地任务与结果数据库
    ├── document_payloads/         # 大正文压缩侧存
    ├── outputs/                   # 生成成果
    └── logs/                      # Web 与 Worker 日志
```

## 13. 当前边界

- 复杂扫描件、低清图片、超大表格和大量 Office 内嵌图片会显著增加耗时。
- 大数据包会在配置的扫描安全边界内完整清点并分批处理；命中任何文件、目录、节点、单目录
  条目或深度上限都会明确标记清点不完整。单个超长文档会在正文硬上限内完整分块分析；超过
  硬上限或解析器只能提取有限正文时仍受正文投影和格式能力约束，并明确标为部分覆盖。
- 普通单文件、归档容器、归档成员、单归档累计实际解压内容和一次导出原始内容均不超过
  10 GiB；总资料包可以更大，但必须分批分析并分范围导出。
- 大包首轮是有限语义投影，不等同于全部正文深度分析；三类覆盖率会分别标记完整、部分、
  失败和按需深入状态。
- 自动主题、价值判断和语义证据需要人工复核；精确数字结果也应核对字段含义和单位。
- 系统聚焦资料理解和分析交接，不替代最终报告责任人对事实、版权、保密和引用的审查。

仓库地址：[woshizhoulingjie/sjfx-data-analysis-agent](https://github.com/woshizhoulingjie/sjfx-data-analysis-agent)

## 14. 交互式对话与离线翻译

当前交互式对话采用“意图识别 → 有界检索 → 证据回答 → 引用校验”流程。用户可直接输入自然语言指令，例如：

```text
帮我找出数据包中关于供应链风险的文件，并总结主要问题
翻译并总结这份阿拉伯语报告
比较两份报告在时间和责任方面的差异
重新说明上一轮提到的风险
```

### 对话执行架构

```text
用户指令
    ↓
IntentRouter / AnalysisPlanner
    ↓
当前范围和多轮上下文解析
    ↓
EvidenceRetriever（BM25/FTS + 证据质量筛选）
    ↓
原文证据 + 已有中文工作译本
    ↓
本地 Ollama 回答模型 / 结构化确定性计算
    ↓
ClaimVerifier 校验、引用和覆盖率
```

对于“翻译并总结”等组合指令，系统会使用统一的 `multi_task` 流程，不会丢失检索证据或只执行其中一个任务。只有包含明确指代或延续信号的追问才继承上一轮意图和主题范围；“预算多少？”“风险有哪些？”等独立短问题会重新规划。长指令最多支持 8000 字符。

### 任务与性能边界

- 普通事实问答、翻译和简单总结走有界检索快速路径，不会对整个数据包建立批量中间结果。
- 单文件字段值和精确数字查询同样走定向检索；“多少”本身不是启动全包结构化分析的条件。
- 结构化统计、跨文件比较、时间线、关系、风险和矛盾分析才会启动深度批处理。
- 每轮使用持久化 Worker 执行，支持进度、取消、重试、补充深析和重启恢复。
- `/api/conversation/<session_id>/turns` 是标准接口；旧的 `/messages` 路由仅作兼容别名，不再绕过 Worker。
- 前端为一次逻辑发送保留同一个 `idempotency_key`，网络重试不会重复创建对话轮次。
- `conversation_messages` 保存完整审计历史，页面按页向上加载；模型只读取滚动摘要和最近完整轮次，长对话上下文保持有界。
- 页面分别展示范围、候选、实际检查、深析完成和未检查文件数；“引用核验通过”与“全范围分析完成”是两个不同状态。
- 索引状态为 `rebuilding` 或 `interrupted` 时，对话接口返回 409，并提示通过 `POST /api/scans/<scan_id>/rebuild-search-index` 完成或恢复重建。

### 离线翻译

外文文档在导入后生成中文工作译本，原文始终作为最终证据：

```text
解析/OCR
  ↓
语言检测与安全切分
  ↓
NLLB-200 distilled 600M（CTranslate2 INT8）
  ↓
译文质量检查
  ↓
SQLite document_translations + 原文/译文 sidecar
  ↓
中文工作译本供对话和大模型分析使用
```

默认配置为 `offline_nllb:600m:ct2-int8` + CPU，批量大小为 4，CPU 线程为 4，不占用共享 GPU。大数据包导入期只翻译有界工作集，全文补齐由后台任务执行。翻译失败会保留原文并标记真实状态，不会伪造完成。

### 安全运行原则

- Web 和 Worker 分别在 `sjfx-web.service` 与 `sjfx-worker.service` 中运行，默认只允许一个 Worker。
- 文档解析、OCR、翻译均限制为 CPU，共享 Ollama embedding 默认关闭。
- 数据库、译文 sidecar、模型和用户输出均不提交到 GitHub；只提交源代码、测试和部署说明。

### 对话模块验收

```bash
PYTHONPATH=. CUDA_VISIBLE_DEVICES='' OMP_NUM_THREADS=4 \
  .venv/bin/python -m pytest -q \
  tests/test_conversation.py tests/test_analysis_turns.py \
  tests/test_engineering_v2_storage.py tests/test_engineering_v2_frontend.py
```

建议额外验证阿拉伯语、泰语、印地语、中阿混合文、长指令、结构化统计、网络重试和 Worker 重启恢复。
