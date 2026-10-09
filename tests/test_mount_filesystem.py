"""Filesystem selection must not access unrelated hard network mounts."""
from pathlib import Path
from unittest.mock import patch

from config import _mount_filesystem


def _detect(candidate, resolved, mounts):
    accessed = []

    def resolve(path, *args, **kwargs):
        accessed.append(path)
        assert path == Path(candidate), "Unrelated mount was accessed"
        return Path(resolved)

    with patch.object(Path, "is_file", return_value=True), patch.object(
        Path, "read_text", return_value=mounts
    ), patch.object(Path, "resolve", resolve):
        result = _mount_filesystem(candidate)
    assert accessed == [Path(candidate)]
    return result


def test_unavailable_unrelated_mount_is_never_resolved():
    mounts = """1 0 8:1 / / rw - ext4 /dev/sda1 rw
2 1 0:20 / /home/unavailable rw - nfs4 server:/offline rw
3 1 0:21 / /dev/shm rw - tmpfs tmpfs rw
"""
    assert _detect("/dev/shm/app/data", "/dev/shm/app/data", mounts) == "tmpfs"


def test_longest_mount_prefix_uses_resolved_candidate():
    mounts = """1 0 8:1 / / rw - ext4 /dev/sda1 rw
2 1 0:20 / /mnt/nas rw - nfs4 server:/data rw
3 2 8:2 / /mnt/nas/team/local rw - ext4 /dev/sda2 rw
"""
    assert _detect("/symlink/data", "/mnt/nas/team/local/data", mounts) == "ext4"
    assert _detect("/symlink/data", "/mnt/nas/team/database", mounts) == "nfs4"


def test_escaped_mount_path_preserves_filesystem_selection():
    mounts = "1 0 0:20 / /mnt/team\\040files rw - nfs4 server:/files rw\n"
    assert _detect("/alias", "/mnt/team files/data", mounts) == "nfs4"
