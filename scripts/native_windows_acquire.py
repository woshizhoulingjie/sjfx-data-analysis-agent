"""Acquire public Windows parser tools into a private resource directory.

No application files or production documents are read or changed.
Cygwin packages are SHA512-verified against the mirror's setup.ini; all
downloads additionally record SHA256 for reproducible inventory.
"""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, io, json, pathlib, re, shutil, tarfile, urllib.request

MIRROR = 'https://mirrors.kernel.org/sourceware/cygwin/'

def fetch(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with urllib.request.urlopen(url, timeout=60) as response, target.open('wb') as stream:
            shutil.copyfileobj(response, stream)
    print('downloaded', target.name, target.stat().st_size, flush=True)
    return {'url': url, 'path': str(target), 'bytes': target.stat().st_size,
            'sha256': hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--root', type=pathlib.Path, required=True)
    ap.add_argument('--report', type=pathlib.Path, required=True); args = ap.parse_args()
    root=args.root.resolve(); downloads=root/'downloads'; downloads.mkdir(parents=True,exist_ok=True)
    manifest=[]
    urls = [
      ('https://github.com/ip7z/7zip/releases/download/26.04/7z2604-x64.exe','7z2604-x64.exe','7-Zip 26.04','LGPL-2.1-or-later + unRAR restriction'),
      ('https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe','tesseract-5.4.0.exe','Tesseract 5.4.0.20240606','Apache-2.0; bundled libraries retain individual licenses'),
      ('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/tha.traineddata','tha.traineddata','tessdata_fast main','Apache-2.0'),
      ('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata','eng.traineddata','tessdata_fast main','Apache-2.0'),
    ]
    def item(spec):
        url,name,version,license=spec
        try:return {**fetch(url,downloads/name),'version':version,'license':license}
        except Exception as exc:return {'url':url,'error':repr(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        manifest.extend(pool.map(item,urls))
    ini=root/'cygwin-setup.ini'
    manifest.append(fetch(MIRROR+'x86_64/setup.ini',ini))
    text=ini.read_text(encoding='utf-8'); packages={}
    for name,body in re.findall(r'^@ ([^\n]+)\n(.*?)(?=\n@ |\Z)',text,re.M|re.S):
        current=re.split(r'^\[',body,maxsplit=1,flags=re.M)[0]
        match=re.search(r'^install: (\S+) (\d+) (\w+)$',current,re.M)
        if match:
            deps=re.search(r'^depends2: (.+)$',current,re.M)
            packages[name]={'name':name,'relative_url':match[1],'bytes':int(match[2]),'sha512':match[3],
                 'version':re.search(r'^version: (.+)$',current,re.M)[1],
                 'deps':re.findall(r'(?:^|,)\s*([^ ,(]+)',deps[1]) if deps else []}
    selected=set()
    excluded={'_windows','bash','unzip','ca-certificates','crypto-policies','fontconfig','libgsf-common'}
    def collect(name):
        if name in selected or name in excluded or name not in packages:return
        selected.add(name)
        for dep in packages[name]['deps']:collect(dep)
    for name in ('antiword','catdoc','readpst'):collect(name)
    print('Cygwin DLL dependency package closure:',', '.join(sorted(selected)),flush=True)
    def package(name):
        p=packages[name]; target=downloads/pathlib.Path(p['relative_url']).name
        try:
            result=fetch(MIRROR+p['relative_url'],target)
            actual=hashlib.file_digest(target.open('rb'),'sha512').hexdigest()
            if actual != p['sha512']:raise ValueError('Cygwin SHA512 mismatch')
            return {**p,**result,'sha512_verified':True,'license':'See bundled usr/share/doc and package source; Cygwin DLL LGPL-3.0-or-later, utilities GPL-3.0-or-later'}
        except Exception as exc:return {**p,'error':repr(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        records=list(pool.map(package, sorted(selected)))
    manifest.extend(records)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    bundle=root/'tools'/'legacy-office'
    for record in records:
        if 'error' in record:continue
        path=pathlib.Path(record['path'])
        if path.suffix=='.zst':
            from backports import zstd
            data=zstd.ZstdFile(path,'rb')
            archive=tarfile.open(fileobj=data,mode='r|')
        else:archive=tarfile.open(path)
        with archive:
            for member in archive:
                # Only regular files are needed; do not materialize links.
                if not member.isfile():continue
                destination=(bundle/member.name).resolve()
                if not destination.is_relative_to(bundle.resolve()):raise ValueError('unsafe tar path')
                destination.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as stream, destination.open('wb') as out:shutil.copyfileobj(stream,out)
    print('Cygwin Windows parser bundle:',bundle,flush=True)

if __name__=='__main__':main()
