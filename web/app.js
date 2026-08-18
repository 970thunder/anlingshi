const state = { window: 50 };
const $ = (id) => document.getElementById(id);
const META = { blue: { code: '1', name: '幽颅贤者', color: '蓝' }, red: { code: '2', name: '赤峰女皇', color: '红' } };
const meta = (side) => META[side] || { code: '--', name: '', color: '' };
const label = (side) => { const m = meta(side); return m.name ? `${m.code} · ${m.name}（${m.color}）` : m.code; };
const fmt = (v) => v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '--';
async function getJson(path) { const r = await fetch(path); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
function codeCell(item, actual) {
  if (actual) return actual === 'blue' ? '<span class="cell-code blue">1</span>' : '<span class="cell-code red">2</span>';
  if (!item || !item.predicted_side) return item?.status === 'error' ? '<span class="model-error">失败</span>' : '<span class="muted">--</span>';
  const m = meta(item.predicted_side); return `<span class="cell-code ${item.predicted_side}">${m.code}</span><small>${Math.round((item.probability || 0) * 100)}%</small>${item.correct === 1 ? '<b class="hit">✓</b>' : item.correct === 0 ? '<b class="miss">×</b>' : ''}`;
}
function renderModelStats(items) {
  const names = { bayesian_frequency: '统计基线', recent_trend: '走势模型', transition_markov: '转移模型', deepseek: 'DeepSeek', qwen: 'Qwen', gpt: 'GPT', ensemble: '集成' };
  $('modelStats').innerHTML = (items.length ? items : Object.keys(names).map(name => ({ model_name: name, samples: 0, accuracy: 0, coverage: 0 }))).map(item => `<article class="metric"><span>${names[item.model_name] || item.model_name}</span><strong>${item.samples ? `${Math.round(item.accuracy * 100)}%` : '--'}</strong><small>${item.samples} 次 · 覆盖 ${Math.round((item.coverage || 0) * 100)}%</small></article>`).join('');
}
function renderRows(batches) {
  $('tableMeta').textContent = `${batches.length} 条`;
  const names = ['bayesian_frequency', 'recent_trend', 'transition_markov', 'deepseek', 'qwen', 'gpt', 'ensemble'];
  $('predictionRows').innerHTML = batches.length ? batches.map(batch => { const byName = Object.fromEntries(batch.models.map(item => [item.model_name, item])); const actual = batch.actual_winner; return `<tr><td><strong>${batch.target_round_id || `预测 #${batch.id}`}</strong><br><small>${fmt(batch.created_at)}</small></td><td>${actual === 'blue' ? codeCell(null, 'blue') : '<span class="blank-cell">待结算</span>'}</td><td>${actual === 'red' ? codeCell(null, 'red') : '<span class="blank-cell">待结算</span>'}</td>${names.map(name => `<td>${codeCell(byName[name])}</td>`).join('')}</tr>`; }).join('') : '<tr><td colspan="10" class="empty">等待上一场结算后生成下一场预测</td></tr>';
}
async function refresh() { try { const [stats, prediction, batches, modelStats] = await Promise.all([getJson(`/api/v1/stats?window=${state.window}`), getJson(`/api/v1/prediction?window=${state.window}`), getJson('/api/v1/predictions?limit=100'), getJson(`/api/v1/model-stats?window=${state.window}`)]); renderRows(batches.items); renderModelStats(modelStats.items); $('trendSummary').textContent = `${stats.counts.blue} 个 1 · ${stats.counts.red} 个 2`; $('trend').innerHTML = stats.sequence.map(item => `<span class="trend-item ${item.winner}" title="${label(item.winner)}">${meta(item.winner).code}</span>`).join(''); $('predictionCode').textContent = prediction.predicted_side ? label(prediction.predicted_side) : '--'; $('predictionCode').className = `prediction-side ${prediction.predicted_side || 'neutral'}`; $('predictionProbability').textContent = prediction.ready ? `${Math.round(prediction.probability * 100)}%` : '--'; $('predictionNote').textContent = prediction.ready ? `集成 ${prediction.sample_size} 场样本，仅作统计参考。` : '样本不足 10 场时不显示参考信号。'; $('liveText').textContent = '实时连接'; $('liveDot').className = 'dot'; $('lastUpdated').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false }); } catch (e) { $('liveText').textContent = '服务未连接'; $('liveDot').className = 'dot offline'; console.error(e); } }
document.querySelectorAll('[data-window]').forEach(button => button.addEventListener('click', () => { state.window = Number(button.dataset.window); document.querySelectorAll('[data-window]').forEach(item => item.classList.toggle('active', item === button)); refresh(); }));
const stream = new EventSource('/api/v1/stream'); stream.addEventListener('match', refresh); stream.addEventListener('prediction', refresh); stream.onerror = () => { $('liveText').textContent = '等待重连'; $('liveDot').className = 'dot offline'; }; refresh();
