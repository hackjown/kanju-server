#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 app.py 里 create_user / verify_user 返回值解包问题（返回 (row, err)）"""
import re, os

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
s = open(P, encoding="utf-8").read()

old_reg = '''    try:
        u = KM.create_user(acc, pwd, invite_code=inv, nickname=nick)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    sid = KM.new_session(u["id"])'''
new_reg = '''    u, err = KM.create_user(acc, pwd, invite_code=inv, nickname=nick)
    if err or not u:
        return jsonify({"ok": False, "error": err or "注册失败"}), 400
    sid = KM.new_session(u["id"])'''

old_login = '''    u = KM.verify_user(acc, pwd)
    if not u:
        return jsonify({"ok": False, "error": "账号或密码错误"}), 401
    sid = KM.new_session(u["id"])'''
new_login = '''    u, err = KM.verify_user(acc, pwd)
    if err or not u:
        return jsonify({"ok": False, "error": err or "账号或密码错误"}), 401
    sid = KM.new_session(u["id"])'''

n = 0
for a, b in ((old_reg, new_reg), (old_login, new_login)):
    if a in s:
        s = s.replace(a, b)
        n += 1

open(P, "w", encoding="utf-8").write(s)
print("替换成功段数:", n)
print("app.py 行数:", len(s.splitlines()))
