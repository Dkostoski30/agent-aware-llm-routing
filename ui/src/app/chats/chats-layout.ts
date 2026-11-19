import { Component, OnInit, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { ClusterPanel } from '../cluster/cluster-panel';
import { ClusterToggle } from '../cluster/cluster-toggle';
import { ChatList } from './chat-list';
import { ChatListToggle } from './chat-list-toggle';
import { Chats } from './chats';

/** The chat screens' columns: the chat list (P3.13b), the empty chat (/chats) or an open one (/chats/:id), then the
 * cluster panel (P3.11, here since P3.13c), list and panel each behind its toggle (D14). The list is read once here,
 * not per chat, since switching chats keeps this layout; showing or hiding a column leaves the chat as it is. */
@Component({
  selector: 'app-chats-layout',
  imports: [ChatList, ClusterPanel, RouterOutlet],
  template: `
    @if (toggle.shown()) {
      <app-chat-list />
    }
    <div class="chat"><router-outlet /></div>
    @if (panel.shown()) {
      <aside class="ag-panel" aria-label="Cluster"><app-cluster-panel /></aside>
    }
  `,
  styles: `
    :host {
      display: flex;
      flex: 1;
      min-height: 0;
    }
    .chat {
      display: flex;
      flex-direction: column;
      flex: 1;
      min-width: 0;
      min-height: 0;
    }
    .ag-panel {
      flex: none;
      width: var(--panel-width);
    }
  `,
})
export class ChatsLayout implements OnInit {
  protected readonly toggle = inject(ChatListToggle);
  protected readonly panel = inject(ClusterToggle);
  private readonly chats = inject(Chats);

  ngOnInit(): void {
    this.chats.reload();
  }
}
