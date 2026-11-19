import { Component, input } from '@angular/core';
import { Status } from '../ui/status';
import { ClusterPod } from './cluster';

/** One pod as the design's PodCard (D14), kept to its name, state and running · waiting (the user, 2026-11-04: the
 * UI has too much going on), so no KV, hit-rate or note cells. A count not scraped yet shows as a dash, not 0. */
@Component({
  selector: 'app-pod-card',
  imports: [Status],
  template: `
    <div class="ag-pod" [class]="'ag-pod--' + pod().state">
      <div class="ag-pod-head">
        <span class="ag-pod-name">{{ pod().name }}</span>
        <app-status [state]="pod().state" />
      </div>
      <div class="ag-pod-grid">
        <div>
          <b>{{ pod().running ?? '–' }} · {{ pod().queue ?? '–' }}</b><span>Running · waiting</span>
        </div>
      </div>
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class PodCard {
  readonly pod = input.required<ClusterPod>();
}
