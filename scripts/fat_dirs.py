#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FAT32/exFAT 目录树重建 —— 锚点法(无需引导扇区, 适用于 FAT 表/根目录已损坏的镜像).

原理: 每个目录簇的首条记录必为 "." 条目(name=".          ", attr=0x10), 该 11 字节模式
      在随机数据中命中概率约 (1/256)^8, 可作为可信锚点; 从锚点按 32 字节步长顺序解析至
      0x00 结束标记, 即可还原该目录全部条目(含长文件名与已删除项)。
      再用 "条目的 cluster == 某目录 '.' 条目的 cluster" 关联父子, 还原真实路径。

用法:
    python fat_dirs.py <镜像> [--out 输出目录]
产出: <输出目录>/fat_dirs.json  { 锚点偏移: {own_cluster, parent_cluster, entries:[...]} }
条目字段: name(长名优先) / shortname / attr / size / cluster / date / time / deleted / kind
"""
import argparse, json, os

MARK = b"\x2e" + b"\x20" * 10 + b"\x10"      # "." 目录项
WIN = 256 * 1024
CHUNK = 64 * 1024 * 1024

_ALLOWED = set(range(0x20, 0x7F)) - set(b'"*+,/:;<=>?[\\]|')


def d2s(d):
    if d == 0:
        return ""
    y, mo, da = 1980 + ((d >> 9) & 0x7F), (d >> 5) & 0xF, d & 0x1F
    return f"{y:04d}-{mo:02d}-{da:02d}" if (1 <= mo <= 12 and 1 <= da <= 31) else ""


def t2s(t):
    return f"{(t>>11)&0x1F:02d}:{(t>>5)&0x3F:02d}:{(t&0x1F)*2:02d}" if t else ""


def lfn_checksum(s11):
    s = 0
    for c in s11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def decode_lfn(recs):
    raw = b"".join(bytes(r[1:11]) + bytes(r[14:26]) + bytes(r[28:32]) for r in recs)
    s = raw.decode("utf-16-le", "ignore")
    out = []
    for ch in s:
        if ch == "\x00":
            break
        if ch != "\uffff":
            out.append(ch)
    return "".join(out).strip()


def valid_sfn11(rec):
    b = bytes(rec[0:11])
    if b[0] in (0xE5, 0x05):        # 0xE5 = 删除标记, 首字符已被覆盖
        return all(c in _ALLOWED for c in b[1:8]) and all(c in _ALLOWED for c in b[8:11])
    return all(c in _ALLOWED for c in b[0:8]) and all(c in _ALLOWED for c in b[8:11])


def valid_date(rec):
    wd = int(rec[24]) | (int(rec[25]) << 8)
    if wd == 0:
        return True
    y, mo, da = 1980 + ((wd >> 9) & 0x7F), (wd >> 5) & 0xF, wd & 0x1F
    return 1980 <= y <= 2035 and 1 <= mo <= 12 and 1 <= da <= 31


def emit(rec, off, pending):
    raw0, at = int(rec[0]), int(rec[11])
    nm = bytes(rec[0:8]).rstrip(b" ").decode("latin-1")
    ex = bytes(rec[8:11]).rstrip(b" ").decode("latin-1")
    deleted = raw0 == 0xE5
    longname = decode_lfn(list(reversed(pending))) if pending else ""   # 长名片段逆序存放
    if longname and not deleted:
        chks = {int(r[13]) for r in pending}
        if not (len(chks) == 1 and next(iter(chks)) == lfn_checksum(bytes(rec[0:11]))):
            longname = ""                                              # 校验和不符则弃用长名
    if deleted and not longname:
        nm = "?" + nm[1:]
    short = nm + ("." + ex if ex else "")
    size = int(rec[28]) | (int(rec[29]) << 8) | (int(rec[30]) << 16) | (int(rec[31]) << 24)
    cl = ((int(rec[20]) | (int(rec[21]) << 8)) << 16) | (int(rec[26]) | (int(rec[27]) << 8))
    wd = int(rec[24]) | (int(rec[25]) << 8)
    kind = "volumelabel" if at & 0x08 else ("dir" if at & 0x10 else "file")
    return dict(offset=off, name=longname or short, shortname=short, attr=at,
                size=0 if kind != "file" else size, cluster=cl, date=d2s(wd),
                time=t2s(int(rec[22]) | (int(rec[23]) << 8)), deleted=deleted,
                kind=kind, has_lfn=bool(longname))


def parse_dir(buf, base):
    out, i, pending = [], 0, []
    n = len(buf) // 32
    while i < n:
        rec = buf[i * 32:(i + 1) * 32]
        if len(rec) < 32 or rec[0] == 0x00:
            break
        at = rec[11]
        if at == 0x0F:
            pending.append(rec) if (rec[12] == 0 and rec[26] == 0 and rec[27] == 0) else pending.clear()
            i += 1
            continue
        if at > 0x3F:
            break
        if int(rec[0]) != 0x2E:
            if not valid_sfn11(rec) or not valid_date(rec):
                break
            if not pending and int(rec[0]) == 0xE5:
                break                      # 无长名佐证的孤立删除项: 视为越界噪声
        out.append(emit(rec, base + i * 32, pending))
        pending = []
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("img")
    ap.add_argument("--out", default="fat_out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    size = os.path.getsize(a.img)
    f = open(a.img, "rb", buffering=0)

    marks, tail, tail_off, off = [], b"", 0, 0
    while True:
        buf = f.read(CHUNK)
        if not buf:
            break
        data = tail + buf
        base, p = tail_off, 0
        while True:
            j = data.find(MARK, p)
            if j < 0:
                break
            if (base + j) % 32 == 0:
                marks.append(base + j)
            p = j + 1
        tail = data[-(len(MARK) - 1):]
        tail_off = base + len(data) - len(tail)
        off += len(buf)
        print(f"  ... 锚点扫描 {off/1024**3:.2f} GiB, 命中 {len(marks)}", flush=True)
    print(f"目录锚点总数: {len(marks)}")

    dirs, total = {}, 0
    for mk in marks:
        f.seek(mk)
        b = f.read(min(WIN, size - mk))
        if len(b) < 32:
            continue
        ents = parse_dir(b, mk)
        if not ents:
            continue
        own = ((int(b[20]) | (int(b[21]) << 8)) << 16) | (int(b[26]) | (int(b[27]) << 8))
        par = None
        if len(b) >= 64:
            par = ((int(b[52]) | (int(b[53]) << 8)) << 16) | (int(b[58]) | (int(b[59]) << 8))
        dirs[mk] = dict(offset=mk, own_cluster=own, parent_cluster=par, entries=ents)
        total += len(ents)
    print(f"解析出目录 {len(dirs)} 个, 条目 {total} 条")

    clus2name = {}
    for d in dirs.values():
        for e in d["entries"]:
            if e["name"] not in (".", "..") and e["kind"] == "dir" and e["cluster"] > 0:
                clus2name.setdefault(e["cluster"], e["name"])
    for d in dirs.values():
        d["name"] = clus2name.get(d["own_cluster"], "")
        d["parent_name"] = clus2name.get(d["parent_cluster"], "") if d["parent_cluster"] else ""

    path = os.path.join(a.out, "fat_dirs.json")
    json.dump({str(k): v for k, v in dirs.items()}, open(path, "w", encoding="utf-8"),
              ensure_ascii=False)
    print("已写入", path)


if __name__ == "__main__":
    main()
