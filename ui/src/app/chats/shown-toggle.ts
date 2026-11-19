import { signal } from '@angular/core';

/** Whether a column beside the chat is shown: the chat list (P3.13b) or the cluster panel (P3.13c). Shown until this
 * browser remembers otherwise, under its own key, as it does the theme. */
export class ShownToggle {
  readonly shown;

  constructor(private readonly key: string) {
    this.shown = signal(localStorage.getItem(key) !== 'hidden');
  }

  toggle(): void {
    this.shown.set(!this.shown());
    localStorage.setItem(this.key, this.shown() ? 'shown' : 'hidden');
  }
}
