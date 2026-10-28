import { Component, input, output } from '@angular/core';
import { Icon, IconName } from './icon';

export interface Scenario {
  icon: IconName;
  title: string;
  prompt: string;
  source: string;
  tools: string[];
}

/** A demo scenario as a starter (ScenarioCard card). Picking it only fills the composer with its example;
 * the model picks the scenario (D14). A div with role=button keeps bundle.css's h3 and p, which a <button>
 * may not contain. */
@Component({
  selector: 'app-scenario-card',
  imports: [Icon],
  template: `
    <div
      class="ag-scenario"
      [class.ag-scenario--on]="on()"
      role="button"
      tabindex="0"
      [attr.aria-pressed]="on()"
      (click)="pick.emit()"
      (keydown.enter)="pick.emit()"
      (keydown.space)="$event.preventDefault(); pick.emit()"
    >
      <h3><app-icon [name]="scenario().icon" />{{ scenario().title }}</h3>
      <p>{{ scenario().prompt }}</p>
      <span class="ag-src">{{ scenario().source }}</span>
      <div class="ag-tools">
        @for (tool of scenario().tools; track tool) {
          <span class="ag-tag">{{ tool }}</span>
        }
      </div>
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class ScenarioCard {
  readonly scenario = input.required<Scenario>();
  readonly on = input(false);
  readonly pick = output();
}
