function $(i){return document.getElementById(i);}
function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function say(el, text, cls){ var e=$(el); e.className='msg '+(cls||''); e.textContent=text||''; }

async function api(path, opts){
  var r = await fetch(path, Object.assign({headers:{'Accept':'application/json'}}, opts||{}));
  if (r.status === 401) { location.href='/login'; throw new Error('未登录'); }
  var d = null; try { d = await r.json(); } catch(e) { d = null; }
  return {status: r.status, data: d};
}

var ME = null, USERS = [];

async function loadMe(){
  var r = await api('/api/auth/me');
  if (!r.data || !r.data.ok) { location.href='/login'; return false; }
  ME = r.data.user;
  $('who').textContent = ME.display_name || ME.username;
  if (ME.role !== 'admin') {
    document.querySelector('.wrap').innerHTML =
      '<div class="panel"><h2>需要管理员权限</h2><p class="sub">当前账号不是管理员。</p>' +
      '<a class="btn" href="/">返回工作台</a></div>';
    return false;
  }
  if (ME.must_change_pw) { location.href='/change-password'; return false; }
  return true;
}

async function loadUsers(){
  var r = await api('/api/admin/users');
  if (!r.data || !r.data.ok) { say('userMsg', '加载失败：' + (r.data&&r.data.error||r.status), 'err'); return; }
  USERS = r.data.users || [];
  var rows = USERS.map(function(u){
    var isMe = u.user_id === ME.user_id;
    var tag = u.role === 'admin' ? '<span class="tag admin">管理员</span>' : '<span class="tag">普通用户</span>';
    var st  = u.status === 'active' ? '<span class="tag on">正常</span>' : '<span class="tag off">已停用</span>';
    var acts = [];
    acts.push('<button class="btn" data-act="materials" data-id="'+u.user_id+'">查看资料</button>');
    acts.push('<button class="btn" data-act="reset" data-id="'+u.user_id+'" data-name="'+esc(u.username)+'">重置密码</button>');
    if (isMe) {
      acts.push('<span class="hint">当前账号</span>');
    } else if (u.status === 'active') {
      acts.push('<button class="btn danger" data-act="disable" data-id="'+u.user_id+'" data-name="'+esc(u.username)+'">停用</button>');
    } else {
      acts.push('<button class="btn" data-act="enable" data-id="'+u.user_id+'">启用</button>');
    }
    return '<tr><td><b>'+esc(u.username)+'</b></td><td>'+esc(u.display_name)+'</td><td>'+tag+'</td><td>'+st+'</td>' +
      '<td class="mono">'+esc(u.owner_id)+'</td><td class="hint">'+(u.data_stats ?
        '普通包 '+u.data_stats.ordinary_packages+' / 大包 '+u.data_stats.large_packages+'<br>问答 '+u.data_stats.conversations+' / 翻译 '+u.data_stats.translations : '统计暂不可用')+'</td>'+
      '<td class="hint">'+esc(u.last_login_utc||'从未登录')+'</td>' +
      '<td><div class="row">'+acts.join(' ')+'</div></td></tr>';
  }).join('');
  $('userRows').innerHTML = rows || '<tr><td colspan="8" class="hint">没有用户</td></tr>';
  if (r.data.data_error) say('userMsg', r.data.data_error, 'err');
  else say('userMsg', '', '');
  window.SJFXAdminMaterials?.setUsers(USERS, ME);

  var active = USERS.filter(function(u){return u.status==='active';}).length;
  var admins = USERS.filter(function(u){return u.role==='admin' && u.status==='active';}).length;
  $('stats').innerHTML =
    '<div class="stat"><span>账号总数</span><b>'+USERS.length+'</b></div>' +
    '<div class="stat"><span>活跃账号</span><b>'+active+'</b></div>' +
    '<div class="stat"><span>活跃管理员</span><b>'+admins+'</b></div>' +
    '<div class="stat"><span>数据空间</span><b>'+new Set(USERS.map(function(u){return u.owner_id;})).size+'</b></div>';
  [['ordinary_packages','普通数据包'],['large_packages','大数据包'],['conversations','问答会话'],['translations','翻译记录']].forEach(function(entry){
    var total = USERS.every(function(u){return !!u.data_stats;}) ? USERS.reduce(function(sum,u){return sum+(u.data_stats[entry[0]]||0);},0) : '—';
    $('stats').insertAdjacentHTML('beforeend','<div class="stat"><span>'+entry[1]+'</span><b>'+total+'</b></div>');
  });

  var sel = $('viewSelect');
  var cur = sel.value;
  sel.innerHTML = '<option value="">全部用户（管理员默认）</option>' +
    USERS.map(function(u){
      return '<option value="'+esc(u.owner_id)+'">'+esc(u.display_name||u.username)+'（'+esc(u.username)+'）</option>';
    }).join('');
  sel.value = cur;
}

async function loadView(){
  var r = await api('/api/admin/view-as');
  if (!r.data || !r.data.ok) return;
  var v = r.data.viewing_owner;
  var target = USERS.find(function(u){return u.owner_id===v;});
  $('curView').textContent = !v || v===ME.owner_id ? '全部用户' : (target?.display_name || target?.username || v);
  $('viewSelect').value = v || '';
}

async function applyView(){
  var v = $('viewSelect').value || null;
  var r = await api('/api/admin/view-as', {method:'POST', headers:{'Content-Type':'application/json'},
                                           body: JSON.stringify({owner_id: v})});
  if (r.status === 200 && r.data && r.data.ok) {
    say('viewMsg', '已切换到：' + (v || '全部用户'), 'ok');
    await loadView();
  } else {
    say('viewMsg', '切换失败：' + ((r.data&&r.data.error) || r.status), 'err');
  }
}

async function resetPassword(id, name){
  var pw = prompt('为「' + name + '」设置新密码（至少 8 位）。\n该用户下次登录会被要求改密。');
  if (!pw) return;
  var r = await api('/api/admin/users/' + encodeURIComponent(id) + '/password',
                    {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({password: pw})});
  if (r.status === 200 && r.data && r.data.ok) say('userMsg', '已重置「'+name+'」的密码，并作废其现有会话', 'ok');
  else say('userMsg', '重置失败：' + ((r.data&&r.data.error) || r.status), 'err');
}

async function setStatus(id, status, name){
  if (status === 'disabled' && !confirm('确定停用「' + name + '」？\n该用户将立即无法登录，已有数据保留。')) return;
  var r = await api('/api/admin/users/' + encodeURIComponent(id),
                    {method:'DELETE', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({status: status})});
  if (r.status === 200 && r.data && r.data.ok) {
    say('userMsg', (status==='disabled'?'已停用 ':'已启用 ') + name, 'ok');
    await loadUsers();
  } else {
    say('userMsg', '操作失败：' + ((r.data&&r.data.error) || r.status), 'err');
  }
}

async function createUser(){
  var name = prompt('新账号名（3-32 位，字母开头）：');
  if (!name) return;
  var pw = prompt('为「' + name + '」设置初始密码（至少 8 位）：');
  if (!pw) return;
  var role = confirm('设为管理员吗？\n确定=管理员，取消=普通用户') ? 'admin' : 'user';
  var r = await api('/api/admin/users', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({username:name, password:pw, role:role, must_change_pw:true})});
  if (r.status === 200 && r.data && r.data.ok) {
    say('userMsg', '已创建 ' + name + '（' + role + '），归属空间 ' + r.data.owner_id, 'ok');
    await loadUsers();
  } else {
    say('userMsg', '创建失败：' + ((r.data&&r.data.error) || r.status), 'err');
  }
}

document.addEventListener('click', function(e){
  var b = e.target.closest('[data-act]');
  if (!b) return;
  var id = b.dataset.id, name = b.dataset.name, act = b.dataset.act;
  if (act === 'reset') resetPassword(id, name);
  else if (act === 'disable') setStatus(id, 'disabled', name);
  else if (act === 'enable') setStatus(id, 'active', name);
  else if (act === 'materials') window.SJFXAdminMaterials?.open(id);
});

$('applyView').addEventListener('click', applyView);
$('refreshBtn').addEventListener('click', function(){ loadUsers(); loadView(); });
$('newUserBtn').addEventListener('click', createUser);
$('logoutBtn').addEventListener('click', async function(){
  await fetch('/api/auth/logout', {method:'POST'}); location.href='/login';
});

(async function(){
  if (!await loadMe()) return;
  await loadUsers();
  await loadView();
})();
