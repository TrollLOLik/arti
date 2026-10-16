import { useEffect, useRef, useState } from 'react';
import { ApiError, isAborted, request } from './api';
import { createAttempt, isHistoryRevision, mergeMessages, requestMessages, sendAttempt, sendError, validateText } from './chat';
import type { SendAttempt } from './chat';
import { conversationFeed } from './liveEvents';
import type { ConnectionState } from './liveEvents';
import type { Bootstrap, ChatRequest, EventPage, Message, MessagePage } from './types';

interface State {
  messages: Message[]; next: number | null; loading: boolean; error: boolean;
  olderBusy: boolean; olderError: boolean; sending: boolean; attempt: SendAttempt | null;
  sendError: string | null; retryable: boolean; revoked: boolean; resetToken: number; acceptedToken: number;
  connection: ConnectionState; historyRevision: number | null;
}
interface Actions {
  refresh(): void; older(): Promise<void>; send(text?: string): Promise<boolean>;
}
const initial: State = { messages: [], next: null, loading: true, error: false, olderBusy: false, olderError: false, sending: false, attempt: null, sendError: null, retryable: false, revoked: false, resetToken: 0, acceptedToken: 0, connection: 'connecting', historyRevision: null };
const noop: Actions = { refresh() {}, async older() {}, async send() { return false; } };

/** Keep the list fresh too, without opening a second stream for the visible chat. */
export function useConversationPreview(id: string | undefined, enabled: boolean, onPreview: (message: Message | null) => void, onExpired: () => void) {
  useEffect(() => {
    if (!id || !enabled) return;
    let revision: number | null = null;
    let lastSeq = 0;
    let disposed = false;
    const feed = conversationFeed(id, {
      state(page) {
        if (disposed) return;
        const resetSeq = Math.max(0, ...page.items.filter(item => item.kind === 'history.reset').map(item => item.seq));
        if ((revision !== null && revision !== page.history_revision) || resetSeq) { lastSeq = 0; onPreview(null); }
        revision = page.history_revision;
        const incoming = page.items.flatMap(item => item.seq > resetSeq && item.kind === 'message.created' && item.message ? [item.message] : []);
        const latest = incoming.sort((a, b) => b.seq - a.seq)[0];
        if (latest && latest.seq >= lastSeq) { lastSeq = latest.seq; onPreview(latest); }
      },
      expired: onExpired, revoked: () => onPreview(null), resync: () => { lastSeq = 0; onPreview(null); }, connection() {},
    });
    const visibility = () => { if (document.hidden) feed.pause(); else feed.resume(); };
    visibility(); document.addEventListener('visibilitychange', visibility);
    return () => { disposed = true; feed.stop(); document.removeEventListener('visibilitychange', visibility); };
  }, [id, enabled, onPreview, onExpired]);
}

export function useConversation(id: string, bootstrap: Bootstrap, onExpired: () => void, onPreview: (message: Message | null) => void) {
  const [state, setState] = useState<State>(initial);
  const actions = useRef<Actions>(noop);
  const enabled = bootstrap.capabilities.chat_send;
  const live = bootstrap.capabilities.live_events ?? bootstrap.capabilities.history_sync;
  const csrf = bootstrap.csrf_token;
  useEffect(() => {
    let disposed = false;
    let epoch = 0;
    let revision: number | null = null;
    let messages: Message[] = [];
    let historyReady = false;
    let loadedFrom = 0;
    let buffered: Message[] = [];
    let bufferOverflow = false;
    let next: number | null = null;
    let jobStates: ChatRequest[] = [];
    let attempt: SendAttempt | null = null;
    let sending = false;
    let olderBusy = false;
    let historyController: AbortController | null = null;
    const controllers = new Set<AbortController>();
    const acknowledged = new Set<string>();
    setState(initial);
    const update = (patch: Partial<State>) => { if (!disposed) setState(previous => ({ ...previous, ...patch })); };
    function publish() {
      messages = requestMessages(messages, jobStates);
      update({ messages });
      onPreview(messages[messages.length - 1] ?? null);
    }
    function closePrivate() {
      epoch++; messages = []; attempt = null; jobStates = []; next = null; buffered = [];
      for (const controller of controllers) controller.abort();
      controllers.clear();
      feed?.stop();
      update({ messages: [], attempt: null, next: null, loading: false, sending: false, revoked: true, sendError: null, historyRevision: null });
      setState(previous => ({ ...previous, resetToken: previous.resetToken + 1 }));
      onPreview(null);
    }
    function failure(cause: unknown) {
      if (cause instanceof ApiError && cause.status === 401) { closePrivate(); onExpired(); return true; }
      if (cause instanceof ApiError && (cause.status === 403 || cause.status === 404)) { closePrivate(); return true; }
      return false;
    }
    function reset() {
      epoch++; messages = []; next = null; attempt = null; sending = false; olderBusy = false; jobStates = [];
      historyReady = false; loadedFrom = 0; buffered = []; bufferOverflow = false;
      acknowledged.clear();
      for (const controller of controllers) controller.abort();
      controllers.clear();
      update({ messages: [], next: null, loading: true, error: false, olderBusy: false, olderError: false, attempt: null, sending: false, sendError: null, retryable: false, historyRevision: null });
      setState(previous => ({ ...previous, resetToken: previous.resetToken + 1 }));
      onPreview(null);
      void history();
    }
    async function history() {
      historyController?.abort();
      const controller = new AbortController(); historyController = controller; controllers.add(controller);
      const currentEpoch = epoch;
      update({ loading: true, error: false });
      try {
        const page = await request<MessagePage>(`/api/conversations/${encodeURIComponent(id)}/messages?limit=50`, { signal: controller.signal });
        if (disposed || controller.signal.aborted || currentEpoch !== epoch) return;
        if (isHistoryRevision(page.history_revision)) {
          if (revision !== null && revision !== page.history_revision) {
            revision = page.history_revision; reset(); return;
          }
          revision = page.history_revision;
        }
        historyReady = true;
        loadedFrom = page.items[0]?.seq ?? 0;
        messages = mergeMessages(mergeMessages(messages, page.items), buffered.filter(message => message.seq >= loadedFrom));
        buffered = []; next = page.next_before;
        publish(); update({ next, loading: false, historyRevision: revision });
        if (bufferOverflow) { bufferOverflow = false; void history(); }
      } catch (cause) {
        if (disposed || controller.signal.aborted || isAborted(cause) || currentEpoch !== epoch) return;
        if (!failure(cause)) update({ error: true, loading: false });
      } finally { controllers.delete(controller); }
    }
    function events(page: EventPage) {
      if (disposed) return;
      const changed = revision !== null && revision !== page.history_revision;
      revision = page.history_revision;
      if (changed || page.items.some(event => event.kind === 'history.reset' || event.kind === 'membership.changed')) reset();
      const resetSeq = Math.max(0, ...page.items.filter(event => event.kind === 'history.reset').map(event => event.seq));
      const incoming = page.items.flatMap(event => event.seq > resetSeq && event.kind === 'message.created' && event.message ? [event.message] : []);
      for (const message of incoming) {
        if (attempt?.client_message_id === message.client_message_id) { acknowledged.add(attempt.client_message_id); attempt = null; update({ attempt: null, sendError: null, retryable: false }); setState(previous => ({ ...previous, acceptedToken: previous.acceptedToken + 1 })); }
      }
      jobStates = page.requests;
      if (historyReady) {
        messages = mergeMessages(messages, incoming.filter(message => message.seq >= loadedFrom));
        publish(); update({ historyRevision: revision });
      } else {
        buffered = mergeMessages(buffered, incoming);
        if (buffered.length > 500) { buffered = buffered.slice(-500); bufferOverflow = true; }
      }
    }
    const feed = live ? conversationFeed(id, { state: events, expired: () => { closePrivate(); onExpired(); }, revoked: closePrivate, resync: reset, connection: value => update({ connection: value }) }) : null;
    actions.current = {
      refresh() { reset(); },
      async older() {
        if (!next || olderBusy || disposed) return;
        olderBusy = true; update({ olderBusy: true, olderError: false });
        const controller = new AbortController(); controllers.add(controller); const currentEpoch = epoch;
        try {
          const page = await request<MessagePage>(`/api/conversations/${encodeURIComponent(id)}/messages?limit=50&before=${next}`, { signal: controller.signal });
          if (disposed || controller.signal.aborted || currentEpoch !== epoch) return;
          if (page.history_revision !== undefined && revision !== null && revision !== page.history_revision) { revision = page.history_revision; reset(); return; }
          loadedFrom = Math.min(loadedFrom, page.items[0]?.seq ?? loadedFrom);
          messages = mergeMessages(messages, page.items); next = page.next_before;
          publish(); update({ next });
        } catch (cause) {
          if (disposed || controller.signal.aborted || isAborted(cause) || currentEpoch !== epoch) return;
          if (!failure(cause)) update({ olderError: true });
        } finally {
          controllers.delete(controller);
          if (currentEpoch === epoch) { olderBusy = false; update({ olderBusy: false }); }
        }
      },
      async send(text) {
        if (!enabled || sending || disposed || !historyReady || !isHistoryRevision(revision)) return false;
        if (!attempt) {
          const invalid = validateText(text ?? '');
          if (invalid) { update({ sendError: invalid, retryable: false }); return false; }
          acknowledged.clear();
          attempt = createAttempt(text!, revision);
        }
        const activeAttempt = attempt;
        const currentEpoch = epoch;
        sending = true; update({ sending: true, attempt, sendError: null, retryable: false });
        const controller = new AbortController(); controllers.add(controller);
        try {
          const result = await sendAttempt(id, activeAttempt, csrf, controller.signal);
          if (disposed || controller.signal.aborted || currentEpoch !== epoch) return false;
          acknowledged.add(activeAttempt.client_message_id);
          const known = jobStates.find(job => job.id === result.request.id);
          messages = mergeMessages(messages, [{ ...result.message, request_id: result.request.id, request_state: known?.state ?? result.request.state }]);
          if (!known) jobStates = [...jobStates, { ...result.request, message_id: result.message.id }];
          attempt = null; update({ attempt: null, sendError: null, retryable: false }); publish();
          setState(previous => ({ ...previous, acceptedToken: previous.acceptedToken + 1 }));
          return true;
        } catch (cause) {
          if (disposed || controller.signal.aborted || isAborted(cause) || currentEpoch !== epoch) return false;
          if (failure(cause)) return false;
          if (cause instanceof ApiError && cause.status === 409 && cause.code === 'history_reset') { revision = null; reset(); return false; }
          if (acknowledged.has(activeAttempt.client_message_id)) { attempt = null; update({ attempt: null, sendError: null }); return true; }
          const error = sendError(cause);
          if (!error.uncertain) attempt = null;
          update({ attempt, sendError: error.text, retryable: error.uncertain });
          return false;
        } finally {
          controllers.delete(controller);
          if (currentEpoch === epoch) { sending = false; update({ sending: false }); }
        }
      },
    };
    void history();
    const visibility = () => { if (document.hidden) feed?.pause(); else feed?.resume(); };
    visibility(); document.addEventListener('visibilitychange', visibility);
    return () => {
      disposed = true; actions.current = noop; feed?.stop();
      document.removeEventListener('visibilitychange', visibility);
      for (const controller of controllers) controller.abort();
    };
  }, [id, csrf, enabled, live, onExpired, onPreview]);
  return { ...state, refresh: () => actions.current.refresh(), older: () => actions.current.older(), send: (text?: string) => actions.current.send(text) };
}
