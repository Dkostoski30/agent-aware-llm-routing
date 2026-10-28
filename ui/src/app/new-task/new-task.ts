import { HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Runs } from '../runs/runs';
import { Composer } from '../ui/composer';
import { Horse } from '../ui/horse';
import { ScenarioCard } from '../ui/scenario-card';
import { SCENARIOS } from './scenarios';

/** The New task screen (NewTaskScreen card): the horse at 4x, the composer and the three scenarios. A card
 * stays ringed while the composer holds its example unchanged. */
@Component({
  selector: 'app-new-task',
  imports: [Composer, Horse, ScenarioCard],
  template: `
    <div class="ag-scroll">
      <div class="ag-column ag-hero">
        <app-horse size="4x" />
        <h1>What should the agent work on?</h1>
        <p>
          Describe a task, or start from an example below. Each scenario runs on fixed data with
          deterministic tools; the model picks the scenario and the tool to call next.
        </p>
        <app-composer
          [hero]="true"
          placeholder="Describe the task, or start from a scenario below"
          [(value)]="task"
          [disabled]="starting()"
          [hint]="hint()"
          (send)="start($event)"
        />
        <div class="ag-scenarios">
          @for (scenario of scenarios; track scenario.title) {
            <app-scenario-card
              [scenario]="scenario"
              [on]="task() === scenario.prompt"
              (pick)="task.set(scenario.prompt)"
            />
          }
        </div>
      </div>
    </div>
  `,
  styles: ':host { display: contents; }',
})
export class NewTask {
  private readonly runs = inject(Runs);
  private readonly router = inject(Router);
  protected readonly scenarios = SCENARIOS;
  protected readonly task = signal('');
  protected readonly starting = signal(false);
  protected readonly hint = signal<string | undefined>(undefined);

  protected start(task: string): void {
    this.starting.set(true);
    this.hint.set(undefined);
    this.runs.start(task).subscribe({
      next: (runId) => void this.router.navigate(['/runs', runId]),
      error: (error: HttpErrorResponse) => {
        this.starting.set(false);
        this.hint.set(`The run did not start: HTTP ${error.status}.`);
      },
    });
  }
}
