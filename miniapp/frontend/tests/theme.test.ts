import assert from 'node:assert/strict';
import { test } from 'node:test';
import { systemTheme, updateTelegramChrome } from '../src/theme.ts';

test('ordinary browser follows both media preferences even with Telegram SDK loaded', () => {
  const browserSdk = { initData: '', colorScheme: 'light' as const };
  assert.equal(systemTheme(browserSdk, true), 'dark');
  assert.equal(systemTheme(browserSdk, false), 'light');
  assert.equal(systemTheme(undefined, true), 'dark');
});

test('a Telegram launch follows Telegram theme instead of browser media', () => {
  assert.equal(systemTheme({ initData: 'signed-launch', colorScheme: 'light' }, true), 'light');
  assert.equal(systemTheme({ initData: 'signed-launch', colorScheme: 'dark' }, false), 'dark');
});

test('outside Telegram no chrome methods are called', () => {
  const fail = () => { assert.fail('ordinary browser must not call Telegram chrome'); };
  updateTelegramChrome({ initData: '', isVersionAtLeast: () => true, setHeaderColor: fail, setBackgroundColor: fail }, '#14151a');
});

test('unsupported chrome versions are skipped and pre-6.9 headers use the safe keyword', () => {
  const calls: string[] = [];
  const methods = { initData: 'signed-launch', setHeaderColor: (color: string) => calls.push(`header:${color}`), setBackgroundColor: (color: string) => calls.push(`background:${color}`) };
  updateTelegramChrome({ ...methods, isVersionAtLeast: () => false }, '#14151a');
  assert.deepEqual(calls, []);
  updateTelegramChrome({ ...methods, isVersionAtLeast: version => version === '6.1' }, '#14151a');
  assert.deepEqual(calls, ['background:#14151a', 'header:bg_color']);
  calls.length = 0;
  updateTelegramChrome({ ...methods, isVersionAtLeast: () => true }, '#14151a');
  assert.deepEqual(calls, ['background:#14151a', 'header:#14151a']);
});
