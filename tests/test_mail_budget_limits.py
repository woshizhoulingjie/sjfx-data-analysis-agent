"""Whole-task allowance and hard supervisor boundary regressions."""
import ast
from pathlib import Path
from unittest import TestCase

from config import Config
from services.mail.research.intent import build_task_spec, research_budget, research_budget_limits
from services.mail.person_corpus import PERSON_DEFAULT_MODEL_CALL_BUDGET, PERSON_MAX_MODEL_CALL_BUDGET


class BudgetContractTests(TestCase):
    def test_direct_specs_receive_current_defaults_and_preserve_explicit_small_budget(self):
        spec=build_task_spec('分析报价条件','pkg','v1',0)
        expected=research_budget()
        self.assertEqual(spec['budget'],expected)
        self.assertEqual(expected['max_model_calls'],512)
        self.assertEqual(expected['max_tokens'],5000000)
        small=build_task_spec('查询交付日期','pkg','v1',0,max_model_calls=0,max_tool_calls=2,max_seconds=60)
        self.assertEqual(small['budget']['max_model_calls'],0)
        self.assertEqual(small['budget']['max_tool_calls'],2)
        self.assertEqual(small['budget']['max_seconds'],60)

    def test_all_limits_are_accepted_and_higher_or_invalid_values_rejected(self):
        limits=research_budget_limits()
        self.assertEqual(research_budget(limits),{**research_budget(),**limits})
        for key,maximum in limits.items():
            for invalid in (maximum+1,True,'100',-1):
                with self.subTest(key=key,invalid=invalid),self.assertRaises(ValueError):
                    research_budget({key:invalid})
        for invalid in ([],False,'budget'):
            with self.assertRaises(ValueError):research_budget(invalid)
        with self.assertRaises(ValueError):research_budget({'max_context_tokens':1000000})

    def test_resume_budget_preserves_scope_parameters_and_never_reduces_usage_allowance(self):
        original=research_budget()
        changed=research_budget({'max_model_calls':1024,'max_seconds':172800},previous=original)
        self.assertEqual(original['max_model_calls'],512)
        self.assertEqual(changed['max_context_tokens'],original['max_context_tokens'])
        self.assertEqual(changed['max_tokens'],original['max_tokens'])
        with self.assertRaises(ValueError):research_budget({'max_tokens':original['max_tokens']-1},previous=original)
        self.assertEqual(PERSON_DEFAULT_MODEL_CALL_BUDGET,4096)
        self.assertEqual(PERSON_MAX_MODEL_CALL_BUDGET,Config.MAIL_RESEARCH_MODEL_CALL_LIMIT)

    def test_supervisor_honors_selected_timeout_with_checkpoint_grace_and_finite_ceiling(self):
        # Load only the actual supervisor boundary without starting worker services.
        module=ast.parse((Path(__file__).resolve().parents[1]/'worker.py').read_text(encoding='utf-8'))
        fn=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='_task_runtime_limit')
        namespace={'Config':Config}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'worker.py','exec'),namespace)
        boundary=namespace['_task_runtime_limit'];grace=Config.MAIL_RESEARCH_WORKER_GRACE_SECONDS
        self.assertEqual(boundary({'task_type':'mail_research','options':{'research_timeout_seconds':172800}}),172800+grace)
        self.assertEqual(boundary({'task_type':'mail_research'}),Config.MAIL_RESEARCH_TIMEOUT_SECONDS+grace)
        for invalid in (True,'forever',0,-1):
            self.assertEqual(boundary({'task_type':'mail_research','options':{'research_timeout_seconds':invalid}}),Config.MAIL_RESEARCH_TIMEOUT_SECONDS+grace)
        self.assertEqual(boundary({'task_type':'mail_research','options':{'research_timeout_seconds':10**12}}),Config.MAIL_RESEARCH_TIMEOUT_LIMIT_SECONDS+grace)
        self.assertEqual(boundary({'task_type':'generate_report'}),Config.JOB_REPORT_TIMEOUT_SECONDS)
