/* Plays the animated Payrolla mark once, then leaves the finished mark standing.
 *
 * The animation is a "stacked alpha" MP4: colour in the top half of each
 * frame, the alpha matte in the bottom half. H.264 has no alpha channel and
 * Safari ignores VP9's, so the two halves are recombined here on a WebGL
 * canvas -- the one route to a transparent video that plays everywhere.
 * scripts/logo/build_logo_assets.py makes the files.
 *
 * Markup (see landing.html):
 *   <div data-mark-anim data-video-lg="....mp4" data-video-sm="....mp4"
 *        data-media-class="hero-mark-media">
 *     <picture class="hero-mark-media"><img class="hero-mark-still" ...></picture>
 *   </div>
 *
 * The still is the final frame in the same box, and it is the answer to every
 * failure: no WebGL, a video that will not load or play, one that stalls, a
 * browser that pauses it part-way (iOS low-power mode does). Each ends with
 * `is-still` on the box and the finished mark showing -- never an empty hero
 * and never a half-built P. Whether to try at all (reduced motion, Save-Data)
 * is decided by the inline script before the hero paints, which puts
 * `mark-motion` on <html>; without it this file does nothing.
 */
(function () {
  'use strict';

  if (!document.documentElement.classList.contains('mark-motion')) return;

  var WIDE = '(min-width: 921px)';  // landing.html's own breakpoint
  var START_MS = 6000;              // a mark that has not started by then never will
  var STALL_MS = 3000;              // nor one stuck mid-build for this long

  var VERT = 'attribute vec2 p; varying vec2 uv;' +
    'void main(){ uv = vec2(p.x * 0.5 + 0.5, 0.5 - p.y * 0.5); gl_Position = vec4(p, 0.0, 1.0); }';
  var FRAG = 'precision mediump float; uniform sampler2D t; varying vec2 uv;' +
    'void main(){' +
    ' vec3 c = texture2D(t, vec2(uv.x, uv.y * 0.5)).rgb;' +
    ' float a = texture2D(t, vec2(uv.x, 0.5 + uv.y * 0.5)).r;' +
    ' gl_FragColor = vec4(c * a, a); }';

  function setup(gl) {
    var prog = gl.createProgram();
    [[gl.VERTEX_SHADER, VERT], [gl.FRAGMENT_SHADER, FRAG]].forEach(function (s) {
      var sh = gl.createShader(s[0]);
      gl.shaderSource(sh, s[1]);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
      gl.attachShader(prog, sh);
    });
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, 'p');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  }

  function play(box) {
    var canvas = document.createElement('canvas');
    var video = document.createElement('video');
    var settled = false;
    var startTimer = setTimeout(finish, START_MS);
    var stallTimer;
    var gl;

    // Hand over to the still (the same final frame) and free the GPU.
    function finish() {
      if (settled) return;
      settled = true;
      clearTimeout(startTimer);
      clearTimeout(stallTimer);
      box.classList.remove('is-playing');
      box.classList.add('is-still');
      canvas.remove();
      video.removeAttribute('src');
      video.load();
      video.remove();
      var lose = gl && gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
    }

    function draw() {
      if (video.readyState < 2) return;   // no frame yet
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, video);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    var byFrame = 'requestVideoFrameCallback' in video;
    function tick() {
      if (settled || video.ended) return;
      try { draw(); } catch (e) { finish(); return; }
      if (byFrame) video.requestVideoFrameCallback(tick);
      else requestAnimationFrame(tick);
    }

    try {
      gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false });
      if (!gl) throw new Error('no webgl');
      setup(gl);
    } catch (e) {
      finish();
      return;
    }

    canvas.className = box.getAttribute('data-media-class') || '';
    canvas.setAttribute('aria-hidden', 'true');
    video.muted = video.defaultMuted = video.playsInline = true;
    video.setAttribute('muted', '');
    video.setAttribute('playsinline', '');
    video.preload = 'auto';
    // In the document but out of sight: iOS will not decode a detached video.
    video.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none';

    video.addEventListener('error', finish);
    // A pause that is not the end is someone else stopping it part-way.
    video.addEventListener('pause', function () { if (!video.ended) finish(); });
    video.addEventListener('waiting', function () {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(finish, STALL_MS);
    });
    video.addEventListener('playing', function () { clearTimeout(stallTimer); });
    video.addEventListener('loadedmetadata', function () {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight / 2;
      gl.viewport(0, 0, canvas.width, canvas.height);
      box.appendChild(canvas);
    }, { once: true });
    video.addEventListener('ended', function () {
      try { draw(); } catch (e) { /* the still covers it */ }
      finish();
    });

    video.src = box.getAttribute(window.matchMedia(WIDE).matches ? 'data-video-lg' : 'data-video-sm');
    box.appendChild(video);
    var started = video.play();
    var go = function () {
      if (settled) return;
      clearTimeout(startTimer);
      box.classList.add('is-playing');
      tick();
    };
    if (started && started.then) started.then(go, finish);
    else go();
  }

  function start() {
    var boxes = document.querySelectorAll('[data-mark-anim]');
    for (var i = 0; i < boxes.length; i++) play(boxes[i]);
  }

  // A tab opened in the background would play to an empty room (and Chrome
  // pauses muted video there, which reads as a failure). Wait to be seen.
  if (document.visibilityState === 'hidden') {
    document.addEventListener('visibilitychange', function onShow() {
      if (document.visibilityState === 'hidden') return;
      document.removeEventListener('visibilitychange', onShow);
      start();
    });
  } else {
    start();
  }
})();
