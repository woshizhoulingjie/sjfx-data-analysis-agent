"""Synthetic mail locator identity and format compatibility checks."""
import hashlib
import mailbox
import os
import zipfile
from email.message import EmailMessage

import pytest

from services.mail.models import SourceRef
from services.mail.policy import MailPolicy, MailPolicyError
from services.mail.source import EmlAdapter, ZipAdapter, MboxAdapter, open_locator
from services.mail.foundation import source_inventory


@pytest.mark.parametrize('kind', ['eml', 'zip', 'mbox'])
def test_inventory_locator_preserves_message_and_rejects_replacement(tmp_path, kind):
    policy = MailPolicy((tmp_path,), 100, 1000000, 1000000, 10000000, 200, 32, 1000, 3)
    message = EmailMessage()
    message['Subject'] = 'Synthetic locator regression'
    message.set_content('Synthetic mail body only.')
    raw = message.as_bytes()
    path = tmp_path / ('source.' + kind)
    if kind == 'eml':
        path.write_bytes(raw)
        adapter = EmlAdapter()
    elif kind == 'zip':
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('inside.eml', raw)
        adapter = ZipAdapter()
    else:
        box = mailbox.mbox(path)
        box.add(message)
        box.close()
        adapter = MboxAdapter()
    revision = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    source = SourceRef('synthetic', kind, str(path), revision)
    entries = list(source_inventory(source, policy, adapter))
    assert len(entries) == 1
    member, status, error = entries[0]
    assert status == 'pending' and error is None
    with open_locator(member.locator, policy=policy) as stream:
        assert b'Synthetic mail body only.' in stream.read()
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    with pytest.raises(MailPolicyError):
        with open_locator(member.locator, policy=policy) as stream:
            stream.read()


def test_file_snapshot_survives_path_replacement_without_reading_new_file(tmp_path):
    from services.mail.source import file_identity, open_authorized_file
    policy = MailPolicy((tmp_path,), 100, 1000000, 1000000, 10000000, 200, 32, 1000, 3)
    path = tmp_path / 'source.eml'
    path.write_bytes(b'original')
    locator = {'kind': 'file', 'path': str(path), 'identity': file_identity(path)}
    with open_authorized_file(locator, policy) as stream:
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(b'new-secret')
        if os.name=='nt':
            # An ordinary Windows read handle prevents path replacement.
            with pytest.raises(PermissionError):replacement.replace(path)
        else:
            replacement.replace(path)
        assert stream.read() == b'original'
    if os.name=='nt':
        replacement.replace(path)
        with pytest.raises(MailPolicyError):
            with open_authorized_file(locator, policy) as stream:stream.read()
