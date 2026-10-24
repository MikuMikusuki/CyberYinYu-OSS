# 贡献指南

感谢参与改进 CyberYinYu。

## 开发流程

1. Fork 仓库并从 `main` 创建分支。
2. 修改代码或文档。
3. 本地至少运行：

   ```bash
   python -m compileall -q .
   python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').rglob('*.py')]"
   ```

4. 提交 PR，说明改动目的、行为变化与验证方式。

## 约束

- Python 3.10+；不引入不必要的第三方依赖。
- 不提交任何真实 QQ 号、群号、API key、token、服务器地址或聊天记录。
- 新增配置项时同步更新 `.env.example` 与 `README.md`。
- 修改提示词或记忆逻辑时，说明对隐私与安全边界的影响。
- 保持 `ai/deepseek_tools.py` 与 `ai/bailian_*.py` 的隔离，不要互相 import。

## 提交信息

使用简短的祈使句，例如：

```
fix: avoid empty reply when tool rounds are exhausted
docs: document the owner confirmation flow
```
