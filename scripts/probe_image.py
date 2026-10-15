#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像探查: GPT 分区表 + 熵/零字节分布 + 文件系统与文件签名统计.
用于判断: 卡是否被 Android 用作 adoptable storage / 哪里是加密区 / 哪里还有明文数据.

用法:
    python probe_image.py <镜像> [--out 摘要.json] [--window MiB] [--max-gib N]

输出 JSON 字段:
    mbr / gpt分区列表 / 每窗口的 zero_ratio 与 entropy / 签名统计 / 结论提示
"""
import argparse, collections, json, math, os, struct

GPT_TYPES = {
    "193d1ea4-b3ca-11e4-b075-10604b889dcf": "android_expand (可合并存储/加密)",
    "19a710a2-b3ca-11e4-b026-10604b889dcf": "android_meta",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data (FAT/exFAT)",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem (ext4/f2fs)",
}

SIGS = [
    (b"\xff\xd8\xff\xe0", "jpeg_jfif"), (b"\xff\xd8\xff\xe1", "jpeg_exif"),
    (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF8", "gif"), (b"RIFF", "riff"),
    (b"ftyp", "mp4_ftyp"), (b"SQLite format 3\x00", "sqlite"),
    (b"PK\x03\x04", "zip"), (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x10\x20\xf5\xf2", "f2fs_magic"),
]


def parse_gpt(f):
    f.seek(512)
    hdr = f.read(512)
    if hdr[:8] != b"EFI PART":
        return None
    entry_lba = struct.unpack("<Q", hdr[72:80])[0]
    n, sz = struct.unpack("<II", hdr[80:88])
    f.seek(entry_lba * 512)
    out = []
    for i in range(min(n, 32)):
        e = f.read(sz if sz else 128)
        if len(e) < 128 or e[0:16] == b"\x00" * 16:
            continue
        tg = e[0:16]
        guid_str = "%08x-%04x-%04x-%s-%s" % (
            struct.unpack("<I", tg[0:4])[0], struct.unpack("<H", tg[4:6])[0],
            struct.unpack("<H", tg[6:8])[0], tg[8:10].hex(), tg[10:16].hex())
        first, last = struct.unpack("<QQ", e[32:48])
        name = e[56:128].decode("utf-16-le", "ignore").split("\x00")[0]
        out.append({"index": i + 1, "name": name, "type_guid": guid_str,
                    "type": GPT_TYPES.get(guid_str, guid_str),
                    "first_lba": first, "last_lba": last,
                    "size_bytes": (last - first + 1) * 512})
    return out


def shannon(buf, step=997):
    """采样步长取质数(997): 避免与 512 扇区/4K 块对齐造成的采样偏差,
    窗口 8 MiB 时约 8.4k 样本, 足以稳定区分密文(≈7.99)与明文/压缩数据"""
    sample = buf[::step] if len(buf) > step * 64 else buf[::max(1, step // 8)]
    if not sample:
        return 0.0
    cnt = collections.Counter(sample)
    n = len(sample)
    return -sum((c / n) * math.log2(c / n) for c in cnt.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("img")
    ap.add_argument("--out", default="probe_summary.json")
    ap.add_argument("--window", type=int, default=8, help="统计窗口 MiB")
    ap.add_argument("--max-gib", type=float, default=0, help="只扫描前 N GiB(0=全部)")
    a = ap.parse_args()

    size = os.path.getsize(a.img)
    limit = min(size, int(a.max_gib * 1024**3)) if a.max_gib else size
    win = a.window * 1024 * 1024

    f = open(a.img, "rb")
    gpt = parse_gpt(f)
    if gpt:
        print("GPT 分区:")
        for p in gpt:
            print(f"  #{p['index']} {p['name']:<16} {p['type']:<38} "
                  f"{p['size_bytes']/1024**3:7.3f} GiB  LBA {p['first_lba']}")
    else:
        print("未检测到 GPT —— 若这是单个分区的镜像(如 part2.img), "
              "请对整盘镜像(含分区表)再跑一次以查看分区布局。")

    print("\n扫描熵分布 / 签名...")
    zones, sigc = [], collections.Counter()
    prev_tail = b""
    off = 0
    while off < limit:
        buf = f.read(min(win, limit - off))
        if not buf:
            break
        data = prev_tail + buf
        for sig, name in SIGS:
            sigc[name] += data.count(sig)
        zr = buf.count(0) / len(buf)
        ent = shannon(buf)
        # 密文: 零字节占比≈1/256 且熵≈8.0; 压缩数据(如 JPEG)熵也高但零字节分布有偏
        zones.append({"offset": off, "zero_ratio": round(zr, 4), "entropy": round(ent, 2),
                      "kind": "高熵/密文" if (ent > 7.7 and zr < 0.01) else
                              ("空/填充" if zr > 0.9 else "明文数据")})
        prev_tail = data[-(max(len(s) for s, _ in SIGS) - 1):]
        off += len(buf)
    f.close()

    kind_n = collections.Counter(z["kind"] for z in zones)
    print("\n区域分布:", dict(kind_n))
    print("签名计数:", dict(sigc.most_common(8)))

    enc_head = None
    for z in zones:
        if z["kind"] != "高熵/密文":
            enc_head = z["offset"]
            break
    print(f"\n开头高熵区结束于: {enc_head/1024**2:.1f} MiB" if enc_head else "\n开头即为明文")

    summary = {"image": os.path.abspath(a.img), "size_bytes": size, "scanned_bytes": limit,
               "gpt": gpt, "zones": zones, "zone_stats": dict(kind_n),
               "signatures": dict(sigc), "first_plaintext_offset": enc_head,
               "hints": []}
    if gpt and any("android_expand" in p["type"] for p in gpt):
        summary["hints"].append("检测到 android_expand 分区: 该卡被用作 adoptable storage, "
                                "分区内容由 dm-crypt 加密, 需手机端 /data/misc/vold 密钥。")
    if kind_n.get("高熵/密文", 0) > 0:
        summary["hints"].append("存在高熵区(加密或压缩数据), 明文区仍可用雕刻恢复残留文件。")
    json.dump(summary, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n摘要已写入", a.out)
    for h in summary["hints"]:
        print("  *", h)


if __name__ == "__main__":
    main()
