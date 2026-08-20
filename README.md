# SJFX 数据分析智能体操作手册

SJFX 用来处理“一批拿回来但还不知道里面有什么”的本地资料。系统会先建立原始目录和数据概览，再生成主题目录、价值判断、问题—回答—证据链，最后把用户选中的主题、文档或证据导出为可交给整编人员/整编 Agent 的资料包。

本手册面向第一次接触项目的使用者。按照“5 分钟启动”配置后，即可在浏览器完成导入、分析、深挖、检索和导出。

## 1. 你能用它完成什么

- 递归盘点服务器目录，立即显示原始目录树、文件数、体积和格式分布。
- 解析 PDF、Word、PowerPoint、Excel、CSV、JSON、图片、TXT、Markdown、HTML，以及受限解压的 ZIP/TAR 压缩包。
- 对完全相同的文件做 SHA-256 去重，对相似文档做聚类。
- 建立 `主题 → 子方向 → 文档 → 原文证据` 的可下钻目录。
- 生成内容概览、价值判断、推荐问题及可继续研究的方向。
- 形成“有价值的问题 → 谨慎回答 → 有效正文证据”的结论—证据链。
- 对 CSV/XLSX/JSON 等结构化数据生成字段类型、缺失值、重复值、异常值、时间范围和质量评分。
- 对结构化数据执行带字段、表名和来源定位的合计、平均、最大、最小、数量问答。
- 勾选多个主题、目录、文档或证据组合导出；相同源文件自动去重。
- 生成概览 Word 和包含原始资料、分析成果、覆盖率及交接说明的 ZIP 包。

## 2. 使用前先理解三个概念

### 原始目录

原始目录完全按照服务器磁盘中的文件夹结构显示。目录盘点完成后就会出现，不需要等模型分析结束。

### 主题目录

主题目录按照资料内容重新组织。它不是磁盘文件夹，分析完成后才出现，可以逐层查看主题、子方向、文档和证据。

### 问题—回答—证据链

证据不是“和问题看起来相似的文字”。系统会排除文件名、标题、章节名、问题复述和过短主题词，只保留能够陈述事实、机制、因果、数字、影响或结论的正文；对于具体问题，还要求正文与问题存在直接概念匹配、可靠间接信号或足够强的语义关联。证据不足时系统应明确显示不足，而不是用无关片段凑数。

每条证据会尽量显示：

- 来源文件；
- 页码、章节或表格/行范围；
- 原文片段与最关键的“支撑原句”；
- 直接证据、间接证据或语义证据类型；
- 入选原因及内容哈希。

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
pip install -r requirements.txt
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

# 只能分析这些目录及其子目录。Linux 多个根目录使用冒号分隔。
SCAN_ALLOWED_ROOTS=/data/incoming:/home/your-user/datasets
```

重要说明：

- 浏览器填写的是服务器上的绝对路径，不是你自己电脑上的路径。
- `SCAN_ALLOWED_ROOTS` 必须覆盖待分析目录，否则页面会提示路径不在白名单。
- 绑定 `0.0.0.0` 时必须开启鉴权并设置强 Token。
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

### 6.2 选择解析模式

“快速解析”适合首轮概览：

- 常见可复制文本优先快速提取；
- 扫描型 PDF 只做有界预览/OCR；
- 速度快、资源占用较低，适合大数据包首轮分析。

“高精度解析”适合重点资料：

- 使用 Docling 做版面分析；
- 加强 OCR、TableFormer 表格识别和 Office 内嵌图片识别；
- 耗时和内存占用明显更高。

建议先快速分析整个数据包，再选中重要主题、目录或文档执行“补充分析当前范围”，不要一开始就对 4–5 GB 全包执行高精度 OCR。

### 6.3 查看原始目录和主题目录

- 点击“原始目录”：核对磁盘目录、文件层级和文件是否完整出现。
- 点击“主题目录”：按内容浏览主题、子方向、文档和证据。
- 点击任意节点：右侧显示本地摘要、成员文件、覆盖率和证据。
- 勾选节点：加入组合导出清单；点击节点本身只负责查看，不等于勾选。

### 6.4 生成深度摘要

1. 在原始目录或主题目录中选择一个节点。
2. 点击“生成模型深度摘要”。
3. Worker 会仅对当前节点对应的文件范围执行任务。
4. 完成后查看问题、价值、回答、证据链及局限性。

如果当前模型不可用，系统可能返回本地保底摘要并明确标注降级。主题节点会通过 `node_id` 绑定真实成员文件，不会退回成根目录摘要。

### 6.5 判断证据是否有效

一条合格证据应同时满足：

- 它是正文陈述，而不是标题、目录、文件名或问题本身；
- 它能直接或可靠地间接解释当前问题；
- 它保留来源文件和位置；
- “支撑原句”能独立表达事实、机制、因果、数字、影响或结论。

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

该入口不是通用聊天，只针对已经生成画像的 CSV/XLSX/JSON 数值字段。

可使用的问题示例：

- `销售额的总和是多少？`
- `订单金额的平均值是多少？`
- `最高温度是多少？`
- `记录数量是多少？`

结果会返回操作类型、字段、表/成员、数值以及来源证据。若问题无法映射到确定字段，系统会拒绝猜测。

### 6.8 补充分析当前范围

大包首轮只分析有代表性的文件。当覆盖率卡片显示待处理文件时：

1. 在主题目录或原始目录选中感兴趣节点；
2. 点击“补充分析当前范围”；
3. 等待本批任务完成；
4. 必要时再次执行，直到关注范围达到所需覆盖率。

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

## 7. 4–5 GB 数据包怎么使用

达到 1 GB 或 3000 个文件时，系统默认进入大数据包模式：

```text
全量盘点
  ↓
有界首轮解析
  ↓
覆盖率和未分析范围透明展示
  ↓
按主题/目录分批补充分析
  ↓
组合导出
```

它的目标是稳定处理和透明覆盖，不宣称首轮完整阅读 4–5 GB 的每个字符。实际耗时主要受文件数量、扫描件比例、OCR 页数、复杂表格、Office 图片和磁盘 IO 影响。

常用边界配置：

```env
MAX_SCAN_FILES=50000
MAX_SCAN_DEPTH=32
MAX_SINGLE_FILE_BYTES=10737418240
MAX_PARSE_SECONDS=300
MAX_WORKER_MEMORY_MB=8192
MAX_PARSE_PROCESS_MEMORY_MB=8192
ENABLE_PARSE_PROCESS_ISOLATION=1
MAX_STRUCTURED_PROFILE_ROWS=100000
MAX_STRUCTURED_PROFILE_BYTES=268435456
MAX_STRUCTURED_JSON_RECORD_BYTES=16777216
MAX_STRUCTURED_JSON_RECORD_CHARS=16777216
MAX_EXPORT_BYTES=5368709120

# Docling/RapidOCR 默认只使用 CPU，给本地 Qwen/ Ollama 留出 GPU 显存。
DOCLING_DEVICE=cpu
DOCLING_CPU_THREADS=4

# SQLite WAL 维护（Worker 启动和周期性执行 checkpoint）。
SJFX_SQLITE_BUSY_TIMEOUT_MS=30000
SJFX_SQLITE_WAL_AUTOCHECKPOINT=1000
SJFX_SQLITE_JOURNAL_SIZE_LIMIT=67108864
SJFX_SQLITE_CHECKPOINT_INTERVAL=60

LARGE_PACKAGE_THRESHOLD_BYTES=1073741824
LARGE_PACKAGE_THRESHOLD_FILES=3000
LARGE_PACKAGE_INITIAL_PARSE_FILES=700
LARGE_PACKAGE_DEEPEN_BATCH_FILES=500
LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE=30000
```

修改这些数值前先用真实样本压测。提高首轮文件数会同时增加解析时间、内存、侧存空间和模型等待时间。

## 8. 安全配置

### Token 与访问隔离

- API 支持 `X-SJFX-Token` 或 `Authorization: Bearer ...`。
- 扫描、任务、结果和下载按 Token 的不可逆指纹隔离。
- 浏览器把 Token 保存在当前浏览器的 localStorage 中，下载时也会自动携带请求头。
- 不同 Token 无法读取彼此的扫描和成果。
- 可选设置 `SJFX_API_TOKEN_EXPIRES_AT`（UTC epoch 或 ISO-8601 时间）让泄露的 Token
  自动失效；修改 Token 或过期时间后必须重启 Web 和 Worker。未设置时仍建议定期更换
  `.env` 中的 Token。

如果浏览器保存了错误 Token，按 `F12` 打开控制台并执行：

```javascript
localStorage.removeItem('sjfx_api_token')
```

刷新页面后重新输入正确 Token。

### 扫描白名单

`SCAN_ALLOWED_ROOTS` 限制系统只能读取明确授权的目录。Linux 多个根目录使用冒号分隔：

```env
SCAN_ALLOWED_ROOTS=/data/incoming:/data/research
```

Windows 使用分号分隔（例如 `C:\data;D:\research`），不会把盘符中的冒号误当成分隔符。

系统不会跟随符号链接，并限制目录深度、文件总数、单文件体积、解析时间和 Worker 内存。
每个真实文档默认在独立解析进程中执行；超过 `MAX_PARSE_SECONDS` 或
`MAX_PARSE_PROCESS_MEMORY_MB` 会终止该子进程并把文件标记为可重试失败，主 Worker
不会被损坏的 PDF/OCR 调用永久卡住。需要排查兼容性时可临时设
`ENABLE_PARSE_PROCESS_ISOLATION=0`，验证后应恢复为 `1`。
Linux 默认使用 `fork` 以减少子进程启动成本；如果未来在 Worker 内预加载了 CUDA/PyTorch，
建议改为 `SJFX_PARSE_START_METHOD=spawn`，避免原生运行时继承状态。

### 压缩包边界

```env
MAX_ARCHIVE_ENTRIES=1500
MAX_ARCHIVE_MEMBER_BYTES=134217728
MAX_ARCHIVE_UNCOMPRESSED_BYTES=2147483648
```

这些限制用于降低路径穿越和压缩炸弹风险。压缩包超过边界时会被标记为受限或失败，不应随意把上限改成无限。

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
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试通过后，再按第 5 节重新启动 Web 和 Worker。

### 查看日志

```bash
tail -f logs/app.log
tail -f logs/worker.log
```

Web 日志主要看启动、接口和端口；Worker 日志主要看扫描、解析、模型、报告和导出任务。

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
python -m unittest discover -s tests -v
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
10. 重启服务后历史任务与结果仍可从 SQLite 恢复。

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
│   ├── exporter.py                # Word/ZIP 导出与内容去重
│   └── storage.py                 # SQLite WAL 和压缩侧存
├── static/ 与 templates/          # 浏览器界面
├── tests/                         # 自动回归测试
├── deploy/                        # systemd 示例
├── data/agent.db                  # 本地任务与结果数据库（不提交）
├── data/document_payloads/        # 大正文压缩侧存（不提交）
├── outputs/                       # 生成成果（不提交）
└── logs/                          # 运行日志（不提交）
```

## 13. 当前边界

- 复杂扫描件、低清图片、超大表格和大量 Office 内嵌图片会显著增加耗时。
- 大数据包首轮结果是透明覆盖的内容概览，不等于逐字完整阅读全部资料。
- 自动主题、价值判断和语义证据需要人工复核；精确数字结果也应核对字段含义和单位。
- 系统聚焦资料理解和分析交接，不替代最终报告责任人对事实、版权、保密和引用的审查。

仓库地址：[woshizhoulingjie/sjfx-data-analysis-agent](https://github.com/woshizhoulingjie/sjfx-data-analysis-agent)
