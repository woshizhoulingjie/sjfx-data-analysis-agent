/* Explicit file translation only. No import, mail, or file-protection hooks. */
(function (root) {
  'use strict';
  const fields = ['original', 'translated', 'working'];
  const escape = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
  let cached = null;
  let renderedKey = '';
  let currentDocument = null;
  let fontSize = 17;

  function cancelled() {
    const error = new Error('已切换文件，停止读取旧文档');
    error.name = 'AbortError';
    return error;
  }

  function signature(page) {
    return JSON.stringify([page.status, page.full_translation, page.provider_id, page.source_level,
      page.progress, page.titles, page.errors, page.warnings,
      ...fields.map((key) => page[key] ? [page[key].total_characters, page[key].text] : null)]);
  }

  // Server offsets count Unicode code points, not JavaScript UTF-16 code units.
  const characters = (text) => Array.from(text).length;

  async function readComplete({ scanId, path, api, isCurrent = () => true, onProgress = () => {} }) {
    const pageSize = 20000;
    async function get(offset) {
      if (!isCurrent()) throw cancelled();
      const query = new URLSearchParams({ path, view: 'bilingual', offset: String(offset), limit: String(pageSize) });
      const page = await api(`/api/translation/${encodeURIComponent(scanId)}?${query}`);
      if (!isCurrent()) throw cancelled();
      if (!page || page.path !== path) throw new Error('返回的文件与所选文件不一致，请重新打开。');
      return page;
    }
    const first = await get(0);
    const key = JSON.stringify([scanId, path, signature(first)]);
    if (cached && cached.key === key) return cached.page;
    const chunks = {};
    const counts = {};
    const totals = {};
    fields.forEach((field) => {
      chunks[field] = []; counts[field] = 0;
      totals[field] = first[field] == null ? null : Number(first[field].total_characters);
      if (totals[field] != null && (!Number.isSafeInteger(totals[field]) || totals[field] < 0)) {
        throw new Error('文档长度无效，无法确认正文是否完整。');
      }
    });
    const total = Math.max(0, ...Object.values(totals).filter((v) => v != null));
    let page = first;
    let offset = 0;
    const revision = JSON.stringify([first.status, first.full_translation, first.progress, first.provider_id]);
    while (true) {
      if (JSON.stringify([page.status, page.full_translation, page.progress, page.provider_id]) !== revision) {
        throw new Error('译文正在更新，请稍后重新打开；不会把不同进度的正文拼在一起。');
      }
      fields.forEach((field) => {
        const part = page[field];
        if ((part == null ? null : Number(part.total_characters)) !== totals[field]) {
          throw new Error('正文在读取期间发生变化，请重新打开。');
        }
        if (part == null) return;
        const text = String(part.text == null ? '' : part.text);
        const length = characters(text);
        const expected = Math.max(0, Math.min(pageSize, totals[field] - offset));
        if (Number(part.offset) !== offset || length !== expected || Boolean(part.has_more) !== (offset + length < totals[field])) {
          throw new Error('正文分页不完整，请重试；当前内容不会标记为全文。');
        }
        chunks[field].push(text); counts[field] += length;
      });
      onProgress(Math.min(offset + pageSize, total), total);
      offset += pageSize;
      if (offset >= total) break;
      page = await get(offset);
    }
    const result = { ...first, reader_complete: true };
    fields.forEach((field) => {
      if (totals[field] == null) return;
      if (counts[field] !== totals[field]) throw new Error('全文长度校验失败，请重试。');
      result[field] = { ...first[field], text: chunks[field].join(''), offset: 0, has_more: false };
    });
    // Only cache finished documents; partial work can change at any moment.
    if (['completed', 'not_required'].includes(first.status)) cached = { key, page: result };
    return result;
  }

  function prose(text) {
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    let paragraph = [];
    const flush = () => { if (paragraph.length) { html.push(`<p>${escape(paragraph.join('\n'))}</p>`); paragraph = []; } };
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (!line.trim()) { flush(); continue; }
      if (heading) { flush(); const level = Math.min(6, heading[1].length + 1); html.push(`<h${level}>${escape(heading[2])}</h${level}>`); continue; }
      // Render only explicit Markdown tables; never guess columns from prose.
      if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1])) {
        flush();
        const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
        const header = cells(line); i += 1; const rows = [];
        while (i + 1 < lines.length && lines[i + 1].trim() && lines[i + 1].includes('|')) rows.push(cells(lines[++i]));
        html.push(`<div class="tr-table-scroll"><table><thead><tr>${header.map((v) => `<th scope="col">${escape(v)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((v) => `<td>${escape(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }
      paragraph.push(line);
    }
    flush();
    return html.join('') || '<p class="tr-empty">此文件暂无可读正文。</p>';
  }

  function completeTranslation(page) {
    const content = page.status === 'not_required' ? selectedText(page, 'original') : selectedText(page, 'translated');
    return Boolean(page.full_translation && ['completed', 'not_required'].includes(page.status) &&
      typeof content === 'string' && content.trim() &&
      !(page.errors || []).length && !(page.warnings || []).includes('source_preserved_after_quality_review'));
  }

  function selectedText(page, view) {
    if (view === 'original') return page.original && page.original.text;
    const text = page.translated && page.translated.text;
    return text != null ? text : (page.working && page.working.text) ?? (page.status === 'not_required' ? page.original && page.original.text : null);
  }

  function render({ page, view, title }) {
    const document = root.document;
    const host = document.getElementById('translationDocumentContent');
    if (!host) return;
    const text = selectedText(page, view);
    const ready = completeTranslation(page);
    const key = JSON.stringify([page.path, view, signature(page)]);
    currentDocument = { page, view, title };
    // Job polling must not reset reading position, selection, or focus.
    if (renderedKey === key && host.querySelector('.tr-reader')) return;
    const sameFile = host.dataset.documentPath === page.path;
    const oldScroll = host.scrollTop;
    host.dataset.documentPath = page.path;
    renderedKey = key;
    const notice = view === 'original' ? '原文阅读 · 按文档顺序完整呈现已解析正文'
      : ready ? '全文已完成 · 可连续阅读、复制或下载'
      : text != null ? '尚未完成全文翻译 · 当前内容可能包含未译原文，请完成翻译后再作为完整译文使用。'
      : '选择文件后点击“翻译当前文件”，完成后将在这里呈现整篇译文。';
    const warnings = (page.warnings || []).map((item) => ({
      line_structure_changed: '换行已按语义调整。',
      reference_metadata_preserved: '参考文献、链接与编号保留原文。',
      source_preserved_after_quality_review: '部分段落翻译未通过校验，仍保留原文；这不是完整中文译文。',
      figure_label_preserved: '图表坐标与标签保留原文，避免改动数值。'
    }[item] || (typeof item === 'string' ? item : JSON.stringify(item))));
    const errors = (page.errors || []).map((item) => typeof item === 'string' ? item : item.message || item.code || JSON.stringify(item));
    const article = (label, content, original) => `<section class="tr-paper" aria-label="${label}"><div class="tr-paper-label">${label}</div><article class="tr-prose"${original ? ' dir="auto"' : ' lang="zh-CN"'}>${content == null ? '<p class="tr-empty">尚无正文，请先启动文件翻译。</p>' : prose(content)}</article><div class="tr-end">${content == null ? '等待正文' : '— 已显示当前正文的全部内容 —'}</div></section>`;
    const content = view === 'bilingual'
      ? `<div class="tr-bilingual">${article('原文', selectedText(page, 'original'), true)}${article('中文译文', selectedText(page, 'translated'), false)}</div>`
      : article(view === 'original' ? '原文' : '中文译文', text, view === 'original');
    host.innerHTML = `<div class="tr-reader"><div class="tr-toolbar" role="group" aria-label="阅读工具"><span>连续阅读</span><button type="button" data-reader-action="smaller" aria-label="缩小正文字号">A−</button><button type="button" data-reader-action="larger" aria-label="放大正文字号">A＋</button><button type="button" data-reader-action="focus" aria-pressed="false">专注阅读</button><button type="button" data-reader-action="copy"${text == null ? ' disabled' : ''}>复制${view === 'original' ? '原文' : '译文'}</button><button type="button" data-reader-action="download"${text == null ? ' disabled' : ''}>下载 TXT</button></div><div class="tr-notice ${ready || view === 'original' ? 'tr-ready' : 'tr-pending'}">${escape(notice)}</div>${view === 'bilingual' ? '<div class="tr-hint">左右均为完整正文；原文与译文长度不同，不按字符强行对齐。</div>' : ''}${errors.length ? `<div class="tr-errors" role="alert">${errors.map(escape).join('<br>')}</div>` : ''}${warnings.length ? `<details class="tr-warnings"><summary>译文说明（${warnings.length}）</summary>${warnings.map(escape).join('<br>')}</details>` : ''}<div class="tr-action-state" role="status" aria-live="polite"></div>${content}</div>`;
    host.style.setProperty('--tr-font-size', `${fontSize}px`);
    host.scrollTop = sameFile ? oldScroll : 0;
    host.onclick = async (event) => {
      const button = event.target.closest('[data-reader-action]');
      if (!button) return;
      const action = button.dataset.readerAction;
      const panel = host.closest('[data-view="translation"]');
      const status = host.querySelector('.tr-action-state');
      if (action === 'smaller' || action === 'larger') {
        fontSize = Math.max(14, Math.min(24, fontSize + (action === 'larger' ? 1 : -1)));
        host.style.setProperty('--tr-font-size', `${fontSize}px`);
        status.textContent = `正文字号：${fontSize}px`; return;
      }
      if (action === 'focus') {
        const focused = panel.classList.toggle('tr-focus');
        button.setAttribute('aria-pressed', String(focused));
        button.textContent = focused ? '退出专注' : '专注阅读'; return;
      }
      const current = currentDocument;
      const body = selectedText(current.page, current.view);
      if (body == null) return;
      const incomplete = current.view !== 'original' && !completeTranslation(current.page);
      const output = (incomplete ? '【未完成的译文，可能包含未译原文】\n\n' : '') + body;
      try {
        if (action === 'copy') {
          if (!root.navigator.clipboard) throw new Error('当前连接不支持剪贴板，请下载 TXT 或选中正文复制。');
          await root.navigator.clipboard.writeText(output); status.textContent = '已复制当前完整正文。';
        } else if (action === 'download') {
          const blob = new root.Blob(['\ufeff', output], { type: 'text/plain;charset=utf-8' });
          const url = root.URL.createObjectURL(blob);
          const link = document.createElement('a'); link.href = url;
          link.download = `${String(current.title || '文档').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').slice(0, 120)}-${current.view === 'original' ? '原文' : incomplete ? '未完成译文' : '中文译文'}.txt`;
          link.click(); root.setTimeout(() => root.URL.revokeObjectURL(url), 1000);
          status.textContent = '已导出当前完整正文。';
        }
      } catch (error) { status.textContent = error.message || '操作失败，请重试。'; }
    };
    const focus = host.querySelector('[data-reader-action="focus"]');
    const focused = host.closest('[data-view="translation"]').classList.contains('tr-focus');
    focus.setAttribute('aria-pressed', String(focused)); focus.textContent = focused ? '退出专注' : '专注阅读';
  }

  function reset() { cached = null; currentDocument = null; renderedKey = ''; }
  const api = { readComplete, render, reset, prose, completeTranslation, selectedText };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.SJFXTranslationReader = api;
}(typeof window !== 'undefined' ? window : globalThis));
