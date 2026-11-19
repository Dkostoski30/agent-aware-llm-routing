import { httpResource } from '@angular/common/http';
import { Component, computed } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { ClusterPanel } from './cluster/cluster-panel';
import { TopBar } from './ui/top-bar';

/** The profile and model the top bar's chip names; make ui writes it from config/<profile>.yaml. */
interface ProfileFile {
  profile: string;
  model: string;
}

/** The split view of every screen (README: Layout): the conversation on the left, the cluster panel on the right
 * (P3.11), which stays empty until someone logs in; P3.13c adds its toggle. */
@Component({
  selector: 'app-root',
  imports: [ClusterPanel, RouterOutlet, TopBar],
  template: `
    <div class="ag-root ag-app">
      <app-top-bar [profile]="chip()?.profile" [model]="chip()?.model" />
      <div class="ag-split">
        <main class="ag-main"><router-outlet /></main>
        <aside class="ag-panel" aria-label="Cluster"><app-cluster-panel /></aside>
      </div>
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
