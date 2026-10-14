"""Inventory actual model/tool bytes, PE architecture, DLLs and licenses."""
from __future__ import annotations
import argparse, hashlib, importlib.metadata, json, pathlib, platform, shutil, sqlite3, struct, subprocess, sys

def fileinfo(path,root):
    with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    row={'path':path.relative_to(root).as_posix(),'bytes':path.stat().st_size,'sha256':sha}
    if path.suffix.lower() in {'.exe','.dll','.pyd'}:
        with path.open('rb') as stream:
            header=stream.read(64)
            if header[:2]==b'MZ':
                stream.seek(struct.unpack_from('<I',header,60)[0]);pe=stream.read(6)
                row['pe_architecture']={0x8664:'x86_64',0x14c:'x86',0xaa64:'aarch64'}.get(struct.unpack_from('<H',pe,4)[0],'unknown')
    return row

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--resources',type=pathlib.Path,required=True)
    ap.add_argument('--report',type=pathlib.Path,required=True);args=ap.parse_args()
    root=args.resources.resolve();report=args.report.resolve();report.parent.mkdir(parents=True,exist_ok=True)
    metadata=json.loads((report.parent/'upstream-model-metadata.json').read_text(encoding='utf8'))
    upstream={}
    for repo in ('docling-project/docling-layout-heron','docling-project/docling-models'):
        prefix='models/docling/'+repo.replace('/','--')+'/'
        for row in metadata[repo]['siblings']:
            if row.get('lfs',{}).get('sha256'):upstream[prefix+row['rfilename']]={'repo':repo,'revision':metadata[repo]['sha'],'sha256':row['lfs']['sha256']}
    files=[]
    for folder in ('models','tools'):
        for path in sorted((root/folder).rglob('*')):
            if path.is_file():
                row=fileinfo(path,root)
                if row['path'] in upstream:
                    row['upstream_lfs']=upstream[row['path']]
                    row['upstream_sha256_matches']=row['sha256']==upstream[row['path']]['sha256']
                files.append(row)
    licenses={
        'antiword':{'version':'0.37-1','license':'GPL-2.0-or-later','source':'https://cygwin.com/packages/summary/antiword.html','text':'tools/legacy-office/usr/share/doc/antiword/COPYING'},
        'catppt':{'version':'catdoc 0.95-2','license':'GPL-2.0-or-later','source':'https://cygwin.com/packages/summary/catdoc.html','text':'tools/legacy-office/usr/share/doc/catdoc/COPYING','limitation':'Cygwin catppt returned exit 0 with no body on two genuine public PPT files; not accepted as working.'},
        'readpst':{'version':'0.6.71-2','license':'GPL-2.0-or-later','source':'https://cygwin.com/packages/summary/readpst.html','text':'tools/legacy-office/usr/share/doc/libpst/COPYING','runtime':'Cygwin 3.6.11-1; binaries execute on Windows, no WSL'},
        'cygwin':{'version':'3.6.11-1','license':'Cygwin DLL LGPL-3.0-or-later; standalone utilities GPL-3.0-or-later','text':'tools/legacy-office/usr/share/doc/Cygwin/CYGWIN_LICENSE'},
        '7zip':{'version':'26.04','license':'LGPL-2.1-or-later + BSD portions + unRAR restriction','source':'https://github.com/ip7z/7zip/releases/tag/26.04','text':'tools/7zip/License.txt'},
        'tesseract':{'version':'5.4.0.20240606','license':'Apache-2.0, third-party DLL licenses retained by upstream distribution','source':'https://github.com/UB-Mannheim/tesseract/releases/tag/v5.4.0.20240606','text':'tools/tesseract/doc/LICENSE'},
        'tessdata':{'source':'https://github.com/tesseract-ocr/tessdata_fast','license':'Apache-2.0','languages':['tha','eng']},
        'rapidocr_models':{'source':'https://github.com/RapidAI/RapidOCR','original_models':'https://github.com/PaddlePaddle/PaddleOCR','license':'Apache-2.0','transport':'read-only copy of preexisting offline server ONNX files; source byte identity recorded locally'},
        'docling_layout':{'source':'https://huggingface.co/docling-project/docling-layout-heron','revision':metadata['docling-project/docling-layout-heron']['sha'],'license':metadata['docling-project/docling-layout-heron']['cardData']['license'],'transport':'read-only server model copy; SHA256 checked against upstream LFS'},
        'docling_tableformer':{'source':'https://huggingface.co/docling-project/docling-models','revision':metadata['docling-project/docling-models']['sha'],'licenses':metadata['docling-project/docling-models']['cardData']['license'],'transport':'read-only server model copy; SHA256 checked against upstream LFS'},
        'nllb_ct2':{'source':'https://huggingface.co/facebook/nllb-200-distilled-600M','license':'CC-BY-NC-4.0','format':'existing CTranslate2 int8 conversion; model.bin is a derived artifact, not an original HF weight','transport':'read-only server model copy; local SHA256 recorded'},
    }
    check=subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True,encoding='utf8',errors='replace')
    packages=sorted([{'name':d.metadata['Name'],'version':d.version} for d in importlib.metadata.distributions()],key=lambda r:r['name'].lower())
    node_path=shutil.which('node')
    node_runtime={'role':'frontend regression runtime; not required by the Python application','executable':node_path,'available':bool(node_path)}
    if node_path:
        node_check=subprocess.run([node_path,'--version'],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=10)
        node_runtime.update(version=node_check.stdout.strip(),exit_code=node_check.returncode,
                            sha256=hashlib.file_digest(pathlib.Path(node_path).open('rb'),'sha256').hexdigest())
    result={'platform':platform.platform(),'python':platform.python_version(),'python_executable':sys.executable,
            'sqlite_version':sqlite3.sqlite_version,'frontend_node':node_runtime,'resources_root':str(root),
            'package_check':{'passed':check.returncode==0,'output':check.stdout+check.stderr},'packages':packages,
            'licenses_and_sources':licenses,'files':files,'dlls':[r for r in files if r['path'].lower().endswith('.dll')],
            'verified_upstream_weights':[r for r in files if r.get('upstream_sha256_matches')]}
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    (report.parent/'requirements-windows-verified.freeze.txt').write_text('\n'.join(f"{p['name']}=={p['version']}" for p in packages)+'\n',encoding='utf8')
    print(json.dumps({'files':len(files),'dlls':len(result['dlls']),'upstream_lfs_matches':len(result['verified_upstream_weights']),'pip_check':check.returncode},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
