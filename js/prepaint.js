// prepaint.js — loaded synchronously at the top of <body>, before first paint.
//
// Applying these from js/chrome.js instead would flash the full chrome for
// ~100ms on every load, which reads as a fault. Both preferences change the
// header's height and row count, so both have to be on <body> before the first
// layout. A file rather than an inline <script> because the page is served
// with `script-src 'self'`.
try {
    if (localStorage.getItem('assist.chromeCollapsed'))
        document.body.classList.add('chrome-collapsed');
    if (localStorage.getItem('assist.tabsWrap'))
        document.body.classList.add('tabs-wrap');
} catch (e) {}
