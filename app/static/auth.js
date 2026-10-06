(function () {
  'use strict';
  var toggle = document.querySelector('.password-toggle');
  var password = document.getElementById('password');
  if (toggle && password) toggle.addEventListener('click', function () {
    var show = password.type === 'password';
    password.type = show ? 'text' : 'password';
    toggle.textContent = show ? 'Hide' : 'Show';
    toggle.setAttribute('aria-pressed', show ? 'true' : 'false');
  });
  document.addEventListener('click', function (event) {
    var hint = event.target.closest('[data-demo-email]');
    var email = document.getElementById('email');
    if (hint && email) {
      email.value = hint.getAttribute('data-demo-email');
      if (password) password.focus();
    }
  });
})();
