# -*- coding: utf-8 -*-
import os, glob

base = r'E:\gx\new_docs\逻辑资料V1\AI智能体相关法律、准则、底稿、案例、报告模版【20260703】\7、案例'
# 找到案例目录
case = None
for d in os.listdir(base):
    if '3999' in d:
        case = os.path.join(base, d)
print('case dir:', case)
mat = None
for d in os.listdir(case):
    if '拆分' in d:
        mat = os.path.join(case, d)
print('material dir:', mat)
print('--- files ---')
for f in sorted(os.listdir(mat)):
    print(repr(f))
