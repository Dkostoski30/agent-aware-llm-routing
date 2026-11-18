import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Chat, ChatWithMessages, Chats } from './chats';

const OLDER: Chat = { id: 3, title: 'Total revenue?', created: 1_794_950_000 };
const NEWER: Chat = { id: 7, title: 'Which clauses?', created: 1_794_950_800 };
const SALES = { id: 'sales', name: 'sales.csv', type: 'csv' as const, content: 'region\nNorth\n' };

describe('Chats', () => {
  let chats: Chats;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    chats = TestBed.inject(Chats);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists the chats as the app backend sends them, newest first', () => {
    chats.reload();
    const request = http.expectOne('/api/chats');
    expect(request.request.method).toBe('GET');
    request.flush([NEWER, OLDER]);
    expect(chats.list()).toEqual([NEWER, OLDER]);
    expect(chats.listLost()).toBe(false);
  });

  it('empties the list while it is read again, so no earlier user’s chats stay on screen', () => {
    chats.reload();
    http.expectOne('/api/chats').flush([NEWER, OLDER]);
    chats.reload();
    expect(chats.list()).toEqual([]);
    http.expectOne('/api/chats').flush([OLDER]);
    expect(chats.list()).toEqual([OLDER]);
  });

  it('says so when the list cannot be read, and clears that on the next answer', () => {
    chats.reload();
    http.expectOne('/api/chats').flush('down', { status: 502, statusText: 'Bad Gateway' });
    expect(chats.listLost()).toBe(true);
    chats.reload();
    http.expectOne('/api/chats').flush([OLDER]);
    expect(chats.listLost()).toBe(false);
  });

  it('creates a chat with its title and puts it at the top of the list', () => {
    chats.reload();
    http.expectOne('/api/chats').flush([OLDER]);
    let created: Chat | undefined;
    chats.create('Which clauses?').subscribe((chat) => (created = chat));

    const request = http.expectOne('/api/chats');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ title: 'Which clauses?' });
    request.flush(NEWER);
    expect(created).toEqual(NEWER);
    expect(chats.list()).toEqual([NEWER, OLDER]);
  });

  it('reads one chat with its messages', () => {
    const chat: ChatWithMessages = { ...NEWER, messages: [] };
    let read: ChatWithMessages | undefined;
    chats.read('7').subscribe((answer) => (read = answer));
    http.expectOne('/api/chats/7').flush(chat);
    expect(read).toEqual(chat);
  });

  it('sends a message with its files to its chat', () => {
    let runId: string | undefined;
    chats.send('7', 'Total revenue?', [SALES]).subscribe((sent) => (runId = sent.run_id));

    const request = http.expectOne('/api/chats/7/messages');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ task: 'Total revenue?', files: [SALES] });
    request.flush({ user_message_id: 1, assistant_message_id: 2, run_id: 'run-0a1b2c3d4e5f' });
    expect(runId).toBe('run-0a1b2c3d4e5f');
  });

  it('sends no files field for a message without files', () => {
    chats.send('7', 'And by region?', []).subscribe();
    expect(http.expectOne('/api/chats/7/messages').request.body).toEqual({
      task: 'And by region?',
    });
  });

  it('keeps the chat id whole in the path', () => {
    chats.read('7/messages').subscribe({ error: () => undefined });
    http.expectOne('/api/chats/7%2Fmessages').flush('no', { status: 404, statusText: 'Not Found' });
  });
});
