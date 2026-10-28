import { Component, inject, input } from '@angular/core';
import { Router } from '@angular/router';
import { Theme } from '../theme/theme';
import { Horse } from './horse';
import { Icon } from './icon';

/** Across the top of every screen (TopBar card). The chip names the profile and model, so a screenshot says
 * which setup produced it; it is left out when the UI was started without a profile. */
@Component({
  selector: 'app-top-bar',
  imports: [Horse, Icon],
  template: `
    <header class="ag-topbar">
      <span class="ag-brand"><app-horse [state]="working() ? 'working' : 'idle'" />Tenax</span>
      <button type="button" class="ag-btn ag-btn--ghost ag-btn--sm" (click)="newTask()">
        <app-icon name="plus" />New task
      </button>
      <span class="ag-spacer"></span>
      @if (profile()) {
        <span class="ag-profile"
          >Profile <b>{{ profile() }}</b> · <code>{{ model() }}</code></span
        >
      }
      <button
        type="button"
        class="ag-btn ag-btn--ghost ag-btn--icon"
        aria-label="Switch theme"
        (click)="theme.toggle()"
      >
        <app-icon name="moon" />
      </button>
    </header>
  `,
  styles: ':host { display: contents; }',
})
export class TopBar {
  readonly profile = input<string>();
  readonly model = input<string>();
  readonly working = input(false);
  protected readonly theme = inject(Theme);
  private readonly router = inject(Router);

  protected newTask(): void {
    void this.router.navigate(['/']);
  }
}
