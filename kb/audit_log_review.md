---
doc_id: audit_log_review
title: 查 IT 助手的审计日志
visible_roles: [it_admin]
category: 安全
---

## 问题
- IT 助手的审计日志里记了哪些操作
- 怎么查某个员工最近通过 IT 助手做了什么
- 审计日志里的 deny 是什么意思

## 环境
- 适用于 IT 管理员；审计日志存在 IT 助手的业务库里，只能追加，不能修改或删除
- 日志保存不少于 6 个月

## 解决步骤
1. 按员工工号和时间范围查记录。
2. 看事件类型：login 登录、password_reset_request 申请验证码、password_reset 重置密码、tool_call 工具调用、llm_call 模型调用、approval 审批、provision 开通权限。
3. 看判定结果：allow 已执行，deny 被权限规则拒绝，error 执行出错，duplicate 重复调用被拦下，cancel 用户取消；审批另有 approve 批准、reject 驳回。
4. 同一段对话的记录，按对话编号串起来看。
5. 发现可疑操作，按《账号疑似被盗的处理》处理。

## 相关
- 《账号疑似被盗的处理》
- 《解锁前的身份核验》
