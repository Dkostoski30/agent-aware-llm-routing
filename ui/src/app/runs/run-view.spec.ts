import { RunCall } from './run-calls';
import { RunEnd, RunStarted, RunStep, ToolCall } from './run-events';
import { NO_RUN, RunView, applyEvent, formatMs, segments, shortPod, stepCards } from './run-view';

const RUN: RunStarted = {
  run_id: 'run-0a1b2c3d4e5f',
  task: 'What is 7 times 8?',
  expected_steps: 3,
  created_ms: 1_000,
};

function calc(op: string, a: number, b: number, result: number): ToolCall {
  return { name: 'calculator', arguments: { op, a, b }, result: { result } };
}

function step(n: number, tool_calls: ToolCall[], extra: Partial<RunStep> = {}): RunStep {
  return {
    run_id: RUN.run_id,
    step: n,
    expected_steps: 3,
    content: null,
    tool_calls,
    error: null,
    ...extra,
  };
}

function end(steps: number, extra: Partial<RunEnd> = {}): RunEnd {
  return {
    run_id: RUN.run_id,
    status: 'done',
    answer: '56',
    error: null,
    steps,
    invalid_calls: 0,
    elapsed_ms: 2_100,
    ...extra,
  };
}

const STEP_1 = step(1, [calc('multiply', 7, 8, 56)]);
const STEP_2 = step(2, [], { content: '56' });

function view(steps: RunStep[], ended?: RunEnd): RunView {
  return { run: RUN, steps, end: ended };
}

describe('applyEvent', () => {
  it('starts from the run event, then adds steps in arrival order, then the end', () => {
    let current = applyEvent(NO_RUN, { kind: 'run', data: RUN });
    current = applyEvent(current, { kind: 'step', data: STEP_1 });
    current = applyEvent(current, { kind: 'step', data: STEP_2 });
    current = applyEvent(current, { kind: 'end', data: end(2) });
    expect(current).toEqual({ run: RUN, steps: [STEP_1, STEP_2], end: end(2) });
  });

  it('starts over on a second run event, so a reconnect replaces the steps instead of repeating them', () => {
    const before = view([STEP_1]);
    const after = applyEvent(applyEvent(before, { kind: 'run', data: RUN }), {
      kind: 'step',
      data: STEP_1,
    });
    expect(after.steps).toEqual([STEP_1]);
  });
});

describe('stepCards', () => {
  it('shows the landed steps, the call in flight and the rest of the estimate while the run goes', () => {
    expect(stepCards(view([STEP_1]))).toEqual([
      { n: 1, tool: 'calculator', args: 'multiply · 7 · 8', state: 'done' },
      { n: 2, tool: 'Waiting for the model', args: '', state: 'running' },
      { n: 3, tool: 'Not started', args: '', state: 'pending' },
    ]);
  });

  it('shows only the steps the run took once it ends, even before the estimate', () => {
    expect(stepCards(view([STEP_1, STEP_2], end(2))).map((card) => [card.n, card.state])).toEqual([
      [1, 'done'],
      [2, 'done'],
    ]);
  });

  it('keeps adding cards past the estimate', () => {
    const steps = [STEP_1, step(2, [calc('add', 56, 0, 56)]), step(3, [calc('add', 56, 0, 56)])];
    expect(stepCards(view(steps)).map((card) => [card.n, card.state])).toEqual([
      [1, 'done'],
      [2, 'done'],
      [3, 'done'],
      [4, 'running'],
    ]);
  });

  it('numbers the call in flight after the last landed step, as the agent numbered it', () => {
    const third = step(3, [calc('add', 56, 0, 56)]);
    expect(stepCards(view([STEP_1, third])).map((card) => [card.n, card.state])).toEqual([
      [1, 'done'],
      [3, 'done'],
      [4, 'running'],
    ]);
  });

  it('names a text reply and repeated calls of one tool', () => {
    const reads = step(1, [
      { name: 'read_document', arguments: { file: 'contract_a' }, result: { text: 'a' } },
      { name: 'read_document', arguments: { file: 'contract_b' }, result: { text: 'b' } },
    ]);
    const [first, second] = stepCards(view([reads, STEP_2], end(2)));
    expect([first.tool, first.args]).toEqual(['read_document ×2', 'contract_a, contract_b']);
    expect([second.tool, second.args]).toEqual(['Text reply', '']);
  });

  it('marks a step the runtime sent an error back for, with the error as its note', () => {
    const invalid = step(1, [
      {
        name: 'calculator',
        arguments: '{"op": "add"',
        result: { error: 'arguments are not valid JSON' },
      },
    ]);
    const rejected = step(2, [], { content: 'maybe 56', error: 'Error: answer with a call' });
    const [first, second] = stepCards(view([invalid, rejected]));
    expect(first).toEqual({
      n: 1,
      tool: 'calculator',
      args: '{"op": "add"',
      state: 'retried',
      note: 'arguments are not valid JSON',
    });
    expect([second.state, second.note]).toEqual(['retried', 'Error: answer with a call']);
  });
});

describe('segments', () => {
  it('has one segment per expected step: done, running, pending', () => {
    expect(segments(view([STEP_1]))).toEqual(['done', 'run', '']);
  });

  it('adds amber segments past the estimate', () => {
    expect(segments(view([STEP_1, STEP_1, STEP_1]))).toEqual(['done', 'done', 'done', 'over']);
    expect(segments(view([STEP_1, STEP_1, STEP_1, STEP_1], end(4)))).toEqual([
      'done',
      'done',
      'done',
      'over',
    ]);
  });

  it('shrinks to the steps taken when the run ends early', () => {
    expect(segments(view([STEP_1, STEP_2], end(2)))).toEqual(['done', 'done']);
  });
});

describe('formatMs', () => {
  it('writes ms under a second and seconds with one decimal above', () => {
    expect([formatMs(290), formatMs(999), formatMs(2_100), formatMs(21_084)]).toEqual([
      '290 ms',
      '999 ms',
      '2.1 s',
      '21.1 s',
    ]);
  });
});

describe('stepCards with call records', () => {
  const CALL: RunCall = {
    step: 1,
    ts_start: 1_793_600_000_000,
    duration_ms: 290,
    gap_before_ms: 0,
    expected_steps: 3,
    prompt_tokens: 1_534,
    completion_tokens: 9,
    cached_tokens: 1_352,
    pod: 'vllm-5c9565db76-6bq8s',
  };

  it('gives a landed step its pod, cache and latency', () => {
    const [first] = stepCards(view([STEP_1]), [CALL]);
    expect(first.served).toEqual({
      pod: { short: 'vllm-6bq8s', full: 'vllm-5c9565db76-6bq8s' },
      cache: {
        text: '88% cached',
        detail: '1,352 of 1,534 prompt tokens cached',
        hit: true,
      },
      latency: '290 ms',
    });
  });

  it('leaves out what a record lacks, and a cache figure without both counts', () => {
    const bare = { ...CALL, pod: undefined, prompt_tokens: undefined };
    expect(stepCards(view([STEP_1]), [bare])[0].served).toEqual({ latency: '290 ms' });
    const noCached = { ...CALL, cached_tokens: undefined };
    expect(stepCards(view([STEP_1]), [noCached])[0].served?.cache).toBeUndefined();
  });

  it('calls exactly 0 cached tokens a miss, and any other count a hit', () => {
    const cache = (cached_tokens: number) =>
      stepCards(view([STEP_1]), [{ ...CALL, cached_tokens }])[0].served?.cache;
    expect(cache(0)).toMatchObject({ detail: '0 of 1,534 prompt tokens cached', hit: false });
    expect(cache(1)).toMatchObject({ detail: '1 of 1,534 prompt tokens cached', hit: true });
  });

  it('rounds the percent down, so 100% means every token was cached', () => {
    const text = (cached_tokens: number) =>
      stepCards(view([STEP_1]), [{ ...CALL, prompt_tokens: 400, cached_tokens }])[0].served?.cache
        ?.text;
    expect(text(399)).toBe('99% cached');
    expect(text(400)).toBe('100% cached');
  });

  it.each([
    ['no prompt tokens', 0, 0],
    ['no prompt tokens but some cached', 0, 5],
    ['more cached than the prompt holds', 400, 500],
    ['a negative prompt count', -1, 0],
    ['a negative cached count', 400, -1],
  ])('shows no cache badge for %s, rather than a nonsense percent', (_, prompt, cached) => {
    const call = { ...CALL, prompt_tokens: prompt, cached_tokens: cached };
    const served = stepCards(view([STEP_1]), [call])[0].served;
    expect(served?.cache).toBeUndefined();
    expect(served?.latency).toBe('290 ms');
  });

  it('shows no pod badge for an empty pod name', () => {
    expect(stepCards(view([STEP_1]), [{ ...CALL, pod: '' }])[0].served?.pod).toBeUndefined();
  });

  it('matches records to steps by number and gives no step a call that is not its own', () => {
    const third = step(3, [calc('add', 56, 0, 56)]);
    const cards = stepCards(view([STEP_1, third]), [{ ...CALL, step: 3 }]);
    expect(cards.map((card) => [card.n, card.served?.latency])).toEqual([
      [1, undefined],
      [3, '290 ms'],
      [4, undefined],
    ]);
  });

  it('gives the call in flight and the pending cards no badges', () => {
    const later = { ...CALL, step: 2 };
    const cards = stepCards(view([STEP_1]), [CALL, later]);
    expect(cards.map((card) => [card.state, card.served === undefined])).toEqual([
      ['done', false],
      ['running', true],
      ['pending', true],
    ]);
  });

  it('keeps a retried step in its state with its badges', () => {
    const [card] = stepCards(view([step(1, [], { error: 'Error: answer with a call' })]), [CALL]);
    expect([card.state, card.served?.latency]).toEqual(['retried', '290 ms']);
  });
});

describe('shortPod', () => {
  it.each([
    ['vllm-5c9565db76-6bq8s', 'vllm-6bq8s'],
    ['vllm-6f9c8d7b5-x2kqp', 'vllm-x2kqp'],
    ['llm-d-sim-5c9565db76-6bq8s', 'llm-d-sim-6bq8s'],
  ])('drops the ReplicaSet hash of %s', (full, short) => {
    expect(shortPod(full)).toBe(short);
  });

  it.each([
    ['vllm-0'],
    ['sim-2'],
    ['llm-d-2'],
    ['vllm-6bq8s'],
    ['vllm-0.31.0-1a2b3c4d'],
    ['llm-d-6bq8s'],
  ])('leaves %s as it is, since it is not shaped like a pod of a Deployment', (name) => {
    expect(shortPod(name)).toBe(name);
  });
});
