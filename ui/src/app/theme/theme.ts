import { DOCUMENT, Injectable, inject, signal } from '@angular/core';
import { ThemeName, cssVariables } from './tokens';

// Where this browser remembers the theme picked in the top bar.
export const THEME_STORAGE_KEY = 'tenax-theme';

/** The active theme: dark unless this browser remembered light (README: Visual foundations). */
@Injectable({ providedIn: 'root' })
export class Theme {
  private readonly root = inject(DOCUMENT).documentElement;
  readonly current = signal<ThemeName>(
    localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark',
  );

  /** Writes the current theme's tokens onto <html>, where bundle.css reads them. */
  apply(): void {
    for (const [name, value] of Object.entries(cssVariables(this.current()))) {
      this.root.style.setProperty(name, value);
    }
    // Native parts (scrollbars, the textarea caret) follow the theme too.
    this.root.style.colorScheme = this.current();
  }

  toggle(): void {
    this.current.set(this.current() === 'dark' ? 'light' : 'dark');
    localStorage.setItem(THEME_STORAGE_KEY, this.current());
    this.apply();
  }
}
