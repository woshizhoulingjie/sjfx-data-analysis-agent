"""Browser authorization. Device-signed agent transport remains a separate boundary."""
import time

from .store import ProtectionError


SELF_SERVICE = {
    '/protection': {'GET'},
    '/protection/setup': {'GET'},
    '/protection/download': {'GET'},
    '/protection/api/status': {'GET'},
    '/protection/api/session': {'GET'},
    '/protection/api/setup': {'GET'},
    '/protection/api/enrollments': {'POST'},
    '/protection/api/revoke-self': {'POST'},
}
ADMIN = {
    '/protection/admin': {'GET'},
    **{'/protection/api/' + name: methods for name, methods in {
        'policy': {'POST'}, 'collections': {'GET', 'POST'},
        'cancel-collection': {'POST'}, 'files': {'GET'}, 'devices': {'GET'},
        'revoke': {'POST'}, 'bindings': {'GET'}, 'events': {'GET'},
        'trace': {'GET'}, 'actions': {'GET', 'POST'}, 'preview': {'GET'},
        'review': {'POST'}, 'verify-ledger': {'GET'},
    }.items()},
}


def authorize(user, path, method):
    if path not in SELF_SERVICE and user.get('role') != 'admin':
        raise ProtectionError('仅管理员可查看监视信息或执行安全处置', 403, 'admin_required')
    methods = SELF_SERVICE.get(path, ADMIN.get(path))
    if methods is None:
        raise ProtectionError('接口不存在', 404)
    if method not in methods:
        raise ProtectionError('请求方法不允许', 405, 'method_not_allowed')


def management_owner(user):
    # viewing_owner is validated and stored by the existing admin view-as flow.
    # Never accept owner or role from browser query parameters/body/headers.
    if user.get('role') == 'admin':
        return user.get('viewing_owner') or user['owner_id']
    return user['owner_id']


def setup(store, user):
    with store.db() as db:
        devices = db.execute(
            'SELECT id,name,heartbeat FROM devices WHERE owner=? AND user_id=? AND revoked IS NULL ORDER BY created DESC',
            (user['owner_id'], user['user_id'])).fetchall()
        collections = db.execute(
            "SELECT id,state FROM collections WHERE owner=? AND state IN ('queued','running','ready') ORDER BY created DESC",
            (user['owner_id'],)).fetchall()
    return {
        'devices': [{'id': d['id'], 'name': d['name'],
                     'connected': d['heartbeat'] > time.time() - 120} for d in devices],
        # No file paths, health payloads, risks, logs or command results here.
        'tasks': [{'id': c['id'], 'label': '待关联资料 ' + str(i + 1),
                   'ready': c['state'] == 'ready'} for i, c in enumerate(collections)],
    }


def enroll(store, user, data):
    if user.get('role') != 'admin' and data.get('consent') is not True:
        raise ProtectionError('请先阅读并同意文件保护授权说明', 400, 'consent_required')
    if data.get('consent') is True:
        store.event(user['owner_id'], 'authorization.accepted', payload={
            'user_id': user['user_id'], 'notice_version': '2026-09-26',
            'scope': 'selected_folders_and_system_operations',
        })
    return {'token': store.enrollment(user['owner_id'], user['user_id']), 'expires_in': 600}


def revoke_self(store, user, data):
    device_id = str(data.get('device_id', ''))
    with store.db(True) as db:
        row = db.execute('SELECT id FROM devices WHERE id=? AND owner=? AND user_id=?',
                         (device_id, user['owner_id'], user['user_id'])).fetchone()
        if not row:
            raise ProtectionError('未找到当前账号的设备', 404)
        db.execute('UPDATE devices SET revoked=? WHERE id=?', (time.time(), device_id))
        # Invalidate unused pairing codes issued by this same user as well.
        db.execute('UPDATE enrollments SET expires=0 WHERE owner=? AND user_id=? AND used IS NULL',
                   (user['owner_id'], user['user_id']))
        store._event(db, user['owner_id'], 'device.revoked', device=device_id,
                     payload={'user_id': user['user_id'], 'self_service': True})
    return {}
