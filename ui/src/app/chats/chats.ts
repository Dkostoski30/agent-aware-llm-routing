import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { RunFile } from '../runs/run-files';

/** A chat as the app backend's /chats endpoints answer it (app/app_backend/chats.py). */
export interface Chat {
  id: number;
  title: string;
  created: number;
}

/** An attachment without its content, which only the run needs. */
export interface FileInfo {
  id: string;
  name: string;
  type: string;
  size: number;
}

export interface Message {
  id: number;
  role: 'user' | 'assistant';
  // The user's task, or the run's answer or error; empty while the run is running.
  text: string;
  status: 'running' | 'done' | 'failed';
  run_id: string | null;
  created: number;
  files: FileInfo[];
}

export interface ChatWithMessages extends Chat {
  messages: Message[];
}

/** The app backend's 202 to a message: its run has started. */
export interface Sent {
  user_message_id: number;
  assistant_message_id: number;
  run_id: string;
}

/** The fields of the app backend's NewMessage the UI sends; a message without files sends none. */
interface NewMessage {
  task: string;
  files?: RunFile[];
}

const CHATS_API = '/api/chats';

/** The logged-in user's chats, from the app backend through the dev server's /api proxy (proxy.conf.json). The
 * list is kept here, so the chat list beside the chat shows a chat the empty chat has just created. */
@Injectable({ providedIn: 'root' })
export class Chats {
  private readonly http = inject(HttpClient);
  private readonly listed = signal<Chat[]>([]);
  private readonly lost = signal(false);
  /** Newest first, as the app backend orders them. */
  readonly list = this.listed.asReadonly();
  /** The last read of the list failed. */
  readonly listLost = this.lost.asReadonly();

  /** Reads the list again. It is emptied first, so after a logout and a login no other user's chats stay. */
  reload(): void {
    this.listed.set([]);
    this.http.get<Chat[]>(CHATS_API).subscribe({
      next: (chats) => {
        this.lost.set(false);
        this.listed.set(chats);
      },
      error: () => this.lost.set(true),
    });
  }

  create(title: string): Observable<Chat> {
    return this.http
      .post<Chat>(CHATS_API, { title })
      .pipe(tap((chat) => this.listed.update((chats) => [chat, ...chats])));
  }

  /** The chat and its messages. The app backend finishes messages whose run has ended before it answers. */
  read(chatId: string): Observable<ChatWithMessages> {
    return this.http.get<ChatWithMessages>(`${CHATS_API}/${encodeURIComponent(chatId)}`);
  }

  /** Starts the message's run. The app backend adds the chat's earlier files to it, so a file is sent once. */
  send(chatId: string, task: string, files: RunFile[]): Observable<Sent> {
    const body: NewMessage = files.length > 0 ? { task, files } : { task };
    return this.http.post<Sent>(`${CHATS_API}/${encodeURIComponent(chatId)}/messages`, body);
  }
}

/** The HTTP status and, when the app backend gave one, its reason ({detail} from FastAPI, as for a refused file). */
export function refusal(error: HttpErrorResponse): string {
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  return typeof detail === 'string' ? `HTTP ${error.status}, ${detail}` : `HTTP ${error.status}`;
}
