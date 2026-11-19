import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Auth } from '../auth/auth';
import { ChatListToggle } from '../chats/chat-list-toggle';
import { ClusterToggle } from '../cluster/cluster-toggle';
import { TopBar } from './top-bar';

describe('TopBar', () => {
  let fixture: ComponentFixture<TopBar>;
  let http: HttpTestingController;
  let page: HTMLElement;

  const button = (label: string) =>
    Array.from(page.querySelectorAll<HTMLButtonElement>('button')).find(
      (candidate) => candidate.textContent?.trim() === label,
    );

  async function logIn() {
    TestBed.inject(Auth).login('ana@example.com', 'a long enough password').subscribe();
    http.expectOne('/api/auth/login').flush({ id: 1, email: 'ana@example.com' });
    await fixture.whenStable();
  }

  beforeEach(async () => {
    localStorage.clear();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(TopBar);
    page = fixture.nativeElement;
    await fixture.whenStable();
  });

  afterEach(() => http.verify());

  it('offers the panel’s toggle only to a logged-in user', async () => {
    expect(button('Cluster')).toBeUndefined();
    await logIn();
    expect(button('Cluster')).toBeDefined();
  });

  it('shows or hides the cluster panel, and says which', async () => {
    await logIn();
    expect(button('Cluster')?.getAttribute('aria-pressed')).toBe('true');
    button('Cluster')?.click();
    await fixture.whenStable();
    expect(TestBed.inject(ClusterToggle).shown()).toBe(false);
    expect(button('Cluster')?.getAttribute('aria-pressed')).toBe('false');
    expect(TestBed.inject(ChatListToggle).shown()).toBe(true);
  });
});
