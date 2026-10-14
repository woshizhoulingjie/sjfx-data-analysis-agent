/* Attach package approvals; the local runtime rechecks every operation. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const storageKey = 'sjfx-operation-approvals';
  function selectedApprovals() {
    try {
      const selected = JSON.parse(sessionStorage.getItem(storageKey) || '{}');
      if (!selected || typeof selected !== 'object' || Array.isArray(selected)) throw Error('Invalid map');
      const valid = {}, header = {};
      for (const [registration, approval] of Object.entries(selected)) {
        if (registration && approval && typeof approval.approval_id === 'string' && approval.approval_id &&
            Number.isFinite(approval.expires) && approval.expires > Date.now() / 1000) {
          valid[registration] = approval;
          header[registration] = approval.approval_id;
        }
      }
      sessionStorage.setItem(storageKey, JSON.stringify(valid));
      return header;
    } catch {
      sessionStorage.removeItem(storageKey);
      return {};
    }
  }
  window.fetch = function(input, init) {
    const request = new Request(input, init), url = new URL(request.url);
    if (url.origin !== location.origin || !(/^\/api\//.test(url.pathname) || /^\/outputs\//.test(url.pathname)) ||
        /^\/api\/(?:auth|admin)(?:\/|$)/.test(url.pathname)) return nativeFetch(request);
    const approvals = selectedApprovals();
    if (!Object.keys(approvals).length || request.headers.has('X-SJFX-Approvals')) return nativeFetch(request);
    const headers = new Headers(request.headers);
    headers.set('X-SJFX-Approvals', JSON.stringify(approvals));
    // Send once. Local admission does not require a retry for device proof.
    return nativeFetch(new Request(request, {headers}));
  };
  function navigation() {
    const nav = document.querySelector('.side-nav');
    if (nav && !nav.querySelector('[data-package-protection]')) {
      const link = document.createElement('a');
      link.href = '/protection/packages';link.className = 'nav-item';
      link.dataset.packageProtection = '1';link.textContent = '本地资料保护';nav.append(link);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', navigation);
  else navigation();
})();
