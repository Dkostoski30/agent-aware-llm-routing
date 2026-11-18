import { TestBed } from '@angular/core/testing';
import { CHAT_LIST_STORAGE_KEY, ChatListToggle } from './chat-list-toggle';

describe('ChatListToggle', () => {
  beforeEach(() => localStorage.clear());

  const fresh = () => {
    TestBed.resetTestingModule();
    return TestBed.inject(ChatListToggle);
  };

  it('shows the chat list in a browser that remembered nothing', () => {
    expect(fresh().shown()).toBe(true);
  });

  it('hides and shows the list, and this browser remembers the last choice', () => {
    const toggle = fresh();
    toggle.toggle();
    expect(toggle.shown()).toBe(false);
    expect(fresh().shown()).toBe(false);

    const again = fresh();
    again.toggle();
    expect(again.shown()).toBe(true);
    expect(fresh().shown()).toBe(true);
  });

  it('keeps its choice under its own key', () => {
    fresh().toggle();
    expect(localStorage.getItem(CHAT_LIST_STORAGE_KEY)).toBe('hidden');
  });
});
