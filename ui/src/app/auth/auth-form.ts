import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Auth } from './auth';

export type AuthMode = 'login' | 'register';

/** /login and /register, one form by the route's mode. The design system has no login card (D14), so it is the
 * hero's heading and a primary button from the design system, with two fields drawn from its tokens. The rules on
 * email and password are the app backend's: the form checks nothing itself (novalidate, no length) and shows the
 * backend's reason as it came. */
@Component({
  selector: 'app-auth-form',
  imports: [RouterLink],
  template: `
    <div class="ag-scroll">
      <div class="ag-column ag-hero">
        <h1>{{ text().title }}</h1>
        <form novalidate (submit)="submit($event, email.value, password.value)">
          <label
            >Email
            <input #email name="email" type="email" autocomplete="username" />
          </label>
          <label
            >Password
            <input #password name="password" type="password" [autocomplete]="text().autocomplete" />
          </label>
          @if (error()) {
            <p role="alert" class="error">{{ error() }}</p>
          }
          <button type="submit" class="ag-btn ag-btn--primary" [disabled]="busy()">
            {{ text().title }}
          </button>
        </form>
        <p>
          {{ text().other }} <a [routerLink]="text().otherLink">{{ text().otherTitle }}</a>
        </p>
      </div>
    </div>
  `,
  styles: `
    :host {
      display: contents;
    }
    form {
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
      width: 100%;
      text-align: left;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: var(--space-1);
      font: 500 13px/18px var(--font-sans);
      color: var(--ink);
    }
    input {
      border: 1px solid var(--line-strong);
      border-radius: var(--radius-md);
      background: var(--surface);
      color: var(--ink);
      font: 400 15px/24px var(--font-sans);
      padding: var(--space-2) var(--space-3);
    }
    .error {
      color: var(--failed-ink);
    }
    a {
      color: var(--accent-ink);
    }
  `,
})
export class AuthForm {
  readonly mode = input.required<AuthMode>();
  private readonly auth = inject(Auth);
  private readonly router = inject(Router);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | undefined>(undefined);
  protected readonly text = computed(() => TEXT[this.mode()]);

  protected submit(event: Event, email: string, password: string): void {
    event.preventDefault();
    this.busy.set(true);
    this.error.set(undefined);
    const answer =
      this.mode() === 'login'
        ? this.auth.login(email, password)
        : this.auth.register(email, password);
    answer.subscribe({
      next: () => void this.router.navigate(['/']),
      error: (error: HttpErrorResponse) => {
        this.busy.set(false);
        this.error.set(reason(error));
      },
    });
  }
}

const TEXT: Record<
  AuthMode,
  { title: string; autocomplete: string; other: string; otherTitle: string; otherLink: string }
> = {
  login: {
    title: 'Log in',
    autocomplete: 'current-password',
    other: 'No account yet?',
    otherTitle: 'Register',
    otherLink: '/register',
  },
  register: {
    title: 'Register',
    autocomplete: 'new-password',
    other: 'Registered already?',
    otherTitle: 'Log in',
    otherLink: '/login',
  },
};

/** The app backend's reason ({detail} from FastAPI: a wrong password, a short password, a taken email), or, when it
 * gave none, that it did not answer: the proxy's 5xx when make app-forward is not running, or status 0. */
function reason(error: HttpErrorResponse): string {
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  return typeof detail === 'string'
    ? detail
    : `The app backend did not answer (HTTP ${error.status}).`;
}
