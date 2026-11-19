import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable, firstValueFrom, switchMap, tap } from 'rxjs';

/** The user the app backend's /auth endpoints answer with. */
export interface User {
  id: number;
  email: string;
}

/** The auth API's path behind the dev server's proxy, which sends it to make app-forward (proxy.conf.json). */
export const AUTH_API = '/api/auth/';

/** Who is logged in, by the app backend's session cookie. The cookie is HttpOnly, so the UI never holds the token:
 * it asks GET /auth/me once, then follows its own login and logout. */
@Injectable({ providedIn: 'root' })
export class Auth {
  private readonly http = inject(HttpClient);
  private readonly current = signal<User | null>(null);
  readonly user = this.current.asReadonly();
  // Set once /auth/me has answered, or a login or logout has: from then on current is the answer.
  private asked: Promise<User | null> | undefined;

  /** The logged-in user, or null when the app backend says nobody is (401) or does not answer: either way the
   * visitor has to log in, and the login form then shows why it fails. */
  load(): Promise<User | null> {
    this.asked ??= firstValueFrom(this.http.get<User>(`${AUTH_API}me`)).then(
      (user) => this.remember(user),
      () => this.remember(null),
    );
    return this.asked;
  }

  login(email: string, password: string): Observable<User> {
    return this.http
      .post<User>(`${AUTH_API}login`, { email, password })
      .pipe(tap((user) => this.remember(user)));
  }

  /** Registers, then logs in with the same credentials, since register opens no session. */
  register(email: string, password: string): Observable<User> {
    return this.http
      .post<User>(`${AUTH_API}register`, { email, password })
      .pipe(switchMap(() => this.login(email, password)));
  }

  /** Forgets the user only once the backend has deleted the session; until then the cookie still works. */
  logout(): Observable<void> {
    return this.http.post<void>(`${AUTH_API}logout`, null).pipe(tap(() => this.forget()));
  }

  /** The session is gone (a 401 from a later request, or a logout). */
  forget(): void {
    this.remember(null);
  }

  private remember(user: User | null): User | null {
    this.current.set(user);
    this.asked = Promise.resolve(user);
    return user;
  }
}
