# -*- coding: utf-8 -*-
"""Generate a complex abnormal-access dataset plus ground truth for accuracy runs.

Outputs:
  accuracy/abnormal-access-complex.xlsx  (30 rows, same 9 columns as the skill expects)
  accuracy/abnormal-access-complex.ground-truth.json

Ground truth follows the *business* filter rules with full conditions:
  - crawlers only when public-only; whitelisted IPs only when doing registered
    monitoring/maintenance behavior; probes only with no successful auth/data;
    URL probes only when 404 and not touching sensitive paths.
Rows 21-30 are deliberately tricky edge cases to test whether the agent applies
every condition instead of the shorthand rule text.
"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

HEADERS = [
    "序号", "时间", "源IP", "源归属", "目标资产", "目标端口",
    "事件类型", "行为摘要", "请求数",
]

# (seq, time, ip, owner, target, port, event, behavior, requests, keep, category, tricky)
ROWS = [
    # ---- straightforward low risk (filter) ----
    (1, "2026-09-08 09:00:01", "10.0.1.23", "内网-开发网段", "10.0.8.10-10.0.8.20", "22/TCP", "端口扫描",
     "对开发网段端口探测，无任何成功认证", 320, False, "", False),
    (2, "2026-09-08 09:12:33", "66.249.79.25", "海外-Googlebot", "公开文档区", "80/TCP", "搜索引擎抓取",
     "Googlebot 抓取公开页面，UA 正常", 60, False, "", False),
    (3, "2026-09-08 10:03:02", "192.168.200.5", "内网-监控探针(白名单)", "全网资产", "161/UDP", "定时探测",
     "已登记监控探针定时存活探测", 24, False, "", False),
    (4, "2026-09-08 10:27:19", "10.20.0.8", "内网-供应商维护(白名单)", "财务应用服务器", "3389/TCP", "维护窗口连接",
     "供应商在登记维护窗口短时远程连接，操作记录正常", 1, False, "", False),
    (5, "2026-09-08 11:05:44", "40.77.167.19", "海外-Bingbot", "公开文档区", "80/TCP", "搜索引擎抓取",
     "Bingbot 抓取公开页面，UA 正常", 40, False, "", False),
    (6, "2026-09-08 11:40:08", "10.0.1.45", "内网-测试网段", "10.0.8.31", "445/TCP", "端口扫描",
     "对单台主机 445 探测，无交互数据", 12, False, "", False),
    (7, "2026-09-08 12:15:51", "10.0.2.99", "内网-测试网段", "10.0.8.40-10.0.8.45", "53/UDP", "DNS探测",
     "DNS 版本探测，无外联与投递", 80, False, "", False),
    (8, "2026-09-08 13:22:36", "45.83.10.201", "海外-未知", "门户网站根路径", "443/TCP", "URL探测",
     "仅命中 404 页面，未触达敏感路径", 150, False, "", False),
    (9, "2026-09-08 14:09:12", "10.0.5.12", "内网-测试网段", "10.0.8.60", "1433/TCP", "端口扫描",
     "SQL 端口探测，无成功登录", 30, False, "", False),
    (10, "2026-09-08 14:48:27", "66.249.79.40", "海外-Googlebot", "公开文档区", "80/TCP", "搜索引擎抓取",
     "Googlebot 抓取 robots/sitemap 与公开文档", 55, False, "", False),
    # ---- straightforward high priority (keep) ----
    (11, "2026-09-08 02:11:36", "203.0.113.7", "海外-未知", "核心业务服务器", "22/TCP", "SSH爆破",
     "480 次认证失败后成功登录一次，持续 3 分钟", 481, True, "SSH/RDP爆破", False),
    (12, "2026-09-08 05:58:27", "203.0.113.99", "海外-未知", "远程桌面网关", "3389/TCP", "RDP爆破",
     "600 次认证失败，源未命中白名单", 600, True, "SSH/RDP爆破", False),
    (13, "2026-09-08 03:45:12", "198.51.100.22", "海外-未知", "管理后台(config)", "443/TCP", "敏感路径访问",
     "/admin/config.jsp 先 401 后 200", 5, True, "敏感路径访问", False),
    (14, "2026-09-08 04:30:59", "10.0.1.66", "内网-疑似失陷主机", "多台服务器", "445+3389/TCP", "横向移动",
     "对内网多台服务器批量连接并用内网账号尝试", 90, True, "横向移动", False),
    (15, "2026-09-08 07:22:08", "45.155.204.77", "海外-未知", "备份存储", "443/TCP", "敏感文件下载",
     "下载 /backup/2026q1-archive.zip（2.1GB）", 1, True, "敏感文件下载", False),
    (16, "2026-09-08 08:47:53", "91.108.56.20", "海外-未知", "OA系统", "443/TCP", "异常登录",
     "非白名单地区海外 IP 成功登录并高频读取通讯录", 23, True, "异常登录", False),
    (17, "2026-09-08 09:59:21", "10.0.3.12", "内网-研发", "堡垒机", "22/TCP", "弱口令尝试",
     "对堡垒机尝试常见弱口令，含 root/admin", 40, True, "弱口令/Webshell特征", False),
    (18, "2026-09-08 11:33:47", "185.220.101.34", "海外-未知", "内容管理后台", "443/TCP", "Webshell特征",
     "上传伪装图片的 shell.jsp 并访问可疑参数", 2, True, "弱口令/Webshell特征", False),
    (19, "2026-09-08 12:58:03", "185.220.102.11", "海外-未知", "核心业务服务器", "22/TCP", "SSH爆破",
     "300 次认证失败后短暂成功登录", 301, True, "SSH/RDP爆破", False),
    (20, "2026-09-08 13:47:29", "91.108.57.1", "海外-未知", "远程桌面网关", "3389/TCP", "RDP爆破",
     "550 次认证失败", 550, True, "SSH/RDP爆破", False),
    # ---- tricky edge cases (keep; test full conditions of every rule) ----
    (21, "2026-09-08 06:10:15", "66.249.79.31", "海外-Googlebot", "管理后台(config)", "443/TCP", "敏感路径访问",
     "Googlebot UA 却访问 /admin/config.jsp 并返回 200（非公开页面）", 3, True, "敏感路径访问", True),
    (22, "2026-09-08 06:42:57", "192.168.200.5", "内网-监控探针(白名单)", "公网跳板机", "3389/TCP", "RDP爆破",
     "白名单探针 IP 却对跳板机持续 620 次 RDP 认证失败", 620, True, "SSH/RDP爆破", True),
    (23, "2026-09-08 07:03:21", "10.20.0.8", "内网-供应商维护(白名单)", "备份存储", "443/TCP", "敏感文件下载",
     "供应商 IP 在非维护窗口批量下载 /backup/2026q1-archive.zip", 18, True, "敏感文件下载", True),
    (24, "2026-09-08 07:55:44", "10.0.1.88", "内网-测试网段", "10.0.8.70", "22/TCP", "端口扫描",
     "先扫描发现 22 开放，随后出现一次成功 SSH 登录", 25, True, "异常登录", True),
    (25, "2026-09-08 09:20:33", "40.77.167.55", "海外-Bingbot", "文件下载区", "443/TCP", "敏感路径访问",
     "Bingbot UA 访问 /backup/ 目录列表并返回 200", 6, True, "敏感路径访问", True),
    (26, "2026-09-08 10:12:05", "45.83.10.215", "海外-未知", "后台登录入口", "443/TCP", "URL探测",
     "仅命中 404，但路径为 /admin/login.php（含敏感词）", 220, True, "敏感路径访问", True),
    (27, "2026-09-08 11:51:18", "192.168.200.5", "内网-监控探针(白名单)", "堡垒机", "22/TCP", "弱口令尝试",
     "白名单探针 IP 对堡垒机尝试 root/admin 弱口令", 38, True, "弱口令/Webshell特征", True),
    (28, "2026-09-08 13:31:09", "198.51.100.77", "海外-未知", "运维控制台", "443/TCP", "敏感路径访问",
     "访问 /console/ 长期 403 后突然 200（疑似绕过）", 12, True, "敏感路径访问", True),
    (29, "2026-09-08 14:52:41", "10.0.1.66", "内网-疑似失陷主机", "文件服务器+数据库", "445+1433/TCP", "横向移动",
     "横向连接文件服务器并尝试 WMI 远程执行，随后连接数据库", 44, True, "横向移动", True),
    (30, "2026-09-08 15:40:22", "185.220.101.66", "海外-未知", "应用服务器", "443/TCP", "Webshell特征",
     "上传伪装为 gif 的 .aspx 文件并执行请求", 4, True, "弱口令/Webshell特征", True),
]


def main() -> None:
    out = Path(__file__).parent / "accuracy"
    out.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "异常访问记录"
    sheet.append(HEADERS)
    ground: list[dict] = []
    for row in ROWS:
        seq, ts, ip, owner, target, port, event, behavior, reqs, keep, category, tricky = row
        sheet.append([seq, ts, ip, owner, target, port, event, behavior, reqs])
        ground.append(
            {
                "seq": seq,
                "expected": "keep" if keep else "filter",
                "category": category if keep else "low-risk",
                "tricky": tricky,
            }
        )
    workbook.save(out / "abnormal-access-complex.xlsx")
    (out / "abnormal-access-complex.ground-truth.json").write_text(
        json.dumps({"headers": HEADERS, "records": ground}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {out} ({len(ROWS)} records)")


if __name__ == "__main__":
    main()
