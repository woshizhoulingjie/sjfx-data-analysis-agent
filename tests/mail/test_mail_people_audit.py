"""Acceptance: per-role drilldown stays aligned through a conversation scope."""
from email.message import EmailMessage

from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.people import contacts, correspondence, bilateral, relationships
from services.mail.conversations import rebuild
from services.mail.graph_metrics import confirm_entity
from config import Config


def test_conversation_scope_keeps_role_lists_pair_roles_and_unique_range(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'MAIL_ENABLE_GRAPH_METRICS', True)
    monkeypatch.setattr(Config, 'MAIL_ENABLE_CONVERSATIONS', True)
    repo = MailRepository(Storage(tmp_path / 'audit.sqlite3'))
    repo.migrate()
    package, _ = repo.create_package(owner_id='owner', display_name='Audit', source_kind='directory',
        source_path=str(tmp_path), source_revision='audit', config={})
    pid = package['package_id']

    def add(number, sender, to, cc=None, bcc=None, parent=None, raw=None):
        if raw is None:
            msg = EmailMessage()
            msg['From'], msg['To'], msg['Message-ID'] = sender, to, '<audit-%s@example.com>' % number
            msg['Date'] = 'Tue, 07 May 2024 %02d:00:00 +0700' % (number + 10)
            msg['Subject'] = 'changed subject ' + str(number)
            if cc: msg['Cc'] = cc
            if bcc: msg['Bcc'] = bcc
            if parent: msg['In-Reply-To'] = '<audit-%s@example.com>' % parent
            msg.set_content('完整正文 / Complete body / เนื้อหาครบถ้วน')
            raw = msg.as_bytes()
        result = repo.upsert_ingested_message(package_id=pid,
            source={'locator_type':'directory','root_path':str(tmp_path),'container_sha256':'audit','source_revision':'audit'},
            member=MailMember('%s.eml' % number, number, len(raw), {'kind':'file','path':'unused'}),
            raw=raw, parsed=parse_eml(raw))
        return result['message_key'], raw

    first, raw = add(1, 'a@example.com', 'outside@example.com', cc='b@example.com')
    add(7, '', '', raw=raw)  # duplicate source never adds a logical message
    reply, _ = add(2, 'b@example.com', 'alias@example.com', bcc='a@example.com', parent=1)
    add(3, 'a@example.com', 'b@example.com')  # unrelated despite overlapping people
    eid = confirm_entity(repo, pid, 'owner', 'Alice', ['a@example.com', 'alias@example.com'])
    rebuild(repo, pid, 'owner')
    with repo._connection() as c:
        cid = c.execute('SELECT conversation_id FROM mail_conversation_members WHERE message_key=?', (first,)).fetchone()[0]
    filters = {'conversation_id': cid}
    result = correspondence(repo, pid, 'owner', entity_id=eid, **filters)
    assert result['total'] == result['role_counts']['total'] == result['time_range']['message_count'] == 2
    assert result['role_counts'] == {'total':2,'sent':1,'direct':1,'cc':0,'bcc':1,'received':1}
    assert {item['message_key'] for item in result['items']} == {first, reply}
    assert all(item['conversations'] == [cid] for item in result['items'])
    for direction in ('sent', 'direct', 'cc', 'bcc', 'received'):
        page = correspondence(repo, pid, 'owner', entity_id=eid, direction=direction, limit=1, **filters)
        assert page['total'] == result['role_counts'][direction]
        assert page['time_range']['message_count'] == page['total']
    assert next(item for item in result['items'] if item['message_key'] == first)['participants'] == [
        {'role':'from','address':'a@example.com'}, {'role':'to','address':'outside@example.com'},
        {'role':'cc','address':'b@example.com'}]
    person = next(item for item in contacts(repo, pid, 'owner', view='person', **filters)['items'] if item['entity_id'] == eid)
    assert person['total'] == 2
    pair = bilateral(repo, pid, 'owner', entity_id=eid, counterpart='b@example.com', **filters)
    assert pair['total'] == pair['time_range']['message_count'] == 2
    assert next(item for item in pair['items'] if item['message_key'] == first)['pair_roles'] == ['sent_cc']
    assert next(item for item in pair['items'] if item['message_key'] == reply)['pair_roles'] == ['received','received_bcc']
    relationship = next(item for item in relationships(repo, pid, 'owner', entity_id=eid, **filters)['items'] if item['counterpart'] == 'b@example.com')
    assert relationship['total'] == pair['total']
    assert correspondence(repo, pid, 'owner', entity_id=eid, conversation_id='missing')['total'] == 0

