/**
 * FiroGate Currency Switcher v2
 * Button: "🌐 Change Currency ▾"
 * After selection: "🇺🇸 USD ▾" (flag + code only)
 * Features: search, live FIRO price, keyboard nav
 */
(function(global) {
  'use strict';

  var CURRENCIES = [
    // ─ Popular ─
    { code: 'USD', symbol: '$',  flag: '🇺🇸', name: 'US Dollar',          short: 'US Dollar',    rate: 1       },
    { code: 'EUR', symbol: '€',  flag: '🇪🇺', name: 'Euro',               short: 'Euro',         rate: 0.92    },
    { code: 'GBP', symbol: '£',  flag: '🇬🇧', name: 'British Pound',      short: 'British',      rate: 0.79    },
    // ─ Middle East ─
    { code: 'SAR', symbol: 'SAR',flag: '🇸🇦', name: 'Saudi Riyal',        short: 'Saudi',        rate: 3.75    },
    { code: 'AED', symbol: 'AED',flag: '🇦🇪', name: 'UAE Dirham',         short: 'UAE',          rate: 3.67    },
    { code: 'KWD', symbol: 'KWD',flag: '🇰🇼', name: 'Kuwaiti Dinar',      short: 'Kuwaiti',      rate: 0.307   },
    { code: 'QAR', symbol: 'QAR',flag: '🇶🇦', name: 'Qatari Riyal',       short: 'Qatari',       rate: 3.64    },
    { code: 'JOD', symbol: 'JOD',flag: '🇯🇴', name: 'Jordanian Dinar',    short: 'Jordanian',    rate: 0.709   },
    { code: 'EGP', symbol: 'EGP',flag: '🇪🇬', name: 'Egyptian Pound',     short: 'Egyptian',     rate: 50.9    },
    { code: 'BHD', symbol: 'BHD',flag: '🇧🇭', name: 'Bahraini Dinar',     short: 'Bahraini',     rate: 0.376   },
    { code: 'OMR', symbol: 'OMR',flag: '🇴🇲', name: 'Omani Rial',         short: 'Omani',        rate: 0.385   },
    { code: 'IQD', symbol: 'IQD',flag: '🇮🇶', name: 'Iraqi Dinar',        short: 'Iraqi',        rate: 1310    },
    // ─ Asia ─
    { code: 'JPY', symbol: '¥',  flag: '🇯🇵', name: 'Japanese Yen',       short: 'Japanese',     rate: 154.0   },
    { code: 'CNY', symbol: '¥',  flag: '🇨🇳', name: 'Chinese Yuan',       short: 'Chinese',      rate: 7.24    },
    { code: 'INR', symbol: '₹',  flag: '🇮🇳', name: 'Indian Rupee',       short: 'Indian',       rate: 83.5    },
    { code: 'KRW', symbol: '₩',  flag: '🇰🇷', name: 'South Korean Won',   short: 'Korean',       rate: 1340    },
    { code: 'SGD', symbol: 'SGD',flag: '🇸🇬', name: 'Singapore Dollar',   short: 'Singapore',    rate: 1.34    },
    { code: 'HKD', symbol: 'HKD',flag: '🇭🇰', name: 'Hong Kong Dollar',   short: 'Hong Kong',    rate: 7.82    },
    { code: 'MYR', symbol: 'MYR',flag: '🇲🇾', name: 'Malaysian Ringgit',  short: 'Malaysian',    rate: 4.72    },
    { code: 'THB', symbol: '฿',  flag: '🇹🇭', name: 'Thai Baht',          short: 'Thai',         rate: 35.1    },
    { code: 'IDR', symbol: 'IDR',flag: '🇮🇩', name: 'Indonesian Rupiah',  short: 'Indonesian',   rate: 15800   },
    { code: 'PKR', symbol: 'PKR',flag: '🇵🇰', name: 'Pakistani Rupee',    short: 'Pakistani',    rate: 278     },
    { code: 'BDT', symbol: 'BDT',flag: '🇧🇩', name: 'Bangladeshi Taka',   short: 'Bangladeshi',  rate: 110     },
    { code: 'PHP', symbol: '₱',  flag: '🇵🇭', name: 'Philippine Peso',    short: 'Philippine',   rate: 56.5    },
    { code: 'VND', symbol: '₫',  flag: '🇻🇳', name: 'Vietnamese Dong',    short: 'Vietnamese',   rate: 25400   },
    // ─ Europe ──
    { code: 'CHF', symbol: 'CHF',flag: '🇨🇭', name: 'Swiss Franc',        short: 'Swiss',        rate: 0.90    },
    { code: 'SEK', symbol: 'SEK',flag: '🇸🇪', name: 'Swedish Krona',      short: 'Swedish',      rate: 10.4    },
    { code: 'NOK', symbol: 'NOK',flag: '🇳🇴', name: 'Norwegian Krone',    short: 'Norwegian',    rate: 10.6    },
    { code: 'DKK', symbol: 'DKK',flag: '🇩🇰', name: 'Danish Krone',       short: 'Danish',       rate: 6.87    },
    { code: 'PLN', symbol: 'PLN',flag: '🇵🇱', name: 'Polish Zloty',       short: 'Polish',       rate: 3.98    },
    { code: 'CZK', symbol: 'CZK',flag: '🇨🇿', name: 'Czech Koruna',       short: 'Czech',        rate: 23.1    },
    { code: 'HUF', symbol: 'HUF',flag: '🇭🇺', name: 'Hungarian Forint',   short: 'Hungarian',    rate: 360     },
    { code: 'RUB', symbol: '₽',  flag: '🇷🇺', name: 'Russian Ruble',      short: 'Russian',      rate: 91.0    },
    { code: 'TRY', symbol: '₺',  flag: '🇹🇷', name: 'Turkish Lira',       short: 'Turkish',      rate: 32.0    },
    { code: 'RON', symbol: 'RON',flag: '🇷🇴', name: 'Romanian Leu',       short: 'Romanian',     rate: 4.58    },
    { code: 'UAH', symbol: '₴',  flag: '🇺🇦', name: 'Ukrainian Hryvnia',  short: 'Ukrainian',    rate: 41.2    },
    // ─ Americas ─
    { code: 'CAD', symbol: 'CAD',flag: '🇨🇦', name: 'Canadian Dollar',    short: 'Canadian',     rate: 1.36    },
    { code: 'MXN', symbol: 'MXN',flag: '🇲🇽', name: 'Mexican Peso',       short: 'Mexican',      rate: 17.2    },
    { code: 'BRL', symbol: 'R$', flag: '🇧🇷', name: 'Brazilian Real',     short: 'Brazilian',    rate: 5.05    },
    { code: 'ARS', symbol: 'ARS',flag: '🇦🇷', name: 'Argentine Peso',     short: 'Argentine',    rate: 915     },
    { code: 'CLP', symbol: 'CLP',flag: '🇨🇱', name: 'Chilean Peso',       short: 'Chilean',      rate: 950     },
    { code: 'COP', symbol: 'COP',flag: '🇨🇴', name: 'Colombian Peso',     short: 'Colombian',    rate: 3900    },
    // ─ Oceania ─
    { code: 'AUD', symbol: 'AUD',flag: '🇦🇺', name: 'Australian Dollar',  short: 'Australian',   rate: 1.53    },
    { code: 'NZD', symbol: 'NZD',flag: '🇳🇿', name: 'New Zealand Dollar', short: 'New Zealand',  rate: 1.64    },
    // ─ Africa ──
    { code: 'ZAR', symbol: 'ZAR',flag: '🇿🇦', name: 'South African Rand', short: 'S. African',   rate: 18.6    },
    { code: 'NGN', symbol: '₦',  flag: '🇳🇬', name: 'Nigerian Naira',     short: 'Nigerian',     rate: 1580    },
    { code: 'KES', symbol: 'KES',flag: '🇰🇪', name: 'Kenyan Shilling',    short: 'Kenyan',       rate: 129     },
    { code: 'GHS', symbol: 'GHS',flag: '🇬🇭', name: 'Ghanaian Cedi',      short: 'Ghanaian',     rate: 15.4    },
    { code: 'MAD', symbol: 'MAD',flag: '🇲🇦', name: 'Moroccan Dirham',    short: 'Moroccan',     rate: 10.0    },
    { code: 'TND', symbol: 'TND',flag: '🇹🇳', name: 'Tunisian Dinar',     short: 'Tunisian',     rate: 3.12    },
  ];

  var STORAGE_KEY = 'fg_currency';
  var _current    = null;   // null = not selected yet → show "Change Currency"
  var _usdPrice   = null;

  // ─ Persistence ──
  function _load() {
    try {
      var v = localStorage.getItem(STORAGE_KEY);
      if (v && CURRENCIES.find(function(c){ return c.code === v; })) _current = v;
    } catch(_) {}
  }

  function _save(code) {
    try { localStorage.setItem(STORAGE_KEY, code); } catch(_) {}
    document.cookie = STORAGE_KEY + '=' + code + ';path=/;max-age=31536000;SameSite=Lax';
  }

  // ─ Helpers ─
  function getCurrency(code) {
    if (!code) return null;
    return CURRENCIES.find(function(c){ return c.code === code; }) || null;
  }

  function convert(usdAmount) {
    var cur = getCurrency(_current);
    if (!cur) return usdAmount;
    return usdAmount * cur.rate;
  }

  function format(nativeAmount) {
    var cur = getCurrency(_current) || CURRENCIES[0];
    var decimals = cur.code === 'JPY' ? 0 : cur.code === 'KWD' ? 3 : 2;
    // If symbol == code (no real unicode symbol), put it after the number
    var hasSymbol = cur.symbol !== cur.code;
    var num;
    try {
      num = nativeAmount.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
    } catch(_) {
      num = nativeAmount.toFixed(decimals);
    }
    // Single-char symbols: $1,234 — multi/code: 1,234 SAR
    if (hasSymbol && cur.symbol.length === 1) {
      return cur.symbol + ' ' + num + ' ' + cur.code;
    } else {
      return num + ' ' + cur.code;
    }
  }

  function fromFiro(firoAmount) {
    if (!_usdPrice || !firoAmount || !_current) return null;
    return format(convert(firoAmount * _usdPrice));
  }

  // ─ Price fetch ─
  async function fetchPrice(fresh) {
    try {
      var url = '/api/price' + (fresh ? '?fresh=true' : '');
      var r   = await fetch(url);
      if (!r.ok) return;
      var d   = await r.json();
      if (d.price) {
        _usdPrice = parseFloat(d.price);
        document.dispatchEvent(new CustomEvent('fg:price-updated', {
          detail: { price: _usdPrice }
        }));
        _updatePriceBadges();
      }
    } catch(_) {}
  }

  function _updatePriceBadges() {
    document.querySelectorAll('.fg-curr-price-line').forEach(function(el) {
      if (_usdPrice && _current) {
        var cur = getCurrency(_current);
        var rate = (_usdPrice * cur.rate).toFixed(4);
        el.textContent = '1 FIRO ≈ ' + cur.symbol + rate + ' ' + cur.code;
        el.style.display = '';
      } else {
        el.style.display = 'none';
      }
    });
  }

  // ─ Select ─
  function select(code) {
    var cur = getCurrency(code);
    if (!cur) return;
    _current = cur.code;
    _save(cur.code);
    _updateAll();
    _updatePriceBadges();
    document.dispatchEvent(new CustomEvent('fg:currency-changed', {
      detail: { code: cur.code, symbol: cur.symbol, flag: cur.flag }
    }));
  }

  function _updateAll() {
    document.querySelectorAll('[data-fg-curr-wrap]').forEach(_rebuildWrap);
  }

  // ─ Build dropdown ─
  function _buildMenu(wrap, btn) {
    var menu = document.createElement('div');
    menu.className = 'fg-curr-menu';
    menu.setAttribute('role', 'listbox');

    // Search box
    var searchWrap = document.createElement('div');
    searchWrap.className = 'fg-curr-search-wrap';
    var searchIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
    searchWrap.innerHTML = searchIcon;
    var searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'fg-curr-search';
    searchInput.placeholder = 'Search currency…';
    searchInput.setAttribute('autocomplete', 'off');
    searchWrap.appendChild(searchInput);
    menu.appendChild(searchWrap);

    // Stop any click inside the menu from bubbling to document (would close it)
    menu.addEventListener('click', function(e) { e.stopPropagation(); });

    // Live price line
    var priceLine = document.createElement('div');
    priceLine.className = 'fg-curr-price-line';
    priceLine.style.display = 'none';
    menu.appendChild(priceLine);

    // Divider
    var div = document.createElement('div');
    div.className = 'fg-curr-divider';
    menu.appendChild(div);

    // Currency items
    var listWrap = document.createElement('div');
    listWrap.className = 'fg-curr-list';
    menu.appendChild(listWrap);

    function renderItems(filter) {
      listWrap.innerHTML = '';
      var filtered = filter
        ? CURRENCIES.filter(function(c) {
            var q = filter.toLowerCase();
            return c.code.toLowerCase().includes(q) || c.name.toLowerCase().includes(q);
          })
        : CURRENCIES;

      if (!filtered.length) {
        var none = document.createElement('div');
        none.className = 'fg-curr-no-results';
        none.textContent = 'No currencies found';
        listWrap.appendChild(none);
        return;
      }

      filtered.forEach(function(c) {
        var item = document.createElement('button');
        item.className = 'fg-curr-item' + (c.code === _current ? ' active' : '');
        item.setAttribute('role', 'option');
        item.setAttribute('data-code', c.code);

        // Convert FIRO → this currency (if price available)
        var priceStr = '';
        if (_usdPrice) {
          var rate    = (_usdPrice * c.rate);
          var dec     = c.code === 'JPY' ? 0 : c.code === 'KWD' ? 3 : 2;
          var hasSymbol = c.symbol !== c.code;
          var numStr;
          try { numStr = rate.toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
          catch(_) { numStr = rate.toFixed(dec); }
          var display = (hasSymbol && c.symbol.length === 1)
            ? c.symbol + ' ' + numStr
            : numStr + ' ' + c.code;
          priceStr = '<span class="fg-curr-item-rate">' + display + '</span>';
        }

        item.innerHTML =
          '<span class="fg-curr-item-flag">' + c.flag + '</span>' +
          '<span class="fg-curr-item-info">' +
            '<span class="fg-curr-item-name">' + c.short + '</span>' +
            '<span class="fg-curr-item-code">' + c.code + '</span>' +
          '</span>' +
          priceStr +
          '<svg class="fg-curr-item-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>';

        item.onclick = function(e) {
          e.stopPropagation();
          select(c.code);
          wrap.classList.remove('open');
          btn.setAttribute('aria-expanded', 'false');
        };
        listWrap.appendChild(item);
      });
    }

    renderItems('');

    searchInput.addEventListener('input', function() {
      renderItems(this.value);
    });

    // Re-render items when price updates (to show rates)
    document.addEventListener('fg:price-updated', function() {
      renderItems(searchInput.value);
      _updatePriceBadges();
    });

    return menu;
  }

  // ─ Build full wrap ─
  function _rebuildWrap(wrap) {
    var variant = wrap.getAttribute('data-fg-curr-variant') || 'compact';
    var cur     = getCurrency(_current);

    wrap.innerHTML = '';

    // Build new classes but PRESERVE any existing classes on the element
    // (e.g. lp-nav-hide-mobile, lp-curr-desktop-only set in HTML)
    var existingClasses = (wrap.getAttribute('data-original-classes') || '');
    if (!existingClasses && wrap.className) {
      // First time — save original classes before we overwrite them
      existingClasses = Array.from(wrap.classList)
        .filter(function(c) { return c !== 'fg-curr-wrap' && c !== 'fg-curr-mobile' && c !== 'fg-curr-sidebar-inline'; })
        .join(' ');
      wrap.setAttribute('data-original-classes', existingClasses);
    }

    var classes = 'fg-curr-wrap';
    if (variant === 'mobile')          classes += ' fg-curr-mobile';
    if (variant === 'sidebar-inline')  classes += ' fg-curr-sidebar-inline';
    if (existingClasses)               classes += ' ' + existingClasses;

    wrap.className = classes;
    wrap.setAttribute('data-fg-curr-wrap', '1');
    wrap.setAttribute('data-fg-curr-variant', variant);

    // Toggle button
    var btn = document.createElement('button');
    btn.className = 'fg-curr-toggle';
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');

    if (!cur) {
      // No currency selected yet — globe icon + "Change Currency"
      btn.innerHTML =
        '<svg class="fg-curr-globe" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 010 20M12 2a15.3 15.3 0 000 20"/></svg>' +
        '<span class="fg-curr-label">Change Currency</span>' +
        '<svg class="fg-curr-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>';
    } else {
      // Currency selected — flag + "Change Currency"
      btn.innerHTML =
        '<span class="fg-curr-flag">' + cur.flag + '</span>' +
        '<span class="fg-curr-label">Change Currency</span>' +
        '<svg class="fg-curr-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>';
    }

    btn.onclick = function(e) {
      e.stopPropagation();
      var isOpen = wrap.classList.contains('open');
      // Close all other currency dropdowns
      document.querySelectorAll('[data-fg-curr-wrap].open').forEach(function(w) {
        w.classList.remove('open');
        var b = w.querySelector('.fg-curr-toggle');
        if (b) b.setAttribute('aria-expanded', 'false');
      });
      // Close any open i18n switcher too
      document.querySelectorAll('.fg-i18n-switcher.is-open').forEach(function(s) {
        s.classList.remove('is-open');
        var b = s.querySelector('.fg-i18n-toggle');
        if (b) b.setAttribute('aria-expanded', 'false');
        var m = s.querySelector('.fg-i18n-menu');
        if (m) m.style.display = 'none';
      });
      if (!isOpen) {
        wrap.classList.add('open');
        btn.setAttribute('aria-expanded', 'true');
        setTimeout(function() {
          var s = wrap.querySelector('.fg-curr-search');
          if (s) s.focus();
        }, 60);
      }
    };

    var menu = _buildMenu(wrap, btn);
    wrap.appendChild(btn);
    wrap.appendChild(menu);
  }

  // ─ Mount ──
  function mount(el, opts) {
    if (!el) return;
    opts = opts || {};
    el.setAttribute('data-fg-curr-wrap', '1');
    el.setAttribute('data-fg-curr-variant', opts.variant || 'compact');
    _rebuildWrap(el);
  }

  function mountAll() {
    document.querySelectorAll('[data-currency-switcher]').forEach(function(el) {
      mount(el, { variant: el.getAttribute('data-currency-switcher') || 'compact' });
    });
  }

  // Close on outside click / Escape
  document.addEventListener('click', function() {
    document.querySelectorAll('[data-fg-curr-wrap].open').forEach(function(w) {
      w.classList.remove('open');
      var b = w.querySelector('.fg-curr-toggle');
      if (b) b.setAttribute('aria-expanded', 'false');
    });
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('[data-fg-curr-wrap].open').forEach(function(w) {
        w.classList.remove('open');
        var b = w.querySelector('.fg-curr-toggle');
        if (b) b.setAttribute('aria-expanded', 'false');
      });
    }
  });

  // Cross-close: when i18n switcher opens, close currency — and vice versa
  // i18n.js adds class 'is-open' to .fg-i18n-switcher elements
  var _crossObs = new MutationObserver(function(mutations) {
    mutations.forEach(function(m) {
      if (m.type === 'attributes' && m.attributeName === 'class') {
        var el = m.target;
        // If an i18n switcher just opened, close all currency dropdowns
        if (el.classList.contains('fg-i18n-switcher') && el.classList.contains('is-open')) {
          document.querySelectorAll('[data-fg-curr-wrap].open').forEach(function(w) {
            w.classList.remove('open');
            var b = w.querySelector('.fg-curr-toggle');
            if (b) b.setAttribute('aria-expanded', 'false');
          });
        }
      }
    });
  });

  // Start observing once DOM is ready
  document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.fg-i18n-switcher').forEach(function(el) {
      _crossObs.observe(el, { attributes: true, attributeFilter: ['class'] });
    });
    // Also re-observe when new switchers are dynamically added (mobile nav)
    new MutationObserver(function(mutations) {
      mutations.forEach(function(m) {
        m.addedNodes.forEach(function(node) {
          if (node.nodeType === 1) {
            node.querySelectorAll && node.querySelectorAll('.fg-i18n-switcher').forEach(function(el) {
              _crossObs.observe(el, { attributes: true, attributeFilter: ['class'] });
            });
          }
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  });

  // ─ Init ─
  _load();
  document.addEventListener('DOMContentLoaded', function() {
    mountAll();
    fetchPrice(false); // cached price on load
  });

  // ─ Public API ─
  global.FGCurrency = {
    currencies:       CURRENCIES,
    getSelected:      function() { return getCurrency(_current); },
    isSelected:       function() { return !!_current; },
    select:           select,
    convert:          convert,
    format:           format,
    fromFiro:         fromFiro,
    fetchPrice:       fetchPrice,
    mount:            mount,
    mountAll:         mountAll,
    get usdPrice()    { return _usdPrice; },
  };

})(window);
