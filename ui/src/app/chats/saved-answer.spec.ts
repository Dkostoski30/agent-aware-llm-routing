import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RunCall } from '../runs/run-calls';
import { Message } from './chats';
import { SavedAnswer } from './saved-answer';
import { SavedRun } from './saved-runs';

const RUN_ID = 'run-0a1b2c3d4e5f';
const RUN_URL = `/api/runs/${RUN_ID}`;
const CALLS_URL = `${RUN_URL}/calls`;
const ANSWERED: Message = {
  id: 2,
  role: 'assistant',
  text: 'The result is 56.',
  status: 'done',
  run_id: RUN_ID,
  created: 1_794_950_000,
  files: [],
};
const FAILED: Message = { ...ANSWERED, status: 'failed', text: 'LLM call failed: HTTP 500' };
const RUN: SavedRun = {
  run_id: RUN_ID,
  status: 'done',
  task: 'What is 7 times 8?',
  answer: 'The result is 56.',
  error: null,
  expected_steps: 2,
  invalid_calls: 0,
  steps: [
    {
      step: 1,
      content: null,
      tool_calls: [
        { name: 'calculator', arguments: { op: 'multiply', a: 7, b: 8 }, result: { result: 56 } },
      ],
      error: null,
    },
    { step: 2, content: 'The result is 56.', tool_calls: [], error: null },
  ],
};
const CALL: RunCall = {
  step: 1,
  ts_start: 1_793_600_000_000,
  duration_ms: 290,
  gap_before_ms: 0,
  expected_steps: 2,
  prompt_tokens: 1_534,
  cached_tokens: 1_352,
  pod: 'vllm-5c9565db76-6bq8s',
};

describe('SavedAnswer', () => {
  let fixture: ComponentFixture<SavedAnswer>;
  let http: HttpTestingController;
  let page: HTMLElement;

  const text = (selector: string) =>
    page.querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim();
  const button = () => page.querySelector<HTMLButtonElement>('button[aria-expanded]');
  const cards = () =>
    Array.from(page.querySelectorAll('.ag-step')).map((card) =>
      Array.from(card.querySelectorAll('.ag-step-tool, .ag-tag')).map((part) =>
        part.textContent?.trim(),
      ),
    );

  async function show(message: Message, open = false) {
    fixture = TestBed.createComponent(SavedAnswer);
    fixture.componentRef.setInput('message', message);
    fixture.componentRef.setInput('open', open);
    page = fixture.nativeElement;
    await fixture.whenStable();
  }

  async function flip() {
    button()?.click();
    await fixture.whenStable();
  }

  async function answer(run: SavedRun | number, calls: RunCall[] | number = [CALL]) {
    // Both are asked at once; the calls are answered first, since a failed run read drops the calls request.
    const runRequest = http.expectOne(RUN_URL);
    const callsRequest = http.expectOne(CALLS_URL);
    if (typeof calls === 'number') callsRequest.flush('', { status: calls, statusText: 'Error' });
    else callsRequest.flush({ calls });
    if (typeof run === 'number') runRequest.flush('', { status: run, statusText: 'Error' });
    else runRequest.flush(run);
    await fixture.whenStable();
  }

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('shows the answer with its steps folded away, and reads no steps', async () => {
    await show(ANSWERED);
    expect(text('.ag-answer p')).toBe('The result is 56.');
    expect(button()?.textContent?.trim()).toBe('Show steps');
    expect(button()?.getAttribute('aria-expanded')).toBe('false');
    expect(cards()).toEqual([]);
    http.expectNone(RUN_URL);
    http.expectNone(CALLS_URL);
  });

  it('reads the run and its calls on expand and shows each step with its badges', async () => {
    await show(ANSWERED);
    await flip();
    await answer(RUN);
    expect(button()?.textContent?.trim()).toBe('Hide steps');
    expect(button()?.getAttribute('aria-expanded')).toBe('true');
    expect(text('.ag-section-head small')).toBe('2 taken');
    expect(cards()).toEqual([
      ['calculator', 'vllm-6bq8s', '88% cached', '290 ms'],
      ['Text reply'],
    ]);
    expect(page.querySelector('.ag-notice--warn')).toBeNull();
  });

  it('folds the steps away and shows them again without reading them again', async () => {
    await show(ANSWERED);
    await flip();
    await answer(RUN);
    await flip();
    expect(cards()).toEqual([]);
    await flip();
    expect(cards().length).toBe(2);
    http.expectNone(RUN_URL);
  });

  it('says in one line that the steps are gone once the runtime no longer has the run', async () => {
    await show(ANSWERED);
    await flip();
    await answer(404, 404);
    expect(text('.steps-line')).toBe('The steps of this answer are no longer kept.');
    expect(cards()).toEqual([]);
    expect(text('.ag-answer p')).toBe('The result is 56.');
  });

  it('says so when the steps cannot be read', async () => {
    await show(ANSWERED);
    await flip();
    await answer(502, 502);
    expect(text('.steps-line')).toBe(
      'The steps could not be read: the app backend or the agent runtime did not answer.',
    );
    expect(cards()).toEqual([]);
  });

  it('reads the steps again on the next expand after they could not be read', async () => {
    await show(ANSWERED);
    await flip();
    await answer(502, 502);
    await flip();
    await flip();
    await answer(RUN);
    expect(page.querySelector('.steps-line')).toBeNull();
    expect(cards().length).toBe(2);
  });

  it('does not read again once the runtime said the run is gone', async () => {
    await show(ANSWERED);
    await flip();
    await answer(404, 404);
    await flip();
    await flip();
    http.expectNone(RUN_URL);
    expect(text('.steps-line')).toBe('The steps of this answer are no longer kept.');
  });

  it('shows the steps without badges when only the calls cannot be read', async () => {
    await show(ANSWERED);
    await flip();
    await answer(RUN, 502);
    expect(cards()).toEqual([['calculator'], ['Text reply']]);
    expect(text('.ag-calls-lost')).toBe('Pod, cache and duration could not be read.');
  });

  it('warns above the steps when no tool result backs the answer', async () => {
    const unbacked: SavedRun = {
      ...RUN,
      steps: [
        {
          ...RUN.steps[0],
          tool_calls: [{ name: 'compare', arguments: {}, result: { error: 'not a group' } }],
        },
        RUN.steps[1],
      ],
    };
    await show(ANSWERED);
    expect(page.querySelector('.ag-notice--warn')).toBeNull();
    await flip();
    await answer(unbacked);
    expect(text('.ag-notice--warn')).toBe('No tool result backs this answer.');
  });

  it('opens with its steps read when the chat asks, as for the answer whose run just ended', async () => {
    await show(ANSWERED, true);
    await answer(RUN);
    expect(button()?.textContent?.trim()).toBe('Hide steps');
    expect(cards().length).toBe(2);
  });

  it('shows a failed run’s sentence, and its steps on expand without a warning, having no answer', async () => {
    const failedRun: SavedRun = {
      ...RUN,
      status: 'failed',
      answer: null,
      error: FAILED.text,
      steps: [
        {
          ...RUN.steps[0],
          tool_calls: [{ name: 'compare', arguments: {}, result: { error: 'not a group' } }],
        },
      ],
    };
    await show(FAILED);
    expect(text('.ag-notice--fail')).toBe('The run failed. LLM call failed: HTTP 500');
    expect(page.querySelector('.ag-answer')).toBeNull();
    await flip();
    await answer(failedRun);
    expect(cards().map(([tool]) => tool)).toEqual(['compare']);
    expect(page.querySelector('.ag-notice--warn')).toBeNull();
  });

  it('offers no steps for a message whose run never started', async () => {
    await show({ ...FAILED, run_id: null, text: 'The agent runtime did not answer.' });
    expect(text('.ag-notice--fail')).toBe('The run failed. The agent runtime did not answer.');
    expect(button()).toBeNull();
  });
});
