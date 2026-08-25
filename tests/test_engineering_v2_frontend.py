import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EngineeringV2FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.script = (PROJECT_ROOT / "static" / "engineering-v2.js").read_text(encoding="utf-8")
        cls.shell = (PROJECT_ROOT / "static" / "product-shell.js").read_text(encoding="utf-8")
        cls.style = (PROJECT_ROOT / "static" / "engineering-v2.css").read_text(encoding="utf-8")

    def test_page_ids_remain_unique_and_v2_assets_are_local(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)), "HTML element ids must remain unique")
        self.assertIn('/static/engineering-v2.css?v=2', self.html)
        self.assertIn('/static/engineering-v2.js?v=4', self.html)
        self.assertNotRegex(self.script, r'https?://|\bcdn\b')
        self.assertNotRegex(self.style, r'@import|https?://')

    def test_api_token_is_normalized_before_becoming_a_request_header(self):
        app_script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('/static/app.js?v=20', self.html)
        for script in (app_script, self.script):
            self.assertIn('normalizeApiToken', script)
            self.assertIn("removeItem('sjfx_api_token')", script)
            self.assertIn(r'/^[\x21-\x7e]+$/', script)

    def test_shell_exposes_dedicated_chat_translation_and_package_overview_routes(self):
        for route in ("chat", "translation", "overview"):
            self.assertIn('data-route="{}"'.format(route), self.html)
            self.assertIn("{}: '{}'".format(route, route), self.shell)
        self.assertIn("window.SJFXEngineering?.activate(route)", self.shell)
        self.assertIn('data-view="chat"', self.html)
        self.assertIn('data-view="translation"', self.html)

    def test_package_overview_uses_intrinsic_endpoint_and_has_no_pipeline_widgets(self):
        overview_html = self.html.split('data-view="overview"', 1)[1].split('data-view="chat"', 1)[0]
        for system_widget in (
            'progressBar', 'jobStatusChip', 'taskPipelineMount', 'activeTaskList',
            'dashboardCoverage', 'dashboardValue', 'overviewResultMount',
        ):
            self.assertNotIn('id="{}"'.format(system_widget), overview_html)
        self.assertIn('/api/package-overview/', self.script)
        for intrinsic_mount in (
            'packageOverviewTreemap', 'packageOverviewSummary',
            'packageOverviewDirectories', 'packageOverviewFormats', 'packageOverviewTypes',
            'packageOverviewLanguages', 'packageOverviewTimeline', 'packageOverviewTopics',
            'packageOverviewEntities', 'packageOverviewRelationships',
            'packageOverviewDuplicates', 'packageOverviewOutliers',
        ):
            self.assertIn('id="{}"'.format(intrinsic_mount), overview_html)
        for brief_mount in (
            'packageOverviewBriefSummary', 'packageOverviewFindings',
            'packageOverviewDirection', 'packageOverviewResearchDetails',
            'packageOverviewReportBtn', 'packageOverviewScopeFiles',
        ):
            self.assertIn('id="{}"'.format(brief_mount), overview_html)
        self.assertIn('response.research_brief', self.script)
        self.assertIn('renderResearchBrief', self.script)

    def test_overview_dimensions_drill_into_conversation_scope(self):
        self.assertIn("data-overview-scope-kind", self.script)
        self.assertIn("data-overview-scope-paths", self.script)
        self.assertIn("overview_drilldown", self.script)
        self.assertIn("applyOverviewScope", self.script)
        for kind in ("directory", "topic", "entity", "time", "file_type"):
            self.assertIn("kind: '{}'".format(kind), self.script)
        self.assertIn('<option value="file_type">文件类型</option>', self.html)
        self.assertIn("scopeConstraints", self.script)
        self.assertIn("renderSelectedScope", self.script)
        self.assertIn("carryScopeToConversation", self.script)

    def test_conversation_client_keeps_scope_follow_up_citation_and_promotion_contracts(self):
        self.assertIn("'/api/conversations'", self.script)
        self.assertIn('/api/conversation/${encodeURIComponent(sessionId)}?scan_id=', self.script)
        self.assertIn('/messages', self.script)
        self.assertIn('persist_scope', self.script)
        self.assertIn('turn.citations', self.script)
        self.assertIn('citation.original_text', self.script)
        self.assertIn('citation.translated_text', self.script)
        self.assertIn('watchPromotion', self.script)
        self.assertIn('promotion_job_id', self.script)
        self.assertIn('系统已自动重新检索并续写回答', self.script)
        self.assertIn('conversationContextChip', self.script)
        self.assertIn('safeMarkdown', self.script)
        self.assertIn('escapeHtml(value)', self.script)
        self.assertIn('data-copy-message', self.script)
        self.assertIn('data-regenerate-message', self.script)
        for kind in ('package', 'topic', 'directory', 'entity', 'time', 'file_type', 'files'):
            self.assertIn('<option value="{}">'.format(kind), self.html)

    def test_translation_client_keeps_original_chinese_bilingual_and_paging_contracts(self):
        self.assertIn('/api/translation/', self.script)
        self.assertIn('/api/translations/', self.script)
        self.assertIn('/api/translate-package/', self.script)
        self.assertIn("translatePackage('preview_and_priority')", self.script)
        self.assertIn("translatePackage('deep_backfill')", self.script)
        for view in ('translated', 'original', 'bilingual'):
            self.assertIn('data-translation-view="{}"'.format(view), self.html)
        self.assertIn('PAGE_SIZE = 6000', self.script)
        self.assertIn('translationPrevBtn', self.script)
        self.assertIn('translationNextBtn', self.script)
        self.assertIn('escapeHtml(original)', self.script)
        self.assertIn('escapeHtml(translated)', self.script)

    def test_visuals_are_native_responsive_and_accessible(self):
        self.assertIn('<svg class="v2-donut"', self.script)
        self.assertIn('<svg class="v2-timeline-svg"', self.script)
        self.assertIn('<svg class="v2-relationship-svg"', self.script)
        self.assertIn('class="v2-treemap-node', self.script)
        self.assertIn('@media(max-width:780px)', self.style)
        self.assertIn('@media(prefers-reduced-motion:reduce)', self.style)
        self.assertIn(':focus-visible', self.style)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('role="img"', self.script)


if __name__ == "__main__":
    unittest.main()
