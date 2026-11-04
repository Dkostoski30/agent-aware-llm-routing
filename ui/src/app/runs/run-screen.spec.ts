import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of } from 'rxjs';
import { RunCall, RunCalls } from './run-calls';
import { RunEnd, RunEvent, RunEvents, RunStarted, RunStep } from './run-events';
import { RunScreen } from './run-screen';

const RUN: RunStarted = {
  run_id: 'run-0a1b2c3d4e5f',
  task: 'What is 7 times 8?',
  expected_steps: 2,
  created_ms: 1_000,
};
const STEP_1: RunStep = {
  run_id: RUN.run_id,
  step: 1,
  expected_steps: 2,
  content: null,
  tool_calls: [
    { name: 'calculator', arguments: { op: 'multiply', a: 7, b: 8 }, result: { result: 56 } },
  ],
  error: null,
};
const STEP_2: RunStep = { ...STEP_1, step: 2, content: 'The result is 56.', tool_calls: [] };
const END: RunEnd = {
  run_id: RUN.run_id,
  status: 'done',
  answer: 'The result is 56.',
  error: null,
  steps: 2,
  invalid_calls: 0,
  elapsed_ms: 2_100,
};

describe('RunScreen', () => {
  let fixture: ComponentFixture<RunScreen>;
  let page: HTMLElement;
  let events: Subject<RunEvent>;
  let watched: string[];

  const text = (selector: string) =>
    page.querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim();
  const cards = () =>
    Array.from(page.querySelectorAll('.ag-step')).map((card) => [
      card.querySelector('.ag-step-n')?.textContent?.trim(),
      card.querySelector('.ag-step-tool')?.textContent?.trim(),
      card.className.replace('ag-step ', ''),
    ]);

  async function send(event: RunEvent) {
    events.next(event);
    await fixture.whenStable();
  }

  beforeEach(async () => {
    events = new Subject<RunEvent>();
    watched = [];
    TestBed.configureTestingModule({
      providers: [
        {
          provide: RunEvents,
          useValue: { watch: (id: string) => (watched.push(id), events.asObservable()) },
        },
        { provide: RunCalls, useValue: { read: () => of([]) } },
      ],
    });
    fixture = TestBed.createComponent(RunScreen);
    fixture.componentRef.setInput('id', RUN.run_id);
    page = fixture.nativeElement;
    await fixture.whenStable();
  });

  it('watches the run of its route', () => {
    expect(watched).toEqual([RUN.run_id]);
  });

  it('shows each step card as it lands, then the answer on the end event', async () => {
    await send({ kind: 'run', data: RUN });
    expect(text('.ag-run-head h1')).toBe('What is 7 times 8?');
    expect(text('.ag-now')).toBe('Step 1 · 2 expected');
    expect(cards()).toEqual([
      ['1', 'Waiting for the model', 'ag-step--running'],
      ['2', 'Not started', 'ag-step--pending'],
    ]);
    expect(page.querySelector('.ag-answer-wait')).not.toBeNull();

    await send({ kind: 'step', data: STEP_1 });
    expect(text('.ag-now')).toBe('Step 2 · 2 expected');
    expect(cards()).toEqual([
      ['1', 'calculator', 'ag-step--done'],
      ['2', 'Waiting for the model', 'ag-step--running'],
    ]);
    expect(text('.ag-step--done .ag-step-args')).toBe('multiply · 7 · 8');

    await send({ kind: 'step', data: STEP_2 });
    await send({ kind: 'end', data: END });
    expect(cards()).toEqual([
      ['1', 'calculator', 'ag-step--done'],
      ['2', 'Text reply', 'ag-step--done'],
    ]);
    expect(text('.ag-now')).toBe('Finished in 2.1 s');
    expect(page.querySelector('.ag-answer-wait')).toBeNull();
    expect(text('.ag-answer p')).toBe('The result is 56.');
    expect(text('.ag-answer-foot')).toBe(
      '2 steps · 2.1 s · numbers computed by calculator, not by the model',
    );
  });

  it('draws the run again from its replay after a reconnect', async () => {
    await send({ kind: 'run', data: RUN });
    await send({ kind: 'step', data: STEP_1 });
    await send({ kind: 'run', data: RUN });
    await send({ kind: 'step', data: STEP_1 });
    expect(cards().map(([n, , state]) => [n, state])).toEqual([
      ['1', 'ag-step--done'],
      ['2', 'ag-step--running'],
    ]);
  });

  it('says why a failed run has no answer', async () => {
    await send({ kind: 'run', data: RUN });
    await send({
      kind: 'end',
      data: {
        ...END,
        status: 'failed',
        answer: null,
        error: 'LLM call failed: HTTP 500',
        steps: 1,
      },
    });
    expect(text('.ag-now')).toBe('Failed after 2.1 s');
    expect(text('.ag-status--failed')).toBe('Failed');
    expect(cards()).toEqual([]);
    expect(page.querySelector('.ag-answer')).toBeNull();
    expect(text('.ag-notice--fail')).toBe('The run failed. LLM call failed: HTTP 500');
  });

  it('warns above an answer no tool result backs, and still shows the answer', async () => {
    await send({ kind: 'run', data: RUN });
    await send({
      kind: 'step',
      data: {
        ...STEP_1,
        tool_calls: [{ name: 'compare', arguments: {}, result: { error: 'not a group' } }],
      },
    });
    await send({ kind: 'step', data: STEP_2 });
    await send({ kind: 'end', data: END });
    expect(text('.ag-notice--warn')).toBe('No tool result backs this answer.');
    expect(text('.ag-answer p')).toBe('The result is 56.');
    expect(text('.ag-answer-foot')).toBe('2 steps · 2.1 s');
  });

  it('keeps a tool error whole on hover, since the card shows only its first lines', async () => {
    const error = `'photos' is not a group of id in reviews; groups: ${'a'.repeat(68)}`;
    await send({ kind: 'run', data: RUN });
    await send({
      kind: 'step',
      data: { ...STEP_1, tool_calls: [{ name: 'compare', arguments: {}, result: { error } }] },
    });
    const note = page.querySelector<HTMLElement>('.ag-step-note');
    expect(note?.textContent?.trim()).toBe(error);
    expect(note?.title).toBe(error);
  });

  it('shows no warning on an answer a tool result backs', async () => {
    await send({ kind: 'run', data: RUN });
    await send({ kind: 'step', data: STEP_1 });
    await send({ kind: 'step', data: STEP_2 });
    await send({ kind: 'end', data: END });
    expect(page.querySelector('.ag-notice--warn')).toBeNull();
  });

  it('says so when the run events cannot be read', async () => {
    events.error(new Error('closed'));
    await fixture.whenStable();
    expect(text('.ag-notice--fail')).toContain('The run events could not be read.');
  });
});

describe('RunScreen badges', () => {
  const CALL_1: RunCall = {
    step: 1,
    ts_start: 1_793_600_000_000,
    duration_ms: 812,
    gap_before_ms: 0,
    expected_steps: 2,
    prompt_tokens: 398,
    completion_tokens: 9,
    cached_tokens: 352,
    pod: 'vllm-5c9565db76-6bq8s',
  };
  const CALL_2: RunCall = { ...CALL_1, step: 2, duration_ms: 1_600, cached_tokens: 0 };

  let fixture: ComponentFixture<RunScreen>;
  let page: HTMLElement;
  let events: Subject<RunEvent>;
  // One per request, in order, so a test decides what each answers and when.
  let reads: Subject<RunCall[]>[];
  let asked: string[];

  const tags = (n: number) =>
    Array.from(page.querySelectorAll('.ag-step')[n].querySelectorAll('.ag-tag')).map((tag) =>
      tag.textContent?.trim(),
    );
  const lostLine = () => page.querySelector('.ag-calls-lost')?.textContent?.trim();

  async function send(event: RunEvent) {
    events.next(event);
    await fixture.whenStable();
  }

  async function answer(read: number, calls: RunCall[]) {
    reads[read].next(calls);
    reads[read].complete();
    await fixture.whenStable();
  }

  beforeEach(async () => {
    events = new Subject<RunEvent>();
    reads = [];
    asked = [];
    TestBed.configureTestingModule({
      providers: [
        { provide: RunEvents, useValue: { watch: () => events.asObservable() } },
        {
          provide: RunCalls,
          useValue: {
            read: (id: string) => {
              asked.push(id);
              const read = new Subject<RunCall[]>();
              reads.push(read);
              return read.asObservable();
            },
          },
        },
      ],
    });
    fixture = TestBed.createComponent(RunScreen);
    fixture.componentRef.setInput('id', RUN.run_id);
    page = fixture.nativeElement;
    await fixture.whenStable();
    await send({ kind: 'run', data: RUN });
  });

  it('does not ask before a step lands', () => {
    expect(asked).toEqual([]);
  });

  it('shows the card first, asks for the calls of its run, then shows the badges', async () => {
    await send({ kind: 'step', data: STEP_1 });
    expect(asked).toEqual([RUN.run_id]);
    expect(tags(0)).toEqual([]);

    await answer(0, [CALL_1]);
    expect(tags(0)).toEqual(['vllm-6bq8s', '88% cached', '812 ms']);
  });

  it('gives each step its own call, by step number', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await send({ kind: 'step', data: STEP_2 });
    await answer(1, [CALL_1, CALL_2]);
    expect(tags(0)).toEqual(['vllm-6bq8s', '88% cached', '812 ms']);
    expect(tags(1)).toEqual(['vllm-6bq8s', '0% cached', '1.6 s']);
  });

  it('drops a request still waiting when a newer step lands', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await send({ kind: 'step', data: STEP_2 });
    expect(reads[0].observed).toBe(false);
    expect(reads[1].observed).toBe(true);
  });

  it('still shows the badges when the last answer comes after the end event', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await send({ kind: 'end', data: { ...END, steps: 1 } });
    await answer(0, [CALL_1]);
    expect(tags(0)).toEqual(['vllm-6bq8s', '88% cached', '812 ms']);
  });

  it('keeps the run on a failed request, says so, and recovers with the next answer', async () => {
    await send({ kind: 'step', data: STEP_1 });
    reads[0].error(new Error('502'));
    await fixture.whenStable();
    expect(lostLine()).toBe('Pod, cache and duration could not be read.');
    expect(tags(0)).toEqual([]);
    expect(page.querySelector('.ag-notice--fail')).toBeNull();

    await send({ kind: 'step', data: STEP_2 });
    await send({ kind: 'end', data: END });
    expect(page.querySelector('.ag-answer')).not.toBeNull();
    await answer(1, [CALL_1, CALL_2]);
    expect(lostLine()).toBeUndefined();
    expect(tags(0)).toEqual(['vllm-6bq8s', '88% cached', '812 ms']);
  });

  it('keeps the badges it has when a later request fails', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await answer(0, [CALL_1]);
    await send({ kind: 'step', data: STEP_2 });
    reads[1].error(new Error('502'));
    await fixture.whenStable();
    expect(tags(0)).toEqual(['vllm-6bq8s', '88% cached', '812 ms']);
    expect(lostLine()).toBe('Pod, cache and duration could not be read.');
  });

  it('puts the error tag after the badges on a retried step', async () => {
    const rejected: RunStep = { ...STEP_2, step: 1, error: 'Error: answer with a call' };
    await send({ kind: 'step', data: rejected });
    await answer(0, [CALL_1]);
    expect(tags(0)).toEqual(['vllm-6bq8s', '88% cached', '812 ms', 'error']);
  });

  it('shows no tags row on a landed step without a record', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await answer(0, []);
    expect(page.querySelector('.ag-step--done .ag-tags')).toBeNull();
  });

  it('hides only the badge a missing field belongs to', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await answer(0, [{ ...CALL_1, pod: undefined }]);
    expect(tags(0)).toEqual(['88% cached', '812 ms']);
  });

  it('tints a hit, outlines a miss, and keeps the full pod name on hover', async () => {
    await send({ kind: 'step', data: STEP_1 });
    await send({ kind: 'step', data: STEP_2 });
    await answer(1, [CALL_1, CALL_2]);
    const [first, second] = Array.from(page.querySelectorAll('.ag-step')).map((card) =>
      Array.from(card.querySelectorAll<HTMLElement>('.ag-tag')),
    );
    expect(first[1].className).toBe('ag-tag ag-tag--hit');
    expect(second[1].className).toBe('ag-tag ag-tag--miss');
    expect(first[0].title).toBe('vllm-5c9565db76-6bq8s');
    expect(first[1].title).toBe('352 of 398 prompt tokens cached');
    expect(first[0].className).toBe('ag-tag');
    expect(first[2].className).toBe('ag-tag');
  });
});
