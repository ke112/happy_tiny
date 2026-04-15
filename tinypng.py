#!/usr/bin/env python3
"""
TinyPNG 批量压缩工具
用法: python3 tinypng.py <文件或目录路径>
输出: 在同级目录生成 <原名>_<时间戳> 的压缩结果
失败的图片会记录到 JSON 文件并自动重试直到全部成功

大概比例:
Pass rate  : Round 1=> 78%  Round 2=> 74%  Round 3=> 83%  Round 4=> 100%  Final=> 100%
Image size : before 168.87MB  after 21.15MB  saved 147.72MB (87.5%)
Total cost : 8m57s
"""

import sys
import os
import random
import time
import json
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.util.retry import Retry

try:
    import requests
except ImportError:
    print("缺少 requests 库，正在安装...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

# 支持的图片格式
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# TinyPNG Web API
UPLOAD_URL = "https://tinypng.com/backend/opt/shrink"

# 并发数
MAX_WORKERS = 10

# 客户端节流间隔（秒）。已用随机 X-Forwarded-For 绕服务端限流，这里保留极小间隔即可，
# 真正被限速时由 compress_image 内对 429 的指数退避自适应处理。
RATE_LIMIT_INTERVAL = 0.2


def random_ip():
    """生成随机 IP 用于绕过频率限制"""
    return ".".join(str(random.randint(1, 254)) for _ in range(4))


def make_headers():
    """构造请求头，每次使用随机 IP"""
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36",
        "X-Forwarded-For": random_ip(),
        "Accept": "application/json",
    }


class RateLimiter:
    """
    自适应限流器：
    - 正常按 min_interval 放行；
    - 遇到 429 / 连接异常时，由上层调用 on_429() 触发全局冷却并拉长 interval（所有 worker 共享）；
    - 持续成功后 on_success() 缓慢恢复到 min_interval。
    目的：避免所有 worker 集体撞墙反复触发 429。
    """
    def __init__(self, min_interval: float = 0.2, max_interval: float = 4.0):
        self._min = min_interval
        self._max = max_interval
        self._interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self._cooldown_until = 0.0
        self._success_streak = 0

    def acquire(self):
        while True:
            with self._lock:
                now = time.time()
                if now < self._cooldown_until:
                    wait = self._cooldown_until - now
                else:
                    wait = self._last + self._interval - now
                    if wait <= 0:
                        self._last = now
                        return
            time.sleep(max(wait, 0.01))

    def on_429(self, cooldown: float = 15.0):
        with self._lock:
            self._interval = min(self._max, max(self._interval * 2, 1.0))
            self._cooldown_until = max(self._cooldown_until, time.time() + cooldown)
            self._success_streak = 0

    def on_conn_error(self, cooldown: float = 3.0):
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.time() + cooldown)

    def on_success(self):
        with self._lock:
            if self._interval <= self._min:
                return
            self._success_streak += 1
            if self._success_streak >= 15:
                self._interval = max(self._min, self._interval * 0.7)
                self._success_streak = 0


def create_session() -> requests.Session:
    """创建带连接池和自动重连的 Session"""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=1,
        backoff_factor=1,
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS * 2,
        pool_maxsize=MAX_WORKERS * 2,
        max_retries=retry,
    )
    session.mount("https://", adapter)
    return session


def compress_image(src_path: Path, dst_path: Path, session: requests.Session, limiter: RateLimiter) -> dict:
    """
    压缩单张图片
    返回: {"src": 原路径, "dst": 输出路径, "before": 原大小, "after": 压缩后大小, "ok": bool, "error": str}
    """
    result = {
        "src": str(src_path),
        "dst": str(dst_path),
        "before": src_path.stat().st_size,
        "after": 0,
        "ok": False,
        "error": "",
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            limiter.acquire()

            with open(src_path, "rb") as f:
                resp = session.post(
                    UPLOAD_URL,
                    data=f.read(),
                    headers=make_headers(),
                    timeout=(10, 120),
                )

            if resp.status_code == 429:
                result["error"] = "频率限制 HTTP 429"
                # 触发全局冷却，所有 worker 共同降速，避免集体撞墙
                limiter.on_429(cooldown=15.0 + random.uniform(0, 5))
                continue

            if resp.status_code != 201:
                result["error"] = f"上传失败 HTTP {resp.status_code}: {resp.text[:200]}"
                if 500 <= resp.status_code < 600:
                    limiter.on_conn_error(cooldown=3.0)
                if attempt < max_retries - 1:
                    continue
                return result

            data = resp.json()
            download_url = data.get("output", {}).get("url")
            if not download_url:
                result["error"] = f"未获取到下载链接: {json.dumps(data, ensure_ascii=False)[:200]}"
                return result

            dl_resp = session.get(download_url, headers=make_headers(), timeout=(10, 120))
            if dl_resp.status_code != 200:
                result["error"] = f"下载失败 HTTP {dl_resp.status_code}"
                if attempt < max_retries - 1:
                    time.sleep(1 + random.uniform(0, 1))
                    continue
                return result

            dst_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dst_path, "wb") as f:
                f.write(dl_resp.content)

            result["after"] = dst_path.stat().st_size
            result["ok"] = True
            limiter.on_success()
            return result

        except requests.exceptions.RequestException as e:
            result["error"] = str(e)
            # 连接池 / TLS / 超时类错误：大概率是服务端主动断连的副作用，全局冷却一下再继续
            limiter.on_conn_error(cooldown=3.0 + random.uniform(0, 2))
            if attempt < max_retries - 1:
                continue

    return result


def collect_images(input_path: Path) -> list[Path]:
    """收集所有支持的图片文件"""
    if input_path.is_file():
        if input_path.suffix.lower() in SUPPORTED_EXTS:
            return [input_path]
        else:
            print(f"不支持的文件格式: {input_path.suffix}")
            return []

    images = []
    for root, _, files in os.walk(input_path):
        for fname in sorted(files):
            fpath = Path(root) / fname
            if fpath.suffix.lower() in SUPPORTED_EXTS:
                images.append(fpath)
    return images


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


def format_elapsed(seconds: float) -> str:
    """格式化耗时"""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    elif m > 0:
        return f"{m}m{s:02d}s"
    else:
        return f"{s}s"


def load_failed_list(fail_file: Path) -> list[dict]:
    """从 JSON 文件加载失败列表"""
    if fail_file.exists():
        with open(fail_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_failed_list(fail_file: Path, failed: list[dict]):
    """保存失败列表到 JSON 文件"""
    if failed:
        with open(fail_file, "w", encoding="utf-8") as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)
    elif fail_file.exists():
        fail_file.unlink()


def run_batch(tasks: list[tuple[Path, Path]], total_all: int, done_offset: int, start_time: float,
              session: requests.Session, limiter: RateLimiter):
    """
    并发压缩一批任务
    返回: (成功列表, 失败列表, total_before, total_after)
    """
    succeeded = []
    failed = []
    total_before = 0
    total_after = 0
    done_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(compress_image, src, dst, session, limiter): (src, dst)
            for src, dst in tasks
        }
        for future in as_completed(future_map):
            done_count += 1
            r = future.result()
            total_before += r["before"]
            elapsed = format_elapsed(time.time() - start_time)

            if r["ok"]:
                total_after += r["after"]
                saved = r["before"] - r["after"]
                pct = (saved / r["before"] * 100) if r["before"] > 0 else 0
                status = f"✅ -{pct:.1f}% ({format_size(r['before'])} → {format_size(r['after'])})"
                succeeded.append(r)
            else:
                total_after += r["before"]
                status = f"❌ {r['error'][:60]}  ↻ 稍后自动重试"
                failed.append(r)

            name = Path(r["src"]).name
            print(f"  [{done_offset + done_count}/{total_all}] {name}  {status}  ⏱ {elapsed}")

    return succeeded, failed, total_before, total_after


def main():
    if len(sys.argv) < 2:
        try:
            raw = input("请拖入文件或目录路径: ").strip().strip("'\"")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(0)
        if not raw:
            print("未输入路径")
            sys.exit(1)
    else:
        raw = sys.argv[1]

    input_path = Path(raw.strip()).resolve()
    if not input_path.exists():
        print(f"路径不存在: {input_path}")
        sys.exit(1)

    # 收集图片
    images = collect_images(input_path)
    if not images:
        print("未找到支持的图片文件")
        sys.exit(1)

    # 生成输出目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if input_path.is_file():
        base_name = input_path.stem
        output_dir = input_path.parent / f"{base_name}_{timestamp}"
    else:
        output_dir = input_path.parent / f"{input_path.name}_{timestamp}"

    output_dir.mkdir(parents=True, exist_ok=True)

    # 失败记录文件放在输出目录中
    fail_file = output_dir / "_failed.json"

    print(f"{'=' * 60}")
    print(f"  TinyPNG 批量压缩工具")
    print(f"{'=' * 60}")
    print(f"  输入: {input_path}")
    print(f"  输出: {output_dir}")
    print(f"  图片数量: {len(images)}")
    print(f"  并发数: {MAX_WORKERS}")
    print(f"{'=' * 60}\n")

    # 构建任务列表
    tasks = []
    for img in images:
        if input_path.is_file():
            dst = output_dir / img.name
        else:
            rel = img.relative_to(input_path)
            dst = output_dir / rel
        tasks.append((img, dst))

    total_all = len(tasks)
    grand_before = 0
    grand_after = 0
    all_success_count = 0

    start_time = time.time()

    # 全局复用 Session 与限流器，避免每轮重建 TLS 连接池
    session = create_session()
    limiter = RateLimiter(min_interval=RATE_LIMIT_INTERVAL)

    # === 第一轮压缩 ===
    print("--- 第 1 轮压缩 ---\n")
    succeeded, failed, batch_before, batch_after = run_batch(
        tasks, total_all, 0, start_time, session, limiter
    )
    print(f"\n  第 1 轮完成，总用时: {format_elapsed(time.time() - start_time)}")
    grand_before += batch_before
    grand_after += batch_after
    all_success_count += len(succeeded)

    # 保存失败记录
    save_failed_list(fail_file, failed)

    # === 自动重试失败的图片 ===
    retry_round = 1
    while failed:
        retry_round += 1
        fail_count = len(failed)
        print(f"\n--- 第 {retry_round} 轮重试 (剩余 {fail_count} 张失败) ---\n")
        time.sleep(1)

        retry_tasks = [(Path(r["src"]), Path(r["dst"])) for r in failed]
        # 重试时不累加 grand_before（这些图片已经统计过原始大小）
        # 需要从 grand_after 中扣除之前按原大小计入的部分
        prev_fail_size = sum(r["before"] for r in failed)
        grand_after -= prev_fail_size

        succeeded_retry, failed, batch_before_retry, batch_after_retry = run_batch(
            retry_tasks, total_all, all_success_count, start_time, session, limiter
        )
        grand_after += batch_after_retry
        all_success_count += len(succeeded_retry)
        print(f"\n  第 {retry_round} 轮完成，总用时: {format_elapsed(time.time() - start_time)}")

        # 更新失败记录文件
        save_failed_list(fail_file, failed)

    # === 汇总 ===
    saved_total = grand_before - grand_after
    pct_total = (saved_total / grand_before * 100) if grand_before > 0 else 0

    print(f"\n{'=' * 60}")
    print(f"  全部压缩完成!")
    print(f"  总数: {total_all}  全部成功 ✅")
    if retry_round > 1:
        print(f"  共经过 {retry_round} 轮 (含 {retry_round - 1} 轮重试)")
    print(f"  压缩前: {format_size(grand_before)}")
    print(f"  压缩后: {format_size(grand_after)}")
    print(f"  节省:   {format_size(saved_total)} ({pct_total:.1f}%)")
    print(f"  总用时: {format_elapsed(time.time() - start_time)}")
    print(f"  输出目录: {output_dir}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
