import { Component, DestroyRef, OnInit, computed, inject, input, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Answer } from '../ui/answer';
import { Icon } from '../ui/icon';
import { RunHeader } from '../ui/run-header';
import { StepCard } from '../ui/step-card';
import { RunEvents } from './run-events';
import {
  NO_RUN,
  applyEvent,
  computedBy,
  expectedSteps,
  formatMs,
  segments,
  stepCards,
  stepInFlight,
} from './run-view';

/** A run as its events arrive (RunScreen card): the header, the step cards filling the grid, then the answer. */
@Component({
  selector: 'app-run-screen',
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
                  />
                }
              </div>
            </div>
          }
          @if (view().end; as end) {
            @if (end.status === 'done') {
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
                <code>{{ id() }}</code> stopped: the run is unknown or expired, or the agent did not
                answer. Reloading the page tries again.
              </div>
            </div>
          </div>
        }
      </div>
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class RunScreen implements OnInit {
  // Bound from the route's :id (withComponentInputBinding in app.config.ts).
  readonly id = input.required<string>();
  private readonly events = inject(RunEvents);
  private readonly destroyRef = inject(DestroyRef);
  protected readonly view = signal(NO_RUN);
  // The stream failed for good (unknown or expired run, agent unreachable).
  protected readonly lost = signal(false);

  protected readonly cards = computed(() => stepCards(this.view()));
  protected readonly segments = computed(() => segments(this.view()));
  protected readonly expected = computed(() => expectedSteps(this.view()));
  protected readonly stepInFlight = computed(() => stepInFlight(this.view()));
  protected readonly computedBy = computed(() => computedBy(this.view()));
  protected readonly elapsed = computed(() => formatMs(this.view().end?.elapsed_ms ?? 0));
  // Counts the step events, not end.steps, which also counts a call that failed without an answer (card 18b).
  protected readonly caption = computed(() =>
    this.view().end
      ? `${this.view().steps.length} taken`
      : `${this.view().steps.length} done · ${this.expected()} expected`,
  );

  // The id input is set before ngOnInit, not in the constructor.
  ngOnInit(): void {
    this.events
      .watch(this.id())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (event) => this.view.update((view) => applyEvent(view, event)),
        error: () => this.lost.set(true),
      });
  }
}
