import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';
import { AUTH_API, Auth } from './auth';

/** A 401 from any request outside the auth API means the session expired or was deleted, so the user logs in again.
 * The auth API's own 401s stay with their caller: a wrong password is the login form's to show, and /auth/me's is
 * the guard's answer. The error still reaches the caller. */
export const unauthorized: HttpInterceptorFn = (request, next) => {
  const auth = inject(Auth);
  const router = inject(Router);
  return next(request).pipe(
    catchError((error: unknown) => {
      if (
        error instanceof HttpErrorResponse &&
        error.status === 401 &&
        !request.url.startsWith(AUTH_API)
      ) {
        auth.forget();
        void router.navigate(['/login']);
      }
      return throwError(() => error);
    }),
  );
};
