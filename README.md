# TinyPNG 批量压缩工具

通过 TinyPNG Web API 批量压缩 PNG / JPG / JPEG / WebP 图片，支持并发处理和自动重试。

## 使用方法

```bash
python3 tinypng.py <文件或目录路径>
```

也可以直接运行后拖入文件/目录路径：

```bash
python3 tinypng.py
```

压缩结果输出到同级目录下的 `<原名>_<时间戳>` 文件夹中，目录结构保持不变。

## 特性

- 支持 PNG、JPG、JPEG、WebP 格式
- 5 线程并发压缩
- 失败自动重试，直到全部成功
- 保留原始目录结构
- 实时显示压缩进度和压缩率

## 依赖

- Python 3.10+
- requests

```bash
pip install requests
```

## License

MIT
