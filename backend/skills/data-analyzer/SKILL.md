---
name: data-analyzer
description: >
  数据分析技能，对 CSV/TSV 文件做描述性统计、分组聚合与简单分析。
  当用户需要统计 CSV 数据、计算均值/计数/分组汇总、查看数据分布时使用本技能。
  凡涉及"数据统计"、"CSV 分析"、"分组汇总"、"描述性统计"，均应使用本技能。
metadata:
  capability:
    id: analyze-tabular-data
    responsibility: 对表格数据做描述性统计与分组聚合。
---

# 数据分析技能

对 CSV/TSV 文件做描述性统计与分组聚合。

## 使用方式

```bash
python scripts/analyze.py --path "/workspace/data/sales.csv" --group-by "region" --metric "amount"
```

### 参数说明

| 参数 | 说明 |
|------|------|
| `--path <path>` | CSV/TSV 文件路径（必需） |
| `--group-by <col>` | 可选，按该列分组 |
| `--metric <col>` | 可选，对数值列做聚合（sum/mean/count） |
| `--format text\|json` | 输出格式，默认 json |

### 输出

- 无 `--group-by`：输出各数值列的描述性统计（count/mean/min/max/sum）。
- 有 `--group-by`：输出按该列分组的 `--metric` 聚合结果。
