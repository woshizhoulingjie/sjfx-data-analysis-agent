"""归属解析：**整个多用户隔离的安全心脏**。

设计方案 §3.1 说"整个隔离收敛到一个函数"。这个模块就是那个函数。

## 为什么它必须单独成文件并配密集测试

`app.py` 里有 **60 处**调用 `_request_owner_id()`。改这一个函数，
那 60 处同时生效 —— 这是改造最大的杠杆，**也是最大的风险**：
写错一行就是全局越权（A 看到 B 的全部数据）。

所以这里把决策逻辑与 Flask 解耦：`resolve_owner()` 是**纯函数**，
输入是"会话信息"，输出是"该用什么 owner 过滤"。不依赖 request 也能测。

## 三条铁律（改代码前必读）

1. **绝不从客户端输入取 owner。**
   `?as_owner=`、请求头、cookie 里的 owner 一律不可信。只从**服务端会话**派生。
   设计方案里 `authz_source` 这条决策专门防的就是这个。

2. **`None` 表示"不过滤"（看全部）。**
   这是 `services/storage.py` 的既有约定（实测 `scan_owned`：
   `return not owner_id or (row["owner_id"] or "legacy") == owner_id`）。
   **所以任何"意外返回 None"都等于越权** —— 这是本模块最危险的失败模式，
   因此下面每个分支都有明确测试。

3. **无会话时回落到配置值，不是 None。**
   回落成 None 会让所有脚本/后台任务变成"看全部"，那是不可接受的放大。
   回落到 `Config.OWNER_ID` 与改造前行为一致（零回归）。
"""

from typing import Optional, Tuple

ROLE_ADMIN = "admin"
ROLE_USER = "user"

#: 无会话时回落用的 owner 来源标识（仅用于日志/诊断）
FALLBACK_CONFIG = "config"
SOURCE_SESSION = "session"
SOURCE_VIEW_AS = "view_as"
SOURCE_CONFIG_FALLBACK = "config_fallback"


class OwnerDecision(object):
    """一次归属解析的结果。带 `source` 便于排查"为什么看到了/看不到"。"""

    __slots__ = ("owner_id", "source", "user", "note")

    def __init__(self, owner_id, source, user=None, note=None):
        self.owner_id = owner_id
        self.source = source
        self.user = user
        self.note = note

    def __repr__(self):
        return "<OwnerDecision owner=%r source=%s%s>" % (
            self.owner_id, self.source, (" note=" + self.note) if self.note else "")


def resolve_owner(session, viewing_owner, config_owner_id):
    """把"当前请求是谁"解析成"该用什么 owner 过滤"。

    参数
    ----
    session : dict 或 None
        已由 UserStore.resolve_session() 校验过的会话。必须是**服务端派生**的，
        **绝不能**来自客户端输入。None 表示无有效会话。
    viewing_owner : str 或 None
        管理员在会话里设置的"视角"。普通用户传什么都忽略。
    config_owner_id : str 或 None
        配置里的 SJFX_OWNER_ID，用作无会话时的回落。

    返回
    ----
    OwnerDecision(owner_id, source)
        owner_id 为 None 表示"不过滤"（仅管理员）。

    决策表（这是本模块的全部逻辑，每一行都有测试）
    ------------------------------------------------
    | 会话            | 角色   | viewing_owner | 结果                      |
    |-----------------|--------|---------------|---------------------------|
    | 无              | —      | —             | config_owner_id（回落）    |
    | 有              | user   | 任意          | 自己的 owner_id（忽略视角）|
    | 有              | admin  | 无            | None（看全部）            |
    | 有              | admin  | 自己          | None（等价于看全部）       |
    | 有              | admin  | 某用户 owner  | 该 owner                  |
    | 有但角色未知    | ?      | 任意          | 自己的 owner_id（保守）    |
    """
    # ---- 分支 1：无会话 → 回落配置值（绝不用 None）----
    if not session:
        return OwnerDecision(config_owner_id, SOURCE_CONFIG_FALLBACK,
                             note="无有效会话，回落配置值（保持与改造前一致）")

    role = session.get("role")
    own_owner = session.get("owner_id")

    # ---- 分支 2：普通用户 → 永远只有自己，视角参数被忽略 ----
    # 注意：这里**不信任** viewing_owner。普通用户就算构造了它也必须无效。
    if role != ROLE_ADMIN:
        if not own_owner:
            # 会话有效但没有 owner_id —— 数据异常。
            # 保守起见回落到配置值而不是 None（None 会放大成"看全部"）。
            return OwnerDecision(config_owner_id, SOURCE_CONFIG_FALLBACK,
                                 user=session,
                                 note="会话缺少 owner_id（异常），保守回落配置值")
        return OwnerDecision(own_owner, SOURCE_SESSION, user=session)

    # ---- 分支 3：管理员 ----
    if not viewing_owner:
        # 管理员默认看全部 —— None 在 storage 层表示"不过滤"
        return OwnerDecision(None, SOURCE_SESSION, user=session,
                             note="管理员默认视角：全部用户")

    if viewing_owner == own_owner:
        # 管理员"切到自己"等价于看全部（语义更直观，行为一致）
        return OwnerDecision(None, SOURCE_VIEW_AS, user=session,
                             note="管理员视角=自己，等价于全部")

    return OwnerDecision(viewing_owner, SOURCE_VIEW_AS, user=session,
                         note="管理员切换到指定用户视角")


def can_switch_view(session):
    """谁能切换视角。只有管理员。"""
    return bool(session) and session.get("role") == ROLE_ADMIN


def validate_view_target(session, target_owner_id, owner_exists):
    """校验管理员要切到的视角是否合法。

    `owner_exists(owner_id) -> bool` 由调用方注入（通常是
    `user_store.get_by_owner(owner_id) is not None`）。

    返回 (ok: bool, error: str 或 None)
    """
    if not can_switch_view(session):
        return False, "只有管理员可以切换视角"
    if not target_owner_id:
        return True, None              # 空 = 回到"全部"，合法
    if not isinstance(target_owner_id, str) or len(target_owner_id) > 64:
        return False, "视角标识非法"
    if not owner_exists(target_owner_id):
        # 不区分"不存在"与"不该看" —— 避免用视角接口探测有哪些用户
        return False, "目标视角不存在"
    return True, None
