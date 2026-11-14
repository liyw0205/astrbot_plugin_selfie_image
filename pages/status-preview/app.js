const query = new URLSearchParams(location.search);
const state = {
  design: /^[1-5]$/.test(query.get('v')) ? query.get('v') : '1',
  scenario: ['mixed', 'healthy', 'empty', 'stress'].includes(query.get('scenario')) ? query.get('scenario') : 'mixed',
  page: Math.min(2, Math.max(1, Number.parseInt(query.get('page'), 10) || 1)),
};
const names = ['清透简报', '石墨监控', '双栏卡片', '瑞士排版', '紧凑表格'];
const report = document.getElementById('report');
const stage = document.querySelector('.stage');
const error = document.getElementById('error');
const dimensions = document.getElementById('dimensions');
let requestNumber = 0;

function pages() { return ['mixed', 'stress'].includes(state.scenario) ? 2 : 1; }
function update() {
  const currentRequest = ++requestNumber;
  state.page = Math.min(state.page, pages());
  const version = state.design.padStart(2, '0');
  const title = names[Number(state.design) - 1];
  const params = new URLSearchParams({ design: state.design, scenario: state.scenario, page: state.page });
  document.querySelectorAll('[data-design]').forEach(link => {
    const active = link.dataset.design === state.design;
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
    link.href = `?${new URLSearchParams({v: link.dataset.design, scenario: state.scenario, page: state.page})}`;
  });
  document.title = `${version} ${title} · 生图状态预览`;
  document.getElementById('image-label').textContent = `${version} / ${title}`;
  document.getElementById('current-version').textContent = `方案 ${version} / 05`;
  document.getElementById('page-number').textContent = state.page;
  document.getElementById('page-count').textContent = `${state.page} / ${pages()}`;
  document.getElementById('previous').disabled = state.page === 1;
  document.getElementById('next').disabled = state.page === pages();
  document.getElementById('scenario').value = state.scenario;
  document.getElementById('download').href = `/report.png?${params}&download=1`;
  stage.setAttribute('aria-busy', 'true');
  dimensions.textContent = '生成中';
  error.hidden = true;
  const pending = new Image();
  pending.onload = () => {
    if (currentRequest !== requestNumber) return;
    report.src = pending.src;
    report.alt = `${title}版生图模型运行状态，${document.getElementById('scenario').selectedOptions[0].textContent}，第${state.page}页`;
    report.width = pending.naturalWidth;
    report.height = pending.naturalHeight;
    dimensions.textContent = `${pending.naturalWidth} × ${pending.naturalHeight}`;
    stage.setAttribute('aria-busy', 'false');
  };
  pending.onerror = () => {
    if (currentRequest !== requestNumber) return;
    error.hidden = false;
    dimensions.textContent = '加载失败';
    stage.setAttribute('aria-busy', 'false');
  };
  pending.src = `/report.png?${params}`;
  history.replaceState(null, '', `?${new URLSearchParams({ v: state.design, scenario: state.scenario, page: state.page })}`);
}

document.querySelectorAll('[data-design]').forEach(link => link.addEventListener('click', event => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  state.design = link.dataset.design;
  update();
}));
document.getElementById('scenario').addEventListener('change', event => {
  state.scenario = event.target.value;
  state.page = 1;
  update();
});
for (const [id, delta] of [['previous', -1], ['next', 1]]) {
  document.getElementById(id).addEventListener('click', () => {state.page += delta; update();});
}
for (const id of ['fit', 'actual']) {
  document.getElementById(id).addEventListener('click', () => {
    document.getElementById('viewport').classList.toggle('actual', id === 'actual');
    document.getElementById('fit').setAttribute('aria-pressed', String(id === 'fit'));
    document.getElementById('actual').setAttribute('aria-pressed', String(id === 'actual'));
  });
}
window.addEventListener('popstate', () => location.reload());
update();
