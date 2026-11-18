import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { ChatListToggle } from './chat-list-toggle';
import { Chat } from './chats';
import { ChatsLayout } from './chats-layout';

@Component({ template: '<p class="opened">chat</p>' })
class OpenChat {}

const OLDER: Chat = { id: 3, title: 'Total revenue?', created: 1_794_950_000 };
const NEWER: Chat = { id: 7, title: 'Which clauses are in the contract?', created: 1_794_950_800 };

describe('ChatsLayout', () => {
  let http: HttpTestingController;
  let harness: RouterTestingHarness;
  let page: HTMLElement;

  const titles = () =>
    Array.from(page.querySelectorAll('nav[aria-label="Chats"] a')).map((link) =>
      link.textContent?.trim(),
    );
  const marked = () =>
    Array.from(page.querySelectorAll('nav[aria-label="Chats"] a[aria-current="page"]')).map(
      (link) => link.textContent?.trim(),
    );

  async function open(url: string, chats: Chat[] = [NEWER, OLDER]) {
    await harness.navigateByUrl(url);
    http.expectOne('/api/chats').flush(chats);
    await harness.fixture.whenStable();
  }

  beforeEach(async () => {
    localStorage.clear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([
          {
            path: 'chats',
            component: ChatsLayout,
            children: [{ path: ':id', component: OpenChat }],
          },
        ]),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    harness = await RouterTestingHarness.create();
    page = harness.routeNativeElement as HTMLElement;
  });

  afterEach(() => http.verify());

  it('lists the chats beside the open one, which it marks', async () => {
    await open('/chats/7');
    page = harness.routeNativeElement as HTMLElement;
    expect(titles()).toEqual(['Which clauses are in the contract?', 'Total revenue?']);
    expect(marked()).toEqual(['Which clauses are in the contract?']);
    expect(page.querySelector('.opened')).not.toBeNull();
  });

  it('keeps a long title whole on hover', async () => {
    await open('/chats/7');
    page = harness.routeNativeElement as HTMLElement;
    const link = page.querySelector<HTMLElement>('nav[aria-label="Chats"] a');
    expect(link?.title).toBe('Which clauses are in the contract?');
  });

  it('opens a chat from the list', async () => {
    await open('/chats/7');
    page = harness.routeNativeElement as HTMLElement;
    page.querySelectorAll<HTMLAnchorElement>('nav[aria-label="Chats"] a')[1].click();
    await harness.fixture.whenStable();
    expect(marked()).toEqual(['Total revenue?']);
  });

  it('says so when there are no chats yet', async () => {
    await open('/chats/7', []);
    page = harness.routeNativeElement as HTMLElement;
    expect(page.querySelector('nav[aria-label="Chats"]')?.textContent?.trim()).toBe(
      'No chats yet.',
    );
  });

  it('says so when the list cannot be read', async () => {
    await harness.navigateByUrl('/chats/7');
    http.expectOne('/api/chats').flush('down', { status: 502, statusText: 'Bad Gateway' });
    await harness.fixture.whenStable();
    page = harness.routeNativeElement as HTMLElement;
    expect(page.querySelector('nav[aria-label="Chats"]')?.textContent?.trim()).toBe(
      'The chats could not be read.',
    );
  });

  it('hides and shows the list with the toggle, keeping the open chat', async () => {
    await open('/chats/7');
    page = harness.routeNativeElement as HTMLElement;
    TestBed.inject(ChatListToggle).toggle();
    await harness.fixture.whenStable();
    expect(page.querySelector('nav[aria-label="Chats"]')).toBeNull();
    expect(page.querySelector('.opened')).not.toBeNull();

    TestBed.inject(ChatListToggle).toggle();
    await harness.fixture.whenStable();
    expect(titles()).toEqual(['Which clauses are in the contract?', 'Total revenue?']);
    http.expectNone('/api/chats');
  });
});
