"""User-triggered durable work has fair admission and observable transitions."""
from pathlib import Path
from contextlib import closing
import threading
import time
import pytest
from config import Config
from services.model_dispatch import background_generation,generation_slot,_connect


def test_user_work_follows_fifo_and_reports_actual_admission(tmp_path,monkeypatch):
    monkeypatch.setattr(Config,'DATA_DIR',tmp_path)
    monkeypatch.setenv('SJFX_GENERATION_CONCURRENCY','1')
    transitions=[];order=[];errors=[]
    def user_work():
        try:
            with background_generation(priority=100,on_status=lambda s:transitions.append(s)):
                with generation_slot('fixture://user-fifo',timeout=4):order.append('person')
        except Exception as exc:errors.append(exc)
    def later_foreground():
        try:
            with generation_slot('fixture://user-fifo',timeout=4):order.append('later-foreground')
        except Exception as exc:errors.append(exc)
    with generation_slot('fixture://user-fifo'):
        first=threading.Thread(target=user_work);first.start()
        deadline=time.monotonic()+2
        while 'waiting_for_model' not in transitions and time.monotonic()<deadline:time.sleep(.01)
        assert transitions==['waiting_for_model']
        second=threading.Thread(target=later_foreground);second.start()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with closing(_connect()) as c:count=c.execute('SELECT COUNT(*) FROM admission').fetchone()[0]
            if count==3:break
            time.sleep(.01)
        assert count==3
    first.join(4);second.join(4)
    assert not errors
    assert order==['person','later-foreground']
    assert transitions==['waiting_for_model','running']
    with closing(_connect()) as c:assert c.execute('SELECT COUNT(*) FROM admission').fetchone()[0]==0


def test_observer_failure_releases_ticket_and_context(tmp_path,monkeypatch):
    monkeypatch.setattr(Config,'DATA_DIR',tmp_path)
    def observer(state):raise RuntimeError('checkpoint changed')
    with pytest.raises(RuntimeError,match='checkpoint changed'):
        with background_generation(priority=100,on_status=observer):
            with generation_slot('fixture://observer'):pytest.fail('must not generate')
    with closing(_connect()) as c:assert c.execute('SELECT COUNT(*) FROM admission').fetchone()[0]==0
    with generation_slot('fixture://observer'):pass


def test_priority_validation_does_not_poison_default_context():
    with pytest.raises(ValueError):
        with background_generation(priority=True):pass
