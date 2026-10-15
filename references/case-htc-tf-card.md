# 案例复盘：HTC 手机 TF 卡（7.39 GiB）

全过程与本 skill 的脚本一一对应，可作为新案的参照基线。

## 0. 画像

- 镜像：`android_expand_part2.img`，7.39 GiB，等同整盘镜像 `usb_backup .dd` 的 GPT 分区 2。
- GPT 分区 2 名为 `android_expand`，分区 1 为 `android_meta`（16 MiB，全 0，无密钥）。
- 结论：Android 6.0 adoptable storage，dm-crypt 加密，ext4/f2fs。
  密钥 `expand_a5f322fd-8508-090f-c9c5-a9e11f22ee70.key` 随 10 年前的手机一起丢失。

## 1. 探查（probe_image.py 对应步骤）

- 开头约 103 MiB 为满熵（零字节占比 1/256），即 dm-crypt 密文；
  更精细的扫描显示加密区不止开头，而是"高熵海洋 + 明文孤岛"。
- 其余约 7.28 GiB 为明文旧 FAT32 数据区，大量条目被删除（`0xE5`）。
- 候选弱密钥（全零/全 FF/default_password 及其哈希、GUID 各形态，CBC-ESSIV 与 XTS）全部未命中。

## 2. 目录项重建（fat_dirs.py）

- 锚点法得到 1012 个目录簇、9188 条目录项（8395 文件 / 793 子目录），840 个不同目录路径。
- 已删除条目 8566 条，标称总大小约 6.5 GiB。
- 主要线索目录：`tencent/mobileqq`、`weibo`、`com.cooliris.media/cache`、`ime/theme`、
  `SinaAppMarket`、`com.htc.lucy`、`drawable-mdpi`。
- 簇号 → 偏移映射拟合失败（最佳 1/48 支持），确认簇链已断。

## 3. 雕刻（carve.py）

- 首批严格 JPEG 雕刻即得到 12 张完整原图（2368×4224，EXIF 显示 HTC M8Sw）。
- 全量雕刻：1902 个文件 / 2.27 GiB。其中相机原图 80 张（199 MB，2017 年 31 张、2018 年 49 张）、
  其他 JPEG 853、PNG 204、GIF/WEBP/BMP 若干、视频 5 个（2.1 GB）、SQLite 2 个、
  ZIP 条目 757 个（从 APK 内部按 local header 提取，保留包内相对路径如 `res/...`、`META-INF/...`）。
- 最大视频 1950 MB，box 链完整：`ftyp → moov(0.3MB, faststart) → free → mdat(1949.9MB)`，可直接播放。

## 4. 原名还原（match_restore.py）

- 用精确文件大小配对：89 个 ≥512 KB 的雕刻文件中 **48 个唯一命中真实文件名，0 例歧义**。
- 命中示例：`01_0001`(2.54 MB) → `IMAG0947.jpg`（2017-04-27）；
  `01_0006`(2.12 MB) → `IMAG1110.jpg`（2017-08-06）。均为 HTC 相机 `IMAGxxxx.jpg` 命名。
- EXIF 拍摄时间与目录项日期**逐张吻合**，构成交叉验证。
- 51 个文件（46 高置信 + 5 中）落在同一目录簇，父目录块已被覆写 → 目录名不可考，
  按 HTC 惯例标注为 `DCIM/100HTC`，CSV 中"目录名来源"列标为推断，未伪装成实测。
- 34 个无匹配文件按 `IMG_20171010_170309.jpg` 规范命名归入 `_未匹配/`。
- 抽查：`IMAG0947.jpg` MD5 与源文件一致，PIL 解码 2368×4224 正常。

## 5. 旁证挖掘（值得复用的一招）

全盘搜索 `/mnt/sdcard/`、`/storage/` 开头的路径字符串，可挖出安卓媒体库残留记录
（含 `_data` 完整路径、title、mime）。本案例挖到 156 条，但属于 2014 年那一层
（`sina/weibo`、`DCIM/新建文件夹 (2)`、`funshion`、`iphone`、`MTXX`、`zapya`），
与 2017 年 HTC 照片不同层，未能用于配对。若目标文件属于同层，这是补齐目录名的最强线索。

## 交付边界

- 加密区（采纳为内部存储之后写入的数据）不可恢复，需明确告知用户。
- 明文区（采纳之前的 FAT32 时代数据）可恢复文件名与数据，但两者配对率受目录项存活情况限制。
