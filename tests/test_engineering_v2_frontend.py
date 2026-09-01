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
        self.assertIn('/static/engineering-v2.css?v=6', self.html)
        self.assertIn('/static/engineering-v2.js?v=10', self.html)
        self.assertNotRegex(self.script, r'https?://|\bcdn\b')
        self.assertNotRegex(self.style, r'@import|https?://')

    def test_api_token_is_normalized_before_becoming_a_request_header(self):
        app_script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('/static/app.js?v=23', self.html)
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

    def test_relationship_graph_and_table_paths_select_file_scope(self):
        self.assertIn("function renderRelationships(overview)", self.script)
        self.assertIn("kind: 'files'", self.script)
        self.assertIn("v2-relationship-node v2-overview-scope", self.script)
        self.assertIn("data-overview-scope-paths", self.script)
        self.assertIn("v2-relationship-path", self.script)
        self.assertIn("关联文件", self.script)
        self.assertIn("packageOverviewScopeFiles", self.script)
        self.assertIn("openBriefFile(path)", self.script)
        self.assertIn(".v2-relationship-node", self.style)
        self.assertIn(".v2-relationship-path", self.style)

    def test_conversation_client_keeps_scope_follow_up_citation_and_promotion_contracts(self):
        self.assertIn("'/api/conversations'", self.script)
        self.assertIn('/api/conversation/${encodeURIComponent(sessionId)}?scan_id=', self.script)
        self.assertIn('/turns', self.script)
        self.assertIn('persist_scope', self.script)
        self.assertIn('turn.citations', self.script)
        self.assertIn('citation.original_text', self.script)
        self.assertIn('citation.translated_text', self.script)
        self.assertIn('watchPromotion', self.script)
        self.assertIn('watchAnalysisTurn', self.script)
        self.assertIn('data-cancel-turn', self.script)
        self.assertIn('data-retry-turn', self.script)
        self.assertIn('data-continue-deep-turn', self.script)
        self.assertIn('/continue-deep-analysis', self.script)
        self.assertIn('data-rebuild-search-index', self.script)
        self.assertIn('/rebuild-search-index', self.script)
        self.assertIn("error.code = payload.code", self.script)
        self.assertIn('promotion_job_id', self.script)
        self.assertIn('系统已自动重新检索并续写回答', self.script)
        self.assertIn('conversationContextChip', self.script)
        self.assertIn('safeMarkdown', self.script)
        self.assertIn('escapeHtml(value)', self.script)
        self.assertIn('state.searchIndex.usable === false', self.script)
        self.assertIn('阶段性索引可用', self.script)
        self.assertIn('data-copy-message', self.script)
        self.assertIn('data-regenerate-message', self.script)
        self.assertIn('analysisQualityMarkup', self.script)
        self.assertIn('turn.quality_metrics', self.script)
        for metric in (
            '范围文件', '候选文件', '实际检查', '未检查', '深析完成',
            '分析批次', '范围检查率', '候选深析率', '覆盖率', '引用',
            '结论支持率', '无证据结论', '反证', '矛盾', '未解析文件',
        ):
            self.assertIn(metric, self.script)
        for stage in ('batching', 'tool_execution', 'repairing'):
            self.assertIn(stage, self.script)
        self.assertIn('.v2-quality', self.style)
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
        self.assertIn('translationListPrevBtn', self.html)
        self.assertIn('translationListNextBtn', self.html)
        self.assertIn('translationSearch', self.html)
        self.assertIn('translationLanguageFilter', self.html)
        self.assertIn('TRANSLATION_LIST_PAGE_SIZE = 100', self.script)
        self.assertIn('translationListOffset', self.script)
        self.assertIn('translationListRequestSeq', self.script)
        self.assertIn('language: controls.language', self.script)
        self.assertIn('escapeHtml(original)', self.script)
        self.assertIn('escapeHtml(translated)', self.script)

    def test_translation_work_list_uses_bounded_server_paging_and_filters(self):
        for element_id in (
            'translationSearch', 'translationLanguageFilter',
            'translationListPrevBtn', 'translationListNextBtn',
            'translationListPageInfo',
        ):
            self.assertIn('id="{}"'.format(element_id), self.html)
        for contract in (
            'TRANSLATION_LIST_PAGE_SIZE = 100', 'translationListRequestSeq',
            'translationListOffset', 'translationSearchTimer',
            'translationLanguageLabel', 'translationListControls',
            'translationListNextBtn', 'source_availability',
        ):
            self.assertIn(contract, self.script)
        self.assertIn('.v2-translation-filters', self.style)
        self.assertIn('.v2-list-pager', self.style)

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

    def test_package_file_queue_and_evidence_traceback_are_frontend_contracts(self):
        app_script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        processing_style = (PROJECT_ROOT / "static" / "large-package-processing.css").read_text(encoding="utf-8")
        self.assertIn('id="fileWorkflowPanel"', self.html)
        self.assertIn('/api/file-workflow/${encodeURIComponent(state.scan.scan_id)}', app_script)
        self.assertIn('FILE_WORKFLOW_LABELS', app_script)
        self.assertIn('normalizedFileStatus', app_script)
        self.assertIn('data-file-workflow-page', app_script)
        self.assertIn('data-file-open', app_script)
        self.assertIn('data-evidence-source', app_script)
        self.assertIn('openEvidenceSource', app_script)
        self.assertIn('data-evidence-prioritize', app_script)
        self.assertIn('prioritizeEvidenceSource', app_script)
        self.assertIn('/api/package-processing/${encodeURIComponent(scanId)}/resume', app_script)
        self.assertIn('.file-workflow-row', processing_style)
        self.assertIn('.evidence-source-link', processing_style)
        self.assertIn('.evidence-prioritize-link', processing_style)


if __name__ == "__main__":
    unittest.main()
