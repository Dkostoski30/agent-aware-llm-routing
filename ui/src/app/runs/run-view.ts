import { RunEnd, RunEvent, RunStarted, RunStep, ToolCall } from './run-events';

/** What the run screen knows about a run: its run event, the steps so far and, once it is over, its end. */
export interface RunView {
  run?: RunStarted;
  steps: RunStep[];
  end?: RunEnd;
}

export const NO_RUN: RunView = { steps: [] };

export function applyEvent(view: RunView, event: RunEvent): RunView {
  switch (event.kind) {
    // A reconnect replays the run from its run event, so the run event starts the view over.
    case 'run':
      return { run: event.data, steps: [] };
    case 'step':
      return { ...view, steps: [...view.steps, event.data] };
    case 'end':
      return { ...view, end: event.data };
  }
}

export type StepState = 'done' | 'running' | 'pending' | 'retried';

/** The inputs of one StepCard. Pod, cache and latency come with the call recorder (P2.6). */
export interface StepCardData {
  n: number;
  tool: string;
  args: string;
  state: StepState;
  note?: string;
}

/** x-expected-steps of the latest step, which the run tracker may update after step 1 (D13). */
export function expectedSteps(view: RunView): number {
  return view.steps.at(-1)?.expected_steps ?? view.run?.expected_steps ?? 0;
}

/** The x-step of the call in flight: one past the last landed step, whose number the agent sent. */
export function stepInFlight(view: RunView): number {
  return (view.steps.at(-1)?.step ?? 0) + 1;
}

/** The landed steps; while the run goes, also the call in flight and dashed cards up to the estimate. */
export function stepCards(view: RunView): StepCardData[] {
  const cards = view.steps.map(landedCard);
  if (view.end) return cards;
  const running = stepInFlight(view);
  cards.push({ n: running, tool: 'Waiting for the model', args: '', state: 'running' });
  for (let n = running + 1; n <= expectedSteps(view); n++) {
    cards.push({ n, tool: 'Not started', args: '', state: 'pending' });
  }
  return cards;
}

function landedCard(step: RunStep): StepCardData {
  const card: StepCardData = {
    n: step.step,
    tool: toolLabel(step.tool_calls),
    args: step.tool_calls.map((call) => argsText(call.arguments)).join(', '),
    state: 'done',
  };
  // The runtime sent an error back to the model: an invalid call or a tool's error on the data (in a result; the
  // event does not tell the two apart), or a rejected text reply (on the step).
  const note = step.error ?? step.tool_calls.map(resultError).find((error) => error !== undefined);
  return note === undefined ? card : { ...card, state: 'retried', note };
}

function toolLabel(calls: ToolCall[]): string {
  if (calls.length === 0) return 'Text reply';
  const names = calls.map((call) => call.name);
  return names.every((name) => name === names[0]) && names.length > 1
    ? `${names[0]} ×${names.length}`
    : names.join(', ');
}

/** The argument values, as the system emits them: "contract_a · termination". */
function argsText(args: unknown): string {
  if (typeof args === 'string') return args;
  if (typeof args !== 'object' || args === null) return JSON.stringify(args);
  return Object.values(args)
    .map((value) => (typeof value === 'object' ? JSON.stringify(value) : String(value)))
    .join(' · ');
}

function resultError(call: ToolCall): string | undefined {
  const error = call.result?.['error'];
  return typeof error === 'string' ? error : undefined;
}

/** The header's bar: one segment per expected step, amber past the estimate; the steps taken once it ends. */
export function segments(view: RunView): string[] {
  const done = view.steps.length;
  const expected = expectedSteps(view);
  const count = view.end ? done : Math.max(expected, done + 1);
  return Array.from({ length: count }, (_, i) => {
    const n = i + 1;
    if (n > expected) return 'over';
    if (n <= done) return 'done';
    return n === done + 1 && !view.end ? 'run' : '';
  });
}

/** The tools whose results the answer can rest on: every tool that ran without an error, final_answer aside. */
export function computedBy(view: RunView): string[] {
  const names = view.steps.flatMap((step) =>
    step.tool_calls
      .filter((call) => call.name !== 'final_answer' && call.result !== null && !resultError(call))
      .map((call) => call.name),
  );
  return [...new Set(names)];
}

/** Under a second in ms, above in seconds with one decimal (README: Content). */
export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}
