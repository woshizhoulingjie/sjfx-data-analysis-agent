"""Actual managed helper/executor start and stop without business/model work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_managed_helper_and_executor_cold_start_and_stop(tmp_path):
    helper=os.getenv('SJFX_TEST_WINDOWS_HELPER')
    if os.name=='nt' and not helper:pytest.skip('Select the actual compiled Windows helper explicitly')
    script=Path(__file__).resolve().parents[1]/'scripts/validate_desktop_lifecycle.py'
    evidence=tmp_path/'lifecycle.json'
    command=[sys.executable,str(script),'--temp-root',str(tmp_path),'--evidence',str(evidence)]
    if os.name=='nt':command+=['--helper-binary',helper]
    env={key:value for key,value in os.environ.items() if not key.startswith('SJFX_') and key not in {'SCAN_ALLOWED_ROOTS','CVD_CERTS_DIR'}}
    result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=80)
    assert result.returncode==0,result.stderr[-4000:]
    report=json.loads(evidence.read_text(encoding='utf-8'))
    assert report['helper_authenticated_ready'] and report['executor_ready']
    assert report['all_owned_children_stopped']
    assert report['global_audit_changes'] is False and report['access_audit_healthy'] is False
    assert report['model_files_created']==0
