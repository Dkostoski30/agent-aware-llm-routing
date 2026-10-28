import { Component, computed, input } from '@angular/core';

// The 16px line icons from bundle.js that these screens use; later screens add theirs.
const ICONS = {
  send: 'M8 13V3M4 7l4-4 4 4',
  plus: 'M8 3v10M3 8h10',
  moon: 'M13 9.6A5.5 5.5 0 1 1 6.4 3a4.4 4.4 0 0 0 6.6 6.6z',
  table:
    'M3.5 3h9a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM2.5 6.5h11M6.5 6.5V13',
  doc: 'M4 2.5h5l3 3v8H4zM9 2.5v3h3M6 9h4M6 11h3',
  chat: 'M4.5 2.5h7a2 2 0 0 1 2 2v4.5a2 2 0 0 1-2 2H7.5L4.5 13.5V11h0a2 2 0 0 1-2-2V4.5a2 2 0 0 1 2-2z',
};

export type IconName = keyof typeof ICONS;

/** A decorative icon; the word beside it, or the button's aria-label, carries the meaning. */
@Component({
  selector: 'app-icon',
  template: `<svg class="ag-icon" viewBox="0 0 16 16" aria-hidden="true">
    <path [attr.d]="path()" />
  </svg>`,
  styles: ':host { display: contents; }',
})
export class Icon {
  readonly name = input.required<IconName>();
  protected readonly path = computed(() => ICONS[this.name()]);
}
