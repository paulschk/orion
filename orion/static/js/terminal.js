/* Interactive terminal: xterm.js over the panel's websocket PTY bridge.
 *
 * Wire format is raw bytes in both directions; control messages are JSON text
 * frames. Bytes in, bytes out, so nothing has to survive a UTF-8 round trip.
 *
 * A terminal is the most latency-sensitive thing the panel serves: every
 * keystroke is a round trip, and a single dropped frame is immediately visible.
 * So this reconnects on its own, and keeps the connection warm in between.
 */
(function () {
  const pane = document.getElementById('terminal');
  if (!pane || !window.Terminal) return;

  const statusEl = document.getElementById('terminal-status');
  const setStatus = (text, tone) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = 'sub' + (tone ? ' ' + tone : '');
  };

  const css = getComputedStyle(document.documentElement);
  const term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: css.getPropertyValue('--mono').trim() || 'monospace',
    scrollback: 10000,
    // Wheel behaviour, made deliberate rather than incidental:
    // - animated by a fixed duration, so a notch always moves the same amount
    //   instead of depending on the device;
    // - typing returns the view to the prompt, which is what a shell user
    //   expects after scrolling back through history;
    // - Alt jumps further for long output.
    smoothScrollDuration: 90,
    scrollOnUserInput: true,
    fastScrollModifier: 'alt',
    fastScrollSensitivity: 5,
    theme: {
      background: '#05070d',
      foreground: '#e0e0e0',
      cursor: '#3b82f6',
      selectionBackground: '#1f2937',
      black: '#0b0c10', red: '#fca5a5', green: '#86efac', yellow: '#fcd34d',
      blue: '#93c5fd', magenta: '#c4b5fd', cyan: '#67e8f9', white: '#e0e0e0'
    }
  });

  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(pane);
  // Reachable from the console: measuring scroll behaviour needs the instance.
  window.orionTerm = term;

  const state = pane.dataset.state || '';
  if (state !== 'running') {
    fitToWholeRows();
    term.writeln('\x1b[90mInstance ' + (state === 'stopped' ? 'arrêtée' : state) +
                 ' — démarrez-la pour ouvrir une session.\x1b[0m');
    setStatus('indisponible', 'dim');
    return;
  }

  // Codes the server closes with on purpose. Reconnecting cannot help, so they
  // are reported and the terminal is left alone.
  const DEFINITIVE = {
    4401: 'session expirée, rechargez la page',
    4403: 'origine refusée',
    4404: 'accès refusé',
    4409: 'instance non démarrée',
    4429: 'trop de sessions ouvertes'
  };
  const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
  const KEEPALIVE_MS = 25000;

  let ws = null;
  let attempt = 0;
  let retryTimer = null;
  let keepaliveTimer = null;
  let stopped = false;
  const encoder = new TextEncoder();

  function stopKeepalive() {
    if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
  }

  function reconnect(reason) {
    if (stopped || retryTimer) return;
    const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    setStatus('reconnexion dans ' + Math.round(wait / 1000) + ' s…', 'warn');
    term.writeln('\r\n\x1b[33m[connexion perdue' + (reason ? ' — ' + reason : '') +
                 '] nouvelle tentative dans ' + Math.round(wait / 1000) + ' s.\x1b[0m');
    retryTimer = setTimeout(function () { retryTimer = null; connect(); }, wait);
  }

  function connect() {
    if (stopped) return;
    setStatus(attempt ? 'reconnexion…' : 'connexion…');
    ws = new WebSocket(pane.dataset.ws);
    ws.binaryType = 'arraybuffer';

    ws.onopen = function () {
      const wasRetry = attempt > 0;
      attempt = 0;
      setStatus('connecté', 'ok');
      sendResize();
      term.focus();
      // The old shell died with the old socket; say so rather than pretending
      // the session survived.
      if (wasRetry) {
        term.writeln('\x1b[90m[session rétablie — un nouveau shell a été ouvert]\x1b[0m');
      }
      stopKeepalive();
      // A relay or a NAT can drop a connection that has been silent. A control
      // frame the server ignores costs nothing and keeps it warm.
      keepaliveTimer = setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, KEEPALIVE_MS);
    };

    ws.onmessage = function (event) {
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'error') {
            term.writeln('\r\n\x1b[31m[orion] ' + msg.message + '\x1b[0m');
          }
        } catch (e) { /* not a control frame */ }
        return;
      }
      term.write(new Uint8Array(event.data), refreshJump);
    };

    ws.onerror = function () {
      /* onclose always follows; act there so there is one path. */
    };

    ws.onclose = function (event) {
      stopKeepalive();
      const why = DEFINITIVE[event.code];
      if (why) {
        stopped = true;
        setStatus(why, 'dim');
        term.writeln('\r\n\x1b[90m[session terminée — ' + why + ']\x1b[0m');
        return;
      }
      reconnect(event.code === 1006 ? 'liaison interrompue' : 'code ' + event.code);
    };
  }

  function sendResize() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
  }

  // A scrollbar nobody styled is the same scrollbar nobody can find: it comes
  // and goes with the platform's overlay behaviour, so the terminal seems to
  // scroll only sometimes. It is styled in the stylesheet; this keeps the
  // marker honest about where in the buffer the view actually sits.
  const jump = document.createElement('button');
  jump.type = 'button';
  jump.className = 'term-jump';
  jump.textContent = '\u2193 Dernières lignes';
  jump.hidden = true;
  jump.addEventListener('click', function () {
    term.scrollToBottom();
    term.focus();
  });
  pane.appendChild(jump);

  function refreshJump() {
    const buf = term.buffer.active;
    jump.hidden = buf.viewportY >= buf.baseY;
  }
  if (term.onScroll) term.onScroll(refreshJump);
  // The viewport is a real scroller, so its own event is the one that cannot be
  // missed. xterm's onScroll does not fire on every path that moves the view -
  // notably not for a wheel the custom handler above has consumed, which is
  // precisely when the marker matters.
  const viewport = pane.querySelector('.xterm-viewport');
  if (viewport) viewport.addEventListener('scroll', refreshJump, { passive: true });
  window.addEventListener('resize', refreshJump);

  // The wheel belongs to the terminal while there is history to move through,
  // and is handed back to the page once the view reaches either end. Without
  // this the page behind scrolls whenever the buffer is at an end - which is
  // exactly the "it scrolls the wrong thing" behaviour.
  term.attachCustomWheelEventHandler(function (event) {
    if (!event.deltaY) return true;
    const buf = term.buffer.active;
    const scrollingUp = event.deltaY < 0;
    if (scrollingUp ? buf.viewportY <= 0 : buf.viewportY >= buf.baseY) {
      return false;                       // nothing left here: let the page have it
    }
    return true;                          // xterm consumes it and preventDefaults
  });

  // A terminal is made of whole rows, so a pane of an arbitrary height always
  // leaves a strip of empty background under the last line. It is snapped to a
  // whole number of rows - computed from a *fixed* target and the measured
  // chrome, never from the pane's current height. Reading the height you are
  // about to write turns this into a loop that takes a row off on every pass:
  // the pane ends up 2px short (the borders), the next measurement no longer
  // fits the last row, and the terminal folds itself away a line at a time.
  const TARGET_HEIGHT = 460;
  const BASE_PAD = 8;      // the stylesheet's own bottom padding
  // A few rows of clearance under the last line. The prompt lives on the final
  // row of the screen, and with the pane snapped flush to that row it sat
  // against the pane's bottom edge - which reads as though it were falling out
  // of the window, and is impossible to see when the pane's foot is at the
  // window's foot. Three rows lift it clear without giving up a single line of
  // scrollback.
  const EXTRA_ROWS = 3;
  // `fixed` deliberately does not read padding-bottom: that is the value this
  // function writes, so feeding it back is the loop that folds the pane.
  function fitToWholeRows() {
    const screen = pane.querySelector('.xterm-screen');
    if (!screen) return;
    const style = getComputedStyle(pane);
    const fixed = BASE_PAD + ['paddingTop', 'borderTopWidth', 'borderBottomWidth']
      .reduce(function (total, key) { return total + (parseFloat(style[key]) || 0); }, 0);

    const cell = term.rows > 0 ? screen.getBoundingClientRect().height / term.rows : 0;
    if (!cell) { fit.fit(); return; }

    const proposal = fit.proposeDimensions && fit.proposeDimensions();
    const cols = (proposal && proposal.cols) || term.cols;
    const rows = Math.max(2, Math.floor((TARGET_HEIGHT - fixed) / cell));

    const padBottom = Math.round(BASE_PAD + EXTRA_ROWS * cell);
    if (pane.style.paddingBottom !== padBottom + 'px') pane.style.paddingBottom = padBottom + 'px';

    if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);
    const wanted = Math.round(rows * cell + fixed + EXTRA_ROWS * cell) + 'px';
    if (pane.style.height !== wanted) pane.style.height = wanted;
  }

  term.onData(function (data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
  });
  term.onResize(sendResize);

  if (window.ResizeObserver) {
    let raf = null;
    new ResizeObserver(function () {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(fitToWholeRows);
    }).observe(pane);
  }
  window.addEventListener('resize', fitToWholeRows);

  // Browsers throttle timers in a hidden tab, so a backgrounded session often
  // comes back dropped. Reconnect the moment the tab is looked at again.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || stopped) return;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    attempt = 0;
    connect();
  });

  // The cell height is a property of the font: measuring it before the webfont
  // has loaded sizes the pane for the fallback and leaves it wrong afterwards.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(fitToWholeRows).catch(function () {});
  }

  fitToWholeRows();
  connect();
})();
