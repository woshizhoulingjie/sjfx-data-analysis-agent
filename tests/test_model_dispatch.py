import multiprocessing as mp
import tempfile
import time
import unittest
from pathlib import Path
from config import Config
from services.model_dispatch import generation_slot,background_generation,_connect,_drop_conn
from contextlib import closing,contextmanager
from unittest.mock import patch


@contextmanager
def close_dispatch_connections():
    try:
        yield
    finally:
        # Windows cannot remove the temporary DB while its cached handle is open.
        _drop_conn()


def take_slot(root,background,events,ready,hold):
    Config.DATA_DIR=Path(root)
    if background:
        with background_generation():
            ready.set()
            with generation_slot('fixture://shared',timeout=8):
                events.put(('background',time.monotonic()));time.sleep(hold)
    else:
        ready.set()
        with generation_slot('fixture://shared',timeout=8):
            events.put(('foreground',time.monotonic()));time.sleep(hold)


class DispatchTests(unittest.TestCase):
    def test_cross_process_frontend_precedes_waiting_background(self):
        ctx=mp.get_context('spawn')
        with tempfile.TemporaryDirectory() as folder,patch.object(Config,'DATA_DIR',Path(folder)),close_dispatch_connections():
            events=ctx.Queue();br=ctx.Event();fr=ctx.Event()
            with generation_slot('fixture://shared'):
                background=ctx.Process(target=take_slot,args=(folder,True,events,br,.05));background.start();self.assertTrue(br.wait(4))
                foreground=ctx.Process(target=take_slot,args=(folder,False,events,fr,.05));foreground.start();self.assertTrue(fr.wait(4))
                end=time.monotonic()+4
                while time.monotonic()<end:
                    with closing(_connect()) as c:n=c.execute('SELECT COUNT(*) FROM admission').fetchone()[0]
                    if n==3:break
                    time.sleep(.02)
                self.assertEqual(n,3)
            first=events.get(timeout=5);second=events.get(timeout=5)
            foreground.join(5);background.join(5)
            self.assertEqual(first[0],'foreground');self.assertEqual(second[0],'background')
            self.assertEqual(foreground.exitcode,0);self.assertEqual(background.exitcode,0)
    def test_cancelled_background_does_not_call_model_or_leave_ticket(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(Config,'DATA_DIR',Path(folder)),close_dispatch_connections():
            from services.ollama import LocalModelError
            with background_generation(lambda:True):
                with self.assertRaises(LocalModelError):
                    with generation_slot('fixture://cancel'):self.fail('cancelled work admitted')
            with closing(_connect()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM admission').fetchone()[0],0)

if __name__=='__main__':unittest.main()
