import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { SavedStep } from '../runs/run-view';

/** A run as the agent runtime's GET /runs/{id} answers it (agent/agent_runtime/app.py), whole, with its steps. */
export interface SavedRun {
  run_id: string;
  status: 'running' | 'done' | 'failed';
  task: string;
  answer: string | null;
  error: string | null;
  expected_steps: number;
  invalid_calls: number;
  steps: SavedStep[];
}

/** A run read once it has ended, passed on by the app backend after its owner check (P3.12b), through the dev
 * server's /api proxy. The runtime keeps a run for agent.history_ttl_seconds; after that this answers 404. */
@Injectable({ providedIn: 'root' })
export class SavedRuns {
  private readonly http = inject(HttpClient);

  read(runId: string): Observable<SavedRun> {
    return this.http.get<SavedRun>(`/api/runs/${encodeURIComponent(runId)}`);
  }
}
