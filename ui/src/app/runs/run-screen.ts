import { Component, input } from '@angular/core';

/** The run route; it names the run until P1.4b fills it with the run's steps and answer. */
@Component({
  selector: 'app-run-screen',
  template: `
    <div class="ag-scroll">
      <div class="ag-column">
        <code class="ag-mono">{{ id() }}</code>
      </div>
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class RunScreen {
  // Bound from the route's :id (withComponentInputBinding in app.config.ts).
  readonly id = input.required<string>();
}
