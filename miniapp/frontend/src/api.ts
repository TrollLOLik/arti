export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(status === 401 ? 'Сессия завершена' : 'Не удалось загрузить данные');
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

/** The server's arbitrary message/body is deliberately never shown or logged. */
export async function request<T>(path: string, options: { method?: 'GET' | 'POST' | 'PUT'; body?: unknown; csrf?: string; signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (options.csrf) headers['X-CSRF-Token'] = options.csrf;
  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? 'GET', credentials: 'include', cache: 'no-store',
      headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'network');
  }
  let body: unknown;
  try { body = await response.json(); } catch { throw new ApiError(response.status, 'invalid_response'); }
  if (!response.ok) {
    const value = body as { error?: { code?: unknown } } | null;
    const code = typeof value?.error?.code === 'string' ? value.error.code : 'unavailable';
    throw new ApiError(response.status, code);
  }
  return body as T;
}

export function isAborted(error: unknown): boolean { return error instanceof DOMException && error.name === 'AbortError'; }
