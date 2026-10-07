/* Local SSH monitoring: real snapshots, no simulated progress or remote writes. */
(() => {
  const root = document.querySelector('#monitor');
  if (!root) return;
  const find = selector => root.querySelector(selector);
  const status = find('[data-monitor-status]');
  const auto = find('[data-auto-refresh]');
  const password = find('[name=password]');
  const logInput = find('[name=log_path]');
  let connected = root.dataset.connected === '1', busy = false, timer;
  let currentLog = '', failures = 0;
  const node = (tag, text, cls) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = String(text);
    if (cls) el.className = cls;
    return el;
  };
  const display = (value, suffix = '') => Number.isFinite(value) ? `${value}${suffix}` : '—';
  function state(value) {
    connected = value;
    document.querySelector('[data-connection]').textContent = value ? '已连接' : '未连接';
    find('[data-refresh]').disabled = !value || busy;
    find('[data-disconnect]').disabled = !value || busy;
  }
  function notice(message, error = false) {
    status.textContent = message;
    status.classList.toggle('error', error);
  }
  async function request(path, fields) {
    fields.set('csrf', root.dataset.csrf);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
      const response = await fetch(path, {method: 'POST', body: fields, signal: controller.signal});
      if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
        state(false);
        throw new Error('登录或页面已过期，请刷新后重新登录。');
      }
      const data = await response.json();
      if (!response.ok) {
        if (data.connected === false) state(false);
        throw new Error(data.error || '查询未完成，请稍后重试。');
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('本次查询超时，已保留上次快照。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function chart(points) {
    const section = find('[data-metrics]');
    section.hidden = !points.length;
    if (!points.length) return;
    const canvas = find('[data-loss-chart]'), ctx = canvas.getContext('2d');
    const width = canvas.width, height = canvas.height;
    ctx.clearRect(0, 0, width, height);
    const steps = points.map(p => p.step), losses = points.map(p => p.loss);
    const xmin = Math.min(...steps), xmax = Math.max(...steps), ymin = Math.min(...losses), ymax = Math.max(...losses);
    const x = value => 60 + (value - xmin) / (xmax - xmin || 1) * (width - 90);
    const y = value => height - 36 - (value - ymin) / (ymax - ymin || 1) * (height - 62);
    ctx.strokeStyle = '#e2e8e5'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(60, 18); ctx.lineTo(60, height - 36); ctx.lineTo(width - 25, height - 36); ctx.stroke();
    ctx.fillStyle = '#64757d'; ctx.font = '13px sans-serif';
    ctx.fillText(ymax.toPrecision(4), 4, 26); ctx.fillText(ymin.toPrecision(4), 4, height - 36);
    ctx.fillText(String(xmin), 60, height - 10); ctx.fillText(String(xmax), width - 80, height - 10);
    ctx.strokeStyle = '#267b68'; ctx.lineWidth = 2; ctx.beginPath();
    points.forEach((p, i) => {
      if (!i || p.step < points[i - 1].step) ctx.moveTo(x(p.step), y(p.loss));
      else ctx.lineTo(x(p.step), y(p.loss));
    }); ctx.stroke();
    ctx.fillStyle = '#267b68';
    points.forEach(p => { ctx.beginPath(); ctx.arc(x(p.step), y(p.loss), 3, 0, Math.PI * 2); ctx.fill(); });
    const last = points.at(-1);
    find('[data-metric-caption]').textContent = `${points.length} 条明确的 step/loss 记录；最新 step ${last.step}，loss ${last.loss}。仅来自当前日志片段，不代表完整训练历史。`;
  }
  function render(data) {
    find('[data-server-title]').textContent = `${data.server.name} · ${data.server.host}:${data.server.port}`;
    find('[data-updated]').textContent = `更新于 ${new Date(data.updated_at).toLocaleTimeString()} · ${data.uptime || '系统负载不可用'}`;
    find('[data-warnings]').replaceChildren(...data.warnings.map(w => node('p', w)));
    const cards = data.gpus.map(gpu => {
      const card = node('div', undefined, 'monitor-gpu');
      card.append(node('h3', `GPU ${gpu.index} · ${gpu.name}`), node('strong', display(gpu.utilization, '%')));
      card.append(node('p', `显存 ${display(gpu.used)} / ${display(gpu.total)} MiB`));
      const progress = node('progress'); progress.max = 100;
      if (Number.isFinite(gpu.used) && gpu.total > 0) progress.value = Math.min(100, gpu.used / gpu.total * 100);
      card.append(progress, node('p', `温度 ${display(gpu.temperature, ' °C')} · 功耗 ${display(gpu.power, ' W')}`));
      return card;
    });
    find('[data-gpus]').replaceChildren(...(cards.length ? cards : [node('p', '未检测到可读取的 GPU。', 'muted')]));
    const rows = data.processes.map(process => {
      const row = node('tr');
      const duration = Number.isFinite(process.seconds) ? `${Math.floor(process.seconds / 3600)}h ${Math.floor(process.seconds % 3600 / 60)}m` : '—';
      [process.pid, duration, display(process.cpu, '%'), display(process.memory, '%'), process.command].forEach(v => row.append(node('td', v)));
      return row;
    });
    if (!rows.length) { const row = node('tr'), cell = node('td', '未发现当前用户的训练类进程。', 'muted'); cell.colSpan = 5; row.append(cell); rows.push(row); }
    find('[data-processes]').replaceChildren(...rows);
    find('[data-log]').textContent = data.log || (data.log_path ? '日志暂无内容或无法读取，请查看上方提示。' : '尚未指定日志路径');
    find('[data-log-description]').textContent = data.log_path ? `${data.log_path}${data.truncated ? ' · 仅展示末尾片段' : ''}` : '输入训练输出的日志路径；未指定时不会扫描服务器文件。';
    chart(data.metrics);
  }
  function schedule() {
    clearTimeout(timer);
    if (connected && auto.checked && !document.hidden && failures < 3) timer = setTimeout(refresh, 15000);
  }
  async function refresh() {
    if (busy || !connected) return;
    clearTimeout(timer); busy = true; state(connected);
    notice('正在读取服务器快照…');
    try {
      const fields = new FormData(); fields.set('log_path', currentLog);
      const data = await request('/api/monitor/server', fields);
      render(data); failures = 0;
      notice('只读监控已更新；不会修改训练或自动保存实验结论。');
    } catch (error) {
      failures++;
      notice(`${error.message} 当前显示的可能是旧快照。${failures >= 3 ? '连续查询失败，自动刷新已暂停；点击刷新重试。' : ''}`, true);
    } finally { busy = false; state(connected); schedule(); }
  }
  find('[data-connect-form]').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    clearTimeout(timer); busy = true;
    const button = event.submitter; button.disabled = true;
    notice('正在校验 SSH 主机并登录…');
    try {
      const fields = new FormData(event.target);
      password.value = ''; // Never retain the secret in the DOM or browser storage.
      await request('/api/monitor/connect', fields);
      failures = 0; state(true); notice('服务器连接成功。');
    } catch (error) { notice(error.message, true); }
    finally { busy = false; button.disabled = false; state(connected); }
    if (connected) refresh();
  });
  find('[data-disconnect]').addEventListener('click', async () => {
    if (busy) return;
    busy = true; clearTimeout(timer);
    try {
      await request('/api/monitor/disconnect', new FormData());
      state(false); notice('已断开，内存中的登录凭据已清除。快照仅供回看。');
    } catch (error) { notice(error.message, true); }
    finally { busy = false; state(connected); schedule(); }
  });
  find('[data-refresh]').addEventListener('click', () => { failures = 0; refresh(); });
  find('[data-log-form]').addEventListener('submit', event => {
    event.preventDefault(); currentLog = logInput.value.trim(); failures = 0;
    if (!connected) notice('请先连接服务器，再读取日志。', true); else refresh();
  });
  auto.addEventListener('change', schedule);
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else schedule(); });
  window.addEventListener('pagehide', () => clearTimeout(timer));
  state(connected);
  if (connected) refresh();
})();
