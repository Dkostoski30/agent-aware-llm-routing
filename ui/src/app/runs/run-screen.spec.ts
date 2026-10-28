import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject } from 'rxjs';
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

  it('says so when the run events cannot be read', async () => {
    events.error(new Error('closed'));
    await fixture.whenStable();
    expect(text('.ag-notice--fail')).toContain('The run events could not be read.');
  });
});
