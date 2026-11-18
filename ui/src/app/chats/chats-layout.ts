import { Component, OnInit, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { ChatList } from './chat-list';
import { ChatListToggle } from './chat-list-toggle';
import { Chats } from './chats';

/** The conversation side of the split view (P3.13b): the chat list, when shown, then the empty chat (/chats) or an
 * open one (/chats/:id). The list is read once here, not per chat, since switching chats keeps this layout. */
@Component({
  selector: 'app-chats-layout',
  imports: [ChatList, RouterOutlet],
  template: `
    @if (toggle.shown()) {
      <app-chat-list />
    }
    <div class="chat"><router-outlet /></div>
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
  `,
})
export class ChatsLayout implements OnInit {
  protected readonly toggle = inject(ChatListToggle);
  private readonly chats = inject(Chats);

  ngOnInit(): void {
    this.chats.reload();
  }
}
