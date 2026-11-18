import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Auth, User } from './auth';

const USER: User = { id: 1, email: 'ana@example.com' };
const PASSWORD = 'a long enough password';

describe('Auth', () => {
  let http: HttpTestingController;
  let auth: Auth;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    auth = TestBed.inject(Auth);
  });

  afterEach(() => http.verify());

  it('asks the app backend for the logged-in user once and remembers the answer', async () => {
    const first = auth.load();
    http.expectOne('/api/auth/me').flush(USER);
    expect(await first).toEqual(USER);
    expect(auth.user()).toEqual(USER);

    expect(await auth.load()).toEqual(USER);
    http.expectNone('/api/auth/me');
  });

  it('knows nobody is logged in after a 401 from /auth/me', async () => {
    const loaded = auth.load();
    http
      .expectOne('/api/auth/me')
      .flush({ detail: 'not logged in' }, { status: 401, statusText: 'Unauthorized' });
    expect(await loaded).toBeNull();
    expect(auth.user()).toBeNull();
  });

  it('knows nobody is logged in when the app backend does not answer', async () => {
    const loaded = auth.load();
    http.expectOne('/api/auth/me').error(new ProgressEvent('error'));
    expect(await loaded).toBeNull();
  });

  it('logs in and holds the user', () => {
    let answered: User | undefined;
    auth.login(USER.email, PASSWORD).subscribe((user) => (answered = user));

    const request = http.expectOne('/api/auth/login');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ email: USER.email, password: PASSWORD });
    request.flush(USER);
    expect(answered).toEqual(USER);
    expect(auth.user()).toEqual(USER);
  });

  it('registers, then logs in with the same credentials, since register opens no session', () => {
    let answered: User | undefined;
    auth.register(USER.email, PASSWORD).subscribe((user) => (answered = user));

    const register = http.expectOne('/api/auth/register');
    expect(register.request.method).toBe('POST');
    expect(register.request.body).toEqual({ email: USER.email, password: PASSWORD });
    http.expectNone('/api/auth/login');
    register.flush(USER, { status: 201, statusText: 'Created' });

    const login = http.expectOne('/api/auth/login');
    expect(login.request.body).toEqual({ email: USER.email, password: PASSWORD });
    login.flush(USER);
    expect(answered).toEqual(USER);
    expect(auth.user()).toEqual(USER);
  });

  it('does not log in when register is refused', () => {
    let refused: unknown;
    auth.register(USER.email, 'short').subscribe({ error: (error: unknown) => (refused = error) });
    http
      .expectOne('/api/auth/register')
      .flush(
        { detail: 'the password needs at least 15 characters' },
        { status: 422, statusText: 'Unprocessable Entity' },
      );
    http.expectNone('/api/auth/login');
    expect(refused).toBeDefined();
    expect(auth.user()).toBeNull();
  });

  it('logs out and forgets the user', async () => {
    const loaded = auth.load();
    http.expectOne('/api/auth/me').flush(USER);
    await loaded;

    let done = false;
    auth.logout().subscribe(() => (done = true));
    const request = http.expectOne('/api/auth/logout');
    expect(request.request.method).toBe('POST');
    request.flush(null, { status: 204, statusText: 'No Content' });
    expect(done).toBe(true);
    expect(auth.user()).toBeNull();
    expect(await auth.load()).toBeNull();
    http.expectNone('/api/auth/me');
  });

  it('keeps the user when logout fails, since the session still works on the backend', async () => {
    const loaded = auth.load();
    http.expectOne('/api/auth/me').flush(USER);
    await loaded;

    auth.logout().subscribe({ error: () => undefined });
    http.expectOne('/api/auth/logout').error(new ProgressEvent('error'));
    expect(auth.user()).toEqual(USER);
  });
});
