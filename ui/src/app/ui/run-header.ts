import { Component, input } from '@angular/core';
import { Horse } from './horse';
import { Status } from './status';

/** The task as a heading, then where the run is (RunHeader card). Running reads "Step 3 · 5 expected": expected
 * is x-expected-steps, an estimate, so the wording never promises it. The task type comes with the scenario step
 * (P4.8), the Answer/Steps tabs with the step table (P2.6). */
@Component({
  selector: 'app-run-header',
  imports: [Horse, Status],
  template: `
    <div class="ag-run-head">
      <h1>{{ title() }}</h1>
      <div class="ag-run-meta">
        <span class="ag-now">
          @switch (state()) {
            @case ('running') {
              <app-horse state="working" />Step {{ step() }} · {{ expected() }} expected
            }
            @case ('done') {
              <app-horse />Finished in {{ elapsed() }}
            }
            @case ('failed') {
              <app-horse /><app-status state="failed" label="Failed" /> after {{ elapsed() }}
            }
          }
        </span>
        <code>{{ runId() }}</code>
      </div>
      <div class="ag-progress" aria-hidden="true">
        @for (segment of segments(); track $index) {
          <i [class]="segment"></i>
        }
      </div>
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class RunHeader {
  readonly title = input.required<string>();
  readonly runId = input.required<string>();
  readonly state = input.required<'running' | 'done' | 'failed'>();
  // The x-step of the call in flight and x-expected-steps; shown while running.
  readonly step = input(0);
  readonly expected = input(0);
  // Shown once the run is over.
  readonly elapsed = input('');
  readonly segments = input.required<string[]>();
}
