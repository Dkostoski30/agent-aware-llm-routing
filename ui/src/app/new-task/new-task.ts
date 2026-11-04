import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { RunFile, readRunFile } from '../runs/run-files';
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
          [disabled]="busy()"
          [hint]="hint()"
          [attached]="attachedNames()"
          (send)="start($event)"
          (picked)="attach($event)"
          (removed)="remove($event)"
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
  // A pick is still being read: a send now would leave its files out of the request. Attach is disabled meanwhile, so
  // picks never overlap.
  private readonly reading = signal(false);
  protected readonly busy = computed(() => this.starting() || this.reading());
  protected readonly hint = signal<string | undefined>(undefined);
  private readonly files = signal<RunFile[]>([]);
  protected readonly attachedNames = computed(() => this.files().map((file) => file.name));

  protected start(task: string): void {
    this.starting.set(true);
    this.hint.set(undefined);
    this.runs.start(task, this.files()).subscribe({
      next: (runId) => void this.router.navigate(['/runs', runId]),
      error: (error: HttpErrorResponse) => {
        this.starting.set(false);
        this.hint.set(`The run did not start: ${refusal(error)}.`);
      },
    });
  }

  protected async attach(picked: File[]): Promise<void> {
    this.hint.set(undefined);
    this.reading.set(true);
    try {
      const read = await Promise.all(picked.map(readRunFile));
      const files: RunFile[] = [];
      const refused: string[] = [];
      for (const result of read) {
        if ('refused' in result) refused.push(result.refused);
        else files.push(result);
      }
      this.files.update((attached) => [...attached, ...files]);
      if (refused.length > 0) this.hint.set(refused.join('; '));
    } finally {
      this.reading.set(false);
    }
  }

  protected remove(position: number): void {
    this.hint.set(undefined);
    this.files.update((files) => files.filter((_, index) => index !== position));
  }
}

/** The HTTP status and, when the agent gave one, its reason ({detail} from FastAPI, as for a file it refuses). */
function refusal(error: HttpErrorResponse): string {
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  return typeof detail === 'string' ? `HTTP ${error.status}, ${detail}` : `HTTP ${error.status}`;
}
