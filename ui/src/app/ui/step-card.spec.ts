import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Served, StepState } from '../runs/run-view';
import { StepCard } from './step-card';

describe('StepCard', () => {
  let fixture: ComponentFixture<StepCard>;

  const SERVED: Served = { pod: { short: 'vllm-6bq8s', full: 'vllm-5c9565db76-6bq8s' }, latency: '290 ms' };

  async function render(state: StepState, served: Served, note?: string) {
    fixture = TestBed.createComponent(StepCard);
    fixture.componentRef.setInput('n', 4);
    fixture.componentRef.setInput('tool', 'diff_clauses');
    fixture.componentRef.setInput('state', state);
    fixture.componentRef.setInput('served', served);
    fixture.componentRef.setInput('note', note);
    await fixture.whenStable();
  }

  const tags = () =>
    Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('.ag-tag')).map((tag) =>
      tag.textContent?.trim(),
    );
  const card = () => (fixture.nativeElement as HTMLElement).querySelector('.ag-step') as HTMLElement;

  it('tags a call sent again after its pod died, after its latency, with the failed border', async () => {
    await render('done', { ...SERVED, retries: 2 });
    expect(tags()).toEqual(['vllm-6bq8s', '290 ms', 'retry 2']);
    expect(card().classList).toContain('ag-step--retried');
    expect(card().querySelector('.ag-tag--retry')?.textContent?.trim()).toBe('retry 2');
  });

  it('tags the call a resumed runtime sent, with the failed border', async () => {
    await render('done', { ...SERVED, resumed: true });
    expect(tags()).toEqual(['vllm-6bq8s', '290 ms', 'resumed']);
    expect(card().classList).toContain('ag-step--retried');
  });

  it('shows the retry, the resume and a tool error together, the error last', async () => {
    await render('retried', { ...SERVED, retries: 1, resumed: true }, 'arguments are not valid JSON');
    expect(tags()).toEqual(['vllm-6bq8s', '290 ms', 'retry 1', 'resumed', 'error']);
  });

  it('draws a call sent once as before: no event tag, its own state', async () => {
    await render('done', SERVED);
    expect(tags()).toEqual(['vllm-6bq8s', '290 ms']);
    expect(card().classList).toContain('ag-step--done');
    expect(card().classList).not.toContain('ag-step--retried');
  });
});
