"""Synthetic executable/HTTP contracts; not native antivirus quality acceptance."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from sjfx_protection.scanner import inspect_file


def synthetic_clam(tmp_path, code, output):
    command = tmp_path / ('synthetic scanner.cmd' if os.name == 'nt' else 'synthetic scanner.sh')
    if os.name == 'nt':
        command.write_text('@echo off\n' + ('echo ' + output + '\n' if output else '') +
                           'exit /b ' + str(code) + '\n', encoding='ascii')
    else:
        command.write_text('#!/bin/sh\nprintf "%s\\n" "' + output + '"\nexit ' + str(code) + '\n')
        command.chmod(0o700)
    return str(command)


@pytest.mark.parametrize(('code', 'output'), [(1, ''), (1, 'unrelated output'), (0, '')])
def test_clam_returncode_without_scan_receipt_is_unchecked(tmp_path, code, output):
    sample = tmp_path / 'sample.txt'
    sample.write_text('ordinary text')
    result = inspect_file(sample, clam=synthetic_clam(tmp_path, code, output))
    assert result['engines']['clamav'] == 'error'
    assert result['risk'] == 'unchecked'


def test_configured_yara_without_rules_is_unchecked(tmp_path):
    sample = tmp_path / 'sample.txt'
    sample.write_text('ordinary text')
    result = inspect_file(sample, clam=synthetic_clam(tmp_path, 0, 'sample.txt: OK'), yara='configured-yara')
    assert result['engines']['yara_x'] == 'error'
    assert result['risk'] == 'unchecked'


@pytest.mark.parametrize(('code', 'output', 'risk'),
                         [(0, 'sample.txt: OK', 'no_detection'),
                          (1, 'sample.txt: Test.Signature FOUND', 'dangerous')])
def test_clam_valid_receipts_preserve_verdicts(tmp_path, code, output, risk):
    sample = tmp_path / 'sample.txt'
    sample.write_text('ordinary text')
    result = inspect_file(sample, clam=synthetic_clam(tmp_path, code, output))
    assert result['engines']['clamav'] == 'completed'
    assert result['risk'] == risk


@pytest.mark.parametrize(('response', 'engine', 'risk'),
                         [({}, 'error', 'unchecked'),
                          ({'symbols': {}}, 'error', 'unchecked'),
                          ({'score': 0, 'symbols': []}, 'error', 'unchecked'),
                          ({'score': 0, 'symbols': {}, 'is_skipped': True}, 'error', 'unchecked'),
                          ({'score': 0, 'symbols': {}}, 'completed', 'no_detection'),
                          ({'score': 2, 'symbols': {'TEST': {'score': 2}}}, 'completed', 'review')])
def test_rspamd_scan_receipt_controls_verdict(tmp_path, response, engine, risk):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sample = tmp_path / 'sample.eml'
    sample.write_text('From: a@example.invalid\nTo: b@example.invalid\n\nordinary text')
    try:
        result = inspect_file(sample, clam=synthetic_clam(tmp_path, 0, 'sample.eml: OK'),
                              rspamd='http://127.0.0.1:' + str(server.server_port))
        assert result['engines']['rspamd'] == engine
        assert result['risk'] == risk
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
