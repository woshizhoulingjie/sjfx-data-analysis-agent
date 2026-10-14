"""Verify real Docling PDF inference from Unicode Windows installation paths."""
from __future__ import annotations
import argparse,json,os,pathlib,sys,time
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--resources',required=True,type=pathlib.Path)
    ap.add_argument('--samples',required=True,type=pathlib.Path);ap.add_argument('--work',required=True,type=pathlib.Path)
    args=ap.parse_args();work=args.work.resolve();work.mkdir(parents=True,exist_ok=True)
    envfile=work/'acceptance.env';envfile.write_text('# Isolated Docling probe\n',encoding='utf8')
    settings={'SJFX_ENV_FILE':envfile,'SJFX_STATE_DIR':work/'state','SJFX_OUTPUT_DIR':work/'generated',
      'SJFX_PARSE_TEMP_DIR':work/'parse-temp','SJFX_TOOLS_DIR':args.resources.resolve()/'tools',
      'SJFX_MODELS_DIR':args.resources.resolve()/'models'}
    os.environ.update({k:str(v) for k,v in settings.items()});os.environ['DOCLING_DEVICE']='cpu'
    from config import Config
    from services.unified_parser import UnifiedDocumentParser
    parser=UnifiedDocumentParser(Config.DOCLING_ARTIFACTS_DIR,Config.RAPIDOCR_MODEL_DIR,max_chars=100000)
    from reportlab.pdfgen.canvas import Canvas
    table=work/'中文 表格 table.pdf';canvas=Canvas(str(table),pagesize=(700,800))
    canvas.setFont('Helvetica',18);canvas.drawString(60,725,'SJFXTEST2026 Project budget and delivery')
    canvas.setFont('Helvetica',15)
    for y in (630,575,520):canvas.line(60,y,640,y)
    for x in (60,340,640):canvas.line(x,520,x,630)
    for x,y,text in [(75,595,'Project'),(355,595,'Budget'),(75,540,'SJFXTEST2026'),(355,540,'125000')]:canvas.drawString(x,y,text)
    canvas.drawString(60,450,'Delivery date is 2026-12-20.');canvas.save()
    report={'unicode_python_installation':any(ord(c)>127 for c in sys.executable),
       'python_executable':sys.executable,'status_before':parser.status(),'cases':[]}
    for path in [args.samples/'scan.pdf',args.samples/'text.pdf',table]:
        start=time.monotonic();row={'case':path.name,'passed':False}
        try:
            value=parser.parse(path,mode='accurate');text=value.get('text','');p=value.get('parser',{})
            complete=value.get('parse_status')=='complete' and value.get('coverage',{}).get('complete') is True
            native=p.get('name')=='Docling' and p.get('degraded') is False
            table_ok=(value.get('structure',{}).get('table_count',0)>0) if path==table else True
            row.update(passed=native and complete and all(t in text for t in ['SJFXTEST2026','125000','2026-12-20']) and table_ok,
               text=text,parser=p,coverage=value.get('coverage'),structure=value.get('structure'),warnings=value.get('warnings'),
               real_docling_without_fallback=native)
        except Exception as exc:row.update(error_type=type(exc).__name__,error=str(exc))
        row['elapsed_seconds']=round(time.monotonic()-start,3);report['cases'].append(row)
        (work/'docling-pdf-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    report['status_after']=parser.status();report['passed']=all(r['passed'] for r in report['cases'])
    (work/'docling-pdf-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
