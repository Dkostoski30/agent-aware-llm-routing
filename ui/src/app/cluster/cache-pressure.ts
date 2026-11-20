import { Component, computed, input } from '@angular/core';

/** The cluster's cache pressure (P4.11) in RemainingWork's style: one number and its caption, no dial and no chart
 * (the user, 2026-11-04: the UI has too much going on). From the profile's warning value up it turns to the design
 * system's warning colour, draining's amber, with the word "high", since a state is never only a colour (D14). */
@Component({
  selector: 'app-cache-pressure',
  template: `
    <section class="ag-psec">
      <h3 class="ag-psec-title">Cache pressure</h3>
      <div class="ag-rw">
        <div class="ag-metric" [class.high]="high()">{{ shown() }}@if (high()) {<small>high</small>}</div>
        <div class="ag-rw-cap">
          Context of the active runs over the KV cache of the ready pods; high from {{ warn().toFixed(2) }}.
        </div>
      </div>
    </section>
  `,
  styles: `
    :host {
      display: contents;
    }
    .high,
    .high small {
      color: var(--draining-ink);
    }
  `,
})
export class CachePressure {
  readonly pressure = input.required<number | null>();
  readonly warn = input.required<number>();
  protected readonly shown = computed(() => this.pressure()?.toFixed(2) ?? '–');
  protected readonly high = computed(() => {
    const pressure = this.pressure();
    return pressure !== null && pressure >= this.warn();
  });
}
