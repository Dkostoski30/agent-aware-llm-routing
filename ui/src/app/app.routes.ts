import { Routes } from '@angular/router';
import { AuthForm } from './auth/auth-form';
import { requireGuest, requireUser } from './auth/guards';
import { NewTask } from './new-task/new-task';
import { RunScreen } from './runs/run-screen';

export const routes: Routes = [
  { path: 'login', component: AuthForm, data: { mode: 'login' }, canActivate: [requireGuest] },
  {
    path: 'register',
    component: AuthForm,
    data: { mode: 'register' },
    canActivate: [requireGuest],
  },
  { path: '', component: NewTask, canActivate: [requireUser] },
  { path: 'runs/:id', component: RunScreen, canActivate: [requireUser] },
];
