# ATS detection UI

企业招聘官网 ATS 检测的独立前端原型。

当前页面没有连接 Python 检测模块，也不会访问互联网。页面使用仓库中已
调研的公开样本演示以下操作：

- 选择已收录企业招聘站；
- 模拟检测进度；
- 查看 ATS、置信度、证据、复核状态和浏览器需求；
- 添加一个临时自定义官网；
- 导出本次演示结果为 JSON。

## Local development

```powershell
npm install
npm run dev
```

默认地址为 `http://localhost:3000`。

## Validation

```powershell
npm run build
node --test tests/rendered-html.test.mjs
```

此页面没有后端集成计划；不得把演示结果误认为实时检测结果。
