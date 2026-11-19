import { Injectable } from '@angular/core';
import { ShownToggle } from './shown-toggle';

// Where this browser remembers whether the chat list is shown.
export const CHAT_LIST_STORAGE_KEY = 'tenax-chat-list';

/** Whether the chat list sits beside the chat (P3.13b). The top bar's Chats button flips it, so the chat can take
 * the width in the demo. */
@Injectable({ providedIn: 'root' })
export class ChatListToggle extends ShownToggle {
  constructor() {
    super(CHAT_LIST_STORAGE_KEY);
  }
}
