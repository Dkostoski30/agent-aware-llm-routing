import { RunCall } from './run-calls';
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

/** How a step's call was served, as its badges read (Tag card). A measurement the call lacks has no badge. */
export interface Served {
  pod?: { short: string; full: string };
  // hit: at least one token came from the prefix cache; text says how much of the prompt, detail the exact counts.
  cache?: { text: string; detail: string; hit: boolean };
  latency: string;
}

/** The inputs of one StepCard. served is absent on a step whose call record has not arrived, or that has none. */
export interface StepCardData {
  n: number;
  tool: string;
  args: string;
  state: StepState;
  note?: string;
  served?: Served;
}

/** x-expected-steps of the latest step, which the run tracker may update after step 1 (D13). */
export function expectedSteps(view: RunView): number {
  return view.steps.at(-1)?.expected_steps ?? view.run?.expected_steps ?? 0;
}

/** The x-step of the call in flight: one past the last landed step, whose number the agent sent. */
export function stepInFlight(view: RunView): number {
  return (view.steps.at(-1)?.step ?? 0) + 1;
}

/** The landed steps, each with its call record if calls has one; while the run goes, also the call in flight and
 * dashed cards up to the estimate. */
export function stepCards(view: RunView, calls: RunCall[] = []): StepCardData[] {
  const callOfStep = new Map(calls.map((call) => [call.step, call]));
  const cards = view.steps.map((step) => landedCard(step, callOfStep.get(step.step)));
  if (view.end) return cards;
  const running = stepInFlight(view);
  cards.push({ n: running, tool: 'Waiting for the model', args: '', state: 'running' });
  for (let n = running + 1; n <= expectedSteps(view); n++) {
    cards.push({ n, tool: 'Not started', args: '', state: 'pending' });
  }
  return cards;
}

function landedCard(step: RunStep, call: RunCall | undefined): StepCardData {
  const card: StepCardData = {
    n: step.step,
    tool: toolLabel(step.tool_calls),
    args: step.tool_calls.map((toolCall) => argsText(toolCall.arguments)).join(', '),
    state: 'done',
    served: call && served(call),
  };
  // The runtime sent an error back to the model: an invalid call or a tool's error on the data (in a result; the
  // event does not tell the two apart), or a rejected text reply (on the step).
  const note = step.error ?? step.tool_calls.map(resultError).find((error) => error !== undefined);
  return note === undefined ? card : { ...card, state: 'retried', note };
}

function served(call: RunCall): Served {
  return {
    pod: call.pod ? { short: shortPod(call.pod), full: call.pod } : undefined,
    cache: cacheBadge(call),
    latency: formatMs(call.duration_ms),
  };
}

function cacheBadge({ prompt_tokens: prompt, cached_tokens: cached }: RunCall): Served['cache'] {
  if (prompt === undefined || cached === undefined) return undefined;
  // The schema can't rule out all of these and the agent only checks types (card 24): no percent for them.
  if (prompt <= 0 || cached < 0 || cached > prompt) return undefined;
  // Down, so 100% means every token was cached: vLLM leaves a part-filled block uncached, so 99.6% is not.
  const percent = Math.floor((100 * cached) / prompt);
  return {
    text: `${percent}% cached`,
    detail: `${cached.toLocaleString('en')} of ${prompt.toLocaleString('en')} prompt tokens cached`,
    hit: cached > 0,
  };
}

// <Deployment>-<ReplicaSet hash>-<pod suffix>, as Kubernetes names a Deployment's pods. The suffix has 5 characters.
// The hashes here have 9 or 10; 5 as the lower bound is this project's choice, so a word such as the d of llm-d-6bq8s
// is not taken for a hash. The shape can match other workloads' names: recheck when P2.7 names the simulator's pods.
const DEPLOYMENT_POD = /^(.+)-[a-z0-9]{5,10}-([a-z0-9]{5})$/;

/** The pod's name without its ReplicaSet hash, which only separates rollouts: vllm-5c9565db76-6bq8s is vllm-6bq8s. */
export function shortPod(name: string): string {
  return name.replace(DEPLOYMENT_POD, '$1-$2');
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
