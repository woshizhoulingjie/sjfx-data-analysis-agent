import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from sjfx_local_protection import antivirus as av


def summary(*, known=100, scanned=1, infected=0, target="target.txt: OK", extra=""):
    return (target + "\n" + extra + "\n----------- SCAN SUMMARY -----------\n"
        f"Known viruses: {known}\nEngine version: 1.4.6\nScanned files: {scanned}\nInfected files: {infected}\n"
        "Data scanned: 0.00 MB\nData read: 0.00 MB\nTime: 1 sec\nEnd Date: 2026:10:14 00:00:00\n")


@pytest.mark.parametrize("output", ["", "target.txt: OK\n", summary(known=0), summary(scanned=0),
    summary(scanned=2), summary(extra="ERROR decoding member"), summary(extra="WARNING size limit exceeded"),
    summary().replace("End Date:", "Missing:"), summary() + summary(), summary(infected=1)])
def test_exit_zero_cannot_replace_complete_receipt(output):
    assert av.parse_receipt(0, output)["status"] != "completed"


def test_exact_full_clean_receipt():
    result = av.parse_receipt(0, summary())
    assert result["status"] == "completed"
    assert result["known_signatures"] == 100 and result["scanned_files"] == 1


def test_detection_survives_incomplete_container():
    result = av.parse_receipt(1, summary(infected=1, target="target.txt: Synthetic.Malware FOUND",
        extra="target.txt: Heuristics.Limits.Exceeded FOUND"))
    assert result["status"] == "incomplete" and result["detected"] is True


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv('SJFX_PROTECTION_CLAM_CERTS', raising=False)
    monkeypatch.delenv('CVD_CERTS_DIR', raising=False)
    database = tmp_path / "database"
    database.mkdir()
    (database / "daily.cvd").write_bytes(b"ClamAV-VDB:date:28151:100:90:hash:builder:0\n")
    return {"clamav_path": str(tmp_path / "synthetic-scanner"), "clamav_database": str(database),
            "av_work_dir": str(tmp_path / "restricted-work"), "av_timeout": 1, "av_max_bytes": 1000}


def fake_process(output=None, exit_code=0):
    def run(args, **kwargs):
        assert kwargs.get("shell", False) is False
        assert kwargs["env"]["TEMP"] == "." and kwargs["env"]["TMP"] == "."
        if "--version" in args:
            kwargs["stdout"].write(b"ClamAV 1.4.6/28151/date\n")
        else:
            assert args[-1] == "target.txt"
            assert "--no-summary" not in args
            assert (Path(kwargs["cwd"]) / "target.txt").read_bytes() == b"normal"
            kwargs["stdout"].write((output or summary()).encode())
        return subprocess.CompletedProcess(args, exit_code)
    return run


def test_staged_immutable_version_private_cwd_and_full_receipt(config, monkeypatch):
    monkeypatch.setattr(av.subprocess, "run", fake_process())
    result = av.scan(data=b"normal", name="--untrusted.txt", policy=config)
    assert result["status"] == "completed"
    assert result["input_sha256"] == hashlib.sha256(b"normal").hexdigest()
    assert result["original_name"] == "--untrusted.txt" and result["output_complete"]
    assert not list(Path(config["av_work_dir"]).iterdir())
    if os.name != "nt":
        assert stat.S_IMODE(Path(config["av_work_dir"]).stat().st_mode) == 0o700


@pytest.mark.parametrize("failure", ["timeout", "missing", "truncated"])
def test_engine_failures_close_admission(config, monkeypatch, failure):
    if failure == "truncated":
        monkeypatch.setattr(av.subprocess, "run", fake_process(summary() + "x" * 128000))
    else:
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired("scanner", 1) if failure == "timeout" else FileNotFoundError("scanner")
        monkeypatch.setattr(av.subprocess, "run", run)
    assert av.scan(data=b"normal", name="a.txt", policy=config)["status"] != "completed"


def test_empty_and_input_budget_are_incomplete(config, monkeypatch):
    monkeypatch.setattr(av.subprocess, "run", lambda *a, **k: pytest.fail("must not execute"))
    assert av.scan(data=b"", policy=config)["status"] == "incomplete"
    assert av.scan(data=b"x" * 1001, policy=config)["status"] == "incomplete"


def test_database_version_and_configuration_are_observed(config):
    before = av.database_identity(config["clamav_database"])
    target = Path(config["clamav_database"]) / "daily.cvd"
    target.write_bytes(target.read_bytes().replace(b"28151", b"28152"))
    after = av.database_identity(config["clamav_database"])
    assert before["fingerprint"] != after["fingerprint"]
    assert after["files"][0]["version"] == "28152"


def test_portable_certificates_flag_environment_and_content_identity(config, monkeypatch, tmp_path):
    certificates = tmp_path / 'portable-certificates'
    certificates.mkdir()
    (certificates/'signer.pem').write_bytes(b'Synthetic test certificate, never passed to a real engine')
    config['clamav_certs'] = str(certificates)
    observed = []
    original = fake_process()
    def process(args, **kwargs):
        flag = next(value for value in args if value.startswith('--cvdcertsdir='))
        assert kwargs['env']['CVD_CERTS_DIR'] == flag.split('=', 1)[1]
        assert not any(value.startswith('--certs-dir') or 'no-verify' in value for value in args)
        observed.append(flag)
        return original(args, **kwargs)
    monkeypatch.setattr(av.subprocess, 'run', process)
    result = av.scan(data=b'normal', name='a.txt', policy=config)
    assert result['status'] == 'completed' and len(observed) == 2
    before = result['certificate_identity']['fingerprint']
    (certificates/'signer.pem').write_bytes(b'Different synthetic certificate')
    assert av.certificates_identity(certificates)['fingerprint'] != before
    Path(config['clamav_path']).write_bytes(b'synthetic executable metadata')
    assert av.runtime_identity(config)['certificate_identity']['fingerprint'] != before


def test_official_certificate_environment_is_observed(config, monkeypatch, tmp_path):
    certificates = tmp_path / 'official-env-certs'; certificates.mkdir()
    monkeypatch.setenv('CVD_CERTS_DIR', str(certificates))
    assert av.configuration(config)['certificates'] == str(certificates.resolve())
    assert av.certificates_identity(certificates)['status'] == 'configured_empty'


def test_unconfigured_windows_legacy_engine_gets_no_certificate_flag(config, monkeypatch):
    original = fake_process()
    def process(args, **kwargs):
        assert not any(value.startswith('--cvdcertsdir=') for value in args)
        return original(args, **kwargs)
    monkeypatch.setattr(av.subprocess, 'run', process)
    result = av.scan(data=b'normal', name='a.txt', policy=config)
    assert result['status'] == 'completed'
    assert result['certificate_identity']['status'] == 'engine_default'


def test_missing_configured_certificate_directory_never_runs_engine(config, monkeypatch):
    config['clamav_certs'] = config['av_work_dir'] + '/missing-certs'
    monkeypatch.setattr(av.subprocess, 'run', lambda *a, **k: pytest.fail('must not execute'))
    assert av.scan(data=b'normal', policy=config)['status'] == 'error'


def test_certificate_content_and_official_environment_change_cache_key(config, monkeypatch, tmp_path):
    from sjfx_protection.inspection import profile_key
    Path(config['clamav_path']).write_bytes(b'synthetic executable metadata')
    certificates = tmp_path/'cache-certs'; certificates.mkdir()
    (certificates/'signer.pem').write_bytes(b'old synthetic certificate')
    monkeypatch.setenv('SJFX_PROTECTION_CLAM', config['clamav_path'])
    monkeypatch.setenv('SJFX_PROTECTION_CLAM_DATABASE', config['clamav_database'])
    monkeypatch.setenv('CVD_CERTS_DIR', str(certificates))
    before = profile_key({}, 'a.txt')
    (certificates/'signer.pem').write_bytes(b'new synthetic certificate')
    assert profile_key({}, 'a.txt') != before
    monkeypatch.delenv('CVD_CERTS_DIR')
    assert profile_key({}, 'a.txt') != before


def test_missing_database_never_accepts_synthetic_exit_zero(config, monkeypatch):
    config["clamav_database"] += "/missing"
    monkeypatch.setattr(av.subprocess, "run", lambda *a, **k: pytest.fail("must not execute"))
    assert av.scan(data=b"normal", policy=config)["status"] == "error"


def test_public_windows_workspace_is_rejected(tmp_path, monkeypatch):
    if os.name != "nt": pytest.skip("Windows Public folder")
    monkeypatch.setenv("PUBLIC", str(tmp_path / "public"))
    with pytest.raises(ValueError, match="Public"):
        av._private_root(tmp_path / "public" / "scan")


def test_workspace_link_is_rejected(tmp_path):
    target = tmp_path / "owned"
    target.mkdir()
    alias = tmp_path / "alias"
    try: alias.symlink_to(target, target_is_directory=True)
    except OSError: pytest.skip("Account cannot create a symlink")
    with pytest.raises(ValueError, match="links"):
        av._private_root(alias)


def test_real_existing_word_versions():
    # Explicit opt-in; reuses existing ordinary user acceptance samples.
    directory = os.getenv("SJFX_TEST_REAL_WORD_DIR")
    if not directory: pytest.skip("Real Word engine validation is explicitly enabled")
    files = sorted(Path(directory).glob("*.docx"))
    assert len(files) >= 3
    receipts = []
    for path in files[:3]:
        from sjfx_protection.scanner import inspect_file
        inspected = inspect_file(path, name=path.name)
        assert inspected['risk'] == 'no_detection', inspected
        assert inspected['coverage']['complete'], inspected
        result = inspected['engines']['clamav_receipt']
        assert result["status"] == "completed", result
        assert result["real"] and result["known_signatures"] > 0 and result["scanned_files"] == 1
        assert result["output_complete"] and result["input_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        receipts.append({'inspection': inspected, 'antivirus': result})
    destination = os.getenv("SJFX_TEST_REAL_AV_OUTPUT")
    if destination: Path(destination).write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
