import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';
import { RunFile } from './run-files';

/** The fields of schemas/run-request.v1.json the UI sends. */
interface RunRequest {
  task: string;
  files?: RunFile[];
}

/** The agent runtime's run API, reached through the dev server's /api proxy (proxy.conf.json). */
@Injectable({ providedIn: 'root' })
export class Runs {
  private readonly http = inject(HttpClient);

  /** Starts a run and returns its id; the agent answers 202 before the run's first step. */
  start(task: string, files: RunFile[] = []): Observable<string> {
    const body: RunRequest = files.length > 0 ? { task, files } : { task };
    return this.http
      .post<{ run_id: string }>('/api/runs', body)
      .pipe(map((response) => response.run_id));
  }
}
