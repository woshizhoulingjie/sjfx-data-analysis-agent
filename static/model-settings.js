(() => {
  'use strict';
  const host = document.getElementById('modelSettingsPanel');
  if (!host) return;
  let state = null, baseline = '', locked = false;
  const labels = {generation: '生成模型', embedding: '向量模型'};
  const backendLabels = {vllm: 'vLLM', ollama: 'Ollama', openai_compatible: 'OpenAI 兼容服务', disabled: '关闭向量模型'};
  const node = (tag, text, attrs = {}) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    return el;
  };
  const message = node('p', '', {role: 'status', 'aria-live': 'polite', class: 'model-settings-status'});
  const results = node('pre', '', {class: 'model-test-results', tabindex: '0'});
  const summary = node('div', undefined, {class: 'model-settings-summary'});
  const form = node('form', undefined, {class: 'model-settings-form', autocomplete: 'off'});
  const control = (role, name) => form.elements.namedItem(role + '.' + name);
  const supportsInterface = () => state && Number.isInteger(state.model_interface_version) && state.model_interface_version >= 2;
  host.replaceChildren(node('h2', '模型服务配置'), node('p', '正在验证管理员权限…'), message);

  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-SJFX-Model-Settings': '1'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('模型设置服务返回无效响应'); }
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  }

  function input(fieldset, role, name, title, value, options = {}) {
    const label = node('label', title), id = 'model-' + role + '-' + name;
    const el = options.values ? node('select') : node('input');
    el.id = id; el.name = role + '.' + name;
    if (options.values) options.values.forEach(([key, caption]) => el.append(node('option', caption, {value: key})));
    else {
      el.type = options.type || 'text';
      if (options.min !== undefined) el.min = options.min;
      if (options.max !== undefined) el.max = options.max;
      if (el.type === 'password') {
        el.autocomplete = 'new-password';
        el.placeholder = value ? '已设置；留空保留，输入替换' : '可选；留空保持未设置';
      }
    }
    if (options.type === 'checkbox') el.checked = Boolean(value);
    else if (options.type !== 'password') el.value = value == null ? '' : String(value);
    label.htmlFor = id; label.append(el); fieldset.append(label);
    return el;
  }

  function payload() {
    const settings = {generation: {}, embedding: {}};
    for (const el of form.querySelectorAll('input,select')) {
      const [role, name] = el.name.split('.');
      if (role === 'embedding' && control(role, 'backend').value === 'disabled' && name !== 'backend') continue;
      if (name === 'api_key' && !el.value) continue;
      settings[role][name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;
    }
    return settings;
  }
  function updateStatus() {
    if (locked || !state) return;
    if (!supportsInterface()) {
      message.textContent = '新模型配置代码已部署，请先重启 Web 与全部 Worker 以启用。当前仅显示运行与已保存配置。';
      return;
    }
    const dirty = JSON.stringify(payload()) !== baseline;
    message.textContent = (dirty ? '表单有未保存修改；当前运行服务仍如上所示。' :
      state.restart_required ? '表单显示已保存配置；重启 Web 与全部 Worker 后生效。' : '已保存配置与当前运行一致。') + ' ' + state.notice;
  }
  function syncDisabled() {
    for (const el of form.querySelectorAll('input,select,button')) {
      const [role, name] = el.name.split('.');
      el.disabled = locked || !supportsInterface() && el.getAttribute('data-model-action') !== 'reload' || role === 'embedding' && name !== 'backend' && control(role, 'backend').value === 'disabled' ||
        role === 'generation' && name === 'supports_json_response_format' && control(role, 'backend').value !== 'openai_compatible';
    }
  }
  function clearBinding(role) {
    for (const name of role === 'generation' ? ['tokenizer_path', 'tokenizer_version', 'model_version'] : ['tokenizer_path', 'version', 'weights_id', 'config_id']) control(role, name).value = '';
    if (role === 'embedding') { control(role, 'dimension').value = '0'; control(role, 'encoding').value = 'plain'; }
  }
  function clearCredential(role) {
    const el = control(role, 'api_key'); el.value = ''; el.placeholder = '新服务凭据（可选）';
    control(role, 'clear_api_key').checked = true;
  }

  function render() {
    form.replaceChildren(); summary.replaceChildren();
    const table = node('table'), head = node('tr');
    ['配置', '生成模型', '向量模型'].forEach(text => head.append(node('th', text)));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const tbody = node('tbody');
    for (const [key, title] of [['active', '当前运行'], ['saved', supportsInterface() ? '已保存（表单）' : '已保存']]) {
      const tr = node('tr'); tr.append(node('th', title));
      for (const role of ['generation', 'embedding']) {
        const row = state[key][role], td = node('td');
        td.append(node('b', (backendLabels[row.backend] || row.backend) + ' / ' + row.model), node('div', row.base_url),
          node('small', (role === 'generation' ? row.enabled : row.backend !== 'disabled') ? row.service_location : '已关闭'));
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(tbody); summary.append(table);
    if (state.active.embedding.lane_overrides) {
      const lanes = state.active.embedding.lanes;
      summary.append(node('p', '当前向量服务存在独立配置：大数据包 ' + lanes.large.base_url + ' / ' + lanes.large.model + '；邮件 ' + lanes.mail.base_url + ' / ' + lanes.mail.model + (state.active.embedding.large_fallback_base_url ? '；大包备用服务 ' + state.active.embedding.large_fallback_base_url : '') + '。保存向量设置后，这些使用位置将统一到表单配置，旧备用地址将清除。', {class: 'model-lane-notice'}));
    }
    for (const role of supportsInterface() ? ['generation', 'embedding'] : []) {
      const row = state.saved[role], fieldset = node('fieldset'); fieldset.append(node('legend', labels[role]));
      const backends = role === 'generation' ? [['vllm', 'vLLM'], ['ollama', 'Ollama'], ['openai_compatible', 'OpenAI 兼容服务']] :
        [['openai_compatible', 'OpenAI 兼容服务'], ['ollama', 'Ollama'], ['disabled', '关闭向量模型']];
      const backend = input(fieldset, role, 'backend', '服务类型', row.backend, {values: backends});
      const url = input(fieldset, role, 'base_url', '服务地址（本机、内网或服务器 HTTP(S)）', row.base_url);
      const model = input(fieldset, role, 'model', '服务中的模型名称', row.model);
      fieldset.append(node('p', '本机地址指 SJFX 运行的机器。使用内网或服务器地址时，业务内容会发送到该服务。', {class: 'model-field-help'}));
      input(fieldset, role, 'timeout_seconds', '请求超时（秒）', row.timeout_seconds, {type: 'number', min: role === 'generation' ? 60 : 2, max: role === 'generation' ? 3600 : 60});
      input(fieldset, role, 'concurrency', role === 'generation' ? '生成并发上限' : '大数据包向量并发', row.concurrency, {type: 'number', min: 1, max: role === 'generation' ? 8 : 4});
      const advanced = node('details', undefined, {class: 'model-advanced-settings'});
      advanced.append(node('summary', '模型版本与分词器（可选）'));
      if (role === 'generation') {
        input(fieldset, role, 'context_tokens', '模型上下文窗口（tokens）', row.context_tokens, {type: 'number', min: 2048, max: 1048576});
        input(fieldset, role, 'enabled', '启用生成模型', row.enabled, {type: 'checkbox'});
        input(fieldset, role, 'supports_json_response_format', '服务支持 JSON response_format', row.supports_json_response_format, {type: 'checkbox'});
        input(advanced, role, 'model_version', '模型版本标识（可选）', row.model_version);
        input(advanced, role, 'tokenizer_path', '与模型匹配的分词器目录（可选）', row.tokenizer_path);
        input(advanced, role, 'tokenizer_version', '分词器版本标识（可选）', row.tokenizer_version);
        advanced.append(node('p', row.tokenizer_status === 'loaded' ? '已保存分词器可加载，预算使用本地分词器。' : row.tokenizer_status === 'unavailable' ? '已保存分词器无法加载，预算使用保守字节上界。' : '未配置分词器，预算使用保守字节上界。', {class: 'model-field-help'}));
        advanced.append(node('p', '不填写分词器时采用保守的字节预算；切换模型或服务会清除旧绑定。', {class: 'model-field-help'}));
      } else {
        input(fieldset, role, 'encoding', '向量编码方式', row.encoding, {values: [['plain', '通用文本'], ['e5', 'E5 前缀（仅匹配 E5 模型）']]});
        input(advanced, role, 'dimension', '已知向量维数（0 为未知）', row.dimension, {type: 'number', min: 0, max: 65536});
        input(advanced, role, 'version', '模型版本标识（可选）', row.version);
        input(advanced, role, 'weights_id', '权重标识（可选）', row.weights_id);
        input(advanced, role, 'config_id', '配置标识（可选）', row.config_id);
        input(advanced, role, 'tokenizer_path', '与向量模型匹配的分词器目录（可选）', row.tokenizer_path);
        fieldset.append(node('p', '统一用于普通检索、大数据包和邮件。切换模型会清除旧维数与版本绑定；已有向量索引需按新身份重建。', {class: 'model-field-help'}));
      }
      fieldset.append(advanced);
      const secret = input(fieldset, role, 'api_key', '认证凭据（留空保留，输入替换）', row.api_key_configured, {type: 'password'});
      const clear = input(fieldset, role, 'clear_api_key', '清除现有认证凭据', false, {type: 'checkbox'});
      secret.addEventListener('input', () => { if (secret.value) clear.checked = false; });
      clear.addEventListener('change', () => { if (clear.checked) secret.value = ''; });
      let signature = url.value + '\n' + model.value;
      function identityChanged() {
        const next = url.value + '\n' + model.value;
        if (next !== signature) {
          if (url.value !== signature.split('\n')[0]) clearCredential(role);
          clearBinding(role); signature = next;
        }
        updateStatus();
      }
      url.addEventListener('input', identityChanged); model.addEventListener('input', identityChanged);
      backend.addEventListener('change', () => {
        if (backend.value !== 'disabled') {
          url.value = ''; model.value = ''; clearBinding(role); clearCredential(role);
          if (role === 'generation') control(role, 'supports_json_response_format').checked = backend.value !== 'openai_compatible';
          signature = '\n';
        }
        syncDisabled(); updateStatus();
      });
      form.append(fieldset);
    }
    const actions = node('div', undefined, {class: 'model-settings-actions'});
    actions.append(node('button', '保存配置', {type: 'submit', class: 'primary'}));
    const reload = node('button', '重新读取', {type: 'button', class: 'secondary', 'data-model-action': 'reload'}); reload.addEventListener('click', load); actions.append(reload);
    for (const role of ['generation', 'embedding']) {
      const button = node('button', '测试' + labels[role], {type: 'button', class: 'secondary'});
      button.addEventListener('click', () => test(role)); actions.append(button);
    }
    form.append(actions); baseline = JSON.stringify(payload()); syncDisabled(); updateStatus();
  }
  function clearSecrets() { form.querySelectorAll('input[type=password]').forEach(el => { el.value = ''; }); }
  function busy(value) { locked = value; syncDisabled(); }
  async function load() {
    busy(true);
    try {
      state = await api('/api/admin/model-settings');
      host.replaceChildren(node('h2', '模型服务配置'), node('p', supportsInterface() ? '管理员可独立选择生成与向量服务。表单从已保存配置读取；保存后重启 Web 和全部 Worker 生效。' : '当前运行的应用接口尚未启用新版模型配置，以下信息只读。'), summary, form, message, results);
      locked = false; results.textContent = ''; render();
    } catch (error) { clearSecrets(); message.textContent = error.message + '；请使用管理员账号登录。'; }
    finally { busy(false); }
  }
  form.addEventListener('input', updateStatus); form.addEventListener('change', updateStatus);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!supportsInterface()) { updateStatus(); return; }
    const draft = payload(); busy(true);
    try { state = await api('/api/admin/model-settings', 'PUT', draft); clearSecrets(); locked = false; results.textContent = ''; render(); }
    catch (error) { clearSecrets(); message.textContent = error.message + '；此次修改未保存。'; }
    finally { busy(false); }
  });
  async function test(role) {
    if (!supportsInterface()) { updateStatus(); return; }
    const draft = payload(); busy(true); message.textContent = '正在请求所选模型服务，最长 20 秒…'; results.textContent = '';
    try {
      const data = await api('/api/admin/model-test', 'POST', {role, settings: {[role]: draft[role]}, timeout_seconds: 20});
      const row = data.roles[role], flag = value => value === true ? '通过' : value === false ? '未通过' : '未知';
      message.textContent = (data.ok ? '实际推理测试通过。' : row.code === 'disabled' ? '所选模型已关闭；未执行推理。' : '测试未通过；请查看诊断结果。') + ' 测试的是表单草稿；未保存、未改变当前运行配置。';
      results.textContent = ['服务可达：' + flag(row.reachable), '配置模型存在：' + flag(row.configured_model_present),
        '实际推理：' + flag(row.inference), '能力：' + JSON.stringify(row.capability || {}),
        '检查详情：' + JSON.stringify(row.checks || {code: row.code}, null, 2)].join('\n');
    } catch (error) { message.textContent = error.message + '；测试未保存配置。'; }
    finally { clearSecrets(); busy(false); }
  }
  load();
})();
