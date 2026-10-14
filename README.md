# SJFX 数据分析 Agent：完整系统说明与快速部署

Windows/Linux 的统一启动、目录配置及本阶段验证范围见 [双平台运行说明](docs/platform-compatibility.md)。当前兼容源码版需要预先准备运行时、依赖和模型资源；完整离线安装包将在后续阶段交付。

SJFX 是一个本地运行、多用户隔离、证据可回溯、任务可恢复的数据分析系统。它把目录扫描、多格式文件解析、摘要、智能目录、情报概览、邮件关系挖掘、翻译、结构化分析、交互式对话和报告导出组织成一个完整工作流。对几万到几十万文件的大数据包，系统先生成“文件 → 内容分组 → 主题”三层智能目录，再让用户从目录里挑选资料进入普通分析和资料问答；可选的文件保护模块负责服务器与本机文件的关联、检查和审计。

本文同时作为 README 和新成员培训文档，目标是让读者能够：

- 理解系统目前已经实现的模块和模块之间的关系。
- 在一台 Linux 服务器或本地电脑上快速启动系统。
- 理解多用户隔离是怎么实现的，以及为什么安全边界收敛在一个函数里。
- 知道一次普通导入和一次大数据包处理会发生什么。
- 理解大数据包智能目录的三层架构、命名流程和提速开关。
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

每个长任务都有状态、心跳、尝试次数和检查点。用户可以暂停、继续、结束本次运行、切换数据包、补充剩余文件，已经完成的结果不会被清空。多个用户共用同一套实例，但彼此的数据在接口层和磁盘层都是隔离的。

## 二、功能模块总览

| 模块 | 当前已实现 | 适合解决的问题 | 主要入口 |
| --- | --- | --- | --- |
| 多用户与访问控制 | 注册、登录、改密、会话、按 owner 隔离、管理员切视角 | 多人共用一套系统而互不可见 | `sjfx_multiuser/` |
| 文件上传入口 | 网页分片上传、断点续传、按会话落盘到自己的目录 | 让用户不必接触服务器路径和账号 | `sjfx_multiuser/upload_routes.py` |
| 交互式对话 | 基于当前数据包、摘要和证据问答；支持检索、实体、主题和时间范围 | “这个结论来自哪里？”、“某人涉及哪些事项？” | `services/conversation.py`、`services/turn_runtime.py` |
| 文件导入与解析 | 目录扫描、多格式解析、压缩包成员、哈希和检查点 | 把散落资料变成可检索的统一文档 | `services/unified_parser.py`、`package_analysis.py` |
| 智能目录 | 原始目录、逻辑目录、主题/实体/事项节点和版本 | 看清数据包结构和资料之间的组织关系 | `services/folder_analysis.py`、`package_overview.py` |
| 摘要生成 | 文件初步摘要、深度摘要、节点摘要、跨文件概览 | 先快速浏览，再深挖重点 | `app.py`、`services/reporting.py` |
| 大数据包 | inventory、quick、deep、summary、report 隔离队列 | 数百到数千文件的可恢复分阶段处理 | `large_package_worker.py` |
| 大包智能目录 | 文件 → 内容分组 → 主题三层目录；有预算的模型命名；人工改名锁定和历史恢复 | 几万文件的资料包先看清“有哪些内容” | `services/large_package_catalog*.py`、`large_package_semantic.py` |
| 目录 → 普通包桥接 | 勾选目录节点，服务端展开校验后建普通数据包 | 从大包里挑一批资料做深度分析和问答 | `services/large_package_catalog_import.py` |
| 大包资料问答 | 按需建立的独立问答索引；中英泰混合检索；主题图 | 在未全量深读的大包里直接提问 | `scripts/large_qa_worker.py`、`services/large_qa_*.py` |
| 情报阅读面板 | 普通包和大包共用的概览面板：研究方向、研究动作、预期产出、代表资料 | 把概览从“一段文字”变成可执行的研究清单 | `static/intelligence-overview.{css,js}` |
| 邮件人物往来 | 按邮箱地址聚合往来、关系和有检查点的人物分析 | “这个人和谁来往、谈了什么” | `services/mail/people*.py`、`person_*.py` |
| 模型准入闸门 | 跨进程生成槽位、前台优先、租约回收 | 后台命名不挤占用户问答 | `services/model_dispatch.py` |
| 文件保护（可选） | 服务器/本机文件关联、被动内容检查、隔离处置、审计 | 追踪文件进出系统的履历 | `sjfx_protection/` |
| 邮件关系挖掘 | 回复链、引用链、事项链、通信流、共享实体 | 深挖邮件联系和事项网络 | `services/mail/` |
| 邮件研究台 | 自然语言检索、字段抽取、结果集收敛、证据导出 | 从几千封邮件里定位事实并保留出处 | `static/mail-research.js`、`services/mail/research/` |
| 翻译 | 本地翻译、翻译记忆、导入阶段工作译文和质量复核 | 让跨语言资料进入同一分析流程 | `services/translation.py`、`offline_translation.py` |
| 证据校验 | 原文定位、引用、哈希、支持状态和置信度 | 防止模型猜测成为正式结论 | `services/evidence.py`、`claim_verifier.py` |
| 结构化分析 | CSV/XLSX/JSON 字段、缺失、重复、异常和汇总 | 处理表格和清单型资料 | `services/structured_profile.py`、`homogeneous_documents.py` |
| 报告导出 | JSON、Word、ZIP 证据包 | 交付机器结果和人工报告 | `services/exporter.py`、`reporting.py` |
| 任务控制 | 暂停、继续、重试、补充、切换、前台优先 | 保证系统长时间运行且可控 | `worker.py`、`storage.py` |

下面按模块解释“现在做到什么”和“预期还能做到什么”。

## 三、多用户隔离与访问控制

这一层是整个系统最近一次大改造的结果，也是改动风险最集中的地方，因此单独成章。

### 3.1 账号体系

| 能力 | 实现 |
| --- | --- |
| 注册 | `POST /register`，用户名/密码强度校验，按来源 IP 做注册频率限制 |
| 登录 | `POST /login`，成功后下发会话 cookie `sjfx_session` |
| 登出 | `GET/POST /logout`、`POST /api/auth/logout`，会话立即作废 |
| 当前身份 | `GET /api/auth/me` |
| 自主改密 | `POST /api/auth/change-password` |
| 强制改密 | 管理员重置密码后置 `must_change_pw`，服务端强制跳转改密页（不是仅前端拦截） |
| 密码存储 | `werkzeug.security` 的 scrypt（参数 `32768:8:1`），零新增依赖 |
| 会话 | 服务端会话表 + 有效期 + 机器指纹 + 可被管理员批量吊销 |

角色只有两种：`admin` 和 `user`。第一个管理员由 `UserStore.bootstrap_first_admin()` 在初始化时创建，`owner_id` 固定为 `primary`。

页面：`/login`、`/register`、`/change-password`、`/admin`；用户栏脚本由 `/auth-static/userbar.js` 提供。

### 3.2 归属隔离模型：一个纯函数

`app.py` 里有 60 处调用 `_request_owner_id()`。把这 60 处同时改对，靠的不是耐心，而是把决策收敛进一个纯函数：

```python
# sjfx_multiuser/ownership.py
resolve_owner(session, viewing_owner, config_owner_id) -> OwnerDecision
```

它不依赖 request 对象，因此可以脱离框架直接测试。三条铁律（改代码前必读）：

1. **绝不从客户端输入取 owner。**`?as_owner=`、请求头、cookie 里的 owner 一律不可信，只从服务端会话派生。`OwnerDecision.source` 会记录来源是 `session`、`view_as` 还是 `config_fallback`，便于排查“为什么看到了/看不到”。
2. **`None` 表示“不过滤”，也就是能看全部。**这是 `services/storage.py` 的既有约定（`scan_owned` 里 `not owner_id or ...`）。因此**任何意外返回 `None` 都等于越权**，这是本模块最危险的失败模式，每个分支都有对应测试。
3. **无会话时回落到配置值，而不是 `None`。**回落成 `None` 会让所有脚本和后台任务变成“看全部”；回落到 `Config.OWNER_ID`（`SJFX_OWNER_ID`，默认 `primary`）与改造前行为一致，零回归。

### 3.3 磁盘隔离

导出产物按归属分目录存放：`outputs/<owner>/…`，旧的平铺链接通过 `outputs/legacy/` 兼容。上传暂存区同样按归属分层：`outputs/_uploads/<owner>/…`。

### 3.4 管理员与切视角

管理员默认拥有全局视角，也可以“切成某个用户”来复现对方看到的内容：

| 接口 | 作用 |
| --- | --- |
| `GET /api/admin/users` | 用户列表 |
| `POST /api/admin/users` | 建用户 |
| `POST /api/admin/users/<user_id>/password` | 重置密码（可要求强制改密） |
| `DELETE /api/admin/users/<user_id>` | 停用/删除用户 |
| `GET/POST /api/admin/view-as` | 查看或切换当前视角 |

切视角是**服务端会话状态**，不是客户端参数——这正是 3.2 铁律 1 要防的东西。

### 3.5 路由归属审计闸门

`sjfx_multiuser/route_audit.py` 把“每个带 id 的接口都要校验归属”从人工纪律变成自动检查：

```bash
python sjfx_multiuser/route_audit.py            # 默认检查 app.py
python sjfx_multiuser/route_audit.py 路径/app.py
```

它分两层：

1. **模式层**：扫描每个处理函数，看有没有归属过滤写法（`_request_owner_id()`、`_effective_owner()`、`scan_owned()`、`require_scan()`、`_require_admin()`、`_large_package_owned()`、`owner_id=` 传参）。
2. **清单层**：模式没命中的必须出现在 `KNOWN_SAFE_DELEGATORS` 里，**且必须写明原因**。

之所以不做成“启动即失败”，是因为实测有 11 条带 id 路由的文本里不含归属写法但实际安全（例如 9 条大包路由走 `_large_package_owned()`，另有 2 条是纯转发，校验在被调函数内部）。把误报做成启动条件，只会逼人把闸门关掉。**误报比不检查更糟**——所以用“证据 + 显式清单”。

当前闸门的实测结果（2026-09-29，对这个 app.py）：

```text
审计对象: app.py
  路由总数: 99
  带 id 参数: 77
  模式层判定有守卫: 72
  清单层放行（纯转发）: 5
  未被覆盖: 0
结论: PASS
```

邮件人物往来接口（`services/mail/people_api.py`）和文件保护接口（`sjfx_protection/`）不在 `app.py` 里注册，它们各自在模块内部按真实会话校验归属。

### 3.6 认证相关的安全默认值

`config.py` 在启动时强制下列约束，不满足直接拒绝启动：

- 绑定非回环地址时，`AUTH_REQUIRED` 必须为真（`AUTH_REQUIRED` 在回环地址上默认 `0`，其他情况默认 `1`）。
- `AUTH_REQUIRED=1` 时必须配置 `SJFX_API_ACCESS_TOKEN`，否则报错退出。
- 启用认证时自动关闭交互式 API 文档。
- 模型地址默认只允许 `127.0.0.1`、`localhost`、`::1`，资料不会被意外发往公网。

请求鉴权分三条路：

| 路径 | 鉴权方式 |
| --- | --- |
| `/api/auth/*`、`/api/admin/*` | 会话 cookie；`/api/admin/*` 额外要求 admin 角色 |
| `/api/uploads/*`、`/upload` | 会话 cookie（见第四章） |
| `/outputs/...?ticket=…` | 一次性下载票据，校验后立即作废 |
| 其余 `/api/*` | `X-SJFX-Token` 或 `Authorization: Bearer <SJFX_API_ACCESS_TOKEN>` |

已知边界：`document_translations` 表按设计**没有 `owner_id` 列**，`storage.list_translation_page()` 只按 `scan_id` 查询。因此 `/api/translations/<scan_id>` 的归属校验必须在路由层补：当前实现在鉴权中间件里对该路径单独校验 `scan_owned()`，不属于当前用户时统一返回 404（而不是 403，避免泄露“这个 id 存在”）。这是“表结构没带归属”的接口的通用处理方式，新增同类接口时请照此办理。

## 四、分片上传入口

上传解决的是“用户不需要碰服务器账号和路径”，它与扫描、分析、审计是并行的两条线：文件用 scp 传进来还是网页传进来，系统内的处理链路完全一样。

| 接口 | 说明 |
| --- | --- |
| `GET /upload` | 上传页面 |
| `POST /api/uploads` | 开始一次上传会话（声明文件清单） |
| `GET /api/uploads/<session_id>` | 查询进度，用于断点续传 |
| `PUT/POST /api/uploads/<session_id>/chunks/<int:file_index>` | 上传单个分片 |
| `POST /api/uploads/<session_id>/finalize` | 合并分片并落盘 |
| `DELETE /api/uploads/<session_id>` | 中止并清理 |

行为约定：

- 落盘根目录为 `outputs/_uploads/<owner>/`，目标必须落在 `SCAN_ALLOWED_ROOTS` 之内，否则拒绝。
- **单个分片必须 ≤ 4 MB**：`app.py` 的 `max_content_length` 是 `4 * 1024 * 1024`，超过会被框架直接拒绝。
- 鉴权走会话 cookie，未登录一律 401；不允许用全局 token 代替，因为浏览器无法安全携带它。
- 上传完成后产物就是普通的待扫描目录，后续走标准扫描流程。

## 五、交互式对话

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

## 六、文件导入与解析

### 导入范围

系统支持 PDF、DOCX、PPTX、XLSX、CSV、JSON、TXT、Markdown、HTML、图片、邮件和 ZIP/TAR 等压缩包。解析器根据格式提取文本、表格、标题、页码、段落、图片、附件和元数据；压缩包成员使用逻辑路径保存。

旧版 Office 格式也走同一条统一解析链：DOC 用 antiword，PPT 按当前修订读取 OLE 幻灯片文本，XLS 用 xlrd（保留工作表、行位置和公式缓存值，不重算公式），RTF 用 striprtf。原生读取进程有时间、地址空间和输出上限，不执行宏。安装：`bash scripts/install_legacy_readers.sh`，组件装在项目 `.tools/legacy-office` 和虚拟环境里，不改系统包。详见 `docs/legacy-import-and-catalog-speed.md`。

普通包并行解析时优先提交较大的文件（`PARSE_LARGEST_FIRST=1`，结果仍按原顺序交付），文档向量按内容哈希去重，相同内容只请求一次。

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

## 七、智能目录与情报概览

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

普通数据包和大数据包共用一个情报阅读面板（`static/intelligence-overview.{css,js}`）。推荐方向会列出核心研究问题、推荐理由、研究动作、预期产出、代表资料和支撑原文，只展示简报实际给出的内容，旧简报不补造。深度解析和快速解析分开显示，未知值显示“未知”，不用零值冒充完成。同内容轮询不重建面板，展开状态和焦点保留。详见 `docs/performance-intelligence-20260926.md`。

普通数据包的智能目录（`services/folder_analysis.py`）与大包的三层目录是两套实现：普通包目录跟随导入流程生成，大包目录见上文“大数据包智能目录”一章。

## 八、摘要生成

### 两级摘要

| 阶段 | 作用 | 可信边界 |
| --- | --- | --- |
| 初步摘要 | 快速了解文件主题、参与者、事项和待核查点 | 用于筛选和导航，不能代替完整深度核验 |
| 深度摘要 | 读取更完整内容，补充细节、证据和跨文件联系 | 可进入正式概览，但仍必须保留原文证据 |

文件摘要之后会生成节点摘要，再生成跨文件情报概览。摘要任务按批次执行，避免一次请求超过模型上下文。模型输出使用结构化字段，便于后续检索、比较和导出。

### 模型的职责

模型负责语言归纳、主题解释、摘要和研究方向候选；扫描、解析、哈希、字段抽取、去重、证据定位和关系强证据匹配由确定性代码完成。这样模型不可用时，系统仍然可以生成目录、字段、证据和基础关系。

## 九、大数据包处理

### 为什么独立

大数据包可能包含数百到数千个文件。若和普通导入共用一个队列，长任务会阻塞用户的新操作。因此大包使用独立任务表、文件队列和 Worker。

### 阶段

```text
inventory（流式盘点，1%～19%）
  → quick_parse（有预算的内容画像：字段、标题、关键词、代表片段，20%～65%）
  → 语义向量与内容分组，生成智能目录（65%～68%）
  → recognize（对识别不足的文件补一次有预算的采样/OCR）
  → catalog_ready（70%，用户即可按目录/文件名/类型选择；模型命名在后台继续）
  → deep_parse（只处理选中的文件）
  → 文件摘要和节点摘要 → 情报概览
  → report_ready（100%，JSON / DOCX / ZIP）
```

页面显示完整相对路径、类型、大小、快速状态、深度状态和摘要片段。用户可以只看待补充文件，也可以查看全部文件。进入“大包目录 → 智能目录 → 补充内容识别”可以继续处理识别不足的文件，旧任务也从这里升级。

### 长任务的资源背压

大包 worker 在每个调度周期检查状态盘、解析临时盘、Python 临时目录和 SQLite 临时目录的余量（至少 1 GiB）。磁盘、内存、文件句柄不足时进入 30 秒退避，已领取的队列项退回 retry 并退还本次的尝试次数，空间恢复后自动继续。资源故障不会被记成“文件解析失败”，也不会生成空的成功预览。详见 `docs/engineering-large-package-backpressure.md`。

### 压缩包成员

大包清点区分真实文件（`source_kind=physical_file`）和已核验的压缩包成员。成员清单先落到私有 SQLite，再整体发布；容器变化、重名或扫描失败时不会发布半套成员。导入时只相信来源库的标记，不相信前端传入的标记。详见 `docs/engineering-large-archive-members.md`、`docs/engineering-real-file-handoff.md`。

### 大包的实际上限

系统对单个内容对象设置统一的 10 GiB 安全上限；默认一次大包深度批次最多 500 个文件，重型文件会自动降低批次大小。扫描、目录、压缩包成员、结构化记录和报告也有独立边界。达到边界时系统会标记清单不完整或范围受限，而不是静默丢弃文件。

大包的目标是“可持续”，不是承诺任何硬件都能在短时间内深度理解几千个文件。吞吐取决于磁盘、解析耗时、模型吞吐、显存和文件格式。

## 十、大数据包智能目录：三层架构

大包智能目录只作用于独立大包（`large_package.db` 和大包 worker）。它不改写源文件、普通导入和已保存的深度摘要。

### 三层结构

```text
第一层  文件        全量盘点 + 有界内容画像（字段 / 标题 / 关键词 / 代表片段）+ 384 维语义向量
          ↓ k-means 语义聚类（大小自适应；正文太短、拿不到向量的文件单独成组）
第二层  内容分组    叶组；模型根据组内代表材料命名，名称存在组级表里
          ↓ 按分组向量再聚类，主题数量由轮廓系数（silhouette）自适应选出
第三层  主题        版本化的多级导航树；每个叶组只有一个父主题
```

几条不变的规则：

- 命名和主题归纳只改名字和上层导航，从不改变叶组里有哪些文件。
- 模型名称存在组级表里，重命名不修改每个文件；同名组不合并。
- 名称只来自内容，不用文件名推断主题。
- “没有共同主题”“证据不足”“只会说泛化词”按状态处理，统一成 `内容主题不统一（待细分）`、`证据不足（待补充内容）`、`名称过于泛化（待细分）`，并标记 needs_review，不冒充目录名。
- 人工改名会锁定，自动命名不会覆盖；可以从历史版本恢复。
- 目录节点编号稳定，主题在两版之间按成员重合度（Jaccard ≥ 0.5）继承名字，避免刷新后名字跳变。

### 命名流程

1. **取证据**：每组先取中心、不同区域和边界上的材料，再按 MMR 去重补足，每份证据带上预览阶段算好的字段、关键词和章节标题；在超过 30% 文件里重复出现的短行被当作页眉页脚模板剔除；HTML/XML 源码先剥离。
2. **调模型**：单组或多组一批，vLLM 后端下输出受 JSON Schema 约束，输出 token 有上限。实测输入约 0.7 ms/token（命中前缀缓存后接近 0），输出约 22 ms/token，所以证据可以给足，输出必须锁死。
3. **校验**：名称要能被引用证据支持，还要用没参与起草的“留出成员”复核；不通过的标记待复核。
4. **记账**：每轮有生成请求数和 token 预算，调用前原子预留，完成后按实际用量核销；模型不可用时按“依赖不可用”长退避，不烧掉重试额度。
5. **不阻塞**：内容分组和抽取式名称就绪后即进入 70%，用户可以开始选择；模型命名在后台继续。

深度摘要或内容变化后，只有受影响的组重新排队（`lp_catalog_changes` 触发器 + `repair_changed` 增量修复），不整包重做。

### 提速开关

下表的开关默认都保持改造前的行为，打开后可以随时关掉回退。生产参考值是 2026-09-29 在单卡 RTX 3090 + Qwen3.6-27B 上实测后采用的配置。

| 环境变量 | 默认 | 生产参考 | 作用 |
| --- | --- | --- | --- |
| `SJFX_GENERATION_CONCURRENCY` | `1` | `3` | 跨进程生成槽位数，必须 ≤ vLLM `--max-num-seqs` |
| `LLM_MAX_CONCURRENCY` | `1` | `3` | 单进程内模型并发上限 |
| `SJFX_LP_NAMING_PARALLEL` | `0` | `1` | 大包 worker 命名走并行路径 |
| `SJFX_LP_NAMING_SAME_PACKAGE_LANES` | `1` | `2` | 同一个包同时跑几路命名 |
| `SJFX_LP_LEAF_NAME_INHERIT_JACCARD` | `0`（关） | `0.9` | 重新聚类后成员基本没变的叶组直接沿用旧名，不再调模型 |
| `SJFX_LP_CACHE_KEY_UNIFIED` | `0` | `1` | 统一命名缓存键，提高缓存命中 |
| `SJFX_GENERATION_BG_BORROW_IDLE_SECONDS` | `0`（关） | 不设 | 前台空闲多久后，后台任务可以借用前台保留槽 |
| `SJFX_LP_NAMING_COMPACT_OUTPUT` | `0` | 不设 | 紧凑输出格式；实测截断和待复核变多，暂不启用 |
| `SJFX_LP_CATALOG_NAMING_OUTPUT_TOKENS` | 代码内默认 | 不设 | 命名输出 token 预算 |

生产参考配置在 78 组的测试包上把命名从每组 5.88 秒降到 5.41 秒（约 −8%），前台问答首字延迟 p95 为改动前的 1.05 倍。把 vLLM 的 `--max-num-batched-tokens` 从 256 提到 512 没有吞吐收益，首字延迟变差，KV 缓存容量几乎减半，所以保持 256。这些是单次运行的结果，5% 以内的差异可能是噪声。

### 只读的体检与验收工具

| 脚本 | 作用 |
| --- | --- |
| `scripts/catalog_health.py [--bad-only] [--include-cancelled]` | 所有包的目录是否健康：待归并叶组、无名叶组、空转修复等 |
| `scripts/catalog_speed_stats.py --days 2 --jaccard` | 各路径调用数、token、截断率、缓存未命中原因；用来定提速阈值 |
| `scripts/catalog_acceptance_sample.py` | 分层抽样生成人工盲评卡，零模型调用 |
| `scripts/catalog_boundary_audit.py` | 找出疑似“分错组”的混合组给人看，不自动修 |
| `scripts/catalog_name_support_audit.py` | 用留出成员定向验证名称是否有支撑 |
| `scripts/catalog_evidence_ab.py` | 改动前后的取文方式各命名一次，并排对比 |

这些脚本只读打开生产库（`mode=ro`），不写数据。规格与历次实测见 `docs/large-catalog-model-v2.md`、`docs/large-catalog-evidence-l1-l3.md`、`docs/isolated-large-content-recognition.md`。

## 十一、从智能目录导入普通数据包

大包目录负责“看清有什么”，普通数据包负责“深入分析”。两者之间的桥接是：

```text
在智能目录里勾选节点
  → POST /api/large-packages/<id>/catalog/import
  → 服务端展开节点、校验归属、源文件指纹和排除列表
  → 一个事务里建 scan / 选择范围 / 导入任务 / 分析任务 / 桥接审计
  → 普通 worker 接手解析、摘要、检索和问答
```

选择快照、原包标识、源文件指纹和幂等请求都会保留，大文件不重复复制。选中文件的全部父目录会一起建立，未选中的兄弟文件不会被带进来。导入记录在 `GET /api/large-packages/<id>/catalog/imports` 查看。实现见 `services/large_package_catalog_import.py`。

## 十二、大数据包问答的可复核流程

大数据包问答必须满足以下顺序，任何一步失败都要在回答中披露，而不是静默降级：

```text
当前大包与会话
  → 问题范围解析
  → 大包摘要/目录/文件索引强检索
  → 去除空摘要、失败摘要、纯元数据
  → 相关片段按文件聚合和限量
  → vLLM/Qwen 根据“问题 + 检索内容”生成答案
  → 记录实际引用文件与证据
  → 页面按实际证据数量渲染
```

“强检索”表示允许同义词、主题、实体和摘要信号参与召回，目标是找到足够相关的文件，不要求字面完全匹配；它不表示把整个大包无差别发送给模型。上下文仍受批量、字符数和 token 限制，以保证稳定性。

回答下方的证据条数是动态集合：有多少实际参与回答的文件就显示多少；检索结果少时不会用无关文件凑数，检索结果多时也不会被旧的 16 条展示上限截断。文件列表中的“范围文件”是任务范围统计，不能直接当成回答引用。

### 问答索引旁路进程

大包问答的检索索引由独立的 `scripts/large_qa_worker.py` 维护。它是单写者、可随时重启的旁路进程：只读打开 `large_package.db`，把可丢弃、带版本的索引写到 `LARGE_QA_INDEX_DIR`（默认 `<状态目录>/large_qa`），从不修改源数据。

- **按需建索引**（`LARGE_QA_DEMAND_ONLY=1`，默认）：只处理最近在对话里用过的包，以及被提问点名、还有待解析文件的包。旧实现每 120 秒遍历全部包，1.5 天读了 337 GB 磁盘，覆盖率只有 7%。
- **混合检索**：中文二元词、英文数字分词、向量和主题材料用 RRF 融合，先按目录/文件授权过滤再排序，并如实报告覆盖范围。
- **泰文**：独立的 `search_thai` 表，用 PyThaiNLP `newmm-safe` 固定词典分词，不下载模型、不调外部服务；原有检索表不动。详见 `docs/engineering-multilingual-qa.md`。
- **主题图**：可续跑的主题地图，保留精确来源路径；可选本地模型润色（`LARGE_QA_REFINE_TOPICS`）。
- **资源背压**：磁盘不足时退避，不降低文档质量。

手动运行：

```bash
.venv/bin/python scripts/large_qa_worker.py              # 常驻
.venv/bin/python scripts/large_qa_worker.py --once       # 跑一轮
.venv/bin/python scripts/large_qa_worker.py --package lp-xxx --summaries-only
```

`LARGE_QA_ENABLED=0` 可整体关闭。

## 十三、大数据包情报概览的两个阶段

```mermaid
stateDiagram-v2
    [*] --> 初步概览: inventory/quick 有可用摘要
    初步概览 --> 深度分析中: 用户选择文件或节点
    深度分析中 --> 深度概览: 深度摘要落盘
    深度概览 --> 深度概览: 新批次完成后自动刷新
    初步概览 --> 初步简报: 允许下载
    深度概览 --> 正式简报: 允许下载
```

初步概览至少包含资料范围、可用摘要数、主题/目录信号、初步研究方向和限制说明。深度概览在相同数据包 ID 下更新版本号，引用新的摘要和证据统计。前端使用轮询和缓存失效机制获取新版本，用户无需重新创建任务。

## 十四、邮件关系挖掘

邮件关系是系统中的一个模块，建立在统一解析、证据索引和任务队列之上。

### 14.1 抽取的字段

对 `.eml`、`.msg`、`.mbox` 以及具有类似字段结构的文档，系统会抽取：

- 日期。
- From、To、Cc、Bcc、Reply-To。
- Subject。
- Message-ID、In-Reply-To、References。
- 文号、事项号、项目号、合同号。
- 附件名称和附件数量。
- 邮箱、电话、IP、URL、案件号等实体。
- 正文摘要、动作类型、语言和来源哈希。

### 14.2 关系构建

关系从强证据到弱证据依次构建：

1. `In-Reply-To → Message-ID`：建立高置信度 `reply_to`。
2. `References` 头或正文明确文号：建立 `references`。
3. 事项号、项目号、合同号完全一致：建立 `same_matter`。
4. 发件人/收件人方向、主题变体和时间顺序：推导 `correspondence_flow`。
5. 邮箱、电话、IP、URL、案件号等共享实体：建立 `shared_entity` 候选。
6. 每条边保存来源文件、目标文件、理由、置信度和证据片段。

`validated` 表示有明确字段或文号证据；`candidate` 表示主题、时间或共享实体等弱信号；`derived` 表示多项特征推导。共享一个名字不会直接被当作确定关系。

### 14.3 几千份邮件怎样处理

不能把几千份邮件一次性发送给模型。推荐拆成可恢复批次：

```text
A：解析邮件头、正文和附件元数据
B：抽取实体、身份线索和附件指纹
C：建立回复链、引用链和事项链
D：恢复缺失邮件头的主题/参与者/时间线程
E：只对关键线程做深度摘要、时间线和调查报告
```

当前关系算法使用倒排索引和有界候选比较，保护上限为最多 50,000 条派生关系、每个源文件最多 500 条边、主题候选最多 200,000 对。这些阈值限制派生图的规模，不限制原始邮件数量，也不删除原始证据。

### 14.4 当前可以回答什么

- 一封邮件回复了哪一封邮件。
- 哪些邮件引用同一文件或事项。
- 某事项的邮件时间顺序和参与者。
- 某个邮箱、机构或实体在哪些文件中出现。
- 哪些联系是明确证据，哪些只是候选线索。

### 14.5 调查级增强方向

还可以继续增加实体消歧、缺少 Message-ID 时的线程聚类、附件与独立文件联动、事件时间线、通信网络中心性/桥接节点分析、关系人工确认和审计日志。

## 十五、邮件研究台

邮件工作台 `/mail-research` 支持在 EML/ZIP/目录/MBOX 中以自然语言查找内容、抽取带原文证据的字段、在上一结果集内继续筛选，以及导出研究结果。语义索引采用独立邮件向量表，可配置单独的本机 CPU E5 服务；关闭 `MAIL_ENABLE_VECTOR_INDEX` 即回到字面检索。扫描附件可在隔离进程中进行泰文/英文 OCR。模型不可用或资料未处理时，页面保留可回查结果，并披露覆盖缺口。

所有新增 API 继续校验资料包所有者。精确回复链只来自邮件引用头；同主题关系明确标记候选。附件相同内容由 SHA256 判断，同名家族的版本序号仅表示时间顺序。字段抽取保存原文、证据位置和归一化状态，无法确定的日期保持未知。使用说明、隔离安装和回退见 `docs/mail-mining.md`。

### 人物往来

研究台新增“人物”视角（`static/mail-people.{css,js}`）：按邮箱地址聚合某人收发的邮件和往来关系，不凭姓名推断身份。人物分析按批次读取全部相关邮件并保存检查点，中断后可以续跑；最终综述滚动汇总每一批已保存的证据，而不是只看排名靠前的几封。多语言资料按语言平衡选取检索词。

| 接口 | 作用 |
| --- | --- |
| `GET /api/mail/packages/<package_id>/people` | 人物列表 |
| `GET /api/mail/packages/<package_id>/person/messages` | 某人的往来邮件 |
| `GET /api/mail/packages/<package_id>/person/relationships` | 某人的往来关系 |
| `GET/POST /api/mail/packages/<package_id>/person/analyses` | 查看或发起人物分析 |
| `GET /api/mail/person-analyses/<turn_id>`、`POST .../resume` | 查询进度、续跑 |

普通导入里的邮件文件也会通过 `services/mail_document.py` 转成普通文档证据，不写邮件域的表。

## 十六、翻译模块

翻译模块支持两类场景：

1. 用户对单个文档或选定内容发起翻译。
2. 导入阶段构建受限的中文工作译文，用于主题、目录和证据检索。

翻译结果会保留源语言、译文、段落对应关系、翻译版本和质量状态。导入翻译有文件数、字符数和单元数边界，避免翻译任务占满分析队列。翻译是分析辅助层，正式证据仍然保留原文；重要结论应同时展示原文片段。前端的译文阅读器（`static/translation-reader.{css,js}`）支持原文、译文和双语对照三种连续阅读方式，译文不完整时会提示。

注意：`document_translations` 表没有 `owner_id` 列，译文接口的归属校验在路由层完成，详见 3.6。

## 十七、证据校验与可信度

系统把证据分为原文片段、字段证据、关系证据和模型解释。每条证据尽量包含：

- 来源文件和逻辑路径。
- 页码、段落、表格、字符范围或邮件头字段。
- 原文片段和证据类型。
- 源文件哈希、解析版本和生成时间。
- `supported`、`partially_supported` 或 `insufficient` 状态。

报告中的结论必须能回到证据。无法验证的结论会降低置信度或保留为待核查问题。

回答生成后还有一组确定性的校验和修补，全部不额外调用模型：

| 模块 | 做什么 |
| --- | --- |
| `services/claim_verifier.py` | 数字、单位和数量结论必须绑定到具体来源记录 |
| `services/grounded_clauses.py` | “A 且 B”这类复合事实，每一项都要在同一条来源记录里找到支持 |
| `services/answer_completion.py` | 问题明确要数量、而回答漏掉了引用里已有的数量时，做有界的抽取式补全 |
| `services/answer_retention.py` | 证据、范围和包版本都没变时，保留上一版整段已验证的回答，不拼接草稿 |
| `services/summary_fidelity.py` | 摘要保留原文的确定程度：未确认完成不等于已完成，未授权不等于明令禁止，计划不等于已实施 |
| `services/formal_summary.py` | 把节点结论投影到正式目录页的“结论 / 证据”界面 |

这些规则的背景和边界分别记录在 `docs/engineering-*.md`。

## 十八、结构化数据分析

CSV、XLSX、JSON 和 JSONL 会先做结构画像：列名和类型、行数、缺失值、重复值、异常值、时间范围、唯一值分布和关联字段。多个同构文件可以做字段对齐、汇总和差异分析。

系统会保留每个统计结果的文件范围和排除原因，避免把格式不一致、重复数据或缺失字段静默纳入计算。结构化结果可以进入智能目录、实体统计、关系图和报告。

## 十九、文件保护（可选模块）

`sjfx_protection/` 是一个与分析状态隔离的可选模块，用来建立“服务器上的资料”和“用户电脑上的文件”之间的对应关系，并记录文件进出系统的履历。它由 `SJFX_PROTECTION_MODE` 控制：

| 模式 | 行为 |
| --- | --- |
| `off`（默认） | 纯透传，不初始化数据库，对主系统零影响 |
| `audit` | 记录关联、检查和访问事件；默认不拦截。另设 `SJFX_PROTECTION_ALLOW_ENFORCEMENT=1` 后，可以按账号单独开启强制保护 |
| `enforce` | 所有账号的业务接口都必须经过本机助手验证，导入目录必须完成内容关联和安全检查 |

主要能力：

- 在 `/protection` 登记服务器资料目录，再通过本机助手（`sjfx_protection/downloads/` 里的 Windows 安装包）选择本地对应目录；对应关系靠账号、设备签名、SHA-256 和本地文件身份建立，不从路径推算。
- 被动内容检查：邮件 HTML 和附件、伪装的可执行文件、危险扩展名、宏、压缩包路径穿越、解压膨胀和嵌套。不打开外链、不执行附件、不解压落盘。可选接入 ClamAV、YARA、rspamd（`SJFX_PROTECTION_CLAM*`、`SJFX_PROTECTION_YARA*`、`SJFX_PROTECTION_RSPAMD`），引擎缺失时不显示为“安全”。
- 确认危险的文件排队隔离，只有助手回执 `completed` 才算处置完成。
- 服务器目录核验记录新增、修改、替换、移动和删除；变化后旧清单失效，需要重新登记。

页面按真实登录角色分流：`/protection` 对普通用户显示授权向导，对管理员显示详细面板；`/protection/setup` 是本人授权向导；`/protection/admin` 仅管理员可访问。带设备签名的 `/agent/v2/*` 协议独立鉴权。`GET /protection/api/status` 返回 `protection_version` 可确认新模块已加载。

用户须知、管理员范围和上线注意事项见 `sjfx_protection/ACCESS.md`、`sjfx_protection/HARDENING.md`。

## 二十、技术栈

| 层 | 技术 |
| --- | --- |
| Web/API | Python 3.10+（生产实例为 3.12.8）、FastAPI、Uvicorn、Jinja2、`web_compat.py` 兼容边界 |
| 前端 | 原生 JavaScript、HTML、CSS |
| 认证 | 服务端会话表 + 会话 cookie（`sjfx_session`）、`werkzeug.security` scrypt 口令哈希 |
| 解析 | Docling、RapidOCR、pypdf、python-docx、python-pptx、openpyxl、pandas；旧格式用 antiword、xlrd、striprtf |
| 邮件 | Python email 生态、字段规则、实体正则和结构化关系算法 |
| 模型 | 本地 vLLM OpenAI-compatible API（生成：Qwen3.6-27B；向量：multilingual-e5-small）；兼容 Ollama；JSON Schema 约束输出 |
| 统计与聚类 | NumPy、scikit-learn（k-means、轮廓系数） |
| 多语言检索 | SQLite FTS、PyThaiNLP 5.3.8（泰文分词，固定词典） |
| 持久化 | SQLite WAL、事务队列、检查点和状态机 |
| 导出 | python-docx、JSON、ZIP |
| 运维 | 独立 Worker、进程锁、心跳、超时、日志和自动恢复 |
| 测试 | pytest、Python/JavaScript 语法检查、API 和大包回归 |

`web_compat.py` 的定位需要特别说明：它是一层薄薄的 FastAPI 兼容边界，让历史同步处理器可以渐进迁移，**不是 Flask 模拟层**，新代码不要往里加东西。

## 二十一、部署环境

### 21.1 推荐环境

- Windows 11 x64 或 Linux x86_64；代码最低要求 Python 3.10 或更高版本，本阶段统一以 Python 3.12 验证。其他系统版本需另做验收。
- 普通 CPU 解析建议 8 GB 以上内存；启用 Docling/OCR 建议 16 GB 以上。
- 本地磁盘用于 SQLite、缓存、解析临时文件和报告；原始资料可以放在 NAS。
- 如果启用本地大模型，显存和模型大小决定摘要吞吐。
- 生产环境不要把 SQLite 放在 NFS/CIFS 上；项目已经支持将状态目录切换到本地磁盘。

### 21.2 Linux 开发环境示例

本节命令供联网构建机和现有 Linux 开发环境参考。Windows/Linux 新实例的统一配置、启动和停止见 [双平台运行说明](docs/platform-compatibility.md)；断网客户现场将使用后续预置依赖的安装包。

```bash
git clone https://github.com/woshizhoulingjie/sjfx-data-analysis-agent.git
cd sjfx-data-analysis-agent

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 安装完整应用依赖，包含 MSG 解析。可选的 Linux vLLM 模型服务须在另一个虚拟环境安装 `requirements-vllm.txt`，避免其 `lark` 固定版本与 MSG 的 RTFDE 依赖冲突。现有共用模型环境保持原依赖，并通过 `SJFX_MSG_PYTHON` 指定独立 MSG 解释器。`requirements.lock.txt` 是旧 Linux 共用服务器快照，未包含完整 MSG 依赖，不能作为新应用或 Windows 安装的完整入口。

复制配置并至少修改访问令牌和扫描根目录：

```bash
cp .env.example .env
# 编辑 .env：
# SJFX_API_ACCESS_TOKEN=一段随机长令牌
# SCAN_ALLOWED_ROOTS=/path/to/your/datasets
# AUTH_REQUIRED=1
# VLLM_BASE_URL=http://127.0.0.1:8001/v1
# VLLM_MODEL=你的模型名
```

> 访问令牌请使用足够长的随机串（例如 `python -c "import secrets;print(secrets.token_urlsafe(32))"`）。系统本身不限制长度，但短令牌是整条鉴权链上最容易被攻破的一环。

### 21.3 模型服务

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

### 21.4 启动运行单元

开发环境打开四个终端（第四个可选）：

```bash
# 终端一：Web 页面和 API
python -u app.py

# 终端二：普通导入、摘要、对话和翻译任务
python -u worker.py

# 终端三：大数据包任务（盘点、画像、智能目录、命名、深度解析）
python -u large_package_worker.py

# 终端四（可选）：大包问答索引旁路进程
python -u scripts/large_qa_worker.py
```

app 和 worker 自带滚动日志，写到 `<SJFX_STATE_DIR>/logs/app.log`、`worker.log`；用 nohup 启动时把 stdout 重定向到同一目录下的另一个文件（例如 `app-stdout.log`），避免同一行写两遍。

默认页面地址是 `http://127.0.0.1:18000`。首次启动时 `bootstrap_first_admin()` 会创建管理员账号，把初始密码打印到日志并标记为**首次登录必须修改**；此后新用户通过 `/register` 自助注册，或由管理员在 `/admin` 创建。请第一时间登录并改掉日志里那个初始密码。

启用认证后，机器对机器的 API 请求需要携带令牌：

```http
X-SJFX-Token: <SJFX_API_ACCESS_TOKEN>
# 或
Authorization: Bearer <SJFX_API_ACCESS_TOKEN>
```

浏览器访问不需要令牌，走登录会话。

生产环境可参考 `deploy/sjfx-web.service.example`、`deploy/sjfx-worker.service.example`、`deploy/sjfx-large-worker.service.example` 和根目录的 `sjfx-vllm.service.example` 使用 systemd 管理。问答旁路进程没有附带示例单元，按同样方式写一个即可；如果它没有声明依赖 vLLM 单元，重启 vLLM 后要手动重启它。

运维上的建议：用一把维护锁文件让看门狗在计划内重启期间跳过检查；重启 vLLM 前先确认能回滚（vLLM 冷启动加载 27B 模型、编译和 CUDA graph 捕获约需 6～9 分钟，这期间前台问答会失败）。

### 21.5 重要配置

| 配置 | 作用 |
| --- | --- |
| `SJFX_API_ACCESS_TOKEN` | 机器访问令牌（`AUTH_REQUIRED=1` 时必填） |
| `AUTH_REQUIRED` | 是否强制鉴权；绑定非回环地址时必须为真 |
| `SJFX_OWNER_ID` | 无会话场景（脚本/后台任务）回落的归属，默认 `primary` |
| `SCAN_ALLOWED_ROOTS` | 允许扫描的数据根目录；也是上传落盘的白名单 |
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
| `VLLM_EMBED_BASE_URL`、`VLLM_EMBED_MODEL` | 向量服务，默认 `http://127.0.0.1:8002/v1`、`multilingual-e5-small` |
| `SJFX_GENERATION_CONCURRENCY`、`LLM_MAX_CONCURRENCY` | 生成并发，必须 ≤ vLLM `--max-num-seqs` |
| `SJFX_LP_*` | 大包内容识别、分组、命名和提速开关，常用项见“大数据包智能目录”一章 |
| `LARGE_QA_ENABLED`、`LARGE_QA_DEMAND_ONLY`、`LARGE_QA_INDEX_DIR` | 问答索引旁路进程 |
| `SJFX_PROTECTION_MODE` | 文件保护：`off`（默认）/ `audit` / `enforce` |
| `PARSE_LARGEST_FIRST` | 普通包并行解析优先提交大文件，`0` 恢复原顺序 |

## 二十二、仓库结构

```text
app.py                         API、任务编排、鉴权中间件、摘要和报告入口
web_compat.py                  FastAPI 兼容边界（不是 Flask 模拟层）
worker.py                      普通 Worker
large_package_worker.py        大数据包 Worker
sjfx_multiuser/                多用户隔离：账号、会话、归属、上传、审计闸门
  users.py                     用户与会话存储（scrypt 口令、会话吊销）
  ownership.py                 resolve_owner()：整个隔离的安全心脏
  auth_routes.py               登录/注册/改密/管理员接口
  auth_pages.py                登录、注册、改密、管理页面
  upload_routes.py             分片上传与断点续传
  uploads.py                   上传会话的落盘实现
  http_adapter.py              cookie 构造与解析、会话 cookie 名
  route_audit.py               路由归属审计闸门（可独立运行）
  runtime.py                   登录/改密视图的主体发布
services/storage.py            SQLite 队列、状态和结果
services/unified_parser.py     多格式统一解析
services/package_analysis.py   普通数据包解析
services/large_package*.py     大包策略、隔离存储和报告
  large_package_semantic.py    内容画像、语义向量、k-means 分组
  large_package_catalog_naming.py
                               组级模型命名：取证据、预算、校验、缓存
  large_package_catalog_batch.py
                               多组一批命名
  large_package_catalog_topics.py / _tree.py
                               主题层与版本化多级导航树
  large_package_catalog_quality.py
                               目录变更的内容检查与增量修复
  large_package_catalog_import.py
                               智能目录 → 普通数据包桥接
  large_package_resources.py   资源准入与背压
services/large_qa_*.py         大包问答索引、检索、泰文、主题图
services/model_dispatch.py     跨进程生成准入闸门
services/answer_*.py、grounded_clauses.py、summary_fidelity.py
                               回答的确定性校验与修补
services/mail/                 邮件域：解析、关系、研究台、人物往来、检索、导出
sjfx_protection/               可选文件保护模块（自带页面、worker 和测试）
scripts/large_qa_worker.py     问答索引旁路进程
scripts/catalog_*.py           智能目录体检、统计与验收工具（只读）
services/homogeneous_documents.py
                               同构文档字段、实体和关系
services/evidence.py           证据选择、引用和校验
services/reporting.py          情报概览上下文
services/exporter.py           Word/JSON/ZIP 导出
services/translation.py        翻译服务和翻译记忆
services/conversation*.py      对话上下文和运行时
static/                        前端资源
templates/                     页面模板
docs/                          项目说明和验收记录
tests/                         自动化测试
```

## 二十三、验证和排错

代码检查：

```bash
python -m py_compile app.py worker.py large_package_worker.py services/storage.py
node --check static/app.js
node --check static/large-package.js
node --check static/large-package-result.js
python sjfx_multiuser/route_audit.py      # 路由归属审计闸门
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
9. 两个账号互相看不到对方的数据包、会话与导出产物。
10. 管理员切视角后能看到目标用户的范围，切回后恢复全局视角。
11. 分片上传能中断后续传，落盘到自己的 `outputs/_uploads/<owner>/`。
12. 大包智能目录三层都能展开，人工改名、锁定和历史恢复有效。
13. 从智能目录勾选节点导入普通包，并在普通包里完成问答。
14. 大包问答在未全量深读时能返回带来源的回答，并如实说明覆盖范围。

遇到文件失败时，按四层排查：数据包状态 → 文件状态 → 队列状态 → 运行日志。遇到概览按钮不可用时，先检查前置摘要任务是否完成、状态机是否推进，不要直接删除数据包重来。

常见原因包括：扫描根目录未授权、文件权限不足、解析临时磁盘不足、Docling/OCR 原生依赖缺失、本地模型服务不可用、SQLite 放在网络盘、模型请求超时和历史任务处于取消中。短暂 SQLite 锁现在会保存检查点并自动重试。

排查“智能目录卡住、名字不出来、一直在重试”时，按这个顺序查：`scripts/catalog_health.py --bad-only` 看是哪个包、哪类问题 → 该包的工作流事件（例如 `rows_skipped`、退避原因）→ `lp_catalog_changes` 和命名队列里是否有长期不动的行 → vLLM 是否在线、生成槽位是否被占满。不要为了“重来”直接删包；要停掉不再需要的包，用产品自带的取消入口，已完成的结果会保留。

排查“为什么某个用户看到了不该看的东西”时，按这个顺序查：`resolve_owner()` 的返回值和 `source` → 该路由是否在 `KNOWN_SAFE_DELEGATORS` 里 → 目标表本身有没有 `owner_id` 列（没有的话必须像 `/api/translations/` 那样在路由层补校验）。

## 二十四、当前版本功能基线（按实际代码核对）

本节把系统已经落地的能力按“输入—处理—输出”重新列出，便于产品验收和新成员快速判断某个按钮对应哪条后端链路。

### 24.1 输入层

- 目录扫描支持授权根目录、递归深度、文件数量、单文件大小和总容量限制。
- 网页分片上传（≤4 MB/片）支持断点续传，落盘按归属隔离。
- 每个文件写入稳定 ID、相对路径、扩展名、大小、修改时间和内容指纹。
- 压缩包成员使用逻辑路径保存；原始文件路径始终可回查。
- PDF、Office、表格、纯文本、网页、图片和邮件走统一文档对象；专用解析器负责格式差异。
- 解析临时目录、缓存目录和状态目录可分别配置，避免大文件占满系统盘。

### 24.2 分析层

- 初步摘要优先追求覆盖率和可见性，适合快速判断数据包主题。
- 深度摘要只处理用户选定的文件/节点，适合高价值资料精读。
- 同构文档可按模板、字段、表格结构和邮件元数据做批量比较。
- 目录分析将路径结构、语义主题、实体和事项组织为可浏览节点。
- 结构化分析先做字段剖析和质量检查，再按问题选择相关行或列。
- 邮件分析把线程、引用、附件、实体和关系分别保存，可独立重试。
- 大包智能目录按“文件 → 内容分组 → 主题”三层生成，命名有预算、有证据、可复核，内容变化后只修受影响的组。
- 大包问答走独立的按需索引，支持中英泰混合检索；回答里的数字和复合事实逐项绑定来源记录。

### 24.3 输出层

- 页面显示任务进度、范围、摘要覆盖、实际检查、引用数、待核查项和失败项。
- 情报概览区分初步版和深度版，并披露当前版本使用的摘要范围。
- 资料问答显示回答、引用文件、文件路径、证据标识和不足提示。
- 简报支持 JSON、Word 和证据 ZIP；初步摘要完成即可下载，深度完成后自动更新。
- 导出结果携带数据包、任务、摘要版本和生成时间等元数据，便于审计。
- 所有产物按 `outputs/<owner>/` 隔离，跨用户下载一律拒绝。

## 二十五、主要 API 速查

| 场景 | API |
| --- | --- |
| 服务健康 | `GET /api/status` |
| 当前身份 | `GET /api/auth/me` |
| 注册 / 登录 / 登出 | `POST /register`、`POST /login`、`POST /api/auth/logout` |
| 自主改密 | `POST /api/auth/change-password` |
| 用户管理（管理员） | `GET/POST /api/admin/users`、`POST /api/admin/users/<user_id>/password`、`DELETE /api/admin/users/<user_id>` |
| 切视角（管理员） | `GET/POST /api/admin/view-as` |
| 上传 | `GET /upload`、`POST /api/uploads`、`PUT /api/uploads/<session_id>/chunks/<n>`、`POST /api/uploads/<session_id>/finalize` |
| 创建普通扫描 | `POST /api/scan` |
| 查询扫描 | `GET /api/scan/{scan_id}` |
| 数据源 | `GET /api/data-sources`、`POST /api/data-sources/select` |
| 普通问答 | `POST /api/ask`、`POST /api/general-chat` |
| 文件检索 | `GET /api/retrieve`、`GET /api/search/files` |
| 对话 | `/api/conversations`、`/api/conversation/{session_id}/messages` |
| 普通概览 | `GET /api/package/{scan_id}/overview` |
| 普通报告/导出 | `POST /api/report`、`POST /api/export` |
| 创建大包 | `POST /api/large-packages` |
| 大包状态/文件 | `GET /api/large-packages/{id}`、`GET .../{id}/files` |
| 大包目录 | `GET .../{id}/catalog`、`GET .../{id}/nodes` |
| 大包选择 | `POST .../{id}/select` |
| 大包概览 | `GET .../{id}/report` |
| 大包简报 | `GET .../{id}/report/download` |
| 大包控制 | `POST .../{id}/pause`、`resume`、`cancel`、`retry-failed` |
| 大包历史 | `GET /api/large-packages/history` |
| 智能目录 | `GET .../{id}/catalog`、`GET .../{id}/catalog/tree`、`GET .../{id}/catalog/nodes` |
| 目录改名 / 完善 / 重建 | `POST .../{id}/catalog/nodes/{node_id}/name`、`POST .../{id}/catalog/improve`、`POST .../{id}/catalog/rebuild` |
| 目录导入普通包 | `POST .../{id}/catalog/import`、`GET .../{id}/catalog/imports` |
| 普通包目录分页 / 摘要分页 | `GET /api/tree/{scan_id}?kind=physical\|analysis&node_key=&offset=&limit=`、`GET /api/summaries/{scan_id}` |
| 邮件人物往来 | `GET /api/mail/packages/{package_id}/people` 等，见邮件研究台一章 |
| 文件保护 | `/protection`、`/protection/setup`、`/protection/admin`、`GET /protection/api/status` |
| 翻译 | `GET /api/translations/{scan_id}`、`POST /api/translate-package/{scan_id}` |
| 下载票据 | `GET /api/download-ticket` |

路径中的 `{id}`、`{scan_id}` 和 `{session_id}` 必须来自当前用户可访问的数据包或会话；不要在客户端自行拼接其他用户的 ID。

## 二十六、生产 vLLM/Qwen 运行约定

当前生产实例的模型后端约定为：

```env
LLM_BACKEND=vllm
ENABLE_VLLM=1
VLLM_BASE_URL=http://127.0.0.1:8001/v1
VLLM_MODEL=qwen3.6:27b
```

生成模型在 8001 端口，向量模型（multilingual-e5-small）在 8002 端口。`sjfx-vllm.service.example` 是生成服务的示例单元；并发相关参数建议放在 systemd drop-in 里调整，不改主单元文件。

Web、普通 worker、大包 worker 和问答旁路进程只通过统一分发层访问模型；它们不启动、停止或替换 vLLM。

**生成准入闸门**（`services/model_dispatch.py`）：所有生成调用先拿一个跨进程的生成槽位，向量调用不经过闸门。

- 槽位总数是 `SJFX_GENERATION_CONCURRENCY`，必须不大于 vLLM 的 `--max-num-seqs`，否则多出来的请求会在 vLLM 内部排队，闸门的优先级就失效了。
- 用户问答走前台优先级；目录命名等可选工作走后台优先级，最多用 N−1 个槽位，始终给前台留一个。设了 `SJFX_GENERATION_BG_BORROW_IDLE_SECONDS` 后，前台空闲超过这个秒数，后台可以借用保留槽。
- 槽位是租约，持有进程死掉后会被回收。

调整 `--max-num-seqs` 或 `--max-num-batched-tokens` 前，先看 vLLM 启动日志里的 KV 缓存容量：单卡 24 GiB 上跑 27B 模型，KV 缓存很紧，参数改大可能让能同时容纳的满长请求数下降。

发布文档、前端资源或大包专用服务时，应确认：

1. 改动如果涉及 `services/model_dispatch.py` 或 `services/vllm.py`，同时测过前台问答的首字延迟，并能一键回退；
2. `.env` 中后端仍为 `vllm`，模型仍为 Qwen；
3. vLLM 进程和 8001/8002 端口保持运行；
4. 新代码只改变传给模型的检索上下文和输出展示，不改变模型服务本身。

## 二十七、发布前检查表

- [ ] `git status` 中没有 `.env`、数据库、原始资料、日志、缓存和备份文件。
- [ ] 仓库中没有任何真实凭据（口令、令牌、私钥）；测试脚本从环境变量读取令牌。
- [ ] 普通数据包完成一次扫描、初步摘要、问答和导出。
- [ ] 大数据包完成 inventory、quick、目录、初步概览和简报下载。
- [ ] 大数据包深度任务完成后，概览版本和简报内容发生更新。
- [ ] 多个问题的证据条数随实际引用变化，不再固定为 16。
- [ ] 空摘要、失败摘要和纯元数据未出现在回答证据中。
- [ ] 大包问答不会触发普通索引重建。
- [ ] 目录节点和单文件标题可显示中文，同时保留原始路径。
- [ ] 切换会话/数据包后旧请求不会覆盖新回答。
- [ ] `python sjfx_multiuser/route_audit.py` 通过，新增的带 id 路由要么有归属校验、要么在 `KNOWN_SAFE_DELEGATORS` 里写明原因。
- [ ] 用第二个账号验证：看不到对方的扫描、会话和导出产物。
- [ ] `/api/status` 正常，三个应用进程（以及启用时的问答旁路进程）状态正常。
- [ ] `scripts/catalog_health.py --bad-only` 没有新增问题包。
- [ ] 大包智能目录能到 70% 进入选择，人工改名锁定后不被自动命名覆盖。
- [ ] 从智能目录勾选节点导入普通包，导入后普通解析和摘要正常派发。
- [ ] 新增的 `SJFX_*` 开关默认值保持旧行为，并写明怎么关回去。
- [ ] vLLM/Qwen 配置和模型进程未被发布操作修改。
- [ ] 目标测试、语法检查和人工页面验收均有记录。

## 二十八、当前能力边界与路线

当前系统已经形成“目录 → 解析 → 摘要 → 证据 → 关系 → 概览 → 报告”的工程闭环，并且支持多用户隔离下的多人共用。以下是明确的后续方向：

- 文件全生命周期审计：`sjfx_protection/` 已经实现服务器目录与本机文件的关联、内容检查、隔离处置和审计事件（默认关闭）。还没做的是把系统内的解析、检索命中、导出、删除等事件与本机履历串成一条可查询的时间线。
- 智能目录的分组边界：定向审计里约 39% 的组被判为“名称与留出成员不是同一主题”，主因是一个组里混了两件事，而不是名字编造。`scripts/catalog_boundary_audit.py` 目前只把可疑组找出来给人看，不自动重分组，因为现有的几何判据在这类语料上区分不出来。
- 智能目录命名速度：生产参考配置实测只快了约 8%。再往上的办法要么改命名提示词，要么动分组算法，都需要先用 `scripts/catalog_evidence_ab.py` 做质量对比。
- 100 GB 数据包端到端验收还在进行中，`docs/engineering-100gb-acceptance.md` 不是验收通过证明。
- 智能目录里单独选中压缩包内部的某个成员导入普通包，仍有限制，见 `docs/engineering-real-file-handoff.md`。
- 邮件实体消歧：合并同一人的多个邮箱、姓名、别名和签名。
- 缺失头部时的线程聚类和事项聚类。
- 邮件附件与独立文件的哈希、文号和内容关联。
- 事件抽取和可回溯时间线。
- 按人物、机构、事项、时间窗口切片的关系网络分析。
- 关系人工确认、排除、批注和审计日志。
- 关系图增量合并、撤销和局部重算。
- 几千份邮件的吞吐、内存、GPU、失败率和重试监控。

这些增强都应保持现有原则：原文保留、证据可回溯、强关系和候选线索分开、长任务可恢复、用户可以随时控制范围、跨用户数据不可见。

## 二十九、给新成员的阅读路线

建议按以下顺序阅读代码：

1. `app.py`：路由、鉴权中间件、状态推进、摘要和报告编排。
2. `sjfx_multiuser/ownership.py`：先读懂三条铁律，再读其他任何业务代码。
3. `sjfx_multiuser/users.py`：用户、会话、口令与吊销。
4. `sjfx_multiuser/auth_routes.py`、`upload_routes.py`：认证接口与上传入口。
5. `sjfx_multiuser/route_audit.py`：隔离纪律是怎么被固化成检查的。
6. `services/storage.py`：数据库、任务队列和检查点。
7. `worker.py`：任务领取、子进程监督、取消、超时和恢复。
8. `services/unified_parser.py`：格式解析和统一文档对象。
9. `services/evidence.py`：证据索引和校验。
10. `services/mail/`：邮件字段、实体、关系和研究会话。
11. `services/translation.py`：翻译计划、记忆和质量控制。
12. `services/reporting.py`、`services/exporter.py`：概览和报告。
13. `static/app.js`、`static/large-package*.js`：前端轮询、补充和切换。
14. `large_package_worker.py`：大包的阶段推进、目录轮次和命名调度。
15. `services/large_package_semantic.py` → `large_package_catalog_naming.py` → `large_package_catalog_topics.py` / `_tree.py`：按三层顺序读智能目录。
16. `services/model_dispatch.py`：生成槽位、优先级和租约。
17. `services/large_qa_index.py`、`large_qa_search.py`：大包问答索引和检索。

## 三十、本次更新（2026-09-29）

相对 2026-09-22 版本，主要变化：

- **大包智能目录**：形成“文件 → 内容分组 → 主题”三层架构；命名改为有预算、带证据、可校验的组级命名；新增主题层自适应聚类、版本化导航树、名字继承、增量修复，以及人工改名锁定和历史恢复。
- **智能目录提速**：两路并发命名、未变叶组沿用旧名、统一缓存键、证据缓存预热、发布退避，以及配套的测量点和只读统计脚本，所有开关默认关闭。
- **缺陷修复**：主题层残缺时叶组落入“待归并主题”、代表材料索引越界、命名任务搁置后永不拾取、没有向量的单文件组导致修复逻辑无限重试等。
- **目录 → 普通包桥接**：从智能目录勾选节点直接建普通数据包。
- **大包问答**：独立的按需问答索引旁路进程，中英泰混合检索，主题图。
- **回答质量**：数字与单位绑定来源记录、复合事实逐项落地、漏答数量的抽取式补全、已验证回答保留、原文限定词保留。
- **大包稳健性**：资源背压与无损恢复、压缩包成员身份保留、真实文件交接时父目录闭包和分页目录计数修正、统计口径一致。
- **邮件**：人物往来视角与可续跑的人物分析。
- **界面**：共用的情报阅读面板、译文阅读器、重新组织的导航。
- **旧格式导入**：DOC / PPT / XLS / RTF。
- **文件保护**：新增可选模块 `sjfx_protection/`，默认关闭。
- **测试**：新增约 90 个测试文件。

## 三十一、本次更新（2026-09-22 快照）

- 新增多用户隔离层 `sjfx_multiuser/`：注册、登录、改密、会话 cookie、管理员切视角、按 owner 的接口隔离和 `outputs/<owner>/` 磁盘隔离，以及路由归属审计闸门 `route_audit.py`。详见第三章。
- 新增网页分片上传入口 `/upload` 与 `/api/uploads/*`，支持断点续传，落盘到 `outputs/_uploads/<owner>/`。详见第四章。
- 修掉一个实际越权：`/api/translations/<scan_id>` 曾可跨用户读取他人译文，因为 `document_translations` 表没有 `owner_id` 列。现在在鉴权中间件按 `scan_owned()` 校验并统一返回 404。
- 归属修复：写路径原先直接写 `_request_owner_id() or "legacy"`，会让管理员的写操作落到 `legacy` 桶形成孤儿数据。现在写路径的归属统一由 `resolve_owner()` 给出，无会话时回落到 `Config.OWNER_ID`（默认 `primary`）；`or "legacy"` 只在后台任务执行/入队路径上作为兜底保留，与读取侧 `scan_owned()` 的 `row["owner_id"] or "legacy"` 约定对称。
- 大包与检索链路新增 `services/large_package_catalog_{merge,profile,topics}.py`、`large_package_repair.py`、`llm_trace.py`、`multiturn_structured_qa.py`、`ordinary_recall.py`。
- 新增前端资源 `static/export-center.{css,js}`、`mail-modules.js`、`reading-layout.css` 和 `sjfx_multiuser/web_assets/userbar.js`。
- `.gitignore` 增加规则：基准采集目录、真实用户样本、打包的第三方二进制和就地编辑备份一律不入库。

## 三十二、一句话总结

SJFX 的核心不是让模型直接猜答案，而是把海量资料拆成可恢复批次，把确定性解析、证据和关系先落盘，再让模型在受控上下文中生成摘要和解释，最后让用户可以补充、暂停、切换、复核并导出结果；而多用户隔离保证这一切在多人共用时，每个人的资料对其他人都不存在。

## 三十三、贡献约定

涉及大数据包的修改应尽量局限在 `services/large_package_*`、`services/conversation_package_context.py` 以及对应前端资源和测试中，并明确说明是否影响普通数据包。涉及模型调用时必须保持统一分发层和生产 vLLM/Qwen 约定。

涉及智能目录的修改，还要遵守：

1. 不改变三层架构的约束：命名和主题归纳不能改变叶组成员。
2. 改命名提示词、取证据方式或分组算法前，先用 `scripts/catalog_evidence_ab.py` 或同等的并排对比证明质量不下降。
3. 提速类改动必须挂在默认关闭的开关后面，写明回退方法，并同时报告耗时、截断率、待复核比例和前台首字延迟。

涉及多用户与鉴权的修改，必须遵守：

1. 不要绕过 `resolve_owner()` 自行判断归属。
2. 不要接受来自客户端输入（查询参数、请求头、cookie）的 owner。
3. 新增带 id 的路由时，同步更新 `route_audit.py` 的清单并写明理由。
4. 新增含归属数据的表时，必须带 `owner_id` 列；如果确实无法带，就要在路由层补校验并在此文档中登记。

提交信息应说明行为变化和验证方式；文档、测试和源代码一起提交。任何临时备份、运行日志、数据库和原始资料都放在仓库之外，任何真实凭据都不进入版本库。
