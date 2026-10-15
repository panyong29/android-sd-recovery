#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深度文件雕刻: 按文件结构解析真实边界, 逐个解码/校验后再落盘, 避免"假文件".

支持: JPEG(标记流严格解析 + PIL 全量解码) / PNG(块链 + CRC 校验) / GIF / WEBP / BMP /
      MP4·3GP(box 链) / SQLite(页头校验 + 可打开验证) / ZIP·APK(local header 链或中央目录)

用法:
    python carve.py <镜像> --out <输出目录> [--start MiB] [--end MiB] [--min-photo KB]
产出: <输出目录>/
        01_photos/ 02_jpeg/ 03_png/ ... 分类文件
        carve_records.json   每条: cat/path/offset/size/w/h/exif/incomplete/md5
        恢复清单.csv / 恢复报告.html
"""
import argparse, base64, collections, csv, hashlib, io, json, os, sqlite3, struct, time, zlib
from PIL import Image

DIRS = {"photo": "01_photos", "jpeg": "02_jpeg", "png": "03_png", "gif": "04_gif",
        "webp": "05_webp", "bmp": "06_bmp", "video": "07_video", "db": "08_databases",
        "zip": "09_archives"}
EXT = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp", "bmp": ".bmp",
       "sqlite": ".db", "zip": ".zip"}


class Reader:
    """按需渐进读取, 避免为小文件读几十 MB"""
    def __init__(self, f, off, cap, fsize):
        self.f, self.off, self.cap, self.fsize = f, off, min(cap, fsize - off), fsize
        self.data = bytearray()
        self._ensure(65536)

    def _ensure(self, n):
        if len(self.data) >= n or len(self.data) >= self.cap:
            return
        want = min(max(n, len(self.data) * 4), self.cap)
        self.f.seek(self.off + len(self.data))
        self.data += self.f.read(want - len(self.data))

    def need(self, i):
        self._ensure(i + 1)
        return i < len(self.data)


def jpeg_end(r):
    if bytes(r.data[:3]) != b"\xff\xd8\xff":
        return None
    i = 2
    while True:
        if not r.need(i + 1) or r.data[i] != 0xFF:
            return None
        m = r.data[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xD9:
            return i + 2
        if not r.need(i + 3):
            return None
        seg = (r.data[i + 2] << 8) | r.data[i + 3]
        if seg < 2:
            return None
        if m == 0xDA:                       # SOS 之后是熵编码数据, 逐字节扫到 FFD9
            i += 2 + seg
            while True:
                if not r.need(i + 1):
                    return None
                if r.data[i] == 0xFF:
                    m2 = r.data[i + 1]
                    if m2 == 0x00 or 0xD0 <= m2 <= 0xD7:   # FF00 填充 / 重启标记
                        i += 2
                        continue
                    if m2 == 0xD9:
                        return i + 2
                    return None
                i += 1
        i += 2 + seg


def png_end(r):
    if bytes(r.data[:8]) != b"\x89PNG\r\n\x1a\n":
        return None
    i = 8
    while True:
        if not r.need(i + 11):
            return None
        ln = struct.unpack(">I", r.data[i:i + 4])[0]
        typ = bytes(r.data[i + 4:i + 8])
        if ln > 64 * 1024 * 1024 or not r.need(i + 12 + ln - 1):
            return None
        crc = struct.unpack(">I", r.data[i + 8 + ln:i + 12 + ln])[0]
        if zlib.crc32(r.data[i + 4:i + 8 + ln]) & 0xffffffff != crc:
            return None
        i += 12 + ln
        if typ == b"IEND":
            return i


def gif_end(r):
    if bytes(r.data[:6]) not in (b"GIF87a", b"GIF89a"):
        return None
    i = 6
    if not r.need(i + 6):
        return None
    packed = r.data[i + 4]
    i += 7
    if packed & 0x80:
        i += 3 * (2 << (packed & 7))
    while True:
        if not r.need(i):
            return None
        b = r.data[i]
        if b == 0x3B:
            return i + 1
        if b == 0x21:
            i += 2
            while True:
                if not r.need(i):
                    return None
                sz = r.data[i]
                i += 1
                if sz == 0:
                    break
                i += sz
        elif b == 0x2C:
            if not r.need(i + 9):
                return None
            pk = r.data[i + 9]
            i += 10
            if pk & 0x80:
                i += 3 * (2 << (pk & 7))
            i += 1
            while True:
                if not r.need(i):
                    return None
                sz = r.data[i]
                i += 1
                if sz == 0:
                    break
                i += sz
        else:
            return None


def webp_end(r):
    if bytes(r.data[:4]) != b"RIFF" or bytes(r.data[8:12]) != b"WEBP":
        return None
    ln = struct.unpack("<I", r.data[4:8])[0] + 8
    return ln if 20 <= ln <= 100 * 1024 * 1024 else None


def bmp_end(r):
    if bytes(r.data[:2]) != b"BM":
        return None
    ln = struct.unpack("<I", r.data[2:6])[0]
    return ln if 100 <= ln <= 200 * 1024 * 1024 else None


def mp4_end(f, off, fsize):
    f.seek(off)
    hdr = f.read(16)
    if len(hdr) < 12 or hdr[4:8] != b"ftyp":
        return None
    if not hdr[8:12].startswith((b"isom", b"mp4", b"3gp", b"M4V", b"M4A", b"avc1", b"qt",
                                 b"iso", b"MSNV")):
        return None
    pos, total, moov, mdat = off, 0, False, False
    CAP = 2 * 1024**3
    while total < CAP:
        f.seek(pos)
        b = f.read(16)
        if len(b) < 8:
            break
        size = struct.unpack(">I", b[:4])[0]
        typ, hl = b[4:8], 8
        if size == 1:
            size = struct.unpack(">Q", b[8:16])[0]
            hl = 16
        elif size == 0:
            size = min(fsize - pos, CAP - total)
        if size < hl:
            return None
        moov = moov or typ == b"moov"
        mdat = mdat or typ == b"mdat"
        if not all(32 <= c < 127 for c in typ):
            return None
        pos += size
        total += size
    return total if (mdat and moov) else (-total if mdat else None)   # 负值=缺 moov


def sqlite_end(r):
    if bytes(r.data[:16]) != b"SQLite format 3\x00" or not r.need(99):
        return None
    ps = struct.unpack(">H", r.data[16:18])[0]
    ps = 65536 if ps == 1 else ps
    if ps < 512 or ps > 65536 or (ps & (ps - 1)) != 0:
        return None
    pages = struct.unpack(">I", r.data[28:32])[0]
    ln = ps * pages if pages else ps
    return ln if ln <= 1024**3 else None


def zip_end(r):
    if bytes(r.data[:4]) != b"PK\x03\x04":
        return None
    i = 0
    while True:
        if not r.need(i + 29):
            return None
        sig = bytes(r.data[i:i + 4])
        if sig == b"PK\x03\x04":
            flags = struct.unpack("<H", r.data[i + 6:i + 8])[0]
            csz = struct.unpack("<I", r.data[i + 18:i + 22])[0]
            nl = struct.unpack("<H", r.data[i + 26:i + 28])[0]
            el = struct.unpack("<H", r.data[i + 28:i + 30])[0]
            if nl > 4096 or flags & 0x08:      # 有 data descriptor 时无法可靠定位
                return None
            i += 30 + nl + el + csz
        elif sig == b"PK\x01\x02":
            while True:
                if not r.need(i + 21):
                    return None
                if bytes(r.data[i:i + 4]) == b"PK\x05\x06":
                    cl = struct.unpack("<H", r.data[i + 20:i + 22])[0]
                    return i + 22 + cl
                if bytes(r.data[i:i + 4]) != b"PK\x01\x02":
                    return None
                if not r.need(i + 45):
                    return None
                nl, el, cl2 = (struct.unpack("<H", r.data[i + 28 + k * 2:i + 30 + k * 2])[0]
                               for k in range(3))
                i += 46 + nl + el + cl2
        else:
            return None


SIGS = [
    (b"\xff\xd8\xff", "jpeg", lambda b, i: b[i + 3:i + 4] in (b"\xe0", b"\xe1")),
    (b"\x89PNG\r\n\x1a\n", "png", None),
    (b"GIF8", "gif", lambda b, i: b[i:i + 6] in (b"GIF87a", b"GIF89a")),
    (b"RIFF", "webp", lambda b, i: b[i + 8:i + 12] == b"WEBP"),
    (b"BM", "bmp", None),
    (b"ftyp", "mp4", None),
    (b"SQLite format 3\x00", "sqlite", None),
    (b"PK\x03\x04", "zip", None),
]
CAP_MIB = {"jpeg": 30, "png": 120, "gif": 60, "webp": 120, "bmp": 220,
           "sqlite": 1100, "zip": 1100}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("img")
    ap.add_argument("--out", default="carved")
    ap.add_argument("--start", type=int, default=0, help="起始偏移 MiB")
    ap.add_argument("--end", type=int, default=0, help="结束偏移 MiB(0=到文件尾)")
    ap.add_argument("--min-photo", type=int, default=300, help="相机原图最小 KB")
    a = ap.parse_args()

    for d in DIRS.values():
        os.makedirs(os.path.join(a.out, d), exist_ok=True)
    fsize = os.path.getsize(a.img)
    start = a.start * 1024 * 1024
    end = (a.end * 1024 * 1024) if a.end else fsize
    f = open(a.img, "rb")

    print("扫描签名...", flush=True)
    hits = []
    CH = 64 * 1024 * 1024
    for sig, typ, extra in SIGS:
        pos, prev = start, b""
        while pos < end:
            f.seek(pos)
            buf = prev + f.read(min(CH, end - pos))
            base, i = pos - len(prev), 0
            while True:
                i = buf.find(sig, i)
                if i < 0:
                    break
                if extra is None or extra(buf, i):
                    off = base + i - (4 if typ == "mp4" else 0)
                    if off >= start:
                        hits.append((off, typ))
                i += 1
            prev = buf[-64:]
            pos += CH
        print(f"  {typ:7s}: {sum(1 for _, t in hits if t == typ)}", flush=True)

    hits.sort()
    print(f"签名总数 {len(hits)}, 开始结构校验与雕刻...", flush=True)

    records, seen, t0 = [], set(), time.time()
    for idx, (off, typ) in enumerate(hits):
        if any(off >= rr["offset"] and off < rr["offset"] + rr["size"] for rr in records[-3:]):
            continue                                   # 跳过已雕刻区间内的嵌套签名
        try:
            if typ == "mp4":
                ln = mp4_end(f, off, fsize)
                if ln is None or abs(ln) < 64 * 1024:
                    continue
                incomplete = ln < 0
                ln = abs(ln)
                f.seek(off)
                raw = f.read(ln)
                w = h = 0
                exif, cat, ext = {}, "video", ".mp4"
            else:
                r = Reader(f, off, CAP_MIB[typ] * 1024 * 1024, fsize)
                fn = {"jpeg": jpeg_end, "png": png_end, "gif": gif_end, "webp": webp_end,
                      "bmp": bmp_end, "sqlite": sqlite_end, "zip": zip_end}[typ]
                ln = fn(r)
                if not ln or ln < (100 if typ == "sqlite" else 400):
                    continue
                raw = bytes(r.data[:ln]) if len(r.data) >= ln else None
                if raw is None:
                    f.seek(off)
                    raw = f.read(ln)
                w = h = 0
                exif, incomplete, cat = {}, False, typ
                ext = EXT[typ]
                if typ in ("jpeg", "png", "gif", "webp", "bmp"):
                    im = Image.open(io.BytesIO(raw))
                    im.load()
                    w, h = im.size
                    if w < 64 or h < 64:
                        continue
                    if typ == "jpeg":
                        e = im.getexif()
                        exif = {"make": str(e.get(0x010F) or ""), "model": str(e.get(0x0110) or ""),
                                "date": str(e.get(0x9003) or e.get(0x0132) or "")}
                elif typ == "sqlite":
                    tmp = os.path.join(a.out, ".tmp.db")
                    open(tmp, "wb").write(raw)
                    try:
                        con = sqlite3.connect("file:" + tmp + "?mode=ro&immutable=1", uri=True)
                        nobj = len(con.execute("select name from sqlite_master").fetchall())
                        con.close()
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    if nobj == 0:
                        continue
            if typ == "jpeg":
                cat = "photo" if ((w >= 1200 and h >= 900) or exif.get("make")) else "jpeg"
                if cat == "photo" and len(raw) < a.min_photo * 1024:
                    cat = "jpeg"
            if typ == "zip" and b"AndroidManifest.xml" in raw[:4096] + raw[-4096:]:
                ext = ".apk"

            md5 = hashlib.md5(raw).hexdigest()
            if md5 in seen:
                continue
            seen.add(md5)
            seq = sum(1 for x in records if x["cat"] == cat) + 1
            name = (f"{DIRS[cat][:2]}_{seq:04d}_{w}x{h}{ext}" if w else
                    f"{DIRS[cat][:2]}_{seq:04d}{ext}")
            path = os.path.join(a.out, DIRS[cat], name)
            open(path, "wb").write(raw)
            records.append(dict(cat=cat, path=path, offset=off, size=len(raw), w=w, h=h,
                                exif=exif, incomplete=incomplete, md5=md5))
        except Exception:
            continue
        if idx % 300 == 0:
            print(f"  [{time.time()-t0:5.0f}s] {idx}/{len(hits)} 签名, 已恢复 {len(records)}",
                  flush=True)

    f.close()
    print(f"\n雕刻完成: {len(records)} 个文件, 耗时 {time.time()-t0:.0f}s")
    json.dump(records, open(os.path.join(a.out, "carve_records.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    with open(os.path.join(a.out, "恢复清单.csv"), "w", newline="", encoding="utf-8-sig") as fp:
        wr = csv.writer(fp)
        wr.writerow(["类别", "文件名", "大小MB", "宽", "高", "拍摄时间", "设备", "镜像偏移", "完整性"])
        for r in records:
            wr.writerow([DIRS[r["cat"]], os.path.basename(r["path"]), round(r["size"] / 1024**2, 2),
                         r["w"], r["h"], r["exif"].get("date", ""),
                         (r["exif"].get("make", "") + " " + r["exif"].get("model", "")).strip(),
                         r["offset"], "可能不完整" if r["incomplete"] else "完整"])

    stats = collections.Counter(r["cat"] for r in records)
    thumbs = []
    for r in records:
        if r["cat"] == "photo" and len(thumbs) < 300:
            try:
                im = Image.open(r["path"])
                im.thumbnail((220, 220))
                b = io.BytesIO()
                im.convert("RGB").save(b, "JPEG", quality=68)
                thumbs.append((r, base64.b64encode(b.getvalue()).decode()))
            except Exception:
                pass
    cards = "".join(
        f"<div class=c><img src='data:image/jpeg;base64,{b}' loading=lazy><div class=i>"
        f"{os.path.basename(r['path'])}<br>{r['w']}×{r['h']} · {r['size']/1024**2:.1f}MB<br>"
        f"{r['exif'].get('date','')}</div></div>" for r, b in thumbs)
    open(os.path.join(a.out, "恢复报告.html"), "w", encoding="utf-8").write(
        "<!doctype html><meta charset=utf-8><title>雕刻恢复报告</title><style>"
        "body{font-family:sans-serif;background:#f4f6f9;margin:0;padding:24px}"
        ".gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}"
        ".c{background:#fff;border-radius:8px;overflow:hidden}.c img{width:100%;height:170px;"
        "object-fit:cover;display:block}.i{font-size:11px;color:#555;padding:6px}</style>"
        f"<h1>雕刻恢复报告</h1><p>共 {len(records)} 个文件 · "
        f"{sum(r['size'] for r in records)/1024**3:.2f} GiB</p><div class=gal>{cards}</div>")
    for k in sorted(stats):
        print(f"  {DIRS[k]}: {stats[k]} 个")
    print("输出目录:", a.out)


if __name__ == "__main__":
    main()
