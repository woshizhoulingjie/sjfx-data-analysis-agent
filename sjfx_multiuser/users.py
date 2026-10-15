"""用户与会话：多用户隔离的身份层。

设计目标（对应 sjfx-多用户隔离改造设计方案.md §6）：
  - 用户注册后只能看到自己的数据；管理员可看全局
  - `owner_id` 与登录名**解耦**：登录名可改，归属键永不变
  - 密码用 werkzeug.security（项目已装，实测 scrypt 可用，零新增依赖）

两条**必须遵守**的接口约定（改这里之前先读 §3.1）：
  1. `owner_id` 在注册时生成、**永不修改**。一旦改了，用户就找不到自己的历史数据。
  2. `role` 只有 admin / user 两种，且 admin **不能自助注册**得到。

本模块不做权限判断（那是 routes 的事），只负责"回答当前请求是谁"。
"""

import hashlib
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

try:
    from werkzeug.security import check_password_hash, generate_password_hash
except ImportError:  # 允许在没有 werkzeug 的环境做纯逻辑测试
    generate_password_hash = None
    check_password_hash = None

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

#: 保留名 —— 不许注册。这些名字要么是系统用过的 owner 值，
#: 要么会和内部约定冲突（见设计方案 §6.3）。
RESERVED_USERNAMES = frozenset((
    "admin", "administrator", "root", "system", "sjfx", "primary",
    "legacy", "default", "anonymous", "guest", "null", "none",
    "test", "demo", "api", "token", "owner", "superuser", "sysadmin",
))

#: 用户名规则：3-32 位，字母数字下划线连字符，必须字母开头
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,31}$")
#: owner_id 规则：只允许安全字符（它会进文件路径，必须严）
OWNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

MIN_PASSWORD_LENGTH = 8
SESSION_HOURS_DEFAULT = 168

USER_DDL = """
CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    owner_id       TEXT NOT NULL UNIQUE,
    username       TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user',
    password_hash  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL,
    last_login_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_owner ON users(owner_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    machine_fp     TEXT,
    client_fp      TEXT,
    -- 管理员的"视角"：存在**服务端会话**里，绝不从请求参数取。
    -- 这是 authz_source 决策的核心 —— 从客户端取等于把归属交给客户端。
    viewing_owner  TEXT,
    created_at_utc TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    revoked_at_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at_utc);

-- 注册限流：按 IP 记数，防脚本批量注册（设计方案 §6.3）
CREATE TABLE IF NOT EXISTS register_attempts (
    ip             TEXT NOT NULL,
    at_utc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reg_ip ON register_attempts(ip, at_utc);
"""


class AuthError(Exception):
    """认证/注册相关的可预期错误。消息可以直接给用户看。"""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    """UTC 时间戳，ISO-8601 带 Z，**毫秒精度**（与项目其它时间戳口径一致）。

    ⚠️ 注意毫秒截断带来的边界效应：同一毫秒内写入的记录与查询，
    在 `at_utc >= since` 这类比较下会被视为"同一时刻"。
    对限流功能无影响（真实场景不会同毫秒），但写测试时要注意 ——
    不要断言"0 分钟窗口一定为空"，那是未定义行为（实测会因进程调度而摇摆）。
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(text):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def make_owner_id():
    """生成一个**稳定、不可猜、可安全用于路径**的 owner_id。

    为什么不用用户名作 owner_id：用户名可能想改（设计规格书 §6.2），
    但归属键一旦变了，用户就找不到自己的历史数据了。

    为什么不用纯随机：纯随机会让运维排查困难（看到 `a3f9...` 不知道是谁）。
    折中：`u-` 前缀 + 16 位随机 hex —— 既不可猜，也一眼看出是"用户 owner"。
    """
    return "u-" + secrets.token_hex(8)


def fingerprint_from_request(request):
    """机器指纹：区分"同一账号不同机器"，用于后续的归属存疑判定。

    刻意用粗糙但稳定的组合。**这不是安全边界**，只是相关性信号。
    """
    ua = request.headers.get("User-Agent", "")
    lang = request.headers.get("Accept-Language", "")
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    raw = "|".join([ua, lang, ip])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def client_ip(request):
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")


class UserStore(object):
    """用户与会话的唯一入口。

    每个线程一个连接（sqlite3 连接不能跨线程共享）。
    """

    def __init__(self, db_path):
        self.db_path = os.path.abspath(db_path)
        d = os.path.dirname(self.db_path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o700)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init()

    # ---------------- 基础设施 ----------------

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init(self):
        conn = self._conn()
        conn.executescript(USER_DDL)
        # 轻量迁移：老库（没有 viewing_owner 列）要能继续用。
        # 用 PRAGMA 检查而不是"try ALTER 忽略异常"——后者会把真正的错误也吞掉。
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        if "viewing_owner" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN viewing_owner TEXT")

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------- 校验 ----------------

    @staticmethod
    def validate_username(username, allow_reserved=False):
        """校验用户名。

        `allow_reserved=True` 只给**内部引导流程**用（bootstrap 要建名为 `admin`
        的初始管理员）—— 那个名字恰好在保留名表里。
        对外注册接口**绝不能**传这个参数。
        """
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError("账号需 3-32 位，字母开头，只能包含字母、数字、_ 和 -")
        if not allow_reserved and username.lower() in RESERVED_USERNAMES:
            raise AuthError("该账号名被系统保留，请换一个")
        return username

    @staticmethod
    def validate_password(password):
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError("密码至少 %d 位" % MIN_PASSWORD_LENGTH)
        if password.strip() == "":
            raise AuthError("密码不能全是空白字符")
        return password

    @staticmethod
    def validate_owner_id(owner_id, allow_reserved=False):
        """校验 owner_id。

        `allow_reserved=True` 只给内部引导流程用 —— 初始管理员的 owner_id
        就是配置里的 `primary`，而它在保留名表里（防止普通用户占用）。
        """
        if not OWNER_ID_RE.match(owner_id or ""):
            raise AuthError("owner_id 含非法字符（会用于文件路径，必须严格）")
        if not allow_reserved and owner_id in RESERVED_USERNAMES:
            raise AuthError("owner_id 不能使用保留字")
        return owner_id

    # ---------------- 用户 ----------------

    def user_count(self):
        return self._conn().execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

    def admin_count(self):
        return self._conn().execute(
            "SELECT COUNT(*) AS c FROM users WHERE role=? AND status=?",
            (ROLE_ADMIN, STATUS_ACTIVE)).fetchone()["c"]

    def create_user(self, username, password, display_name=None, role=ROLE_USER,
                    must_change_pw=False, owner_id=None, allow_reserved=False):
        """建号。调用方负责鉴权（注册接口 / 管理员接口）。

        **不在这里做"谁能建管理员"的判断** —— 那是路由层的事。
        但这里保证：owner_id 唯一且合法、role 合法、密码合规、用户名唯一。

        `allow_reserved` 只给内部引导流程用（bootstrap 要建 `admin` 用户、
        其 owner_id 是 `primary`，两者都在保留名表里）。**对外注册接口绝不能传。**

        用户名唯一性检查**不区分大小写**（lower() 比较）。
        原因：保留名检查是大小写不敏感的，若重名检查敏感，就会出现
        `Alice` 与 `alice` 共存 —— 既让用户困惑，也留了冒充空间。
        """
        username = self.validate_username(username, allow_reserved=allow_reserved)
        self.validate_password(password)
        if role not in ROLES:
            raise AuthError("非法角色: %r" % role)

        owner_id = (self.validate_owner_id(owner_id, allow_reserved=allow_reserved)
                    if owner_id else make_owner_id())
        user_id = secrets.token_hex(8)

        conn = self._conn()
        with self._lock:
            # lower(username) 比较：与保留名检查的大小写口径保持一致
            if conn.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)",
                            (username,)).fetchone():
                raise AuthError("账号已存在（账号名不区分大小写）")
            if conn.execute("SELECT 1 FROM users WHERE owner_id=?", (owner_id,)).fetchone():
                raise AuthError("归属空间已被占用（owner_id 冲突）")
            conn.execute(
                "INSERT INTO users (user_id, owner_id, username, display_name, role, "
                "password_hash, status, must_change_pw, created_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, owner_id, username, display_name or username, role,
                 generate_password_hash(password), STATUS_ACTIVE,
                 1 if must_change_pw else 0, _iso(_now())))
        return self.get_user(user_id)

    def bootstrap_first_admin(self, owner_id=None, username="admin", password=None):
        """首次启动时创建唯一的管理员。

        幂等：已有任何用户时返回 None。

        **关键设计（设计方案 §8.4 方案 A）**：默认 `owner_id` 就是配置里那个值
        （通常是 `primary`），这样现有的 295 个扫描 / 409 个产物 **不需要任何数据迁移**
        就直接归管理员所有。
        """
        if self.user_count() > 0:
            return None
        password = password or secrets.token_urlsafe(12)
        owner_id = owner_id or "primary"
        user = self.create_user(username=username, password=password,
                                display_name="管理员", role=ROLE_ADMIN,
                                must_change_pw=True, owner_id=owner_id,
                                # `admin` 在保留名表里（防止用户注册时抢占），
                                # 但引导流程建的就是它 —— 所以这里显式放行。
                                allow_reserved=True)
        user["initial_password"] = password
        return user

    def get_user(self, user_id):
        row = self._conn().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_by_username(self, username):
        """按登录名查用户。**不区分大小写**，与重名检查口径一致。"""
        row = self._conn().execute(
            "SELECT * FROM users WHERE lower(username)=lower(?)",
            ((username or "").strip(),)).fetchone()
        return dict(row) if row else None

    def get_by_owner(self, owner_id):
        row = self._conn().execute("SELECT * FROM users WHERE owner_id=?", (owner_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self):
        return [dict(r) for r in self._conn().execute(
            "SELECT user_id, owner_id, username, display_name, role, status, "
            "created_at_utc, last_login_utc FROM users ORDER BY created_at_utc").fetchall()]

    def set_password(self, user_id, password, must_change_pw=False):
        self.validate_password(password)
        with self._lock:
            self._conn().execute(
                "UPDATE users SET password_hash=?, must_change_pw=? WHERE user_id=?",
                (generate_password_hash(password), 1 if must_change_pw else 0, user_id))

    def set_status(self, user_id, status):
        if status not in (STATUS_ACTIVE, STATUS_DISABLED):
            raise AuthError("非法状态: %r" % status)
        with self._lock:
            self._conn().execute("UPDATE users SET status=? WHERE user_id=?",
                                 (status, user_id))

    def set_role(self, user_id, role):
        if role not in ROLES:
            raise AuthError("非法角色: %r" % role)
        with self._lock:
            self._conn().execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))

    # ---------------- 登录 ----------------

    def authenticate(self, username, password):
        """校验账号密码。失败返回 None。

        **刻意不区分"账号不存在"与"密码错误"** —— 调用方应统一返回同一句话，
        否则登录接口会变成账号枚举器（设计方案 §6.4）。
        """
        user = self.get_by_username((username or "").strip())
        if user is None:
            # 恒定时间：账号不存在也做一次哈希校验，避免通过响应时间枚举账号
            if check_password_hash is not None:
                try:
                    check_password_hash(
                        "scrypt:32768:8:1$AAAAAAAAAAAAAAAA$" + "0" * 128, password or "")
                except Exception:  # noqa: BLE001
                    pass
            return None
        if user["status"] != STATUS_ACTIVE:
            return None
        if check_password_hash is None:
            return None
        if not check_password_hash(user["password_hash"], password or ""):
            return None
        return user

    def login(self, username, password, machine_fp=None, hours=SESSION_HOURS_DEFAULT):
        user = self.authenticate(username, password)
        if user is None:
            raise AuthError("账号或密码错误")

        session_id = secrets.token_urlsafe(24)
        now = _now()
        with self._lock:
            self._conn().execute(
                "INSERT INTO sessions (session_id, user_id, machine_fp, created_at_utc, "
                "expires_at_utc) VALUES (?,?,?,?,?)",
                (session_id, user["user_id"], machine_fp, _iso(now),
                 _iso(now + timedelta(hours=hours))))
            self._conn().execute("UPDATE users SET last_login_utc=? WHERE user_id=?",
                                 (_iso(now), user["user_id"]))
        return {"session_id": session_id, "user": user,
                "expires_at_utc": _iso(now + timedelta(hours=hours)),
                "must_change_pw": bool(user["must_change_pw"])}

    def resolve_session(self, session_id):
        """把会话 cookie 解析成用户。返回 None 表示无效/过期/被撤销/已停用。"""
        if not session_id:
            return None
        row = self._conn().execute(
            "SELECT s.*, u.username, u.display_name, u.role, u.owner_id, "
            "u.status AS user_status, u.must_change_pw "
            "FROM sessions s JOIN users u ON u.user_id=s.user_id "
            "WHERE s.session_id=?", (session_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        if row.get("revoked_at_utc"):
            return None
        exp = _parse_iso(row.get("expires_at_utc"))
        if exp and exp <= _now():
            return None
        if row.get("user_status") != STATUS_ACTIVE:
            return None
        return row

    def logout(self, session_id):
        with self._lock:
            self._conn().execute("UPDATE sessions SET revoked_at_utc=? WHERE session_id=?",
                                 (_iso(_now()), session_id))

    def revoke_user_sessions(self, user_id, keep_session_id=None):
        """作废某用户全部会话（改密/停用后调用）。

        为什么必须作废：密码泄露后改密，若不作废旧会话，
        已经进来的人照样能继续操作 —— 改密就失去了意义。
        """
        with self._lock:
            if keep_session_id:
                self._conn().execute(
                    "UPDATE sessions SET revoked_at_utc=? WHERE user_id=? "
                    "AND revoked_at_utc IS NULL AND session_id<>?",
                    (_iso(_now()), user_id, keep_session_id))
            else:
                self._conn().execute(
                    "UPDATE sessions SET revoked_at_utc=? WHERE user_id=? "
                    "AND revoked_at_utc IS NULL", (_iso(_now()), user_id))

    def purge_expired_sessions(self, days_old=7):
        """清理过期会话行（防表无限增长）。"""
        cutoff = _iso(_now() - timedelta(days=days_old))
        with self._lock:
            cur = self._conn().execute(
                "DELETE FROM sessions WHERE expires_at_utc < ?", (cutoff,))
            return cur.rowcount

    # ---------------- 管理员视角（存在服务端会话里） ----------------

    def set_viewing_owner(self, session_id, viewing_owner):
        """设置管理员当前视角。

        **为什么存在服务端会话表而不是让客户端传参数**：
        视角决定"看到谁的数据"。若从请求参数取，普通用户只要构造
        `?as_owner=别人的id` 就能越权 —— 这就是 `authz_source` 决策要防的全局越权。
        存在服务端，客户端改不动它。
        """
        with self._lock:
            self._conn().execute(
                "UPDATE sessions SET viewing_owner=? WHERE session_id=?",
                (viewing_owner or None, session_id))

    def get_viewing_owner(self, session_id):
        row = self._conn().execute(
            "SELECT viewing_owner FROM sessions WHERE session_id=?",
            (session_id,)).fetchone()
        return row["viewing_owner"] if row else None

    # ---------------- 注册限流 ----------------

    def record_register_attempt(self, ip):
        with self._lock:
            self._conn().execute("INSERT INTO register_attempts (ip, at_utc) VALUES (?,?)",
                                 (ip or "unknown", _iso(_now())))

    def register_attempts_recent(self, ip, minutes=60):
        since = _iso(_now() - timedelta(minutes=minutes))
        return self._conn().execute(
            "SELECT COUNT(*) AS c FROM register_attempts WHERE ip=? AND at_utc>=?",
            (ip or "unknown", since)).fetchone()["c"]

    def purge_register_attempts(self, hours_old=24):
        cutoff = _iso(_now() - timedelta(hours=hours_old))
        with self._lock:
            cur = self._conn().execute(
                "DELETE FROM register_attempts WHERE at_utc < ?", (cutoff,))
            return cur.rowcount
