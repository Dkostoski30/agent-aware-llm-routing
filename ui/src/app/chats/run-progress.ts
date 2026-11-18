import { Component, computed, inject, input, output, signal } from '@angular/core';
import { takeUntilDestroyed, toObservable } from '@angular/core/rxjs-interop';
import { EMPTY, Observable, Subject, catchError, merge, of, switchMap, tap } from 'rxjs';
import { RunCall, RunCalls } from '../runs/run-calls';
import { RunEvent, RunEvents } from '../runs/run-events';
import {
  NO_RUN,
  answerUnbacked,
  applyEvent,
  computedBy,
  expectedSteps,
  formatMs,
  segments,
  stepCards,
  stepInFlight,
} from '../runs/run-view';
import { Answer } from '../ui/answer';
import { Icon } from '../ui/icon';
import { RunHeader } from '../ui/run-header';
import { StepCard } from '../ui/step-card';

/** An answer still running in a chat, as its events arrive (RunScreen card, moved from /runs/:id in P3.13b): the
 * header, the step cards filling the grid, then the answer. The chat reads its saved answer once this says the run
 * ended, and shows that instead. */
@Component({
  selector: 'app-run-progress',
  imports: [Answer, Icon, RunHeader, StepCard],
  template: `
    <div class="ag-scroll">
      <div class="ag-column">
        @if (view().run; as run) {
          <app-run-header
            [title]="run.task"
            [runId]="run.run_id"
            [state]="view().end?.status ?? 'running'"
            [step]="stepInFlight()"
            [expected]="expected()"
            [elapsed]="elapsed()"
            [segments]="segments()"
          />
          @if (cards().length > 0) {
            <div class="ag-section">
              <div class="ag-section-head">
                Steps<small>{{ caption() }}</small>
              </div>
              <div class="ag-steps">
                @for (card of cards(); track card.n) {
                  <app-step-card
                    [n]="card.n"
                    [tool]="card.tool"
                    [args]="card.args"
                    [state]="card.state"
                    [note]="card.note"
                    [served]="card.served"
                  />
                }
              </div>
              @if (callsLost()) {
                <div class="ag-step-note ag-calls-lost">
                  Pod, cache and duration could not be read.
                </div>
              }
            </div>
          }
          @if (view().end; as end) {
            @if (end.status === 'done') {
              @if (unbacked()) {
                <div class="ag-section">
                  <div class="ag-notice ag-notice--warn">
                    <app-icon name="warn" />
                    <div><b>No tool result backs this answer.</b></div>
                  </div>
                </div>
              }
              <app-answer [text]="end.answer">
                {{ view().steps.length }} {{ view().steps.length === 1 ? 'step' : 'steps' }} ·
                {{ elapsed() }}
                @if (computedBy().length > 0) {
                  · numbers computed by <code class="ag-mono">{{ computedBy().join(', ') }}</code
                  >, not by the model
                }
              </app-answer>
            } @else {
              <div class="ag-section">
                <div class="ag-notice ag-notice--fail">
                  <app-icon name="warn" />
                  <div><b>The run failed.</b> {{ end.error }}</div>
                </div>
              </div>
            }
          } @else if (!lost()) {
            <app-answer />
          }
        }
        @if (lost()) {
          <div class="ag-section">
            <div class="ag-notice ag-notice--fail">
              <app-icon name="warn" />
              <div>
                <b>The run events could not be read.</b> The stream for
                <code>{{ runId() }}</code> stopped: the agent runtime or the app backend did not
                answer. Reloading the page tries again.
              </div>
            </div>
          </div>
        }
      </div>
    </div>
  `,
  styles: `
    :host {
      display: contents;
    }
    .ag-calls-lost {
      margin-top: var(--space-2);
    }
  `,
})
export class RunProgress {
  readonly runId = input.required<string>();
  /** The end event came, or the events can no longer be read: either way the saved message now says more. */
  readonly runEnded = output();
  private readonly events = inject(RunEvents);
  private readonly runCalls = inject(RunCalls);
  protected readonly view = signal(NO_RUN);
  // The stream failed for good (unknown or expired run, agent unreachable).
  protected readonly lost = signal(false);
  private readonly calls = signal<RunCall[]>([]);
  // The last request for the calls failed; the cards keep the badges of the last answer.
  protected readonly callsLost = signal(false);

  protected readonly cards = computed(() => stepCards(this.view(), this.calls()));
  protected readonly segments = computed(() => segments(this.view()));
  protected readonly expected = computed(() => expectedSteps(this.view()));
  protected readonly stepInFlight = computed(() => stepInFlight(this.view()));
  protected readonly computedBy = computed(() => computedBy(this.view()));
  protected readonly unbacked = computed(() => answerUnbacked(this.view()));
  protected readonly elapsed = computed(() => formatMs(this.view().end?.elapsed_ms ?? 0));
  // Counts the step events, not end.steps, which also counts a call that failed without an answer (card 18b).
  protected readonly caption = computed(() =>
    this.view().end
      ? `${this.view().steps.length} taken`
      : `${this.view().steps.length} done · ${this.expected()} expected`,
  );

  // A new run id stops following the old run and starts from nothing (P2.6: a reused component re-watches its id).
  constructor() {
    toObservable(this.runId)
      .pipe(
        switchMap((runId) => {
          this.view.set(NO_RUN);
          this.lost.set(false);
          this.calls.set([]);
          this.callsLost.set(false);
          return this.follow(runId);
        }),
        takeUntilDestroyed(),
      )
      .subscribe();
  }

  /** The run's events, and its calls after each step. Every answer holds all the calls so far, so a newer step
   * replaces a request still waiting (switchMap), and the next answer makes up for a failed one. */
  private follow(runId: string): Observable<unknown> {
    const stepLanded = new Subject<void>();
    const calls = stepLanded.pipe(
      switchMap(() => this.runCalls.read(runId).pipe(catchError(() => of(undefined)))),
      tap((calls) => {
        this.callsLost.set(calls === undefined);
        if (calls !== undefined) this.calls.set(calls);
      }),
    );
    const events = this.events.watch(runId).pipe(
      tap((event: RunEvent) => {
        this.view.update((view) => applyEvent(view, event));
        if (event.kind === 'step') stepLanded.next();
        if (event.kind === 'end') this.runEnded.emit();
      }),
      catchError(() => {
        this.lost.set(true);
        this.runEnded.emit();
        return EMPTY;
      }),
    );
    // calls first, so it listens before the first step lands.
    return merge(calls, events);
  }
}
