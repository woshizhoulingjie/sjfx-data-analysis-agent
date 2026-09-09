# SJFX 项目讲解：从文件导入到邮件关系网络

## 1. 为什么要拆成这个系统

一次性把全部文件交给大模型会遇到上下文不足、单文件失败拖垮整批、无法暂停恢复、结论无法回到原文等问题。SJFX 先把资料变成可靠的目录和证据，再把耗时工作切成持久化小任务，模型只在受控上下文中解释证据。

## 2. 三个进程

`app.py` 通过 `web_compat.py` 创建 FastAPI 应用，负责页面、API、认证、范围选择、任务控制和报告下载。它不直接承担长时间解析。

`worker.py` 从普通 `analysis_jobs` 队列领取扫描、解析、摘要、节点、概览、翻译和对话任务。任务在受监督子进程中运行，父进程负责心跳、超时、取消和检查点恢复。

`large_package_worker.py` 使用隔离存储和队列处理大数据包 inventory、quick、deep、summary 和 report，防止大型任务阻塞普通导入。

`services/storage.py` 是所有进程共享的状态边界。SQLite WAL 中保存任务、文件状态、检查点、摘要、证据和关系图；事务负责安全领取队列和解决取消/完成竞争。

## 3. 状态机和恢复

普通导入大致经过：

```text
queued → scanning → waiting_for_selection → parsing_selected
→ parsed_overview → preliminary_summarizing → preliminary_nodes
→ preliminary_overview → deep_summarizing_files
→ deep_summarizing_nodes → deep_update_available
→ deep_overview_updating → completed / partial
```

暂停会记录 `paused_from_state`，停止相关队列任务但保留已完成检查点。继续时恢复原阶段，保留 completed 子任务，只为 failed、cancelled、cancelling 子任务重新建队列。补充选择在稳定状态下追加新文件，不清空旧结果。SQLite 短暂锁冲突会保存当前检查点并自动重试，避免误报永久失败。

大数据包将同一思想拆成 inventory、quick parse、selection、deep parse、summary、report 六个阶段。页面始终展示完整路径和待补充数量。

## 4. 邮件关系算法

每封邮件先标准化成记录：路径和哈希、日期、发件人/收件人/抄送、主题、Message-ID、In-Reply-To、References、事项号、动作类型、正文摘要、附件和实体。

关系按顺序构建：

1. In-Reply-To → Message-ID，生成高置信度回复边。
2. References 头和正文文号，生成引用边。
3. 事项号、项目号、合同号完全一致，生成同事项边。
4. 参与者方向、主题变体和时间顺序，补充通信流程候选。
5. 邮箱、电话、IP、URL、案件号等共享实体，生成带证据的候选边。
6. 每条关系写入来源、目标、置信度、理由和证据片段。

`validated` 表示有明确字段或文号证据；`candidate` 表示主题、时间或共享实体等弱信号；`derived` 表示由多个特征推导。共享一个姓名不会直接被当成确定关系。

## 5. 几千份邮件如何深挖

正确方式是批次化，而不是一个巨型模型请求：

```text
批次 A：邮件头和正文元数据
批次 B：实体、附件指纹和规范化身份
批次 C：回复链、事项链和通信边
批次 D：对关键线程做深度摘要和时间线
批次 E：按实体、事项、时间窗口生成调查视图
```

当前代码使用倒排索引，限制主题候选和派生边数量，同时保留全部原始记录。几千份邮件可以持续解析和合并；模型只处理选出的重要线程。要达到调查级别，还需增加实体消歧、缺失头部的线程聚类、附件联动、事件时间线、网络中心性和人工复核。

## 6. 模型和证据的边界

扫描、解析、哈希、字段抽取、去重和证据定位优先由确定性代码完成。模型负责文件/节点摘要、跨文件主题归纳、概览和研究方向候选。正式报告只能引用允许范围内已经完成的阶段结果，不能用未完成摘要或无证据的模型猜测替代事实。

## 7. 建议阅读顺序

1. `app.py`：路由、编排和状态推进。
2. `services/storage.py`：队列、检查点和持久化。
3. `worker.py`：领取、监督、取消、超时和重试。
4. `services/unified_parser.py`：多格式统一解析。
5. `services/evidence.py`：证据选择和校验。
6. `services/homogeneous_documents.py`：邮件字段和关系图。
7. `services/reporting.py`、`services/exporter.py`：报告生成。
8. `static/app.js`、`static/large-package*.js`：前端轮询、补充和切换。

## 8. 验证方法

```bash
python -m py_compile app.py worker.py services/storage.py large_package_worker.py
node --check static/app.js
node --check static/large-package.js
node --check static/large-package-result.js
pytest -q
```

实际验收要覆盖：普通导入、暂停/继续、补充剩余文件、切换数据包、大数据包 quick/deep/report，以及邮件关系数量、置信度和证据回溯。
