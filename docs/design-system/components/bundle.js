/* Agent UI templates: each function takes plain data and returns the markup the Angular components render. No framework. */
window.AgentUI = (function () {
  var NAME = 'Tenax';

  /* ---- The pixel horse: 14 x 12 art pixels, '#' filled, 'o' the eye (a hole). Rows 0-6 never move; rows 7-11 are the frames. ---- */
  var TOP = [
    '..........##..',
    '.........####.',
    '........##o###',
    '........######',
    '.......####..#',
    '......####....',
    '#########.....'
  ];
  var STAND = ['#.#######.....', '#.#######.....', '..#.#..#.#....', '..#.#..#.#....', '..#.#..#.#....'];
  var TROT_A = ['#.#######.....', '#.#######.....', '..#.#..#.#....', '...##..#..#...', '....#..#......'];
  var TROT_B = ['#.#######.....', '..#######.....', '..#.#..#.#....', '..#..#..##....', '..#......#....'];

  function pixels(rows, y0) {
    var d = '';
    rows.forEach(function (row, i) {
      var x = 0;
      while (x < row.length) {
        if (row[x] === '#') {
          var s = x;
          while (row[x] === '#') x++;
          d += 'M' + s + ' ' + (y0 + i) + 'h' + (x - s) + 'v1h-' + (x - s) + 'z';
        } else x++;
      }
    });
    return d;
  }

  function horse(o) {
    o = o || {};
    return '<svg class="ag-horse' + (o.size ? ' ag-horse--' + o.size : '') + '" viewBox="0 0 14 12" data-state="' + (o.state || 'idle') + '" role="img" aria-label="' + (o.label || NAME) + '">' +
      '<path d="' + pixels(TOP, 0) + '"/>' +
      '<path class="stand" d="' + pixels(STAND, 7) + '"/>' +
      '<path class="trot-a" d="' + pixels(TROT_A, 7) + '"/>' +
      '<path class="trot-b" d="' + pixels(TROT_B, 7) + '"/>' +
      '<rect class="lid" x="10" y="2" width="1" height="1"/></svg>';
  }

  /* ---- Icons, 16 x 16 ---- */
  var ICONS = {
    send: 'M8 13V3M4 7l4-4 4 4',
    plus: 'M8 3v10M3 8h10',
    moon: 'M13 9.6A5.5 5.5 0 1 1 6.4 3a4.4 4.4 0 0 0 6.6 6.6z',
    chevron: 'M4 6l4 4 4-4',
    close: 'M4 4l8 8M12 4l-8 8',
    table: 'M3.5 3h9a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM2.5 6.5h11M6.5 6.5V13',
    doc: 'M4 2.5h5l3 3v8H4zM9 2.5v3h3M6 9h4M6 11h3',
    chat: 'M4.5 2.5h7a2 2 0 0 1 2 2v4.5a2 2 0 0 1-2 2H7.5L4.5 13.5V11h0a2 2 0 0 1-2-2V4.5a2 2 0 0 1 2-2z',
    play: 'M5 3.5v9l7-4.5z',
    power: 'M8 2.5V7.5M4.8 4.6a4.8 4.8 0 1 0 6.4 0',
    info: 'M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 7.5V11M8 5h.01',
    warn: 'M8 2.5l6 10.5H2zM8 6.5v3M8 11.3h.01',
    retry: 'M3 8a5 5 0 1 0 1.6-3.7M3 2.5v3h3',
    pods: 'M3 3.5h10v3.5H3zM3 9h10v3.5H3zM5.5 5.25h.01M5.5 10.75h.01',
    clock: 'M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 5v3.2l2 1.3',
    spill: 'M3 8h6M6 5l3 3-3 3M11 4v8'
  };
  function icon(name) { return '<svg class="ag-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="' + ICONS[name] + '"/></svg>'; }

  /* ---- Small parts ---- */
  function tag(text, kind) { return '<span class="ag-tag' + (kind ? ' ag-tag--' + kind : '') + '">' + text + '</span>'; }
  var STATUS = { ready: 'Ready', starting: 'Starting', draining: 'Draining', failed: 'Killed' };
  function status(state, label) { return '<span class="ag-status ag-status--' + state + '"><i class="ag-dot ag-dot--' + state + '"></i>' + (label || STATUS[state]) + '</span>'; }
  function btn(label, o) {
    o = o || {};
    return '<button class="ag-btn' + (o.kind ? ' ag-btn--' + o.kind : '') + (o.size ? ' ag-btn--' + o.size : '') + '"' + (o.disabled ? ' disabled' : '') + (o.aria ? ' aria-label="' + o.aria + '"' : '') + '>' + (o.icon ? icon(o.icon) : '') + label + '</button>';
  }
  function select(label) { return '<span class="ag-select">' + label + icon('chevron') + '</span>'; }
  function segmented(items, on) { return '<span class="ag-segmented">' + items.map(function (i) { return '<span' + (i === on ? ' class="on"' : '') + '>' + i + '</span>'; }).join('') + '</span>'; }
  function ms(v) { return v < 1000 ? v + ' ms' : (v / 1000).toFixed(1) + ' s'; }

  /* ---- Top bar ---- */
  function topbar(o) {
    return '<header class="ag-topbar"><span class="ag-brand">' + horse({ state: o.working ? 'working' : 'idle' }) + NAME + '</span>' +
      btn('New task', { kind: 'ghost', icon: 'plus', size: 'sm' }) + '<span class="ag-spacer"></span>' +
      '<span class="ag-profile">Profile <b>' + o.profile + '</b> · <code>' + o.model + '</code></span>' +
      btn('', { kind: 'ghost', icon: 'moon', aria: 'Switch theme' }).replace('ag-btn--ghost', 'ag-btn--ghost ag-btn--icon') + '</header>';
  }

  /* ---- Composer ---- */
  function composer(o) {
    o = o || {};
    return '<div class="ag-composer' + (o.hero ? ' ag-composer--hero' : '') + '">' +
      '<div class="ag-input' + (o.value ? '' : ' ph') + '">' + (o.value || o.placeholder || 'Ask a follow-up') + '</div>' +
      '<div class="ag-composer-row">' + (o.scenario ? select(o.scenario) : '') + (o.source ? '<span class="ag-composer-hint ag-mono">' + o.source + '</span>' : '') +
      '<span class="ag-spacer"></span>' + (o.hint ? '<span class="ag-composer-hint">' + o.hint + '</span>' : '') +
      btn('', { kind: 'primary', icon: 'send', aria: 'Send', disabled: o.disabled }).replace('ag-btn--primary', 'ag-btn--primary ag-btn--send') + '</div></div>';
  }

  /* ---- New task ---- */
  function scenario(s) {
    return '<div class="ag-scenario' + (s.on ? ' ag-scenario--on' : '') + '"><h3>' + icon(s.icon) + s.title + '</h3>' +
      '<p>' + s.prompt + '</p><span class="ag-src">' + s.source + '</span>' +
      '<div class="ag-tools">' + s.tools.map(function (t) { return tag(t); }).join('') + '</div></div>';
  }
  function newTask(o) {
    return '<div class="ag-scroll"><div class="ag-column ag-hero">' + horse({ size: '4x' }) +
      '<h1>What should the agent work on?</h1><p>Pick a scenario. Each one runs on fixed data with deterministic tools; the model only decides which tool to call next.</p>' +
      composer(o.composer) + '<div class="ag-scenarios">' + o.scenarios.map(scenario).join('') + '</div></div></div>';
  }

  /* ---- Run ---- */
  function progress(segs) { return '<div class="ag-progress" aria-hidden="true">' + segs.map(function (s) { return '<i class="' + s + '"></i>'; }).join('') + '</div>'; }
  function runHead(o) {
    var now = o.done ? horse({}) + 'Finished in ' + o.elapsed : horse({ state: 'working' }) + 'Step ' + o.step + ' · ' + o.expected + ' expected';
    return '<div class="ag-run-head"><h1>' + o.title + '</h1><div class="ag-run-meta"><span class="ag-now">' + now + '</span>' +
      '<code>' + o.runId + '</code><span>' + o.type + '</span>' + (o.done ? '' : '<span>' + o.elapsed + '</span>') + '</div>' + progress(o.segments) + '</div>';
  }
  function tabs(items) {
    return '<div class="ag-tabs" role="tablist">' + items.map(function (t) {
      return '<span role="tab"' + (t.on ? ' class="on" aria-selected="true"' : '') + '>' + t.label + (t.count ? '<em>' + t.count + '</em>' : '') + '</span>';
    }).join('') + '</div>';
  }
  function stepCard(s) {
    var tags = '';
    if (s.state === 'running') tags = tag(s.pod) + tag('running', 'muted');
    else if (s.state !== 'pending') tags = tag(s.pod) + tag(s.cache, s.cache) + tag(ms(s.ms)) + (s.retry ? tag('retry ' + s.retry, 'retry') : '') + (s.spill ? tag('spilled', 'spill') : '');
    return '<div class="ag-step ag-step--' + s.state + '"><div class="ag-step-top"><span class="ag-step-n">' + s.n + '</span>' +
      (s.par ? '<span class="ag-step-par">∥ ' + s.par + '</span>' : '') + '</div>' +
      '<div class="ag-step-tool">' + s.tool + '</div>' + (s.args ? '<div class="ag-step-args">' + s.args + '</div>' : '') +
      (s.note ? '<div class="ag-step-note">' + s.note + '</div>' : '') + (tags ? '<div class="ag-tags">' + tags + '</div>' : '') + '</div>';
  }
  function steps(list, head) {
    return '<div class="ag-section"><div class="ag-section-head">Steps<small>' + head + '</small></div><div class="ag-steps">' + list.map(stepCard).join('') + '</div></div>';
  }
  function answer(o) {
    if (!o.html) return '<div class="ag-section"><div class="ag-answer-head">' + horse({ state: 'working' }) + 'Answer</div><div class="ag-answer-wait">' + icon('clock') + o.wait + '</div></div>';
    return '<div class="ag-section ag-answer"><div class="ag-answer-head">' + horse({}) + 'Answer</div>' + o.html + '<div class="ag-answer-foot">' + o.foot + '</div></div>';
  }
  function cite(n) { return '<span class="ag-cite" title="Step ' + n + '">' + n + '</span>'; }
  function notice(o) { return '<div class="ag-section"><div class="ag-notice ag-notice--' + o.kind + '">' + icon(o.icon) + '<div>' + o.html + '</div></div></div>'; }
  function stepTable(rows) {
    return '<div class="ag-section"><table class="ag-table"><thead><tr><th>#</th><th>Tool call</th><th>Pod</th><th>Cache</th><th>TTFT</th><th>Duration</th><th>Gap before</th><th>Tokens in / out</th></tr></thead><tbody>' +
      rows.map(function (r) {
        return '<tr><td><span class="ag-step-n">' + r.n + '</span></td><td><code>' + r.call + '</code>' + (r.par ? '<small>∥ ' + r.par + '</small>' : '') + '</td><td><code>' + r.pod + '</code></td><td>' + tag(r.cache, r.cache) + '</td>' +
          '<td>' + ms(r.ttft) + '</td><td>' + ms(r.ms) + '</td><td>' + ms(r.gap) + '</td><td>' + r.tin + ' / ' + r.tout + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }
  function run(o) {
    return '<div class="ag-scroll"><div class="ag-column">' + runHead(o.head) + tabs(o.tabs) + (o.blocks || []).join('') + '</div></div>' +
      '<div class="ag-dock">' + composer(o.composer) + '</div>';
  }

  /* ---- Step detail sheet ---- */
  function kv(pairs) { return '<dl class="ag-kv">' + pairs.map(function (p) { return '<dt>' + p[0] + '</dt><dd>' + p[1] + '</dd>'; }).join('') + '</dl>'; }
  function stepDetail(o) {
    var pct = Math.round(100 * o.cached / o.tin);
    return '<div class="ag-scrim"></div><aside class="ag-sheet" aria-label="Step ' + o.n + '"><div class="ag-sheet-head"><span class="ag-step-n">' + o.n + '</span><h2>' + o.tool + '</h2>' +
      '<span class="ag-spacer"></span>' + btn('', { kind: 'ghost', icon: 'close', aria: 'Close' }).replace('ag-btn--ghost', 'ag-btn--ghost ag-btn--icon') + '</div><div class="ag-sheet-body">' +
      '<section><h3 class="ag-kv-title">Call</h3><pre class="ag-code">' + o.call + '</pre></section>' +
      '<section><h3 class="ag-kv-title">Result · computed by the tool</h3><pre class="ag-code">' + o.result + '</pre></section>' +
      '<section><h3 class="ag-kv-title">Routing</h3>' + kv([['Pod', '<code>' + o.pod + '</code>'], ['Why', o.why], ['Cache', tag(o.cache, o.cache) + ' ' + o.cached.toLocaleString('en') + ' of ' + o.tin.toLocaleString('en') + ' prompt tokens cached']]) +
      '<div class="ag-cachebar"><i style="width:' + pct + '%"></i></div></section>' +
      '<section><h3 class="ag-kv-title">Timing</h3><div class="ag-stats">' +
      [[ms(o.ttft), 'Time to first token'], [ms(o.ms), 'Duration'], [ms(o.gap), 'Gap before (tool time)'], [o.tin.toLocaleString('en'), 'Prompt tokens'], [o.tout, 'Completion tokens'], [o.queue, 'Queue on arrival']]
        .map(function (s) { return '<div class="ag-stat"><b>' + s[0] + '</b><span>' + s[1] + '</span></div>'; }).join('') + '</div></section>' +
      '<section><h3 class="ag-kv-title">Headers · schemas/headers.v1.json</h3>' + kv(o.headers.map(function (h) { return ['<code>' + h[0] + '</code>', '<code>' + h[1] + '</code>']; })) + '</section>' +
      '</div></aside>';
  }

  /* ---- Cluster panel ---- */
  function spark(series, reps, max, repMax) {
    var w = 240, h = 56, n = series.length - 1;
    var x = function (i) { return (i * w / n).toFixed(1); };
    var y = function (v, top) { return (h - 2 - v * (h - 8) / top).toFixed(1); };
    var line = series.map(function (v, i) { return x(i) + ',' + y(v, max); }).join(' ');
    var steps = reps.map(function (v, i) { return (i ? x(i) + ',' + y(reps[i - 1], repMax) + ' ' : '') + x(i) + ',' + y(v, repMax); }).join(' ');
    return '<svg class="ag-spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true"><polygon class="area" points="0,' + h + ' ' + line + ' ' + w + ',' + h + '"/>' +
      '<polyline class="line" vector-effect="non-scaling-stroke" points="' + line + '"/><polyline class="reps" vector-effect="non-scaling-stroke" points="' + steps + '"/></svg>';
  }
  function remaining(o) {
    return '<section class="ag-psec"><h3 class="ag-psec-title">Remaining work<small>last 5 min</small></h3><div class="ag-rw">' +
      '<div class="ag-metric">' + o.value + '<small>steps</small></div>' + spark(o.series, o.reps, o.max, o.repMax) +
      '<div class="ag-rw-cap">Σ(<code>expected</code> − <code>step</code>) over ' + o.runs + ' active runs. KEDA scales on this.</div>' +
      '<div class="ag-rw-legend"><span><i></i>Remaining work</span><span><i class="reps"></i>Pods, 0 to ' + o.repMax + '</span></div></div>' +
      (o.scale ? '<div class="ag-scale">' + icon('pods') + o.scale + '</div>' : '') + '</section>';
  }
  function podCard(p) {
    var cells = [
      ['<b>' + p.run + ' · ' + p.wait + '</b><span>Running · waiting</span>', ''],
      ['<b>' + p.kv + '%</b><span>KV cache</span>', '<div class="ag-meter' + (p.kv > 85 ? ' ag-meter--warn' : '') + '"><i style="width:' + p.kv + '%"></i></div>'],
      ['<b>' + p.hit + '%</b><span>Prefix hits</span>', '<div class="ag-meter"><i style="width:' + p.hit + '%"></i></div>']
    ];
    return '<div class="ag-pod ag-pod--' + p.state + '"><div class="ag-pod-head"><span class="ag-pod-name">' + p.name + '</span>' + status(p.state, p.label) + '</div>' +
      (p.boot !== undefined ? '<div class="ag-meter ag-meter--start" style="margin-top:10px"><i style="width:' + p.boot + '%"></i></div>' : '') +
      '<div class="ag-pod-grid">' + cells.map(function (c) { return '<div>' + c[0] + c[1] + '</div>'; }).join('') + '</div>' +
      (p.note ? '<div class="ag-pod-note">' + p.note + '</div>' : '') + '</div>';
  }
  function pods(o) {
    return '<section class="ag-psec"><h3 class="ag-psec-title">Pods<small>' + o.caption + '</small></h3><div class="ag-pods">' + o.list.map(podCard).join('') + '</div></section>';
  }
  function runRow(r) {
    var pct = Math.min(100, Math.round(100 * r.step / r.exp));
    return '<div class="ag-runrow' + (r.you ? ' ag-runrow--you' : '') + '"><span class="ag-runrow-rank">' + r.rank + '</span>' +
      '<span><span class="ag-runrow-id">' + r.id + (r.you ? ' · you' : '') + '</span><span class="ag-runrow-type">' + r.type + '</span></span>' +
      '<span class="ag-runrow-bar">' + r.step + ' / ' + r.exp + '<span class="ag-meter"><i style="width:' + pct + '%"></i></span></span>' +
      '<span class="ag-runrow-pod">' + r.pod + '</span></div>';
  }
  function activeRuns(o) {
    return '<section class="ag-psec"><h3 class="ag-psec-title">Active runs<small>' + o.caption + '</small></h3><div class="ag-runs">' + o.rows.map(runRow).join('') +
      (o.more ? '<div class="ag-runs-more">' + o.more + '</div>' : '') + '</div></section>';
  }
  function flags(o) {
    return '<section class="ag-psec"><h3 class="ag-psec-title">Routing rules<small>' + o.source + '</small></h3><div class="ag-flags">' + o.items.map(function (f) {
      return '<div class="ag-flag"><span>' + f[0] + '</span><code>' + f[1] + '</code><i class="ag-switch' + (f[2] ? ' ag-switch--on' : '') + '" role="switch" aria-checked="' + f[2] + '" aria-label="' + f[0] + '"></i></div>';
    }).join('') + '</div></section>';
  }
  function demo(o) {
    return '<div class="ag-demo"><div class="ag-demo-row">' + btn('Start 20 tasks', { kind: 'primary', icon: 'play', disabled: o.busy }) + segmented(['steady', 'burst'], o.pattern) + '</div>' +
      '<div class="ag-demo-row">' + btn('Kill pod', { kind: 'danger', icon: 'power' }) + select(o.pod) + '</div>' +
      '<div class="ag-demo-cap">' + o.caption + '</div></div>';
  }
  function panel(o) {
    return '<aside class="ag-panel" aria-label="Cluster"><div class="ag-panel-scroll"><div class="ag-panel-head"><h2>Cluster</h2><span class="ag-spacer"></span><span class="ag-live"><i class="ag-dot"></i>' + o.live + '</span></div>' +
      remaining(o.rw) + pods(o.pods) + activeRuns(o.runs) + flags(o.flags) + '</div>' + demo(o.demo) + '</aside>';
  }

  /* ---- The whole screen ---- */
  function app(o) {
    return '<div class="ag-root ag-app">' + topbar(o.top) + '<div class="ag-split"><main class="ag-main">' + o.main + (o.sheet || '') + '</main>' + panel(o.panel) + '</div></div>';
  }

  return {
    name: NAME, horse: horse, icon: icon, tag: tag, status: status, btn: btn, select: select, segmented: segmented,
    topbar: topbar, composer: composer, scenario: scenario, newTask: newTask, progress: progress, runHead: runHead, tabs: tabs,
    stepCard: stepCard, steps: steps, answer: answer, cite: cite, notice: notice, stepTable: stepTable, run: run, stepDetail: stepDetail,
    remaining: remaining, podCard: podCard, pods: pods, activeRuns: activeRuns, flags: flags, demo: demo, panel: panel, app: app
  };
})();
