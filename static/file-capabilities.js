/* Render the server's live format contract for both import modes. */
(() => {
  async function loadFormats() {
    const card = document.getElementById('newImportCard');
    if (!card || typeof api !== 'function') return;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = '支持的文件类型与当前解析能力';
    details.appendChild(summary);
    const content = document.createElement('div');
    content.setAttribute('role', 'status');
    content.textContent = '正在读取服务器格式能力…';
    details.appendChild(content);
    card.appendChild(details);
    try {
      const data = await api('/api/file-capabilities');
      content.textContent = '';
      const note = document.createElement('p');
      note.textContent = '普通导入和大包深度解析使用相同格式规则。预览只读取有界样本；缺组件、加密、损坏、超时和部分解析会单独标记。';
      content.appendChild(note);
      for (const [label, predicate] of [
        ['文档正文', row => row.document && !row.archive_members],
        ['压缩包与压缩流', row => row.archive_members],
        ['邮件专区逐封导入', row => row.mail_source]
      ]) {
        const line = document.createElement('p');
        const rows = (data.formats || []).filter(predicate);
        line.textContent = label + '：' + rows.map(row => row.extension +
          (row.availability === 'dependency_missing' ? '（缺组件）' : '')).join('、');
        content.appendChild(line);
      }
      const missing = (data.formats || []).filter(row => row.missing_dependencies?.length);
      if (missing.length) {
        const line = document.createElement('p');
        line.textContent = '当前不可用：' + missing.map(row => row.extension + ' 缺少 ' + row.missing_dependencies.join(' / ')).join('；');
        content.appendChild(line);
      }
      const ocr = document.createElement('p');
      ocr.textContent = data.ocr?.available ? 'OCR 组件及模型文件已就绪，识别结果仍受质量与预算影响。' : 'OCR 组件或模型文件未就绪，图片与扫描件暂不能承诺文字识别。';
      content.appendChild(ocr);
    } catch (_) {
      content.textContent = '暂时无法读取格式能力，请刷新页面重试。';
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadFormats);
  else loadFormats();
})();
