// login.js — the device-approval request on the sign-in page.
//
// Served at /login.js by routes/static.py:login_script, the one script exempt
// from the auth gate: the browser asking to be let in has no cookie yet, and
// everything under /js/ is behind the gate. A file rather than an inline
// <script> because every page is served with `script-src 'self'`. It holds no
// secret: the claim it polls with comes back from POST /access/request.
(function () {
  var btn = document.getElementById('req-btn');
  var out = document.getElementById('req-state');
  var timer = null, poller = null, remaining = 0;

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    if (poller) { clearInterval(poller); poller = null; }
  }
  function clock(s) {
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function idle(msg) {
    stop();
    out.className = 'req-state';
    out.textContent = msg || '';
    btn.disabled = false;
    btn.textContent = 'Request device approval';
  }
  function waiting(code) {
    // The request has landed; leaving the button on "Requesting…" would read
    // as a stuck spinner for the whole five minutes.
    btn.textContent = 'Request sent';
    out.className = 'req-state waiting';
    out.textContent = '';
    var big = document.createElement('div');
    big.className = 'req-code';
    big.textContent = code;
    var msg = document.createElement('div');
    msg.textContent = 'Waiting for approval\u2026 ';
    var cl = document.createElement('span');
    cl.id = 'req-clock';
    cl.textContent = clock(remaining);
    msg.appendChild(cl);
    out.appendChild(big);
    out.appendChild(msg);
    timer = setInterval(function () {
      remaining = Math.max(0, remaining - 1);
      var el = document.getElementById('req-clock');
      if (el) el.textContent = clock(remaining);
    }, 1000);
  }
  function poll(claim) {
    poller = setInterval(function () {
      fetch('/access/request/status?claim=' + encodeURIComponent(claim))
        .then(function (r) { return r.json(); })
        .then(function (st) {
          if (st.status === 'approved') {
            stop();
            out.className = 'req-state';
            out.textContent = 'Approved \u2014 loading\u2026';
            location.href = '/';
          } else if (st.status === 'denied') {
            idle('Request denied.');
          } else if (st.status === 'expired') {
            idle('No one answered. Paste the token instead.');
          } else if (st.status === 'device_cap') {
            idle('This Assist has the maximum number of devices. Revoke one, then ask again.');
          } else if (st.status === 'unavailable') {
            idle('Sign-in is unavailable right now. Paste the token, or try again.');
          } else {
            remaining = st.remaining_sec;
          }
        })
        .catch(function () { /* transient — the next tick re-syncs */ });
    }, 2000);
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    btn.textContent = 'Requesting\u2026';
    fetch('/access/request', { method: 'POST' })
      .then(function (r) {
        return r.json().then(function (d) { return { code: r.status, body: d }; });
      })
      .then(function (res) {
        if (res.code === 429) { idle('Too many requests. Try again in a moment.'); return; }
        if (!res.body || !res.body.ok) { idle('Could not request approval.'); return; }
        remaining = res.body.request.expires_in;
        waiting(res.body.request.code);
        poll(res.body.claim);
      })
      .catch(function () { idle('Could not reach the server.'); });
  });
})();
