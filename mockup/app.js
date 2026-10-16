/* Арти · концепция 04 «Левая рука» — интерактивный прототип.
   Vanilla JS, без модулей: открывается двойным щелчком (file://). */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ utils */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const app = $('#app');
  const deviceScreen = $('.device-screen');
  const RM = window.matchMedia('(prefers-reduced-motion: reduce)');
  const reduced = () => RM.matches;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const icon = (id, cls = 'ic') => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const store = {
    get(k) { try { return window.localStorage.getItem('arti4.' + k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem('arti4.' + k, v); } catch (e) { /* storage unavailable */ } },
  };
  const plural = (n, one, few, many) => {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  };
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  let scale = 1;
  const isMobile = () => window.innerWidth < 560;
  /** Rect of el relative to the app box, in unscaled app pixels. */
  function rel(el) {
    const a = app.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return { x: (r.left - a.left) / scale, y: (r.top - a.top) / scale, w: r.width / scale, h: r.height / scale, aw: a.width / scale, ah: a.height / scale };
  }

  /* --------------------------------------------------------------- portrait */
  const PORTRAIT = 'assets/arti-portrait-v4.webp';
  const STAR_D = 'M12 0C12.6 7.6 16.4 11.4 24 12 16.4 12.6 12.6 16.4 12 24 11.4 16.4 7.6 12.6 0 12 7.6 11.4 11.4 7.6 12 0Z';
  const CROPS = {
    avatar: { vb: '372 168 440 440', glow: 34, spark: 18 },
    eyes: { vb: '330 262 560 150', glow: 22, spark: 13 },
    room: { vb: '140 196 880 368', glow: 24, spark: 14 },
  };
  function portraitSVG(kind) {
    const c = CROPS[kind] || CROPS.avatar;
    const eye = (x, y, d) =>
      `<g class="eye-glow" style="animation-delay:${d}s"><circle class="halo" cx="${x}" cy="${y}" r="${c.glow}" fill="url(#pupilGlow)"/>` +
      `<path class="spark" d="${STAR_D}" transform="translate(${x} ${y}) scale(${(c.spark / 24).toFixed(3)}) translate(-12 -12)" fill="#fff"/></g>`;
    return `<svg viewBox="${c.vb}" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">` +
      `<image href="${PORTRAIT}" x="0" y="0" width="1145" height="1374"/>` +
      `<g class="pupils">${eye(486, 319, 0)}${eye(702, 366, 0.8)}</g></svg>`;
  }
  function paintPortraits(root = document) { $$('[data-pt]', root).forEach((el) => { el.innerHTML = portraitSVG(el.dataset.pt); }); }

  /* ------------------------------------------------------------------ toast */
  let toastT = 0;
  function toast(msg) {
    const el = $('#toast');
    el.querySelector('span').textContent = msg;
    el.classList.add('is-on');
    clearTimeout(toastT);
    toastT = setTimeout(() => el.classList.remove('is-on'), 2800);
  }

  /* ------------------------------------------------------------------ theme */
  const cord = $('#cord');
  const darkMQ = window.matchMedia('(prefers-color-scheme: dark)');
  let themeMode = 'day';
  const resolvedTheme = () => (themeMode === 'auto' ? (darkMQ.matches ? 'evening' : 'day') : themeMode);
  let themeAnimT = 0;
  function applyTheme(animate) {
    const t = resolvedTheme();
    const prev = app.dataset.theme;
    if (animate && prev !== t && !reduced()) {
      app.classList.add('theme-anim');
      clearTimeout(themeAnimT);
      themeAnimT = setTimeout(() => app.classList.remove('theme-anim'), 750);
      const g = $('.app-glow');
      g.classList.remove('flick-on', 'flick-off');
      void g.offsetWidth;
      g.classList.add(t === 'evening' ? 'flick-on' : 'flick-off');
    }
    app.dataset.theme = t;
    deviceScreen.dataset.theme = t;
    cord.setAttribute('aria-label', t === 'day' ? 'Включить вечерний свет' : 'Включить дневной свет');
    cord.title = t === 'day' ? 'Потяни — вечер' : 'Потяни — день';
    setSeg($('#themeSeg'), themeMode, 'mode');
    const lb = $('#roomLampBtn span');
    if (lb) lb.textContent = t === 'day' ? 'Включить лампу' : 'Выключить лампу';
    const mc = document.querySelector('meta[name="theme-color"]');
    if (mc && isMobile()) mc.setAttribute('content', t === 'day' ? '#F2ECE3' : '#121012');
  }
  function setThemeMode(m, opts = {}) {
    themeMode = m;
    if (opts.save !== false) store.set('theme', m);
    applyTheme(opts.animate !== false);
  }
  const toggleTheme = () => { app.classList.add('cord-used'); store.set('pulled', '1'); setThemeMode(resolvedTheme() === 'day' ? 'evening' : 'day'); };
  if (darkMQ.addEventListener) darkMQ.addEventListener('change', () => { if (themeMode === 'auto') applyTheme(true); });

  /* ------------------------------------------------------------- lamp cord */
  let drag = null;
  let dragged = false;
  function swingCord() {
    cord.classList.remove('swing');
    void cord.offsetWidth;
    cord.classList.add('swing');
  }
  cord.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    drag = { y: e.clientY };
    dragged = false;
    try { cord.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
    cord.classList.add('is-dragging');
  });
  cord.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dy = Math.max(0, (e.clientY - drag.y) / scale);
    if (dy > 3) dragged = true;
    const pull = dy < 36 ? dy : 36 + (dy - 36) * 0.3;
    cord.style.setProperty('--pull', Math.min(pull, 58).toFixed(1) + 'px');
  });
  function endDrag(e) {
    if (!drag) return;
    const dy = (e.clientY - drag.y) / scale;
    drag = null;
    cord.classList.remove('is-dragging');
    cord.style.setProperty('--pull', '0px');
    if (dragged) {
      swingCord();
      if (dy >= 24) toggleTheme();
    }
  }
  cord.addEventListener('pointerup', endDrag);
  cord.addEventListener('pointercancel', endDrag);
  cord.addEventListener('click', (e) => {
    if (dragged) { dragged = false; e.preventDefault(); return; }
    pullCord();
  });
  function pullCord() {
    if (reduced()) { toggleTheme(); return; }
    cord.style.setProperty('--pull', '30px');
    setTimeout(() => {
      cord.style.setProperty('--pull', '0px');
      swingCord();
      toggleTheme();
    }, 220);
  }

  /* ------------------------------------------------------------- segmented */
  function setSeg(seg, value, key) {
    if (!seg) return;
    const btns = $$(':scope > button', seg);
    let idx = 0;
    btns.forEach((b, i) => {
      const on = b.dataset[key] === value;
      if (on) idx = i;
      b.classList.toggle('is-on', on);
      if (b.getAttribute('role') === 'tab') b.setAttribute('aria-selected', String(on));
      else b.setAttribute('aria-checked', String(on));
    });
    seg.style.setProperty('--n', btns.length);
    seg.style.setProperty('--i', idx);
  }

  /* ------------------------------------------------------------ navigation */
  const ROOTS = ['chats', 'create', 'space', 'profile'];
  const PARENT = { arti: 'chats', anya: 'chats', person: 'chats', group: 'chats', room: 'group', new: 'chats', image: 'create', project: 'space', organizer: 'space', notebook: 'profile' };
  const VALID = ROOTS.concat(Object.keys(PARENT));
  const screens = {};
  $$('.screen').forEach((s) => { screens[s.dataset.screen] = s; });
  let stack = ['chats'];
  const current = () => stack[stack.length - 1];
  const chainOf = (id) => { const c = [id]; while (PARENT[c[0]]) c.unshift(PARENT[c[0]]); return c; };

  function transition(fromId, toId, mode) {
    const from = fromId ? screens[fromId] : null;
    const to = screens[toId];
    if (!to || from === to) return;
    const dur = reduced() ? 220 : 420;
    to.classList.remove('leave-left', 'leave-right', 'fade-out');
    clearTimeout(to._t);
    if (mode === 'push') to.classList.add('enter-right');
    else if (mode === 'pop') to.classList.add('enter-left');
    else if (mode === 'fade') to.classList.add('fade-in');
    to.classList.add('is-active');
    void to.offsetWidth;
    requestAnimationFrame(() => {
      to.classList.remove('enter-right', 'enter-left', 'fade-in');
      if (from) from.classList.add(mode === 'push' ? 'leave-left' : mode === 'pop' ? 'leave-right' : 'fade-out');
    });
    if (from) {
      clearTimeout(from._t);
      from._t = setTimeout(() => {
        if (current() === fromId) { from.classList.remove('leave-left', 'leave-right', 'fade-out'); return; }
        from.classList.remove('is-active', 'leave-left', 'leave-right', 'fade-out');
      }, dur);
    }
  }
  function go(id) {
    if (id === 'artiroom') { openSheet(); return; }
    if (id === 'splash') { playSplash(); return; }
    if (!screens[id]) return;
    if (ROOTS.includes(id)) { tab(id); return; }
    const cur = current();
    if (cur === id) { onEnter(id, false); return; }
    const idx = stack.indexOf(id);
    if (idx >= 0) {
      stack = stack.slice(0, idx + 1);
      transition(cur, id, 'pop');
    } else if (chainOf(id)[0] !== stack[0] || (PARENT[id] && !stack.includes(PARENT[id]))) {
      stack = chainOf(id);
      transition(cur, id, 'fade');
    } else {
      stack.push(id);
      transition(cur, id, 'push');
    }
    afterNav(true);
  }
  function back() {
    if (stack.length <= 1) return;
    const from = stack.pop();
    transition(from, current(), 'pop');
    afterNav(false);
  }
  function tab(id) {
    const from = current();
    stack = [id];
    if (from !== id) transition(from, id, 'fade');
    else { const sc = $('.scroll', screens[id]); if (sc) sc.scrollTo({ top: 0, behavior: reduced() ? 'auto' : 'smooth' }); }
    afterNav(from !== id);
  }
  function openDirect(id) {
    stack = chainOf(id);
    Object.values(screens).forEach((s) => s.classList.remove('is-active', 'enter-right', 'enter-left', 'leave-left', 'leave-right', 'fade-in', 'fade-out'));
    screens[id].classList.add('is-active');
    afterNav(true);
  }
  function afterNav(entered) {
    const id = current();
    const isRoot = ROOTS.includes(id);
    // keyboard focus follows the visible screen (the old one becomes inert)
    const ae = document.activeElement;
    const aeScreen = ae && ae.closest ? ae.closest('.screen') : null;
    if (aeScreen && aeScreen !== screens[id]) {
      screens[id].setAttribute('tabindex', '-1');
      screens[id].focus({ preventScroll: true });
    }
    syncInert();
    document.body.classList.toggle('has-tabs', isRoot);
    syncCordTuck();
    app.classList.toggle('no-tabs', !isRoot);
    cord.classList.toggle('hide', !isRoot);
    cord.tabIndex = isRoot ? 0 : -1;
    setTabs(stack[0]);
    store.set('screen', id);
    markMap(sheetOpen ? 'artiroom' : id);
    closePicker();
    closeStyleMenu();
    document.body.dataset.screen = id;
    if (entered) onEnter(id, true);
  }
  /** Only the visible screen is reachable by keyboard / screen readers. */
  function syncInert() {
    const cur = current();
    const blocked = sheetOpen || !splash.hidden;
    Object.keys(screens).forEach((k) => { screens[k].inert = blocked || k !== cur; });
    $('#tabbar').inert = blocked;
    cord.inert = blocked;
  }
  function syncCordTuck() {
    const sc = $('.scroll', screens[current()]);
    app.classList.toggle('cord-tucked', ROOTS.includes(current()) && !!sc && sc.scrollTop > 8);
  }
  function setTabs(rootId) {
    $$('.tab').forEach((t) => {
      const on = t.dataset.tab === rootId;
      t.classList.toggle('is-on', on);
      if (on) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
    });
    placeRibbon(true);
  }
  let lastRibbon = null;
  function placeRibbon(animate) {
    const on = $('.tab.is-on');
    const rb = $('#tabRibbon');
    if (!on || !rb) return;
    const x = on.offsetLeft + on.offsetWidth / 2 + 8;
    rb.style.setProperty('--rx', x + 'px');
    if (animate && lastRibbon !== null && lastRibbon !== on.dataset.tab && !reduced()) {
      rb.classList.remove('swing');
      void rb.offsetWidth;
      rb.classList.add('swing');
    }
    lastRibbon = on.dataset.tab;
  }

  const enterHooks = {};
  function onEnter(id, fresh) { if (enterHooks[id]) enterHooks[id](fresh); }

  /* ------------------------------------------------------------- delegation */
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-go],[data-tab],[data-back],[data-open],[data-toast],[data-close-sheet],[data-jump],[data-demo]');
    if (!t) return;
    if (t.hasAttribute('data-back')) back();
    else if (t.dataset.go) {
      if (t.dataset.person) setPerson(t.dataset.person);
      if (sheetOpen) closeSheet(true);
      go(t.dataset.go);
    } else if (t.dataset.tab) {
      if (sheetOpen) closeSheet(true);
      tab(t.dataset.tab);
      if (t.dataset.spaceTab) setSpaceTab(t.dataset.spaceTab);
    } else if (t.dataset.open === 'artiroom') openSheet();
    else if (t.dataset.toast) toast(t.dataset.toast);
    else if (t.hasAttribute('data-close-sheet')) closeSheet();
    else if (t.dataset.jump) jump(t.dataset.jump);
    else if (t.dataset.demo) demo(t.dataset.demo);
  });

  /* ------------------------------------------------------------- sheet */
  const sheetWrap = $('#roomSheet');
  const sheet = $('.sheet', sheetWrap);
  let sheetOpen = false;
  let sheetReturn = null;
  let sheetT = 0;
  function openSheet() {
    if (sheetOpen) return;
    sheetOpen = true;
    sheetReturn = document.activeElement;
    clearTimeout(sheetT);
    sheetWrap.hidden = false;
    // Правила жизни в образе объясняются один раз, при первом входе в Комнату.
    if (!store.get('lifeExplained')) showExplainer(true);
    sheet.style.removeProperty('--drag');
    void sheetWrap.offsetWidth;
    sheetWrap.classList.add('is-open');
    syncInert();
    setTimeout(() => sheet.focus({ preventScroll: true }), 60);
    markMap('artiroom');
    closePicker();
  }
  function closeSheet(silent) {
    if (!sheetOpen) return;
    sheetOpen = false;
    sheetWrap.classList.remove('is-open');
    sheet.style.removeProperty('--drag');
    stopPlayers(sheetWrap);
    sheetT = setTimeout(() => { if (!sheetOpen) sheetWrap.hidden = true; }, reduced() ? 220 : 460);
    syncInert();
    if (!silent && sheetReturn && sheetReturn.focus && document.contains(sheetReturn)) sheetReturn.focus({ preventScroll: true });
    markMap(current());
  }
  (function sheetDrag() {
    const grab = $('#sheetGrab');
    let sy = null;
    grab.addEventListener('pointerdown', (e) => { sy = e.clientY; try { grab.setPointerCapture(e.pointerId); } catch (er) { /* noop */ } sheet.classList.add('is-dragging'); });
    grab.addEventListener('pointermove', (e) => { if (sy === null) return; const dy = Math.max(0, (e.clientY - sy) / scale); sheet.style.setProperty('--drag', dy + 'px'); });
    const end = (e) => {
      if (sy === null) return;
      const dy = (e.clientY - sy) / scale;
      sy = null;
      sheet.classList.remove('is-dragging');
      if (dy > 110) closeSheet(); else sheet.style.setProperty('--drag', '0px');
    };
    grab.addEventListener('pointerup', end);
    grab.addEventListener('pointercancel', end);
  })();
  // keep keyboard focus inside the sheet while it is open
  sheet.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const f = $$('button, [tabindex="0"], textarea, input', sheet).filter((el) => !el.disabled && el.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === sheet)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  $('#roomLampBtn').addEventListener('click', () => { swingCord(); toggleTheme(); });

  /* ------------------------------------------------------------- splash */
  const splash = $('#splash');
  let splashT = 0;
  function playSplash() {
    clearTimeout(splashT);
    if (sheetOpen) closeSheet(true);
    splash.hidden = false;
    splash.classList.remove('run', 'out', 'static');
    deviceScreen.classList.add('splashing');
    syncInert();
    void splash.offsetWidth;
    if (reduced()) {
      splash.classList.add('static');
      splashT = setTimeout(endSplash, 900);
    } else {
      splash.classList.add('run');
      splashT = setTimeout(endSplash, 1800);
    }
    markMap('splash');
    setTimeout(() => $('.splash-skip').focus({ preventScroll: true }), 30);
  }
  function endSplash() {
    clearTimeout(splashT);
    if (splash.hidden) return;
    splash.classList.add('out');
    setTimeout(() => {
      splash.hidden = true;
      splash.classList.remove('run', 'out', 'static');
      deviceScreen.classList.remove('splashing');
      syncInert();
      app.classList.remove('arrive');
      void app.offsetWidth;
      if (!reduced()) app.classList.add('arrive');
      markMap(sheetOpen ? 'artiroom' : current());
    }, reduced() ? 0 : 240);
  }
  $('.splash-skip').addEventListener('click', endSplash);
  $('#tgClose').addEventListener('click', playSplash);

  /* ------------------------------------------------------------- chats */
  // frame: 'image' — жизнь персонажа в образе; 'real' — настоящее чтение со ссылкой.
  const STATES = [
    { frame: 'image', line: 'Реставрирую Запись № 7. Не хватает одной фразы — и меня это раздражает больше, чем следует.', status: 'реставрирует запись' },
    { frame: 'image', line: 'Вышиваю. Сменила узор: прежний был не мой.', status: 'вышивает' },
    { frame: 'real', line: 'Читала, как чай возили клиперами через полмира. Скорость ради вкуса — это я понимаю.', status: 'читает' },
    { frame: 'image', line: 'Завариваю бергамот. Проверяю, нравится ли он мне на самом деле.', status: 'заваривает бергамот' },
  ];
  const FRAME_LABEL = { image: 'В образе', real: 'Читала · источник ↗' };
  let stateIdx = 0;
  let artiTyping = false;
  let lifeOn = true;
  function showState(i, animate) {
    stateIdx = i;
    const st = STATES[i];
    const pres = $('.presence');
    const line = $('#presenceLine');
    const frame = $('#presenceFrame');
    const apply = () => {
      line.textContent = st.line;
      frame.textContent = FRAME_LABEL[st.frame];
      frame.dataset.frame = st.frame;
      line.classList.remove('swap');
    };
    if (animate && !reduced()) {
      pres.classList.remove('blink');
      void pres.offsetWidth;
      pres.classList.add('blink');
      line.classList.add('swap');
      setTimeout(apply, 480);
    } else {
      apply();
    }
    if (!artiTyping) $('#artiStatus').textContent = st.status;
  }
  function rotatePresence() {
    if (document.hidden) return;
    let next = stateIdx;
    for (let n = 0; n < STATES.length; n++) {
      next = (next + 1) % STATES.length;
      if (lifeOn || STATES[next].frame === 'real') break;
    }
    if (next !== stateIdx) showState(next, true);
  }
  setInterval(rotatePresence, 6000);

  const LIFE_HINTS = {
    on: 'Чай, вышивка и запись — жизнь её персонажа. Если выключить, Арти не будет о них рассказывать, а в Комнате останутся чтение и настроение.',
    off: 'Арти не рассказывает о жизни в образе. В Комнате — только прочитанное по-настоящему и настроение.',
  };
  function setLife(value, persist) {
    lifeOn = value !== 'off';
    app.classList.toggle('life-off', !lifeOn);
    setSeg($('#lifeSeg'), lifeOn ? 'on' : 'off', 'life');
    $('#lifeHint').textContent = LIFE_HINTS[lifeOn ? 'on' : 'off'];
    $('#roomKicker').textContent = lifeOn ? 'Жизнь в образе · ведётся последовательно' : 'Сейчас у Арти';
    if (!lifeOn && STATES[stateIdx].frame !== 'real') showState(STATES.findIndex((s) => s.frame === 'real'), false);
    if (persist) store.set('life', lifeOn ? 'on' : 'off');
  }
  $$('#lifeSeg > button').forEach((b) => b.addEventListener('click', () => setLife(b.dataset.life, true)));

  function showExplainer(open) {
    $('#lifeExplainer').hidden = !open;
    $('#lifeHowBtn').setAttribute('aria-expanded', String(open));
  }
  $('#lifeHowBtn').addEventListener('click', () => showExplainer($('#lifeExplainer').hidden));
  $('#lifeExplainerOk').addEventListener('click', () => {
    showExplainer(false);
    store.set('lifeExplained', '1');
    $('#lifeHowBtn').focus({ preventScroll: true });
  });
  $('#readLink').addEventListener('click', () => toast('В прототипе ссылка не открывается — здесь будет настоящий источник.'));

  function timeNote() {
    const h = new Date().getHours();
    if (h >= 5 && h < 11) return 'ты сегодня рано';
    if (h >= 11 && h < 17) return 'день в разгаре — садись';
    if (h >= 17 && h < 23) return 'ты сегодня поздно';
    return 'не спится? мне тоже';
  }
  $('#timeNote').textContent = timeNote();

  let chatFilter = 'all';
  function applyChatFilter() {
    const q = $('#chatSearch').value.trim().toLowerCase();
    let n = 0;
    $$('#chatList .chat-item').forEach((li) => {
      const ok = (chatFilter === 'all' || li.dataset.kind === chatFilter) && (!q || li.dataset.text.includes(q));
      li.hidden = !ok;
      if (ok) n++;
    });
    $('#chatEmpty').hidden = n > 0;
  }
  $('#chatSearch').addEventListener('input', applyChatFilter);
  $$('#chatFilter .chip').forEach((c) => c.addEventListener('click', () => {
    chatFilter = c.dataset.filter;
    $$('#chatFilter .chip').forEach((x) => { const on = x === c; x.classList.toggle('is-on', on); x.setAttribute('aria-selected', String(on)); });
    applyChatFilter();
  }));

  /* ------------------------------------------------------------- players */
  const players = [];
  function seeded(seed) { let s = seed; return () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646; }; }
  function buildPlayer(el) {
    if (el._player) return el._player;
    const dur = +el.dataset.dur || 30;
    const gap = (el.dataset.gap || '').split('-').map(Number);
    const g0 = gap[0] || 0, g1 = gap[1] || 0;
    const kind = el.classList.contains('player--found') ? 'found' : el.classList.contains('player--voice') ? 'voice' : 'gap';
    const title = el.dataset.title || '';
    const N = kind === 'voice' ? 32 : 50;
    const rnd = seeded(kind === 'voice' ? 23 : 7);
    let bars = '';
    for (let i = 0; i < N; i++) {
      const t = ((i + 0.5) / N) * dur;
      const inGap = g1 && t >= g0 && t < g1;
      let h = 0.22 + 0.78 * Math.min(1, Math.abs(Math.sin(i * 0.47) * 0.55 + rnd() * 0.6));
      if (kind === 'found' && inGap) h = Math.max(h, 0.7);
      bars += `<i class="${inGap ? (kind === 'found' ? 'is-found' : 'is-gap') : ''}" style="--h:${h.toFixed(2)}"></i>`;
    }
    const gc = g1 ? ((g0 + g1) / 2 / dur) * 100 : 0;
    const gapMark = g1
      ? (kind === 'found'
        ? `<span class="gap-q" style="left:${gc}%">${icon('star')}</span>`
        : `<span class="gap-q" style="left:${gc}%">?</span><span class="gap-say" style="left:${gc}%">тут тишина</span>`)
      : '';
    el.innerHTML =
      `<button type="button" class="play" aria-label="Слушать${title ? ': ' + esc(title) : ''}">${icon('play', 'ic i-play')}${icon('pause', 'ic i-pause')}</button>` +
      `<span class="p-col">${title ? `<span class="pl-title mono">${esc(title)} · ${fmt(dur)}</span>` : ''}` +
      `<span class="wave" role="slider" tabindex="0" aria-label="Позиция воспроизведения" aria-valuemin="0" aria-valuemax="${dur}" aria-valuenow="0" aria-valuetext="0:00 из ${fmt(dur)}">` +
      `<span class="bars">${bars}</span><span class="bars played" aria-hidden="true">${bars}</span>${gapMark}<span class="playhead"></span></span></span>` +
      `<span class="ptime"><span class="cur">0:00</span>${title ? '' : ' / ' + fmt(dur)}</span>`;
    const p = { el, dur, g0, g1, kind, t: 0, playing: false, raf: 0, last: 0, cur: $('.cur', el), wave: $('.wave', el), btn: $('.play', el) };
    el._player = p;
    players.push(p);
    p.btn.addEventListener('click', () => (p.playing ? pause(p) : play(p)));
    p.wave.addEventListener('pointerdown', (e) => { seekAt(p, e.clientX); });
    p.wave.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { setPos(p, Math.min(p.dur, p.t + 2)); e.preventDefault(); }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { setPos(p, Math.max(0, p.t - 2)); e.preventDefault(); }
      else if (e.key === 'Home') { setPos(p, 0); e.preventDefault(); }
      else if (e.key === 'End') { setPos(p, p.dur); e.preventDefault(); }
      else if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); p.playing ? pause(p) : play(p); }
    });
    return p;
  }
  function seekAt(p, clientX) {
    const r = p.wave.getBoundingClientRect();
    setPos(p, Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * p.dur);
  }
  function setPos(p, t) {
    p.t = t;
    p.el.style.setProperty('--p', (t / p.dur).toFixed(4));
    p.el.classList.toggle('has-pos', t > 0 || p.playing);
    p.cur.textContent = fmt(t);
    p.wave.setAttribute('aria-valuenow', String(Math.round(t)));
    p.wave.setAttribute('aria-valuetext', `${fmt(t)} из ${fmt(p.dur)}`);
    p.el.classList.toggle('in-gap', !!p.g1 && p.kind === 'gap' && t >= p.g0 && t < p.g1 + 1.2);
  }
  function play(p) {
    players.forEach((o) => { if (o !== p && o.playing) pause(o); });
    if (p.t >= p.dur) setPos(p, 0);
    p.playing = true;
    p.el.classList.add('is-playing', 'has-pos');
    p.btn.setAttribute('aria-label', 'Пауза');
    p.last = performance.now();
    const step = (now) => {
      if (!p.playing) return;
      const dt = (now - p.last) / 1000;
      p.last = now;
      const nt = p.t + dt;
      if (nt >= p.dur) { setPos(p, p.dur); pause(p); setTimeout(() => { if (!p.playing) setPos(p, 0); }, 900); return; }
      setPos(p, nt);
      p.raf = requestAnimationFrame(step);
    };
    p.raf = requestAnimationFrame(step);
  }
  function pause(p) {
    p.playing = false;
    cancelAnimationFrame(p.raf);
    p.el.classList.remove('is-playing');
    p.btn.setAttribute('aria-label', 'Слушать');
  }
  function stopPlayers(root) { players.forEach((p) => { if (!root || root.contains(p.el)) pause(p); }); }

  /* ------------------------------------------------------------- threads */
  const threads = {
    arti: { el: $('#artiThread'), clock: [19, 41] },
    anya: { el: $('#anyaThread'), clock: [18, 36] },
    group: { el: $('#groupThread'), clock: [17, 58] },
    person: { el: $('#personThread'), clock: [17, 24] },
  };
  function tick(th) {
    let [h, m] = th.clock;
    m += 1;
    if (m >= 60) { m = 0; h = (h + 1) % 24; }
    th.clock = [h, m];
    return `${h}:${String(m).padStart(2, '0')}`;
  }
  function scrollThread(el, smooth = true) {
    requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: smooth && !reduced() ? 'smooth' : 'auto' }));
  }
  function addReactBtn(row) {
    if (row.querySelector('.react-btn') || row.classList.contains('typing')) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'react-btn';
    b.setAttribute('aria-label', 'Поставить реакцию');
    b.innerHTML = icon('smile');
    b.addEventListener('click', (e) => { e.stopPropagation(); openPicker(row.querySelector('.bubble')); });
    row.appendChild(b);
  }
  function addMsg(key, who, html, opts = {}) {
    const th = threads[key];
    const time = opts.time || tick(th);
    const row = document.createElement('div');
    row.className = `msg ${who}${opts.cls ? ' ' + opts.cls : ''}${opts.anim === false ? '' : ' is-new'}`;
    const meta = `<span class="meta"><span class="mono">${time}</span>${who === 'me' ? icon('check', 'ic ic-xs tick2') : ''}</span>`;
    const pre = key === 'group' && who === 'arti' ? `<span class="avatar avatar-xs"><span class="pt">${portraitSVG('avatar')}</span></span>` : '';
    const head = key === 'group' && who === 'arti' ? '<span class="ribbon-mark" aria-hidden="true"></span><span class="sender">Арти <span class="ai-tag mono">ИИ</span></span>' : '';
    row.innerHTML = `${pre}<div class="bubble" tabindex="0">${head}${html}${opts.noMeta ? '' : meta}</div>`;
    if (key === 'group' && who === 'arti') row.classList.add('in-group');
    const end = th.el.querySelector('.live-end');
    th.el.insertBefore(row, end);
    $$('.player', row).forEach(buildPlayer);
    addReactBtn(row);
    scrollThread(th.el);
    return row;
  }
  function sysNote(key, text) {
    const th = threads[key];
    const row = document.createElement('div');
    row.className = 'sys-note is-new';
    row.innerHTML = `<span>${esc(text)}</span>`;
    th.el.insertBefore(row, th.el.querySelector('.live-end'));
    scrollThread(th.el);
  }
  function setArtiStatus(text, typing) {
    const s = $('#artiStatus');
    s.textContent = text || STATES[stateIdx].status;
    s.classList.toggle('is-typing', !!typing);
  }
  function putStamp(row, r) {
    const b = row.querySelector('.bubble');
    if (!b || b.querySelector('.stamp')) return;
    const s = document.createElement('span');
    s.className = 'stamp';
    s.setAttribute('role', 'img');
    s.setAttribute('aria-label', r === '✦' ? 'Арти отметила звездой' : 'Арти отметила сердцем');
    s.innerHTML = r === '✦' ? icon('star') : `<span aria-hidden="true">${r}</span>`;
    b.appendChild(s);
  }
  const TYPING_STEPS = ['подбирает слово…', 'сверяется с тетрадью…', 'пишет…'];
  async function artiReply(key, text, stampRow, stamp) {
    const th = threads[key];
    if (stampRow && stamp) { await wait(reduced() ? 150 : 520); putStamp(stampRow, stamp); await wait(reduced() ? 150 : 520); }
    else await wait(reduced() ? 150 : 380);
    const typing = document.createElement('div');
    typing.className = `msg arti typing is-new${key === 'group' ? ' in-group' : ''}`;
    typing.innerHTML = `${key === 'group' ? `<span class="avatar avatar-xs"><span class="pt">${portraitSVG('avatar')}</span></span>` : ''}<div class="bubble" aria-live="polite"><span class="breath">${icon('star')}</span><span class="hand typing-text">${TYPING_STEPS[0]}</span></div>`;
    th.el.insertBefore(typing, th.el.querySelector('.live-end'));
    scrollThread(th.el);
    if (key === 'arti') { artiTyping = true; setArtiStatus('печатает…', true); }
    const tt = $('.typing-text', typing);
    for (let i = 1; i < TYPING_STEPS.length; i++) {
      await wait(reduced() ? 350 : 980);
      tt.classList.add('swap');
      await wait(reduced() ? 0 : 200);
      tt.textContent = TYPING_STEPS[i];
      tt.classList.remove('swap');
    }
    await wait(reduced() ? 350 : 900);
    typing.remove();
    const row = addMsg(key, 'arti', esc(text));
    if (key === 'arti') { artiTyping = false; setArtiStatus(null, false); }
    return row;
  }
  let replyChain = Promise.resolve();
  const queueReply = (key, text, stampRow, stamp) => { replyChain = replyChain.then(() => artiReply(key, text, stampRow, stamp)).catch(() => {}); return replyChain; };

  const REPLIES = [
    'Возражение принято. Мне даже понравилось. Не порть момент повторением.',
    'Сверилась с тетрадью: ты просил сначала спрашивать. Спрашиваю — красный свет или янтарный?',
    'Хороший выбор. Я бы сделала так же — но мне приятнее, что выбрала ты.',
  ];
  const GROUP_REPLIES = [
    'Записала. Кофе — в 12:30, без компромиссов.',
    'Добавила в план. Ранний подъём по-прежнему исключён — я помню.',
  ];
  let replyIdx = 0, sendCount = 0, groupIdx = 0, roomArtiOn = true;
  function send(key, text) {
    const html = esc(text).replace(/@Арти/gi, '<span class="mention">@Арти</span>');
    const row = addMsg(key, 'me', html);
    if (key === 'arti') {
      sendCount += 1;
      const stamp = sendCount % 2 === 1 ? (sendCount % 4 === 1 ? '✦' : '♥') : null;
      const reply = REPLIES[replyIdx % REPLIES.length];
      replyIdx += 1;
      queueReply('arti', reply, row, stamp);
    } else if (key === 'group') {
      if (/@арти/i.test(text)) {
        if (roomArtiOn) { queueReply('group', GROUP_REPLIES[groupIdx % GROUP_REPLIES.length], row, '✦'); groupIdx += 1; }
        else setTimeout(() => sysNote('group', 'Арти сейчас не отвечает в этой комнате. Включить можно в «О комнате».'), 400);
      }
    }
    return row;
  }

  $$('.composer').forEach((form) => {
    const ta = $('textarea', form);
    const key = form.dataset.thread;
    const sendBtn = $('.send', form);
    const sync = () => {
      const has = ta.value.trim().length > 0;
      form.classList.toggle('has-text', has);
      sendBtn.setAttribute('aria-label', has ? 'Отправить' : 'Записать голосовое');
      ta.style.height = 'auto';
      ta.style.height = Math.min(Math.max(ta.scrollHeight + 2, 42), 120) + 'px';
    };
    form._sync = sync;
    ta.addEventListener('input', sync);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true })); }
    });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const v = ta.value.trim();
      if (!v) { toast('Удерживай кнопку, чтобы записать голосовое.'); return; }
      ta.value = '';
      sync();
      send(key, v);
    });
  });

  /* ------------------------------------------------------------- reactions */
  const picker = $('#picker');
  let pickTarget = null;
  let lpTimer = 0, lpStart = null, suppressClick = false;
  function openPicker(bubble) {
    if (!bubble) return;
    pickTarget = bubble;
    picker.hidden = false;
    const r = rel(bubble);
    const pw = picker.offsetWidth || 250, ph = picker.offsetHeight || 50;
    let top = r.y - ph - 8;
    if (top < 64) top = r.y + r.h + 8;
    top = Math.min(top, r.ah - ph - 80);
    const isMe = bubble.closest('.msg').classList.contains('me');
    let left = isMe ? r.x + r.w - pw : r.x;
    left = Math.max(8, Math.min(left, r.aw - pw - 8));
    picker.style.top = top + 'px';
    picker.style.left = left + 'px';
    const first = $('button', picker);
    setTimeout(() => first.focus({ preventScroll: true }), 10);
  }
  function closePicker(restore) {
    if (picker.hidden) return;
    picker.hidden = true;
    if (restore && pickTarget) pickTarget.focus({ preventScroll: true });
    pickTarget = null;
  }
  $$('button', picker).forEach((b, i, all) => {
    b.addEventListener('click', () => { if (pickTarget) addReaction(pickTarget, b.dataset.r); closePicker(true); });
    b.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight') { all[(i + 1) % all.length].focus(); e.preventDefault(); }
      if (e.key === 'ArrowLeft') { all[(i - 1 + all.length) % all.length].focus(); e.preventDefault(); }
    });
  });
  function addReaction(bubble, r) {
    let box = $('.reacts', bubble);
    if (!box) { box = document.createElement('span'); box.className = 'reacts'; const meta = $(':scope > .meta', bubble); bubble.insertBefore(box, meta ? meta.nextSibling : null); }
    const ex = $$('.react-chip', box).find((c) => c.dataset.r === r);
    if (ex) { ex.remove(); if (!box.children.length) box.remove(); return; }
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'react-chip';
    chip.dataset.r = r;
    chip.setAttribute('aria-label', `Ваша реакция ${r === '✦' ? 'звезда' : r}. Убрать`);
    chip.innerHTML = (r === '✦' ? icon('star') : `<span aria-hidden="true">${r}</span>`) + '<span class="who">вы</span>';
    chip.addEventListener('click', (e) => { e.stopPropagation(); chip.remove(); if (!box.children.length) box.remove(); });
    box.appendChild(chip);
  }
  const bubbleOf = (el) => (el && el.closest ? el.closest('.thread .msg:not(.typing) .bubble') : null);
  document.addEventListener('pointerdown', (e) => {
    const b = bubbleOf(e.target);
    if (!b || e.button !== 0 || e.target.closest('button, .wave, a')) return;
    lpStart = { x: e.clientX, y: e.clientY };
    clearTimeout(lpTimer);
    lpTimer = setTimeout(() => { suppressClick = true; openPicker(b); }, 480);
  });
  document.addEventListener('pointermove', (e) => { if (lpStart && Math.hypot(e.clientX - lpStart.x, e.clientY - lpStart.y) > 8) { clearTimeout(lpTimer); lpStart = null; } });
  document.addEventListener('pointerup', () => { clearTimeout(lpTimer); lpStart = null; });
  document.addEventListener('pointercancel', () => { clearTimeout(lpTimer); lpStart = null; });
  document.addEventListener('contextmenu', (e) => { const b = bubbleOf(e.target); if (!b) return; e.preventDefault(); openPicker(b); });
  document.addEventListener('click', (e) => {
    if (suppressClick) { suppressClick = false; return; }
    if (!picker.hidden && !picker.contains(e.target) && !e.target.closest('.react-btn')) closePicker();
  }, true);
  document.addEventListener('keydown', (e) => {
    const b = bubbleOf(document.activeElement);
    if (b && document.activeElement === b && (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10') || e.key === 'Enter')) { e.preventDefault(); openPicker(b); }
  });

  /* ------------------------------------------------------------- deck */
  const deck = $('#deck');
  const card3 = $('#card3');
  $$('.card[data-choice]', deck).forEach((c) => c.addEventListener('click', () => {
    if (deck.classList.contains('is-decided')) return;
    deck.classList.add('is-decided');
    c.classList.add('is-picked');
    c.setAttribute('aria-pressed', 'true');
    setTimeout(() => {
      const row = addMsg('arti', 'me', esc(c.dataset.choice));
      queueReply('arti', 'Хороший выбор. Я бы сделала так же — но мне приятнее, что выбрала ты.', row, '✦');
    }, reduced() ? 50 : 480);
  }));
  card3.addEventListener('click', () => {
    if (!card3.classList.contains('is-flipped')) {
      card3.classList.add('is-flipped');
      card3.setAttribute('aria-label', 'Твой вариант: «Я бы…». Да. Их не только два.');
      setTimeout(focusComposerWithPrompt, reduced() ? 0 : 700);
    } else focusComposerWithPrompt();
  });
  function focusComposerWithPrompt() {
    const form = $('.composer[data-thread="arti"]');
    const ta = $('textarea', form);
    if (!ta.value.trim()) ta.value = 'Я бы ';
    form._sync();
    ta.focus({ preventScroll: true });
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  function resetDeck() {
    deck.classList.remove('is-decided');
    $$('.card', deck).forEach((c) => { c.classList.remove('is-picked', 'is-flipped'); c.removeAttribute('aria-pressed'); });
    card3.setAttribute('aria-label', 'Третий — твой. Перевернуть карту');
  }

  /* ------------------------------------------------------------- people chats */
  const PEOPLE = {
    max: {
      name: 'Макс', av: 'М', cls: 'av-sage', status: 'был недавно', clock: [17, 24],
      html: `<div class="date-sep"><span class="mono">Сегодня</span></div>
        <div class="msg them"><div class="bubble" tabindex="0">Как прошла встреча? Расскажи голосом, если не лень.<span class="meta"><span class="mono">17:10</span></span></div></div>
        <div class="msg me"><div class="bubble voice-bubble" tabindex="0"><span class="player player--voice" data-dur="18"></span><span class="meta"><span class="mono">17:24</span>${icon('check', 'ic ic-xs tick2')}</span></div></div>`,
    },
    dasha: {
      name: 'Даша', av: 'Д', cls: 'av-sand', status: 'была вчера', clock: [21, 12],
      html: `<div class="date-sep"><span class="mono">Вчера</span></div>
        <div class="msg me"><div class="bubble" tabindex="0">Отправила наброски. Посмотри, когда будет минутка.<span class="meta"><span class="mono">21:05</span>${icon('check', 'ic ic-xs tick2')}</span></div></div>
        <div class="msg them"><div class="bubble" tabindex="0">Спасибо! Уже смотрю наброски<span class="meta"><span class="mono">21:12</span></span></div></div>`,
    },
  };
  let personKey = null;
  function setPerson(k) {
    const p = PEOPLE[k];
    if (!p || personKey === k) return;
    personKey = k;
    stopPlayers($('#personThread'));
    const av = $('#personAv');
    av.className = `avatar avatar-sm mono-av ${p.cls}`;
    av.textContent = p.av;
    $('#personName').textContent = p.name;
    $('#personStatus').textContent = p.status;
    screens.person.setAttribute('aria-label', `Чат: ${p.name}`);
    $('.composer[data-thread="person"] textarea').setAttribute('aria-label', `Сообщение: ${p.name}`);
    threads.person.clock = p.clock.slice();
    const th = $('#personThread');
    th.innerHTML = p.html + '<div class="live-end" aria-hidden="true"></div>';
    $$('.player', th).forEach(buildPlayer);
    $$('.msg', th).forEach(addReactBtn);
  }

  /* ------------------------------------------------------------- room & new */
  const roomSw = $('#roomArtiSwitch');
  roomSw.addEventListener('click', () => {
    roomArtiOn = !roomArtiOn;
    roomSw.classList.toggle('is-on', roomArtiOn);
    roomSw.setAttribute('aria-checked', String(roomArtiOn));
    toast(roomArtiOn ? 'Арти снова отвечает на @Арти в этой комнате.' : 'Арти не будет отвечать в этой комнате, пока не включите.');
  });
  const remSw = $('#remSwitch');
  remSw.addEventListener('click', () => {
    const on = !remSw.classList.contains('is-on');
    remSw.classList.toggle('is-on', on);
    remSw.setAttribute('aria-checked', String(on));
    toast(on ? 'Напоминания включены.' : 'Напоминания выключены. Дела останутся в списке.');
  });
  $$('#newTabs > button').forEach((b) => b.addEventListener('click', () => {
    setSeg($('#newTabs'), b.dataset.newtab, 'newtab');
    $$('.new-panel').forEach((p) => { p.hidden = p.dataset.newpanel !== b.dataset.newtab; });
  }));
  function updatePick() {
    const n = $$('#peoplePick .person.is-on').length;
    $('#pickCount').textContent = `Выбрано ${n}`;
    const cg = $('#createGroup');
    cg.disabled = n === 0;
  }
  $$('#peoplePick .person').forEach((p) => p.addEventListener('click', () => {
    const on = !p.classList.contains('is-on');
    p.classList.toggle('is-on', on);
    p.setAttribute('aria-pressed', String(on));
    updatePick();
  }));
  $('#peopleSearch').addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    $$('#peoplePick li').forEach((li) => { li.hidden = q && !$('.person', li).dataset.name.includes(q); });
  });
  $('#createGroup').addEventListener('click', () => {
    const name = $('#groupName').value.trim() || 'Новая группа';
    go('group');
    toast(`Группа «${name}» готова. Позовите @Арти, когда понадобится.`);
  });

  /* ------------------------------------------------------------- hoops */
  const MOTIFS = {
    star: [
      'M100 32C103.4 75.1 124.9 96.6 168 100',
      'M168 100C124.9 103.4 103.4 124.9 100 168',
      'M100 168C96.6 124.9 75.1 103.4 32 100',
      'M32 100C75.1 96.6 96.6 75.1 100 32',
      'M100 78C101.1 92 108 98.9 122 100 108 101.1 101.1 108 100 122 98.9 108 92 101.1 78 100 92 98.9 98.9 92 100 78Z',
    ],
    bloom: [
      'M100 172C95 150 104 128 101 104',
      'M99 160C76 154 60 136 62 114C82 120 96 136 99 160Z',
      'M101 104C80 101 62 84 70 62C77 46 95 48 102 62C110 46 130 44 137 60C145 82 124 101 101 104Z',
    ],
    sun: [],
    rose: [],
  };
  (function buildGeneratedMotifs() {
    const c = (r) => `M${100 + r} 100A${r} ${r} 0 1 1 ${100 - r} 100A${r} ${r} 0 1 1 ${100 + r} 100`;
    let z = '';
    for (let i = 0; i <= 24; i++) {
      const a = (i / 24) * Math.PI * 2 - Math.PI / 2;
      const r = i % 2 ? 50 : 68;
      z += `${i ? 'L' : 'M'}${(100 + r * Math.cos(a)).toFixed(1)} ${(100 + r * Math.sin(a)).toFixed(1)}`;
    }
    MOTIFS.sun = [c(40), z, c(20), 'M100 90C100.5 95 102 99.5 107 100 102 100.5 100.5 102 100 107 99.5 102 98 100.5 93 100 98 99.5 99.5 98 100 90Z'];
    let r5 = '';
    for (let i = 0; i <= 160; i++) {
      const th = (i / 160) * Math.PI;
      const rr = 60 * Math.cos(5 * th);
      r5 += `${i ? 'L' : 'M'}${(100 + rr * Math.cos(th - Math.PI / 2)).toFixed(1)} ${(100 + rr * Math.sin(th - Math.PI / 2)).toFixed(1)}`;
    }
    MOTIFS.rose = [r5];
  })();
  const PROJ = [
    { id: 'p1', title: 'Выбрать музыку', sub: 'Запись № 7 · из разговора', done: true },
    { id: 'p2', title: 'Собрать палитру', sub: 'Красный, графит, светлый фон', done: true },
    { id: 'p3', title: 'Сделать один кадр', sub: 'Без срока · изображение в «Мастерской»', done: false },
    { id: 'p4', title: 'Поставить свет', sub: 'Красный или янтарный — решить вместе', done: false },
    { id: 'p5', title: 'Отправить Ане', sub: 'Когда кадр будет готов', done: false },
  ];
  const projDone = () => PROJ.filter((t) => t.done).length;
  const HOOPS = {
    evening: { motif: 'star', total: 5, done: projDone, label: 'Вечер без повода' },
    gift: { motif: 'bloom', total: 3, done: () => 1, label: 'Подарок Ане' },
    walk: { motif: 'sun', total: 4, done: () => 4, label: 'Фотопрогулка' },
    hers: { motif: 'rose', partial: 0.62, label: 'Вышивка Арти' },
  };
  let uid = 0;
  function hoopSVG(key) {
    const h = HOOPS[key];
    const id = 'hp' + ++uid;
    const paths = MOTIFS[h.motif];
    const masks = paths.map((d, i) => `<mask id="${id}m${i}" maskUnits="userSpaceOnUse" x="-10" y="-10" width="220" height="220"><path d="${d}" pathLength="1" fill="none" stroke="#fff" stroke-width="10" stroke-linecap="butt" stroke-linejoin="round" stroke-dasharray="1 2" stroke-dashoffset="1"/></mask>`).join('');
    const guide = paths.map((d) => `<path d="${d}"/>`).join('');
    const st = paths.map((d, i) => `<g class="st" mask="url(#${id}m${i})"><path class="st-shadow" d="${d}"/><path class="st-thread" d="${d}"/><path class="st-hi" d="${d}"/></g>`).join('');
    const knot = h.total ? `<g class="knot"><circle cx="150" cy="158" r="3.6"/><circle cx="147.6" cy="156.4" r="1" fill="#fff" opacity=".5"/><path d="M150 161c-2 6 2 9 -1 14" fill="none" stroke="var(--red)" stroke-width="1.3" stroke-linecap="round"/></g>` : '';
    const holes = key === 'hers' ? '<g class="holes" fill="var(--pencil)" opacity=".5">' + Array.from({ length: 14 }, (_, i) => { const a = (i / 14) * Math.PI * 2; return `<circle cx="${(100 + 74 * Math.cos(a)).toFixed(1)}" cy="${(100 + 74 * Math.sin(a)).toFixed(1)}" r=".8"/>`; }).join('') + '</g>' : '';
    return `<svg class="hoop" viewBox="-8 -18 216 226" role="img" aria-label="${esc(h.label)}">` +
      `<defs><clipPath id="${id}c"><circle cx="100" cy="100" r="87"/></clipPath>${masks}</defs>` +
      `<circle class="hoop-back" cx="100" cy="100" r="88"/>` +
      `<g clip-path="url(#${id}c)" style="isolation:isolate"><circle class="hoop-fabric" cx="100" cy="100" r="90"/><rect class="hoop-weave" x="0" y="0" width="200" height="200" fill="url(#linenPat)"/>` +
      `<circle cx="100" cy="100" r="88" fill="url(#hoopShade)"/>${holes}<g class="guide">${guide}</g><g class="stitches">${st}</g>${knot}</g>` +
      `<circle class="ring-in" cx="100" cy="100" r="88.5"/><circle class="ring-out" cx="100" cy="100" r="94"/><circle class="ring-hi" cx="100" cy="100" r="97.6"/>` +
      `<g class="clamp"><rect x="89" y="-9" width="22" height="9" rx="2"/><rect x="94" y="-16" width="12" height="8" rx="2"/><path d="M96.5 -12h7"/></g>` +
      `<g class="needle" opacity="0"><path d="M0 0L-30 -2.3-30 2.3Z" fill="url(#needleGrad)"/><ellipse cx="-25" cy="0" rx="3" ry=".9" fill="var(--linen)"/>` +
      `<g class="needle-glint"><path d="${STAR_D}" transform="scale(.55) translate(-12 -12)" fill="#fff"/></g></g></svg>`;
  }
  const hoopEls = {};
  function setHoopStatic(svg, key) {
    const h = HOOPS[key];
    const mps = $$('mask path', svg);
    if (h.partial != null) { mps.forEach((m) => m.setAttribute('stroke-dashoffset', (1 - h.partial).toFixed(3))); return; }
    const d = h.done();
    mps.forEach((m, i) => m.setAttribute('stroke-dashoffset', i < d ? '0' : '1'));
    svg.classList.toggle('is-complete', d >= h.total);
  }
  function buildHoops() {
    $$('[data-hoop]').forEach((slot) => {
      const key = slot.dataset.hoop;
      slot.innerHTML = hoopSVG(key);
      const svg = $('svg', slot);
      (hoopEls[key] = hoopEls[key] || []).push(svg);
      setHoopStatic(svg, key);
    });
  }
  function sew(svg, i, dir) {
    return new Promise((res) => {
      const mp = $$('mask path', svg)[i];
      const thread = $$('.st-thread', svg)[i];
      const needle = $('.needle', svg);
      if (!mp || !thread) { res(); return; }
      if (reduced() || !svg.getBoundingClientRect().width) { mp.setAttribute('stroke-dashoffset', dir > 0 ? '0' : '1'); res(); return; }
      const len = thread.getTotalLength();
      const D = dir > 0 ? 1250 : 800;
      const t0 = performance.now();
      needle.setAttribute('opacity', '1');
      const f = (now) => {
        const t = Math.min(1, (now - t0) / D);
        const e = 1 - Math.pow(1 - t, 2);
        const v = dir > 0 ? e : 1 - e;
        mp.setAttribute('stroke-dashoffset', (1 - v).toFixed(4));
        const L = Math.max(0.01, Math.min(len - 0.01, v * len));
        const p = thread.getPointAtLength(L);
        const p2 = thread.getPointAtLength(Math.min(len, L + 1));
        const p1 = thread.getPointAtLength(Math.max(0, L - 1));
        let a = (Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180) / Math.PI;
        if (dir < 0) a += 180;
        const bob = 1 + Math.sin(t * Math.PI * 14) * 0.08;
        needle.setAttribute('transform', `translate(${p.x.toFixed(2)} ${p.y.toFixed(2)}) rotate(${a.toFixed(1)}) scale(${bob.toFixed(3)})`);
        if (t < 1) requestAnimationFrame(f);
        else {
          needle.classList.remove('glint');
          void needle.getBoundingClientRect();
          needle.classList.add('glint');
          setTimeout(() => { needle.setAttribute('opacity', '0'); needle.classList.remove('glint'); res(); }, 520);
        }
      };
      requestAnimationFrame(f);
    });
  }
  let sewChain = Promise.resolve();

  /* ------------------------------------------------------------- project */
  function renderTasks() {
    $('#projTasks').innerHTML = PROJ.map((t) => `<li><button type="button" class="task${t.done ? ' is-done' : ''}" role="checkbox" aria-checked="${t.done}" data-task="${t.id}"><span class="tick">${icon('check')}</span><span class="task-text"><span class="task-title">${esc(t.title)}</span><span class="task-sub">${esc(t.sub)}</span></span></button></li>`).join('');
  }
  function syncProject(bump) {
    const d = projDone();
    $('#projDone').textContent = d;
    if (bump && !reduced()) { const n = $('#projDone'); n.classList.remove('bump'); void n.offsetWidth; n.classList.add('bump'); setTimeout(() => n.classList.remove('bump'), 450); }
    $$('[data-count="evening"]').forEach((el) => { el.textContent = d >= 5 ? '5 из 5 · готово' : `${d} из 5`; el.classList.toggle('is-done', d >= 5); });
    const next = PROJ.find((t) => !t.done);
    $('#nextTask').textContent = next ? next.title : 'Всё готово — завязать узелок';
    $('#projDoneNote').hidden = d < 5;
    $('#pathResult').classList.toggle('is-now', d >= 5);
    $$('.path-thread')[1] && $$('.path-thread')[1].classList.toggle('is-pending', d < 5);
    (hoopEls.evening || []).forEach((svg) => svg.classList.toggle('is-complete', d >= 5));
    $$('#projTasks .task').forEach((b) => {
      const t = PROJ.find((x) => x.id === b.dataset.task);
      b.classList.toggle('is-done', t.done);
      b.setAttribute('aria-checked', String(t.done));
    });
  }
  function toggleTask(id, opts = {}) {
    const t = PROJ.find((x) => x.id === id);
    if (!t) return;
    const before = projDone();
    t.done = opts.force != null ? opts.force : !t.done;
    const after = projDone();
    if (after === before) { syncProject(false); return; }
    syncProject(true);
    const dir = after > before ? 1 : -1;
    const idx = dir > 0 ? after - 1 : before - 1;
    const visible = (hoopEls.evening || []).filter((svg) => svg.closest('.screen.is-active'));
    const hidden = (hoopEls.evening || []).filter((svg) => !visible.includes(svg));
    hidden.forEach((svg) => setHoopStatic(svg, 'evening'));
    const reveal = () => {
      const svg = visible[0];
      const sc = svg && svg.closest('.scroll');
      if (!sc) return Promise.resolve();
      const sr = sc.getBoundingClientRect(), r = svg.getBoundingClientRect();
      if (r.top >= sr.top - 4 && r.bottom <= sr.bottom + 4) return Promise.resolve();
      sc.scrollTo({ top: Math.max(0, sc.scrollTop + (r.top - sr.top) / scale - 16), behavior: reduced() ? 'auto' : 'smooth' });
      return wait(reduced() ? 0 : 450);
    };
    sewChain = sewChain.then(reveal).then(() => Promise.all(visible.map((svg) => sew(svg, idx, dir)))).then(() => {
      (hoopEls.evening || []).forEach((svg) => setHoopStatic(svg, 'evening'));
    });
    renderTodos();
    if (after === 5 && dir > 0) setTimeout(() => toast('Пяльцы зашиты. Узелок завязан.'), reduced() ? 0 : 1300);
  }
  $('#projTasks').addEventListener('click', (e) => {
    const b = e.target.closest('.task');
    if (b) toggleTask(b.dataset.task);
  });

  /* ------------------------------------------------------------- files */
  const FILES = [
    { name: 'Красный, без повода.png', meta: 'Изображение · из разговора', thumb: true, go: 'image' },
    { name: 'Идеи для вечера.docx', meta: 'Документ · сегодня', ic: 'doc' },
    { name: 'Запись № 7 — черновик.wav', meta: 'Музыка · вчера', ic: 'music', open: 'artiroom' },
    { name: 'Маршрут субботы.pdf', meta: 'Документ · из группы «Идеи на выходные»', ic: 'doc' },
    { name: 'Палитра вечера.png', meta: 'Изображение · 15.10', thumb: true, go: 'image' },
    { name: 'Бюджет подарка.xlsx', meta: 'Таблица · 14.10', ic: 'table' },
    { name: 'Голосовое для Макса.ogg', meta: 'Голос · 0:18 · сегодня', ic: 'voice' },
    { name: 'Список кадров.docx', meta: 'Документ · 13.10', ic: 'doc' },
  ];
  function fileRow(f, isNew) {
    const lead = f.thumb ? `<span class="file-ic"><img src="assets/paper-bloom-v4.webp" alt="" width="44" height="44" loading="lazy"></span>` : `<span class="file-ic">${icon(f.ic || 'doc')}</span>`;
    const attr = f.go ? `data-go="${f.go}"` : f.open ? `data-open="${f.open}"` : `data-toast="«${esc(f.name)}» откроется в просмотрщике."`;
    return `<li><button type="button" class="file${isNew ? ' is-new' : ''}" ${attr}>${lead}<span class="file-body"><span class="file-name">${esc(f.name)}</span><span class="mono">${esc(f.meta)}</span></span>${icon('chev', 'ic ic-sm')}</button></li>`;
  }
  function renderFiles() {
    $('[data-files="recent"]').innerHTML = FILES.slice(0, 2).map((f) => fileRow(f)).join('');
    $('[data-files="all"]').innerHTML = FILES.map((f) => fileRow(f)).join('');
    const h = $('[data-spacepanel="files"] .section-label');
    if (h) h.textContent = `Все файлы · ${FILES.length}`;
  }
  function setSpaceTab(k) {
    $$('#spaceTabs [data-spacetab]').forEach((b) => { const on = b.dataset.spacetab === k; b.classList.toggle('is-on', on); b.setAttribute('aria-selected', String(on)); });
    $$('.space-panel').forEach((p) => { p.hidden = p.dataset.spacepanel !== k; });
  }
  $$('#spaceTabs [data-spacetab]').forEach((b) => b.addEventListener('click', () => setSpaceTab(b.dataset.spacetab)));

  /* ------------------------------------------------------------- organizer */
  const DAYS = [['пн', 12], ['вт', 13], ['ср', 14], ['чт', 15], ['пт', 16], ['сб', 17], ['вс', 18]];
  const TODOS = [
    { id: 't2', title: 'Встреча с Аней', meta: 'Сегодня, 20:00 · за 30 минут', day: 15, bell: true, done: false },
    { id: 't3', title: 'Отправить Даше наброски', meta: 'Завтра, 10:00', day: 16, done: false },
    { id: 't4', title: 'Забрать фотографии', meta: 'Завтра, 18:00', day: 16, done: false },
    { id: 't1', title: 'Сделать один кадр', meta: 'Вечер без повода · без срока', day: null, link: 'p3' },
    { id: 'd1', title: 'Выбрать музыку', meta: 'Вечер без повода · 15.10', day: 15, link: 'p1' },
    { id: 'd2', title: 'Собрать палитру', meta: 'Вечер без повода · 15.10', day: 15, link: 'p2' },
  ];
  const isDone = (t) => (t.link ? PROJ.find((p) => p.id === t.link).done : t.done);
  let todoTab = 'open';
  let selDay = 15;
  function renderWeek() {
    $('#week').innerHTML = DAYS.map(([w, d]) => {
      const has = TODOS.some((t) => t.day === d && !isDone(t));
      return `<button type="button" class="day${d === selDay ? ' is-sel' : ''}${d === 15 ? ' is-today' : ''}${has ? ' has-tasks' : ''}" role="option" aria-selected="${d === selDay}" data-day="${d}" aria-label="${w}, ${d} октября${d === 15 ? ', сегодня' : ''}"><span class="mono">${w}</span><span class="day-num">${d}</span></button>`;
    }).join('');
  }
  function renderTodos() {
    const open = TODOS.filter((t) => !isDone(t));
    const done = TODOS.filter(isDone);
    $('#openCount').textContent = open.length;
    $('#doneCount').textContent = done.length;
    const list = todoTab === 'open' ? open : done;
    const groups = todoTab === 'open'
      ? [['Сегодня · четверг, 15', (t) => t.day === 15, 15], ['Завтра · пятница, 16', (t) => t.day === 16, 16], ['Без срока', (t) => t.day == null, null]]
      : [['Готово', () => true, 'x']];
    const row = (t) => `<div class="todo" data-todo="${t.id}"><button type="button" class="task${isDone(t) ? ' is-done' : ''}" role="checkbox" aria-checked="${isDone(t)}"><span class="tick">${icon('check')}</span><span class="task-text"><span class="task-title">${esc(t.title)}</span><span class="task-sub">${esc(t.meta)}</span></span></button>${t.bell ? icon('bell', 'ic ic-sm todo-bell') : ''}</div>`;
    let html = '';
    groups.forEach(([label, fn, day]) => {
      const items = list.filter(fn);
      if (!items.length) return;
      html += `<section class="todo-group${day === selDay ? ' is-focus' : ''}" data-group="${day}"><h3 class="mono">${label}</h3>${items.map(row).join('')}</section>`;
    });
    if (!html) html = `<p class="day-note"><span class="hand">${todoTab === 'open' ? 'Всё сделано. Можно ничего не делать — это тоже план.' : 'Пока ничего не отмечено.'}</span></p>`;
    $('#todoList').innerHTML = html;
    const dayHas = TODOS.some((t) => t.day === selDay && !isDone(t));
    $('#dayNote').hidden = todoTab !== 'open' || dayHas;
    renderWeek();
  }
  $('#week').addEventListener('click', (e) => {
    const b = e.target.closest('.day');
    if (!b) return;
    selDay = +b.dataset.day;
    renderTodos();
    const g = $(`.todo-group[data-group="${selDay}"]`);
    if (g) g.scrollIntoView({ block: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
  });
  $$('#todoTabs > button').forEach((b) => b.addEventListener('click', () => {
    todoTab = b.dataset.todotab;
    setSeg($('#todoTabs'), todoTab, 'todotab');
    renderTodos();
  }));
  $('#todoList').addEventListener('click', (e) => {
    const b = e.target.closest('.task');
    if (!b) return;
    const row = b.closest('.todo');
    const t = TODOS.find((x) => x.id === row.dataset.todo);
    const next = !isDone(t);
    b.classList.toggle('is-done', next);
    b.setAttribute('aria-checked', String(next));
    if (!reduced()) row.classList.add('is-leaving');
    setTimeout(() => {
      if (t.link) toggleTask(t.link, { force: next });
      else t.done = next;
      renderTodos();
    }, reduced() ? 0 : 950);
  });

  /* ------------------------------------------------------------- profile */
  $$('#themeSeg > button').forEach((b) => b.addEventListener('click', () => setThemeMode(b.dataset.mode)));
  const INIT_HINTS = {
    sometimes: 'Напишет, если найдёт что-то стоящее. Не чаще раза в день.',
    business: 'Только напоминания и ответы по проектам. Без «просто так».',
    never: 'Будет ждать, пока ты напишешь первой. Напоминания останутся.',
  };
  $$('#initSeg > button').forEach((b) => b.addEventListener('click', () => {
    setSeg($('#initSeg'), b.dataset.init, 'init');
    $('#initHint').textContent = INIT_HINTS[b.dataset.init];
    store.set('initiative', b.dataset.init);
  }));

  /* ------------------------------------------------------------- notebook */
  const NB = [
    { id: 'n1', type: 'Договорились', text: 'Сначала спрашивать. Выбирать за тебя — только если попросишь.', src: 'Разговор · 15.10.2026 19:14 · с твоих слов' },
    { id: 'n2', type: 'Знаю о тебе', text: 'Не любишь ранний подъём. Возражение было убедительным.', src: 'Группа «Идеи на выходные» · 15.10 17:54' },
    { id: 'n3', type: 'Знаю о тебе', text: 'Любишь тёплые оттенки. Красный — да, но не кричащий.', src: 'Разговор · 12.10 · с твоих слов' },
    { id: 'n4', type: 'Не забыть', text: 'Спросить, чем закончилась встреча с Аней.', src: 'Дела · 16.10 20:00' },
    { id: 'n5', type: 'Мне показалось', text: 'По вечерам тебе не до длинных ответов. Проверю, прежде чем решать.', src: 'Мой вывод · уверенность: невысокая', tentative: true },
  ];
  const PAGES = [['n1', 'n2', 'quote'], ['n3', 'n4'], ['n5', 'end']];
  let page = 0;
  let flipping = false;
  function entryHTML(n) {
    const conf = n.tentative ? '<span class="conf" aria-hidden="true"><i class="on"></i><i></i><i></i></span>' : '';
    return `<article class="entry${n.tentative ? ' is-tentative' : ''}" data-id="${n.id}">` +
      (n.tentative ? '<span class="entry-q" aria-hidden="true">?</span>' : '') +
      `<p class="entry-type mono">${esc(n.type)}</p>` +
      `<p class="entry-body"><span class="entry-text">${esc(n.text)}</span></p>` +
      `<p class="entry-src"><span class="slip mono">${esc(n.src)}${conf}</span></p>` +
      `<p class="entry-ctl"><button type="button" data-act="fix">${icon('pencil')}Исправить</button><button type="button" data-act="strike">${icon('strike')}Вычеркнуть</button></p>` +
      '</article>';
  }
  function renderNotebook() {
    const pages = $('#pages');
    pages.innerHTML = PAGES.map((ids, pi) => {
      const inner = ids.map((id) => {
        if (id === 'quote') return '<aside class="margin-quote"><svg viewBox="0 0 30 40" aria-hidden="true"><path d="M26 36C14 32 8 22 10 6M4 12l6-8 6 7"/></svg><p class="hand">Не помню, как именно договорились. Запись есть — значит, в силе.</p></aside>';
        if (id === 'end') return '<div class="page-end"><span class="mono">Конец записей · тетрадь № 1</span><p class="hand">Если что-то не так — исправь. Я перепишу.</p></div>';
        return entryHTML(NB.find((n) => n.id === id));
      }).join('');
      const curl = pi < PAGES.length - 1 ? '<button type="button" class="page-curl" data-next aria-label="Перевернуть страницу"></button>' : '';
      return `<div class="page${pi === page ? ' is-current' : ''}" data-page="${pi}" role="group" aria-label="Страница ${pi + 1} из ${PAGES.length}"><span class="page-no mono">${pi + 1}</span>${inner}${curl}</div>`;
    }).join('');
    updatePager();
  }
  function updatePager() {
    $('#pgNum').textContent = `Стр. ${page + 1} / ${PAGES.length}`;
    $('#pgPrev').disabled = page === 0;
    $('#pgNext').disabled = page === PAGES.length - 1;
  }
  function turnTo(n) {
    if (n < 0 || n >= PAGES.length || n === page || flipping) return;
    const pages = $$('.page');
    const cur = pages[page], nxt = pages[n];
    const dir = n > page ? 1 : -1;
    if (reduced()) {
      cur.classList.remove('is-current');
      nxt.classList.add('is-current');
      page = n;
      updatePager();
      return;
    }
    flipping = true;
    if (dir > 0) {
      nxt.classList.add('is-under');
      cur.classList.remove('is-current');
      cur.classList.add('turn-out');
    } else {
      cur.classList.remove('is-current');
      cur.classList.add('is-under');
      nxt.classList.add('turn-in');
    }
    setTimeout(() => {
      pages.forEach((p) => p.classList.remove('turn-out', 'turn-in', 'is-under', 'is-current'));
      nxt.classList.add('is-current');
      nxt.scrollTop = 0;
      page = n;
      flipping = false;
      updatePager();
    }, 760);
  }
  $('#pgPrev').addEventListener('click', () => turnTo(page - 1));
  $('#pgNext').addEventListener('click', () => turnTo(page + 1));
  (function swipe() {
    const el = $('#pages');
    let sx = null, sy = 0;
    el.addEventListener('pointerdown', (e) => { if (e.target.closest('textarea, button')) return; sx = e.clientX; sy = e.clientY; });
    el.addEventListener('pointerup', (e) => {
      if (sx === null) return;
      const dx = e.clientX - sx, dy = e.clientY - sy;
      sx = null;
      if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) turnTo(page + (dx < 0 ? 1 : -1));
    });
    el.addEventListener('pointercancel', () => { sx = null; });
  })();
  function nbCount() {
    const live = $$('.entry').filter((e) => !e.classList.contains('is-gone')).length;
    const txt = `${live} ${plural(live, 'запись', 'записи', 'записей')} · последняя 16.10`;
    $('#nbSub').textContent = txt;
    $('#nbCount').textContent = txt;
    $('.notebook-object').setAttribute('aria-label', `Тетрадь Арти: ${txt}. Открыть`);
  }
  $('#pages').addEventListener('click', (e) => {
    if (e.target.closest('[data-next]')) { turnTo(page + 1); return; }
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const entry = b.closest('.entry');
    const act = b.dataset.act;
    const text = $('.entry-body .entry-text:not(.old)', entry) || $('.entry-text', entry);
    if (act === 'fix') {
      if (entry.classList.contains('is-fixing')) return;
      entry.classList.add('is-fixing');
      text.classList.add('struck');
      const ed = document.createElement('div');
      ed.className = 'fix-editor';
      ed.innerHTML = `<textarea rows="2" aria-label="Исправить запись">${esc(text.textContent)}</textarea><div class="fix-btns"><button type="button" data-act="save">Сохранить</button><button type="button" data-act="cancel">Отмена</button></div>`;
      $('.entry-body', entry).after(ed);
      $('.entry-ctl', entry).hidden = true;
      const ta = $('textarea', ed);
      setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, reduced() ? 0 : 450);
      ta.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); $('[data-act="save"]', ed).click(); }
        if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); $('[data-act="cancel"]', ed).click(); }
      });
    } else if (act === 'cancel') {
      text.classList.remove('struck');
      $('.fix-editor', entry).remove();
      $('.entry-ctl', entry).hidden = false;
      entry.classList.remove('is-fixing');
      $('[data-act="fix"]', entry).focus();
    } else if (act === 'save') {
      const ta = $('.fix-editor textarea', entry);
      const v = ta.value.trim();
      if (!v || v === text.textContent.trim()) { $('[data-act="cancel"]', entry).click(); return; }
      $('.fix-editor', entry).remove();
      text.classList.add('old');
      const body = $('.entry-body', entry);
      body.insertAdjacentHTML('beforeend', `<br><span class="entry-text is-writing">${esc(v)}</span>`);
      const nw = $$('.entry-text', body).pop();
      setTimeout(() => nw.classList.remove('is-writing'), reduced() ? 0 : 1300);
      const srcs = $$('.entry-src', entry);
      srcs.forEach((p) => $$('.slip', p).forEach((sl) => sl.classList.add('is-old')));
      srcs[srcs.length - 1].insertAdjacentHTML('afterend', '<p class="entry-src"><span class="slip mono is-fixed">Исправлено тобой · 16.10</span></p>');
      $('.entry-ctl', entry).hidden = false;
      entry.classList.remove('is-fixing', 'is-tentative');
      const q = $('.entry-q', entry);
      if (q) q.remove();
      $('[data-act="fix"]', entry).focus();
      toast('Переписала. Старая запись осталась зачёркнутой — для честности.');
    } else if (act === 'strike') {
      $$('.entry-text', entry).forEach((t) => t.classList.add('struck'));
      $('.entry-ctl', entry).hidden = true;
      setTimeout(() => {
        entry.classList.add('is-gone');
        entry.insertAdjacentHTML('beforeend', '<p class="ghost mono">Вычеркнуто по твоей просьбе · 16.10 <button type="button" data-act="undo">Вернуть</button></p>');
        nbCount();
        const u = $('[data-act="undo"]', entry);
        if (u) u.focus({ preventScroll: true });
      }, reduced() ? 50 : 1100);
    } else if (act === 'undo') {
      $('.ghost', entry).remove();
      entry.classList.remove('is-gone');
      $$('.entry-text', entry).forEach((t) => { if (!t.classList.contains('old')) t.classList.remove('struck'); });
      $('.entry-ctl', entry).hidden = false;
      nbCount();
      $('[data-act="strike"]', entry).focus();
    }
  });
  function openBook() {
    const book = $('#book');
    book.classList.remove('opening');
    if (reduced()) return;
    void book.offsetWidth;
    book.classList.add('opening');
    clearTimeout(book._t);
    book._t = setTimeout(() => book.classList.remove('opening'), 1050);
  }

  /* ------------------------------------------------------------- image / Проявка */
  const tray = $('#tray');
  const devImg = $('#devImg');
  const devProg = $('#devProgress');
  const FORMATS = { '1:1': '1024 × 1024', '4:5': '1024 × 1280', '16:9': '1344 × 768' };
  let fmtSel = '1:1';
  let devState = 'idle';
  let variant = 0;
  let devRaf = 0;
  let savedOnce = false;
  $$('#formatChips .chip').forEach((c) => c.addEventListener('click', () => {
    fmtSel = c.dataset.format;
    $$('#formatChips .chip').forEach((x) => { const on = x === c; x.classList.toggle('is-on', on); x.setAttribute('aria-checked', String(on)); });
    tray.dataset.format = fmtSel;
    $('#resultSize').textContent = FORMATS[fmtSel];
    if (devState === 'done') { devProg.textContent = `Готово · ${FORMATS[fmtSel]}`; setTimeout(() => placeDevArrow(false), reduced() ? 0 : 520); }
  }));
  const styleBtn = $('#styleBtn');
  const styleMenu = $('#styleMenu');
  function closeStyleMenu() { if (!styleMenu.hidden) { styleMenu.hidden = true; styleBtn.setAttribute('aria-expanded', 'false'); } }
  styleBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = styleMenu.hidden;
    styleMenu.hidden = !open;
    styleBtn.setAttribute('aria-expanded', String(open));
    if (open) { const sel = $('[aria-selected="true"]', styleMenu) || $('button', styleMenu); sel.focus(); }
  });
  $$('button', styleMenu).forEach((b, i, all) => {
    b.addEventListener('click', () => {
      all.forEach((x) => x.setAttribute('aria-selected', String(x === b)));
      $('#styleName').textContent = b.textContent;
      closeStyleMenu();
      styleBtn.focus();
    });
    b.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { all[(i + 1) % all.length].focus(); e.preventDefault(); }
      if (e.key === 'ArrowUp') { all[(i - 1 + all.length) % all.length].focus(); e.preventDefault(); }
    });
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.style-pick')) closeStyleMenu(); });

  function develop() {
    if (devState === 'developing') return;
    cancelAnimationFrame(devRaf);
    devState = 'developing';
    tray.dataset.state = 'developing';
    $('#devNote').classList.remove('is-on');
    $('#devArrow').classList.remove('is-on');
    $('#resultMeta').hidden = true;
    const gb = $('#genBtn');
    gb.disabled = true;
    $('span', gb).textContent = 'Проявляется…';
    devImg.style.transform = variant % 2 ? 'scaleX(-1)' : '';
    devImg.style.objectPosition = variant % 2 ? '40% 50%' : '50% 50%';
    $('.result-name').textContent = variant ? `Красный, без повода · ${variant + 1}` : 'Красный, без повода';
    const D = reduced() ? 450 : 2600;
    const t0 = performance.now();
    const frame = (now) => {
      const t = Math.min(1, (now - t0) / D);
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      devImg.style.opacity = Math.min(1, 0.1 + t * 1.8).toFixed(3);
      devImg.style.filter = `blur(${((1 - e) * 16).toFixed(2)}px) contrast(${(0.32 + 0.68 * e).toFixed(3)}) brightness(${(1.6 - 0.6 * e).toFixed(3)}) saturate(${e.toFixed(3)}) sepia(${((1 - e) * 0.55).toFixed(3)})`;
      tray.style.setProperty('--grain-a', ((1 - e) * 0.85).toFixed(3));
      devProg.textContent = `Проявляется · ${Math.round(t * 100)}%`;
      if (t < 1) devRaf = requestAnimationFrame(frame);
      else finishDevelop();
    };
    devRaf = requestAnimationFrame(frame);
  }
  function finishDevelop() {
    devState = 'done';
    tray.dataset.state = 'done';
    devImg.style.filter = '';
    devImg.style.opacity = '';
    tray.style.setProperty('--grain-a', '0');
    devProg.textContent = `Готово · ${FORMATS[fmtSel]}`;
    const gb = $('#genBtn');
    gb.disabled = false;
    $('span', gb).textContent = 'Создать заново';
    $('#resultMeta').hidden = false;
    $('#resultSize').textContent = FORMATS[fmtSel];
    requestAnimationFrame(() => { $('#devNote').classList.add('is-on'); placeDevArrow(true); });
    const sc = $('#imageScroll');
    setTimeout(() => { const r = $('#resultMeta'); if (r && !r.hidden) sc.scrollTo({ top: Math.max(0, r.offsetTop - sc.clientHeight + r.offsetHeight + 40), behavior: reduced() ? 'auto' : 'smooth' }); }, 200);
  }
  /** Her arrow runs from the margin note to the red flower, wherever the crop puts it. */
  function placeDevArrow(animate) {
    const svgA = $('#devArrow');
    if (devState !== 'done') { svgA.classList.remove('is-on'); return; }
    const wrap = $('.tray-wrap');
    const W = tray.clientWidth, H = tray.clientHeight;
    if (!W || !H) return;
    const S = Math.max(W, H);
    const mirrored = variant % 2 === 1;
    const ox = (W - S) * (mirrored ? 0.4 : 0.5), oy = (H - S) * 0.5;
    let tx = ox + 0.73 * S, ty = oy + 0.3 * S;
    if (mirrored) tx = W - tx;
    const wr = wrap.getBoundingClientRect(), nr = $('#devNote .hand').getBoundingClientRect();
    const sx = (nr.left - wr.left) / scale + 26, sy = (nr.top - wr.top) / scale - 2;
    const c1x = Math.min(W - 8, sx + 70), c1y = sy - 70;
    const c2x = Math.min(W - 6, Math.max(tx, sx) + 60), c2y = ty + 70;
    const ex = tx + 8, ey = ty + 10;
    const curve = `M${sx.toFixed(1)} ${sy.toFixed(1)}C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${ex.toFixed(1)} ${ey.toFixed(1)}`;
    const ang = Math.atan2(ey - c2y, ex - c2x);
    const hd = (a) => `${(ex - 10 * Math.cos(ang + a)).toFixed(1)} ${(ey - 10 * Math.sin(ang + a)).toFixed(1)}`;
    const head = `M${hd(0.5)}L${ex.toFixed(1)} ${ey.toFixed(1)}L${hd(-0.5)}`;
    svgA.innerHTML = `<path class="halo" d="${curve}"/><path class="halo head" d="${head}"/><path class="ink" d="${curve}"/><path class="ink head" d="${head}"/>`;
    const len = Math.ceil($('.ink', svgA).getTotalLength()) + 2;
    svgA.style.setProperty('--len', len);
    svgA.classList.remove('is-on');
    if (animate === false || reduced()) { svgA.classList.add('is-on'); return; }
    void svgA.getBoundingClientRect();
    requestAnimationFrame(() => svgA.classList.add('is-on'));
  }
  $('#genBtn').addEventListener('click', () => { if (devState === 'done') variant += 1; develop(); });
  $('#againBtn').addEventListener('click', () => { variant += 1; develop(); });
  function flyThread(fromEl, toEl) {
    if (reduced()) return;
    const svg = $('#threadFly');
    if (svg.parentElement !== app) app.appendChild(svg); // span the whole app, above the tab bar
    if (!$('#flyMask', svg)) {
      svg.innerHTML = '<defs><mask id="flyMask" maskUnits="userSpaceOnUse" x="-50" y="-50" width="600" height="1100"><path class="fm" fill="none" stroke="#fff" stroke-width="8" stroke-linecap="round"/></mask></defs><path class="fly-path" mask="url(#flyMask)"/>';
    }
    const path = $('.fly-path', svg);
    const fm = $('.fm', svg);
    const a = rel(fromEl), b = rel(toEl);
    const x1 = a.x + a.w / 2, y1 = a.y + a.h / 2;
    const x2 = b.x + 16, y2 = b.y + b.h / 2;
    const d = `M${x1.toFixed(1)} ${y1.toFixed(1)}C${(x1 + 120).toFixed(1)} ${(y1 - 40).toFixed(1)} ${(x2 + 150).toFixed(1)} ${(y2 + 120).toFixed(1)} ${x2.toFixed(1)} ${y2.toFixed(1)}`;
    path.setAttribute('d', d);
    fm.setAttribute('d', d);
    const len = path.getTotalLength();
    path.style.transition = 'none';
    path.style.opacity = '1';
    fm.style.transition = 'none';
    fm.style.strokeDasharray = `${len} ${len}`;
    fm.style.strokeDashoffset = String(len);
    void fm.getBoundingClientRect();
    fm.style.transition = 'stroke-dashoffset .8s cubic-bezier(.5,0,.2,1)';
    fm.style.strokeDashoffset = '0';
    clearTimeout(svg._t);
    svg._t = setTimeout(() => { path.style.transition = 'opacity .5s'; path.style.opacity = '0'; }, 1400);
  }
  $('#saveToProject').addEventListener('click', (e) => {
    toast('Сохранено в «Вечер без повода»');
    flyThread(e.currentTarget, $('#toast'));
    if (!savedOnce) {
      savedOnce = true;
      const name = variant ? `Красный, без повода ${variant + 1}.png` : 'Красный, без повода (финал).png';
      FILES.unshift({ name, meta: 'Изображение · сохранено в проект', thumb: true, go: 'image' });
      renderFiles();
      const li = $('[data-files="recent"] .file');
      if (li) li.classList.add('is-new');
      $('#materials').insertAdjacentHTML('afterbegin', `<button type="button" class="material mat-image is-new" data-go="image"><img src="assets/paper-bloom-v4.webp" alt="" width="120" height="120" style="${variant % 2 ? 'transform:scaleX(-1)' : ''}"><span class="mat-name">${esc(name)}</span><span class="mono">Изображение · новое</span></button>`);
    }
  });
  $('#toChat').addEventListener('click', () => {
    go('arti');
    setTimeout(() => {
      const row = addMsg('arti', 'me', `<img src="assets/paper-bloom-v4.webp" alt="Красный, без повода" width="200" height="200" style="${variant % 2 ? 'transform:scaleX(-1)' : ''}">`, { cls: 'photo' });
      const m = $('.meta', row);
      if (m) m.classList.add('on-photo');
      queueReply('arti', 'Красный оставила бы и я. Повесить его в проект — или сначала поставим свет?', row, '♥');
    }, reduced() ? 50 : 480);
  });

  /* ------------------------------------------------------------- enter hooks */
  const firstVisit = {};
  enterHooks.arti = () => { if (!firstVisit.arti) { firstVisit.arti = true; scrollThread($('#artiThread'), false); } };
  enterHooks.anya = () => { if (!firstVisit.anya) { firstVisit.anya = true; scrollThread($('#anyaThread'), false); } };
  enterHooks.group = () => { if (!firstVisit.group) { firstVisit.group = true; scrollThread($('#groupThread'), false); } };
  enterHooks.person = () => scrollThread($('#personThread'), false);
  enterHooks.notebook = (fresh) => { if (fresh) openBook(); };
  enterHooks.organizer = () => renderTodos();
  enterHooks.space = () => (hoopEls.evening || []).forEach((svg) => setHoopStatic(svg, 'evening'));

  /* ------------------------------------------------------------- notes / demos */
  const notes = $('#notes');
  const fab = $('#notesFab');
  const scrim = $('#notesScrim');
  function toggleNotes(open) {
    notes.classList.toggle('is-open', open);
    scrim.hidden = !open;
    fab.setAttribute('aria-expanded', String(open));
    if (open) setTimeout(() => notes.focus({ preventScroll: true }), 50);
    else if (isMobile()) fab.focus({ preventScroll: true });
  }
  fab.addEventListener('click', () => toggleNotes(!notes.classList.contains('is-open')));
  let fabT = 0;
  app.addEventListener('scroll', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.classList.contains('scroll') && t.closest('.screen.is-root.is-active')) app.classList.toggle('cord-tucked', t.scrollTop > 8);
    if (isMobile()) {
      fab.classList.add('tucked');
      clearTimeout(fabT);
      fabT = setTimeout(() => fab.classList.remove('tucked'), 900);
    }
  }, true);
  scrim.addEventListener('click', () => toggleNotes(false));
  $('#notesClose').addEventListener('click', () => toggleNotes(false));
  function closeNotesIfSheet() { if (isMobile() && notes.classList.contains('is-open')) toggleNotes(false); }

  function markMap(id) {
    $$('#screenMap button').forEach((b) => b.setAttribute('aria-current', String(b.dataset.jump === id)));
  }
  /** At 700–1099 px the notes sit under the phone: scroll the phone into view first. */
  function revealDevice() {
    if (isMobile()) return Promise.resolve();
    const r = $('#device').getBoundingClientRect();
    if (r.top >= -40 && r.bottom <= window.innerHeight + 40) return Promise.resolve();
    window.scrollTo({ top: Math.max(0, window.scrollY + r.top - 16), behavior: reduced() ? 'auto' : 'smooth' });
    return wait(reduced() ? 60 : 480);
  }
  function jump(id) {
    closeNotesIfSheet();
    revealDevice().then(() => jumpNow(id));
  }
  function jumpNow(id) {
    if (id === 'splash') { playSplash(); return; }
    if (id === 'artiroom') {
      if (!sheetOpen) { if (current() !== 'chats') tab('chats'); setTimeout(openSheet, current() === 'chats' ? 0 : 260); }
      return;
    }
    if (!screens[id]) return;
    if (sheetOpen) closeSheet(true);
    if (id === 'person') setPerson('max');
    go(id);
  }
  function lightMoment(btn) {
    $$('.moment').forEach((m) => m.classList.remove('is-lit'));
    const m = btn && btn.closest('.moment');
    if (m) m.classList.add('is-lit');
  }
  let demoBusy = false;
  async function demo(name) {
    lightMoment($(`[data-demo="${name}"]`));
    closeNotesIfSheet();
    if (demoBusy) return;
    demoBusy = true;
    try {
      await revealDevice();
      if (name !== 'splash' && name !== 'presence' && sheetOpen) closeSheet(true);
      const settle = reduced() ? 120 : 520;
      switch (name) {
        case 'splash':
          playSplash();
          break;
        case 'lamp':
          if (!ROOTS.includes(current())) { tab('chats'); await wait(settle); }
          cord.focus({ preventScroll: true });
          pullCord();
          break;
        case 'presence': {
          if (sheetOpen) { closeSheet(true); await wait(300); }
          if (current() !== 'chats') { tab('chats'); await wait(settle); }
          const sc = $('.scroll', screens.chats);
          sc.scrollTo({ top: 0, behavior: reduced() ? 'auto' : 'smooth' });
          const p = $('.presence');
          p.classList.remove('is-pointed');
          void p.offsetWidth;
          p.classList.add('is-pointed');
          rotatePresence();
          await wait(reduced() ? 200 : 1300);
          openSheet();
          break;
        }
        case 'initiative': {
          go('arti');
          await wait(settle);
          const el = $('#initiative');
          scrollToEl($('#artiThread'), el);
          el.classList.remove('is-pointed');
          void el.offsetWidth;
          el.classList.add('is-pointed');
          await wait(reduced() ? 100 : 600);
          const p = $('.player', el)._player;
          if (p && !p.playing) { setPos(p, 29); play(p); }
          break;
        }
        case 'cards':
          go('arti');
          await wait(settle);
          resetDeck();
          scrollToEl($('#artiThread'), deck, 'center');
          await wait(reduced() ? 100 : 700);
          card3.classList.remove('nudge');
          void card3.offsetWidth;
          if (!reduced()) card3.classList.add('nudge');
          setTimeout(() => card3.classList.remove('nudge'), 2000);
          card3.focus({ preventScroll: true });
          break;
        case 'typing':
          go('arti');
          await wait(settle);
          send('arti', 'А если сегодня и музыка, и тишина?');
          break;
        case 'notebook':
          if (current() === 'notebook') openBook(); else go('notebook');
          break;
        case 'stitch': {
          go('project');
          await wait(settle + 100);
          const sc = $('#projectScroll');
          sc.scrollTo({ top: 0, behavior: reduced() ? 'auto' : 'smooth' });
          await wait(reduced() ? 50 : 350);
          let next = PROJ.find((t) => !t.done);
          if (!next) { PROJ.forEach((t, i) => { t.done = i < 2; }); syncProject(false); (hoopEls.evening || []).forEach((svg) => setHoopStatic(svg, 'evening')); renderTodos(); next = PROJ.find((t) => !t.done); await wait(200); }
          toggleTask(next.id);
          break;
        }
        case 'develop':
          go('image');
          await wait(settle);
          $('#imageScroll').scrollTo({ top: 120, behavior: reduced() ? 'auto' : 'smooth' });
          if (devState === 'done') variant += 1;
          develop();
          break;
        default:
          break;
      }
    } finally {
      demoBusy = false;
    }
  }
  function scrollToEl(scroller, el, block) {
    const top = el.offsetTop - (block === 'center' ? (scroller.clientHeight - el.offsetHeight) / 2 : 80);
    scroller.scrollTo({ top: Math.max(0, top), behavior: reduced() ? 'auto' : 'smooth' });
  }

  /* ------------------------------------------------------------- keyboard */
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!picker.hidden) { closePicker(true); e.preventDefault(); return; }
    if (!styleMenu.hidden) { closeStyleMenu(); styleBtn.focus(); e.preventDefault(); return; }
    if (!splash.hidden) { endSplash(); e.preventDefault(); return; }
    if (sheetOpen) { closeSheet(); e.preventDefault(); return; }
    if (notes.classList.contains('is-open')) { toggleNotes(false); e.preventDefault(); }
  });

  /* ------------------------------------------------------------- device fit */
  function fit() {
    const w = window.innerWidth, h = window.innerHeight;
    let s = 1;
    const main = $('.stage-main');
    const top = main.getBoundingClientRect().top + window.scrollY + (parseFloat(getComputedStyle(main).paddingTop) || 0);
    const avail = h - top - 16;
    if (w < 560) s = 1;
    else if (w >= 1100) s = Math.min(1, avail / 868);
    else s = Math.min(1, (w - 32) / 414, Math.max(0.6, avail / 868));
    const cs = $('#chatSearch');
    if (cs) cs.placeholder = w < 360 ? 'Поиск' : 'Поиск по чатам и сообщениям';
    s = Math.max(0.55, s);
    scale = w < 560 ? 1 : s;
    document.documentElement.style.setProperty('--s', scale.toFixed(4));
    placeRibbon(false);
    if (devState === 'done') placeDevArrow(false);
  }
  window.addEventListener('resize', fit);

  /* ------------------------------------------------------------- deep links */
  function parseHash() {
    const p = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    return { screen: p.get('screen'), theme: p.get('theme') };
  }
  window.addEventListener('hashchange', () => {
    const h = parseHash();
    if (h.theme === 'evening' || h.theme === 'day') setThemeMode(h.theme, { save: false });
    if (h.screen) jump(h.screen);
  });

  /* ------------------------------------------------------------- init */
  function init() {
    paintPortraits();
    $$('.player').forEach(buildPlayer);
    $$('.thread .msg').forEach(addReactBtn);
    buildHoops();
    renderTasks();
    syncProject(false);
    renderFiles();
    renderTodos();
    renderNotebook();
    setSeg($('#newTabs'), 'group', 'newtab');
    setSeg($('#todoTabs'), 'open', 'todotab');
    const savedInit = store.get('initiative');
    const initVal = savedInit && INIT_HINTS[savedInit] ? savedInit : 'sometimes';
    setSeg($('#initSeg'), initVal, 'init');
    $('#initHint').textContent = INIT_HINTS[initVal];
    setLife(store.get('life') === 'off' ? 'off' : 'on', false);
    setPerson('max');
    fit();

    const saved = store.get('theme');
    themeMode = saved === 'evening' || saved === 'day' || saved === 'auto' ? saved : 'day';
    const h = parseHash();
    if (h.theme === 'evening' || h.theme === 'day') themeMode = h.theme;
    applyTheme(false);
    if (store.get('pulled')) app.classList.add('cord-used');

    let start = h.screen || store.get('screen') || 'chats';
    if (start !== 'splash' && start !== 'artiroom' && !VALID.includes(start)) start = 'chats';
    const intro = (!h.screen || h.screen === 'splash') && (!reduced() || h.screen === 'splash');
    openDirect(start === 'splash' || start === 'artiroom' ? 'chats' : start);
    if (start === 'artiroom') openSheet();
    if (intro) playSplash();
    requestAnimationFrame(() => placeRibbon(false));
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => placeRibbon(false));
  }
  init();
})();
