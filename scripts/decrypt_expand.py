#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Android adoptable storage (android_expand) 分区 dm-crypt 解密.

算法(AOSP): Android 6.0/6.0.1 = aes-cbc-essiv:sha256, 128-bit 随机密钥;
            部分厂商/新版内核 = aes-xts-plain64(需 --cipher xts)。
密钥位置(手机内): /data/misc/vold/expand_<分区UUID>.key —— 16 字节原始二进制。

用法:
    python decrypt_expand.py <key文件> <输入镜像> <输出镜像> [--cipher cbc-essiv|xts] [--limit MiB]

key 文件支持三种格式(自动识别): 16 字节原始二进制 / 32 位 hex 文本 / 24 字符 base64。
解密后自动校验 ext4(偏移 1080: 53 EF)与 f2fs(偏移 1024: 10 20 F5 F2)魔数。

依赖: pycryptodome (pip install pycryptodome)
注意: 无密钥时不要尝试穷举 —— 128 位真随机密钥在数学上不可解, 只能从手机或备份中取。
"""
import argparse, base64, hashlib, os, struct
from Crypto.Cipher import AES

SECTOR = 512


def load_key(path):
    raw = open(path, "rb").read()
    s = raw.strip()
    if len(raw) == 16:
        return raw, "16字节原始"
    if len(raw) == 32 and all(c in b"0123456789abcdefABCDEF" for c in s):
        return bytes.fromhex(s.decode()), "hex文本"
    try:
        b = base64.b64decode(s, validate=True)
        if len(b) == 16:
            return b, "base64"
    except Exception:
        pass
    raise SystemExit(f"无法识别密钥格式: {path} ({len(raw)} 字节)")


def essiv_iv(key16, sector_no):
    salt = hashlib.sha256(key16).digest()
    return AES.new(salt, AES.MODE_ECB).encrypt(struct.pack("<Q", sector_no) + bytes(8))


def cbc_essiv_decrypt(key16, data, first_sector):
    out = bytearray(len(data))
    for s in range(len(data) // SECTOR):
        iv = essiv_iv(key16, first_sector + s)
        out[s * SECTOR:(s + 1) * SECTOR] = AES.new(key16, AES.MODE_CBC, iv).decrypt(
            data[s * SECTOR:(s + 1) * SECTOR])
    return bytes(out)


def _gf_mul_x(t):
    v = int.from_bytes(t, "little")
    carry = v >> 127
    v = (v << 1) & ((1 << 128) - 1)
    if carry:
        v ^= 0x87
    return v.to_bytes(16, "little")


def xts_decrypt(key32, data, first_sector):
    e1, e2 = AES.new(key32[:16], AES.MODE_ECB), AES.new(key32[16:], AES.MODE_ECB)
    out = bytearray(len(data))
    for s in range(len(data) // SECTOR):
        t = e2.encrypt(struct.pack("<Q", first_sector + s) + bytes(8))
        sec = data[s * SECTOR:(s + 1) * SECTOR]
        pt = bytearray()
        for j in range(SECTOR // 16):
            c = sec[j * 16:(j + 1) * 16]
            x = bytes(a ^ b for a, b in zip(c, t))
            p = e1.decrypt(x)
            pt += bytes(a ^ b for a, b in zip(p, t))
            t = _gf_mul_x(t)
        out[s * SECTOR:(s + 1) * SECTOR] = pt
    return bytes(out)


def magic_ok(buf):
    ext4 = buf[1024 + 56:1024 + 58] == b"\x53\xef"
    f2fs = buf[1024:1028] == b"\x10\x20\xf5\xf2"
    return (ext4 or f2fs), ("ext4" if ext4 else ("f2fs" if f2fs else "未知"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--cipher", default="cbc-essiv", choices=["cbc-essiv", "xts"])
    ap.add_argument("--limit", type=int, default=0, help="只解密前 N MiB(先验证密钥)")
    a = ap.parse_args()

    key, fmt = load_key(a.key)
    print(f"密钥: {key.hex()} ({fmt})   算法: {a.cipher}")
    dec = (lambda d, s: xts_decrypt(key if len(key) == 32 else key + key, d, s)) \
        if a.cipher == "xts" else (lambda d, s: cbc_essiv_decrypt(key, d, s))

    total = os.path.getsize(a.src)
    if a.limit:
        total = min(total, a.limit * 1024 * 1024)
    total -= total % SECTOR
    print(f"解密 {total/1024**3:.3f} GiB -> {a.dst}")

    CH = 16 * 1024 * 1024
    fin, fout, done, first = open(a.src, "rb"), open(a.dst, "wb"), 0, b""
    while done < total:
        n = min(CH, total - done)
        pt = dec(fin.read(n), done // SECTOR)
        if done == 0:
            first = pt[:2048]
        fout.write(pt)
        done += n
        print(f"\r  {done/1024**2:.0f}/{total/1024**2:.0f} MiB ({done*100//total}%)",
              end="", flush=True)
    fin.close()
    fout.close()
    print()

    ok, fs = magic_ok(first)
    if ok:
        print(f"校验通过: 检测到 {fs} 文件系统魔数, 密钥与算法正确")
        print(f"输出 {a.dst} 可直接挂载(Linux: mount -o ro,loop)或用 7-Zip/DiskGenius 打开")
    else:
        print("未检测到 ext4/f2fs 魔数, 请确认: 1) 密钥是否属于本卡 2) 尝试 --cipher xts")


if __name__ == "__main__":
    main()
