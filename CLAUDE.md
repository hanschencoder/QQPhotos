# QQPhotos

## 命令

```bash
pip3 install requests browser-cookie3 tqdm        # 必须依赖
pip3 install Pillow                                # 可选：读 EXIF 拍摄时间
python3 download.py <群号>                         # 下载到 ./photos/
python3 download.py <群号> -o /path/to/output      # 指定输出目录
python3 download.py <群号> -j 4                    # 调整线程数（默认 8）
python3 download.py <群号> -f                      # 忽略缓存强制刷新相册列表
```

## 架构

单文件脚本 `download.py`，流程：
1. `browser_cookie3` 读 Chrome 的 `.qq.com` Cookie（含 HttpOnly，无需手动导出）
2. `g_tk` 由 `p_skey` 哈希得来，用于 API 鉴权
3. 相册列表：`h5.qzone.qq.com/proxy/domain/u.photo.qzone.qq.com/cgi-bin/upp/qun_list_album_v2`
4. 照片列表：同域名下 `qun_list_photo_v2`，字段 `burl` 含原图路径（去掉末尾 `/400` 即得原图）
5. 8 线程并发下载；文件名优先用 EXIF 拍摄时间，回退到上传时间，下载中加 `.downloading` 后缀
6. 相册列表 + 照片列表缓存 1 小时，路径 `~/.cache/qqphotos/{群号}.json`，增量写入

## 约定

- `WORKERS`（默认 8）/ `ALBUM_CACHE_TTL`（默认 3600s）在脚本顶部直接修改
- API 响应是 JSONP（`_Callback({...})`），用 `parse_jsonp()` 解析
- 视频（`videoflag` 字段）跳过不下载
