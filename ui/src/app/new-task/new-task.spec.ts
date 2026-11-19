import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { TestRequest } from '@angular/common/http/testing';
import { Chats } from '../chats/chats';
import { NewTask } from './new-task';
import { SCENARIOS } from './scenarios';

@Component({ template: '' })
class OpenChat {}

const CHAT = { id: 7, title: 'What is 7 times 8?', created: 1_794_950_000 };
const SENT = { user_message_id: 1, assistant_message_id: 2, run_id: 'run-0a1b2c3d4e5f' };

describe('NewTask', () => {
  let fixture: ComponentFixture<NewTask>;
  let http: HttpTestingController;
  let page: HTMLElement;

  const textarea = () => page.querySelector('textarea') as HTMLTextAreaElement;
  const send = () => page.querySelector('[aria-label="Send"]') as HTMLButtonElement;
  const cards = () => Array.from(page.querySelectorAll<HTMLElement>('.ag-scenario'));
  const url = () => TestBed.inject(Router).url;

  /** Answers the chat's creation and returns the message request that follows it. */
  async function created(): Promise<TestRequest> {
    http.expectOne({ method: 'POST', url: '/api/chats' }).flush(CHAT);
    await fixture.whenStable();
    return http.expectOne('/api/chats/7/messages');
  }

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
        provideRouter([{ path: 'chats/:id', component: OpenChat }]),
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
    http.expectNone('/api/chats');
    await type('What is 7 times 8?');
    expect(send().disabled).toBe(false);
  });

  it('on Enter creates a chat titled with the task, sends the task to it and opens it', async () => {
    await type('What is 7 times 8?');
    await press({});

    const create = http.expectOne({ method: 'POST', url: '/api/chats' });
    expect(create.request.body).toEqual({ title: 'What is 7 times 8?' });
    expect(send().disabled).toBe(true);
    create.flush(CHAT);
    await fixture.whenStable();

    const message = http.expectOne('/api/chats/7/messages');
    expect(message.request.method).toBe('POST');
    expect(message.request.body).toEqual({ task: 'What is 7 times 8?' });
    message.flush(SENT);
    await fixture.whenStable();
    expect(url()).toBe('/chats/7');
  });

  it('puts the new chat at the top of the chat list', async () => {
    await type('What is 7 times 8?');
    await press({});
    (await created()).flush(SENT);
    expect(TestBed.inject(Chats).list()).toEqual([CHAT]);
  });

  it('sends with the send button too', async () => {
    await type('What is 7 times 8?');
    send().click();
    http.expectOne({ method: 'POST', url: '/api/chats' });
  });

  it('adds a line on Shift+Enter instead of sending', async () => {
    await type('What is 7 times 8?');
    await press({ shiftKey: true });
    http.expectNone('/api/chats');
  });

  it('stays on the page and says why when the chat cannot be created', async () => {
    await type('What is 7 times 8?');
    await press({});

    http
      .expectOne({ method: 'POST', url: '/api/chats' })
      .flush('app down', { status: 503, statusText: 'Service Unavailable' });
    await fixture.whenStable();
    expect(url()).toBe('/');
    expect(page.querySelector('.ag-composer-hint')?.textContent).toContain('HTTP 503');
    expect(send().disabled).toBe(false);
  });

  it('sends a refused message again to the chat it created, not to a new one', async () => {
    await type('What is 7 times 8?');
    await press({});
    (await created()).flush(
      { detail: 'the runtime refused the run' },
      { status: 422, statusText: 'Unprocessable Content' },
    );
    await fixture.whenStable();
    expect(url()).toBe('/');
    expect(page.querySelector('.ag-composer-hint')?.textContent).toContain(
      'HTTP 422, the runtime refused the run',
    );

    await press({});
    http.expectNone({ method: 'POST', url: '/api/chats' });
    http.expectOne('/api/chats/7/messages').flush(SENT);
    await fixture.whenStable();
    expect(url()).toBe('/chats/7');
  });

  describe('attached files', () => {
    const SALES = 'region,revenue\nNorth,1\n';
    const CONTRACT = '## Fees\nTen euros.\n';
    const picker = () => page.querySelector('input[type=file]') as HTMLInputElement;
    const attached = () =>
      Array.from(page.querySelectorAll('.ag-composer .ag-tag')).map((tag) =>
        tag.textContent?.trim(),
      );
    const hint = () => page.querySelector('.ag-composer-hint')?.textContent ?? '';

    /** What the browser does when the user picks files: fills the input's files, fires change. */
    function pick(...files: File[]) {
      Object.defineProperty(picker(), 'files', { value: files, configurable: true });
      picker().dispatchEvent(new Event('change'));
    }

    async function attach(...files: File[]) {
      pick(...files);
      await vi.waitFor(() => expect(attached().length + hint().length).toBeGreaterThan(0));
      await fixture.whenStable();
    }

    it('asks the browser for the file types the UI can send, several at once', () => {
      expect(picker().accept).toBe('.csv,.txt,.md');
      expect(picker().multiple).toBe(true);
    });

    it('opens the picker from the attach button', () => {
      const click = vi.spyOn(picker(), 'click');
      (page.querySelector('[aria-label="Attach files"]') as HTMLButtonElement).click();
      expect(click).toHaveBeenCalledOnce();
    });

    it('sends the attached files with the task, in the order they were attached', async () => {
      await attach(new File([SALES], 'sales.csv'));
      await attach(new File([CONTRACT], 'contract-2025.txt'));
      expect(attached()).toEqual(['sales.csv', 'contract-2025.txt']);

      await type('Total revenue?');
      await press({});
      const request = await created();
      expect(request.request.body).toEqual({
        task: 'Total revenue?',
        files: [
          { id: 'sales', name: 'sales.csv', type: 'csv', content: SALES },
          { id: 'contract-2025', name: 'contract-2025.txt', type: 'text', content: CONTRACT },
        ],
      });
    });

    it('takes several files picked at once', async () => {
      await attach(new File([SALES], 'sales.csv'), new File([CONTRACT], 'terms.md'));
      expect(attached()).toEqual(['sales.csv', 'terms.md']);
    });

    it('removes a file, which then stays out of the request', async () => {
      await attach(new File([SALES], 'sales.csv'), new File([CONTRACT], 'terms.md'));
      (page.querySelector('[aria-label="Remove sales.csv"]') as HTMLButtonElement).click();
      await fixture.whenStable();
      expect(attached()).toEqual(['terms.md']);

      await type('Which clauses?');
      await press({});
      const body = (await created()).request.body as { files: { name: string }[] };
      expect(body.files.map((file) => file.name)).toEqual(['terms.md']);
    });

    it('clears the picker, so the same file can be picked again after removing it', async () => {
      Object.defineProperty(picker(), 'value', {
        value: 'C:\\fakepath\\sales.csv',
        writable: true,
      });
      await attach(new File([SALES], 'sales.csv'));
      expect(picker().value).toBe('');
    });

    it('names a refused file and why, and keeps the files it could read', async () => {
      await attach(new File(['x'], 'report.pdf'), new File([SALES], 'sales.csv'));
      expect(attached()).toEqual(['sales.csv']);
      expect(hint()).toContain('report.pdf');
    });

    it('clears a refusal when the next file is picked', async () => {
      await attach(new File(['x'], 'report.pdf'));
      expect(hint()).toContain('report.pdf');
      await attach(new File([SALES], 'sales.csv'));
      expect(hint()).toBe('');
    });

    it('holds the send until a picked file has been read', async () => {
      let finishRead: (bytes: ArrayBuffer) => void = () => undefined;
      const slow = new File([SALES], 'sales.csv');
      slow.arrayBuffer = () => new Promise((resolve) => (finishRead = resolve));
      await type('Total revenue?');

      pick(slow);
      await fixture.whenStable();
      expect(send().disabled).toBe(true);
      await press({});
      http.expectNone('/api/chats');

      finishRead(new TextEncoder().encode(SALES).buffer as ArrayBuffer);
      await vi.waitFor(() => expect(attached()).toEqual(['sales.csv']));
      await fixture.whenStable();
      expect(send().disabled).toBe(false);
    });

    it('shows the reason the app gives for refusing the files, and keeps them', async () => {
      await attach(new File([SALES], 'sales.csv'));
      await type('Total revenue?');
      await press({});

      (await created()).flush(
        { detail: "files: duplicate file id 'sales'" },
        { status: 422, statusText: 'Unprocessable Content' },
      );
      await fixture.whenStable();
      expect(url()).toBe('/');
      expect(hint()).toContain('HTTP 422');
      expect(hint()).toContain("files: duplicate file id 'sales'");
      expect(attached()).toEqual(['sales.csv']);
    });

    it('clears a reason when a file is removed, since it may be about that file', async () => {
      await attach(new File([SALES], 'sales.csv'));
      await type('Total revenue?');
      await press({});
      (await created()).flush(
        { detail: "files: duplicate file id 'sales'" },
        { status: 422, statusText: 'Unprocessable Content' },
      );
      await fixture.whenStable();
      expect(hint()).toContain('duplicate');

      (page.querySelector('[aria-label="Remove sales.csv"]') as HTMLButtonElement).click();
      await fixture.whenStable();
      expect(hint()).toBe('');
    });

    it('frees the composer when a file is too large to read', async () => {
      const huge = new File([SALES], 'huge.csv');
      huge.arrayBuffer = () => Promise.reject(new RangeError('Array buffer allocation failed'));
      await type('Total revenue?');
      await attach(huge);
      expect(hint()).toContain('too large');
      expect(send().disabled).toBe(false);
    });

    it('frees the composer even when reading fails in a way nobody expected', async () => {
      const broken = new File([SALES], 'sales.csv');
      broken.arrayBuffer = () => Promise.reject(new Error('a bug'));
      await type('Total revenue?');
      await expect(fixture.componentInstance['attach']([broken])).rejects.toThrow('a bug');
      await fixture.whenStable();
      expect(send().disabled).toBe(false);
    });

    it('disables attaching and removing while the message is sent', async () => {
      await attach(new File([SALES], 'sales.csv'));
      await type('Total revenue?');
      await press({});
      await fixture.whenStable();
      const attachButton = page.querySelector('[aria-label="Attach files"]') as HTMLButtonElement;
      const remove = page.querySelector('[aria-label="Remove sales.csv"]') as HTMLButtonElement;
      expect([attachButton.disabled, remove.disabled]).toEqual([true, true]);
      (await created()).flush(SENT);
    });
  });
});
