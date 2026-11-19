import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { AuthForm } from './auth-form';
import { User } from './auth';

const USER: User = { id: 1, email: 'ana@example.com' };
const PASSWORD = 'a long enough password';

describe('AuthForm', () => {
  let fixture: ComponentFixture<AuthForm>;
  let http: HttpTestingController;
  let page: HTMLElement;
  let navigated: unknown[][];

  const field = (name: string) => page.querySelector(`input[name="${name}"]`) as HTMLInputElement;
  const submitButton = () => page.querySelector('button[type="submit"]') as HTMLButtonElement;
  const alert = () => page.querySelector('[role="alert"]')?.textContent?.trim();

  async function open(mode: 'login' | 'register') {
    fixture = TestBed.createComponent(AuthForm);
    fixture.componentRef.setInput('mode', mode);
    page = fixture.nativeElement;
    await fixture.whenStable();
  }

  async function submit(email: string, password: string) {
    field('email').value = email;
    field('password').value = password;
    page.querySelector('form')?.dispatchEvent(new Event('submit'));
    await fixture.whenStable();
  }

  beforeEach(() => {
    navigated = [];
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    vi.spyOn(TestBed.inject(Router), 'navigate').mockImplementation(async (commands) => {
      navigated.push([...commands]);
      return true;
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('logs in and opens the app', async () => {
    await open('login');
    await submit(USER.email, PASSWORD);
    expect(submitButton().disabled).toBe(true);
    http.expectOne('/api/auth/login').flush(USER);
    await fixture.whenStable();
    expect(navigated).toEqual([['/']]);
  });

  it("shows the backend's reason for a wrong password and stays on the form", async () => {
    await open('login');
    await submit(USER.email, 'not the password');
    http
      .expectOne('/api/auth/login')
      .flush({ detail: 'wrong email or password' }, { status: 401, statusText: 'Unauthorized' });
    await fixture.whenStable();
    expect(alert()).toBe('wrong email or password');
    expect(submitButton().disabled).toBe(false);
    expect(navigated).toEqual([]);
  });

  // The password rule lives in config/ (app.min_password_length); the UI only repeats the 422.
  it("shows the backend's password rule for a short password on register, with no rule of its own", async () => {
    await open('register');
    expect(field('password').minLength).toBe(-1);
    await submit(USER.email, 'short');
    http
      .expectOne('/api/auth/register')
      .flush(
        { detail: 'the password needs at least 15 characters' },
        { status: 422, statusText: 'Unprocessable Entity' },
      );
    await fixture.whenStable();
    expect(alert()).toBe('the password needs at least 15 characters');
    expect(navigated).toEqual([]);
  });

  it('registers, logs in at once and opens the app', async () => {
    await open('register');
    await submit(USER.email, PASSWORD);
    http.expectOne('/api/auth/register').flush(USER, { status: 201, statusText: 'Created' });
    http.expectOne('/api/auth/login').flush(USER);
    await fixture.whenStable();
    expect(navigated).toEqual([['/']]);
  });

  it('says the app backend did not answer when the error has no reason', async () => {
    await open('login');
    await submit(USER.email, PASSWORD);
    http.expectOne('/api/auth/login').flush('', { status: 504, statusText: 'Gateway Timeout' });
    await fixture.whenStable();
    expect(alert()).toBe('The app backend did not answer (HTTP 504).');
  });

  it('clears the last error on the next try', async () => {
    await open('login');
    await submit(USER.email, 'not the password');
    http
      .expectOne('/api/auth/login')
      .flush({ detail: 'wrong email or password' }, { status: 401, statusText: 'Unauthorized' });
    await fixture.whenStable();
    await submit(USER.email, PASSWORD);
    expect(alert()).toBeUndefined();
    http.expectOne('/api/auth/login').flush(USER);
  });

  it('links each form to the other', async () => {
    await open('login');
    expect(page.querySelector('a')?.getAttribute('href')).toBe('/register');
    await open('register');
    expect(page.querySelector('a')?.getAttribute('href')).toBe('/login');
  });
});
