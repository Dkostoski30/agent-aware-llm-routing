import { Component, input } from '@angular/core';
import { StepCardData } from '../runs/run-view';
import { StepCard } from '../ui/step-card';

/** A run's step cards under their section head, the same while the run goes (RunProgress) and once it is saved
 * (SavedAnswer). When the call records could not be read, the cards have no badges and one line says so. */
@Component({
  selector: 'app-step-grid',
  imports: [StepCard],
  template: `
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
        <div class="ag-step-note ag-calls-lost">Pod, cache and duration could not be read.</div>
      }
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
export class StepGrid {
  readonly cards = input.required<StepCardData[]>();
  readonly caption = input.required<string>();
  readonly callsLost = input(false);
}
