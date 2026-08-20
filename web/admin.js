let csrf = '';
let models = [];
const $ = (id) => document.getElementById(id);

async function json(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function showAdmin() {
  $('loginPanel').hidden = true;
  $('adminPanel').hidden = false;
  $('logout').hidden = false;
  loadModels();
  loadDevices();
}

async function loadModels() {
  try {
    const data = await json('/api/v1/admin/models');
    models = data.items;
    $('modelRows').innerHTML = models.map((model) => `<tr><td>${model.display_name}<br><small>${model.name}</small></td><td>${model.base_url || '--'}</td><td>${model.model_name || '--'}</td><td>${model.enabled ? '启用' : '停用'}<br><small>${model.last_status || ''}</small></td><td>${model.key_configured ? `已配置 ···${model.key_suffix}` : '未配置'}</td><td>${model.weight}</td><td><button data-edit="${model.id}">编辑</button> <button data-test="${model.id}">测试</button> <button data-delete="${model.id}">删除</button></td></tr>`).join('');
    document.querySelectorAll('[data-edit]').forEach((button) => { button.onclick = () => editModel(Number(button.dataset.edit)); });
    document.querySelectorAll('[data-test]').forEach((button) => { button.onclick = () => testModel(Number(button.dataset.test)); });
    document.querySelectorAll('[data-delete]').forEach((button) => { button.onclick = () => deleteModel(Number(button.dataset.delete)); });
  } catch (error) { alert(error.message); }
}

function editModel(id) {
  const model = models.find((item) => item.id === id);
  $('editor').hidden = false;
  $('modelId').value = model?.id || '';
  $('modelName').value = model?.name || '';
  $('modelName').disabled = Boolean(model);
  $('displayName').value = model?.display_name || '';
  $('baseUrl').value = model?.base_url || '';
  $('apiKey').value = '';
  $('modelNameValue').value = model?.model_name || '';
  $('weight').value = model?.weight ?? 1;
  $('timeout').value = model?.timeout_seconds ?? 15;
  $('enabled').checked = Boolean(model?.enabled);
  $('formMessage').textContent = '';
}

async function testModel(id) {
  try {
    const data = await json(`/api/v1/admin/models/${id}/test`, { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
    alert(`测试成功，耗时 ${data.latency_ms} ms`);
    loadModels();
  } catch (error) { alert(`测试失败：${error.message}`); }
}

async function deleteModel(id) {
  if (!confirm('确认删除该模型配置？')) return;
  try { await json(`/api/v1/admin/models/${id}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } }); loadModels(); } catch (error) { alert(error.message); }
}

async function loadDevices() {
  try {
    const data = await json('/api/v1/admin/devices');
    $('deviceRows').innerHTML = data.items.length ? data.items.map((device) => `<tr><td>${device.name}</td><td><code>${device.device_id}</code></td><td>${device.last_credential_at || '--'}</td><td>${device.expires_at || '未上传'} ${device.credential_active ? '有效' : ''}</td><td><button data-device-delete="${device.device_id}">撤销</button></td></tr>`).join('') : '<tr><td colspan="5" class="empty">尚未创建授权设备</td></tr>';
    document.querySelectorAll('[data-device-delete]').forEach((button) => { button.onclick = () => deleteDevice(button.dataset.deviceDelete); });
  } catch (error) { alert(error.message); }
}

async function createDevice() {
  const name = prompt('设备名称，例如：办公室 Windows 主机');
  if (!name) return;
  try {
    const data = await json('/api/v1/admin/devices', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ name }) });
    alert(`请立即保存以下信息，配对令牌只显示一次：\n\n设备 ID：${data.device.device_id}\n配对令牌：${data.pairing_token}`);
    loadDevices();
  } catch (error) { alert(error.message); }
}

async function deleteDevice(id) {
  if (!confirm('撤销后该设备无法再上传凭证，确认继续？')) return;
  try { await json(`/api/v1/admin/devices/${encodeURIComponent(id)}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } }); loadDevices(); } catch (error) { alert(error.message); }
}

$('loginForm').onsubmit = async (event) => {
  event.preventDefault();
  try {
    const data = await json('/api/v1/admin/login', { method: 'POST', body: JSON.stringify({ username: $('username').value, password: $('password').value }) });
    csrf = data.csrf_token;
    showAdmin();
  } catch (error) { $('loginError').textContent = error.message; }
};

$('modelForm').onsubmit = async (event) => {
  event.preventDefault();
  const payload = { name: $('modelName').value, display_name: $('displayName').value, base_url: $('baseUrl').value, api_key: $('apiKey').value || null, model_name: $('modelNameValue').value, enabled: $('enabled').checked, weight: Number($('weight').value), timeout_seconds: Number($('timeout').value) };
  try {
    const id = $('modelId').value;
    await json(id ? `/api/v1/admin/models/${id}` : '/api/v1/admin/models', { method: id ? 'PUT' : 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(payload) });
    $('editor').hidden = true;
    loadModels();
  } catch (error) { $('formMessage').textContent = error.message; }
};

$('newModel').onclick = () => editModel(null);
$('cancelEdit').onclick = () => { $('editor').hidden = true; };
$('testModel').onclick = () => { const id = Number($('modelId').value); if (id) testModel(id); else $('formMessage').textContent = '请先保存模型配置'; };
$('newDevice').onclick = createDevice;
$('logout').onclick = async () => { await json('/api/v1/admin/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } }); location.reload(); };
