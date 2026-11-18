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
  // The pane, not just something called `terminal`: a page that carries the id
  // twice - the tile and the pane - hands back whichever comes first, and every
  // read below then answers for the wrong element. That is not a small mistake:
  // `pane.dataset.state` came back empty, the module took the "not running"
  // branch, and the exception it raised there ended the script before the
  // keyboard handler and the sizing were ever installed.
  if (!pane || !pane.classList.contains('term-pane') || !window.Terminal) return;

  // Le plein écran, branché avant tout le reste : il marche aussi sur une
  // instance arrêtée — l'écran dit alors pourquoi il n'y a pas de session. Le
  // navigateur fournit Échap, et le redimensionnement qui suit le changement.
  const fsButton = document.getElementById('term-fs');
  if (fsButton) {
    if (!pane.requestFullscreen) {
      fsButton.hidden = true;             // API absente : offrir le bouton mentirait
    } else {
      fsButton.addEventListener('click', function () {
        if (document.fullscreenElement === pane) document.exitFullscreen();
        else pane.requestFullscreen();
      });
      document.addEventListener('fullscreenchange', function () {
        fsButton.textContent = document.fullscreenElement === pane ? 'Quitter' : 'Plein écran';
        // Ne pas s'en remettre à l'événement du navigateur pour le
        // redimensionnement : on recalcule ici, dans la frame qui suit, quand le
        // nouveau cadre est en place. Le calcul lit lui-même l'état plein écran,
        // donc `fitToWholeRows` fait ce qu'il faut dans les deux sens.
        requestAnimationFrame(fitToWholeRows);
      });
    }
  }

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

  // Declared before anything can call the function that uses them:
  // `fitToWholeRows` is a hoisted declaration, but these are `const`s, and a
  // call arriving early - the "not running" branch, a ResizeObserver's first
  // callback - would hit the temporal dead zone and end the whole script.
  const TARGET_HEIGHT = 460;
  const BASE_PAD = 8;      // the stylesheet's own bottom padding
  const EXTRA_ROWS = 3;    // rows of clearance kept under the last line

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
  // TARGET_HEIGHT and BASE_PAD are declared at the top of this module, so that
  // a call arriving before this point cannot find them uninitialised.
  // A few rows of clearance under the last line. The prompt lives on the final
  // row of the screen, and with the pane snapped flush to that row it sat
  // against the pane's bottom edge - which reads as though it were falling out
  // of the window, and is impossible to see when the pane's foot is at the
  // window's foot. Three rows lift it clear without giving up a single line of
  // scrollback.
  // `fixed` deliberately does not read padding-bottom: that is the value this
  // function writes, so feeding it back is the loop that folds the pane.
  function fitToWholeRows() {
    const xterm = pane.querySelector('.xterm');
    const screen = pane.querySelector('.xterm-screen');
    if (!xterm || !screen) return;
    const style = getComputedStyle(pane);
    // The pane's own top chrome only: the bottom padding is written below, so
    // reading it here would feed the height back into itself.
    const fixed = ['paddingTop', 'borderTopWidth', 'borderBottomWidth']
      .reduce(function (total, key) { return total + (parseFloat(style[key]) || 0); }, 0);

    const before = screen.getBoundingClientRect();
    // Both dimensions are measured from what the renderer actually produced.
    //
    // Sauf que la boîte de l'écran vaut la hauteur *visible* dès que le panneau
    // est plus court que son contenu : la cellule qu'on en déduit sort alors trop
    // petite, on demande trop de rangées, et le débord apparaît dès que le
    // navigateur impose la hauteur du panneau — c'est le cas en plein écran.
    // Le moteur, lui, connaît la cellule indépendamment du cadre ; c'est la
    // source, et la mesure de la boîte reste le repli si l'interne disparaît.
    // FitAddon, de son côté, ne propose toujours rien dans ce build.
    const engine = term._core && term._core._renderService && term._core._renderService.dimensions;
    const engineCell = engine && engine.css && engine.css.cell ? engine.css.cell : null;
    const cellH = (engineCell && engineCell.height) || (term.rows > 0 ? before.height / term.rows : 0);
    const cellW = (engineCell && engineCell.width) || (term.cols > 0 ? before.width / term.cols : 0);
    if (!cellH || !cellW) { fit.fit(); return; }

    // clientWidth already excludes the borders, so only the padding comes off
    // the usable width.
    const usable = pane.clientWidth - (parseFloat(style.paddingLeft) || 0)
      - (parseFloat(style.paddingRight) || 0);
    const cols = Math.max(20, Math.floor(usable / cellW));
    // Plein écran : le navigateur impose sa hauteur au panneau, donc celle écrite
    // ici est ignorée et c'est `clientHeight` qui dit la place réelle — la lire ne
    // referme pas la boucle, puisqu'elle n'est pas la valeur qu'on écrit. Hors
    // plein écran, on garde la cible fixe de la maquette, précisément pour cette
    // raison : un panneau qui se relit se replie une rangée à la fois.
    const target = document.fullscreenElement === pane
      ? pane.clientHeight - (BASE_PAD + EXTRA_ROWS * cellH)
      : TARGET_HEIGHT;
    const rows = Math.max(2, Math.floor((target - fixed) / cellH));

    if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);

    // Measure what the screen really occupies instead of modelling it. The
    // renderer offsets the screen a few pixels inside its container - 6px here -
    // and a model that forgets that offset leaves the container one pixel short,
    // which is still an overflow even if `overflow: hidden` makes it invisible.
    // The offset is a property of the layout (it follows the width, not the
    // height), so reading it here cannot feed back into the height written.
    const xtermRect = xterm.getBoundingClientRect();
    const screenRect = screen.getBoundingClientRect();
    const xtermStyle = getComputedStyle(xterm);
    const screenTop = screenRect.top - xtermRect.top
      - (parseFloat(xtermStyle.borderTopWidth) || 0) - (parseFloat(xtermStyle.paddingTop) || 0);
    const needed = screenTop + screenRect.height;

    // The clearance belongs to the *pane's* bottom padding, not to the inside of
    // the terminal. The viewport is stretched over its container, so space
    // reserved inside `.xterm` puts the foot of the scrollable area below the
    // last line of text - the wheel keeps scrolling past the end of the output
    // and the viewport no longer ends where the terminal does.
    const padBottom = BASE_PAD + EXTRA_ROWS * cellH;
    if (pane.style.paddingBottom !== padBottom + 'px') pane.style.paddingBottom = padBottom + 'px';

    // Not rounded: a whole-pixel container around a fractional screen is exactly
    // the remainder this is meant to remove.
    const wanted = needed + fixed + padBottom + 'px';
    if (pane.style.height !== wanted) pane.style.height = wanted;

    // En plein écran, le navigateur impose sa hauteur au panneau : celle écrite
    // juste au-dessus est ignorée, et seule la cellule mesurée décide du débord.
    // Elle vient de ce que le moteur a produit, mais un changement de cadre peut
    // le faire rendre à une autre échelle que celle mesurée avant. On regarde donc
    // ce qui est vraiment rendu, et on retire jusqu'à deux rangées — une
    // correction unique et bornée, jamais une boucle : un redimensionnement qui
    // se relit est précisément ce qui repliait le panneau une ligne à la fois.
    const after = screen.getBoundingClientRect();
    const over = after.bottom - (pane.getBoundingClientRect().bottom - padBottom);
    if (over > 1 && term.rows > 3) {
      const drop = Math.min(2, Math.ceil(over / cellH));
      if (drop > 0) term.resize(term.cols, Math.max(2, term.rows - drop));
    }
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
