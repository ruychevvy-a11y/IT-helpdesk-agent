---
doc_id: shared_drive
title: 打开共享盘
visible_roles: [employee, manager, it_admin]
category: 文件共享
---

## 问题
- 共享盘怎么打开
- 怎么把共享盘映射成 Z 盘
- 在家里打不开共享盘

## 环境
- 共享盘地址 \\fileserver\share，按部门分文件夹，另有「公共」文件夹
- 只能在公司内网访问

## 解决步骤
1. 在资源管理器地址栏输入 \\fileserver\share 后回车。
2. 要固定成盘符：右键「此电脑」，选「映射网络驱动器」，填入上面的地址。
3. Mac：在访达按 Command + K，输入 smb://fileserver/share。
4. 在公司外面，先连 VPN 再打开。

## 相关
- 《VPN 连不上》
- 《共享盘文件夹没有访问权限》
