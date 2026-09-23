/* Device proof is obtained from the user's installed assistant, never from another account device. */
(() => {
  'use strict';
  const originalFetch = window.fetch.bind(window);
  const config = originalFetch('/protection/api/status', {credentials:'same-origin'})
    .then(r => r.ok ? r.json() : {mode:'off'}).catch(() => ({mode:'unknown'}));
  window.fetch = async function(input, init) {
    const request = new Request(input, init);
    const url = new URL(request.url);
    if (url.origin !== location.origin || !(/^\/api\//.test(url.pathname) || /^\/outputs\//.test(url.pathname)) ||
        /^\/api\/(auth|admin)\//.test(url.pathname)) return originalFetch(request);
    const status = await config;
    if (status.mode !== 'enforce') return originalFetch(request);
    const sr = await originalFetch('/protection/api/session', {credentials:'same-origin'});
    if (!sr.ok) return new Response(JSON.stringify({ok:false,error:'请先登录',code:'login_required'}),
      {status:401,headers:{'Content-Type':'application/json'}});
    const session = await sr.json();
    try {
      const pr = await originalFetch('http://127.0.0.1:18765/sign', {
        method:'POST',credentials:'omit',headers:{'Content-Type':'application/json','X-SJFX-Client':'1'},
        body:JSON.stringify({session_tag:session.session_tag,method:request.method,target:url.pathname+url.search})
      });
      const proof = await pr.json();
      if (!pr.ok || !proof.ok) throw new Error(proof.error || '助手尚未就绪');
      const headers = new Headers(request.headers);
      Object.entries(proof.headers).forEach(([k,v]) => headers.set(k,v));
      return originalFetch(request, {headers});
    } catch (err) {
      return new Response(JSON.stringify({ok:false,error:'需要启动本地文件助手：'+err.message,
        code:'device_proof_required',protection_url:'/protection'}),
        {status:423,headers:{'Content-Type':'application/json'}});
    }
  };
  const addNavigation = () => config.then(status => {
    if (status.mode === 'off') return;
    const nav=document.querySelector('.side-nav');
    if(nav){const a=document.createElement('a');a.className='nav-item';a.href='/protection';a.textContent='文件保护';nav.appendChild(a);}
  }).catch(() => {});
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',addNavigation); else addNavigation();
})();
