(function () {
  'use strict';

  var LANGS = [
    { code: 'en', name: 'English', native: 'English',  flag: '🇬🇧', dir: 'ltr' },
    { code: 'ar', name: 'Arabic',  native: 'العربية',   flag: '🇸🇦', dir: 'rtl' },
    { code: 'ru', name: 'Russian', native: 'Русский',   flag: '🇷🇺', dir: 'ltr' },
    { code: 'de', name: 'German',  native: 'Deutsch',   flag: '🇩🇪', dir: 'ltr' },
    { code: 'zh', name: 'Chinese', native: '简体中文',   flag: '🇨🇳', dir: 'ltr' }
  ];
  var DEFAULT_LANG = 'en';
  var RTL = { ar: 1 };
  var LS_KEY = 'fg_lang';
  var COOKIE = 'fg_lang';
  var BUNDLE_URL = function (l) { return '/static/i18n/' + l + '.json'; };

  var state = {
    lang: DEFAULT_LANG,
    bundle: {},
    invertIndex: {},
    loaded: { en: true },
    observerArmed: false,
    walking: false
  };

  function cookieGet(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' +
      name.replace(/([.$?*|{}()\[\]\\\/\+^])/g, '\\$1') + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function cookieSet(name, value, days) {
    var host = location.hostname;
    var parts = host.split('.');
    var dom = '';
    if (parts.length >= 2 && host !== 'localhost' && !/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      dom = '; domain=.' + parts.slice(-2).join('.');
    }
    var sec = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = name + '=' + encodeURIComponent(value) +
      '; Path=/; Max-Age=' + (days * 86400) + '; SameSite=Lax' + sec + dom;
  }

  function detectInitialLang() {
    try {
      var qs = new URLSearchParams(location.search).get('lang');
      if (qs && supported(qs)) return qs;
    } catch (_) {}
    var ls = '';
    try { ls = localStorage.getItem(LS_KEY) || ''; } catch (_) {}
    if (ls && supported(ls)) return ls;
    var ck = cookieGet(COOKIE);
    if (ck && supported(ck)) return ck;
    var nav = (navigator.language || navigator.userLanguage || '').toLowerCase().split('-')[0];
    if (supported(nav)) return nav;
    return DEFAULT_LANG;
  }
  function supported(c) {
    c = (c || '').toLowerCase();
    for (var i = 0; i < LANGS.length; i++) if (LANGS[i].code === c) return true;
    return false;
  }

  function loadBundle(lang) {
    if (lang === 'en') return Promise.resolve({});
    if (state.loaded[lang] && state.bundle && lang === state.lang) {
      return Promise.resolve(state.bundle);
    }
    return fetch(BUNDLE_URL(lang), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });
  }

  function t(key) {
    if (!key && key !== '') return key;
    if (state.lang === 'en') return key;
    var b = state.bundle || {};
    var direct = b[key];
    if (typeof direct === 'string' && direct.length) return direct;
    var trimmed = String(key).trim();
    if (trimmed !== key) {
      var t2 = b[trimmed];
      if (typeof t2 === 'string' && t2.length) {
        var lead = key.match(/^\s*/)[0];
        var trail = key.match(/\s*$/)[0];
        return lead + t2 + trail;
      }
    }
    return key;
  }

  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, CODE: 1, PRE: 1, KBD: 1, SAMP: 1, VAR: 1 };
  function shouldSkip(el) {
    if (!el || el.nodeType !== 1) return false;
    if (SKIP_TAGS[el.tagName]) return true;
    if (el.hasAttribute('data-no-i18n')) return true;
    if (el.classList && (el.classList.contains('no-i18n') ||
                          el.classList.contains('mono') ||
                          el.classList.contains('addr') ||
                          el.classList.contains('hash'))) return true;
    return false;
  }

  function translateAttribute(el, attr, key) {
    if (!el || !key) return;
    var origAttr = '__fgOrig_' + attr;
    if (el[origAttr] === undefined) {
      el[origAttr] = el.getAttribute(attr) || '';
    }
    var src = el[origAttr] || key;
    var v;
    if (state.lang === 'en') {
      v = src;
    } else {
      var lookup = (state.bundle || {})[key] || (state.bundle || {})[src];
      v = (typeof lookup === 'string' && lookup.length) ? lookup : src;
    }
    if (el.getAttribute(attr) !== v) el.setAttribute(attr, v);
  }

  function translateElementAttrs(el) {
    var k;
    k = el.getAttribute('data-i18n');
    if (k) {
      if (el.__fgOrig_text === undefined) el.__fgOrig_text = el.textContent;
      var src = el.__fgOrig_text || k;
      var newText;
      if (state.lang === 'en') {
        newText = src;
      } else {
        var look = (state.bundle || {})[k] || (state.bundle || {})[src];
        newText = (typeof look === 'string' && look.length) ? look : src;
      }
      if (el.textContent !== newText) el.textContent = newText;
    }
    k = el.getAttribute('data-i18n-html');
    if (k) {
      if (el.__fgOrig_html === undefined) el.__fgOrig_html = el.innerHTML;
      var srcH = el.__fgOrig_html || k;
      if (state.lang === 'en') {
        el.innerHTML = srcH;
      } else {
        var lookH = (state.bundle || {})[k];
        el.innerHTML = (typeof lookH === 'string' && lookH.length) ? lookH : srcH;
      }
    }
    k = el.getAttribute('data-i18n-placeholder');
    if (k) translateAttribute(el, 'placeholder', k);
    k = el.getAttribute('data-i18n-title');
    if (k) translateAttribute(el, 'title', k);
    k = el.getAttribute('data-i18n-aria-label');
    if (k) translateAttribute(el, 'aria-label', k);
    k = el.getAttribute('data-i18n-value');
    if (k) translateAttribute(el, 'value', k);
    k = el.getAttribute('data-i18n-alt');
    if (k) translateAttribute(el, 'alt', k);
  }

  function walkAndTranslate(root) {
    if (!root) root = document.body || document.documentElement;
    if (!root) return;
    if (state.walking) return;
    state.walking = true;
    try {
      var bundle = state.bundle || {};
      var isEnglish = state.lang === 'en';

      var withAttrs;
      try {
        withAttrs = root.querySelectorAll(
          '[data-i18n], [data-i18n-html], [data-i18n-placeholder],' +
          ' [data-i18n-title], [data-i18n-aria-label], [data-i18n-value], [data-i18n-alt]'
        );
      } catch (_) { withAttrs = []; }
      for (var i = 0; i < withAttrs.length; i++) translateElementAttrs(withAttrs[i]);
      if (root.nodeType === 1 && root.hasAttribute && (
            root.hasAttribute('data-i18n') ||
            root.hasAttribute('data-i18n-placeholder') ||
            root.hasAttribute('data-i18n-title'))) {
        translateElementAttrs(root);
      }

      var walker;
      try {
        walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
          acceptNode: function (node) {
            var p = node.parentNode;
            if (!p) return NodeFilter.FILTER_REJECT;
            var el = p;
            while (el && el.nodeType !== 1) el = el.parentNode;
            if (!el) return NodeFilter.FILTER_REJECT;
            var cur = el;
            while (cur && cur !== root) {
              if (shouldSkip(cur)) return NodeFilter.FILTER_REJECT;
              cur = cur.parentNode;
            }
            if (root && shouldSkip(root)) return NodeFilter.FILTER_REJECT;
            if (node.__fgOrig !== undefined) return NodeFilter.FILTER_ACCEPT;
            var v = node.nodeValue;
            if (!v || !v.trim()) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
          }
        });
      } catch (_) { walker = null; }

      if (walker) {
        var node;
        var updates = [];
        while ((node = walker.nextNode())) {
          if (node.__fgOrig === undefined) {
            node.__fgOrig = node.nodeValue;
          }
          var orig = node.__fgOrig;
          if (!orig || !orig.trim()) continue;
          var trimmed = orig.trim();
          var lead = orig.match(/^\s*/)[0];
          var trail = orig.match(/\s*$/)[0];

          var nextValue;
          if (isEnglish) {
            nextValue = orig;
          } else {
            var translated = bundle[trimmed];
            if (typeof translated === 'string' && translated.length) {
              nextValue = lead + translated + trail;
            } else {
              nextValue = orig;
            }
          }
          if (node.nodeValue !== nextValue) updates.push([node, nextValue]);
        }
        for (var j = 0; j < updates.length; j++) updates[j][0].nodeValue = updates[j][1];
      }
    } finally {
      state.walking = false;
    }
  }

  var pendingRoots = null;
  var pendingScheduled = false;
  function scheduleWalk(root) {
    if (!pendingRoots) pendingRoots = [];
    for (var i = 0; i < pendingRoots.length; i++) {
      if (pendingRoots[i].contains && pendingRoots[i].contains(root)) return;
    }
    pendingRoots.push(root);
    if (pendingScheduled) return;
    pendingScheduled = true;
    var run = function () {
      pendingScheduled = false;
      var roots = pendingRoots || [];
      pendingRoots = null;
      if (roots.length > 12) {
        walkAndTranslate(document.body);
      } else {
        for (var i = 0; i < roots.length; i++) walkAndTranslate(roots[i]);
      }
    };
    if (typeof queueMicrotask === 'function') queueMicrotask(run);
    else Promise.resolve().then(run);
  }

  function armObserver() {
    if (state.observerArmed) return;
    if (!('MutationObserver' in window)) return;
    var obs = new MutationObserver(function (muts) {
      if (state.walking) return;
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'childList' && m.addedNodes && m.addedNodes.length) {
          for (var j = 0; j < m.addedNodes.length; j++) {
            var nd = m.addedNodes[j];
            if (nd.nodeType === 1) {
              scheduleWalk(nd);
            } else if (nd.nodeType === 3 && nd.parentNode) {
              scheduleWalk(nd.parentNode);
            }
          }
        }
      }
    });
    obs.observe(document.documentElement || document.body, {
      childList: true, subtree: true, characterData: false
    });
    state.observerArmed = true;
  }

  function _cssStr(s) {
    return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }
  function applyCssContentVars() {
    var root = document.documentElement;
    root.style.setProperty('--fg-i18n-lang-label', _cssStr(t('Language')));
    root.style.setProperty('--fg-i18n-curr-label', _cssStr(t('Currency')));
    root.style.setProperty('--fg-ds-type-to-search', _cssStr(t('Type to search payments…')));
  }

  function applyLangAttrs(lang) {
    var html = document.documentElement;
    html.setAttribute('lang', lang);
    html.setAttribute('dir', RTL[lang] ? 'rtl' : 'ltr');
    var classes = (html.className || '').split(/\s+/).filter(function (c) {
      return c && c.indexOf('lang-') !== 0;
    });
    classes.push('lang-' + lang);
    if (RTL[lang]) classes.push('is-rtl');
    else classes = classes.filter(function (c) { return c !== 'is-rtl'; });
    html.className = classes.join(' ').trim();
  }

  function langItem(l, current) {
    return '<button type="button" class="fg-i18n-item' + (l.code === current ? ' is-active' : '') +
      '" data-lang="' + l.code + '" data-testid="i18n-lang-' + l.code + '">' +
      '<span class="fg-i18n-flag" aria-hidden="true">' + l.flag + '</span>' +
      '<span class="fg-i18n-native">' + l.native + '</span>' +
      (l.code === current
        ? '<svg class="fg-i18n-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>'
        : '') +
      '</button>';
  }

  function renderSwitcher(el) {
    if (!el || el.__fgI18nRendered) return;
    var variant = el.getAttribute('data-i18n-switcher') || 'compact';
    var current = state.lang;
    var meta = LANGS.filter(function (x) { return x.code === current; })[0] || LANGS[0];

    el.classList.add('fg-i18n-switcher');
    el.classList.add('fg-i18n-' + variant);

    el.innerHTML =
      '<button type="button" class="fg-i18n-toggle" aria-haspopup="listbox"' +
      ' aria-expanded="false" data-testid="i18n-switcher-toggle"' +
      ' aria-label="' + meta.native + '">' +
        '<span class="fg-i18n-cur-flag" aria-hidden="true">' + meta.flag + '</span>' +
        '<span class="fg-i18n-cur-label">' + meta.native + '</span>' +
        '<svg class="fg-i18n-chev" width="11" height="11" viewBox="0 0 24 24"' +
        ' fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">' +
          '<polyline points="6 9 12 15 18 9"/>' +
        '</svg>' +
      '</button>' +
      '<div class="fg-i18n-menu" role="listbox">' +
        LANGS.map(function (l) { return langItem(l, current); }).join('') +
      '</div>';

    el.__fgI18nRendered = true;

    var toggle = el.querySelector('.fg-i18n-toggle');
    var menu   = el.querySelector('.fg-i18n-menu');
    if (toggle && menu) {
      toggle.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = el.classList.toggle('is-open');
        toggle.setAttribute('aria-expanded', String(open));
      });
      menu.addEventListener('click', function (e) {
        var btn = e.target.closest('.fg-i18n-item');
        if (!btn) return;
        var code = btn.getAttribute('data-lang');
        if (code) {
          var profilePopup = el.closest('#sb-profile-popup');
          changeLanguage(code);
          el.classList.remove('is-open');
          toggle.setAttribute('aria-expanded', 'false');
          if (profilePopup) profilePopup.classList.remove('open');
        }
      });
      document.addEventListener('click', function (e) {
        if (!el.contains(e.target)) {
          el.classList.remove('is-open');
          toggle.setAttribute('aria-expanded', 'false');
        }
      });
    }
  }

  function refreshAllSwitchers() {
    var nodes;
    try { nodes = document.querySelectorAll('[data-i18n-switcher]'); }
    catch (_) { return; }
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      el.__fgI18nRendered = false;
      el.innerHTML = '';
      renderSwitcher(el);
    }
  }

  function mountSwitchers() {
    var nodes;
    try { nodes = document.querySelectorAll('[data-i18n-switcher]'); }
    catch (_) { return; }
    for (var i = 0; i < nodes.length; i++) renderSwitcher(nodes[i]);
  }

  function persistLang(lang) {
    try { localStorage.setItem(LS_KEY, lang); } catch (_) {}
    cookieSet(COOKIE, lang, 365);
    try {
      fetch('/api/i18n/set?lang=' + encodeURIComponent(lang), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang: lang })
      }).catch(function () {});
    } catch (_) {}
  }

  function changeLanguage(lang) {
    if (!supported(lang)) return Promise.resolve(false);
    return loadBundle(lang).then(function (bundle) {
      state.lang = lang;
      state.bundle = bundle || {};
      state.loaded[lang] = true;
      applyLangAttrs(lang);
      persistLang(lang);
      walkAndTranslate(document.body);
      applyCssContentVars();
      refreshAllSwitchers();
      try {
        document.dispatchEvent(new CustomEvent('i18n:changed', {
          detail: { lang: lang, dir: RTL[lang] ? 'rtl' : 'ltr' }
        }));
      } catch (_) {}
      return true;
    });
  }

  function init() {
    var lang = detectInitialLang();
    applyLangAttrs(lang);
    state.lang = lang;
    mountSwitchers();
    armObserver();

    if (lang === 'en') {
      walkAndTranslate(document.body || document.documentElement);
      applyCssContentVars();
      return;
    }
    loadBundle(lang).then(function (b) {
      state.bundle = b || {};
      state.loaded[lang] = true;
      walkAndTranslate(document.body || document.documentElement);
      applyCssContentVars();
      refreshAllSwitchers();
      try {
        document.dispatchEvent(new CustomEvent('i18n:ready', { detail: { lang: lang } }));
      } catch (_) {}
    });
  }

  applyLangAttrs(detectInitialLang());

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.i18n = {
    get lang() { return state.lang; },
    get dir()  { return RTL[state.lang] ? 'rtl' : 'ltr'; },
    isRTL: function () { return !!RTL[state.lang]; },
    supported: LANGS.map(function (l) { return l.code; }),
    languages: LANGS,
    t: t,
    changeLanguage: changeLanguage,
    translate: walkAndTranslate,
    mountSwitchers: mountSwitchers,
    refreshSwitchers: refreshAllSwitchers
  };
  window.t = t;
})();
