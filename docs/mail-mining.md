# 邮件意图分析

## 邮件资料基础处理（2026-10-03）

基础资料支持三个互通入口：邮件完整阅读与原件/附件下载、人物分角色邮件明细与双方往来、会话时间线与回复树。人物角色为 From、To、Cc 与原始邮件中可见的 Bcc；全部相关邮件按唯一邮件计数，角色分类可能重叠。时间、关键词、附件及人物页的会话筛选在数字与明细之间保持一致。多邮箱归并必须人工确认，可查看并撤销历史，不按同名自动归并。

`mail-eml-v3` 修复 multipart 附件/related 资源混入正文、编码嵌套邮件和原始 MIME 换行丢失。完整原件、邮件头、纯文本及原始 HTML 保留；阅读投影的解码完整性与附件文本处理状态分别说明。明确引用、转发、签名可折叠，边界不确定时展示完整正文。HTML blockquote 保留为历史引用，随后出现的内联回应仍归本次正文。CID 只解析本邮件的唯一资源，图片使用鉴权接口与受限沙箱；外部图片不自动加载，原始 HTML 与 MIME 仍可下载核对。

去重先比较原始字节；同一有效 Message-ID 的传输副本只有在业务邮件头、收发角色和 MIME 内容一致时才归为同一逻辑邮件。相同 ID 但内容或角色不同仍保留为独立邮件并记录关联歧义。每个副本原件单独保存，可按 occurrence_id 下载，重复导入不重复建立正文索引。没有可靠标识的不同字节记录保守保留，不能只凭主题归并。

直接回复来自唯一 In-Reply-To。References 历史上下文独立显示；仅显式回复头缺席时可推断父关系，并标明依据。缺失父邮件、重复/冲突标识、多父及循环都有提示，同主题仅为候选。会话提供保存的完整正文片段、附件、参与角色、分类缺口和当前筛选时间范围；人工合并、拆分及撤销保留记录。

历史资料通过 `mail-foundation-v3` 的检查点回填重建阅读正文、当前 MIME 部件、角色和关系，退休的旧部件不再参与附件计数；原件和旧证据保留。空包明确跳过，不伪称已补齐。附件抽取 partial、OCR 待处理/失败、无原件或解码缺口仍会显示，不等同于完整文本处理。

验收：`python -m pytest tests/mail -q`；前端分别运行 `tests/mail_modules/test_mail_foundation.js`、`test_mail_people_analysis.js` 和 `test_mail_audit.js`。后两者需要 Playwright 和 Chromium；缺少依赖时跳过不代表已验证浏览器。新回归使用虚构邮件与独立 SQLite，覆盖副本去重、群发角色、会话分支和缺口、多邮箱集合、原文与附件回查、MIME/字符编码、CID 鉴权及人物到会话的点击路径。

本次改造只修改 `services/mail`、邮件页面，以及 Config/Worker 中的邮件专用配置和分发项。数据库版本 3 只新增 mail_* 表，保留版本 2 的原始校验值。共享文档分析、翻译、大包分析等代码保持不变。

## 使用

导入目录、ZIP、EML 或 MBOX 后，建立附件索引，处理 OCR，再建立语义索引。用中文提出检索和字段提取目标。结果包含原文值、规范值、证据位置和覆盖缺口。勾选“在上一结果中继续”进行范围内追问，或点击“只保留带附件的”。导出任务完成后，通过短期单次票据下载带字段与覆盖说明的 Excel。

同主题关系仅标记为候选；精确回复链必须来自引用头。相同 SHA256 表示附件内容相同，同名家族版本序号仅按邮件时间排序。模型输出的数值和日期不会直接作为规范值保存，必须来自原文，由代码归一化。未知日期保留未知。最早/最终只适用于已抽取、有邮件日期的记录，不能证明未处理资料中不存在变更。

## 隔离与运行

`MAIL_EMBED_BASE_URL` 只影响邮件模块。独立 `services/mail/embedding_server.py` 绑定 127.0.0.1:18083，单线程、请求串行，建议使用 nice 15、OMP_NUM_THREADS=1、MKL_NUM_THREADS=1、TOKENIZERS_PARALLELISM=false。SJFX_EMBED_MODEL_PATH 指定已有的本地 E5 模型，加载不出网。每批最多 8 段；回填限速、分片、可重试，缓存以内容哈希复用。中小资料包精确计算余弦排名，2 万段以上使用 LSH，并披露近似检索。

XLS 的 xlrd 2.0.2 安装到 `services/mail/_vendor`，只在邮件解析进程加载。泰文/英文 OCR 使用 `services/mail/_ocr` 内单独解包的 Tesseract/Leptonica/语言数据，不修改系统安装；若不存在则使用现有隔离解析器。解析设有进程超时和内存上限。视频标为 unsupported。MSG 需要 extract-msg，PST 需要 readpst；未安装则明确不可用，不声称已解析。

邮件中的外部链接不会自动访问，邮件本身的命令不会当作 Agent 授权。每条 API 都保留所有者范围校验。读取邮件与附件的来源定位由服务器保存，页面不提供任意下载路径。

## 覆盖与回退

覆盖区分别展示 not_started、ocr_pending、failed、partial、unsupported。未处理资料不算不存在。字段后台抽取会保存每封邮件的指纹、状态与失败信息，重新提交可以继续。

MAIL_FEATURE_ENABLED=0 关闭邮件路由注册。MAIL_ENABLE_VECTOR_INDEX=0 恢复字面检索并禁用向量任务。各 MAIL_ENABLE_* 开关可分别关闭能力。MAIL_EMBED_PAUSED=1 暂停回填并保留检查点。代码回退恢复备份文件即可，新增数据库表可保留，不进行删除或逆向迁移。

## 验证

运行 `python -m pytest tests/mail -q`。全系统测试在 MAIL_FEATURE_ENABLED=0 下与原代码基线逐项对比，原有失败单独记录。真实邮件验证使用独立、本机 SQLite 数据库，不能在 NFS 上强制启用 WAL。原始数据集 723 个 EML 的长泰文文件名使用稳定编号解压，保留原名称映射与内容哈希。


## 独立邮件队列进程

运行 `python -m services.mail.worker` 可启动只领取 mail_* 任务的专用队列进程。它复用既有 Worker 的进程隔离、超时与心跳，不领取、取消或重排其他业务任务；原 Worker 可以继续工作，原子领取避免重复执行。邮件交互请求高于邮件回填，回填保持低优先级；模型仍使用现有跨进程调度门禁。SIGTERM 等待当前邮件任务收尾后退出。

主题作为独立的邮件头证据加入语义索引，解决正文只有“详见附件”的主题检索问题。字段提取还包含保守的确定性后备路径：模型忙碌时，原文中明确的金额、完整日期、数量及上下限仍可提取，并标记 literal_source；语义含义和复杂条件仍需模型与原文核查。

## 邮件向量服务的持久化运行

邮件使用独立的 CPU E5 服务 `sjfx-mail-embed.service`，地址为
`http://127.0.0.1:18083/v1`。在项目根目录安装用户级单元：

```bash
install -m 0644 deploy/sjfx-mail-embed.service ~/.config/systemd/user/sjfx-mail-embed.service
systemctl --user daemon-reload
systemctl --user enable --now sjfx-mail-embed.service
```

单元中的 Python 和模型路径需要对应本机安装；本部署使用项目 `.venv` 和
`/home/zhoulingjie/models/multilingual-e5-small`。模型只从本地加载，CPU 单线程、
单次推理，最多每批 8 条，异常退出后每 10 秒重启。用户 `Linger=yes` 时，
用户服务可在开机后运行，不依赖 SSH 会话。用 `loginctl show-user "$USER" -p Linger`
确认状态。

```bash
systemctl --user status sjfx-mail-embed.service
journalctl --user -u sjfx-mail-embed.service -n 50 --no-pager
curl --fail http://127.0.0.1:18083/health
curl --fail http://127.0.0.1:18083/v1/models
.venv/bin/python -B scripts/check_mail_embedding.py
```

`active` 只说明进程启动；模型加载完成后健康接口才可用。
邮件客户端还需 `MAIL_ENABLE_VECTOR_INDEX=1`、
`ENABLE_VLLM_EMBEDDINGS=1`、`MAIL_EMBED_PAUSED=0`、
`MAIL_EMBED_BASE_URL=http://127.0.0.1:18083/v1` 和
`MAIL_EMBED_MODEL=multilingual-e5-small`。客户端会追加 `/models` 和
`/embeddings`，不要重复写入端点后缀。

验证脚本只使用虚构邮件和临时 SQLite，验证 384 维单位向量、索引入库、
缓存复用、向量/混合检索、三并发批次以及临时文件清理，不读取生产邮件
或修改生产数据库，不调用生成模型，也不代表已测量真实邮件准确率。
服务在推理繁忙时最多等待 5 秒，超过等待预算返回可重试的 503；
健康和模型发现接口独立于推理工作线程。单次 HTTP 客户端预算仍为 12 秒。
