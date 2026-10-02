/* kancil agent.js — injected into viewer pages.
 * Runs in the REAL rendered DOM (JS executed). Registers this tab with
 * Kancil, long-polls for commands, executes them, posts results back.
 * No dependency. Same-origin with the viewer, so no CORS issues.
 */
(function () {
  'use strict';
  // absolute gateway base derived from our own <script src> — the page
  // carries <base href="origin"> so relative URLs would resolve wrongly.
  var _cs = document.currentScript;
  var _src = (_cs && _cs.src) || '';
  var BASE = _src.replace(/\/agent\.js.*$/, '/agent') || '/__kancil__/agent';
  // the real page URL (we are served through the viewer gateway)
  var REAL_URL = (_cs && _cs.getAttribute('data-kancil-url')) || location.href;
  var TAB = 'tab-' + Math.random().toString(36).slice(2) +
            Date.now().toString(36);
  var seq = 0;
  var polling = false;

  function post(path, obj) {
    return fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(obj)
    }).catch(function () {});
  }

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + el.id;
    var parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      var name = el.tagName.toLowerCase();
      if (el.id) { parts.unshift('#' + el.id); break; }
      var sib = el, n = 1;
      while ((sib = sib.previousElementSibling)) {
        if (sib.tagName === el.tagName) n++;
      }
      parts.unshift(name + ':nth-of-type(' + n + ')');
      el = el.parentElement;
    }
    return parts.join(' > ');
  }

  function snapshot() {
    var els = [];
    var nodes = document.querySelectorAll(
      'a, button, input, select, textarea, [role="button"], [onclick]');
    for (var i = 0; i < nodes.length && i < 300; i++) {
      var el = nodes[i];
      var r = el.getBoundingClientRect();
      var text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                  el.getAttribute('placeholder') || '').replace(/\s+/g, ' ')
                  .trim().slice(0, 80);
      els.push({ tag: el.tagName.toLowerCase(), type: el.type || '',
                 text: text, sel: cssPath(el),
                 x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height) });
    }
    return { url: REAL_URL, title: document.title,
             count: els.length, elements: els };
  }

  function execCmd(c) {
    var out = {};
    try {
      var q = (c.args && c.args.selector) || '';
      if (c.action === 'click') {
        var el = document.querySelector(q);
        if (!el) throw new Error('no match: ' + q);
        el.scrollIntoView({ block: 'center' });
        el.click();
        out = { clicked: true, selector: q };
      } else if (c.action === 'type') {
        var t = document.querySelector(q);
        if (!t) throw new Error('no match: ' + q);
        t.focus();
        t.value = c.args.text || '';
        t.dispatchEvent(new Event('input', { bubbles: true }));
        t.dispatchEvent(new Event('change', { bubbles: true }));
        out = { typed: true, selector: q };
      } else if (c.action === 'scroll') {
        window.scrollTo(c.args.x || 0, c.args.y || 0);
        out = { scrolled: true, x: c.args.x || 0, y: c.args.y || 0 };
      } else if (c.action === 'eval') {
        /* jshint evil:true */
        var v = eval(c.args.js || '');
        out = { result: String(v).slice(0, 4000) };
      } else if (c.action === 'snapshot') {
        out = snapshot();
      } else if (c.action === 'text') {
        out = { text: (document.body ? document.body.innerText : '')
                .slice(0, 8000) };
      } else {
        out = { error: 'unknown action: ' + c.action };
      }
    } catch (e) {
      out = { error: String((e && e.message) || e).slice(0, 300) };
    }
    return post('/result', { tab: TAB, id: c.id, out: out }).then(function () {
      seq = Math.max(seq, c.seq);
    });
  }

  function poll() {
    if (polling) return;
    polling = true;
    fetch(BASE + '/poll?tab=' + encodeURIComponent(TAB) + '&seq=' + seq)
      .then(function (r) { return r.json(); })
      .then(function (cmds) {
        polling = false;
        var p = Promise.resolve();
        (cmds || []).forEach(function (c) {
          p = p.then(function () { return execCmd(c); });
        });
        return p.then(function () { setTimeout(poll, 300); });
      })
      .catch(function () { polling = false; setTimeout(poll, 2000); });
  }

  // register, then start polling (also re-register on visibility change)
  function register() {
    post('/register', { tab: TAB, url: REAL_URL,
                        title: document.title,
                        ua: navigator.userAgent });
  }
  register();
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) register();
  });
  if (document.readyState === 'complete') poll();
  else window.addEventListener('load', poll);
  setTimeout(poll, 4000); // fallback if load already fired oddly
})();
