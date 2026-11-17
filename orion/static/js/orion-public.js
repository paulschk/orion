/* Public-site motion. Same feel as nestor.ink: a short fade-in-up on load, a
   slow drifting hero glow, and sections that rise as they enter the viewport.
   Deliberately small - no framework, ~2 KB, and it degrades to static HTML. */
(function () {
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var main = document.getElementById('main');
  if (main && !reduce) { main.classList.add('page-transition'); }

  // Sticky nav picks up a backdrop once the page has moved.
  var nav = document.querySelector('.pnav');
  function onScroll() {
    if (nav) { nav.classList.toggle('scrolled', window.scrollY > 12); }
    if (hero && !reduce) {
      var offset = Math.min(window.scrollY * 0.35, 90);
      hero.style.setProperty('--hero-shift', offset.toFixed(1) + 'px');
      hero.style.setProperty('--hero-fade', Math.max(0.15, 1 - offset / 320).toFixed(3));
    }
    ticking = false;
  }
  var hero = document.querySelector('.hero');
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(onScroll); }
  }, { passive: true });
  onScroll();

  // A pulsing dot in front of each section title, as on the portfolio site.
  document.querySelectorAll('.section-title').forEach(function (el) {
    var dot = document.createElement('span');
    dot.className = 'section-dot';
    dot.setAttribute('aria-hidden', 'true');
    el.prepend(dot);
  });

  var targets = document.querySelectorAll(
    'section .card, section .section-title, section .section-lead, section .pill');
  if (!targets.length) { return; }
  if (reduce || !('IntersectionObserver' in window)) {
    targets.forEach(function (el) { el.classList.add('in'); });
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) { return; }
      var el = entry.target;
      var sibs = Array.prototype.filter.call(
        el.parentElement.children, function (c) { return c.classList.contains('reveal'); });
      var idx = Math.max(0, sibs.indexOf(el));
      el.style.transitionDelay = Math.min(idx * 60, 300) + 'ms';
      el.classList.add('in');
      io.unobserve(el);
    });
  }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });

  targets.forEach(function (el) {
    el.classList.add('reveal');
    io.observe(el);
  });

  // ---------------------------------------------------------------- burger
  // A real disclosure control: aria-expanded, closes on Escape, on an outside
  // click, and on link activation - the things a client actually expects.
  var pnavToggle = document.getElementById('pnav-toggle');
  var pnavMenu = document.getElementById('pnav-menu');
  if (pnavToggle && pnavMenu) {
    var setPnav = function (open) {
      pnavMenu.classList.toggle('open', open);
      pnavToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      pnavToggle.setAttribute('aria-label', open ? 'Fermer le menu' : 'Ouvrir le menu');
    };
    pnavToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      setPnav(pnavToggle.getAttribute('aria-expanded') !== 'true');
    });
    pnavMenu.addEventListener('click', function (e) {
      if (e.target.closest('a')) { setPnav(false); }
    });
    document.addEventListener('click', function (e) {
      if (!pnavMenu.contains(e.target) && !pnavToggle.contains(e.target)) { setPnav(false); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { setPnav(false); }
    });
    window.addEventListener('resize', function () {
      if (window.innerWidth > 768) { setPnav(false); }
    });
  }
})();
