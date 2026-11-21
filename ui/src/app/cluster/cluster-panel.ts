import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, DestroyRef, effect, inject, signal } from '@angular/core';
import { Subscription } from 'rxjs';
import { Auth } from '../auth/auth';
import { CachePressure } from './cache-pressure';
import { CLUSTER_API, ClusterDown, ClusterView } from './cluster';
import { DemoControls } from './demo-controls';
import { PodCard } from './pod-card';
import { RemainingWork } from './remaining-work';

/** The cluster panel's content (P3.11): the remaining work, the cache pressure (P4.11) and the vLLM pods, from the
 * app backend's GET /cluster, asked again after the poll interval each answer carries, so the UI holds no number of
 * its own. Only a logged-in user asks, since the endpoint answers 401 to anyone else. When the numbers cannot be
 * read, it says so instead of showing zeros or numbers that are no longer true. The demo buttons (P4.6a) sit below
 * the scrolling part, as the design pins them, and only while the pods they offer are the latest read. */
@Component({
  selector: 'app-cluster-panel',
  imports: [CachePressure, DemoControls, PodCard, RemainingWork],
  template: `
    <div class="ag-panel-scroll">
      <div class="ag-panel-head"><h2>Cluster</h2></div>
      @if (lost(); as lost) {
        <p class="lost">
          The cluster numbers could not be read.{{ lost === 'stopped' ? ' Reload the page to try again.' : '' }}
        </p>
      } @else if (view(); as view) {
        <app-remaining-work [steps]="view.remaining_work" [runs]="view.active_runs" />
        <app-cache-pressure [pressure]="view.cache_pressure" [warn]="view.pressure_warn" />
        <section class="ag-psec">
          <h3 class="ag-psec-title">Pods</h3>
          <div class="ag-pods">
            @for (pod of view.pods; track pod.name) {
              <app-pod-card [pod]="pod" />
            }
          </div>
        </section>
      }
    </div>
    @if (view(); as view) {
      @if (!lost()) {
        <app-demo-controls [pods]="view.pods" />
      }
    }
  `,
  styles: `
    :host {
      display: contents;
    }
    .lost {
      margin: 0;
      padding: var(--space-3) var(--space-4);
      border-top: 1px solid var(--line);
      color: var(--ink-muted);
      font: 400 12px/16px var(--font-sans);
    }
  `,
})
export class ClusterPanel {
  private readonly http = inject(HttpClient);
  protected readonly view = signal<ClusterView | null>(null);
  // 'retrying': the last read failed and another is due; 'stopped': it failed and nothing said when to ask again.
  protected readonly lost = signal<'retrying' | 'stopped' | null>(null);
  private request: Subscription | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private pollSeconds: number | undefined;

  constructor() {
    const user = inject(Auth).user;
    effect(() => {
      this.stop();
      if (user()) {
        this.read();
      }
    });
    inject(DestroyRef).onDestroy(() => this.stop());
  }

  private read(): void {
    this.request = this.http.get<ClusterView>(CLUSTER_API).subscribe({
      next: (view) => {
        this.view.set(view);
        this.lost.set(null);
        this.again(view.poll_seconds);
      },
      error: (error: HttpErrorResponse) => {
        // A 503 names the interval; anything else (the app backend unreachable) falls back to the last one known.
        this.pollSeconds = (error.error as ClusterDown | null)?.poll_seconds ?? this.pollSeconds;
        if (this.pollSeconds === undefined) {
          this.lost.set('stopped');
          return;
        }
        this.lost.set('retrying');
        this.again(this.pollSeconds);
      },
    });
  }

  private again(seconds: number): void {
    this.pollSeconds = seconds;
    this.timer = setTimeout(() => this.read(), seconds * 1000);
  }

  /** No request in flight, none due, and nothing shown: a logout or the panel's removal. The lost line shows
   * ahead of the view, so a failed read needs no clearing of its own. */
  private stop(): void {
    this.request?.unsubscribe();
    clearTimeout(this.timer);
    this.view.set(null);
    this.lost.set(null);
  }
}
