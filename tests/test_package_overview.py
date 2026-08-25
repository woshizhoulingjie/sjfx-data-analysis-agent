import unittest

from services.package_overview import (
    SCHEMA_VERSION,
    OverviewLimits,
    PackageOverviewAggregator,
    build_package_overview,
    build_package_overview_from_storage,
)


def scan_fixture():
    return {
        "root": "/data/package",
        "file_count": 4,
        "directory_count": 2,
        "total_size": 10_000_260,
        "tree": {
            "kind": "directory", "path": ".", "scan_depth": 0,
            "file_count": 4, "directory_count": 2,
            "direct_file_count": 1, "direct_directory_count": 2,
            "total_size": 10_000_260,
            "children": [
                {
                    "kind": "directory", "path": "letters", "scan_depth": 1,
                    "file_count": 2, "directory_count": 0,
                    "direct_file_count": 2, "direct_directory_count": 0,
                    "total_size": 160,
                    "children": [
                        {"kind": "file", "path": "letters/a.eml", "extension": ".eml", "size": 100, "modified_at": "2023-01-02T00:00:00+00:00"},
                        {"kind": "file", "path": "letters/b.eml", "extension": ".eml", "size": 60, "modified_at": "2024-02-03T00:00:00+00:00"},
                    ],
                },
                {
                    "kind": "directory", "path": "tables", "scan_depth": 1,
                    "file_count": 1, "directory_count": 0,
                    "direct_file_count": 1, "direct_directory_count": 0,
                    "total_size": 100,
                    "children": [
                        {"kind": "file", "path": "tables/data.csv", "extension": ".csv", "size": 100, "modified_at": "2024-05-01T00:00:00+00:00"},
                    ],
                },
                {"kind": "file", "path": "video.mp4", "extension": ".mp4", "size": 10_000_000},
            ],
        },
    }


def projected_documents():
    return iter([
        {
            "path": "letters/a.eml",
            "payload": {
                "source": {"path": "letters/a.eml", "extension": ".eml", "size": 100},
                "classification": {"document_role": "信件", "primary_topic": "项目交付"},
                "language": "en",
                "entities": {"people": ["Alice", "Bob"], "organizations": ["Acme"]},
                "temporal": {"document_date": "2022-12-31"},
            },
        },
        {
            "path": "letters/b.eml",
            "payload": {
                "source": {"path": "letters/b.eml", "extension": ".eml", "size": 60},
                "classification": {"document_role": "信件", "primary_topic": "项目交付"},
                "languages": ["English", "zh-CN"],
                "named_entities": [
                    {"type": "person", "name": "Alice"},
                    {"type": "organization", "name": "Beta Org"},
                ],
                "document_date": "2023-01-03",
            },
        },
        {
            "path": "tables/data.csv",
            "payload": {
                "source": {"path": "tables/data.csv", "extension": ".csv", "size": 100},
                "classification": {"document_role": "结构化数据", "topic_memberships": ["经营", "项目交付"]},
                "language": {"code": "zh", "name": "中文"},
            },
        },
    ])


class PackageOverviewTests(unittest.TestCase):
    def test_empty_result_has_stable_data_only_schema(self):
        result = build_package_overview()

        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["package"], {
            "root": None, "file_count": 0, "total_bytes": 0,
            "directory_count": 0, "max_depth": 0,
        })
        self.assertEqual(result["directories"]["items"], [])
        self.assertEqual(result["formats"]["items"], [])
        self.assertEqual(result["topics"]["items"], [])
        self.assertEqual(result["entities"]["people"]["items"], [])
        self.assertEqual(result["file_relationships"]["items"], [])
        self.assertEqual(result["duplicates"]["exact_groups"]["items"], [])
        self.assertEqual(result["outliers"]["isolated_files"]["items"], [])

        forbidden = {"progress", "job", "worker", "parsed_files", "pending_files", "failed_files", "model_telemetry"}

        def walk(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(result)

    def test_aggregates_physical_and_semantic_dimensions_without_double_counting(self):
        result = build_package_overview(scan=scan_fixture(), documents=projected_documents())

        self.assertEqual(result["package"]["file_count"], 4)
        self.assertEqual(result["package"]["total_bytes"], 10_000_260)
        self.assertEqual(result["package"]["directory_count"], 2)
        self.assertEqual(result["directories"]["items"][0]["path"], ".")
        self.assertEqual(result["directories"]["items"][0]["recursive_file_count"], 4)

        formats = {item["format"]: item for item in result["formats"]["items"]}
        self.assertEqual(formats[".eml"]["file_count"], 2)
        self.assertEqual(formats[".mp4"]["total_bytes"], 10_000_000)
        self.assertEqual(result["formats"]["file_count"], 4)

        doc_types = {item["document_type"]: item["file_count"] for item in result["document_types"]["items"]}
        self.assertEqual(doc_types, {"信件": 2, "结构化数据": 1})
        self.assertEqual(result["document_types"]["unknown_file_count"], 1)

        languages = {item["language"]: item for item in result["languages"]["items"]}
        self.assertEqual(languages["en"]["file_count"], 2)
        self.assertEqual(languages["en"]["label"], "英语")
        self.assertEqual(languages["zh"]["file_count"], 2)
        self.assertEqual(result["languages"]["unknown_file_count"], 1)

        topics = {item["topic"]: item["file_count"] for item in result["topics"]["items"]}
        self.assertEqual(topics["项目交付"], 3)
        self.assertEqual(topics["经营"], 1)

        people = {item["name"]: item["file_count"] for item in result["entities"]["people"]["items"]}
        organizations = {item["name"]: item["file_count"] for item in result["entities"]["organizations"]["items"]}
        self.assertEqual(people, {"Alice": 2, "Bob": 1})
        self.assertEqual(organizations, {"Acme": 1, "Beta Org": 1})

        modified = {item["period"]: item["file_count"] for item in result["timeline"]["file_modified"]["items"]}
        self.assertEqual(modified, {"2024": 2, "2023": 1})
        document_dates = {item["period"]: item["file_count"] for item in result["timeline"]["document_dates"]["items"]}
        self.assertEqual(document_dates, {"2022": 1, "2023": 1})

    def test_analysis_supplies_authoritative_topics_relations_duplicates_and_outliers(self):
        analysis = {
            "topic_clusters": [
                {"topic": "交付计划", "members": ["letters/a.eml", "letters/b.eml"], "representative_documents": ["letters/a.eml"]},
                {"topic": "经营数据", "members": ["tables/data.csv"], "representative_documents": ["tables/data.csv"]},
            ],
            "file_relationships": [
                {"source_file": "letters/a.eml", "target_file": "letters/b.eml", "relation": "reply_to", "weight": 0.9},
                {"source_file": "letters/a.eml", "target_file": "tables/data.csv", "relation": "references", "weight": 0.5},
            ],
            "exact_duplicate_groups": [
                {"group_id": "DUP-1", "sha256": "abc", "canonical": "a.txt", "members": ["a.txt", "copy-a.txt", "copy2-a.txt"]},
                {"group_id": "DUP-2", "sha256": "def", "canonical": "b.txt", "members": ["b.txt", "copy-b.txt"]},
            ],
            "similar_document_clusters": [
                {"cluster_id": "SIM-1", "representative": "old.doc", "members": ["old.doc", "new.doc"]},
            ],
            "anomalous_files": [{"path": "video.mp4", "reason": "唯一的视频资料", "score": 0.8}],
            "isolated_files": [{"path": "tables/data.csv", "reason": "未形成内容引用"}],
        }
        result = build_package_overview(
            scan=scan_fixture(), documents=projected_documents(), analysis=analysis,
        )

        self.assertEqual(
            {item["topic"]: item["file_count"] for item in result["topics"]["items"]},
            {"交付计划": 2, "经营数据": 1},
        )
        relations = result["file_relationships"]
        self.assertEqual(relations["relationship_count"], 2)
        self.assertEqual(relations["items"][0]["relation"], "reply_to")
        exact = result["duplicates"]["exact_groups"]
        self.assertEqual(exact["group_count"], 2)
        self.assertEqual(exact["duplicate_file_count"], 3)
        self.assertEqual(exact["items"][0]["content_hash"], "abc")
        self.assertEqual(result["duplicates"]["near_duplicate_groups"]["duplicate_file_count"], 1)
        self.assertEqual(result["outliers"]["anomalous_files"]["items"][0]["path"], "video.mp4")
        self.assertEqual(result["outliers"]["isolated_files"]["items"][0]["path"], "tables/data.csv")

    def test_high_cardinality_is_bounded_and_disclosed(self):
        limits = OverviewLimits(
            max_items_per_section=3,
            max_representative_files=2,
            max_group_keys=8,
            max_relation_keys=8,
            max_candidate_files=8,
        )
        aggregator = PackageOverviewAggregator(limits)
        for index in range(100):
            aggregator.ingest_document({
                "source": {"path": "f-{}.txt".format(index), "size": index + 1},
                "language": "lang-{}".format(index),
                "topics": ["topic-{}".format(index)],
            }, include_physical=True)
            aggregator._relationships.add(
                "f-{}.txt".format(index), "f-{}.txt".format((index + 1) % 100), weight=index + 1,
            )
        result = aggregator.finalize()

        self.assertLessEqual(len(result["languages"]["items"]), 3)
        self.assertTrue(result["languages"]["truncated"])
        self.assertTrue(result["languages"]["counts_are_approximate"])
        self.assertIsNone(result["languages"]["distinct_count"])
        self.assertGreaterEqual(result["languages"]["distinct_count_lower_bound"], 9)
        self.assertLessEqual(len(result["file_relationships"]["items"]), 3)
        self.assertTrue(result["file_relationships"]["truncated"])
        self.assertLessEqual(len(aggregator._languages.entries), 8)
        self.assertLessEqual(len(aggregator._relationships.entries), 8)

    def test_duplicate_totals_remain_exact_when_group_details_are_truncated(self):
        groups = [
            {
                "group_id": "DUP-{:03d}".format(index),
                "canonical": "{}/canonical.txt".format(index),
                "members": [
                    "{}/canonical.txt".format(index),
                    "{}/copy-1.txt".format(index),
                    "{}/copy-2.txt".format(index),
                ],
            }
            for index in range(20)
        ]
        result = build_package_overview(
            analysis={"exact_duplicate_groups": groups},
            limits=OverviewLimits(max_items_per_section=3),
        )

        exact = result["duplicates"]["exact_groups"]
        self.assertEqual(len(exact["items"]), 3)
        self.assertEqual(exact["group_count"], 20)
        self.assertEqual(exact["duplicate_file_count"], 40)
        self.assertTrue(exact["truncated"])
        self.assertEqual(exact["omitted_count"], 17)

    def test_preview_dates_and_sample_duplicate_candidates_are_visible(self):
        documents = iter([{
            "path": "letters/a.txt",
            "payload": {
                "source": {"path": "letters/a.txt", "extension": ".txt", "size": 20},
                "preview": {
                    "dates": ["2025-04-03"],
                    "entities": {"people": ["Alice"], "organizations": ["Acme"]},
                },
            },
        }])
        result = build_package_overview(
            documents=documents,
            analysis={"sample_duplicate_candidates": [{
                "sample_sha256": "sample-1", "paths": ["a.txt", "copy.txt"],
                "file_count": 2, "kind": "sample_candidate",
            }]},
        )

        years = result["timeline"]["document_dates"]["items"]
        self.assertEqual(years[0]["period"], "2025")
        self.assertEqual(result["entities"]["people"]["items"][0]["name"], "Alice")
        candidates = result["duplicates"]["sample_candidate_groups"]
        self.assertEqual(candidates["group_count"], 1)
        self.assertEqual(candidates["candidate_duplicate_file_count"], 1)
        self.assertFalse(candidates["authoritative"])
        self.assertFalse(result["duplicates"]["exact_groups"]["authoritative"])

    def test_large_package_empty_exact_groups_are_not_claimed_as_verified(self):
        result = build_package_overview(analysis={
            "exact_duplicate_groups": [],
            "policy": {"large_package": {"enabled": True}},
        })
        exact = result["duplicates"]["exact_groups"]
        self.assertEqual(exact["duplicate_file_count"], 0)
        self.assertFalse(exact["authoritative"])
        self.assertEqual(exact["status"], "not_computed_for_entire_package")

    def test_storage_adapter_ingests_content_map_entities_and_sample_duplicates(self):
        class ContentMapStorage:
            def get_scan(self, _scan_id):
                return {"root": "/data", "file_count": 2, "directory_count": 0,
                        "total_size": 20, "tree": {"kind": "directory", "path": ".", "children": []}}

            def iter_documents(self, _scan_id, hydrate=True, batch_size=None):
                return iter(())

            def get_analysis(self, _scan_id):
                return None

            def get_content_map(self, _scan_id):
                return {
                    "entities": {"people": [{"name": "Alice", "file_count": 2}]},
                    "duplicates": [{"sample_sha256": "x", "paths": ["a", "b"],
                                    "file_count": 2, "kind": "sample_candidate"}],
                }

        result = build_package_overview_from_storage(ContentMapStorage(), "scan")
        self.assertEqual(result["entities"]["people"]["items"][0]["file_count"], 2)
        self.assertEqual(result["duplicates"]["sample_candidate_groups"]["group_count"], 1)

    def test_single_relationship_record_and_size_outlier_are_supported(self):
        files = [
            {"kind": "file", "path": "small-{}.txt".format(index), "size": 10}
            for index in range(20)
        ] + [{"kind": "file", "path": "huge.bin", "size": 10_000_000}]
        result = build_package_overview(
            files=files,
            analysis={
                "file_relationships": {
                    "source_file": "small-0.txt", "target_file": "small-1.txt",
                    "relation": "same_event", "weight": 1,
                },
            },
        )

        self.assertEqual(result["file_relationships"]["relationship_count"], 1)
        anomalies = result["outliers"]["anomalous_files"]["items"]
        self.assertEqual(anomalies[0]["path"], "huge.bin")
        self.assertEqual(anomalies[0]["source"], "size_distribution")

    def test_generator_input_never_reads_or_retains_full_text(self):
        class ExplodingText:
            def __str__(self):
                raise AssertionError("overview must not read full text")

        consumed = []

        def documents():
            for index in range(25):
                consumed.append(index)
                yield {
                    "path": "folder/{}.txt".format(index),
                    "payload": {
                        "source": {"path": "folder/{}.txt".format(index), "extension": ".txt", "size": 10},
                        "classification": {"document_role": "文本资料"},
                        "text": ExplodingText(),
                    },
                }

        result = build_package_overview(documents=documents())
        self.assertEqual(consumed, list(range(25)))
        self.assertEqual(result["package"]["file_count"], 25)
        self.assertEqual(result["package"]["total_bytes"], 250)
        self.assertFalse(hasattr(PackageOverviewAggregator(), "documents"))

    def test_storage_adapter_requires_bounded_projection_and_batching(self):
        class FakeStorage:
            def __init__(self):
                self.calls = []

            def get_scan(self, scan_id):
                self.calls.append(("scan", scan_id))
                return scan_fixture()

            def iter_documents(self, scan_id, hydrate=True, batch_size=None):
                self.calls.append(("documents", scan_id, hydrate, batch_size))
                return projected_documents()

            def get_analysis(self, scan_id):
                self.calls.append(("analysis", scan_id))
                return None

        storage = FakeStorage()
        result = build_package_overview_from_storage(storage, "scan-1", batch_size=17)

        self.assertEqual(result["package"]["file_count"], 4)
        self.assertIn(("documents", "scan-1", False, 17), storage.calls)

    def test_unknown_scan_raises_key_error(self):
        class MissingStorage:
            def get_scan(self, _scan_id):
                return None

        with self.assertRaises(KeyError):
            build_package_overview_from_storage(MissingStorage(), "missing")

    def test_custom_limits_bypass_shared_snapshot(self):
        class CachedStorage:
            def __init__(self):
                self.saved = None

            def get_package_overview(self, _scan_id):
                return {"schema_version": SCHEMA_VERSION, "cached": True}

            def get_scan(self, _scan_id):
                return scan_fixture()

            def iter_documents(self, _scan_id, hydrate=True, batch_size=None):
                self.hydrate = hydrate
                self.batch_size = batch_size
                return projected_documents()

            def get_analysis(self, _scan_id):
                return None

            def save_package_overview(self, _scan_id, payload):
                self.saved = payload

        storage = CachedStorage()
        result = build_package_overview_from_storage(
            storage, "scan-1", limits=OverviewLimits(max_items_per_section=2),
        )
        self.assertNotIn("cached", result)
        self.assertIsNone(storage.saved)
        self.assertFalse(storage.hydrate)


if __name__ == "__main__":
    unittest.main()
