import { Injectable, InjectionToken, inject } from '@angular/core';
import { Observable } from 'rxjs';

/** The data of the events of GET /runs/{id}/events, as schemas/run-events.v1.json defines them (P1.4b-1). */
export interface RunStarted {
  run_id: string;
  task: string;
  expected_steps: number;
  created_ms: number;
}

export interface ToolCall {
  name: string;
  // The call's arguments as JSON, or the raw text when the model wrote invalid JSON.
  arguments: unknown;
  result: Record<string, unknown> | null;
}

export interface RunStep {
  run_id: string;
  step: number;
  expected_steps: number;
  content: string | null;
  tool_calls: ToolCall[];
  error: string | null;
}

export interface RunEnd {
  run_id: string;
  status: 'done' | 'failed';
  answer: string | null;
  error: string | null;
  steps: number;
  invalid_calls: number;
  elapsed_ms: number;
}

export type RunEvent =
  | { kind: 'run'; data: RunStarted }
  | { kind: 'step'; data: RunStep }
  | { kind: 'end'; data: RunEnd };

/** The part of the browser's EventSource the UI uses, so a test can stand in for it. */
export interface EventStream {
  readyState: number;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

export const OPEN_EVENT_STREAM = new InjectionToken<(url: string) => EventStream>(
  'OPEN_EVENT_STREAM',
  { providedIn: 'root', factory: () => (url: string) => new EventSource(url) },
);

// EventSource.CLOSED: the browser gave up on the stream and will not reconnect.
const CLOSED = 2;

/** A run's events from the agent runtime over SSE (D6), through the dev server's /api proxy. */
@Injectable({ providedIn: 'root' })
export class RunEvents {
  private readonly open = inject(OPEN_EVENT_STREAM);

  /** Completes after the end event. A dropped connection is reconnected by the browser, and the agent replays
   * the run from its run event. It errors once the browser gives up: the agent answered 404 (unknown or expired
   * run) or another error, or the dev server could not reach it (make agent-forward stopped). */
  watch(runId: string): Observable<RunEvent> {
    return new Observable<RunEvent>((subscriber) => {
      const stream = this.open(`/api/runs/${encodeURIComponent(runId)}/events`);
      stream.addEventListener('run', (event) => {
        subscriber.next({ kind: 'run', data: JSON.parse(event.data) as RunStarted });
      });
      stream.addEventListener('step', (event) => {
        subscriber.next({ kind: 'step', data: JSON.parse(event.data) as RunStep });
      });
      // The agent closes the stream after end; without close() the browser would reconnect and replay the run.
      stream.addEventListener('end', (event) => {
        subscriber.next({ kind: 'end', data: JSON.parse(event.data) as RunEnd });
        subscriber.complete();
      });
      stream.onerror = () => {
        if (stream.readyState === CLOSED) subscriber.error(new Error(`no events for ${runId}`));
      };
      return () => stream.close();
    });
  }
}
