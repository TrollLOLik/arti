export interface User { id: number; first_name: string; username?: string }
export interface Session { user: User; csrf_token: string; expires_at: string; mode: 'demo' | 'telegram' }
export interface Capabilities {
  chat_read: boolean; chat_send: boolean; human_chats: boolean; rooms: boolean;
  create: boolean; projects: boolean; library: boolean; organizer: boolean;
  memory: boolean; voices: boolean; history_sync: boolean; live_events?: boolean;
  /** «Сейчас у Арти», «Комната Арти» and the «Не упоминать» setting. */
  arti_life?: boolean;
}
export interface Bootstrap { mode: Session['mode']; user: User; csrf_token: string; timezone: string; bot_username: string; capabilities: Capabilities }
export interface Conversation { id: string; kind: 'arti_private'; title: string; role: 'owner'; created_at: string; updated_at: string; last_message: Message | null; message_count: number; unread_count: number; event_cursor?: number; history_ready?: boolean }
export type RequestState = 'queued' | 'running' | 'completed' | 'succeeded' | 'failed' | 'cancelled' | 'expired' | 'deadline' | 'delivery_unknown';
export interface Message { id: string; conversation_id: string; seq: number; author_id: number | null; author_kind: 'user' | 'arti' | 'system'; text: string; created_at: string; client_message_id: string | null; reply_to_id: string | null; source?: 'miniapp' | 'telegram'; request_id?: string | null; request_state?: RequestState | null; mirror_state?: 'prepared' | 'sending' | 'delivered' | 'delivery_unknown' | 'cancelled' | null
  /** Not sent by the API yet. Rendered only when present: reactions with MA07, the «написала первой» marker with MA10. */
  reactions?: Reaction[]; initiative?: boolean }
export interface Reaction { emoji: string; by: 'user' | 'arti' }
export interface MessagePage { items: Message[]; next_before: number | null; history_revision?: number }
export interface ChatRequest { id: string; state: RequestState; message_id?: string; error_code?: string }
export interface SendResult { message: Message; request: ChatRequest }
export interface ConversationEvent { seq: number; kind: 'message.created' | 'history.reset' | 'membership.changed'; resource_id: string | null; message: Message | null }
export interface EventPage { items: ConversationEvent[]; cursor: number; requests: ChatRequest[]; history_revision: number }
export interface ResourceItem { id: string; title?: string; filename?: string; goal?: string; kind?: string; state?: string; due_at?: string | null; created_at?: string; size_bytes?: number }
export interface ResourcePage { items: ResourceItem[]; supported?: boolean }
/** Her in-character life is declared fiction; reading is real; mood is from this person's own conversation. */
export interface LifeSettings { mentions: boolean; explainer_seen: boolean }
export interface LifeEvent { kind: string; summary: string; feeling: string; at: string; title: string }
export interface RestorationStage { name: string; goal: string; state: 'done' | 'current' | 'next' }
export interface Pursuit {
  kind: 'embroidery' | 'restoration' | 'tea'; title: string; status: 'active' | 'paused'; progress: number;
  intention: string | null; last_event: LifeEvent | null;
  embroidery?: { motif: string; stitched: number; total: number; replaced: { motif: string; stitched: number; total: number; reason: string | null } | null };
  restoration?: { record: { number: number; medium: string; content: string; seconds: number }; stage: number; step: number; steps: number; stages: RestorationStage[] };
  tea?: { tasted: number; known: number };
}
export interface EmbroideryPattern { number: number; motif: string; size: number; rows: string[]; palette: { thread: string; color: string }[]; stitched: number; total: number }
export type Verdict = 'да' | 'нет' | 'спорно';
export interface Taste { item: string; category: string; verdict: Verdict; reason: string; basis: string; source: string; previous: Verdict | null; at: string }
export interface ReadingNote { title: string; url: string; opinion: string; read_on: string; at: string }
export interface Mood { label: string; emotion: string | null; valence: number; arousal: number }
export interface ArtiLife { settings: LifeSettings; in_character: boolean; pursuits: Pursuit[]; embroidery: EmbroideryPattern | null; tastes: Taste[]; reading: ReadingNote | null; mood: Mood | null }
export type Tab = 'chats' | 'create' | 'space' | 'profile';
export interface Route { tab: Tab; conversation: boolean }
export type Theme = 'system' | 'light' | 'dark';

interface BackButton { show(): void; hide(): void; onClick(callback: () => void): void; offClick(callback: () => void): void }
export interface TelegramWebApp {
  initData?: string;
  colorScheme?: 'light' | 'dark';
  viewportHeight?: number;
  viewportStableHeight?: number;
  safeAreaInset?: { top: number; bottom: number; left: number; right: number };
  contentSafeAreaInset?: { top: number; bottom: number; left: number; right: number };
  ready?(): void;
  expand?(): void;
  isVersionAtLeast?(version: string): boolean;
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  openLink?(url: string): void;
  BackButton?: BackButton;
  onEvent?(event: string, callback: () => void): void;
  offEvent?(event: string, callback: () => void): void;
}
declare global { interface Window { Telegram?: { WebApp?: TelegramWebApp } } }
