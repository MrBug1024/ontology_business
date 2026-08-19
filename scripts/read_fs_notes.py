# -*- coding: utf-8 -*-
import os
import xlrd
import docx

root = r'E:\gx\new_docs\逻辑资料V1\AI智能体相关法律、准则、底稿、案例、报告模版【20260703】'
case = [x for x in os.listdir(os.path.join(root, '7、案例')) if '3999' in x][0]
mat = os.path.join(root, '7、案例', case)
for dd in os.listdir(mat):
    if '拆分' in dd:
        mat = os.path.join(mat, dd)

# 1. 经审计的财务报表.xls
print('=== 经审计的财务报表.xls ===')
fs_path = os.path.join(mat, '财务报表', '2、经审计的财务报表-（会计准则）.xls')
wb = xlrd.open_workbook(fs_path)
print('sheets:', wb.sheet_names())
for sh in wb.sheets():
    print(f'-- {sh.name} ({sh.nrows}x{sh.ncols})')
    for i in range(min(sh.nrows, 14)):
        vals = [str(sh.cell_value(i, j))[:13] for j in range(min(sh.ncols, 7))]
        print('   ', ' | '.join(vals))

# 2. 附注模版 docx（8、审计报告模版/一般企业附注.docx）
print()
print('=== 一般企业附注.docx（前 60 段）===')
d = docx.Document(os.path.join(root, '8、审计报告模版', '一般企业附注.docx'))
n = 0
for para in d.paragraphs:
    t = para.text.strip()
    if t:
        print(t[:100])
        n += 1
        if n >= 60:
            break
