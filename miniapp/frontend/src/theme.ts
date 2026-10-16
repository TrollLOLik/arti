import type { TelegramWebApp } from './types';

/** Concept 04: day «бумага» and evening «лампа» paper colours for the browser and Telegram chrome. */
export const CHROME_COLOR = { light: '#f2ece3', dark: '#121012' } as const;

/** The SDK is also present in an ordinary browser, where its defaults are not a system preference. */
export function systemTheme(webapp: TelegramWebApp | undefined, prefersDark: boolean): 'light' | 'dark' {
  return webapp?.initData && webapp.colorScheme ? webapp.colorScheme : prefersDark ? 'dark' : 'light';
}

export function updateTelegramChrome(webapp: TelegramWebApp | undefined, color: string): void {
  if (!webapp?.initData || !webapp.isVersionAtLeast) return;
  try {
    if (webapp.isVersionAtLeast('6.1')) webapp.setBackgroundColor?.(color);
    if (webapp.isVersionAtLeast('6.9')) webapp.setHeaderColor?.(color);
    else if (webapp.isVersionAtLeast('6.1')) webapp.setHeaderColor?.('bg_color');
  } catch { /* Chrome colors are optional on older Telegram clients. */ }
}
