/* Landing page behaviour: the mobile menu, the header's scrolled state, the
 * pipeline animation trigger, and the request form's interest preset. */
(() => {
  'use strict';
  const landing = document.querySelector('.glass-landing');
  if (!landing) return;

  // --- Mobile menu --------------------------------------------------------
  const nav = landing.querySelector('#site-nav');
  const toggle = landing.querySelector('#nav-toggle');
  function closeMenu(returnFocus) {
    nav.classList.remove('is-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open navigation');
    if (returnFocus) toggle.focus();
  }
  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    nav.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
  });
  nav.addEventListener('click', event => { if (event.target.closest('a')) closeMenu(false); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') closeMenu(true);
  });
  window.matchMedia('(min-width: 961px)').addEventListener('change', event => {
    if (event.matches) closeMenu(false);
  });

  // --- Header gains a shadow once the page has scrolled -------------------
  const header = landing.querySelector('[data-header]');
  const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 8);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // --- Pipeline: run the draw-in once, when it comes into view ------------
  const pipe = landing.querySelector('[data-pipe]');
  if (pipe) {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced || !('IntersectionObserver' in window)) {
      pipe.classList.add('is-done');
    } else {
      pipe.classList.add('is-armed');
      const observer = new IntersectionObserver(entries => {
        if (!entries[0].isIntersecting) return;
        pipe.classList.add('is-in');
        observer.disconnect();
      }, { threshold: 0.35 });
      observer.observe(pipe);
    }
  }

  // --- Features: cards rise in, staggered, the first time they show -------
  // The classes come off once the last card lands, so the animation's fill
  // no longer pins the cards and the hover lift works.
  const reveal = landing.querySelector('[data-reveal]');
  const motionOk = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reveal && motionOk && 'IntersectionObserver' in window) {
    reveal.classList.add('is-armed');
    const settle = () => reveal.classList.remove('is-armed', 'is-in');
    const observer = new IntersectionObserver(entries => {
      if (!entries[0].isIntersecting) return;
      observer.disconnect();
      reveal.classList.add('is-in');
      const last = reveal.lastElementChild;
      if (last) last.addEventListener('animationend', settle, { once: true });
      window.setTimeout(settle, 2500);  // in case animationend never fires
    }, { threshold: 0.15 });
    observer.observe(reveal);
  }

  // --- Request form: "Get a licence" links preselect the desktop option ----
  function preset(value) {
    const radio = landing.querySelector('.request-form input[name="interest"][value="' + value + '"]');
    if (radio) radio.checked = true;
  }
  landing.querySelectorAll('[data-interest]').forEach(link => {
    link.addEventListener('click', event => {
      event.preventDefault();
      preset(link.dataset.interest);
      const target = document.getElementById('request-access');
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const first = target.querySelector('input:not([type=hidden]):not([tabindex="-1"])');
      if (first) first.focus({ preventScroll: true });
    });
  });

  // After a submit, move focus to the outcome so it is announced.
  const done = landing.querySelector('[data-request-done]');
  if (done) done.focus();
  const firstError = landing.querySelector('.request-form [aria-invalid="true"]');
  if (firstError) firstError.focus();
})();
