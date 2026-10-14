"""A Windows junction is a directory indirection even when it is not a symlink."""
import os
import subprocess

import pytest

from sjfx_protection.store import Store
from sjfx_protection.worker import inventory, verify_inventory


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='native Windows junction')


def junction(path, target):
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(path), str(target)],
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert path.is_junction() and not path.is_symlink()


@pytest.mark.parametrize('phase', ['inventory', 'verify'])
def test_empty_junction_invalidates_complete_inventory(tmp_path, phase):
    root = tmp_path / '资料'
    root.mkdir()
    external = tmp_path / '外部'
    external.mkdir()
    store = Store(tmp_path / 'state')
    cid = store.create_collection('alice', str(root))
    if phase == 'verify':
        inventory(store, cid, [root])
        assert store.collection('alice', cid)['state'] == 'ready'
    link = root / '转接目录'
    junction(link, external)
    try:
        if phase == 'verify':
            verify_inventory(store, cid)
        else:
            inventory(store, cid, [root])
        assert store.collection('alice', cid)['state'] == 'failed'
        with store.db() as db:
            assert db.execute('SELECT value FROM settings WHERE key=?',
                              ('verified:' + cid,)).fetchone() is None
    finally:
        # Remove only the junction itself, preserving the external directory.
        link.rmdir()
        assert external.is_dir()


def test_junction_selected_as_inventory_root_is_rejected(tmp_path):
    actual = tmp_path / '资料'
    actual.mkdir()
    link = tmp_path / '转接根'
    junction(link, actual)
    try:
        store = Store(tmp_path / 'state')
        cid = store.create_collection('alice', str(link))
        inventory(store, cid, [tmp_path])
        assert store.collection('alice', cid)['state'] == 'failed'
    finally:
        link.rmdir()
        assert actual.is_dir()
