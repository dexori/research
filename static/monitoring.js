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
  let currentLog = logInput.value.trim(), failures = 0, lastStart = 0;
  let points = [], segment = 0, previewPath = currentLog;
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
  function chart(allPoints, key, selector, emptySelector) {
    const points = allPoints.filter(p => Number.isFinite(p[key]));
    const canvas = find(selector), ctx = canvas.getContext('2d');
    const width = canvas.width, height = canvas.height;
    ctx.clearRect(0, 0, width, height);
    canvas.hidden = !points.length; find(emptySelector).hidden = !!points.length;
    if (!points.length) return;
    const steps = points.map(p => p.step), losses = points.map(p => p[key]);
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
      if (!i || p.step < points[i - 1].step) ctx.moveTo(x(p.step), y(p[key]));
      else ctx.lineTo(x(p.step), y(p[key]));
    }); ctx.stroke();
    ctx.fillStyle = '#267b68';
    if (points.length < 100) points.forEach(p => { ctx.beginPath(); ctx.arc(x(p.step), y(p[key]), 3, 0, Math.PI * 2); ctx.fill(); });
  }
  function training(data) {
    if (data.history) { points = data.history.points; segment = data.history.segment; }
    else {
      if (data.history_reset || previewPath !== data.log_path) points = [];
      previewPath = data.log_path;
      for (const p of data.metrics || []) {
        if (points.length && p.step < points.at(-1).step) points = [];
        const old = points.find(q => q.step === p.step);
        if (old) Object.assign(old, p); else points.push(p);
      }
      points = points.slice(-2000);
    }
    chart(points, 'loss', '[data-loss-chart]', '[data-loss-empty]');
    chart(points, 'lr', '[data-lr-chart]', '[data-lr-empty]');
    const latest = points.at(-1), step = latest?.step;
    const total = Number(root.dataset.total);
    const epochs = Number(root.dataset.epochs);
    find('[data-current-step]').textContent = `Step：${Number.isFinite(step) ? `${latest.approximate ? '≈' : ''}${step}` : '—'}`;
    find('[data-current-loss]').textContent = `Loss：${display(latest?.loss)}`;
    find('[data-current-lr]').textContent = `学习率：${display(latest?.lr)}`;
    const byStep = Number.isFinite(step) && total > 0;
    const byEpoch = !byStep && Number.isFinite(latest?.epoch) && epochs > 0;
    const percent = byStep ? Math.min(100, step / total * 100) : byEpoch ? Math.min(100, latest.epoch / epochs * 100) : null;
    find('[data-training-progress]').textContent = percent === null ? (latest ? `${step} steps` : '—') : `${percent.toFixed(1)}% · ${byEpoch ? `${latest.epoch} / ${epochs} epochs` : `${step} / ${total}`}`;
    find('[data-training-bar]').value = percent ?? 0;
    find('[data-progress-note]').textContent = byEpoch ? '按日志 epoch / 已记录计划 epoch 计算；不是训练完成或成功判定。' : total > 0 ? '按日志 step / 已记录计划步数计算；达到计划步数不等于实验成功。' : '尚未填写计划步数，只显示实际 step；在实验档案中补充即可显示百分比。';
    if (latest?.approximate) find('[data-progress-note]').textContent += ' 日志使用 K/M/B 简写，step 与百分比为近似值；未恢复被日志舍入的精度。';
    const pid = root.dataset.pid;
    if (data.processes) find('[data-run-status]').textContent = pid ? (data.processes.some(p => p.pid === pid) ? `关联 PID ${pid} 正在运行` : `未检测到 PID ${pid}，请核对日志，不代表完成`) : '未绑定 PID · 状态以日志为准';
    find('[data-metric-caption]').textContent = points.length ? `${points.length} 条已采集指标 · ${data.history ? `本地历史，运行段 ${segment + 1}` : '当前会话预览，未写入实验历史'}。曲线只来自实际日志；最多显示最近 2,000 个点，不保证覆盖接入前全部历史。` : '等待明确的 step/loss/lr 记录；不会生成模拟曲线。';
  }
  function render(data) {
    find('[data-server-title]').textContent = `${data.server.name} · ${data.server.host}:${data.server.port}`;
    find('[data-updated]').textContent = `更新于 ${new Date(data.updated_at).toLocaleTimeString()}${Number.isFinite(data.latency_ms) ? ` · 查询 ${data.latency_ms} ms` : ''} · 进程信息约每 5 秒更新`;
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
      if (root.dataset.pid && process.pid === root.dataset.pid) row.classList.add('is-associated');
      const duration = Number.isFinite(process.seconds) ? `${Math.floor(process.seconds / 3600)}h ${Math.floor(process.seconds % 3600 / 60)}m` : '—';
      [process.pid, duration, display(process.cpu, '%'), display(process.memory, '%'), process.command].forEach(v => row.append(node('td', v)));
      return row;
    });
    if (!rows.length) { const row = node('tr'), cell = node('td', '未发现当前用户的训练类进程。', 'muted'); cell.colSpan = 5; row.append(cell); rows.push(row); }
    find('[data-processes]').replaceChildren(...rows);
    const log = find('[data-log]');
    const logText = data.log || (data.log_path ? '日志暂无内容或无法读取，请查看上方提示。' : '尚未指定日志路径');
    if (log.textContent !== logText) {
      const follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
      log.textContent = logText;
      if (follow) log.scrollTop = log.scrollHeight;
    }
    find('[data-log-description]').textContent = data.log_path ? `${data.log_path}${data.truncated ? ' · 仅展示末尾片段' : ''}` : '输入训练输出的日志路径；未指定时不会扫描服务器文件。';
    training(data);
  }
  function schedule() {
    clearTimeout(timer);
    if (connected && auto.checked && !document.hidden) {
      const delay = failures ? Math.min(15000, 1000 * 2 ** Math.min(failures, 4)) : Math.max(150, 1000 - (performance.now() - lastStart));
      timer = setTimeout(refresh, delay);
    }
  }
  async function refresh() {
    if (busy || !connected) return;
    clearTimeout(timer); busy = true; lastStart = performance.now(); state(connected);
    try {
      const fields = new FormData(); fields.set('log_path', currentLog);
      fields.set('experiment', root.dataset.experiment);
      const data = await request('/api/monitor/server', fields);
      render(data); failures = 0;
      notice(`实时监控已更新${data.history ? '，曲线已记录到该实验的本地历史' : ''}；不会修改训练或自动填写实验结论。`);
    } catch (error) {
      failures++;
      notice(`${error.message} 当前显示的可能是旧快照；连接仍有效时会自动重试。`, true);
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
  try {
    const history = JSON.parse(root.dataset.history || 'null');
    if (history) {
      training({history});
      notice('已恢复本地曲线历史，等待服务器的新数据；此时不是实时快照。');
    }
  } catch { notice('本地历史暂时无法读取，请刷新页面。', true); }
  if (connected) refresh();
})();
