"""Exact source coverage when a character slice leaves a tiny same-unit tail."""
import pytest

from services.storage import Storage
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.person_corpus import next_chunks, resolve_selection


@pytest.fixture
def tail_corpus(tmp_path):
    repo = MailRepository(Storage(tmp_path/'tails.sqlite3'), text_segment_chars=20000)
    repo.migrate()
    package, _ = repo.create_package(owner_id='owner', display_name='Synthetic slice tails',
        source_kind='directory', source_path=str(tmp_path), source_revision='r1', config={})
    pid = package['package_id']
    serial = [0]

    def add(text):
        ordinal = serial[0]; serial[0] += 1
        # Explicit MIME bytes avoid adding an unrelated trailing newline to the
        # intended one-character source tail or genuine one-character reply.
        headers = ('From: alice@example.com\r\nTo: bob@example.com\r\n'
                   'Subject: Synthetic source %s\r\nMessage-ID: <tail-%s@example.com>\r\n'
                   'Date: Wed, 07 Oct 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n'
                   'Content-Type: text/plain; charset=utf-8\r\n'
                   'Content-Transfer-Encoding: 8bit\r\n\r\n')%(ordinal,ordinal)
        raw = headers.encode('ascii')+text.encode('utf-8')
        key = repo.upsert_ingested_message(package_id=pid,
            source={'locator_type':'directory','root_path':str(tmp_path),
                    'source_revision':'r1','container_sha256':'r1'},
            member=MailMember('%s.eml'%ordinal,ordinal,len(raw),{}),
            raw=raw, parsed=parse_eml(raw))['message_key']
        with repo._connection() as connection:
            rows = [dict(row) for row in connection.execute(
                'SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=? ORDER BY char_start',
                (pid,key))]
        return key, rows

    return repo, pid, add


def assert_contiguous(chunks, rows):
    original = ''.join(row['text'] for row in rows)
    by_segment = {row['segment_id']:row for row in rows}
    cursor = rows[0]['char_start']
    for chunk in chunks:
        row = by_segment[chunk['segment_id']]
        assert chunk['char_start'] == cursor
        assert chunk['char_end'] == cursor+len(chunk['text'])
        assert chunk['text'] == row['text'][cursor-row['char_start']:chunk['char_end']-row['char_start']]
        assert chunk['text_sha256'] == row['text_sha256']
        cursor = chunk['char_end']
    assert cursor == rows[-1]['char_end']
    assert ''.join(chunk['text'] for chunk in chunks) == original
    assert len({chunk['chunk_id'] for chunk in chunks}) == len(chunks)


@pytest.mark.parametrize('tail_length',[1,64])
def test_small_same_segment_tail_finishes_without_an_extra_slice(tail_corpus,tail_length):
    repo,pid,add = tail_corpus
    _,rows = add('A'*9000+'尾'*tail_length)
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    budget = sum(len(row['text']) for row in rows)-tail_length
    chunks,cursor = next_chunks(repo,pid,'owner',selected,[0,0],budget)
    assert_contiguous(chunks,rows)
    assert sum(len(chunk['text']) for chunk in chunks) <= budget+64
    assert next_chunks(repo,pid,'owner',selected,cursor,budget)[0] == []


def test_contiguous_tail_storage_segment_keeps_its_own_provenance(tail_corpus):
    repo,pid,add = tail_corpus; repo.text_segment_chars = 9000
    _,rows = add('A'*9000+'尾')
    assert len(rows) == 2 and len(rows[-1]['text']) == 1
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,cursor = next_chunks(repo,pid,'owner',selected,[0,0],9000)
    assert_contiguous(chunks,rows)
    assert chunks[-1]['segment_id'] == rows[-1]['segment_id']
    assert chunks[-1]['text'] == '尾'
    assert next_chunks(repo,pid,'owner',selected,cursor,9000)[0] == []


def test_large_tail_remains_bounded_and_resumed_offsets_have_no_gaps(tail_corpus):
    repo,pid,add = tail_corpus
    _,rows = add('A'*9000+'尾'*65)
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,cursor = next_chunks(repo,pid,'owner',selected,[0,0],9000)
    assert sum(len(chunk['text']) for chunk in chunks) == 9000
    later,end = next_chunks(repo,pid,'owner',selected,cursor,9000)
    assert_contiguous(chunks+later,rows)
    assert next_chunks(repo,pid,'owner',selected,end,9000)[0] == []


@pytest.mark.parametrize('budget',[1,511])
def test_tiny_budgets_preserve_the_original_hard_limit(tail_corpus,budget):
    repo,pid,add = tail_corpus
    _,rows = add('A'*budget+'尾')
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,cursor = next_chunks(repo,pid,'owner',selected,[0,0],budget)
    assert sum(len(chunk['text']) for chunk in chunks) == budget
    later,end = next_chunks(repo,pid,'owner',selected,cursor,budget)
    assert_contiguous(chunks+later,rows)
    assert next_chunks(repo,pid,'owner',selected,end,budget)[0] == []


def test_independent_short_reply_is_not_borrowed_or_merged(tail_corpus):
    repo,pid,add = tail_corpus
    first,rows = add('A'*9000); reply,reply_rows = add('好')
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,cursor = next_chunks(repo,pid,'owner',selected,[0,0],9000)
    assert_contiguous(chunks,rows)
    assert {chunk['message_key'] for chunk in chunks} == {first}
    later,_ = next_chunks(repo,pid,'owner',selected,cursor,9000)
    assert_contiguous(later,reply_rows)
    assert len(later) == 1 and later[0]['message_key'] == reply
    assert later[0]['text'] == '好'


@pytest.mark.parametrize('field,value',[
    ('mime_path','another-part'),('source_kind','attachment'),('page_number',2),
    ('sheet_name','Other sheet'),('cell_range','B2'),('char_start',9001)])
def test_tail_allowance_never_crosses_source_unit_or_offset_boundaries(tail_corpus,field,value):
    repo,pid,add = tail_corpus; repo.text_segment_chars = 9000
    key,rows = add('A'*9000+'尾')
    with repo._connection() as connection:
        if field == 'char_start':
            connection.execute('UPDATE mail_text_segments SET char_start=?,char_end=? WHERE segment_id=?',
                (value,value+1,rows[-1]['segment_id']))
        else:
            connection.execute('UPDATE mail_text_segments SET '+field+'=? WHERE segment_id=?',
                (value,rows[-1]['segment_id']))
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,cursor = next_chunks(repo,pid,'owner',selected,[0,0],9000)
    assert sum(len(chunk['text']) for chunk in chunks) == 9000
    assert {chunk['segment_id'] for chunk in chunks} == {rows[0]['segment_id']}
    later,_ = next_chunks(repo,pid,'owner',selected,cursor,9000)
    assert len(later) == 1 and later[0]['message_key'] == key
    assert later[0]['text'] == '尾'


def test_borrowed_tail_must_pass_the_original_source_hash_check(tail_corpus):
    repo,pid,add = tail_corpus; repo.text_segment_chars = 9000
    _,rows = add('A'*9000+'尾')
    with repo._connection() as connection:
        connection.execute('UPDATE mail_text_segments SET text=? WHERE segment_id=?',
            ('改',rows[-1]['segment_id']))
    selected = resolve_selection(repo,pid,'owner','alice@example.com')
    with pytest.raises(RuntimeError,match='哈希不一致'):
        next_chunks(repo,pid,'owner',selected,[0,0],9000)
