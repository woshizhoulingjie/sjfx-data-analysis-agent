from email.message import EmailMessage
import hashlib
import json
from pathlib import Path
import sqlite3
import io
import zipfile
from contextlib import contextmanager

import pytest

from sjfx_local_protection import risk
from sjfx_local_protection import antivirus
from sjfx_protection import inspection


def email_bytes(sender="Research Team <staff@example.test>", reply=None, body="本周资料已归档。", html=False, auth=None):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "reader@example.test"
    mail["Subject"] = "通知"
    if reply: mail["Reply-To"] = reply
    if auth: mail["Authentication-Results"] = auth
    mail.set_content(body, subtype="html" if html else "plain")
    return mail.as_bytes()


def scan(data, name="notice.eml", **policy):
    return risk.inspect_bytes(data, name, {"required_av": False, **policy})


def rules(result):
    return {item["rule"] for item in result["findings"]}


def test_ordinary_mail_and_forged_auth_header_remain_unverified():
    result = scan(email_bytes(auth="claimed.example; spf=pass; dkim=pass; dmarc=pass"))
    assert result["verdict"] == "no_detection" and result["complete"]
    assert result["mail"][0]["authentication"]["status"] == "unverified"
    assert result["mail"][0]["reputation"]["status"] == "not_checked"


def test_reply_difference_is_review_without_dangerous_combo():
    result = scan(email_bytes(reply="support@vendor.test"))
    assert result["verdict"] == "review" and result["complete"]
    assert "mail_reply_to_domain_mismatch" in rules(result)
    assert "mail_phishing_combination" not in rules(result)


@pytest.mark.parametrize("sender,body", [
    ("Microsoft Support <security@microsoft.com.evil.test>", "Urgent: verify your account at https://evil.test/login"),
    ("支付宝 <billing@unrelated.test>", "账户即将冻结，请立即登录验证账号 https://invalid.test/"),
    ('"ceo@company.test" <accounts@other.test>', "紧急，今日内转账至指定账户。请保密。"),
    ("Research <staff@example.test>", "Urgent: send your password and verification code at https://invalid.test/"),
])
def test_identity_and_inducement_combination_is_dangerous(sender, body):
    reply = "outside@other.test" if sender.startswith("Research") else None
    result = scan(email_bytes(sender, reply, body))
    assert result["verdict"] == "dangerous" and result["complete"]
    assert "mail_phishing_combination" in rules(result)


def test_allowed_brand_subdomain_is_not_spoofed():
    result = scan(email_bytes("Microsoft <staff@news.microsoft.com>"))
    assert "mail_brand_sender_mismatch" not in rules(result)


def test_idna_homograph_domain_and_inducement():
    homograph = "mіcrosoft.com".encode("idna").decode()  # Cyrillic i.
    result = scan(email_bytes(f"Microsoft <staff@{homograph}>", body="Verify your account immediately"))
    assert "mail_lookalike_sender_domain" in rules(result)
    assert result["verdict"] == "dangerous"


def test_lookalike_link_is_evidence_even_if_sender_is_allowed():
    result = scan(email_bytes('Microsoft <a@microsoft.com>', body='Immediately verify your account at https://micros0ft.com/login'))
    assert 'mail_lookalike_link_domain' in rules(result)
    assert result['verdict'] == 'dangerous'


def test_mail_header_and_link_domain_budgets_are_incomplete():
    assert not scan(b'From: '+b'x'*9000+b' <a@example.test>\r\nSubject: x\r\n\r\nnormal')['complete']
    body=' '.join('https://site%d.test/'%index for index in range(8))
    assert not scan(email_bytes(body=body),max_members=2)['complete']


def test_existing_link_form_and_attachment_rules_survive():
    result = scan(email_bytes(body='<a href="https://evil.test/">https://bank.test/login</a><form><input type="password"></form>', html=True))
    assert {"link_target_mismatch", "credential_form"} <= rules(result)
    assert result["verdict"] == "dangerous"
    message = EmailMessage()
    message["From"] = "staff@example.test"
    message.set_content("normal")
    message.add_attachment(b"MZsynthetic", maintype="application", subtype="octet-stream", filename="invoice.pdf")
    result = scan(message.as_bytes())
    assert "disguised_executable" in rules(result) and result["members"]


def test_invalid_duplicate_sender_and_bad_encoding_fail_closed():
    data = b"From: a@example.test\r\nFrom: b@other.test\r\nSubject: x\r\n\r\nnormal"
    assert not scan(data)["complete"]
    data = b"From: a@example.test\r\nContent-Type: text/plain; charset=unknown-codec\r\n\r\nnormal"
    assert not scan(data)["complete"]


def test_body_rule_budget_configuration_and_finding_budget_are_incomplete():
    assert "mail_combination_budget" in rules(scan(email_bytes(body="x" * 200), max_html_bytes=100))
    assert not scan(email_bytes(), mail_brand_domains={"Brand": "not-a-list"})["complete"]
    result = scan(email_bytes("Microsoft <a@evil.test>", "a@third.test", "Urgent verify your account https://evil.test"), max_findings=2)
    assert result["verdict"] == "dangerous" and not result["complete"]


def test_msg_cannot_claim_eml_or_complete_support():
    result = scan(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic", "notice.msg")
    assert result["verdict"] == "incomplete" and not result["complete"]
    assert result["mail"][0]["parser"] == "extract_msg"


def test_office_root_relationship_and_zero_padded_thumbnail():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', '<document/>')
        archive.writestr('_rels/.rels', '<Relationships><Relationship TargetMode="External" Target="https://invalid.test"/></Relationships>')
        archive.writestr('docProps/thumbnail.jpeg', b'\xff\xd8\xffsynthetic\xff\xd9\0')
    result = scan(output.getvalue(), 'ordinary.docx')
    assert result['complete']
    assert 'office_external_relationship' in rules(result)
    assert 'unsupported_format' not in rules(result)
    assert scan(b'\xff\xd8\xffsynthetic\xff\xd9\0', 'thumb.jpeg')['complete']
    assert not scan(b'\xff\xd8\xffsynthetic\xff\xd9<script>', 'thumb.jpeg')['complete']


def test_large_container_outer_av_pass_cannot_override_static_limit(tmp_path, monkeypatch):
    from sjfx_protection.scanner import inspect_file
    path = tmp_path / 'container.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('large.txt', 'x' * 200)
    monkeypatch.setattr(antivirus, 'scan', lambda **kwargs: {'status': 'completed', 'real': True, 'known_signatures': 100, 'scanned_files': 1, 'output_complete': True, 'revision': 'ClamAV 1.4.6/28151/date'})
    result = inspect_file(path, max_bytes=10, virus_max_bytes=1000)
    assert result['risk'] == 'incomplete' and not result['coverage']['complete']


def test_large_text_stream_checks_html_formula_names_and_av_budget(tmp_path, monkeypatch):
    from sjfx_protection.scanner import inspect_file
    path = tmp_path / 'large.txt'
    monkeypatch.setattr(antivirus, 'scan', lambda **kwargs: {'status': 'completed', 'real': True, 'known_signatures': 100, 'scanned_files': 1, 'output_complete': True, 'revision': 'ClamAV 1.4.6/28151/date'})
    path.write_bytes(b'normal research\n' * 100)
    assert inspect_file(path, max_bytes=10, virus_max_bytes=10000)['risk'] == 'no_detection'
    assert inspect_file(path, max_bytes=10, virus_max_bytes=10000, name='disguise.ps1')['risk'] == 'dangerous'
    path.write_bytes(b'normal\n' * 100 + b'<form><input type="password"></form>')
    assert inspect_file(path, max_bytes=10, virus_max_bytes=10000)['risk'] == 'incomplete'
    path = tmp_path / 'large.csv'
    path.write_bytes(b'one,two\n' * 100 + b'"=SYNTHETIC",one')
    assert inspect_file(path, max_bytes=10, virus_max_bytes=10000)['risk'] == 'review'
    assert inspect_file(path, max_bytes=10, virus_max_bytes=10)['risk'] == 'incomplete'


def test_rule_filename_engine_database_and_config_changes_invalidate_cache(monkeypatch):
    monkeypatch.setattr(antivirus, "runtime_identity", lambda: {"database_identity": {"fingerprint": "one"}})
    baseline = inspection.profile_key({}, "a.eml")
    assert inspection.profile_key({}, "b.eml") != baseline
    original_revision = risk.REVISION
    monkeypatch.setattr(risk, "REVISION", "new-rules")
    assert inspection.profile_key({}, "a.eml") != baseline
    monkeypatch.setattr(risk, "REVISION", original_revision)
    monkeypatch.setattr(antivirus, "runtime_identity", lambda: {"database_identity": {"fingerprint": "two"}})
    assert inspection.profile_key({}, "a.eml") != baseline
    assert inspection.profile_key({"mail_brand_domains": {}}, "a.eml") != baseline


class EvidenceStore:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "evidence.sqlite"
        with self.db(True) as database:
            database.execute("CREATE TABLE inspections(id TEXT,version_id TEXT,cache_key TEXT,name TEXT,risk TEXT,coverage TEXT,engines TEXT,findings TEXT,created REAL,expires REAL)")

    @contextmanager
    def db(self, write=False):
        database = sqlite3.connect(self.path)
        database.row_factory = sqlite3.Row
        try:
            yield database
            if write: database.commit()
        finally: database.close()


def test_real_persistence_uses_modern_rules_and_strict_av_receipt(tmp_path, monkeypatch):
    data = email_bytes("Microsoft <a@evil.test>", body="Urgent verify your account https://evil.test/")
    path = tmp_path / "hash.eml"
    path.write_bytes(data)
    version = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(antivirus, "runtime_identity", lambda: {"database_identity": {"fingerprint": "one"}})
    monkeypatch.setattr(antivirus, "scan", lambda **kwargs: {"status": "completed", "real": True,
        "known_signatures": 100, "scanned_files": 1, "output_complete": True,
        "input_sha256": version, "revision": "ClamAV 1.4.6/28151/date", "database_identity": {"fingerprint": "one"}})
    profile = {"static_max_bytes": 1024 * 1024, "virus_max_bytes": 1024 * 1024,
        "large_timeout_seconds": 30, "required_engines": ["static", "clamav"], "inspection_ttl_seconds": 60}
    store = EvidenceStore(tmp_path)
    result = inspection.evidence(store, path, "original.eml", version, profile)
    assert result["risk"] == "dangerous"
    assert "mail_phishing_combination" in {item["rule"] for item in json.loads(result["findings"])}
    assert json.loads(result["coverage"])["mail"][0]["authentication"]["status"] == "unverified"
    assert json.loads(result["engines"])["clamav_receipt"]["known_signatures"] == 100
    assert inspection.evidence(store, path, 'original.eml', version, profile)['id'] == result['id']


def test_database_changed_between_cache_lookup_and_scan_cannot_complete(tmp_path, monkeypatch):
    path = tmp_path / 'hash.txt'; path.write_bytes(b'normal')
    version = hashlib.sha256(b'normal').hexdigest()
    identity = {'database_identity': {'fingerprint': 'one'}}
    monkeypatch.setattr(antivirus, 'runtime_identity', lambda: identity)
    def scan(**kwargs):
        identity['database_identity']['fingerprint'] = 'two'
        return {'status': 'completed', 'real': True, 'known_signatures': 100, 'scanned_files': 1,
                'output_complete': True, 'input_sha256': version, 'revision': 'ClamAV 1.4.6/28152/date'}
    monkeypatch.setattr(antivirus, 'scan', scan)
    profile = {'static_max_bytes': 1000, 'virus_max_bytes': 1000, 'large_timeout_seconds': 1,
               'required_engines': ['static', 'clamav'], 'inspection_ttl_seconds': 60}
    result = inspection.evidence(EvidenceStore(tmp_path), path, 'original.txt', version, profile)
    assert result['risk'] == 'incomplete'
    assert 'inspection_configuration_changed' in json.loads(result['coverage'])['incomplete_reasons']


def test_uncertified_antivirus_approval_cannot_be_persisted_as_complete(tmp_path, monkeypatch):
    path = tmp_path / "hash.txt"
    path.write_bytes(b"normal")
    version = hashlib.sha256(b"normal").hexdigest()
    monkeypatch.setattr(antivirus, "runtime_identity", lambda: {})
    monkeypatch.setattr(antivirus, "scan", lambda **kwargs: {"status": "completed", "real": True,
        "known_signatures": 0, "scanned_files": 1, "output_complete": True})
    profile = {"static_max_bytes": 1000, "virus_max_bytes": 1000, "large_timeout_seconds": 1,
               "required_engines": ["static", "clamav"], "inspection_ttl_seconds": 60}
    result = inspection.evidence(EvidenceStore(tmp_path), path, "original.txt", version, profile)
    assert result["risk"] == "incomplete" and not json.loads(result["coverage"])["complete"]


def test_configured_organization_domains_reach_actual_saved_results(tmp_path, monkeypatch):
    data=email_bytes('My Organization <a@unrelated.test>', body='Urgent verify your account https://invalid.test/')
    path=tmp_path/'hash.eml';path.write_bytes(data)
    version=hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(antivirus,'runtime_identity',lambda: {})
    monkeypatch.setattr(antivirus,'scan',lambda **kwargs: {'status':'unavailable','real':False})
    profile={'static_max_bytes':10000,'virus_max_bytes':10000,'large_timeout_seconds':1,
             'required_engines':['static'],'inspection_ttl_seconds':60,
             'mail_brand_domains':{'My Organization':['organization.test']}}
    result=inspection.evidence(EvidenceStore(tmp_path),path,'original.eml',version,profile)
    assert result['risk']=='dangerous'
    assert 'mail_brand_sender_mismatch' in {f['rule'] for f in json.loads(result['findings'])}
