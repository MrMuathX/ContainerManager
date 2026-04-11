/* ── ContainerManager Frontend ────────────────────────────────────────────── */

const API = '';   // same origin
let containers = [];
let selectedId = null;
let currentUpdateXhr = null;
let selectedDetail = null;
let filter = 'all';
let searchQuery = '';
let sortField = 'name';
let sortOrder = 'asc';
let logWs = null;
let termWs = null;
let xtermInstance = null;
let fitAddon = null;
let statsInterval = null;
let listInterval = null;
let selectedCheckboxIds = new Set();
let dashboardViewMode = localStorage.getItem('dashViewMode') || 'grid';

const debounce = (fn, delay) => {
  let timeoutId;
  return (...args) => {
    if (timeoutId) clearTimeout(timeoutId);
    timeoutId = setTimeout(() => fn(...args), delay);
  };
};

/* ─── Toast (stack height shifts floating AI chat above toasts) ─────────────── */
function syncFloatingChatForToasts() {
  const tc = document.getElementById('toastContainer');
  if (!tc) return;
  const h = tc.offsetHeight;
  const extra = h > 0 ? h + 12 : 0;
  document.documentElement.style.setProperty('--toast-stack-offset', `${extra}px`);
}

function toast(msg, type = 'info', duration = 3500) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const content = typeof msg === 'object' ? JSON.stringify(msg, null, 2) : msg;
  el.textContent = content;
  document.getElementById('toastContainer').appendChild(el);
  requestAnimationFrame(syncFloatingChatForToasts);
  setTimeout(() => {
    el.style.animation = 'slideOut .25s ease forwards';
    setTimeout(() => {
      el.remove();
      syncFloatingChatForToasts();
    }, 280);
  }, duration);
}

/* ─── Confirm dialog ───────────────────────────────────────────────────────── */
function confirm(title, msg) {
  return new Promise(resolve => {
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMsg').textContent = msg;
    document.getElementById('confirmModal').style.display = 'flex';
    const yes = document.getElementById('btnConfirmYes');
    const no = document.getElementById('btnConfirmNo');
    const close = () => { document.getElementById('confirmModal').style.display = 'none'; };
    yes.onclick = () => { close(); resolve(true); };
    no.onclick = () => { close(); resolve(false); };
  });
}

/* ─── API helpers ──────────────────────────────────────────────────────────── */
async function api(path, method = 'GET', body) {
  const opts = { method, credentials: 'include', headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(API + path, opts);
    if (r.status === 401) { window.location.href = '/login'; throw new Error('Unauthorized'); }
    if (!r.ok) {
      let detail = r.statusText;
      try { const err = await r.json(); detail = err.detail || JSON.stringify(err); } catch (e) { }
      throw new Error(`API Error ${r.status}: ${detail}`);
    }
    return r;
  } catch (e) {
    console.error(`Fetch error for ${path}:`, e);
    throw e;
  }
}

function animateValue(elementId) {
  const el = document.querySelector(`#${elementId} .stat-val`);
  if (!el) return;
  el.style.transition = 'none';
  el.style.color = 'var(--green-bright)';
  el.style.transform = 'scale(1.1)';
  setTimeout(() => {
    el.style.transition = 'color 0.8s, transform 0.8s';
    el.style.color = '';
    el.style.transform = '';
  }, 50);
}

function updateStat(elementId, value) {
  const el = document.querySelector(`#${elementId} .stat-val`);
  if (el) { el.textContent = value; animateValue(elementId); }
}

/* ─── System stats ─────────────────────────────────────────────────────────── */
async function loadStats() {
  try {
    const r = await api('/api/system');
    const stats = await r.json();
    updateStat('statCpu', stats.cpu_percent + '%');
    updateStat('statMem', `${stats.mem_used_gb}/${stats.mem_total_gb} GB`);
    updateStat('statDisk', stats.disk_percent + '%');
    updateStat('statContainers', `${stats.docker_containers_running}/${stats.docker_containers_total}`);
  } catch (e) { console.warn('System stats load failed:', e.message); }

  try {
    const mq_r = await api('/api/system/mqtt-status');
    const mq = await mq_r.json();
    const el = document.getElementById('mqttStatus');
    el.textContent = mq.available ? 'Connected' : 'Error';
    el.className = mq.available ? 'stat-val status-up' : 'stat-val status-down';
  } catch (e) {
    const el = document.getElementById('mqttStatus');
    if (el) { el.textContent = 'Disconnected'; el.className = 'stat-val status-down'; }
  }
}

/* ─── Container list ───────────────────────────────────────────────────────── */
async function loadContainers() {
  try {
    const r = await api('/api/containers?all=true');
    containers = await r.json();
    renderList();
  } catch (e) {
    console.error('Failed to load containers.', e);
    toast(`Failed to load containers: ${e.message}`, 'error');
  }
}

function dotClass(status) {
  if (status === 'running') return 'running';
  if (status === 'paused') return 'paused';
  return 'stopped';
}

/* Compare helper for sorting */
function sortValue(c, field) {
  switch (field) {
    case 'name': return (c.name || '').toLowerCase();
    case 'status': return c.status || '';
    case 'image': return (c.image || '').toLowerCase();
    case 'created': return c.created || '';
    case 'uptime': return c.uptime || '';
    default: return (c.name || '').toLowerCase();
  }
}

let collapsedFolders = new Set(JSON.parse(localStorage.getItem('collapsedFolders') || '[]'));

function toggleFolder(name) {
  if (collapsedFolders.has(name)) collapsedFolders.delete(name);
  else collapsedFolders.add(name);
  localStorage.setItem('collapsedFolders', JSON.stringify(Array.from(collapsedFolders)));
  renderList();
}

function getGroup(c) {
  // Use compose project if available
  if (c.labels && c.labels['com.docker.compose.project']) {
    return c.labels['com.docker.compose.project'];
  }
  // Split by common delimiters and take first component or "Other"
  const parts = c.name.split(/[_-]/);
  if (parts.length > 1) {
    // If it's something like app-web-1, group by app-web? 
    // Let's try to be smart: if more than 2 parts, use first two
    if (parts.length > 2) return parts[0] + ' / ' + parts[1];
    return parts[0];
  }
  return 'Other';
}

function renderList() {
  const query = searchQuery.toLowerCase();
  let items = containers.slice();
  if (filter === 'running') items = items.filter(c => c.status === 'running');
  if (filter === 'stopped') items = items.filter(c => c.status !== 'running');
  if (query) items = items.filter(c => c.name.toLowerCase().includes(query) || c.image.toLowerCase().includes(query));

  // Sort
  items.sort((a, b) => {
    const va = sortValue(a, sortField);
    const vb = sortValue(b, sortField);
    const cmp = va < vb ? -1 : va > vb ? 1 : 0;
    return sortOrder === 'asc' ? cmp : -cmp;
  });

  // Grouping
  const groups = {};
  items.forEach(c => {
    const g = getGroup(c);
    if (!groups[g]) groups[g] = [];
    groups[g].push(c);
  });

  const list = document.getElementById('containerList');
  list.innerHTML = '';

  // Dashboard special item
  const dashItem = document.createElement('div');
  dashItem.className = 'c-item' + (!selectedId ? ' active' : '');
  dashItem.innerHTML = `
    <div class="c-item-dot" style="background:var(--blue)"></div>
    <div class="c-item-info">
      <div class="c-item-name">Main Dashboard</div>
      <div class="c-item-image">Cluster Overview</div>
    </div>
  `;
  dashItem.onclick = () => showDashboard();
  list.appendChild(dashItem);

  // Sorting groups alphabetically
  const sortedGroupNames = Object.keys(groups).sort((a, b) => {
    if (a === 'Other') return 1;
    if (b === 'Other') return -1;
    return a.localeCompare(b);
  });

  sortedGroupNames.forEach(gName => {
    const folder = document.createElement('div');
    folder.className = 'folder-item';
    const isCollapsed = collapsedFolders.has(gName);
    
    // Split nested groups by ' / ' if you want a visual tree look
    const displayLabel = gName;
    
    folder.innerHTML = `
      <div class="folder-header ${isCollapsed ? 'collapsed' : ''}" onclick="toggleFolder('${esc(gName)}')">
        <svg class="chevron" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:4px"><polyline points="6 9 12 15 18 9"></polyline></svg>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
        <span style="flex:1; margin-left:8px">${esc(displayLabel)} <small style="opacity:0.6">(${groups[gName].length})</small></span>
      </div>
      <div class="folder-content ${isCollapsed ? 'hidden' : ''}"></div>
    `;
    
    const content = folder.querySelector('.folder-content');
    groups[gName].forEach(c => {
      const isActive = c.id === selectedId;
      const isChecked = selectedCheckboxIds.has(c.id);
      const div = document.createElement('div');
      div.className = 'c-item' + (isActive ? ' active' : '');
      div.dataset.id = c.id;
      
      const failedIcon = (c.exit_code !== 0 && c.status !== 'running') ? 
        `<span class="status-down" title="Failed (Exit Code ${c.exit_code})" style="margin-left:4px">⚠</span>` : '';

      div.innerHTML = `
        <input type="checkbox" class="c-item-check" data-cid="${c.id}"${isChecked ? ' checked' : ''} onclick="event.stopPropagation(); toggleCheckbox('${c.id}', this.checked)">
        <div class="c-item-dot ${dotClass(c.status)}"></div>
        <div class="c-item-info">
          <div class="c-item-name">${esc(c.name)}${failedIcon}</div>
          <div class="c-item-image">${esc(c.image)}</div>
        </div>`;
      div.onclick = () => selectContainer(c.id);
      content.appendChild(div);
    });
    list.appendChild(folder);
  });

  updateBackupBtnLabel();
  updateSelectAllCheckboxState();
  if (!selectedId) showDashboardViewOnly();
}

function showDashboard() {
  selectedId = null;
  selectedDetail = null;
  renderList();
  showDashboardViewOnly();
}

function showDashboardViewOnly() {
  document.getElementById('dashboardView').style.display = 'block';
  document.getElementById('detailView').style.display = 'none';
  
  // Update view switcher active state
  document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
  if (dashboardViewMode === 'grid') document.getElementById('btnViewGrid')?.classList.add('active');
  else document.getElementById('btnViewList')?.classList.add('active');

  renderDashboard();
}

function toggleDashboardView(mode) {
  dashboardViewMode = mode;
  localStorage.setItem('dashViewMode', mode);
  showDashboardViewOnly();
}

function renderDashboard() {
  const container = document.getElementById('dashboardGrid');
  if (!container) return;
  
  // Set layout class
  container.className = dashboardViewMode === 'grid' ? 'dashboard-grid' : 'dashboard-list';
  
  if (containers.length === 0) {
    container.innerHTML = '<div class="no-data">No containers found.</div>';
    return;
  }

  const query = searchQuery.toLowerCase();
  let items = containers.slice();
  if (filter === 'running') items = items.filter(c => c.status === 'running');
  if (filter === 'stopped') items = items.filter(c => c.status !== 'running');
  if (query) items = items.filter(c => c.name.toLowerCase().includes(query) || c.image.toLowerCase().includes(query));

  if (dashboardViewMode === 'grid') {
    container.innerHTML = items.map(c => {
      const isRunning = c.status === 'running';
      const failedText = (c.exit_code !== 0 && !isRunning) ? ` (Exit: ${c.exit_code})` : '';
      const group = getGroup(c);
      
      return `
      <div class="dashboard-card" onclick="selectContainer('${c.id}')">
        <div class="d-card-header">
          <div class="d-card-title">
            <div class="d-card-name">${esc(c.name)}</div>
            <div class="d-card-image">${esc(c.image)}</div>
          </div>
          <div class="d-card-status">
            ${!isRunning ? `<div class="badge stopped">${c.status.toUpperCase()}${failedText}</div>` : ''}
            <div class="status-dot ${dotClass(c.status)}"></div>
          </div>
        </div>
        
        <div class="d-card-meta">
          ${group !== 'Other' ? `<span class="meta-tag">Group: ${esc(group)}</span>` : ''}
          ${c.labels && c.labels['com.docker.compose.project'] ? `<span class="meta-tag" title="Project">Project: ${esc(c.labels['com.docker.compose.project'])}</span>` : ''}
        </div>

        <div class="d-card-actions">
          <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'view-logs')" title="View Logs">📄 Logs</button>
          <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'view-terminal')" title="Terminal">⌨ Term</button>
          <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'start')" ${isRunning ? 'disabled' : ''}>▶ Start</button>
          <button class="btn btn-warning btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'stop')" ${!isRunning ? 'disabled' : ''}>⏹ Stop</button>
          <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'restart')" title="Restart Container">↺ Refresh</button>
        </div>
      </div>
      `;
    }).join('');
  } else {
    // Grouping for List Mode
    const groups = {};
    items.forEach(c => {
      const g = getGroup(c);
      if (!groups[g]) groups[g] = [];
      groups[g].push(c);
    });

    const sortedGroupNames = Object.keys(groups).sort((a, b) => {
      if (a === 'Other') return 1;
      if (b === 'Other') return -1;
      return a.localeCompare(b);
    });

    let html = '';
    sortedGroupNames.forEach(gName => {
      html += `
        <div class="dashboard-group-header">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
          ${esc(gName)} (${groups[gName].length})
        </div>
      `;
      html += groups[gName].map(c => {
        const isRunning = c.status === 'running';
        const failedText = (c.exit_code !== 0 && !isRunning) ? ` (Exit: ${c.exit_code})` : '';
        const group = getGroup(c);
        return `
        <div class="d-list-item" onclick="selectContainer('${c.id}')">
          <div class="d-list-info">
            <div class="status-dot ${dotClass(c.status)}"></div>
            <div class="d-list-name">${esc(c.name)}</div>
            <div class="d-list-image">${esc(c.image)}</div>
          </div>
          <div class="d-list-status">
            ${!isRunning ? `<div class="badge stopped">${c.status.toUpperCase()}${failedText}</div>` : ''}
          </div>
          <div class="d-list-actions">
            <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'view-logs')" title="View Logs">📄 Logs</button>
            <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'view-terminal')" title="Terminal">⌨ Term</button>
            <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'start')" ${isRunning ? 'disabled' : ''}>▶ Start</button>
            <button class="btn btn-warning btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'stop')" ${!isRunning ? 'disabled' : ''}>⏹ Stop</button>
            <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); quickAction('${c.id}', 'restart')" title="Restart">↺ Refresh</button>
          </div>
        </div>
        `;
      }).join('');
    });
    container.innerHTML = html;
  }
}

async function quickAction(id, action) {
  if (action === 'view-logs') {
    await selectContainer(id);
    switchTab('logs');
    return;
  }
  if (action === 'view-terminal') {
    await selectContainer(id);
    switchTab('terminal');
    return;
  }
  try {
    toast(`${action.charAt(0).toUpperCase() + action.slice(1)}ing container...`, 'info', 1500);
    await api(`/api/containers/${id}/${action}`, 'POST');
    await loadContainers(); 
    toast(`${action.charAt(0).toUpperCase() + action.slice(1)}ed successfully`, 'success');
  } catch (e) {
    toast(`${action} failed: ${e.message}`, 'error');
  }
}


function updateSelectAllCheckboxState() {
  const chk = document.getElementById('chkSelectAll');
  if (!chk) return;
  const query = searchQuery.toLowerCase();
  let items = containers.slice();
  if (filter === 'running') items = items.filter(c => c.state === 'running');
  if (filter === 'stopped') items = items.filter(c => c.state === 'stopped');
  if (query) items = items.filter(c => c.name.toLowerCase().includes(query) || c.image.toLowerCase().includes(query));

  if (items.length === 0) { chk.checked = false; chk.indeterminate = false; return; }
  const allSelected = items.every(c => selectedCheckboxIds.has(c.id));
  const someSelected = items.some(c => selectedCheckboxIds.has(c.id));
  chk.checked = allSelected;
  chk.indeterminate = someSelected && !allSelected;
}

function toggleSelectAll(checked) {
  const query = searchQuery.toLowerCase();
  let items = containers.slice();
  if (filter === 'running') items = items.filter(c => c.state === 'running');
  if (filter === 'stopped') items = items.filter(c => c.state === 'stopped');
  if (query) items = items.filter(c => c.name.toLowerCase().includes(query) || c.image.toLowerCase().includes(query));

  items.forEach(c => {
    if (checked) selectedCheckboxIds.add(c.id);
    else selectedCheckboxIds.delete(c.id);
  });
  renderList();
}

function toggleCheckbox(id, checked) {
  if (checked) selectedCheckboxIds.add(id);
  else selectedCheckboxIds.delete(id);
  updateBackupBtnLabel();
}

function updateBackupBtnLabel() {
  const btn = document.getElementById('btnBackupAll');
  if (!btn) return;
  const svg = btn.querySelector('svg') ? btn.querySelector('svg').outerHTML : '';
  if (selectedCheckboxIds.size > 0) {
    btn.innerHTML = `${svg} Backup Selected (${selectedCheckboxIds.size})`;
  } else {
    btn.innerHTML = `${svg} Backup All`;
  }
}

/* ─── Select container ─────────────────────────────────────────────────────── */
async function selectContainer(id) {
  selectedId = id;
  renderList();
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('detailView').style.display = 'flex';
  document.getElementById('detailView').style.flexDirection = 'column';

  // Reset tabs
  stopLogs();
  stopTerminal();

  await loadDetail(id);
  document.getElementById('aiChatHistory').innerHTML = '';
  switchTab('overview');
}

async function loadDetail(id) {
  try {
    const r = await api(`/api/containers/${id}`);
    selectedDetail = await r.json();
    renderDetail(selectedDetail);
    await loadMonitoringConfig(id);
  } catch (e) { toast(`Error loading details for ${id}: ${e.message}`, 'error'); }
}

async function loadMonitoringConfig(id) {
  try {
    const r = await api(`/api/containers/${id}/monitoring`);
    const conf = await r.json();
    document.getElementById('monEnabled').checked = conf.enabled;
    document.getElementById('monAutoRestart').checked = conf.auto_restart;
    document.getElementById('monAutoStartOnStop').checked = conf.auto_start_on_stop || false;
    document.getElementById('monLogs').checked = conf.monitor_logs;
    document.getElementById('monPatterns').value = conf.log_patterns.join(',');
  } catch (e) { console.error("Monitor load err", e); }
}

function renderDetail(d) {
  document.getElementById('detailName').textContent = d.name;
  const dot = document.getElementById('detailStatusDot');
  dot.className = 'status-dot ' + dotClass(d.status);
  const badge = document.getElementById('detailStatus');
  badge.textContent = d.status;
  badge.className = 'badge ' + (d.status === 'running' ? 'running' : d.status === 'paused' ? 'paused' : 'stopped');

  const running = d.state === 'running';
  document.getElementById('btnStart').style.display = running ? 'none' : '';
  document.getElementById('btnStop').style.display = running ? '' : 'none';
  document.getElementById('btnRestart').style.display = running ? '' : 'none';

  setRing('ringCpu', 'lblCpu', d.stats?.cpu_percent, '%');
  setRing('ringMem', 'lblMem', d.stats?.mem_percent, '%');
  document.getElementById('lblNetRx').textContent = d.stats ? `${d.stats.net_rx_mb} MB` : '—';
  document.getElementById('lblNetTx').textContent = d.stats ? `${d.stats.net_tx_mb} MB` : '—';

  const rows = [
    ['ID', d.short_id],
    ['Image', d.image],
    ['Status', d.status],
    ['Uptime', d.uptime || '—'],
    ['Created', d.created],
    ['Network', d.network_mode],
    ['Restart', d.restart_policy],
    d.command ? ['Command', d.command] : null,
  ].filter(Boolean);
  document.getElementById('infoTableBody').innerHTML = rows.map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join('');

  const portsList = document.getElementById('portsList');
  if (d.ports.length) {
    portsList.innerHTML = d.ports.map(p => `
      <div class="port-entry">
        <span class="port-host">${p.host_ip ? esc(p.host_ip) + ':' : ''}${esc(p.host_port || '—')}</span>
        <span class="port-arrow">→</span>
        <span class="port-container">${esc(p.container_port)}</span>
      </div>`).join('');
  } else {
    portsList.innerHTML = '<span class="no-data">No port bindings</span>';
  }

  const mountsList = document.getElementById('mountsList');
  if (d.mounts.length) {
    mountsList.innerHTML = d.mounts.map(m => `
      <div class="mount-entry">
        <div class="mount-type">${esc(m.type || 'bind')}</div>
        <div class="mount-source">${esc(m.source || '')}</div>
        <div class="mount-dest">→ ${esc(m.destination || '')}</div>
      </div>`).join('');
  } else {
    mountsList.innerHTML = '<span class="no-data">No mounts</span>';
  }

  const envList = document.getElementById('envList');
  if (d.env.length) {
    envList.innerHTML = d.env.map(e => {
      const idx = e.indexOf('=');
      const k = idx > -1 ? e.slice(0, idx) : e;
      const v = idx > -1 ? e.slice(idx + 1) : '';
      return `<div class="env-entry"><div class="env-key">${esc(k)}</div><div class="env-val">${esc(v)}</div></div>`;
    }).join('');
  } else {
    envList.innerHTML = '<span class="no-data">No environment variables</span>';
  }

  const labelsList = document.getElementById('labelsList');
  const labels = Object.entries(d.labels);
  if (labels.length) {
    labelsList.innerHTML = labels.map(([k, v]) =>
      `<div class="env-entry"><div class="env-key">${esc(k)}</div><div class="env-val">${esc(v)}</div></div>`).join('');
  } else {
    labelsList.innerHTML = '<span class="no-data">No labels</span>';
  }
}

function setRing(ringId, lblId, pct, unit) {
  const ring = document.getElementById(ringId);
  const lbl = document.getElementById(lblId);
  if (pct === undefined || pct === null) {
    ring.setAttribute('stroke-dasharray', `0 100`);
    lbl.textContent = '—';
    return;
  }
  const clamped = Math.min(100, Math.max(0, pct));
  ring.setAttribute('stroke-dasharray', `${clamped.toFixed(1)} ${(100 - clamped).toFixed(1)}`);
  lbl.textContent = `${pct.toFixed(0)}${unit}`;
}

/* ─── Tabs ─────────────────────────────────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = p.id === `tab-${name}` ? 'block' : 'none');

  if (name === 'logs') {
    startLogs();
  } else {
    stopLogs();
  }

  if (name !== 'terminal') {
    stopTerminal();
    const overlay = document.getElementById('terminalStartOverlay');
    const xcontainer = document.getElementById('xtermContainer');
    const reconnBtn = document.getElementById('btnReconnectTerm');
    if (overlay) overlay.style.display = 'flex';
    if (xcontainer) xcontainer.style.display = 'none';
    if (reconnBtn) reconnBtn.style.display = 'none';
  }
}

/* ─── Live logs ────────────────────────────────────────────────────────────── */
function startLogs() {
  if (!selectedId) return;
  stopLogs();
  const output = document.getElementById('logOutput');
  const status = document.getElementById('logsStatus');
  output.innerHTML = '';
  status.textContent = 'Connecting…';
  status.className = 'logs-status';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  logWs = new WebSocket(`${proto}://${location.host}/api/containers/${selectedId}/ws/logs`);
  logWs.onopen = () => { status.textContent = 'Live'; status.className = 'logs-status conn'; };
  logWs.onmessage = ({ data }) => {
    const line = document.createElement('span');
    line.className = 'log-line';
    const low = data.toLowerCase();
    if (low.includes('error') || low.includes('err ')) line.classList.add('log-error');
    else if (low.includes('warn')) line.classList.add('log-warn');
    line.textContent = data;
    output.appendChild(line);
    output.appendChild(document.createElement('br'));
    if (document.getElementById('chkAutoScroll').checked) output.scrollTop = output.scrollHeight;
  };
  logWs.onclose = () => { status.textContent = 'Disconnected'; status.className = 'logs-status disc'; };
  logWs.onerror = () => { status.textContent = 'Disconnected'; status.className = 'logs-status disc'; };
}

function exportLogsToCSV() {
  if (!selectedDetail) { toast('No container selected', 'error'); return; }
  const output = document.getElementById('logOutput');
  const lines = output.querySelectorAll('.log-line');
  if (lines.length === 0) { toast('No logs to export', 'info'); return; }
  let csvContent = "Timestamp,Message\n";
  lines.forEach(line => {
    const text = line.textContent || '';
    const match = text.match(/^(\d{4}-\d{2}-\d{2}T[\d:\.]+Z?[\+\-\d:]*)?\s*(.*)$/);
    let ts = "", msg = text;
    if (match && match[1]) { ts = match[1]; msg = match[2]; }
    msg = msg.replace(/"/g, '""');
    csvContent += `"${ts}","${msg}"\n`;
  });
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `${selectedDetail.name}_logs.csv`;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
  toast('Logs exported to CSV', 'success');
}

function stopLogs() { if (logWs) { logWs.close(); logWs = null; } }

/* ─── Terminal ─────────────────────────────────────────────────────────────── */
function startTerminal() {
  if (!selectedId) return;
  document.getElementById('terminalStartOverlay').style.display = 'none';
  document.getElementById('xtermContainer').style.display = 'block';
  document.getElementById('btnReconnectTerm').style.display = 'block';
  const container = document.getElementById('xtermContainer');
  if (!xtermInstance) {
    xtermInstance = new Terminal({
      theme: { background: '#000000', foreground: '#f0f9ff', cursor: '#0ea5e9' },
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
      fontSize: 13, cursorBlink: true,
    });
    fitAddon = new FitAddon.FitAddon();
    xtermInstance.loadAddon(fitAddon);
    xtermInstance.open(container);
    fitAddon.fit();
    window.addEventListener('resize', () => fitAddon.fit());
  }
  connectTerminal();
  setTimeout(() => fitAddon.fit(), 50);
}

function connectTerminal() {
  if (termWs) { termWs.close(); termWs = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  termWs = new WebSocket(`${proto}://${location.host}/api/containers/${selectedId}/ws/exec`);
  termWs.binaryType = 'arraybuffer';
  termWs.onopen = () => {
    if (xtermInstance) { xtermInstance.clear(); xtermInstance.writeln('\x1b[32mContainerManager Terminal — Connected\x1b[0m\r\n'); }
    xtermInstance.onData(data => { if (termWs && termWs.readyState === WebSocket.OPEN) termWs.send(new TextEncoder().encode(data)); });
  };
  termWs.onmessage = ({ data }) => {
    if (xtermInstance) {
      if (data instanceof ArrayBuffer) xtermInstance.write(new Uint8Array(data));
      else xtermInstance.write(data);
    }
  };
  termWs.onclose = () => { if (xtermInstance) xtermInstance.writeln('\r\n\x1b[31m— Connection closed —\x1b[0m'); };
  termWs.onerror = () => { if (xtermInstance) xtermInstance.writeln('\r\n\x1b[31m— WebSocket error —\x1b[0m'); };
}

function stopTerminal() { if (termWs) { termWs.close(); termWs = null; } }

/* ─── Ports Modal ──────────────────────────────────────────────────────────── */
async function loadPorts() {
  document.getElementById('portsModal').style.display = 'flex';
  const list = document.getElementById('allPortsList');
  list.innerHTML = '<div class="no-data" style="margin-top:1rem;">Loading...</div>';
  document.getElementById('portsCount').textContent = 'Loading...';
  try {
    const r = await api('/api/system/ports');
    const ports = await r.json();
    document.getElementById('portsCount').textContent = `${ports.length} Active`;
    if (ports.length === 0) {
      list.innerHTML = '<div class="no-data" style="margin-top:1rem;">No ports directly mapped by running containers.</div>';
      return;
    }
    list.innerHTML = ports.map(p => `
      <div class="port-entry" style="display:flex; align-items:center;">
        <span class="port-badge">${esc(p.port)}</span>
        <span class="p-cont">${esc(p.container_name)} <span style="color:var(--text-muted);font-size:11px;">(${esc(p.protocol)})</span></span>
        <span class="p-id">${esc(p.container_id)}</span>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = `<div class="no-data" style="margin-top:1rem;color:var(--red);">Error loading ports: ${e.message}</div>`;
  }
}

/* ─── Container actions ────────────────────────────────────────────────────── */
async function containerAction(action, method = 'POST', body) {
  if (!selectedId) return;
  try {
    const r = await api(`/api/containers/${selectedId}/${action}`, method, body);
    const data = await r.json();
    toast(data.message, data.success ? 'success' : 'error');
    if (data.success) { await loadContainers(); await loadDetail(selectedId); }
  } catch (e) { toast(String(e), 'error'); }
}

async function doRename() {
  if (!selectedId || !selectedDetail) return;
  const modal = document.getElementById('renameModal');
  const input = document.getElementById('renameInput');
  const btnConfirm = document.getElementById('btnConfirmRename');
  const btnCancel = document.getElementById('btnCancelRename');
  input.value = selectedDetail.name;
  modal.style.display = 'flex';
  setTimeout(() => { input.focus(); input.select(); }, 10);
  return new Promise(resolve => {
    const close = () => {
      modal.style.display = 'none';
      btnCancel.onclick = null; btnConfirm.onclick = null; input.onkeydown = null;
    };
    btnCancel.onclick = () => { close(); resolve(); };
    btnConfirm.onclick = async () => {
      const newName = input.value.trim();
      if (newName && newName !== selectedDetail.name) await containerAction('rename', 'POST', { new_name: newName });
      close(); resolve();
    };
    input.onkeydown = (e) => {
      if (e.key === 'Enter') btnConfirm.click();
      if (e.key === 'Escape') btnCancel.click();
    };
  });
}

async function updateContainer(id) {
  if (!id) return;
  const modal = document.getElementById('updateModal');
  const log = document.getElementById('updateProgress');
  const closeBtn = document.getElementById('btnCloseUpdate');
  const cancelBtn = document.getElementById('btnCancelUpdate');
  modal.style.display = 'flex';
  log.innerHTML = 'Connecting...';
  closeBtn.style.display = 'none';
  cancelBtn.style.display = 'block';
  const controller = new AbortController();
  cancelBtn.onclick = () => { controller.abort(); modal.style.display = 'none'; toast('Update cancelled', 'info'); };
  try {
    const response = await fetch(`/api/containers/${id}/pull`, { method: 'POST', signal: controller.signal, credentials: 'include' });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    log.innerHTML = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n').filter(l => l.trim());
      for (const line of lines) {
        try {
          const data = JSON.parse(line);
          const p = document.createElement('div');
          p.className = `log-line ${data.status}`;
          p.textContent = `[${data.status}] ${data.message}`;
          log.appendChild(p); log.scrollTop = log.scrollHeight;
          if (data.status === 'done') {
            closeBtn.style.display = 'block'; cancelBtn.style.display = 'none';
            await loadContainers(); if (selectedId) await loadDetail(selectedId);
          }
          if (data.status === 'error') { closeBtn.style.display = 'block'; cancelBtn.style.display = 'none'; }
        } catch (e) { console.warn("Error parsing NDJSON line", e); }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') return;
    const p = document.createElement('div');
    p.className = 'log-line error'; p.textContent = `Error: ${err.message}`;
    log.appendChild(p); closeBtn.style.display = 'block'; cancelBtn.style.display = 'none';
  }
}

async function doRemove() {
  if (!selectedId) return;
  const name = selectedDetail?.name || selectedId;
  const ok = await confirm('Uninstall Container', `Remove "${name}" and its container? This cannot be undone.`);
  if (!ok) return;
  const r = await api(`/api/containers/${selectedId}`, 'DELETE');
  const data = await r.json();
  toast(data.message, data.success ? 'success' : 'error');
  if (data.success) {
    selectedId = null; selectedDetail = null;
    document.getElementById('detailView').style.display = 'none';
    document.getElementById('emptyState').style.display = '';
    stopLogs(); stopTerminal();
    await loadContainers();
  }
}

/* ─── Excel Export ──────────────────────────────────────────────────────────── */
function exportListToExcel() {
  if (!containers || containers.length === 0) { toast('No containers to export', 'error'); return; }
  const data = containers.map(c => ({
    Name: c.name, Status: c.status, Image: c.image, Created: c.created,
    Uptime: c.uptime || 'N/A',
    Ports: c.ports.map(p => `${p.host_port}->${p.container_port}`).join(', ')
  }));
  const worksheet = XLSX.utils.json_to_sheet(data);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, "Containers");
  worksheet["!cols"] = Object.keys(data[0]).map(k => ({ wch: Math.max(k.length, 12) + 5 }));
  XLSX.writeFile(workbook, `containers_export_${new Date().toISOString().slice(0, 10)}.xlsx`);
  toast('Dashboard exported to Excel', 'success');
}

/* ─── Backup Logic ─────────────────────────────────────────────────────────── */
function downloadBackup(id) {
  if (!id) return;
  const link = document.createElement('a');
  link.href = `/api/containers/${id}/backup`;
  link.setAttribute('download', '');
  document.body.appendChild(link); link.click(); document.body.removeChild(link);
  toast('Starting backup download...', 'info');
}

async function downloadAllBackups() {
  if (selectedCheckboxIds.size > 0) {
    // Selective backup — download each individually and zip in browser
    toast(`Preparing backup for ${selectedCheckboxIds.size} container(s)...`, 'info');
    const zip = new JSZip();
    const ids = Array.from(selectedCheckboxIds);
    for (const id of ids) {
      try {
        const c = containers.find(x => x.id === id);
        const name = c ? c.name : id;
        const r = await fetch(`/api/containers/${id}/backup`, { credentials: 'include' });
        if (!r.ok) { toast(`Backup failed for ${name}`, 'error'); continue; }
        const buf = await r.arrayBuffer();
        zip.file(`${name}_backup.zip`, buf);
      } catch (e) { toast(`Error backing up ${id}: ${e.message}`, 'error'); }
    }
    const blob = await zip.generateAsync({ type: 'blob' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `selected_containers_backup.zip`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('Selected backup ready!', 'success');
  } else {
    const link = document.createElement('a');
    link.href = `/api/containers/all/backup`;
    link.setAttribute('download', '');
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
    toast('Preparing master backup ZIP...', 'info');
  }
}

/* ─── Helpers ──────────────────────────────────────────────────────────────── */
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ─── Registry & Image Management ───────────────────────────────────────────── */
let monacoEditor = null;

if (window.require) {
  require.config({ paths: { 'vs': 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.38.0/min/vs' } });
}

document.getElementById('btnRegistry').onclick = () => { document.getElementById('registryModal').style.display = 'flex'; };
document.getElementById('btnCloseRegistry').onclick = () => { document.getElementById('registryModal').style.display = 'none'; };

document.getElementById('btnSearchImage').onclick = async () => {
  const q = document.getElementById('imageSearchInput').value.trim();
  if (!q) return;
  const resultsContainer = document.getElementById('imageSearchResults');
  resultsContainer.innerHTML = '<div class="no-data">Searching...</div>';
  try {
    const r = await api(`/api/images/search?q=${encodeURIComponent(q)}`);
    const results = await r.json();
    if (results.length === 0) { resultsContainer.innerHTML = '<div class="no-data">No results found.</div>'; return; }
    window._openRunConfig = openRunConfig;
    resultsContainer.innerHTML = results.map(img => `
      <div class="port-entry" style="display:flex; justify-content:space-between; align-items:center;">
        <div>
          <div style="font-weight: 600; color: var(--text);">${esc(img.name)}</div>
          <div style="font-size: 12px; color: var(--text-muted);">${img.star_count} stars ${img.is_official ? '• Official' : ''}</div>
        </div>
        <button class="btn btn-sm btn-primary" onclick="window._openRunConfig('${esc(img.name)}')">Deploy</button>
      </div>
    `).join('');
  } catch (e) {
    resultsContainer.innerHTML = `<div class="no-data" style="color:var(--red);">Search failed: ${e.message}</div>`;
  }
};

// File Upload Drag and Drop
const uploadZone = document.getElementById('uploadZone');
const uploadInput = document.getElementById('uploadInput');
uploadZone.onclick = () => uploadInput.click();
uploadZone.ondragover = (e) => { e.preventDefault(); uploadZone.style.borderColor = 'var(--blue)'; uploadZone.style.background = 'rgba(14, 165, 233, 0.1)'; };
uploadZone.ondragleave = (e) => { e.preventDefault(); uploadZone.style.borderColor = ''; uploadZone.style.background = ''; };
uploadZone.ondrop = (e) => {
  e.preventDefault(); uploadZone.style.borderColor = ''; uploadZone.style.background = '';
  if (e.dataTransfer.files.length > 0) handleFileUpload(e.dataTransfer.files[0]);
};
uploadInput.onchange = (e) => { if (e.target.files.length > 0) handleFileUpload(e.target.files[0]); };

async function handleFileUpload(file) {
  const formData = new FormData();
  formData.append('file', file);
  toast(`Uploading ${file.name}...`, 'info');
  try {
    const r = await fetch('/api/images/upload', { method: 'POST', body: formData });
    const data = await r.json();
    if (r.ok) toast(data.message, 'success');
    else toast(data.detail || 'Upload failed', 'error');
  } catch (e) { toast(`Upload error: ${e.message}`, 'error'); }
}

// Monaco Run Config
function openRunConfig(imageName) {
  document.getElementById('registryModal').style.display = 'none';
  document.getElementById('runConfigModal').style.display = 'flex';
  const currentDeployConfig = { image: imageName || "nginx:latest", name: "", env: ["TZ=UTC"], ports: { "80/tcp": "8080" } };
  const code = JSON.stringify(currentDeployConfig, null, 2);
  if (!monacoEditor) {
    require(['vs/editor/editor.main'], function () {
      monacoEditor = monaco.editor.create(document.getElementById('monacoEditorContainer'), {
        value: code, language: 'json', theme: 'vs-dark',
        minimap: { enabled: false }, automaticLayout: true, fontSize: 14, fontFamily: "'JetBrains Mono', monospace"
      });
    });
  } else { monacoEditor.setValue(code); }
}

document.getElementById('btnCancelRunConfig').onclick = () => { document.getElementById('runConfigModal').style.display = 'none'; };
document.getElementById('btnConfirmRunConfig').onclick = async () => {
  if (!monacoEditor) return;
  const configText = monacoEditor.getValue();
  try {
    const config = JSON.parse(configText);
    document.getElementById('runConfigModal').style.display = 'none';
    toast(`Deploying ${config.image}...`, 'info');
    const r = await api('/api/containers', 'POST', config);
    const data = await r.json();
    if (r.ok && data.success) { toast(data.message, 'success'); loadContainers(); }
    else toast(data.message || data.detail || 'Failed to deploy container', 'error');
  } catch (e) { toast('Invalid JSON configuration', 'error'); }
};

/* ─── AI Integration ────────────────────────────────────────────────────────── */
document.getElementById('btnAI').onclick = async () => {
  document.getElementById('aiSettingsModal').style.display = 'flex';
  try {
    const r = await api('/api/ai/config', 'GET');
    if (r.ok) {
      const config = await r.json();
      document.getElementById('aiProvider').value = config.provider;
      const provOpt = document.querySelector(`#providerSelect [data-value="${config.provider}"]`);
      if (provOpt) {
        document.querySelector('#providerSelect .custom-select-option.selected')?.classList.remove('selected');
        provOpt.classList.add('selected');
        document.getElementById('providerTrigger').textContent = provOpt.textContent;
      }
      updateModelOptions(config.provider);
      document.getElementById('aiModel').value = config.model;
      const modelOpt = document.querySelector(`#modelSelect [data-value="${config.model}"]`);
      if (modelOpt) {
        document.querySelector('#modelSelect .custom-select-option.selected')?.classList.remove('selected');
        modelOpt.classList.add('selected');
        document.getElementById('modelTrigger').textContent = modelOpt.textContent;
      } else {
        document.getElementById('modelTrigger').textContent = config.model;
      }
      document.getElementById('aiKey').value = config.api_key;
      document.getElementById('aiEndpoint').value = config.local_endpoint;
      document.getElementById('aiSystemPrompt').value = config.system_prompt;
    }
  } catch (e) { console.error("Could not load AI config", e); }
};

document.getElementById('btnCancelAIConfig').onclick = () => { document.getElementById('aiSettingsModal').style.display = 'none'; };

document.getElementById('btnSaveAIConfig').onclick = async () => {
  const config = {
    provider: document.getElementById('aiProvider').value,
    model: document.getElementById('aiModel').value,
    api_key: document.getElementById('aiKey').value,
    local_endpoint: document.getElementById('aiEndpoint').value,
    system_prompt: document.getElementById('aiSystemPrompt').value,
    default_agent_name: document.getElementById('aiModel').value,
    temperature: 0.7
  };
  try {
    const r = await api('/api/ai/config', 'POST', config);
    if (r.ok) { toast('AI settings saved', 'success'); document.getElementById('aiSettingsModal').style.display = 'none'; }
    else toast('Failed to save AI settings', 'error');
  } catch (e) { toast('Error saving AI settings', 'error'); }
};

/* ─── Floating AI Chat Panel ────────────────────────────────────────────────── */
const aiPanel = document.getElementById('aiChatPanel');
const aiDragHandle = document.getElementById('aiChatDragHandle');
const aiBubble = document.getElementById('aiChatBubble');
let chatMinimized = false;
let savedPanelState = {};   // stores size/position before minimize

// Open chat panel (via bubble)
aiBubble.onclick = () => {
  chatMinimized = false;
  aiBubble.style.display = 'none';
  aiPanel.style.display = 'flex';
  aiPanel.style.removeProperty('width');
  aiPanel.style.removeProperty('height');
  const ctxName = selectedDetail ? `${selectedDetail.name} (${selectedDetail.image})` : 'System';
  document.getElementById('aiChatContextInfo').textContent = `Context: ${ctxName}`;
  setTimeout(() => document.getElementById('aiChatInput').focus(), 50);
};

// Close
document.getElementById('btnCloseAIChat').onclick = () => {
  aiPanel.style.display = 'none';
  aiBubble.style.display = 'flex';
  chatMinimized = false;
};

// Minimize to bubble
document.getElementById('btnMinimizeChat').onclick = () => {
  chatMinimized = true;
  savedPanelState = {
    left: aiPanel.style.left, top: aiPanel.style.top,
    width: aiPanel.style.width || aiPanel.offsetWidth + 'px',
    height: aiPanel.style.height || aiPanel.offsetHeight + 'px'
  };
  aiPanel.style.display = 'none';
  aiBubble.style.display = 'flex';
};

// Drag logic
let dragging = false, dragOffX = 0, dragOffY = 0;
aiDragHandle.addEventListener('mousedown', (e) => {
  if (e.target.closest('.ai-chat-header-btns')) return;
  dragging = true;
  const rect = aiPanel.getBoundingClientRect();
  dragOffX = e.clientX - rect.left;
  dragOffY = e.clientY - rect.top;
  document.body.style.userSelect = 'none';
});

document.addEventListener('mousemove', (e) => {
  if (!dragging) return;
  let left = e.clientX - dragOffX;
  let top = e.clientY - dragOffY;
  left = Math.max(0, Math.min(left, window.innerWidth - aiPanel.offsetWidth));
  top = Math.max(0, Math.min(top, window.innerHeight - aiPanel.offsetHeight));
  aiPanel.style.left = left + 'px';
  aiPanel.style.top = top + 'px';
});

document.addEventListener('mouseup', () => {
  dragging = false;
  document.body.style.userSelect = '';
});

// Resize logic
const aiResizeHandle = document.getElementById('aiChatResize');
let resizing = false, resizeStartX = 0, resizeStartY = 0, resizeStartW = 0, resizeStartH = 0;
aiResizeHandle.addEventListener('mousedown', (e) => {
  resizing = true;
  resizeStartX = e.clientX;
  resizeStartY = e.clientY;
  resizeStartW = aiPanel.offsetWidth;
  resizeStartH = aiPanel.offsetHeight;
  document.body.style.userSelect = 'none';
  e.stopPropagation();
});

document.addEventListener('mousemove', (e) => {
  if (!resizing) return;
  const newW = Math.max(340, resizeStartW + (e.clientX - resizeStartX));
  const newH = Math.max(280, resizeStartH + (e.clientY - resizeStartY));
  aiPanel.style.width = newW + 'px';
  aiPanel.style.height = newH + 'px';
});

document.addEventListener('mouseup', () => { resizing = false; document.body.style.userSelect = ''; });

// Download conversation
document.getElementById('btnDownloadChat').onclick = () => {
  const history = document.getElementById('aiChatHistory');
  const name = selectedDetail ? selectedDetail.name : 'chat';
  const html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>AI Chat – ${esc(name)}</title>
  <style>body{font-family:sans-serif;max-width:800px;margin:40px auto;background:#0f172a;color:#f0f9ff;padding:20px}
  .user{text-align:right;margin:12px 0}.ai{text-align:left;margin:12px 0}
  .bubble{display:inline-block;padding:10px 14px;border-radius:14px;max-width:80%}
  .user .bubble{background:#0ea5e9;color:#fff}.ai .bubble{background:#1e293b;border:1px solid #334155}
  h1{color:#38bdf8;margin-bottom:4px}p.ctx{color:#64748b;font-size:13px;margin-bottom:20px}
  pre{white-space:pre-wrap;word-break:break-word}code{background:#0f172a;padding:2px 6px;border-radius:4px}
  </style></head><body>
  <h1>AI Chat – ${esc(name)}</h1>
  <div>${history.innerHTML}</div></body></html>`;
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `ai_chat_${name}_${Date.now()}.html`;
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
  toast('Conversation downloaded', 'success');
};

// Send AI chat message
document.getElementById('btnSendAIChat').onclick = async () => {
  const input = document.getElementById('aiChatInput');
  const prompt = input.value.trim();
  if (!prompt) return;
  const history = document.getElementById('aiChatHistory');

  const userDiv = document.createElement('div');
  userDiv.className = 'ai-msg ai-msg-user';
  userDiv.innerHTML = `<div class="ai-bubble ai-bubble-user">${esc(prompt)}</div>`;
  history.appendChild(userDiv);
  input.value = '';

  const typingDiv = document.createElement('div');
  typingDiv.id = 'aiTypingIndicator';
  typingDiv.className = 'ai-msg ai-msg-ai';
  typingDiv.innerHTML = `<div class="ai-bubble ai-bubble-ai"><span class="ai-typing">●</span><span class="ai-typing" style="animation-delay:.2s">●</span><span class="ai-typing" style="animation-delay:.4s">●</span></div>`;
  history.appendChild(typingDiv);
  history.scrollTop = history.scrollHeight;

  try {
    const r = await api('/api/ai/ask', 'POST', { prompt, container_id: selectedId });
    const indicator = document.getElementById('aiTypingIndicator');
    if (indicator) indicator.remove();

    const aiDiv = document.createElement('div');
    aiDiv.className = 'ai-msg ai-msg-ai';
    const bubbleDiv = document.createElement('div');
    bubbleDiv.className = 'ai-bubble ai-bubble-ai ai-bubble-html';

    if (r.ok) {
      const data = await r.json();
      // Render markdown as HTML using marked.js (if available)
      if (typeof marked !== 'undefined') {
        bubbleDiv.innerHTML = marked.parse(data.reply || '');
      } else {
        bubbleDiv.innerHTML = `<pre style="white-space:pre-wrap;">${esc(data.reply)}</pre>`;
      }
    } else {
      const err = await r.json();
      bubbleDiv.innerHTML = `<b style="color:var(--red)">Error:</b> ${esc(err.detail)}`;
    }
    aiDiv.appendChild(bubbleDiv);
    history.appendChild(aiDiv);
  } catch (e) {
    const indicator = document.getElementById('aiTypingIndicator');
    if (indicator) indicator.remove();
    const aiDiv = document.createElement('div');
    aiDiv.className = 'ai-msg ai-msg-ai';
    aiDiv.innerHTML = `<div class="ai-bubble ai-bubble-ai"><b style="color:var(--red)">Network Error:</b> ${esc(e.message)}</div>`;
    history.appendChild(aiDiv);
  }
  history.scrollTop = history.scrollHeight;
};

document.getElementById('aiChatInput').onkeydown = (e) => {
  if (e.key === 'Enter') document.getElementById('btnSendAIChat').click();
};

/* ─── MQTT Settings ────────────────────────────────────────────────────────── */
document.getElementById('statMqtt').style.cursor = 'pointer';
document.getElementById('statMqtt').onclick = async () => {
  try {
    const r = await api('/api/system/system-config');
    if (r.ok) {
      const config = await r.json();
      document.getElementById('sysDashboardPassword').value = config.dashboard_password || '';
      document.getElementById('mqttEnabled').checked = config.enabled;
      document.getElementById('mqttHost').value = config.host || '';
      document.getElementById('mqttPort').value = config.port || 1883;
      document.getElementById('mqttUser').value = config.user || '';
      document.getElementById('mqttPassword').value = config.password || '';
      document.getElementById('mqttClientId').value = config.client_id || '';
      document.getElementById('mqttPrefix').value = config.discovery_prefix || '';
      document.getElementById('sysAppUrl').value = config.app_url || '';
      document.getElementById('mqttSettingsModal').style.display = 'flex';
    } else toast('Could not fetch System config.', 'error');
  } catch (e) { toast('Error loading System config: ' + e.message, 'error'); }
};

document.getElementById('btnCancelMQTTConfig').onclick = () => { document.getElementById('mqttSettingsModal').style.display = 'none'; };

document.getElementById('btnSaveMQTTConfig').onclick = async () => {
  const payload = {
    dashboard_password: document.getElementById('sysDashboardPassword').value,
    app_url: document.getElementById('sysAppUrl').value,
    enabled: document.getElementById('mqttEnabled').checked,
    host: document.getElementById('mqttHost').value.trim(),
    port: parseInt(document.getElementById('mqttPort').value.trim(), 10) || 1883,
    user: document.getElementById('mqttUser').value.trim(),
    password: document.getElementById('mqttPassword').value,
    client_id: document.getElementById('mqttClientId').value.trim(),
    discovery_prefix: document.getElementById('mqttPrefix').value.trim()
  };
  try {
    const r = await api('/api/system/system-config', 'POST', payload);
    if (r.ok) { document.getElementById('mqttSettingsModal').style.display = 'none'; toast('System Config Saved! Restarting backend...', 'success'); setTimeout(() => location.reload(), 2000); }
    else { const err = await r.json(); toast('Failed to save config: ' + (err.detail || ''), 'error'); }
  } catch (e) { toast('Error saving System config', 'error'); }
};

document.getElementById('btnTestMQTTConfig').onclick = async () => {
  const btn = document.getElementById('btnTestMQTTConfig');
  const originalText = btn.textContent;
  btn.textContent = 'Testing...'; btn.disabled = true;
  const payload = {
    dashboard_password: document.getElementById('sysDashboardPassword').value,
    app_url: document.getElementById('sysAppUrl').value,
    enabled: document.getElementById('mqttEnabled').checked,
    host: document.getElementById('mqttHost').value.trim(),
    port: parseInt(document.getElementById('mqttPort').value.trim(), 10) || 1883,
    user: document.getElementById('mqttUser').value.trim(),
    password: document.getElementById('mqttPassword').value,
    client_id: document.getElementById('mqttClientId').value.trim(),
    discovery_prefix: document.getElementById('mqttPrefix').value.trim()
  };
  try {
    const r = await api('/api/system/mqtt-test', 'POST', payload);
    const data = await r.json();
    if (r.ok) toast(data.message || 'Connection successful!', 'success');
    else toast(data.detail || 'Connection failed.', 'error');
  } catch (e) { toast('Error testing MQTT connection: ' + e.message, 'error'); }
  finally { btn.textContent = originalText; btn.disabled = false; }
};

/* ─── AI Model Selects ─────────────────────────────────────────────────────── */
const providerModels = {
  openai: ["gpt-5", "gpt-5-pro", "gpt-5-chat", "gpt-5.1", "o3-mini", "o1", "o1-preview", "gpt-4.5-turbo", "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
  anthropic: ["claude-4", "claude-3-7-sonnet-20250219", "claude-3-5-sonnet-latest", "claude-3-5-haiku-20241022", "claude-3-opus-20240229", "claude-3-haiku-20240307"],
  gemini: ["gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-3-pro", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
  openrouter: ["Fetching all models from OpenRouter API..."],
  lmstudio: ["local-model"],
  ollama: ["llama3.1", "llama3.2", "llama3", "mistral", "phi3", "nomic-embed-text", "codellama"]
};

let fetchedOpenRouterModels = null;

function setupCustomSelect(containerId, onSelect, allowCustom = false) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const trigger = container.querySelector('.custom-select-trigger');
  const optionsBox = container.querySelector('.custom-select-options');
  const searchInput = container.querySelector('.custom-select-search input');
  const hiddenInput = container.querySelector('input[type="hidden"]');
  const optionsList = container.querySelector('#modelOptionsList') || optionsBox;

  trigger.onclick = (e) => {
    e.stopPropagation();
    const isOpen = container.classList.contains('open');
    document.querySelectorAll('.custom-select-container').forEach(c => c.classList.remove('open'));
    if (!isOpen) {
      container.classList.add('open');
      if (searchInput) { searchInput.value = ''; filterOptions(''); setTimeout(() => searchInput.focus(), 10); }
    }
  };

  function filterOptions(query) {
    const opts = optionsList.querySelectorAll('.custom-select-option');
    let hasMatch = false;
    opts.forEach(opt => {
      const text = opt.textContent.toLowerCase();
      if (text.includes(query.toLowerCase())) { opt.classList.remove('hidden'); hasMatch = true; }
      else opt.classList.add('hidden');
    });
    let customOpt = optionsList.querySelector('.custom-value-option');
    if (allowCustom && query.trim().length > 0 && !hasMatch) {
      if (!customOpt) { customOpt = document.createElement('div'); customOpt.className = 'custom-select-option custom-value-option'; optionsList.appendChild(customOpt); }
      customOpt.textContent = `Use: ${query}`; customOpt.dataset.value = query; customOpt.classList.remove('hidden');
    } else if (customOpt) customOpt.classList.add('hidden');
  }

  if (searchInput) {
    searchInput.oninput = (e) => filterOptions(e.target.value);
    searchInput.onclick = (e) => e.stopPropagation();
    searchInput.onkeydown = (e) => { if (e.key === 'Enter') { const first = optionsList.querySelector('.custom-select-option:not(.hidden)'); if (first) first.click(); } };
  }

  optionsList.onclick = (e) => {
    const opt = e.target.closest('.custom-select-option');
    if (!opt) return;
    const val = opt.dataset.value;
    const text = opt.textContent.startsWith('Use: ') ? val : opt.textContent;
    hiddenInput.value = val; trigger.textContent = text;
    optionsList.querySelectorAll('.custom-select-option').forEach(o => o.classList.remove('selected'));
    opt.classList.add('selected');
    container.classList.remove('open');
    if (onSelect) onSelect(val);
  };
}

async function updateModelOptions(provider) {
  const list = document.getElementById('modelOptionsList');
  if (!list) return;
  let models = providerModels[provider] || [];
  if (provider === 'openrouter') {
    if (fetchedOpenRouterModels) { models = fetchedOpenRouterModels; }
    else {
      list.innerHTML = `<div class="custom-select-option" style="color: var(--text-muted); pointer-events: none;">Fetching models...</div>`;
      try {
        const res = await fetch('https://openrouter.ai/api/v1/models');
        const data = await res.json();
        fetchedOpenRouterModels = data.data.map(m => m.id);
        models = fetchedOpenRouterModels;
      } catch (err) { console.error('Failed to fetch OpenRouter models', err); models = providerModels.openrouter; }
    }
  }
  list.innerHTML = models.map(m => `<div class="custom-select-option" data-value="${m}">${m}</div>`).join('');
  const currentModel = document.getElementById('aiModel').value;
  if (models.includes(currentModel)) {
    const opt = list.querySelector(`[data-value="${currentModel}"]`);
    if (opt) opt.classList.add('selected');
    document.getElementById('modelTrigger').textContent = currentModel;
  } else if (models.length > 0) {
    const first = models[0];
    document.getElementById('aiModel').value = first;
    document.getElementById('modelTrigger').textContent = first;
    const opt = list.querySelector(`[data-value="${first}"]`);
    if (opt) opt.classList.add('selected');
  } else { document.getElementById('modelTrigger').textContent = "None"; }
}

window.onmousedown = (e) => {
  if (!e.target.closest('.custom-select-container')) document.querySelectorAll('.custom-select-container').forEach(c => c.classList.remove('open'));
};

/* ─── Push to Registry ────────────────────────────────────────────────────── */
document.getElementById('btnPushImage').onclick = async () => {
  const image = document.getElementById('pushImageName').value.trim();
  const registry = document.getElementById('pushRegistryUrl').value.trim();
  const username = document.getElementById('pushUsername').value.trim();
  const password = document.getElementById('pushPassword').value;
  const progressEl = document.getElementById('pushProgress');

  if (!image) { toast('Enter the local image name/tag', 'error'); return; }

  progressEl.textContent = 'Pushing...';
  document.getElementById('btnPushImage').disabled = true;

  try {
    const response = await fetch('/api/images/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ image, registry, username, password })
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      chunk.split('\n').filter(l => l.trim()).forEach(line => {
        try {
          const d = JSON.parse(line);
          progressEl.textContent = d.message;
          if (d.status === 'done') { progressEl.style.color = 'var(--green)'; toast('Image pushed successfully!', 'success'); }
          if (d.status === 'error') { progressEl.style.color = 'var(--red)'; toast('Push failed: ' + d.message, 'error'); }
        } catch (_) { }
      });
    }
  } catch (e) {
    progressEl.textContent = 'Error: ' + e.message;
    toast('Push error: ' + e.message, 'error');
  } finally {
    document.getElementById('btnPushImage').disabled = false;
  }
};

/* ─── Init ─────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const toastContainer = document.getElementById('toastContainer');
  if (toastContainer && typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => syncFloatingChatForToasts()).observe(toastContainer);
  }
  syncFloatingChatForToasts();

  // Custom selects
  setupCustomSelect('providerSelect', (val) => { updateModelOptions(val); });
  setupCustomSelect('modelSelect', null, true);
  setupCustomSelect('notifWebMethodSelect');
  setupCustomSelect('sortSelectContainer', (val) => { sortField = val; renderList(); });
  updateModelOptions('openai');

  // Initial load
  loadContainers();
  loadStats();

  // Poll every 5 seconds
  listInterval = setInterval(loadContainers, 5000);
  statsInterval = setInterval(loadStats, 5000);

  // Select all logic
  const chkAll = document.getElementById('chkSelectAll');
  if (chkAll) chkAll.onclick = (e) => toggleSelectAll(e.target.checked);

  // Sort controls
  document.getElementById('btnSortOrder').onclick = () => {
    sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
    document.getElementById('btnSortOrder').textContent = sortOrder === 'asc' ? '↑' : '↓';
    renderList();
  };

  // Refresh button
  document.getElementById('btnRefresh').onclick = async () => {
    await Promise.all([loadContainers(), loadStats()]);
    if (selectedId) await loadDetail(selectedId);
    toast('Refreshed', 'info', 1500);
  };

  // Logout
  document.getElementById('btnLogout').onclick = async () => {
    await api('/api/auth/logout', 'POST');
    window.location.href = '/login';
  };

  // Search
  document.getElementById('searchInput').oninput = e => {
    searchQuery = e.target.value;
    renderList();
  };

  // Filter tabs
  document.querySelectorAll('.filter-tab').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.filter-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      filter = btn.dataset.filter;
      renderList();
    };
  });

  // Modal close on ESC
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal-overlay').forEach(modal => {
        modal.style.display = 'none';
      });
    }
  });

  // Tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => switchTab(btn.dataset.tab);
  });

  // Action buttons
  document.getElementById('btnRename').addEventListener('click', (e) => { e.preventDefault(); doRename(); });
  document.getElementById('btnBackupSingle').onclick = () => downloadBackup(selectedId);
  document.getElementById('btnExportImage').onclick = () => {
    if (selectedId) window.location.href = `/api/containers/${selectedId}/export-image`;
  };
  document.getElementById('btnStart').onclick = () => containerAction('start');
  document.getElementById('btnStop').onclick = () => containerAction('stop');
  document.getElementById('btnRestart').onclick = () => containerAction('restart');
  document.getElementById('btnUpdate').onclick = () => updateContainer(selectedId);
  document.getElementById('btnRemove').onclick = () => doRemove();
  document.getElementById('btnExportExcel').onclick = exportListToExcel;
  document.getElementById('btnBackupAll').onclick = downloadAllBackups;

  // Ports
  document.getElementById('btnPorts').onclick = loadPorts;
  document.getElementById('btnClosePorts').onclick = () => { document.getElementById('portsModal').style.display = 'none'; };

  // Logs toolbar
  document.getElementById('btnClearLogs').onclick = () => { document.getElementById('logOutput').innerHTML = ''; };
  document.getElementById('btnExportLogsCSV').onclick = () => { exportLogsToCSV(); };

  // Update modal close
  document.getElementById('btnCloseUpdate').onclick = () => { document.getElementById('updateModal').style.display = 'none'; };

  // Terminal reconnect & start
  const btnStartTerm = document.getElementById('btnStartTermSession');
  if (btnStartTerm) btnStartTerm.onclick = () => startTerminal();
  document.getElementById('btnReconnectTerm').onclick = () => { connectTerminal(); };

  // Dashboard buttons
  document.getElementById('btnRefreshDashboard').onclick = () => loadContainers();
  document.getElementById('btnStartAll').onclick = async () => {
    if (await confirm('Start All', 'Are you sure you want to start all containers?')) {
      toast('Starting all containers...', 'info');
      await api('/api/containers/bulk/start', 'POST', { ids: containers.map(c => c.id) });
      loadContainers();
    }
  };
  document.getElementById('btnStopAll').onclick = async () => {
    if (await confirm('Stop All', 'Are you sure you want to stop all active containers?')) {
      toast('Stopping containers...', 'info');
      await api('/api/containers/bulk/stop', 'POST', { ids: containers.map(c => c.id) });
      loadContainers();
    }
  };

  // --- Notification Settings ---
  document.getElementById('btnNotifications').onclick = async () => {
    document.getElementById('notificationSettingsModal').style.display = 'flex';
    try {
      const r = await api('/api/system/notifications');
      const conf = await r.json();

      // Map values to UI
      document.getElementById('notifSmtpHost').value = conf.email.smtp_host;
      document.getElementById('notifSmtpPort').value = conf.email.smtp_port;
      document.getElementById('notifSmtpTls').checked = conf.email.use_tls;
      document.getElementById('notifSmtpUser').value = conf.email.smtp_user;
      document.getElementById('notifSmtpPass').value = conf.email.smtp_password;
      document.getElementById('notifFrom').value = conf.email.from_email;
      document.getElementById('notifTo').value = conf.email.to_email;

      document.getElementById('notifTeleToken').value = conf.telegram.bot_token;
      document.getElementById('notifTeleChat').value = conf.telegram.chat_id;

      document.getElementById('notifMqttTopic').value = conf.mqtt.topic;

      document.getElementById('notifWebUrl').value = conf.webhook.url;
      const webMethod = conf.webhook.method || 'POST';
      document.getElementById('notifWebMethod').value = webMethod;
      document.getElementById('notifWebMethodTrigger').textContent = webMethod;
      document.querySelectorAll('#notifWebMethodSelect .custom-select-option').forEach(o => o.classList.toggle('selected', o.dataset.value === webMethod));

      // toggles
      document.querySelectorAll('.provider-toggle').forEach(chk => {
        chk.checked = conf.enabled_providers.includes(chk.dataset.provider);
      });
    } catch (e) { console.error("Could not load notif config", e); }
  };

  document.getElementById('btnCancelNotifConfig').onclick = () => {
    document.getElementById('notificationSettingsModal').style.display = 'none';
  };

  document.getElementById('btnSaveNotifConfig').onclick = async () => {
    const enabled = Array.from(document.querySelectorAll('.provider-toggle'))
      .filter(c => c.checked).map(c => c.dataset.provider);

    const conf = {
      enabled_providers: enabled,
      email: {
        smtp_host: document.getElementById('notifSmtpHost').value,
        smtp_port: parseInt(document.getElementById('notifSmtpPort').value) || 587,
        use_tls: document.getElementById('notifSmtpTls').checked,
        smtp_user: document.getElementById('notifSmtpUser').value,
        smtp_password: document.getElementById('notifSmtpPass').value,
        from_email: document.getElementById('notifFrom').value,
        to_email: document.getElementById('notifTo').value
      },
      telegram: {
        bot_token: document.getElementById('notifTeleToken').value,
        chat_id: document.getElementById('notifTeleChat').value
      },
      mqtt: {
        topic: document.getElementById('notifMqttTopic').value
      },
      webhook: {
        url: document.getElementById('notifWebUrl').value,
        method: document.getElementById('notifWebMethod').value,
        headers: {}
      }
    };

    try {
      const r = await api('/api/system/notifications', 'POST', conf);
      if (r.ok) {
        toast('Notification settings saved', 'success');
        document.getElementById('notificationSettingsModal').style.display = 'none';
      }
    } catch (e) { toast('Error saving notifications', 'error'); }
  };

  // --- Monitoring Auto-Save ---
  const saveMonitoring = async () => {
    if (!selectedId) return;
    const conf = {
      enabled: document.getElementById('monEnabled').checked,
      auto_restart: document.getElementById('monAutoRestart').checked,
      auto_start_on_stop: document.getElementById('monAutoStartOnStop').checked,
      monitor_logs: document.getElementById('monLogs').checked,
      log_patterns: document.getElementById('monPatterns').value.split(',').map(p => p.trim()).filter(p => p)
    };
    try {
      await api(`/api/containers/${selectedId}/monitoring`, 'POST', conf);
      // Auto-saved silently for cleaner UX, could add a tiny indicator if needed
    } catch (e) {
      console.error('Error auto-saving monitoring:', e);
    }
  };

  ['monEnabled', 'monAutoRestart', 'monAutoStartOnStop', 'monLogs'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', saveMonitoring);
  });
  document.getElementById('monPatterns').addEventListener('input', debounce(saveMonitoring, 1000));

  // --- Notification Test ---
  window.testNotification = async (provider, e) => {
    e.preventDefault();
    e.stopPropagation();

    // Default object for Pydantic validation
    const sets = { email: {}, telegram: {}, mqtt: {}, webhook: {} };

    // Only populate the one being tested for 100% isolation
    if (provider === 'email') {
      sets.email = {
        smtp_host: document.getElementById('notifSmtpHost').value,
        smtp_port: parseInt(document.getElementById('notifSmtpPort').value) || 587,
        smtp_user: document.getElementById('notifSmtpUser').value,
        smtp_password: document.getElementById('notifSmtpPass').value,
        from_email: document.getElementById('notifFrom').value,
        to_email: document.getElementById('notifTo').value,
        use_tls: document.getElementById('notifSmtpTls').checked
      };
    } else if (provider === 'telegram') {
      sets.telegram = {
        bot_token: document.getElementById('notifTeleToken').value,
        chat_id: document.getElementById('notifTeleChat').value
      };
    } else if (provider === 'mqtt') {
      sets.mqtt = { topic: document.getElementById('notifMqttTopic').value };
    } else if (provider === 'webhook') {
      const hStr = document.getElementById('notifWebHeaders').value.trim();
      let heads = {};
      if (hStr) {
        try { heads = JSON.parse(hStr); }
        catch (err) { return toast('Invalid Webhook Headers JSON', 'error'); }
      }
      sets.webhook = {
        url: document.getElementById('notifWebUrl').value,
        method: document.getElementById('notifWebMethod').value,
        headers: heads
      };
    }

    const b = e.currentTarget;
    const oT = b.textContent;
    b.disabled = true;
    b.textContent = '...';

    try {
      const r = await api(`/api/system/notifications/test?provider=${provider}`, 'POST', sets);
      const res = await r.json();
      if (r.ok) toast(res.message, 'success');
      else throw new Error(res.detail || 'Test failed');
    } catch (err) {
      toast(err.message || 'Test failed', 'error');
    } finally {
      b.disabled = false;
      b.textContent = oT;
    }
  };
});
