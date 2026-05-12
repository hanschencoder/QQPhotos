#!/usr/bin/env python3
"""QQ Zone 群相册批量下载工具"""

import argparse
import os
import re
import sys
import time
import json
from datetime import datetime
from threading import Lock, Event
from queue import Queue
import requests
import browser_cookie3
from pathlib import Path
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKERS = 8
ALBUM_CACHE_TTL = 3600  # 1 小时

PROXY_BASE = "https://h5.qzone.qq.com/proxy/domain/u.photo.qzone.qq.com/cgi-bin/upp"

_G = "\033[92m"   # green
_R = "\033[91m"   # red
_Y = "\033[93m"   # yellow
_C = "\033[96m"   # cyan
_B = "\033[1m"    # bold
_0 = "\033[0m"    # reset

_name_lock = Lock()
_taken: set = set()
_stop = Event()


def make_headers(group_id: str) -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/109 Mobile Safari/537.36 QQ/8.9.68",
        "Referer": f"https://h5.qzone.qq.com/groupphoto/index?inqq=1&groupId={group_id}",
    }


# ── 认证 ──────────────────────────────────────────────────────────────────────

def load_cookies() -> dict:
    cookies = {}
    for domain in [".qq.com", ".qzone.qq.com"]:
        try:
            jar = browser_cookie3.chrome(domain_name=domain)
            for c in jar:
                cookies[c.name] = c.value
        except Exception:
            pass
    if not cookies.get("uin"):
        print("错误：未能从 Chrome 读取 QQ 登录 Cookie")
        print("请确保已在 Chrome 中登录 QQ 空间，并且 Chrome 正在运行")
        sys.exit(1)
    return cookies


def g_tk(key: str) -> int:
    h = 5381
    for c in key:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


# ── API ───────────────────────────────────────────────────────────────────────

def _parse_dt(raw, fmt: str = "%Y%m%d_%H%M%S") -> str | None:
    """将 API 时间字段（时间戳或 'YYYY-MM-DD HH:MM:SS'）转为指定格式字符串"""
    if not raw:
        return None
    s = str(raw).strip()
    if s.replace(".", "").isdigit():
        try:
            return datetime.fromtimestamp(int(float(s))).strftime(fmt)
        except Exception:
            return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").strftime(fmt)
    except ValueError:
        return None


def parse_jsonp(text: str) -> dict:
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        raise ValueError(f"无法解析响应: {text[:100]}")
    return json.loads(m.group())


def api_get(session: requests.Session, endpoint: str, params: dict, headers: dict) -> dict:
    resp = session.get(f"{PROXY_BASE}/{endpoint}", params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return parse_jsonp(resp.text)


def fetch_all_albums(session: requests.Session, group_id: str, uin: str, gtk: int, headers: dict) -> list[dict]:
    albums, start, num = [], 0, 30
    while True:
        data = api_get(session, "qun_list_album_v2", {
            "qunId": group_id, "albumId": "", "uin": uin,
            "start": start, "num": num, "g_tk": gtk,
            "getCommentCnt": 0, "getMemberRole": 0, "hostUin": uin,
            "getalbum": 0, "platform": 2,
            "inCharset": "UTF-8", "outCharset": "UTF-8",
        }, headers)
        if data.get("code") != 0:
            print(f"获取相册失败: {data}")
            break
        batch = data.get("data", {}).get("album", [])
        if not batch:
            break
        albums.extend(batch)
        if len(batch) < num:
            break
        start += num
        time.sleep(0.3)
    return albums


def fetch_all_photos(session: requests.Session, group_id: str, album_id: str,
                     uin: str, gtk: int, headers: dict) -> list[dict]:
    photos, start, num = [], 0, 100
    while True:
        data = api_get(session, "qun_list_photo_v2", {
            "qunId": group_id, "albumId": album_id, "uin": uin,
            "start": start, "num": num, "g_tk": gtk,
            "getCommentCnt": 0, "getMemberRole": 0, "hostUin": uin,
            "getalbum": 0, "platform": 2,
            "inCharset": "UTF-8", "outCharset": "UTF-8",
        }, headers)
        if data.get("code") != 0:
            break
        batch = data.get("data", {}).get("photos", [])
        if not batch:
            break
        photos.extend(batch)
        if len(batch) < num:
            break
        start += num
        time.sleep(0.2)
    return photos


# ── 下载 ──────────────────────────────────────────────────────────────────────

def original_url(burl: str) -> str:
    """将缩略图尺寸替换为 /0 得到原图，保留 query 参数"""
    burl = burl.replace("http://", "https://")
    return re.sub(r'/\d+(\?|$)', r'/0\1', burl)


def photo_filename(photo: dict, index: int) -> str:
    """上传时间 + lloc 后6位（保证唯一），回退到序号"""
    ext = "gif" if photo.get("burl", "").lower().endswith(".gif") else "jpg"
    dt = _parse_dt(photo.get("uploadtime") or photo.get("time"))
    lloc = photo.get("lloc") or photo.get("id") or ""
    uid = re.sub(r'[^A-Za-z0-9]', '', lloc)[-6:] or f"{index+1:04d}"
    if dt:
        return f"{dt}_{uid}.{ext}"
    return f"{uid}.{ext}"


def sanitize_dir(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "未命名相册"


SKIP = "skip"
DONE = "done"
FAIL = "fail"


def _read_exif_dt(path: Path) -> str | None:
    """从 JPEG 读取 EXIF DateTimeOriginal，返回 'YYYYMMDD_HHMMSS' 或 None"""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(path) as img:
            exif = img._getexif()
            if not exif:
                return None
            for tag, val in exif.items():
                if TAGS.get(tag) == "DateTimeOriginal":
                    return datetime.strptime(val, "%Y:%m:%d %H:%M:%S").strftime("%Y%m%d_%H%M%S")
    except Exception:
        pass
    return None


def _alloc_path(preferred: Path) -> Path:
    """线程安全：返回 preferred（若空闲）或 preferred_1、preferred_2…"""
    with _name_lock:
        candidate = preferred
        stem, suffix = preferred.stem, preferred.suffix
        i = 0
        while candidate.exists() or candidate in _taken:
            i += 1
            candidate = preferred.parent / f"{stem}_{i}{suffix}"
        _taken.add(candidate)
        return candidate


def download_one(session: requests.Session, url: str, dest: Path,
                 save_dir: Path, headers: dict) -> tuple[str, str]:
    """返回 (SKIP/DONE/FAIL, 描述)"""
    if dest.exists() and dest.stat().st_size > 0:
        return SKIP, str(dest.relative_to(save_dir))
    tmp = dest.with_suffix(dest.suffix + ".downloading")
    final = None
    try:
        resp = session.get(url, headers=headers, timeout=60, stream=True)
        resp.raise_for_status()
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(65536):
                if _stop.is_set():
                    break
                f.write(chunk)
        if _stop.is_set():
            tmp.unlink(missing_ok=True)
            return FAIL, f"{dest.relative_to(save_dir)} (已中断)"
        exif_dt = _read_exif_dt(tmp)
        if exif_dt:
            preferred = dest.parent / f"{exif_dt}{dest.suffix}"
            if preferred.exists() and preferred.stat().st_size > 0:
                tmp.unlink(missing_ok=True)
                return SKIP, str(preferred.relative_to(save_dir))
        else:
            preferred = dest
        final = _alloc_path(preferred)
        tmp.rename(final)
        return DONE, str(final.relative_to(save_dir))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        if final is not None:
            with _name_lock:
                _taken.discard(final)
        return FAIL, f"{dest.relative_to(save_dir)} ({e})"


# ── 缓存 ─────────────────────────────────────────────────────────────────────

def _cache_path(group_id: str) -> Path:
    return Path.home() / ".cache" / "qqphotos" / f"{group_id}.json"


def _load_cache(group_id: str) -> dict | None:
    """返回完整缓存 dict（含 albums / photos），过期或不存在返回 None"""
    try:
        data = json.loads(_cache_path(group_id).read_text(encoding="utf-8"))
        age = int(time.time() - data["fetched_at"])
        if age < ALBUM_CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _save_cache(group_id: str, albums: list[dict], photos: dict, fetched_at: float) -> None:
    """增量写入缓存，保留原始 fetched_at 以便 TTL 计算正确"""
    path = _cache_path(group_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": fetched_at, "albums": albums, "photos": photos},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── 主流程 ────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QQ 群相册批量下载工具")
    p.add_argument("group_id", help="QQ 群号，例如 940758815")
    p.add_argument("-o", "--output", default="./photos", help="照片保存目录（默认 ./photos）")
    p.add_argument("-j", "--workers", type=int, default=WORKERS, help=f"并发下载线程数（默认 {WORKERS}）")
    p.add_argument("-f", "--force-refresh", action="store_true", help="忽略缓存，强制重新获取相册列表")
    return p.parse_args()


def main():
    args = parse_args()
    group_id = args.group_id
    save_dir = Path(args.output)
    headers = make_headers(group_id)

    print(f"{_Y}🔑 正在从 Chrome 读取 QQ 登录 Cookie...{_0}")
    cookies = load_cookies()
    gtk = g_tk(cookies.get("p_skey", cookies.get("skey", "")))
    uin = cookies.get("p_uin", cookies.get("uin", "")).lstrip("o")
    print(f"{_G}✅ 已登录 QQ：{_C}{_B}{uin}{_0}")

    session = requests.Session()
    session.cookies.update(cookies)

    print(f"\n{_Y}📋 正在获取群 {_C}{group_id}{_Y} 的相册列表...{_0}")
    albums = None
    cached_photos: dict = {}
    cache_fetched_at = time.time()

    if not args.force_refresh:
        cache = _load_cache(group_id)
        if cache is not None:
            albums = cache["albums"]
            cached_photos = cache.get("photos", {})
            cache_fetched_at = cache["fetched_at"]
            age = int(time.time() - cache_fetched_at)
            mins = age // 60
            print(f"{_C}💾 使用缓存（{mins} 分钟前获取），共 {_B}{len(albums)}{_0}{_C} 个相册{_0}  "
                  f"{_Y}[用 -f 强制刷新]{_0}\n")

    if albums is None:
        albums = fetch_all_albums(session, group_id, uin, gtk, headers)
        if not albums:
            print(f"{_R}❌ 未找到相册，请检查群号或确认 Chrome 已登录 QQ 空间{_0}")
            sys.exit(1)
        print(f"{_G}📁 共 {_C}{_B}{len(albums)}{_0}{_G} 个相册{_0}\n")

    tasks: list[tuple[str, Path]] = []
    for album in albums:
        album_id   = album.get("id", "")
        ctime = album.get("createtime") or album.get("ctime") or 0
        title = album.get("title", album_id)
        date_prefix = _parse_dt(ctime, "%Y-%m-%d")
        album_name = sanitize_dir(f"{date_prefix}-{title}" if date_prefix else title)

        if album_id in cached_photos:
            photos = cached_photos[album_id]
            print(f"  {_C}🖼  [{album_name}]{_0}  {len(photos)} 张  {_Y}💾{_0}")
        else:
            photos = fetch_all_photos(session, group_id, album_id, uin, gtk, headers)
            cached_photos[album_id] = photos
            _save_cache(group_id, albums, cached_photos, cache_fetched_at)
            print(f"  {_C}🖼  [{album_name}]{_0}  {len(photos)} 张")

        album_dir = save_dir / album_name
        for i, photo in enumerate(photos):
            burl = photo.get("burl", "")
            if not burl or photo.get("videoflag"):
                continue
            tasks.append((original_url(burl), album_dir / photo_filename(photo, i)))

    total = len(tasks)
    print(f"\n{_B}📦 共 {_C}{total}{_0}{_B} 张照片，保存到 {_C}{save_dir.resolve()}{_0}\n")

    def cleanup_downloading():
        leftovers = list(save_dir.rglob("*.downloading"))
        if leftovers:
            print(f"\n{_Y}🧹 正在清理 {len(leftovers)} 个未完成的临时文件...{_0}")
            for f in leftovers:
                f.unlink(missing_ok=True)

    # 每线程一个槽位 bar，显示当前正在下载的文件
    workers = args.workers
    slot_bars = []
    for i in range(workers):
        b = tqdm(total=0, bar_format="{desc}", position=i, leave=False, dynamic_ncols=True)
        b.set_description_str(f"  {_Y}⏳ [{i+1}] 等待中{_0}")
        slot_bars.append(b)
    slot_q: Queue = Queue()
    for i in range(workers):
        slot_q.put(i)

    def _do_download(url, dest):
        slot = slot_q.get()
        label = str(dest.relative_to(save_dir))
        slot_bars[slot].set_description_str(f"  {_C}📥 [{slot+1}] {label[:72]}{_0}")
        try:
            return download_one(session, url, dest, save_dir, headers)
        finally:
            slot_bars[slot].set_description_str(f"  {_Y}⏳ [{slot+1}] 等待中{_0}")
            slot_q.put(slot)

    ok = fail = skip = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_dest = {
            pool.submit(_do_download, url, dest): dest
            for url, dest in tasks
        }
        with tqdm(total=total, unit="张", position=workers,
                  dynamic_ncols=True, leave=True) as bar:
            for fut in as_completed(future_to_dest):
                dest = future_to_dest[fut]
                status, info = fut.result()

                if status == DONE:
                    ok += 1
                    tqdm.write(f"  {_G}✓{_0} {info}")
                elif status == SKIP:
                    skip += 1
                else:
                    fail += 1
                    tqdm.write(f"  {_R}✗{_0} {info}")

                bar.update(1)
                bar.set_postfix(完成=ok, 跳过=skip, 失败=fail)
        pool.shutdown(wait=False)
    except KeyboardInterrupt:
        _stop.set()
        session.close()                              # 中断正在阻塞的网络 I/O
        pool.shutdown(wait=False, cancel_futures=True)
        for b in slot_bars:
            b.close()
        time.sleep(0.3)                              # 给线程处理异常、自删 tmp 的时间
        cleanup_downloading()                        # 兜底清理残留
        print(f"\n{_Y}⚠️  已中断{_0}")
        os._exit(1)

    for b in slot_bars:
        b.close()

    print(f"\n{_G}🎉 完成：{_C}{ok}{_G} 张下载，{_0}{skip} 张跳过，{_R}{fail} 张失败{_0}")
    print(f"{_Y}📂 保存位置：{_C}{save_dir.resolve()}{_0}")


if __name__ == "__main__":
    main()
