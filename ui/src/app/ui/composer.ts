import { Component, computed, input, model, output } from '@angular/core';
import { ACCEPTED_FILES } from '../runs/run-files';
import { Icon } from './icon';

/** Where the task is typed (Composer card). Enter sends, Shift+Enter adds a line. No scenario select: the
 * model picks the scenario (D13, D14). The design has no card for attached files, so they are built from its
 * button and tag (D14); whoever holds the composer reads and keeps them. */
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
      @if (attached().length > 0) {
        <div class="ag-tags attached">
          @for (name of attached(); track $index) {
            <span class="ag-tag">
              {{ name }}
              <button
                type="button"
                class="remove"
                [attr.aria-label]="'Remove ' + name"
                [disabled]="disabled()"
                (click)="removed.emit($index)"
              >
                <app-icon name="close" />
              </button>
            </span>
          }
        </div>
      }
      <div class="ag-composer-row">
        <input
          #picker
          type="file"
          hidden
          multiple
          [accept]="acceptedFiles"
          (change)="onPick($event)"
        />
        <button
          type="button"
          class="ag-btn ag-btn--ghost ag-btn--sm"
          aria-label="Attach files"
          [disabled]="disabled()"
          (click)="picker.click()"
        >
          <app-icon name="plus" />Attach
        </button>
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
  styles: `
    :host {
      display: contents;
    }
    textarea::placeholder {
      color: var(--ink-muted);
    }
    .attached {
      margin-top: var(--space-2);
    }
    .remove {
      display: inline-flex;
      padding: 0;
      border: 0;
      background: transparent;
      color: var(--ink-muted);
      cursor: pointer;
    }
  `,
})
export class Composer {
  readonly hero = input(false);
  readonly placeholder = input('Ask a follow-up');
  readonly hint = input<string>();
  readonly disabled = input(false);
  readonly value = model('');
  /** The names of the attached files, in order. */
  readonly attached = input<string[]>([]);
  readonly send = output<string>();
  readonly picked = output<File[]>();
  /** The position in attached of the file to take off. */
  readonly removed = output<number>();

  protected readonly acceptedFiles = ACCEPTED_FILES;
  protected readonly canSend = computed(() => !this.disabled() && this.value().trim().length > 0);

  protected onInput(event: Event): void {
    this.value.set((event.target as HTMLTextAreaElement).value);
  }

  protected onPick(event: Event): void {
    const picker = event.target as HTMLInputElement;
    this.picked.emit(Array.from(picker.files ?? []));
    // A browser fires no change for the file already in the input, so a file removed could not be picked again.
    picker.value = '';
  }

  // (keydown.enter) fires only for Enter without modifiers, so Shift+Enter keeps its default: a new line.
  protected submit(event?: Event): void {
    event?.preventDefault();
    if (this.canSend()) this.send.emit(this.value().trim());
  }
}
