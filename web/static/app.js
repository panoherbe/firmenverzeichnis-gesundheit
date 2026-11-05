// Small helpers, no framework: guided tour, chart export (JPG/PNG via canvas), table CSV.
(function () {
  var steps = [], i = 0, box;
  function show() {
    document.querySelectorAll('.tour-on').forEach(function (e) { e.classList.remove('tour-on'); });
    if (i >= steps.length) { box.style.display = 'none'; try { localStorage.setItem('tourDone', '1'); } catch (e) {} return; }
    var el = steps[i], r = el.getBoundingClientRect();
    el.classList.add('tour-on');
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    var de = document.documentElement.lang !== 'en';
    box.innerHTML = '<div>' + el.getAttribute('data-tour') + '</div><button>' +
      (i < steps.length - 1 ? (de ? 'Weiter' : 'Next') : (de ? 'Fertig' : 'Done')) + ' (' + (i + 1) + '/' + steps.length + ')</button>' +
      ' <button class="ghost" style="color:inherit">' + (de ? 'Beenden' : 'Close') + '</button>';
    box.style.display = 'block';
    setTimeout(function () {
      var r2 = el.getBoundingClientRect();
      box.style.top = Math.min(window.innerHeight - box.offsetHeight - 10, Math.max(10, r2.bottom + 10)) + 'px';
      box.style.left = Math.max(10, Math.min(window.innerWidth - box.offsetWidth - 10, r2.left)) + 'px';
    }, 300);
    var b = box.querySelectorAll('button');
    b[0].onclick = function () { i++; show(); };
    b[1].onclick = function () { i = steps.length; show(); };
    b[0].focus();
  }
  window.startTour = function () {
    box = document.getElementById('tour');
    steps = Array.prototype.filter.call(document.querySelectorAll('[data-tour]'), function (e) { return e.offsetParent; });
    i = 0; if (steps.length) show();
  };
  window.exportChart = function (id, type) {
    var svg = document.getElementById(id), xml = new XMLSerializer().serializeToString(svg);
    var vb = svg.viewBox.baseVal, scale = 2, img = new Image();
    img.onload = function () {
      // branded image: coloured band with product name and chart title, the chart, a footer line with the date
      var top = 70 * scale, foot = 34 * scale, d = svg.dataset, accent = d.accent || '#0b5cad';
      var c = document.createElement('canvas'); c.width = vb.width * scale; c.height = vb.height * scale + top + foot;
      var ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
      ctx.fillStyle = accent; ctx.fillRect(0, 0, c.width, 30 * scale);
      ctx.fillStyle = '#fff'; ctx.font = 'bold ' + 13 * scale + 'px Arial, Helvetica, sans-serif';
      ctx.fillText(d.brand || '', 14 * scale, 20 * scale);
      ctx.fillStyle = '#1a1a1a'; ctx.font = 'bold ' + 14 * scale + 'px Arial, Helvetica, sans-serif';
      ctx.fillText(d.title || '', 14 * scale, 56 * scale);
      ctx.drawImage(img, 0, top, vb.width * scale, vb.height * scale);
      ctx.fillStyle = '#6b7280'; ctx.font = 10 * scale + 'px Arial, Helvetica, sans-serif';
      ctx.fillText((d.footer ? d.footer + ' · ' : '') + new Date().toLocaleDateString('de-DE'), 14 * scale, c.height - 12 * scale);
      var a = document.createElement('a'); a.download = id + (type === 'image/jpeg' ? '.jpg' : '.png');
      a.href = c.toDataURL(type, 0.92); a.click();
    };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
  };
  window.tableCsv = function (key) {
    var rows = JSON.parse(document.getElementById('data-' + key).textContent);
    var csv = '﻿' + 'label;count\n' + rows.map(function (r) { return '"' + String(r.label).replace(/"/g, '""') + '";' + r.n; }).join('\n');
    var a = document.createElement('a'); a.download = key + '.csv';
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.click();
  };
  // event handlers live here, not inline: the server's CSP is script-src 'self'
  document.addEventListener('change', function (e) {
    if (e.target.hasAttribute('data-autosubmit') && e.target.form) { e.target.form.submit(); }
  });
  document.addEventListener('click', function (e) {
    var back = e.target.closest('a[data-back]');
    if (back && history.length > 1 && document.referrer.indexOf(location.host) > -1) { e.preventDefault(); history.back(); return; }
    var t = e.target.closest('[data-tour-start],[data-export-chart],[data-table-csv],[data-copy],[data-copy-url],[data-select]');
    if (!t) { return; }
    if (t.hasAttribute('data-select')) { t.select(); return; }
    e.preventDefault();
    if (t.hasAttribute('data-tour-start')) { window.startTour(); }
    else if (t.hasAttribute('data-export-chart')) { window.exportChart(t.getAttribute('data-export-chart'), t.getAttribute('data-type')); }
    else if (t.hasAttribute('data-table-csv')) { window.tableCsv(t.getAttribute('data-table-csv')); }
    else if (t.hasAttribute('data-copy-url')) { copy(location.href.split('?')[0]); t.textContent = '\u2713'; }
    else {
      var el = document.getElementById(t.getAttribute('data-copy'));
      if (el) { if (el.select) { el.select(); } copy(el.value !== undefined ? el.value : el.innerText); }
    }
  });
  function copy(text) { try { navigator.clipboard.writeText(text); } catch (e) {} }
  document.addEventListener('DOMContentLoaded', function () {
    if (location.hash === '#tour') { window.startTour(); }
    var tz = document.querySelector('select[data-autotz]');
    if (tz) {
      try {
        var z = Intl.DateTimeFormat().resolvedOptions().timeZone;
        for (var i = 0; i < tz.options.length; i++) { if (tz.options[i].value === z) { tz.selectedIndex = i; } }
      } catch (e) {}
    }
  });
})();
