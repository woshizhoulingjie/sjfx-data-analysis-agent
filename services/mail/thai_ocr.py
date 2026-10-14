"""Portable Thai/English Tesseract OCR confined to the mail parsing subprocess."""
from pathlib import Path
import os,subprocess,time
from services.parse_temp import temporary_parse_directory


def _is_windows():
    return os.name == 'nt'


def _resource_roots():
    from config import Config
    tools = Path(Config.TOOLS_DIR)
    return (tools / 'tesseract', tools / 'mail-ocr', Path(__file__).parent / '_ocr')


def _resolve_resources():
    """Select one complete local tool bundle; never mix roots or download."""
    windows = _is_windows()
    for root in _resource_roots():
        binaries = ((root / 'tesseract.exe', root / 'bin' / 'tesseract.exe') if windows
                    else (root / 'usr' / 'bin' / 'tesseract', root / 'bin' / 'tesseract',
                          root / 'tesseract'))
        binary = next((path for path in binaries if path.is_file()), None)
        if binary is None:
            continue
        model_dirs = (root / 'tessdata', root / 'usr' / 'share' / 'tesseract-ocr' / '5' / 'tessdata',
                      root / 'usr' / 'share' / 'tessdata', root / 'share' / 'tessdata')
        tessdata = next((path for path in model_dirs
                        if all((path / name).is_file() for name in ('tha.traineddata', 'eng.traineddata'))), None)
        if tessdata is None:
            continue
        libraries = () if windows else tuple(path for path in (
            root / 'usr' / 'lib' / 'x86_64-linux-gnu', root / 'usr' / 'lib64',
            root / 'usr' / 'lib', root / 'lib64', root / 'lib',
        ) if path.is_dir())
        return binary, tessdata, libraries
    return None


def available():
    return _resolve_resources() is not None


def ocr_image(image, deadline):
    """Recognize Thai/English only; callers must select this language explicitly."""
    resources = _resolve_resources()
    if resources is None:
        raise RuntimeError('邮件泰文 OCR 缺少本地 Tesseract 或 tha/eng 语言模型')
    binary, tessdata, libraries = resources
    env = {**os.environ, 'OMP_THREAD_LIMIT': '1', 'OMP_NUM_THREADS': '1',
           'TESSDATA_PREFIX': str(tessdata)}
    if _is_windows():
        env.pop('LD_LIBRARY_PATH', None)
    elif libraries:
        env['LD_LIBRARY_PATH'] = ':'.join([str(path) for path in libraries] +
                                         ([env['LD_LIBRARY_PATH']] if env.get('LD_LIBRARY_PATH') else []))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(str(binary), 0)
    process=subprocess.run([str(binary),str(image),'stdout','-l','tha+eng','--psm','3'],capture_output=True,timeout=remaining,env=env)
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
            with temporary_parse_directory(prefix='mail-ocr-pages-') as folder:
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
