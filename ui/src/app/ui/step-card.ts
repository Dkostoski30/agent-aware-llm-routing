import { Component, input } from '@angular/core';
import { Served, StepState } from '../runs/run-view';

/** One LLM call of the run, as evidence: its number, the tool the model chose, the arguments and how it was served
 * (StepCard card). Tags in the Tag card's order: pod, cache, latency, then any event. */
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
      @if (served() || state() === 'running' || state() === 'retried') {
        <div class="ag-tags">
          @if (served(); as how) {
            @if (how.pod; as pod) {
              <span class="ag-tag" [title]="pod.full">{{ pod.short }}</span>
            }
            @if (how.cache; as cache) {
              <span
                class="ag-tag"
                [class]="cache.hit ? 'ag-tag--hit' : 'ag-tag--miss'"
                [title]="cache.detail"
                >{{ cache.text }}</span
              >
            }
            <span class="ag-tag">{{ how.latency }}</span>
          }
          @switch (state()) {
            @case ('running') {
              <span class="ag-tag ag-tag--muted">running</span>
            }
            @case ('retried') {
              <span class="ag-tag ag-tag--retry">error</span>
            }
          }
        </div>
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
  readonly served = input<Served>();
}
