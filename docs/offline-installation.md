# SJFX 离线安装、升级与卸载

使用与目标系统一致的 `windows-x64.zip` 或 `linux-x64.tar.gz`。包内包含应用、CPython 3.12、应用依赖、CPU 文档解析/OCR、QuickMT 英中/中英/泰英翻译资源、multilingual-e5-small 向量模型与 CPU 服务、桌面文件保护助手和执行器、ClamAV 引擎与签名库，以及原生 Office、邮件、压缩包工具。安装过程不运行 pip，不下载依赖或权重。

默认向量服务为本机 `127.0.0.1:18083/v1` 的 multilingual-e5-small，由启动器管理，输出 384 维 E5 向量。管理员选择其他向量服务后，启动器按保存的配置在重启时决定是否启动内置 E5。生成模型通过独立接口选择，默认尚未启用；本包不含服务器 vLLM Qwen3.6、Ollama 或生成模型权重，所选生成引擎需预先离线安装或在可达内网提供。QuickMT 的泰文到中文经过英文中转。

本次交付面向 Windows 11 x64 与 Ubuntu 24.04 x86_64/glibc 2.39。其他系统、ARM 和客户 GPU 服务需另验收。Windows 包使用 CPython 3.12.14，Linux 包使用可迁移的 CPython 3.12.15；无需预装系统 Python。实际验证范围、归档 SHA256、资源来源与限制以随包的交付报告为准。

## 安装前

保留原始压缩包及其 SHA256 文件。解压到独立目录，不与最终程序目录或实例数据目录重叠。程序目录存放应用、运行时及只读资源；实例目录存放配置、数据库、日志、缓存、上传和导出。实例必须在可写本地磁盘，SQLite 不应放在网络共享或同步目录。首次安装需为解压包和程序副本预留空间；升级还保留上一份程序备份。

安装器会严格检查每个文件的 SHA256、大小和清单，发现缺失、改动或未列出的文件会停止；拒绝符号链接和越界路径。Linux 另比较 POSIX mode；Windows 不校验 POSIX mode，安装后为程序文件设置只读属性。清单校验用于检查内容完整性；交付归档的 SHA256 应通过可信渠道核对。

## Windows

在已解压包根目录打开 PowerShell，使用当前用户可写的目录：

```powershell
$program = Join-Path $env:LOCALAPPDATA 'SJFX-App'
$instance = Join-Path $env:LOCALAPPDATA 'SJFX-Data'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -InstallDir $program -DataDir $instance
& "$program\runtime\python.exe" -B -X utf8 "$program\app\sjfxctl.py" --env-file "$instance\config\sjfx.env" init-admin
& "$program\runtime\python.exe" -B -X utf8 "$program\app\sjfxctl.py" --env-file "$instance\config\sjfx.env" doctor --scope full --require-feature msg --require-feature pst
& "$program\start_sjfx.bat" --env-file "$instance\config\sjfx.env"
```

命令中的临时执行策略只作用于该 PowerShell 进程。安装到 Program Files 等受保护目录时，需具有该目录的写入权限。

解压目录和程序目录应使用较短的本机路径，避免多层时间戳或嵌套目录。实测部分 Windows 原生 DLL 在深路径中会报“文件名或扩展名太长”；上面的 LocalAppData 默认位置和较短的 `C:\Apps\SJFX` 位置可避免这种路径长度限制。

## Linux

用 `tar` 解压以保留执行权限。在解压包根目录运行：

```sh
export SJFX_INSTALL_DIR="$HOME/.local/opt/sjfx"
export SJFX_DATA_DIR="$HOME/.local/share/sjfx"
sh ./install_linux.sh
"$SJFX_INSTALL_DIR/runtime/bin/python3" -B -X utf8 "$SJFX_INSTALL_DIR/app/sjfxctl.py" --env-file "$SJFX_DATA_DIR/config/sjfx.env" init-admin
"$SJFX_INSTALL_DIR/runtime/bin/python3" -B -X utf8 "$SJFX_INSTALL_DIR/app/sjfxctl.py" --env-file "$SJFX_DATA_DIR/config/sjfx.env" doctor --scope full --require-feature msg --require-feature pst
"$SJFX_INSTALL_DIR/start_sjfx.sh" --env-file "$SJFX_DATA_DIR/config/sjfx.env"
```

安装到普通用户可写目录无需 root。原生工具通过各自的私有库目录运行；不要把工具库目录加入整个 Python 进程的 `LD_LIBRARY_PATH`。

## 首次使用与日常管理

实例配置为 `<实例目录>/config/sjfx.env`。仅在全新实例首次使用时执行 `init-admin`，通过隐藏输入设置管理员密码；该命令只保存密码哈希，不输出或记录明文，已有账号时拒绝重新初始化。首次启动后访问 `http://127.0.0.1:18000`，使用 `admin` 登录并修改初始密码。升级和重装复用原账号。日志位于 `<实例目录>/state/logs`，配置及安装身份文件由实例所有者保管。

管理员在“系统设置 → 模型服务配置”中填写实际生成服务的地址和模型名并启用，执行生成/结构化输出测试，保存后重启全部应用角色。向量服务已有内置默认值，也可独立切换。模型列表可达不等于推理已通过。涉及内网或服务器服务时，请求所需业务内容会发送到指定地址，详见 `app/docs/local-model-interface.md`。

扫描资料前在配置的 `SCAN_ALLOWED_ROOTS` 中加入允许的目录；默认仅允许实例上传目录。Windows 多个根用分号分隔，Linux 用冒号分隔。始终显式指定同一个实例配置。

使用包内解释器执行 `app/sjfxctl.py --env-file <配置绝对路径> status` 或 `stop --timeout 30`。默认启动包含 Web、普通 worker、大包 worker、QA worker、保护助手、保护执行器和 E5 七个角色；使用外部或禁用向量配置时没有内置 E5 进程。启动器停止或异常退出时清理其拥有的进程。`doctor` 检查前置条件，内容解析、译文质量和生成回答仍需实际材料验收。

文件保护在新实例中默认启用。身份、状态、清单、处置队列和履历位于 `state/desktop_protection` 与 `state/file_protection`。安装器将初始病毒签名库复制到实例的 `models/protection/clamav`，后续离线更新应在服务停止后使用经验证的完整签名库替换该可写目录并保留上一版本；升级程序保留已有签名库。不得把未完成的扫描当作安全回执。程序访问审计依赖 Windows 相应权限或 Linux auditd 配置，健康报告会明确其实际覆盖范围；普通用户运行不代表已获得系统级审计覆盖。

## 升级、程序路径迁移与恢复

先停止正在进行的重要任务，再使用同一实例配置执行 `stop --timeout 30` 并确认成功，最后备份完整实例目录（包括 SQLite sidecar）；备份完成后才升级。在新版本解压包根目录运行 `upgrade_windows.ps1`（同安装命令的 `-InstallDir`、`-DataDir`）或 `upgrade_linux.sh`（同安装环境变量）。安装器停止旧实例，替换程序目录，保留原配置、数据库和输出；旧程序保留为 `.backup-*`。

更改程序路径时，使用新的程序目录和原实例目录重新运行安装器。安装器根据实例的 `config/install-root.txt` 定位并停止旧程序，迁移原来指向旧程序的模型、工具、分词器和保护助手路径；自定义外部资源路径与模型选择保留。迁移后应检查新配置、运行 `doctor` 并启动验收，再处理旧程序目录。

安装阶段失败会尝试恢复旧程序和安装元数据。已成功升级后的业务回退需要配套实例备份，不能仅凭旧程序目录保证数据库向后兼容。

## 卸载

Windows 先将 PowerShell 的当前目录切换到程序目录之外（例如 `Set-Location -LiteralPath $env:USERPROFILE`），再通过脚本绝对路径运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<程序绝对路径>\uninstall_windows.ps1" -InstallDir "<程序绝对路径>" -DataDir "<实例绝对路径>"`；Linux 设置同一组安装环境变量后运行 `sh <程序绝对路径>/uninstall_linux.sh keep-data`。默认仅删除程序，保留全部实例数据，重新安装时可复用原实例。

Windows 的 `-PurgeData` 或 Linux 的 `purge-data` 会同时删除实例。仅在已备份且明确不再需要数据时使用。历史 `.backup-*` 和迁移前程序目录需另行处理，确认路径后再删除。

## 资源与许可

保留包内各模型、工具和运行时的许可证、来源记录及修改说明。E5 使用 MIT 许可，QuickMT 权重附 CC BY 4.0 声明；本包不含具有非商业许可限制的 NLLB 权重。ClamAV 与原生工具包含 GPL/LGPL 等不同许可；资源保留其通知、来源记录及为本次修改保存的源码和补丁，再分发时需遵循相应许可。
