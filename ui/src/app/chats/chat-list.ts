import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { Chats } from './chats';

/** The user's chats, newest first, in a column left of the chat (P3.13b). The design has no card for it, so it is
 * built from its tokens (D14): the panel's surface and hairline, mirrored to the left, and the accent-soft mark the
 * active-runs list gives the user's own run. A long title is cut short; the whole one shows on hover. */
@Component({
  selector: 'app-chat-list',
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav aria-label="Chats">
      @if (chats.listLost()) {
        <p>The chats could not be read.</p>
      } @else {
        @for (chat of chats.list(); track chat.id) {
          <a
            [routerLink]="['/chats', chat.id]"
            routerLinkActive="on"
            ariaCurrentWhenActive="page"
            [title]="chat.title"
            >{{ chat.title }}</a
          >
        } @empty {
          <p>No chats yet.</p>
        }
      }
    </nav>
  `,
  styles: `
    :host {
      /* No token sets this width: wide enough for a short question's first words, narrow enough that the chat
         and the cluster panel keep their room on a laptop. */
      --chat-list-width: 240px;
      flex: none;
      width: var(--chat-list-width);
      overflow: auto;
      border-right: 1px solid var(--line);
      background: var(--surface);
    }
    nav {
      display: grid;
      gap: 2px;
      padding: var(--space-3) var(--space-2);
    }
    a {
      display: block;
      padding: 7px var(--space-2);
      border-radius: var(--radius-sm);
      color: var(--ink);
      font: 400 13px/18px var(--font-sans);
      text-decoration: none;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    a:hover {
      background: var(--surface-2);
    }
    a.on {
      background: var(--accent-soft);
      color: var(--accent-ink);
    }
    p {
      margin: 0;
      padding: 7px var(--space-2);
      color: var(--ink-muted);
      font: 400 12px/16px var(--font-sans);
    }
  `,
})
export class ChatList {
  protected readonly chats = inject(Chats);
}
