// 部署页面前端逻辑 / Deploy page frontend logic

// 步骤 → 进度条索引映射（5步：Clone / Build / Deploy / 验证 / 完成）
// Step-to-bar-index map: Clone=0 Build=1 Deploy=2 Health=3 Done=4
const _STEP_IDX = {
  clone:0, backup:0,
  pre_build:1, build:1, post_build:1,
  pre_deploy:2, compose_up:2, post_deploy:2,
  health:3
};
const _BAR_LABELS = ['Clone','Build','Deploy','验证','完成'];

function updateStepsBar(id, step, phase) {
  const bar = document.getElementById('steps-' + id);
  if (!bar) return;
  const items = bar.querySelectorAll('.step-item');
  const idx = (step in _STEP_IDX) ? _STEP_IDX[step] : -1;
  items.forEach((el, i) => {
    const dot = el.querySelector('.step-dot');
    el.className = 'step-item';
    if (phase === 'done') {
      el.classList.add('done'); dot.textContent = '✓';
    } else if (phase === 'error') {
      if (i < idx)      { el.classList.add('done');    dot.textContent = '✓'; }
      else if (i === idx){ el.classList.add('error');  dot.textContent = '✗'; }
      else               { el.classList.add('pending'); dot.textContent = ''; }
    } else if (idx < 0) {
      el.classList.add('pending'); dot.textContent = '';
    } else {
      if (i < idx)      { el.classList.add('done');   dot.textContent = '✓'; }
      else if (i === idx){ el.classList.add('active'); dot.textContent = ''; }
      else               { el.classList.add('pending'); dot.textContent = ''; }
    }
  });
}

// 相对时间显示 / Relative time display
function relTime(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60)    return d + '秒前';
  if (d < 3600)  return Math.floor(d/60) + '分钟前';
  if (d < 86400) return Math.floor(d/3600) + '小时前';
  return Math.floor(d/86400) + '天前';
}
function updateTimes() {
  document.querySelectorAll('.rt[data-ts]').forEach(el => el.textContent = relTime(+el.dataset.ts));
}
updateTimes();
setInterval(updateTimes, 5000);

// 添加工程弹窗 / Add project modal
function openAddModal() {
  document.getElementById('add-modal').style.display = '';
  setTimeout(() => { const f = document.getElementById('add-modal').querySelector('input'); if(f) f.focus(); }, 50);
}
function closeAddModal() {
  document.getElementById('add-modal').style.display = 'none';
}

// 工程选择 / Project selection
function selectProject(id) {
  document.querySelectorAll('.proj-item').forEach(el => el.classList.remove('selected'));
  const item = document.getElementById('item-' + id);
  if (item) item.classList.add('selected');
  document.querySelectorAll('.proj-detail').forEach(el => el.style.display = 'none');
  const detail = document.getElementById('proj-' + id);
  if (detail) detail.style.display = '';
  const emp = document.getElementById('detail-empty');
  if (emp) emp.style.display = 'none';
}

// 提交添加表单 / Submit add form
async function submitAddForm(event) {
  event.preventDefault();
  const fd = new FormData(event.target);
  const data = Object.fromEntries(fd.entries());
  data.self_service = fd.has('self_service');
  const r = await fetch('/deploy/projects', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  });
  if (r.ok) {
    location.reload();
  } else {
    const e = await r.json();
    alert('添加失败: ' + (e.detail || '未知错误'));
  }
}

// 删除工程 / Delete project
async function deleteProject(id) {
  if (!confirm('确认删除此工程？')) return;
  const r = await fetch('/deploy/projects/' + id, {method: 'DELETE'});
  if (r.ok) {
    const item = document.getElementById('item-' + id);
    if (item) item.remove();
    const detail = document.getElementById('proj-' + id);
    if (detail) detail.remove();
    const list = document.getElementById('proj-list');
    if (list && !list.querySelector('.proj-item')) {
      list.innerHTML = '<div class="sidebar-empty">还没有工程</div>';
    }
    document.querySelectorAll('.proj-detail').forEach(el => el.style.display = 'none');
    const emp = document.getElementById('detail-empty');
    if (emp) emp.style.display = '';
  }
}

// 触发部署 / Trigger deploy
async function triggerDeploy(id, btn) {
  if (!confirm('确认触发部署？\ngit clone → build → docker compose up')) return;
  btn.disabled = true;
  btn.textContent = '部署中…';
  const rb = document.getElementById('rollback-btn-' + id);
  if (rb) rb.disabled = true;
  const logbox = document.getElementById('logbox-' + id);
  if (logbox) logbox.textContent = '';
  const r = await fetch('/deploy/projects/' + id + '/trigger', {method: 'POST'});
  if (r.status === 409) {
    alert('该工程已有部署任务在执行中');
    btn.disabled = false;
    btn.textContent = '部署';
    if (rb) rb.disabled = false;
    return;
  }
  connectSSE(id);
}

// 触发回滚 / Trigger rollback
async function triggerRollback(id, btn) {
  if (!confirm('确认回滚？将恢复上次成功部署的镜像版本。')) return;
  const depBtn = document.getElementById('deploy-btn-' + id);
  if (depBtn) depBtn.disabled = true;
  btn.disabled = true;
  btn.textContent = '回滚中…';
  const logbox = document.getElementById('logbox-' + id);
  if (logbox) logbox.textContent = '';
  const r = await fetch('/deploy/projects/' + id + '/rollback', {method: 'POST'});
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    alert('回滚失败: ' + (e.detail || r.status));
    btn.disabled = false;
    btn.textContent = '回滚';
    if (depBtn) { depBtn.disabled = false; depBtn.textContent = '部署'; }
    return;
  }
  connectSSE(id);
}

// SSE 实时日志 / SSE real-time log streaming
const _sse = {};
const _watchTimers = {};
function connectSSE(id) {
  if (_sse[id]) { _sse[id].close(); delete _sse[id]; }
  const es = new EventSource('/deploy/projects/' + id + '/stream');
  _sse[id] = es;
  const logbox = document.getElementById('logbox-' + id);
  es.onmessage = e => {
    if (e.data === '__done__') {
      es.close(); delete _sse[id];
      pollStatus(id);
      return;
    }
    if (e.data.startsWith('__step:')) {
      const step = e.data.slice(7, -2);
      updateStepsBar(id, step, 'running');
      return;
    }
    if (logbox) {
      logbox.textContent += (logbox.textContent ? '\n' : '') + e.data;
      logbox.scrollTop = logbox.scrollHeight;
    }
  };
  es.onerror = async () => {
    es.close(); delete _sse[id];
    try {
      const d = await pollStatus(id);
      const busy = d && ['pulling','building','restarting'].includes(d.phase);
      if (busy) {
        setTimeout(() => connectSSE(id), 1500);
      }
    } catch(e) {}
  };
}

// 状态映射 / Status mapping
const _phaseLabels = {idle:'IDLE',pulling:'拉取中…',building:'构建中…',restarting:'重启中…',done:'完成',error:'失败'};
const _phaseColors = {idle:'#94a3b8',pulling:'#f59e0b',building:'#3b82f6',restarting:'#8b5cf6',done:'#22c55e',error:'#dc2626'};
function _dockerColor(s) {
  if (!s || s === '—') return '#94a3b8';
  if (s.startsWith('Up')) return '#22c55e';
  if (s.startsWith('Exited')) return '#64748b';
  if (s.startsWith('Restarting')) return '#f59e0b';
  return '#94a3b8';
}

// 保存全局配置 / Save global setting
async function saveSetting(event, key) {
  event.preventDefault();
  const form = event.target;
  const fd = new FormData(form);
  const value = fd.get('value') || '';
  const isPassword = form.querySelector('input[type="password"]') !== null;
  if (isPassword && value === '') return;
  const body = new FormData();
  body.append('key', key);
  body.append('value', value);
  const r = await fetch('/deploy/config/update', {method: 'POST', body});
  if (r.ok) {
    const saved = form.querySelector('.setting-saved');
    if (saved) { saved.style.display = 'inline'; setTimeout(() => saved.style.display = 'none', 2000); }
  }
}

// 工程类型切换 / Project type toggle
function onProjectTypeChange(sel) {
  const form = sel.closest('form');
  const isFe = sel.value === 'frontend';
  const isJava = sel.value === 'java';
  form.querySelectorAll('[data-docker-only]').forEach(el => el.style.display = isFe ? 'none' : '');
  form.querySelectorAll('[data-frontend-only]').forEach(el => el.style.display = isFe ? '' : 'none');
  form.querySelectorAll('[data-java-only]').forEach(el => el.style.display = isJava ? '' : 'none');
}

// 编辑工程 / Edit project
function editProject(id) {
  const card = document.getElementById('proj-' + id);
  const p = JSON.parse(card.dataset.project);
  document.getElementById('edit-id').value = p.id;
  document.getElementById('edit-name').value = p.name || '';
  document.getElementById('edit-repo-url').value = p.repo_url || '';
  document.getElementById('edit-github-token').value = '';
  document.getElementById('edit-service-name').value = p.service_name || '';
  document.getElementById('edit-image-name').value = p.image_name || '';
  document.getElementById('edit-compose-file').value = p.compose_file || '';
  document.getElementById('edit-self-service').checked = !!p.self_service;
  const ptSel = document.getElementById('edit-project-type');
  ptSel.value = p.project_type || '';
  document.getElementById('edit-build-command').value = p.build_command || '';
  document.getElementById('edit-app-dir').value = p.app_dir || '';
  document.getElementById('edit-dist-dir').value = p.dist_dir || '';
  document.getElementById('edit-nginx-container').value = p.nginx_container || '';
  document.getElementById('edit-deploy-target-dir').value = p.deploy_target_dir || '';
  document.getElementById('edit-node-version').value = p.node_version || '';
  document.getElementById('edit-java-version').value = p.java_version || '';
  onProjectTypeChange(ptSel);
  document.getElementById('edit-modal').style.display = '';
}

function closeEditModal() {
  document.getElementById('edit-modal').style.display = 'none';
}

// 提交编辑表单 / Submit edit form
async function submitEditForm(event) {
  event.preventDefault();
  const fd = new FormData(event.target);
  const id = fd.get('_id');
  const data = Object.fromEntries(fd.entries());
  delete data._id;
  data.self_service = fd.has('self_service');
  const r = await fetch('/deploy/projects/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  });
  if (r.ok) {
    location.reload();
  } else {
    const e = await r.json();
    alert('保存失败: ' + (e.detail || '未知错误'));
  }
}

// 轮询工程状态 / Poll project status
async function pollStatus(id) {
  try {
    const r = await fetch('/deploy/projects/' + id + '/status');
    const d = await r.json();
    const detail = document.getElementById('proj-' + id);
    const p = detail ? JSON.parse(detail.dataset.project) : {};
    const isFrontend = p.project_type === 'frontend';
    const badge  = document.getElementById('badge-' + id);
    const btn    = document.getElementById('deploy-btn-' + id);
    const rbBtn  = document.getElementById('rollback-btn-' + id);
    const isStat = document.getElementById('item-status-' + id);
    const busy   = ['pulling','building','restarting'].includes(d.phase);
    let c, label;
    if (busy) {
      c = _phaseColors[d.phase]; label = _phaseLabels[d.phase];
      if (rbBtn) rbBtn.disabled = true;
    } else if (isFrontend) {
      if (d.phase === 'done' || d.last_phase === 'done')      { c = '#22c55e'; label = '已发布'; }
      else if (d.phase === 'error' || d.last_phase === 'error'){ c = '#dc2626'; label = '发布失败'; }
      else                                                     { c = '#94a3b8'; label = '待发布'; }
      if (btn) { btn.disabled = false; btn.textContent = '部署'; }
      if (rbBtn) { rbBtn.disabled = false; rbBtn.textContent = '回滚'; }
    } else {
      const s = d.docker_status || '—';
      c = _dockerColor(s); label = s;
      if (btn) { btn.disabled = false; btn.textContent = '部署'; }
      if (rbBtn) { rbBtn.disabled = false; rbBtn.textContent = '回滚'; }
    }
    if (badge) { badge.textContent = label; badge.style.color = c; badge.style.background = c + '18'; }
    if (isStat) { isStat.textContent = label; isStat.style.color = c; isStat.style.background = c + '18'; }
    if (d.step !== undefined) updateStepsBar(id, d.step, d.phase);
    if (d.finished_at) {
      const ts = Math.floor(new Date(d.finished_at).getTime() / 1000);
      const ok  = d.phase === 'done';
      const action = isFrontend ? '发布' : '部署';
      const ldEl = document.getElementById('last-deploy-' + id);
      if (ldEl) {
        const rc2 = ok ? '#22c55e' : '#dc2626';
        const rv  = ok ? `✓\u00a0${action}成功` : `✗\u00a0${action}失败`;
        ldEl.innerHTML = `<span class="rt" data-ts="${ts}">—</span>\u00a0<span style="color:${rc2};font-weight:600">${rv}</span>`;
      }
      const metaEl = document.getElementById('item-meta-' + id);
      if (metaEl) {
        const sym = ok ? '✓' : '✗';
        metaEl.innerHTML = `<span class="rt" data-ts="${ts}">—</span> ${sym}`;
      }
      updateTimes();
    }
    return d;
  } catch(e) {}
  return null;
}

// 初始化工程状态 / Initialize project status
async function initProject(id) {
  const logbox = document.getElementById('logbox-' + id);
  if (logbox && !logbox.textContent.trim()) {
    try {
      const r = await fetch('/deploy/projects/' + id + '/logs');
      const text = await r.text();
      if (text.trim()) {
        logbox.textContent = text.trimEnd();
        logbox.scrollTop = logbox.scrollHeight;
      }
    } catch(e) {}
  }
  await pollStatus(id);
  if (_watchTimers[id]) clearInterval(_watchTimers[id]);
  _watchTimers[id] = setInterval(async () => {
    const d = await pollStatus(id);
    const busy = d && ['pulling','building','restarting'].includes(d.phase);
    if (busy && !_sse[id]) connectSSE(id);
  }, 3000);
}
