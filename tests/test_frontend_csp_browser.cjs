'use strict';
// Production upload HTML and response CSP, synthetic HTTP responses only.
const assert=require('node:assert/strict'),crypto=require('node:crypto'),fs=require('node:fs');
const path=require('node:path'),{spawnSync}=require('node:child_process');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const python=process.env.SJFX_TEST_PYTHON || 'python';
const fixture=spawnSync(python,['-X','utf8','-c',`
import json
from sjfx_multiuser.upload_routes import upload_page_html
from web_compat import _SecurityHeadersMiddleware
messages=[]
async def downstream(scope, receive, send):
    await send({'type':'http.response.start','status':200,'headers':[]})
async def send(message):
    messages.append(message)
# This ASGI response never suspends; no platform event loop or thread is needed.
coroutine=_SecurityHeadersMiddleware(downstream)({'type':'http','path':'/upload'},None,send)
try:
    coroutine.send(None)
except StopIteration:
    pass
headers={key.decode():value.decode() for key,value in messages[0]['headers']}
print(json.dumps({'html':upload_page_html(4),'csp':headers['content-security-policy']}))
`],{cwd:root,encoding:'utf8',timeout:15000});
assert.equal(fixture.status,0,fixture.stderr);
const {html,csp}=JSON.parse(fixture.stdout);
assert(csp.includes("script-src 'self';"));
assert(csp.includes("img-src 'self' data: blob:;"));
assert(!csp.includes("script-src 'self' 'unsafe-inline'"));
const input=Buffer.from([0,255,2,128,9,10,11]),received=[];
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2xkAAAAASUVORK5CYII=','base64');
let began=0,finalized=0,fixtureError=null;
async function syntheticResponse(route){
  const req=route.request(),u=new URL(req.url());
  const send=(body,type='application/json',status=200)=>route.fulfill({status,headers:{'Content-Type':type,'Content-Security-Policy':csp},body:typeof body==='string'||Buffer.isBuffer(body)?body:JSON.stringify(body)});
  try{
    if(u.pathname==='/upload')return send(html,'text/html; charset=utf-8');
    if(u.pathname==='/mail-image')return send('<!doctype html><meta charset="utf-8"><div id="mail-status"></div><select id="mail-package"><option value="p" selected>合成邮件包</option></select><div id="mail-foundation-reader"></div><script src="/static/mail-ui.js" defer></script><script src="/static/mail-foundation.js" defer></script>','text/html; charset=utf-8');
    if(u.pathname.startsWith('/static/'))return send(fs.readFileSync(path.join(root,u.pathname.slice(1))),'application/javascript');
    if(u.pathname==='/api/mail/messages/synthetic/content')return send({ok:true,content:{raw_available:true,parts:[{mime_path:'2',filename_raw:'synthetic.png',is_inline_resource:true,content_type:'image/png',byte_size:png.length}]}});
    if(u.pathname==='/api/mail/messages/synthetic/parts/2/preview-image')return send(png,'image/png');
    if(u.pathname==='/api/uploads'&&req.method()==='POST'){began++;return send({session_id:'synthetic-session'});}
    if(u.pathname==='/api/uploads/synthetic-session')return send({max_chunk_bytes:4,file_count:1,total_bytes:input.length,files:[{rel_path:'合成.bin',size:input.length,state:'uploading',received_bytes:Buffer.concat(received).length,chunks_done:received.length}]});
    if(u.pathname==='/api/uploads/synthetic-session/chunks/0'){
      const body=req.postDataBuffer();
      assert.equal(Number(u.searchParams.get('chunk_index')),received.length);
      assert.equal(u.searchParams.get('sha256'),crypto.createHash('sha256').update(body).digest('hex'));
      received.push(body);return send({chunks_done:received.length,file_complete:Buffer.concat(received).length===input.length});
    }
    if(u.pathname==='/api/uploads/synthetic-session/finalize'){assert.deepEqual(Buffer.concat(received),input);finalized++;return send({file_count:1,dest_dir:'/synthetic/assembled/owner/session'});}
    return send({},'application/json',404);
  }catch(error){fixtureError=error;return send({error:error.message},'application/json',500);}
}
(async()=>{
  let browser;
  try{
    browser=await chromium.launch({headless:true});const page=await browser.newPage();const failures=[];
    await page.route('http://synthetic.test/**',syntheticResponse);
    page.on('pageerror',error=>failures.push(error.message));
    page.on('console',message=>{if(/Content Security Policy|violates.*directive/i.test(message.text()))failures.push(message.text());});
    const origin='http://synthetic.test';
    await page.goto(origin+'/upload');
    await page.locator('#files').setInputFiles({name:'合成.bin',mimeType:'application/octet-stream',buffer:input});
    await page.locator('#go').click();await page.waitForFunction(()=>document.getElementById('stat').textContent==='完成');
    assert.equal(began,1);assert.equal(finalized,1);assert.equal(received.length,2);
    assert.equal(await page.locator('#ready').isVisible(),true);
    await page.goto(origin+'/mail-image');
    await page.evaluate(()=>window.SJFXMailFoundation.renderMessage('synthetic',{},{package_id:'p'}));
    await page.getByRole('button',{name:'预览内嵌图片',exact:true}).click();
    await page.waitForFunction(()=>{const image=document.querySelector('#mail-foundation-reader img');return image&&image.complete&&image.naturalWidth===1;});
    assert((await page.locator('#mail-foundation-reader img').getAttribute('src')).startsWith('blob:'));
    assert.deepEqual(failures,[],'upload and inline image preview must cause no CSP violation or script error');
    if(fixtureError)throw fixtureError;
    console.log('PASS: real Chromium executes external upload scripts under production CSP and loads a mail Blob image');
  }finally{if(browser)await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
