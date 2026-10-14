# SJFX 模型服务接口与部署配置（第三阶段）

应用请求管理员明确选择的 HTTP(S) 模型服务，支持客户本机、客户内网及服务器上的 vLLM、Ollama 或 OpenAI 兼容服务。`127.0.0.1`、`localhost`、`::1` 指 **SJFX 应用运行的机器**，不是打开网页的浏览器所在机器。SJFX 和模型运行在客户同一台机器时可填写回环地址；分开运行时填写模型机器可访问的内网或服务器地址。

URL 不携带用户名、密码、查询或片段，认证凭据单独填写。模型请求忽略系统 HTTP 代理、拒绝重定向，HTTPS 校验证书。每次请求验证并固定实际连接的解析地址，拒绝元数据、链路本地、未指定、组播、保留地址及其它禁止地址；远端域名不能通过解析指向回环服务。数据库、资料、历史任务和结果仍保存在实例目录；**选择内网或服务器模型时，请求所需业务内容会发送到指定服务**。适配层不安装引擎、不下载权重、不直接加载 GGUF、不启动模型服务。

## 管理员配置与生效

1. 先在模型所在机器启动 HTTP 服务，确认加载的模型名称和可用上下文窗口。
2. 使用已完成初始密码修改的管理员账号，打开“系统设置 → 模型服务配置”。生成和向量分别选择服务类型，填写服务地址、模型名称及可选凭据。普通用户和全局 API Token 无权修改模型设置。
3. 点击对应测试按钮。测试表单草稿会临时允许这次明确填写的端点，只发送短测试内容，不保存配置，也不改变当前运行客户端。通过需要实际生成、结构化输出或有效向量，`/models` 可达不能代替推理成功。
4. 保存配置，重启 Web 和全部 Worker。页面分别显示当前运行、已保存和未保存的表单修改；仅保存或测试成功不会让当前进程即时切换模型。
5. 更换向量模型后重建对应索引。向量配置统一用于普通检索、大数据包和邮件；页面显示已有邮件/大包独立地址及备用地址，保存向量设置会统一这些地址并清除旧大包备用服务，防止业务继续调用旧服务。

代码升级后首次使用新版配置，需要先重启 Web 和全部 Worker 以启用新接口；此后每次保存模型配置，仍需再重启这些应用角色以应用新的模型设置。未启用新接口时，页面只读并显示重启提示。

配置持久化到实例 `model_settings.json`，沿用 `version: 1` / `env_overrides`，在各应用角色下次启动时覆盖 `.env`。已有回环 `.env` 和旧 v1 文件仍兼容。认证凭据仅写入，不在读取、诊断或错误中回显；同一服务留空保留，勾选“清除”删除。切换服务类型必须重新明确填写地址和模型名称；切换类型或服务地址时未填写新凭据会清除旧凭据，避免发给新服务。关闭生成或向量不会自动切换其它后端。

页面自动记录明确保存的远端地址许可，无须管理员额外维护技术列表。直接用实例 `.env` 部署时，将精确 HTTP(S) 来源（协议、主机、端口）写入 `SJFX_MODEL_ALLOWED_ENDPOINTS` JSON 数组，例如 `["http://192.168.10.20:11434"]`。不接受通配符；服务 URL 的路径可为 `/v1`，来源许可无需路径。设置、测试与实例环境均不得包含真实密钥的 URL。

生成窗口支持 2048–1048576 tokens；未明确配置时默认 4096，必须按实际模型能力填写。小窗口会缩小批次和输出预算，过长完整输入明确失败，由业务分片处理，不静默裁剪原文。可选 `LLM_TOKENIZER_PATH`（兼容 `CONVERSATION_TOKENIZER_PATH`）仅加载明确选择的本地分词器；未设置或加载失败使用诚实的 UTF-8 字节上界。页面显示实际加载状态，不宣称远端精确 token 数。切换地址、模型或后端时，未提供新绑定会清除旧分词器和版本标识；`VLLM_MODEL_PATH` 不再隐式提供另一模型的分词器。

## 生成接口

业务通过 `services.model_backend.create_model_client(config)` 获取传输，再通过 `PydanticAgentRuntime` 做结构化业务校验。关闭所选后端时工厂返回 `None`，不会改用其他后端。Web、普通分析、大包摘要、目录命名、主题、报告、问答、检索重排和显式文件翻译共用这个选择。导入用 NLLB 与可显式配置的原生 Ollama 翻译仍独立。

`chat(system_prompt, user_prompt, temperature, max_tokens, retries, timeout)` 返回 `content`、`model`、`usage` 和 `finish_reason`。`chat_json`/业务 runtime 增加解析后的 `json`，校验必需字段。格式纠正最多一次；截断、不符合契约或无效响应不会作为成功的模型分析。调用方可记录显式降级结果，但不能将其标为模型深度分析完成。

| 后端 | HTTP 协议 | 能力与限制 |
| --- | --- | --- |
| `vllm` | OpenAI `/models`、`/chat/completions`；特定上下文拒绝时有限调用 `/tokenize` | 保留 vLLM JSON Schema、关闭 thinking、原文不裁剪的精确预算修正和可选 trace |
| `ollama` | 原生 `/api/chat`，健康检查保留兼容 `/v1/models` | 保留 `think:false`、原生 JSON format、num_ctx/num_predict 与流式结束标记 |
| `openai_compatible` | `/models`、非流式 `/chat/completions` | 只发送通用 chat 参数。可显式声明 JSON object `response_format`；不发送 vLLM 私有参数，不默认宣称 JSON Schema、tokenize 或 thinking 控制 |

通用服务必须返回 OpenAI chat envelope：`choices[0].message.content` 是非空字符串，带正常完成原因；响应模型若提供必须与配置相符。结构化输出以应用校验结果为准。通过协议模拟和实际现有服务验证不等于所有宣称 OpenAI 兼容的引擎都已验证；新的引擎需实际连接测试和业务验收。

## 向量接口与身份

业务通过 `services.embedding_backend.create_embedding_client(config, lane='ordinary'|'large'|'mail')` 获取独立向量客户端。支持 OpenAI `/models` + `/embeddings` 和原生 Ollama `/api/tags` + `/api/embed`。OpenAI 响应每条必须带从 0 开始且完整唯一的 `index`，数量与输入一致；全部向量必须为有限、非零、同维数的数值数组，响应模型不能与配置冲突。

`client_identity(client)` 将模型名、服务端点、已提供的版本/权重/配置标识、维度设置、编码策略、字符上限和服务可提供的 revision/digest/created 等信息组成持久身份。本机向量权重目录用于资源身份核验；不同业务编码策略分别隔离。缓存沿用现有 `model` namespace；大包、邮件和普通检索沿用已有 SQLite 表和索引元数据，增加身份与维数绑定，保留旧 namespace。

同名权重在 HTTP 服务侧替换且服务不公布 revision/digest 时，应用无法单凭模型名识别实际权重字节。管理员必须更新 `SJFX_EMBED_VERSION`、`SJFX_EMBED_WEIGHTS_ID` 或 `SJFX_EMBED_CONFIG_ID`；若配置本机目录，应确保对应服务确实使用该目录。维度变化或公布的服务版本变化会被拒绝，不能与旧向量混用。`SJFX_EMBED_ENCODING` 明确选择 `e5` 或 `plain`，E5 前缀仅用于匹配的模型；`SJFX_EMBED_TOKENIZER_PATH` 可指定匹配向量模型的分词器。切换模型或服务时，页面清除旧维数、分词器、版本和权重绑定，缺省使用通用文本编码，避免旧 E5 参数误用于新模型。

不兼容切换保留资料、解析结果与旧向量。普通/大包问答可显式降级为词法检索；邮件/索引诊断显示需要重建。显式重建在候选数据库中计算，验证后仅事务发布向量相关行，保留旧快照；资料或索引已变化时拒绝发布。`scripts/rebuild_embeddings.py` 提供构建、发布和恢复入口，详细步骤见交付操作说明。不要清空原始资料、数据库或历史队列来切换模型。

## 故障、等待与诊断

`LocalModelError` 提供安全中文消息、`code`、`retryable` 与可用的 HTTP status，不回显服务错误 body、认证密钥或含密钥的异常文本。稳定类别包括配置错误、认证失败、模型缺失、资源不足/繁忙、连接失败、超时、上下文不足、无效响应/结构化输出及向量不兼容。

生成和向量都有有限准入与 HTTP 等待。响应体大小及读取时间受限；重试只用于有依据的瞬时故障且次数有上限。格式纠正也有次数上限。业务任务继续沿用持久队列、检查点、取消与恢复机制。恢复服务后重试原任务，不自动下载、切换引擎或删除历史。

`sjfxctl.py status/doctor` 的服务列表检查明确标为 `model_list_only`，不证明实际推理。`model-test` 和管理员界面会真实发送短生成、结构化及向量请求，分别报告可达性、模型是否存在、实际推理和能力。测试表单未保存时标为 draft；保存后重启全部应用角色才生效，模型服务由管理员独立管理。`docs/model-config-openai.env.example` 提供客户本机 OpenAI 兼容部署，`docs/model-config-remote-ollama.env.example` 提供内网/服务器 Ollama 部署。

## 离线资源边界

`SJFX_MODELS_DIR`、`SJFX_EMBED_MODEL_PATH`、`VLLM_MODEL_PATH`、OCR/Docling/NLLB 工具目录与 doctor 为后续离线安装器提供入口。生成与嵌入独立配置；一个接口通过不代表 OCR、NLLB 或旧 Office 资源已安装。客户 GPU、量化、模型大小和并发调优需硬件确认。正式安装器和完整依赖/权重离线闭包属于后续交付。

