# SJFX Windows/Linux 兼容基础

本阶段提供跨平台路径配置、完整应用进程启动/停止、模型后端选择与依赖入口，并已在隔离目录准备真实 Windows/Linux 原生工具和离线模型资源进行资源级验收。`packaging/build_offline_bundle.py` 和配套安装/升级/卸载脚本可分发已准备好的运行时、工具和模型资源。完整离线包预置 CPU `multilingual-e5-small` 向量子服务和本机保护助手及 executor；生成服务引擎（vLLM、Ollama 或其他 OpenAI 兼容服务）和 Qwen 等生成权重需独立部署、配置和管理。具体最终归档的安装、升级、卸载与冷启动验收以其交付报告为准。

## 支持与验证范围

应用采用 Python + 浏览器界面 + 本地 SQLite。当前实际准备并验证的运行时为 Windows 11 x64（10.0.26200、Python 3.12.14）及 Ubuntu 24.04 x64（glibc 2.39、Python 3.12.15）。代码按 Windows/POSIX 分支实现；其他 Windows 版本、Linux 发行版与 ARM 架构需另做环境验收，不能据此声称全部系统均已验证。

应用不直接加载 GPU 生成模型。Windows 和 Linux 应用均可连接管理员明确选择的本机、内网或服务器生成服务（vLLM、Ollama 或 OpenAI 兼容接口），生成与向量分别配置。完整离线包的新实例默认通过受控 CPU E5 子服务提供向量；管理员选择外部向量服务或 `disabled` 时，下一次启动跳过包内 E5。回环地址指应用运行的机器；远端配置与精确地址许可见 `docs/local-model-interface.md`。外部模型所在机器需要对应引擎和权重，按其硬件确定推理配置。

仓库当前不包含针对某台生产服务器的 systemd、Windows 服务、Docker Compose 或路径固定的部署模板，也不提交 `.python-version`。这些文件会把运行用户、挂载点或旧模型路径固化到交付物；正式部署应依据目标机器的 Python 3.12 解释器、服务管理器和模型服务配置单独生成。离线包自身只使用安装参数和数据目录，不依赖这些服务器专用文件。

## 配置目录

| 配置项 | 用途 |
| --- | --- |
| SJFX_ENV_FILE | 显式指定实例配置文件；指定文件不存在会报错，不回落到其他实例 |
| SJFX_STATE_DIR | 本机数据库、正文与索引等状态目录 |
| SJFX_OUTPUT_DIR | 导出和上传目录 |
| SJFX_MODELS_DIR | 模型资源根目录 |
| SJFX_TOOLS_DIR | 原生辅助工具根目录，旧 Office 使用其 legacy-office/usr 子目录 |
| SJFX_PARSE_TEMP_DIR | 可写临时目录，解析和启动器子进程临时文件使用此目录 |
| SJFX_EMBED_MODEL_PATH | 本机向量权重；兼容 VLLM_EMBED_MODEL_PATH |
| SJFX_DOCLING_ARTIFACTS_DIR / SJFX_RAPIDOCR_MODEL_DIR | 可单独覆盖解析/OCR资源目录 |
| TRANSLATION_QUICKMT_MODEL_DIR | QuickMT 三方向资源和已校验清单；路径按本机平台填写 |
| SJFX_EMBED_PORT | 独立 CPU 向量服务端口，不与 Web PORT 混用 |
| SJFX_MANAGED_EMBEDDING / SJFX_MANAGED_EMBED_PORT / SJFX_MANAGED_EMBED_MODEL_PATH | 完整离线包受控 E5 的启用、端口与本地权重路径；默认端口 18083，实际启动仍取决于当前向量选择 |

配置中的相对路径统一以应用目录为基准，与终端工作目录无关。管理命令 `--env-file` 的相对参数以当前终端目录为基准，再转换为绝对路径。Windows 多个扫描根用分号分隔，Linux 用冒号分隔。

启动器创建实例解析临时目录，并为全部受控角色设置 `TEMP`、`TMP`、`TMPDIR`、`SQLITE_TMPDIR`；只修改子进程环境。完整离线包默认启用 E5 与 QA 时共有七个角色，包括本机保护助手、executor 和向量子服务。系统临时目录不可写时，SQLite 临时表、排序和解析工具仍使用实例目录。直接运行 Python/pytest 时也应提供真实存在且可写的临时目录，不能只依据目录存在或 `os.access` 判断；本次 Windows 验收遇到默认用户 Temp 实际写入被拒绝，指定隔离目录后扫描级联删除与批量目录写入均通过。

推荐把程序目录与配置、数据分开；SQLite 状态应在本机磁盘，不能放在 NAS/SMB 或同步目录。Windows 用户级实例可放 `%LOCALAPPDATA%\SJFX`；Linux 可放用户本地数据盘。生产已有模型在项目外时，要显式填写模型路径，不能仅移动源码。

## 新建实例

下列命令中的 Python 应替换为该实例使用的 Python 3.12 路径。源码版需要事先准备应用依赖；包含预置运行时和资源的离线包按 [离线安装说明](offline-installation.md) 操作。

Windows 示例：

```powershell
python -X utf8 sjfxctl.py --env-file "D:\SJFX\config\sjfx.env" setup --backend ollama --port 18000 --data-dir "D:\SJFX\state" --output-dir "D:\SJFX\outputs" --models-dir "D:\SJFX\models"
python -X utf8 sjfxctl.py --env-file "D:\SJFX\config\sjfx.env" init-admin
python -X utf8 sjfxctl.py --env-file "D:\SJFX\config\sjfx.env" doctor
python -X utf8 sjfxctl.py --env-file "D:\SJFX\config\sjfx.env" start --open-browser
```

Linux 示例：

```sh
python3 -X utf8 sjfxctl.py --env-file /data/sjfx/config/sjfx.env setup --backend vllm --port 18000 --data-dir /data/sjfx/state --output-dir /data/sjfx/outputs --models-dir /data/sjfx/models
python3 -X utf8 sjfxctl.py --env-file /data/sjfx/config/sjfx.env init-admin
python3 -X utf8 sjfxctl.py --env-file /data/sjfx/config/sjfx.env doctor
python3 -X utf8 sjfxctl.py --env-file /data/sjfx/config/sjfx.env start
```

`setup` 不覆盖已有配置，会生成随机实例 API 认证材料并且不在终端输出它。随后在配置中填好真实模型名、模型路径和端口。扫描原始资料目录时，把需要的目录加入 `SCAN_ALLOWED_ROOTS`；默认只允许本实例上传目录。

全新实例首次启动前执行 `init-admin`，通过两次隐藏输入设置管理员密码。该命令只保存密码哈希，不在参数、终端结果或日志中记录明文；已有账号时拒绝重新初始化。正式离线安装未初始化时 Web 启动会要求先执行此命令，首次登录仍须修改初始密码。升级和重装复用已有账号。

## 日常管理

所有命令使用同一个 `--env-file`：

```text
start           后台启动整套应用
start --open-browser   启动后打开浏览器
run             前台运行控制器，便于调试/服务管理
init-admin      为全新实例交互初始化管理员，不记录密码
status          查看进程及已启用本地模型服务状态
stop            请求本实例控制器停止全部自有进程树
doctor          默认完整部署前置条件检查；缺失必需资源时退出码为 1
doctor --scope runtime   仅判断应用模块与运行目录是否具备启动前置条件
doctor --require-feature msg --require-feature pst   将可选邮件格式列为必需
```

Windows 入口 `start_sjfx.bat --env-file "D:\SJFX\config\sjfx.env"`；旧 `start_demo.bat` 转入同一完整启动流程。Linux 入口 `sh start_sjfx.sh --env-file /data/sjfx/config/sjfx.env`。

启动顺序为本机 `protection-helper` 健康检查与 `protection-executor` 初始化，随后按当前配置启动并等待包内 `embedding`，再启动 Web 建库并就绪，最后等待普通 `worker`、`large-worker` 和已启用的 `qa-worker` 初始化。默认完整配置共有这七个受控角色；外部或关闭向量时不启动 `embedding`，关闭 QA 时不启动 `qa-worker`。全部已选择角色就绪后才报告 `application_ready`，停止和异常退出时回收本实例拥有的进程树。外部生成服务健康另列 `local_model_services`，应用就绪不代替实际模型推理或业务验收。

`doctor` 默认 `--scope full`，报告 `full_prerequisites_ready` 并据此返回退出码；它不会下载或加载权重。`--scope runtime` 只检查启动所需模块是否存在及 data/outputs/parse_temp/logs 的实际创建、写入、读取和替换能力。该范围通过可用于继续启动验收，不能据此判断 PDF OCR、翻译或智能分析可用。

每项检查注明验证级别：`presence`/`module_presence` 是文件、大小、可读性及适用 JSON 结构检查；`write_probe` 是临时文件写入实测；`execution_probe` 是最多 5 秒的原生工具帮助/版本执行探测。工具能执行不代表已经正确解析真实内容。`local_model_services` 只查询模型列表，连通也不代表推理通过；短生成、结构化输出和向量推理需另用 `model-test`。`complete_functional_readiness` 固定为 `null`、`functional_verification` 为 `not_tested`，直到另有真实业务验收证据。旧 `model_resources` 字段仅保留目录清单兼容用途，其 `contains_files` 不参与就绪判断。

普通 PDF/图片解析没有独立禁用开关，完整检查始终要求 Docling Heron 的 `config.json`、`model.safetensors`（至少 150 MiB），TableFormer accurate 或 fast 的权重及 `tm_config.json`，以及 RapidOCR 的 `ch_PP-OCRv5_det_mobile.onnx`、`ch_PP-OCRv5_rec_mobile.onnx`、`ch_ppocr_mobile_v2.0_cls_mobile.onnx`。只放说明文件、空文件或明显截断的权重会失败。邮件显式选择 server OCR 时还要求 `ch_PP-OCRv5_rec_server.onnx`。

启用 `offline_nllb` 翻译时，检查按应用相同规则选择 CT2 或 Transformers 目录。CT2 需要 `model.bin`、`config.json`、tokenizer 配置和 tokenizer 数据，以及 shared 或 source/target vocabulary；Transformers 需要配置、tokenizer 和完整单文件或索引列出的全部分片。`ENABLE_TRANSLATION=0` 只免除该离线翻译检查，仍检查普通解析资源。外部生成/向量服务的本地权重目录由其服务管理，SJFX 按已启用端点检查模型列表。

完整检查还实际执行 antiword、catppt、RAR 所用 7z/7zz，以及邮件 auto/tha 路由所需 Tesseract，并核对 `tha.traineddata`、`eng.traineddata`。MSG 的 `extract-msg` 和 PST 的 `readpst` 默认明确列为可选；需要这两种格式时在命令中加入对应 `--require-feature`，缺失会令完整检查失败。资源齐备仅证明部署前置条件，真实扫描 PDF、表格、旧 Office、邮件和翻译准确性仍需使用代表性资料验收。

启动器管理包内 CPU E5、本机助手、executor 及应用角色，不安装或启动外部 GPU 生成引擎，也不停止用户另外启动的 Ollama/vLLM。现有服务器若使用系统服务管理，应继续使用原管理方式；不要同时用新启动器管理同一状态目录。

Windows 用拥有句柄的 Job Objects 回收完整进程树，包含 venv redirector 已创建的实际解释器；Linux 用私有会话/进程组与父身份监测回收。正常停止有有限退出等待，仍在执行的长任务可能被终止；现有检查点和租约恢复机制继续沿用。控制器异常退出后，再次启动也不应留下占用端口的自有进程。

普通分析与 QA 后台解析都使用当前 RSS 检查内存预算，Windows 不再跳过 Linux `/proc` 才能提供的检查。QA 超过内存预算会按资源不足处理，超时、异常和正常返回均回收所属原生工具后代；SQLite 事务连接在提交或回滚后明确关闭。

进程回收仅接受真实整数且大于 1 的有效 PID，拒绝布尔值、伪整数和溢出编号；无效编号不会传递给系统信号或进程查找接口。

## 依赖与模型入口

- `requirements-app.txt`：共同应用依赖，不安装 vLLM 引擎。
- `requirements-windows.txt`：Windows 应用入口。
- `requirements-vllm.txt`：Linux vLLM 引擎依赖，安装在独立的模型服务环境。
- `requirements.txt`：Windows/Linux 默认应用入口，包含 MSG 和设备签名依赖。

不要把应用与 vLLM 依赖装到同一新环境：vLLM 的 `lark==1.2.2` 与 MSG 解析的 RTFDE 依赖冲突。现有共用环境可以保留，并通过 `SJFX_MSG_PYTHON` 指定独立 MSG 解析环境。`requirements.lock.txt` 保留的是旧 Linux 共用服务器环境快照，未包含完整 MSG 依赖，不能作为新应用环境的完整依赖入口。

这些是构建机使用的依赖约束，不是客户断网现场在线安装指令。Windows/Linux 完整运行时分别准备，当前解释器版本为 3.12.14/3.12.15；原生库、模型资源、来源与哈希随各平台交付清单记录。现有 Linux 锁文件不能直接当成 Windows 锁文件，资源级验证也不代替最终归档的安装验收。

2026-10-14 服务器实际配置已选择 QuickMT（CPU int8、beam5、4线程、batch8），导入自动翻译关闭。源码默认仍为 NLLB，共享翻译由 `TRANSLATION_PROVIDER` 选择，显式文件/所选文件/普通包/大包翻译由 `TRANSLATION_FILE_BACKEND` 选择；两者选择同一离线模型时复用实例，选择不同模型时各自加载明确指定的资源。部署诊断同时检查单独选择的文件翻译资源。邮件中文模式仍走邮件专用模型翻译与审校流程。真实推理可运行和译文质量应分别验收。

模型后端工厂已覆盖普通生成、后台目录命名、主题细化、方向报告及文件翻译。Ollama 文件翻译按客户端能力传参，并保留输出 JSON 校验；禁用选中后端不会隐式切换到另一后端。

旧 Office 工具支持平台 `.exe` 和外置工具根目录。本阶段已在 Windows 11 x64 隔离目录准备 antiword、catppt、7-Zip、readpst 及其 DLL，并实际读取公开 DOC、PPT、RAR、7z、MSG 和 PST 样本。DOC 使用原生 antiword；PPT 使用应用的 OLE 当前版本阅读器。原生 catppt 在两份真实 PPT 上退出码为 0 却返回空正文，未计为功能通过。Docling 对扫描、文字和中文路径表格 PDF 的 CPU 直接推理，以及 NLLB CTranslate2 int8 的实际业务翻译均已通过；Windows PDF 后端使用 PyPDFium 以处理中文安装路径。资源哈希、来源、许可及逐项结果保存在本次验收证据目录。正式安装包、真实 GPU 引擎和安装包在断网新机上的完整冷启动仍须后续整机验收。

邮件泰文/英文 OCR 支持外置 Tesseract 工具及 `tha.traineddata`、`eng.traineddata`，同时保留旧 Linux 资源目录。本阶段已在 Windows 隔离目录准备 Tesseract 5.4.0.20240606 可执行文件、所需 DLL 和两个语言模型，实际运行 `tha+eng` 并精确识别合成图像中的全部英文预算、交付日期和泰文预算句子。该结果证明此环境下真实图像 OCR 可执行；更多字体、画质和邮件附件的准确率仍需按业务资料验收。正式安装包仍须预置这些工具、语言文件与相应许可。

## 可复现平台回归

使用已安装应用依赖的 Python 3.12 执行：

```text
python -X utf8 scripts/run_platform_regression.py --output-dir /absolute/path/to/evidence
```

Windows 同样使用绝对输出路径，如 `D:\SJFX\evidence`。脚本使用临时隔离实例，写出平台/解释器/依赖版本、命令、日志及 JUnit XML，不安装依赖或启动模型引擎。默认覆盖路径、启动停止、进程回收、目录与部署资源、压缩包、旧 Office、邮件 OCR，以及新增 QuickMT 资源加载/后端选择、翻译检查点/FTS/进度和文件保护；`--full` 运行 `tests` 与 `sjfx_protection/tests` 两组完整 Python 用例。跳过用例须保留具体原因，回归结果不替代权重与原生格式的真实功能验收。

GitHub Actions 保留 Ubuntu 的全量 Python 用例与 ruff 门禁，并另设 Ubuntu/Windows 双系统、Python 3.12 平台组合，分别使用 `requirements-app.txt`/`requirements-windows.txt`，Linux 使用 CPU PyTorch。CI 不通过 `requirements-dev.txt` 引入 vLLM 引擎，自动保存两个平台的回归证据；工作流配置尚需远程实际运行后才可称为 CI 已通过。
