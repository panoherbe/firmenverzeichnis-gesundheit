"""
Minimal PDF writer (no dependency): A4 pages, Helvetica in WinAnsi encoding (covers German and most Western
European text), wrapped text, simple tables and horizontal bar charts. Enough for result lists, profiles and the
statistics export; characters outside cp1252 become "?".

    doc = Doc("Title"); doc.text("…"); doc.table(["A", "B"], rows, [0.5, 0.5]); doc.bars("Chart", [("x", 3)])
    pdf_bytes = doc.render()
"""

from __future__ import annotations

import datetime as dt

W, H, M = 595.28, 841.89, 42  # A4 in points, margin
# Helvetica widths (1/1000 em) for cp1252 32..255; unknown → 556
_WIDTHS = {c: w for c, w in zip(range(32, 127), [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
    556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
    722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556,
    556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334,
    260, 334, 584])}
ACCENT = (0.04, 0.36, 0.68)


def enc(s: str) -> bytes:
    return str(s).encode("cp1252", "replace")


def width(s: str, size: float) -> float:
    return sum(_WIDTHS.get(b, 556) for b in enc(s)) * size / 1000


def esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def wrap(s: str, size: float, maxw: float) -> list[str]:
    lines, cur = [], ""
    for word in str(s).split():
        nxt = f"{cur} {word}".strip()
        if width(nxt, size) <= maxw or not cur:
            cur = nxt
        else:
            lines.append(cur)
            cur = word
    while cur and width(cur, size) > maxw and len(cur) > 3:  # hard cut a single overlong word
        cut = len(cur)
        while width(cur[:cut] + "…", size) > maxw and cut > 1:
            cut -= 1
        lines.append(cur[:cut])
        cur = cur[cut:]
    return lines + ([cur] if cur else []) or [""]


class Doc:
    def __init__(self, title: str):
        self.title = title
        self.pages: list[list[bytes]] = []
        self.y = 0.0
        self.new_page()
        self.text(title, 16, bold=True)
        self.y -= 4

    def new_page(self) -> None:
        self.pages.append([])
        self.y = H - M

    def need(self, h: float) -> None:
        if self.y - h < M + 20:
            self.new_page()

    def put(self, x: float, y: float, s: str, size: float = 10, bold: bool = False, rgb=(0, 0, 0)) -> None:
        self.pages[-1].append(b"%.3f %.3f %.3f rg BT /F%d %.1f Tf %.2f %.2f Td (%s) Tj ET" % (
            *rgb, 2 if bold else 1, size, x, y, esc(enc(s))))

    def rect(self, x: float, y: float, w: float, h: float, rgb=ACCENT) -> None:
        self.pages[-1].append(b"%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f" % (*rgb, x, y, w, h))

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.pages[-1].append(b"0.85 0.87 0.9 RG 0.5 w %.2f %.2f m %.2f %.2f l S" % (x1, y1, x2, y2))

    def text(self, s: str, size: float = 10, bold: bool = False) -> None:
        for ln in wrap(s, size, W - 2 * M):
            self.need(size * 1.4)
            self.y -= size * 1.4
            self.put(M, self.y, ln, size, bold)
        self.y -= size * 0.6

    def table(self, head: list[str], rows: list[tuple], cols: list[float], size: float = 8.5, header: bool = True) -> None:
        xs, x = [], M
        for c in cols:
            xs.append(x)
            x += c * (W - 2 * M)
        widths = [c * (W - 2 * M) - 6 for c in cols]

        def row(cells, bold=False):
            wrapped = [wrap(str(v if v is not None else ""), size, wd) for v, wd in zip(cells, widths)]
            h = max(len(w) for w in wrapped) * size * 1.3 + 4
            self.need(h)
            for i, w in enumerate(wrapped):
                for j, ln in enumerate(w):
                    self.put(xs[i], self.y - size * 1.2 * (j + 1), ln, size, bold)
            self.y -= h
            self.line(M, self.y + 1, W - M, self.y + 1)

        if header:
            row(head, bold=True)
        for r in rows:
            row(r)
        self.y -= 8

    def bars(self, title: str, data: list[tuple[str, int]], size: float = 8.5) -> None:
        if not data:
            return
        self.need(24 + len(data) * 15)
        self.text(title, 11, bold=True)
        label_w, top = 170, max(n for _l, n in data) or 1
        span = W - 2 * M - label_w - 50
        for label, n in data:
            self.y -= 15
            self.put(M, self.y + 3, wrap(label, size, label_w - 8)[0], size)
            self.rect(M + label_w, self.y + 1, max(1, span * n / top), 10)
            self.put(M + label_w + span * n / top + 5, self.y + 3, f"{n:,}".replace(",", "."), size)
        self.y -= 14

    def render(self) -> bytes:
        objs: list[bytes] = []

        def add(b: bytes) -> int:
            objs.append(b)
            return len(objs)

        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        pages_id = len(objs) + 1 + 2 * len(self.pages)
        kids = []
        stamp = dt.date.today().strftime("%d.%m.%Y")
        for i, ops in enumerate(self.pages, 1):
            foot = b"0.4 0.4 0.4 rg BT /F1 7 Tf %.2f 22 Td (%s) Tj ET" % (M, esc(enc(f"{self.title} · {stamp} · {i}/{len(self.pages)}")))
            body = b"\n".join(ops + [foot])
            c = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body))
            kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R "
                            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (pages_id, W, H, c, f1, f2)))
        add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
        info = add(b"<< /Title (%s) /Producer (firmenverzeichnis-gesundheit) >>" % esc(enc(self.title)))
        root = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
        out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, root, info, xref)
        return bytes(out)
