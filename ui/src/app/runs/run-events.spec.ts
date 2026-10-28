import { TestBed } from '@angular/core/testing';
import runEventsSchema from '../../../../schemas/run-events.v1.json';
import {
  EventStream,
  OPEN_EVENT_STREAM,
  RunEnd,
  RunEvent,
  RunEvents,
  RunStarted,
  RunStep,
  ToolCall,
} from './run-events';

// EventSource.readyState values (HTML spec); jsdom has no EventSource to read them from.
const CONNECTING = 0;
const CLOSED = 2;

/** Stands in for the browser's EventSource: the test sends the events and the errors. */
class FakeStream implements EventStream {
  readyState = 1;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, (event: MessageEvent<string>) => void>();

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, listener);
  }

  close(): void {
    this.closed = true;
  }

  send(type: string, data: object): void {
    this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify(data) }));
  }

  fail(readyState: number): void {
    this.readyState = readyState;
    this.onerror?.(new Event('error'));
  }
}

const RUN: RunStarted = {
  run_id: 'run-0a1b2c3d4e5f',
  task: 'What is 7 times 8?',
  expected_steps: 2,
  created_ms: 1_793_191_313_874,
};
const CALL: ToolCall = {
  name: 'calculator',
  arguments: { op: 'multiply', a: 7, b: 8 },
  result: { result: 56 },
};
const STEP: RunStep = {
  run_id: RUN.run_id,
  step: 1,
  expected_steps: 2,
  content: null,
  tool_calls: [CALL],
  error: null,
};
const END: RunEnd = {
  run_id: RUN.run_id,
  status: 'done',
  answer: 'The result is 56.',
  error: null,
  steps: 2,
  invalid_calls: 0,
  elapsed_ms: 21_084,
};

describe('RunEvents', () => {
  let stream: FakeStream | undefined;
  let received: RunEvent[];
  let completed: boolean;
  let failed: boolean;

  function watch() {
    return TestBed.inject(RunEvents)
      .watch(RUN.run_id)
      .subscribe({
        next: (event) => received.push(event),
        complete: () => (completed = true),
        error: () => (failed = true),
      });
  }

  beforeEach(() => {
    stream = undefined;
    received = [];
    completed = false;
    failed = false;
    TestBed.configureTestingModule({
      providers: [
        { provide: OPEN_EVENT_STREAM, useValue: (url: string) => (stream = new FakeStream(url)) },
      ],
    });
  });

  it('opens the run events through the /api proxy and passes them on in order', () => {
    watch();
    expect(stream?.url).toBe('/api/runs/run-0a1b2c3d4e5f/events');
    stream?.send('run', RUN);
    stream?.send('step', STEP);
    expect(received).toEqual([
      { kind: 'run', data: RUN },
      { kind: 'step', data: STEP },
    ]);
  });

  it('closes the stream and completes on the end event, so the browser does not reconnect', () => {
    watch();
    stream?.send('end', END);
    expect(received).toEqual([{ kind: 'end', data: END }]);
    expect([completed, stream?.closed]).toEqual([true, true]);
  });

  it('closes the stream when the subscriber leaves', () => {
    watch().unsubscribe();
    expect(stream?.closed).toBe(true);
  });

  it('lets the browser reconnect after a dropped connection', () => {
    watch();
    stream?.fail(CONNECTING);
    expect([failed, stream?.closed]).toEqual([false, false]);
  });

  it('fails once the browser gives up, as on a 404 for an unknown run', () => {
    watch();
    stream?.fail(CLOSED);
    expect([failed, stream?.closed]).toEqual([true, true]);
  });

  // Contract test (D12): each event type has exactly the fields schemas/run-events.v1.json requires; every
  // field there is required and no other is allowed.
  it('reads the fields that schemas/run-events.v1.json defines', () => {
    const defs = runEventsSchema.$defs;
    expect(Object.keys(RUN).sort()).toEqual([...defs.run.required].sort());
    expect(Object.keys(STEP).sort()).toEqual([...defs.step.required].sort());
    expect(Object.keys(CALL).sort()).toEqual(
      [...defs.step.properties.tool_calls.items.required].sort(),
    );
    expect(Object.keys(END).sort()).toEqual([...defs.end.required].sort());
    expect(defs.end.properties.status.enum).toEqual(['done', 'failed']);
  });
});
