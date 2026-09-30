"""测试进程的环境隔离。

`config.load_local_env()` 在没有 `SJFX_ENV_FILE` 时会读 `DEFAULT_STATE_DIR/.env`；
而本仓库放在 NFS 上，`DEFAULT_STATE_DIR` 会落到 `~/.local/state/...` ——
也就是**生产**的 .env。于是即使设了 `SJFX_STATE_DIR`，测试也会把生产开关
（如 `SJFX_LP_NAMING_SAME_PACKAGE_LANES=2`）带进来，用例在测"这台机器的部署参数"
而不是代码本身（实测 test_model_dispatch / test_naming_parallel_lanes 因此变红）。

这里在任何测试模块 import config 之前，把 `SJFX_ENV_FILE` 指向一个空文件：
候选列表第一项存在且为空 → 不读任何 .env。子进程（test_startup_contract 等用
`os.environ.copy()`）也会继承这一设置。显式设置过 `SJFX_ENV_FILE` 的调用方不受影响。
"""
import atexit
import os
import tempfile

if not str(os.environ.get("SJFX_ENV_FILE") or "").strip():
    _fd, _empty_env = tempfile.mkstemp(prefix="sjfx-test-", suffix=".env")
    os.close(_fd)
    os.environ["SJFX_ENV_FILE"] = _empty_env

    def _cleanup(path=_empty_env):
        try:
            os.unlink(path)
        except OSError:
            pass

    atexit.register(_cleanup)
