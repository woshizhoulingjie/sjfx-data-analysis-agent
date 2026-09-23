# 导出中心前端集成测试

用 jsdom 加载**真实的** `templates/index.html` 导出中心区块与**真实的**
`static/export-center.js`，通过真实 HTTP 打到本机运行中的服务，覆盖
「智能目录树 → 勾选/排除 → 提交 → 结果卡 → 导出记录」整条链路。

两个 tab 各一个用例：

| 文件 | 覆盖 |
| --- | --- |
| `test_export_center_large.js` | 大数据包导出：主题/子目录/文件三级勾选、按节点与按文件的排除、载荷四要素、结果卡、导出记录、tab 切换 |
| `test_export_center_ordinary.js` | 普通文件导出：`/api/tree` 智能目录、目录+文件混合选择、`/api/export` 载荷、结果卡与记录来源名 |

## 运行

需要 jsdom（未加入项目依赖，装在 `.tmp/domtest`）：

```bash
NODE=$(ls -d ~/.vscode-server/cli/servers/*/server/node | head -1)
cd .tmp/domtest
"$NODE" ../../tests/export_center/test_export_center_large.js
"$NODE" ../../tests/export_center/test_export_center_ordinary.js
```

前置条件：`app.py` / `worker.py` / `large_package_worker.py` 已启动，
且存在至少一个 `catalog_ready` 的大数据包（用例里默认取
`lp-605781becd3f404580f9e56f24f4ee5b`，可按需修改文件顶部常量）。

## 注意

测试会**真实创建导出任务**并在 `outputs/` 留下压缩包、在导出记录里留下条目。
跑完请按需清理，或把 `task_topic` 换成可识别的测试标记再跑。
