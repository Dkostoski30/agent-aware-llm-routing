import { ComponentFixture, TestBed } from '@angular/core/testing';
import { CachePressure } from './cache-pressure';

describe('CachePressure', () => {
  let fixture: ComponentFixture<CachePressure>;
  let page: HTMLElement;

  const metric = () => page.querySelector('.ag-metric') as HTMLElement;
  const number = () => metric().firstChild?.textContent?.trim();
  const caption = () => page.querySelector('.ag-rw-cap')?.textContent?.replace(/\s+/g, ' ').trim();

  function show(pressure: number | null, warn = 1) {
    fixture.componentRef.setInput('pressure', pressure);
    fixture.componentRef.setInput('warn', warn);
    fixture.detectChanges();
  }

  beforeEach(() => {
    fixture = TestBed.createComponent(CachePressure);
    page = fixture.nativeElement as HTMLElement;
  });

  it('shows the pressure with two decimals', () => {
    show(0.4237);
    expect(number()).toBe('0.42');
  });

  it('stays in the plain colour under the warning value', () => {
    show(0.99);
    expect(metric().classList).not.toContain('high');
    expect(metric().textContent).not.toContain('high');
  });

  it('turns to the warning colour, with a word, at the warning value', () => {
    show(1);
    expect(metric().classList).toContain('high');
    expect(metric().querySelector('small')?.textContent?.trim()).toBe('high');
  });

  it('stays in the warning colour over the warning value, and leaves it once back under', () => {
    show(1.62, 1.25);
    expect(metric().classList).toContain('high');
    show(1.24, 1.25);
    expect(metric().classList).not.toContain('high');
  });

  it('names the warning value the profile sets', () => {
    show(0.5, 1.25);
    expect(caption()).toBe('Context of the active runs over the KV cache of the ready pods; high from 1.25.');
  });

  it('shows a dash, never 0, when there is no pressure to read', () => {
    show(null);
    expect(number()).toBe('–');
    expect(metric().classList).not.toContain('high');
  });
});
