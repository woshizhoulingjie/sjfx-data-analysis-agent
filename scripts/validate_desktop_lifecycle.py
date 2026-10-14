"""Isolated helper/executor ownership validation; no Web, audit writes or models."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sjfxctl


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',required=True);parser.add_argument('--temp-root',required=True);parser.add_argument('--helper-binary')
    cli=parser.parse_args();base=Path(tempfile.mkdtemp(prefix='desktop-lifecycle-',dir=cli.temp_root))
    listeners=[socket.socket(),socket.socket()]
    for listener in listeners: listener.bind(('127.0.0.1',0))
    port,web_port=(listener.getsockname()[1] for listener in listeners)
    for listener in listeners: listener.close()
    args=argparse.Namespace(env_file=str(base/'instance.env'),data_dir=str(base/'state'),output_dir=str(base/'outputs'),models_dir=None,tools_dir=None,parse_temp_dir=cli.temp_root,backend='ollama',port=web_port,helper_port=port,helper_binary=cli.helper_binary,clam_database=None)
    with contextlib.redirect_stdout(io.StringIO()): sjfxctl.setup(args)
    config=sjfxctl._load_config(args)
    from config import ensure_runtime_directories
    from sjfx_protection.desktop_runtime import prepare_environment,helper_command,helper_health
    from services.process_liveness import terminate_process_tree
    ensure_runtime_directories();paths=prepare_environment(config);runtime=sjfxctl._runtime(config);runtime.mkdir(exist_ok=True)
    # This component test explicitly does not request native audit changes.
    os.environ['SJFX_DESKTOP_ACCESS_AUDIT']='0'
    run_id='isolated-desktop-component';children=[];evidence={'state':str(base),'platform':sys.platform,'helper_port':port,'global_audit_changes':False}
    try:
        helper=sjfxctl._spawn_role('protection-helper',config,runtime,run_id,helper_command(paths));children.append(helper)
        deadline=time.monotonic()+30
        while True:
            try: health=helper_health(paths);break
            except OSError:
                if helper[0].poll() is not None: raise RuntimeError('Helper exited during isolated startup')
                if time.monotonic()>deadline: raise RuntimeError('Helper readiness timed out')
                time.sleep(.1)
        executor=sjfxctl._spawn_role('protection-executor',config,runtime,run_id,[sys.executable,'-X','utf8','-m','sjfx_protection.desktop_runtime','executor','--state',paths['core_state']]);children.append(executor)
        ready=executor[3].with_suffix('.ready')
        while not ready.exists():
            if executor[0].poll() is not None: raise RuntimeError('Executor exited; inspect launcher-protection-executor.log')
            if time.monotonic()>deadline: raise RuntimeError('Executor readiness timed out')
            time.sleep(.1)
        evidence.update(helper_authenticated_ready=health['ok'],executor_ready=ready.read_text()==run_id,
            access_audit_healthy=health['access_audit_healthy'],model_files_created=len(list((base/'models').rglob('*'))),
            installation_id=paths['installation_id'])
    finally:
        Path(paths['stop_file']).write_text(run_id)
        for process,owner,log,gate in reversed(children):
            if process is helper[0]:
                try: process.wait(timeout=4)
                except Exception: pass
            terminate_process_tree(process,job=owner,grace_seconds=3,private_group=os.name!='nt')
            log.close();gate.unlink(missing_ok=True);gate.with_suffix('.ready').unlink(missing_ok=True)
        evidence['all_owned_children_stopped']=all(process.poll() is not None for process,_,_,_ in children)
        Path(cli.evidence).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))


if __name__=='__main__': main()
