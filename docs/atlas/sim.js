// The atlas's sandbox model, shared by index.html (3D) and flat.html (2D). Pure JavaScript, no DOM: the pages render it,
// their experiment tables run it headless, and so does the smoke test. A classic script rather than a module, so the
// pages also work opened from disk (file://), where browsers refuse to load module imports.
(function () {
  'use strict';

  // The sandbox model. Pure JavaScript, no DOM: the page renders it and the presets' tables run it headless.

  // Task types shaped like the converted AgentProcessBench traces: steps per run and a prompt that grows each step.
  const TASK_TYPES = [
    { name: 'tau2', min: 8, max: 12, start: 1500, grow: 230 },
    { name: 'gaia', min: 6, max: 10, start: 1300, grow: 280 },
    { name: 'hotpotqa', min: 4, max: 7, start: 1100, grow: 320 },
    { name: 'bfcl', min: 3, max: 5, start: 900, grow: 260 },
  ];
  for (const t of TASK_TYPES) t.expected = Math.round((t.min + t.max) / 2);

  // Illustrative constants, chosen so a run takes about 40 s and its effects show in seconds.
  // Rule values (thresholds, windows, targets) follow config/real.yaml where one exists.
  // The sandbox's own mechanics, the same on every architecture.
  const C = {
    transit: 0.45,            // seconds per network leg, for the animation
    outMin: 24, outMax: 64,   // reply tokens
    prefillBase: 0.05,        // seconds a call spends before its first prompt token, cached or not
    decodeCrowd: 0.12,        // each other call running on the pod slows decoding by this share
    window: 60,               // the live numbers' window
    keepRuns: 6,              // seconds a finished run stays on screen
    actionCalls: 2,           // a preset's kill or scale-down waits until the pod it hits holds this many calls,
    actionWait: 30,           // but no longer than this, so it shows what the rule protects
    seeds: 5,                 // seeds per variant in an experiment's table
  };

  // KV tokens a pod holds: its KV memory over the bytes one token takes (K and V, in every layer, for every KV head), in
  // vLLM's blocks of 16 tokens. The thesis's 480 MiB and Qwen3-0.6B's shape give 274 blocks, 4,384 tokens, as
  // config/real.yaml says.
  function kvTokensFor(hw) {
    const perToken = 2 * hw.layers * hw.kvHeads * hw.headDim * hw.bytes;
    return Math.floor((hw.kvMiB * 1048576) / perToken / 16) * 16;
  }

  const THESIS_HW = { kvMiB: 480, layers: 28, kvHeads: 8, headDim: 128, bytes: 2 };

  // The architecture: what a reader changes to see the system on their own hardware, model, workload and settings.
  // The defaults are the thesis's (config/real.yaml) where it has the value, and labelled sandbox guesses where not.
  const DEFAULT_ARCH = {
    kvTokens: kvTokensFor(THESIS_HW), // KV cache per pod
    maxContext: 4096,                 // max_model_len
    maxSeqs: 4,                       // calls a pod runs at once (a sandbox limit; real vLLM is limited by KV tokens here)
    prefillTps: 2500,                 // prompt tokens a pod reads per second (a guess)
    decodeTps: 28,                    // reply tokens per second for one call (a guess)
    coldStart: 30,                    // seconds from a new pod to Ready (real pods: about 2 min; shortened to watch it)
    promptScale: 1,                   // the task types' prompt sizes, times this
    toolMin: 1.5, toolMax: 5,         // seconds of tool time between a run's calls (a guess)
    guardThreshold: 1,                // routing.overload_queue_threshold
    guardSustained: 10,               // routing.overload_sustained_seconds
    parallelThreshold: 2,             // routing.parallel_queue_threshold
    agingSeconds: 30,                 // routing.priority_aging_seconds
    warmupWindow: 30,                 // how long a new pod counts as new for warm-up routing
    remainingTarget: 10,              // autoscaling.remaining_work_target, steps left per pod
    queueTarget: 1,                   // autoscaling.queue_target, calls waiting per pod
    pressureTarget: 1,                // live context per pod, in shares of one pod's KV cache (P4.12's signal)
    scrape: 5,                        // seconds between the autoscaler's readings
    cooldown: 30,                     // seconds the signal stays lower before pods are removed
    retries: 2, retryBackoff: 3,      // the agent runtime's retries on a lost pod, and the seconds before each
  };
  const ARCH_KEYS = Object.keys(DEFAULT_ARCH);

  // Ready-made architectures. Only the thesis's is the setup the measurements come from.
  const ARCH_PROFILES = [
    { id: 'thesis', name: 'Thesis: RTX 3050, Qwen3-0.6B', hw: THESIS_HW, cfg: {},
      note: 'Two pods share one 4 GB GPU, 480 MiB of KV cache each (config/real.yaml). The measured results come from this setup.' },
    { id: 'gpu24-8b', name: '24 GB GPU per pod, an 8B model', hw: { kvMiB: 5600, layers: 32, kvHeads: 8, headDim: 128, bytes: 2 },
      cfg: { maxContext: 32768, maxSeqs: 16, prefillTps: 8000, decodeTps: 40, coldStart: 60, promptScale: 8 },
      note: "A rough example, not measured: about 5,600 MiB left for KV beside the 8B model's ~15 GiB of weights at 90% memory use, a Llama-3.1-8B shape (32 layers, 8 KV heads of 128), prompts 8 times the thesis's. Speeds are guesses." },
    { id: 'gpu80-8b', name: '80 GB GPU per pod, an 8B model', hw: { kvMiB: 56000, layers: 32, kvHeads: 8, headDim: 128, bytes: 2 },
      cfg: { maxContext: 32768, maxSeqs: 64, prefillTps: 20000, decodeTps: 60, coldStart: 90, promptScale: 8 },
      note: 'A rough example, not measured: the same model and prompts with ten times the KV memory. Speeds are guesses.' },
  ];

  function archOf(cfg) { return Object.fromEntries(ARCH_KEYS.map(k => [k, cfg[k]])); }

  const DEFAULT_CFG = {
    seed: 6, rate: 3, burst: false, burstEvery: 45, burstSize: 5, fanOut: false,
    routing: 'default', affinity: true, overloadGuard: false, parallelSpill: false, leastContext: false, priority: false, warmup: false,
    drain: true, retry: true, pods: 2, minPods: 1, maxPods: 6, autoscaler: 'off',
    ...DEFAULT_ARCH,
  };

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function quantile(values, p) {
    if (!values.length) return null;
    const a = [...values].sort((x, y) => x - y);
    return a[Math.max(0, Math.ceil(p * a.length) - 1)];
  }

  class Sim {
    constructor(cfg) {
      this.cfg = { ...DEFAULT_CFG, ...cfg };
      this.listeners = [];
      this.reset();
    }

    on(fn) { this.listeners.push(fn); }
    emit(type, data) { for (const fn of this.listeners) fn(type, data); }

    reset() {
      // Separate streams, so a variant changes routing and nothing else: the same runs arrive at the same times.
      this.rngArrive = mulberry32(this.cfg.seed * 7919 + 1);
      this.rngRoute = mulberry32(this.cfg.seed * 7919 + 2);
      this.t = 0;
      this.runs = []; this.calls = []; this.pods = [];
      this.ids = { run: 1, pod: 0, call: 1 };
      this.rr = 0; this.schedule = [];
      this.nextArrival = this.cfg.rate > 0 ? 0.5 : Infinity;
      this.nextBurst = 15;
      this.lastScrape = -this.cfg.scrape; this.belowSince = null;
      this.desired = this.cfg.pods;
      this.stats = { done: 0, failed: 0, interrupted: 0, retries: 0, laterSteps: 0, moved: 0, spills: 0, remaps: 0,
        idlePodSeconds: 0, evicted: 0, warmFirst: 0, firstWhileNew: 0, podsMax: this.cfg.pods, pressureMax: 0 };
      this.samples = []; this.runTimes = []; this.series = [];
      for (let i = 0; i < this.cfg.pods; i++) this.addPod(true);
      this.emit('reset', {});
    }

    setCfg(patch) {
      const prev = { ...this.cfg };
      Object.assign(this.cfg, patch);
      if ('rate' in patch && patch.rate !== prev.rate) this.nextArrival = this.cfg.rate > 0 ? this.gap() : Infinity;
      if (patch.burst && !prev.burst) this.nextBurst = this.t + 3;
      if ('autoscaler' in patch && patch.autoscaler !== prev.autoscaler) {
        this.belowSince = null;
        if (patch.autoscaler === 'off') { this.desired = this.cfg.pods; this.reconcile(); }
      }
      if ('pods' in patch && this.cfg.autoscaler === 'off') { this.desired = this.cfg.pods; this.reconcile(); }
      if ('maxPods' in patch && this.cfg.autoscaler !== 'off' && this.desired > this.cfg.maxPods) { this.desired = this.cfg.maxPods; this.reconcile(); }
    }

    gap() { return this.t - Math.log(1 - this.rngArrive()) * (60 / this.cfg.rate); }

    at(t, action) { this.schedule.push({ t, action }); this.schedule.sort((a, b) => a.t - b.t); }

    addPod(initial) {
      const p = { id: this.ids.pod++, state: initial ? 'ready' : 'starting', createdAt: this.t,
        readyAt: initial ? this.t : this.t + this.cfg.coldStart, initial, cache: new Map(), served: new Map(), running: [], waiting: [],
        overSince: null, lastRemap: -Infinity, goneAt: null };
      p.name = 'vllm-' + p.id;
      this.pods.push(p);
      this.emit('pod', { pod: p, what: initial ? 'ready' : 'starting' });
      return p;
    }

    podById(id) { return this.pods.find(p => p.id === id); }
    live() { return this.runs.filter(r => r.state === 'tool' || r.state === 'call'); }
    readyPods() { return this.pods.filter(p => p.state === 'ready'); }
    allocated(p) { let s = 0; for (const c of p.running) s += c.prompt + c.out; return s; }
    pinsOn(p) { return this.live().filter(r => r.pin === p.id).length; }
    load(p) { return this.pinsOn(p) * 100 + p.running.length + p.waiting.length; }
    pinnedContext(p) { return this.live().reduce((s, r) => s + (r.pin === p.id ? r.latestPrompt : 0), 0); }
    // The longest prompt: the context window less the reply, and never more than one pod's KV cache holds (vLLM would not start).
    maxPrompt() { return Math.min(this.cfg.maxContext, this.cfg.kvTokens) - C.outMax; }
    isNew(p) { return !p.initial && p.state === 'ready' && this.t - p.readyAt <= this.cfg.warmupWindow; }
    priorityOn() { return this.cfg.routing === 'plugin' && this.cfg.priority; }
    pinning() { return this.cfg.routing === 'plugin' && this.cfg.affinity; }

    // The context a pod's runs will bring back: pinned runs with affinity, otherwise the runs whose last call went there.
    demandOn(p) {
      const pinning = this.pinning();
      return this.live().reduce((s, r) => s + ((pinning ? r.pin : r.prevPod) === p.id ? r.latestPrompt : 0), 0);
    }

    // What fills a pod's KV cache, bottom up: the calls running now, then the contexts kept for later, most recently
    // used first, so the last ones are the next to be thrown out.
    kvParts(p) {
      const inUse = new Map();
      for (const c of p.running) {
        const e = inUse.get(c.run.id);
        inUse.set(c.run.id, { runId: c.run.id, tokens: (e ? e.tokens : 0) + c.prompt + c.out, live: true });
      }
      const parts = [...inUse.values()];
      const idle = [...p.cache.entries()].filter(([rid]) => !inUse.has(rid)).sort((a, b) => b[1].last - a[1].last);
      for (const [rid, e] of idle) parts.push({ runId: rid, tokens: e.tokens, live: false });
      return parts;
    }

    pressureNow() {
      const ready = this.readyPods().length;
      const context = this.live().reduce((s, r) => s + r.latestPrompt, 0);
      return context / (Math.max(ready, 1) * this.cfg.kvTokens);
    }

    newRun() {
      const type = TASK_TYPES[Math.floor(this.rngArrive() * TASK_TYPES.length)];
      const steps = type.min + Math.floor(this.rngArrive() * (type.max - type.min + 1));
      const id = this.ids.run++;
      const r = { id, name: `${type.name}-${id}`, type, steps, expected: type.expected, step: 0,
        hue: (id * 137.508) % 360, startT: this.t, endT: null, state: 'tool', nextT: this.t,
        latestPrompt: 0, pin: null, prevPod: null, pending: 0, rng: mulberry32(this.cfg.seed * 104729 + id) };
      this.runs.push(r);
      this.emit('run', { run: r });
      return r;
    }

    sendStep(r) {
      r.step += 1;
      const prompt = Math.min(Math.round((r.type.start + r.type.grow * (r.step - 1)) * this.cfg.promptScale), this.maxPrompt());
      r.latestPrompt = prompt;
      r.state = 'call';
      const k = this.cfg.fanOut && r.step > 1 && r.rng() < 0.5 ? 2 : 1;
      const group = k > 1 ? `g${r.id}-${r.step}` : null;
      r.pending = k;
      for (let i = 0; i < k; i++) {
        const c = { id: this.ids.call++, run: r, step: r.step, index: i, prompt,
          out: C.outMin + Math.floor(r.rng() * (C.outMax - C.outMin + 1)), group,
          phase: 'toGateway', phaseStart: this.t, phaseEnd: this.t + C.transit, pod: null, reason: '', attempt: 0,
          arriveT: null, startT: null, endT: null, cached: 0, ttft: null };
        this.calls.push(c);
        this.emit('send', { call: c });
      }
    }

    // The scorers of the default picker: prefix match 3, queue 2, KV usage 2, the max score wins.
    // The prefix match reads the picker's own record of where each run's prompts went (llm-d's
    // approx-prefix-cache-producer), not the pod's cache, so it does not see an eviction.
    best(cands, c) {
      let qmax = -Infinity, qmin = Infinity;
      for (const p of cands) { qmax = Math.max(qmax, p.waiting.length); qmin = Math.min(qmin, p.waiting.length); }
      let best = cands[0], top = -Infinity;
      for (const p of cands) {
        const sent = p.served.get(c.run.id);
        const prefix = sent ? Math.min(sent, c.prompt) / c.prompt : 0;
        const queue = qmax === qmin ? 1 : (qmax - p.waiting.length) / (qmax - qmin);
        const kv = 1 - Math.min(1, this.allocated(p) / this.cfg.kvTokens);
        const s = 3 * prefix + 2 * queue + 2 * kv + this.rngRoute() * 0.01;
        if (s > top) { top = s; best = p; }
      }
      return best;
    }

    pick(c) {
      const cfg = this.cfg, r = c.run;
      const cand = this.readyPods();
      if (!cand.length) return null;
      if (cfg.routing === 'round-robin') { const p = cand[this.rr % cand.length]; this.rr += 1; return { pod: p, reason: 'round-robin' }; }
      if (cfg.routing === 'default') return { pod: this.best(cand, c), reason: c.step === 1 ? 'first call' : 'scorers' };

      if (cfg.affinity && r.pin !== null) {
        const pinned = cand.find(p => p.id === r.pin);
        if (!pinned) { const p = this.best(cand, c); r.pin = p.id; this.stats.remaps++; return { pod: p, reason: 'remap: pod left' }; }
        if (cfg.overloadGuard && pinned.waiting.length > this.cfg.guardThreshold) {
          const others = cand.filter(p => p !== pinned && p.waiting.length <= this.cfg.guardThreshold);
          if (others.length) {
            const target = this.best(others, c);
            const sustained = pinned.overSince !== null && this.t - pinned.overSince >= this.cfg.guardSustained && this.t - pinned.lastRemap >= this.cfg.guardSustained;
            if (sustained) { r.pin = target.id; pinned.lastRemap = this.t; this.stats.remaps++; return { pod: target, reason: 'remap: sustained overload' }; }
            this.stats.spills++;
            return { pod: target, reason: 'spill: overload' };
          }
        }
        if (c.group && cfg.parallelSpill) {
          const inFlight = this.calls.filter(x => x.group === c.group && x !== c && x.pod === pinned.id
            && (x.phase === 'toPod' || x.phase === 'waiting' || x.phase === 'running')).length;
          if (pinned.waiting.length + inFlight <= this.cfg.parallelThreshold) return { pod: pinned, reason: 'parallel: stay' };
          const roomy = cand.filter(p => p !== pinned && p.waiting.length <= this.cfg.parallelThreshold);
          if (roomy.length) { this.stats.spills++; return { pod: this.best(roomy, c), reason: 'parallel: spill' }; }
          return { pod: pinned, reason: 'parallel: no room' };
        }
        return { pod: pinned, reason: 'pinned' };
      }

      let pod = null, reason = c.step === 1 ? 'first call' : 'scorers';
      if (c.step === 1 && cfg.warmup) {
        const fresh = cand.filter(p => this.isNew(p));
        const roomy = fresh.filter(p => p.waiting.length <= 1);
        if (fresh.length && fresh.length < cand.length && roomy.length) { pod = this.best(roomy, c); reason = 'warm-up: new pod'; }
      }
      if (!pod && c.step === 1 && cfg.affinity && cfg.leastContext) {
        let low = Infinity, lows = [];
        for (const p of cand) {
          const ctx = this.pinnedContext(p);
          if (ctx < low) { low = ctx; lows = [p]; } else if (ctx === low) lows.push(p);
        }
        pod = lows.length === 1 ? lows[0] : this.best(lows, c);
        reason = 'least context';
      }
      if (!pod) pod = this.best(cand, c);
      if (cfg.affinity) r.pin = pod.id;
      return { pod, reason };
    }

    route(c) {
      const choice = this.pick(c);
      if (!choice) { c.phase = 'hold'; c.phaseStart = this.t; c.phaseEnd = this.t + 0.5; c.reason = 'no Ready pod'; return; }
      const { pod, reason } = choice, r = c.run;
      c.pod = pod.id; c.reason = reason; c.phase = 'toPod'; c.phaseStart = this.t; c.phaseEnd = this.t + C.transit;
      if (c.index === 0 && c.attempt === 0) {
        if (c.step > 1) { this.stats.laterSteps++; if (r.prevPod !== null && r.prevPod !== pod.id) this.stats.moved++; }
        if (c.step === 1) {
          const cand = this.readyPods(), fresh = cand.filter(p => this.isNew(p));
          if (fresh.length && fresh.length < cand.length) { this.stats.firstWhileNew++; if (this.isNew(pod)) this.stats.warmFirst++; }
        }
      }
      if (c.index === 0) r.prevPod = pod.id;
      pod.served.set(r.id, c.prompt);
      this.emit('route', { call: c, pod, reason });
    }

    arrive(c) {
      const p = this.podById(c.pod);
      if (!p || p.state === 'failed') { this.failCall(c, 'pod gone', false); return; }
      c.phase = 'waiting'; c.arriveT = this.t; c.phaseEnd = Infinity;
      p.waiting.push(c);
      this.emit('arrive', { call: c, pod: p });
    }

    priorityKey(c) {
      const r = c.run;
      return c.arriveT + this.cfg.agingSeconds * Math.max(0, (r.expected - c.step) / r.expected);
    }

    waitingOrder(p) {
      if (!this.priorityOn()) return p.waiting;
      return [...p.waiting].sort((a, b) => this.priorityKey(a) - this.priorityKey(b));
    }

    admit(p) {
      const kv = this.cfg.kvTokens;
      p.kvBlocked = false;
      while (p.running.length < this.cfg.maxSeqs && p.waiting.length > 0) {
        const c = this.waitingOrder(p)[0];
        const need = c.prompt + c.out;
        const allocated = this.allocated(p);
        if (allocated + need > kv) { p.kvBlocked = true; break; }
        // Free memory by evicting the cached prefixes of runs that are not running here, least recently used first.
        const runningRuns = new Set(p.running.map(x => x.run.id));
        const idle = [];
        let idleTokens = 0;
        for (const [rid, e] of p.cache) if (rid !== c.run.id && !runningRuns.has(rid)) { idle.push([rid, e]); idleTokens += e.tokens; }
        let over = allocated + need + idleTokens - kv;
        if (over > 0) {
          idle.sort((a, b) => a[1].last - b[1].last);
          for (const [rid, e] of idle) {
            if (over <= 0) break;
            const cut = Math.min(e.tokens, over);
            e.tokens -= cut; over -= cut; this.stats.evicted += cut;
            if (e.tokens <= 0) p.cache.delete(rid);
            this.emit('evict', { pod: p, runId: rid, tokens: cut });
          }
        }
        const own = p.cache.get(c.run.id);
        const hit = own ? Math.min(own.tokens, c.prompt) : 0;
        p.waiting.splice(p.waiting.indexOf(c), 1);
        const prefill = C.prefillBase + (c.prompt - hit) / this.cfg.prefillTps;
        const crowd = 1 + C.decodeCrowd * p.running.length;
        c.phase = 'running'; c.startT = this.t; c.cached = hit; c.phaseEnd = Infinity;
        c.ttft = (this.t - c.arriveT) + prefill;
        c.endT = this.t + prefill + (c.out / this.cfg.decodeTps) * crowd;
        p.running.push(c);
        this.samples.push({ t: this.t, step: c.step, late: c.step > 1 && c.step >= c.run.expected - 1,
          ttft: c.ttft, cached: hit / c.prompt, pressure: this.pressureNow() });
        if (this.samples.length > 50000) this.samples.splice(0, 10000);
        this.emit('start', { call: c, pod: p, hit });
      }
    }

    podTick(p, h) {
      const t = this.t;
      if (p.state === 'starting' && t >= p.readyAt) { p.state = 'ready'; this.emit('pod', { pod: p, what: 'ready' }); }
      if (p.state === 'failed' || p.state === 'starting') return;
      for (let i = p.running.length - 1; i >= 0; i--) {
        const c = p.running[i];
        if (t < c.endT) continue;
        p.running.splice(i, 1);
        const e = p.cache.get(c.run.id);
        p.cache.set(c.run.id, { tokens: Math.max(e ? e.tokens : 0, c.prompt + c.out), last: t });
        c.phase = 'toAgent'; c.phaseStart = t; c.phaseEnd = t + C.transit;
        this.emit('done', { call: c, pod: p });
      }
      this.admit(p);
      if (p.state === 'ready' && p.running.length === 0 && p.waiting.length === 0) this.stats.idlePodSeconds += h;
      if (p.waiting.length > this.cfg.guardThreshold) { if (p.overSince === null) p.overSince = t; } else p.overSince = null;
      if (p.state === 'draining' && p.running.length === 0 && p.waiting.length === 0
        && !this.calls.some(c => c.phase === 'toPod' && c.pod === p.id)) {
        p.state = 'gone'; this.emit('pod', { pod: p, what: 'gone' });
      }
    }

    failCall(c, why, interrupted) {
      const r = c.run;
      if (this.cfg.retry && c.attempt < this.cfg.retries && r.state === 'call') {
        c.attempt++; this.stats.retries++;
        c.phase = 'retryWait'; c.phaseStart = this.t; c.phaseEnd = this.t + this.cfg.retryBackoff; c.pod = null;
        this.emit('retry', { call: c, why });
        return;
      }
      c.phase = 'gone';
      if (r.state === 'call' || r.state === 'tool') {
        r.state = 'failed'; r.endT = this.t; r.pin = null;
        this.stats.failed++;
        if (interrupted) this.stats.interrupted++;
        this.emit('runFail', { run: r, why });
      }
    }

    deliver(c) {
      c.phase = 'gone';
      const r = c.run;
      if (r.state !== 'call') return;
      r.pending -= 1;
      if (r.pending > 0) return;
      if (r.step >= r.steps) {
        r.state = 'done'; r.endT = this.t; r.pin = null;
        this.stats.done++;
        this.runTimes.push(r.endT - r.startT);
        this.emit('runEnd', { run: r });
      } else {
        r.state = 'tool';
        r.nextT = this.t + this.cfg.toolMin + r.rng() * (this.cfg.toolMax - this.cfg.toolMin);
      }
    }

    // how: 'drain' (a scale-down, which drains when drain is on) or 'kill'.
    removePod(p, how) {
      if (how === 'drain' && this.cfg.drain) { p.state = 'draining'; this.emit('pod', { pod: p, what: 'draining' }); return; }
      const victims = [...p.running, ...p.waiting, ...this.calls.filter(c => c.phase === 'toPod' && c.pod === p.id)];
      p.state = 'failed'; p.goneAt = this.t + 1.2; p.running = []; p.waiting = []; p.cache.clear();
      this.emit('pod', { pod: p, what: how === 'kill' ? 'killed' : 'removed without drain' });
      for (const c of victims) this.failCall(c, how === 'kill' ? 'pod killed' : 'pod removed', how !== 'kill');
    }

    reconcile() {
      const active = this.pods.filter(p => p.state === 'ready' || p.state === 'starting');
      let n = active.length;
      while (n < this.desired) { this.addPod(false); n++; }
      if (n > this.desired) {
        const key = p => (p.state === 'starting' ? 0 : 1);
        const order = [...active].sort((a, b) => (key(a) - key(b)) || (this.load(a) - this.load(b)));
        for (const p of order.slice(0, n - this.desired)) {
          if (p.state === 'starting') { p.state = 'gone'; this.emit('pod', { pod: p, what: 'gone' }); }
          else this.removePod(p, 'drain');
        }
      }
      this.stats.podsMax = Math.max(this.stats.podsMax, this.pods.filter(p => p.state === 'ready' || p.state === 'starting').length);
    }

    inFlight(p) { return p.running.length + p.waiting.length + this.calls.filter(c => c.phase === 'toPod' && c.pod === p.id).length; }

    // The pod a kill or a scale-down hits: the most calls in flight, then the most pinned runs.
    busiest() {
      const ready = this.readyPods();
      if (!ready.length) return null;
      const key = p => this.inFlight(p) * 100 + this.pinsOn(p);
      return ready.reduce((a, b) => (key(b) > key(a) ? b : a));
    }

    hitsWork() { const p = this.busiest(); return p !== null && this.inFlight(p) >= C.actionCalls; }

    kill() {
      const p = this.busiest();
      if (!p) return null;
      this.removePod(p, 'kill');
      this.reconcile(); // the ReplicaSet replaces it
      return p;
    }

    scaleDown() {
      if (this.readyPods().length <= 1) return null;
      const p = this.busiest();
      const active = this.pods.filter(x => x.state === 'ready' || x.state === 'starting').length;
      this.desired = Math.max(this.cfg.minPods, active - 1);
      if (this.cfg.autoscaler === 'off') this.cfg.pods = this.desired;
      this.removePod(p, 'drain');
      this.emit('cfg', {});
      return p;
    }

    burstNow() { for (let i = 0; i < this.cfg.burstSize; i++) this.newRun(); this.emit('burst', { size: this.cfg.burstSize }); }

    action(name) {
      if (name === 'kill') this.kill();
      else if (name === 'scaleDown') this.scaleDown();
      else if (name === 'burst') this.burstNow();
    }

    scrape() {
      const t = this.t, kv = this.cfg.kvTokens;
      const live = this.live();
      const ready = this.readyPods().length;
      const starting = this.pods.filter(p => p.state === 'starting').length;
      const remaining = live.reduce((s, r) => s + Math.max(r.expected - r.step, 1), 0);
      const context = live.reduce((s, r) => s + r.latestPrompt, 0);
      const queue = this.pods.reduce((s, p) => s + p.waiting.length, 0);
      const busy = this.pods.some(p => p.state === 'ready' && p.running.length > 0);
      const asks = {
        remaining: Math.ceil(remaining / this.cfg.remainingTarget),
        queue: Math.ceil(queue / this.cfg.queueTarget),
        pressure: Math.ceil(context / (kv * this.cfg.pressureTarget)),
        cpu: busy ? this.cfg.maxPods : this.cfg.minPods,
      };
      const pressure = context / (Math.max(ready, 1) * kv);
      this.stats.pressureMax = Math.max(this.stats.pressureMax, pressure);
      this.series.push({ t, ready, starting, desired: this.desired, remaining, queue, context, pressure, asks });
      while (this.series.length && this.series[0].t < t - 240) this.series.shift();
      const a = this.cfg.autoscaler;
      if (a === 'off') this.desired = this.cfg.pods;
      else {
        const want = Math.max(this.cfg.minPods, Math.min(this.cfg.maxPods, asks[a]));
        if (want > this.desired) { this.desired = want; this.belowSince = null; this.emit('scale', { to: want, signal: a }); }
        else if (want < this.desired) {
          if (this.belowSince === null) this.belowSince = t;
          else if (t - this.belowSince >= this.cfg.cooldown) { this.desired = want; this.belowSince = null; this.emit('scale', { to: want, signal: a }); }
        } else this.belowSince = null;
      }
      this.reconcile();
    }

    tick(h) {
      this.t += h;
      const t = this.t;
      while (this.schedule.length && this.schedule[0].t <= t && (this.hitsWork() || t >= this.schedule[0].t + C.actionWait)) {
        this.action(this.schedule.shift().action);
      }
      if (t >= this.nextArrival) { this.newRun(); this.nextArrival = this.cfg.rate > 0 ? this.gap() : Infinity; }
      if (this.cfg.burst && t >= this.nextBurst) { this.burstNow(); this.nextBurst = t + this.cfg.burstEvery; }
      for (const r of this.runs) if (r.state === 'tool' && t >= r.nextT) this.sendStep(r);
      for (const c of this.calls) {
        if (c.phase === 'gone') continue;
        if (c.run.state === 'failed' && c.phase !== 'waiting' && c.phase !== 'running') { c.phase = 'gone'; continue; }
        if (t < c.phaseEnd) continue;
        if (c.phase === 'toGateway' || c.phase === 'hold' || c.phase === 'retryWait') this.route(c);
        else if (c.phase === 'toPod') this.arrive(c);
        else if (c.phase === 'toAgent') this.deliver(c);
      }
      for (const p of this.pods) this.podTick(p, h);
      for (const p of this.pods) if (p.state === 'failed' && t >= p.goneAt) { p.state = 'gone'; this.emit('pod', { pod: p, what: 'gone' }); }
      this.pods = this.pods.filter(p => p.state !== 'gone');
      this.calls = this.calls.filter(c => c.phase !== 'gone');
      this.runs = this.runs.filter(r => r.endT === null || t - r.endT <= C.keepRuns);
      if (t - this.lastScrape >= this.cfg.scrape) { this.lastScrape = t; this.scrape(); }
    }

    step(dt) {
      let left = dt;
      while (left > 1e-9) { const h = Math.min(0.1, left); this.tick(h); left -= h; }
    }

    // What the run tracker and the pods report now; cheap enough to read every frame.
    gauges() {
      const live = this.live();
      const ready = this.readyPods().length;
      const context = live.reduce((s, r) => s + r.latestPrompt, 0);
      return {
        live: live.length, ready, desired: this.desired,
        starting: this.pods.filter(p => p.state === 'starting').length,
        draining: this.pods.filter(p => p.state === 'draining').length,
        queue: this.pods.reduce((s, p) => s + p.waiting.length, 0),
        remaining: live.reduce((s, r) => s + Math.max(r.expected - r.step, 1), 0),
        context, pressure: context / (Math.max(ready, 1) * this.cfg.kvTokens),
      };
    }

    snapshot(windowed) {
      const t = this.t;
      const src = windowed ? this.samples.filter(s => s.t >= t - C.window) : this.samples;
      const later = src.filter(s => s.step > 1);
      return {
        ...this.gauges(),
        t, done: this.stats.done, failed: this.stats.failed, interrupted: this.stats.interrupted,
        retries: this.stats.retries, moved: this.stats.moved, laterSteps: this.stats.laterSteps,
        spills: this.stats.spills, remaps: this.stats.remaps,
        cached: later.length ? later.reduce((s, x) => s + x.cached, 0) / later.length : null,
        ttft50: quantile(later.map(s => s.ttft), 0.5), ttft95: quantile(later.map(s => s.ttft), 0.95),
        ttftLate: quantile(src.filter(s => s.late).map(s => s.ttft), 0.5),
        ttftFirst: quantile(src.filter(s => s.step === 1).map(s => s.ttft), 0.5),
        over1: later.length ? later.filter(s => s.pressure > 1).length / later.length : null,
        podsMax: this.stats.podsMax, idlePodMin: this.stats.idlePodSeconds / 60, pressureMax: this.stats.pressureMax,
        runP50: quantile(this.runTimes, 0.5), evicted: this.stats.evicted,
        warmFirst: this.stats.warmFirst, firstWhileNew: this.stats.firstWhileNew,
      };
    }
  }

  const secs = v => (v === null ? '–' : v < 10 ? `${v.toFixed(2)} s` : `${v.toFixed(1)} s`);
  const pct = v => (v === null ? '–' : `${Math.round(v * 100)}%`);
  const count = v => String(Math.round(v));
  const share = (a, b) => (b > 0 ? a / b : null);
  // val reads one number from a run's snapshot, show formats it; an experiment's table gives the median over seeds.
  const METRICS = {
    cached: { label: 'Cached share, later steps', val: s => s.cached, show: v => v.toFixed(2) },
    movedShare: { label: 'Later steps moved', val: s => share(s.moved, s.laterSteps), show: pct },
    ttft50: { label: 'First token p50, later steps', val: s => s.ttft50, show: secs },
    ttft95: { label: 'First token p95, later steps', val: s => s.ttft95, show: secs },
    ttftLate: { label: "First token p50, runs' last steps", val: s => s.ttftLate, show: secs },
    ttftFirst: { label: "First token p50, runs' first steps", val: s => s.ttftFirst, show: secs },
    runP50: { label: 'Run time p50', val: s => s.runP50, show: secs },
    evicted: { label: 'Tokens evicted', val: s => s.evicted, show: v => `${(v / 1000).toFixed(1)}k` },
    over1: { label: 'Later steps over pressure 1', val: s => s.over1, show: pct },
    pressureMax: { label: 'Highest pressure', val: s => s.pressureMax, show: v => v.toFixed(2) },
    podsMax: { label: 'Most pods', val: s => s.podsMax, show: count },
    idlePodMin: { label: 'Idle pod-minutes', val: s => s.idlePodMin, show: v => v.toFixed(1) },
    warmShare: { label: 'First calls to a new pod', val: s => share(s.warmFirst, s.firstWhileNew), show: pct },
    interrupted: { label: 'Interrupted runs', val: s => s.interrupted, show: count },
    failed: { label: 'Failed runs', val: s => s.failed, show: count },
    done: { label: 'Runs finished', val: s => s.done, show: count },
    retries: { label: 'Calls retried', val: s => s.retries, show: count },
  };

  // The median and range of one metric over a variant's seeds; null where no seed had a value.
  function spread(snaps, key) {
    const vals = snaps.map(METRICS[key].val).filter(v => v !== null).sort((a, b) => a - b);
    if (!vals.length) return null;
    return { med: vals[Math.floor(vals.length / 2)], lo: vals[0], hi: vals[vals.length - 1] };
  }

  // Each preset sets the sandbox like one experiment. "measured" rows are copied from experiments/log.md.
  const PRESETS = [
    {
      id: 'baseline', tag: 'Gate 2', title: 'Round-robin against the default picker', horizon: 300,
      base: { routing: 'round-robin', rate: 4, burst: false, fanOut: false, pods: 2, autoscaler: 'off' },
      variants: [{ label: 'Round-robin', patch: { routing: 'round-robin' } }, { label: 'Default picker', patch: { routing: 'default' } }],
      metrics: ['cached', 'movedShare', 'ttft50', 'runP50'],
      watch: "Round-robin sends each later step to the next pod in turn, so many land where the run's context isn't. The default picker's prefix scorer sends a run back to where its prompts went, its estimate of the cache.",
      measured: [
        ['Prefix-cache hit rate', '0.737 near round-robin, 0.815 default picker'],
        ['Total run time', 'default 6.6% faster over both variant orders (2.9% in one order alone)'],
        ['Setup', '35 replayed runs on 2 real pods, 309 calls per variant'],
      ],
      source: 'Gate 2',
    },
    {
      id: 'affinity', tag: 'P3.3', title: 'Task affinity', horizon: 300,
      base: { routing: 'plugin', affinity: true, overloadGuard: false, parallelSpill: false, leastContext: false, priority: false, warmup: false,
        rate: 4.5, burst: false, fanOut: false, pods: 2, autoscaler: 'off' },
      variants: [
        { label: 'Default picker', patch: { routing: 'default' } },
        { label: 'Affinity', patch: { routing: 'plugin', leastContext: false } },
        { label: 'Affinity + least context', patch: { routing: 'plugin', leastContext: true } },
      ],
      metrics: ['movedShare', 'cached', 'evicted', 'ttft50'],
      watch: "Watch the demand mark on each pod's KV tank: the context its runs will bring back. When two long runs share a pod the mark rises past the top of the tank and turns red while the other pod has room, P3.3's collision. Least context first places a run's first call by that number: proposed for the pool under pressure 1, not yet measured there.",
      measured: [
        ['Later steps moved', '44 of 243 default, 0 of 243 affinity'],
        ['Uncached prompt tokens under the KV limit', '91,505 default, 64,698 affinity (−29%)'],
        ['Hit rate', '0.821 default, 0.807 affinity: two long runs pinned to one pod overflowed it while the pool had room'],
      ],
      source: 'P3.3, P4.9',
    },
    {
      id: 'burst', tag: 'P3.7', title: 'The plugin under a burst', horizon: 300,
      base: { routing: 'plugin', affinity: true, overloadGuard: true, parallelSpill: true, leastContext: false, priority: false, warmup: false,
        rate: 1.2, burst: true, burstEvery: 45, burstSize: 5, fanOut: true, pods: 2, autoscaler: 'off' },
      variants: [{ label: 'Default picker', patch: { routing: 'default' } }, { label: 'Thesis plugin', patch: { routing: 'plugin' } }],
      metrics: ['over1', 'cached', 'ttft95', 'runP50'],
      watch: "Every burst pushes the pressure past 1. Then every pod's memory is full whatever the routing, and the two variants converge.",
      measured: [
        ['First token p95', '47.88 s plugin, 49.54 s default'],
        ['Run completion p95', '426.48 s plugin, 405.77 s default (+5.1%), inside the 11–21% swing between files'],
        ['Later-step calls over pressure 1', '86% plugin, 89% default (P4.9)'],
      ],
      source: 'P3.7, P4.9',
    },
    {
      id: 'pressure', tag: 'P4.9', title: 'Cache pressure', horizon: 300,
      base: { routing: 'default', rate: 7, burst: false, fanOut: false, pods: 2, autoscaler: 'off' },
      variants: [{ label: '7 new runs a minute', patch: { rate: 7 } }, { label: '3.5 a minute', patch: { rate: 3.5 } }],
      metrics: ['over1', 'cached', 'ttft50', 'pressureMax'],
      watch: "The pressure gauge behind the pods, and the marker on the measured curve below, show the sandbox's pressure now. Drag the KV cache slider: the same load slides along the curve.",
      measured: [
        ['Pressure under 0.75', 'first token p50 0.4 s, p90 0.7 s, cached share 0.86'],
        ['1 to 1.25', 'p50 1.7 s, p90 15.8 s, cached 0.74'],
        ['1.5 to 2', 'p50 7.1 s, p90 27.9 s, cached 0.45'],
        ['3 and over', 'p50 23.1 s, p90 41.7 s, cached 0.34'],
        ['Limit', 'pressure and the queue rise together; the pooled shape of 7,412 calls, not a law per replay'],
      ],
      source: 'P4.9',
    },
    {
      id: 'autoscale', tag: 'P3.10', title: 'Remaining work against the queue', horizon: 300,
      base: { routing: 'default', rate: 1.2, burst: true, burstEvery: 45, burstSize: 5, fanOut: false, pods: 1, maxPods: 6, autoscaler: 'remaining' },
      variants: [
        { label: 'Remaining work', patch: { autoscaler: 'remaining' } },
        { label: 'Queue', patch: { autoscaler: 'queue' } },
        { label: 'Pressure', patch: { autoscaler: 'pressure' } },
        { label: 'CPU', patch: { autoscaler: 'cpu' } },
      ],
      metrics: ['podsMax', 'idlePodMin', 'runP50', 'ttft95'],
      watch: "The timeline below shows what each signal asks for, in pods. Remaining work rises as runs arrive, the queue only once their calls wait. The sandbox's idle pod-minutes need not match the measured ones: its timings are illustrative, so take the mechanism from here and the numbers from Measured. Pressure is P4.12's signal, not measured yet.",
      measured: [
        ['Pods asked for, sim burst', '4 then 5 on remaining work, 5 then 8 on the queue'],
        ['Idle pod-minutes', '31–56% fewer on remaining work, in all four pairs'],
        ['Completion', 'the same within noise; on the burst both scaled in the same scrape'],
        ['CPU, real 1↔2', 'one busy pod reads ~100%: it scales to the maximum whenever anything runs'],
      ],
      source: 'P3.9b, P3.10',
    },
    {
      id: 'priority', tag: 'P4.1', title: 'Progress priority', horizon: 300,
      base: { routing: 'plugin', affinity: true, overloadGuard: false, parallelSpill: false, leastContext: false, priority: true, warmup: false,
        rate: 9, burst: false, fanOut: false, pods: 1, autoscaler: 'off' },
      variants: [{ label: 'Priority on', patch: { priority: true } }, { label: 'Priority off', patch: { priority: false } }],
      metrics: ['ttftLate', 'ttftFirst', 'runP50', 'done'],
      watch: 'One pod with a queue. With priority on, waiting calls glow brighter the closer their run is to its last step, and those go first; the aging stops a first step from waiting forever.',
      measured: [
        ['Check', "1 real pod, a run's last step sent behind 6 waiting first steps"],
        ['On', 'it went ahead of all 6, first byte after 31.8 s'],
        ['Off', 'it went behind all 6, first byte after 81.7 s'],
        ['Not measured yet', 'the effect on a replay (P5.1)'],
      ],
      source: 'P4.1',
    },
    {
      id: 'warmup', tag: 'P4.2', title: 'Warm-up routing', horizon: 300,
      base: { routing: 'plugin', affinity: true, overloadGuard: false, parallelSpill: false, leastContext: false, priority: false, warmup: true,
        rate: 1.2, burst: true, burstEvery: 45, burstSize: 5, fanOut: false, pods: 1, maxPods: 6, autoscaler: 'remaining' },
      variants: [{ label: 'Warm-up on', patch: { warmup: true } }, { label: 'Warm-up off', patch: { warmup: false } }],
      metrics: ['warmShare', 'runP50', 'podsMax', 'ttft95'],
      watch: "When a blue pod turns green, the next runs' first calls go to it. The room check reads each queue as last seen, so a burst can land whole on one new pod.",
      measured: [
        ['First calls while a pod was new', '14 of 14 to a new pod with warm-up, 8 of 14 without'],
        ['Run time p50', '56.6 s on, 41.8 s off, one seed: each burst went whole to one new pod'],
      ],
      source: 'P4.2',
    },
    {
      id: 'drain', tag: 'P4.3', title: 'Drain on scale-down', horizon: 300,
      base: { routing: 'plugin', affinity: true, overloadGuard: false, parallelSpill: false, leastContext: false, priority: false, warmup: false,
        rate: 6, burst: false, fanOut: false, pods: 2, autoscaler: 'off', drain: true, retry: false },
      at: [{ t: 40, action: 'scaleDown' }],
      variants: [{ label: 'Drain on', patch: { drain: true } }, { label: 'Drain off', patch: { drain: false } }],
      metrics: ['interrupted', 'failed', 'done', 'runP50'],
      watch: 'From 40 s, the first time a pod holds 2 calls, that pod is removed. With the drain it turns amber, takes no new calls and finishes what it holds; without it, its calls are cut and their runs fail.',
      measured: [
        ['Real 2 → 1 pods during a replay', '0 interrupted runs with the drain, 2 without'],
        ['Why', 'vLLM aborts the calls it is running on SIGTERM'],
      ],
      source: 'P4.3',
    },
    {
      id: 'failover', tag: 'P4.4', title: 'Failover with a retry', horizon: 300,
      base: { routing: 'plugin', affinity: true, overloadGuard: false, parallelSpill: false, leastContext: false, priority: false, warmup: false,
        rate: 5, burst: false, fanOut: false, pods: 2, autoscaler: 'off', drain: true, retry: true },
      at: [{ t: 40, action: 'kill' }],
      variants: [{ label: 'Retry on', patch: { retry: true } }, { label: 'Retry off', patch: { retry: false } }],
      metrics: ['failed', 'retries', 'done', 'runP50'],
      watch: 'From 40 s, the first time a pod holds 2 calls, that pod is killed and a replacement starts. With the retry its calls go back through the gateway after 3 s and land on the other pod, without their cache.',
      measured: [
        ['Sim burst, a pod force-deleted mid-replay', '30 of 30 runs completed with the retry, 27 of 30 without'],
        ['Live, real pods', 'a vLLM kill and an agent-runtime kill under 5 runs each: 10 of 10 finished, all correct'],
      ],
      source: 'P4.4',
    },
  ];

  // The measured curve (P4.9): later-step calls of every real-pod replay, by pressure at the call's start.
  const MEASURED_CURVE = [
    { lo: 0, hi: 0.75, n: 3174, p50: 0.4, p90: 0.7, cached: 0.86 },
    { lo: 0.75, hi: 1, n: 579, p50: 0.6, p90: 4.9, cached: 0.75 },
    { lo: 1, hi: 1.25, n: 289, p50: 1.7, p90: 15.8, cached: 0.74 },
    { lo: 1.25, hi: 1.5, n: 317, p50: 4.9, p90: 22.6, cached: 0.57 },
    { lo: 1.5, hi: 2, n: 529, p50: 7.1, p90: 27.9, cached: 0.45 },
    { lo: 2, hi: 3, n: 1870, p50: 13.2, p90: 30.5, cached: 0.35 },
    { lo: 3, hi: 4.31, n: 654, p50: 23.1, p90: 41.7, cached: 0.34 },
  ];

  // A variant on a given architecture: the preset sets the experiment, `arch` the hardware, model and settings.
  function runVariant(preset, variantIndex, seed, arch = DEFAULT_ARCH) {
    const sim = new Sim({ ...DEFAULT_CFG, ...arch, seed, ...preset.base, ...preset.variants[variantIndex].patch });
    for (const a of preset.at || []) sim.at(a.t, a.action);
    sim.step(preset.horizon);
    return sim.snapshot(false);
  }

  // Every variant on the seeds from `seed` on: one list of snapshots per variant.
  function runTable(preset, seed, arch = DEFAULT_ARCH) {
    return preset.variants.map((_, i) => Array.from({ length: C.seeds }, (__, k) => runVariant(preset, i, seed + k, arch)));
  }

  globalThis.AtlasSim = {
    TASK_TYPES, C, DEFAULT_CFG, DEFAULT_ARCH, ARCH_KEYS, ARCH_PROFILES, kvTokensFor, archOf, Sim, quantile, secs, METRICS, spread,
    PRESETS, MEASURED_CURVE, runVariant, runTable,
  };
})();
