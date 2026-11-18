import { Injectable, signal } from '@angular/core';

// Where this browser remembers whether the chat list is shown.
export const CHAT_LIST_STORAGE_KEY = 'tenax-chat-list';

/** Whether the chat list sits beside the chat (P3.13b). The top bar's Chats button flips it, so the chat can take
 * the width in the demo; this browser remembers the choice, as it does the theme. */
@Injectable({ providedIn: 'root' })
export class ChatListToggle {
  readonly shown = signal(localStorage.getItem(CHAT_LIST_STORAGE_KEY) !== 'hidden');

  toggle(): void {
    this.shown.set(!this.shown());
    localStorage.setItem(CHAT_LIST_STORAGE_KEY, this.shown() ? 'shown' : 'hidden');
  }
}
