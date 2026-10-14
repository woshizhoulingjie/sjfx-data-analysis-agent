"""Real Windows legacy readers, archive extraction, mail conversion, and OCR.

Requires public Apache Tika/rarfile fixtures and externally acquired tools.
Never counts an installed package, mock response or empty body as a pass.
"""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, platform, sys, time

ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--resources',required=True,type=pathlib.Path)
    ap.add_argument('--work',required=True,type=pathlib.Path);args=ap.parse_args()
    work=args.work.resolve();resources=args.resources.resolve();work.mkdir(parents=True,exist_ok=True)
    envfile=work/'acceptance.env';envfile.write_text('# Isolated native Windows acceptance\n',encoding='utf8')
    for key,path in {'SJFX_ENV_FILE':envfile,'SJFX_STATE_DIR':work/'state','SJFX_OUTPUT_DIR':work/'generated',
                     'SJFX_PARSE_TEMP_DIR':work/'parse-temp','SJFX_TOOLS_DIR':resources/'tools',
                     'SJFX_MODELS_DIR':resources/'models'}.items():os.environ[key]=str(path)
    os.environ['PYTHONUTF8']='1';os.environ['SCAN_ALLOWED_ROOTS']=str(work)
    os.environ['OMP_NUM_THREADS']='2';os.environ['DOCLING_DEVICE']='cpu'
    from config import Config
    report={'os':platform.platform(),'python':platform.python_version(),'real_execution':True,'cases':[]}
    def run(name,action):
        start=time.monotonic();row={'case':name,'passed':False}
        try:row.update(action())
        except Exception as exc:row.update(error_type=type(exc).__name__,error=str(exc))
        row['elapsed_seconds']=round(time.monotonic()-start,3);report['cases'].append(row)
        (work/'native-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    fixtures=work/'公开 样本'
    from services.legacy_office import extract_legacy, _native
    for name,token in [('testWORD.doc','Sample Word Document'),('testPPT.ppt','Sample Powerpoint Slide')]:
        def legacy(name=name,token=token):
            value=extract_legacy(fixtures/name,max_chars=100000,seconds=30)
            return {'passed':token.casefold() in value['text'].casefold() and value.get('metadata',{}).get('content_complete') is True,**value}
        run('app:legacy:'+name,legacy)
    def catppt():
        text,metadata,truncated=_native(fixtures/'testPPT.ppt','.ppt',100000,15)
        return {'passed':'Sample Powerpoint Slide' in text and not truncated,'text':text,'metadata':metadata}
    run('native:catppt',catppt)
    from services.archive_formats import ArchiveReader
    def rar():
        rows=[]
        with ArchiveReader(fixtures/'rar5-crc.rar') as archive:
            for entry in archive.entries():
                if not entry.regular or entry.directory:continue
                with archive.open_member(entry) as stream:data=stream.read()
                rows.append({'name':entry.name,'expected_bytes':entry.size,'actual_bytes':len(data),
                             'sha256':hashlib.sha256(data).hexdigest(),'snippet':data[:140].decode('utf8','replace'),
                             'compressed':entry.compressed_size<entry.size,'verified':len(data)==entry.size and bool(data)})
        return {'passed':bool(rows) and all(r['verified'] for r in rows) and any(r['compressed'] for r in rows),'members':rows}
    run('app:rar_compressed',rar)
    def sevenzip():
        import py7zr
        path=work/'合成 中文.7z';body='SJFXTEST2026 真实七压缩验收 125000'.encode('utf8')
        with py7zr.SevenZipFile(path,'w') as z:z.writestr(body,'中文 成员.txt')
        with ArchiveReader(path) as archive:
            entries=list(archive.entries())
            with archive.open_member(entries[0]) as stream:data=stream.read()
        return {'passed':data==body,'member':entries[0].name,'text':data.decode('utf8'),'sha256':hashlib.sha256(data).hexdigest()}
    run('app:7z_synthetic',sevenzip)
    from services.mail.source import AdapterRegistry
    from services.mail.models import SourceRef
    from services.mail.policy import MailPolicy
    from email.parser import BytesParser
    from email import policy as email_policy
    for name in ('test-outlook.msg','testPST_variousBodyTypes.pst'):
        def mail(name=name):
            path=fixtures/name;revision=hashlib.file_digest(path.open('rb'),'sha256').hexdigest()
            source=SourceRef('native-acceptance',path.suffix[1:],str(path),revision)
            adapter=AdapterRegistry().resolve(source);members=list(adapter.inventory(source,MailPolicy.from_config(Config)))
            rows=[]
            for member in members:
                with adapter.open_message(source,member) as stream:raw=stream.read()
                message=BytesParser(policy=email_policy.default).parsebytes(raw)
                parts=[p for p in message.walk() if p.get_content_maintype()=='text']
                body='\n'.join(str(p.get_content()) for p in parts)
                rows.append({'name':member.name,'subject':str(message.get('Subject','')),'body':body[:2500],
                             'bytes':len(raw),'body_chars':len(body),'sha256':hashlib.sha256(raw).hexdigest()})
            return {'passed':bool(rows) and all(r['bytes']>50 and r['body_chars']>10 for r in rows),'messages':rows}
        run('app:mail:'+name,mail)
    def thai_ocr():
        from PIL import Image,ImageDraw,ImageFont
        from services.mail.thai_ocr import ocr_image
        path=work/'泰文 英文 scan.png';image=Image.new('RGB',(2300,650),'white');draw=ImageDraw.Draw(image)
        font=ImageFont.truetype('C:/Windows/Fonts/tahoma.ttf',64)
        draw.text((60,70),'Project budget is 125000 yuan.',font=font,fill='black')
        draw.text((60,210),'Delivery date is 2026-12-20.',font=font,fill='black')
        draw.text((60,350),'งบประมาณโครงการ 125000 บาท',font=font,fill='black')
        image.save(path);text=ocr_image(path,time.monotonic()+40)
        expected=['Project budget is 125000 yuan.','Delivery date is 2026-12-20.','งบประมาณโครงการ 125000 บาท']
        return {'passed':all(line in text for line in expected),'expected_lines':expected,
                'text':text,'image':str(path),'languages':'tha+eng'}
    run('app:tesseract_thai_and_english',thai_ocr)
    return 0 if all(row['passed'] for row in report['cases']) else 1

if __name__=='__main__':raise SystemExit(main())
