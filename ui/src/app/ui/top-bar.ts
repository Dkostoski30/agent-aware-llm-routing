import { Component, inject, input } from '@angular/core';
import { Router } from '@angular/router';
import { Auth } from '../auth/auth';
import { ChatListToggle } from '../chats/chat-list-toggle';
import { ClusterToggle } from '../cluster/cluster-toggle';
import { Theme } from '../theme/theme';
import { Horse } from './horse';
import { Icon } from './icon';

/** Across the top of every screen (TopBar card). The chip names the profile and model, so a screenshot says
 * which setup produced it; it is left out when the UI was started without a profile. Chats, New chat and Log out
 * show only to a logged-in user (P3.13a); Chats shows or hides the chat list (P3.13b), Cluster the cluster panel
 * (P3.13c). */
@Component({
  selector: 'app-top-bar',
  imports: [Horse, Icon],
  template: `
    <header class="ag-topbar">
      <span class="ag-brand"><app-horse [state]="working() ? 'working' : 'idle'" />Tenax</span>
      @if (auth.user()) {
        <button
          type="button"
          class="ag-btn ag-btn--ghost ag-btn--sm"
          [attr.aria-pressed]="chatList.shown()"
          (click)="chatList.toggle()"
        >
          <app-icon name="chat" />Chats
        </button>
        <button
          type="button"
          class="ag-btn ag-btn--ghost ag-btn--sm"
          [attr.aria-pressed]="cluster.shown()"
          (click)="cluster.toggle()"
        >
          <app-icon name="pods" />Cluster
        </button>
        <button type="button" class="ag-btn ag-btn--ghost ag-btn--sm" (click)="newChat()">
          <app-icon name="plus" />New chat
        </button>
      }
      <span class="ag-spacer"></span>
      @if (profile()) {
        <span class="ag-profile"
          >Profile <b>{{ profile() }}</b> · <code>{{ model() }}</code></span
        >
      }
      <button
        type="button"
        class="ag-btn ag-btn--ghost ag-btn--icon"
        aria-label="Switch theme"
        (click)="theme.toggle()"
      >
        <app-icon name="moon" />
      </button>
      @if (auth.user()) {
        <button type="button" class="ag-btn ag-btn--ghost ag-btn--sm" (click)="logOut()">
          Log out
        </button>
      }
    </header>
  `,
  styles: ':host { display: contents; }',
})
export class TopBar {
  readonly profile = input<string>();
  readonly model = input<string>();
  readonly working = input(false);
  protected readonly theme = inject(Theme);
  protected readonly auth = inject(Auth);
  protected readonly chatList = inject(ChatListToggle);
  protected readonly cluster = inject(ClusterToggle);
  private readonly router = inject(Router);

  protected newChat(): void {
    void this.router.navigate(['/chats']);
  }

  /** Back to /login once the backend has deleted the session; a failed logout leaves the user logged in. */
  protected logOut(): void {
    this.auth.logout().subscribe(() => void this.router.navigate(['/login']));
  }
}
