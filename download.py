#!/usr/bin/env python3
"""QQ Zone 群相册批量下载工具"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Event, Lock, get_ident

import browser_cookie3
import requests
from tqdm import tqdm

try:
    import colorama
    colorama.init()
except ImportError:
    pass

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

def load_cookies(group_id: str = "") -> dict:
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
        login_url = f"https://h5.qzone.qq.com/groupphoto/index?inqq=1&groupId={group_id}" if group_id else "https://h5.qzone.qq.com/groupphoto/index?inqq=1"
        print(f"请用 Chrome 打开以下链接登录：{login_url}")
        sys.exit(1)
    return cookies


def g_tk(key: str) -> int:
    h = 5381
    for c in key:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


# ── API ───────────────────────────────────────────────────────────────────────

def _parse_dt(raw, fmt: str = "%Y%m%d_%H%M%S") -> str | None:
    """将 API 时间字段（Unix 时间戳或 'YYYY-MM-DD HH:MM:SS'）转为指定格式字符串"""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        return datetime.fromtimestamp(int(float(s))).strftime(fmt)
    except (ValueError, OSError, OverflowError):
        pass
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
    ext = "gif" if photo.get("burl", "").lower().endswith(".gif") else "jpg"
    dt = _parse_dt(photo.get("uploadtime") or photo.get("time"))
    if dt:
        return f"{dt}.{ext}"
    return f"{index+1:04d}.{ext}"


def sanitize_dir(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "未命名相册"


DONE = "done"
FAIL = "fail"


def _read_exif_dt(path: Path) -> str | None:
    """从 JPEG 读取 EXIF DateTimeOriginal + SubSecTimeOriginal，返回 'YYYYMMDD_HHMMSS[_mmm]' 或 None"""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(path) as img:
            exif = img._getexif()
            if not exif:
                return None
            dt_str = subsec = None
            for tag, val in exif.items():
                name = TAGS.get(tag)
                if name == "DateTimeOriginal":
                    dt_str = val
                elif name == "SubSecTimeOriginal":
                    subsec = str(val).strip()
                if dt_str and subsec:
                    break
            if not dt_str:
                return None
            base = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S").strftime("%Y%m%d_%H%M%S")
            if subsec and subsec.isdigit():
                return f"{base}_{(subsec + '000')[:3]}"
            return base
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


def _cleanup_downloading(save_dir: Path) -> None:
    leftovers = list(save_dir.rglob("*.downloading"))
    if leftovers:
        print(f"\n{_Y}🧹 正在清理 {len(leftovers)} 个未完成的临时文件...{_0}")
        for f in leftovers:
            f.unlink(missing_ok=True)


def download_one(session: requests.Session, url: str, dest: Path,
                 save_dir: Path, headers: dict) -> tuple[str, str]:
    """返回 (DONE/FAIL, 描述)；跳过逻辑由调用方的 url_map 预过滤负责"""
    tmp = dest.parent / f".tmp_{get_ident()}.downloading"
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
        preferred = dest.parent / f"{exif_dt}{dest.suffix}" if exif_dt else dest
        final = _alloc_path(preferred)
        tmp.replace(final)
        return DONE, str(final.relative_to(save_dir))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        if final is not None:
            with _name_lock:
                _taken.discard(final)
        return FAIL, f"{dest.relative_to(save_dir)} ({e})"


# ── 缓存 ─────────────────────────────────────────────────────────────────────

def _cache_path(save_dir: Path) -> Path:
    return save_dir / ".qqphotos_cache.json"


def _load_cache(save_dir: Path) -> dict | None:
    """返回完整缓存 dict（含 albums / photos），过期或不存在返回 None"""
    try:
        data = json.loads(_cache_path(save_dir).read_text(encoding="utf-8"))
        age = int(time.time() - data["fetched_at"])
        if age < ALBUM_CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _save_cache(save_dir: Path, albums: list[dict], photos: dict, fetched_at: float) -> None:
    """增量写入缓存，保留原始 fetched_at 以便 TTL 计算正确"""
    path = _cache_path(save_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"fetched_at": fetched_at, "albums": albums, "photos": photos},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        pass


def _map_path(save_dir: Path) -> Path:
    return save_dir / ".qqphotos_map.json"


def _load_map(save_dir: Path) -> dict:
    """加载 URL→已保存文件名 映射（相对路径），不存在则返回空 dict"""
    try:
        return json.loads(_map_path(save_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_map(save_dir: Path, url_map: dict) -> None:
    try:
        path = _map_path(save_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(url_map, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
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

    save_dir.mkdir(parents=True, exist_ok=True)

    print(f"{_Y}🔑 正在从 Chrome 读取 QQ 登录 Cookie...{_0}")
    cookies = load_cookies(group_id)
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
        cache = _load_cache(save_dir)
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
        album_id = album.get("id", "")
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
            _save_cache(save_dir, albums, cached_photos, cache_fetched_at)
            print(f"  {_C}🖼  [{album_name}]{_0}  {len(photos)} 张")

        album_dir = save_dir / album_name
        for i, photo in enumerate(photos):
            burl = photo.get("burl", "")
            if not burl or photo.get("videoflag"):
                continue
            tasks.append((original_url(burl), album_dir / photo_filename(photo, i)))

    # 主线程预过滤：命中 URL 映射且文件存在则立即跳过，不进线程池
    url_map = _load_map(save_dir)
    download_tasks: list[tuple[str, Path]] = []
    pre_skip = 0
    for url, dest in tasks:
        if url in url_map:
            mapped = save_dir / url_map[url]
            if mapped.exists() and mapped.stat().st_size > 0:
                pre_skip += 1
                continue
        download_tasks.append((url, dest))

    total_photos = len(tasks)
    total = len(download_tasks)
    skip = pre_skip
    if pre_skip:
        print(f"{_Y}⏭  {pre_skip} 张已有记录，直接跳过{_0}")
    print(f"\n{_B}📦 共 {_C}{total_photos}{_0}{_B} 张，需下载 {_C}{total}{_0}{_B} 张，保存到 {_C}{save_dir.resolve()}{_0}\n")

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

    ok = fail = 0
    _map_dirty = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_task = {
            pool.submit(_do_download, url, dest): (url, dest)
            for url, dest in download_tasks
        }
        with tqdm(total=total, unit="张", position=workers,
                  dynamic_ncols=True, leave=True) as bar:
            for fut in as_completed(future_to_task):
                url, dest = future_to_task[fut]
                status, info = fut.result()

                if status == DONE:
                    ok += 1
                    url_map[url] = info
                    _map_dirty += 1
                    if _map_dirty % 50 == 0:
                        _save_map(save_dir, url_map)
                    tqdm.write(f"  {_G}✓{_0} {info}")
                else:
                    fail += 1
                    tqdm.write(f"  {_R}✗{_0} {info}")

                bar.update(1)
                bar.set_postfix(完成=ok, 跳过=skip, 失败=fail)
        pool.shutdown(wait=False)
        _save_map(save_dir, url_map)
    except KeyboardInterrupt:
        _stop.set()
        session.close()                              # 中断正在阻塞的网络 I/O
        pool.shutdown(wait=False, cancel_futures=True)
        for b in slot_bars:
            b.close()
        time.sleep(0.3)                              # 给线程处理异常、自删 tmp 的时间
        _cleanup_downloading(save_dir)
        _save_map(save_dir, url_map)
        print(f"\n{_Y}⚠️  已中断{_0}")
        os._exit(1)

    for b in slot_bars:
        b.close()

    print(f"\n{_G}🎉 完成：{_C}{ok}{_G} 张下载，{_0}{skip} 张跳过，{_R}{fail} 张失败{_0}")
    print(f"{_Y}📂 保存位置：{_C}{save_dir.resolve()}{_0}")


if __name__ == "__main__":
    main()
