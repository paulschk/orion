/* Public-site motion and navigation, in the manner of nestor.ink: a short
   fade-in-up, a drifting hero glow, sections that rise as they enter the
   viewport - and page changes fetched in the background and swapped into #main
   instead of a full reload. No framework; it degrades to plain links with
   JavaScript off, and to a real navigation if anything goes wrong. */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var FADE_MS = 250;          // must match the opacity transition on #main
  var mainEl = document.getElementById('main');
  var hero = null;
  var busy = false;

  // ------------------------------------------------------------------ helpers

  // The server marks the active link on load; after a swap we do it here.
  function setActive(path) {
    var here = (path || '/').replace(/\/$/, '') || '/';
    document.querySelectorAll('.pnav nav a:not(.btn)').forEach(function (a) {
      var href = (a.getAttribute('href') || '').replace(/\/$/, '') || '/';
      a.classList.toggle('active', href === here);
    });
  }

  function applyHead(doc) {
    if (doc.title) { document.title = doc.title; }
    [['meta[name="description"]', 'content'],
     ['link[rel="canonical"]', 'href'],
     ['meta[property="og:title"]', 'content'],
     ['meta[property="og:description"]', 'content'],
     ['meta[property="og:url"]', 'content']].forEach(function (pair) {
      var from = doc.querySelector(pair[0]);
      var to = document.querySelector(pair[0]);
      if (from && to) { to.setAttribute(pair[1], from.getAttribute(pair[1])); }
    });
  }

  // ------------------------------------------------------- page initialisation
  // Runs on load and again after every swap, so it must be idempotent.

  var revealObserver = null;

  function initPage(root) {
    root = root || document;
    hero = document.querySelector('.hero');

    root.querySelectorAll('.section-title').forEach(function (el) {
      if (el.querySelector('.section-dot')) { return; }
      var dot = document.createElement('span');
      dot.className = 'section-dot';
      dot.setAttribute('aria-hidden', 'true');
      el.prepend(dot);
    });

    var targets = root.querySelectorAll(
      'section .card, section .section-title, section .section-lead, section .pill');
    if (!targets.length) { return; }
    if (reduce || !('IntersectionObserver' in window)) {
      targets.forEach(function (el) { el.classList.add('in'); });
      return;
    }
    if (!revealObserver) {
      revealObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) { return; }
          var el = entry.target;
          var sibs = Array.prototype.filter.call(
            el.parentElement.children, function (c) { return c.classList.contains('reveal'); });
          var idx = Math.max(0, sibs.indexOf(el));
          el.style.transitionDelay = Math.min(idx * 60, 300) + 'ms';
          el.classList.add('in');
          revealObserver.unobserve(el);
        });
      }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });
    }
    targets.forEach(function (el) {
      el.classList.add('reveal');
      revealObserver.observe(el);
    });
  }

  // ------------------------------------------------------------------- scroll

  var ticking = false;
  function onScroll() {
    var nav = document.querySelector('.pnav');
    if (nav) { nav.classList.toggle('scrolled', window.scrollY > 12); }
    if (hero && !reduce) {
      var offset = Math.min(window.scrollY * 0.35, 90);
      hero.style.setProperty('--hero-shift', offset.toFixed(1) + 'px');
      hero.style.setProperty('--hero-fade', Math.max(0.15, 1 - offset / 320).toFixed(3));
    }
    ticking = false;
  }
  window.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(onScroll); }
  }, { passive: true });

  // ---------------------------------------------------------------- burger

  var pnavToggle = document.getElementById('pnav-toggle');
  var pnavMenu = document.getElementById('pnav-menu');

  function setPnav(open) {
    if (!pnavMenu) { return; }
    pnavMenu.classList.toggle('open', open);
    if (pnavToggle) {
      pnavToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      pnavToggle.setAttribute('aria-label', open ? 'Fermer le menu' : 'Ouvrir le menu');
    }
  }

  if (pnavToggle && pnavMenu) {
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

  // ----------------------------------------------------------- AJAX navigation

  function swap(html, path, push) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var incoming = doc.getElementById('main');
    if (!incoming) {
      // Not a page we can swap (the app shell has no #main): navigate properly.
      window.location.href = path;
      return;
    }
    applyHead(doc);

    var finish = function () {
      mainEl.innerHTML = incoming.innerHTML;
      mainEl.classList.remove('page-transition');
      void mainEl.offsetWidth;            // restart the fade-in-up
      mainEl.classList.add('page-transition');
      mainEl.style.opacity = '1';
      mainEl.removeAttribute('aria-busy');
      if (push) { history.pushState({ path: path }, '', path); }
      initPage(mainEl);
      setActive(path);
      window.scrollTo({ top: 0 });
      onScroll();
      busy = false;
    };

    if (reduce) { finish(); return; }
    // The fade-in-up animation fills forwards, and an animation outranks an
    // inline style, so the class has to come off before the opacity can drop.
    mainEl.classList.remove('page-transition');
    void mainEl.offsetWidth;
    mainEl.style.opacity = '0';
    setTimeout(finish, FADE_MS);
  }

  function navigate(path, push) {
    if (busy) { return; }
    busy = true;
    if (mainEl) { mainEl.setAttribute('aria-busy', 'true'); }
    fetch(path, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) { throw new Error('http ' + response.status); }
        return response.text();
      })
      .then(function (html) { swap(html, path, push); })
      .catch(function () {
        // Let the browser handle it: a real navigation always works.
        busy = false;
        if (mainEl) { mainEl.removeAttribute('aria-busy'); }
        window.location.href = path;
      });
  }

  function isSwapable(anchor) {
    if (!anchor || anchor.hasAttribute('data-no-ajax') || anchor.hasAttribute('download')) {
      return false;
    }
    if (anchor.target && anchor.target !== '_self') { return false; }
    var href = anchor.getAttribute('href');
    if (!href || /^(mailto:|tel:|javascript:)/.test(href)) { return false; }
    if (href.charAt(0) === '#') { return false; }
    var url = new URL(anchor.href, window.location.href);
    if (url.origin !== window.location.origin) { return false; }
    // Static files, the session endpoints and the panel's own pages are not
    // documents we can swap: leave them to the browser.
    if (url.pathname.indexOf('/static/') === 0) { return false; }
    if (url.pathname === '/logout' || url.pathname === '/healthz') { return false; }
    if (/^\/(app|admin)(\/|$)/.test(url.pathname)) { return false; }
    return true;
  }

  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0) { return; }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
    var anchor = event.target.closest('a');
    if (!isSwapable(anchor)) { return; }
    var url = new URL(anchor.href, window.location.href);
    if (url.pathname === window.location.pathname && url.search === window.location.search) {
      return;                                  // same page: let the browser reload
    }
    event.preventDefault();
    setPnav(false);
    navigate(url.pathname + url.search, true);
  });

  window.addEventListener('popstate', function () {
    navigate(window.location.pathname + window.location.search, false);
  });

  // --------------------------------------------------------------------- boot

  if (mainEl && !reduce) { mainEl.classList.add('page-transition'); }
  initPage(document);
  onScroll();
})();
