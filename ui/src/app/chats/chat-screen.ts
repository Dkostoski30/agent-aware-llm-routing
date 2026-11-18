import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, inject, input, signal } from '@angular/core';
import { takeUntilDestroyed, toObservable } from '@angular/core/rxjs-interop';
import { Subject, catchError, of, startWith, switchMap, tap } from 'rxjs';
import { Answer } from '../ui/answer';
import { Composer } from '../ui/composer';
import { Icon } from '../ui/icon';
import { Attachments } from './attachments';
import { ChatWithMessages, Chats, refusal } from './chats';
import { RunProgress } from './run-progress';

/** An open chat (P3.13b): its saved messages, the composer docked below. An answer whose run is still running
 * follows the run's events through the app backend; when the run ends the chat is read again, so the saved answer
 * is what stays on screen, after a reload too. */
@Component({
  selector: 'app-chat-screen',
  imports: [Answer, Composer, Icon, RunProgress],
  template: `
    <div class="ag-scroll">
      <div class="ag-column">
        @for (message of chat()?.messages ?? []; track message.id) {
          @if (message.role === 'user') {
            <div class="ag-section">
              <p class="said">{{ message.text }}</p>
              @if (message.files.length > 0) {
                <div class="ag-tags">
                  @for (file of message.files; track file.id) {
                    <span class="ag-tag">{{ file.name }}</span>
                  }
                </div>
              }
            </div>
          } @else if (message.status === 'running' && message.run_id) {
            <app-run-progress [runId]="message.run_id" (runEnded)="readAgain()" />
          } @else if (message.status === 'done') {
            <app-answer [text]="message.text" />
          } @else {
            <div class="ag-section">
              <div class="ag-notice ag-notice--fail">
                <app-icon name="warn" />
                <div><b>The run failed.</b> {{ message.text }}</div>
              </div>
            </div>
          }
        }
        @if (lost()) {
          <div class="ag-section">
            <div class="ag-notice ag-notice--fail">
              <app-icon name="warn" />
              <div>
                <b>The chat could not be read.</b> It does not exist, or the app backend did not
                answer.
              </div>
            </div>
          </div>
        }
      </div>
    </div>
    <div class="ag-dock">
      <app-composer
        [(value)]="task"
        [disabled]="busy()"
        [hint]="hint()"
        [attached]="attachments.names()"
        (send)="send($event)"
        (picked)="attach($event)"
        (removed)="remove($event)"
      />
    </div>
  `,
  styles: `
    :host {
      display: contents;
    }
    .said {
      margin: 0;
      font: 500 15px/24px var(--font-sans);
      white-space: pre-line;
    }
    .ag-tags {
      margin-top: var(--space-2);
    }
  `,
})
export class ChatScreen {
  // Bound from the route's :id (withComponentInputBinding in app.config.ts).
  readonly id = input.required<string>();
  private readonly chats = inject(Chats);
  private readonly readAgainNow = new Subject<void>();
  protected readonly chat = signal<ChatWithMessages | undefined>(undefined);
  // The last read of the chat failed: an unknown or another user's chat (404), or no app backend.
  protected readonly lost = signal(false);
  protected readonly task = signal('');
  protected readonly hint = signal<string | undefined>(undefined);
  protected readonly attachments = new Attachments();
  private readonly sending = signal(false);
  // One run at a time per chat, so the next question's files and the answer it builds on are settled.
  private readonly lastRunning = computed(() => this.chat()?.messages.at(-1)?.status === 'running');
  protected readonly busy = computed(
    () =>
      this.chat() === undefined ||
      this.sending() ||
      this.attachments.reading() ||
      this.lastRunning(),
  );

  // A new :id reads that chat and drops the draft, since the route reuses this component (P2.6). Each later read of
  // the same chat replaces one still waiting.
  constructor() {
    toObservable(this.id)
      .pipe(
        tap(() => {
          this.chat.set(undefined);
          this.lost.set(false);
          this.task.set('');
          this.hint.set(undefined);
          this.attachments.clear();
        }),
        switchMap((id) =>
          this.readAgainNow.pipe(
            startWith(undefined),
            switchMap(() => this.chats.read(id).pipe(catchError(() => of(undefined)))),
          ),
        ),
        takeUntilDestroyed(),
      )
      .subscribe((chat) => {
        this.lost.set(chat === undefined);
        if (chat !== undefined) this.chat.set(chat);
      });
  }

  protected readAgain(): void {
    this.readAgainNow.next();
  }

  /** The chat is read again after a refusal too: the app saves a message whose run could not start (its 502). */
  protected send(task: string): void {
    this.sending.set(true);
    this.hint.set(undefined);
    this.chats.send(this.id(), task, this.attachments.files()).subscribe({
      next: () => {
        this.task.set('');
        this.attachments.clear();
        this.sent();
      },
      error: (error: HttpErrorResponse) => {
        this.hint.set(`The run did not start: ${refusal(error)}.`);
        this.sent();
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

  private sent(): void {
    this.sending.set(false);
    this.readAgain();
  }
}
