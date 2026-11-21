import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import demoSchema from '../../../../schemas/demo.v1.json';
import { ClusterPod } from './cluster';
import { DEMO_API, DEMO_KILL_API, DEMO_REPLAY_API, DemoReplay, DemoRunner, DemoView } from './demo';
import { DemoControls } from './demo-controls';

const READY: ClusterPod = { name: 'vllm-568fc4b669-c9kb2', state: 'ready', queue: 0, running: 1 };
const STARTING: ClusterPod = { name: 'vllm-568fc4b669-fgdgm', state: 'starting', queue: null, running: null };
const DRAINING: ClusterPod = { name: 'vllm-568fc4b669-drn01', state: 'draining', queue: 0, running: 0 };
const FAILED: ClusterPod = { name: 'vllm-568fc4b669-dead1', state: 'failed', queue: null, running: null };
const COMMAND = 'make replay TRACE=t.jsonl PATTERN=steady PROFILE=real OUT=o.jsonl';
const RUNNING: DemoReplay = {
  pattern: 'steady',
  command: COMMAND,
  started_at: '2026-11-20T21:05:03Z',
  state: 'running',
  runs_total: 20,
  runs_finished: 7,
  calls_failed: 0,
  exit_code: null,
};
const IDLE_RUNNER: DemoRunner = { replay: null, last_kill: null };
const VIEW: DemoView = {
  enabled: true,
  poll_seconds: 5,
  patterns: [
    { name: 'steady', runs: 20 },
    { name: 'burst', runs: 30 },
  ],
  runner: IDLE_RUNNER,
  runner_error: null,
};

describe('DemoControls', () => {
  let http: HttpTestingController;
  let fixture: ComponentFixture<DemoControls>;
  let page: HTMLElement;

  // The caption's lines, one sentence each, joined as they read.
  const caption = () =>
    Array.from(page.querySelectorAll('.ag-demo-cap p'))
      .map((line) => line.textContent?.replace(/\s+/g, ' ').trim())
      .join(' ');
  const buttons = () => Array.from(page.querySelectorAll<HTMLButtonElement>('.ag-demo-row > .ag-btn'));
  const start = () => buttons()[0];
  const kill = () => buttons()[1];
  const patterns = () => Array.from(page.querySelectorAll<HTMLButtonElement>('.ag-segmented button'));
  const select = () => page.querySelector<HTMLSelectElement>('select')!;
  const options = () => Array.from(select().options).map((option) => option.value);

  function answer(view: DemoView = VIEW) {
    http.expectOne(DEMO_API).flush(view);
    fixture.detectChanges();
  }

  function press(button: HTMLButtonElement) {
    button.click();
    fixture.detectChanges();
  }

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(DemoControls);
    fixture.componentRef.setInput('pods', [READY, STARTING, DRAINING, FAILED]);
    page = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
  });

  afterEach(() => {
    http.verify();
    vi.useRealTimers();
  });

  it('shows nothing on a profile without the buttons, and asks no more', () => {
    answer({ ...VIEW, enabled: false, patterns: [], runner: null });
    expect(page.querySelector('.ag-demo')).toBeNull();
    vi.advanceTimersByTime(60_000);
    http.expectNone(DEMO_API);
  });

  it('names the chosen pattern and its runs on Start', () => {
    answer();
    expect(start().textContent?.trim()).toBe('Start 20 tasks');
    expect(patterns().map((button) => button.textContent?.trim())).toEqual(['steady', 'burst']);
    expect(patterns()[0].getAttribute('aria-checked')).toBe('true');
    press(patterns()[1]);
    expect(start().textContent?.trim()).toBe('Start 30 tasks');
    expect(patterns()[1].getAttribute('aria-checked')).toBe('true');
    expect(patterns()[0].getAttribute('aria-checked')).toBe('false');
  });

  it('says what the buttons do while nothing has run', () => {
    answer();
    expect(caption()).toBe(
      'Start replays recorded tasks through the gateway (make replay). Kill deletes the pod at once; its runs continue on another one.',
    );
  });

  it('starts the chosen pattern and shows the replay the runner answers', () => {
    answer();
    press(patterns()[1]);
    press(start());
    const request = http.expectOne(DEMO_REPLAY_API);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ pattern: 'burst' });
    expect(start().disabled).toBe(true);
    request.flush({ replay: { ...RUNNING, pattern: 'burst', runs_total: 30, runs_finished: 0 }, last_kill: null });
    fixture.detectChanges();
    expect(caption()).toBe(`Replay running: 0 of 30 finished · ${COMMAND}`);
    expect(start().disabled).toBe(true);
  });

  it('keeps Start disabled while a replay runs, and Kill enabled', () => {
    answer({ ...VIEW, runner: { replay: RUNNING, last_kill: null } });
    expect(start().disabled).toBe(true);
    expect(kill().disabled).toBe(false);
    expect(caption()).toBe(`Replay running: 7 of 20 finished · ${COMMAND}`);
  });

  it('enables Start again once the replay ended, and says how it ended', () => {
    answer({ ...VIEW, runner: { replay: { ...RUNNING, state: 'done', runs_finished: 20, exit_code: 0 }, last_kill: null } });
    expect(start().disabled).toBe(false);
    expect(caption()).toBe(`Replay done: 20 of 20 finished · ${COMMAND}`);
  });

  it('names the exit code of a replay that failed', () => {
    answer({ ...VIEW, runner: { replay: { ...RUNNING, state: 'failed', runs_finished: 18, exit_code: 2 }, last_kill: null } });
    expect(caption()).toBe(`Replay failed (exit 2): 18 of 20 finished · ${COMMAND}`);
  });

  it('calls a replay with lost calls done, and counts what it lost', () => {
    const lost = { ...RUNNING, state: 'done' as const, runs_finished: 20, calls_failed: 1, exit_code: 2 };
    answer({ ...VIEW, runner: { replay: lost, last_kill: null } });
    expect(caption()).toBe(`Replay done: 20 of 20 finished, 1 call lost · ${COMMAND}`);
  });

  it('counts calls lost while the replay still runs, in the plural past one', () => {
    answer({ ...VIEW, runner: { replay: { ...RUNNING, calls_failed: 2 }, last_kill: null } });
    expect(caption()).toBe(`Replay running: 7 of 20 finished, 2 calls lost · ${COMMAND}`);
  });

  it('offers only pods that are still serving: not draining, not failed', () => {
    answer();
    expect(options()).toEqual([READY.name, STARTING.name]);
  });

  it('kills the chosen pod and says when', () => {
    answer();
    select().value = STARTING.name;
    select().dispatchEvent(new Event('change'));
    fixture.detectChanges();
    press(kill());
    const request = http.expectOne(DEMO_KILL_API);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ pod: STARTING.name });
    request.flush({ replay: null, last_kill: { pod: STARTING.name, at: '2026-11-20T21:07:41Z' } });
    fixture.detectChanges();
    expect(caption()).toContain(`${STARTING.name} killed at 21:07:41 UTC.`);
  });

  it('says it is killing the pod while the press is in flight (P4.6c)', () => {
    answer();
    expect(kill().textContent?.trim()).toBe('Kill pod');
    press(kill());
    expect(kill().textContent?.trim()).toBe(`Killing ${READY.name}…`);
    expect(kill().disabled).toBe(true);
    http.expectOne(DEMO_KILL_API).flush({ replay: null, last_kill: { pod: READY.name, at: '2026-11-20T21:07:41Z' } });
    fixture.detectChanges();
    expect(kill().textContent?.trim()).toBe('Kill pod');
  });

  it('shows the runner refusing the last Ready pod, and offers Kill again', () => {
    answer();
    press(kill());
    const detail = `no other vLLM pod is Ready: ${READY.name}'s runs would have nowhere to go`;
    http.expectOne(DEMO_KILL_API).flush({ detail }, { status: 409, statusText: 'Conflict' });
    fixture.detectChanges();
    expect(caption()).toBe(detail);
    expect(kill().textContent?.trim()).toBe('Kill pod');
    expect(kill().disabled).toBe(false);
  });

  it('kills the first pod on offer when none was chosen', () => {
    answer();
    press(kill());
    expect(http.expectOne(DEMO_KILL_API).request.body).toEqual({ pod: READY.name });
  });

  it('moves the choice to a pod still on offer when the chosen one leaves', () => {
    answer();
    select().value = STARTING.name;
    select().dispatchEvent(new Event('change'));
    fixture.componentRef.setInput('pods', [READY, { ...STARTING, state: 'draining' }]);
    fixture.detectChanges();
    expect(options()).toEqual([READY.name]);
    press(kill());
    expect(http.expectOne(DEMO_KILL_API).request.body).toEqual({ pod: READY.name });
  });

  it('disables Kill when no pod is on offer', () => {
    fixture.componentRef.setInput('pods', [DRAINING, FAILED]);
    answer();
    expect(kill().disabled).toBe(true);
  });

  it('shows the replay and the last kill together', () => {
    answer({ ...VIEW, runner: { replay: RUNNING, last_kill: { pod: READY.name, at: '2026-11-20T21:07:41Z' } } });
    expect(caption()).toBe(`Replay running: 7 of 20 finished · ${COMMAND} ${READY.name} killed at 21:07:41 UTC.`);
  });

  it('says why when the runner cannot answer, and disables both buttons', () => {
    answer({ ...VIEW, runner: null, runner_error: 'The demo runner is not running: make demo-runner' });
    expect(caption()).toBe('The demo runner is not running: make demo-runner');
    expect(start().disabled).toBe(true);
    expect(kill().disabled).toBe(true);
  });

  it('shows a refusal in place of the caption until the next answer', () => {
    answer();
    press(start());
    http
      .expectOne(DEMO_REPLAY_API)
      .flush({ detail: `a replay is running: ${COMMAND}` }, { status: 409, statusText: 'Conflict' });
    fixture.detectChanges();
    expect(caption()).toBe(`a replay is running: ${COMMAND}`);
    expect(start().disabled).toBe(false);
    vi.advanceTimersByTime(5_000);
    answer();
    expect(caption()).toContain('Start replays recorded tasks');
  });

  it('says the app backend did not answer when a press gets no body', () => {
    answer();
    press(kill());
    http.expectOne(DEMO_KILL_API).flush(null, { status: 502, statusText: 'Bad Gateway' });
    fixture.detectChanges();
    expect(caption()).toBe('The app backend did not answer.');
  });

  it('asks again after the poll interval the answer carries, not sooner', () => {
    answer({ ...VIEW, poll_seconds: 3 });
    vi.advanceTimersByTime(2_999);
    http.expectNone(DEMO_API);
    vi.advanceTimersByTime(1);
    answer({ ...VIEW, runner: { replay: RUNNING, last_kill: null } });
    expect(caption()).toContain('7 of 20 finished');
  });

  it('shows nothing and asks no more when GET /demo fails', () => {
    http.expectOne(DEMO_API).flush(null, { status: 500, statusText: 'Internal Server Error' });
    fixture.detectChanges();
    expect(page.querySelector('.ag-demo')).toBeNull();
    vi.advanceTimersByTime(60_000);
    http.expectNone(DEMO_API);
  });

  it('stops asking when it is removed', () => {
    answer();
    fixture.destroy();
    vi.advanceTimersByTime(60_000);
    http.expectNone(DEMO_API);
  });

  it('reads the fields schemas/demo.v1.json defines (D12)', () => {
    answer();
    expect(Object.keys(VIEW).sort()).toEqual(Object.keys(demoSchema.properties).sort());
    expect([...demoSchema.required].sort()).toEqual(Object.keys(VIEW).sort());
    const replay = demoSchema.$defs.replay;
    expect(Object.keys(RUNNING).sort()).toEqual(Object.keys(replay.properties).sort());
    expect([...replay.required].sort()).toEqual(Object.keys(RUNNING).sort());
    expect([...replay.properties.state.enum].sort()).toEqual(['done', 'failed', 'running']);
    expect(Object.keys(IDLE_RUNNER).sort()).toEqual(Object.keys(demoSchema.$defs.runner.properties).sort());
    expect(Object.keys({ pod: '', at: '' }).sort()).toEqual(Object.keys(demoSchema.$defs.kill.properties).sort());
  });
});
