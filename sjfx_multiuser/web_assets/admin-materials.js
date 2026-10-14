/* Read-only account material catalog; existing account/view-as controls remain. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  let users = [], user = null, offset = 0, page = null;
  let revision = 0, request = null, historyRevision = 0, sessionId = '', messageOffset = 0;
  const kinds = { ordinary: '普通数据包', large: '大数据包', conversations: '问答历史', translations: '翻译历史' };
  const statuses = { saved: '已保存', completed: '已完成', active: '正常', failed: '失败', queued: '排队中',
    running: '处理中', paused: '已暂停', waiting_for_selection: '等待选择', pending: '等待处理',
    quick_ready: '快速处理完成', preview_ready: '预览就绪', imported: '已导入', processing: '处理中' };
  function say(id, value, error) { $(id).textContent = value || ''; $(id).className = 'msg' + (error ? ' err' : ''); }
  function time(value) {
    if (!value) return '—';
    const raw = typeof value === 'number' ? value * 1000 : String(value).replace(' ', 'T');
    const date = new Date(typeof raw === 'string' && !/[Z+]\d*$/.test(raw) && !/[+-]\d\d:\d\d$/.test(raw) ? raw + 'Z' : raw);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
  }
  async function api(path, options) {
    const timer = new AbortController();
    const timeout = setTimeout(() => timer.abort(), 15000);
    const signal = options?.signal;
    const cancel = () => timer.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) timer.abort();
    try {
      const response = await fetch(path, { credentials: 'same-origin', ...options, signal: timer.signal });
      if (response.status === 401) { location.href = '/login'; throw new Error('请重新登录'); }
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '读取失败（' + response.status + '）');
      return data;
    } finally { clearTimeout(timeout); signal?.removeEventListener('abort', cancel); }
  }
  function cell(row, text, cls) {
    const node = document.createElement('td'); node.textContent = text; if (cls) node.className = cls;
    row.appendChild(node); return node;
  }
  function hideHistory() {
    historyRevision++; sessionId = ''; messageOffset = 0; $('materialConversation').hidden = true;
    $('materialMessages').replaceChildren();
  }
  function render(data) {
    $('materialRows').replaceChildren();
    data.items.forEach(item => {
      const row = document.createElement('tr');
      cell(row, item.name);
      cell(row, item.id + (item.source_id !== item.id ? '\n来源：' + item.source_id : '') + '\n' + item.root, 'mono');
      cell(row, statuses[item.status] || item.status || '—'); cell(row, time(item.updated_at), 'hint');
      const action = cell(row, '');
      if (data.kind === 'conversations') {
        const button = document.createElement('button'); button.className = 'btn'; button.textContent = '查看消息';
        button.addEventListener('click', () => { sessionId = item.id; messageOffset = 0; loadMessages(false); });
        action.appendChild(button);
      }
      $('materialRows').appendChild(row);
    });
    if (!data.items.length) {
      const row = document.createElement('tr'); const empty = cell(row, '该用户暂无' + kinds[data.kind], 'hint');
      empty.colSpan = 5; $('materialRows').appendChild(row);
    }
    $('materialPage').textContent = '共 ' + data.total + ' 条，第 ' + (Math.floor(data.offset / data.limit) + 1) + ' 页';
    $('materialPrev').disabled = data.offset === 0; $('materialNext').disabled = !data.has_more;
  }
  async function load() {
    if (!user) return;
    const version = ++revision, target = user.user_id, kind = $('materialKind').value;
    request?.abort(); request = new AbortController(); hideHistory(); page = null;
    $('materialPrev').disabled = true; $('materialNext').disabled = true;
    $('materialRows').replaceChildren(); say('materialMsg', '正在读取资料…');
    try {
      const data = await api('/api/admin/users/' + encodeURIComponent(target) + '/materials?kind=' + kind + '&offset=' + offset,
        { signal: request.signal });
      if (version !== revision || target !== user?.user_id || kind !== $('materialKind').value) return;
      page = data; render(data); say('materialMsg', '');
    } catch (error) { if (version === revision) say('materialMsg', '读取失败：' + error.message + '。可点击刷新资料重试。', true); }
  }
  async function loadMessages(older) {
    const version = ++historyRevision, target = user?.user_id, sid = sessionId;
    if (!target || !sid) return;
    const nextOffset = older && $('materialMessages').children.length ? messageOffset + 50 : 0;
    $('materialConversation').hidden = false; $('materialOlder').disabled = true;
    if (!older) $('materialMessages').replaceChildren();
    say('materialConversationMsg', '正在读取历史消息…');
    try {
      const data = await api('/api/admin/users/' + encodeURIComponent(target) + '/conversations/' + encodeURIComponent(sid) + '?offset=' + nextOffset);
      if (version !== historyRevision || target !== user?.user_id || sid !== sessionId) return;
      messageOffset = nextOffset; $('materialConversationTitle').textContent = data.title || '历史问答';
      const fragment = document.createDocumentFragment();
      data.messages.forEach(message => {
        const block = document.createElement('div'); block.className = 'stat'; block.style.marginTop = '10px';
        const label = document.createElement('span'); label.textContent = (message.role === 'user' ? '用户' : '助手') + (message.created_at ? ' · ' + time(message.created_at) : '');
        const content = document.createElement('div'); content.style.whiteSpace = 'pre-wrap'; content.style.overflowWrap = 'anywhere';
        content.textContent = String(message.content || '（无文字内容）'); block.append(label, content); fragment.appendChild(block);
      });
      if (older) $('materialMessages').prepend(fragment); else $('materialMessages').replaceChildren(fragment);
      $('materialOlder').disabled = !data.has_more;
      say('materialConversationMsg', '共 ' + data.total + ' 条消息，已显示 ' + Math.min(data.total, nextOffset + data.messages.length) + ' 条');
    } catch (error) {
      if (version === historyRevision) { say('materialConversationMsg', '历史读取失败：' + error.message, true); $('materialOlder').disabled = false; }
    }
  }
  function open(id) {
    user = users.find(item => item.user_id === id);
    if (!user) return;
    offset = 0; $('materialPanel').hidden = false;
    $('materialTitle').textContent = (user.display_name || user.username) + '（' + user.username + '）的资料';
    $('materialOwner').textContent = '归属空间：' + user.owner_id + '。此处按账号统计；停用账号的资料仍保留。';
    const stats = user.data_stats;
    $('materialStats').textContent = stats ? '普通包 ' + stats.ordinary_packages + ' · 大包 ' + stats.large_packages +
      ' · 问答会话 ' + stats.conversations + ' · 翻译记录 ' + stats.translations + '（已完成 ' + stats.completed_translations + '）' : '资料统计暂不可用';
    load(); $('materialPanel').scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
  $('materialKind').addEventListener('change', () => { offset = 0; load(); });
  $('materialRefresh').addEventListener('click', load);
  $('materialPrev').addEventListener('click', () => { offset = Math.max(0, offset - (page?.limit || 25)); load(); });
  $('materialNext').addEventListener('click', () => { if (page?.has_more) { offset += page.limit; load(); } });
  $('materialOlder').addEventListener('click', () => loadMessages(true));
  $('materialWorkbench').addEventListener('click', async () => {
    if (!user) return;
    $('materialWorkbench').disabled = true;
    try {
      await api('/api/admin/view-as', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ owner_id: user.owner_id }) });
      location.href = '/';
    } catch (error) { say('materialMsg', '视角切换失败：' + error.message, true); $('materialWorkbench').disabled = false; }
  });
  window.SJFXAdminMaterials = { open, setUsers(value) {
    users = value;
    if (user) { user = users.find(item => item.user_id === user.user_id) || null; if (user) open(user.user_id); }
  } };
})();
