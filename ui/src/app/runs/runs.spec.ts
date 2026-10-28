import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import runRequestSchema from '../../../../schemas/run-request.v1.json';
import { Runs } from './runs';

describe('Runs', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('posts the task to the agent and returns the run id', () => {
    let runId: string | undefined;
    TestBed.inject(Runs)
      .start('What is 7 times 8?')
      .subscribe((id) => (runId = id));

    const request = http.expectOne('/api/runs');
    expect(request.request.method).toBe('POST');
    request.flush({ run_id: 'run-0a1b2c3d4e5f' });
    expect(runId).toBe('run-0a1b2c3d4e5f');
  });

  // Contract test (D12): the body is a run-request.v1.
  it('sends a body that schemas/run-request.v1.json accepts', () => {
    TestBed.inject(Runs).start('What is 7 times 8?').subscribe();

    const body = http.expectOne('/api/runs').request.body as Record<string, unknown>;
    const keys = Object.keys(body);
    for (const required of runRequestSchema.required) expect(keys).toContain(required);
    for (const key of keys) expect(Object.keys(runRequestSchema.properties)).toContain(key);
    expect(runRequestSchema.properties.task.type).toBe('string');
    expect(typeof body['task']).toBe('string');
    expect((body['task'] as string).length).toBeGreaterThanOrEqual(
      runRequestSchema.properties.task.minLength,
    );
  });
});
