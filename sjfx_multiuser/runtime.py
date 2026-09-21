"""请求级运行时状态（框架无关）。

## 为什么需要这个模块

原始设计想用 `flask.g` 在"视图层"和"归属解析层"之间传当前用户。
但读了 `web_compat.py` 才发现 **sjfx 的 HTTP 层是 FastAPI，没有 `g`**：

```python
class SJFXFastAPI(FastAPI):
    def before_request(self, func): ...     # 只有这个钩子
    # 没有 after_request、没有 g
```

而且 `web_compat` 用的是 `contextvars` 而不是 thread-local：

```python
_request_context: ContextVar["RequestData | None"] = ContextVar(...)
```

## 设计

这里用 `contextvars.ContextVar` —— 与 `web_compat` 同一种机制，
所以在 FastAPI 的 async 环境与 `run_in_threadpool` 里都能正确工作
（thread-local 在 async 下会串请求，是个经典坑）。

**关键约束**：这份状态是**服务端**的。客户端无法通过任何请求参数影响它。
这正是 `authz_source` 决策要保证的 —— 归属绝不能来自客户端输入。
"""

import contextvars

#: 当前请求已认证用户（由认证路由在"身份确定"时设置）
_current_user = contextvars.ContextVar("sjfx_current_user", default=None)

#: 当前请求的错误/调试注记（可选，便于排查）
_note = contextvars.ContextVar("sjfx_note", default=None)


def set_current_user(user):
    """设置当前请求的已认证用户。

    谁调用：
      - 登录视图（成功的登录）—— 因为会话 cookie 是本响应才设上的，
        `request.cookies` 此刻还读不到，不显式设置的话"成功的登录"会没有主体
      - 改密视图 —— 因为会话在响应前就被作废并清了 cookie
      - 归属解析中间件（从有效会话解析出来后）

    user 的形状：至少含 `user_id` / `username` / `role` / `owner_id`。
    """
    _current_user.set(user)


def get_current_user():
    """读当前请求的已认证用户。None 表示未认证（或不在请求上下文里）。"""
    return _current_user.get()


def clear_current_user():
    _current_user.set(None)


def set_note(text):
    _note.set(text)


def get_note():
    return _note.get()


class current_user_scope(object):
    """上下文管理器：临时设置当前用户并在退出时恢复。

    用于 `before_request` 里解析会话 —— 保证请求结束后状态被清掉，
    不会泄漏到下一个请求（contextvars 在协程复用下可能残留，显式清理更稳）。
    """

    def __init__(self, user):
        self.user = user
        self._token = None

    def __enter__(self):
        self._token = _current_user.set(self.user)
        return self.user

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _current_user.reset(self._token)
        return False
