#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建 mobile/www：拷贝前端并注入服务器地址（手机 App 用）"""
import os, re, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT = os.path.join(ROOT, "frontend")
WWW = os.path.join(ROOT, "mobile", "www")

# 默认服务器地址（可 --api 覆盖）
API = "http://47.94.251.143:8080"
if len(sys.argv) > 2 and sys.argv[1] == "--api":
    API = sys.argv[2].rstrip("/")

# 构建 www/static 结构（与后端 /static mount 对齐）：
#   www/index.html 引用相对路径 "static/..." → 本地命中 www/static/，浏览器命中服务器 /static/
if os.path.isdir(WWW):
    shutil.rmtree(WWW)
os.makedirs(os.path.join(WWW, "static"), exist_ok=True)
shutil.copy2(os.path.join(FRONT, "index.html"), os.path.join(WWW, "index.html"))
shutil.copy2(os.path.join(FRONT, "app.js"), os.path.join(WWW, "static", "app.js"))
shutil.copy2(os.path.join(FRONT, "style.css"), os.path.join(WWW, "static", "style.css"))
if os.path.isdir(os.path.join(FRONT, "vendor")):
    shutil.copytree(os.path.join(FRONT, "vendor"), os.path.join(WWW, "static", "vendor"))

# 注入 window.API_BASE
idx = os.path.join(WWW, "index.html")
s = open(idx, encoding="utf-8").read()
inject = f'<script>window.API_BASE="{API}";</script>'
if "window.API_BASE" not in s:
    s = s.replace("</head>", "  " + inject + "\n</head>", 1)
open(idx, "w", encoding="utf-8").write(s)
print(f"✅ www 构建完成，API → {API}")
print(f"   {WWW}")
