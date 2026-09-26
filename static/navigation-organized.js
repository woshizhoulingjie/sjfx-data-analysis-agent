/* Presentation only: keep original nodes, routes, listeners and APIs. */
(() => {
  'use strict';
  const KEY = 'sjfx_navigation_groups_v1';
  const groups = [
    {id:'ordinary', title:'普通资料', hint:'导入资料 · 目录与概览', open:true, routes:['source','physical','analysis','overview']},
    {id:'large', title:'大数据包', hint:'批量处理 · 目录与研判', open:false, routes:['large-import','large-catalog','large-overview','large-select']},
    {id:'research', title:'研究工具', hint:'问答 · 翻译 · 邮件分析', open:true, routes:['chat','translation'], links:['/mail-research']},
    {id:'delivery', title:'任务与成果', hint:'处理进度 · 成果导出', open:false, routes:['tasks','exports']},
    {id:'system', title:'系统管理', hint:'文件保护 · 系统设置', open:false, routes:['settings'], links:['/protection']},
  ];
  const labels = {source:'资料导入',physical:'原始目录',analysis:'智能目录',overview:'情报概览',
    'large-import':'大包导入','large-catalog':'原始与智能目录','large-overview':'情报概览',
    'large-select':'选择深度摘要',chat:'资料问答',translation:'全文翻译',tasks:'任务进度',exports:'成果导出',settings:'系统设置'};
  function initialize() {
    const nav=document.querySelector('#primaryNav .side-nav');
    if(!nav || nav.classList.contains('organized-nav')) return;
    let saved={};
    try {const v=JSON.parse(localStorage.getItem(KEY)||'{}');if(v && typeof v==='object' && !Array.isArray(v))saved=v;}catch(_){}
    let compact=false;
    const sections=new Map();
    const persist=()=>{try{localStorage.setItem(KEY,JSON.stringify(saved));}catch(_){}};
    const isRail=()=>matchMedia('(min-width:761px)').matches && document.body.classList.contains('nav-collapsed');
    const oldGroups=[...nav.querySelectorAll(':scope > .nav-group')];
    const home=nav.querySelector('[data-route="dashboard"]');
    nav.classList.add('organized-nav');nav.setAttribute('aria-label','按业务分类的功能导航');
    if(home){const landing=document.createElement('div');landing.className='organized-home';nav.prepend(landing);landing.append(home);home.setAttribute('aria-label','工作台');}
    function makeSection(group){
      const section=document.createElement('details');section.className='organized-group';section.dataset.navCategory=group.id;
      const summary=document.createElement('summary');summary.className='organized-heading';summary.tabIndex=0;
      const mark=document.createElement('span');mark.className='organized-category-icon';mark.setAttribute('aria-hidden','true');
      const sample=nav.querySelector('[data-route="'+group.routes[0]+'"] .nav-icon');
      if(sample)mark.append(sample.cloneNode(true));else mark.textContent='◇';
      const text=document.createElement('span');text.className='organized-heading-text';
      const title=document.createElement('span');title.className='organized-title';title.textContent=group.title;
      const hint=document.createElement('span');hint.className='organized-hint';hint.textContent=group.hint;text.append(title,hint);
      const count=document.createElement('span');count.className='organized-count';count.setAttribute('aria-hidden','true');
      const chevron=document.createElement('span');chevron.className='organized-chevron';chevron.setAttribute('aria-hidden','true');
      summary.append(mark,text,count,chevron);
      const body=document.createElement('div');body.className='organized-items';body.id='organized-items-'+group.id;
      summary.setAttribute('aria-controls',body.id);section.append(summary,body);
      section.open=typeof saved[group.id]==='boolean'?saved[group.id]:group.open;
      section.addEventListener('toggle',()=>{summary.setAttribute('aria-expanded',String(section.open));if(!compact){saved[group.id]=section.open;persist();}});
      summary.setAttribute('aria-expanded',String(section.open));
      nav.append(section);sections.set(group.id,{section,summary,body,group});return sections.get(group.id);
    }
    groups.forEach(makeSection);
    const linkedPath=item=>{
      const href=item.getAttribute('href');if(!href)return '';
      try{const u=new URL(href,location.href);return u.origin===location.origin?(u.pathname.endsWith('/')?u.pathname.slice(0,-1):u.pathname):'';}catch(_){return '';}
    };
    function organize(){
      for(const item of nav.querySelectorAll('.nav-item')){
        if(item===home)continue;
        const route=item.dataset.route;
        const group=groups.find(g=>g.routes.includes(route)||(g.links||[]).includes(linkedPath(item)));
        const target=group?sections.get(group.id):(sections.get('other')||makeSection({id:'other',title:'其他工具',hint:'扩展功能入口',open:true,routes:[]}));
        if(item.parentElement!==target.body)target.body.append(item);
        if(!item.dataset.organizedLabel){
          if(!item.querySelector('.nav-icon')){
            const icon=document.createElement('span');icon.className='nav-icon';icon.setAttribute('aria-hidden','true');
            const existing=target.summary.querySelector('svg');
            if(existing)icon.append(existing.cloneNode(true));else icon.textContent='◇';
            const text=document.createElement('span');
            [...item.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).forEach(n=>text.append(n));
            item.prepend(icon);item.append(text);
          }
          const text=[...item.children].find(el=>el.tagName==='SPAN'&&!el.classList.contains('nav-icon'));
          if(labels[route]&&text)text.textContent=labels[route];
          item.setAttribute('aria-label',target.group.title+' · '+item.textContent.trim());
          item.title=item.getAttribute('aria-label');item.dataset.organizedLabel='1';
        }
      }
      for(const {body,group,summary} of sections.values()){
        const items=[...body.children];
        const keys=group.id==='system'?['/protection','settings']:[...group.routes,...(group.links||[])];
        const rank=item=>{const n=keys.indexOf(item.dataset.route||linkedPath(item));return n<0?keys.length:n;};
        [...items].sort((a,b)=>rank(a)-rank(b)).forEach((item,i)=>{if(body.children[i]!==item)body.insertBefore(item,body.children[i]||null);});
        summary.querySelector('.organized-count').textContent=String(items.length);
      }
      oldGroups.forEach(group=>{if(![...group.children].some(el=>!el.classList.contains('nav-label')))group.remove();});
    }
    function syncRoute(){
      const route=document.body.dataset.route||window.SJFXShell?.route||'dashboard';
      const current=({packages:'source','large-result':'large-overview',evidence:'chat'})[route]||route;
      for(const {section,body} of sections.values()){
        const selected=[...body.querySelectorAll('[data-route]')].find(item=>item.dataset.route===current);
        section.classList.toggle('has-current',Boolean(selected));
        if(selected&&!section.open)section.open=true;
      }
      for(const item of nav.querySelectorAll('[data-route]')){
        const active=item.dataset.route===current;item.classList.toggle('active',active);
        if(active)item.setAttribute('aria-current','page');else item.removeAttribute('aria-current');
      }
    }
    function syncRail(){
      const next=isRail();if(compact===next)return;compact=next;
      for(const {section,group,body} of sections.values()){
        section.open=compact||(typeof saved[group.id]==='boolean'?saved[group.id]:group.open);
        if(!compact&&body.contains(document.activeElement))section.open=true;
      }
      if(!compact)syncRoute();
      nav.querySelectorAll('.nav-item').forEach(item=>{item.title=item.getAttribute('aria-label')||item.textContent.trim();});
    }
    organize();syncRoute();syncRail();
    // Preserve late extension entrances, including conditional protection links.
    new MutationObserver(records=>{
      if(records.some(r=>[...r.addedNodes].some(n=>n.nodeType===1&&(n.matches('.nav-item')||n.querySelector('.nav-item'))))){
        organize();syncRoute();if(compact)sections.forEach(s=>{s.section.open=true;});
      }
    }).observe(nav,{childList:true,subtree:true});
    new MutationObserver(records=>{
      if(records.some(r=>r.attributeName==='class'))syncRail();
      if(records.some(r=>r.attributeName==='data-route'))syncRoute();
    }).observe(document.body,{attributes:true,attributeFilter:['class','data-route']});
    window.addEventListener('sjfx-shell-state',syncRoute);
    window.addEventListener('resize',syncRail,{passive:true});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initialize);else initialize();
})();
