/* Small helpers. Everything else is server-rendered. */

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
  if (known === undefined || known === next) return;
  if (next === 'running' || next === 'error') {
    window.location.reload();
  }
});

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
