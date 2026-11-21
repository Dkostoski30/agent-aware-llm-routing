/** One replay the host's demo runner started (schemas/demo.v1.json, P4.6a). */
export interface DemoReplay {
  pattern: string;
  command: string;
  started_at: string;
  state: 'running' | 'done' | 'failed';
  runs_total: number;
  runs_finished: number;
  // Calls with no full reply after any retry; a replay with some is still done once it wrote its file.
  calls_failed: number;
  exit_code: number | null;
}

export interface DemoKill {
  pod: string;
  at: string;
}

/** The runner's GET /status: its latest replay and kill, null before the first. */
export interface DemoRunner {
  replay: DemoReplay | null;
  last_kill: DemoKill | null;
}

/** The body of the app backend's GET /demo. runner is null when the runner did not answer, and runner_error then
 * says why; a profile without the buttons has enabled false and nothing else. */
export interface DemoView {
  enabled: boolean;
  poll_seconds: number;
  patterns: { name: string; runs: number }[];
  runner: DemoRunner | null;
  runner_error: string | null;
}

export const DEMO_API = '/api/demo';
export const DEMO_REPLAY_API = '/api/demo/replay';
export const DEMO_KILL_API = '/api/demo/kill';
