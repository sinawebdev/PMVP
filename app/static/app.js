/*
 * Payrolla — shared front-end behaviour.
 *
 * One mechanism for user feedback: auto-dismissing toasts. They are raised from
 * three places, all funnelled through payrollaToast():
 *   1. Server-rendered Flask flashes  -> a JSON island (#flash-data) read on load.
 *   2. HTMX partial responses         -> an "HX-Trigger: {showToast: {...}}" header,
 *                                        which htmx re-emits as a `showToast` event.
 *   3. Ad-hoc client-side calls        -> payrollaToast(type, msg) directly.
 *
 * CSRF (Flask-WTF): every mutating request must carry the session token rendered
 * into <meta name="csrf-token">. The block below attaches it to htmx requests and
 * fetch() as an X-CSRFToken header, and injects a hidden csrf_token field into
 * classic form POSTs — so no per-form template edits are needed.
 */
(function () {
  "use strict";

  // --- CSRF -----------------------------------------------------------------
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  // htmx: header on every request it issues.
  document.addEventListener("htmx:configRequest", function (evt) {
    var t = csrfToken();
    if (t && evt.detail && evt.detail.headers) evt.detail.headers["X-CSRFToken"] = t;
  });

  // fetch: attach the header to mutating requests (leave GET/HEAD alone).
  var _origFetch = window.fetch;
  if (typeof _origFetch === "function") {
    window.fetch = function (input, init) {
      init = init || {};
      var method = (init.method
        || (input && typeof input !== "string" && input.method)
        || "GET").toUpperCase();
      var t = csrfToken();
      if (t && method !== "GET" && method !== "HEAD") {
        var headers = new Headers(
          init.headers || (input && typeof input !== "string" && input.headers) || {}
        );
        if (!headers.has("X-CSRFToken")) headers.set("X-CSRFToken", t);
        init.headers = headers;
      }
      return _origFetch.call(this, input, init);
    };
  }

  // Classic (non-htmx) form POSTs: inject a hidden csrf_token so a normal submit
  // carries the token. htmx forms use the header above; having both is harmless.
  function injectCsrf(form) {
    if (!form || form.tagName !== "FORM") return;
    if ((form.getAttribute("method") || "get").toLowerCase() === "get") return;
    if (form.querySelector('input[name="csrf_token"]')) return;
    var t = csrfToken();
    if (!t) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "csrf_token";
    input.value = t;
    form.appendChild(input);
  }
  document.addEventListener("submit", function (e) { injectCsrf(e.target); }, true);
  document.addEventListener("htmx:afterSwap", function () {
    document.querySelectorAll("form").forEach(injectCsrf);
  });
  function injectAllForms() { document.querySelectorAll("form").forEach(injectCsrf); }

  var VALID = { success: 1, danger: 1, info: 1, warning: 1 };
  var ICONS = {
    success: "bi-check-circle-fill",
    danger: "bi-exclamation-octagon-fill",
    info: "bi-info-circle-fill",
    warning: "bi-exclamation-triangle-fill",
  };

  function region() {
    var el = document.getElementById("toast-region");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast-region";
      el.className = "toast-region";
      el.setAttribute("aria-live", "polite");
      el.setAttribute("aria-atomic", "false");
      document.body.appendChild(el);
    }
    return el;
  }

  // Public: raise a toast. `type` is a Flask flash category (success/danger/info/warning);
  // anything unknown falls back to "info". Auto-dismisses; hovering pauses the timer.
  function payrollaToast(type, message, timeoutMs) {
    if (!message) return;
    type = VALID[type] ? type : "info";
    var life = typeof timeoutMs === "number" ? timeoutMs : 5000;

    var toast = document.createElement("div");
    toast.className = "toast-item toast-" + type;
    toast.setAttribute("role", type === "danger" ? "alert" : "status");

    var icon = document.createElement("i");
    icon.className = "bi " + (ICONS[type] || ICONS.info) + " toast-icon";
    icon.setAttribute("aria-hidden", "true");

    var body = document.createElement("div");
    body.className = "toast-body";
    body.textContent = message; // textContent — never inject message as HTML

    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.innerHTML = "&times;";

    toast.appendChild(icon);
    toast.appendChild(body);
    toast.appendChild(close);
    region().appendChild(toast);

    // Enter on next frame so the CSS transition runs.
    requestAnimationFrame(function () { toast.classList.add("toast-show"); });

    var timer = null;
    function dismiss() {
      if (!toast.parentNode) return;
      toast.classList.remove("toast-show");
      toast.classList.add("toast-hide");
      window.setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 260);
    }
    function arm() { timer = window.setTimeout(dismiss, life); }
    function disarm() { if (timer) window.clearTimeout(timer); }

    close.addEventListener("click", dismiss);
    toast.addEventListener("mouseenter", disarm);
    toast.addEventListener("mouseleave", arm);
    if (life > 0) arm();
    return toast;
  }
  window.payrollaToast = payrollaToast;

  // 1. Drain server-rendered flashes on first paint.
  function drainFlashes() {
    var island = document.getElementById("flash-data");
    if (!island) return;
    var pairs;
    try { pairs = JSON.parse(island.textContent || "[]"); } catch (e) { return; }
    (pairs || []).forEach(function (pair) {
      // Flask hands back [category, message]; bootstrap alert categories map 1:1.
      payrollaToast(pair[0], pair[1]);
    });
    island.parentNode && island.parentNode.removeChild(island);
  }

  // 2. HTMX responses that carry an HX-Trigger toast payload.
  //    Header form: {"showToast": {"type": "success", "msg": "..."}}
  document.body && document.body.addEventListener("showToast", handleTriggeredToast);
  function handleTriggeredToast(evt) {
    var d = evt.detail || {};
    // htmx wraps a single-value trigger as {value: ...} on older configs; accept both.
    var payload = d.msg || d.type ? d : (d.value || {});
    payrollaToast(payload.type, payload.msg);
  }

  // ---------------------------------------------------------------------------
  // Styled confirm modal — replaces native confirm() (which froze the renderer).
  // Returns a Promise<boolean>. Escape / backdrop / Cancel resolve false.
  // ---------------------------------------------------------------------------
  function payrollaConfirm(question, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var lastFocus = document.activeElement;
      var backdrop = document.createElement("div");
      backdrop.className = "cn-modal-backdrop";

      var modal = document.createElement("div");
      modal.className = "cn-modal";
      modal.setAttribute("role", "alertdialog");
      modal.setAttribute("aria-modal", "true");

      var title = document.createElement("div");
      title.className = "cn-modal-title";
      var icon = document.createElement("i");
      icon.className = "bi " + (opts.danger ? "bi-exclamation-octagon-fill" : "bi-question-circle-fill");
      var titleText = document.createElement("span");
      titleText.textContent = opts.title || (opts.danger ? "Please confirm" : "Are you sure?");
      title.appendChild(icon);
      title.appendChild(titleText);

      var text = document.createElement("p");
      text.className = "cn-modal-text";
      text.textContent = question || "Are you sure?";

      // Typed confirmation — the top tier of the consequence scale, for actions
      // with no undo. Making the user reproduce the subject's name turns a reflex
      // click into a deliberate one, and it cannot be muscle-memoried away.
      var typed = null;
      if (opts.requireTyped) {
        typed = document.createElement("input");
        typed.type = "text";
        typed.className = "form-control form-control-sm cn-modal-input";
        typed.setAttribute("aria-label", "Type " + opts.requireTyped + " to confirm");
        typed.placeholder = opts.requireTyped;
        typed.autocomplete = "off";
      }

      var actions = document.createElement("div");
      actions.className = "cn-modal-actions";
      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn btn-sm btn-outline-secondary";
      cancel.textContent = opts.cancelText || "Cancel";
      var ok = document.createElement("button");
      ok.type = "button";
      ok.className = "btn btn-sm " + (opts.danger ? "btn-danger" : "btn-primary");
      ok.textContent = opts.confirmText || (opts.danger ? "Delete" : "Confirm");
      actions.appendChild(cancel);
      actions.appendChild(ok);

      modal.appendChild(title);
      modal.appendChild(text);
      if (typed) modal.appendChild(typed);
      modal.appendChild(actions);
      backdrop.appendChild(modal);
      document.body.appendChild(backdrop);
      requestAnimationFrame(function () { backdrop.classList.add("cn-open"); });

      if (typed) {
        ok.disabled = true;
        typed.addEventListener("input", function () {
          ok.disabled = typed.value.trim() !== opts.requireTyped;
        });
        typed.focus();
      } else {
        ok.focus();
      }

      var done = false;
      function close(result) {
        if (done) return;
        done = true;
        backdrop.classList.remove("cn-open");
        window.setTimeout(function () {
          if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
          if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
        }, 180);
        document.removeEventListener("keydown", onKey, true);
        resolve(result);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); close(false); }
        else if (e.key === "Tab") {
          // Trap focus between the two buttons.
          e.preventDefault();
          (document.activeElement === ok ? cancel : ok).focus();
        }
      }
      cancel.addEventListener("click", function () { close(false); });
      ok.addEventListener("click", function () { close(true); });
      backdrop.addEventListener("click", function (e) { if (e.target === backdrop) close(false); });
      document.addEventListener("keydown", onKey, true);
    });
  }
  window.payrollaConfirm = payrollaConfirm;

  // Route htmx's hx-confirm through the styled modal instead of window.confirm.
  document.body && document.body.addEventListener("htmx:confirm", function (evt) {
    if (!evt.detail || !evt.detail.question) return; // no hx-confirm on this element
    evt.preventDefault();
    var danger = evt.detail.elt && evt.detail.elt.getAttribute("data-confirm-danger") === "1";
    payrollaConfirm(evt.detail.question, { danger: danger }).then(function (ok) {
      if (ok) evt.detail.issueRequest(true);
    });
  });

  // Plain (non-htmx) forms opt into the same styled modal with data-confirm.
  // htmx elements already route through hx-confirm above; this is the other half,
  // so no template ever needs window.confirm() — which blocks the renderer, can't
  // be styled, and offers no consequence scaling.
  //
  //   data-confirm="Question?"        the prompt (required)
  //   data-confirm-danger="1"         destructive styling + "Delete" button
  //   data-confirm-title="…"          heading override
  //   data-confirm-ok="…"             confirm-button label
  //   data-confirm-typed="ACME"       require the user to type this first
  //
  // Capture phase, so it runs before the submit-feedback handler in base.html and
  // a cancelled confirm never leaves a button stuck on "Working…".
  // The attributes may sit on the form OR on the button that submitted it — a
  // form with several submitters (bulk approve / reject / distribute, each with
  // its own formaction) needs a different question per button.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.matches) return;
    var submitter = e.submitter && e.submitter.getAttribute("data-confirm")
      ? e.submitter
      : null;
    var source = submitter || (form.matches("[data-confirm]") ? form : null);
    if (!source) return;
    if (form.getAttribute("data-confirmed") === "1") {
      form.removeAttribute("data-confirmed");
      return; // second pass: the user already said yes
    }
    e.preventDefault();
    e.stopPropagation();
    payrollaConfirm(source.getAttribute("data-confirm"), {
      danger: source.getAttribute("data-confirm-danger") === "1",
      title: source.getAttribute("data-confirm-title") || undefined,
      confirmText: source.getAttribute("data-confirm-ok") || undefined,
      requireTyped: source.getAttribute("data-confirm-typed") || undefined
    }).then(function (confirmed) {
      if (!confirmed) return;
      form.setAttribute("data-confirmed", "1");
      // Re-submit through the same button so its formaction still applies.
      if (form.requestSubmit) form.requestSubmit(submitter || undefined);
      else form.submit();
    });
  }, true);

  // Delegated table filter (input[data-table-filter] -> #tableId). Delegation keeps
  // it working after htmx swaps replace the input and rows.
  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!input || !input.matches || !input.matches("[data-table-filter]")) return;
    var table = document.getElementById(input.getAttribute("data-table-filter"));
    if (!table) return;
    var term = input.value.trim().toLowerCase();
    table.querySelectorAll("tbody tr").forEach(function (row) {
      row.style.display = row.textContent.toLowerCase().indexOf(term) === -1 ? "none" : "";
    });
  });

  // ---------------------------------------------------------------------------
  // Staged upload loader (C1) — perceived progress only. Shows an indeterminate
  // bar and cycles reassuring copy while a workbook is parsed. Not tied to real
  // parse state (no background worker on the free tier). Returns {stop()}.
  // ---------------------------------------------------------------------------
  var UPLOAD_STAGES = [
    "Reading workbook…",
    "Validating rows…",
    "Computing PAYE & SSNIT…",
    "Almost done…",
  ];
  function payrollaStagedLoader(container, opts) {
    if (!container) return { stop: function () {} };
    opts = opts || {};
    var messages = opts.messages || UPLOAD_STAGES;
    var msgEl = container.querySelector(".upload-stage-msg");
    var i = 0;
    if (msgEl) msgEl.textContent = messages[0];
    container.classList.add("is-loading");
    var interval = window.setInterval(function () {
      i = Math.min(i + 1, messages.length - 1);
      if (msgEl) msgEl.textContent = messages[i];
      if (i >= messages.length - 1) window.clearInterval(interval);
    }, opts.intervalMs || 4500);
    return {
      stop: function () {
        window.clearInterval(interval);
        container.classList.remove("is-loading");
      },
    };
  }
  window.payrollaStagedLoader = payrollaStagedLoader;

  // Full-page forms opt in with data-staged-loader="<overlay id>". On submit the
  // overlay is shown and stays up until the page navigates — motion + copy the
  // whole time, and the overlay blocks re-clicks (double-submit impossible).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.matches || !form.matches("[data-staged-loader]")) return;
    if (form.matches("[hx-post],[hx-get],[hx-put],[hx-delete],[hx-patch]")) return;
    payrollaStagedLoader(document.getElementById(form.getAttribute("data-staged-loader")));
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      drainFlashes();
      injectAllForms();
    });
  } else {
    drainFlashes();
    injectAllForms();
  }
})();

// --- Raw-hours import preview -----------------------------------------------
// Blockers first, then counts, then a bounded warning summary. Shared by the
// Payroll Runs upload panel and the standalone upload screen so a preview never
// reads one way on one page and another way on the other — and so neither can
// regress to dumping JSON.stringify(preview) at the user, which is what the
// standalone screen used to do.
window.payrollaRawPreview = (function () {
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function render(d) {
    var p = d.preview || {};
    var parts = [];
    var blocked = p.blocked || [];
    var warnings = p.warnings || [];

    // What would stop this import, stated before anything else.
    if (blocked.length) {
      parts.push(
        '<p class="mb-1 text-danger"><strong>' + blocked.length +
        " worker" + (blocked.length === 1 ? "" : "s") +
        " not on the roster</strong> — add them first or they will be skipped: " +
        esc(blocked.slice(0, 10).join(", ")) +
        (blocked.length > 10 ? " and " + (blocked.length - 10) + " more" : "") +
        "</p>"
      );
    }

    if (d.mode === "seed") {
      parts.push(
        "<p class=\"mb-1\">" + p.employees + " employees found — " + p.hourly +
        " hourly, " + p.salaried + " salaried — " + p.icu_members +
        " ICU members.</p>"
      );
    } else {
      parts.push(
        "<p class=\"mb-1\">" + p.employees + " rows in the file — " + p.matched +
        " matched to the roster.</p>"
      );
    }

    parts.push(
      warnings.length
        ? '<p class="small text-muted mb-0">' + warnings.length + " warning" +
          (warnings.length === 1 ? "" : "s") + ": " +
          esc(warnings.slice(0, 5).join("; ")) +
          (warnings.length > 5 ? " …" : "") + "</p>"
        : '<p class="small text-success mb-0">No warnings.</p>'
    );

    return (
      '<div class="alert ' + (blocked.length ? "alert-warning" : "alert-info") + '">' +
      "<div class=\"mb-2\"><strong>" +
      (d.mode === "seed" ? "Seed preview" : "Monthly preview") + "</strong> — " +
      esc(d.client_name) + " · " + esc(d.period) + "</div>" +
      parts.join("") +
      '<button class="btn btn-success mt-2" id="raw_confirm_btn" type="button" ' +
      "onclick=\"rawConfirm('" + d.token + "')\">Confirm " +
      (d.mode === "seed" ? "seed" : "monthly run") + "</button></div>"
    );
  }

  return { render: render, esc: esc };
})();


/* --- Tabs -------------------------------------------------------------------
   Claims any [data-tabs] tablist rendered by macros/ui.html::tabs.

   The panels arrive VISIBLE — this script is what hides them, so a browser that
   never runs it shows the whole page in document order rather than one panel
   and three dead buttons. The deep link (#tab-rows-panel) and the browser's own
   in-page search both still work because the target panel is opened on load and
   whenever the hash changes. */
(function () {
  var lists = document.querySelectorAll("[data-tabs]");
  if (!lists.length) return;

  Array.prototype.forEach.call(lists, function (list) {
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    var panels = tabs.map(function (tab) {
      return document.getElementById(tab.getAttribute("aria-controls"));
    });
    if (panels.some(function (p) { return !p; })) return; // markup mismatch: leave it alone

    function select(index, focus) {
      tabs.forEach(function (tab, i) {
        var on = i === index;
        tab.setAttribute("aria-selected", on ? "true" : "false");
        // Roving tabindex: the tablist is ONE tab stop, arrows move within it.
        tab.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
      });
      if (focus) tabs[index].focus();
    }

    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { select(i); });
      tab.addEventListener("keydown", function (e) {
        var next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1
                 : e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : null;
        if (next === null) return;
        e.preventDefault();
        select((next + tabs.length) % tabs.length, true);
      });
    });

    function openFromHash() {
      if (!window.location.hash) return false;
      var target = window.location.hash.slice(1);
      for (var i = 0; i < tabs.length; i++) {
        // Match the panel itself, or anything inside it — an in-page link to a
        // row deep in a hidden panel must open the panel that holds it.
        if (panels[i].id === target || panels[i].querySelector("#" + CSS.escape(target))) {
          select(i);
          return true;
        }
      }
      return false;
    }

    if (!openFromHash()) select(0);
    window.addEventListener("hashchange", openFromHash);
  });
})();
