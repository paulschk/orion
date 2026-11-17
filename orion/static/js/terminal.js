/* Interactive terminal: xterm.js over the panel's websocket PTY bridge.
 *
 * Wire format is raw bytes in both directions; control messages are JSON text
 * frames. Bytes in, bytes out, so nothing has to survive a UTF-8 round trip.
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
    scrollback: 5000,
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

  const state = pane.dataset.state || '';
  if (state !== 'running') {
    fit.fit();
    term.writeln('\x1b[90mInstance ' + (state === 'stopped' ? 'arrêtée' : state) +
                 ' — démarrez-la pour ouvrir une session.\x1b[0m');
    setStatus('indisponible', 'dim');
    return;
  }

  let ws = null;
  let finished = false;
  const encoder = new TextEncoder();

  const CLOSE_REASONS = {
    4401: 'session expirée, rechargez la page',
    4403: 'origine refusée',
    4404: 'accès refusé',
    4409: 'instance non démarrée',
    4429: 'trop de sessions ouvertes',
    1011: 'erreur interne'
  };

  function connect() {
    setStatus('connexion…');
    ws = new WebSocket(pane.dataset.ws);
    ws.binaryType = 'arraybuffer';

    ws.onopen = function () {
      setStatus('connecté', 'ok');
      sendResize();
      term.focus();
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
      term.write(new Uint8Array(event.data));
    };

    ws.onerror = function () {
      if (!finished) setStatus('erreur de connexion', 'bad');
    };

    ws.onclose = function (event) {
      if (finished) return;
      finished = true;
      const why = CLOSE_REASONS[event.code];
      setStatus(why || 'session terminée', 'dim');
      term.writeln('\r\n\x1b[90m[session terminée' + (why ? ' — ' + why : '') +
                   ']\x1b[0m');
      term.writeln('\x1b[90mRechargez la page pour en ouvrir une nouvelle.\x1b[0m');
    };
  }

  function sendResize() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
  }

  term.onData(function (data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
  });
  term.onResize(sendResize);

  if (window.ResizeObserver) {
    let raf = null;
    new ResizeObserver(function () {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(function () { fit.fit(); });
    }).observe(pane);
  }
  window.addEventListener('resize', function () { fit.fit(); });

  fit.fit();
  connect();
})();
