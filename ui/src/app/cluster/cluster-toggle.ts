import { Injectable } from '@angular/core';
import { ShownToggle } from '../chats/shown-toggle';

// Where this browser remembers whether the cluster panel is shown.
export const CLUSTER_PANEL_STORAGE_KEY = 'tenax-cluster-panel';

/** Whether the cluster panel sits beside the chat (D14, P3.13c). The top bar's Cluster button flips it, so the chat
 * can take the width when the cluster doesn't matter. */
@Injectable({ providedIn: 'root' })
export class ClusterToggle extends ShownToggle {
  constructor() {
    super(CLUSTER_PANEL_STORAGE_KEY);
  }
}
