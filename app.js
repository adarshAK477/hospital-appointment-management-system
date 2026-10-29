// Mobile menu
var menuBtn = document.getElementById('menuBtn');
if (menuBtn) {
  menuBtn.addEventListener('click', function () {
    document.getElementById('nav').classList.toggle('open');
  });
}

// Auto-submit the parent form when a [data-autosubmit] control changes
document.querySelectorAll('[data-autosubmit]').forEach(function (el) {
  el.addEventListener('change', function () {
    if (el.form) el.form.submit();
  });
});

// Confirm dialogs for destructive actions
document.querySelectorAll('form[data-confirm]').forEach(function (f) {
  f.addEventListener('submit', function (ev) {
    if (!window.confirm(f.getAttribute('data-confirm'))) ev.preventDefault();
  });
});

// Fade out success messages after a few seconds
document.querySelectorAll('.flash.success').forEach(function (f) {
  setTimeout(function () {
    f.style.transition = 'opacity .5s';
    f.style.opacity = '0';
    setTimeout(function () { f.remove(); }, 600);
  }, 6000);
});
