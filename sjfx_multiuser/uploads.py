"""可断点续传的分片上传管理器（框架无关，可本地测试）。

## 为什么必须分片 + 断点续传

实测约束：`app.py:136` 把 `max_content_length` 设成 **4MB**，
`web_compat._read_request_body()` 超限直接抛 `_RequestBodyTooLarge` → 413。

而用户的场景是"U 盘里约 100G 数据"。按 4MB 分片，那是 **25600 个请求**。
在真实网络上传这么久，**中途断开是必然事件**，不是可能性。
所以"断点续传"不是优化，是**可用性的前提** ——
没有它，用户传到 80% 断一次就要从头再来。

## 设计

```
<root>/<owner_id>/<session_id>/
    session.json          ← 清单：文件列表、每个文件的分片状态
    files/
        <file_index>.part  ← 正在接收的文件（按分片顺序 append）
        <file_index>.done  ← 已收完（原子改名，出现即代表完整）
```

**关键设计选择**

1. **状态落盘，不只在内存** —— 服务重启后仍能续传。
   否则运维重启一次，用户的上传就全废了。

2. **分片按顺序 append，不预分配** —— 因为浏览器/HTTP 不保证到达顺序，
   而"随机写"需要 seek 到偏移。这里要求**分片按序提交**（index 必须等于当前段数），
   代价是客户端必须串行发送，收益是服务端逻辑极简且天然防重放/防乱序。
   > 对 100G 场景，串行发送本来就是唯一可行的（带宽是瓶颈，不是并发）。

3. **完成后原子改名 `.part` → `.done`** —— 出现 `.done` 就代表文件完整，
   组装阶段不需要再校验长度（虽然仍会校验）。

4. **每个分片带 SHA-256 校验** —— 网络传输损坏是真实的，
   而"静默损坏"的数据比"失败"更糟（会污染后续分析）。

5. **路径安全是硬要求** —— 文件名来自客户端，必须严格校验，
   否则 `../../etc/passwd` 就能写到任意位置。

## 不做的事

- 不做秒传/去重（那需要先算全文件哈希，与"边传边处理"冲突）
- 不做并行分片（见第 2 条）
- 不做 resumable.js 那套复杂协议（自研更可控，且能对齐 owner 隔离）
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time

#: 单片上限（字节）。必须 <= app.py 的 max_content_length（实测 4MB）。
#: 留 64KB 余量给 multipart/头部开销。
MAX_CHUNK_BYTES = 4 * 1024 * 1024 - 65536

#: 单文件上限（默认 10GiB，与 Config.MAX_SINGLE_FILE_BYTES 的默认一致）
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024

#: 段内不允许出现的字符：路径分隔符与控制字符。
#: 只能用来检查**单个路径段** —— 归一化后的整串路径本来就含 /，
#: 拿这个正则去匹配整串会把所有正常的多级路径全部误拒。
_UNSAFE_SEG = re.compile(r"[/\\\x00-\x1f]")
_DOTS_ONLY = re.compile(r"^\.+$")

#: session_id / owner_id 允许的字符（它们会进路径）
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class UploadError(Exception):
    """可预期的上传错误，消息可以直接给用户看。"""


def safe_rel_path(rel_path):
    """把客户端给的相对路径规范化，或抛错。

    这是**安全边界**：`rel_path` 完全来自客户端，
    不校验就能用 `../../../` 写到服务器任意位置。

    规则：
      - 拒绝空、拒绝绝对路径、拒绝盘符（`C:`）
      - 拒绝 `..` 段
      - 反斜杠统一成正斜杠（U 盘来自 Windows，路径是 `a\\b\\c`）
      - 逐段拒绝控制字符、限制段长度
    """
    if not rel_path or not isinstance(rel_path, str):
        raise UploadError("文件路径为空")
    raw = rel_path.replace("\\", "/").strip()
    if not raw:
        raise UploadError("文件路径为空")
    if len(raw) > 1024:
        raise UploadError("文件路径过长")
    if raw.startswith("/"):
        raise UploadError("不允许绝对路径")
    if re.match(r"^[A-Za-z]:", raw):
        raise UploadError("不允许盘符路径")

    parts = []
    for seg in raw.split("/"):
        seg = seg.strip()
        if not seg or seg == ".":
            continue
        if seg == "..":
            raise UploadError("文件路径不允许包含 ..")
        # 逐段检查：走到这里分隔符已经消解完，
        # 段内再出现 / \\ 或控制字符就一定是伪造的。
        # 注意不能拿这个正则去匹配归一化后的整串 —— 整串本来就含 /，
        # 那样会把所有正常的多级路径（包括中文名）全部误拒。
        if _UNSAFE_SEG.search(seg):
            raise UploadError("文件路径含非法字符")
        if len(seg.encode("utf-8")) > 255:
            raise UploadError("文件名过长（超过 255 字节）")
        parts.append(seg)
    if not parts:
        raise UploadError("文件路径无效")
    return "/".join(parts)


def _check_id(value, label):
    if not _SAFE_ID.match(value or ""):
        raise UploadError("%s 含非法字符" % label)
    return value


class UploadManager(object):
    """管理一批文件的分片上传。

    线程安全：每个进程一个实例，内部用文件锁语义（原子改名 + 顺序 append）保证一致。
    多进程并发同一 session 不在支持范围内（上传是单用户串行的）。
    """

    def __init__(self, root, max_file_bytes=DEFAULT_MAX_FILE_BYTES,
                 max_chunk_bytes=MAX_CHUNK_BYTES, session_ttl_seconds=7 * 86400):
        self.root = os.path.abspath(root)
        self.max_file_bytes = int(max_file_bytes)
        self.max_chunk_bytes = int(max_chunk_bytes)
        self.session_ttl_seconds = int(session_ttl_seconds)
        os.makedirs(self.root, mode=0o700, exist_ok=True)

    # ---------------- 路径 ----------------

    def _session_dir(self, owner_id, session_id):
        _check_id(owner_id, "owner_id")
        _check_id(session_id, "session_id")
        return os.path.join(self.root, owner_id, session_id)

    def _manifest_path(self, owner_id, session_id):
        return os.path.join(self._session_dir(owner_id, session_id), "session.json")

    def _load(self, owner_id, session_id):
        path = self._manifest_path(owner_id, session_id)
        if not os.path.isfile(path):
            raise UploadError("上传会话不存在或已过期")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            raise UploadError("上传会话清单损坏：%r" % exc)

    def _save(self, owner_id, session_id, manifest):
        manifest["updated_at"] = time.time()
        path = self._manifest_path(owner_id, session_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # 原子：清单不会被读成半截

    # ---------------- 会话 ----------------

    def begin(self, owner_id, session_id, files, package_name=None):
        """建立上传会话。

        files: [{"rel_path": "...", "size": 123, "sha256": "..."(可选)}]
        返回会话清单（含每个文件的分片数）。
        """
        _check_id(owner_id, "owner_id")
        _check_id(session_id, "session_id")
        if not files:
            raise UploadError("文件列表为空")

        seen = set()
        prepared = []
        total_bytes = 0
        for item in files:
            rel = safe_rel_path(item.get("rel_path"))
            if rel in seen:
                raise UploadError("文件列表里有重复路径：%s" % rel)
            seen.add(rel)
            size = item.get("size")
            try:
                size = int(size)
            except (TypeError, ValueError):
                raise UploadError("文件 %s 缺少合法大小" % rel)
            if size < 0:
                raise UploadError("文件 %s 大小非法" % rel)
            if size > self.max_file_bytes:
                raise UploadError("文件 %s 超过单文件上限（%d 字节）"
                                  % (rel, self.max_file_bytes))
            total_bytes += size
            chunks_total = ((size + self.max_chunk_bytes - 1) // self.max_chunk_bytes
                            if size else 0)
            prepared.append({
                "rel_path": rel,
                "size": size,
                "sha256": (item.get("sha256") or "").lower() or None,
                "chunks_total": chunks_total,
                "chunks_done": 0,
                "received_bytes": 0,
                # 0 字节文件天然完整 —— 它没有任何分片可传。
                # 早先把 chunks_total==0 但 state 仍留成 pending，
                # 结果是它永远无法被标记完成，整个会话卡死在组装这一步。
                # 空文件在 U 盘里是常态（Thumbs.db、占位文件），必须支持。
                "state": "complete" if size == 0 else "pending",
            })

        sdir = self._session_dir(owner_id, session_id)
        os.makedirs(os.path.join(sdir, "files"), mode=0o700, exist_ok=True)

        manifest = {
            "schema": 1,
            "session_id": session_id,
            "owner_id": owner_id,
            "package_name": package_name,
            "created_at": time.time(),
            "updated_at": time.time(),
            "max_chunk_bytes": self.max_chunk_bytes,
            "file_count": len(prepared),
            "total_bytes": total_bytes,
            "files": prepared,
        }
        self._save(owner_id, session_id, manifest)
        return manifest

    def status(self, owner_id, session_id):
        """返回进度快照（用于断点续传：客户端据此知道哪些文件已完成）。"""
        m = self._load(owner_id, session_id)
        done = sum(1 for f in m["files"] if f["state"] == "complete")
        got = sum(f["received_bytes"] for f in m["files"])
        return {
            "session_id": m["session_id"],
            "file_count": m["file_count"],
            "files_complete": done,
            "total_bytes": m["total_bytes"],
            "received_bytes": got,
            "max_chunk_bytes": m["max_chunk_bytes"],
            "complete": done == m["file_count"],
            "files": [{
                "rel_path": f["rel_path"], "size": f["size"],
                "chunks_total": f["chunks_total"], "chunks_done": f["chunks_done"],
                "received_bytes": f["received_bytes"], "state": f["state"],
            } for f in m["files"]],
        }

    # ---------------- 分片写入 ----------------

    def put_chunk(self, owner_id, session_id, file_index, chunk_index, data,
                  chunk_sha256=None):
        """接收一个分片。

        **要求分片按序到达**（chunk_index 必须等于当前的 chunks_done）。
        理由见模块文档第 2 条：串行发送是 100G 场景下唯一可行的方式，
        而按序 append 让"重放/乱序/越界"这三类问题自然消失。

        幂等性：重复提交同一个 chunk_index 会返回 `already=True` 而不重复写入
        —— 这对"客户端超时重试但服务端其实已收到"的场景很重要。

        `chunk_index` 可以传 `None`，表示"就传下一片"（等价于当前的 chunks_done）。
        这不是便利糖，而是**安全默认**：并发/乱序下客户端自己维护的索引一旦算错，
        服务端会把重复索引当成"重试"而静默跳过，用户就会**无声地少收数据**。
        服务端本来就知道期望值，让它自己决定比信任客户端更安全。
        """
        m = self._load(owner_id, session_id)
        try:
            file_index = int(file_index)
        except (TypeError, ValueError):
            raise UploadError("file_index 非法")
        if file_index < 0 or file_index >= len(m["files"]):
            raise UploadError("file_index 越界")
        entry = m["files"][file_index]

        # 长度校验**要在写入之前**做。
        # 早先是等 chunks_done 追平 chunks_total 才校验，于是"声明 30 字节却传 50"
        # 这种提前溢出会先把 20 字节垃圾写进 .part 才报错。
        received = entry["received_bytes"]

        if chunk_index is None:
            chunk_index = entry["chunks_done"]          # 下一个期待的分片
        else:
            try:
                chunk_index = int(chunk_index)
            except (TypeError, ValueError):
                raise UploadError("chunk_index 非法")

        expected = entry["chunks_done"]

        # 文件已收完 —— 只接受"重发最后一片"，其余一律拒绝。
        # 为什么必须接受它：最后一片的响应在网络上丢了是**必然事件**，
        # 客户端会重试；而 state=="complete" 的提前返回正是唯一没有幂等兜底的分支，
        # 会把这类重试判成"乱序"报错，让用户卡在 99.99%。
        if entry["state"] == "complete":
            if chunk_index == expected - 1:
                return {"already": True, "file_complete": True,
                        "chunks_done": expected, "chunks_total": entry["chunks_total"],
                        "session_complete": all(f["state"] == "complete"
                                                for f in m["files"])}
            raise UploadError("该文件已收完")

        data = data or b""
        if not data:
            raise UploadError("分片内容为空")
        if len(data) > self.max_chunk_bytes:
            raise UploadError("分片超过上限（%d 字节）" % self.max_chunk_bytes)

        # 每片校验：静默损坏的数据比失败更糟（会污染后续分析）
        if chunk_sha256:
            got = hashlib.sha256(data).hexdigest()
            if got.lower() != str(chunk_sha256).lower():
                raise UploadError("分片校验失败（期望 %s，实际 %s）"
                                  % (str(chunk_sha256)[:12], got[:12]))

        if chunk_index < expected:
            # 已经收过这一段 —— 视为重试，不重复写入
            return {"already": True, "chunks_done": expected,
                    "chunks_total": entry["chunks_total"]}
        if chunk_index > expected:
            raise UploadError("分片乱序：期望 %d，收到 %d" % (expected, chunk_index))
        if expected >= entry["chunks_total"]:
            raise UploadError("该文件分片已收完")
        if received + len(data) > entry["size"]:
            raise UploadError("文件 %s 收到的字节超出声明大小（%d + %d > %d）"
                              % (entry["rel_path"], received, len(data), entry["size"]))

        # 写到 .part（append）
        sdir = self._session_dir(owner_id, session_id)
        part = os.path.join(sdir, "files", "%d.part" % file_index)
        # 上次因长度不符回滚过状态时，.part 里可能残留多余字节 —— 对齐到已确认长度
        if os.path.isfile(part) and os.path.getsize(part) != received:
            with open(part, "r+b") as fh:
                fh.truncate(received)
                fh.flush()
                os.fsync(fh.fileno())
        with open(part, "ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())      # 断电也不丢已确认的分片

        entry["chunks_done"] = expected + 1
        entry["received_bytes"] = received + len(data)

        # 收完 → 校验总长、原子改名 .done
        completed = False
        if entry["chunks_done"] >= entry["chunks_total"]:
            actual = os.path.getsize(part)
            if actual != entry["size"]:
                # 长度不符：**状态和文件都要回滚**，让客户端能干净地重传。
                # 早先只回滚了清单、没截断 .part，残留字节会把重传的文件永久污染
                # —— 而这正是"分片校验失败"最想防的静默损坏。
                entry["chunks_done"] = expected
                entry["received_bytes"] = received
                with open(part, "r+b") as fh:
                    fh.truncate(received)
                    fh.flush()
                    os.fsync(fh.fileno())
                self._save(owner_id, session_id, m)
                raise UploadError("文件 %s 长度不符（期望 %d，实际 %d）"
                                  % (entry["rel_path"], entry["size"], actual))
            done_path = os.path.join(sdir, "files", "%d.done" % file_index)
            os.replace(part, done_path)
            entry["state"] = "complete"
            completed = True

        self._save(owner_id, session_id, m)
        return {
            "already": False,
            "chunks_done": entry["chunks_done"],
            "chunks_total": entry["chunks_total"],
            "file_complete": completed,
            "session_complete": all(f["state"] == "complete" for f in m["files"]),
        }

    # ---------------- 组装 ----------------

    def assemble(self, owner_id, session_id, dest_dir, allowed_root=None):
        """Assemble all .done files into the destination and support finalize retries."""
        m = self._load(owner_id, session_id)
        pending = [f["rel_path"] for f in m["files"] if f["state"] != "complete"]
        if pending:
            raise UploadError("还有 %d 个文件未传完，例如 %s"
                              % (len(pending), pending[0]))

        sdir = self._session_dir(owner_id, session_id)
        dest_dir = os.path.abspath(dest_dir)
        if allowed_root:
            allowed_root = os.path.abspath(allowed_root)
            if not (dest_dir == allowed_root
                    or dest_dir.startswith(allowed_root + os.sep)):
                raise UploadError("目标目录必须位于 %s 之下" % allowed_root)

        prior_dest = m.get("assembly_destination") or m.get("assembled_to")
        if prior_dest and os.path.abspath(prior_dest) != dest_dir:
            raise UploadError("该上传会话已开始组装到另一个目录，拒绝改写目标")

        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        m.setdefault("assembled_files", {})
        m["assembly_destination"] = dest_dir
        self._save(owner_id, session_id, m)

        written = []
        for idx, entry in enumerate(m["files"]):
            rel = safe_rel_path(entry["rel_path"])
            target = os.path.abspath(os.path.join(dest_dir, *rel.split("/")))
            if not (target == dest_dir or target.startswith(dest_dir + os.sep)):
                raise UploadError("文件 %s 的目标路径越界" % entry["rel_path"])
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            key = str(idx)
            record = m["assembled_files"].get(key)

            if record:
                if (record.get("rel_path") != entry["rel_path"]
                        or record.get("size") != entry["size"]
                        or not os.path.isfile(target)
                        or os.path.getsize(target) != entry["size"]):
                    raise UploadError("已组装文件与会话记录不一致：%s" % entry["rel_path"])
                written.append({"rel_path": entry["rel_path"], "path": target,
                                "size": entry["size"]})
                continue

            src = os.path.join(sdir, "files", "%d.done" % idx)
            pending_idx = m.get("assembling_file")
            if pending_idx == idx and os.path.isfile(target):
                if os.path.getsize(target) != entry["size"]:
                    raise UploadError("组装中断且目标文件大小不符，保留现场供检查：%s"
                                      % entry["rel_path"])
                # A previous attempt may have stopped after placing the file.
                if os.path.isfile(src):
                    os.remove(src)
            else:
                if os.path.exists(target):
                    raise UploadError("目标文件已存在，拒绝覆盖：%s" % target)
                m["assembling_file"] = idx
                self._save(owner_id, session_id, m)

                if entry["size"] == 0:
                    with open(target, "xb"):
                        pass
                else:
                    if not os.path.isfile(src):
                        raise UploadError("文件 %s 的已完成分片丢失" % entry["rel_path"])
                    temp_fd, temp_path = tempfile.mkstemp(
                        prefix=".sjfx-assemble-", suffix=".tmp",
                        dir=os.path.dirname(target),
                    )
                    try:
                        with open(src, "rb") as source, os.fdopen(temp_fd, "wb") as output:
                            temp_fd = None
                            shutil.copyfileobj(source, output, 1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                        try:
                            os.link(temp_path, target)  # atomic, same filesystem, no overwrite
                        except FileExistsError:
                            raise UploadError("目标文件已存在，拒绝覆盖：%s" % target)
                        except OSError:
                            # Rare filesystems may disallow hard links. Use exclusive
                            # creation as a last resort; the complete copy is staged first.
                            try:
                                with open(temp_path, "rb") as source, open(target, "xb") as output:
                                    shutil.copyfileobj(source, output, 1024 * 1024)
                                    output.flush()
                                    os.fsync(output.fileno())
                            except FileExistsError:
                                raise UploadError("目标文件已存在，拒绝覆盖：%s" % target)
                        shutil.copystat(src, target)
                        os.remove(src)
                    finally:
                        if temp_fd is not None:
                            os.close(temp_fd)
                        if os.path.exists(temp_path):
                            os.remove(temp_path)

            m["assembled_files"][key] = {
                "rel_path": entry["rel_path"], "size": entry["size"]
            }
            m["assembling_file"] = None
            self._save(owner_id, session_id, m)
            written.append({"rel_path": entry["rel_path"], "path": target,
                            "size": entry["size"]})

        m["assembled_at"] = time.time()
        m["assembled_to"] = dest_dir
        m["assembling_file"] = None
        self._save(owner_id, session_id, m)
        return {"ok": True, "file_count": len(written), "dest_dir": dest_dir,
                "files": written}

    # ---------------- 清理 ----------------

    def abort(self, owner_id, session_id):
        """放弃一个上传会话，删掉它的分片目录。

        必须有这个入口：用户点了"取消"，或建错了会话，那些 `.part` 文件会一直占着
        磁盘直到 TTL 到期 —— 100G 的场景下这就是 100G 的白占。
        """
        sdir = self._session_dir(owner_id, session_id)
        if not os.path.isdir(sdir):
            raise UploadError("上传会话不存在或已过期")
        shutil.rmtree(sdir, ignore_errors=True)
        return True

    def purge_expired(self, now=None):
        """清掉超过 TTL 的会话目录（默认 7 天）。

        为什么必须有：100G 上传会产生大量分片文件，
        用户放弃上传后如果不清理，磁盘会被慢慢吃光。
        """
        now = now or time.time()
        removed = []
        if not os.path.isdir(self.root):
            return removed
        for owner in os.listdir(self.root):
            owner_dir = os.path.join(self.root, owner)
            if not os.path.isdir(owner_dir):
                continue
            for sid in os.listdir(owner_dir):
                sdir = os.path.join(owner_dir, sid)
                manifest = os.path.join(sdir, "session.json")
                try:
                    if not os.path.isfile(manifest):
                        continue
                    with open(manifest, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                    age = now - float(meta.get("updated_at") or 0)
                    if age > self.session_ttl_seconds:
                        shutil.rmtree(sdir, ignore_errors=True)
                        removed.append("%s/%s" % (owner, sid))
                except (OSError, ValueError):
                    continue
        return removed
