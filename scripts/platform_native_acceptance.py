"""Real public legacy/archive/mail fixtures on an explicitly isolated instance.

The same fixture hashes and expected body content are checked on both platforms.
The direct catppt probe is reported separately from the application's PPT parser.
"""
from __future__ import annotations

import argparse
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--env-file', required=True, type=Path)
    ap.add_argument('--work', required=True, type=Path)
    ap.add_argument('--fixtures', required=True, type=Path)
    ap.add_argument('--ocr-image', required=True, type=Path)
    args = ap.parse_args()
    if not args.env_file.is_file():
        raise SystemExit('Explicit isolated environment file does not exist')
    os.environ['SJFX_ENV_FILE'] = str(args.env_file.resolve())
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    from config import Config
    report = {'os': platform.platform(), 'python': sys.version, 'real_execution': True,
              'fixture_directory': str(args.fixtures.resolve()), 'cases': []}

    def run(name, action, required=True):
        start = time.monotonic()
        row = {'case': name, 'passed': False, 'required_application_case': required}
        try:
            row.update(action())
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error=str(exc))
        row['elapsed_seconds'] = round(time.monotonic() - start, 3)
        report['cases'].append(row)
        report['all_required_passed'] = all(r['passed'] for r in report['cases'] if r['required_application_case'])
        (work / 'native-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)

    from services.legacy_office import extract_legacy, _native
    for name, token in [('testWORD.doc', 'Sample Word Document'), ('testPPT.ppt', 'Sample Powerpoint Slide')]:
        def legacy(name=name, token=token):
            value = extract_legacy(args.fixtures / name, max_chars=100000, seconds=30)
            return {**value, 'passed': token.casefold() in value['text'].casefold()
                    and value.get('metadata', {}).get('content_complete') is True}
        run('app:legacy:' + name, legacy)

    def catppt():
        text, metadata, truncated = _native(args.fixtures / 'testPPT.ppt', '.ppt', 100000, 15)
        return {'passed': 'Sample Powerpoint Slide' in text and not truncated,
                'text': text, 'metadata': metadata,
                'scope': 'direct optional fallback tool; application PPT is checked separately'}
    run('native:catppt', catppt, required=False)

    from services.archive_formats import ArchiveReader
    def rar():
        rows = []
        expected_hash = '2eaebb4c18cdef7f20089f8a2fa3475bc59c2a193f66e2f1513609a4bef13e22'
        with ArchiveReader(args.fixtures / 'rar5-crc.rar') as archive:
            for entry in archive.entries():
                if not entry.regular or entry.directory:
                    continue
                with archive.open_member(entry) as stream:
                    body = stream.read()
                digest = hashlib.sha256(body).hexdigest()
                rows.append({'name': entry.name, 'bytes': len(body), 'sha256': digest,
                             'compressed': entry.compressed_size < entry.size,
                             'verified': len(body) == 2048 and digest == expected_hash})
        return {'passed': len(rows) == 2 and all(r['verified'] for r in rows)
                and any(r['compressed'] for r in rows), 'members': rows}
    run('app:rar_compressed_exact_bytes', rar)

    def sevenzip():
        import py7zr
        path = work / '合成 中文.7z'
        body = 'SJFXTEST2026 真实七压缩验收 125000'.encode('utf-8')
        with py7zr.SevenZipFile(path, 'w') as archive:
            archive.writestr(body, '中文 成员.txt')
        with ArchiveReader(path) as archive:
            entries = list(archive.entries())
            with archive.open_member(entries[0]) as stream:
                extracted = stream.read()
        return {'passed': extracted == body and entries[0].name == '中文 成员.txt',
                'text': extracted.decode('utf-8'), 'sha256': hashlib.sha256(extracted).hexdigest()}
    run('app:7z_chinese_exact_bytes', sevenzip)

    from services.mail.source import AdapterRegistry
    from services.mail.models import SourceRef
    from services.mail.policy import MailPolicy
    for name in ('test-outlook.msg', 'testPST_variousBodyTypes.pst'):
        def mail(name=name):
            path = args.fixtures / name
            revision = hashlib.sha256(path.read_bytes()).hexdigest()
            source = SourceRef('native-acceptance', path.suffix[1:], str(path), revision)
            adapter = AdapterRegistry().resolve(source)
            members = list(adapter.inventory(source, MailPolicy.from_config(Config)))
            rows = []
            for member in members:
                with adapter.open_message(source, member) as stream:
                    raw = stream.read()
                message = BytesParser(policy=email_policy.default).parsebytes(raw)
                body = '\n'.join(str(part.get_content()) for part in message.walk()
                                 if part.get_content_maintype() == 'text')
                token = 'original email' if name.endswith('.pst') else 'Outlook Express'
                rows.append({'name': member.name, 'subject': str(message.get('Subject', '')),
                             'body': body[:2500], 'body_chars': len(body), 'bytes': len(raw),
                             'expected_content_found': token in body,
                             'sha256': hashlib.sha256(raw).hexdigest()})
            expected_count = 4 if name.endswith('.pst') else 1
            return {'passed': len(rows) == expected_count and all(r['expected_content_found'] for r in rows),
                    'messages': rows}
        run('app:mail:' + name, mail)

    def thai_ocr():
        from services.mail.thai_ocr import ocr_image
        text = ocr_image(args.ocr_image, time.monotonic() + 40)
        expected = ['Project budget is 125000 yuan.', 'Delivery date is 2026-12-20.',
                    'งบประมาณโครงการ 125000 บาท']
        return {'passed': all(line in text for line in expected), 'text': text,
                'expected_lines': expected, 'languages': 'tha+eng',
                'image_sha256': hashlib.sha256(args.ocr_image.read_bytes()).hexdigest()}
    run('app:tesseract_thai_and_english', thai_ocr)
    return 0 if report['all_required_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
