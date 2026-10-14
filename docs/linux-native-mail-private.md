# Ubuntu 24.04 私有 PST / MSG 依赖准备

`scripts/install_linux_native_mail.py` 供构建机或隔离验收目录使用。只适用于 Ubuntu 24.04 x64、Linux Python 3.12。脚本不会执行 `apt install`、`apt update` 或系统升级，不会调用生产 venv 的 pip，也不会修改生产配置、生产库或生产 `.venv`。

默认固定 Debian 包 `pst-utils:amd64=0.6.76-1.1build1`、`libpst4t64:amd64=0.6.76-1.1build1`，MSG 使用 `extract-msg==0.56.1`。其中 pst-utils 的已审查 SHA256 为：

```text
aadfc35b93b4aa3a0659a016191f83821d9eee5fc3aa8a9887833a212332b7b5
```

脚本查询现有 apt-cache 的包元数据，使用 `apt-get download` 下载两个指定版本，逐项校验包 SHA256、包名、版本和架构，再以 `dpkg-deb -x` 解包到显式私有根目录。libpst4t64 的预期校验和来自该主机现有 apt-cache 元数据并保存到清单；此步骤依赖已有可信 Ubuntu 包索引。没有索引或指定版本时会失败，不能据此自动安装、升级系统或替换为其他版本。

## 运行前选择隔离路径

下面的路径是示例，执行者应替换为自己的隔离源码和现有应用解释器。`--root` 必须是绝对路径，且目录不存在或为空。不要填写生产目录。最方便的布局是隔离源码的 `.tools/mail-native`；已有 `.tools/7zip` 等其他工具目录可以保留，因为脚本只写新的 `mail-native` 子目录。

```sh
/absolute/app/.venv/bin/python -B -X utf8 /absolute/isolated/source/scripts/install_linux_native_mail.py \
  --root /absolute/isolated/source/.tools/mail-native \
  --application-python /absolute/app/.venv/bin/python
```

`--application-python` 只用于查询应用 site-packages 和创建新 venv，查询使用 `-I -B`。生产解释器、其包目录和库都保持只读。脚本在私有根目录内创建 `msg-venv`，先使用该新 venv 的 pip 安装 extract-msg 与依赖，再在新环境的 site-packages 写入 `sjfx_application_read_only.pth`，引用现有应用 site-packages。pip 使用 `--no-cache-dir --ignore-installed`，安装报告记录包版本、下载 URL 和哈希；它没有通过该 `.pth` 选择、卸载生产依赖的机会。

失败时保留清单和日志供审查；脚本不删除任何目录。重新尝试应另选一个新的空目录，避免把部分安装误认为成功。每条命令默认限时 600 秒，可使用 `--command-timeout` 调整至 1–1800 秒。

## 产出与检查

| 路径（相对 `--root`） | 内容 |
| --- | --- |
| `bin/readpst` | 包装入口，用自己的 `usr/lib/x86_64-linux-gnu` 等私有库路径设置 `LD_LIBRARY_PATH` |
| `usr/bin/readpst`、`usr/lib/...` | 从 Ubuntu deb 解包得到的原生程序与 libpst |
| `usr/share/doc/*/copyright` | 两个 Debian 包的许可证与版权文件 |
| `downloads/*.deb` | 原始下载包，可据清单重验 SHA256 |
| `msg-venv` | 新建的 Linux 独立 MSG / 应用验收环境 |
| `pip-install-report.json` | MSG 依赖版本、来源与下载哈希 |
| `licenses/python` | 新 venv 的许可证文件及 METADATA 副本 |
| `logs`、`manifest.json` | 各命令日志、版本、校验和、库解析结果和构建状态 |

readpst 包装器每次从自身位置确定根目录，明确使用私有库路径，不依赖修改系统动态库缓存。脚本运行 `ldd` 并拒绝任何 `not found`，然后执行 `readpst -V`；如果其他系统库缺失，会报告失败并保留日志，不补装系统依赖。MSG 仅执行 import 检查，必须确认模块路径在新 venv 中。

可在隔离目录复查：

```sh
/absolute/isolated/source/.tools/mail-native/bin/readpst -V
/absolute/isolated/source/.tools/mail-native/msg-venv/bin/python -I -B -c \
  'import extract_msg,importlib.metadata; print(extract_msg.__file__); print(importlib.metadata.version("extract-msg"))'
```

这些只证明工具与模块可用。清单中的 `pst_content_parsing_tested`、`msg_content_parsing_tested`、`functional_parsing_verified` 均为 false，不替代真实 PST/MSG 转换验收。

## 后续隔离业务验收

审查清单与许可证后，由主验收流程在隔离源码中使用新解释器运行真实公开 PST/MSG 样本，验证导出 EML、正文与附件。若采用上述 `.tools/mail-native` 布局，readpst 的实例工具解析器可以找到包装器。MSG 的依赖判断与转换子进程使用当前解释器，因此测试应用和转换代码需使用 `msg-venv/bin/python`。仅切换 readpst 路径不会让原生产解释器自动看见 extract_msg。

独立 venv 通过 `.pth` 引用当前主机应用依赖，只适用于同一 Linux 主机及同一 Python ABI；不要把它复制到 Windows，也不要以 Windows venv 替代 Linux 安装。脚本不会自动切换启动命令或配置，后续采用哪一个解释器、工具目录与部署方案应由验收者明确设置。
