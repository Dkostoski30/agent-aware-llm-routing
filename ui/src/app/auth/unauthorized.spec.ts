import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Auth } from './auth';
import { unauthorized } from './unauthorized';

describe('unauthorized', () => {
  let http: HttpTestingController;
  let client: HttpClient;
  let navigated: unknown[][];
  let forgotten: number;

  beforeEach(() => {
    navigated = [];
    forgotten = 0;
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([unauthorized])),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: Auth, useValue: { forget: () => forgotten++ } satisfies Partial<Auth> },
      ],
    });
    const router = TestBed.inject(Router);
    vi.spyOn(router, 'navigate').mockImplementation(async (commands) => {
      navigated.push([...commands]);
      return true;
    });
    http = TestBed.inject(HttpTestingController);
    client = TestBed.inject(HttpClient);
  });

  afterEach(() => http.verify());

  /** Sends one GET to url, answers it with status, and returns the error the caller saw. */
  function answer(url: string, status: number): unknown {
    let seen: unknown;
    client.get(url).subscribe({ error: (error: unknown) => (seen = error) });
    http.expectOne(url).flush({ detail: 'refused' }, { status, statusText: 'Refused' });
    return seen;
  }

  it('sends the user back to /login when a later request finds the session gone', () => {
    const seen = answer('/api/chats', 401);
    expect(forgotten).toBe(1);
    expect(navigated).toEqual([['/login']]);
    expect(seen).toMatchObject({ status: 401 });
  });

  it("leaves a 401 from the auth API to its caller, so login can show the backend's reason", () => {
    const seen = answer('/api/auth/login', 401);
    expect(forgotten).toBe(0);
    expect(navigated).toEqual([]);
    expect(seen).toMatchObject({ status: 401 });
  });

  it('leaves other errors alone', () => {
    answer('/api/chats', 502);
    expect(forgotten).toBe(0);
    expect(navigated).toEqual([]);
  });
});
