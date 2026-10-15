# Android Adoptable Storage（可合并存储）加密机制

## 判定依据

满足任一条件即可判定该卡被用作 adoptable storage（内部存储扩展）而非便携式存储：

1. GPT 分区表中存在名为 `android_expand` 的分区，
   类型 GUID `193D1EA4-B3CA-11E4-B075-10604B889DCF`；
   通常还有一个 16 MiB 的 `android_meta` 分区（类型 GUID `19A710A2-B3CA-11E4-B026-10604B889DCF`）。
2. 分区开头（或大段区域）呈高熵：字节频率接近均匀，零字节占比约 1/256。
3. 该分区既无 FAT/exFAT 引导扇区，也无 ext4/f2fs 魔数。

注意：`android_meta` 分区里通常全是 0，**不含密钥**；它只存放"本卡已被某手机采纳"的标记。

## 加密参数（AOSP）

| 项 | 值 |
|---|---|
| 层 | dm-crypt（块设备层，整分区） |
| Android 6.0/6.0.1 | `aes-cbc-essiv:sha256`，128-bit 密钥 |
| 部分厂商 / 高版本 | `aes-xts-plain64` |
| 密钥长度 | 16 字节（真随机，由 /dev/urandom 生成） |
| 密钥存放（手机端） | `/data/misc/vold/expand_<分区UUID>.key` |
| IV（ESSIV） | `AES_encrypt(sha256(key), sector_le64 ‖ 0)`，每扇区独立 |
| 文件系统 | ext4 或 f2fs |

分区 UUID 可从 GPT 分区条目的前 16 字节（type GUID 之外的 disk GUID）或分区名推断；
密钥文件名中的 UUID 与 `android_expand` 分区对应。

## 密钥获取途径（按可行性排序）

1. 手机可开机且已 root：`adb pull /data/misc/vold/` 整个目录。
2. 手机无法进系统但能进 TWRP recovery：`adb pull /data/misc/vold/`（recovery 通常无需解密 data）。
3. 手机变砖但 eMMC 可读：对 userdata 分区做镜像（chip-off / JTAG / 9008 模式），
   再从镜像中提取 `expand_*.key`。
4. 整机备份（TWRP 全盘备份、厂商云备份含 /data）中翻找同名文件。
5. 手机彻底丢失/损坏：**无解**。128-bit 真随机密钥不可暴力破解，也不存在弱口令/PIN 派生——
   密钥由 TEE/Keymaster 无关的随机数生成并明文存放，只在手机本地。

## 实战要点

- 先用 `--limit 64` 只解密前 64 MiB 验证密钥与算法，避免整盘 7 GiB 白跑。
- "高熵"不等于"加密"：JPEG/MP4 压缩数据同样接近 8 bit/byte。判定要同时看
  零字节占比（密文接近 1/256，压缩数据通常有明显偏差）与位置（分区开头连续整片高熵才是密文）。
- dm-crypt 只加密"被写入的扇区"。从未写过或写后未覆写的扇区保留旧明文，
  因此整盘常呈"高熵海洋 + 明文孤岛"结构——明文孤岛正是雕刻的战场。
- 解密成功后：Linux `mount -o ro,loop part2_dec.img /mnt`；Windows 用 7-Zip / DiskGenius
  直接读 ext4。此时文件名、目录树、时间戳全部完整，优于任何雕刻方案。
