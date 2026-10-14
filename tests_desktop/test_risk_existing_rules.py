"""Existing passive rule regressions, carried forward against the modern module.
Legacy exit-code-only AV assertions are replaced by test_clamav_receipts.
"""
import base64
import bz2
import codecs
from email.message import EmailMessage
import gzip
import importlib.util
import io
import lzma
from pathlib import Path
import struct
import tarfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

from sjfx_local_protection import risk

def scan(data, name, **policy):
    return risk.inspect_bytes(data, name, {"required_av": False, **policy})


def rules(result):
    return {item["rule"] for item in result["findings"]}


def zipped(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return output.getvalue()


def png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b"")


class PassiveRiskTests(unittest.TestCase):
    def test_plain_text_success_records_explicit_external_av_boundary(self):
        result = scan("正常资料\n项目状态".encode(), "notes.txt")
        self.assertEqual(result["verdict"], "no_detection")
        self.assertTrue(result["complete"])
        self.assertEqual(result["engines"]["av"]["status"], "not_requested")
        self.assertFalse(result["engines"]["av"]["real"])

    def test_default_never_calls_static_only_complete_when_av_missing(self):
        with patch.object(risk.shutil, "which", return_value=None), patch.dict(risk.os.environ, {}, clear=True):
            result = risk.inspect_bytes(b"normal", "a.txt")
        self.assertEqual(result["verdict"], "incomplete")
        self.assertFalse(result["complete"])
        self.assertEqual(result["engines"]["static"]["status"], "completed")
        self.assertIn("required_antivirus_incomplete", rules(result))

    def test_test_adapter_cannot_falsely_claim_real_av(self):
        result = scan(b"normal", "a.txt", required_av=True,
            av_adapter=lambda *_: {"status": "completed", "real": True, "revision": "pretend"})
        self.assertEqual(result["verdict"], "incomplete")
        self.assertFalse(result["engines"]["av"]["real"])
        self.assertEqual(result["engines"]["av"]["revision"], "test-only")

    def test_test_adapter_detection_is_blocked_and_labeled(self):
        result = scan(b"fake synthetic marker", "a.txt",
            av_adapter=lambda *_: {"status": "completed", "detected": True, "detail": "synthetic"})
        self.assertEqual(result["verdict"], "dangerous")
        self.assertEqual(result["engines"]["av"]["source"], "injected_test")

    def test_av_failure_and_adapter_exception_incomplete(self):
        def failure(*_):
            raise RuntimeError("synthetic")
        result = scan(b"x", "x.txt", av_adapter=failure)
        self.assertFalse(result["complete"])
        self.assertEqual(result["verdict"], "incomplete")

    def test_truncated_pe_elf_shortcut_and_macho_magic_cannot_hide_as_document(self):
        for magic in (b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xcf", b"L\0\0\0\1\x14\2\0\0\0\0\0\xc0\0\0\0"):
            with self.subTest(magic=magic):
                result = scan(magic + b"synthetic", "invoice.pdf")
                self.assertEqual(result["verdict"], "dangerous")
                self.assertIn("disguised_executable", rules(result))

    def test_filename_disguise_and_bidi(self):
        result = scan(b"synthetic", "invoice.pdf.EXE")
        self.assertIn("double_extension", rules(result))
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("bidirectional_filename", rules(scan(b"plain", "pay\u202etxt.txt")))

    def test_html_encodings_and_entity_obfuscation(self):
        text = '<form action="https://invalid.test"><input type="passWORD"><a href="java&#x09;script:synthetic">click</a></form>'
        for data in (text.encode(), codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
                     codecs.BOM_UTF32_BE + text.encode("utf-32-be")):
            result = scan(data, "invoice.html")
            self.assertEqual(result["verdict"], "dangerous")
            self.assertIn("credential_form", rules(result))
            self.assertIn("active_uri", rules(result))

    def test_html_attribute_case_self_closing_svg_and_renamed_html(self):
        result = scan(b'<svg><script/><a xlink:href="data:text/html,x"/><g ONLOAD="synthetic"/></svg>', "image.txt")
        self.assertEqual(result["verdict"], "review")
        self.assertTrue({"active_html", "active_uri", "html_event_handler"} <= rules(result))

    def test_link_target_mismatch_is_passive(self):
        with patch.object(risk.subprocess, "run", side_effect=AssertionError("must not execute")):
            result = scan(b'<a href="https://other.invalid/a">https://bank.invalid/login</a>', "mail.html")
        self.assertIn("link_target_mismatch", rules(result))
        self.assertEqual(result["verdict"], "review")

    def test_unknown_html_encoding_and_html_limit_are_incomplete(self):
        result = scan(b'<meta charset="made-up"><p>x</p>', "x.html")
        self.assertFalse(result["complete"])
        result = scan(b"<p>" + b"x" * 100 + b"</p>", "x.html", max_html_bytes=30)
        self.assertIn("html_size_limit", rules(result))
        self.assertFalse(result["complete"])

    def test_nested_mail_attachment_magic_and_provenance(self):
        nested = EmailMessage()
        nested["Subject"] = "synthetic nested"
        nested.set_content("hello")
        nested.add_attachment(b"MZsynthetic", maintype="application", subtype="octet-stream", filename="invoice.pdf")
        outer = EmailMessage()
        outer["Subject"] = "synthetic"
        outer.set_content("hello")
        outer.add_attachment(nested)
        result = scan(outer.as_bytes(), "outer.eml")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("disguised_executable", rules(result))
        attached = [m for m in result["members"] if m["member_name"] == "invoice.pdf"]
        self.assertEqual(attached[0]["sha256"], risk.hashlib.sha256(b"MZsynthetic").hexdigest())
        self.assertGreater(attached[0]["depth"], 1)

    def test_mail_html_transfer_and_charset(self):
        body = '<form><input type="password"></form>'.encode("utf-16")
        message = b"Subject: x\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-16\r\nContent-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(body)
        result = scan(message, "x.eml")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("credential_form", rules(result))

    def test_mail_bad_transfer_or_multipart_and_msg_are_incomplete(self):
        bad = b"Subject: x\r\nContent-Type: multipart/mixed; boundary=missing\r\n\r\nx"
        self.assertFalse(scan(bad, "x.eml")["complete"])
        self.assertFalse(scan(b"Subject: x\r\n\r\nx", "x.msg")["complete"])
        transfer = b"Subject: x\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!invalid"
        self.assertFalse(scan(transfer, "x.eml")["complete"])

    def test_mail_depth_and_part_budget_not_reported_clean(self):
        nested = EmailMessage()
        nested["Subject"] = "x"
        nested.set_content("hello")
        for _ in range(5):
            parent = EmailMessage()
            parent["Subject"] = "x"
            parent.set_content("hello")
            parent.add_attachment(nested)
            nested = parent
        result = scan(nested.as_bytes(), "x.eml", max_depth=2)
        self.assertFalse(result["complete"])
        self.assertIn("depth_limit", rules(result))

    def test_normal_zip_recursive_members(self):
        data = zipped([("folder/notes.txt", b"hello"), ("nested.zip", zipped([("inner.txt", b"world")]))])
        result = scan(data, "package.zip")
        self.assertEqual(result["verdict"], "no_detection")
        self.assertEqual(len(result["members"]), 3)

    def test_archive_path_escape_drive_unc_and_ads(self):
        for path in ("../escape.txt", "C:\\outside.txt", "\\\\host\\file.txt", "a:stream.txt", "/absolute.txt"):
            with self.subTest(path=path):
                result = scan(zipped([(path, b"plain")]), "x.zip")
                self.assertEqual(result["verdict"], "dangerous")
                self.assertIn("archive_path_escape", rules(result))

    def test_archive_symlink_blocks_without_extraction(self):
        info = zipfile.ZipInfo("link.txt")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        result = scan(zipped([(info, b"../outside.txt")]), "x.zip")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("archive_link", rules(result))

    def test_archive_encryption_metadata_cannot_look_complete(self):
        data = bytearray(zipped([("normal.txt", b"synthetic")]))
        struct.pack_into("<H", data, 6, struct.unpack_from("<H", data, 6)[0] | 1)
        offset = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, offset + 8, struct.unpack_from("<H", data, offset + 8)[0] | 1)
        result = scan(bytes(data), "x.zip")
        self.assertFalse(result["complete"])
        self.assertIn("encrypted_archive_member", rules(result))

    def test_archive_ratio_total_member_and_depth_limits(self):
        result = scan(zipped([("bomb.txt", b"0" * 10000)], zipfile.ZIP_DEFLATED), "x.zip", max_ratio=2)
        self.assertIn("expansion_limit", rules(result))
        self.assertFalse(result["complete"])
        result = scan(zipped([("a.txt", b"abc"), ("b.txt", b"def")]), "x.zip", max_expanded_bytes=4)
        self.assertFalse(result["complete"])
        result = scan(zipped([("a/", b""), ("b/", b""), ("c.txt", b"x")]), "x.zip", max_members=2)
        self.assertIn("member_limit", rules(result))
        deep = zipped([("nested.zip", zipped([("inner.zip", zipped([("x.txt", b"x")]))]))])
        self.assertIn("depth_limit", rules(scan(deep, "x.zip", max_depth=1)))

    def test_malformed_zip_and_invalid_office_incomplete(self):
        self.assertFalse(scan(b"PK\x03\x04synthetic", "x.zip")["complete"])
        self.assertIn("invalid_office_package", rules(scan(zipped([("x.txt", b"x")]), "x.docx")))

    def test_ooxml_external_links_utf16_macro_and_activity(self):
        rels = '<?xml version="1.0" encoding="UTF-16"?><Relationships><Relationship TargetMode="External" Target="https://invalid.test/"/></Relationships>'.encode("utf-16")
        types = b'<Types><Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'
        data = zipped([("[Content_Types].xml", types), ("word/document.xml", b"<document/>"),
            ("word/_rels/document.xml.rels", rels), ("word/vbaProject.bin", b"synthetic"),
            ("word/embeddings/object.bin", b"synthetic")])
        result = scan(data, "x.docx")
        self.assertTrue({"office_external_relationship", "embedded_macro", "office_macro_content_type",
            "office_active_or_embedded_object"} <= rules(result))
        self.assertFalse(result["complete"])

    def test_normal_ooxml_success_and_entity_parser_gap(self):
        parts = [("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<document>normal</document>")]
        self.assertEqual(scan(zipped(parts), "x.docx")["verdict"], "no_detection")
        parts[1] = ("word/document.xml", b'<!DOCTYPE x [<!ENTITY x "synthetic">]><document>&x;</document>')
        result = scan(zipped(parts), "x.docx")
        self.assertFalse(result["complete"])
        self.assertIn("office_xml_entity", rules(result))

    def test_tar_traversal_special_members_and_no_extraction(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 5
            archive.addfile(info, io.BytesIO(b"plain"))
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../other"
            archive.addfile(link)
        result = scan(output.getvalue(), "x.tar")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("archive_link_or_special_member", rules(result))

    def test_compressed_streams_and_expansion_limits(self):
        for encode, extension in ((gzip.compress, ".gz"), (bz2.compress, ".bz2"), (lzma.compress, ".xz")):
            with self.subTest(extension=extension):
                self.assertEqual(scan(encode(b"normal content"), "x.txt" + extension)["verdict"], "no_detection")
                result = scan(encode(b"0" * 10000), "x.txt" + extension, max_ratio=2)
                self.assertFalse(result["complete"])
        result = scan(gzip.compress(b"normal") + gzip.compress(b"MZsynthetic"), "x.txt.gz")
        self.assertFalse(result["complete"])
        self.assertIn("compressed_trailing_data", rules(result))

    def test_pdf_name_obfuscation_and_compressed_hidden_objects(self):
        result = scan(b"%PDF-1.7\n1 0 obj << /Java#53cript (synthetic) >> endobj\n%%EOF", "x.pdf")
        self.assertIn("pdf_active_object", rules(result))
        result = scan(b"%PDF-1.7\n1 0 obj << /Filter /FlateDecode >> endobj\n%%EOF", "x.pdf")
        self.assertFalse(result["complete"])
        self.assertIn("pdf_opaque_stream_requires_parser", rules(result))

    def test_images_invalid_unknown_and_size_budget(self):
        self.assertEqual(scan(png(), "x.png")["verdict"], "no_detection")
        self.assertFalse(scan(png() + b"MZsynthetic", "x.png")["complete"])
        self.assertFalse(scan(b"pretend pdf", "x.pdf")["complete"])
        self.assertFalse(scan(b"unknown", "x.weird")["complete"])
        self.assertFalse(scan(b"large data", "x.txt", max_bytes=3)["complete"])

    def test_dangerous_detection_does_not_erase_incomplete(self):
        data = zipped([("../x.txt", b"plain"), ("x.weird", b"opaque")])
        result = scan(data, "x.zip")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertFalse(result["complete"])

    def test_budget_parameter_type_validation(self):
        for value in (0, -1, True, "1", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scan(b"x", "x.txt", max_bytes=value)
        with self.assertRaises(TypeError):
            risk.inspect_bytes("text", "x.txt")

    def test_executable_script_blocks_even_when_synthetic_content_is_harmless(self):
        result = scan(b"Write-Host harmless synthetic fixture", "malicious.ps1")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("executable_content", rules(result))

    def test_finding_cap_preserves_a_later_dangerous_hit(self):
        result = scan(b'<script></script><form><input type="password"></form>', "x.html", max_findings=2)
        self.assertEqual(result["verdict"], "dangerous")
        self.assertFalse(result["complete"])
        self.assertLessEqual(len(result["findings"]), 2)
        self.assertTrue(any(f["severity"] == "dangerous" for f in result["findings"]))

    def test_srcdoc_credential_markup_and_depth_are_inspected(self):
        result = scan(b'<iframe srcdoc="&lt;form&gt;&lt;input type=&quot;password&quot;&gt;&lt;/form&gt;"></iframe>', "x.html")
        self.assertEqual(result["verdict"], "dangerous")
        self.assertIn("credential_form", rules(result))

    def test_standalone_av_rejects_missing_receipt_errors_and_overlong_output(self):
        for output in (b'', b'placeholder done', b'stdin: OK\nERROR decoding member',
                       b'stdin: OK\nWARNING size limit exceeded', b'stdin: OK\n' + b'x' * 128000):
            def fake_process(args, **kwargs):
                kwargs['stdout'].write(b'ClamAV 1.4.3/28000/date\n' if '--version' in args else output)
                return risk.subprocess.CompletedProcess(args, 0)
            with self.subTest(output=output[:50]), patch.object(risk.subprocess, 'run', side_effect=fake_process):
                result = scan(b'normal', 'x.txt', required_av=True, clamav_path='synthetic-scanner')
            self.assertFalse(result['complete'])
            self.assertEqual(result['verdict'], 'incomplete')

    def test_standalone_av_rejects_placeholder_database_and_unbounded_version(self):
        for version in (b'ClamAV 1.4.3', b'ClamAV 1.4.3/unknown/date',
                        b'ClamAV 1.4.3/28000', b'ClamAV 1.4.3/28000/' + b'x' * 4000):
            def fake_process(args, **kwargs):
                kwargs['stdout'].write(version if '--version' in args else b'stdin: OK\n')
                return risk.subprocess.CompletedProcess(args, 0)
            with self.subTest(version=version[:50]), patch.object(risk.subprocess, 'run', side_effect=fake_process):
                result = scan(b'normal', 'x.txt', required_av=True, clamav_path='synthetic-scanner')
            self.assertFalse(result['complete'])
            self.assertEqual(result['engines']['av']['revision'], 'unknown')
    def test_real_av_timeout_and_bad_version_are_incomplete(self):
        with patch.object(risk.subprocess, "run", side_effect=risk.subprocess.TimeoutExpired("synthetic", 1)):
            result = scan(b"x", "x.txt", required_av=True, clamav_path="synthetic-scanner")
        self.assertFalse(result["complete"])

    def test_xz_dictionary_memory_and_mime_parser_tree_budget(self):
        data = lzma.compress(b"normal", filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 16 * 1024 * 1024}])
        result = scan(data, "x.txt.xz", max_decoder_memory_bytes=1024 * 1024)
        self.assertFalse(result["complete"])
        message = b"Subject: x\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n" + (b"--x\r\nContent-Type: text/plain\r\n\r\nx\r\n" * 10) + b"--x--\r\n"
        result = scan(message, "x.eml", max_members=2)
        self.assertIn("mail_parser_member_limit", rules(result))
        self.assertFalse(result["complete"])
        with patch.object(risk.subprocess, "run", return_value=risk.subprocess.CompletedProcess([], 0, stdout=b"not antivirus")):
            result = scan(b"x", "x.txt", required_av=True, clamav_path="synthetic-scanner")
        self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
