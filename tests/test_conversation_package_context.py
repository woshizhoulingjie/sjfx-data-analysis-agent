import unittest

from services import answer_quality as aq
from services.conversation import ConversationScope, _normalise_citations
from services.conversation_package_context import build_reading_manifest, build_standard_context
from services.ordinary_recall import augment_candidates


class MixedSummaryStorage:
    def __init__(self):
        self.offsets = []

    def get_package_overview(self, _):
        return {"summary": "Persisted package overview"}

    def get_analysis_overview(self, _):
        return {"summary": "Persisted node overview"}

    def list_summaries_page(self, _, offset=0, limit=500):
        self.offsets.append(offset)
        pages = {
            0: {"items": [
                {"path": ".", "type": "folder", "payload": {"summary": "Package summary"}},
                {"path": "topic", "type": "folder", "payload": {"summary": "The delivery node summary reports that the backup battery lasts 72 hours after charging."}},
                {"path": "topic/delivery.txt", "type": "preliminary_file_summary", "payload": {"summary": "Preliminary delivery"}},
            ], "next_offset": 3},
            3: {"items": [
                {"path": "topic/delivery.txt", "type": "deep_file_summary", "payload": {"summary": "Battery lasts 72 hours"}},
                {"path": "topic/delivery.txt", "type": "deep_node_summary", "payload": {"summary": "Wrong node identity"}},
                {"path": "node:group-1", "type": "preliminary_node_summary", "payload": {"summary": "Synthetic node"}},
                {"path": "naming:group-1", "type": "semantic_naming", "payload": {"summary": "Synthetic naming"}},
                {"path": "other/legacy.txt", "type": "file", "payload": {"summary": "The legacy delivery record states that the backup battery lasts 48 hours after charging."}},
            ], "next_offset": None},
        }
        return pages[offset]

    def inventory_paths_under(self, *_):
        return ["topic/delivery.txt", "other/legacy.txt", "other/pending.txt"]


class StandardContextSourceTests(unittest.TestCase):
    def test_all_pages_keep_only_file_summaries_and_pending_inventory(self):
        storage = MixedSummaryStorage()
        context = build_standard_context(storage, "scan")
        files = {row["path"]: row for row in context["file_summaries"]}
        self.assertEqual(set(files), {"topic/delivery.txt", "other/legacy.txt", "other/pending.txt"})
        self.assertEqual(storage.offsets, [0, 3])
        self.assertEqual(files["topic/delivery.txt"]["summary_type"], "deep_file_summary")
        self.assertIn("72", files["topic/delivery.txt"]["summary"])
        self.assertEqual(files["other/legacy.txt"]["summary_type"], "file")
        self.assertEqual(files["other/pending.txt"]["summary"], "")
        self.assertEqual(context["reading_manifest"]["files_read"], 3)
        self.assertEqual(context["reading_manifest"]["summary_backed_files"], 2)
        self.assertEqual(context["reading_manifest"]["files_without_summary"], 1)
        self.assertFalse(context["reading_manifest"]["complete"])
        self.assertEqual(context["package_overview"], storage.get_package_overview("scan"))
        self.assertEqual(context["analysis_overview"], storage.get_analysis_overview("scan"))

    def test_directory_qa_recall_keeps_exact_file_and_no_node_candidate(self):
        context = build_standard_context(MixedSummaryStorage(), "scan")
        scope = ConversationScope(kind="directory", value="topic")
        context["file_summaries"] = [row for row in context["file_summaries"] if scope.contains_source(row["path"])]
        reading = build_reading_manifest(context["file_summaries"])
        exact = {"evidence_id": "TEXT-delivery", "source_path": "topic/delivery.txt",
                 "original_text": "The backup battery will last 72 hours after a full charge.",
                 "text": "The backup battery will last 72 hours after a full charge.",
                 "location": {"source": "text"}, "evidence_quality": {"eligible": True}}
        scores = {row["path"]: 0.9 for row in context["file_summaries"]}
        results = augment_candidates([exact], scores, context, "scan")
        self.assertEqual(results, [exact])
        self.assertEqual(reading["files_read"], 1)
        self.assertEqual(reading["summary_backed_files"], 1)
        self.assertTrue(reading["complete"])
        citations = _normalise_citations(results, limit=10)
        self.assertEqual([row["source_path"] for row in citations], ["topic/delivery.txt"])
        self.assertEqual(citations[0]["original_text"], exact["original_text"])
        self.assertEqual(aq.source_kind(citations[0]), "original")

    def test_real_file_semantic_fallback_preserves_summary_provenance(self):
        context = build_standard_context(MixedSummaryStorage(), "scan")
        rows = augment_candidates([], {"other/legacy.txt": 0.9}, context, "scan")
        citations = _normalise_citations(rows, limit=10)
        self.assertEqual([row["source_path"] for row in citations], ["other/legacy.txt"])
        self.assertEqual(aq.source_kind(citations[0]), "summary")
        self.assertEqual(citations[0]["location"]["source"], "semantic_file_summary")

    def test_preliminary_file_and_persisted_files_without_inventory_survive(self):
        class Storage(MixedSummaryStorage):
            def inventory_paths_under(self, *_):
                return []

            def list_summaries_page(self, *_args, **_kwargs):
                return {"items": [
                    {"path": "archive.zip::delivery.txt", "type": "preliminary_file_summary",
                     "payload": {"summary": "Persisted preliminary delivery record"}},
                    {"path": "historical.txt", "type": "file",
                     "payload": {"summary": "Persisted legacy file record"}},
                    {"path": "archive.zip", "type": "deep_node_summary",
                     "payload": {"summary": "Archive container node"}},
                ], "next_offset": None}

        context = build_standard_context(Storage(), "scan")
        files = {row["path"]: row for row in context["file_summaries"]}
        self.assertEqual(set(files), {"archive.zip::delivery.txt", "historical.txt"})
        self.assertEqual(files["archive.zip::delivery.txt"]["summary_type"], "preliminary_file_summary")


if __name__ == "__main__":
    unittest.main()
