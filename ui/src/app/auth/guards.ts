import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { Auth } from './auth';

/** Every screen but /login and /register: a visitor who is not logged in logs in first. */
export const requireUser: CanActivateFn = async () => {
  const router = inject(Router);
  const user = await inject(Auth).load();
  return user !== null || router.parseUrl('/login');
};

/** /login and /register: a logged-in user has nothing to do there and goes to the app. */
export const requireGuest: CanActivateFn = async () => {
  const router = inject(Router);
  const user = await inject(Auth).load();
  return user === null || router.parseUrl('/');
};
