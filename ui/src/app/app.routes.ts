import { Routes } from '@angular/router';
import { NewTask } from './new-task/new-task';
import { RunScreen } from './runs/run-screen';

export const routes: Routes = [
  { path: '', component: NewTask },
  { path: 'runs/:id', component: RunScreen },
];
