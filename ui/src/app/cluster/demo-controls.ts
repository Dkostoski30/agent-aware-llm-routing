import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, DestroyRef, computed, inject, input, signal } from '@angular/core';
import { Subscription } from 'rxjs';
import { Icon } from '../ui/icon';
import { ClusterPod } from './cluster';
import { DEMO_API, DEMO_KILL_API, DEMO_REPLAY_API, DemoRunner, DemoView } from './demo';

const NO_ANSWER = 'The app backend did not answer.';

/** The design's DemoControls (D14, P4.6a), at the bottom of the cluster panel: Start replays the recorded demo
 * trace with the chosen pattern, Kill force-deletes the chosen vLLM pod, both run on the host by make demo-runner
 * through the app backend. It reads GET /demo on its own, again after the interval each answer carries, and shows
 * nothing on a profile without the buttons or when the app backend cannot say. Pods come from the panel's own
 * read, so the select offers only pods that still serve: a draining or failed pod is already on its way out. */
@Component({
  selector: 'app-demo-controls',
  imports: [Icon],
  template: `
    @if (view(); as view) {
      @if (view.enabled) {
        <div class="ag-demo">
          <div class="ag-demo-row">
            <button
              type="button"
              class="ag-btn ag-btn--primary"
              [disabled]="!view.runner || running() || pressing()"
              (click)="start()"
            >
              <app-icon name="play" />Start {{ runs() }} tasks
            </button>
            <span class="ag-segmented" role="radiogroup" aria-label="Replay pattern">
              @for (choice of view.patterns; track choice.name) {
                <button
                  type="button"
                  role="radio"
                  [class.on]="choice.name === pattern()"
                  [attr.aria-checked]="choice.name === pattern()"
                  (click)="chosenPattern.set(choice.name)"
                >
                  {{ choice.name }}
                </button>
              }
            </span>
          </div>
          <div class="ag-demo-row">
            <button
              type="button"
              class="ag-btn ag-btn--danger"
              [disabled]="!view.runner || !pod() || pressing()"
              (click)="kill()"
            >
              <app-icon name="power" />{{ killing() === null ? 'Kill pod' : 'Killing ' + killing() + '…' }}
            </button>
            <select class="ag-select" aria-label="Pod to kill" (change)="choosePod($event)">
              @for (offer of offered(); track offer.name) {
                <option [value]="offer.name" [selected]="offer.name === pod()">{{ offer.name }}</option>
              }
            </select>
          </div>
          <div class="ag-demo-cap">
            @if (refusal(); as refusal) {
              <p>{{ refusal }}</p>
            } @else if (view.runner; as runner) {
              @if (runner.replay; as replay) {
                <p>
                  Replay {{ replay.state }}{{ replay.state === 'failed' ? ' (exit ' + replay.exit_code + ')' : '' }}:
                  {{ replay.runs_finished }} of {{ replay.runs_total }} finished{{ lost(replay.calls_failed) }} ·
                  <code>{{ replay.command }}</code>
                </p>
              } @else if (!runner.last_kill) {
                <p>
                  Start replays recorded tasks through the gateway (<code>make replay</code>). Kill deletes the pod at
                  once; its runs continue on another one.
                </p>
              }
              @if (runner.last_kill; as last) {
                <p>{{ last.pod }} killed at {{ last.at.slice(11, 19) }} UTC.</p>
              }
            } @else {
              <p>{{ view.runner_error }}</p>
            }
          </div>
        </div>
      }
    }
  `,
  styles: `
    :host {
      display: contents;
    }
    .ag-demo-cap p {
      margin: 0;
    }
    /* The design's segmented control is spans; here its items are buttons, so they take the span's look. */
    .ag-segmented button {
      padding: 3px 9px;
      border: 0;
      border-radius: var(--radius-xs);
      background: transparent;
      color: var(--ink-muted);
      font: 500 12px/16px var(--font-sans);
      cursor: pointer;
    }
    .ag-segmented button.on {
      background: var(--surface);
      color: var(--ink);
      box-shadow: var(--shadow-raise);
    }
  `,
})
export class DemoControls {
  readonly pods = input.required<ClusterPod[]>();
  private readonly http = inject(HttpClient);
  protected readonly view = signal<DemoView | null>(null);
  protected readonly chosenPattern = signal<string | null>(null);
  private readonly chosenPod = signal<string | null>(null);
  protected readonly pressing = signal(false);
  // The pod a Kill press is deleting until the runner answers: the runner lists the pods and then deletes, both with
  // kubectl, ~1.5 s in which the button showed nothing (P4.6c).
  protected readonly killing = signal<string | null>(null);
  // A press the runner or the app backend refused, shown in place of the caption until the next read.
  protected readonly refusal = signal<string | null>(null);
  private requests = new Subscription();
  private timer: ReturnType<typeof setTimeout> | undefined;

  protected readonly pattern = computed(() => this.chosenPattern() ?? this.view()?.patterns[0]?.name ?? null);
  protected readonly runs = computed(
    () => this.view()?.patterns.find((choice) => choice.name === this.pattern())?.runs,
  );
  protected readonly running = computed(() => this.view()?.runner?.replay?.state === 'running');
  protected readonly offered = computed(() =>
    this.pods().filter((pod) => pod.state === 'ready' || pod.state === 'starting'),
  );
  // The chosen pod while it is on offer, else the first that is, so Kill never names a pod that left.
  protected readonly pod = computed(() => {
    const names = this.offered().map((pod) => pod.name);
    const chosen = this.chosenPod();
    return chosen !== null && names.includes(chosen) ? chosen : (names[0] ?? null);
  });

  constructor() {
    this.read();
    inject(DestroyRef).onDestroy(() => {
      this.requests.unsubscribe();
      clearTimeout(this.timer);
    });
  }

  /** A replay that wrote its file is done even with calls lost (a kill can cost one), so the loss is named
   * beside the runs rather than called a failure. */
  protected lost(calls: number): string {
    return calls === 0 ? '' : `, ${calls} call${calls === 1 ? '' : 's'} lost`;
  }

  protected choosePod(event: Event): void {
    this.chosenPod.set((event.target as HTMLSelectElement).value);
  }

  protected start(): void {
    this.press(DEMO_REPLAY_API, { pattern: this.pattern() });
  }

  protected kill(): void {
    this.killing.set(this.pod());
    this.press(DEMO_KILL_API, { pod: this.pod() });
  }

  private read(): void {
    this.requests.add(
      this.http.get<DemoView>(DEMO_API).subscribe({
        next: (view) => {
          this.view.set(view);
          this.refusal.set(null);
          if (view.enabled) {
            this.timer = setTimeout(() => this.read(), view.poll_seconds * 1000);
          }
        },
        // The panel around it says when the app backend is gone, and makes this anew when it is back.
        error: () => this.view.set(null),
      }),
    );
  }

  private press(url: string, body: Record<string, string | null>): void {
    this.pressing.set(true);
    this.requests.add(
      this.http.post<DemoRunner>(url, body).subscribe({
        next: (runner) => {
          this.pressing.set(false);
          this.killing.set(null);
          this.refusal.set(null);
          this.view.update((view) => view && { ...view, runner, runner_error: null });
        },
        error: (error: HttpErrorResponse) => {
          this.pressing.set(false);
          this.killing.set(null);
          const detail = (error.error as { detail?: unknown } | null)?.detail;
          this.refusal.set(typeof detail === 'string' ? detail : NO_ANSWER);
        },
      }),
    );
  }
}
