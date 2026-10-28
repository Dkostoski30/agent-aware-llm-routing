import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { RunScreen } from '../runs/run-screen';
import { NewTask } from './new-task';
import { SCENARIOS } from './scenarios';

describe('NewTask', () => {
  let fixture: ComponentFixture<NewTask>;
  let http: HttpTestingController;
  let page: HTMLElement;

  const textarea = () => page.querySelector('textarea') as HTMLTextAreaElement;
  const send = () => page.querySelector('[aria-label="Send"]') as HTMLButtonElement;
  const cards = () => Array.from(page.querySelectorAll<HTMLElement>('.ag-scenario'));

  async function type(text: string) {
    textarea().value = text;
    textarea().dispatchEvent(new Event('input'));
    await fixture.whenStable();
  }

  async function press(init: KeyboardEventInit) {
    textarea().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ...init }));
    await fixture.whenStable();
  }

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([{ path: 'runs/:id', component: RunScreen }]),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(NewTask);
    page = fixture.nativeElement;
    await fixture.whenStable();
  });

  afterEach(() => http.verify());

  it('shows the three scenarios in order', () => {
    expect(cards().map((card) => card.querySelector('h3')?.textContent?.trim())).toEqual([
      'CSV analysis',
      'Contracts',
      'Support',
    ]);
  });

  it('fills the composer with a scenario example and rings that card while the text is unchanged', async () => {
    cards()[1].click();
    await fixture.whenStable();
    expect(textarea().value).toBe(SCENARIOS[1].prompt);
    expect(cards().map((card) => card.classList.contains('ag-scenario--on'))).toEqual([
      false,
      true,
      false,
    ]);

    await type(SCENARIOS[1].prompt + ' Answer in one line.');
    expect(cards().some((card) => card.classList.contains('ag-scenario--on'))).toBe(false);
  });

  it('fills the composer from the keyboard too', async () => {
    cards()[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await fixture.whenStable();
    expect(textarea().value).toBe(SCENARIOS[0].prompt);
  });

  it('disables send while the task is empty or blank', async () => {
    expect(send().disabled).toBe(true);
    await type('   ');
    expect(send().disabled).toBe(true);
    await press({});
    http.expectNone('/api/runs');
    await type('What is 7 times 8?');
    expect(send().disabled).toBe(false);
  });

  it('sends the run request on Enter and opens the returned run', async () => {
    await type('What is 7 times 8?');
    await press({});

    const request = http.expectOne('/api/runs');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ task: 'What is 7 times 8?' });
    expect(send().disabled).toBe(true);

    request.flush({ run_id: 'run-0a1b2c3d4e5f' });
    await fixture.whenStable();
    expect(TestBed.inject(Router).url).toBe('/runs/run-0a1b2c3d4e5f');
  });

  it('sends with the send button too', async () => {
    await type('What is 7 times 8?');
    send().click();
    http.expectOne('/api/runs');
  });

  it('adds a line on Shift+Enter instead of sending', async () => {
    await type('What is 7 times 8?');
    await press({ shiftKey: true });
    http.expectNone('/api/runs');
  });

  it('stays on the page and says why when the agent refuses the run', async () => {
    await type('What is 7 times 8?');
    await press({});

    http
      .expectOne('/api/runs')
      .flush('agent down', { status: 503, statusText: 'Service Unavailable' });
    await fixture.whenStable();
    expect(TestBed.inject(Router).url).toBe('/');
    expect(page.querySelector('.ag-composer-hint')?.textContent).toContain('HTTP 503');
    expect(send().disabled).toBe(false);
  });
});
