# -*- coding: utf-8 -*-
import os
import xlrd

root = r'E:\gx\new_docs\逻辑资料V1\AI智能体相关法律、准则、底稿、案例、报告模版【20260703】'
case = [x for x in os.listdir(os.path.join(root, '7、案例')) if '3999' in x][0]
mat = os.path.join(root, '7、案例', case)
for dd in os.listdir(mat):
    if '拆分' in dd:
        mat = os.path.join(mat, dd)

wb = xlrd.open_workbook(os.path.join(mat, '财务报表', '2、经审计的财务报表-（会计准则）.xls'))

def dump(name, maxr=60):
    sh = wb.sheet_by_name(name)
    print(f'== {name} ==')
    for i in range(min(sh.nrows, maxr)):
        vals = [str(sh.cell_value(i, j))[:16] for j in range(min(sh.ncols, 5))]
        line = ' | '.join(vals).rstrip(' |')
        if line.strip():
            print(f'{i:3d}', line)

dump('资产负债表')
dump('资产负债表续')
dump('利润表')
