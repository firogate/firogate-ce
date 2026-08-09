(function () {
  'use strict';

  var canvas = document.getElementById('stars');
  if (!canvas) return;
  var context = canvas.getContext('2d');

  var particleCount = 40;
  var flareCount = 16;
  var motion = 0.05;
  var color = '#F5C542';
  var colorPalette = ['#F5C542', '#EFCB68', '#B82334', '#D94F5C', '#E8A33D', '#F5C542'];
  var particleSizeBase = 2;
  var particleSizeMultiplier = 0.5;
  var flareSizeBase = 100;
  var flareSizeMultiplier = 100;
  var lineWidth = 1;
  var linkChance = 75;
  var linkLengthMin = 5;
  var linkLengthMax = 7;
  var linkOpacity = 0.28;
  var linkFade = 90;
  var linkSpeed = 1;
  var glareAngle = -60;
  var glareOpacityMultiplier = 0.05;
  var renderParticles = true;
  var renderParticleGlare = true;
  var renderFlares = true;
  var renderLinks = true;
  var renderMesh = false;
  var flicker = true;
  var flickerSmoothing = 15;
  var randomMotion = true;
  var noiseLength = 1000;
  var noiseStrength = 1;

  var mouse = { x: 0, y: 0 };
  var c = 1000;
  var n = 0;
  var nAngle = 2 * Math.PI / noiseLength;
  var nRad = 100;
  var nPos = { x: 0, y: 0 };
  var points = [];
  var vertices = [];
  var particles = [];
  var flares = [];
  var links = [];
  var EPSILON = 1 / 1048576;

  function randomColor(palette) { return palette[Math.floor(Math.random() * palette.length)]; }
  function random(a, b, float) {
    return float ? Math.random() * (b - a) + a : Math.floor(Math.random() * (b - a + 1)) + a;
  }

  function supertriangle(pts) {
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (var i = pts.length; i--;) {
      if (pts[i][0] < minX) minX = pts[i][0];
      if (pts[i][0] > maxX) maxX = pts[i][0];
      if (pts[i][1] < minY) minY = pts[i][1];
      if (pts[i][1] > maxY) maxY = pts[i][1];
    }
    var dx = maxX - minX, dy = maxY - minY, d = Math.max(dx, dy);
    var midX = minX + 0.5 * dx, midY = minY + 0.5 * dy;
    return [
      [midX - 20 * d, midY - d],
      [midX, midY + 20 * d],
      [midX + 20 * d, midY - d]
    ];
  }

  function circumcircle(pts, i, j, k) {
    var x1 = pts[i][0], y1 = pts[i][1];
    var x2 = pts[j][0], y2 = pts[j][1];
    var x3 = pts[k][0], y3 = pts[k][1];
    var fabsy1y2 = Math.abs(y1 - y2);
    var fabsy2y3 = Math.abs(y2 - y3);
    var xc, yc, m1, m2, mx1, mx2, my1, my2;

    if (fabsy1y2 < EPSILON && fabsy2y3 < EPSILON) throw new Error('Eek! Coincident points!');

    if (fabsy1y2 < EPSILON) {
      m2 = -((x3 - x2) / (y3 - y2));
      mx2 = (x2 + x3) / 2;
      my2 = (y2 + y3) / 2;
      xc = (x2 + x1) / 2;
      yc = m2 * (xc - mx2) + my2;
    } else if (fabsy2y3 < EPSILON) {
      m1 = -((x2 - x1) / (y2 - y1));
      mx1 = (x1 + x2) / 2;
      my1 = (y1 + y2) / 2;
      xc = (x3 + x2) / 2;
      yc = m1 * (xc - mx1) + my1;
    } else {
      m1 = -((x2 - x1) / (y2 - y1));
      m2 = -((x3 - x2) / (y3 - y2));
      mx1 = (x1 + x2) / 2;
      mx2 = (x2 + x3) / 2;
      my1 = (y1 + y2) / 2;
      my2 = (y2 + y3) / 2;
      xc = (m1 * mx1 - m2 * mx2 + my2 - my1) / (m1 - m2);
      yc = fabsy1y2 > fabsy2y3 ? m1 * (xc - mx1) + my1 : m2 * (xc - mx2) + my2;
    }

    var dx = x2 - xc, dy = y2 - yc;
    return { i: i, j: j, k: k, x: xc, y: yc, r: dx * dx + dy * dy };
  }

  function dedup(edges) {
    var a, b, m, n;
    for (var j = edges.length; j;) {
      b = edges[--j]; a = edges[--j];
      for (var i = j; i;) {
        n = edges[--i]; m = edges[--i];
        if ((a === m && b === n) || (a === n && b === m)) {
          edges.splice(j, 2);
          edges.splice(i, 2);
          break;
        }
      }
    }
  }

  function delaunay(pts) {
    var n = pts.length;
    if (n < 3) return [];
    pts = pts.slice(0);

    var indices = new Array(n);
    for (var i = n; i--;) indices[i] = i;
    indices.sort(function (i, j) { return pts[j][0] - pts[i][0]; });

    var st = supertriangle(pts);
    pts.push(st[0], st[1], st[2]);

    var open = [circumcircle(pts, n + 0, n + 1, n + 2)];
    var closed = [];
    var edges = [];

    for (i = indices.length; i--; edges.length = 0) {
      var c2 = indices[i];
      for (var j = open.length; j--;) {
        var dx = pts[c2][0] - open[j].x;
        if (dx > 0 && dx * dx > open[j].r) {
          closed.push(open[j]);
          open.splice(j, 1);
          continue;
        }
        var dy = pts[c2][1] - open[j].y;
        if (dx * dx + dy * dy - open[j].r > EPSILON) continue;
        edges.push(open[j].i, open[j].j, open[j].j, open[j].k, open[j].k, open[j].i);
        open.splice(j, 1);
      }
      dedup(edges);
      for (j = edges.length; j;) {
        var b2 = edges[--j], a2 = edges[--j];
        open.push(circumcircle(pts, a2, b2, c2));
      }
    }

    for (i = open.length; i--;) closed.push(open[i]);
    open.length = 0;

    for (i = closed.length; i--;) {
      if (closed[i].i < n && closed[i].j < n && closed[i].k < n) {
        open.push(closed[i].i, closed[i].j, closed[i].k);
      }
    }
    return open;
  }

  function sizeRatio() { return canvas.width >= canvas.height ? canvas.width : canvas.height; }

  function noisePoint(t) {
    var angle = nAngle * t;
    return { x: nRad * Math.cos(angle), y: nRad * Math.sin(angle) };
  }

  function position(x, y, z) {
    return {
      x: x * canvas.width + (canvas.width / 2 - mouse.x + (nPos.x - 0.5) * noiseStrength) * z * motion,
      y: y * canvas.height + (canvas.height / 2 - mouse.y + (nPos.y - 0.5) * noiseStrength) * z * motion
    };
  }

  function Particle() {
    this.x = random(-0.1, 1.1, true);
    this.y = random(-0.1, 1.1, true);
    this.z = random(0, 4);
    this.color = randomColor(colorPalette);
    this.opacity = random(0.1, 1, true);
    this.flicker = 0;
    this.neighbors = [];
  }

  Particle.prototype.render = function () {
    var p = position(this.x, this.y, this.z);
    var r = (this.z * particleSizeMultiplier + particleSizeBase) * (sizeRatio() / 1000);
    var opacity = this.opacity;

    if (flicker) {
      var f = random(-0.5, 0.5, true);
      this.flicker += (f - this.flicker) / flickerSmoothing;
      if (this.flicker > 0.5) this.flicker = 0.5;
      if (this.flicker < -0.5) this.flicker = -0.5;
      opacity += this.flicker;
      if (opacity > 1) opacity = 1;
      if (opacity < 0) opacity = 0;
    }

    context.fillStyle = this.color;
    context.globalAlpha = opacity;
    context.beginPath();
    context.arc(p.x, p.y, r, 0, 2 * Math.PI, false);
    context.fill();
    context.closePath();

    if (renderParticleGlare) {
      context.globalAlpha = opacity * glareOpacityMultiplier;
      context.beginPath();
      context.ellipse(p.x, p.y, 100 * r, r, (glareAngle - (nPos.x - 0.5) * noiseStrength * motion) * (Math.PI / 180), 0, 2 * Math.PI, false);
      context.fill();
      context.closePath();
    }
    context.globalAlpha = 1;
  };

  function Flare() {
    this.x = random(-0.25, 1.25, true);
    this.y = random(-0.25, 1.25, true);
    this.z = random(0, 2);
    this.color = randomColor(colorPalette);
    this.opacity = random(0.001, 0.01, true);
  }

  Flare.prototype.render = function () {
    var p = position(this.x, this.y, this.z);
    var r = (this.z * flareSizeMultiplier + flareSizeBase) * (sizeRatio() / 1000);
    context.beginPath();
    context.globalAlpha = this.opacity;
    context.arc(p.x, p.y, r, 0, 2 * Math.PI, false);
    context.fillStyle = this.color;
    context.fill();
    context.closePath();
    context.globalAlpha = 1;
  };

  function Link(startVertex, length) {
    this.length = length;
    this.verts = [startVertex];
    this.stage = 0;
    this.linked = [startVertex];
    this.distances = [];
    this.traveled = 0;
    this.fade = 0;
    this.finished = false;
  }

  Link.prototype.drawLine = function (pts, opacity) {
    if (typeof opacity !== 'number') opacity = linkOpacity;
    if (pts.length <= 1 || opacity <= 0) return;
    context.globalAlpha = opacity;
    context.beginPath();
    for (var i = 0; i < pts.length - 1; i++) {
      context.moveTo(pts[i][0], pts[i][1]);
      context.lineTo(pts[i + 1][0], pts[i + 1][1]);
    }
    context.strokeStyle = '#F5C542';
    context.lineWidth = lineWidth;
    context.stroke();
    context.closePath();
    context.globalAlpha = 1;
  };

  Link.prototype.render = function () {
    var i, pts, e;
    switch (this.stage) {
      case 0:
        var last = particles[this.verts[this.verts.length - 1]];
        if (last && last.neighbors && last.neighbors.length > 0) {
          var next = last.neighbors[random(0, last.neighbors.length - 1)];
          if (this.verts.indexOf(next) === -1) this.verts.push(next);
        } else {
          this.stage = 3;
          this.finished = true;
        }
        if (this.verts.length >= this.length) {
          for (i = 0; i < this.verts.length - 1; i++) {
            var a = particles[this.verts[i]], b = particles[this.verts[i + 1]];
            var dx = a.x - b.x, dy = a.y - b.y;
            this.distances.push(Math.sqrt(dx * dx + dy * dy));
          }
          this.stage = 1;
        }
        break;
      case 1:
        if (this.distances.length > 0) {
          pts = [];
          for (i = 0; i < this.linked.length; i++) {
            e = particles[this.linked[i]];
            var pp = position(e.x, e.y, e.z);
            pts.push([pp.x, pp.y]);
          }
          var step = 0.00001 * linkSpeed * canvas.width;
          this.traveled += step;
          var segLen = this.distances[this.linked.length - 1];
          if (this.traveled >= segLen) {
            this.traveled = 0;
            this.linked.push(this.verts[this.linked.length]);
            e = particles[this.linked[this.linked.length - 1]];
            var np = position(e.x, e.y, e.z);
            pts.push([np.x, np.y]);
            if (this.linked.length >= this.verts.length) this.stage = 2;
          } else {
            var from = particles[this.linked[this.linked.length - 1]];
            var to = particles[this.verts[this.linked.length]];
            var remaining = segLen - this.traveled;
            var ix = (this.traveled * to.x + remaining * from.x) / segLen;
            var iy = (this.traveled * to.y + remaining * from.y) / segLen;
            var iz = (this.traveled * to.z + remaining * from.z) / segLen;
            var ip = position(ix, iy, iz);
            pts.push([ip.x, ip.y]);
          }
          this.drawLine(pts);
        } else {
          this.stage = 3;
          this.finished = true;
        }
        break;
      case 2:
        if (this.verts.length > 1) {
          if (this.fade < linkFade) {
            this.fade++;
            pts = [];
            var opacity = (1 - this.fade / linkFade) * linkOpacity;
            for (i = 0; i < this.verts.length; i++) {
              e = particles[this.verts[i]];
              var vp = position(e.x, e.y, e.z);
              pts.push([vp.x, vp.y]);
            }
            this.drawLine(pts, opacity);
          } else {
            this.stage = 3;
            this.finished = true;
          }
        } else {
          this.stage = 3;
          this.finished = true;
        }
        break;
      default:
        this.finished = true;
    }
  };

  function startLink(vertex, length) { links.push(new Link(vertex, length)); }

  function resize() {
    var dpr = window.devicePixelRatio || 1;
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
  }

  function render() {
    if (randomMotion) {
      n++;
      if (n >= noiseLength) n = 0;
      nPos = noisePoint(n);
    }

    context.clearRect(0, 0, canvas.width, canvas.height);

    if (renderParticles) {
      for (var i = 0; i < particleCount; i++) particles[i].render();
    }

    if (renderMesh) {
      context.beginPath();
      for (var v = 0; v < vertices.length - 1; v++) {
        if ((v + 1) % 3 !== 0) {
          var pa = particles[vertices[v]], pb = particles[vertices[v + 1]];
          var oa = position(pa.x, pa.y, pa.z), ob = position(pb.x, pb.y, pb.z);
          context.moveTo(oa.x, oa.y);
          context.lineTo(ob.x, ob.y);
        }
      }
      context.strokeStyle = randomColor(colorPalette);
      context.lineWidth = lineWidth;
      context.stroke();
      context.closePath();
    }

    if (renderLinks) {
      if (random(0, linkChance) === linkChance) {
        var len = random(linkLengthMin, linkLengthMax);
        startLink(random(0, particles.length - 1), len);
      }
      for (var l = links.length - 1; l >= 0; l--) {
        if (links[l] && !links[l].finished) links[l].render();
        else delete links[l];
      }
    }

    if (renderFlares) {
      for (var f = 0; f < flareCount; f++) flares[f].render();
    }
  }

  function init() {
    window.requestAnimFrame = window.requestAnimationFrame
      || window.webkitRequestAnimationFrame
      || window.mozRequestAnimationFrame
      || function (cb) { window.setTimeout(cb, 1000 / 60); };

    resize();
    mouse.x = canvas.clientWidth / 2;
    mouse.y = canvas.clientHeight / 2;

    for (var i = 0; i < particleCount; i++) {
      var p = new Particle();
      particles.push(p);
      points.push([p.x * c, p.y * c]);
    }

    vertices = delaunay(points);
    var triangles = [];
    var tri = [];
    for (i = 0; i < vertices.length; i++) {
      if (tri.length === 3) { triangles.push(tri); tri = []; }
      tri.push(vertices[i]);
    }

    for (i = 0; i < particles.length; i++) {
      for (var t = 0; t < triangles.length; t++) {
        if (triangles[t].indexOf(i) !== -1) {
          triangles[t].forEach(function (vertex) {
            if (vertex !== i && particles[i].neighbors.indexOf(vertex) === -1) {
              particles[i].neighbors.push(vertex);
            }
          });
        }
      }
    }

    if (renderFlares) {
      for (i = 0; i < flareCount; i++) flares.push(new Flare());
    }

    if ('ontouchstart' in document.documentElement && window.DeviceOrientationEvent) {
      window.addEventListener('deviceorientation', function (e) {
        mouse.x = canvas.clientWidth / 2 - e.gamma / 90 * (canvas.clientWidth / 2) * 2;
        mouse.y = canvas.clientHeight / 2 - e.beta / 90 * (canvas.clientHeight / 2) * 2;
      }, true);
    } else {
      document.body.addEventListener('mousemove', function (e) {
        mouse.x = e.clientX;
        mouse.y = e.clientY;
      });
    }

    (function loop() {
      requestAnimFrame(loop);
      resize();
      render();
    }());
  }

  if (canvas) init();
}());
