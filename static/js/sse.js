/**
 * FiroGate SSE Client — production-hardened.
 *
 * Reconnect revalidation:
 *   On every reconnect the server sends a fresh DB snapshot.
 *   The client also makes one REST call to sync UI on reconnect.
 *   This prevents stale UI after missed events.
 *
 * Browser SSE limits:
 *   HTTP/1.1: 6 connections/domain. We use one stream per page.
 *   HTTP/2 (default via nginx): multiplexed — no practical limit.
 *
 * Lifecycle:
 *   open → live dot (green, fades after 2.5s)
 *   error/retry → sync dot (amber pulse)
 *   terminal/closed → dot hidden
 *
 * Mobile:
 *   visibilitychange → pause on hide, reconnect on show (with 300ms delay)
 *   pagehide → hard close (covers iOS Safari app switch)
 *   beforeunload → hard close
 */
(function(global) {
  'use strict';

  var BACKOFF_BASE = 1000;   // ms
  var BACKOFF_MAX  = 30000;  // ms cap
  var MAX_RETRIES  = 15;
  var FALLBACK_MS  = 15000;  // polling interval when EventSource unavailable

  // ─ Status dot ─
  var _dot = null, _dotTimer = null;

  function _getDot() {
    if (_dot) return _dot;
    if (typeof document === 'undefined') return null;
    _dot = document.createElement('div');
    _dot.id = 'fg-live-dot';
    _dot.style.cssText = [
      'position:fixed', 'bottom:14px', 'right:14px', 'z-index:9100',
      'width:7px', 'height:7px', 'border-radius:50%',
      'opacity:0', 'transition:opacity .5s,background .4s',
      'pointer-events:none',
    ].join(';');
    var s = document.createElement('style');
    s.textContent = '@keyframes _fgpulse{0%,100%{opacity:.2}50%{opacity:1}}';
    document.head.appendChild(s);
    document.body && document.body.appendChild(_dot);
    return _dot;
  }

  function _dotState(state) {
    var d = _getDot();
    if (!d) return;
    clearTimeout(_dotTimer);
    if (state === 'live') {
      d.style.background = '#22C55E';
      d.style.opacity    = '0.8';
      d.style.animation  = 'none';
      _dotTimer = setTimeout(function() { d.style.opacity = '0'; }, 2500);
    } else if (state === 'sync') {
      d.style.background = '#F5C542';
      d.style.opacity    = '1';
      d.style.animation  = '_fgpulse 1.2s ease-in-out infinite';
    } else {
      d.style.opacity = '0';
      d.style.animation = 'none';
    }
  }

  // ─ Reconnect revalidation helper ──
  // Called by payment stream on reconnect to sync UI with DB truth
  function _revalidate(restUrl, onData) {
    if (!restUrl) return;
    fetch(restUrl, { credentials: 'include' })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) { if (d) onData(d); })
      .catch(function() {});
  }

  // ─ SSEStream ─
  function SSEStream(opts) {
    this.url         = opts.url;
    this.restUrl     = opts.restUrl || null;   // for reconnect revalidation
    this.onEvent     = opts.onEvent  || function() {};
    this.onClose     = opts.onClose  || function() {};
    this.onReconnect = opts.onReconnect || null; // optional hook
    this._es         = null;
    this._retries    = 0;
    this._closed     = false;
    this._paused     = false;
    this._timer      = null;
    this._fallback   = null;
    this._isFirst    = true;   // tracks first vs reconnect

    this._registerHandlers();
    this._connect();
  }

  SSEStream.prototype._registerHandlers = function() {
    var self = this;
    // Mobile: iOS pagehide fires when switching apps
    window.addEventListener('pagehide',     function() { self.close(); });
    window.addEventListener('beforeunload', function() { self.close(); });
    document.addEventListener('visibilitychange', function() {
      if (self._closed) return;
      if (document.hidden) {
        self._paused = true;
        self._hardClose();
        _dotState('off');
      } else {
        self._paused = false;
        // Brief delay: lets network restore after device sleep
        clearTimeout(self._timer);
        self._timer = setTimeout(function() { self._connect(); }, 300);
      }
    });
  };

  SSEStream.prototype._connect = function() {
    if (this._closed || this._paused || this._es) return;

    if (!global.EventSource) {
      this._startFallback();
      return;
    }

    var self = this;
    var isReconnect = !this._isFirst;
    this._isFirst = false;

    _dotState('sync');

    // On reconnect: call REST to sync UI with DB truth (before SSE snapshot arrives)
    if (isReconnect && this.restUrl && this.onReconnect) {
      _revalidate(this.restUrl, this.onReconnect);
    }

    try {
      this._es = new EventSource(this.url, { withCredentials: true });

      this._es.onopen = function() {
        self._retries = 0;
        _dotState('live');
        if (isReconnect) {
          // Server also sends fresh DB snapshot on reconnect — handle it
          // via onEvent which checks for "payment.status" / "dashboard.snapshot"
        }
      };

      this._es.onmessage = function(e) {
        var data;
        try { data = JSON.parse(e.data); } catch(_) { return; }
        self.onEvent(data);
        if (_isTerminal(data.type)) {
          self._closed = true;
          self._hardClose();
          self.onClose(data.type);
          _dotState('off');
        }
      };

      this._es.onerror = function() {
        self._hardClose();
        if (!self._closed && !self._paused) self._scheduleRetry();
      };

    } catch(e) {
      this._startFallback();
    }
  };

  SSEStream.prototype._hardClose = function() {
    if (this._es) { this._es.close(); this._es = null; }
    clearTimeout(this._timer);
  };

  SSEStream.prototype._scheduleRetry = function() {
    if (this._closed || this._retries >= MAX_RETRIES) {
      _dotState('off');
      this.onClose('max_retries');
      return;
    }
    var base  = Math.min(BACKOFF_BASE * Math.pow(2, this._retries), BACKOFF_MAX);
    var jitter = Math.random() * 600;
    this._retries++;
    _dotState('sync');
    var self = this;
    this._timer = setTimeout(function() {
      if (!self._closed && !self._paused) self._connect();
    }, base + jitter);
  };

  // Polling fallback when EventSource unavailable
  SSEStream.prototype._startFallback = function() {
    if (this._fallback || this._closed || !this.restUrl) return;
    var self = this;
    this._fallback = setInterval(function() {
      if (self._closed) { clearInterval(self._fallback); return; }
      if (document.hidden) return;
      _revalidate(self.restUrl, self.onEvent);
    }, FALLBACK_MS);
  };

  SSEStream.prototype.close = function() {
    if (this._closed) return;
    this._closed = true;
    this._hardClose();
    clearInterval(this._fallback);
    _dotState('off');
  };

  function _isTerminal(type) {
    return [
      'payment.confirmed', 'payment.expired', 'payment.cancelled',
      'stream.end', 'stream.timeout',
    ].indexOf(type) !== -1;
  }

  // ─ Public API ─
  global.FGStream = {
    /**
     * Checkout page payment stream.
     *
     * @param {string}   paymentId
     * @param {string}   token      — HMAC checkout token
     * @param {function} onEvent    — called with each parsed event
     * @param {function} onClose    — called when stream ends
     * @param {function} onReconnect — called on reconnect with REST data (optional)
     * @returns {SSEStream}
     */
    payment: function(paymentId, token, onEvent, onClose, onReconnect) {
      var t   = token ? '?t=' + encodeURIComponent(token) : '';
      var url = '/api/events/payment/' + encodeURIComponent(paymentId) + t;
      var rest = '/api/payments/public/' + encodeURIComponent(paymentId) + t;
      return new SSEStream({
        url: url, restUrl: rest,
        onEvent: onEvent, onClose: onClose, onReconnect: onReconnect,
      });
    },

    /**
     * Dashboard merchant stream (authenticated).
     *
     * @param {function} onEvent
     * @param {function} onClose
     * @returns {SSEStream}
     */
    merchant: function(onEvent, onClose) {
      return new SSEStream({
        url: '/api/events/merchant',
        restUrl: null,   // dashboard REST sync handled by onEvent snapshot
        onEvent: onEvent, onClose: onClose,
      });
    },
  };

})(window);
