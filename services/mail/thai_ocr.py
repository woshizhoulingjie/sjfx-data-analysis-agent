"""Portable Thai/English Tesseract OCR confined to the mail parsing subprocess."""
from pathlib import Path
import os,subprocess,tempfile,time


def available():
    root=Path(__file__).parent/'_ocr'
    return (root/'usr/bin/tesseract').is_file() and (root/'usr/share/tesseract-ocr/5/tessdata/tha.traineddata').is_file()


def extract(path,max_chars,deadline):
    root=Path(__file__).parent/'_ocr';binary=root/'usr/bin/tesseract'
    env={**os.environ,'OMP_THREAD_LIMIT':'1','OMP_NUM_THREADS':'1','LD_LIBRARY_PATH':str(root/'usr/lib/x86_64-linux-gnu'),'TESSDATA_PREFIX':str(root/'usr/share/tesseract-ocr/5/tessdata')}
    def ocr(image):
        timeout=max(1,deadline-time.monotonic())
        process=subprocess.run([str(binary),str(image),'stdout','-l','tha+eng','--psm','3'],capture_output=True,timeout=timeout,env=env)
        if process.returncode:raise RuntimeError('邮件 OCR 进程退出 ({})'.format(process.returncode))
        return process.stdout.decode('utf-8',errors='replace')
    values=[]
    if path.suffix.lower()=='.pdf':
        import pypdfium2 as pdfium
        pdf=pdfium.PdfDocument(str(path))
        try:
            with tempfile.TemporaryDirectory(prefix='mail-ocr-pages-') as folder:
                for i in range(len(pdf)):
                    if time.monotonic()>=deadline-2:return '\n\n'.join(values),False
                    page=pdf[i];bitmap=page.render(scale=2.2)
                    image=Path(folder)/'page.png'
                    bitmap.to_pil().save(image)
                    bitmap.close();page.close()
                    try:text=ocr(image)
                    except subprocess.TimeoutExpired:return '\n\n'.join(values),False
                    values.append('[第 {} 页]\n{}'.format(i+1,text))
                    if sum(len(v) for v in values)>max_chars:return '\n\n'.join(values)[:max_chars],False
        finally:pdf.close()
    else:values=[ocr(path)]
    return '\n\n'.join(values)[:max_chars],True
