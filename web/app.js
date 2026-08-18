const state = { window: 50 };
const $ = (id) => document.getElementById(id);
const pct = (value) => `${Math.round((value || 0) * 100)}%`;
const CODE_META = {
  blue: { code: '1', name: '\u5e7d\u9885\u8d24\u8005', color: '\u84dd' },
  red: { code: '2', name: '\u8d64\u5cf0\u5973\u7687', color: '\u7ea2' },
};
const metaOf = (winner) => CODE_META[winner] || { code: String(winner ?? '--'), name: '', color: '' };
const codeOf = (winner) => metaOf(winner).code;
const labelOf = (winner) => {
  const meta = metaOf(winner);
  return meta.name ? `${meta.code} - ${meta.name} (${meta.color})` : meta.code;
};
const fmtTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '--';

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderStats(data, prediction) {
  $('sampleSize').textContent = data.total;
  $('windowLabel').textContent = `Window ${state.window}`;
  $('code1Rate').textContent = pct(data.rates.blue);
  $('code2Rate').textContent = pct(data.rates.red);
  $('code1Count').textContent = `${data.counts.blue} rounds`;
  $('code2Count').textContent = `${data.counts.red} rounds`;
  $('currentCode').textContent = labelOf(data.current);
  $('currentStreak').textContent = data.current ? `Streak ${data.current_streak}` : 'Waiting';
  $('trendSummary').textContent = data.total ? `1: ${data.counts.blue}  |  2: ${data.counts.red}` : 'No data';
  $('trend').innerHTML = data.sequence.length
    ? data.sequence.map((item, index) => `<span class="trend-item ${item.winner === 'red' ? 'red' : 'blue'}" title="#${index + 1} | ${labelOf(item.winner)} | ${fmtTime(item.occurred_at)}">${codeOf(item.winner)}</span>`).join('')
    : '<div class="empty">Waiting for settlements</div>';
  $('predictionCode').textContent = prediction.predicted_side ? labelOf(prediction.predicted_side) : '--';
  $('predictionCode').className = `prediction-side ${prediction.predicted_side || 'neutral'}`;
  $('predictionProbability').textContent = prediction.ready ? pct(prediction.probability) : '--';
  $('code1Bar').style.width = pct(prediction.probabilities.blue);
  $('code2Bar').style.width = pct(prediction.probabilities.red);
  $('code1Prob').textContent = pct(prediction.probabilities.blue);
  $('code2Prob').textContent = pct(prediction.probabilities.red);
  $('predictionNote').textContent = prediction.ready ? `Based on ${prediction.sample_size} rounds; statistics only.` : 'Need at least 10 rounds.';
}

function renderRows(items) {
  $('tableMeta').textContent = `${items.length} records`;
  $('matchRows').innerHTML = items.length
    ? items.map(item => `<tr><td>${item.round_id || `Local #${item.id}`}</td><td class="winner ${item.winner === 'red' ? 'red' : 'blue'}">${labelOf(item.winner)}</td><td>${fmtTime(item.occurred_at)}</td><td>${Math.round(item.confidence * 100)}%</td><td>${item.source}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">Waiting for collector</td></tr>';
}

async function refresh() {
  try {
    const [stats, prediction, results] = await Promise.all([
      getJson(`/api/v1/stats?window=${state.window}`),
      getJson(`/api/v1/prediction?window=${state.window}`),
      getJson('/api/v1/results?limit=100'),
    ]);
    renderStats(stats, prediction);
    renderRows(results.items);
    $('lastUpdated').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    $('liveText').textContent = 'Live';
    $('liveDot').className = 'dot';
  } catch (error) {
    $('liveText').textContent = 'Offline';
    $('liveDot').className = 'dot offline';
    console.error(error);
  }
}

document.querySelectorAll('[data-window]').forEach((button) => button.addEventListener('click', () => {
  state.window = Number(button.dataset.window);
  document.querySelectorAll('[data-window]').forEach((item) => item.classList.toggle('active', item === button));
  refresh();
}));

const stream = new EventSource('/api/v1/stream');
stream.addEventListener('match', () => {
  refresh();
  $('matchRows').classList.add('fresh');
  setTimeout(() => $('matchRows').classList.remove('fresh'), 800);
});
stream.onerror = () => { $('liveText').textContent = 'Reconnecting'; $('liveDot').className = 'dot offline'; };
refresh();
