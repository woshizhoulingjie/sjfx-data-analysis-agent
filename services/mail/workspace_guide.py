"""Read-only workspace guidance backed by the installed mail services."""
from __future__ import annotations

from config import Config
from .analysis_api import FILTERS
from .source import AdapterRegistry


_FORMATS = {
    'eml': ('EML 邮件', ['.eml'], '导入单封 EML 邮件。'),
    'zip': ('ZIP 邮件包', ['.zip'], '收集 ZIP 中的 EML 邮件；其他文件不会作为独立邮件导入。'),
    'directory': ('邮件目录', [], '递归收集服务器目录中的 EML 邮件。'),
    'mbox': ('MBOX 邮箱', ['.mbox', '.mbx'], '导入 MBOX 邮箱内的邮件。'),
    'msg': ('Outlook MSG', ['.msg'], '需要服务器安装 extract-msg 解析器。'),
    'pst': ('Outlook PST', ['.pst'], '需要服务器安装 readpst 转换工具。'),
}
_SCOPE_LABELS = {
    'date_from': '开始日期', 'date_to': '结束日期', 'q': '关键词',
    'has_attachment': '是否含附件', 'participant': '参与方',
    'conversation_id': '会话', 'message_keys': '所选邮件', 'roles': '参与角色',
    'include_explicit_attachments': '阅读显式附件', 'issue_id': '事项',
    'as_of': '截止时间', 'manifest': '固定范围清单', 'view': '资料视图',
}
_SCOPE_NOTES = [
    '分析限于当前邮件资料包；日期、关键词、参与方等条件与所选邮件取交集。',
    '所选邮件为空时没有可分析邮件；单封和双方分析的上下文也限于当前范围。',
    '显式附件是否阅读由分析范围决定；未解析或 OCR 未完成的内容会形成覆盖缺口。',
    '任务发起后保留分析范围；修改筛选条件不会扩大已经发起的任务。',
]


def workspace_guide(services):
    """Report parser/runtime availability without scanning files or reading rows."""
    model_available = bool(getattr(services, 'model', None))
    queue_available = bool(getattr(services, 'job_factory', None))
    analysis_available = model_available and queue_available
    formats = []
    for capability in AdapterRegistry().support_matrix():
        name = capability['format']
        label, extensions, description = _FORMATS.get(name, (name.upper(), [], '邮件来源解析器。'))
        formats.append({**capability, 'label': label, 'extensions': extensions, 'description': description})
    modes = [
        ('research', '自由研究', '围绕问题检索并梳理邮件证据、变化和资料缺口。',
         '输入研究问题；可沿用当前筛选范围或已有结果继续追问。',
         '研究回答、邮件或附件证据及覆盖说明。',
         ['检索、研究或全量筛查由研究问题自动判断；要求所有、全部或逐封时进入全量筛查。']),
        ('message', '单封分析', '梳理一封邮件的通信意图、条件及下一步要求。',
         '先打开一封邮件，可补充分析目标。',
         '单封通信分析与相关原文依据。',
         ['目标邮件必须在当前分析范围内；相关上下文不会越出该范围。']),
        ('pair', '双方分析', '梳理两位参与方的目标、分歧和通信变化。',
         '选择两位参与方，可补充分析目标。',
         '双方通信分析与相关原文依据。',
         ['以双方通信作为目标邮件，结合范围内相关上下文；不自动分析所有参与方。']),
        ('screen', '逐封筛查', '对固定邮件范围逐封判断是否符合研究目标。',
         '输入筛查目标并确认邮件范围。',
         '逐封判断、证据与无法判断或失败的覆盖说明。',
         ['对范围内固定清单逐封筛查；完成状态和覆盖说明决定能否视为完整结果。']),
    ]
    return {
        'ok': True, 'version': 1,
        'imports': {
            'path_location': 'server',
            'input': '服务器允许资料目录内的文件或目录路径。',
            'formats': formats,
            'scope_notes': ['此路径由服务器读取，浏览器中的本机路径不能直接作为服务器来源。',
                            '导入后再浏览和分析；导入是否可排队取决于任务队列是否就绪。'],
        },
        'analysis_modes': [
            {'kind': kind, 'label': label, 'purpose': purpose, 'input': input_text,
             'output': output, 'scope_notes': notes, 'available': analysis_available}
            for kind, label, purpose, input_text, output, notes in modes
        ],
        'scope': {'fields': [{'key': key, 'label': _SCOPE_LABELS[key]} for key in FILTERS],
                  'notes': list(_SCOPE_NOTES)},
        'runtime': {'model_available': model_available, 'queue_available': queue_available,
                    'budgets': {
                        'research_model_calls': Config.MAIL_RESEARCH_MAX_MODEL_CALLS,
                        'analysis_model_calls': Config.MAIL_ANALYSIS_MAX_MODEL_CALLS,
                        'person_model_calls': Config.MAIL_PERSON_MAX_MODEL_CALLS,
                        'tokens': Config.MAIL_RESEARCH_MAX_TOKENS,
                        'tool_calls': Config.MAIL_RESEARCH_MAX_TOOL_CALLS,
                        'seconds': Config.MAIL_RESEARCH_TIMEOUT_SECONDS,
                        'limits': {'model_calls': Config.MAIL_RESEARCH_MODEL_CALL_LIMIT,
                                   'tokens': Config.MAIL_RESEARCH_TOKEN_LIMIT,
                                   'tool_calls': Config.MAIL_RESEARCH_TOOL_CALL_LIMIT,
                                   'seconds': Config.MAIL_RESEARCH_TIMEOUT_LIMIT_SECONDS},
                    }},
    }


def register_workspace_guide_routes(app, services):
    from web_compat import jsonify
    from .api import _mail_endpoint, _owner

    @app.route('/api/mail/workspace-guide')
    @_mail_endpoint
    def mail_workspace_guide():
        _owner(services)
        response = jsonify(workspace_guide(services))
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return response
