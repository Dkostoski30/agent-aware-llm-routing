import { RunEnd, RunStarted, RunStep, ToolCall } from './run-events';
import { NO_RUN, RunView, applyEvent, formatMs, segments, stepCards } from './run-view';

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
