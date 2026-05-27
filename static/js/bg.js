
(function () {
  'use strict';

  var canvas = document.getElementById('bg-canvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var W = 0, H = 0;

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }
  window.addEventListener('resize', resize, { passive: true });
  resize();

  var PAL = [
    [14,  8,  3],
    [20, 12,  4],
    [26, 16,  5],
    [12,  7,  2],
    [34, 21,  5],
    [42, 26,  7],
    [18, 11,  4],
    [ 9,  6,  2],
    [24, 16,  8],
    [50, 34,  8, 1],
    [44, 28,  6, 1],
  ];

  function rnd(a, b)  { return a + Math.random() * (b - a); }
  function rndI(a, b) { return Math.floor(rnd(a, b + 1)); }

  function Poly(init) {
    this.sides   = rndI(3, 8);
    this.radius  = rnd(60, 380);
    this.offsets = [];
    for (var i = 0; i < this.sides; i++) this.offsets.push(rnd(0.48, 1.0));

    if (init) {
      this.x = rnd(-80, W + 80);
      this.y = rnd(-80, H + 80);
    } else {
      var edge = rndI(0, 3);
      this.x = edge === 0 ? -(this.radius + 20) : edge === 1 ? W + this.radius + 20 : rnd(-80, W + 80);
      this.y = edge === 2 ? -(this.radius + 20) : edge === 3 ? H + this.radius + 20 : rnd(-80, H + 80);
    }

    var spd  = rnd(0.05, 0.26);
    var ang  = rnd(0, Math.PI * 2);
    this.vx  = Math.cos(ang) * spd;
    this.vy  = Math.sin(ang) * spd;
    this.rot = rnd(0, Math.PI * 2);
    this.dr  = rnd(-0.0005, 0.0005);

    var c    = PAL[rndI(0, PAL.length - 1)];
    this.cr  = c[0]; this.cg = c[1]; this.cb = c[2]; this.hi = !!c[3];

    var base = this.hi ? rnd(0.06, 0.18) : rnd(0.03, 0.16);
    this.a   = base;
    this.da  = rnd(-0.00015, 0.00015);
    this.aMin = 0.02;
    this.aMax = this.hi ? 0.20 : 0.18;

    this.fill  = Math.random() > 0.28;
    this.blur  = Math.random() > 0.55 ? rnd(5, 26) : 0;
    this.lw    = rnd(0.4, 2.0);
  }

  Poly.prototype.buildPath = function () {
    ctx.beginPath();
    for (var i = 0; i < this.sides; i++) {
      var a = (i / this.sides) * Math.PI * 2 + this.rot;
      var r = this.radius * this.offsets[i];
      var x = this.x + Math.cos(a) * r;
      var y = this.y + Math.sin(a) * r;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.closePath();
  };

  Poly.prototype.draw = function () {
    ctx.save();
    if (this.blur > 0) ctx.filter = 'blur(' + this.blur + 'px)';
    var col = 'rgba(' + this.cr + ',' + this.cg + ',' + this.cb + ',' + this.a.toFixed(3) + ')';
    if (this.fill) {
      ctx.fillStyle = col;
      this.buildPath();
      ctx.fill();
    } else {
      ctx.strokeStyle = col;
      ctx.lineWidth = this.lw;
      this.buildPath();
      ctx.stroke();
    }
    ctx.restore();
  };

  Poly.prototype.update = function () {
    this.x   += this.vx;
    this.y   += this.vy;
    this.rot += this.dr;
    this.a   += this.da;
    if (this.a < this.aMin) { this.a = this.aMin; this.da *= -1; }
    if (this.a > this.aMax) { this.a = this.aMax; this.da *= -1; }
    var m = this.radius + 60;
    if (this.x < -m || this.x > W + m || this.y < -m || this.y > H + m) {
      Poly.call(this, false);
    }
  };

  var COUNT = Math.max(12, Math.min(28, Math.floor(W * H / 40000)));
  var polys = [];
  for (var i = 0; i < COUNT; i++) polys.push(new Poly(true));

  function frame() {
    ctx.clearRect(0, 0, W, H);

    ctx.fillStyle = '#0a0804';
    ctx.fillRect(0, 0, W, H);

    var g1 = ctx.createRadialGradient(W * .5, 0, 0, W * .5, 0, W * .52);
    g1.addColorStop(0,   'rgba(155, 85, 8, 0.052)');
    g1.addColorStop(0.5, 'rgba(95, 50, 4, 0.024)');
    g1.addColorStop(1,   'transparent');
    ctx.fillStyle = g1;
    ctx.fillRect(0, 0, W, H);

    var g2 = ctx.createRadialGradient(W * .82, H * .88, 0, W * .82, H * .88, W * .36);
    g2.addColorStop(0,   'rgba(85, 44, 4, 0.038)');
    g2.addColorStop(1,   'transparent');
    ctx.fillStyle = g2;
    ctx.fillRect(0, 0, W, H);

    polys.sort(function(a, b) { return b.radius - a.radius; });
    for (var i = 0; i < polys.length; i++) {
      polys[i].draw();
      polys[i].update();
    }

    requestAnimationFrame(frame);
  }

  frame();
}());
