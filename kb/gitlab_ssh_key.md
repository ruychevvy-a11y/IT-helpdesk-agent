---
doc_id: gitlab_ssh_key
title: GitLab 拉代码要求输入密码
visible_roles: [employee, manager, it_admin]
category: 开发工具
---

## 问题
- git clone 公司仓库一直要输入密码
- GitLab 拉代码提示 Permission denied (publickey)
- 新电脑怎么配置 GitLab 的 SSH 密钥

## 环境
- 公司 GitLab：gitlab.example.com，研发部、IT 部入职默认开通
- 拉取代码推荐用 SSH 方式

## 解决步骤
1. 在终端运行 ssh-keygen -t ed25519，一路回车生成密钥。
2. 打开 ~/.ssh/id_ed25519.pub，复制全部内容。
3. 登录 gitlab.example.com，在「用户设置 > SSH 密钥」里粘贴并保存。
4. 运行 ssh -T git@gitlab.example.com，看到欢迎信息就配置好了。
5. 提示没有项目权限，找项目负责人把你加进项目；还没有 GitLab 账号的，按《申请系统权限》处理。

## 相关
- 《申请系统权限》
- 《更换电脑和数据迁移》
