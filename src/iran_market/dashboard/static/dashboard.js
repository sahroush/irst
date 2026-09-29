(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { timeframe: '', instruments: [], chart: null, rsiChart: null, instrumentId: null, poll: null, queryTimer: null, requestId: 0, chartPoints: [], rsiValues: [], rsiVisible: false, syncingRange: false };
  const els = {
    active: $('active'), building: $('building'), signalsCount: $('signals-count'), snapshot: $('snapshot'),
    health: $('health-badge'), search: $('search'), refresh: $('refresh'), instrumentRows: $('instrument-rows'),
    signalRows: $('signal-rows'), instrumentTotal: $('instrument-total'), error: $('page-error'),
    status: $('page-status'), updated: $('updated-at'), detail: $('detail'), detailTitle: $('detail-title'),
    timeframe: $('timeframe'), closeDetail: $('close-detail'), chart: $('market-chart'),
    chartSummary: $('chart-summary'), chartEmpty: $('chart-empty'), marketState: $('market-state'), candleAge: $('candle-age'), rsiToggle: $('rsi-toggle'), rsiLegend: $('rsi-legend'), rsiPanel: $('rsi-panel'), rsiChart: $('rsi-chart'),
  };
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const num = (value) => Number.isFinite(Number(value)) ? Number(value).toLocaleString() : '—';
  const dateSeconds = (value) => {
    const ms = typeof value === 'number' ? value * 1000 : Date.parse(value);
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
  };
  const ago = (value) => {
    const ms = Date.parse(value);
    if (!Number.isFinite(ms)) return '—';
    const seconds = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return new Date(ms).toLocaleString();
  };
  async function get(url) {
    const response = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`.trim());
    return response.json();
  }
  function setError(message = '') {
    els.error.hidden = !message;
    els.error.textContent = message;
  }
  function updateHealth(status) {
    const value = String(status || 'UNKNOWN').toUpperCase();
    els.health.textContent = value;
    els.health.className = `health-badge ${value === 'HEALTHY' ? 'health-healthy' : value === 'DEGRADED' ? 'health-degraded' : value === 'UNHEALTHY' ? 'health-unhealthy' : ''}`;
  }
  function renderInstruments(rows) {
    state.instruments = Array.isArray(rows) ? rows : [];
    els.instrumentTotal.textContent = `${state.instruments.length} shown`;
    els.instrumentRows.innerHTML = state.instruments.map((item) => {
      const label = item.symbol || item.isin || item.id;
      return `<tr class="instrument-row ${state.instrumentId === item.id ? 'selected' : ''}">
        <td><button class="instrument-select" type="button" data-instrument-id="${esc(item.id)}" aria-pressed="${state.instrumentId === item.id}" aria-label="Open chart for ${esc(label)}"><span class="instrument-name">${esc(label)}</span>${item.name && item.name !== label ? `<span class="sub-name">${esc(item.name)}</span>` : ''}</button></td>
        <td>${esc(num(item.close))}</td><td>${esc(ago(item.updated_at))}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="3" class="empty">No matching instruments.</td></tr>';
  }
  function renderSignals(rows) {
    const signals = Array.isArray(rows) ? rows : [];
    els.signalRows.innerHTML = signals.map((signal) => {
      const up = signal.direction === 'bullish';
      const label = signal.symbol || signal.instrument_id || '—';
      return `<tr class="${signal.instrument_id ? 'instrument-row' : ''}">
        <td>${signal.instrument_id ? `<button class="instrument-select" type="button" data-instrument-id="${esc(signal.instrument_id)}" aria-label="Open chart for ${esc(label)}">${esc(label)}</button>` : esc(label)}</td><td class="${up ? 'direction-up' : 'direction-down'}">${up ? '▲' : '▼'} ${esc(signal.direction || '—')}</td><td>${esc(ago(signal.candle_time))}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="3" class="empty">No signals yet.</td></tr>';
  }
  function markSelection() {
    els.instrumentRows.querySelectorAll('.instrument-select').forEach((button) => {
      const selected = button.dataset.instrumentId === state.instrumentId;
      button.closest('tr').classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }
  async function loadDashboard() {
    if (!state.timeframe) return;
    try {
      setError();
      const timeframe = encodeURIComponent(state.timeframe);
      const healthPromise = get('/api/health');
      const signalsPromise = get(`/api/signals?limit=30&timeframe=${timeframe}`);
      const instrumentsPromise = get(`/api/instruments?limit=100&timeframe=${timeframe}&q=${encodeURIComponent(els.search.value.trim())}`)
        .then((instruments) => {
          renderInstruments(instruments);
          if (!state.instrumentId && instruments.length) openInstrument(instruments[0].id);
          else if (state.instrumentId && state.chart) openInstrument(state.instrumentId);
          markSelection();
          return instruments;
        });
      const [health, signals] = await Promise.all([healthPromise, signalsPromise, instrumentsPromise]);
      els.active.textContent = num(health.active_instruments);
      els.building.textContent = num(health.building_candles);
      els.signalsCount.textContent = num(health.signals);
      els.snapshot.textContent = ago(health.last_snapshot_time);
      els.marketState.textContent = health.market_state || '—';
      updateHealth(health.status);
      renderSignals(signals);
      els.updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      els.status.textContent = 'Dashboard updated';
    } catch (error) {
      setError(`Dashboard data unavailable: ${error.message}`);
      els.status.textContent = 'Dashboard update failed';
    }
  }
  function candleTime(candle) {
    return dateSeconds(candle.start_time ?? candle.time ?? candle.timestamp);
  }
  function normalizedCandles(candles) {
    return (Array.isArray(candles) ? candles : []).map((candle) => ({
      time: candleTime(candle), open: Number(candle.open), high: Number(candle.high),
      low: Number(candle.low), close: Number(candle.close), volume: Number(candle.volume ?? 0),
      status: candle.status,
    })).filter((item) => item.time !== null && [item.open, item.high, item.low, item.close].every(Number.isFinite))
      .sort((a, b) => a.time - b.time).filter((item, index, all) => index === 0 || item.time !== all[index - 1].time);
  }
  function createChart() {
    if (state.chart || !window.LightweightCharts) return state.chart;
    const charts = window.LightweightCharts;
    state.chart = charts.createChart(els.chart, {
      autoSize: true,
      layout: { background: { type: 'solid', color: '#101722' }, textColor: '#a5b3c4', fontFamily: 'Inter, system-ui, sans-serif' },
      grid: { vertLines: { color: '#1c2837' }, horzLines: { color: '#1c2837' } },
      rightPriceScale: { borderColor: '#2a394d', scaleMargins: { top: 0.08, bottom: 0.28 } },
      timeScale: {
        borderColor: '#2a394d', timeVisible: true, secondsVisible: false, rightOffset: 3,
        tickMarkFormatter: (time) => new Date(Number(time) * 1000).toLocaleString('en-GB', {
          timeZone: 'Asia/Tehran', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
        }),
      },
      crosshair: { mode: charts.CrosshairMode?.Normal ?? 0 },
      localization: {
        priceFormatter: (price) => Number(price).toLocaleString(),
        timeFormatter: (time) => new Date(Number(time) * 1000).toLocaleString('en-GB', {
          timeZone: 'Asia/Tehran', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
        }),
      },
    });
    const candles = state.chart.addCandlestickSeries({
      upColor: '#39c992', downColor: '#f0787b', borderUpColor: '#39c992', borderDownColor: '#f0787b',
      wickUpColor: '#39c992', wickDownColor: '#f0787b', priceLineVisible: true,
    });
    const volume = state.chart.addHistogramSeries({
      priceFormat: { type: 'volume' }, priceScaleId: '',
      scaleMargins: { top: 0.82, bottom: 0 }, lastValueVisible: false, priceLineVisible: false,
    });
    const bullTrend = state.chart.addLineSeries({ color: '#39c992', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const bearTrend = state.chart.addLineSeries({ color: '#f0787b', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    state.chart.__series = { candles, volume, bullTrend, bearTrend };
    state.chart.timeScale().subscribeVisibleTimeRangeChange(() => syncChartRanges(state.chart.timeScale(), state.rsiChart?.timeScale()));
    state.chart.subscribeCrosshairMove((param) => {
      const candle = param.seriesData.get(candles);
      els.chartSummary.textContent = candle
        ? `O ${num(candle.open)}  H ${num(candle.high)}  L ${num(candle.low)}  C ${num(candle.close)}  ·  Vol ${num(param.seriesData.get(volume)?.value)}`
        : state.chartPoints.length ? `${state.chartPoints.length} candles · hover chart for OHLCV` : 'No candle data';
    });
    return state.chart;
  }
  function createRSIChart() {
    if (state.rsiChart || !window.LightweightCharts) return state.rsiChart;
    const charts = window.LightweightCharts;
    state.rsiChart = charts.createChart(els.rsiChart, {
      autoSize: true,
      layout: { background: { type: 'solid', color: '#141e2b' }, textColor: '#8c9bad', fontFamily: 'Inter, system-ui, sans-serif' },
      grid: { vertLines: { color: '#243143' }, horzLines: { color: '#243143' } },
      rightPriceScale: { borderColor: '#243143', scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { visible: false, borderVisible: false, rightOffset: 3 },
      crosshair: { mode: charts.CrosshairMode?.Normal ?? 0 },
    });
    const line = state.rsiChart.addLineSeries({
      color: '#76a9ff', lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    });
    for (const value of [30, 50, 70]) {
      line.createPriceLine({ price: value, color: value === 50 ? '#8c9bad' : '#53657d', lineWidth: 1, lineStyle: 2, axisLabelVisible: value === 50, title: String(value) });
    }
    state.rsiChart.__series = { line };
    state.rsiChart.timeScale().subscribeVisibleTimeRangeChange(() => syncChartRanges(state.rsiChart.timeScale(), state.chart?.timeScale()));
    return state.rsiChart;
  }
  function setRSIData(values) {
    if (!state.rsiChart) return;
    state.rsiChart.__series.line.setData(rsiPoints(values, state.chartPoints));
  }
  function syncChartRanges(source, target) {
    if (!source || !target || state.syncingRange) return;
    const range = source.getVisibleRange();
    if (!range) return;
    state.syncingRange = true;
    try { target.setVisibleRange(range); }
    finally { state.syncingRange = false; }
  }
  function supertrendPoints(values, candles) {
    if (!Array.isArray(values)) return [];
    return values.flatMap((value, index) => {
      if (value == null) return [];
      const price = value && typeof value === 'object' ? Number(value.value ?? value.supertrend) : Number(value);
      const time = value && typeof value === 'object' && value.time != null ? dateSeconds(value.time) : candles[index]?.time;
      if (!Number.isFinite(price) || time === null || time === undefined || !candles[index]) return [];
      const bullish = candles[index].close >= price;
      return [{ time, value: price, bullish }];
    }).sort((a, b) => a.time - b.time);
  }
  function normalizeMarkers(markers) {
    if (!Array.isArray(markers)) return [];
    return markers.flatMap((marker) => {
      const time = dateSeconds(marker.time ?? marker.candle_time);
      if (time === null) return [];
      const bullish = marker.direction === 'bullish' || marker.position === 'belowBar';
      return [{ time, position: bullish ? 'belowBar' : 'aboveBar', color: bullish ? '#39c992' : '#f0787b', shape: bullish ? 'arrowUp' : 'arrowDown', text: marker.text || (bullish ? 'Buy' : 'Sell') }];
    }).sort((a, b) => a.time - b.time);
  }
  function rsiPoints(values, candles) {
    if (!Array.isArray(values)) return [];
    return values.flatMap((value, index) => {
      if (value == null || !candles[index] || !Number.isFinite(Number(value))) return [];
      return [{ time: candles[index].time, value: Number(value) }];
    });
  }
  async function openInstrument(id) {
    if (!id) return;
    setError();
    const request = ++state.requestId;
    state.instrumentId = id;
    markSelection();
    els.detail.hidden = false;
    els.chartEmpty.hidden = true;
    els.chartSummary.textContent = 'Loading chart…';
    try {
      const tf = els.timeframe.value || state.timeframe;
      const url = `/api/instruments/${encodeURIComponent(id)}${tf ? `?timeframe=${encodeURIComponent(tf)}` : ''}`;
      const data = await get(url);
      if (request !== state.requestId) return;
      const inst = data.instrument || {};
      els.detailTitle.textContent = `${inst.symbol || inst.isin || id} · ${tf || 'default timeframe'}`;
      const points = normalizedCandles(data.candles);
      if (!points.length) {
        els.chartSummary.textContent = 'No candle data';
        els.chartEmpty.hidden = false;
        els.candleAge.textContent = 'Candle: —';
        state.chartPoints = [];
        state.rsiValues = [];
        if (state.chart) state.chart.__series.candles.setData([]), state.chart.__series.volume.setData([]), state.chart.__series.bullTrend.setData([]), state.chart.__series.bearTrend.setData([]);
        if (state.rsiChart) setRSIData([]);
        return;
      }
      const chart = createChart();
      if (!chart) throw new Error('Lightweight Charts failed to load');
      const { candles, volume, bullTrend, bearTrend } = chart.__series;
      state.chartPoints = points;
      state.rsiValues = data.rsi_series || [];
      candles.setData(points.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      volume.setData(points.map((item) => ({ time: item.time, value: item.volume, color: item.close >= item.open ? 'rgba(57,201,146,.42)' : 'rgba(240,120,123,.42)' })));
      const trendPoints = supertrendPoints(data.supertrend_series, points);
      bullTrend.setData(trendPoints.map((point) => point.bullish ? { time: point.time, value: point.value } : { time: point.time }));
      bearTrend.setData(trendPoints.map((point) => point.bullish ? { time: point.time } : { time: point.time, value: point.value }));
      if (state.rsiChart) setRSIData(state.rsiValues);
      const markers = normalizeMarkers(data.markers);
      candles.setMarkers(markers.filter((marker) => points.some((point) => point.time === marker.time)));
      chart.timeScale().fitContent();
      if (state.rsiChart) {
        const range = chart.timeScale().getVisibleRange();
        if (range) state.rsiChart.timeScale().setVisibleRange(range);
      }
      const current = data.supertrend || {};
      els.chartSummary.textContent = `${points.length} ${tf || 'timeframe'} candles · last close ${num(points[points.length - 1].close)} · ${points[points.length - 1].status || 'unknown'} · Supertrend ${current.state || '—'}${current.value == null ? '' : ` at ${num(current.value)}`}`;
      els.candleAge.textContent = `Candle: ${ago(data.candles[data.candles.length - 1].start_time)}`;
      els.chartEmpty.hidden = true;
      els.status.textContent = `Chart loaded for ${inst.symbol || inst.isin || id}`;
    } catch (error) {
      if (request !== state.requestId) return;
      els.chartSummary.textContent = '';
      setError(`Chart unavailable: ${error.message}`);
      els.status.textContent = 'Chart load failed';
    }
  }
  async function loadConfig() {
    try {
      const config = await get('/api/config');
      const options = Array.isArray(config.available_timeframes) ? config.available_timeframes : [];
      state.timeframe = options.includes('1d') ? '1d' : (options.includes(config.scanner_timeframe) ? config.scanner_timeframe : (options[0] || ''));
      els.timeframe.innerHTML = options.map((timeframe) => `<option value="${esc(timeframe)}">${esc(timeframe)}</option>`).join('');
      els.timeframe.value = state.timeframe;
      return Boolean(state.timeframe);
    } catch (error) {
      state.timeframe = '';
      els.timeframe.innerHTML = '<option value="">Unavailable</option>';
      setError(`Dashboard config unavailable: ${error.message}`);
      return false;
    }
  }
  async function init() {
    if (await loadConfig()) await loadDashboard();
    state.poll = window.setInterval(loadDashboard, 30000);
  }

  for (const body of [els.instrumentRows, els.signalRows]) body.addEventListener('click', (event) => {
    const button = event.target.closest('.instrument-select');
    if (button) openInstrument(button.dataset.instrumentId);
  });
  els.search.addEventListener('input', () => {
    window.clearTimeout(state.queryTimer);
    state.queryTimer = window.setTimeout(loadDashboard, 220);
  });
  els.refresh.addEventListener('click', loadDashboard);
  els.rsiToggle.addEventListener('click', () => {
    state.rsiVisible = !state.rsiVisible;
    els.rsiToggle.setAttribute('aria-pressed', String(state.rsiVisible));
    els.rsiToggle.textContent = `${state.rsiVisible ? 'Hide' : 'Show'} RSI (14)`;
    els.rsiLegend.hidden = !state.rsiVisible;
    els.rsiPanel.hidden = !state.rsiVisible;
    if (state.rsiVisible) {
      createRSIChart();
      setRSIData(state.rsiValues);
      const range = state.chart?.timeScale().getVisibleRange();
      if (range) state.rsiChart.timeScale().setVisibleRange(range);
    }
  });
  els.closeDetail.addEventListener('click', () => {
    state.requestId++;
    state.instrumentId = null;
    state.chartPoints = [];
    state.rsiValues = [];
    markSelection();
    els.detailTitle.textContent = 'Select instrument';
    els.chartSummary.textContent = 'Choose an instrument to load chart.';
    els.candleAge.textContent = 'Candle: —';
    if (state.chart) {
      state.chart.__series.candles.setData([]);
      state.chart.__series.volume.setData([]);
      state.chart.__series.bullTrend.setData([]);
      state.chart.__series.bearTrend.setData([]);
    }
    if (state.rsiChart) setRSIData([]);
  });
  els.timeframe.addEventListener('change', () => {
    state.timeframe = els.timeframe.value;
    loadDashboard();
    if (state.instrumentId && !state.chart) openInstrument(state.instrumentId);
  });
  document.querySelectorAll('[data-range]').forEach((button) => button.addEventListener('click', () => {
    if (!state.chart || !state.chartPoints.length) return;
    const timeframe = els.timeframe.value || state.timeframe;
    const amount = Number((timeframe.match(/^\d+/) || ['30'])[0]);
    const barsPerSession = timeframe.endsWith('d') ? 1 : timeframe.endsWith('h') ? Math.ceil(3.5 / amount) : Math.ceil(210 / amount);
    const count = button.dataset.range === '1D' ? barsPerSession : button.dataset.range === '1W' ? barsPerSession * 6 : state.chartPoints.length;
    document.querySelectorAll('[data-range]').forEach((item) => item.classList.toggle('active', item === button));
    state.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, state.chartPoints.length - count), to: state.chartPoints.length - 1 });
  }));
  window.addEventListener('beforeunload', () => window.clearInterval(state.poll), { once: true });
  init();
})();
