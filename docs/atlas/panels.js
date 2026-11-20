// The atlas's panels, shared by index.html (3D) and flat.html (2D): controls and the architecture, the experiments and
// what was measured, the live numbers and charts, the event log, the guided tour, the run inspector and shareable links.
// A page calls AtlasPanels.mount(hooks) and draws the sandbox itself; hooks lets the panels move its view and clear it.
(function () {
  'use strict';
  const {
    C, DEFAULT_CFG, DEFAULT_ARCH, ARCH_KEYS, ARCH_PROFILES, kvTokensFor, archOf, Sim, METRICS, spread, PRESETS, MEASURED_CURVE,
    runTable, secs,
  } = AtlasSim;
  const $ = id => document.getElementById(id);
  const fmtInt = n => Math.round(n).toLocaleString('en-US');
  const esc = s => String(s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[ch]);

  const sim = new Sim({});
  const ui = { playing: true, speed: 1, preset: null, variant: 0, table: [], modified: false, hw: { ...ARCH_PROFILES[0].hw } };
  let hooks = { views: [], see: {}, clear() {}, retheme() {}, focus() {} };

  // ===== Palette and theme =====
  const PALETTE_NAMES = ['bg', 'surface', 'surface-2', 'surface-3', 'line', 'line-strong', 'ink', 'ink-muted', 'accent', 'accent-ink',
    'accent-soft', 'ready', 'ready-ink', 'ready-soft', 'starting', 'starting-ink', 'starting-soft', 'draining', 'draining-ink',
    'draining-soft', 'failed', 'failed-ink', 'failed-soft'];
  let P = {};
  const readPalette = () => {
    const cs = getComputedStyle(document.documentElement);
    P = Object.fromEntries(PALETTE_NAMES.map(n => [n, cs.getPropertyValue('--' + n).trim()]));
  };
  let theme = 'dark';
  try {
    const saved = localStorage.getItem('atlas-theme');
    if (saved === 'dark' || saved === 'light') theme = saved;
    else if (matchMedia('(prefers-color-scheme: light)').matches) theme = 'light';
  } catch (err) {
    // Storage can be unavailable (a private window, file:// policies); the default theme stands.
  }
  document.documentElement.dataset.theme = theme;
  readPalette();

  // A run's colour: its hue, a lightness that reads on the theme's background.
  const runHSL = run => ({ h: run.hue / 360, s: 0.62, l: theme === 'dark' ? 0.62 : 0.46 });
  const runColor = (run, a = 1) => `hsl(${run.hue.toFixed(1)} 62% ${theme === 'dark' ? 62 : 46}% / ${a})`;

  // Why the picker chose a pod, in words, and the colour token that goes with it.
  const REASONS = {
    pinned: ['kept on its pod', 'accent'], 'parallel: stay': ['parallel call, same pod', 'accent'],
    'least context': ['least context first', 'ready'], 'warm-up: new pod': ['warm-up: new pod', 'starting'],
    'spill: overload': ['pod busy: spilled', 'draining'], 'parallel: spill': ['parallel call spilled', 'draining'],
    'parallel: no room': ['parallel, no room elsewhere', 'draining'], 'remap: pod left': ['pod gone: moved', 'starting'],
    'remap: sustained overload': ['busy too long: moved', 'starting'], 'first call': ['first call: best score', 'ink'],
    scorers: ['best score', 'ink'], 'round-robin': ['next in turn', 'ink-muted'],
  };
  const reason = key => { const r = REASONS[key] || [key, 'ink']; return { text: r[0], token: r[1] }; };

  // ===== Architecture =====
  const bytesPerToken = hw => 2 * hw.layers * hw.kvHeads * hw.headDim * hw.bytes;
  const currentProfile = () => ARCH_PROFILES.find(p => {
    const arch = { ...DEFAULT_ARCH, ...p.cfg, kvTokens: kvTokensFor(p.hw) };
    return ARCH_KEYS.every(k => arch[k] === sim.cfg[k]);
  }) || null;
  const isThesisArch = () => ARCH_KEYS.every(k => DEFAULT_ARCH[k] === sim.cfg[k]);

  let tableTimer = null;
  function archChanged() {
    if (!ui.preset) return;
    clearTimeout(tableTimer);
    tableTimer = setTimeout(() => { ui.table = runTable(ui.preset, sim.cfg.seed, archOf(sim.cfg)); renderExperiment(); }, 250);
  }

  function applyProfile(p) {
    ui.hw = { ...p.hw };
    sim.setCfg({ ...DEFAULT_ARCH, ...p.cfg, kvTokens: kvTokensFor(p.hw) });
    if (ui.preset) { ui.table = runTable(ui.preset, sim.cfg.seed, archOf(sim.cfg)); renderExperiment(); }
    restart();
    refreshControls();
  }

  const ARCH_GROUPS = [
    { title: 'Context and speed', open: true, fields: [
      ['maxContext', 'Max context (tokens)', 512, 262144, 512], ['maxSeqs', 'Calls a pod runs at once', 1, 512, 1],
      ['prefillTps', 'Prompt reading (tokens/s)', 100, 200000, 100], ['decodeTps', 'Reply writing (tokens/s)', 1, 1000, 1],
      ['coldStart', 'Cold start (s)', 1, 600, 1], ['promptScale', 'Prompt size ×', 0.25, 64, 0.25],
      ['toolMin', 'Tool time from (s)', 0, 120, 0.5], ['toolMax', 'Tool time to (s)', 0, 300, 0.5],
    ] },
    { title: 'Routing rules', note: 'config/real.yaml, routing', fields: [
      ['guardThreshold', 'Overload: calls waiting', 0, 50, 1], ['guardSustained', 'Overload lasts (s)', 1, 300, 1],
      ['parallelThreshold', 'Parallel: calls waiting', 0, 50, 1], ['agingSeconds', 'Priority aging (s)', 0, 600, 1],
      ['warmupWindow', 'A pod counts as new (s)', 1, 600, 1],
    ] },
    { title: 'Autoscaler and retries', note: 'config/real.yaml, autoscaling', fields: [
      ['remainingTarget', 'Steps left per pod', 1, 200, 1], ['queueTarget', 'Calls waiting per pod', 1, 100, 1],
      ['pressureTarget', 'Pressure per pod', 0.1, 4, 0.05], ['scrape', 'Reads every (s)', 1, 120, 1],
      ['cooldown', 'Scale down after (s)', 0, 900, 5], ['retries', 'Retries on a lost pod', 0, 10, 1],
      ['retryBackoff', 'Before a retry (s)', 0, 60, 0.5],
    ] },
  ];
  const HW_FIELDS = [['kvMiB', 'KV memory per pod (MiB)', 16, 1048576, 16], ['layers', 'Model layers', 1, 256, 1],
    ['kvHeads', 'KV heads', 1, 128, 1], ['headDim', 'Head size', 16, 512, 16], ['bytes', 'Bytes per value', 0.5, 4, 0.5]];

  function numInput(id, min, max, step, read, write) {
    const input = document.createElement('input');
    Object.assign(input, { type: 'number', id, min, max, step });
    input.addEventListener('change', () => {
      const v = Number(input.value);
      if (!Number.isFinite(v)) { input.value = read(); return; }
      const clamped = Math.min(max, Math.max(min, v));
      input.value = clamped;
      write(clamped);
    });
    return input;
  }

  const archRefs = [];
  function buildArchitecture(root) {
    const s = document.createElement('section');
    s.className = 'sec';
    s.innerHTML = `<h2 class="overline">Architecture</h2><div class="presets profiles"></div>
      <p class="caption arch-note"></p>
      <div class="ctl"><div class="row"><label class="label" for="s-kv">KV cache per pod</label><output for="s-kv"></output></div>
        <input type="range" id="s-kv" min="10" max="20" step="0.01"><span class="caption">GPU memory a pod keeps contexts in</span></div>
      <details class="group" open><summary>Hardware and model</summary>
        <div class="fields hw"></div><p class="caption calc"></p></details>`;
    const list = s.querySelector('.profiles');
    for (const p of ARCH_PROFILES) {
      const b = document.createElement('button');
      b.className = 'preset'; b.dataset.id = p.id;
      b.innerHTML = `<span class="tag">${p.id === 'thesis' ? 'Thesis' : p.id === 'gpu24-8b' ? '24 GB' : '80 GB'}</span><span class="pt">${esc(p.name)}</span>`;
      b.addEventListener('click', () => applyProfile(p));
      list.appendChild(b);
    }
    const kv = s.querySelector('#s-kv'), kvOut = s.querySelector('output[for="s-kv"]');
    kv.addEventListener('input', () => {
      const tokens = Math.max(256, Math.round(2 ** Number(kv.value) / 16) * 16);
      ui.hw.kvMiB = Math.round((tokens * bytesPerToken(ui.hw)) / 1048576);
      kvOut.textContent = `${fmtInt(tokens)} tokens`;
      sim.setCfg({ kvTokens: tokens });
      archChanged(); refreshControls();
    });
    const hwBox = s.querySelector('.hw');
    for (const [key, label, min, max, step] of HW_FIELDS) {
      const f = document.createElement('div');
      f.className = 'field';
      f.innerHTML = `<label for="hw-${key}">${label}</label>`;
      const input = numInput(`hw-${key}`, min, max, step, () => ui.hw[key], v => {
        ui.hw[key] = v;
        sim.setCfg({ kvTokens: Math.max(256, kvTokensFor(ui.hw)) });
        archChanged(); refreshControls();
      });
      f.appendChild(input);
      hwBox.appendChild(f);
      archRefs.push(() => { input.value = ui.hw[key]; });
    }
    for (const g of ARCH_GROUPS) {
      const d = document.createElement('details');
      d.className = 'group'; d.open = !!g.open;
      d.innerHTML = `<summary>${g.title}${g.note ? ` <span class="caption">· ${g.note}</span>` : ''}</summary><div class="fields"></div>`;
      const box = d.querySelector('.fields');
      for (const [key, label, min, max, step] of g.fields) {
        const f = document.createElement('div');
        f.className = 'field';
        f.innerHTML = `<label for="a-${key}">${label}</label>`;
        const input = numInput(`a-${key}`, min, max, step, () => sim.cfg[key], v => {
          const patch = { [key]: v };
          if (key === 'toolMin' && v > sim.cfg.toolMax) patch.toolMax = v;
          if (key === 'toolMax' && v < sim.cfg.toolMin) patch.toolMin = v;
          sim.setCfg(patch);
          archChanged(); refreshControls();
        });
        f.appendChild(input);
        box.appendChild(f);
        archRefs.push(() => { input.value = sim.cfg[key]; });
      }
      s.appendChild(d);
    }
    const acts = document.createElement('div');
    acts.className = 'actions';
    acts.innerHTML = `<button class="btn block" id="arch-thesis">Back to the thesis setup</button>
      <button class="btn block" id="share">Copy a link to this setup</button><input class="share-url" id="share-url" readonly hidden>`;
    s.appendChild(acts);
    acts.querySelector('#arch-thesis').addEventListener('click', () => applyProfile(ARCH_PROFILES[0]));
    acts.querySelector('#share').addEventListener('click', copyLink);
    root.appendChild(s);

    archRefs.push(() => {
      const prof = currentProfile();
      for (const b of list.children) b.setAttribute('aria-pressed', String(!!prof && b.dataset.id === prof.id));
      const warn = sim.cfg.maxContext > sim.cfg.kvTokens
        ? ' <b class="warn-text">The max context is more than one pod\'s KV cache holds: vLLM would refuse to start, so prompts stop at the cache\'s size.</b>' : '';
      s.querySelector('.arch-note').innerHTML = (prof ? esc(prof.note) : 'Custom: your own values. The measured results come from the thesis setup.') + warn;
      kv.value = Math.log2(sim.cfg.kvTokens);
      kvOut.textContent = `${fmtInt(sim.cfg.kvTokens)} tokens`;
      const per = bytesPerToken(ui.hw);
      s.querySelector('.calc').textContent = `${fmtInt(per / 1024)} KiB per token (K and V × ${ui.hw.layers} layers × ${ui.hw.kvHeads} KV heads × ${ui.hw.headDim} × ${ui.hw.bytes} bytes). ${fmtInt(ui.hw.kvMiB)} MiB holds ${fmtInt(kvTokensFor(ui.hw))} tokens, in blocks of 16.`;
    });
  }

  // ===== Links =====
  const HW_KEYS = HW_FIELDS.map(f => f[0]);
  function linkParams() {
    const q = new URLSearchParams();
    const prof = currentProfile();
    if (prof && prof.id !== 'thesis') q.set('profile', prof.id);
    if (!prof) {
      for (const k of HW_KEYS) if (ui.hw[k] !== ARCH_PROFILES[0].hw[k]) q.set(k, ui.hw[k]);
      for (const k of ARCH_KEYS) if (sim.cfg[k] !== DEFAULT_ARCH[k]) q.set(k, sim.cfg[k]);
    }
    if (sim.cfg.seed !== DEFAULT_CFG.seed) q.set('seed', sim.cfg.seed);
    if (ui.preset && !ui.modified) { q.set('preset', ui.preset.id); if (ui.variant) q.set('variant', ui.variant); }
    else for (const k of Object.keys(DEFAULT_CFG)) if (!ARCH_KEYS.includes(k) && k !== 'seed' && sim.cfg[k] !== DEFAULT_CFG[k]) q.set(k, sim.cfg[k]);
    return q;
  }
  function copyLink() {
    const q = linkParams().toString();
    const url = location.href.split(/[?#]/)[0] + (q ? `?${q}` : '');
    const field = $('share-url'), btn = $('share');
    const showField = () => { field.hidden = false; field.value = url; field.select(); btn.textContent = 'Copy the link below'; };
    if (!navigator.clipboard) { showField(); return; }
    navigator.clipboard.writeText(url).then(() => { btn.textContent = 'Link copied'; setTimeout(() => { btn.textContent = 'Copy a link to this setup'; }, 2000); }, showField);
  }

  // Values from the address: an architecture, an experiment or single settings, a moment (t, in seconds) and a view.
  function readLink() {
    const q = new URLSearchParams(location.search);
    const typed = (key, raw) => {
      const d = DEFAULT_CFG[key];
      if (typeof d === 'number') { const v = Number(raw); return Number.isFinite(v) ? v : d; }
      if (typeof d === 'boolean') return raw === 'true' || raw === '1';
      return raw;
    };
    const prof = ARCH_PROFILES.find(p => p.id === q.get('profile'));
    if (prof) { ui.hw = { ...prof.hw }; sim.cfg = { ...sim.cfg, ...DEFAULT_ARCH, ...prof.cfg, kvTokens: kvTokensFor(prof.hw) }; }
    let hwSet = false;
    for (const k of HW_KEYS) if (q.has(k)) { const v = Number(q.get(k)); if (Number.isFinite(v) && v > 0) { ui.hw[k] = v; hwSet = true; } }
    if (hwSet) sim.cfg.kvTokens = Math.max(256, kvTokensFor(ui.hw));
    for (const k of Object.keys(DEFAULT_CFG)) if (q.has(k)) sim.cfg[k] = typed(k, q.get(k));
    $('seed').value = sim.cfg.seed;
    const preset = PRESETS.find(p => p.id === q.get('preset'));
    const variant = preset ? Math.min(preset.variants.length - 1, Math.max(0, Number(q.get('variant')) || 0)) : 0;
    return { preset, variant, t: Math.min(1800, Math.max(0, Number(q.get('t')) || 0)), view: q.get('view'), tour: q.get('tour'), any: [...q.keys()].length > 0 };
  }

  // ===== Controls =====
  const CONTROL_SECTIONS = [
    { title: 'Load', items: [
      { kind: 'slider', key: 'rate', label: 'New runs per minute', min: 0, max: 30, step: 0.5, fmt: v => Number(v).toFixed(1) },
      { kind: 'switch', key: 'burst', label: 'Bursts', hint: 'Runs arrive 5 at once, every 45 s' },
      { kind: 'switch', key: 'fanOut', label: 'Parallel calls', hint: 'Half the later steps send 2 calls with one x-parallel-group' },
      { kind: 'button', label: 'Send 5 runs now', action: 'burst' },
    ] },
    { title: 'Routing', items: [
      { kind: 'seg', key: 'routing', label: 'Routing', options: [['round-robin', 'Round-robin'], ['default', 'Default picker'], ['plugin', 'Thesis plugin']] },
      { kind: 'switch', key: 'affinity', label: 'Task affinity', hint: 'Keep a run on the pod that has its context', needs: 'plugin' },
      { kind: 'switch', key: 'overloadGuard', label: 'Overload guard', hint: 'Spill a call when its pod has a queue; move the run if it lasts', needs: 'affinity' },
      { kind: 'switch', key: 'parallelSpill', label: 'Parallel spill', hint: 'A parallel group stays on its pod while it has room', needs: 'affinity' },
      { kind: 'switch', key: 'leastContext', label: 'Least context first', hint: "A run's first call goes to the pod whose runs hold the least context", needs: 'affinity' },
      { kind: 'switch', key: 'priority', label: 'Progress priority', hint: 'Runs close to their last step go first, with aging', needs: 'plugin' },
      { kind: 'switch', key: 'warmup', label: 'Warm-up routing', hint: 'First calls go to a pod that just turned Ready', needs: 'plugin' },
    ] },
    { title: 'Pods', items: [
      { kind: 'slider', key: 'pods', label: 'Pods', min: 1, max: 10, step: 1, fmt: v => String(v), needs: 'off' },
      { kind: 'switch', key: 'drain', label: 'Drain on scale-down', hint: 'A removed pod takes no new calls and finishes its own' },
      { kind: 'switch', key: 'retry', label: 'Retry on a lost pod', hint: "The agent runtime sends a lost call again" },
    ] },
    { title: 'Autoscaler', items: [
      { kind: 'seg', key: 'autoscaler', label: 'Autoscaler', options: [['off', 'Off'], ['cpu', 'CPU'], ['queue', 'Queue'], ['remaining', 'Remaining work'], ['pressure', 'Pressure']] },
      { kind: 'slider', key: 'maxPods', label: 'Most pods', min: 1, max: 10, step: 1, fmt: v => String(v) },
    ] },
    { title: 'Actions', items: [
      { kind: 'button', label: 'Kill the busiest pod', action: 'kill', tone: 'danger' },
      { kind: 'button', label: 'Scale down one pod', action: 'scaleDown' },
    ] },
  ];
  const NEEDS_TEXT = { plugin: 'Needs the thesis plugin', affinity: 'Needs task affinity', off: 'Set by the autoscaler' };
  function itemEnabled(item) {
    const cfg = sim.cfg;
    if (item.needs === 'plugin') return cfg.routing === 'plugin';
    if (item.needs === 'affinity') return cfg.routing === 'plugin' && cfg.affinity;
    if (item.needs === 'off') return cfg.autoscaler === 'off';
    return true;
  }

  const controlRefs = [];
  function buildControls() {
    const root = $('controls');
    const exp = document.createElement('section');
    exp.className = 'sec';
    exp.innerHTML = '<h2 class="overline">Experiments</h2><div class="presets" id="presets"></div>';
    root.appendChild(exp);
    const list = exp.querySelector('#presets');
    const free = document.createElement('button');
    free.className = 'preset'; free.dataset.id = '';
    free.innerHTML = '<span class="tag">Free</span><span class="pt">Free play</span>';
    list.appendChild(free);
    for (const p of PRESETS) {
      const b = document.createElement('button');
      b.className = 'preset'; b.dataset.id = p.id;
      b.innerHTML = `<span class="tag">${p.tag}</span><span class="pt">${esc(p.title)}</span>`;
      list.appendChild(b);
    }
    list.addEventListener('click', e => {
      const b = e.target.closest('.preset');
      if (!b) return;
      if (b.dataset.id) applyPreset(PRESETS.find(p => p.id === b.dataset.id), 0);
      else { ui.preset = null; ui.modified = false; renderExperiment(); refreshControls(); }
    });
    buildArchitecture(root);

    for (const sec of CONTROL_SECTIONS) {
      const s = document.createElement('section');
      s.className = 'sec';
      s.innerHTML = `<h2 class="overline">${sec.title}</h2>`;
      const actions = document.createElement('div');
      actions.className = 'actions';
      for (const item of sec.items) {
        if (item.kind === 'slider') {
          const w = document.createElement('div');
          w.className = 'ctl';
          const id = `s-${item.key}`;
          w.innerHTML = `<div class="row"><label class="label" for="${id}">${item.label}</label><output for="${id}"></output></div>
            <input type="range" id="${id}" min="${item.min}" max="${item.max}" step="${item.step}"><span class="caption need"></span>`;
          const input = w.querySelector('input'), out = w.querySelector('output'), need = w.querySelector('.need');
          input.addEventListener('input', () => { out.textContent = item.fmt(input.value); change({ [item.key]: Number(input.value) }); });
          controlRefs.push(() => {
            input.value = sim.cfg[item.key]; out.textContent = item.fmt(sim.cfg[item.key]);
            const on = itemEnabled(item); input.disabled = !on; w.classList.toggle('off', !on); need.textContent = on ? '' : NEEDS_TEXT[item.needs];
          });
          s.appendChild(w);
        } else if (item.kind === 'switch') {
          const w = document.createElement('div');
          w.className = 'ctl sw-row';
          const lid = `l-${item.key}`;
          w.innerHTML = `<div><span class="label" id="${lid}">${item.label}</span><span class="caption"></span></div>
            <button class="switch" role="switch" aria-labelledby="${lid}"><span class="knob"></span></button>`;
          const btn = w.querySelector('button'), cap = w.querySelector('.caption');
          btn.addEventListener('click', () => change({ [item.key]: !sim.cfg[item.key] }));
          controlRefs.push(() => {
            btn.setAttribute('aria-checked', String(!!sim.cfg[item.key]));
            const on = itemEnabled(item); btn.disabled = !on; w.classList.toggle('off', !on);
            cap.textContent = on ? item.hint : NEEDS_TEXT[item.needs];
          });
          s.appendChild(w);
        } else if (item.kind === 'seg') {
          const g = document.createElement('div');
          g.className = 'seg'; g.setAttribute('role', 'radiogroup'); g.setAttribute('aria-label', item.label);
          for (const [val, text] of item.options) {
            const b = document.createElement('button');
            b.setAttribute('role', 'radio'); b.dataset.val = val; b.textContent = text;
            b.addEventListener('click', () => change({ [item.key]: val }));
            g.appendChild(b);
          }
          controlRefs.push(() => { for (const b of g.children) b.setAttribute('aria-checked', String(b.dataset.val === sim.cfg[item.key])); });
          s.appendChild(g);
        } else if (item.kind === 'button') {
          const b = document.createElement('button');
          b.className = `btn block${item.tone === 'danger' ? ' danger' : ''}`;
          b.textContent = item.label;
          b.addEventListener('click', () => sim.action(item.action));
          actions.appendChild(b);
        }
      }
      if (actions.children.length) s.appendChild(actions);
      root.appendChild(s);
    }
  }

  function refreshControls() {
    for (const sync of controlRefs) sync();
    for (const sync of archRefs) sync();
    for (const b of document.querySelectorAll('#presets .preset')) b.setAttribute('aria-pressed', String((b.dataset.id || null) === (ui.preset ? ui.preset.id : null)));
  }

  function change(patch) {
    sim.setCfg(patch);
    if (ui.preset) { ui.modified = true; renderExperiment(); }
    refreshControls();
    renderTour();
  }

  function applyPreset(preset, variant) {
    ui.preset = preset; ui.variant = variant; ui.modified = false;
    sim.cfg = { ...DEFAULT_CFG, ...archOf(sim.cfg), seed: sim.cfg.seed, ...preset.base, ...preset.variants[variant].patch };
    restart();
    ui.table = runTable(preset, sim.cfg.seed, archOf(sim.cfg));
    renderExperiment();
    refreshControls();
  }

  function restart() {
    sim.reset();
    if (ui.preset && !ui.modified) for (const a of ui.preset.at || []) sim.at(a.t, a.action);
    history.clear();
    tickerLines.length = 0;
    inspect(null);
    hooks.clear();
  }

  // ===== Metrics panel =====
  function buildMetrics() {
    $('metrics').innerHTML = `
      <section class="sec">
        <h2 class="overline">Now <span class="badge sand">sandbox</span></h2>
        <div class="big3">
          <div class="big"><span class="caption">Cache pressure</span><span class="metric" id="m-pressure">0.00</span><span class="caption">context ÷ pods' KV</span></div>
          <div class="big"><span class="caption">Remaining work</span><span class="metric" id="m-remaining">0</span><span class="caption">steps left</span></div>
          <div class="big"><span class="caption">Pods Ready</span><span class="metric" id="m-pods">0</span><span class="caption" id="m-pods-cap"></span></div>
        </div>
        <dl class="rows" id="m-rows"></dl>
      </section>
      <section class="sec" id="exp"></section>
      <section class="sec">
        <h2 class="overline">What each autoscaler would ask for</h2>
        <canvas class="chart" id="timeline" role="img" aria-label="Pods each signal would ask for over the last three minutes, with the pods Ready and wanted"></canvas>
        <div class="chart-legend" id="tl-legend"></div>
      </section>
      <section class="sec">
        <h2 class="overline">Pressure on the measured curve</h2>
        <canvas class="chart" id="curve" role="img" aria-label="Measured first-token time by cache pressure, with the sandbox's pressure now"></canvas>
        <p class="caption" id="curve-cap" style="margin-top:8px"></p>
      </section>`;
    $('tl-legend').innerHTML = [
      ['remaining', 'accent', 'Remaining work ÷ target'], ['queue', 'draining', 'Queue ÷ target'], ['pressure', 'failed-ink', "Context ÷ one pod's KV"],
      ['ready', 'ready', 'Pods Ready'], ['desired', 'starting', 'Pods wanted'],
    ].map(([k, c, t]) => `<span data-k="${k}"><i class="${k === 'desired' ? 'dash' : ''}" style="background:var(--${c});color:var(--${c})"></i>${t}</span>`).join('');
  }

  function renderExperiment() {
    const box = $('exp');
    const p = ui.preset;
    if (!p) {
      box.innerHTML = `<h2 class="overline">Experiment</h2>
        <p class="watch">Free play: change anything on the left and watch the calls move. Pick an experiment to set the sandbox up like one of the thesis's and see what was measured.</p>
        <p class="caption">The sandbox is a model of the system with illustrative timings. Its numbers show the mechanisms, not the results.</p>`;
      return;
    }
    const live = i => (i === ui.variant && !ui.modified ? 'live' : '');
    const cell = (snaps, k) => {
      const s = spread(snaps, k);
      if (!s) return '–';
      const show = METRICS[k].show;
      return s.lo === s.hi ? show(s.med) : `${show(s.med)}<span class="rng">${show(s.lo)} to ${show(s.hi)}</span>`;
    };
    const prof = currentProfile();
    const archNote = isThesisArch() ? '' : `<p class="arch-warn">Measured on the thesis setup (two pods on one RTX 3050, Qwen3-0.6B, 4,384 KV tokens per pod). The sandbox now runs ${prof ? esc(prof.name) : 'a custom architecture'}: compare the mechanisms, not the numbers.</p>`;
    // Each metric's name on its own row over the variants' values, so three variants fit the panel's width.
    const head = p.variants.map((v, i) => `<th class="${live(i)}">${esc(v.label)}</th>`).join('');
    const rows = p.metrics.map(k => `<tr class="name"><th colspan="${p.variants.length}">${METRICS[k].label}</th></tr>
      <tr>${ui.table.map((snaps, i) => `<td class="${live(i)}">${cell(snaps, k)}</td>`).join('')}</tr>`).join('');
    box.innerHTML = `
      <div class="exp-head"><span class="tag on">${p.tag}</span><h2>${esc(p.title)}</h2></div>
      <p class="watch">${esc(p.watch)}</p>
      <div class="variants" role="group" aria-label="Variant shown live">${p.variants.map((v, i) => `<button class="btn" data-v="${i}" aria-pressed="${i === ui.variant && !ui.modified}">${esc(v.label)}</button>`).join('')}</div>
      ${ui.modified ? '<p class="caption" style="margin-bottom:8px">You changed a setting, so the live view no longer matches a variant. Pick one to go back.</p>' : ''}
      <div class="m-head"><h3 class="overline">Each variant, ${C.seeds} seeds</h3><span class="badge sand">sandbox</span></div>
      <p class="caption" style="margin-bottom:4px">The median, with the range over the seeds under it.</p>
      <table class="cmp"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>
      <div class="m-head"><h3 class="overline">Measured</h3><span class="badge meas">experiments/log.md, ${p.source}</span></div>
      ${archNote}
      <dl class="measured">${p.measured.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
      <p class="caption">The sandbox table runs each variant on seeds ${sim.cfg.seed} to ${sim.cfg.seed + C.seeds - 1}, ${p.horizon} s of model time each; within a seed every variant gets the same arrivals. Only the Measured box is a result.</p>`;
    box.querySelector('.variants').addEventListener('click', e => {
      const b = e.target.closest('button[data-v]');
      if (b) applyPreset(p, Number(b.dataset.v));
    });
  }

  function updateHud() {
    const s = sim.snapshot(true);
    const pr = $('m-pressure');
    pr.textContent = s.pressure.toFixed(2);
    pr.classList.toggle('warn', s.pressure >= 1);
    $('m-remaining').textContent = String(s.remaining);
    $('m-pods').textContent = String(s.ready);
    $('m-pods-cap').textContent = `${s.starting} starting · ${s.desired} wanted`;
    const rows = [
      ['Active runs', s.live], ['Runs finished / failed', `${s.done} / ${s.failed}`], ['Calls waiting', s.queue],
      ['Later steps moved', `${s.moved} of ${s.laterSteps}`], ['Spills / remaps', `${s.spills} / ${s.remaps}`],
      ['Cached share, last 60 s', s.cached === null ? '–' : s.cached.toFixed(2)],
      ['First token p50 / p95, 60 s', `${secs(s.ttft50)} / ${secs(s.ttft95)}`],
      ['Run time p50', secs(s.runP50)], ['Tokens evicted', `${(s.evicted / 1000).toFixed(1)}k`],
      ['Retries · interrupted runs', `${s.retries} · ${s.interrupted}`], ['Idle pod-minutes', s.idlePodMin.toFixed(1)],
    ];
    $('m-rows').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
    $('clock').innerHTML = `t <b>${sim.t.toFixed(0)} s</b> · seed <b>${sim.cfg.seed}</b> · <b>${ui.speed}×</b>${ui.playing ? '' : ' · paused'}`;
    for (const el of $('tl-legend').children) el.classList.toggle('active', el.dataset.k === sim.cfg.autoscaler);
    const b = MEASURED_CURVE.find(x => s.pressure >= x.lo && s.pressure < x.hi) || MEASURED_CURVE[MEASURED_CURVE.length - 1];
    $('curve-cap').textContent = `Measured on real pods (P4.9, 7,412 calls). Sandbox pressure now ${s.pressure.toFixed(2)}: at ${b.lo}–${b.hi} the real pods gave a first-token p50 of ${b.p50} s, p90 ${b.p90} s, cached share ${b.cached}.`;
  }

  // ===== Charts =====
  function prepCanvas(cv) {
    const r = cv.getBoundingClientRect();
    const d = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(10, Math.round(r.width)), h = Math.max(10, Math.round(r.height));
    if (cv.width !== w * d || cv.height !== h * d) { cv.width = w * d; cv.height = h * d; }
    const ctx = cv.getContext('2d');
    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = '10.5px "JetBrains Mono", monospace';
    return { ctx, w, h };
  }

  function drawTimeline() {
    const { ctx, w, h } = prepCanvas($('timeline'));
    const S = sim.series, span = 180, now = sim.t, t0 = now - span;
    const L = 24, R = 6, T = 6, B = 16;
    let top = sim.cfg.maxPods;
    for (const p of S) top = Math.max(top, p.asks.remaining, p.asks.queue, p.asks.pressure, p.ready + p.starting);
    top = Math.min(16, top + 1);
    const X = t => L + ((t - t0) / span) * (w - L - R);
    const Y = v => T + (1 - Math.min(v, top) / top) * (h - T - B);
    ctx.strokeStyle = P.line; ctx.fillStyle = P['ink-muted']; ctx.lineWidth = 1;
    const stepY = top > 8 ? 2 : 1;
    for (let v = 0; v <= top; v += stepY) { ctx.beginPath(); ctx.moveTo(L, Y(v) + 0.5); ctx.lineTo(w - R, Y(v) + 0.5); ctx.stroke(); ctx.fillText(String(v), 4, Y(v) + 3.5); }
    ctx.fillText('−3 min', L, h - 3);
    ctx.textAlign = 'right'; ctx.fillText('now', w - R, h - 3); ctx.textAlign = 'left';
    const pts = S.filter(p => p.t >= t0);
    if (pts.length < 2) return;
    const line = (get, color, width, dash, stepped) => {
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
      ctx.beginPath();
      pts.forEach((p, i) => {
        const x = X(p.t), y = Y(get(p));
        if (i === 0) ctx.moveTo(x, y);
        else if (stepped) { ctx.lineTo(x, Y(get(pts[i - 1]))); ctx.lineTo(x, y); } else ctx.lineTo(x, y);
      });
      ctx.stroke(); ctx.setLineDash([]);
    };
    const act = sim.cfg.autoscaler;
    line(p => p.asks.pressure, P['failed-ink'], act === 'pressure' ? 2.5 : 1.25);
    line(p => p.asks.queue, P.draining, act === 'queue' ? 2.5 : 1.25);
    line(p => p.asks.remaining, P.accent, act === 'remaining' ? 2.5 : 1.25);
    line(p => p.ready, P.ready, 2, null, true);
    line(p => p.desired, P.starting, 1.5, [4, 3], true);
  }

  function drawCurve() {
    const { ctx, w, h } = prepCanvas($('curve'));
    const L = 34, R = 8, T = 8, B = 18, xmax = 4.4, ymin = 0.2, ymax = 50;
    const X = v => L + (Math.min(v, xmax) / xmax) * (w - L - R);
    const Y = v => T + (1 - (Math.log10(v) - Math.log10(ymin)) / (Math.log10(ymax) - Math.log10(ymin))) * (h - T - B);
    ctx.strokeStyle = P.line; ctx.fillStyle = P['ink-muted']; ctx.lineWidth = 1;
    for (const v of [0.5, 1, 5, 10, 30]) { ctx.beginPath(); ctx.moveTo(L, Y(v) + 0.5); ctx.lineTo(w - R, Y(v) + 0.5); ctx.stroke(); ctx.fillText(v < 1 ? `${v}s` : `${v} s`, 2, Y(v) + 3.5); }
    for (const v of [0, 1, 2, 3, 4]) ctx.fillText(String(v), X(v) - 3, h - 4);
    ctx.setLineDash([2, 3]); ctx.strokeStyle = P['line-strong'];
    ctx.beginPath(); ctx.moveTo(X(1) + 0.5, T); ctx.lineTo(X(1) + 0.5, h - B); ctx.stroke(); ctx.setLineDash([]);
    const s = sim.pressureNow();
    const b = MEASURED_CURVE.find(x => s >= x.lo && s < x.hi) || MEASURED_CURVE[MEASURED_CURVE.length - 1];
    ctx.fillStyle = s >= 1 ? P['failed-ink'] : P['accent-ink'];
    ctx.globalAlpha = 0.12; ctx.fillRect(X(b.lo), T, X(b.hi) - X(b.lo), h - T - B); ctx.globalAlpha = 1;
    const steps = (key, color, width, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
      ctx.beginPath();
      MEASURED_CURVE.forEach((m, i) => { if (i === 0) ctx.moveTo(X(m.lo), Y(m[key])); else ctx.lineTo(X(m.lo), Y(m[key])); ctx.lineTo(X(m.hi), Y(m[key])); });
      ctx.stroke(); ctx.setLineDash([]);
    };
    steps('p90', P['ink-muted'], 1.25, [4, 3]);
    steps('p50', P.ink, 2);
    ctx.fillStyle = P['ink-muted'];
    ctx.fillText('p90', X(4.31) - 22, Y(41.7) - 5);
    ctx.fillStyle = P.ink;
    ctx.fillText('p50', X(4.31) - 22, Y(23.1) + 13);
    const mx = X(s);
    ctx.strokeStyle = s >= 1 ? P.failed : P.accent; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(mx, T); ctx.lineTo(mx, h - B); ctx.stroke();
    ctx.fillStyle = s >= 1 ? P.failed : P.accent;
    ctx.beginPath(); ctx.arc(mx, Y(b.p50), 4, 0, Math.PI * 2); ctx.fill();
    ctx.textAlign = mx > w - 90 ? 'right' : 'left';
    ctx.fillText(`now ${s.toFixed(2)}`, mx + (mx > w - 90 ? -6 : 6), T + 10);
    ctx.textAlign = 'left';
  }

  // ===== Event log and run history =====
  const tickerLines = [];
  const history = new Map();
  function tick(html) { tickerLines.push(`<div class="tk"><span>${sim.t.toFixed(1).padStart(6, ' ')} s</span> ${html}</div>`); if (tickerLines.length > 40) tickerLines.shift(); }
  function renderTicker() { $('ticker').innerHTML = tickerLines.slice(-6).reverse().join(''); }

  function onSimEvent(type, d) {
    if (type === 'start') {
      const r = d.call.run, share = d.hit / d.call.prompt;
      const h = history.get(r.id) || [];
      const prev = h.length ? h[h.length - 1].pod : null;
      h.push({ step: d.call.step, index: d.call.index, pod: d.pod.name, reason: d.call.reason, cached: share, ttft: d.call.ttft, moved: d.call.step > 1 && prev !== null && prev !== d.pod.name });
      history.set(r.id, h);
      tick(`<b>${esc(r.name)}</b> step ${d.call.step}/${r.steps} → <b>${d.pod.name}</b> · ${reason(d.call.reason).text} · <span class="${share >= 0.5 ? 'hit' : 'miss'}">${Math.round(share * 100)}% cached</span> · first token ${d.call.ttft.toFixed(2)} s`);
    } else if (type === 'evict') {
      const run = sim.runs.find(r => r.id === d.runId);
      tick(`<span class="bad">${d.pod.name} threw out ${fmtInt(d.tokens)} tokens</span> of ${run ? esc(run.name) : 'a finished run'}: its memory was full`);
    } else if (type === 'retry') {
      tick(`<span class="bad">${esc(d.call.run.name)} step ${d.call.step}: ${d.why}</span>, retry ${d.call.attempt} in ${sim.cfg.retryBackoff} s`);
    } else if (type === 'runFail') {
      tick(`<span class="bad">${esc(d.run.name)} failed: ${d.why}</span>`);
    } else if (type === 'runEnd') {
      tick(`<b>${esc(d.run.name)}</b> finished, ${d.run.steps} steps in ${(d.run.endT - d.run.startT).toFixed(1)} s`);
    } else if (type === 'pod') {
      if (d.what !== 'gone') tick(`<span class="pod">${d.pod.name} ${d.what}</span>`);
    } else if (type === 'scale') {
      tick(`<span class="pod">autoscaler on ${d.signal === 'remaining' ? 'remaining work' : d.signal}: ${d.to} pods wanted</span>`);
    } else if (type === 'burst') {
      tick(`<b>burst</b>: ${d.size} runs at once`);
    } else if (type === 'cfg') {
      refreshControls();
    }
  }

  // ===== Run inspector =====
  let inspected = null;
  function inspect(run) {
    inspected = run;
    $('inspector').hidden = !run;
    renderInspector();
  }
  function renderInspector() {
    const box = $('inspector');
    const r = inspected;
    if (!r) return;
    const n = Math.max(r.expected, r.step);
    const pips = Array.from({ length: n }, (_, i) => `<i class="${i < r.step - (r.state === 'call' ? 1 : 0) ? 'done' : i === r.step - 1 && r.state === 'call' ? 'now' : ''}"></i>`).join('');
    const pin = r.pin !== null ? sim.podById(r.pin) : null;
    const state = { tool: 'using a tool', call: 'waiting for the LLM', done: 'finished', failed: 'failed' }[r.state];
    const rows = (history.get(r.id) || []).slice(-12).map(e => `<li class="${e.moved ? 'moved' : ''}"><span class="n">${e.step}${e.index ? 'b' : ''}</span><span class="pod">${e.pod}</span><span class="why">${reason(e.reason).text}</span><span class="c ${e.cached >= 0.5 ? 'hit' : 'miss'}">${Math.round(e.cached * 100)}%</span></li>`).join('');
    box.innerHTML = `<div class="insp-head"><span class="run-sw" style="background:${runColor(r)}"></span><h3>${esc(r.name)}</h3><button class="icon-btn" id="insp-close" aria-label="Stop following this run">×</button></div>
      <p class="caption">Step ${r.step} of ${r.steps} (the agent expects ${r.expected}) · ${state}</p>
      <div class="steps-bar">${pips}</div>
      <p class="caption">Context ${fmtInt(r.latestPrompt)} tokens${pin ? ` · kept on ${pin.name}` : ''}</p>
      ${rows ? `<ol class="hist">${rows}</ol><p class="caption" style="margin-top:8px">Each call: the pod, why the picker chose it, how much of the prompt was cached. Amber pod names are moves.</p>` : '<p class="caption" style="margin-top:8px">No call has reached a pod yet.</p>'}`;
    box.querySelector('#insp-close').addEventListener('click', () => inspect(null));
  }

  // ===== Tooltip =====
  function describe(target) {
    if (target.kind === 'pod') {
      const p = sim.podById(target.id);
      if (!p) return null;
      const kv = sim.cfg.kvTokens;
      const cached = [...p.cache.values()].reduce((s, e) => s + e.tokens, 0);
      return `<h4>${p.name} · ${p.state}</h4>
        <span class="data">running ${p.running.length} of ${sim.cfg.maxSeqs} · waiting ${p.waiting.length}${p.kvBlocked ? ' (waiting for memory)' : ''}</span>
        <span class="data">KV in use ${fmtInt(sim.allocated(p))} of ${fmtInt(kv)} tokens</span>
        <span class="data">kept for later: ${p.cache.size} runs, ${fmtInt(cached)} tokens</span>
        <span class="data">runs kept here ${sim.pinsOn(p)} · they need ${(sim.demandOn(p) / kv).toFixed(2)}× its KV</span>`;
    }
    const r = sim.runs.find(x => x.id === target.id);
    if (!r) return null;
    const pin = r.pin !== null ? sim.podById(r.pin) : null;
    const where = pin ? `kept on ${pin.name}` : r.prevPod !== null ? `last call on vllm-${r.prevPod}` : 'not sent yet';
    return `<h4>${esc(r.name)}</h4>
      <span class="data">${{ tool: 'using a tool', call: 'call in flight', done: 'finished', failed: 'failed' }[r.state]}</span>
      <span class="data">step ${r.step} of ${r.steps} · expected ${r.expected}</span>
      <span class="data">context ${fmtInt(r.latestPrompt)} tokens · ${where}</span>
      <span class="data">click to follow it</span>`;
  }
  function showTip(target, x, y) {
    const tip = $('tooltip');
    const html = target ? describe(target) : null;
    if (!html) { tip.hidden = true; return; }
    tip.innerHTML = html;
    tip.hidden = false;
    const stage = $('stage').getBoundingClientRect();
    tip.style.left = `${Math.min(x + 14, stage.width - 270)}px`;
    tip.style.top = `${Math.min(y + 14, stage.height - 130)}px`;
  }

  // ===== Guided tour =====
  // Each step says what happens; see names the page's own hint for where to look (hooks.see), since the 3D and the
  // 2D page draw it differently.
  const TOUR = [
    { view: 'overview', title: 'A working model of the thesis system',
      text: 'Agent tasks send LLM calls through a gateway to vLLM pods, and an autoscaler adds and removes pods. Everything here runs live in your browser. The tour takes two minutes.' },
    { view: 'runs', title: 'One task, many calls', see: 'runs',
      text: 'A chat message becomes a run: the agent calls the LLM, runs a tool, calls it again. Every call resends the whole conversation, so each call carries more than the last.',
      tryIt: () => ['Send 5 runs now', () => sim.action('burst')] },
    { view: 'gateway', title: 'The gateway picks a pod', see: 'gateway',
      text: "Every call passes the gateway's Endpoint Picker, which chooses the vLLM pod that serves it." },
    { view: 'pods', title: 'A pod remembers', see: 'pods',
      text: 'Each pod keeps recent contexts in GPU memory, the KV cache. A call whose context is still there starts at once; otherwise the pod first rereads the whole prompt.' },
    { view: 'pods', title: 'Memory runs out', see: 'evict',
      text: "When the cache is full, the pod throws out the context it used longest ago, and that run's next call must reread it.",
      tryIt: () => ['Add load: 10 runs a minute', () => change({ rate: 10 })] },
    { view: 'pods', title: 'Task affinity', see: 'affinity',
      text: 'The thesis plugin keeps each run on the pod that holds its context. With round-robin each call goes to the next pod in turn, so contexts end up everywhere and the rereads grow.',
      tryIt: () => (sim.cfg.routing === 'round-robin'
        ? ['Back to the thesis plugin', () => change({ routing: 'plugin', affinity: true })]
        : ['Switch to round-robin', () => change({ routing: 'round-robin' })]) },
    { view: 'pressure', title: 'Cache pressure', see: 'pressure',
      text: "Add up the live runs' contexts and divide by all the pods' memory. Under 1, good routing finds the cache. Over 1 no routing can, and calls wait: the curve measured on real pods (P4.9), on the right." },
    { view: 'autoscaler', title: 'Autoscaling', see: 'autoscaler',
      text: 'KEDA reads one number every few seconds and sets the pod count. Remaining work counts the steps the live runs still have.',
      tryIt: () => ['Scale on remaining work', () => change({ autoscaler: 'remaining' })] },
    { view: 'pods', title: 'Scale-down and failure', see: 'drain',
      text: 'A pod being removed drains: it takes no new calls and finishes its own. When a pod dies, its calls go back through the gateway and are retried on another pod, without their cache.',
      tryIt: () => ['Kill the busiest pod', () => sim.action('kill')] },
    { view: 'overview', title: 'Your turn', see: 'end',
      text: "Pick an experiment on the left to set the sandbox up like one of the thesis's and compare it with what was measured. Change the architecture to try your own hardware. Click a run to follow it." },
  ];
  let tourAt = -1;
  function openTour(i) {
    tourAt = i;
    if (i >= 0) { hooks.focus(TOUR[i].view); markView(TOUR[i].view); }
    renderTour();
  }
  function renderTour() {
    const box = $('tour');
    if (tourAt < 0) { box.hidden = true; return; }
    const st = TOUR[tourAt], last = tourAt === TOUR.length - 1;
    const tryIt = st.tryIt ? st.tryIt() : null;
    box.hidden = false;
    box.innerHTML = `<div class="tour-step">${tourAt === 0 ? 'Welcome' : `${tourAt} of ${TOUR.length - 1}`}</div>
      <h3>${esc(st.title)}</h3><p>${esc(st.text)}</p>${st.see && hooks.see[st.see] ? `<p class="see">${esc(hooks.see[st.see])}</p>` : ''}
      <div class="tour-nav"><div class="tour-dots">${TOUR.map((_, i) => `<i class="${i === tourAt ? 'on' : ''}"></i>`).join('')}</div>
        ${tryIt ? `<button class="btn sm" id="tour-try">${esc(tryIt[0])}</button>` : ''}
        ${tourAt > 0 ? '<button class="btn sm" id="tour-back">Back</button>' : '<button class="btn sm" id="tour-skip">Explore on my own</button>'}
        <button class="btn sm primary" id="tour-next">${tourAt === 0 ? 'Start the tour' : last ? 'Done' : 'Next'}</button></div>`;
    if (tryIt) box.querySelector('#tour-try').addEventListener('click', () => { tryIt[1](); renderTour(); });
    const back = box.querySelector('#tour-back'), skip = box.querySelector('#tour-skip');
    if (back) back.addEventListener('click', () => openTour(tourAt - 1));
    if (skip) skip.addEventListener('click', closeTour);
    box.querySelector('#tour-next').addEventListener('click', () => (last ? closeTour() : openTour(tourAt + 1)));
  }
  function closeTour() {
    tourAt = -1;
    renderTour();
    try { localStorage.setItem('atlas-welcomed', '1'); } catch (err) {
      // Storage can be unavailable; the welcome then shows again next visit.
    }
  }

  // ===== Views =====
  function markView(id) { for (const b of $('views').children) b.setAttribute('aria-checked', String(b.dataset.view === id)); }
  function buildViews() {
    const box = $('views');
    for (const v of hooks.views) {
      const b = document.createElement('button');
      b.setAttribute('role', 'radio'); b.dataset.view = v.id; b.textContent = v.label;
      b.addEventListener('click', () => { hooks.focus(v.id); markView(v.id); });
      box.appendChild(b);
    }
    $('tour-btn').addEventListener('click', () => openTour(1));
  }

  // ===== Top bar =====
  function buildTopbar() {
    const speed = $('speed');
    for (const s of [0.5, 1, 2, 4]) {
      const b = document.createElement('button');
      b.setAttribute('role', 'radio'); b.textContent = `${s}×`; b.dataset.s = String(s);
      b.addEventListener('click', () => { ui.speed = s; syncTopbar(); });
      speed.appendChild(b);
    }
    $('play').addEventListener('click', () => { ui.playing = !ui.playing; syncTopbar(); });
    $('reset').addEventListener('click', () => restart());
    $('seed').addEventListener('change', e => {
      const v = Math.max(1, Math.min(9999, Math.round(Number(e.target.value)) || DEFAULT_CFG.seed));
      e.target.value = v;
      sim.cfg.seed = v;
      if (ui.preset) applyPreset(ui.preset, ui.variant); else restart();
    });
    $('theme').addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = theme;
      try { localStorage.setItem('atlas-theme', theme); } catch (err) {
        // Storage can be unavailable; the theme still changes for this visit.
      }
      readPalette();
      $('theme').textContent = theme === 'dark' ? 'Light' : 'Dark';
      hooks.retheme();
    });
    $('theme').textContent = theme === 'dark' ? 'Light' : 'Dark';
    $('focus').addEventListener('click', () => {
      document.body.classList.toggle('focus');
      $('focus').textContent = document.body.classList.contains('focus') ? 'Show panels' : 'Hide panels';
    });
    document.addEventListener('keydown', e => {
      if (e.code === 'Space' && !(e.target instanceof HTMLInputElement) && !(e.target instanceof HTMLButtonElement)) { e.preventDefault(); ui.playing = !ui.playing; syncTopbar(); }
      if (e.code === 'Escape') { if (tourAt >= 0) closeTour(); else inspect(null); }
    });
  }
  function syncTopbar() {
    $('play').textContent = ui.playing ? 'Pause' : 'Play';
    $('play').setAttribute('aria-pressed', String(ui.playing));
    for (const b of $('speed').children) b.setAttribute('aria-checked', String(Number(b.dataset.s) === ui.speed));
  }

  // ===== Each frame =====
  const acc = { hud: 1, chart: 1 };
  function frame(dt) {
    if (ui.playing) sim.step(dt * ui.speed);
    acc.hud += dt; acc.chart += dt;
    if (acc.hud > 0.2) { updateHud(); renderTicker(); renderInspector(); acc.hud = 0; }
    if (acc.chart > 0.25) { drawTimeline(); drawCurve(); acc.chart = 0; }
  }

  function mount(pageHooks) {
    hooks = { ...hooks, ...pageHooks };
    buildTopbar();
    buildControls();
    buildMetrics();
    buildViews();
    sim.on(onSimEvent);
    const link = readLink();
    if (link.preset) applyPreset(link.preset, link.variant); else restart();
    if (link.t > 0) sim.step(link.t);
    renderExperiment();
    refreshControls();
    syncTopbar();
    const view = hooks.views.some(v => v.id === link.view) ? link.view : 'overview';
    hooks.focus(view, true); markView(view);
    let welcomed = false;
    try { welcomed = localStorage.getItem('atlas-welcomed') === '1'; } catch (err) {
      // Storage can be unavailable; then the welcome shows.
    }
    if (link.tour !== null) openTour(Math.min(TOUR.length - 1, Math.max(0, Number(link.tour) || 0)));
    else if (!link.any && !welcomed) openTour(0);
    return {
      sim, ui, C, palette: () => P, theme: () => theme, runHSL, runColor, reason, frame, showTip, inspect,
      inspected: () => inspected, tick, fonts: '"Hanken Grotesk", system-ui, sans-serif', mono: '"JetBrains Mono", ui-monospace, monospace',
    };
  }

  // The sim, palette and colours are there before mount, so a page can build its scene first.
  globalThis.AtlasPanels = { mount, sim, palette: () => P, theme: () => theme, runHSL, runColor, reason };
})();
