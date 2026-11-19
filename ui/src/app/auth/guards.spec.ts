import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  CanActivateFn,
  Router,
  RouterStateSnapshot,
  UrlTree,
  provideRouter,
} from '@angular/router';
import { Auth, User } from './auth';
import { requireGuest, requireUser } from './guards';

const USER: User = { id: 1, email: 'ana@example.com' };

describe('guards', () => {
  let loggedIn: User | null;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: Auth, useValue: { load: async () => loggedIn } satisfies Partial<Auth> },
      ],
    });
  });

  /** The guard's answer: true, or the URL it sends the visitor to. */
  async function run(guard: CanActivateFn): Promise<true | string> {
    const answer = await TestBed.runInInjectionContext(() =>
      guard({} as ActivatedRouteSnapshot, {} as RouterStateSnapshot),
    );
    if (answer instanceof UrlTree) return TestBed.inject(Router).serializeUrl(answer);
    expect(answer).toBe(true);
    return true;
  }

  it('lets a logged-in user into the app', async () => {
    loggedIn = USER;
    expect(await run(requireUser)).toBe(true);
  });

  it('sends a visitor who is not logged in to /login', async () => {
    loggedIn = null;
    expect(await run(requireUser)).toBe('/login');
  });

  it('sends a logged-in user away from /login and /register to the app', async () => {
    loggedIn = USER;
    expect(await run(requireGuest)).toBe('/');
  });

  it('shows /login and /register to a visitor who is not logged in', async () => {
    loggedIn = null;
    expect(await run(requireGuest)).toBe(true);
  });
});
