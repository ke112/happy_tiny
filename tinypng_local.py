#!/usr/bin/env python3
"""
本地图片批量压缩工具（零云端、秒级完成）
用法:
  python3 tinypng_local.py <文件或目录路径> [--resize N]
  --resize N  若图片最大边 > N 像素，先等比降采样到 N，再做编码层压缩；默认不降采样

输出: 在同级目录生成 <原名>_<时间戳>_local 的压缩结果
依赖（首次使用需 brew 安装）:
  brew install mozjpeg pngquant oxipng webp
压缩策略:
  .jpg/.jpeg  → mozjpeg (djpeg | cjpeg -quality 80 -optimize -progressive)
  .png        → pngquant --quality=65-80  再 oxipng -o 4
  .webp       → cwebp -q 80
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


# =========================
# 配置
# =========================
@dataclass(frozen=True)
class Config:
    max_workers: int = 8                   # 本地 CPU 密集，并发数约等于物理核
    jpeg_quality: int = 80
    png_quality_min: int = 65
    png_quality_max: int = 80
    oxipng_level: int = 4                  # 0-6，越大越慢但更省；4 是性价比点
    webp_quality: int = 80
    resize_max: int = 0                    # >0 时启用降采样：图片最大边超过该像素则等比缩小
    supported_exts: frozenset = frozenset({".png", ".jpg", ".jpeg", ".webp"})


CFG = Config()


# =========================
# 工具探测
# =========================
REQUIRED_TOOLS = {
    "cjpeg": "mozjpeg",        # cjpeg + djpeg 都来自 mozjpeg
    "djpeg": "mozjpeg",
    "pngquant": "pngquant",
    "oxipng": "oxipng",
    "cwebp": "webp",
}


def check_tools() -> dict:
    """返回 {tool: path or None}。缺失项打印 brew 安装提示后退出。"""
    found = {tool: shutil.which(tool) for tool in REQUIRED_TOOLS}
    missing_brew = sorted({REQUIRED_TOOLS[t] for t, p in found.items() if p is None})
    if missing_brew:
        print("缺少以下依赖，请先安装:\n")
        print(f"  brew install {' '.join(missing_brew)}\n")
        # mozjpeg 在 homebrew 里是 keg-only，提示一下
        if "mozjpeg" in missing_brew:
            print("  注: mozjpeg 是 keg-only，安装后可能需要把 "
                  "/opt/homebrew/opt/mozjpeg/bin 加入 PATH，或用 brew link --force mozjpeg")
        sys.exit(1)
    return found


# =========================
# 压缩实现（按扩展名分派）
# =========================
def _compress_jpeg(src: Path, dst: Path):
    """mozjpeg: djpeg 解码 → cjpeg 高质量有损重编码"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # djpeg 解码成 PPM 通过管道喂给 cjpeg；不走 shell 避免路径注入
    djpeg = subprocess.Popen(
        ["djpeg", "-outfile", "/dev/stdout", str(src)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    with open(dst, "wb") as out:
        cjpeg = subprocess.Popen(
            ["cjpeg", "-quality", str(CFG.jpeg_quality),
             "-optimize", "-progressive"],
            stdin=djpeg.stdout, stdout=out, stderr=subprocess.DEVNULL,
        )
    # 让 djpeg 能感知到 SIGPIPE
    djpeg.stdout.close()
    cjpeg.wait()
    djpeg.wait()
    if cjpeg.returncode != 0 or djpeg.returncode != 0:
        raise RuntimeError(f"mozjpeg pipe 失败 (djpeg={djpeg.returncode}, cjpeg={cjpeg.returncode})")


def _compress_png(src: Path, dst: Path):
    """pngquant 有损量化 → oxipng 无损二次优化"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 第一步: pngquant --quality=65-80，--force 覆盖，--strip 去元数据
    r = subprocess.run(
        ["pngquant",
         f"--quality={CFG.png_quality_min}-{CFG.png_quality_max}",
         "--strip", "--force",
         "--output", str(dst), str(src)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    # pngquant returncode=99 表示"无法达到目标质量"——退化成直接拷贝再交给 oxipng
    if r.returncode == 99:
        shutil.copy2(src, dst)
    elif r.returncode != 0:
        raise RuntimeError(f"pngquant 失败 (rc={r.returncode}): {r.stderr.decode(errors='ignore')[:200]}")

    # 第二步: oxipng 无损重压
    r2 = subprocess.run(
        ["oxipng", "-o", str(CFG.oxipng_level), "--strip", "safe", "--quiet", str(dst)],
        stderr=subprocess.PIPE,
    )
    if r2.returncode != 0:
        raise RuntimeError(f"oxipng 失败 (rc={r2.returncode}): {r2.stderr.decode(errors='ignore')[:200]}")


def _compress_webp(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["cwebp", "-quiet", "-q", str(CFG.webp_quality), str(src), "-o", str(dst)],
        stderr=subprocess.PIPE,
    )
    if r.returncode != 0:
        raise RuntimeError(f"cwebp 失败 (rc={r.returncode}): {r.stderr.decode(errors='ignore')[:200]}")


DISPATCH = {
    ".jpg":  _compress_jpeg,
    ".jpeg": _compress_jpeg,
    ".png":  _compress_png,
    ".webp": _compress_webp,
}


def _resize_to_tmp(src: Path, max_dim: int) -> tuple[Path, bool]:
    """若图片最大边 > max_dim，等比降采样写到临时文件并返回 (临时路径, True)；
    否则返回 (原路径, False)。webp/jpeg/png 统一用 Pillow 处理。"""
    try:
        from PIL import Image
    except ImportError:
        # 首次运行时自动装一次；失败就不降采样，让调用方看到原图
        os.system(f"{sys.executable} -m pip install Pillow -q")
        from PIL import Image  # type: ignore

    with Image.open(src) as im:
        w, h = im.size
        if max(w, h) <= max_dim:
            return src, False
        im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        # 每线程 + PID 避免冲突；保持原后缀让下游 mozjpeg/pngquant/cwebp 识别
        tid = threading.get_ident()
        tmp = Path(tempfile.gettempdir()) / f"_tnlocal_{os.getpid()}_{tid}_{src.name}"
        # 保存时沿用源格式（im.format 在 thumbnail 后仍为原格式）
        save_kwargs = {}
        if src.suffix.lower() in (".jpg", ".jpeg"):
            # 尺寸降采样阶段先保留较高质量，真正的有损重压交给 mozjpeg
            save_kwargs = {"quality": 95, "subsampling": "keep"}
        elif src.suffix.lower() == ".webp":
            save_kwargs = {"quality": 95}
        im.save(tmp, format=im.format, **save_kwargs)
        return tmp, True


def compress_one(src: Path, dst: Path) -> dict:
    result = {"src": str(src), "dst": str(dst),
              "before": src.stat().st_size, "after": 0, "ok": False, "error": ""}
    working_src = src
    tmp_to_clean: Path | None = None
    try:
        if CFG.resize_max > 0:
            working_src, resized = _resize_to_tmp(src, CFG.resize_max)
            if resized:
                tmp_to_clean = working_src
        DISPATCH[src.suffix.lower()](working_src, dst)
        result["after"] = dst.stat().st_size
        # 压完反而变大（常见于已经极致压缩过的图）——直接拷贝原图，保证不劣化
        # 注意：若启用了 resize，"变大"的基准应是降采样后的输入大小，但以原图为基准更稳妥，
        # 避免用户启用 resize 后得到比原图更大的文件
        if result["after"] > result["before"]:
            shutil.copy2(src, dst)
            result["after"] = dst.stat().st_size
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if tmp_to_clean is not None:
            try:
                tmp_to_clean.unlink(missing_ok=True)
            except Exception:
                pass
    return result


# =========================
# 工具函数
# =========================
def collect_images(p: Path) -> list[Path]:
    if p.is_file():
        return [p] if p.suffix.lower() in CFG.supported_exts else []
    return sorted(f for f in p.rglob("*")
                  if f.is_file() and f.suffix.lower() in CFG.supported_exts)


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def fmt_elapsed(s: float) -> str:
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def parse_args() -> tuple[Path, int]:
    """返回 (input_path, resize_max)；没给 path 时走交互式。"""
    ap = argparse.ArgumentParser(
        description="本地图片批量压缩 (mozjpeg / pngquant / oxipng / cwebp)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("path", nargs="?", help="图片文件或目录路径")
    ap.add_argument("--resize", type=int, default=0, metavar="N",
                    help="若图片最大边 > N 像素，先等比降采样到 N 再做编码压缩；0 表示不降采样")
    args = ap.parse_args()

    raw = args.path
    if not raw:
        try:
            raw = input("请拖入文件或目录路径: ").strip().strip("'\"")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(0)
        if not raw:
            print("未输入路径")
            sys.exit(1)
    p = Path(raw.strip()).resolve()
    if not p.exists():
        print(f"路径不存在: {p}")
        sys.exit(1)
    if args.resize < 0:
        print("--resize 必须 >= 0")
        sys.exit(1)
    return p, args.resize


# =========================
# 主流程
# =========================
def main():
    global CFG
    check_tools()

    input_path, resize_max = parse_args()
    if resize_max > 0:
        CFG = replace(CFG, resize_max=resize_max)

    images = collect_images(input_path)
    if not images:
        print("未找到支持的图片文件")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = input_path.stem if input_path.is_file() else input_path.name
    output_dir = input_path.parent / f"{base}_{timestamp}_local"
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = []
    for img in images:
        dst = output_dir / img.name if input_path.is_file() else output_dir / img.relative_to(input_path)
        tasks.append((img, dst))

    print("=" * 60)
    print("  本地图片批量压缩工具 (mozjpeg + pngquant + oxipng + cwebp)")
    print("=" * 60)
    print(f"  输入: {input_path}")
    print(f"  输出: {output_dir}")
    print(f"  图片数量: {len(images)}")
    print(f"  并发数: {CFG.max_workers}")
    if CFG.resize_max > 0:
        print(f"  降采样: 最大边 > {CFG.resize_max}px 时等比缩到 {CFG.resize_max}px")
    print("=" * 60 + "\n")

    start = time.time()
    total = len(tasks)
    grand_before = grand_after = 0
    failed = []
    done = 0

    with ThreadPoolExecutor(max_workers=CFG.max_workers) as pool:
        futures = {pool.submit(compress_one, src, dst): (src, dst) for src, dst in tasks}
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            grand_before += r["before"]
            elapsed = fmt_elapsed(time.time() - start)
            name = Path(r["src"]).name
            if r["ok"]:
                grand_after += r["after"]
                saved = r["before"] - r["after"]
                pct = (saved / r["before"] * 100) if r["before"] > 0 else 0
                status = f"✅ -{pct:.1f}% ({fmt_size(r['before'])} → {fmt_size(r['after'])})"
            else:
                grand_after += r["before"]  # 失败按原大小计入，便于总览
                status = f"❌ {r['error'][:100]}"
                failed.append(r)
            print(f"  [{done}/{total}] {name}  {status}  ⏱ {elapsed}")

    saved = grand_before - grand_after
    pct = (saved / grand_before * 100) if grand_before > 0 else 0
    print("\n" + "=" * 60)
    if not failed:
        print("  全部压缩完成!")
        print(f"  总数: {total}  全部成功 ✅")
    else:
        print("  压缩结束（存在失败）")
        print(f"  成功: {total - len(failed)}  失败: {len(failed)}")
        for r in failed[:10]:
            print(f"    - {r['src']}  {r['error'][:80]}")
        if len(failed) > 10:
            print(f"    ... 另 {len(failed) - 10} 个未显示")
    print(f"  压缩前: {fmt_size(grand_before)}")
    print(f"  压缩后: {fmt_size(grand_after)}")
    print(f"  节省:   {fmt_size(saved)} ({pct:.1f}%)")
    print(f"  总用时: {fmt_elapsed(time.time() - start)}")
    print(f"  输出目录: {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
