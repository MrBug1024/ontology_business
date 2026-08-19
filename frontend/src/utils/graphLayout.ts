import type { GraphData, GraphNode } from '@/types'

export interface LayoutNode extends GraphNode {
  x: number
  y: number
  vx: number
  vy: number
  fx?: number | null
  fy?: number | null
  /** schema 模式下节点矩形宽度（由标签长度推算），instance 模式为 0 */
  w: number
}

export interface LayoutResult {
  nodes: LayoutNode[]
  width: number
  height: number
}

/**
 * 轻量力导向布局（Fruchterman-Reingold 风格）。
 * 同步执行固定迭代次数，返回稳定坐标。
 */
export function forceLayout(
  data: GraphData,
  opts: { width?: number; height?: number; iterations?: number } = {},
): LayoutResult {
  const width = opts.width || 1200
  const height = opts.height || 700
  const iterations = opts.iterations || 300
  const nodes: LayoutNode[] = data.nodes.map((n, i) => {
    const angle = (i / Math.max(1, data.nodes.length)) * Math.PI * 2
    const r = Math.min(width, height) * 0.28
    return {
      ...n,
      x: width / 2 + Math.cos(angle) * r + (Math.random() - 0.5) * 40,
      y: height / 2 + Math.sin(angle) * r + (Math.random() - 0.5) * 40,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null,
      w: 0,
    }
  })
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const edges = data.edges.filter((e) => byId.has(e.source) && byId.has(e.target))

  const area = width * height
  const k = Math.sqrt(area / Math.max(1, nodes.length)) * 0.85
  let temp = width / 8

  for (let iter = 0; iter < iterations; iter++) {
    // 斥力
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]
        const b = nodes[j]
        let dx = a.x - b.x
        let dy = a.y - b.y
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01
        const minDist = (a.size || 20) + (b.size || 20) + 26
        if (dist < minDist) dist = minDist * 0.5
        const force = (k * k) / dist
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        a.vx += fx
        a.vy += fy
        b.vx -= fx
        b.vy -= fy
      }
    }
    // 弹簧引力
    for (const e of edges) {
      const a = byId.get(e.source)!
      const b = byId.get(e.target)!
      let dx = a.x - b.x
      let dy = a.y - b.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01
      const force = (dist * dist) / k / 2.2
      const fx = (dx / dist) * force
      const fy = (dy / dist) * force
      a.vx -= fx
      a.vy -= fy
      b.vx += fx
      b.vy += fy
    }
    // 向中心轻微聚合
    for (const n of nodes) {
      n.vx += (width / 2 - n.x) * 0.012
      n.vy += (height / 2 - n.y) * 0.012
    }
    // 应用位移（受温度限制）
    for (const n of nodes) {
      if (n.fx != null) {
        n.x = n.fx
        n.vx = 0
      } else {
        const disp = Math.sqrt(n.vx * n.vx + n.vy * n.vy) || 0.01
        const step = Math.min(disp, temp)
        n.x += (n.vx / disp) * step
      }
      if (n.fy != null) {
        n.y = n.fy
        n.vy = 0
      } else {
        const disp = Math.sqrt(n.vx * n.vx + n.vy * n.vy) || 0.01
        const step = Math.min(disp, temp)
        n.y += (n.vy / disp) * step
      }
      // 边界
      const pad = (n.size || 20) + 14
      n.x = Math.max(pad, Math.min(width - pad, n.x))
      n.y = Math.max(pad, Math.min(height - pad, n.y))
    }
    temp *= 0.985
  }

  // 最终归一化：把节点簇平移到画布中心，避免力导向收敛后偏在角落
  if (nodes.length) {
    let cx = 0
    let cy = 0
    for (const n of nodes) {
      cx += n.x
      cy += n.y
    }
    cx /= nodes.length
    cy /= nodes.length
    for (const n of nodes) {
      n.x += width / 2 - cx
      n.y += height / 2 - cy
    }
  }

  return { nodes, width, height }
}

/** 计算两点间贝塞尔控制点（用于弯曲边，避免重叠）。 */
export function edgePath(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  curve = 0.18,
): { path: string; mx: number; my: number } {
  const dx = x2 - x1
  const dy = y2 - y1
  const cx = (x1 + x2) / 2 - dy * curve
  const cy = (y1 + y2) / 2 + dx * curve
  const path = `M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`
  // 贝塞尔中点
  const mx = 0.25 * x1 + 0.5 * cx + 0.25 * x2
  const my = 0.25 * y1 + 0.5 * cy + 0.25 * y2
  return { path, mx, my }
}
