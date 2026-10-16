import type { Route, Tab } from './types';

export function parseRoute(hash: string): Route {
  const value = hash.replace(/^#\/?/, '');
  if (value === 'chats/arti') return { tab: 'chats', conversation: true };
  if (['chats', 'create', 'space', 'profile'].includes(value)) return { tab: value as Tab, conversation: false };
  return { tab: 'chats', conversation: false };
}

export function navigate(target: Tab | 'chats/arti') { window.location.hash = target; }
