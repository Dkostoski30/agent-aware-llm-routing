import { Component, computed, input, model, output } from '@angular/core';
import { Icon } from './icon';

/** Where the task is typed (Composer card). Enter sends, Shift+Enter adds a line. No scenario select: the
 * model picks the scenario (D13, D14). */
@Component({
  selector: 'app-composer',
  imports: [Icon],
  template: `
    <div class="ag-composer" [class.ag-composer--hero]="hero()">
      <textarea
        class="ag-input"
        rows="1"
        aria-label="Task"
        [placeholder]="placeholder()"
        [value]="value()"
        (input)="onInput($event)"
        (keydown.enter)="submit($event)"
      ></textarea>
      <div class="ag-composer-row">
        <span class="ag-spacer"></span>
        @if (hint()) {
          <span class="ag-composer-hint">{{ hint() }}</span>
        }
        <button
          type="button"
          class="ag-btn ag-btn--primary ag-btn--send"
          aria-label="Send"
          [disabled]="!canSend()"
          (click)="submit()"
        >
          <app-icon name="send" />
        </button>
      </div>
    </div>
  `,
  // bundle.css styles the preview's placeholder div (.ag-input.ph); a textarea's placeholder needs its own rule.
  styles: ':host { display: contents; } textarea::placeholder { color: var(--ink-muted); }',
})
export class Composer {
  readonly hero = input(false);
  readonly placeholder = input('Ask a follow-up');
  readonly hint = input<string>();
  readonly disabled = input(false);
  readonly value = model('');
  readonly send = output<string>();

  protected readonly canSend = computed(() => !this.disabled() && this.value().trim().length > 0);

  protected onInput(event: Event): void {
    this.value.set((event.target as HTMLTextAreaElement).value);
  }

  // (keydown.enter) fires only for Enter without modifiers, so Shift+Enter keeps its default: a new line.
  protected submit(event?: Event): void {
    event?.preventDefault();
    if (this.canSend()) this.send.emit(this.value().trim());
  }
}
