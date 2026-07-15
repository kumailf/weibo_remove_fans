> **免责声明：本仓库仅供学习和参考，若因为使用原因导致误删粉丝，开发者不负责。**

# 微博垃圾粉丝清理工具

Windows 命令行工具，批量移除同时满足以下条件的粉丝：

- 来源为「兴趣推荐」
- 显示「回粉」（对方已关注你，你未回关）

## 环境

- Windows 10 / 11
- Google Chrome
- Python 3.10+

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

## 用法

```bash
# 首次登录（扫码，登录态保存在 .data）
python main.py login

# 全量清理
python main.py clean --confirm

# 限量清理
python main.py clean --limit 10 --confirm

# 只扫描，不移除
python main.py scan
```

扫描结果：`.data/candidates.csv`、`.data/candidates.json`  
操作日志：`.data/actions.jsonl`

常用参数：

| 参数 | 说明 |
|------|------|
| `--limit N` | 最多移除 N 人；不加则处理全部匹配候选 |
| `--max-scrolls N` | 预加载最大次数（默认 100） |
| `--min-delay` / `--max-delay` | 两次移除间隔秒数（默认 0.4–1.0） |

运行中 `Ctrl+C` 可停止；已成功移除的会从候选名单中删掉，剩余名单会保留。

## 注意

- 不要上传或分享 `.data`
- 小批量试跑后再全量，注意微博风控与频率限制
- Chrome 无法启动时，先关闭本工具打开的其它 Chrome 窗口
- 登录失效时重新执行 `python main.py login`
