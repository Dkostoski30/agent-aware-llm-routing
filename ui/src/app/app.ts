import { httpResource } from '@angular/common/http';
import { Component, computed } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { TopBar } from './ui/top-bar';

/** The profile and model the top bar's chip names; make ui writes it from config/<profile>.yaml. */
interface ProfileFile {
  profile: string;
  model: string;
}

/** Every screen: the top bar over the routed screen. The chat screens hold the cluster panel beside the chat
 * (ChatsLayout, P3.13c); the login screens have none, since only a logged-in user can read the cluster. */
@Component({
  selector: 'app-root',
  imports: [RouterOutlet, TopBar],
  template: `
    <div class="ag-root ag-app">
      <app-top-bar [profile]="chip()?.profile" [model]="chip()?.model" />
      <main class="ag-main"><router-outlet /></main>
    </div>
  `,
  styles: ':host { display: block; height: 100%; }',
})
export class App {
  private readonly profileFile = httpResource<ProfileFile>(() => '/profile.json');
  protected readonly chip = computed(() =>
    this.profileFile.hasValue() ? this.profileFile.value() : undefined,
  );
}
