import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { TopBar } from '../ui/top-bar';
import { THEME_STORAGE_KEY, Theme } from './theme';
import { cssVariables } from './tokens';

const root = document.documentElement;

describe('Theme', () => {
  beforeEach(() => {
    localStorage.clear();
    root.removeAttribute('style');
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
  });

  it('applies the dark theme when nothing is remembered', () => {
    TestBed.inject(Theme).apply();
    expect(root.style.getPropertyValue('--bg')).toBe(cssVariables('dark')['--bg']);
    expect(root.style.colorScheme).toBe('dark');
  });

  it('applies the theme this browser remembered', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    TestBed.inject(Theme).apply();
    expect(root.style.getPropertyValue('--bg')).toBe(cssVariables('light')['--bg']);
  });

  it('ignores a remembered value that is not a theme', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'sepia');
    TestBed.inject(Theme).apply();
    expect(root.style.getPropertyValue('--bg')).toBe(cssVariables('dark')['--bg']);
  });

  it('switches from the top bar and remembers the choice', async () => {
    TestBed.inject(Theme).apply();
    const fixture = TestBed.createComponent(TopBar);
    await fixture.whenStable();
    const toggle = fixture.nativeElement.querySelector(
      '[aria-label="Switch theme"]',
    ) as HTMLButtonElement;

    toggle.click();
    expect(root.style.getPropertyValue('--bg')).toBe(cssVariables('light')['--bg']);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');

    toggle.click();
    expect(root.style.getPropertyValue('--bg')).toBe(cssVariables('dark')['--bg']);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });
});
