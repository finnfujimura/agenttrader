const app = document.getElementById("app");

const fetchJson = async (url) => {
  const res = await fetch(url);
  return res.json();
};

const fmt = (n) => (n == null ? "-" : Number(n).toFixed(2));
const fmtTs = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "-");

const drawChart = (points) => {
  if (!points || points.length === 0) return "<div class='card'>No equity curve data</div>";
  const w = 900;
  const h = 220;
  const xs = points.map((p) => p.timestamp);
  const ys = points.map((p) => p.value);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  const poly = points
    .map((p) => {
      const x = ((p.timestamp - minX) / spanX) * (w - 20) + 10;
      const y = h - (((p.value - minY) / spanY) * (h - 20) + 10);
      return `${x},${y}`;
    })
    .join(" ");
  return `<svg class='chart' viewBox='0 0 ${w} ${h}'><polyline fill='none' stroke='#0b6e4f' stroke-width='3' points='${poly}'/></svg>`;
};

const routes = {
  async "/"() {
    const [status, portfolios, backtests] = await Promise.all([
      fetchJson("/api/status"),
      fetchJson("/api/portfolios"),
      fetchJson("/api/backtests"),
    ]);
    const active = portfolios.portfolios.filter((p) => p.status === "running");
    const unrealized = active.reduce((acc, p) => acc + (p.unrealized_pnl || 0), 0);
    app.innerHTML = `
      <div class="grid">
        <div class="card"><div class="label">Cached Markets</div><div class="value">${status.markets_cached}</div></div>
        <div class="card"><div class="label">Active Paper Trades</div><div class="value">${active.length}</div></div>
        <div class="card"><div class="label">Recent Backtests</div><div class="value">${backtests.runs.length}</div></div>
        <div class="card"><div class="label">Unrealized PnL</div><div class="value">${fmt(unrealized)}</div></div>
      </div>`;
  },

  async "/paper"() {
    const data = await fetchJson("/api/portfolios");
    app.innerHTML = `<table><thead><tr><th>ID</th><th>Strategy</th><th>Status</th><th>Started</th><th>Cash</th><th>Reloads</th></tr></thead>
      <tbody>${data.portfolios
        .map(
          (p) => `<tr>
            <td><a href="#/paper/${p.id}" class="mono">${p.id.slice(0, 8)}</a></td>
            <td>${p.strategy_path.split("/").pop()}</td>
            <td>${p.status}</td>
            <td>${fmtTs(p.started_at)}</td>
            <td>${fmt(p.cash_balance)}</td>
            <td>${p.reload_count || 0}</td>
          </tr>`
        )
        .join("")}</tbody></table>`;
  },

  async "/backtests"() {
    const data = await fetchJson("/api/backtests");
    app.innerHTML = `<table><thead><tr><th>ID</th><th>Strategy</th><th>Range</th><th>Status</th><th>Return %</th><th>Sharpe</th></tr></thead>
      <tbody>${data.runs
        .map(
          (r) => `<tr>
            <td><a href="#/backtests/${r.id}" class="mono">${r.id.slice(0, 8)}</a></td>
            <td>${r.strategy_path.split("/").pop()}</td>
            <td>${r.start_date} to ${r.end_date}</td>
            <td>${r.status}</td>
            <td>${fmt(r.metrics?.total_return_pct)}</td>
            <td>${fmt(r.metrics?.sharpe_ratio)}</td>
          </tr>`
        )
        .join("")}</tbody></table>`;
  },

  async "/markets"() {
    const data = await fetchJson("/api/markets?limit=1000");
    const rows = data.markets;
    app.innerHTML = `<div style="margin-bottom:10px"><input id="search" placeholder="Search markets..." /></div><div id="market-table"></div>`;
    const table = document.getElementById("market-table");
    const render = (items) => {
      table.innerHTML = `<table><thead><tr><th>ID</th><th>Platform</th><th>Title</th><th>Category</th><th>Volume</th><th>Price</th></tr></thead><tbody>${items
        .map(
          (m) => `<tr>
            <td class="mono">${m.id.slice(0, 12)}</td>
            <td>${m.platform}</td>
            <td>${m.title}</td>
            <td>${m.category || ""}</td>
            <td>${fmt(m.volume)}</td>
            <td>${fmt(m.price)}</td>
          </tr>`
        )
        .join("")}</tbody></table>`;
    };
    render(rows);
    document.getElementById("search").oninput = (e) => {
      const q = e.target.value.toLowerCase();
      render(rows.filter((m) => m.title.toLowerCase().includes(q) || m.id.toLowerCase().includes(q)));
    };
  },
};

async function renderRoute() {
  const hash = location.hash.slice(1) || "/";

  const mPaper = hash.match(/^\/paper\/([^/]+)$/);
  if (mPaper) {
    const id = mPaper[1];
    const [detail, logs] = await Promise.all([
      fetchJson(`/api/portfolios/${id}`),
      fetchJson(`/api/portfolios/${id}/logs`),
    ]);
    const p = detail.portfolio;
    app.innerHTML = `
      <h2>Portfolio ${id.slice(0, 8)}</h2>
      <div class="grid">
        <div class="card"><div class="label">Status</div><div class="value">${p.status}</div></div>
        <div class="card"><div class="label">Cash</div><div class="value">${fmt(p.cash_balance)}</div></div>
        <div class="card"><div class="label">Reloads</div><div class="value">${p.reload_count || 0}</div></div>
      </div>
      <h3>Positions</h3>
      <table><thead><tr><th>Market</th><th>Side</th><th>Contracts</th><th>Avg Cost</th><th>Current</th><th>uPnL</th></tr></thead><tbody>${p.positions
        .map(
          (x) => `<tr><td>${x.market_title}</td><td>${x.side}</td><td>${fmt(x.contracts)}</td><td>${fmt(x.avg_cost)}</td><td>${fmt(x.current_price)}</td><td>${fmt(x.unrealized_pnl)}</td></tr>`
        )
        .join("")}</tbody></table>
      <h3>Trades</h3>
      <table><thead><tr><th>Action</th><th>Market</th><th>Contracts</th><th>Price</th><th>PnL</th><th>Time</th></tr></thead><tbody>${p.trades
        .map(
          (t) => `<tr><td>${t.action}</td><td class="mono">${t.market_id.slice(0, 12)}</td><td>${fmt(t.contracts)}</td><td>${fmt(t.price)}</td><td>${fmt(t.pnl)}</td><td>${fmtTs(t.filled_at)}</td></tr>`
        )
        .join("")}</tbody></table>
      <h3>Logs</h3>
      <div class="log">${logs.logs.map((l) => `[${fmtTs(l.timestamp)}] ${l.message}`).join("\n")}</div>
    `;
    setTimeout(renderRoute, 10000);
    return;
  }

  const mBacktest = hash.match(/^\/backtests\/([^/]+)$/);
  if (mBacktest) {
    const id = mBacktest[1];
    const data = await fetchJson(`/api/backtests/${id}`);
    app.innerHTML = `
      <h2>Backtest ${id.slice(0, 8)}</h2>
      <div class="grid">
        <div class="card"><div class="label">Final Value</div><div class="value">${fmt(data.final_value)}</div></div>
        <div class="card"><div class="label">Return %</div><div class="value">${fmt(data.metrics?.total_return_pct)}</div></div>
        <div class="card"><div class="label">Sharpe</div><div class="value">${fmt(data.metrics?.sharpe_ratio)}</div></div>
        <div class="card"><div class="label">Max DD %</div><div class="value">${fmt(data.metrics?.max_drawdown_pct)}</div></div>
      </div>
      <h3>Equity Curve</h3>
      ${drawChart(data.equity_curve || [])}
      <h3>Trades</h3>
      <table><thead><tr><th>Action</th><th>Market</th><th>Contracts</th><th>Price</th><th>PnL</th><th>Time</th></tr></thead><tbody>${(data.trades || [])
        .slice(0, 300)
        .map(
          (t) => `<tr><td>${t.action}</td><td>${(t.market_title || t.market_id || "").slice(0, 40)}</td><td>${fmt(t.contracts)}</td><td>${fmt(t.price)}</td><td>${fmt(t.pnl)}</td><td>${fmtTs(t.filled_at)}</td></tr>`
        )
        .join("")}</tbody></table>
    `;
    return;
  }

  const route = routes[hash] || routes["/"];
  await route();
}

window.addEventListener("hashchange", renderRoute);
renderRoute();
setInterval(() => {
  if (["#/", "#/paper", "#/backtests", "#/markets"].includes(location.hash) || location.hash === "") {
    renderRoute();
  }
}, 10000);
