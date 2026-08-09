(function(global) {
  'use strict';

  var BACKOFF_BASE = 1000;
  var BACKOFF_MAX  = 30000;
  var MAX_RETRIES  = 15;
  var FALLBACK_MS  = 15000;

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

  function _revalidate(restUrl, onData, restToken) {
    if (!restUrl) return;
    var headers = restToken ? { 'X-Checkout-Token': restToken } : {};
    fetch(restUrl, { credentials: 'include', headers: headers })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) { if (d) onData(d); })
      .catch(function() {});
  }

  function SSEStream(opts) {
    this.url         = opts.url;
    this.restUrl     = opts.restUrl || null;
    this.restToken   = opts.restToken || null;
    this.onEvent     = opts.onEvent  || function() {};
    this.onClose     = opts.onClose  || function() {};
    this.onReconnect = opts.onReconnect || null;
    this._es         = null;
    this._retries    = 0;
    this._closed     = false;
    this._paused     = false;
    this._timer      = null;
    this._fallback   = null;
    this._isFirst    = true;

    this._registerHandlers();
    this._connect();
  }

  SSEStream.prototype._registerHandlers = function() {
    var self = this;
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

    if (isReconnect && this.restUrl && this.onReconnect) {
      _revalidate(this.restUrl, this.onReconnect, this.restToken);
    }

    try {
      this._es = new EventSource(this.url, { withCredentials: true });

      this._es.onopen = function() {
        self._retries = 0;
        _dotState('live');
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

  SSEStream.prototype._startFallback = function() {
    if (this._fallback || this._closed || !this.restUrl) return;
    var self = this;
    this._fallback = setInterval(function() {
      if (self._closed) { clearInterval(self._fallback); return; }
      if (document.hidden) return;
      _revalidate(self.restUrl, self.onEvent, self.restToken);
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

  global.FGStream = {
    payment: function(paymentId, token, onEvent, onClose, onReconnect) {
      var t    = token ? '?t=' + encodeURIComponent(token) : '';
      var url  = '/api/events/payment/' + encodeURIComponent(paymentId) + t;
      var rest = '/api/payments/public/' + encodeURIComponent(paymentId);
      return new SSEStream({
        url: url, restUrl: rest, restToken: token,
        onEvent: onEvent, onClose: onClose, onReconnect: onReconnect,
      });
    },

    merchant: function(onEvent, onClose) {
      return new SSEStream({
        url: '/api/events/merchant',
        restUrl: null,
        onEvent: onEvent, onClose: onClose,
      });
    },
  };

})(window);
