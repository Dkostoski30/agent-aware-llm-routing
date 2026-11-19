import { TestBed } from '@angular/core/testing';
import { CHAT_LIST_STORAGE_KEY } from '../chats/chat-list-toggle';
import { CLUSTER_PANEL_STORAGE_KEY, ClusterToggle } from './cluster-toggle';

describe('ClusterToggle', () => {
  beforeEach(() => localStorage.clear());

  const fresh = () => {
    TestBed.resetTestingModule();
    return TestBed.inject(ClusterToggle);
  };

  it('shows the cluster panel in a browser that remembered nothing (D14)', () => {
    expect(fresh().shown()).toBe(true);
  });

  it('hides and shows the panel, and this browser remembers the last choice', () => {
    const toggle = fresh();
    toggle.toggle();
    expect(toggle.shown()).toBe(false);
    expect(fresh().shown()).toBe(false);

    const again = fresh();
    again.toggle();
    expect(again.shown()).toBe(true);
    expect(fresh().shown()).toBe(true);
  });

  it('keeps its choice under its own key, apart from the chat list’s', () => {
    fresh().toggle();
    expect(localStorage.getItem(CLUSTER_PANEL_STORAGE_KEY)).toBe('hidden');
    expect(localStorage.getItem(CHAT_LIST_STORAGE_KEY)).toBeNull();
  });
});
