import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Auth } from '../auth/auth';
import clusterSchema from '../../../../schemas/cluster.v1.json';
import { CLUSTER_API, ClusterPod, ClusterView } from './cluster';
import { ClusterPanel } from './cluster-panel';

const READY: ClusterPod = { name: 'vllm-568fc4b669-c9kb2', state: 'ready', queue: 3, running: 2 };
const STARTING: ClusterPod = {
  name: 'vllm-568fc4b669-fgdgm',
  state: 'starting',
  queue: null,
  running: null,
};
const VIEW: ClusterView = { remaining_work: 7, active_runs: 4, poll_seconds: 5, pods: [READY, STARTING] };
const ANA = { id: 1, email: 'ana@example.com' };

describe('ClusterPanel', () => {
  let http: HttpTestingController;
  let auth: Auth;
  let fixture: ComponentFixture<ClusterPanel>;
  let page: HTMLElement;

  const text = () => page.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  const steps = () => page.querySelector('.ag-metric')?.firstChild?.textContent?.trim();
  const pods = () =>
    Array.from(page.querySelectorAll('.ag-pod')).map((pod) =>
      ['.ag-pod-name', '.ag-status', '.ag-pod-grid b'].map((part) =>
        pod.querySelector(part)?.textContent?.trim(),
      ),
    );

  function logIn() {
    auth.login(ANA.email, 'a long enough password').subscribe();
    http.expectOne('/api/auth/login').flush(ANA);
    fixture.detectChanges();
  }

  function answer(view: ClusterView = VIEW) {
    http.expectOne(CLUSTER_API).flush(view);
    fixture.detectChanges();
  }

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    auth = TestBed.inject(Auth);
    fixture = TestBed.createComponent(ClusterPanel);
    page = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
  });

  afterEach(() => {
    http.verify();
    vi.useRealTimers();
  });

  it('asks nothing while nobody is logged in', () => {
    vi.advanceTimersByTime(60_000);
    http.expectNone(CLUSTER_API);
  });

  it('shows the remaining work and each pod with its state, running and waiting', () => {
    logIn();
    answer();
    expect(steps()).toBe('7');
    expect(text()).toContain('over 4 active runs');
    expect(pods()).toEqual([
      [READY.name, 'Ready', '2 · 3'],
      [STARTING.name, 'Starting', '– · –'],
    ]);
  });

  it('asks again after the poll interval the answer carries, not sooner', () => {
    logIn();
    answer({ ...VIEW, poll_seconds: 3 });
    vi.advanceTimersByTime(2_999);
    http.expectNone(CLUSTER_API);
    vi.advanceTimersByTime(1);
    answer({ ...VIEW, remaining_work: 2 });
    expect(steps()).toBe('2');
  });

  it('says the numbers are missing on a 503 instead of showing zeros, and asks again', () => {
    logIn();
    answer();
    vi.advanceTimersByTime(5_000);
    http
      .expectOne(CLUSTER_API)
      .flush(
        { detail: 'Prometheus did not answer', poll_seconds: 5 },
        { status: 503, statusText: 'Service Unavailable' },
      );
    fixture.detectChanges();
    expect(text()).toContain('The cluster numbers could not be read.');
    expect(pods()).toEqual([]);
    expect(steps()).toBeUndefined();
    vi.advanceTimersByTime(5_000);
    answer();
    expect(text()).not.toContain('could not be read');
    expect(pods().length).toBe(2);
  });

  it('takes the poll interval from the 503 when no answer came before', () => {
    logIn();
    http
      .expectOne(CLUSTER_API)
      .flush(
        { detail: 'Prometheus did not answer', poll_seconds: 4 },
        { status: 503, statusText: 'Service Unavailable' },
      );
    vi.advanceTimersByTime(4_000);
    answer();
    expect(pods().length).toBe(2);
  });

  it('keeps the last interval when the app backend itself does not answer', () => {
    logIn();
    answer({ ...VIEW, poll_seconds: 3 });
    vi.advanceTimersByTime(3_000);
    http.expectOne(CLUSTER_API).error(new ProgressEvent('error'));
    fixture.detectChanges();
    expect(text()).toContain('The cluster numbers could not be read.');
    expect(text()).not.toContain('Reload');
    vi.advanceTimersByTime(3_000);
    answer();
    expect(pods().length).toBe(2);
  });

  it('stops asking when the app backend never said how often', () => {
    logIn();
    http.expectOne(CLUSTER_API).error(new ProgressEvent('error'));
    fixture.detectChanges();
    expect(text()).toContain('The cluster numbers could not be read. Reload the page to try again.');
    vi.advanceTimersByTime(60_000);
    http.expectNone(CLUSTER_API);
  });

  it('stops asking and forgets the numbers on logout', () => {
    logIn();
    answer();
    auth.forget();
    fixture.detectChanges();
    expect(pods()).toEqual([]);
    vi.advanceTimersByTime(60_000);
    http.expectNone(CLUSTER_API);
  });

  it('stops asking once removed', () => {
    logIn();
    answer();
    fixture.destroy();
    vi.advanceTimersByTime(60_000);
    http.expectNone(CLUSTER_API);
  });

  // Contract test (D12): the UI's types have exactly the fields schemas/cluster.v1.json defines, all required.
  it('reads the fields that schemas/cluster.v1.json defines', () => {
    expect(Object.keys(VIEW).sort()).toEqual(Object.keys(clusterSchema.properties).sort());
    expect([...clusterSchema.required].sort()).toEqual(Object.keys(VIEW).sort());
    const pod = clusterSchema.properties.pods.items;
    expect(Object.keys(READY).sort()).toEqual(Object.keys(pod.properties).sort());
    expect([...pod.required].sort()).toEqual(Object.keys(READY).sort());
    expect([...pod.properties.state.enum].sort()).toEqual(['draining', 'failed', 'ready', 'starting']);
  });
});
