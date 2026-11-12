(function () {
  var tip = null, owner = null, showTimer = null, warmUntil = 0;
  var GAP = 8, EDGE = 8;

  function el() {
    if (tip) return tip;
    tip = document.createElement('div');
    tip.id = 'fg-tip';
    tip.setAttribute('role', 'tooltip');
    tip.setAttribute('data-no-i18n', '');
    document.body.appendChild(tip);
    return tip;
  }

  function tr(s) { return window.t ? window.t(s) : s; }

  function place(target) {
    var r = target.getBoundingClientRect();
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var vw = document.documentElement.clientWidth;
    var below = r.top - h - GAP < EDGE;
    var side = target.closest('.sidebar') && !target.closest('.ni-children') && vw > 900;
    var x, y;
    if (side) {
      var rtl = document.documentElement.dir === 'rtl';
      x = rtl ? r.left - w - GAP : r.right + GAP;
      y = r.top + r.height / 2 - h / 2;
      tip.setAttribute('data-side', rtl ? 'left' : 'right');
    } else {
      x = r.left + r.width / 2 - w / 2;
      y = below ? r.bottom + GAP : r.top - h - GAP;
      tip.setAttribute('data-side', below ? 'bottom' : 'top');
    }
    x = Math.max(EDGE, Math.min(x, vw - w - EDGE));
    tip.style.transform = 'translate(' + Math.round(x) + 'px,' + Math.round(y) + 'px)';
  }

  function show(target) {
    var text = target.getAttribute('data-tip');
    if (!text) return;
    var t = el();
    var head = '';
    if (target.closest('.sidebar.sb-collapsed')) {
      var lbl = target.querySelector('.ni-label');
      if (lbl) head = lbl.textContent.trim();
    }
    t.textContent = '';
    if (head) {
      var b = document.createElement('b');
      b.textContent = head;
      t.appendChild(b);
    }
    t.appendChild(document.createTextNode(tr(text)));
    owner = target;
    target.setAttribute('aria-describedby', 'fg-tip');
    t.classList.remove('on');
    place(target);
    void t.offsetWidth;
    t.classList.add('on');
  }

  function hide() {
    clearTimeout(showTimer);
    if (owner) {
      owner.removeAttribute('aria-describedby');
      warmUntil = Date.now() + 400;
    }
    owner = null;
    if (tip) tip.classList.remove('on');
  }

  function later(target) {
    clearTimeout(showTimer);
    if (Date.now() < warmUntil) { show(target); return; }
    showTimer = setTimeout(function () { show(target); }, 450);
  }

  document.addEventListener('pointerover', function (e) {
    if (e.pointerType === 'touch') return;
    var t = e.target.closest ? e.target.closest('[data-tip]') : null;
    if (t === owner) return;
    if (!t) { if (owner) hide(); else clearTimeout(showTimer); return; }
    if (owner) hide();
    later(t);
  });

  document.addEventListener('pointerdown', function (e) {
    var t = e.target.closest ? e.target.closest('[data-tip]') : null;
    var tappable = t && (t.classList.contains('tip-i') || !t.closest('button, a, input, select, textarea, label, [onclick]'));
    if (e.pointerType === 'touch' && tappable) {
      if (owner === t) hide(); else { hide(); show(t); }
      return;
    }
    if (owner && t !== owner) hide();
    else if (owner && e.pointerType !== 'touch') hide();
  });

  document.addEventListener('focusin', function (e) {
    var t = e.target.closest ? e.target.closest('[data-tip]') : null;
    if (t && t.matches(':focus-visible')) show(t);
  });
  document.addEventListener('focusout', function () { if (owner) hide(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && owner) hide(); });
  window.addEventListener('scroll', function () { if (owner) hide(); }, true);
  window.addEventListener('resize', function () { if (owner) hide(); });
})();
