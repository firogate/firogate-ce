(function () {
  var W = 180, H = 90;
  var LAND = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAf8Af/AAAAAAAAAAAAAAAAAAAAAAAA//////+AAAAAAAADgAAAAAAAAAAAABf+f///4AA+AAAAAB8AAAAAAAAAABwAz4f///wAAYAAAEAAGAAAAAAAAAAAPi/wAf//wAAAAADAAf/4AHAAAAAAAH4n38AP//gAAAAAMBD///8DAAAADwACfw3/gP//gAAA4AMH///////gAAf////59jwH/+AAAP/gA//////////8P//////h+H/gAAAf/z///////////M///////n8D8A+AA+/f///////////AP/////4g4D4AAAH9/////////////Af/////gHgA4AAAP5///////////PwAHgP///gH0AAAAAH4/////////+CMAADAB///4H+AAAAGCx/////////8A8AAAAA////n/gAAAODj/////////wA4AAAAAf///v/wAAAbP///////////AwAAAAAP/////wAAAH////////////AAAAAAAH////8YAAAB///////////9AAAAAAAD////8EAAAB///////////9AAAAAAAD////+AAAAB//5fP//////4AAAAAAAD////gAAAAPxvwPP//////jAAAAAAAD////AAAAAfib//n/////+CAAAAAAAD///+AAAAAfADf/n////+MCAAAAAAAB///8AAAAAOeRP/3/////mOAAAAAAAA///8AAAAAH+AA///////E8AAAAAAAAf//wAAAAAP/gB///////hgAAAAAAAAP//gAAAAAf/7////////gAAAAAAAAAH/gwAAAAAf////f/////gAAAAAAAAAF+AQAAAAB///+/v/////AAAAAAAAAAC+AAAAAAD/////0X////AAAAAAAAAAAeAQAAAAD////f/D///8gAAAAAAAAAAfEEAAAAH////v/B/z/QAAAAAAAAAAAPMBgAAAD////v+A/B+gAAAAAAAAAAAD8AAAAAD////34AeB/AgAAAAAAAAAAAPAAAAAH////3wAcAfggAAAAAAAAAAADAAAAAD////+AAcAfgAAAAAAAAAAAABDwAAAD////9wAMATAYAAAAAAAAAAAA//AAAB/////gAOAQAIAAAAAAAAAAAAH/gAAA/////gACAIAIAAAAAAAAAAAAH/8AAAfH///AAAAsGAAAAAAAAAAAAAH/+AAAAB//+AAAAcOAAAAAAAAAAAAAP/+AAAAB//8AAAAc+gAAAAAAAAAAAAP//gAAAD//4AAAAMeggAAAAAAAAAAAP//8AAAB//wAAAAGdg+AAAAAAAAAAAf///AAAA//wAAAACAAPkAAAAAAAAAAP///gAAA//wAAAABwAHwAAAAAAAAAAP///AAAA//wAAAAAAIDYAAAAAAAAAAH///AAAAf/wAAAAAAAAAAAAAAAAAAAD//+AAAA//wgAAAAABxAAAAAAAAAAAD//+AAAA//xgAAAAAPxgAAAAAAAAAAA//+AAAA//zgAAAAAf/gAAAAAAAAAAAf/8AAAA//DgAAAAAf/wAAAAAAAAAAAf/8AAAAf/DAAAAAD//4CAAAAAAAAAAf/4AAAAf/DAAAAAH//8AAAAAAAAAAAf/AAAAAf+CAAAAAH//8AAAAAAAAAAA//AAAAAf8AAAAAAH//+AAAAAAAAAAA/+AAAAAP8AAAAAAH//+AAAAAAAAAAA/+AAAAAH4AAAAAAD//+AAAAAAAAAAA/8AAAAAHwAAAAAADwf8AAAAAAAAAAA/4AAAAAAAAAAAAACAH4AAAAAAAAAAB/wAAAAAAAAAAAAAAAD4AEAAAAAAAAB/AAAAAAAAAAAAAAAAAAAGAAAAAAAAB+AAAAAAAAAAAAAAAAAQAMAAAAAAAAB8AAAAAAAAAAAAAAAAAQAYAAAAAAAAB4AAAAAAAAAAAAAAAAAAAwAAAAAAAAD4AAAAAAAAAAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAAAAAAAAAAAAAAAAAAAADwAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAAAAAAA+AA//+//gAAAAAAAAAAAMAAAAAAADP/+H//////AAAAAAAAAAB+AAAAJf////8////////gAAAAAAPGB/AAAD///////////////gAAAf/////8AAAH//////////////+AABn/////+AAAH///////////////8AAE//////4ABw////////////////8AAAX//////nHg////////////////4AAAf/////////////////////////+A/4H///////////////////////////////////////////////////////////////////////////////////////';
  var RAD = Math.PI / 180;
  var bits = null;
  var cache = {};
  var items = [];
  var running = false;
  var last = 0;
  var calm = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var CITIES = [
    [40.7, -74], [34, -118.2], [43.7, -79.4], [19.4, -99.1], [-23.5, -46.6], [-34.6, -58.4], [4.7, -74.1],
    [51.5, -0.1], [48.9, 2.4], [52.5, 13.4], [59.3, 18.1], [41, 29], [55.8, 37.6], [40.4, -3.7],
    [30, 31.2], [6.5, 3.4], [-1.3, 36.8], [-26.2, 28], [33.6, -7.6],
    [25.2, 55.3], [24.7, 46.7], [28.6, 77.2], [19.1, 72.9], [13.8, 100.5], [1.35, 103.8],
    [39.9, 116.4], [31.2, 121.5], [37.6, 127], [35.7, 139.7], [-6.2, 106.8], [-33.9, 151.2], [-36.8, 174.8]
  ];
  var cityVecs = CITIES.map(function (c) {
    var la = c[0] * Math.PI / 180, lo = c[1] * Math.PI / 180;
    return [Math.cos(la) * Math.cos(lo), Math.sin(la), Math.cos(la) * Math.sin(lo)];
  });

  function mask() {
    if (bits) return bits;
    var s = atob(LAND);
    bits = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) bits[i] = s.charCodeAt(i);
    return bits;
  }

  function isLand(lat, lon) {
    var x = Math.floor((lon + 180) / 360 * W) % W;
    var y = Math.min(H - 1, Math.max(0, Math.floor((90 - lat) / 180 * H)));
    var i = y * W + x;
    return (mask()[i >> 3] >> (7 - (i & 7))) & 1;
  }

  function points(step) {
    if (cache[step]) return cache[step];
    var land = [], sea = [];
    for (var lat = -87; lat <= 87; lat += step) {
      var c = Math.cos(lat * RAD), s = Math.sin(lat * RAD);
      var n = Math.max(1, Math.round(360 / step * c));
      for (var k = 0; k < n; k++) {
        var lon = -180 + (k + 0.5) * 360 / n;
        var arr = isLand(lat, lon) ? land : sea;
        arr.push(c * Math.cos(lon * RAD), s, c * Math.sin(lon * RAD));
      }
    }
    cache[step] = { land: new Float32Array(land), sea: new Float32Array(sea) };
    return cache[step];
  }

  function num(el, name, def) {
    var v = parseFloat(el.getAttribute(name));
    return isNaN(v) ? def : v;
  }

  function setup(cv) {
    if (cv._globe) return;
    var it = {
      cv: cv,
      ctx: cv.getContext('2d'),
      step: num(cv, 'data-globe-step', 3),
      speed: num(cv, 'data-globe-speed', 5) * RAD,
      tilt: num(cv, 'data-globe-tilt', 18) * RAD,
      angle: num(cv, 'data-globe-start', 20) * RAD,
      seen: false,
      hopMax: Math.round(num(cv, 'data-globe-hops', 0)),
      alpha: num(cv, 'data-globe-alpha', 1),
      hops: [], hopWait: 0.4, hopColor: '',
      w: 0, h: 0, color: '', tick: 0
    };
    cv._globe = it;
    items.push(it);
    if (window.IntersectionObserver) {
      new IntersectionObserver(function (es) {
        it.seen = es[0].isIntersecting;
        if (it.seen) { size(it); draw(it); start(); }
      }).observe(cv);
    } else {
      it.seen = true;
    }
    if (window.ResizeObserver) {
      new ResizeObserver(function () { if (size(it)) draw(it); }).observe(cv);
    }
    size(it);
    draw(it);
  }

  function size(it) {
    var r = it.cv.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
    if (!w || !h) return false;
    if (w === it.w && h === it.h) return false;
    it.w = w; it.h = h; it.dpr = dpr;
    it.cv.width = w; it.cv.height = h;
    return true;
  }

  function layer(it, pts, R, cx, cy, base, square) {
    var ctx = it.ctx;
    var ca = Math.cos(it.angle), sa = Math.sin(it.angle);
    var ct = Math.cos(it.tilt), st = Math.sin(it.tilt);
    var buckets = [[], [], [], []];
    for (var i = 0; i < pts.length; i += 3) {
      var x = pts[i], y = pts[i + 1], z = pts[i + 2];
      var x1 = x * ca + z * sa;
      var z1 = z * ca - x * sa;
      var y2 = y * ct - z1 * st;
      var z2 = y * st + z1 * ct;
      if (z2 <= 0.02) continue;
      var b = z2 > 0.75 ? 3 : z2 > 0.5 ? 2 : z2 > 0.25 ? 1 : 0;
      buckets[b].push(cx + x1 * R, cy - y2 * R, z2);
    }
    for (var k = 0; k < 4; k++) {
      var arr = buckets[k];
      if (!arr.length) continue;
      ctx.globalAlpha = base * (0.28 + 0.24 * k);
      ctx.beginPath();
      for (var j = 0; j < arr.length; j += 3) {
        var r = it.dot * (0.55 + 0.45 * arr[j + 2]);
        if (square) {
          ctx.rect(arr[j] - r * 0.7, arr[j + 1] - r * 0.7, r * 1.4, r * 1.4);
        } else {
          ctx.moveTo(arr[j] + r, arr[j + 1]);
          ctx.arc(arr[j], arr[j + 1], r, 0, 6.2832);
        }
      }
      ctx.fill();
    }
  }

  function project(it, v, alt) {
    var ca = Math.cos(it.angle), sa = Math.sin(it.angle);
    var ct = Math.cos(it.tilt), st = Math.sin(it.tilt);
    var x1 = v[0] * ca + v[2] * sa;
    var z1 = v[2] * ca - v[0] * sa;
    var y2 = v[1] * ct - z1 * st;
    var z2 = v[1] * st + z1 * ct;
    var k = it.R * (1 + (alt || 0));
    var px = x1 * k, py = y2 * k;
    return { x: it.w / 2 + px, y: it.h / 2 - py, z: z2, out: px * px + py * py > it.R * it.R };
  }

  function slerp(a, b, om, t) {
    var so = Math.sin(om);
    var ka = Math.sin((1 - t) * om) / so, kb = Math.sin(t * om) / so;
    return [a[0] * ka + b[0] * kb, a[1] * ka + b[1] * kb, a[2] * ka + b[2] * kb];
  }

  function spawnHop(it) {
    var front = [];
    for (var i = 0; i < cityVecs.length; i++) {
      if (project(it, cityVecs[i], 0).z > 0.25) front.push(i);
    }
    if (front.length < 2) return;
    for (var tries = 0; tries < 12; tries++) {
      var a = front[Math.floor(Math.random() * front.length)];
      var b = Math.random() < 0.75 ? front[Math.floor(Math.random() * front.length)] : Math.floor(Math.random() * cityVecs.length);
      if (a === b) continue;
      var va = cityVecs[a], vb = cityVecs[b];
      var dot = va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2];
      var om = Math.acos(Math.max(-1, Math.min(1, dot)));
      if (om < 0.3 || om > 1.9) continue;
      it.hops.push({ a: va, b: vb, om: om, t: 0, dur: 1.5 + om * 1.1, lift: 0.07 + om * 0.13 });
      return;
    }
  }

  function stepHops(it, dt) {
    if (!it.hopMax) return;
    for (var i = it.hops.length - 1; i >= 0; i--) {
      var h = it.hops[i];
      h.t += dt / h.dur;
      if (h.t > 1.55) it.hops.splice(i, 1);
    }
    it.hopWait -= dt;
    if (it.hopWait <= 0 && it.hops.length < it.hopMax) {
      spawnHop(it);
      it.hopWait = 0.5 + Math.random() * 0.9;
    }
  }

  function ring(ctx, p, r, alpha, width) {
    if (alpha <= 0.01) return;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, 6.2832);
    ctx.stroke();
  }

  function drawHops(it) {
    if (!it.hops.length) return;
    var ctx = it.ctx, d = it.dpr;
    ctx.strokeStyle = it.hopColor;
    ctx.fillStyle = it.hopColor;
    ctx.lineCap = 'round';
    for (var i = 0; i < it.hops.length; i++) {
      var h = it.hops[i];
      var head = Math.min(1, h.t);
      var tail = Math.max(0, Math.min(1, h.t - 0.45));
      var fade = h.t > 1 ? Math.max(0, 1 - (h.t - 1) / 0.55) : 1;
      var pa = project(it, h.a, 0), pb = project(it, h.b, 0);
      if (pa.z > 0) {
        ctx.globalAlpha = 0.9 * fade;
        ctx.beginPath(); ctx.arc(pa.x, pa.y, 2.2 * d, 0, 6.2832); ctx.fill();
        ring(ctx, pa, (3 + 9 * Math.min(1, h.t * 2.2)) * d, 0.5 * (1 - Math.min(1, h.t * 2.2)), 1.2 * d);
      }
      var steps = 26, prev = null;
      for (var k = 0; k <= steps; k++) {
        var s = tail + (head - tail) * k / steps;
        var p = project(it, slerp(h.a, h.b, h.om, s), h.lift * Math.sin(Math.PI * s));
        var vis = p.z > 0 || p.out;
        if (prev && vis && prev.vis) {
          ctx.globalAlpha = (0.12 + 0.78 * k / steps) * fade;
          ctx.lineWidth = (0.8 + 1.1 * k / steps) * d;
          ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(p.x, p.y); ctx.stroke();
        }
        p.vis = vis; prev = p;
      }
      if (h.t < 1 && prev && prev.vis) {
        ctx.globalAlpha = 1;
        ctx.beginPath(); ctx.arc(prev.x, prev.y, 2.6 * d, 0, 6.2832); ctx.fill();
      }
      if (h.t >= 1 && pb.z > 0) {
        var q = (h.t - 1) / 0.55;
        ctx.globalAlpha = 0.95 * fade;
        ctx.beginPath(); ctx.arc(pb.x, pb.y, 2.6 * d, 0, 6.2832); ctx.fill();
        ring(ctx, pb, (4 + 14 * q) * d, 0.7 * (1 - q), 1.4 * d);
      }
    }
    ctx.globalAlpha = 1;
  }

  function onDark(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var m = getComputedStyle(n).backgroundColor.match(/[\d.]+/g);
      if (m && (m.length < 4 || +m[3] > 0.5)) return (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) < 128;
    }
    return !(window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches);
  }

  function draw(it) {
    if (!it.w || !it.h) return;
    if (!it.color || it.tick % 40 === 0) {
      var cs = getComputedStyle(it.cv);
      it.color = cs.color || '#00A2A7';
      it.hopColor = (cs.getPropertyValue('--globe-hop') || '').trim() || it.color;
      var dim = onDark(it.cv) ? 'opacity(.82)' : '';
      if (it.cv.style.filter !== dim) it.cv.style.filter = dim;
    }
    it.tick++;
    var ctx = it.ctx;
    var p = points(it.step);
    var R = (Math.min(it.w, it.h) / 2 - 2 * it.dpr) * (it.hopMax ? 0.86 : 1);
    it.R = R;
    it.dot = Math.max(0.9 * it.dpr, R * it.step * RAD * 0.26);
    ctx.clearRect(0, 0, it.w, it.h);
    ctx.fillStyle = it.color;
    layer(it, p.sea, R, it.w / 2, it.h / 2, 0.22 * it.alpha, true);
    layer(it, p.land, R, it.w / 2, it.h / 2, it.alpha, false);
    ctx.globalAlpha = 1;
    drawHops(it);
  }

  function frame(now) {
    if (!running) return;
    var dt = Math.min(0.1, (now - last) / 1000);
    if (dt < 0.03) { requestAnimationFrame(frame); return; }
    last = now;
    var any = false;
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (!it.seen || !it.cv.isConnected) continue;
      any = true;
      it.angle += it.speed * dt;
      stepHops(it, dt);
      draw(it);
    }
    if (!any || document.hidden || (calm && calm.matches)) { running = false; return; }
    requestAnimationFrame(frame);
  }

  function start() {
    if (running || document.hidden || (calm && calm.matches)) return;
    running = true;
    last = performance.now();
    requestAnimationFrame(frame);
  }

  function scan(root) {
    var list = (root || document).querySelectorAll('canvas[data-globe]');
    for (var i = 0; i < list.length; i++) setup(list[i]);
    start();
  }

  document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(); });
  } else {
    scan();
  }
  window.FGGlobe = { scan: scan };
})();
