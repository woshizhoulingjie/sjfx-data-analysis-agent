# 邮件分析功能模块拆分 —— 功能回归测试

用 jsdom 加载**真实的** `templates/mail_research.html`，再依次执行
**真实的** `static/mail-research.js` 与 `static/mail-modules.js`，通过真实 HTTP
打运行中的服务。目的是证明"拆成功能模块"是纯结构改动，没有影响任何既有功能。

覆盖 34 项断言：

- 两个脚本执行无异常、页面无 JS 错误
- 模板 77 个 id 全部能取到元素（脚本用 `byId(id).addEventListener` 无空值保护，
  少一个 id 整页就废）
- 8 个模块，任意时刻只显示一个；逐个点击导航都只显示对应模块且高亮正确
- 资料包加载链路照旧：概览 9 张卡片、邮件列表 25 行、共 33 封、时间线、
  分页「1–25 / 33」
- **模块隐藏时数据仍照常加载**（33 条通信关系、网络图、25 条会话、25 条版本）
- 邮件列表点击 → 右侧阅读栏出现标题/元信息/正文
- 通信关系点击 → 参与方写入常驻筛选栏（跨模块反馈链路保持）
- 示例按钮写入提问框
- 每个模块切换后都有内容

## 运行

```bash
NODE=$(ls -d ~/.vscode-server/cli/servers/*/server/node | head -1)
cd .tmp/domtest
"$NODE" ../../tests/mail_modules/test_mail_modules.js
```

依赖 `.tmp/domtest/node_modules/jsdom`。测试只发只读 GET；
翻译接口 `/api/mail/presentation` 被桩掉，不会触发真实翻译排队。
