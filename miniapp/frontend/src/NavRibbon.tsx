import { useLayoutEffect, useRef } from 'react';

/**
 * The red bookmark ribbon marks the active section. It is measured from the
 * real link inside its parent nav, so it follows label widths, fonts and safe-area padding.
 */
export function NavRibbon({ active, axis }: { active: string; axis: 'x' | 'y' }) {
  const ribbon = useRef<HTMLSpanElement>(null);
  const previous = useRef<string | null>(null);
  useLayoutEffect(() => {
    const element = ribbon.current;
    // The parent's ref is attached after child layout effects, so use the DOM parent directly.
    const root = element?.parentElement;
    if (!element || !root) return;
    const place = () => {
      const item = root.querySelector<HTMLElement>(`[data-tab="${active}"]`);
      // A hidden nav (the other breakpoint) has no geometry; place it without motion once it appears.
      if (!item || root.offsetParent === null) { element.classList.remove('is-placed'); return; }
      const offset = axis === 'x' ? item.offsetLeft + item.offsetWidth / 2 + 8 : item.offsetTop + item.offsetHeight / 2 - 6;
      element.style.setProperty('--ribbon-offset', `${Math.round(offset)}px`);
      if (!element.classList.contains('is-placed')) { void element.offsetWidth; element.classList.add('is-placed'); }
    };
    const placed = element.classList.contains('is-placed');
    place();
    if (placed && previous.current !== null && previous.current !== active) {
      element.classList.remove('swing'); void element.offsetWidth; element.classList.add('swing');
    }
    previous.current = active;
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
    observer?.observe(root);
    let live = true;
    void document.fonts?.ready.then(() => { if (live) place(); });
    return () => { live = false; observer?.disconnect(); };
  }, [active, axis]);
  return <span ref={ribbon} className={`nav-ribbon ribbon-${axis}`} aria-hidden="true" />;
}
