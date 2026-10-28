import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

/** The fields of schemas/run-request.v1.json the UI sends; files come with the chat app (P3.13). */
interface RunRequest {
  task: string;
}

/** The agent runtime's run API, reached through the dev server's /api proxy (proxy.conf.json). */
@Injectable({ providedIn: 'root' })
export class Runs {
  private readonly http = inject(HttpClient);

  /** Starts a run and returns its id; the agent answers 202 before the run's first step. */
  start(task: string): Observable<string> {
    const body: RunRequest = { task };
    return this.http
      .post<{ run_id: string }>('/api/runs', body)
      .pipe(map((response) => response.run_id));
  }
}
