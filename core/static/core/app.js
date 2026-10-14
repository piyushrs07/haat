/* Haat - small shared behaviours (kept out of the HTML so pages stay lean and cacheable) */
(function () {
  'use strict';

  // ---- PWA: register the service worker ----
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(function () {});
    });
  }

  // ---- PWA: "Install app" button (Chrome/Edge/Android) + hint for iPhone/iPad Safari ----
  var installBtn = document.getElementById('install-btn');
  var iosHint = document.getElementById('ios-install-hint');
  var deferredPrompt = null;
  var standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    if (installBtn && !standalone) { installBtn.hidden = false; }
  });
  if (installBtn) {
    installBtn.addEventListener('click', function () {
      if (!deferredPrompt) { return; }
      deferredPrompt.prompt();
      deferredPrompt.userChoice.finally(function () { deferredPrompt = null; installBtn.hidden = true; });
    });
  }
  window.addEventListener('appinstalled', function () { if (installBtn) { installBtn.hidden = true; } });
  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
              (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (iosHint && isIOS && !standalone) { iosHint.hidden = false; }

  // ---- Click a small thumbnail to show it as the big product photo ----
  document.addEventListener('click', function (e) {
    var thumb = e.target.closest && e.target.closest('img[data-swap]');
    if (!thumb) { return; }
    var card = thumb.closest('.product-card');
    var main = card && card.querySelector('.product-img img');
    if (main) { main.src = thumb.src; }
  });

  // ---- Live preview of photos chosen in the upload forms ----
  document.querySelectorAll('input[type=file][data-preview]').forEach(function (input) {
    input.addEventListener('change', function () {
      var box = document.getElementById(input.dataset.preview);
      if (!box) { return; }
      if (!input.multiple) { box.innerHTML = ''; }
      Array.prototype.forEach.call(input.files, function (file) {
        var img = document.createElement('img');
        img.src = URL.createObjectURL(file);
        img.onload = function () { URL.revokeObjectURL(img.src); };
        box.appendChild(img);
      });
    });
  });

  // ---- Prevent double-submits (slow mobile networks / Render cold starts make people tap twice) ----
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') { return; }
    if (form.dataset.submitted === '1') { e.preventDefault(); return; }
    form.dataset.submitted = '1';
    form.querySelectorAll('button[type=submit], button:not([type])').forEach(function (b) {
      setTimeout(function () { b.disabled = true; }, 0);   // after the browser has read the form
    });
  });
  // Coming back with the Back button must not leave a form permanently disabled.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) { return; }
    document.querySelectorAll('form[data-submitted]').forEach(function (f) {
      delete f.dataset.submitted;
      f.querySelectorAll('button:disabled').forEach(function (b) { b.disabled = false; });
    });
  });

  // ---- Chat: start scrolled to the newest message ----
  var chat = document.getElementById('chat-box');
  if (chat) { chat.scrollTop = chat.scrollHeight; }
})();
