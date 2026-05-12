# QQPhotos

批量下载 QQ 群相册（群相册）里的所有照片，自动分相册保存，支持断点续传。

## 前置条件

- Chrome 浏览器已登录 QQ 空间（脚本直接读 Chrome Cookie，无需手动导出）
- Python 3.10+

```bash
pip3 install requests browser-cookie3 tqdm   # 必须
pip3 install Pillow                           # 可选：优先用 EXIF 拍摄时间命名文件
```

## 使用方法

确保 Chrome 中已登录 QQ 空间（`h5.qzone.qq.com`），然后运行：

```bash
python3 download.py <群号>
python3 download.py <群号> -o /path/to/output   # 指定保存目录
python3 download.py <群号> -j 4                 # 调整并发线程数（默认 8）
python3 download.py <群号> -f                   # 忽略缓存强制刷新相册列表
```

照片按 `输出目录/相册名/照片文件名` 结构保存。

**示例：**

```bash
python3 download.py 940758815
python3 download.py 940758815 -o ~/Pictures/幼儿园照片
```

## 注意事项

- 87 个相册 / 7000+ 张原图约占 10–30 GB，下载前确认磁盘空间
- 已存在的文件会跳过，可随时中断后重新运行续传
- Cookie 有效期约数天，过期后重新在 Chrome 登录即可
