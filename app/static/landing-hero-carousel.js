/* Approved landing interactions. Illustrative stages never perform payroll actions. */
(() => {
  'use strict';
  const landing = document.querySelector('.glass-landing');
  if (!landing) return;
  const nav = landing.querySelector('#site-nav');
  const toggle = landing.querySelector('#nav-toggle');
  function closeMenu(returnFocus = false) {
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
  nav.addEventListener('click', event => { if (event.target.closest('a')) closeMenu(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') closeMenu(true);
  });
  window.matchMedia('(min-width: 701px)').addEventListener('change', event => {
    if (event.matches) closeMenu();
  });
  landing.querySelectorAll('[data-workspace-tab]').forEach(tab => {
    tab.addEventListener('click', () => {
      landing.querySelectorAll('[data-workspace-tab]').forEach(button => {
        button.setAttribute('aria-pressed', String(button === tab));
      });
      landing.querySelectorAll('[data-workspace-panel]').forEach(panel => {
        panel.hidden = panel.dataset.workspacePanel !== tab.dataset.workspaceTab;
      });
    });
  });
})();

(() => {
  'use strict';
  const carousel = document.getElementById('hero-carousel');
  if (!carousel) return;
  const slides = [...carousel.querySelectorAll('.hero-slide')];
  const dots = [...carousel.querySelectorAll('[data-carousel-slide]')];
  const toggle = document.getElementById('carousel-toggle');
  const title = document.getElementById('carousel-slide-title');
  const counter = document.getElementById('carousel-counter');
  const status = document.getElementById('carousel-status');
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const INTERVAL = 5000;
  let index = 0;
  let timer = null;
  let userPaused = motion.matches;
  let hovered = false;
  let focusPaused = false;
  let inView = true;
  let ready = false;

  function canRotate() {
    return ready && !userPaused && !hovered && !focusPaused && !document.hidden && inView;
  }

  function schedule() {
    window.clearTimeout(timer);
    timer = null;
    carousel.dataset.rotating = String(canRotate());
    toggle.textContent = userPaused ? 'Play' : 'Pause';
    toggle.setAttribute('aria-label', userPaused ? 'Start automatic slide rotation' : 'Pause automatic slide rotation');
    if (canRotate()) timer = window.setTimeout(() => { show(index + 1); }, INTERVAL);
  }

  function show(next, announce = false) {
    index = ((next % slides.length) + slides.length) % slides.length;
    slides.forEach((slide, position) => {
      const active = position === index;
      slide.classList.toggle('is-active', active);
      slide.setAttribute('aria-hidden', String(!active));
      dots[position].setAttribute('aria-pressed', String(active));
    });
    title.textContent = slides[index].dataset.title;
    counter.textContent = (index + 1) + ' / ' + slides.length;
    carousel.dataset.slide = String(index + 1);
    if (announce) status.textContent = 'Slide ' + (index + 1) + ' of ' + slides.length + ': ' + slides[index].dataset.title;
    schedule();
  }

  dots.forEach(dot => dot.addEventListener('click', () => show(Number(dot.dataset.carouselSlide), true)));
  toggle.addEventListener('click', () => {
    userPaused = !userPaused;
    if (!userPaused) focusPaused = false;
    status.textContent = userPaused ? 'Automatic rotation paused.' : 'Automatic rotation enabled. Slides change every five seconds.';
    schedule();
  });
  carousel.addEventListener('pointerenter', event => { if (event.pointerType !== 'touch') { hovered = true; schedule(); } });
  carousel.addEventListener('pointerleave', () => { hovered = false; schedule(); });
  carousel.addEventListener('focusin', () => { focusPaused = true; schedule(); });
  carousel.addEventListener('focusout', () => queueMicrotask(() => {
    if (!carousel.contains(document.activeElement)) { focusPaused = false; schedule(); }
  }));
  carousel.addEventListener('keydown', event => {
    const destinations = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: slides.length - 1 };
    if (Object.prototype.hasOwnProperty.call(destinations, event.key)) {
      event.preventDefault();
      show(destinations[event.key], true);
    }
  });
  document.addEventListener('visibilitychange', schedule);
  motion.addEventListener('change', event => { userPaused = event.matches; schedule(); });
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(entries => {
      inView = entries[0].isIntersecting;
      schedule();
    }, { threshold: 0.1 }).observe(carousel);
  }

  show(0);
  Promise.allSettled(slides.map(slide => slide.querySelector('img').decode())).then(() => {
    ready = true;
    carousel.dataset.ready = 'true';
    schedule();
  });
})();
