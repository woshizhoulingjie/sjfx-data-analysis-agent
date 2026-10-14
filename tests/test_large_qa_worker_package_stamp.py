"""The sidecar must notice preview completing after its first empty sync."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from config import Config
from scripts.large_qa_worker import ReadOnlyStore
from services.large_package_isolated import LargePackageStore
from services.large_qa_demand import package_stamp, should_sync
from services.large_qa_index import LargeQAIndex


@patch.object(Config,'SCAN_ALLOWED_ROOTS',())
def test_package_list_refreshes_empty_index_after_inventory_completes():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        store=LargePackageStore(root/'large.db')
        package=store.create(root)
        reader=ReadOnlyStore(root/'large.db')
        before=reader.packages()[0]
        index=LargeQAIndex(package['id'],root/'qa',create=True)
        assert index.sync_store(reader)==0
        store.upsert_file(package['id'],{'path':'purchase.txt','name':'purchase.txt','size':42})
        store.update_file(package['id'],'purchase.txt',quick_status='completed',
                          local_summary={'sample':'Synthetic purchase total is 120 yuan.'})
        store.update_package(package['id'],phase='catalog_ready',status='waiting_for_selection',progress=100,
                             counts={'quick':{'completed':1}})
        after=reader.packages()[0]
        assert after['owner_id']==before['owner_id']
        assert package_stamp(after)!=package_stamp(before)
        assert should_sync(103,100,package_stamp(before),package_stamp(after),interval=2,force_after=600)
        assert index.sync_store(reader)==1
        assert index.next_parse()['path']=='purchase.txt'
