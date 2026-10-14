
const CHUNK = Number(document.currentScript?.dataset.chunkBytes || 0);
if (!CHUNK) throw new Error("上传分片大小未配置");
const STORAGE_KEY = 'sjfx.upload.resume.v2';
let stopFlag = false;
let memoryResume = null;
const $ = id => document.getElementById(id);
function log(s){ const el=$('log'); el.textContent += s + "\n"; el.scrollTop = el.scrollHeight; }
function say(s){ $('stat').textContent = s; }
function setBar(p){ $('bar').style.width = Math.max(0, Math.min(100, p)).toFixed(1) + '%'; }

function relPath(f){
  return (f.webkitRelativePath || f.name).replace(/^\/+/, '');
}

async function sha256Hex(buf){
  return await SJFXUploadSha256(buf);
}

async function api(path, opts){
  const r = await fetch(path, Object.assign({credentials:'same-origin'}, opts||{}));
  let j = null;
  try { j = await r.json(); } catch(e){}
  if (!r.ok) {
    const error = new Error((j && (j.error || j.detail)) || ('HTTP ' + r.status));
    error.status = r.status;
    throw error;
  }
  return j;
}

function fingerprint(files, packageName){
  const stable = files.map(f => ({
    rel_path: f.rel_path, size: f.size, last_modified: f.last_modified
  })).sort((a,b) => a.rel_path.localeCompare(b.rel_path));
  return JSON.stringify({package_name: packageName, files: stable});
}

function readResume(){
  try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null') || memoryResume; }
  catch(e) { return memoryResume; }
}

function saveResume(sessionId, key){
  memoryResume = {session_id: sessionId, fingerprint: key};
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(memoryResume)); }
  catch(e) { log('当前浏览器无法保存续传记录，请保持本页打开；本页内仍可暂停和继续。'); }
}

function clearResume(){
  memoryResume = null;
  try { sessionStorage.removeItem(STORAGE_KEY); } catch(e) {}
}

function matchesRemote(remoteFiles, localFiles){
  if (!Array.isArray(remoteFiles) || remoteFiles.length !== localFiles.length) return false;
  const local = new Map(localFiles.map(f => [f.rel_path, f.size]));
  if (local.size !== localFiles.length) return false;
  return remoteFiles.every(f => local.has(f.rel_path) && local.get(f.rel_path) === f.size);
}

function receivedBytes(status){
  return (status.files || []).reduce((sum, f) => sum + (Number(f.received_bytes) || 0), 0);
}

$('go').onclick = async () => {
  const picked = [...$('files').files];
  if (!picked.length) { log('先选择原来的文件，再点开始或继续上传'); return; }
  $('go').disabled = true;
  $('ready').hidden = true;
  $('stop').disabled = false;
  stopFlag = false;
  try {
    const packageName = $('pkg').value.trim() || null;
    const files = picked.map(f => ({
      file: f, rel_path: relPath(f), size: f.size,
      last_modified: Number(f.lastModified) || 0
    }));
    const key = fingerprint(files, packageName);
    let saved = readResume();
    let status = null;
    let sessionId = null;

    if (saved && saved.fingerprint === key && saved.session_id) {
      try {
        status = await api('/api/uploads/' + encodeURIComponent(saved.session_id));
        if (status.max_chunk_bytes !== CHUNK || !matchesRemote(status.files, files)) {
          log('原续传会话与当前文件不匹配，将另建上传会话；旧暂存数据会按服务端期限清理。');
          status = null;
          clearResume();
        } else {
          sessionId = saved.session_id;
          log('恢复会话 ' + sessionId + '，从服务器确认的进度继续。');
        }
      } catch(e) {
        if (e.status !== 404) throw e;
        log('原续传会话已过期，将重新上传。');
        clearResume();
        status = null;
      }
    } else if (saved) {
      log('所选文件或数据包名称已变化，将另建上传会话；旧暂存数据会按服务端期限清理。');
      clearResume();
    }

    if (!status) {
      log('建立上传会话…');
      const created = await api('/api/uploads', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          package_name: packageName,
          files: files.map(f => ({rel_path: f.rel_path, size: f.size}))
        })
      });
      sessionId = created.session_id;
      saveResume(sessionId, key);
      status = await api('/api/uploads/' + encodeURIComponent(sessionId));
      if (!matchesRemote(status.files, files)) throw new Error('服务器清单与所选文件不一致，已停止上传');
      log('会话 ' + sessionId + '：' + status.file_count + ' 个文件，共 ' + status.total_bytes + ' 字节');
    }

    const serverFiles = new Map(status.files.map((f, index) => [f.rel_path, Object.assign({index}, f)]));
    let doneBytes = receivedBytes(status);
    const totalBytes = Number(status.total_bytes) || 0;
    setBar(totalBytes ? doneBytes / totalBytes * 100 : 100);

    for (const item of files) {
      let entry = serverFiles.get(item.rel_path);
      if (!entry) throw new Error('服务器缺少文件：' + item.rel_path);
      if (entry.state === 'complete') {
        log('✓ 已完成，跳过 ' + item.file.name);
        continue;
      }
      let offset = Number(entry.received_bytes) || 0;
      let chunkIndex = Number(entry.chunks_done) || 0;
      while (offset < item.size) {
        if (stopFlag) {
          log('已暂停。重新选择相同文件并点击“开始上传”即可从服务器确认的断点继续。');
          return;
        }
        const end = Math.min(offset + CHUNK, item.size);
        const buf = await item.file.slice(offset, end).arrayBuffer();
        const digest = await sha256Hex(buf);
        let guard = 0;
        for (;;) {
          try {
            const ack = await api('/api/uploads/' + encodeURIComponent(sessionId) +
              '/chunks/' + entry.index + '?chunk_index=' + chunkIndex + '&sha256=' + digest, {
                method:'PUT', headers:{'Content-Type':'application/octet-stream'}, body:buf
              });
            const nextOffset = Math.min(item.size, offset + buf.byteLength);
            doneBytes += nextOffset - offset;
            offset = nextOffset;
            chunkIndex = Number(ack.chunks_done) || chunkIndex + 1;
            entry.received_bytes = offset;
            entry.chunks_done = chunkIndex;
            entry.state = ack.file_complete ? 'complete' : entry.state;
            break;
          } catch(e) {
            if (++guard > 4) throw e;
            log('  第 ' + chunkIndex + ' 片请求未确认，检查服务器进度后重试：' + e.message);
            await new Promise(r => setTimeout(r, 800 * guard));
            const latest = await api('/api/uploads/' + encodeURIComponent(sessionId));
            const fresh = latest.files && latest.files[entry.index];
            if (!fresh || fresh.rel_path !== item.rel_path) throw new Error('无法确认服务器分片状态');
            const confirmed = Number(fresh.chunks_done) || 0;
            if (confirmed > chunkIndex) {
              const advanced = (Number(fresh.received_bytes) || 0) - offset;
              doneBytes += Math.max(0, advanced);
              offset = Number(fresh.received_bytes) || 0;
              chunkIndex = confirmed;
              entry = Object.assign({index: entry.index}, fresh);
              break;
            }
            if (confirmed < chunkIndex) throw new Error('服务器进度早于当前分片，请刷新状态后重试');
          }
        }
        const pct = totalBytes ? doneBytes / totalBytes * 100 : 100;
        setBar(pct);
        say(item.file.name + '  ' + pct.toFixed(1) + '%  共 ' + doneBytes + ' / ' + totalBytes + ' 字节');
      }
      log('✓ ' + item.file.name);
    }

    if (stopFlag) {
      log('已暂停。重新选择相同文件并点击“开始上传”即可继续。');
      return;
    }
    $('stop').disabled = true;
    log('全部分片已到服务器，正在组装…');
    const result = await api('/api/uploads/' + encodeURIComponent(sessionId) + '/finalize', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
    });
    log('✓ 组装完成：' + result.file_count + ' 个文件 → ' + result.dest_dir);
    $('openImport').href = '/?upload_path=' + encodeURIComponent(result.dest_dir);
    $('ready').hidden = false;
    say('完成');
    setBar(100);
    clearResume();
  } catch(e) {
    log('✗ ' + e.message);
  } finally {
    $('go').disabled = false;
    $('stop').disabled = true;
  }
};
$('stop').onclick = () => { stopFlag = true; $('stop').disabled = true; };


