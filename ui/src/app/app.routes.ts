import { Routes } from '@angular/router';
import { AuthForm } from './auth/auth-form';
import { requireGuest, requireUser } from './auth/guards';
import { ChatScreen } from './chats/chat-screen';
import { ChatsLayout } from './chats/chats-layout';
import { NewTask } from './new-task/new-task';

export const routes: Routes = [
  { path: 'login', component: AuthForm, data: { mode: 'login' }, canActivate: [requireGuest] },
  {
    path: 'register',
    component: AuthForm,
    data: { mode: 'register' },
    canActivate: [requireGuest],
  },
  { path: '', pathMatch: 'full', redirectTo: 'chats' },
  {
    path: 'chats',
    component: ChatsLayout,
    canActivate: [requireUser],
    children: [
      { path: '', component: NewTask },
      { path: ':id', component: ChatScreen },
    ],
  },
];
