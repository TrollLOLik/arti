import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { ApiError, request } from '../src/api.ts';
import { parseRoute } from '../src/navigation.ts';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('POST carries CSRF only in its header and uses the same-origin cookie session', async () => {
  globalThis.fetch = async (input, init) => {
    assert.equal(input, '/api/auth/logout');
    assert.equal(init?.credentials, 'include');
    assert.equal(init?.cache, 'no-store');
    assert.deepEqual(init?.headers, { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': 'private-test-token' });
    assert.equal(init?.body, '{}');
    return new Response('{"ok":true}', { status: 200 });
  };
  assert.deepEqual(await request('/api/auth/logout', { method: 'POST', body: {}, csrf: 'private-test-token' }), { ok: true });
});

test('arbitrary server errors never become displayed exception text', async () => {
  globalThis.fetch = async () => new Response('{"error":{"code":"unavailable","message":"secret database detail"}}', { status: 503 });
  await assert.rejects(request('/api/bootstrap'), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 503);
    assert.equal(error.code, 'unavailable');
    assert.equal(error.message.includes('secret'), false);
    return true;
  });
});

test('an expired session remains distinguishable from a network error', async () => {
  globalThis.fetch = async () => new Response('{"error":{"code":"unauthorized"}}', { status: 401 });
  await assert.rejects(request('/api/session'), error => error instanceof ApiError && error.status === 401);
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(request('/api/session'), error => error instanceof ApiError && error.status === 0 && error.code === 'network');
});

test('invalid JSON is a safe error and cancelled navigation stays cancellable', async () => {
  globalThis.fetch = async () => new Response('<html>unexpected proxy error</html>', { status: 503 });
  await assert.rejects(request('/api/bootstrap'), error => error instanceof ApiError && error.code === 'invalid_response');
  globalThis.fetch = async () => { throw new DOMException('aborted', 'AbortError'); };
  await assert.rejects(request('/api/bootstrap'), error => error instanceof DOMException && error.name === 'AbortError');
});

test('hash navigation admits only known destinations, with no token or user id parsing', () => {
  assert.deepEqual(parseRoute('#chats/arti'), { tab: 'chats', conversation: true });
  assert.deepEqual(parseRoute('#profile'), { tab: 'profile', conversation: false });
  assert.deepEqual(parseRoute('#unknown?token=sensitive'), { tab: 'chats', conversation: false });
});
