import { NgTemplateOutlet } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Component, DestroyRef, OnInit, computed, inject, input, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, forkJoin, of } from 'rxjs';
import { RunCall, RunCalls } from '../runs/run-calls';
import { answerUnbacked, savedStepCards } from '../runs/run-view';
import { Answer } from '../ui/answer';
import { Icon } from '../ui/icon';
import { Message } from './chats';
import { SavedRun, SavedRuns } from './saved-runs';
import { StepGrid } from './step-grid';

/** What the last read of the steps gave: the run and its calls (undefined when only the calls failed), a run the
 * runtime no longer has, or no answer. */
type Steps =
  | { kind: 'read'; run: SavedRun; calls: RunCall[] | undefined }
  | { kind: 'expired' }
  | { kind: 'lost' };

/** A saved answer, or a failed run's sentence, with its steps folded away under it (P3.13c). The steps are read on
 * the first expand only, through the app backend; the chat opens the answer whose run just ended, so its steps stay
 * on screen. P2.6c's warning needs the steps, so it shows with them. */
@Component({
  selector: 'app-saved-answer',
  imports: [Answer, Icon, NgTemplateOutlet, StepGrid],
  template: `
    @if (message().status === 'done') {
      <app-answer [text]="message().text"><ng-container [ngTemplateOutlet]="flipper" /></app-answer>
    } @else {
      <div class="ag-section">
        <div class="ag-notice ag-notice--fail">
          <app-icon name="warn" />
          <div><b>The run failed.</b> {{ message().text }}</div>
        </div>
        <div class="failed-foot"><ng-container [ngTemplateOutlet]="flipper" /></div>
      </div>
    }
    @if (shown()) {
      @if (read(); as read) {
        @if (unbacked()) {
          <div class="ag-section">
            <div class="ag-notice ag-notice--warn">
              <app-icon name="warn" />
              <div><b>No tool result backs this answer.</b></div>
            </div>
          </div>
        }
        <app-step-grid
          [cards]="cards()"
          [caption]="caption()"
          [callsLost]="read.calls === undefined"
        />
      } @else if (steps()?.kind === 'expired') {
        <p class="steps-line">The steps of this answer are no longer kept.</p>
      } @else if (steps()?.kind === 'lost') {
        <p class="steps-line">
          The steps could not be read: the app backend or the agent runtime did not answer.
        </p>
      }
    }
    <ng-template #flipper>
      @if (message().run_id) {
        <button
          type="button"
          class="ag-btn ag-btn--ghost ag-btn--sm"
          [attr.aria-expanded]="shown()"
          (click)="flip()"
        >
          {{ shown() ? 'Hide steps' : 'Show steps' }}
        </button>
      }
    </ng-template>
  `,
  styles: `
    :host {
      display: contents;
    }
    .failed-foot {
      margin-top: var(--space-2);
    }
    .steps-line {
      margin: 0 0 var(--space-6);
      color: var(--ink-muted);
      font: 400 12px/16px var(--font-sans);
    }
  `,
})
export class SavedAnswer implements OnInit {
  /** An assistant message that is done or failed. */
  readonly message = input.required<Message>();
  /** Start with the steps shown, and read them now. */
  readonly open = input(false);
  private readonly savedRuns = inject(SavedRuns);
  private readonly runCalls = inject(RunCalls);
  private readonly destroyRef = inject(DestroyRef);
  protected readonly shown = signal(false);
  protected readonly steps = signal<Steps | undefined>(undefined);
  // A read is under way or has settled what there is to show; a read nobody answered is tried again on the next
  // expand, a run the runtime no longer has is not.
  private asked = false;

  protected readonly read = computed(() => {
    const steps = this.steps();
    return steps?.kind === 'read' ? steps : undefined;
  });
  protected readonly cards = computed(() => {
    const read = this.read();
    return read ? savedStepCards(read.run.steps, read.calls) : [];
  });
  protected readonly caption = computed(() => `${this.cards().length} taken`);
  protected readonly unbacked = computed(() => {
    const read = this.read();
    return this.message().status === 'done' && read !== undefined && answerUnbacked(read.run);
  });

  ngOnInit(): void {
    if (this.open()) this.flip();
  }

  protected flip(): void {
    this.shown.set(!this.shown());
    const runId = this.message().run_id;
    if (!this.shown() || this.asked || runId === null) return;
    this.asked = true;
    forkJoin({
      run: this.savedRuns.read(runId),
      calls: this.runCalls.read(runId).pipe(catchError(() => of(undefined))),
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: ({ run, calls }) => this.steps.set({ kind: 'read', run, calls }),
        error: (error: HttpErrorResponse) => {
          const expired = error.status === 404;
          this.asked = expired;
          this.steps.set({ kind: expired ? 'expired' : 'lost' });
        },
      });
  }
}
