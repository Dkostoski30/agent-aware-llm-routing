import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, map, of, switchMap, tap } from 'rxjs';
import { Attachments } from '../chats/attachments';
import { Chats, refusal } from '../chats/chats';
import { Composer } from '../ui/composer';
import { Horse } from '../ui/horse';
import { ScenarioCard } from '../ui/scenario-card';
import { SCENARIOS } from './scenarios';

/** The empty chat at /chats (NewTaskScreen card): the horse at 4x, the composer and the three scenarios. A card
 * stays ringed while the composer holds its example unchanged. The first send creates the chat, titled with the
 * task, then sends the task to it and opens it (P3.13b). */
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
          [attached]="attachments.names()"
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
  private readonly chats = inject(Chats);
  private readonly router = inject(Router);
  protected readonly scenarios = SCENARIOS;
  protected readonly task = signal('');
  protected readonly starting = signal(false);
  protected readonly attachments = new Attachments();
  protected readonly busy = computed(() => this.starting() || this.attachments.reading());
  protected readonly hint = signal<string | undefined>(undefined);
  // The chat created for a message the app then refused: the next send goes to it, so a retry makes no second chat.
  private created: number | undefined;

  protected start(task: string): void {
    this.starting.set(true);
    this.hint.set(undefined);
    this.chat(task)
      .pipe(
        switchMap((chatId) =>
          this.chats.send(String(chatId), task, this.attachments.files()).pipe(map(() => chatId)),
        ),
      )
      .subscribe({
        next: (chatId) => void this.router.navigate(['/chats', chatId]),
        error: (error: HttpErrorResponse) => {
          this.starting.set(false);
          this.hint.set(`The run did not start: ${refusal(error)}.`);
        },
      });
  }

  protected async attach(picked: File[]): Promise<void> {
    this.hint.set(undefined);
    this.hint.set(await this.attachments.add(picked));
  }

  protected remove(position: number): void {
    this.hint.set(undefined);
    this.attachments.remove(position);
  }

  /** The chat the task goes to: the one created before, or a new one titled with the task. */
  private chat(task: string): Observable<number> {
    if (this.created !== undefined) return of(this.created);
    return this.chats.create(task).pipe(
      map((chat) => chat.id),
      tap((chatId) => (this.created = chatId)),
    );
  }
}
