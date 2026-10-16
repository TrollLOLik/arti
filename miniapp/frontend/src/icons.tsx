import type { ReactNode } from 'react';

/** Concept 04 line icons: 1.6 stroke, round caps. General icons still come from lucide-react. */
export interface IconProps { size?: number; className?: string }
function Icon({ size = 22, className, children }: IconProps & { children: ReactNode }) {
  return <svg className={`icon${className ? ` ${className}` : ''}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{children}</svg>;
}

export const STAR_PATH = 'M12 0C12.6 7.6 16.4 11.4 24 12 16.4 12.6 12.6 16.4 12 24 11.4 16.4 7.6 12.6 0 12 7.6 11.4 11.4 7.6 12 0Z';
/** Four-pointed star from her pupil: the sign of her presence. */
export function StarIcon(props: IconProps) { return <Icon {...props}><path d={STAR_PATH} fill="currentColor" stroke="none" /></Icon>; }
export function TabChatsIcon(props: IconProps) { return <Icon {...props}><path d="M4.4 12.1C4.4 8 7.8 4.8 12 4.8s7.6 3.2 7.6 7.3-3.4 7.2-7.6 7.2c-1.3 0-2.6-.3-3.7-.9l-3.9 1.1 1.1-3.5a6.9 6.9 0 0 1-1.1-3.9z" /></Icon>; }
export function TabCreateIcon(props: IconProps) { return <Icon {...props}><path d="M12 3.2 17 11l-5 9.8L7 11z" /><path d="M12 11v4.4" /><circle cx="12" cy="10.6" r=".9" fill="currentColor" /></Icon>; }
export function TabSpaceIcon(props: IconProps) { return <Icon {...props}><circle cx="12" cy="13.8" r="7.2" /><circle cx="12" cy="13.8" r="5.2" strokeWidth={1} opacity={.55} /><path d="M10.2 3.6h3.6v3h-3.6z" /><path d="M12 3.6V2.4" /><path d="M8.8 16.6c1.4-.6 2.3-1.7 3-3s1.7-2.4 3.4-2.9" strokeDasharray="1.7 1.5" /></Icon>; }
export function TabProfileIcon(props: IconProps) { return <Icon {...props}><circle cx="12" cy="8.4" r="3.8" /><path d="M4.6 20c1.2-3.7 4.1-5.6 7.4-5.6s6.2 1.9 7.4 5.6" /></Icon>; }
export function TelegramIcon(props: IconProps) { return <Icon {...props}><path d="M20.6 4.6 3.6 11.1l5.6 2.1 2.1 6.1 3.2-3.9 4.6 3.4z" /><path d="m9.2 13.2 11.4-8.6" /></Icon>; }
export function NotebookIcon(props: IconProps) { return <Icon {...props}><rect x="5.5" y="3.5" width="13" height="17" rx="2" /><path d="M15.2 3.6v17M9 3.6v4.6l1.3-1 1.3 1V3.6" /></Icon>; }
export function SendIcon(props: IconProps) { return <Icon {...props}><path d="M12 19.5v-15M6 10.5l6-6 6 6" /></Icon>; }
export function AttachIcon(props: IconProps) { return <Icon {...props}><path d="m19.5 11.3-7.6 7.6a5 5 0 0 1-7.1-7.1l8.1-8.1a3.3 3.3 0 0 1 4.7 4.7l-8 8a1.7 1.7 0 0 1-2.4-2.4l7.3-7.3" /></Icon>; }
export function TickIcon(props: IconProps) { return <Icon {...props}><path d="m5.5 12.6 4.2 4.2 8.8-9.6" /></Icon>; }
