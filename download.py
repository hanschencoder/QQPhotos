#!/usr/bin/env python3
"""QQ Zone 群相册批量下载工具"""

import argparse
import re
import sys
import time
import json
import requests
import browser_cookie3
from pathlib import Path
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKERS = 5

PROXY_BASE = "https://h5.qzone.qq.com/proxy/domain/u.photo.qzone.qq.com/cgi-bin/upp"


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
        print(f"  已获取 {len(albums)} 个相册...", end="\r")
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
    """去掉缩略图尺寸后缀，得到原图 URL"""
    burl = burl.replace("http://", "https://")
    return re.sub(r'/\d+(\?.*)?$', '', burl)


def photo_filename(photo: dict, index: int) -> str:
    """用 lloc（照片唯一 ID）作为文件名，回退到序号"""
    lloc = photo.get("lloc") or photo.get("id") or ""
    # lloc 可能含特殊字符，做简单清理
    lloc = re.sub(r'[\\/:*?"<>|]', '_', lloc)
    ext = "gif" if photo.get("burl", "").lower().endswith(".gif") else "jpg"
    return f"{lloc}.{ext}" if lloc else f"{index+1:04d}.{ext}"


def sanitize_dir(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "未命名相册"


def download_one(session: requests.Session, url: str, dest: Path, headers: dict) -> tuple[bool, str]:
    """返回 (成功与否, 相对路径)"""
    if dest.exists() and dest.stat().st_size > 0:
        return True, str(dest)
    try:
        resp = session.get(url, headers=headers, timeout=60, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        return True, str(dest)
    except Exception as e:
        return False, f"{dest} ({e})"


# ── 主流程 ────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QQ 群相册批量下载工具")
    p.add_argument("group_id", help="QQ 群号，例如 940758815")
    p.add_argument("-o", "--output", default="./photos", help="照片保存目录（默认 ./photos）")
    return p.parse_args()


def main():
    args = parse_args()
    group_id = args.group_id
    save_dir = Path(args.output)
    headers = make_headers(group_id)

    print("正在从 Chrome 读取 QQ 登录 Cookie...")
    cookies = load_cookies()
    gtk = g_tk(cookies.get("p_skey", cookies.get("skey", "")))
    uin = cookies.get("p_uin", cookies.get("uin", "")).lstrip("o")
    print(f"已登录 QQ：{uin}")

    session = requests.Session()
    session.cookies.update(cookies)

    print(f"\n正在获取群 {group_id} 的相册列表...")
    albums = fetch_all_albums(session, group_id, uin, gtk, headers)
    if not albums:
        print("未找到相册，请检查群号或确认 Chrome 已登录 QQ 空间")
        sys.exit(1)
    print(f"共 {len(albums)} 个相册\n")

    # 按相册组织目录，收集所有下载任务
    # 结构：{save_dir}/{相册名}/{照片文件名}
    tasks: list[tuple[str, Path]] = []
    for album in albums:
        album_id   = album.get("id", "")
        album_name = sanitize_dir(album.get("title", album_id))
        photo_cnt  = album.get("photocnt", 0)
        print(f"  [{album_name}] {photo_cnt} 张 → 正在获取照片列表...", end="\r")

        photos = fetch_all_photos(session, group_id, album_id, uin, gtk, headers)
        print(f"  [{album_name}] {len(photos)} 张                    ")

        album_dir = save_dir / album_name
        for i, photo in enumerate(photos):
            burl = photo.get("burl", "")
            if not burl or photo.get("videoflag"):
                continue
            url  = original_url(burl)
            dest = album_dir / photo_filename(photo, i)
            tasks.append((url, dest))

    total = len(tasks)
    print(f"\n共 {total} 张照片，保存到 {save_dir.resolve()}\n")

    ok = fail = skip = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        future_to_dest = {
            pool.submit(download_one, session, url, dest, headers): dest
            for url, dest in tasks
        }
        with tqdm(total=total, unit="张", dynamic_ncols=True) as bar:
            for fut in as_completed(future_to_dest):
                dest = future_to_dest[fut]
                success, info = fut.result()

                if success:
                    # 文件已存在时 info == str(dest)，大小 > 0 说明是跳过
                    if dest.exists() and dest.stat().st_size > 0 and not info.startswith(str(dest) + " ("):
                        skip += 1
                        bar.set_description(f"跳过 {dest.name}")
                    else:
                        ok += 1
                        # 显示刚下完的相对路径
                        rel = dest.relative_to(save_dir)
                        tqdm.write(f"  ✓ {rel}")
                        bar.set_description(f"下载 {dest.name}")
                else:
                    fail += 1
                    tqdm.write(f"  ✗ {info}")

                bar.update(1)
                bar.set_postfix(完成=ok, 跳过=skip, 失败=fail)

    print(f"\n完成：下载 {ok} 张，跳过 {skip} 张，失败 {fail} 张")
    print(f"保存位置：{save_dir.resolve()}")


if __name__ == "__main__":
    main()
