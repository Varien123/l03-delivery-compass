(() => {
  'use strict';

  const DATA = JSON.parse(document.getElementById('app-data').textContent);
  const DAY = 86400000;
  const CNY_START = '2027-02-04';
  const CNY_END = '2027-02-18';
  const STORAGE_KEY = 'l03-compass-local-v1';
  const DEFAULTS = { output: 22000, holiday: 0, export: 0, color: 0 };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const pad2 = (v) => String(v).padStart(2, '0');

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function toUTC(iso) {
    const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
    return Date.UTC(y, m - 1, d);
  }
  function fromUTC(ts) {
    const d = new Date(ts);
    return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
  }
  function addDays(iso, n) {
    return fromUTC(toUTC(iso) + n * DAY);
  }
  function daysBetween(a, b) {
    return Math.round((toUTC(b) - toUTC(a)) / DAY);
  }
  function weekday(iso) {
    return new Date(toUTC(iso)).getUTCDay();
  }
  function weekdayCN(iso) {
    return '日一二三四五六'[weekday(iso)];
  }
  function todayISO() {
    try {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Shanghai',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).format(new Date());
    } catch (e) {
      return fromUTC(Date.now());
    }
  }
  function fmtMD(iso) {
    const [, m, d] = iso.split('-').map(Number);
    return `${m}/${d}`;
  }
  function fmtCN(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return `${y}年${m}月${d}日`;
  }
  function fmtShort(iso) {
    const [, m, d] = iso.split('-').map(Number);
    return `${m}月${d}日`;
  }
  function fmtMonthDay(iso) {
    const [, m, d] = iso.split('-').map(Number);
    return { month: `${m}月`, day: `${d}日` };
  }
  function fmtWeeks(weeks) {
    return `${Number(weeks).toFixed(1)} 周`;
  }
  function fmtPct(value) {
    return `${Math.round(value * 100)}%`;
  }
  function fmtNum(value) {
    return Number(value).toLocaleString('en-US');
  }
  function nextWorkingDay(iso) {
    let d = iso;
    if (d >= '2027-01-01' && d <= '2027-01-03') d = '2027-01-04';
    if (d >= CNY_START && d <= CNY_END) d = '2027-02-19';
    const wd = weekday(d);
    if (wd === 6) d = addDays(d, 2);
    if (wd === 0) d = addDays(d, 1);
    return d;
  }

  function erf(x) {
    const sign = x < 0 ? -1 : 1;
    x = Math.abs(x);
    const a1 = 0.254829592;
    const a2 = -0.284496736;
    const a3 = 1.421413741;
    const a4 = -1.453152027;
    const a5 = 1.061405429;
    const p = 0.3275911;
    const t = 1 / (1 + p * x);
    const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
    return sign * y;
  }
  function normPdf(x, mean, sigma) {
    if (sigma <= 0) return Math.abs(x - mean) < 1e-9 ? 1 : 0;
    const z = (x - mean) / sigma;
    return Math.exp(-0.5 * z * z) / (sigma * Math.sqrt(2 * Math.PI));
  }
  function normCdf(x, mean, sigma) {
    if (sigma <= 0) return x >= mean ? 1 : 0;
    return 0.5 * (1 + erf((x - mean) / (sigma * Math.SQRT2)));
  }

  const state = {
    reports: [],
    sources: [],
    localReports: [],
    localSources: [],
    checks: {},
    overrides: { ...DEFAULTS },
    filter: 'all',
    base: null
  };

  function loadLocal() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const obj = JSON.parse(raw);
        state.localReports = Array.isArray(obj.reports) ? obj.reports : [];
        state.localSources = Array.isArray(obj.sources) ? obj.sources : [];
        state.checks = obj.checks && typeof obj.checks === 'object' ? obj.checks : {};
        if (obj.overrides && typeof obj.overrides === 'object') {
          state.overrides = {
            output: Number(obj.overrides.output) || DEFAULTS.output,
            holiday: Number(obj.overrides.holiday) || 0,
            export: Number(obj.overrides.export) || 0,
            color: Number(obj.overrides.color) || 0
          };
        }
      }
    } catch (e) {
      // localStorage 在部分 file:// 环境不可用；静默降级为内置数据
    }
    state.reports = [...DATA.community, ...state.localReports];
    state.sources = [...DATA.sources, ...state.localSources];
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        reports: state.localReports,
        sources: state.localSources,
        checks: state.checks,
        overrides: state.overrides,
        savedAt: new Date().toISOString()
      }));
    } catch (e) {
      // 忽略隐私模式/配额错误
    }
  }

  function compute(overrides) {
    const opt = { ...DEFAULTS, ...(overrides || {}) };
    const mix = DATA.model.mixture;
    const ref = Number(DATA.model.capacity.referenceOutput) || 22000;
    const commonShift =
      ((ref - opt.output) / 1000) * 0.15 +
      Number(opt.export || 0) +
      Number(opt.color || 0);
    const holiday = Number(opt.holiday || 0);
    const calibration = Number((DATA.model.calibration && DATA.model.calibration.weeks) || 0);
    const preMean = mix.preMeanWeeks + commonShift + calibration;
    const postMean = mix.postMeanWeeks + commonShift + holiday + calibration;
    const preWeight = clamp(
      mix.preWeight +
        ((opt.output - ref) / 1000) * 0.01 -
        holiday * 0.05 -
        Number(opt.export || 0) * 0.02 -
        Number(opt.color || 0) * 0.01,
      0.30,
      0.78
    );
    const sigma = Math.max(0.8, Number(mix.sigmaWeeks) * 7);
    const preEnd = mix.preEnd;
    const postStart = addDays(mix.postStart, Math.round(holiday * 7));
    const horizon = 150;
    const lock = DATA.order.lockedAt;
    const preEndOff = daysBetween(lock, preEnd);
    const postStartOff = daysBetween(lock, postStart);

    const preNorm = normCdf(preEndOff, preMean * 7, sigma) - normCdf(55, preMean * 7, sigma);
    const postNorm = normCdf(horizon, postMean * 7, sigma) - normCdf(postStartOff, postMean * 7, sigma);

    const rows = [];
    for (let off = 55; off <= horizon; off += 1) {
      let p = 0;
      let phase = 'gap';
      if (off <= preEndOff && preNorm > 0) {
        p += preWeight * normPdf(off, preMean * 7, sigma) / preNorm;
        phase = 'pre';
      }
      if (off >= postStartOff && postNorm > 0) {
        p += (1 - preWeight) * normPdf(off, postMean * 7, sigma) / postNorm;
        phase = p > 0 && phase === 'pre' ? 'pre' : 'post';
      }
      if (p > 0) rows.push({ off, date: addDays(lock, off), p, phase });
    }
    const total = rows.reduce((sum, row) => sum + row.p, 0);
    rows.forEach((row) => { row.p /= total; });

    const quantile = (q) => {
      let acc = 0;
      for (const row of rows) {
        acc += row.p;
        if (acc >= q) return row;
      }
      return rows[rows.length - 1];
    };
    const pack = (row) => {
      const date = nextWorkingDay(row.date);
      return {
        date,
        rawDate: row.date,
        weekday: weekdayCN(date),
        weeks: Number((row.off / 7).toFixed(1)),
        phase: row.phase,
        p: row.p
      };
    };

    const buckets = [];
    for (let start = 55; start <= horizon; start += 7) {
      const end = Math.min(start + 6, horizon);
      buckets.push({
        start: addDays(lock, start),
        end: addDays(lock, end),
        p: 0,
        pre: 0,
        post: 0
      });
    }
    rows.forEach((row) => {
      const idx = Math.floor((row.off - 55) / 7);
      const bucket = buckets[idx];
      if (!bucket) return;
      bucket.p += row.p;
      if (row.phase === 'pre') bucket.pre += row.p;
      if (row.phase === 'post') bucket.post += row.p;
    });

    const officialWeeks = DATA.order.appWindowWeeks;
    const meanWeeks = rows.reduce((sum, row) => sum + row.off * row.p, 0) / 7;
    const p10 = pack(quantile(0.10));
    const p25 = pack(quantile(0.25));
    const p50 = pack(quantile(0.50));
    const p75 = pack(quantile(0.75));
    const p90 = pack(quantile(0.90));
    const p95 = pack(quantile(0.95));
    const peak = buckets.reduce((best, bucket) => (bucket.p > best.p ? bucket : best), buckets[0]);
    const isDefault =
      Math.abs(opt.output - DEFAULTS.output) < 1 &&
      Math.abs(opt.holiday - DEFAULTS.holiday) < 0.001 &&
      Math.abs(opt.export - DEFAULTS.export) < 0.001 &&
      Math.abs(opt.color - DEFAULTS.color) < 0.001;

    return {
      p10, p25, p50, p75, p90, p95,
      rows,
      weekly: buckets,
      peak,
      preProbability: preWeight,
      postProbability: 1 - preWeight,
      meanWeeks,
      medianWeeks: p50.weeks,
      confidence: Number(DATA.model.confidence) || 0.64,
      calibrationWeeks: calibration,
      officialWindow: {
        start: addDays(lock, officialWeeks[0] * 7),
        end: addDays(lock, officialWeeks[1] * 7),
        weeks: officialWeeks
      },
      preWindow: { start: addDays(lock, officialWeeks[0] * 7), end: preEnd },
      postWindow: { start: postStart, end: mix.postEnd },
      isDefault,
      overrides: opt
    };
  }

  function renderHero(p) {
    const parts = fmtMonthDay(p.p50.date);
    $('#p50Month').textContent = parts.month;
    $('#p50Day').textContent = parts.day;
    $('#p50Year').textContent = p.p50.date.slice(0, 4);
    $('#p50Week').textContent = `周${p.p50.weekday}`;
    $('#p50Weeks').textContent = p.p50.weeks.toFixed(1);
    $('#officialWeeks').textContent = p.officialWindow.weeks.join('–');
    $('#officialDates').textContent = `${fmtMD(p.officialWindow.start)} – ${fmtMD(p.officialWindow.end)}`;
    $('#confidence').textContent = Math.round(p.confidence * 100);
    $('#sampleCount').textContent = DATA.model.cohort.sampleSize;

    const today = todayISO();
    const toP50 = daysBetween(today, p.p50.date);
    const elapsed = Math.max(0, daysBetween(DATA.order.lockedAt, today));
    if (toP50 > 0) {
      $('#countdown').innerHTML = `距中心估计还有 <b>${toP50}</b> 天`;
    } else if (toP50 === 0) {
      $('#countdown').innerHTML = `中心估计 <b>就是今天</b>`;
    } else {
      $('#countdown').innerHTML = `已超过中心估计 <b>${Math.abs(toP50)}</b> 天`;
    }
    $('#elapsed').innerHTML = `已等 <b>${elapsed}</b> 天`;
    $('#stageText').textContent = elapsed < 85 ? '尚未配车' : '关注配车状态';

    const pct = clamp((elapsed / Math.max(1, p.p50.weeks * 7)) * 100, 8, 88);
    $('#stageFill').style.width = `${pct}%`;

    $('#winStart').textContent = fmtShort(p.p10.date);
    $('#winEnd').textContent = fmtShort(p.p90.date);
    $('#officialRangeLabel').textContent = `${fmtMD(p.officialWindow.start)}–${fmtMD(p.officialWindow.end)}`;
    $('#modelRangeLabel').textContent = `${fmtMD(p.p10.date)}–${fmtMD(p.p90.date)}`;
    $('#preProb').textContent = fmtPct(p.preProbability);
    $('#postProb').textContent = fmtPct(p.postProbability);

    const domainStart = p.officialWindow.start < p.p10.date ? p.officialWindow.start : p.p10.date;
    const domainEnd = p.officialWindow.end > p.p90.date ? p.officialWindow.end : p.p90.date;
    const span = Math.max(1, daysBetween(domainStart, domainEnd));
    const leftPct = (iso) => clamp((daysBetween(domainStart, iso) / span) * 100, 0, 100);
    const widthPct = (a, b) => clamp(((daysBetween(a, b) / span) * 100), 2, 100);

    const officialBand = $('#officialBand');
    officialBand.style.left = `${leftPct(p.officialWindow.start)}%`;
    officialBand.style.width = `${widthPct(p.officialWindow.start, p.officialWindow.end)}%`;

    const modelBand = $('#modelBand');
    modelBand.style.left = `${leftPct(p.p10.date)}%`;
    modelBand.style.width = `${widthPct(p.p10.date, p.p90.date)}%`;

    const pin = $('#p50Pin');
    pin.style.left = `${leftPct(p.p50.date)}%`;
  }

  function renderScenarios(p) {
    $('#optDate').textContent = fmtShort(p.p10.date);
    $('#baseDate').textContent = fmtShort(p.p50.date);
    $('#consDate').textContent = fmtShort(p.p90.date);
    $('#optWeeks').textContent = fmtWeeks(p.p10.weeks);
    $('#baseWeeks').textContent = fmtWeeks(p.p50.weeks);
    $('#consWeeks').textContent = fmtWeeks(p.p90.weeks);
    $('#footP50').textContent = fmtShort(p.p50.date);
    $('#footP90').textContent = fmtShort(p.p90.date);
    $('#footPre').textContent = fmtPct(p.preProbability);
    $('#footPeak').textContent = p.peak ? `${fmtMD(p.peak.start)}–${fmtMD(p.peak.end)}` : '—';
    $('#scenarioState').textContent = p.isDefault ? '基准' : '自定义';
    $('#scenarioState').className = `chip${p.isDefault ? '' : ' lime'}`;

    if (p.isDefault) {
      $('#scenarioNote').textContent = `当前为基准假设：L03 月交付 22,000 台，春节按 2/4–2/18 停摆，出口分流按基准计入${Math.abs(p.calibrationWeeks) >= 0.01 ? `；自动校准 ${p.calibrationWeeks >= 0 ? '+' : ''}${p.calibrationWeeks.toFixed(2)} 周` : ''}。`;
    } else {
      const opt = p.overrides;
      const holidayText = opt.holiday ? `春节额外 +${opt.holiday.toFixed(2)} 周` : '春节基准';
      const exportText = opt.export >= 0 ? `出口 +${opt.export.toFixed(1)} 周` : `出口 ${opt.export.toFixed(1)} 周`;
      $('#scenarioNote').textContent = `沙盘假设：月产能 ${fmtNum(opt.output)} 台，${holidayText}，${exportText}，颜色/配置 ${opt.color >= 0 ? '+' : ''}${opt.color.toFixed(1)} 周。中心估计随之调整为 ${fmtShort(p.p50.date)}${Math.abs(p.calibrationWeeks) >= 0.01 ? `（含自动校准 ${p.calibrationWeeks >= 0 ? '+' : ''}${p.calibrationWeeks.toFixed(2)} 周）` : ''}。`;
    }
  }

  function renderDistribution(p) {
    const compact = window.matchMedia('(max-width: 820px)').matches;
    const W = compact ? 430 : 760;
    const H = compact ? 290 : 300;
    const L = compact ? 32 : 38;
    const R = compact ? 10 : 14;
    const T = compact ? 22 : 24;
    const B = compact ? 30 : 32;
    const all = p.weekly;
    const active = all.filter((b) => b.p > 0);
    if (!active.length) return;
    const first = Math.max(0, all.indexOf(active[0]) - 1);
    const last = Math.min(all.length - 1, all.indexOf(active[active.length - 1]) + 1);
    const buckets = all.slice(first, last + 1);
    const xMin = toUTC(buckets[0].start);
    const xMax = toUTC(buckets[buckets.length - 1].end);
    const span = Math.max(1, xMax - xMin);
    const x = (iso) => L + ((toUTC(iso) - xMin) / span) * (W - L - R);
    const maxP = Math.max(...buckets.map((b) => b.p), 0.01);
    const yMax = Math.max(0.05, Math.ceil((maxP * 100) / 5) * 5 / 100);
    const y = (v) => T + (1 - v / yMax) * (H - T - B);
    const barW = Math.max(7, ((W - L - R) / buckets.length) * 0.62);

    let g = '';
    for (let i = 0; i <= 4; i += 1) {
      const val = (yMax * i) / 4;
      const yy = y(val);
      g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>`;
      g += `<text x="${L - 7}" y="${yy + 3}" text-anchor="end">${(val * 100).toFixed(0)}%</text>`;
    }

    const cnyX1 = x(p.preWindow.end);
    const cnyX2 = x(p.postWindow.start);
    if (cnyX2 > cnyX1) {
      g += `<rect class="cny-band" x="${cnyX1}" y="${T}" width="${cnyX2 - cnyX1}" height="${H - T - B}"/>`;
      g += `<line class="cny-line" x1="${cnyX1}" x2="${cnyX1}" y1="${T}" y2="${H - B}"/>`;
      g += `<line class="cny-line" x1="${cnyX2}" x2="${cnyX2}" y1="${T}" y2="${H - B}"/>`;
      g += `<text class="cny-label" x="${(cnyX1 + cnyX2) / 2}" y="${T + 12}" text-anchor="middle">春节断档</text>`;
    }

    buckets.forEach((b) => {
      if (b.p <= 0) return;
      const x0 = x(b.start);
      const x1 = x(addDays(b.end, 1));
      const cx = (x0 + x1) / 2;
      const h = Math.max(1, (b.p / yMax) * (H - T - B));
      const fill = b.pre >= b.post ? 'url(#gradPre)' : 'url(#gradPost)';
      const label = `${fmtMD(b.start)}–${fmtMD(b.end)} · ${(b.p * 100).toFixed(1)}%`;
      g += `<rect class="bar" x="${cx - barW / 2}" y="${y(b.p)}" width="${barW}" height="${h}" rx="3" fill="${fill}"><title>${label}</title></rect>`;
    });

    const monthStarts = ['2026-12-01', '2027-01-01', '2027-02-01', '2027-03-01'];
    monthStarts.forEach((iso) => {
      if (iso < buckets[0].start || iso > buckets[buckets.length - 1].end) return;
      const xx = x(iso);
      g += `<line class="axis" x1="${xx}" x2="${xx}" y1="${H - B}" y2="${H - B + 4}"/>`;
      g += `<text x="${xx}" y="${H - 11}" text-anchor="middle">${Number(iso.slice(5, 7))}月</text>`;
    });

    if (p.peak && !compact) {
      const peakX = (x(p.peak.start) + x(addDays(p.peak.end, 1))) / 2;
      g += `<text class="p50-label" x="${peakX}" y="${Math.max(T + 11, y(p.peak.p) - 6)}" text-anchor="middle">峰值 ${(p.peak.p * 100).toFixed(0)}%</text>`;
    }

    const px = x(p.p50.rawDate);
    g += `<line class="p50-line" x1="${px}" x2="${px}" y1="${T}" y2="${H - B}"/>`;
    g += `<circle class="p50-dot" cx="${px}" cy="${y(p.p50.p || 0)}" r="3.2"/>`;
    g += `<text class="p50-label" x="${px + 6}" y="${T + 30}" text-anchor="start">P50 ${fmtMD(p.p50.date)}</text>`;
    g += `<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;

    $('#distribution').innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="交付日期概率分布，年前峰值与年后尾部">
        <defs>
          <linearGradient id="gradPre" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#16b8a6"/><stop offset="1" stop-color="#0e8f7e"/>
          </linearGradient>
          <linearGradient id="gradPost" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#f58b70"/><stop offset="1" stop-color="#f26b4b"/>
          </linearGradient>
        </defs>
        ${g}
      </svg>`;
  }

  function renderCapacity() {
    const compact = window.matchMedia('(max-width: 820px)').matches;
    const W = compact ? 430 : 760;
    const H = compact ? 290 : 300;
    const L = compact ? 40 : 42;
    const R = compact ? 38 : 48;
    const T = 26;
    const B = 34;
    const l03 = DATA.capacity.l03Monthly;
    const group = DATA.capacity.groupMonthly;
    const n = l03.length;
    const innerW = W - L - R;
    const step = innerW / n;
    const cx = (i) => L + step * (i + 0.5);
    const leftMax = 25000;
    const rightMax = 60000;
    const yL = (v) => T + (1 - v / leftMax) * (H - T - B);
    const yR = (v) => T + (1 - v / rightMax) * (H - T - B);
    const barW = Math.min(compact ? 22 : 34, step * 0.48);

    let g = '';
    [0, 5000, 10000, 15000, 20000, 25000].forEach((val) => {
      const yy = yL(val);
      g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>`;
      g += `<text x="${L - 7}" y="${yy + 3}" text-anchor="end">${val === 0 ? '0' : `${(val / 10000).toFixed(1)}万`}</text>`;
    });
    [0, 30000, 60000].forEach((val) => {
      const yy = yR(val);
      g += `<text x="${W - R + 7}" y="${yy + 3}" text-anchor="start">${val === 0 ? '0' : `${val / 10000}万`}</text>`;
    });

    g += `<rect x="${cx(3) - step / 2}" y="${T}" width="${step}" height="${H - T - B}" fill="rgba(198,242,78,.07)"/>`;

    g += `<line x1="${L}" x2="${W - R}" y1="${yL(20000)}" y2="${yL(20000)}" stroke="rgba(14,143,126,.35)" stroke-width="1" stroke-dasharray="4 4"/>`;
    if (!compact) {
      g += `<text x="${L + 86}" y="${yL(20000) - 6}" fill="#0e8f7e" font-size="9" font-weight="700">L03 月交付 2 万+</text>`;
    }

    l03.forEach((item, i) => {
      const x = cx(i);
      const y = yL(item.value);
      const h = Math.max(1, H - B - y);
      const actual = item.kind === 'actual';
      const fill = actual ? 'url(#gradCap)' : 'url(#gradCapForecast)';
      const stroke = actual ? 'none' : 'rgba(14,143,126,.35)';
      g += `<rect class="bar" x="${x - barW / 2}" y="${y}" width="${barW}" height="${h}" rx="4" fill="${fill}" stroke="${stroke}" stroke-width="1" ${actual ? '' : 'stroke-dasharray="4 3"'}>
        <title>${item.label}：${fmtNum(item.value)} 台 · ${actual ? '实际' : '预测'}${item.note ? ` · ${esc(item.note)}` : ''}</title>
      </rect>`;
      g += `<text x="${x}" y="${H - 12}" text-anchor="middle">${item.label}</text>`;
      if (actual) {
        const label = item.value >= 10000 ? `${(item.value / 10000).toFixed(1)}万` : fmtNum(item.value);
        g += `<text x="${x}" y="${y - 6}" text-anchor="middle" class="label-strong" font-size="9">${label}</text>`;
      }
    });

    const groupPts = group.map((item, i) => ({ x: cx(i), y: yR(item.value), item }));
    if (groupPts.length) {
      const path = groupPts.map((pt, i) => `${i === 0 ? 'M' : 'L'}${pt.x.toFixed(1)},${pt.y.toFixed(1)}`).join(' ');
      g += `<path d="${path}" fill="none" stroke="#101613" stroke-width="1.6" opacity=".42"/>`;
      groupPts.forEach((pt, i) => {
        const actual = pt.item.kind === 'actual';
        g += `<circle cx="${pt.x}" cy="${pt.y}" r="${actual ? 3.2 : 2.4}" fill="${actual ? '#101613' : '#fff'}" stroke="#101613" stroke-width="1.4" opacity="${actual ? '.75' : '.45'}"/>`;
        if (pt.item.month === '2026-10') {
          g += `<text x="${pt.x}" y="${pt.y - 9}" text-anchor="middle" class="label-strong" font-size="9">5.02万</text>`;
        }
      });
    }

    g += `<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;
    if (!compact) {
      g += `<text x="${L}" y="${T - 9}" font-size="9" fill="#68746b">L03 月交付（柱）</text>`;
      g += `<text x="${W - R}" y="${T - 9}" text-anchor="end" font-size="9" fill="#68746b">集团月交付（线）</text>`;
    }

    $('#capacityChart').innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="L03 月度交付柱状图与集团月度交付折线图">
        <defs>
          <linearGradient id="gradCap" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#17b8a6"/><stop offset="1" stop-color="#0e8f7e"/>
          </linearGradient>
          <linearGradient id="gradCapForecast" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="rgba(23,184,166,.34)"/><stop offset="1" stop-color="rgba(14,143,126,.12)"/>
          </linearGradient>
        </defs>
        ${g}
      </svg>`;

    $('#capacityFacts').innerHTML = DATA.capacity.facts.map((fact) => `
      <div class="fact">
        <span>${esc(fact.label)}</span>
        <b>${esc(fact.value)}<small>${esc(fact.unit || '')}</small></b>
        <i title="${esc(fact.note || '')}">${esc(fact.note || '')}</i>
        <em class="tier">${esc(fact.tier || '')}</em>
      </div>`).join('');

    $('#planTimeline').innerHTML = DATA.capacity.plans.map((plan) => `
      <div class="plan">
        <time>${esc(plan.date)}</time>
        <b>${esc(plan.title)}</b>
        <p>${esc(plan.detail)}</p>
      </div>`).join('');

    $('#constraints').innerHTML = DATA.capacity.constraints.map((text) => `<span>${esc(text)}</span>`).join('');
  }

  function renderCommunity() {
    const compact = window.matchMedia('(max-width: 820px)').matches;
    const reports = [...state.reports].sort((a, b) => a.lockDate.localeCompare(b.lockDate));
    const W = compact ? 430 : 760;
    const H = compact ? 300 : 330;
    const L = compact ? 34 : 42;
    const R = compact ? 12 : 18;
    const T = compact ? 16 : 18;
    const B = compact ? 34 : 38;
    const xStart = '2026-07-01';
    const xEnd = '2026-11-20';
    const xSpan = daysBetween(xStart, xEnd);
    const x = (iso) => L + (daysBetween(xStart, iso) / xSpan) * (W - L - R);
    const yMax = 20;
    const y = (v) => T + (1 - v / yMax) * (H - T - B);

    let g = '';
    for (let v = 0; v <= 20; v += 4) {
      const yy = y(v);
      g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>`;
      g += `<text x="${L - 7}" y="${yy + 3}" text-anchor="end">${v}周</text>`;
    }
    ['2026-07-01', '2026-08-01', '2026-09-01', '2026-10-01', '2026-11-01'].forEach((iso) => {
      const xx = x(iso);
      g += `<line class="axis" x1="${xx}" x2="${xx}" y1="${H - B}" y2="${H - B + 4}"/>`;
      g += `<text x="${xx}" y="${H - 12}" text-anchor="middle">${Number(iso.slice(5, 7))}月</text>`;
    });

    g += `<rect class="band" x="${L}" y="${y(13.5)}" width="${W - L - R}" height="${y(6.5) - y(13.5)}"/>`;
    g += `<text x="${W - R - 4}" y="${y(13.5) + 12}" text-anchor="end" fill="#0e8f7e" font-size="9" font-weight="700">${compact ? '增程样本 6.7–13 周' : '7 月增程样本 6.7–13.0 周'}</text>`;

    reports.forEach((report) => {
      const px = x(report.lockDate);
      const py = y(Number(report.waitWeeks) || 0);
      const status = report.status || 'waiting';
      const cls = status === 'delivered' ? 'delivered' : status === 'allocation' ? 'allocation' : 'waiting';
      const r = report.id && report.id.startsWith('local-') ? 6 : 5.4;
      g += `<circle class="${cls}" cx="${px}" cy="${py}" r="${r}"><title>${esc(report.trim)} · ${esc(report.city || '未披露')} · ${report.lockDate} 锁单 · ${Number(report.waitWeeks).toFixed(1)} 周</title></circle>`;
      if (['c01', 'c02', 'c03'].includes(report.id)) {
        g += `<text class="point-label" x="${px + 8}" y="${py - 7}">${Number(report.waitWeeks).toFixed(1)}w</text>`;
      }
    });

    const today = todayISO();
    const userX = x(DATA.order.lockedAt);
    const userY = y(Math.max(0.2, daysBetween(DATA.order.lockedAt, today) / 7));
    g += `<circle class="user-ring" cx="${userX}" cy="${userY}" r="10">
      <animate attributeName="r" values="9;19" dur="2.2s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values=".55;0" dur="2.2s" repeatCount="indefinite"/>
    </circle>`;
    g += `<circle class="user" cx="${userX}" cy="${userY}" r="6.5"/>`;
    g += `<text class="user-label" x="${userX - 7}" y="${userY - 13}" text-anchor="end">你 · 11/01</text>`;

    g += `<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;
    g += `<text x="${L}" y="${T + 2}" font-size="9" fill="#68746b">等待周数</text>`;
    g += `<text x="${W - R}" y="${T + 2}" text-anchor="end" font-size="9" fill="#68746b">锁单日期 →</text>`;

    $('#scatter').innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="车友锁单日期与等待周数散点图">
        ${g}
      </svg>`;

    $('#communityCount').textContent = `${reports.length} 条样本`;
    const shown = [...reports].sort((a, b) => b.lockDate.localeCompare(a.lockDate)).slice(0, 6);
    $('#reportList').innerHTML = shown.map((report) => {
      const status = report.status || 'waiting';
      const statusText = status === 'delivered' ? '已提车' : status === 'allocation' ? '已配车' : '等车中';
      const waitLabel = status === 'delivered' ? '周' : '已等';
      return `<div class="report">
        <div class="wait">${Number(report.waitWeeks).toFixed(1)}<small>${waitLabel}</small></div>
        <div class="main">
          <b>${esc(report.trim)} · ${esc(report.city || '城市未披露')}</b>
          <span>${esc(report.lockDate.slice(5))} 锁单 · ${esc(report.note || statusText)}</span>
        </div>
        <span class="status ${status}">${statusText}</span>
      </div>`;
    }).join('');
  }

  function renderSources() {
    const filtered = state.sources
      .filter((s) => state.filter === 'all' || s.tier === state.filter)
      .sort((a, b) => {
        const dateCmp = String(b.date).localeCompare(String(a.date));
        if (dateCmp !== 0) return dateCmp;
        return String(a.tier).localeCompare(String(b.tier));
      });

    const counts = { A: 0, B: 0, C: 0 };
    state.sources.forEach((s) => { counts[s.tier] = (counts[s.tier] || 0) + 1; });
    $('#sourceCount').textContent = `${state.sources.length} 条`;
    $('#sourceMeta').textContent = `${filtered.length} 条 · 最近 ${filtered[0] ? filtered[0].date.slice(5) : '—'}`;
    $$('#sourceTabs button').forEach((btn) => {
      const active = btn.dataset.filter === state.filter;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    $('#sourceList').innerHTML = filtered.map((s) => {
      const superseded = s.status === 'superseded';
      return `<div class="source${superseded ? ' superseded' : ''}">
        <span class="tier ${esc(s.tier)}">${esc(s.tier)}</span>
        <div class="s-main">
          <div class="s-top">
            <b title="${esc(s.title)}">${esc(s.title)}</b>
            <time>${esc(s.date.slice(5))}</time>
          </div>
          <div class="s-claim" title="${esc(s.claim)}">${esc(s.claim)}</div>
          <div class="s-bottom">
            <span>${esc(s.publisher)}</span>
            ${s.auto ? '<span class="auto-tag">自动</span>' : ''}
            ${superseded ? '<span class="superseded-tag">已被更新</span>' : ''}
            ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">原文 ↗</a>` : ''}
          </div>
        </div>
      </div>`;
    }).join('');
  }

  function renderUpdate() {
    $('#checklist').innerHTML = DATA.dailyChecklist.map((item, i) => {
      const idx = item.indexOf('：');
      const head = idx > -1 ? item.slice(0, idx) : item;
      const body = idx > -1 ? item.slice(idx + 1) : '';
      const done = state.checks[i] ? ' done' : '';
      return `<div class="check${done}" data-check="${i}" role="checkbox" aria-checked="${state.checks[i] ? 'true' : 'false'}" tabindex="0">
        <span class="box">✓</span>
        <div><b>${esc(head)}</b>${body ? `<p>${esc(body)}</p>` : ''}</div>
      </div>`;
    }).join('');

    $('#localCount').textContent = `本地新增 ${state.localReports.length} 条`;
    $('#localSourceCount').textContent = `本地新增 ${state.localSources.length} 条`;
    $('#autoStatusChip').textContent = `自动 · 数据 ${fmtMD(DATA.meta.dataThrough)}`;
    $('#dataThrough').textContent = fmtMD(DATA.meta.dataThrough);
    $('#footDate').textContent = DATA.meta.dataThrough;

    const auto = DATA.auto || {};
    const autoText = $('#autoStatusText');
    if (auto.lastRun) {
      const autoCount = state.sources.filter((s) => s.auto).length;
      autoText.textContent = `上次 ${String(auto.lastRun).slice(5, 16).replace('T', ' ')} · 自动信源 ${autoCount} 条 · 校准 ${Number(auto.calibrationWeeks || 0) >= 0 ? '+' : ''}${Number(auto.calibrationWeeks || 0).toFixed(2)} 周`;
    } else {
      autoText.textContent = '尚未运行；请等待 09:00 自动任务，或执行 python auto_update.py';
    }

    const cal = Number((DATA.model.calibration && DATA.model.calibration.weeks) || 0);
    const hint = $('#calibrationHint');
    hint.textContent = Math.abs(cal) >= 0.01 ? ` · 校准${cal >= 0 ? '+' : ''}${cal.toFixed(2)}` : '';
    hint.title = Math.abs(cal) >= 0.01 ? `自动校准 ${cal >= 0 ? '+' : ''}${cal.toFixed(2)} 周` : '';

    const history = Array.isArray(DATA.predictionHistory) ? DATA.predictionHistory : [];
    $('#historyCount').textContent = `${history.length} 次`;
    $('#history').innerHTML = history.slice(-4).reverse().map((item) => `
      <div class="history-item">
        <time>${esc(String(item.date || '').slice(5))}</time>
        <b title="${esc(item.note || '')}">${esc(item.note || '模型更新')}</b>
        <span>${esc(item.p50 ? item.p50.slice(5) : '—')}</span>
      </div>`).join('');

    const stale = Math.max(0, daysBetween(DATA.meta.dataThrough, todayISO()));
    const pill = $('#freshness');
    pill.classList.toggle('warn', stale > 2);
    pill.innerHTML = `<i></i>${stale > 2 ? '数据已 ' + stale + ' 天未更新' : '数据截至'} <b>${fmtMD(DATA.meta.dataThrough)}</b>`;
  }

  function renderMethod() {
    $('#methodSummary').textContent = DATA.methodology.summary;
    $('#methodFormula').innerHTML = `<b>WAIT</b> = 官方先验 × 55% + 同配置样本 × 25% + 产能/供需 × 20% + 结构性修正<br><span style="color:rgba(234,240,228,.55)">${esc(DATA.methodology.formula)}</span>`;
    $('#methodRules').innerHTML = DATA.methodology.rules.map((rule, i) => `
      <div class="rule"><span class="n">${pad2(i + 1)}</span><p>${esc(rule)}</p></div>`).join('');
    $('#caveats').innerHTML = DATA.methodology.caveats.map((text, i) => `
      <div class="caveat"><b>注意 ${pad2(i + 1)}</b>${esc(text)}</div>`).join('');
  }

  function renderToday(p) {
    const auto = DATA.auto || {};
    const runRaw = String(auto.lastRun || '');
    const runDate = auto.lastRunDate || runRaw.slice(0, 10);
    const today = todayISO();
    const isToday = runDate === today;
    const strip = $('#todayStrip');
    strip.classList.toggle('stale', !isToday);

    $('#todayTitle').textContent = isToday
      ? '今日预测已更新'
      : runDate
        ? '今日尚未更新'
        : '预测尚未更新';
    if (runRaw) {
      const time = runRaw.slice(11, 16);
      $('#todayRun').textContent = isToday ? `今日 ${time}` : `上次 ${runRaw.slice(5, 10)} ${time}`;
    } else {
      $('#todayRun').textContent = '等待首次运行';
    }

    $('#todayP50').textContent = fmtShort(p.p50.date);
    const before = String(auto.p50Before || '');
    const after = String(auto.p50After || p.p50.date);
    let deltaText = '今日已重算';
    if (/^\d{4}-\d{2}-\d{2}$/.test(before) && /^\d{4}-\d{2}-\d{2}$/.test(after)) {
      const diff = daysBetween(before, after);
      deltaText = diff === 0 ? '较上次不变' : diff < 0 ? `较上次提前 ${-diff} 天` : `较上次推迟 ${diff} 天`;
    }
    $('#todayDelta').textContent = deltaText;
    $('#todayData').textContent = fmtMD(DATA.meta.dataThrough);
    const added = Number(auto.todayNewSources ?? auto.newSources ?? 0) + Number(auto.todayNewReports ?? auto.newReports ?? 0);
    $('#todayNew').textContent = added;
  }

  function renderAll(p) {
    renderToday(p);
    renderHero(p);
    renderScenarios(p);
    renderDistribution(p);
    renderCapacity();
    renderCommunity();
    renderSources();
    renderUpdate();
    renderMethod();
  }

  function syncSliders() {
    const opt = state.overrides;
    $('#outputSlider').value = String(opt.output);
    $('#holidaySlider').value = String(opt.holiday);
    $('#exportSlider').value = String(opt.export);
    $('#colorSlider').value = String(opt.color);
    updateSliderLabels();
  }

  function updateSliderLabels() {
    const opt = state.overrides;
    $('#outputOut').textContent = `${fmtNum(opt.output)} 台`;
    $('#holidayOut').textContent = `${opt.holiday.toFixed(2)} 周`;
    $('#exportOut').textContent = `${opt.export >= 0 ? '+' : ''}${opt.export.toFixed(1)} 周`;
    $('#colorOut').textContent = `${opt.color >= 0 ? '+' : ''}${opt.color.toFixed(1)} 周`;
  }

  function readSliders() {
    state.overrides = {
      output: Number($('#outputSlider').value),
      holiday: Number($('#holidaySlider').value),
      export: Number($('#exportSlider').value),
      color: Number($('#colorSlider').value)
    };
  }

  function refreshFromSliders() {
    readSliders();
    updateSliderLabels();
    const p = compute(state.overrides);
    renderHero(p);
    renderScenarios(p);
    renderDistribution(p);
    persist();
  }

  function toast(message) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.remove('show'), 2600);
  }

  function downloadJSON(payload, filename) {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  function bindEvents() {
    ['outputSlider', 'holidaySlider', 'exportSlider', 'colorSlider'].forEach((id) => {
      $('#' + id).addEventListener('input', refreshFromSliders);
    });

    $('#resetScenario').addEventListener('click', () => {
      state.overrides = { ...DEFAULTS };
      syncSliders();
      refreshFromSliders();
      toast('已恢复基准假设');
    });

    $('#copyBtn').addEventListener('click', async () => {
      const p = compute(state.overrides);
      const text = [
        '小鹏 MONA L03 超级增程 1330 Max · 新月银 · 18 英寸 · 成都',
        '锁单：2026-11-01｜官方窗口：11–16 周',
        `中心估计：${fmtCN(p.p50.date)}（${p.p50.weeks} 周）`,
        `P10–P90：${fmtCN(p.p10.date)} – ${fmtCN(p.p90.date)}`,
        `春节前概率：${fmtPct(p.preProbability)}｜春节后：${fmtPct(p.postProbability)}`,
        `数据截至：${DATA.meta.dataThrough}`,
        '来源：L03 交付罗盘（私人预测，非官方）'
      ].join('\n');
      try {
        await navigator.clipboard.writeText(text);
        toast('结论已复制');
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); toast('结论已复制'); } catch (err) { toast('复制失败，请手动选择文本'); }
        ta.remove();
      }
    });

    $$('#sourceTabs button').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.filter = btn.dataset.filter;
        renderSources();
      });
    });

    $$('.form-tabs button').forEach((btn) => {
      btn.addEventListener('click', () => {
        $$('.form-tabs button').forEach((b) => b.classList.toggle('active', b === btn));
        const isReport = btn.dataset.form === 'report';
        $('#reportForm').hidden = !isReport;
        $('#sourceForm').hidden = isReport;
      });
    });

    $('#reportForm').addEventListener('submit', (event) => {
      event.preventDefault();
      const lock = $('#rLock').value;
      const delivery = $('#rDelivery').value;
      const status = $('#rStatus').value;
      if (!lock) { toast('请填写锁单日期'); return; }
      if (status !== 'waiting' && !delivery) { toast('请填写提车/配车日期'); return; }
      if (delivery && delivery < lock) { toast('提车/配车日期不能早于锁单日期'); return; }
      const waitDate = delivery || todayISO();
      const waitWeeks = Math.max(0, daysBetween(lock, waitDate) / 7);
      const report = {
        id: `local-${Date.now()}`,
        lockDate: lock,
        deliveryDate: delivery || null,
        observedAt: waitDate,
        status,
        trim: $('#rTrim').value.trim() || '1330 Max',
        color: $('#rColor').value.trim() || '未披露',
        city: $('#rCity').value.trim() || '未披露',
        waitWeeks: Number(waitWeeks.toFixed(1)),
        source: '本地录入',
        url: $('#rUrl').value.trim(),
        confidence: 'local',
        note: $('#rNote').value.trim() || '本地新样本'
      };
      state.localReports.push(report);
      state.reports = [...DATA.community, ...state.localReports];
      persist();
      renderCommunity();
      renderUpdate();
      event.target.reset();
      $('#rTrim').value = '1330 Max';
      toast('样本已加入本地，图表已重算');
    });

    $('#sourceForm').addEventListener('submit', (event) => {
      event.preventDefault();
      const url = $('#sUrl').value.trim();
      if (!url) { toast('请填写信源链接'); return; }
      const tier = $('#sTier').value;
      const source = {
        id: `local-s-${Date.now()}`,
        date: $('#sDate').value,
        tier,
        publisher: $('#sPublisher').value.trim(),
        title: $('#sTitle').value.trim(),
        url,
        claim: $('#sClaim').value.trim(),
        confidence: tier === 'A' ? 0.95 : tier === 'B' ? 0.8 : 0.55,
        usedFor: 'user',
        status: 'active'
      };
      state.localSources.push(source);
      state.sources = [...DATA.sources, ...state.localSources];
      persist();
      renderSources();
      renderUpdate();
      event.target.reset();
      toast('信源已加入本地，记得导出更新包');
    });

    $('#exportBtn').addEventListener('click', () => {
      if (!state.localReports.length && !state.localSources.length) {
        toast('还没有本地新增数据');
        return;
      }
      downloadJSON({
        exportedAt: new Date().toISOString(),
        orderId: DATA.order.id,
        dataThrough: DATA.meta.dataThrough,
        overrides: state.overrides,
        reports: state.localReports,
        sources: state.localSources
      }, `l03-update-${todayISO()}.json`);
      toast('更新包已导出');
    });

    $('#resetLocal').addEventListener('click', () => {
      if (!state.localReports.length && !state.localSources.length) {
        toast('当前没有本地数据');
        return;
      }
      if (!window.confirm('确认清除本机新增的样本/信源？内置数据不会被删除。')) return;
      state.localReports = [];
      state.localSources = [];
      state.checks = {};
      state.overrides = { ...DEFAULTS };
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
      state.reports = [...DATA.community];
      state.sources = [...DATA.sources];
      syncSliders();
      refreshFromSliders();
      renderSources();
      renderUpdate();
      toast('已恢复内置数据');
    });

    $('#checklist').addEventListener('click', (event) => {
      const row = event.target.closest('[data-check]');
      if (!row) return;
      const idx = Number(row.dataset.check);
      state.checks[idx] = !state.checks[idx];
      row.classList.toggle('done', Boolean(state.checks[idx]));
      row.setAttribute('aria-checked', state.checks[idx] ? 'true' : 'false');
      persist();
    });
    $('#checklist').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const row = event.target.closest('[data-check]');
      if (!row) return;
      event.preventDefault();
      row.click();
    });

    $('#rLock').value = DATA.order.lockedAt;
    $('#sDate').value = todayISO();
  }

  function setupNav() {
    const targets = ['forecast', 'capacity', 'community', 'sources', 'update']
      .map((id) => document.getElementById(id))
      .filter(Boolean);
    const links = $$('[data-nav]');
    const onScroll = () => {
      const y = window.scrollY + 150;
      let current = targets[0] ? targets[0].id : '';
      targets.forEach((el) => {
        const top = el.getBoundingClientRect().top + window.scrollY;
        if (top <= y) current = el.id;
      });
      links.forEach((link) => {
        link.classList.toggle('active', link.getAttribute('href') === `#${current}`);
      });
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  function init() {
    loadLocal();
    state.base = compute(DEFAULTS);
    syncSliders();
    renderAll(compute(state.overrides));
    bindEvents();
    setupNav();

    let compactMode = window.matchMedia('(max-width: 820px)').matches;
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        const next = window.matchMedia('(max-width: 820px)').matches;
        if (next === compactMode) return;
        compactMode = next;
        const p = compute(state.overrides);
        renderDistribution(p);
        renderCapacity();
        renderCommunity();
      }, 160);
    });
  }

  init();
})();
