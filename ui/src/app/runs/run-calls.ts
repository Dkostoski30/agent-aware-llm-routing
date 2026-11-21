import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

/** One call of schemas/run-calls.v1.json. A measurement the reply did not have is left out (P2.2a). */
export interface RunCall {
  step: number;
  ts_start: number;
  duration_ms: number;
  gap_before_ms: number;
  expected_steps: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_tokens?: number;
  pod?: string;
  // Sent again this many times because its pod died (P4.4); left out when it was sent once (P4.6b).
  retries?: number;
  // The first call a new agent runtime sent for a run it resumed from Redis (P4.4, P4.6b).
  resumed?: true;
}

/** A run's call records from the agent runtime, passed on by the app backend (P3.12b), through the dev server's
 * /api proxy. */
@Injectable({ providedIn: 'root' })
export class RunCalls {
  private readonly http = inject(HttpClient);

  /** All the calls so far, so a later answer replaces an earlier one. */
  read(runId: string): Observable<RunCall[]> {
    return this.http
      .get<{ calls: RunCall[] }>(`/api/runs/${encodeURIComponent(runId)}/calls`)
      .pipe(map((body) => body.calls));
  }
}
