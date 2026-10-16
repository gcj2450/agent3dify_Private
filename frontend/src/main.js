import './style.css';
import { icon, hydrateIcons } from './icons.js';
import { ModelViewer } from './viewer.js';

const $ = id => document.getElementById(id);
hydrateIcons();
const state = { id: null, session: null, sessions: [], attachments: [], config: {}, cursor: -1, events: [], previewVersion: '', sourceVersion: '', messageSignature: '', filesSignature: '', tab: 'model', sending: false };
let viewer, toastTimer, pollTimer, listTimer;
try { viewer = new ModelViewer($('viewport'), size => {
  $('previewEmpty').hidden = true;
  $('dimensions').textContent = `X ${size.x.toFixed(2)}   Y ${size.y.toFixed(2)}   Z ${size.z.toFixed(2)} mm`;
  $('previewStatus').textContent = '已同步最新模型';
  $('modelDot').classList.add('ready');
}); } catch (error) { $('previewStatus').textContent = '浏览器未启用 WebGL，可下载 STEP / STL 查看'; }

function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5000); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}
function artifactUrl(path, version = '') { return `/api/artifacts/${state.id}/${path.split('/').map(encodeURIComponent).join('/')}?v=${encodeURIComponent(version)}`; }
function download(path) { const a = document.createElement('a'); a.href = artifactUrl(path) + '&download=1'; a.download = path.split('/').pop(); a.click(); }
$('downloadDiagnostics').onclick = () => {
  if (!state.id) return;
  const a = document.createElement('a');
  a.href = `/api/sessions/${state.id}/diagnostics`;
  a.download = `agent3dify-${state.id.slice(0, 8)}-diagnostics.zip`;
  a.click();
};
function statusText(status) { return ({ running: '代理正在建模', stopping: '正在停止执行', completed: '本轮已完成', stopped: '已停止 · 可继续对话', error: '执行遇到问题 · 可重试', idle: '工作台已就绪' })[status] || '工作台已就绪'; }
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll('[data-tab]').forEach(btn => { const selected = btn.dataset.tab === tab; btn.classList.toggle('active', selected); btn.setAttribute('aria-selected', selected); });
  for (const name of ['model', 'drawing', 'source']) $(`${name}Pane`).hidden = name !== tab;
  if (tab === 'model') viewer?.resize();
  if (tab === 'source') refreshSource();
}
document.querySelectorAll('[data-tab]').forEach(btn => btn.onclick = () => setTab(btn.dataset.tab));
document.querySelectorAll('[data-view]').forEach(btn => btn.onclick = () => { document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b === btn)); viewer?.setView(btn.dataset.view); });
for (const [id, method] of [['wireframe', 'toggleWireframe'], ['grid', 'toggleGrid']]) $(id).onclick = () => { if (!viewer) return; const active = viewer[method](); $(id).classList.toggle('active', active); $(id).setAttribute('aria-pressed', active); };
$('fit').onclick = () => { viewer?.setView('iso'); document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === 'iso')); };

async function refreshSource() {
  const file = state.session?.files.find(f => f.path === 'generated/model.py');
  if (!file || state.sourceVersion === file.version) return;
  const id = state.id;
  try {
    const response = await fetch(artifactUrl(file.path, file.version));
    if (!response.ok) return;
    const text = await response.text();
    if (id !== state.id) return;
    $('sourceCode').textContent = text;
    state.sourceVersion = file.version;
  } catch { /* retry on next update */ }
}
$('copyCode').onclick = async () => { try { await navigator.clipboard.writeText($('sourceCode').textContent); toast('代码已复制'); } catch { toast('无法读取剪贴板，请选择代码后复制'); } };

function renderMessages(session) {
  const signature = JSON.stringify(session.messages);
  if (signature === state.messageSignature) return;
  state.messageSignature = signature;
  const nearBottom = $('chatScroll').scrollHeight - $('chatScroll').scrollTop - $('chatScroll').clientHeight < 100;
  $('messages').replaceChildren();
  for (const message of session.messages) {
    const item = document.createElement('article'); item.className = `message ${message.role}`;
    const header = document.createElement('div'); header.className = 'message-header';
    const avatar = document.createElement('span'); avatar.className = 'message-avatar';
    avatar.innerHTML = message.role === 'user' ? '你' : icon('cube');
    const who = document.createElement('span'); who.textContent = message.role === 'user' ? '你' : message.role === 'error' ? '执行提示' : 'Agent3dify';
    header.append(avatar, who);
    if (message.status === 'queued' || message.status === 'interrupted') { const badge = document.createElement('span'); badge.className = 'message-badge'; badge.textContent = message.status === 'queued' ? '待处理' : '已中断'; header.append(badge); }
    const time = document.createElement('time'); time.textContent = new Date(message.time * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); header.append(time);
    const body = document.createElement('div'); body.className = 'message-body'; body.textContent = message.text;
    item.append(header, body); $('messages').append(item);
  }
  if (nearBottom || session.messages.length < 3) requestAnimationFrame(() => $('chatScroll').scrollTop = $('chatScroll').scrollHeight);
}
function renderEvents(events) {
  if (!events.length) return;
  state.events.push(...events);
  $('activityCount').textContent = state.events.length;
  for (const event of events) {
    const line = document.createElement('div'); line.className = `activity-line ${event.kind}`;
    const time = document.createElement('time'); time.textContent = new Date(event.time * 1000).toLocaleTimeString('zh-CN', { hour12: false });
    const text = document.createElement('span'); text.textContent = event.text;
    line.append(time, text);
    if (event.detail) { const detail = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = '查看输出'; const pre = document.createElement('pre'); pre.textContent = event.detail; detail.append(summary, pre); line.append(detail); }
    $('activityLog').append(line);
  }
  while ($('activityLog').children.length > 300) $('activityLog').firstChild.remove();
  $('activityLog').scrollTop = $('activityLog').scrollHeight;
}
function sizeText(bytes) { return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : bytes > 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`; }
function renderFiles(files) {
  const signature = JSON.stringify(files);
  if (signature === state.filesSignature) return;
  state.filesSignature = signature;
  const output = files.filter(f => !f.path.startsWith('input/') && !f.path.includes('/history/') && f.path !== 'artifacts/preview.stl');
  const order = ['artifacts/model.step', 'artifacts/model.stl', 'generated/model.py'];
  output.sort((a, b) => (order.includes(a.path) ? order.indexOf(a.path) : 10) - (order.includes(b.path) ? order.indexOf(b.path) : 10));
  $('files').replaceChildren(); $('fileCount').textContent = output.length;
  if (!output.length) { const e = document.createElement('div'); e.className = 'files-empty'; e.textContent = 'STEP、STL、建模代码与检查报告会保存在这里'; $('files').append(e); }
  for (const file of output) {
    const card = document.createElement('div'); card.className = 'file-card';
    const open = document.createElement('button'); open.type = 'button'; open.title = `查看 ${file.path}`;
    const ext = file.path.split('.').pop(); const type = document.createElement('span'); type.className = `file-type ${ext}`; type.textContent = ext.toUpperCase();
    const meta = document.createElement('span'); meta.className = 'file-meta';
    const name = document.createElement('b'); name.textContent = file.path.split('/').pop(); const size = document.createElement('small'); size.textContent = `${sizeText(file.size)} · ${file.path.split('/')[0]}`; meta.append(name, size); open.append(type, meta);
    open.onclick = () => {
      if (ext === 'stl') { setTab('model'); loadPreview(file); }
      else if (ext === 'py') { setTab('source'); refreshSource(); }
      else if (['png', 'jpg', 'svg'].includes(ext)) { setTab('drawing'); $('drawingPreview').src = artifactUrl(file.path, file.version); $('drawingPreview').hidden = false; $('drawingEmpty').hidden = true; $('drawingCaption').textContent = file.path; }
      else download(file.path);
    };
    const save = document.createElement('button'); save.className = 'download-file'; save.title = `下载 ${file.path}`; save.innerHTML = icon('download'); save.onclick = () => download(file.path);
    card.append(open, save); $('files').append(card);
  }
  const exportFile = output.find(f => /\.step$/i.test(f.path)) || output.find(f => /\.stl$/i.test(f.path));
  $('download').disabled = !exportFile;
  $('download').querySelector('span').textContent = exportFile?.path.endsWith('.stl') ? '导出 STL' : '导出 STEP';
  $('download').onclick = () => exportFile && download(exportFile.path);
}
async function loadPreview(file) {
  if (!viewer) return;
  const id = state.id;
  $('previewStatus').textContent = '正在同步模型…';
  try { await viewer.load(artifactUrl(file.path, file.version)); if (state.id === id) state.previewVersion = `${file.path}:${file.version}`; }
  catch (error) { if (error.name !== 'AbortError' && state.id === id) $('previewStatus').textContent = '模型文件写入中，稍后自动重试'; }
}
async function updateSession() {
  const id = state.id;
  if (!id) return;
  try {
    const session = await api(`/api/sessions/${id}?after=${state.cursor}`);
    if (id !== state.id) return;
    state.session = session;
    $('downloadDiagnostics').disabled = false;
    if (session.events.length) state.cursor = session.events.at(-1).id;
    $('welcome').hidden = true;
    $('chatTitle').textContent = session.title;
    $('modelLabel').textContent = session.model;
    $('footerState').textContent = statusText(session.status);
    $('revision').textContent = `第 ${session.turn} 轮迭代`;
    const running = session.status === 'running' || session.status === 'stopping';
    $('working').hidden = !running;
    $('workingText').textContent = session.status === 'stopping' ? '正在停止代理与执行进程…' : '代理正在建模，预览会自动更新';
    $('stop').hidden = !running; $('stop').disabled = session.status === 'stopping';
    $('message').placeholder = running ? '继续输入修改意见，将在本轮完成后处理…' : '哪里需要调整？告诉智能体你的修改意见…';
    $('queueHint').hidden = !session.queue_count;
    $('queueHint').textContent = `${session.queue_count} 条意见待处理 · 当前轮完成后按顺序应用`;
    $('composerHint').textContent = running ? '可随时发送修改意见 · 当前模型会保留' : 'Enter 发送 · Shift + Enter 换行';
    renderMessages(session); renderEvents(session.events); renderFiles(session.files);
    const preview = session.files.find(f => f.path === 'artifacts/preview.stl') || session.files.find(f => f.path === 'artifacts/model.stl');
    if (preview && `${preview.path}:${preview.version}` !== state.previewVersion) await loadPreview(preview);
    if (state.tab === 'source') refreshSource();
    if (!$('drawingPreview').getAttribute('src')) {
      const ref = session.files.find(f => f.path === 'input/reference.png');
      if (ref) { $('drawingPreview').src = artifactUrl(ref.path, ref.version); $('drawingPreview').hidden = false; $('drawingEmpty').hidden = true; $('drawingCaption').textContent = '原始三视图 · input/reference.png'; }
    }
  } catch (error) { if (id === state.id) $('footerState').textContent = `连接中断：${error.message}`; }
  finally { if (id === state.id) { clearTimeout(pollTimer); pollTimer = setTimeout(updateSession, 1000); } }
}
function resetSession() {
  $('downloadDiagnostics').disabled = true;
  clearTimeout(pollTimer); state.id = null; state.session = null; state.cursor = -1; state.events = []; state.previewVersion = ''; state.sourceVersion = ''; state.messageSignature = ''; state.filesSignature = '';
  viewer?.clear(); $('messages').replaceChildren(); $('activityLog').replaceChildren(); $('activityCount').textContent = '0'; $('working').hidden = true; $('stop').hidden = true; $('queueHint').hidden = true; $('previewEmpty').hidden = false; $('previewStatus').textContent = '等待模型生成'; $('modelDot').classList.remove('ready'); $('dimensions').textContent = 'X —   Y —   Z —'; $('drawingPreview').removeAttribute('src'); $('drawingPreview').hidden = true; $('drawingEmpty').hidden = false; $('drawingCaption').textContent = ''; $('sourceCode').textContent = '模型生成后，可在此查看参数化 CadQuery 代码。'; $('download').disabled = true; $('revision').textContent = '未开始建模'; $('footerState').textContent = '工作台已就绪'; $('welcome').hidden = false; $('chatTitle').textContent = '新建任务'; $('fileCount').textContent = '0'; $('files').innerHTML = '<div class="files-empty">STEP、STL、建模代码与检查报告会保存在这里</div>'; $('modelLabel').textContent = state.config.model || '未配置模型'; $('message').placeholder = '描述建模要求，或上传三视图开始…'; $('composerHint').textContent = 'Enter 发送 · Shift + Enter 换行'; setTab('model');
}
async function selectSession(id) { if (id === state.id) return; resetSession(); state.id = id; state.attachments = []; renderAttachments(); localStorage.setItem('agent3dify.session', id); renderSessionList(); await updateSession(); }
function renderSessionList() {
  $('sessions').replaceChildren(); $('sessionCount').textContent = state.sessions.length;
  if (!state.sessions.length) { const el = document.createElement('div'); el.className = 'no-sessions'; el.textContent = '你的建模任务会保存在这里'; $('sessions').append(el); }
  for (const session of state.sessions) {
    const button = document.createElement('button'); button.className = `session-item${session.id === state.id ? ' active' : ''}`; button.title = session.title; button.innerHTML = icon('chat');
    const title = document.createElement('span'); title.textContent = session.title; button.append(title);
    if (session.status === 'running') { const dot = document.createElement('i'); dot.className = 'running'; button.append(dot); }
    button.onclick = () => selectSession(session.id); $('sessions').append(button);
  }
}
async function refreshList() { try { const data = await api('/api/sessions'); state.sessions = data.sessions; renderSessionList(); } catch { /* server may be restarting */ } finally { clearTimeout(listTimer); listTimer = setTimeout(refreshList, 5000); } }
$('newChat').onclick = () => { resetSession(); state.attachments = []; renderAttachments(); $('message').value = ''; localStorage.removeItem('agent3dify.session'); renderSessionList(); };

function renderAttachments() {
  $('attachments').replaceChildren();
  state.attachments.forEach((file, index) => {
    const chip = document.createElement('div'); chip.className = 'attachment'; const img = document.createElement('img'); img.src = file.data; img.alt = `视图 ${index + 1}`; const label = document.createElement('span'); label.textContent = file.name; const remove = document.createElement('button'); remove.type = 'button'; remove.title = '移除此图片'; remove.innerHTML = icon('close'); remove.onclick = () => { state.attachments.splice(index, 1); renderAttachments(); }; chip.append(img, label, remove); $('attachments').append(chip);
  });
}
async function attachFiles(files) {
  if (state.id) { toast('当前任务已有图纸；请新建任务上传另一组图纸'); return; }
  for (const file of files) {
    if (state.attachments.length >= 3) { toast('最多上传 3 张视图'); break; }
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) { toast('请上传 PNG、JPG 或 WEBP 图片'); continue; }
    if (file.size > 12 * 1024 * 1024) { toast('单张图片请小于 12 MB'); continue; }
    const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
    state.attachments.push({ name: file.name, data });
  }
  renderAttachments();
  if (state.attachments.length) { $('drawingPreview').src = state.attachments[0].data; $('drawingPreview').hidden = false; $('drawingEmpty').hidden = true; $('drawingCaption').textContent = `${state.attachments.length} 张参考视图`; $('message').focus(); }
}
for (const id of ['attach', 'chooseDrawing']) $(id).onclick = () => $('fileInput').click();
$('fileInput').onchange = async event => { await attachFiles([...event.target.files]); event.target.value = ''; };
$('sample').onclick = async () => { try { const response = await fetch('/api/sample'); if (!response.ok) throw new Error('示例图片不存在'); const blob = await response.blob(); await attachFiles([new File([blob], '示例工程图.png', { type: 'image/png' })]); toast('示例图已添加，可补充要求后点击发送'); } catch (error) { toast(error.message); } };
document.addEventListener('paste', event => { const files = [...(event.clipboardData?.files || [])]; if (files.length && !$('settings').open) { event.preventDefault(); attachFiles(files); } });
let dragDepth = 0;
document.addEventListener('dragenter', event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); dragDepth++; $('composer').classList.add('drag-overlay'); } });
document.addEventListener('dragover', event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); });
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) $('composer').classList.remove('drag-overlay'); });
document.addEventListener('drop', event => { event.preventDefault(); dragDepth = 0; $('composer').classList.remove('drag-overlay'); attachFiles([...event.dataTransfer.files]); });

$('composer').onsubmit = async event => {
  event.preventDefault(); if (state.sending) return;
  const text = $('message').value.trim();
  if (!text && state.id) return;
  if (!state.id && !state.attachments.length) { toast('请先上传三视图图片'); $('fileInput').click(); return; }
  if (!state.config.has_api_key) { openSettings(); toast('请先配置 API Key'); return; }
  state.sending = true; $('send').disabled = true;
  try {
    if (!state.id) { const result = await api('/api/sessions', { images: state.attachments, message: text, model: state.config.model, title: state.attachments[0].name.replace(/\.[^.]+$/, '') }); $('message').value = ''; await selectSession(result.id); }
    else { await api(`/api/sessions/${state.id}/messages`, { message: text }); $('message').value = ''; clearTimeout(pollTimer); await updateSession(); }
    clearTimeout(listTimer); await refreshList();
  } catch (error) { toast(error.message); }
  finally { state.sending = false; $('send').disabled = false; }
};
$('message').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } };
$('stop').onclick = async () => { if (!state.id) return; $('stop').disabled = true; try { await api(`/api/sessions/${state.id}/stop`, {}); clearTimeout(pollTimer); await updateSession(); } catch (error) { toast(error.message); $('stop').disabled = false; } };

function applyConfig(config) { state.config = config; $('connectionDot').classList.toggle('ready', config.has_api_key); if (!state.id) $('modelLabel').textContent = config.model || '未配置模型'; }
function setModelOptions(models, selected) {
  const values = [...new Set([selected, ...models].filter(Boolean))];
  $('modelInput').replaceChildren(...values.map(model => new Option(model, model, false, model === selected)));
  if (selected && !values.includes(selected)) $('modelInput').value = selected;
}
function openSettings() {
  $('baseUrl').value = state.config.base_url || '';
  setModelOptions([], state.config.model || 'gpt-5.6-sol');
  $('apiKey').value = '';
  $('keyState').textContent = state.config.has_api_key ? '已配置' : '未配置';
  $('settingsStatus').textContent = '';
  $('settings').showModal();
  loadModels(true);
}
$('settingsOpen').onclick = openSettings; $('settingsClose').onclick = () => $('settings').close();
$('settings').onclick = event => { if (event.target === $('settings') && (event.clientX < $('settings').getBoundingClientRect().left || event.clientX > $('settings').getBoundingClientRect().right)) $('settings').close(); };
$('settingsForm').onsubmit = async event => {
  event.preventDefault(); $('saveSettings').disabled = true;
  try { const config = await api('/api/settings', { base_url: $('baseUrl').value, api_key: $('apiKey').value, model: $('modelInput').value }); applyConfig(config); $('apiKey').value = ''; $('settings').close(); toast('设置已保存，新任务和空闲任务将使用当前模型'); }
  catch (error) { $('settingsStatus').textContent = error.message; }
  finally { $('saveSettings').disabled = false; }
};
$('loadModels').onclick = () => loadModels(false);
async function loadModels(silent = false) {
  $('loadModels').disabled = true;
  if (!silent) $('settingsStatus').textContent = '正在读取可用模型…';
  try {
    const data = await api('/api/models');
    const selected = $('modelInput').value || state.config.model || 'gpt-5.6-sol';
    setModelOptions(data.models, selected);
    if (!silent) $('settingsStatus').textContent = `已读取 ${data.models.length} 个模型，可在上方选择或输入名称`;
  } catch (error) {
    if (!silent) $('settingsStatus').textContent = `读取模型失败：${error.message}`;
  } finally { $('loadModels').disabled = false; }
}
async function init() {
  try { applyConfig(await api('/api/config')); if (!state.config.sample_available) $('sample').hidden = true; loadModels(true); await refreshList(); const last = localStorage.getItem('agent3dify.session'); if (last && state.sessions.some(s => s.id === last)) await selectSession(last); }
  catch (error) { toast(`无法连接本地服务：${error.message}`); }
}
init();
