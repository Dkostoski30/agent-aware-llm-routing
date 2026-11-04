import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import runCallsSchema from '../../../../schemas/run-calls.v1.json';
import { RunCall, RunCalls } from './run-calls';

const CALL: Required<RunCall> = {
  step: 1,
  ts_start: 1_793_600_000_000,
  duration_ms: 812,
  gap_before_ms: 0,
  expected_steps: 3,
  prompt_tokens: 398,
  completion_tokens: 9,
  cached_tokens: 352,
  pod: 'vllm-5c9565db76-6bq8s',
};

describe('RunCalls', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads the calls of the run through the /api proxy', () => {
    let calls: RunCall[] | undefined;
    TestBed.inject(RunCalls)
      .read('run-0a1b2c3d4e5f')
      .subscribe((read) => (calls = read));

    const request = http.expectOne('/api/runs/run-0a1b2c3d4e5f/calls');
    expect(request.request.method).toBe('GET');
    request.flush({ run_id: 'run-0a1b2c3d4e5f', calls: [CALL] });
    expect(calls).toEqual([CALL]);
  });

  it('encodes the run id', () => {
    TestBed.inject(RunCalls).read('run/1').subscribe();
    http.expectOne('/api/runs/run%2F1/calls');
  });

  // Contract test (D12): the UI's type has the fields schemas/run-calls.v1.json defines, and only the ones it requires
  // may be relied on.
  it('reads the fields that schemas/run-calls.v1.json defines', () => {
    const items = runCallsSchema.properties.calls.items;
    expect(Object.keys(CALL).sort()).toEqual(Object.keys(items.properties).sort());
    const required: (keyof RunCall)[] = [
      'step',
      'ts_start',
      'duration_ms',
      'gap_before_ms',
      'expected_steps',
    ];
    expect([...items.required].sort()).toEqual([...required].sort());
    expect(runCallsSchema.required).toEqual(['run_id', 'calls']);
  });
});
