/* Search suggestions for the top-bar search box, in both shells.
 *
 * Progressive enhancement over a plain GET form: any form with a
 * data-suggest-url gets a listbox under its input, filled from that URL's JSON
 * ({groups: [{label, items: [{label, hint, url}]}]}, built by app.page_search).
 * Pages come first ("Go to"), then a few records. Without this script the form
 * still submits to the full results page.
 *
 * Keyboard: Down/Up move through the options, Enter opens the highlighted one
 * (or submits the search when none is highlighted), Escape closes. "/" anywhere
 * outside a text field jumps to the search box. The input is an ARIA combobox
 * that points at the highlighted option with aria-activedescendant, so focus
 * never leaves the field.
 */
(function () {
  'use strict';

  var DELAY_MS = 150;

  function setup(form) {
    var input = form.querySelector('input[type="search"]');
    var url = form.getAttribute('data-suggest-url');
    if (!input || !url) return;

    var list = document.createElement('ul');
    list.className = 'search-suggest';
    list.id = (input.id || 'search') + '-suggest';
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Suggestions');
    list.hidden = true;
    form.appendChild(list);

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('autocomplete', 'off');

    var options = [];
    var active = -1;
    var timer = null;
    var latest = 0;

    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function highlight(index) {
      options.forEach(function (option, i) {
        option.el.setAttribute('aria-selected', i === index ? 'true' : 'false');
      });
      active = index;
      if (index >= 0) {
        input.setAttribute('aria-activedescendant', options[index].el.id);
        options[index].el.scrollIntoView({ block: 'nearest' });
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function go(href) {
      window.location.assign(href);
    }

    function render(data) {
      list.textContent = '';
      options = [];
      active = -1;
      (data.groups || []).forEach(function (group) {
        if (!group.items || !group.items.length) return;
        var head = document.createElement('li');
        head.className = 'search-suggest-head';
        head.setAttribute('role', 'presentation');
        head.textContent = group.label;
        list.appendChild(head);
        group.items.forEach(function (item) {
          var li = document.createElement('li');
          li.id = list.id + '-' + options.length;
          li.className = 'search-suggest-item';
          li.setAttribute('role', 'option');
          li.setAttribute('aria-selected', 'false');
          var label = document.createElement('span');
          label.className = 'search-suggest-label';
          label.textContent = item.label;
          li.appendChild(label);
          if (item.hint) {
            var hint = document.createElement('span');
            hint.className = 'search-suggest-hint';
            hint.textContent = item.hint;
            li.appendChild(hint);
          }
          // mousedown, not click: a click lands after the input's blur has
          // already closed the list.
          li.addEventListener('mousedown', function (event) {
            event.preventDefault();
            go(item.url);
          });
          list.appendChild(li);
          options.push({ el: li, url: item.url });
        });
      });
      if (!options.length) {
        close();
        return;
      }
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    function fetchSuggestions() {
      var q = input.value.trim();
      if (!q) {
        close();
        return;
      }
      var mine = ++latest;
      fetch(url + '?q=' + encodeURIComponent(q), {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' }
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          // Answers can arrive out of order; only the newest one is drawn.
          if (data && mine === latest && document.activeElement === input) render(data);
        })
        .catch(function () { /* offline or signed out: the form still works */ });
    }

    input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(fetchSuggestions, DELAY_MS);
    });

    input.addEventListener('keydown', function (event) {
      if (list.hidden) {
        if (event.key === 'ArrowDown' && input.value.trim()) fetchSuggestions();
        return;
      }
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        highlight(Math.min(active + 1, options.length - 1));
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        highlight(Math.max(active - 1, -1));
      } else if (event.key === 'Enter' && active >= 0) {
        event.preventDefault();
        go(options[active].url);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        close();
      }
    });

    input.addEventListener('blur', function () { close(); });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('form[data-suggest-url]'), setup);
  }

  document.addEventListener('keydown', function (event) {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
    var target = event.target;
    if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
    var input = document.querySelector('form[data-suggest-url] input[type="search"]');
    if (input && input.offsetParent !== null) {
      event.preventDefault();
      input.focus();
      input.select();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
