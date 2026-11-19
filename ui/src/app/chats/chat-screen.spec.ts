import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of } from 'rxjs';
import { RunCalls } from '../runs/run-calls';
import { RunEnd, RunEvent, RunEvents, RunStarted } from '../runs/run-events';
import { ChatScreen } from './chat-screen';
import { ChatWithMessages, Message } from './chats';
import { SavedRun } from './saved-runs';

const SALES = 'region,revenue\nNorth,1\n';
const ASKED: Message = {
  id: 1,
  role: 'user',
  text: 'Total revenue?',
  status: 'done',
  run_id: null,
  created: 1_794_950_000,
  files: [{ id: 'sales', name: 'sales.csv', type: 'csv', size: 22 }],
};
const ANSWERED: Message = {
  id: 2,
  role: 'assistant',
  text: 'Total revenue is 1.',
  status: 'done',
  run_id: 'run-0a1b2c3d4e5f',
  created: 1_794_950_000,
  files: [],
};
const RUNNING: Message = { ...ANSWERED, text: '', status: 'running' };
const STARTED: RunStarted = {
  run_id: RUNNING.run_id as string,
  task: 'Total revenue?',
  expected_steps: 2,
  created_ms: 1_000,
};
const END: RunEnd = {
  run_id: RUNNING.run_id as string,
  status: 'done',
  answer: 'Total revenue is 1.',
  error: null,
  steps: 2,
  invalid_calls: 0,
  elapsed_ms: 2_100,
};

const SAVED_RUN: SavedRun = {
  run_id: ANSWERED.run_id as string,
  status: 'done',
  task: 'Total revenue?',
  answer: 'Total revenue is 1.',
  error: null,
  expected_steps: 2,
  invalid_calls: 0,
  steps: [{ step: 1, content: 'Total revenue is 1.', tool_calls: [], error: null }],
};

const chat = (id: number, ...messages: Message[]): ChatWithMessages => ({
  id,
  title: 'Total revenue?',
  created: 1_794_950_000,
  messages,
});

describe('ChatScreen', () => {
  let fixture: ComponentFixture<ChatScreen>;
  let http: HttpTestingController;
  let page: HTMLElement;
  // One stream per watched run, in order.
  let streams: Subject<RunEvent>[];
  let watched: string[];

  const text = (selector: string) =>
    page.querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim();
  const textarea = () => page.querySelector('textarea') as HTMLTextAreaElement;
  const sendButton = () => page.querySelector('[aria-label="Send"]') as HTMLButtonElement;
  const picker = () => page.querySelector('input[type=file]') as HTMLInputElement;
  const attached = () =>
    Array.from(page.querySelectorAll('.ag-composer .ag-tag')).map((tag) => tag.textContent?.trim());
  const expanded = () =>
    Array.from(page.querySelectorAll('button[aria-expanded]')).map((button) =>
      button.getAttribute('aria-expanded'),
    );
  const hint = () => page.querySelector('.ag-composer-hint')?.textContent ?? '';

  async function answerRead(id: number, ...messages: Message[]) {
    http.expectOne(`/api/chats/${id}`).flush(chat(id, ...messages));
    await fixture.whenStable();
  }

  async function open(id: string) {
    fixture.componentRef.setInput('id', id);
    await fixture.whenStable();
  }

  async function type(task: string) {
    textarea().value = task;
    textarea().dispatchEvent(new Event('input'));
    await fixture.whenStable();
  }

  async function attach(file: File) {
    Object.defineProperty(picker(), 'files', { value: [file], configurable: true });
    picker().dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(attached().length).toBeGreaterThan(0));
    await fixture.whenStable();
  }

  beforeEach(async () => {
    streams = [];
    watched = [];
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: RunEvents,
          useValue: {
            watch: (id: string) => {
              watched.push(id);
              const stream = new Subject<RunEvent>();
              streams.push(stream);
              return stream.asObservable();
            },
          },
        },
        { provide: RunCalls, useValue: { read: () => of([]) } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ChatScreen);
    page = fixture.nativeElement;
    await open('7');
  });

  afterEach(() => http.verify());

  it('shows the saved messages of the chat it opens, as after a reload', async () => {
    await answerRead(7, ASKED, ANSWERED);
    expect(text('.said')).toBe('Total revenue?');
    expect(text('.said + .ag-tags')).toBe('sales.csv');
    expect(text('.ag-answer p')).toBe('Total revenue is 1.');
    expect(watched).toEqual([]);
  });

  it('shows a failed run’s sentence, never an HTTP error', async () => {
    const expired = 'The agent runtime no longer has this run: its history expired.';
    await answerRead(7, ASKED, { ...ANSWERED, status: 'failed', text: expired });
    expect(text('.ag-notice--fail')).toBe(`The run failed. ${expired}`);
    expect(page.querySelector('.ag-answer')).toBeNull();
  });

  it('follows an unfinished run, then reads the chat again when it ends', async () => {
    await answerRead(7, ASKED, RUNNING);
    expect(watched).toEqual([RUNNING.run_id]);
    streams[0].next({ kind: 'run', data: STARTED });
    await fixture.whenStable();
    expect(page.querySelector('.ag-answer-wait')).not.toBeNull();

    streams[0].next({ kind: 'end', data: END });
    await fixture.whenStable();
    await answerRead(7, ASKED, ANSWERED);
    // The answer whose run just ended opens with its steps (P3.13c), so they are read once more.
    http.expectOne(`/api/runs/${ANSWERED.run_id}`).flush(SAVED_RUN);
    await fixture.whenStable();
    expect(text('.ag-answer p')).toBe('Total revenue is 1.');
    expect(page.querySelector('.ag-answer-wait')).toBeNull();
    expect(page.querySelector('.ag-run-head')).toBeNull();
  });

  it('opens only the answer whose run just ended, and folds it again in another chat', async () => {
    const older: Message = { ...ANSWERED, id: 4, run_id: 'run-ffffffffffff' };
    await answerRead(7, ASKED, older, { ...ASKED, id: 5 }, RUNNING);
    streams[0].next({ kind: 'end', data: END });
    await fixture.whenStable();
    await answerRead(7, ASKED, older, { ...ASKED, id: 5 }, ANSWERED);
    http.expectOne(`/api/runs/${ANSWERED.run_id}`).flush(SAVED_RUN);
    await fixture.whenStable();
    expect(expanded()).toEqual(['false', 'true']);
    expect(page.querySelectorAll('.ag-step').length).toBe(1);

    await open('9');
    await answerRead(9, ASKED, ANSWERED);
    expect(expanded()).toEqual(['false']);
  });

  it('reads the chat again when the run events cannot be read, and says so if it still runs', async () => {
    await answerRead(7, ASKED, RUNNING);
    streams[0].error(new Error('closed'));
    await fixture.whenStable();
    await answerRead(7, ASKED, RUNNING);
    expect(text('.ag-notice--fail')).toContain('The run events could not be read.');
    expect(watched).toEqual([RUNNING.run_id]);
  });

  it('shows the saved sentence when the run turns out to have failed', async () => {
    await answerRead(7, ASKED, RUNNING);
    streams[0].error(new Error('closed'));
    await fixture.whenStable();
    await answerRead(7, ASKED, { ...RUNNING, status: 'failed', text: 'The run expired.' });
    // Its run just ended too, so it opens and asks for its steps (P3.13c); the runtime no longer has them.
    http
      .expectOne(`/api/runs/${RUNNING.run_id}`)
      .flush({ detail: 'unknown run' }, { status: 404, statusText: 'Not Found' });
    await fixture.whenStable();
    expect(text('.ag-notice--fail')).toBe('The run failed. The run expired.');
  });

  it('reads the chat it switches to, stops following the old run and drops the draft', async () => {
    await answerRead(7, ASKED, RUNNING);
    await type('And by region?');
    await open('9');
    await answerRead(9, { ...ASKED, text: 'Which clauses?', files: [] });
    expect(text('.said')).toBe('Which clauses?');
    expect(streams[0].observed).toBe(false);
    expect(textarea().value).toBe('');
  });

  it('shows nothing of the old chat while the new one is read', async () => {
    await answerRead(7, ASKED, ANSWERED);
    await open('9');
    expect(page.querySelector('.said')).toBeNull();
    await answerRead(9);
  });

  it('waits to send while the last answer runs', async () => {
    await answerRead(7, ASKED, RUNNING);
    await type('And by region?');
    expect(sendButton().disabled).toBe(true);
  });

  it('sends a message with its files, clears the composer, then reads the chat to follow the run', async () => {
    await answerRead(7, ASKED, ANSWERED);
    await attach(new File([SALES], 'sales-2.csv'));
    await type('And by region?');
    sendButton().click();
    await fixture.whenStable();

    const request = http.expectOne('/api/chats/7/messages');
    expect(request.request.body).toEqual({
      task: 'And by region?',
      files: [{ id: 'sales-2', name: 'sales-2.csv', type: 'csv', content: SALES }],
    });
    expect(sendButton().disabled).toBe(true);
    request.flush({ user_message_id: 3, assistant_message_id: 4, run_id: 'run-ffffffffffff' });
    await fixture.whenStable();
    expect(textarea().value).toBe('');
    expect(attached()).toEqual([]);

    await answerRead(
      7,
      ASKED,
      ANSWERED,
      { ...ASKED, id: 3 },
      { ...RUNNING, id: 4, run_id: 'run-ffffffffffff' },
    );
    expect(watched).toEqual(['run-ffffffffffff']);
  });

  it('keeps the text and files and says why when the message is refused', async () => {
    await answerRead(7, ASKED, ANSWERED);
    await attach(new File([SALES], 'sales-2.csv'));
    await type('And by region?');
    sendButton().click();
    http
      .expectOne('/api/chats/7/messages')
      .flush(
        { detail: 'at most 4 files per message (agent.max_files)' },
        { status: 422, statusText: 'Unprocessable Content' },
      );
    await fixture.whenStable();
    expect(hint()).toContain('HTTP 422, at most 4 files per message (agent.max_files)');
    expect(textarea().value).toBe('And by region?');
    expect(attached()).toEqual(['sales-2.csv']);
    expect(sendButton().disabled).toBe(false);
    // The app saves a message whose run could not start (502), so the chat is read again after any refusal.
    await answerRead(7, ASKED, ANSWERED);
  });

  it('says so when the chat cannot be read', async () => {
    http
      .expectOne('/api/chats/7')
      .flush({ detail: 'no such chat' }, { status: 404, statusText: 'Not Found' });
    await fixture.whenStable();
    expect(text('.ag-notice--fail')).toBe(
      'The chat could not be read. It does not exist, or the app backend did not answer.',
    );
  });
});
