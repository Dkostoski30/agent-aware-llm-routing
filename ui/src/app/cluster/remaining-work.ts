import { Component, input } from '@angular/core';

/** The cluster's remaining work as the design's RemainingWork (D14), kept to the number and its caption: no
 * sparkline and no scaling line (the user, 2026-11-04: the UI has too much going on). Each active run counts
 * max(expected − step, 1) (D2, P3.8). */
@Component({
  selector: 'app-remaining-work',
  template: `
    <section class="ag-psec">
      <h3 class="ag-psec-title">Remaining work</h3>
      <div class="ag-rw">
        <div class="ag-metric">{{ steps() }}<small>steps</small></div>
        <div class="ag-rw-cap">
          Σ max(<code>expected</code> − <code>step</code>, 1) over {{ runs() }} active runs.
        </div>
      </div>
    </section>
  `,
  styles: ':host { display: contents; }',
})
export class RemainingWork {
  readonly steps = input.required<number>();
  readonly runs = input.required<number>();
}
