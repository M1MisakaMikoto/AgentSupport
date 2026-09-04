# -*- coding: utf-8 -*-
"""Generate the IT network abnormal-access simulation seed.

Output: seed/abnormal-access-sample.xlsx
16 rows: 8 low-risk rows to be filtered by the given rules, 8 high-priority
rows that must stay in the analysis report.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openpyxl import Workbook

HEADERS = [
    "序号",
    "时间",
    "源IP",
    "源归属",
    "目标资产",
    "目标端口",
    "事件类型",
    "行为摘要",
    "请求数",
]

ROWS = [
    # 低风险（应被口径过滤）
    (1, "2026-09-01 09:12:03", "10.0.1.23", "内网-开发网段", "10.0.8.10-10.0.8.20", "22/TCP", "端口扫描",
     "对开发网段做端口探测，未见任何成功登录", 320),
    (2, "2026-09-01 10:03:41", "66.249.79.25", "海外-Googlebot", "公开文档区", "80/TCP", "搜索引擎抓取",
     "Googlebot 抓取公开页面，UA 正常", 60),
    (3, "2026-09-01 11:27:18", "192.168.200.5", "内网-监控探针(白名单)", "全网资产", "161/UDP", "定时探测",
     "已登记的监控探针定时存活探测", 24),
    (4, "2026-09-01 12:40:55", "10.0.1.45", "内网-测试网段", "10.0.8.31", "445/TCP", "端口扫描",
     "对单台主机 445 探测，无交互数据", 12),
    (5, "2026-09-01 13:15:09", "40.77.167.19", "海外-Bingbot", "公开文档区", "80/TCP", "搜索引擎抓取",
     "Bingbot 抓取公开页面，UA 正常", 40),
    (6, "2026-09-01 14:02:44", "10.20.0.8", "内网-供应商维护白名单", "财务应用服务器", "3389/TCP", "维护窗口连接",
     "供应商在登记维护窗口内短时远程连接，操作记录正常", 1),
    (7, "2026-09-01 15:21:37", "45.83.10.201", "海外-未知", "门户网站根路径", "443/TCP", "URL探测",
     "仅命中 404 页面，未触达任何敏感路径", 150),
    (8, "2026-09-01 16:08:52", "10.0.2.99", "内网-测试网段", "10.0.8.40-10.0.8.45", "53/UDP", "DNS探测",
     "DNS 版本探测，无外联与投递行为", 80),
    # 高优先级（必须保留进报告）
    (9, "2026-09-01 02:11:36", "203.0.113.7", "海外-未知", "核心业务服务器", "22/TCP", "SSH爆破",
     "连续 480 次认证失败后出现一次成功登录，持续约 3 分钟", 481),
    (10, "2026-09-01 03:45:12", "198.51.100.22", "海外-未知", "管理后台(config)", "443/TCP", "敏感路径访问",
     "/admin/config.jsp 先 401 后 200，疑似绕过", 5),
    (11, "2026-09-01 04:30:59", "10.0.1.66", "内网-疑似失陷主机", "多台服务器", "445+3389/TCP", "横向移动",
     "对内网多台服务器发起 445/3389 批量连接，使用内网账号尝试", 90),
    (12, "2026-09-01 05:58:27", "203.0.113.99", "海外-未知", "远程桌面网关", "3389/TCP", "RDP爆破",
     "持续 600 次认证失败，源IP 未命中任何白名单", 600),
    (13, "2026-09-01 07:22:08", "45.155.204.77", "海外-未知", "备份存储", "443/TCP", "敏感文件下载",
     "下载 /backup/2026q1-archive.zip（约 2.1GB），时长与大小异常", 1),
    (14, "2026-09-01 08:47:53", "91.108.56.20", "海外-未知", "OA系统", "443/TCP", "异常登录",
     "非白名单地区海外 IP 成功登录 OA，登录后 30 分钟内高频读通讯录", 23),
    (15, "2026-09-01 09:59:21", "10.0.3.12", "内网-研发", "堡垒机", "22/TCP", "弱口令尝试",
     "对堡垒机使用常见弱口令字典尝试，含 root/admin 账号", 40),
    (16, "2026-09-01 11:33:47", "185.220.101.34", "海外-未知", "内容管理后台", "443/TCP", "Webshell特征",
     "上传 /upload/shell.jsp（伪装为图片），随后访问可疑参数路径", 2),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "seed")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "异常访问记录"
    sheet.append(HEADERS)
    for row in ROWS:
        sheet.append(list(row))
    target = args.out / "abnormal-access-sample.xlsx"
    workbook.save(target)
    print(f"wrote {target} ({len(ROWS)} rows)")


if __name__ == "__main__":
    main()
