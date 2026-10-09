"""Portable Thai/English Tesseract OCR confined to the mail parsing subprocess."""
from pathlib import Path
import os,subprocess,tempfile,time


def available():
    root=Path(__file__).parent/'_ocr'
    return ((root/'usr/bin/tesseract').is_file()
            and all((root/'usr/share/tesseract-ocr/5/tessdata'/name).is_file()
                    for name in ('tha.traineddata', 'eng.traineddata')))


def ocr_image(image, deadline):
    """Recognize Thai/English only; callers must select this language explicitly."""
    root=Path(__file__).parent/'_ocr';binary=root/'usr/bin/tesseract'
    env={**os.environ,'OMP_THREAD_LIMIT':'1','OMP_NUM_THREADS':'1','LD_LIBRARY_PATH':str(root/'usr/lib/x86_64-linux-gnu'),'TESSDATA_PREFIX':str(root/'usr/share/tesseract-ocr/5/tessdata')}
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired(str(binary), 0)
    process=subprocess.run([str(binary),str(image),'stdout','-l','tha+eng','--psm','3'],capture_output=True,timeout=max(.1,deadline-time.monotonic()),env=env)
    if process.returncode:raise RuntimeError('邮件 OCR 进程退出 ({})'.format(process.returncode))
    return process.stdout.decode('utf-8',errors='replace')


def extract_units(path,max_chars,deadline):
    def ocr(image):
        return ocr_image(image, deadline)
    values=[]
    if path.suffix.lower()=='.pdf':
        import pypdfium2 as pdfium
        pdf=pdfium.PdfDocument(str(path))
        try:
            with tempfile.TemporaryDirectory(prefix='mail-ocr-pages-') as folder:
                for i in range(len(pdf)):
                    if time.monotonic()>=deadline-2:return values,False
                    page=pdf[i];bitmap=page.render(scale=2.2)
                    image=Path(folder)/'page.png'
                    bitmap.to_pil().save(image)
                    bitmap.close();page.close()
                    try:text=ocr(image)
                    except subprocess.TimeoutExpired:return values,False
                    remaining=max_chars-sum(len(v['text']) for v in values)
                    values.append({'text':text[:remaining], 'page_number':i+1})
                    if len(text)>remaining:return values,False
        finally:pdf.close()
    else:
        text=ocr(path)
        values=[{'text':text[:max_chars]}]
        return values,len(text)<=max_chars
    return values,True


def extract(path,max_chars,deadline):
    units,complete=extract_units(path,max_chars,deadline)
    return '\n\n'.join(u['text'] for u in units),complete
