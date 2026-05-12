# QQPhotos
一键批量下载 QQ 群相册的所有原图，自动按相册分目录保存，支持断点续传。无需手动导出 Cookie，直接读取 Chrome 登录态。

## 功能特性
- **全量下载原图**：通过 QQ 空间 API 获取相册和照片列表，下载原始分辨率图片（非缩略图）
- **自动分目录**：按相册整理，目录名格式为 `创建日期-相册名`，如 `2024-07-11-毕业庆祝`
- **智能文件命名**：优先读取 EXIF 拍摄时间（精确到毫秒），无 EXIF 则使用上传时间，格式 `YYYYMMDD_HHMMSS[_mmm].jpg`；同名文件自动追加 `_1`、`_2` 后缀
- **断点续传**：维护 URL→文件名映射表，重新运行时主线程秒速跳过已下载文件，不重复请求
- **多线程下载**：默认 8 线程并发，终端实时显示每个线程当前下载的文件和总进度
- **相册列表缓存**：相册和照片列表缓存 1 小时，重复运行无需重新拉取 API（`-f` 可强制刷新）
- **安全中断**：Ctrl+C 立即中断网络请求，自动清理未完成的 `.downloading` 临时文件，并保存已记录的进度

## 前置条件
- Chrome 浏览器已登录 QQ 空间（脚本直接读 Chrome Cookie，无需手动导出）
- Python 3.10+

```bash
pip3 install requests browser-cookie3 tqdm   # 必须
pip3 install Pillow                           # 可选：读取 EXIF 拍摄时间命名文件
```

## 使用方法
确保 Chrome 中已登录 QQ 空间（`h5.qzone.qq.com`），然后运行：

```bash
python3 download.py <群号>
python3 download.py <群号> -o /path/to/output   # 指定保存目录（默认 ./photos）
python3 download.py <群号> -j 4                 # 调整并发线程数（默认 8）
python3 download.py <群号> -f                   # 忽略缓存强制刷新相册列表
```

**示例：**

```bash
python3 download.py 940758815
python3 download.py 940758815 -o ~/Pictures/幼儿园照片
```

**输出目录结构：**

```
~/Pictures/幼儿园照片/
├── 2024-07-11-毕业庆祝/
│   ├── 20240711_120000.jpg
│   ├── 20240711_120001_123.jpg    # 含 EXIF 毫秒
│   └── ...
├── 2024-09-01-开学第一天/
│   └── ...
├── .qqphotos_cache.json           # 相册列表缓存（自动生成）
└── .qqphotos_map.json             # URL→文件名映射，用于断点续传（自动生成）
```

## 实现原理
**认证**：用 `browser_cookie3` 直接读取 Chrome 的 `.qq.com` Cookie（含 HttpOnly 字段），无需手动操作。从 `p_skey` 字段哈希计算出 `g_tk`，作为 QQ 空间 API 的鉴权参数。

**获取相册和照片列表**：调用 `h5.qzone.qq.com` 的内部 API（`qun_list_album_v2` / `qun_list_photo_v2`），响应为 JSONP 格式（`_Callback({...})`）。照片的 `burl` 字段是缩略图地址，将末尾的尺寸参数替换为 `/0` 即可得到原图链接。

**断点续传**：每次成功下载后，将原图 URL 和实际保存路径的映射写入 `.qqphotos_map.json`。下次运行时，主线程在进入下载队列前先查表——命中且文件存在则直接跳过，整个过程无需网络请求。

**文件命名**：下载完成后尝试读取 JPEG 的 EXIF `DateTimeOriginal` 和 `SubSecTimeOriginal`（毫秒），以拍摄时间作为文件名；无 EXIF 则回退到 API 返回的上传时间。文件名由时间戳唯一确定，同名冲突时追加 `_1`、`_2` 后缀。

**多线程**：8 个工作线程共享一个 `requests.Session`，各自使用以线程 ID 命名的临时文件（`.tmp_<tid>.downloading`）下载，完成后原子 rename 到最终路径，避免并发写冲突。Ctrl+C 时通过 `threading.Event` 通知各线程停止，并调用 `session.close()` 中断正在阻塞的网络 I/O。

## 注意事项
- 大群相册（数十个相册 / 数千张原图）可能占用 10 GB 以上，下载前确认磁盘空间
- Cookie 有效期约数天，过期后重新在 Chrome 登录 QQ 空间即可
- 视频文件会自动跳过，仅下载图片
- 需要 Chrome 在运行中（`browser_cookie3` 需要读取 Chrome 的 Cookie 数据库）
