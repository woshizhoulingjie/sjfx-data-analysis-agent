'use strict';
// Run browser code without WebCrypto: the normal LAN HTTP deployment case.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const hashing = fs.readFileSync(path.join(root, 'static/upload-sha256.js'), 'utf8');
const source = fs.readFileSync(path.join(root, 'sjfx_multiuser/upload_routes.py'), 'utf8');
const upload = fs.readFileSync(path.join(root, 'static/upload.js'), 'utf8');
assert.match(source, /<script src="\/static\/upload\.js[^"\n]*" data-chunk-bytes="__CHUNK__" defer><\/script>/);
assert.doesNotMatch(source, /<script>/, 'the strict CSP upload page must contain no inline executable script');

function context(extra = {}) {
  const ctx = vm.createContext({ ArrayBuffer, Uint8Array, Uint32Array, DataView,
    URLSearchParams, AbortController, setTimeout, clearTimeout, ...extra });
  vm.runInContext(hashing, ctx);
  return ctx;
}

async function digestVectors() {
  for (const native of [false, true]) {
    const ctx = context({ crypto: native ? crypto.webcrypto : {} });
    for (const size of [0,1,3,55,56,63,64,65,119,120,127,128,129,1024,4*1024*1024]) {
      const content = crypto.randomBytes(size);
      const expected = crypto.createHash('sha256').update(content).digest('hex');
      assert.equal(await ctx.SJFXUploadSha256(content), expected, `SHA256 native=${native}, bytes=${size}`);
      // A view into a larger array must hash exactly the selected bytes.
      const framed = Buffer.concat([Buffer.from([1,2]), content, Buffer.from([3,4])]);
      assert.equal(await ctx.SJFXUploadSha256(framed.subarray(2,2+size)), expected);
    }
    assert.equal(await ctx.SJFXUploadSha256(Buffer.from('abc')), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    assert.equal(await ctx.SJFXUploadSha256(Buffer.from('中文样本')), crypto.createHash('sha256').update('中文样本').digest('hex'));
  }
}

async function uploadOnHttp(storageUnavailable=false, pauseFirst=false) {
  const bytes = Buffer.from([0,255,2,128,9,10,11]);
  const received = [];
  const file = { name: '数据.bin', size: bytes.length, lastModified: 1,
    slice(start,end) { const part=bytes.subarray(start,end); return {async arrayBuffer(){ return part.buffer.slice(part.byteOffset,part.byteOffset+part.length); }}; }};
  const elements = Object.fromEntries(['go','stop','ready','openImport','files','pkg','log','stat','bar'].map(id => [id, { style:{}, disabled:false, textContent:'', hidden:true, value:'', scrollHeight:0 }]));
  elements.files.files = [file];
  const storage = new Map();
  let sent=0,beginCount=0,pausedOnce=false;
  const destination = '/private/assembled/a/session_1';
  const fetch = async (url, options={}) => {
    let result;
    if (url === '/api/uploads' && options.method==='POST') {
      beginCount++;
      result={session_id:'session_1'};
    } else if (url === '/api/uploads/session_1') {
      result={max_chunk_bytes:4,file_count:1,total_bytes:bytes.length,files:[{rel_path:file.name,size:bytes.length,state:'uploading',received_bytes:sent,chunks_done:received.length}]};
    } else if (url.startsWith('/api/uploads/session_1/chunks/0?')) {
      const query=new URLSearchParams(url.split('?')[1]);
      const body=Buffer.from(options.body);
      assert.equal(query.get('sha256'),crypto.createHash('sha256').update(body).digest('hex'));
      assert.equal(Number(query.get('chunk_index')),received.length);
      received.push(body);sent+=body.length;
      if (pauseFirst && !pausedOnce) { pausedOnce=true;elements.stop.onclick(); }
      result={chunks_done:received.length,file_complete:sent===bytes.length};
    } else if (url === '/api/uploads/session_1/finalize') {
      assert.deepEqual(Buffer.concat(received),bytes);
      result={file_count:1,dest_dir:destination};
    } else { throw new Error('Unexpected browser request: '+url); }
    return {ok:true,status:200,async json(){ return result; }};
  };
  const ctx=context({ crypto:{}, document:{currentScript:{dataset:{chunkBytes:'4'}},getElementById:id=>elements[id]},fetch,
    sessionStorage:{getItem:key=>{if(storageUnavailable)throw new Error('Storage denied');return storage.get(key)||null;},
      setItem:(key,value)=>{if(storageUnavailable)throw new Error('Storage denied');storage.set(key,value);},
      removeItem:key=>{if(storageUnavailable)throw new Error('Storage denied');storage.delete(key);}} });
  vm.runInContext(upload,ctx);
  await elements.go.onclick();
  if (pauseFirst) {
    assert.equal(sent,4,'pause keeps a confirmed prefix');
    assert.equal(elements.ready.hidden,true);
    await elements.go.onclick();
  }
  assert.equal(sent,bytes.length,'LAN HTTP upload must reach finalize');
  assert.equal(elements.stat.textContent,'完成');
  assert.equal(elements.ready.hidden,false);
  assert.equal(new URL(elements.openImport.href,'http://example.test').searchParams.get('upload_path'),destination);
  assert.equal(storage.size,0,'a finished upload clears resume state');
  assert.equal(beginCount,1,'resuming within the page keeps the original session');
}

function uploadedImportHandoff() {
  const script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
  const helper=script.slice(script.indexOf('function prepareUploadedImport()'),script.indexOf('async function restoreWorkspace()'));
  for (const uploaded of ['/private/assembled/a/资料','C:\\data\\assembled\\a\\session_1']) {
    let stage='',focused=false,route='',replaced='',detached=0;
    const input={value:'',focus(){ focused=true; }};
    const window={location:{search:'?unrelated=keep&upload_path='+encodeURIComponent(uploaded),pathname:'/',hash:'#packages'},
      history:{replaceState(_state,_title,url){ replaced=url; }},SJFXShell:{activate(value){route=value;}}};
    const ctx=context({window,$:id=>id==='rootPath'?input:null,
      detachUiFromCurrentWork(){detached++;},setPackageStage(value){stage=value;}});
    vm.runInContext(helper,ctx);
    assert.equal(ctx.prepareUploadedImport(),true);
    assert.equal(input.value,uploaded);assert.equal(stage,'import');assert.equal(route,'packages');
    assert.ok(focused);assert.equal(detached,1);assert.equal(replaced,'/?unrelated=keep#packages');
    window.location.search='';assert.equal(ctx.prepareUploadedImport(),false);
  }
}

function uploadedImportRealShellHandoff() {
  const app=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
  const helper=app.slice(app.indexOf('function prepareUploadedImport()'),app.indexOf('async function restoreWorkspace()'));
  const packageStage=app.slice(app.indexOf('function setPackageStage(stage)'),app.indexOf('async function openSelectionPanel'));
  const large=fs.readFileSync(path.join(root,'static/large-package.js'),'utf8');
  const largeStage=large.slice(large.indexOf('  function setStage(stage)'),large.indexOf('  function render(status, workflow)'));
  const shell=fs.readFileSync(path.join(root,'static/product-shell.js'),'utf8');
  function exercise(source) {
    function element(id) {
      const classes=new Set(['hidden']);
      return {id,value:'',hidden:false,focused:false,dataset:{},
        focus(){this.focused=true;},setAttribute(name){if(name==='hidden')this.hidden=true;},
        style:{removeProperty(){}},classList:{contains:name=>classes.has(name),
          toggle(name,hidden){if(hidden)classes.add(name);else classes.delete(name);}}};
    }
    const elements=Object.fromEntries(['rootPath','dataSourcePanel','newImportCard','largePackagePanel'].map(id=>[id,element(id)]));
    const ctx=context({document:{getElementById:id=>elements[id]||null,
      querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){},
      body:{dataset:{}}},$:id=>elements[id]||null,detachUiFromCurrentWork(){},
      localStorage:{getItem(){return ''; }},sessionStorage:{getItem(){return ''; }},
      location:{search:'?upload_path='+encodeURIComponent('/private/assembled/a/session_1'),pathname:'/',hash:''},
      history:{replaceState(){}},dispatchEvent(){},scrollTo(){},
      CustomEvent:class {constructor(name,options){this.type=name;this.detail=options.detail;}}});
    ctx.window=ctx;
    vm.runInContext(packageStage,ctx);
    vm.runInContext('(()=>{let currentStage="normal";'+largeStage+';window.SJFXLargePackage={setStage,activate(){}};})();',ctx);
    vm.runInContext(shell,ctx);
    vm.runInContext(source,ctx);
    assert.equal(ctx.prepareUploadedImport(),true);
    assert.equal(ctx.SJFXShell.route,'packages');
    assert.equal(elements.rootPath.value,'/private/assembled/a/session_1');
    return elements;
  }
  // This replay preserves the old ordering to prove the real shell side effect
  // is exercised: packages -> large normal -> setPackageStage('source').
  const oldOrder=helper.replace(/window\.SJFXShell\?\.activate\('packages'\);\s*setPackageStage\('import'\);/,
    "setPackageStage('import');\n  window.SJFXShell?.activate('packages');");
  const old=exercise(oldOrder);
  assert.equal(old.newImportCard.classList.contains('hidden'),true,'old ordering must reproduce the hidden import form');
  const fixed=exercise(helper);
  assert.equal(fixed.newImportCard.classList.contains('hidden'),false,'the real packages shell must leave import visible');
  assert.equal(fixed.dataSourcePanel.classList.contains('hidden'),true);
  assert.equal(fixed.rootPath.focused,true);
}

async function deniedStorageWorkspaceAuth() {
  const app=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
  const registry=app.slice(app.indexOf('function loadTaskRegistry()'),app.indexOf('function rememberJob('));
  const authSource=app.slice(app.indexOf('const SJFX_API_TOKEN_KEY'),app.indexOf("document.addEventListener('DOMContentLoaded'"));
  const deniedCalls=[];
  const denied=Object.fromEntries(['getItem','setItem','removeItem'].map(name=>[name,()=>{
    deniedCalls.push(name);throw new Error('Storage denied: '+name);
  }]));
  const overlays=[],requests=[];
  let returnUnauthorized=false;
  const ctx=context({localStorage:denied,sessionStorage:denied,state:{jobs:new Map()},
    fetch:async (url,options)=>{requests.push({url,options});return {status:returnUnauthorized?401:200};},
    document:{getElementById(){return {};},body:{appendChild(overlay){overlays.push(overlay);}},
      createElement(){
        const listeners={};
        const input={value:'',focus(){},addEventListener(name,fn){listeners[name]=fn;}};
        return {input,listeners,removed:false,setAttribute(){},
          querySelector(){return input;},addEventListener(name,fn){listeners[name]=fn;},
          remove(){this.removed=true;}};
      }}});
  ctx.window=ctx;
  vm.runInContext("const TASK_REGISTRY_KEY='synthetic-task-registry';"+registry+authSource,ctx);
  assert.doesNotThrow(()=>ctx.loadTaskRegistry(),'denied get/remove must not stop workspace initialization');
  assert.ok(deniedCalls.includes('removeItem'));
  const auth=ctx.SJFXAuth;
  assert.equal((await auth.request('/api/status')).status,200,'existing cookie session must still reach fetch');
  assert.equal(requests.length,1);
  assert.equal(requests[0].options.headers['X-SJFX-Token'],undefined);
  assert.equal(overlays.length,0,'a valid cookie session does not require a token dialog');
  const entered=auth.ensureToken({force:true});
  assert.equal(overlays.length,1);
  overlays[0].input.value=' synthetic-memory-token ';
  overlays[0].listeners.keydown({key:'Enter'});
  assert.equal(await entered,'synthetic-memory-token','denied setItem must still resolve the submitted token');
  assert.ok(overlays[0].removed);
  assert.ok(deniedCalls.includes('setItem'));
  assert.equal(auth.hasToken(),true);
  assert.equal(await auth.ensureToken(),'synthetic-memory-token');
  assert.equal(overlays.length,1,'same-page reuse must not open another dialog');
  await auth.request('/api/scan',{headers:{'Content-Type':'application/json'}});
  assert.equal(requests.at(-1).options.headers['X-SJFX-Token'],'synthetic-memory-token');
  auth.clearToken();
  assert.equal(auth.hasToken(),false);
  await auth.request('/api/status');
  assert.equal(requests.at(-1).options.headers['X-SJFX-Token'],undefined,'clearToken must also clear memory');
  // A 401 follows the real request wrapper's retry/dialog path, not a mocked
  // ensureToken. The replacement stays usable despite all storage calls failing.
  returnUnauthorized=true;
  const retried=auth.request('/api/protected');
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(overlays.length,2);
  overlays[1].input.value='synthetic-retry-token';
  returnUnauthorized=false;
  overlays[1].listeners.keydown({key:'Enter'});
  assert.equal((await retried).status,200);
  assert.equal(requests.at(-1).options.headers['X-SJFX-Token'],'synthetic-retry-token');
  auth.clearToken();
  assert.equal(auth.hasToken(),false);
}

(async()=>{ await digestVectors();await uploadOnHttp();await uploadOnHttp(true);await uploadOnHttp(true,true);uploadedImportHandoff();uploadedImportRealShellHandoff();await deniedStorageWorkspaceAuth();console.log('PASS: native/fallback SHA vectors, binary HTTP upload, denied storage/resume, real shell import stage, denied-storage registry and cookie/token auth'); })().catch(error=>{console.error(error);process.exitCode=1;});
