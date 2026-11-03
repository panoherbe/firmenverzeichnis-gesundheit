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
      var c = document.createElement('canvas'); c.width = vb.width * scale; c.height = vb.height * scale;
      var ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height);
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
  document.addEventListener('DOMContentLoaded', function () {
    if (location.hash === '#tour') { window.startTour(); }
  });
})();
