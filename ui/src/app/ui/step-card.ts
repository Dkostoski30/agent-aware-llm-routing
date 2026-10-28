import { Component, input } from '@angular/core';
import { StepState } from '../runs/run-view';

/** One LLM call of the run, as evidence: its number, the tool the model chose and the arguments (StepCard card).
 * The pod, cache and latency tags come with the call recorder (P2.6). */
@Component({
  selector: 'app-step-card',
  template: `
    <div class="ag-step" [class]="'ag-step--' + state()">
      <div class="ag-step-top">
        <span class="ag-step-n">{{ n() }}</span>
      </div>
      <div class="ag-step-tool">{{ tool() }}</div>
      @if (args()) {
        <div class="ag-step-args">{{ args() }}</div>
      }
      @if (note()) {
        <div class="ag-step-note">{{ note() }}</div>
      }
      @switch (state()) {
        @case ('running') {
          <div class="ag-tags"><span class="ag-tag ag-tag--muted">running</span></div>
        }
        @case ('retried') {
          <div class="ag-tags"><span class="ag-tag ag-tag--retry">error</span></div>
        }
      }
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class StepCard {
  readonly n = input.required<number>();
  readonly tool = input.required<string>();
  readonly args = input('');
  readonly state = input.required<StepState>();
  readonly note = input<string>();
}
