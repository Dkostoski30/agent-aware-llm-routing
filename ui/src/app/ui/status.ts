import { Component, computed, input } from '@angular/core';

export type StatusState = 'ready' | 'starting' | 'draining' | 'failed';

const LABELS: Record<StatusState, string> = {
  ready: 'Ready',
  starting: 'Starting',
  draining: 'Draining',
  failed: 'Killed',
};

/** A state as a chip: a dot and a word on a tinted ground (Status card). The word is always there; colour alone
 * never carries the state. The pod states name the words; label replaces the word, as for a failed run. */
@Component({
  selector: 'app-status',
  template: `<span class="ag-status" [class]="'ag-status--' + state()"
    ><i class="ag-dot" [class]="'ag-dot--' + state()"></i>{{ word() }}</span
  >`,
  styles: ':host { display: contents; }',
})
export class Status {
  readonly state = input.required<StatusState>();
  readonly label = input<string>();
  protected readonly word = computed(() => this.label() ?? LABELS[this.state()]);
}
