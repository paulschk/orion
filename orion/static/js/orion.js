/* Small helpers. Everything else is server-rendered. */

// Cross-fade page changes instead of flashing white between navigations.
if (window.htmx) { htmx.config.globalViewTransitions = true; }

// Poll the status fragment *only* while something is actually happening. It
// used to keep firing every 3 seconds forever, which made the page twitch.
function orionSyncPolling() {
  const box = document.getElementById('instance-status');
  if (!box) return;
  const status = box.dataset.knownStatus || '';
  const quiet = ['running', 'stopped', 'error', 'destroyed'];
  if (quiet.indexOf(status) !== -1) {
    if (box.hasAttribute('hx-trigger')) {
      box.removeAttribute('hx-trigger');
      box.removeAttribute('hx-get');
      if (window.htmx) { htmx.process(box); }
    }
  }
}
document.addEventListener('DOMContentLoaded', orionSyncPolling);
document.addEventListener('htmx:load', orionSyncPolling);

// Ease the provisioning bar towards the value the server reported, so progress
// moves continuously instead of stepping every poll.
let orionBarShown = 0;
setInterval(function () {
  const span = document.querySelector('#instance-status .bar > span');
  if (!span) { orionBarShown = 0; return; }
  const target = Number(span.dataset.target || 0);
  if (!orionBarShown) { orionBarShown = Math.min(target || 2, 3); }
  if (orionBarShown < target) {
    orionBarShown = Math.min(target, orionBarShown + Math.max(0.7, (target - orionBarShown) * 0.16));
  } else {
    orionBarShown = target;
  }
  span.style.width = orionBarShown.toFixed(1) + '%';
}, 500);


// Template picker on the "new instance" page.
document.addEventListener('click', function (event) {
  const tpl = event.target.closest('.tpl');
  if (!tpl) return;
  const group = tpl.closest('.tpl-pick');
  if (!group) return;
  group.querySelectorAll('.tpl').forEach((el) => el.classList.remove('sel'));
  tpl.classList.add('sel');
  const radio = tpl.querySelector('input[type=radio]');
  if (radio) { radio.checked = true; }
});

// Plan picker: keep the price preview in sync.
document.addEventListener('change', function (event) {
  const picker = event.target.closest('[data-plan-picker]');
  if (!picker) return;
  const target = document.querySelector(picker.dataset.planPicker);
  if (!target) return;
  const option = picker.selectedOptions[0];
  if (option && option.dataset.summary) { target.textContent = option.dataset.summary; }
});

// Confirm destructive forms by typing the instance name.
document.addEventListener('submit', function (event) {
  const form = event.target;
  if (!form.matches('[data-confirm-slug]')) return;
  const input = form.querySelector('input[name=confirm]');
  if (input && input.value.trim() !== form.dataset.confirmSlug) {
    event.preventDefault();
    input.focus();
    input.setAttribute('placeholder', 'Nom exact requis : ' + form.dataset.confirmSlug);
    input.style.borderColor = '#f87171';
  }
});

// Console: keep the last command in the box and focus it on load.
document.addEventListener('htmx:afterSwap', function (event) {
  if (event.detail.target && event.detail.target.id === 'console-out') {
    const box = event.detail.target;
    box.scrollTop = box.scrollHeight;
  }
});

// Copy-to-clipboard for the SSH / credentials blocks.
document.addEventListener('click', function (event) {
  const btn = event.target.closest('[data-copy]');
  if (!btn) return;
  event.preventDefault();
  const text = btn.dataset.copy;
  navigator.clipboard.writeText(text).then(function () {
    const original = btn.textContent;
    btn.textContent = 'Copié';
    setTimeout(function () { btn.textContent = original; }, 1400);
  });
});

// The status fragment polls itself. When the status actually changes - most
// importantly provisioning -> running - reload once so the sections that only
// make sense on a live instance (terminal, SSH details, snapshots) appear
// without the user having to refresh by hand.
document.body.addEventListener('htmx:afterSwap', function (event) {
  const container = event.detail && event.detail.elt;
  if (!container || container.id !== 'instance-status') return;
  const fresh = container.querySelector('[data-instance-status]');
  if (!fresh) return;
  const next = fresh.dataset.instanceStatus;
  const known = container.dataset.knownStatus;
  container.dataset.knownStatus = next;
  orionSyncPolling();
  if (known === undefined || known === next) return;
  if (next === 'running' || next === 'error') {
    // The sections that only make sense on a live instance (terminal, SSH
    // details, snapshots) are server-rendered, so reload exactly once here.
    window.location.reload();
  }
});


// App shell: below the breakpoint the sidebar is an off-canvas drawer opened
// from the burger in the top bar. It used to wrap into a cramped row of links.
(function () {
  var toggle = document.getElementById('side-toggle');
  var side = document.getElementById('side');
  var scrim = document.getElementById('side-scrim');
  if (!toggle || !side) return;

  function setOpen(open) {
    side.classList.toggle('open', open);
    if (scrim) scrim.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? 'Fermer le menu' : 'Ouvrir le menu');
    document.body.style.overflow = open ? 'hidden' : '';
  }
  function close() { setOpen(false); }

  toggle.addEventListener('click', function (e) {
    e.stopPropagation();
    setOpen(!side.classList.contains('open'));
  });
  if (scrim) scrim.addEventListener('click', close);
  side.addEventListener('click', function (e) {
    if (e.target.closest('a')) close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') close();
  });
  var wide = window.matchMedia('(min-width: 901px)');
  if (wide.addEventListener) wide.addEventListener('change', close);
  else if (wide.addListener) wide.addListener(close);
  close();
})();

// The provisioning loader is replaced every couple of seconds. Two things have
// to survive that. The bar must never go backwards: a step that happens to
// report a smaller number reads as the installation starting over, and the
// previous value is only in the DOM that htmx has just thrown away, so it is
// kept here keyed by instance. And the log must stay pinned to its end, unless
// the reader has scrolled up to look at something.
// Both flags have to live outside the DOM: the box is replaced on every poll,
// so anything written onto the element is gone exactly when it is needed. The
// follow flag especially - a reader who has scrolled up to read something must
// not be yanked back to the end two seconds later.
const progressSeen = new Map();
const logFollowing = new Map();

function holdProgress() {
  const box = document.getElementById('instance-progress');
  if (!box) return;
  const key = box.dataset.instanceId || '';
  const fill = box.querySelector('.bar > span[data-target]');
  if (fill) {
    const wanted = Math.max(parseInt(fill.dataset.target, 10) || 0, progressSeen.get(key) || 0);
    progressSeen.set(key, wanted);
    fill.style.width = wanted + '%';
    const pct = box.querySelector('.loader-pct');
    if (pct) pct.textContent = wanted + '%';
  }
  const log = box.querySelector('.loader-log');
  if (log && logFollowing.get(key) !== false) log.scrollTop = log.scrollHeight;
}

document.body.addEventListener('htmx:afterSwap', holdProgress);
document.addEventListener('scroll', function (event) {
  const log = event.target.closest && event.target.closest('.loader-log');
  if (!log) return;
  const box = log.closest('#instance-progress');
  const key = box ? box.dataset.instanceId || '' : '';
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  logFollowing.set(key, atEnd);
}, true);

// Reveal a stored credential once.
document.addEventListener('click', function (event) {
  const btn = event.target.closest('[data-reveal]');
  if (!btn) return;
  event.preventDefault();
  const target = document.querySelector(btn.dataset.reveal);
  if (target) {
    target.textContent = btn.dataset.value;
    btn.remove();
  }
});
