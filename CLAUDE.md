# 开发规范（每次会话开始自动读取）

## 上下文恢复
每次新会话开始，必须先执行：
1. 读取 README.md 了解项目背景和设计约束
2. 读取 README.md 中的开发进度表，确认当前完成到哪个模块
3. 执行 `git log --oneline -5` 确认最新提交状态
4. 告诉我当前进度摘要，等待我确认后再继续

## Git 规范
每个模块完成后必须执行：
```bash
git add <文件>
git commit -m "feat(<模块名>): <一句话描述>"
git push origin master
```
更新 README.md 进度后：
```bash
git add README.md
git commit -m "docs: 更新开发进度，<模块名> 完成"
git push origin origin master
```

## 模块完成标准
每个模块必须包含：
- 功能实现
- 文件底部的 `if __name__ == "__main__"` 自测，使用真实示例数据
- README.md 进度表更新：状态、自测结果、commit 信息（版本号）

## 关键约束（不可违反）
- LLM 只在初始化阶段调用，不在逐行匹配中调用
- 商品名匹配禁止使用 Embedding
- 奶底为空时视为通配符
- 匹配失败填最佳猜测并标注 LOW_CONFIDENCE
- Excel 写入使用 openpyxl，保留原始格式