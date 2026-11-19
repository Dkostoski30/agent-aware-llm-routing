import { StatusState } from '../ui/status';

/** One vLLM pod of schemas/cluster.v1.json. queue and running are null until the pod's metrics are scraped, so a
 * pod still loading never reads as idle. */
export interface ClusterPod {
  name: string;
  state: StatusState;
  queue: number | null;
  running: number | null;
}

/** The body of the app backend's GET /cluster (P3.11): what Prometheus holds, read with the Grafana panels'
 * queries. */
export interface ClusterView {
  remaining_work: number;
  active_runs: number;
  poll_seconds: number;
  pods: ClusterPod[];
}

/** The app backend's 503 when Prometheus does not answer: still says when to ask again. */
export interface ClusterDown {
  detail: string;
  poll_seconds: number;
}

export const CLUSTER_API = '/api/cluster';
